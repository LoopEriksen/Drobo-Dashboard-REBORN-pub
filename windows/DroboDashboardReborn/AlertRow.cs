using System.Windows.Media;

namespace DroboDashboardReborn;

/// <summary>
/// One alert row. The same class covers two different lists: the Status
/// tab's alerts panel (active alerts only, as before) and the new Alerts
/// popup's "Active"/"Resolved" groups (see MainWindow.RenderAlerts) --
/// ClearedText and Active are simply unused/blank for the Status tab's
/// purposes and populated only when the row is built for the popup.
/// </summary>
public sealed class AlertRow
{
    /// <summary>Stable id from the agent (e.g. "drive-failed-bay3"), used to tell
    /// which alerts in the "all" history are the same ones still showing as active.</summary>
    public string Key { get; set; } = "";

    public string Severity { get; set; } = "info";
    public string SeverityText { get; set; } = "";
    public string Message { get; set; } = "";
    public string Since { get; set; } = "";
    public bool Active { get; set; } = true;

    /// <summary>"cleared 3m ago" -- blank for an alert that's still active.</summary>
    public string ClearedText { get; set; } = "";

    /// <summary>first_seen for an active alert, cleared_at for a resolved one.
    /// Not shown anywhere -- only used to sort newest-first within a severity band.</summary>
    public double SortTime { get; set; }

    public Brush ChipBackground { get; set; } = Brushes.Transparent;
    public Brush SeverityBrush { get; set; } = Brushes.Gray;
}
