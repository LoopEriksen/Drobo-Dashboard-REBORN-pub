using System.Windows;
using System.Windows.Media;

namespace DroboDashboardReborn;

/// <summary>
/// One Drobo shown on the landing page's device-picker list -- found by the
/// agent's /api/discover (mDNS or "in use"), confirmed at a manually-added
/// address the agent was asked to check directly, or a manually-added address
/// that did not answer this pass (see DroboSettings.ManualDevice).
///
/// Host/Port are the Drobo's own address (its command port, not the agent's
/// HTTP port) -- shown for information only. This app never opens a socket
/// to the Drobo directly, so clicking a row doesn't dial this address; it
/// connects to the agent that's already configured (see
/// MainWindow.DeviceConnectButton_Click for why that's the right thing to do
/// with only one agent and one Drobo in the picture).
/// </summary>
public sealed class DiscoveredDeviceRow
{
    public string Host { get; set; } = "";
    public int Port { get; set; }
    public string Name { get; set; } = "";
    public string Model { get; set; } = "";
    public string Firmware { get; set; } = "";

    /// <summary>The Drobo's permanent hardware serial -- not a secret (the device
    /// hands it to anyone who connects), and what MainWindow remembers a device by
    /// so a later DHCP-assigned IP change doesn't break "reconnect automatically".</summary>
    public string EsaId { get; set; } = "";

    public bool IsCurrent { get; set; }
    public Visibility InUseVisibility { get; set; } = Visibility.Collapsed;

    /// <summary>"Drobo 5N  ·  firmware 4.3.1  ·  10.0.0.5" -- assembled once in
    /// MainWindow.RenderDevices so the card's DataTemplate stays a plain binding.</summary>
    public string Summary { get; set; } = "";

    /// <summary>"MANUALLY ADDED" or "NOT VERIFIED" -- blank hides the chip
    /// entirely, same convention as InUseVisibility. Colours are set by
    /// MainWindow.RenderDevices (the same pattern AlertRow already uses for
    /// severity: ChipBackground/SeverityBrush), rather than baking colour
    /// logic into this plain view-model.</summary>
    public string BadgeText { get; set; } = "";
    public Visibility BadgeVisibility { get; set; } = Visibility.Collapsed;
    public Brush BadgeBackground { get; set; } = Brushes.Transparent;
    public Brush BadgeForeground { get; set; } = Brushes.Transparent;
}
