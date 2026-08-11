using System;
using System.Text.Json;

namespace DroboDashboardReborn;

/// <summary>
/// Small helpers for pulling typed values out of the agent's JSON without
/// every call site having to check TryGetProperty and the value kind by
/// hand. Used by the Status tab (device/capacity/drives/alerts) and the
/// Shares tab alike -- pulled out of MainWindow.xaml.cs so both can use it
/// without that file becoming a dumping ground.
/// </summary>
internal static class JsonHelpers
{
    public static string Str(JsonElement el, string name, string fallback) =>
        el.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String
            ? (v.GetString() ?? fallback) : fallback;

    public static long Long(JsonElement el, string name) =>
        el.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetInt64() : 0;

    public static double Double(JsonElement el, string name) =>
        el.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetDouble() : 0;

    public static bool Bool(JsonElement el, string name) =>
        el.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.True;

    /// <summary>Auto-scaling byte formatter -- B/KB/MB/GB/TB/PB, same thresholds
    /// and rounding as webui.py's own `bytes()`. The old version always divided
    /// by 1e12, so anything under ~5 GB rendered as a flat "0.00 TB", which
    /// reads as "zero free space" on a near-full array when it's anything but.
    /// Zero bytes is a real reading here, not a missing one, so it prints as
    /// "0 B" rather than an em dash.</summary>
    public static string Bytes(long bytes)
    {
        double n = bytes;
        string[] units = { "B", "KB", "MB", "GB", "TB", "PB" };
        int i = 0;
        while (Math.Abs(n) >= 1000 && i < units.Length - 1) { n /= 1000; i++; }
        string text = i == 0
            ? n.ToString("0", System.Globalization.CultureInfo.InvariantCulture)
            : n.ToString(Math.Abs(n) < 10 ? "0.00" : "0.0", System.Globalization.CultureInfo.InvariantCulture);
        return $"{text} {units[i]}";
    }
}
