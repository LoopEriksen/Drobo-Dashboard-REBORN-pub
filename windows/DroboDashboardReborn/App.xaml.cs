using System.Windows;

namespace DroboDashboardReborn;

/// <summary>
/// Application entry point.
///
/// Its one job beyond the default is the splash screen: put something on screen
/// immediately, because the gap between double-clicking and the main window
/// appearing runs to several seconds (WPF startup, then an mDNS browse that is
/// slow by design), and several seconds of empty desktop is indistinguishable
/// from a program that failed to start.
/// </summary>
public partial class App : Application
{
    /// <summary>
    /// The splash, while it exists. MainWindow reports progress to it during
    /// startup and closes it once the window has actually rendered.
    ///
    /// Null once dismissed, and callers must null-check rather than assume:
    /// SplashWindow's own safety timer can close it at any moment, so "is it
    /// still there?" is a real question rather than a formality.
    /// </summary>
    internal static SplashWindow? Splash { get; set; }

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

        // Shown before StartupUri constructs MainWindow, so it is on screen
        // during the expensive part rather than after it.
        Splash = new SplashWindow();
        Splash.Show();
    }
}
