using System;
using System.Text;
using System.Text.Json;
using static DroboDashboardReborn.JsonHelpers;

namespace DroboDashboardReborn;

/// <summary>
/// Turns the agent's /api/status JSON into a plain-text report a person can
/// actually skim -- the same fields the Status tab already renders, laid out
/// as labeled lines instead of a JSON blob. Used only by "Copy diagnostics"
/// -> "Save to a file"; the clipboard path still copies the raw JSON exactly
/// as before, since that's what someone pastes into a bug tracker that wants
/// structured data.
/// </summary>
internal static class DiagnosticsReport
{
    /// <summary>Builds the full file contents: a human-readable summary, then the
    /// raw JSON under its own heading for anyone who wants it (or whose forum
    /// thread wants exact numbers). The serial-number warning is repeated here,
    /// not just on the Tools tab, because this text can end up saved to a file
    /// and forwarded on somewhere the on-screen warning is long gone.</summary>
    public static string BuildText(string json)
    {
        var sb = new StringBuilder();
        sb.AppendLine("Drobo Dashboard REBORN -- Diagnostics");
        sb.AppendLine("Generated " + DateTime.Now.ToString("yyyy-MM-dd HH:mm"));
        sb.AppendLine();
        sb.AppendLine("This file includes your drive serial numbers, and -- when the Drobo could");
        sb.AppendLine("not be reached -- the private network addresses of this PC. Check who you");
        sb.AppendLine("send it to before sharing it anywhere public (forums, support tickets, etc).");
        sb.AppendLine();

        try
        {
            using var doc = JsonDocument.Parse(json);
            AppendSummary(sb, doc.RootElement);
        }
        catch (Exception ex)
        {
            sb.AppendLine("Could not read the agent's response as JSON (" + ex.Message + ") -- see the raw text below.");
            sb.AppendLine();
        }

        sb.AppendLine(new string('-', 72));
        sb.AppendLine("RAW JSON (for support tickets or advanced troubleshooting)");
        sb.AppendLine(new string('-', 72));
        sb.AppendLine(json);
        return sb.ToString();
    }

    private static void AppendSummary(StringBuilder sb, JsonElement root)
    {
        var device = root.GetProperty("device");
        var reachable = root.TryGetProperty("reachable", out var r) && r.ValueKind == JsonValueKind.True;

        // First, before any device fields: if the Drobo could not be reached
        // and the agent worked out why, that is the whole story and everything
        // below it is stale. A report that opens with an empty DEVICE block
        // reads as "the NAS is broken" when the answer may be that this PC is
        // on the wrong network -- and the person reading the report is often
        // not the person who generated it.
        if (root.TryGetProperty("network_hint", out var hint) && hint.ValueKind == JsonValueKind.Object)
        {
            sb.AppendLine("WHY THE DROBO COULD NOT BE REACHED");
            sb.AppendLine("  " + Str(hint, "headline", ""));
            sb.AppendLine("  " + Str(hint, "detail", ""));
            var caveat = Str(hint, "caveat", "");
            if (caveat.Length > 0) sb.AppendLine("  " + caveat);
            sb.AppendLine();
        }

        sb.AppendLine("DEVICE");
        AppendField(sb, "Name", Str(device, "name", "Drobo"));
        AppendField(sb, "Model", Str(device, "model", ""));
        AppendField(sb, "Firmware", Str(device, "firmware", "?"));
        AppendField(sb, "Address", Str(device, "ip", ""));
        AppendField(sb, "Status", Str(root, "overall_state", "unknown") + (reachable ? "" : "  (unreachable)"));
        AppendField(sb, "Uptime", FormatUptime(Long(device, "uptime_seconds")));
        // Sits with the version it is about. Only present for the builds Drobo
        // withdrew, and worth carrying into a support thread: anyone reading
        // this report otherwise has to know the 5N's release history to spot it.
        if (root.TryGetProperty("firmware_advisory", out var fw)
            && fw.ValueKind == JsonValueKind.Object
            && fw.TryGetProperty("notable", out var n) && n.ValueKind == JsonValueKind.True)
        {
            AppendField(sb, "Firmware note", Str(fw, "headline", ""));
        }
        sb.AppendLine();

        if (root.TryGetProperty("capacity", out var cap) && cap.ValueKind == JsonValueKind.Object)
        {
            sb.AppendLine("CAPACITY");
            double frac = cap.TryGetProperty("used_fraction", out var f) && f.ValueKind == JsonValueKind.Number ? f.GetDouble() : 0;
            AppendField(sb, "Used", $"{Bytes(Long(cap, "used_bytes"))}  ({frac * 100:0.0}%)");
            AppendField(sb, "Free", Bytes(Long(cap, "free_bytes")));
            AppendField(sb, "Usable", Bytes(Long(cap, "usable_bytes")));
            AppendField(sb, "Protection", Bytes(Long(cap, "protection_bytes")));
            AppendField(sb, "Raw", Bytes(Long(cap, "raw_bytes")));
            sb.AppendLine();
        }

        if (root.TryGetProperty("drives", out var drives) && drives.ValueKind == JsonValueKind.Array)
        {
            sb.AppendLine("DRIVES");
            foreach (var d in drives.EnumerateArray())
            {
                int bay = d.TryGetProperty("bay", out var b) && b.ValueKind == JsonValueKind.Number ? b.GetInt32() : 0;
                bool present = d.TryGetProperty("present", out var p) && p.ValueKind == JsonValueKind.True;
                if (!present)
                {
                    sb.AppendLine($"  Bay {bay}   empty");
                    continue;
                }

                var state = Str(d, "state", "unknown");
                var model = Str(d, "model", "disk");
                var serial = Str(d, "serial", "");
                double? tempC = d.TryGetProperty("temperature_c", out var t) && t.ValueKind == JsonValueKind.Number
                    ? t.GetDouble() : (double?)null;
                var tail = tempC.HasValue ? $"{tempC:0.0}C" : "";
                sb.AppendLine($"  Bay {bay}   {state,-8} {model}   serial {serial}   {tail}".TrimEnd());
            }
            sb.AppendLine();
        }

        if (root.TryGetProperty("event_log", out var events) && events.ValueKind == JsonValueKind.Array
            && events.GetArrayLength() > 0)
        {
            sb.AppendLine("RECENT EVENTS");
            foreach (var e in events.EnumerateArray())
                sb.AppendLine($"  {Str(e, "ts_iso", "?"),-20} {Str(e, "severity", "info"),-8} {Str(e, "message", "")}");
            sb.AppendLine();
        }

        if (root.TryGetProperty("performance", out var perf) && perf.ValueKind == JsonValueKind.Object)
        {
            sb.AppendLine("PERFORMANCE");
            AppendField(sb, "Read", Long(perf, "read_mb_per_s") + " MB/s");
            AppendField(sb, "Write", Long(perf, "write_mb_per_s") + " MB/s");
            AppendField(sb, "IOPS", Long(perf, "iops").ToString());
            sb.AppendLine();
        }
    }

    private static void AppendField(StringBuilder sb, string label, string value) =>
        sb.AppendLine($"  {label,-12}{value}");

    private static string FormatUptime(long sec)
    {
        if (sec <= 0) return "just started";
        var d = sec / 86400;
        var h = (sec % 86400) / 3600;
        var m = (sec % 3600) / 60;
        if (d > 0) return $"{d} day{(d == 1 ? "" : "s")}" + (h > 0 ? $" {h}h" : "");
        if (h > 0) return $"{h}h" + (m > 0 ? $" {m}m" : "");
        return $"{Math.Max(1, m)}m";
    }
}
