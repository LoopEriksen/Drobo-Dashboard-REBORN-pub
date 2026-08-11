using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;

namespace DroboDashboardReborn;

/// <summary>
/// Everything this app remembers between launches: the last Drobo you
/// connected to, any addresses you typed in by hand, and whether automatic
/// network discovery is turned on -- all in one settings.json under
/// %LOCALAPPDATA%, never in the repo (same rule as the agent token, see
/// MainWindow.CandidateConfigPaths) and never a password.
///
/// Loaded and saved as one whole document (LoadAll/SaveAll) rather than
/// field-by-field, so adding a manual address can never accidentally wipe
/// out the remembered device, or vice versa.
/// </summary>
internal static class DroboSettings
{
    public sealed class RememberedDevice
    {
        /// <summary>The Drobo's permanent hardware serial. Matched on this, not
        /// the IP -- if DHCP hands the Drobo a new address, esa_id is still how
        /// we recognise it's the same one on the next /api/discover.</summary>
        public string EsaId { get; set; } = "";
        public string Host { get; set; } = "";
        public string Name { get; set; } = "";
        public string Model { get; set; } = "";
    }

    /// <summary>
    /// A Drobo address typed in by hand rather than found by mDNS -- the way in
    /// on networks that filter multicast, where browsing returns nothing however
    /// long it listens.
    ///
    /// These are checked properly now. The agent's /api/discover takes a
    /// <c>?host=</c> parameter meaning "go and look at this address": it
    /// connects, reads the device's own greeting, and reports the name, model
    /// and serial the Drobo claims for itself. So a manual entry is confirmed
    /// the moment it is added, and again on every search, rather than waiting
    /// for an automatic pass to stumble across the same host.
    ///
    /// (Until 2026-07-28 the agent had no such parameter, so this app could
    /// only ever label a typed address "not verified" and hope. The badge still
    /// exists, but it now means the address was asked directly and did not
    /// answer -- a real measurement rather than an absence of one.)
    ///
    /// The app still never opens a socket to the Drobo itself; the agent does
    /// the looking, as it does for everything else.
    /// </summary>
    public sealed class ManualDevice
    {
        public string Host { get; set; } = "";

        /// <summary>0 means "not specified" -- displayed without a port suffix
        /// rather than as port 0, which would read as a real (if useless) port.</summary>
        public int Port { get; set; }

        public string DisplayText => Port > 0 ? $"{Host}:{Port}" : Host;
    }

    public sealed class Settings
    {
        public RememberedDevice? Remembered { get; set; }
        public List<ManualDevice> ManualDevices { get; set; } = new();

        /// <summary>Default ON, per the owner's ask. Only ever OFF when the file
        /// explicitly says so -- see the presence check in LoadAll, which exists
        /// specifically so a missing key can't be misread as "off".</summary>
        public bool AutoDiscoveryEnabled { get; set; } = true;

        /// <summary>What "Copy diagnostics" on the Tools tab does without asking.
        /// Defaults to Ask -- the first click always shows the choice dialog until
        /// the owner either ticks "remember my choice" there, or sets one of the
        /// other two here.</summary>
        public DiagnosticsPreference DiagnosticsPreference { get; set; } = DiagnosticsPreference.Ask;

        /// <summary>Whether the window's close button (X) hides to the tray
        /// instead of exiting. Opt-in, default OFF -- the owner has already
        /// complained twice about this app being intrusive, so the X really
        /// closes it unless this is explicitly turned on. See TrayIconManager.</summary>
        public bool CloseToTrayEnabled { get; set; } = false;

        /// <summary>Master switch for desktop notifications. Default OFF, like
        /// every individual category below it. Allowed back in on 2026-07-28,
        /// narrowed to alerts only; see AlertNotifier for the four rules that
        /// make that acceptable rather than a reversal.</summary>
        public bool NotificationsEnabled { get; set; } = false;

