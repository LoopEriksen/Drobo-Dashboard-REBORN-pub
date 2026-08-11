using System.Windows;

namespace DroboDashboardReborn;

/// <summary>
/// One file share, ready for the Shares tab: what the Drobo calls it, the
/// Windows path to it, and whether Windows currently has it connected.
///
/// The connection fields (IsConnected/DriveLetter/ConnectionText) are read
/// fresh from Windows itself each time the tab refreshes -- the agent has
/// no way to know this, since it is entirely local to this PC. See
/// SmbConnections.GetActiveConnections.
/// </summary>
public sealed class ShareRow
{
    public string Name { get; set; } = "";
    public string Unc { get; set; } = "";
    public bool TimeMachine { get; set; }
    public Visibility TimeMachineVisibility { get; set; } = Visibility.Collapsed;

    public bool IsConnected { get; set; }

    /// <summary>The drive letter this share is mapped to (e.g. "Z:"), or null if it's
    /// connected without one, or not connected at all.</summary>
    public string? DriveLetter { get; set; }

    public string ConnectionText { get; set; } = "Not connected";

    /// <summary>
    /// Whether "Connect as…" can possibly succeed. False only when the agent
    /// has positively established that the Drobo has NO user accounts -- in
    /// which case the sign-in dialog is a door with nothing behind it, and
    /// offering it invites somebody to blame their own typing.
    ///
    /// Defaults to true, and stays true when the account list is merely
    /// unknown. A control disabled on a guess is worse than one that fails
    /// honestly: the failure at least says why.
    /// </summary>
    public bool CanSignIn { get; set; } = true;

    /// <summary>Tooltip for the disabled Connect-as button, so hovering explains
    /// the greying rather than leaving it a mystery. Null when enabled.</summary>
    public string? SignInBlockedReason { get; set; }
}
