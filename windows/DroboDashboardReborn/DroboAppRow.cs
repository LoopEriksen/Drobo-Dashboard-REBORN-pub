using System.Windows;
using System.Windows.Media;

namespace DroboDashboardReborn;

/// <summary>
/// One row of software installed ON the Drobo itself (Python, an SSH server,
/// DroboPix), bound into the ItemsControl on the Tools tab -- see
/// MainWindow.RefreshDroboApps/RenderDroboApps and GET /api/droboapps.
///
/// Running/HasWebUi are already the plain booleans a person would say out
/// loud: the agent flips the device's own inverted "Stopped" field before
/// this ever sees it (see droboapps.py's parse_droboapps), so nothing here
/// re-inverts anything or treats a false value as "missing".
/// </summary>
public sealed class DroboAppRow
{
    public string Name { get; set; } = "";
    public string Version { get; set; } = "";
    public string Description { get; set; } = "";

    /// <summary>The device's own free-text status line (e.g. "Listening on port
    /// 22."), separate from Running -- often empty, and shown only when it
    /// actually says something.</summary>
    public string Status { get; set; } = "";
    public Visibility StatusVisibility { get; set; } = Visibility.Collapsed;

    public bool Running { get; set; }
    public string RunningText { get; set; } = "STOPPED";
    public Brush RunningBrush { get; set; } = Brushes.Gray;
    public Brush RunningChipBackground { get; set; } = Brushes.Transparent;

    public bool HasWebUi { get; set; }
    public Visibility WebUiVisibility { get; set; } = Visibility.Collapsed;
}
