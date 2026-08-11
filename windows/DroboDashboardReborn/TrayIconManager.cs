using System;
using System.ComponentModel;
using System.Drawing;
using System.Windows;

namespace DroboDashboardReborn;

/// <summary>
/// Owns the notification-area (system tray) icon for the app's lifetime.
///
/// Kept entirely separate from MainWindow so the WinForms bits this needs
/// (NotifyIcon, ContextMenuStrip -- see &lt;UseWindowsForms&gt; in the csproj)
/// don't spread into the WPF window's own code. MainWindow only needs to:
///   - construct one of these once, after InitializeComponent
///   - call <see cref="UpdateStatus"/> whenever it refreshes device status
///   - call <see cref="Dispose"/> when the window actually closes
///
/// Behaviour, per the owner's ask:
///   - Tray icon present the whole time the app runs, tinted/badged the
///     same as the mark used everywhere else.
///   - Tooltip reflects the device name and overall health, refreshed from
///     data the app already polls -- nothing new is fetched here.
///   - Left-click restores/shows the window. Right-click: Open, Exit.
///   - Closing to the tray is OPT-IN (see DroboSettings.CloseToTrayEnabled,
///     default OFF) -- the owner has already complained twice about this
///     app being intrusive, so the window's X really closes it unless the
///     owner turned this on themselves.
///   - The icon reflects health: it swaps to a badged variant while a
///     critical alert is active, using alert data the app already fetches.
///
/// NOTIFICATIONS -- the rule here changed on 2026-07-28, and how it changed
/// matters. This class used to say "no toasts, ever", written after the owner
/// twice reported the app being intrusive. The owner then NARROWED that rather
/// than reversing it: notifications are allowed, but only for alerts, each type
/// switchable on its own, and every one off until switched on.
///
/// So <see cref="ShowAlert"/> exists, and it is the only thing in this class
/// that can interrupt anyone. What it will never do is decide FOR itself: it
/// shows what AlertNotifier hands it, and AlertNotifier only ever hands over a
/// newly-active alert whose category the owner switched on. A notification here
/// means something is actually wrong. Nothing about a routine poll can produce
/// one.
///
/// It still never steals focus -- a balloon tip is drawn by the shell without
/// activating the window, which is the whole reason it is the right mechanism
/// here rather than anything that raises a window.
/// </summary>
internal sealed class TrayIconManager : IDisposable
{
    private readonly Window _window;
    private readonly System.Windows.Forms.NotifyIcon _notifyIcon;
    private readonly Icon _normalIcon;
    private readonly Icon _alertIcon;

    // Set only by the tray menu's own "Exit" item, so Window_Closing can
    // tell "the owner asked to actually quit" apart from "the owner clicked
    // the X" -- those must behave differently whenever close-to-tray is on.
    private bool _exitRequested;
    private bool _disposed;

    public TrayIconManager(Window window)
    {
        _window = window;
        _normalIcon = LoadEmbeddedIcon("Assets/AppIcon.ico");
        _alertIcon = LoadEmbeddedIcon("Assets/AppIconAlert.ico");

        var menu = new System.Windows.Forms.ContextMenuStrip();
        menu.Items.Add("Open", null, (_, _) => ShowWindow());
        menu.Items.Add(new System.Windows.Forms.ToolStripSeparator());
        menu.Items.Add("Exit", null, (_, _) => ExitApp());

        _notifyIcon = new System.Windows.Forms.NotifyIcon
        {
            Icon = _normalIcon,
            Text = "Drobo Dashboard REBORN",
            ContextMenuStrip = menu,
            Visible = true,
        };
        _notifyIcon.MouseClick += (_, e) =>
        {
            if (e.Button == System.Windows.Forms.MouseButtons.Left)
                ShowWindow();
        };

        _window.Closing += Window_Closing;
    }