        /// <summary>Per-category switches, keyed by AlertNotifier category id.
        /// A category missing from here counts as OFF -- so a category added in
        /// a later version stays silent until it is deliberately turned on,
        /// rather than surprising someone who upgraded.</summary>
        public Dictionary<string, bool> NotificationCategories { get; set; }
            = AlertNotifier.DefaultEnabled();
    }

    /// <summary>Tools tab, "Copy diagnostics": where the result goes, and whether
    /// that's decided every time or remembered.</summary>
    public enum DiagnosticsPreference { Ask, Clipboard, File }

    private static string FilePath => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "DroboDashboardReborn", "settings.json");

    /// <summary>Never throws -- a missing or corrupt file just means the same
    /// safe defaults a first-ever launch would have: nothing remembered,
    /// nothing manual, discovery on.</summary>
    public static Settings LoadAll()
    {
        var result = new Settings();
        try
        {
            var path = FilePath;
            if (!File.Exists(path)) return result;

            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            var root = doc.RootElement;

            if (root.TryGetProperty("remembered_device", out var d) && d.ValueKind == JsonValueKind.Object)
            {
                string Get(string name) =>
                    d.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String
                        ? (v.GetString() ?? "") : "";
                var esaId = Get("esa_id");
                if (!string.IsNullOrEmpty(esaId)) // nothing worth remembering without a serial to match on
                    result.Remembered = new RememberedDevice { EsaId = esaId, Host = Get("host"), Name = Get("name"), Model = Get("model") };
            }

            if (root.TryGetProperty("manual_devices", out var m) && m.ValueKind == JsonValueKind.Array)
            {
                foreach (var item in m.EnumerateArray())
                {
                    if (item.ValueKind != JsonValueKind.Object) continue;
                    var host = item.TryGetProperty("host", out var h) && h.ValueKind == JsonValueKind.String
                        ? (h.GetString() ?? "") : "";
                    if (string.IsNullOrWhiteSpace(host)) continue;
                    var port = item.TryGetProperty("port", out var p) && p.ValueKind == JsonValueKind.Number
                        ? p.GetInt32() : 0;
                    result.ManualDevices.Add(new ManualDevice { Host = host, Port = port });
                }
            }

            // Explicit presence + kind check, not a bare GetBoolean() default:
            // an ABSENT key must mean "on" (the documented default), while a
            // PRESENT "false" must be honoured. Collapsing those two cases
            // would be exactly the falsy-default trap this codebase has been
            // bitten by before -- see the SeverityRank comment in
            // MainWindow.xaml.cs for the earlier version of the same mistake.
            if (root.TryGetProperty("auto_discovery_enabled", out var a) &&
                (a.ValueKind == JsonValueKind.True || a.ValueKind == JsonValueKind.False))
            {
                result.AutoDiscoveryEnabled = a.ValueKind == JsonValueKind.True;
            }

            if (root.TryGetProperty("diagnostics_preference", out var dp) && dp.ValueKind == JsonValueKind.String)
            {
                result.DiagnosticsPreference = (dp.GetString() ?? "").ToLowerInvariant() switch
                {
                    "clipboard" => DiagnosticsPreference.Clipboard,
                    "file" => DiagnosticsPreference.File,
                    _ => DiagnosticsPreference.Ask,
                };
            }

            // Same explicit presence check as auto_discovery_enabled above --
            // an absent key must mean the documented default (OFF), not just
            // whatever GetBoolean() would fall back to.
            if (root.TryGetProperty("close_to_tray_enabled", out var ct) &&
                (ct.ValueKind == JsonValueKind.True || ct.ValueKind == JsonValueKind.False))
            {
                result.CloseToTrayEnabled = ct.ValueKind == JsonValueKind.True;
            }

            if (root.TryGetProperty("notifications_enabled", out var ne) &&
                (ne.ValueKind == JsonValueKind.True || ne.ValueKind == JsonValueKind.False))
            {
                result.NotificationsEnabled = ne.ValueKind == JsonValueKind.True;
            }

            if (root.TryGetProperty("notification_categories", out var nc)
                && nc.ValueKind == JsonValueKind.Object)
            {
                // Read onto the defaults rather than replacing them, and only
                // for ids we recognise. A category added in a later version is
                // therefore absent from an older settings file and stays OFF,
                // instead of an upgrade quietly starting to notify about
                // something nobody asked for.
                foreach (var (id, _, _) in AlertNotifier.Categories)
                {
                    if (nc.TryGetProperty(id, out var v)
                        && (v.ValueKind == JsonValueKind.True || v.ValueKind == JsonValueKind.False))
                    {
                        result.NotificationCategories[id] = v.ValueKind == JsonValueKind.True;
                    }
                }
            }
        }
        catch
        {
            // Corrupt or unreadable settings just means "start from the safe
            // defaults already set on `result`" -- never a crash.
        }
        return result;
    }

    public static void SaveAll(Settings settings)
    {
        try
        {
            var path = FilePath;
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            var json = JsonSerializer.Serialize(new
            {
                remembered_device = settings.Remembered is { } r ? new
                {
                    esa_id = r.EsaId,
                    host = r.Host,
                    name = r.Name,
                    model = r.Model,
                } : null,
                manual_devices = settings.ManualDevices.Select(m => new { host = m.Host, port = m.Port }).ToArray(),
                auto_discovery_enabled = settings.AutoDiscoveryEnabled,
                diagnostics_preference = settings.DiagnosticsPreference switch
                {
                    DiagnosticsPreference.Clipboard => "clipboard",
                    DiagnosticsPreference.File => "file",
                    _ => "ask",
                },
                close_to_tray_enabled = settings.CloseToTrayEnabled,
                notifications_enabled = settings.NotificationsEnabled,
                notification_categories = settings.NotificationCategories,
            }, new JsonSerializerOptions { WriteIndented = true });
            File.WriteAllText(path, json);
        }
        catch
        {
            // Not being able to save is not worth interrupting the flow for --
            // whatever was on screen a moment ago still works.
        }
    }

    // ---- convenience mutators: each does a full load-modify-save so no
    // caller has to worry about clobbering the other fields in the file ----

    public static void RememberDevice(RememberedDevice device)
    {
        var s = LoadAll();
        s.Remembered = device;
        SaveAll(s);
    }

    /// <summary>"Forget this Drobo" -- turns off the auto-open behaviour without
    /// making the owner go hunt down the settings file themselves.</summary>
    public static void ForgetDevice()
    {
        var s = LoadAll();
        s.Remembered = null;
        SaveAll(s);
    }

    public static void AddManualDevice(string host, int port)
    {
        var s = LoadAll();
        if (!s.ManualDevices.Any(m => string.Equals(m.Host, host, StringComparison.OrdinalIgnoreCase) && m.Port == port))
            s.ManualDevices.Add(new ManualDevice { Host = host, Port = port });
        SaveAll(s);
    }

    public static void RemoveManualDevice(string host, int port)
    {
        var s = LoadAll();
        s.ManualDevices.RemoveAll(m => string.Equals(m.Host, host, StringComparison.OrdinalIgnoreCase) && m.Port == port);
        SaveAll(s);
    }

    public static void SetAutoDiscoveryEnabled(bool enabled)
    {
        var s = LoadAll();
        s.AutoDiscoveryEnabled = enabled;
        SaveAll(s);
    }

    public static void SetDiagnosticsPreference(DiagnosticsPreference pref)
    {
        var s = LoadAll();
        s.DiagnosticsPreference = pref;
        SaveAll(s);
    }

    public static void SetCloseToTrayEnabled(bool enabled)
    {
        var s = LoadAll();
        s.CloseToTrayEnabled = enabled;
        SaveAll(s);
    }
}
