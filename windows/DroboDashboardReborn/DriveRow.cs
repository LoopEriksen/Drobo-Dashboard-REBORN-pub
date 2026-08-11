using System.Windows;
using System.Windows.Media;

namespace DroboDashboardReborn;

/// <summary>
/// One drive-bay row bound into the compact ItemsControl on the Status tab --
/// one dense line per bay rather than a tall card, so all five bays (the 5N
/// has exactly five, never six -- see esatm._trim_phantom_slots) fit without
/// scrolling at the smaller window size.
/// </summary>
public sealed class DriveRow
{
    public string BayLabel { get; set; } = "";

    /// <summary>"ACME DISK-3000A  ·  serial" -- model with internal whitespace
    /// runs collapsed (the agent's model strings have padding baked in) and
    /// serial appended, built once in MainWindow.Render so the row stays a
    /// plain binding.</summary>
    public string ModelSerial { get; set; } = "";

    public string Firmware { get; set; } = "";
    public string Size { get; set; } = "";
    public string StateText { get; set; } = "";
    public Brush StateBrush { get; set; } = Brushes.Gray;
    public Brush Stripe { get; set; } = Brushes.Gray;

    /// <summary>"3 errors" and/or "life 42%" -- only populated when there's
    /// actually something to flag (a healthy drive shows neither; see the
    /// "don't print 0 errors on every row" rule). Empty means nothing to show.</summary>
    public string Flags { get; set; } = "";
    public Brush FlagsBrush { get; set; } = Brushes.Gray;
    public Visibility FlagsVisibility { get; set; } = Visibility.Collapsed;
}