    /// <summary>
    /// Loads one of the two icons baked in as a WPF resource (see the
    /// &lt;Resource&gt; items in the csproj) via a pack URI, rather than a
    /// loose file on disk -- so it keeps working after the self-contained
    /// single-file publish build-installer.ps1 produces, where there is no
    /// loose Assets folder next to the .exe.
    /// </summary>
    private static Icon LoadEmbeddedIcon(string relativePath)
    {
        var uri = new Uri("pack://application:,,,/" + relativePath, UriKind.Absolute);
        var info = System.Windows.Application.GetResourceStream(uri)
            ?? throw new InvalidOperationException($"Missing embedded icon resource: {relativePath}");
        using var stream = info.Stream;
        return new Icon(stream);
    }

    /// <summary>
    /// Refreshes the tooltip and (if needed) swaps the badged icon in.
    /// Called on the same poll tick MainWindow already renders status and
    /// alerts on -- this never fetches anything itself.
    /// </summary>
    public void UpdateStatus(string deviceName, string overallState, bool hasCriticalAlert)
    {
        if (_disposed) return;

        var stateText = overallState switch
        {
            "ok" => "Healthy",
            "warning" => "Warning",
            "failed" => "Failed",
            "unreachable" => "Unreachable",
            _ => "Unknown",
        };

        // NotifyIcon.Text throws if longer than 127 chars (a native shell
        // limit) -- device names are short in practice, but truncate rather
        // than let a long one crash the next poll tick.
        var tooltip = $"{deviceName} — {stateText}";
        if (tooltip.Length > 127) tooltip = tooltip[..127];
        _notifyIcon.Text = tooltip;
        _notifyIcon.Icon = hasCriticalAlert ? _alertIcon : _normalIcon;
    }

    /// <summary>
    /// Show one desktop notification for an alert.
    ///
    /// The caller has already decided this is worth showing (AlertNotifier);
    /// this only draws it. A balloon tip is rendered by the Windows shell as an
    /// ordinary notification, appearing in the Action Center like any other --
    /// and, importantly, WITHOUT activating or raising the window, so it cannot
    /// take what you were typing into.
    ///
    /// Severity picks the icon only. The text is the agent's own alert message,
    /// unedited: it was written to be read by a person and rewording it here
    /// would mean two different descriptions of the same fault depending on
    /// where you saw it.
    /// </summary>
    public void ShowAlert(string severity, string message)
    {
        if (_disposed || string.IsNullOrWhiteSpace(message)) return;

        var icon = severity switch
        {
            "critical" => System.Windows.Forms.ToolTipIcon.Error,
            "warning" => System.Windows.Forms.ToolTipIcon.Warning,
            _ => System.Windows.Forms.ToolTipIcon.Info,
        };
        var title = severity switch
        {
            "critical" => "Drobo: action needed",
            "warning" => "Drobo: warning",
            _ => "Drobo",
        };

        // The shell truncates past ~255 chars; trimming here means the cut
        // lands somewhere we chose rather than mid-word wherever it fell.
        var text = message.Length > 240 ? message[..237] + "..." : message;

        try
        {
            _notifyIcon.ShowBalloonTip(10_000, title, text, icon);
        }
        catch (Exception)
        {
            // A notification that cannot be drawn is not worth taking the app
            // down for. The alert is on the Alerts tab either way, which is the
            // authoritative place -- this was only the courtesy copy.
        }
    }

    private void ShowWindow()
    {
        _window.Show();
        if (_window.WindowState == WindowState.Minimized)
            _window.WindowState = WindowState.Normal;
        _window.Activate();
    }

    private void ExitApp()
    {
        _exitRequested = true;
        _window.Close();
    }

    private void Window_Closing(object? sender, CancelEventArgs e)
    {
        if (_exitRequested) return; // a real exit was requested -- let it happen
        if (!DroboSettings.LoadAll().CloseToTrayEnabled) return; // opt-in only; default is "X really closes"

        e.Cancel = true;
        _window.Hide();
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;

        _window.Closing -= Window_Closing;

        // The classic leaked-tray-icon bug is exactly this line missing:
        // Visible must be set false before Dispose, or the icon lingers in
        // the notification area (invisible, but still occupying a slot)
        // until the user mouses over where it used to be.
        _notifyIcon.Visible = false;
        _notifyIcon.Dispose();
        _normalIcon.Dispose();
        _alertIcon.Dispose();
    }
}
