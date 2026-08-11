using System;
using System.Windows;
using System.Windows.Threading;

namespace DroboDashboardReborn;

/// <summary>
/// The "we are starting, honestly" window. See SplashWindow.xaml for why it
/// exists and why it is not Topmost.
///
/// The only interesting behaviour is the safety timer. A splash that depends on
/// somebody else remembering to close it is a splash that will eventually be
/// left on screen forever by an exception thrown during startup -- with no
/// title bar, no taskbar entry, and no way to dismiss it. So it closes itself
/// regardless after <see cref="MaxLifetime"/>, and anything that finishes
/// sooner simply closes it sooner.
/// </summary>
public partial class SplashWindow : Window
{
    /// <summary>
    /// Longer than a slow start, shorter than somebody's patience.
    ///
    /// A cold start plus the agent's mDNS browse runs to a few seconds; ten
    /// gives that room without stranding anyone if the main window never
    /// arrives. This is a backstop, not the normal path -- normally
    /// MainWindow closes it as soon as it has rendered.
    /// </summary>
    public static readonly TimeSpan MaxLifetime = TimeSpan.FromSeconds(10);

    private readonly DispatcherTimer _safety;
    private bool _closed;

    public SplashWindow()
    {
        InitializeComponent();

        _safety = new DispatcherTimer { Interval = MaxLifetime };
        _safety.Tick += (_, _) => Done();
        _safety.Start();
    }

    /// <summary>Update the line of text under the logo. Safe from any thread.</summary>
    public void Report(string status)
    {
        if (_closed) return;
        if (!Dispatcher.CheckAccess())
        {
            Dispatcher.Invoke(() => Report(status));
            return;
        }
        StatusText.Text = status;
    }

    /// <summary>
    /// Close it. Idempotent on purpose -- both MainWindow and the safety timer
    /// may call this, and whichever gets there first should win without the
    /// other throwing.
    /// </summary>
    public void Done()
    {
        if (_closed) return;
        _closed = true;
        _safety.Stop();
        try
        {
            Close();
        }
        catch (InvalidOperationException)
        {
            // Already closing down (app shutdown raced us). Nothing to do --
            // the window is going away either way, which is all we wanted.
        }
    }
}
