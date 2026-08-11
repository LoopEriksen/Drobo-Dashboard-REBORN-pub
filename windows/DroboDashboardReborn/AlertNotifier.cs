using System;
using System.Collections.Generic;
using System.Linq;

namespace DroboDashboardReborn;

/// <summary>
/// Decides which alerts are worth interrupting someone for.
///
/// This exists inside a rule the owner set twice: the app had been intrusive,
/// and TrayIconManager still says notifications are not something it does
/// casually. On 2026-07-28 the owner narrowed rather than reversed that --
/// notifications are allowed, but ONLY for alerts, each type switchable on its
/// own, and every one off until switched on.
///
/// The distinction that makes this acceptable: a notification appears because
/// a drive failed, never because a poll completed. If this class can ever be
/// made to fire on a routine refresh, it has failed at its actual job.
///
/// Four rules, all enforced below and all tested:
///
///   1. OFF BY DEFAULT. Every category, plus a master switch. A fresh install
///      notifies about nothing at all.
///   2. NEVER ON FIRST LOAD. Opening the app with three standing alerts must
///      not produce three notifications -- you came to the app, it does not
///      need to shout about what you are already looking at. The first pass
///      seeds what is known and stays silent.
///   3. NEVER TWICE for the same alert while it stays active. The agent
///      re-sends long-running alerts on its own timer so they aren't
///      forgotten; that is right for a phone push and wrong for a desktop
///      toast every hour about a drive you already know is dead.
///   4. AGAIN IF IT COMES BACK. An alert that cleared and returned is new
///      information, so it notifies again.
///
/// Deliberately has no reference to the tray icon, WPF, or anything that can
/// display something -- it decides, the caller shows. That keeps the rules
/// testable without a UI.
/// </summary>
internal sealed class AlertNotifier
{
    /// <summary>What was active last time we looked. Populated silently on the
    /// first pass, which is what implements rule 2.</summary>
    private readonly HashSet<string> _known = new(StringComparer.Ordinal);

    private bool _seeded;

    /// <summary>One alert worth telling somebody about.</summary>
    internal readonly record struct Notice(string Key, string Category, string Severity, string Message);

    /// <summary>
    /// Categories, in the order they appear in Settings. The id is what gets
    /// persisted; the label is what the owner reads. Kept here rather than in
    /// the XAML so the list, the settings file and the matching rules cannot
    /// drift apart.
    /// </summary>
    internal static readonly (string Id, string Label, string Description)[] Categories =
    {
        ("drives",      "Drive problems",
                        "A drive has failed, is reporting errors, or is complaining."),
        ("temperature", "Temperature",
                        "The Drobo or a drive is running hotter than the warning threshold."),
        ("capacity",    "Running out of space",
                        "The array has passed its warning or critical capacity level."),
        ("battery",     "Cache battery",
                        "The write-cache battery is weak or has failed."),
        ("unreachable", "Drobo unreachable",
                        "The Drobo stopped answering."),
        ("pack",        "Disk pack health",
                        "Rebuilds, double-degraded events, and status codes changing from their healthy baseline."),
    };

    /// <summary>
    /// Which category an alert key belongs to, or "" for anything unrecognised.
    ///
    /// Unrecognised keys notify about NOTHING. That is the deliberate
    /// direction: if a future alert type is added to the agent and nobody
    /// updates this list, the failure mode is a missing notification rather
    /// than an unexpected one — quiet is the safe way to be wrong here, given
    /// what this app has already been told off for.
    /// </summary>
    internal static string CategoryFor(string alertKey)
    {
        if (string.IsNullOrEmpty(alertKey)) return "";

        // The agent's resolution notices are keyed "<original>-cleared" and
        // carry text like "Resolved: Drive in bay 3 has failed". Today they
        // never reach here -- monitor.py dispatches them without adding them to
        // its active set, so /api/alerts does not list them -- but "today they
        // cannot happen" is a poor foundation for "and if they did, we would
        // pop up ALARM: DRIVE FAILED at you about a drive you just replaced".
        // Excluded explicitly so that stays impossible rather than incidental.
        if (alertKey.EndsWith("-cleared", StringComparison.Ordinal)) return "";

        if (alertKey.StartsWith("drive-", StringComparison.Ordinal)) return "drives";
        if (alertKey.StartsWith("temp-", StringComparison.Ordinal)
            || alertKey.StartsWith("chassis-temp-", StringComparison.Ordinal)) return "temperature";
        if (alertKey.StartsWith("capacity-", StringComparison.Ordinal)) return "capacity";
        if (alertKey.StartsWith("battery-", StringComparison.Ordinal)) return "battery";
        if (alertKey == "unreachable") return "unreachable";
        if (alertKey == "double-degraded" || alertKey == "relayout"
            || alertKey.StartsWith("pack-status-", StringComparison.Ordinal)) return "pack";
        return "";
    }

    /// <summary>
    /// Given the alerts active right now, return the ones to notify about.
    ///
    /// <paramref name="enabled"/> maps category id to whether the owner wants
    /// it; a category absent from the map counts as off.
    /// </summary>
    internal IReadOnlyList<Notice> Evaluate(
        IEnumerable<(string Key, string Severity, string Message)> active,
        bool masterEnabled,
        IReadOnlyDictionary<string, bool> enabled)
    {
        var current = new HashSet<string>(StringComparer.Ordinal);
        var notices = new List<Notice>();

        foreach (var alert in active)
        {
            if (string.IsNullOrEmpty(alert.Key)) continue;
            current.Add(alert.Key);

            // Rule 3 / rule 4 in one line: already-known stays silent, and a
            // key absent from _known is either brand new or has cleared and
            // returned, which are both worth saying.
            if (_known.Contains(alert.Key)) continue;

            var category = CategoryFor(alert.Key);
            if (category.Length == 0) continue;              // unrecognised -> silent
            if (!masterEnabled) continue;
            if (!enabled.TryGetValue(category, out var on) || !on) continue;

            notices.Add(new Notice(alert.Key, category, alert.Severity, alert.Message));
        }

        // Replace wholesale rather than union: an alert that cleared drops out
        // of _known here, which is exactly what lets rule 4 fire if it returns.
        _known.Clear();
        foreach (var key in current) _known.Add(key);

        if (!_seeded)
        {
            // Rule 2. Everything above was computed so `_known` is now correct,
            // then thrown away -- the first pass after connecting is for
            // learning what is already true, not for announcing it.
            _seeded = true;
            return Array.Empty<Notice>();
        }

        return notices;
    }

    /// <summary>
    /// Forget everything and go quiet again until the next first pass.
    ///
    /// Called when switching Drobos: the alerts of the device you just left
    /// have nothing to do with the one you just opened, and carrying them over
    /// would either suppress a real notification or invent one.
    /// </summary>
    internal void Reset()
    {
        _known.Clear();
        _seeded = false;
    }

    /// <summary>Default settings: every category off. A fresh install is silent.</summary>
    internal static Dictionary<string, bool> DefaultEnabled() =>
        Categories.ToDictionary(c => c.Id, _ => false, StringComparer.Ordinal);
}
