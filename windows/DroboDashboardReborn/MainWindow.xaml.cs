using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using Microsoft.Win32;
using static DroboDashboardReborn.JsonHelpers;

namespace DroboDashboardReborn;

/// <summary>
/// Drobo Dashboard REBORN — native Windows client.
///
/// This app knows nothing about the Drobo protocol. It talks only to the local
/// agent's JSON API (the same API the web dashboard uses), so all the Drobo
/// logic stays in one place.
///
/// The front page is a device picker (LandingRoot): it asks the agent what
/// Drobos it can see on the network (/api/discover) and lets you click the
/// one you want, instead of asking you to know an agent URL and paste in a
/// token. Once connected, DashboardRoot (the tabs and the device header)
/// takes over, and polls /api/status on a timer exactly as before.
///
/// DriveRow/AlertRow/ShareRow/DiscoveredDeviceRow (the row view-models) and
/// the small JSON helpers (Str/Long/Double/Bool/Tb) live in their own files
/// -- see DriveRow.cs, AlertRow.cs, ShareRow.cs, DiscoveredDeviceRow.cs and
/// JsonHelpers.cs -- so this file doesn't become the one place every future
/// tab has to fight over.
/// </summary>
public partial class MainWindow : Window
{
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(8) };
    private readonly DispatcherTimer _timer = new() { Interval = TimeSpan.FromSeconds(10) };
    private readonly ObservableCollection<DriveRow> _bays = new();
    private readonly ObservableCollection<VolumeRow> _volumes = new();
    private readonly ObservableCollection<AlertRow> _alerts = new();
    private readonly ObservableCollection<AlertRow> _resolvedAlerts = new();
    private readonly ObservableCollection<ShareRow> _shares = new();
    private readonly ObservableCollection<DiscoveredDeviceRow> _devices = new();
    private readonly ObservableCollection<DroboSettings.ManualDevice> _manualDevices = new();
    private readonly ObservableCollection<DroboAppRow> _droboApps = new();

    // The guest-logon explanation from the last /api/shares response, kept
    // around so a Connect-as dialog opened later (or an ERROR_ACCESS_DENIED
    // from Windows) can reuse the same wording instead of inventing new text.
    private string? _lastAdviceDetail;

    // The Drobo remembered from a previous launch (see DroboSettings), and
    // whether we've already acted on it this run. One-shot on purpose: only
    // the *first* search after startup should auto-connect. A manual "Change
    // Drobo" later in the same session must land on the picker and stay
    // there until the owner clicks something -- it would be exactly the
    // "steals focus / does things on its own" behaviour the owner is
    // already unhappy about if it kept redirecting itself back.
    private DroboSettings.RememberedDevice? _remembered;

    /// <summary>Decides which alerts earn a desktop notification. Long-lived,
    /// because its whole job depends on remembering what it has already seen.</summary>
    private readonly AlertNotifier _notifier = new();

    /// <summary>
    /// Bumped every time we leave a Drobo or start a fresh device search.
    ///
    /// An `await` is a place where the user can act. Both bugs this fixes are
    /// the same shape: work started before the user changed their mind lands
    /// afterwards and stamps a stale result over the new state. WPF gives no
    /// automatic protection here -- the continuation just runs.
    ///
    /// So async work captures this counter before its first await and checks
    /// it after: if it moved, the world changed underneath and the result is
    /// thrown away rather than rendered. Cheaper and far easier to reason
    /// about than cancellation tokens threaded through every call.
    /// </summary>
    private int _generation;

    /// <summary>The notification switches shown on the Settings panel,
    /// generated from AlertNotifier.Categories.</summary>
    private readonly ObservableCollection<NotificationCategoryRow> _notificationRows = new();
    private bool _autoConnectAttempted;

    // Whether /api/discover gets called at all. Persisted (see DroboSettings),
    // default ON. When off, SearchForDevices skips the network call entirely
    // and only ever shows _manualDevices.
    private bool _autoDiscoveryEnabled = true;

    // Guards against AutoDiscoveryCheck_Changed re-saving/re-searching while
    // the constructor is still setting AutoDiscoveryCheck.IsChecked from the
    // settings file -- that assignment fires Checked/Unchecked same as a
    // click would, and startup already searches on its own via
    // StartupSearchOrPrompt.
    private bool _settingsLoaded;

    // The tray icon (see TrayIconManager) -- owns its own NotifyIcon and
    // disposes it when the window actually closes. Nullable only because
    // it's assigned in the constructor after InitializeComponent, not
    // because it's ever expected to be missing afterwards.
    private TrayIconManager? _tray;

    // Device name + overall health as of the last successful Render(), kept
    // around so Refresh() can hand the tray icon a tooltip/badge update
    // without re-fetching anything -- it already has this from the same
    // /api/status response.
    private string _lastDeviceName = "Drobo";
    private string _lastOverallState = "unknown";

    // Severity sort order, worst first. A Dictionary lookup (not a default-0
    // fallback on a missing key) so an unrecognized severity sorts last
    // instead of accidentally tying with "critical". This bit the app once
    // before (rank[x] || 3 in the old web dashboard, where rank 0 -- the most
    // critical -- was falsy and sorted last) -- don't reintroduce that here.
    private static readonly Dictionary<string, int> SeverityRank = new()
    {
        ["critical"] = 0,
        ["warning"] = 1,
        ["info"] = 2,
    };

    // brushes reused across refreshes
    private static readonly Brush Ok       = Brush("#46C07A");
    private static readonly Brush Warning  = Brush("#E9A83C");
    private static readonly Brush Failed   = Brush("#E2564B");
    private static readonly Brush Unknown  = Brush("#8B93A1");
    private static readonly Brush Accent   = Brush("#2C7BC4");
    private static readonly Brush Muted    = Brush("#6B7683");

    public MainWindow()
    {
        InitializeComponent();
        ApplyStartupSizing();
        RefuseToBePinnedOnTop();

        _tray = new TrayIconManager(this);
        Closed += (_, _) => _tray?.Dispose();

        Bays.ItemsSource = _bays;
        Volumes.ItemsSource = _volumes;
        ActiveAlertsList.ItemsSource = _alerts;
        ResolvedAlertsList.ItemsSource = _resolvedAlerts;
        Shares.ItemsSource = _shares;
        DiscoveredDevices.ItemsSource = _devices;
        ManualDevicesList.ItemsSource = _manualDevices;
        DroboAppsList.ItemsSource = _droboApps;

        _timer.Tick += async (_, _) => await Refresh();

        TryAutoFillFromAgentConfig();

        var settings = DroboSettings.LoadAll();
        _remembered = settings.Remembered;
        foreach (var m in settings.ManualDevices) _manualDevices.Add(m);
        _autoDiscoveryEnabled = settings.AutoDiscoveryEnabled;
        AutoDiscoveryCheck.IsChecked = _autoDiscoveryEnabled;
        PrefCloseToTray.IsChecked = settings.CloseToTrayEnabled;
        LoadNotificationSettings(settings);
        _settingsLoaded = true;

        UpdateForgetLinkVisibility();
        UpdateManualDevicesEmptyState();
        // The splash goes when this window has actually PAINTED, not when the
        // constructor returns. ContentRendered is the first moment there is
        // something real behind it -- closing any earlier swaps the splash for
        // a blank rectangle, which looks worse than no splash at all.
        ContentRendered += (_, _) =>
        {
            App.Splash?.Done();
            App.Splash = null;
        };

        App.Splash?.Report("Looking for your Drobo on the network...");
        _ = StartupSearchOrPrompt();

        // Tools tab: the app's own version doesn't depend on being connected
        // to anything, so it's shown immediately -- this is otherwise the
        // only place the app displays it at all, which makes a support
        // question ("what version are you on?") unanswerable without this.
        InitToolsTab();
    }

    // ---------------------------------------------------------------
    // Never sit on top of other windows
    // ---------------------------------------------------------------

    // Dark title bar. The attribute number moved between Windows 10 builds:
    // 19 in the 1809 era, 20 from 20H1 on. Both are declared and both are
    // tried -- see UseDarkTitleBar.
    private const int DWMWA_USE_IMMERSIVE_DARK_MODE = 20;
    private const int DWMWA_USE_IMMERSIVE_DARK_MODE_PRE_20H1 = 19;

    [System.Runtime.InteropServices.DllImport("dwmapi.dll", PreserveSig = true)]
    private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int value, int size);

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool SetWindowPos(IntPtr hWnd, IntPtr after,
                                            int x, int y, int cx, int cy, uint flags);

    private static readonly IntPtr HWND_NOTOPMOST = new(-2);
    private const uint SWP_NOSIZE = 0x0001, SWP_NOMOVE = 0x0002, SWP_NOACTIVATE = 0x0010;

    /// <summary>
    /// Actively push this window out of the always-on-top band, and keep
    /// pushing whenever it is shown or activated.
    /// </summary>
    /// <remarks>
    /// <para>
    /// Topmost="False" in XAML is not enough on its own. It sets the style
    /// when the window is created, but any other program on the machine can
    /// call SetWindowPos(HWND_TOPMOST) on our handle afterwards and pin us
    /// there -- screen-capture tools, recorders and automation harnesses do
    /// this routinely to get a clean shot, and they do not always put it back.
    /// From the owner's side that is indistinguishable from an application
    /// that rudely forces itself in front of everything else.
    /// </para>
    /// <para>
    /// So we do not merely decline to be topmost; we assert it. SWP_NOACTIVATE
    /// matters here: it re-orders the window without stealing focus, which
    /// would be its own version of the same rudeness.
    /// </para>
    /// </remarks>
    private const int WM_WINDOWPOSCHANGING = 0x0046;
    private static readonly IntPtr HWND_TOPMOST = new(-1);

    [StructLayout(LayoutKind.Sequential)]
    private struct WINDOWPOS
    {
        public IntPtr hwnd, hwndInsertAfter;
        public int x, y, cx, cy;
        public uint flags;
    }

    private void RefuseToBePinnedOnTop()
    {
        Topmost = false;

        void Unpin()
        {
            Topmost = false;
            var h = new System.Windows.Interop.WindowInteropHelper(this).Handle;
            if (h != IntPtr.Zero)
                SetWindowPos(h, HWND_NOTOPMOST, 0, 0, 0, 0,
                             SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE);
        }

        /// <summary>
        /// Ask Windows to draw this window's title bar dark, so the one strip
        /// of chrome WPF does not paint stops being a bright white band above
        /// an otherwise dark application.
        ///
        /// WPF has no property for this: the title bar belongs to the desktop
        /// window manager, not to WPF, so it has to be asked directly through
        /// DwmSetWindowAttribute.
        ///
        /// The attribute NUMBER changed during Windows 10's life -- it was 19
        /// in the 1809-era builds and became 20 from 20H1 onwards. Rather than
        /// detect the build, both are attempted: passing an attribute a given
        /// build does not recognise simply returns a failure code and changes
        /// nothing, so trying the wrong one is harmless.
        ///
        /// Entirely cosmetic and entirely optional. Any failure is swallowed:
        /// on a Windows version that does not support it the title bar stays
        /// light, which is exactly how the app looked before, and is not worth
        /// a crash or even a log line.
        /// </summary>
        void UseDarkTitleBar()
        {
            try
            {
                var handle = new System.Windows.Interop.WindowInteropHelper(this).Handle;
                if (handle == IntPtr.Zero) return;
                int on = 1;
                foreach (int attribute in new[] { DWMWA_USE_IMMERSIVE_DARK_MODE,
                                                  DWMWA_USE_IMMERSIVE_DARK_MODE_PRE_20H1 })
                {
                    if (DwmSetWindowAttribute(handle, attribute, ref on, sizeof(int)) == 0)
                        return;   // took effect; no need to try the older number
                }
            }
            catch (DllNotFoundException)
            {
                // No dwmapi.dll. Not a Windows this app can theme, and not a
                // reason to fail to start.
            }
            catch (EntryPointNotFoundException)
            {
            }
        }

        SourceInitialized += (_, _) =>
        {
            Unpin();
            UseDarkTitleBar();

            // Catch the pin at the moment it is attempted. When any process
            // calls SetWindowPos(us, HWND_TOPMOST), Windows sends us this
            // message first with hwndInsertAfter set to HWND_TOPMOST -- so we
            // rewrite it back to HWND_NOTOPMOST before it takes effect. This
            // is what actually stops it; the event handlers below only fire
            // after the fact, and a capture tool can pin us without ever
            // activating or resizing the window.
            var src = System.Windows.Interop.HwndSource.FromHwnd(
                new System.Windows.Interop.WindowInteropHelper(this).Handle);
            src?.AddHook((IntPtr hwnd, int msg, IntPtr wp, IntPtr lp, ref bool handled) =>
            {
                if (msg == WM_WINDOWPOSCHANGING)
                {
                    var pos = Marshal.PtrToStructure<WINDOWPOS>(lp);
                    if (pos.hwndInsertAfter == HWND_TOPMOST)
                    {
                        pos.hwndInsertAfter = HWND_NOTOPMOST;
                        Marshal.StructureToPtr(pos, lp, false);
                    }
                }
                return IntPtr.Zero;
            });
        };

        Activated += (_, _) => Unpin();
        StateChanged += (_, _) => Unpin();

        // Backstop. The hook above catches the ordinary route, but this has
        // now bitten the owner twice, so it gets belt and braces: a cheap
        // check that puts us back where we belong if anything slips through.
        var watchdog = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(500) };
        watchdog.Tick += (_, _) =>
        {
            var h = new System.Windows.Interop.WindowInteropHelper(this).Handle;
            if (h != IntPtr.Zero && (GetWindowLong(h, GWL_EXSTYLE) & WS_EX_TOPMOST) != 0)
                Unpin();
        };
        watchdog.Start();
    }

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern int GetWindowLong(IntPtr hWnd, int nIndex);

    private const int GWL_EXSTYLE = -20;
    private const int WS_EX_TOPMOST = 0x0008;

    // ---------------------------------------------------------------
    // Window sizing -- computed from the current monitor, not a fixed pixel size
    // ---------------------------------------------------------------

    /// <summary>
    /// A hardcoded 900x720 window can dwarf a laptop screen (or look tiny on
    /// a 4K one). Instead, size relative to SystemParameters.WorkArea -- the
    /// screen minus the taskbar, so the window can never land partly behind
    /// it -- and cap Maximize at 75% of that area (the owner's revised ask;
    /// the first pass used 50%, which read as too cramped once the drive-bay
    /// rows got their extra columns).
    ///
    /// Must run before the window is shown (it does -- the constructor runs
    /// before Application.Run displays it), and after InitializeComponent so
    /// MinWidth/MinHeight from XAML are already in place to clamp against.
    /// </summary>
    private void ApplyStartupSizing()
    {
        var work = SystemParameters.WorkArea;

        // Guard against a pathologically small work area (a tiny remote
        // desktop session, say): the max must never end up below the
        // minimum, or the window couldn't render at a self-consistent size.
        MaxWidth = Math.Max(work.Width * 0.75, MinWidth);
        MaxHeight = Math.Max(work.Height * 0.75, MinHeight);

        // Start smaller than the max -- about 65% of the work area -- so
        // there's real headroom to drag the window bigger, rather than
        // opening already at the cap. Clamped between the minimum and the
        // max above so it's never contradictory on an unusual screen.
        Width = Math.Clamp(work.Width * 0.65, MinWidth, MaxWidth);
        Height = Math.Clamp(work.Height * 0.65, MinHeight, MaxHeight);
    }

    // ---------------------------------------------------------------
    // Agent token auto-fill
    // ---------------------------------------------------------------

    /// <summary>
    /// Fill in the agent's token from its config.json so the app can go
    /// straight to searching instead of asking the user to copy a token in --
    /// but only when the agent URL is loopback, since a config.json found on
    /// THIS machine has nothing to do with an agent running somewhere else.
    ///
    /// Fails silently -- if it isn't found the field simply stays blank and
    /// StartupSearchOrPrompt falls back to showing the token box.
    /// </summary>
    private void TryAutoFillFromAgentConfig()
    {
        if (!IsLoopback(AgentUrl.Text)) return;

        foreach (var path in CandidateConfigPaths())
        {
            try
            {
                if (!File.Exists(path)) continue;

                using var doc = JsonDocument.Parse(File.ReadAllText(path));
                if (!doc.RootElement.TryGetProperty("agent", out var agent)) continue;

                var port = agent.TryGetProperty("port", out var p) && p.TryGetInt32(out var pv)
                    ? pv : 7420;

                if (string.IsNullOrWhiteSpace(AgentUrl.Text))
                    AgentUrl.Text = $"http://127.0.0.1:{port}";

                if (agent.TryGetProperty("token", out var t) &&
                    t.ValueKind == JsonValueKind.String)
                {
                    var token = t.GetString();
                    if (!string.IsNullOrEmpty(token)) Token.Password = token;
                }
                return;
            }
            catch
            {
                // Unreadable or malformed config is not worth interrupting
                // startup for -- fall through and let the next candidate try.
            }
        }
    }

    /// <summary>
    /// Where a saved agent token might live, checked in order. The
    /// LOCALAPPDATA path is where THIS app would ever save its own settings
    /// (never into the repo -- see the constraint on credentials); the
    /// agent/config.json walk-up covers the dev-tree / side-by-side layout
    /// where the agent and this app live in the same checkout.
    /// </summary>
    private static IEnumerable<string> CandidateConfigPaths()
    {
        var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        if (!string.IsNullOrEmpty(localAppData))
            yield return Path.Combine(localAppData, "DroboDashboardReborn", "config.json");

        // Walk up from the executable: dist/ -> windows/ -> repo root, then
        // down into agent/. Covers running from dist, from bin/, and in place.
        var here = AppContext.BaseDirectory;
        var dir = new DirectoryInfo(here);
        for (var i = 0; i < 6 && dir is not null; i++, dir = dir.Parent)
            yield return Path.Combine(dir.FullName, "agent", "config.json");
    }

    private static bool IsLoopback(string url) =>
        Uri.TryCreate(url, UriKind.Absolute, out var u) &&
        (string.Equals(u.Host, "127.0.0.1", StringComparison.Ordinal) ||
         string.Equals(u.Host, "localhost", StringComparison.OrdinalIgnoreCase));

    private static SolidColorBrush Brush(string hex) =>
        new((Color)ColorConverter.ConvertFromString(hex));

    private string BaseUrl => AgentUrl.Text.TrimEnd('/');

    // ---------------------------------------------------------------
    // Landing page: pick a Drobo, or fall back to entering an address
    // ---------------------------------------------------------------

    /// <summary>
    /// Decides how the landing page starts: straight into searching if we
    /// already have a token (the common case, once autofill worked), or with
    /// the token box opened and explained if we don't.
    /// </summary>
    private async Task StartupSearchOrPrompt()
    {
        bool ready = IsLoopback(AgentUrl.Text) && !string.IsNullOrWhiteSpace(Token.Password);
        if (ready)
        {
            await SearchForDevices();
        }
        else
        {
            SetConnSettingsExpanded(true);
            TokenHelp.Visibility = Visibility.Visible;
            LandingSub.Text = "Enter your agent token below, then we'll search for it.";
        }
    }

    private async void RefreshBtn_Click(object sender, RoutedEventArgs e) => await SearchForDevices();

    /// <summary>
    /// Asks the agent what Drobos it can see (/api/discover). This is the
    /// agent's own mDNS search plus the one it's already talking to -- it
    /// takes a few seconds and is cached briefly agent-side, so calling this
    /// again from Refresh is cheap.
    ///
    /// When auto-discovery is turned off (see AutoDiscoveryCheck_Changed),
    /// this never calls the agent at all -- the picker shows only whatever
    /// is in _manualDevices, all necessarily unverified, since nothing asked
    /// the network about them.
    /// </summary>
    private async Task SearchForDevices()
    {
        // Four callers can start a search, and nothing stopped two running at
        // once. The damaging order is: a slow discover is in flight, the user
        // unticks auto-discovery, the fast manual-only pass renders and sets
        // the caption "Showing manually added Drobos only" -- and then the old
        // discover lands and fills the list with network-found Drobos beneath
        // that caption. The screen would then assert something the app did not
        // do on this pass.
        //
        // Bumping the generation means the earlier search discards its own
        // result instead of overwriting the newer one. Toggling the setting
        // also counts as a change of world, which is why the bump is here at
        // the top rather than only in ChangeDrobo_Click.
        int generation = ++_generation;

        if (!_autoDiscoveryEnabled)
        {
            LandingSub.Text = "Automatic discovery is off. Showing manually added Drobos only.";
            DiscoverErrorBox.Visibility = Visibility.Collapsed;
            // Still probed. Discovery being off means "don't broadcast", not
            // "don't look" -- the addresses were typed in precisely so they
            // could be checked directly.
            var manualOnly = await ProbeManualDevices();
            if (generation != _generation) return;   // superseded while probing
            RenderDevices(null, manualOnly);
            await MaybeAutoConnectToRememberedDevice();
            return;
        }

        LandingSub.Text = "Pick the one you want to open.";
        SetSearching(true);
        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Get, BaseUrl + "/api/discover");
            req.Headers.Add("X-Agent-Token", Token.Password);
            using var resp = await _http.SendAsync(req);

            if (resp.StatusCode == HttpStatusCode.Unauthorized)
            {
                ShowDiscoverError("The agent rejected that token. Enter the right one below.");
                SetConnSettingsExpanded(true);
                TokenHelp.Visibility = Visibility.Visible;
                return;
            }
            resp.EnsureSuccessStatusCode();

            var json = await resp.Content.ReadAsStringAsync();
            var discovered = JsonDocument.Parse(json).RootElement;
            var probed = await ProbeManualDevices();
            // A newer search (or a Drobo switch) started while this one was in
            // the air. Its result is already on screen; ours is stale by
            // definition, so it is dropped rather than painted over the top.
            if (generation != _generation) return;
            RenderDevices(discovered, probed);
            await MaybeAutoConnectToRememberedDevice();
        }
        catch (Exception ex)
        {
            ShowDiscoverError("Could not search the network. Is the agent running?  (" + ex.Message + ")");
        }
        finally
        {
            // Only the CURRENT search may clear the searching state. An older
            // one finishing late would otherwise re-enable Refresh and hide
            // the "Searching..." panel while the newer search is still going,
            // making a running search look finished.
            if (generation == _generation) SetSearching(false);
        }
    }

    /// <summary>
    /// Skips the picker on the *first* search after launch if this is a
    /// Drobo we've connected to before (see DroboSettings). Matched by
    /// esa_id, not host -- a DHCP-reassigned address is still the same
    /// physical Drobo. One-shot: never fires again this run, so a later
    /// manual "Change Drobo" reliably lands on the picker instead of
    /// bouncing straight back to the same device.
    /// </summary>
    private async Task MaybeAutoConnectToRememberedDevice()
    {
        if (_autoConnectAttempted) return;
        _autoConnectAttempted = true;

        if (_remembered is null) return;

        var match = _devices.FirstOrDefault(d => !string.IsNullOrEmpty(d.EsaId) && d.EsaId == _remembered.EsaId);
        if (match is null)
        {
            // "Isn't showing up right now" is true and unhelpful. The most
            // common reason a remembered Drobo vanishes from this list is that
            // the PC has joined a different network -- mDNS is multicast, and
            // multicast does not cross a subnet boundary any more than the
            // status connection does. So the empty picker and the unreachable
            // dashboard are usually the same problem, and both deserve the same
            // answer. Asking costs one local API call and no network traffic.
            LandingSub.Text = $"Your usual Drobo ({_remembered.Name}) isn't showing up right now.";
            var why = await ExplainMissingDevice(_remembered.Host);
            if (why is not null) LandingSub.Text += "  " + why;
            return;
        }

        LandingSub.Text = $"Reconnecting to {match.Name}…";
        await ConnectAndShowDashboard(match);
    }

    /// <summary>
    /// Why a remembered Drobo might be missing from the picker, or null if we
    /// have nothing useful to say.
    ///
    /// Answers only the one question the agent can settle locally: is this PC
    /// even on that Drobo's network? Anything else -- powered off, unplugged,
    /// still booting -- is indistinguishable from here, so this stays silent
    /// rather than guessing between them.
    ///
    /// Never throws and never blocks the picker. A missing host, an older
    /// agent without the endpoint, or a Drobo we have only ever reached by
    /// mDNS (so no address was remembered) all come back as null, and the
    /// screen reads exactly as it did before this existed.
    /// </summary>
    private async Task<string?> ExplainMissingDevice(string? host)
    {
        if (string.IsNullOrWhiteSpace(host)) return null;
        try
        {
            var root = await FetchJson("/api/netcheck?host=" + Uri.EscapeDataString(host));
            if (!root.TryGetProperty("hint", out var hint) || hint.ValueKind != JsonValueKind.Object)
                return null;  // same network, or the agent couldn't tell
            var headline = Str(hint, "headline", "");
            var detail = Str(hint, "detail", "");
            return headline.Length == 0 ? null : (headline + " " + detail).Trim();
        }
        catch
        {
            return null;
        }
    }

    private void SetSearching(bool searching)
    {
        SearchingPanel.Visibility = searching ? Visibility.Visible : Visibility.Collapsed;
        RefreshBtn.IsEnabled = !searching;
        RefreshBtn.Content = searching ? "Searching…" : "Refresh";
        if (searching) DiscoverErrorBox.Visibility = Visibility.Collapsed;
    }

    private void ShowDiscoverError(string message)
    {
        DiscoverErrorText.Text = message;
        DiscoverErrorBox.Visibility = Visibility.Visible;
        EmptyStateBox.Visibility = Visibility.Collapsed;
        _devices.Clear();
    }

    /// <summary>
    /// Builds the picker list from the agent's /api/discover response (null
    /// when auto-discovery is off, meaning skip it entirely), plus any
    /// manually-added addresses that answered when probed, plus whatever is
    /// left in _manualDevices.
    ///
    /// A manual entry confirmed by either route gets tagged "MANUALLY ADDED"
    /// rather than duplicated. One that answered nothing still gets a row --
    /// a Drobo that is simply switched off should not vanish from the list --
    /// but an honest "NOT VERIFIED" badge, which now means what it says:
    /// we asked that address directly, this pass, and nothing answered.
    /// It used to mean only that automatic discovery hadn't happened to find
    /// it, which was a much weaker claim wearing the same words.
    /// </summary>
    private void RenderDevices(JsonElement? discoverRoot, List<JsonElement>? probedManual = null)
    {
        DiscoverErrorBox.Visibility = Visibility.Collapsed;

        var list = new List<DiscoveredDeviceRow>();
        var manualMatched = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        var devices = new List<JsonElement>();
        if (discoverRoot is { } root && root.TryGetProperty("devices", out var arr) && arr.ValueKind == JsonValueKind.Array)
            devices.AddRange(arr.EnumerateArray());
        // Merged in as though discovered, because in every sense that matters
        // they were: the agent connected and read the device's own greeting.
        // Only how we knew where to look differed. Anything the browse already
        // reported is skipped so a Drobo found both ways gets one row.
        foreach (var d in probedManual ?? new List<JsonElement>())
        {
            var host = Str(d, "host", "");
            if (!devices.Any(x => string.Equals(Str(x, "host", ""), host, StringComparison.OrdinalIgnoreCase)))
                devices.Add(d);
        }

        {
            foreach (var d in devices)
            {
                var model = Str(d, "model", "");
                var firmware = Str(d, "firmware", "");
                var host = Str(d, "host", "");
                bool current = d.TryGetProperty("is_current", out var ic) && ic.ValueKind == JsonValueKind.True;

                var summaryParts = new[] { model, string.IsNullOrEmpty(firmware) ? "" : "firmware " + firmware, host }
                    .Where(s => !string.IsNullOrEmpty(s));

                var row = new DiscoveredDeviceRow
                {
                    Host = host,
                    Port = d.TryGetProperty("port", out var p) && p.ValueKind == JsonValueKind.Number ? p.GetInt32() : 0,
                    Name = string.IsNullOrEmpty(Str(d, "name", "")) ? "Drobo" : Str(d, "name", "Drobo"),
                    Model = model,
                    Firmware = firmware,
                    EsaId = Str(d, "esa_id", ""),
                    IsCurrent = current,
                    InUseVisibility = current ? Visibility.Visible : Visibility.Collapsed,
                    Summary = string.Join("  ·  ", summaryParts),
                };

                if (!string.IsNullOrEmpty(host) &&
                    _manualDevices.Any(m => string.Equals(m.Host, host, StringComparison.OrdinalIgnoreCase)))
                {
                    manualMatched.Add(host);
                    row.BadgeText = "MANUALLY ADDED";
                    row.BadgeVisibility = Visibility.Visible;
                    row.BadgeBackground = Brush("#12202E");
                    row.BadgeForeground = Accent;
                }

                list.Add(row);
            }
        }

        // Any manual address that answered nothing this pass. Still listed --
        // a Drobo that is switched off or unplugged should not disappear from
        // your own list -- but labelled for what it is.
        foreach (var m in _manualDevices)
        {
            if (manualMatched.Contains(m.Host)) continue;
            list.Add(new DiscoveredDeviceRow
            {
                Host = m.Host,
                Port = m.Port,
                Name = m.Host,
                Summary = "Manually added" + (m.Port > 0 ? $"  ·  port {m.Port}" : "") + "  ·  didn't answer",
                BadgeText = "NOT VERIFIED",
                BadgeVisibility = Visibility.Visible,
                BadgeBackground = Brush("#2E2311"),
                BadgeForeground = Warning,
            });
        }

        _devices.Clear();
        foreach (var row in list) _devices.Add(row);

        EmptyStateBox.Visibility = list.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        EmptyStateTitle.Text = _autoDiscoveryEnabled ? "No Drobos showed up." : "Automatic discovery is off.";
        EmptyStateDetail.Text = _autoDiscoveryEnabled
            ? "Automatic discovery doesn't work on every Windows network. You can enter its address yourself instead."
            : "Turn discovery back on in Settings, or add a Drobo's address there yourself.";
    }

    /// <summary>
    /// The row's Connect button. A card shows a Drobo the agent found on the
    /// LAN via mDNS, but this app never opens a socket to the Drobo itself
    /// (see the ports 5000/5001 rule) -- it only ever talks to the agent's
    /// HTTP API. So clicking Connect doesn't dial that device's own address;
    /// it proceeds to connect to the agent that's already configured, which
    /// in the normal one-agent-one-Drobo setup is exactly the same thing
    /// from the owner's point of view: click your Drobo, you're in.
    /// </summary>
    private async void DeviceConnectButton_Click(object sender, RoutedEventArgs e)
    {
        var row = (DiscoveredDeviceRow)((FrameworkElement)sender).DataContext;
        await ConnectAndShowDashboard(row);
    }

    /// <summary>Double-clicking anywhere else on the row connects too -- same
    /// destination as the Connect button. e.ClickCount is what tells a
    /// double-click apart from an ordinary single click, so a stray click on
    /// the row (not the button) does nothing by itself. Clicks that land on
    /// the Connect button never reach here: ButtonBase marks its own
    /// MouseLeftButtonDown handled before this bubbles up.</summary>
    private async void DeviceRow_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
    {
        if (e.ClickCount != 2) return;
        var row = (DiscoveredDeviceRow)((FrameworkElement)sender).DataContext;
        await ConnectAndShowDashboard(row);
    }

    private void EnterAddressManually_Click(object sender, RoutedEventArgs e)
    {
        SetConnSettingsExpanded(true);
        ManualHostInput.Focus();
    }

    /// <summary>
    /// Kept as a no-op so the several callers that used to open the settings
    /// panel still read sensibly.
    ///
    /// The panel is permanently visible now (see the XAML note): it used to be
    /// a collapsed card behind a toggle, which made the page jump every time it
    /// opened or closed. Callers like "the token was rejected, show them where
    /// to fix it" no longer need to reveal anything -- it is already on screen
    /// -- but deleting the calls would lose the record of WHY those moments
    /// wanted the owner's attention pointed at settings.
    /// </summary>
    private void SetConnSettingsExpanded(bool expanded)
    {
        // Nothing to expand any more. Intentionally empty.
    }

    // ---------------------------------------------------------------
    // Settings: manually-added Drobos, and the auto-discovery toggle
    // ---------------------------------------------------------------

    /// <summary>
    /// Parses "host" or "host:port" from the input box, checks the address is
    /// really a Drobo, persists it (see DroboSettings.AddManualDevice --
    /// de-duplicated there by host+port), and re-runs the search so it shows up
    /// in the picker right away instead of waiting for the next Refresh.
    ///
    /// The check is the part that used to be missing. The agent can now probe
    /// one named address on demand (/api/discover?host=), so typing an address
    /// gets an answer immediately -- the device's own name and model back, or a
    /// plain statement that nothing there answered. Before this, a typo and a
    /// powered-off Drobo produced the same silent "NOT VERIFIED" row and no way
    /// to tell them apart.
    ///
    /// A failed probe still saves the entry. Somebody adding an address for a
    /// Drobo that is currently switched off is doing something reasonable, and
    /// throwing their input away to punish them for it would be worse than
    /// keeping it with an honest label.
    /// </summary>
    private async void AddManualDevice_Click(object sender, RoutedEventArgs e)
    {
        var raw = ManualHostInput.Text.Trim();
        if (string.IsNullOrEmpty(raw)) return;

        var host = raw;
        var port = 0;
        var colon = raw.LastIndexOf(':');
        if (colon > 0 && int.TryParse(raw[(colon + 1)..], out var parsedPort) && parsedPort > 0)
        {
            host = raw[..colon];
            port = parsedPort;
        }
        if (string.IsNullOrEmpty(host)) return;

        DroboSettings.AddManualDevice(host, port);
        if (!_manualDevices.Any(m => string.Equals(m.Host, host, StringComparison.OrdinalIgnoreCase) && m.Port == port))
            _manualDevices.Add(new DroboSettings.ManualDevice { Host = host, Port = port });

        ManualHostInput.Clear();
        UpdateManualDevicesEmptyState();

        ManualAddResult.Text = $"Checking {host}…";
        ManualAddResult.Foreground = Muted;
        ManualAddResult.Visibility = Visibility.Visible;

        var probe = await ProbeAddress(host);
        if (probe.found)
        {
            var name = probe.device.ValueKind == JsonValueKind.Object
                ? Str(probe.device, "name", "") : "";
            ManualAddResult.Text = $"Found {(string.IsNullOrEmpty(name) ? "a Drobo" : name)} at {host}.";
            ManualAddResult.Foreground = Ok;
        }
        else
        {
            ManualAddResult.Text = probe.error ?? $"Nothing at {host} answered as a Drobo.";
            ManualAddResult.Foreground = Warning;
        }

        await SearchForDevices();
    }

    /// <summary>
    /// Ask the agent whether one specific address is a Drobo.
    ///
    /// This is the targeted counterpart to the picker's automatic search: no
    /// multicast browse, just a connection to the address given and a read of
    /// whatever greeting comes back. It exists because mDNS is filtered outright
    /// on plenty of real networks -- corporate LANs, guest SSIDs, some consumer
    /// routers -- where browsing returns nothing however long it listens.
    ///
    /// Returns (found, name, error). Never throws: an agent that is down or too
    /// old to know this endpoint comes back as not-found with a readable reason,
    /// and the caller carries on.
    /// </summary>
    private async Task<(bool found, JsonElement device, string? error)> ProbeAddress(string host)
    {
        try
        {
            var root = await FetchJson("/api/discover?host=" + Uri.EscapeDataString(host));
            bool found = root.TryGetProperty("manual_found", out var f)
                         && f.ValueKind == JsonValueKind.True;
            if (!found)
                return (false, default, Str(root, "manual_error", $"Nothing at {host} answered as a Drobo."));

            if (root.TryGetProperty("devices", out var arr) && arr.ValueKind == JsonValueKind.Array)
            {
                foreach (var d in arr.EnumerateArray())
                {
                    if (string.Equals(Str(d, "host", ""), host, StringComparison.OrdinalIgnoreCase))
                        return (true, d.Clone(), null);
                }
            }
            return (true, default, null);
        }
        catch (Exception ex)
        {
            return (false, default, "Could not ask the agent to check that address: " + ex.Message);
        }
    }

    /// <summary>
    /// Check each manually-added address and return the ones that answered, as
    /// the agent described them.
    ///
    /// These are merged into the picker exactly as though discovery had found
    /// them, because in every sense that matters it did — the agent connected to
    /// the address and read the device's own greeting. Only the way we learned
    /// where to look was different, and the row says so with its badge.
    ///
    /// Cheap now that a named probe skips the multicast browse: one short
    /// connection per address, and there is realistically one address.
    /// </summary>
    private async Task<List<JsonElement>> ProbeManualDevices()
    {
        var confirmed = new List<JsonElement>();
        foreach (var m in _manualDevices.ToList())
        {
            if (string.IsNullOrWhiteSpace(m.Host)) continue;
            var (found, device, _) = await ProbeAddress(m.Host);
            if (found && device.ValueKind == JsonValueKind.Object) confirmed.Add(device);
        }
        return confirmed;
    }

    private async void RemoveManualDevice_Click(object sender, RoutedEventArgs e)
    {
        var m = (DroboSettings.ManualDevice)((FrameworkElement)sender).DataContext;
        DroboSettings.RemoveManualDevice(m.Host, m.Port);
        _manualDevices.Remove(m);
        UpdateManualDevicesEmptyState();
        await SearchForDevices();
    }

    private void UpdateManualDevicesEmptyState() =>
        ManualDevicesEmpty.Visibility = _manualDevices.Count == 0 ? Visibility.Visible : Visibility.Collapsed;

    // ---------------------------------------------------------------
    // Settings > PREFERENCES: how this app behaves
    // ---------------------------------------------------------------

    /// <summary>
    /// Fill in the two preference panels whose content comes from the AGENT
    /// rather than from this app's own settings file: whether email alerts are
    /// configured, and whether update checking is switched on.
    ///
    /// Both are agent-side settings deliberately. An SMTP password belongs in
    /// the agent's config.json -- which is gitignored and never leaves the
    /// machine -- not in this window and not in this app's settings file. So
    /// this reports what the agent says and offers a test button; it does not
    /// collect credentials.
    ///
    /// Never throws: an older agent that does not know these endpoints, or one
    /// that is not running, leaves the panels saying so rather than blanking.
    /// </summary>
    private async Task RefreshPreferencePanels()
    {
        try
        {
            var root = await FetchJson("/api/update");
            var state = Str(root, "state", "");
            PrefUpdateStatus.Text = state switch
            {
                "disabled"  => "Switched off. The agent is contacting nothing.",
                "current"   => $"You are on the latest version ({Str(root, "current_version", "?")}).",
                "available" => Str(root, "reason", "An update is available."),
                _           => Str(root, "reason", "Could not check just now."),
            };
        }
        catch (Exception ex)
        {
            PrefUpdateStatus.Text = "Could not ask the agent: " + ex.Message;
        }

        try
        {
            var root = await FetchJson("/api/emailalerts");
            bool configured = root.TryGetProperty("configured", out var c)
                              && c.ValueKind == JsonValueKind.True;
            PrefEmailStatus.Text = configured
                ? Str(root, "summary", "Email alerts are configured in the agent.")
                : "Not configured. Add an \"email\" block to the agent's config.json to turn this on.";
            PrefEmailTestBtn.IsEnabled = configured;
        }
        catch
        {
            // The endpoint may not exist yet -- the backend for this is being
            // built separately. Say so plainly rather than implying a fault.
            PrefEmailStatus.Text = "This agent does not offer email alerts yet.";
            PrefEmailTestBtn.IsEnabled = false;
        }
    }

    private async void PrefEmailTest_Click(object sender, RoutedEventArgs e)
    {
        PrefEmailTestBtn.IsEnabled = false;
        PrefEmailStatus.Text = "Sending a test email...";
        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Post, BaseUrl + "/api/emailalerts/test");
            req.Headers.Add("X-Agent-Token", Token.Password);
            using var resp = await _http.SendAsync(req);
            var body = await resp.Content.ReadAsStringAsync();
            using var doc = JsonDocument.Parse(body);
            PrefEmailStatus.Text = resp.IsSuccessStatusCode
                ? Str(doc.RootElement, "message", "Sent. Check your inbox.")
                : Str(doc.RootElement, "error", $"The agent returned HTTP {(int)resp.StatusCode}.");
        }
        catch (Exception ex)
        {
            PrefEmailStatus.Text = "Could not send: " + ex.Message;
        }
        finally
        {
            PrefEmailTestBtn.IsEnabled = true;
        }
    }

    // ---------------------------------------------------------------
    // Settings: desktop notifications
    // ---------------------------------------------------------------

    /// <summary>
    /// Build the notification switches from AlertNotifier.Categories.
    ///
    /// Generated rather than written in XAML so the list you can toggle and the
    /// list that actually matches alert keys stay the same list -- two
    /// hand-maintained copies would eventually disagree, and that failure looks
    /// like a switch marked "Drive problems" that silences nothing.
    /// </summary>
    private void LoadNotificationSettings(DroboSettings.Settings settings)
    {
        PrefNotifyMaster.IsChecked = settings.NotificationsEnabled;

        _notificationRows.Clear();
        foreach (var (id, label, description) in AlertNotifier.Categories)
        {
            _notificationRows.Add(new NotificationCategoryRow
            {
                Id = id,
                Label = label,
                Description = description,
                // Absent from the file counts as off, matching DroboSettings.
                Enabled = settings.NotificationCategories.TryGetValue(id, out var on) && on,
                MasterOn = settings.NotificationsEnabled,
            });
        }
        PrefNotifyList.ItemsSource = _notificationRows;
    }

    private void PrefNotifyMaster_Changed(object sender, RoutedEventArgs e)
    {
        if (_notificationRows.Count == 0) return;   // still starting up

        bool on = PrefNotifyMaster.IsChecked == true;
        foreach (var row in _notificationRows) row.MasterOn = on;
        SaveNotificationSettings();
    }

    private void PrefNotifyCategory_Changed(object sender, RoutedEventArgs e)
    {
        if (_notificationRows.Count == 0) return;
        SaveNotificationSettings();
    }

    /// <summary>
    /// Persist immediately on every toggle. No Apply button, and no waiting for
    /// the window to close: somebody switching a notification off is usually
    /// doing it BECAUSE it just annoyed them, and "it will stop after you
    /// restart" is not an acceptable answer to that.
    /// </summary>
    private void SaveNotificationSettings()
    {
        var settings = DroboSettings.LoadAll();
        settings.NotificationsEnabled = PrefNotifyMaster.IsChecked == true;
        foreach (var row in _notificationRows)
            settings.NotificationCategories[row.Id] = row.Enabled;
        DroboSettings.SaveAll(settings);
    }

    /// <summary>
    /// Toggling auto-discovery persists immediately and re-runs the search
    /// so the picker reflects it right away. Guarded by _settingsLoaded so
    /// the constructor's own IsChecked assignment (loading the saved value)
    /// doesn't re-save the same value or trigger a second, redundant search
    /// on top of StartupSearchOrPrompt.
    /// </summary>
    private async void AutoDiscoveryCheck_Changed(object sender, RoutedEventArgs e)
    {
        _autoDiscoveryEnabled = AutoDiscoveryCheck.IsChecked == true;
        if (!_settingsLoaded) return;

        DroboSettings.SetAutoDiscoveryEnabled(_autoDiscoveryEnabled);
        await SearchForDevices();
    }

    /// <summary>Tools tab, "Closing the window minimizes to the tray instead
    /// of exiting" -- opt-in, default off (see TrayIconManager.Window_Closing,
    /// which reads this same setting fresh on every close). Guarded by
    /// _settingsLoaded for the same reason as AutoDiscoveryCheck_Changed: the
    /// constructor's own IsChecked assignment must not re-save the value it
    /// just loaded.</summary>
    private void PrefCloseToTray_Changed(object sender, RoutedEventArgs e)
    {
        if (!_settingsLoaded) return;
        DroboSettings.SetCloseToTrayEnabled(PrefCloseToTray.IsChecked == true);
    }

    /// <summary>The agent-connection "Connect" button inside Settings -- same
    /// path as clicking a device row's Connect button, just with a hand-typed
    /// agent URL/token. This one connects to the agent directly rather than
    /// going through a discovered row, so nothing gets remembered here --
    /// only a row click/double-click knows which physical Drobo this is.</summary>
    private async void ConnectBtn_Click(object sender, RoutedEventArgs e) => await ConnectAndShowDashboard();

    /// <param name="device">The card that was clicked, if this connect came from
    /// one -- null for the manual Connect button. Only ever remembered when we
    /// actually have a serial to remember it by.</param>
    private async Task ConnectAndShowDashboard(DiscoveredDeviceRow? device = null)
    {
        _timer.Stop();
        var (ok, error) = await Refresh();
        if (ok)
        {
            _timer.Start();
            ShowDashboard();
            RememberDevice(device);
        }
        else
        {
            ShowDiscoverError(error ?? "Could not connect.");
        }
    }

    private void RememberDevice(DiscoveredDeviceRow? device)
    {
        if (device is null || string.IsNullOrEmpty(device.EsaId)) return;

        _remembered = new DroboSettings.RememberedDevice
        {
            EsaId = device.EsaId,
            Host = device.Host,
            Name = device.Name,
            Model = device.Model,
        };
        DroboSettings.RememberDevice(_remembered);
        UpdateForgetLinkVisibility();
    }

    private void ForgetDevice_Click(object sender, RoutedEventArgs e)
    {
        DroboSettings.ForgetDevice();
        _remembered = null;
        UpdateForgetLinkVisibility();
    }

    private void UpdateForgetLinkVisibility() =>
        ForgetDeviceLink.Visibility = _remembered is not null ? Visibility.Visible : Visibility.Collapsed;

    /// <summary>
    /// "Choose a different Drobo" -- the way back to the picker from the
    /// dashboard. Re-runs discovery (so a Drobo powered on since launch shows
    /// up), and deliberately does NOT touch _autoConnectAttempted: the owner
    /// asked to see the picker, so the remembered device must not silently
    /// reconnect out from under them the moment the search finishes.
    /// </summary>
    private void ChangeDrobo_Click(object sender, RoutedEventArgs e)
    {
        _timer.Stop();
        ShowLanding();
        _ = SearchForDevices();

        // The alerts of the Drobo we just left say nothing about the next one.
        // Without this, switching devices would either suppress a genuine
        // notification (same alert key on both) or fire one for a problem the
        // new device does not have.
        // Bumped BEFORE Reset(), and the order matters. A poll already in
        // flight will now see a changed generation and discard itself, so it
        // cannot land after this line and re-seed the notifier with the Drobo
        // we are walking away from.
        _generation++;
        _notifier.Reset();

        // The Settings tab's config was read from whichever Drobo we were
        // just connected to -- clear it out so a switch to a different
        // device can't leave the old one's network settings on screen
        // looking like they belong to the new one.
        SettingsGrid.Visibility = Visibility.Collapsed;
        SettingsEmpty.Visibility = Visibility.Visible;
        SettingsError.Visibility = Visibility.Collapsed;
        SettingsLinkWarningBox.Visibility = Visibility.Collapsed;
    }

    private void ShowLanding()
    {
        DashboardRoot.Visibility = Visibility.Collapsed;
        LandingRoot.Visibility = Visibility.Visible;
    }

    private void ShowDashboard()
    {
        LandingRoot.Visibility = Visibility.Collapsed;
        DashboardRoot.Visibility = Visibility.Visible;
    }

    // Selected by NAME, not by position. These used to be hardcoded indices
    // (SHARES was 2, TOOLS was 4) which happened to be correct only because
    // the ALERTS tab uses a templated header and is easy to forget when
    // counting. Adding or reordering a single tab would have silently sent
    // this button to the wrong page and stopped the Tools tab ever refreshing.
    private void GoToShares_Click(object sender, RoutedEventArgs e) => MainTabs.SelectedItem = TabShares;

    // ---------------------------------------------------------------
    // Status polling (unchanged behaviour, just returns whether it worked)
    // ---------------------------------------------------------------

    private async Task<(bool ok, string? error)> Refresh()
    {
        // Which Drobo this poll belongs to. If the user leaves for the picker
        // while we are awaiting, everything below is about a device they are
        // no longer looking at -- see _generation.
        int generation = _generation;
        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Get, BaseUrl + "/api/status");
            req.Headers.Add("X-Agent-Token", Token.Password);
            using var resp = await _http.SendAsync(req);

            if (resp.StatusCode == HttpStatusCode.Unauthorized)
            {
                var msg = "Token rejected. Check the token from the agent's config.json.";
                ShowError(msg);
                return (false, msg);
            }
            resp.EnsureSuccessStatusCode();

            var json = await resp.Content.ReadAsStringAsync();
            // The user may have gone back to the picker while that was in the
            // air. Rendering now would repaint a dashboard they have left, and
            // -- worse -- would re-seed AlertNotifier with the OLD Drobo's
            // alerts, so the NEXT device would announce its standing problems
            // the instant it connected. That is precisely the interruption
            // AlertNotifier rule 2 exists to prevent.
            if (generation != _generation) return (false, null);
            Render(JsonDocument.Parse(json).RootElement);
        }
        catch (Exception ex)
        {
            if (generation != _generation) return (false, null);
            var msg = "Cannot reach the agent. Is it running?  (" + ex.Message + ")";
            ShowError(msg);
            return (false, msg);
        }

        if (generation != _generation) return (false, null);

        // Alerts and shares are fetched on the same tick as status, using the
        // same HttpClient/timer/token -- but a failure in either must not
        // disturb the status/capacity/bays panels that already rendered
        // successfully.
        await RefreshAlerts();
        await RefreshShares();

        // Tray tooltip/badge, from data this same tick already fetched --
        // see TrayIconManager.UpdateStatus. _alerts is the active-only list
        // RenderAlerts just rebuilt, so "any critical" here means "active
        // right now", not "ever happened".
        _tray?.UpdateStatus(_lastDeviceName, _lastOverallState, _alerts.Any(a => a.Severity == "critical"));
        return (true, null);
    }

    private async Task<JsonElement> FetchJson(string path)
    {
        using var req = new HttpRequestMessage(HttpMethod.Get, BaseUrl + path);
        req.Headers.Add("X-Agent-Token", Token.Password);
        using var resp = await _http.SendAsync(req);
        resp.EnsureSuccessStatusCode();
        var json = await resp.Content.ReadAsStringAsync();
        using var doc = JsonDocument.Parse(json);
        return doc.RootElement.Clone(); // survives past `doc`'s disposal
    }

    private async Task RefreshAlerts()
    {
        try
        {
            var activeRoot = await FetchJson("/api/alerts");
            var allRoot = await FetchJson("/api/alerts?all=1");
            RenderAlerts(activeRoot, allRoot);
            AlertsError.Visibility = Visibility.Collapsed;
        }
        catch
        {
            // Degrade gracefully: leave whatever alerts were last shown (or
            // the empty state) in place, and note that the fetch failed
            // without blanking anything else in the window.
            AlertsError.Text = "Alerts unavailable.";
            AlertsError.Visibility = Visibility.Visible;
        }
    }

    /// <summary>
    /// Builds the Alerts tab's Active/Resolved groups from the two
    /// /api/alerts responses.
    ///
    /// activeRoot (plain /api/alerts) lists only what's active right now;
    /// allRoot (?all=1) lists everything, active or since resolved. Comparing
    /// the keys that appear in both -- rather than trusting an "active" field
    /// name that might change -- is what tells us which of the "all" entries
    /// have actually cleared.
    /// </summary>
    /// <summary>
    /// Hand the currently-active alerts to <see cref="AlertNotifier"/> and show
    /// whatever it decides is worth a desktop notification.
    ///
    /// Settings are re-read each tick rather than cached, so switching a
    /// category off takes effect immediately instead of at the next restart --
    /// which matters most in the exact moment somebody is switching it off
    /// because it just annoyed them.
    /// </summary>
    private void MaybeNotify(List<AlertRow> active)
    {
        if (_tray is null) return;

        var settings = DroboSettings.LoadAll();
        var notices = _notifier.Evaluate(
            active.Select(a => (a.Key, a.Severity, a.Message)),
            settings.NotificationsEnabled,
            settings.NotificationCategories);

        foreach (var notice in notices)
            _tray.ShowAlert(notice.Severity, notice.Message);
    }

    private void RenderAlerts(JsonElement activeRoot, JsonElement allRoot)
    {
        var activeKeys = new HashSet<string>();
        if (activeRoot.TryGetProperty("alerts", out var activeArr) && activeArr.ValueKind == JsonValueKind.Array)
            foreach (var a in activeArr.EnumerateArray())
                activeKeys.Add(Str(a, "key", ""));

        var activeList = new List<AlertRow>();
        var resolvedList = new List<AlertRow>();

        if (allRoot.TryGetProperty("alerts", out var allArr) && allArr.ValueKind == JsonValueKind.Array)
        {
            foreach (var a in allArr.EnumerateArray())
            {
                var key = Str(a, "key", "");
                var severity = Str(a, "severity", "info");
                var (chipBg, chipFg) = SeverityBrushes(severity);
                bool isActive = activeKeys.Contains(key);

                var row = new AlertRow
                {
                    Key = key,
                    Severity = severity,
                    SeverityText = severity.ToUpperInvariant(),
                    Message = Str(a, "message", ""),
                    Since = "since " + RelativeTime(Double(a, "first_seen")),
                    ChipBackground = chipBg,
                    SeverityBrush = chipFg,
                    Active = isActive,
                };

                if (isActive)
                {
                    row.SortTime = Double(a, "first_seen");
                    activeList.Add(row);
                }
                else
                {
                    var clearedAt = Double(a, "cleared_at");
                    row.ClearedText = "cleared " + RelativeTime(clearedAt);
                    row.SortTime = clearedAt;
                    resolvedList.Add(row);
                }
            }
        }

        SortBySeverityThenRecency(activeList);
        SortBySeverityThenRecency(resolvedList);

        // Desktop notifications. Everything about WHETHER to interrupt lives in
        // AlertNotifier -- it stays silent on the first pass after connecting,
        // never repeats a standing alert, and respects the per-category
        // switches, all of which default off. This just shows what it hands
        // back. See its class comment for why that separation exists.
        MaybeNotify(activeList);

        _alerts.Clear();
        foreach (var row in activeList) _alerts.Add(row);
        _resolvedAlerts.Clear();
        foreach (var row in resolvedList) _resolvedAlerts.Add(row);

        ActiveAlertsEmpty.Visibility = activeList.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        ResolvedAlertsEmpty.Visibility = resolvedList.Count == 0 ? Visibility.Visible : Visibility.Collapsed;

        UpdateAlertsTabIndicator(activeList);
    }

    private static (Brush bg, Brush fg) SeverityBrushes(string severity) => severity switch
    {
        "critical" => (Brush("#301714"), Failed),
        "warning"  => (Brush("#2E2311"), Warning),
        "info"     => (Brush("#12202E"), Accent),
        _          => (Brush("#22262E"), Unknown),
    };

    /// <summary>Worst-first, newest-first within a severity band. TryGetValue
    /// avoids the falsy-zero trap -- see the SeverityRank comment above.</summary>
    private static void SortBySeverityThenRecency(List<AlertRow> list)
    {
        list.Sort((x, y) =>
        {
            int rx = SeverityRank.TryGetValue(x.Severity, out var vx) ? vx : int.MaxValue;
            int ry = SeverityRank.TryGetValue(y.Severity, out var vy) ? vy : int.MaxValue;
            return rx != ry ? rx.CompareTo(ry) : y.SortTime.CompareTo(x.SortTime);
        });
    }

    /// <summary>
    /// The Alerts tab header signals unread problems instead of a separate
    /// badge or dashboard widget: bold text plus a coloured dot while
    /// anything is active, completely plain (no dot at all) when nothing is
    /// -- the calm-by-default rule this app follows everywhere else.
    ///
    /// activeList is already sorted worst-first (see SortBySeverityThenRecency),
    /// so its first entry's own severity colour is exactly the dot colour:
    /// red for a critical alert, amber for a warning.
    /// </summary>
    private void UpdateAlertsTabIndicator(List<AlertRow> activeList)
    {
        if (activeList.Count == 0)
        {
            AlertsDot.Visibility = Visibility.Collapsed;
            AlertsTabText.FontWeight = FontWeights.Normal;
            return;
        }
        AlertsDot.Fill = activeList[0].SeverityBrush;
        AlertsDot.Visibility = Visibility.Visible;
        AlertsTabText.FontWeight = FontWeights.Bold;
    }

    private async Task RefreshShares()
    {
        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Get, BaseUrl + "/api/shares");
            req.Headers.Add("X-Agent-Token", Token.Password);
            using var resp = await _http.SendAsync(req);
            resp.EnsureSuccessStatusCode();

            var json = await resp.Content.ReadAsStringAsync();
            RenderShares(JsonDocument.Parse(json).RootElement);
        }
        catch
        {
            // Same rule as alerts: a failed shares fetch must not blank
            // whatever was showing before, and must not disturb the Status tab.
            SharesError.Text = "Shares unavailable.";
            SharesError.Visibility = Visibility.Visible;
        }
    }

    private void RenderShares(JsonElement root)
    {
        SharesError.Visibility = Visibility.Collapsed;

        // Guest-logon advice: a calm explanation of a Windows policy change,
        // not an error -- see shares.py's access_advice for the full story.
        var advice = root.TryGetProperty("advice", out var adv) && adv.ValueKind == JsonValueKind.Object
            ? adv : default;
        var headline = advice.ValueKind == JsonValueKind.Object ? Str(advice, "headline", "") : "";
        _lastAdviceDetail = advice.ValueKind == JsonValueKind.Object ? Str(advice, "detail", "") : "";
        if (string.IsNullOrEmpty(headline))
        {
            GuestAdviceBox.Visibility = Visibility.Collapsed;
        }
        else
        {
            GuestAdviceHeadline.Text = headline;
            GuestAdviceDetail.Text = _lastAdviceDetail;
            GuestAdviceBox.Visibility = Visibility.Visible;
        }

        // Link-speed warning: the Drobo's own report of a slow network link.
        // Mirrored onto the Settings tab's copy of the same box too -- both
        // read from this one /api/shares answer, so they can never disagree,
        // and Settings never needs a network fetch of its own for this part.
        var link = root.TryGetProperty("link", out var lk) && lk.ValueKind == JsonValueKind.Object ? lk : default;
        bool degraded = link.ValueKind == JsonValueKind.Object && Bool(link, "degraded");
        if (degraded)
        {
            LinkWarning.Text = Str(link, "note", "");
            LinkWarningBox.Visibility = Visibility.Visible;
            SettingsLinkWarning.Text = LinkWarning.Text;
            SettingsLinkWarningBox.Visibility = Visibility.Visible;
        }
        else
        {
            LinkWarningBox.Visibility = Visibility.Collapsed;
            SettingsLinkWarningBox.Visibility = Visibility.Collapsed;
        }

        // Rows: one per share, with the live Windows connection state merged
        // in. Windows knows this, not the agent -- it's entirely local to
        // this PC, so it's read fresh here rather than trusted from the API.
        var active = SmbConnections.GetActiveConnections();

        // Only false when the agent positively knows the Drobo has no user
        // accounts. Absent or unknown leaves it true -- see ShareRow.CanSignIn
        // for why a control greyed out on a guess is worse than one that fails
        // with a reason.
        bool canSignIn = advice.ValueKind != JsonValueKind.Object
                         || !advice.TryGetProperty("can_sign_in", out var csi)
                         || csi.ValueKind != JsonValueKind.False;
        string? blockedReason = canSignIn ? null
            : "This Drobo has no user accounts, so there is no name to sign in "
              + "with. Create one in Drobo Dashboard and give it access to the "
              + "share, then connect with it here.";

        var list = new List<ShareRow>();
        if (root.TryGetProperty("shares", out var arr) && arr.ValueKind == JsonValueKind.Array)
        {
            foreach (var s in arr.EnumerateArray())
            {
                var unc = Str(s, "unc", "");
                var match = active.FirstOrDefault(a =>
                    string.Equals(a.RemoteUnc.TrimEnd('\\'), unc.TrimEnd('\\'), StringComparison.OrdinalIgnoreCase));
                bool connected = !string.IsNullOrEmpty(match.RemoteUnc);
                string? letter = connected && !string.IsNullOrEmpty(match.LocalName) ? match.LocalName : null;
                bool timeMachine = Bool(s, "time_machine");

                list.Add(new ShareRow
                {
                    Name = Str(s, "name", ""),
                    Unc = unc,
                    TimeMachine = timeMachine,
                    TimeMachineVisibility = timeMachine ? Visibility.Visible : Visibility.Collapsed,
                    IsConnected = connected,
                    DriveLetter = letter,
                    ConnectionText = connected
                        ? (letter != null ? $"Connected as {letter}" : "Connected (no drive letter)")
                        : "Not connected",
                    CanSignIn = canSignIn,
                    SignInBlockedReason = blockedReason,
                });
            }
        }

        _shares.Clear();
        foreach (var row in list) _shares.Add(row);
        SharesEmpty.Visibility = list.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
    }

    private async void ShareOpen_Click(object sender, RoutedEventArgs e)
    {
        var row = (ShareRow)((FrameworkElement)sender).DataContext;

        if (!row.IsConnected)
        {
            // Opening an unconnected share would just get silently refused by
            // Windows (the guest logon Windows 11 blocks) -- ask for
            // credentials first instead of launching Explorer at a path it
            // can't reach.
            bool connected = await PromptConnect(row);
            if (!connected) return;
        }

        try
        {
            Process.Start(new ProcessStartInfo(row.Unc) { UseShellExecute = true });
        }
        catch (Exception ex)
        {
            MessageBox.Show(this, "Could not open the share: " + ex.Message,
                "Drobo Dashboard REBORN", MessageBoxButton.OK, MessageBoxImage.Warning);
        }
    }

    private async void ShareConnectAs_Click(object sender, RoutedEventArgs e)
    {
        var row = (ShareRow)((FrameworkElement)sender).DataContext;
        await PromptConnect(row);
    }

    private async Task<bool> PromptConnect(ShareRow row)
    {
        var dialog = new ConnectShareDialog(row.Name, row.Unc, SmbConnections.NextFreeDriveLetter(), _lastAdviceDetail)
        {
            Owner = this,
        };
        bool ok = dialog.ShowDialog() == true;
        if (ok) await RefreshShares();
        return ok;
    }

    private async void ShareDisconnect_Click(object sender, RoutedEventArgs e)
    {
        var row = (ShareRow)((FrameworkElement)sender).DataContext;
        if (!row.IsConnected) return;

        // The IsConnected check above happens BEFORE the await, so on its own
        // it does not stop a second click: both pass it while the first
        // disconnect is still running, and the second one then fails because
        // the drive is already gone -- popping an error dialog at someone whose
        // only crime was double-clicking. Disabling the button for the duration
        // is the fix; re-enabled in `finally` so a thrown disconnect cannot
        // leave a dead button behind.
        var button = sender as System.Windows.Controls.Button;
        if (button is not null)
        {
            if (!button.IsEnabled) return;   // already disconnecting this row
            button.IsEnabled = false;
        }

        try
        {
            string target = row.DriveLetter ?? row.Unc;
            int code = await Task.Run(() => SmbConnections.Disconnect(target));
            if (code != 0)
            {
                MessageBox.Show(this, SmbConnections.DescribeError(code, _lastAdviceDetail),
                    "Drobo Dashboard REBORN", MessageBoxButton.OK, MessageBoxImage.Warning);
            }
            await RefreshShares();
        }
        finally
        {
            // RefreshShares rebuilds the rows, so this button may already be
            // detached from the visual tree by now -- setting IsEnabled on it
            // is harmless either way.
            if (button is not null) button.IsEnabled = true;
        }
    }

    /// <summary>The agent's model strings carry runs of internal padding
    /// (e.g. "ACME      DISK-3000A") -- collapse them to single spaces for display.</summary>
    private static string CollapseWhitespace(string s) =>
        string.Join(" ", s.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));

    /// <summary>Turns a unix-seconds timestamp into "4m ago"-style text.</summary>
    private static string RelativeTime(double unixSeconds)
    {
        if (unixSeconds <= 0) return "unknown";
        var then = DateTimeOffset.FromUnixTimeMilliseconds((long)(unixSeconds * 1000));
        var span = DateTimeOffset.UtcNow - then;
        if (span.TotalSeconds < 0) return "just now";
        if (span.TotalMinutes < 1) return "just now";
        if (span.TotalMinutes < 60) return $"{(int)span.TotalMinutes}m ago";
        if (span.TotalHours < 24) return $"{(int)span.TotalHours}h ago";
        return $"{(int)span.TotalDays}d ago";
    }

    private void ShowError(string message)
    {
        SetPill("unreachable", Unknown, "#22262E", "#39424D");
        DeviceSub.Text = message;
        Footer.Text = "Last attempt failed at " + DateTime.Now.ToString("HH:mm:ss");

        // This path is "cannot reach the AGENT", which is a different problem
        // from "cannot reach the Drobo" -- and the banner's claim comes from the
        // agent, so with the agent gone we have no current reading to show. Left
        // on screen it would be a stale assertion about a check nobody has run
        // since. Clear it.
        ShowNetworkHint(default);
    }

    /// <summary>
    /// Show (or hide) the standing firmware note.
    ///
    /// Gated on the agent's <c>notable</c> flag alone. Which versions matter,
    /// and why, live in drobo_nasd/firmware.py — if that judgement ever changes
    /// this window needs no edit, and there is no second copy of the rule to
    /// drift out of step with the web dashboard.
    /// </summary>
    private void ShowFirmwareAdvisory(JsonElement advisory)
    {
        bool notable = advisory.ValueKind == JsonValueKind.Object
                       && advisory.TryGetProperty("notable", out var n)
                       && n.ValueKind == JsonValueKind.True;
        if (!notable)
        {
            FirmwareBox.Visibility = Visibility.Collapsed;
            return;
        }
        FirmwareHeadline.Text = Str(advisory, "headline", "");
        FirmwareDetail.Text = Str(advisory, "detail", "");
        FirmwareBox.Visibility = Visibility.Visible;
    }

    /// <summary>
    /// Show (or hide) the "this PC is on a different network" banner.
    ///
    /// The check itself lives in the agent, in drobo_nasd.netcheck -- one
    /// implementation, so this window and the web dashboard can never end up
    /// telling you different stories about the same two addresses. All this
    /// does is render what it was handed.
    ///
    /// Pass <c>default</c> to hide it. Every field is optional on the wire, but
    /// the banner is never shown with an empty headline: a warning strip with
    /// no text in it is worse than no warning strip.
    /// </summary>
    private void ShowNetworkHint(JsonElement hint)
    {
        if (hint.ValueKind != JsonValueKind.Object)
        {
            NetworkBox.Visibility = Visibility.Collapsed;
            return;
        }

        var headline = Str(hint, "headline", "");
        if (headline.Length == 0)
        {
            NetworkBox.Visibility = Visibility.Collapsed;
            return;
        }

        NetworkHeadline.Text = headline;
        NetworkDetail.Text = Str(hint, "detail", "");
        NetworkCaveat.Text = Str(hint, "caveat", "");
        NetworkDetail.Visibility = NetworkDetail.Text.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
        NetworkCaveat.Visibility = NetworkCaveat.Text.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
        NetworkBox.Visibility = Visibility.Visible;
    }

    /// <summary>
    /// Temperature and uptime for the device header, when we have them.
    ///
    /// The chassis temperature comes from a command the Drobo answers only
    /// sometimes, so a reading can be a few minutes old. It is therefore shown
    /// WITH its age, always -- an old number presented as current would be a
    /// lie, and the whole point of this reading is that you can trust it.
    /// When the device hasn't answered yet, both are simply absent rather than
    /// shown as a dash or a zero.
    /// </summary>
    private static string DeviceExtras(JsonElement device)
    {
        var sb = new System.Text.StringBuilder();

        if (device.TryGetProperty("temperature_c", out var t)
            && t.ValueKind == JsonValueKind.Number)
        {
            sb.Append($"  ·  {t.GetInt32()} °C");
            if (device.TryGetProperty("temperature_at", out var at)
                && at.ValueKind == JsonValueKind.Number)
            {
                var age = DateTimeOffset.UtcNow.ToUnixTimeSeconds() - (long)at.GetDouble();
                sb.Append(age < 90 ? " (just now)"
                        : age < 3600 ? $" ({age / 60}m ago)"
                        : $" ({age / 3600}h ago)");
            }
        }

        if (device.TryGetProperty("uptime_seconds", out var u)
            && u.ValueKind == JsonValueKind.Number)
        {
            var sec = u.GetInt64();
            var d = sec / 86400;
            var h = (sec % 86400) / 3600;
            sb.Append("  ·  up ");
            sb.Append(d > 0 ? $"{d} day{(d == 1 ? "" : "s")}{(h > 0 ? $" {h}h" : "")}"
                    : h > 0 ? $"{h}h"
                    : $"{Math.Max(1, sec / 60)}m");
        }

        return sb.ToString();
    }

    private void Render(JsonElement s)
    {
        // NOT every /api/status payload is a reading, and this method used to
        // assume otherwise. Two ways that went wrong, both found by review on
        // 2026-07-28:
        //
        //  1. CRASH. An agent that has started but not yet finished its first
        //     poll answers {"reachable": false, "error": "no reading taken
        //     yet", ...} with NO device, capacity or drives keys at all. The
        //     unguarded GetProperty("device") below threw KeyNotFoundException,
        //     inside an async void handler, which in WPF means the app simply
        //     disappears. Connecting to a just-started agent is not an exotic
        //     case -- START DROBO DASHBOARD.bat launches both together.
        //
        //  2. LIES. When the Drobo is unreachable the agent sends a Snapshot
        //     built from defaults: zero capacity, blank device, no drives.
        //     Painting those over the panels replaced the last known-good
        //     reading with "0 B / 0 B" and an empty bay list -- which reads as
        //     "your array is empty", not "we could not ask just now".
        //
        // Both are the same mistake. A payload with no reading in it updates
        // the STATE (pill, banners, footer) and leaves the NUMBERS alone,
        // because the last real reading is still the best information we have
        // and stale-but-labelled beats confidently wrong.
        // "reachable" is part of the test, not just key presence: an
        // unreachable poll DOES carry all three keys, filled with a default
        // Snapshot's zeros. Case 2 above is precisely that payload, so a
        // presence check alone would let it through and repaint the panels.
        bool reachableNow = s.TryGetProperty("reachable", out var rr)
                            && rr.ValueKind == JsonValueKind.True;
        // `device` is fetched on its own line rather than inside the && chain:
        // short-circuiting would leave it definitely-unassigned for the code
        // after the guard, which the compiler rightly refuses.
        bool hasDevice = s.TryGetProperty("device", out var device)
                         && device.ValueKind == JsonValueKind.Object;
        bool hasReading = reachableNow && hasDevice
                          && s.TryGetProperty("capacity", out _)
                          && s.TryGetProperty("drives", out _);

        if (!hasReading)
        {
            SetPill("unreachable", Unknown, "#22262E", "#39424D");
            DeviceSub.Text = Str(s, "error", "Waiting for the first reading from the agent.");
            Footer.Text = "No reading at " + DateTime.Now.ToString("HH:mm:ss");
            _lastOverallState = "unreachable";
            ShowNetworkHint(s.TryGetProperty("network_hint", out var nh)
                            && nh.ValueKind == JsonValueKind.Object ? nh : default);
            return;
        }

        DeviceName.Text = Str(device, "name", "Drobo");
        DeviceSub.Text = $"{Str(device, "model", "")}  ·  firmware {Str(device, "firmware", "?")}"
                       + $"  ·  {Str(device, "ip", "")}  ·  {Str(s, "source", "")} driver"
                       + DeviceExtras(device);

        var state = Str(s, "overall_state", "unknown");
        var reachable = s.TryGetProperty("reachable", out var r) && r.GetBoolean();
        state = reachable ? state : "unreachable";
        switch (state)
        {
            case "ok":       SetPill(state, Ok, "#10281B", "#46C07A"); break;
            case "warning":  SetPill(state, Warning, "#2E2311", "#E9A83C"); break;
            case "failed":   SetPill(state, Failed, "#301714", "#E2564B"); break;
            default:         SetPill(state, Unknown, "#22262E", "#39424D"); break;
        }

        // "unreachable" on its own reads as "your NAS has died". Three times
        // during this project's development it meant nothing more than that
        // Windows had auto-joined a different Wi-Fi network, and the array was
        // healthy throughout -- so when the agent can tell that is what
        // happened, say so here rather than let someone go and check on a Drobo
        // that is fine. network_hint is null unless the device is unreachable
        // AND the mismatch actually explains it, so its presence is the whole
        // test; see monitor._network_hint.
        ShowNetworkHint(s.TryGetProperty("network_hint", out var hint)
                        && hint.ValueKind == JsonValueKind.Object ? hint : default);

        // A standing note about the running firmware, for the two builds Drobo
        // withdrew. `notable` is the agent's single decision -- this window
        // never has to know which versions those are.
        ShowFirmwareAdvisory(s.TryGetProperty("firmware_advisory", out var fw)
                             && fw.ValueKind == JsonValueKind.Object ? fw : default);

        // Remembered for the tray icon's tooltip/badge -- see Refresh(),
        // which hands these to TrayIconManager.UpdateStatus once alerts for
        // this same tick are in too.
        _lastDeviceName = DeviceName.Text;
        _lastOverallState = state;

        // capacity
        var cap = s.GetProperty("capacity");
        long used = Long(cap, "used_bytes"), free = Long(cap, "free_bytes");
        long usable = Long(cap, "usable_bytes"), raw = Long(cap, "raw_bytes");
        long protection = Long(cap, "protection_bytes");
        double frac = cap.TryGetProperty("used_fraction", out var f) ? f.GetDouble() : 0;
        CapUsed.Text = Bytes(used);
        CapFree.Text = Bytes(free);
        CapUsable.Text = Bytes(usable);
        CapProt.Text = Bytes(protection);
        CapRaw.Text = Bytes(raw);

        // The device reports its own alert levels (mRedThreshold/mYellowThreshold,
        // as fractions) in pack_health -- prefer those over the hardcoded
        // defaults whenever it has actually told us, same rule monitor.py's
        // own evaluation follows server-side.
        var packHealth = s.TryGetProperty("pack_health", out var ph) && ph.ValueKind == JsonValueKind.Object
            ? ph : default;
        double redThreshold = packHealth.ValueKind == JsonValueKind.Object
            && packHealth.TryGetProperty("red_threshold_fraction", out var rt) && rt.ValueKind == JsonValueKind.Number
            ? rt.GetDouble() : 0.95;
        double yellowThreshold = packHealth.ValueKind == JsonValueKind.Object
            && packHealth.TryGetProperty("yellow_threshold_fraction", out var yt) && yt.ValueKind == JsonValueKind.Number
            ? yt.GetDouble() : 0.85;
        CapBar.Background = frac >= redThreshold ? Failed : frac >= yellowThreshold ? Warning : Accent;
        double filled = Math.Max(0, Math.Min(1.0, frac));
        CapBarFillCol.Width = new GridLength(filled, GridUnitType.Star);
        CapBarRestCol.Width = new GridLength(1 - filled, GridUnitType.Star);

        // Calm by default: this notice only appears when the pack is actually
        // rebuilding or was double-degraded -- see PackHealth's docstring in
        // models.py. Wording matches webui.py's rebuildPanel exactly, so the
        // two front-ends never say something different about the same array.
        long doubleDegraded = packHealth.ValueKind == JsonValueKind.Object ? Long(packHealth, "double_degraded_count") : 0;
        long relayout = packHealth.ValueKind == JsonValueKind.Object ? Long(packHealth, "relayout_count") : 0;
        if (doubleDegraded > 0)
        {
            RebuildNote.Text = $"Double-degraded count is {doubleDegraded} -- two drives were degraded at "
                              + "once; the array's protection may have been exhausted.";
            RebuildBox.Visibility = Visibility.Visible;
        }
        else if (relayout > 0)
        {
            RebuildNote.Text = $"Relayout count is {relayout} -- rebuilding, this is normal after a drive change.";
            RebuildBox.Visibility = Visibility.Visible;
        }
        else
        {
            RebuildBox.Visibility = Visibility.Collapsed;
        }

        // drive bays -- one dense line per bay; the 5N has exactly five bays
        // and the agent reports exactly five (esatm._trim_phantom_slots
        // already strips a phantom sixth), so this never needs to page.
        _bays.Clear();
        foreach (var d in s.GetProperty("drives").EnumerateArray())
        {
            bool present = d.TryGetProperty("present", out var p) && p.GetBoolean();
            int bay = d.TryGetProperty("bay", out var b) ? b.GetInt32() : 0;
            if (!present)
            {
                _bays.Add(new DriveRow
                {
                    BayLabel = "Bay " + bay, ModelSerial = "empty bay", Firmware = "",
                    Size = "", StateText = "empty", StateBrush = Muted, Stripe = Brush("#39424D"),
                });
                continue;
            }
            var st = Str(d, "state", "unknown");
            var brush = st switch { "ok" => Ok, "warning" => Warning, "failed" => Failed, _ => Unknown };
            double? tempC = d.TryGetProperty("temperature_c", out var t) && t.ValueKind == JsonValueKind.Number
                ? t.GetDouble() : (double?)null;
            var tail = tempC.HasValue ? $"{st}  {tempC:0.0}°C" : st;

            var model = CollapseWhitespace(Str(d, "model", "disk"));
            var serial = Str(d, "serial", "");
            var modelSerial = string.IsNullOrEmpty(serial) ? model : $"{model}  ·  {serial}";

            // Noisy extras only when they matter -- a healthy drive shows
            // neither of these, so the array reads clean at a glance and a
            // sick one stands out instead of "0 errors" cluttering every row.
            long errorCount = Long(d, "error_count");
            long ssdLife = d.TryGetProperty("ssd_life_remaining", out var sl) && sl.ValueKind == JsonValueKind.Number
                ? sl.GetInt64() : 100;
            var flagParts = new List<string>();
            if (errorCount > 0) flagParts.Add($"{errorCount} error{(errorCount == 1 ? "" : "s")}");
            if (ssdLife < 100) flagParts.Add($"life {ssdLife}%");
            var flags = string.Join("  ·  ", flagParts);

            _bays.Add(new DriveRow
            {
                BayLabel = "Bay " + bay,
                ModelSerial = modelSerial,
                Firmware = Str(d, "firmware_rev", ""),
                Size = Bytes(Long(d, "capacity_bytes")),
                StateText = tail,
                StateBrush = brush,
                Stripe = brush,
                Flags = flags,
                // Amber for both -- a nonzero error count and reduced SSD life
                // are both "worth a look", not "this drive has failed" (a real
                // failure is StateText/StateBrush's job, from the drive's own
                // reported state). Matches the web dashboard, which colours
                // both the same var(--warn) amber.
                FlagsBrush = Warning,
                FlagsVisibility = flagParts.Count == 0 ? Visibility.Collapsed : Visibility.Visible,
            });
        }

        RenderPerformance(s);
        RenderVolumes(s);

        Footer.Text = "Updated " + DateTime.Now.ToString("HH:mm:ss") + "  ·  polling every 10s";
    }

    /// <summary>
    /// RECENT ACTIVITY on the Status tab. NOT a live throughput reading --
    /// see PerformanceInfo's docstring in models.py and parse_performance's
    /// in nasd/sysinfo.py. The device's own counters lag ~25s in both
    /// directions, on top of the agent only re-asking every 120s
    /// (sysinfo_seconds), so this can read Idle while the array is genuinely
    /// busy, and can keep showing motion for a couple of minutes after
    /// activity actually stopped.
    ///
    /// "measured" is the only thing that distinguishes "never reported" from
    /// "genuinely idle" -- the counters themselves default to 0 either way,
    /// so checking read/write/iops directly for presence would be exactly
    /// the falsy-zero trap this app avoids elsewhere (see SeverityRank's
    /// comment above). Absent "performance" is treated the same as
    /// measured=false: Bool() already returns false for a missing property,
    /// which is the right default here.
    /// </summary>
    /// <summary>
    /// The volumes the pack is carved into — what Windows shows as drive letters.
    ///
    /// <para>
    /// Read from the status greeting, so this costs no extra command and no
    /// extra connection to a device that measurably tires under load.
    /// </para>
    /// <para>
    /// The care here is about not lying with a true number. The device reports
    /// a per-volume maximum of about 70 TB. That is a ceiling the volume may
    /// grow to — not its size, and emphatically not free space. This array
    /// holds under 9 TB usable, so "70 TB" shown alone would read as headroom
    /// that does not exist. It is labelled as a limit and the pack's real
    /// capacity is rendered directly beneath it, always together.
    /// </para>
    /// </summary>
    private void RenderVolumes(JsonElement s)
    {
        _volumes.Clear();

        if (!s.TryGetProperty("volumes", out var vols) || vols.ValueKind != JsonValueKind.Array
            || vols.GetArrayLength() == 0)
        {
            // No volumes reported. Say nothing rather than showing an empty card.
            VolumesCard.Visibility = Visibility.Collapsed;
            return;
        }

        var cap = s.TryGetProperty("capacity", out var c) && c.ValueKind == JsonValueKind.Object
            ? c : default;
        long usable = cap.ValueKind == JsonValueKind.Object ? Long(cap, "usable_bytes") : 0;
        string packHolds = usable > 0 ? "pack holds " + Bytes(usable) : "";

        int i = 0;
        foreach (var v in vols.EnumerateArray())
        {
            long lun = Long(v, "lun");
            string name = Str(v, "name", "");
            if (string.IsNullOrWhiteSpace(name))
                name = "Volume " + (lun + 1);   // the device's own name, or none invented

            var bits = new List<string>();
            // Explicit presence check: a volume reporting zero partitions is
            // not the same as one that reported none, and 0 is falsy.
            if (v.TryGetProperty("partition_count", out var pc) && pc.ValueKind == JsonValueKind.Number)
                bits.Add(pc.GetInt64() == 1 ? "1 partition" : pc.GetInt64() + " partitions");
            string id = Str(v, "unique_id", "");
            if (!string.IsNullOrEmpty(id)) bits.Add("id " + id);

            long ceiling = Long(v, "max_size_bytes");
            _volumes.Add(new VolumeRow
            {
                Name = name,
                Detail = string.Join("  ·  ", bits),
                Ceiling = ceiling > 0 ? "can grow to " + Bytes(ceiling) : "",
                PackHolds = ceiling > 0 ? packHolds : "",
            });
            i++;
        }

        long maxVols = Long(s, "max_volumes");
        var intro = i == 1
            ? "Your Drobo presents its storage as a single volume"
            : $"Your Drobo presents its storage as {i} volumes";
        if (maxVols > 0) intro += $", out of {maxVols} it could hold";
        intro += ". A volume is what appears in Windows as a drive letter; it grows "
               + "as you add drives rather than being a fixed slice.";
        VolumesIntro.Text = intro;

        VolumesCard.Visibility = Visibility.Visible;
    }

    private void RenderPerformance(JsonElement s)
    {
        var perf = s.TryGetProperty("performance", out var pf) && pf.ValueKind == JsonValueKind.Object
            ? pf : default;
        bool measured = perf.ValueKind == JsonValueKind.Object && Bool(perf, "measured");

        if (!measured)
        {
            PerfUnknown.Visibility = Visibility.Visible;
            PerfIdle.Visibility = Visibility.Collapsed;
            PerfActive.Visibility = Visibility.Collapsed;
            return;
        }

        long readMb = Long(perf, "read_mb_per_s");
        long writeMb = Long(perf, "write_mb_per_s");
        long iops = Long(perf, "iops");
        bool allZero = readMb == 0 && writeMb == 0 && iops == 0;

        PerfUnknown.Visibility = Visibility.Collapsed;
        PerfIdle.Visibility = allZero ? Visibility.Visible : Visibility.Collapsed;
        PerfActive.Visibility = allZero ? Visibility.Collapsed : Visibility.Visible;
        if (!allZero)
        {
            PerfRead.Text = $"{readMb} MB/s";
            PerfWrite.Text = $"{writeMb} MB/s";
            PerfIops.Text = iops.ToString(System.Globalization.CultureInfo.InvariantCulture);
        }
    }

    private void SetPill(string text, Brush fg, string bg, string border)
    {
        StateText.Text = text;
        StateText.Foreground = fg;
        StatePill.Background = Brush(bg);
        StatePill.BorderBrush = Brush(border);
    }

    // ---------------------------------------------------------------
    // Settings tab: read-only view of the Drobo's own network configuration
    // ---------------------------------------------------------------

    private async void SettingsRefreshBtn_Click(object sender, RoutedEventArgs e) => await RefreshSettingsConfig();

    /// <summary>
    /// Fetches /api/droboconfig?section=network on demand -- never on the
    /// 10s poll timer. This opens a second connection to the command port,
    /// and that port has been measured to stop answering for a while after
    /// heavy use (see docs/command-surface.md), while network configuration
    /// itself almost never changes. So the owner asks for it, explicitly,
    /// only when they actually want to look at it.
    /// </summary>
    private async Task RefreshSettingsConfig()
    {
        SettingsRefreshBtn.IsEnabled = false;
        SettingsRefreshBtn.Content = "Reading…";
        SettingsError.Visibility = Visibility.Collapsed;
        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Get, BaseUrl + "/api/droboconfig?section=network");
            req.Headers.Add("X-Agent-Token", Token.Password);
            using var resp = await _http.SendAsync(req);
            var json = await resp.Content.ReadAsStringAsync();
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;

            if (!resp.IsSuccessStatusCode)
            {
                // The agent already writes a plain-English reason into "error"
                // for exactly this case -- no live Drobo connection yet, or the
                // command port refused/timed out (502/503). Show that instead
                // of a bare HTTP status code.
                SettingsError.Text = Str(root, "error", $"The agent returned HTTP {(int)resp.StatusCode}.");
                SettingsError.Visibility = Visibility.Visible;
                return;
            }

            var net = root.TryGetProperty("config", out var cfgEl)
                      && cfgEl.TryGetProperty("DRINASConfig", out var innerEl)
                      && innerEl.TryGetProperty("DRINasNetworkConfig", out var netEl)
                ? netEl : default;

            if (net.ValueKind != JsonValueKind.Object)
            {
                // The device answered but with nothing usable this time -- the
                // same ambiguous "empty" reply docs/command-surface.md
                // describes for other commands. Not an error, just try again.
                SettingsError.Text = "The Drobo answered, but without any network configuration this time. Try Refresh again.";
                SettingsError.Visibility = Visibility.Visible;
                return;
            }

            var ip = net.TryGetProperty("IPConfig", out var ipEl) && ipEl.ValueKind == JsonValueKind.Object ? ipEl : default;
            var jumbo = net.TryGetProperty("JumboFramesConfig", out var jEl) && jEl.ValueKind == JsonValueKind.Object ? jEl : default;

            SettingsDeviceName.Text = DashIfEmpty(Str(net, "NasName", ""));
            SettingsWorkgroup.Text = DashIfEmpty(Str(net, "NasWorkgroup", ""));
            SettingsIp.Text = DashIfEmpty(ip.ValueKind == JsonValueKind.Object ? Str(ip, "IP", "") : "");
            SettingsSubnet.Text = DashIfEmpty(ip.ValueKind == JsonValueKind.Object ? Str(ip, "Subnet", "") : "");
            SettingsGateway.Text = DashIfEmpty(ip.ValueKind == JsonValueKind.Object ? Str(ip, "Gateway", "") : "");

            var dns = ip.ValueKind == JsonValueKind.Object
                ? string.Join(", ", new[] { Str(ip, "DNS1", ""), Str(ip, "DNS2", "") }.Where(v => !string.IsNullOrEmpty(v)))
                : "";
            SettingsDns.Text = DashIfEmpty(dns);

            var mtu = jumbo.ValueKind == JsonValueKind.Object ? Str(jumbo, "MTUSize", "") : "";
            bool jumboOn = jumbo.ValueKind == JsonValueKind.Object && Str(jumbo, "Enabled", "0") == "1";
            SettingsJumbo.Text = string.IsNullOrEmpty(mtu) ? "—" : $"{(jumboOn ? "On" : "Off")}  ·  MTU {mtu}";

            var speed = Str(net, "PortSpeed", "");
            var duplex = Str(net, "PortDuplex", "");
            SettingsPort.Text = string.IsNullOrEmpty(speed) ? "—"
                : $"{speed} Mbit/s" + (string.IsNullOrEmpty(duplex) ? "" : $", {duplex} duplex");

            SettingsEmpty.Visibility = Visibility.Collapsed;
            SettingsGrid.Visibility = Visibility.Visible;
        }
        catch (Exception ex)
        {
            SettingsError.Text = "Could not reach the agent.  (" + ex.Message + ")";
            SettingsError.Visibility = Visibility.Visible;
        }
        finally
        {
            SettingsRefreshBtn.IsEnabled = true;
            SettingsRefreshBtn.Content = "Refresh";
        }
    }

    private static string DashIfEmpty(string s) => string.IsNullOrEmpty(s) ? "—" : s;

    // ---------------------------------------------------------------
    // Tools tab: version info, and the few actions that are safe and
    // actually work in this read-only phase
    // ---------------------------------------------------------------

    private string? _guidePath;

    /// <summary>Set while InitToolsTab is programmatically selecting a
    /// DiagnosticsPrefCombo item, so DiagnosticsPrefCombo_SelectionChanged
    /// knows not to treat that as the owner changing the preference and
    /// re-save the same value it just loaded.</summary>
    private bool _initializingDiagnosticsPref;

    /// <summary>Runs once at startup -- the app's own version doesn't depend on
    /// being connected to anything, so there's no reason to wait for that.</summary>
    private void InitToolsTab()
    {
        var version = Assembly.GetExecutingAssembly()
            .GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion;
        if (string.IsNullOrEmpty(version))
            version = Assembly.GetExecutingAssembly().GetName().Version?.ToString(3) ?? "unknown";
        // The SDK appends a "+<git-sha>" build-metadata suffix automatically
        // (source-revision stamping) -- plumbing detail, not something the
        // owner needs on a version line. Keep just the x.y.z the csproj declares.
        var plus = version.IndexOf('+');
        if (plus >= 0) version = version[..plus];
        ToolsAppVersion.Text = $"Drobo Dashboard REBORN  v{version}";

        _guidePath = FindGuidePath();
        OpenGuideBtn.Visibility = _guidePath is null ? Visibility.Collapsed : Visibility.Visible;

        _initializingDiagnosticsPref = true;
        var pref = DroboSettings.LoadAll().DiagnosticsPreference;
        DiagnosticsPrefCombo.SelectedIndex = pref switch
        {
            DroboSettings.DiagnosticsPreference.Clipboard => 1,
            DroboSettings.DiagnosticsPreference.File => 2,
            _ => 0,
        };
        _initializingDiagnosticsPref = false;
    }

    /// <summary>The "When copying diagnostics:" dropdown next to the button -- lets
    /// the owner change their mind later without hunting down settings.json
    /// themselves, same as ticking "Remember my choice" does the first time.</summary>
    private void DiagnosticsPrefCombo_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_initializingDiagnosticsPref) return;
        var pref = DiagnosticsPrefCombo.SelectedIndex switch
        {
            1 => DroboSettings.DiagnosticsPreference.Clipboard,
            2 => DroboSettings.DiagnosticsPreference.File,
            _ => DroboSettings.DiagnosticsPreference.Ask,
        };
        DroboSettings.SetDiagnosticsPreference(pref);
    }

    /// <summary>
    /// Where docs/accessing-your-files.md lives relative to wherever this app
    /// happens to be running from -- the same walk-up-from-the-exe trick
    /// CandidateConfigPaths uses for the agent's config.json, since a dev
    /// checkout and an installed copy don't put the exe at the same depth
    /// under the repo root. Missing is not an error: the button just hides,
    /// rather than opening to a "file not found" surprise.
    /// </summary>
    private static string? FindGuidePath()
    {
        var here = AppContext.BaseDirectory;
        var dir = new DirectoryInfo(here);
        for (var i = 0; i < 6 && dir is not null; i++, dir = dir.Parent)
        {
            var candidate = Path.Combine(dir.FullName, "docs", "accessing-your-files.md");
            if (File.Exists(candidate)) return candidate;
        }
        return null;
    }

    /// <summary>
    /// TOOLS is the 5th tab (index 4: STATUS, ALERTS, SHARES, SETTINGS, TOOLS)
    /// -- same hardcoded-index convention GoToShares_Click already uses for
    /// SHARES. Refreshed on every visit, not just once at connect: /api/ping
    /// needs no token and costs the agent nothing, so there's no reason for
    /// the uptime figure to sit stale the way the network config deliberately does.
    /// </summary>
    private async void MainTabs_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (ReferenceEquals(MainTabs.SelectedItem, TabTools)) await RefreshToolsInfo();
        // The PREFERENCES panels read agent-side state (is email configured, is
        // update checking on), so they are refreshed when the tab is opened
        // rather than polled -- neither changes without somebody changing it.
        if (ReferenceEquals(MainTabs.SelectedItem, TabSettings)) await RefreshPreferencePanels();
    }

    private async Task RefreshToolsInfo()
    {
        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Get, BaseUrl + "/api/ping");
            using var resp = await _http.SendAsync(req);
            resp.EnsureSuccessStatusCode();
            var json = await resp.Content.ReadAsStringAsync();
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            var agentVersion = Str(root, "version", "?");
            var uptime = Long(root, "uptime_seconds");
            ToolsAgentVersion.Text = $"Agent v{agentVersion}  ·  up {FormatUptime(uptime)}";
        }
        catch (Exception ex)
        {
            ToolsAgentVersion.Text = "Agent: unreachable  (" + ex.Message + ")";
        }
    }

    private static string FormatUptime(long sec)
    {
        if (sec <= 0) return "just started";
        var d = sec / 86400;
        var h = (sec % 86400) / 3600;
        var m = (sec % 3600) / 60;
        if (d > 0) return $"{d} day{(d == 1 ? "" : "s")}" + (h > 0 ? $" {h}h" : "");
        if (h > 0) return $"{h}h" + (m > 0 ? $" {m}m" : "");
        return $"{Math.Max(1, m)}m";
    }

    private void OpenWebDashboard_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            Process.Start(new ProcessStartInfo(BaseUrl) { UseShellExecute = true });
        }
        catch (Exception ex)
        {
            MessageBox.Show(this, "Could not open the web dashboard: " + ex.Message,
                "Drobo Dashboard REBORN", MessageBoxButton.OK, MessageBoxImage.Warning);
        }
    }

    /// <summary>
    /// Sends the current /api/status JSON either to the clipboard (raw, exactly
    /// as the agent returns it) or to a file (reformatted as readable text --
    /// see DiagnosticsReport). This is the one Tools action that touches
    /// something sensitive -- the payload includes drive serial numbers --
    /// which is why the tab also carries a permanent warning about that, not
    /// just this one-off status line, and why the warning is repeated inside
    /// the saved file itself: a file can travel somewhere the on-screen
    /// warning never will.
    ///
    /// The owner asked, in their own words, to be asked once ("copy to
    /// clipboard or save to a file") with a way to make it stick so they don't
    /// get nagged every time -- DroboSettings.DiagnosticsPreference is that
    /// stored answer, changeable afterwards from the combo box added next to
    /// this button. It lives here on the Tools tab,
    /// right next to the button it configures, so the setting and its
    /// effect are visible in one glance.
    /// </summary>
    private async void CopyDiagnostics_Click(object sender, RoutedEventArgs e)
    {
        var pref = DroboSettings.LoadAll().DiagnosticsPreference;

        if (pref == DroboSettings.DiagnosticsPreference.Ask)
        {
            var dialog = new DiagnosticsChoiceDialog { Owner = this };
            var answered = dialog.ShowDialog();
            if (answered != true || dialog.Result == DiagnosticsChoiceDialog.Choice.Cancelled)
                return;

            pref = dialog.Result == DiagnosticsChoiceDialog.Choice.File
                ? DroboSettings.DiagnosticsPreference.File
                : DroboSettings.DiagnosticsPreference.Clipboard;

            if (dialog.RememberChoice)
            {
                DroboSettings.SetDiagnosticsPreference(pref);
                _initializingDiagnosticsPref = true;
                DiagnosticsPrefCombo.SelectedIndex = pref == DroboSettings.DiagnosticsPreference.File ? 2 : 1;
                _initializingDiagnosticsPref = false;
            }
        }

        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Get, BaseUrl + "/api/status");
            req.Headers.Add("X-Agent-Token", Token.Password);
            using var resp = await _http.SendAsync(req);
            resp.EnsureSuccessStatusCode();
            var json = await resp.Content.ReadAsStringAsync();

            if (pref == DroboSettings.DiagnosticsPreference.File)
                SaveDiagnosticsToFile(json);
            else
            {
                Clipboard.SetText(json);
                ToolsActionStatus.Text = "Copied to the clipboard. It includes your drive serial numbers -- check where you paste it.";
            }
        }
        catch (Exception ex)
        {
            ToolsActionStatus.Text = "Could not copy diagnostics: " + ex.Message;
        }
        ToolsActionStatus.Visibility = Visibility.Visible;
    }

    /// <summary>Standard Windows save dialog defaulting to Documents, then writes
    /// DiagnosticsReport's human-readable text (raw JSON included at the end) --
    /// not the JSON alone, since a file is meant to be read by a person, possibly
    /// days later, possibly not by whoever clicked the button.</summary>
    private void SaveDiagnosticsToFile(string json)
    {
        var dialog = new SaveFileDialog
        {
            FileName = $"drobo-diagnostics-{DateTime.Now:yyyy-MM-dd-HHmm}.txt",
            DefaultExt = ".txt",
            Filter = "Text file (*.txt)|*.txt|All files (*.*)|*.*",
            InitialDirectory = Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),
        };
        if (dialog.ShowDialog(this) != true)
            return; // owner cancelled -- leave ToolsActionStatus as it was, nothing happened

        File.WriteAllText(dialog.FileName, DiagnosticsReport.BuildText(json));
        ToolsActionStatus.Text = "Saved to " + dialog.FileName + ". It includes your drive serial numbers -- check who you send it to.";
    }

    /// <summary>
    /// Renders docs/accessing-your-files.md to a styled HTML page (see
    /// MarkdownGuide) and opens that instead of the raw .md file -- Notepad,
    /// this machine's shell default for .md, showed the owner a wall of
    /// unrendered "#" and "**", which is a poor way to hand a non-expert a
    /// help document. .html's own shell default is a browser, so UseShellExecute
    /// here gets us there without hardcoding a browser path.
    /// </summary>
    private void OpenGuide_Click(object sender, RoutedEventArgs e)
    {
        if (_guidePath is null) return;
        try
        {
            var htmlPath = MarkdownGuide.ConvertFileToHtml(_guidePath);
            Process.Start(new ProcessStartInfo(htmlPath) { UseShellExecute = true });
        }
        catch (Exception ex)
        {
            MessageBox.Show(this, "Could not open the guide: " + ex.Message,
                "Drobo Dashboard REBORN", MessageBoxButton.OK, MessageBoxImage.Warning);
        }
    }

    // ---------------------------------------------------------------
    // Tools tab: Identify -- the first control in this app's history that
    // reaches out and changes something on the Drobo itself, rather than
    // just reading it
    // ---------------------------------------------------------------

    /// <summary>
    /// POST /api/identify -- flashes the Drobo's front lights so the owner can
    /// tell which physical box is which. Deliberately a POST, never a GET: a
    /// GET must never carry a side effect (a browser or link-prefetcher would
    /// happily fire one), and this is the one endpoint in the whole app that
    /// changes anything on the hardware. Sent as HttpMethod.Post via
    /// SendAsync rather than the bare PostAsync(url, content) overload -- see
    /// the inline comment below for why.
    ///
    /// Still genuinely low-stakes -- it only blinks some lights and stops on
    /// its own -- so this doesn't dress it up as scary: no confirmation
    /// dialog, no red styling, just the same PrimaryButton look as an
    /// ordinary "do a thing" action elsewhere in the app. What it does get is
    /// weight appropriate to being the only write in the app: a plain-English
    /// line explaining the effect BEFORE the click (see the XAML, shown
    /// unconditionally, not just on hover/tooltip), the button disabled for
    /// the duration of the request so it can't be double-fired, and an honest
    /// result afterwards -- the agent's own message text, never a guess, with
    /// a SIMULATED chip whenever "simulated" comes back true so a demo blink
    /// can never be mistaken for the real thing.
    /// </summary>
    // ---------------------------------------------------------------
    // Backing up to an external drive
    // ---------------------------------------------------------------

    /// <summary>Polls /api/backup/status while a backup runs. Null when idle.</summary>
    private DispatcherTimer? _backupPoll;

    /// <summary>
    /// True only once the agent has PLANNED this exact source/destination/mirror
    /// combination and allowed it.
    ///
    /// This is the UI half of the agent's two-step gate: /api/backup/start
    /// additionally demands confirm:true, but the button should not even be
    /// clickable until the agent has agreed, so the refusals are read before
    /// anything is written rather than after. Any edit to the paths or the
    /// mirror box clears it, because a plan for different inputs says nothing
    /// about these ones.
    /// </summary>
    private bool _backupPlanApproved;

    private void BackupPathChanged(object sender, RoutedEventArgs e)
    {
        // Changing anything invalidates the previous plan. Without this you
        // could check a safe destination, then edit the box to something the
        // agent would refuse, and still have an enabled Start button.
        _backupPlanApproved = false;
        if (BackupStartBtn is not null) BackupStartBtn.IsEnabled = false;
        if (BackupPlanBox is not null) BackupPlanBox.Visibility = Visibility.Collapsed;
    }

    private void BackupPathChanged(object sender, TextChangedEventArgs e) =>
        BackupPathChanged(sender, (RoutedEventArgs)e);

    /// <summary>
    /// Offer the Drobo's own shares as the source, rather than making somebody
    /// remember UNC syntax. Falls back to a plain prompt if the share list has
    /// not loaded -- the box is editable either way.
    /// </summary>
    private void BackupPickSource_Click(object sender, RoutedEventArgs e)
    {
        if (_shares.Count == 0)
        {
            MessageBox.Show(this,
                "No shares have loaded yet. Open the Shares tab first, or type the "
                + "folder yourself -- for example \\\\10.0.0.50\\Photos.",
                "Drobo Dashboard REBORN", MessageBoxButton.OK, MessageBoxImage.Information);
            return;
        }

        // A share already mapped to a drive letter copies faster and avoids a
        // second authentication, so prefer that path when there is one.
        var picker = new System.Windows.Controls.ContextMenu();
        foreach (var share in _shares)
        {
            var path = string.IsNullOrEmpty(share.DriveLetter) ? share.Unc : share.DriveLetter + "\\";
            var item = new System.Windows.Controls.MenuItem { Header = $"{share.Name}   ({path})" };
            var chosen = path;
            item.Click += (_, _) => { BackupSource.Text = chosen; };
            picker.Items.Add(item);
        }
        picker.PlacementTarget = (UIElement)sender;
        picker.IsOpen = true;
    }

    private void BackupPickDest_Click(object sender, RoutedEventArgs e)
    {
        // WinForms' folder browser, available because UseWindowsForms is already
        // on for the tray icon. WPF has no folder picker of its own.
        using var dialog = new System.Windows.Forms.FolderBrowserDialog
        {
            Description = "Choose a folder on your external drive",
            UseDescriptionForTitle = true,
            ShowNewFolderButton = true,
        };
        if (dialog.ShowDialog() == System.Windows.Forms.DialogResult.OK)
            BackupDest.Text = dialog.SelectedPath;
    }

    /// <summary>
    /// Ask the agent what a backup WOULD do. Copies nothing.
    ///
    /// Its refusals and warnings are shown verbatim rather than reworded here:
    /// the agent is the thing that decides, so it should be the thing that
    /// explains, and two descriptions of the same rule would eventually
    /// disagree.
    /// </summary>
    private async void BackupCheck_Click(object sender, RoutedEventArgs e)
    {
        BackupCheckBtn.IsEnabled = false;
        try
        {
            var body = JsonSerializer.Serialize(new
            {
                source = BackupSource.Text.Trim(),
                destination = BackupDest.Text.Trim(),
                mirror = BackupMirror.IsChecked == true,
            });
            var root = await PostJson("/api/backup/plan", body);

            bool allowed = root.TryGetProperty("allowed", out var a)
                           && a.ValueKind == JsonValueKind.True;
            var lines = new List<string>();
            foreach (var key in new[] { "refusals", "warnings" })
            {
                if (root.TryGetProperty(key, out var arr) && arr.ValueKind == JsonValueKind.Array)
                    foreach (var item in arr.EnumerateArray())
                        lines.Add(item.GetString() ?? "");
            }

            if (lines.Count > 0)
            {
                BackupPlanText.Text = string.Join("\n\n", lines.Where(l => l.Length > 0));
                BackupPlanBox.Visibility = Visibility.Visible;
            }
            else
            {
                BackupPlanBox.Visibility = Visibility.Collapsed;
            }

            _backupPlanApproved = allowed;
            BackupStartBtn.IsEnabled = allowed;
            BackupStatus.Text = allowed
                ? "Checked. Nothing has been copied yet -- press Start backup to run it."
                : "This cannot run as set up. See above.";
        }
        catch (Exception ex)
        {
            BackupPlanBox.Visibility = Visibility.Collapsed;
            BackupStatus.Text = "Could not ask the agent: " + ex.Message;
            _backupPlanApproved = false;
            BackupStartBtn.IsEnabled = false;
        }
        finally
        {
            BackupCheckBtn.IsEnabled = true;
        }
    }

    private async void BackupStart_Click(object sender, RoutedEventArgs e)
    {
        // Belt and braces: the button should be disabled, but a stale click
        // queued before an edit must not slip through either.
        if (!_backupPlanApproved) return;

        // Mirroring deletes. A confirmation dialog for an ordinary copy would
        // be noise, but for this one it is the last chance to notice.
        if (BackupMirror.IsChecked == true)
        {
            var answer = MessageBox.Show(this,
                "Mirror is on.\n\nAnything in the backup folder that is not on the Drobo "
                + "will be DELETED from the backup, including files you may have deleted "
                + "from the Drobo by mistake.\n\nRun the backup with mirroring on?",
                "Drobo Dashboard REBORN", MessageBoxButton.YesNo, MessageBoxImage.Warning,
                MessageBoxResult.No);
            if (answer != MessageBoxResult.Yes) return;
        }

        BackupStartBtn.IsEnabled = false;
        BackupCheckBtn.IsEnabled = false;
        try
        {
            var body = JsonSerializer.Serialize(new
            {
                source = BackupSource.Text.Trim(),
                destination = BackupDest.Text.Trim(),
                mirror = BackupMirror.IsChecked == true,
                confirm = true,
            });
            var root = await PostJson("/api/backup/start", body);
            if (root.TryGetProperty("error", out var err))
            {
                BackupStatus.Text = err.GetString() ?? "The agent refused to start it.";
                BackupCheckBtn.IsEnabled = true;
                return;
            }
            BackupStatus.Text = "Backing up...";
            BackupBar.Visibility = Visibility.Visible;
            BackupTail.Visibility = Visibility.Visible;
            StartBackupPolling();
        }
        catch (Exception ex)
        {
            BackupStatus.Text = "Could not start the backup: " + ex.Message;
            BackupCheckBtn.IsEnabled = true;
        }
    }

    /// <summary>
    /// Poll the agent while a copy runs.
    ///
    /// Two seconds rather than the dashboard's ten: this is the one screen
    /// where somebody is actively watching, and a copy of a full array can run
    /// for hours, so it needs to look alive. The timer stops itself the moment
    /// the job finishes -- a poll loop that outlives its job is how you end up
    /// hammering an endpoint forever.
    /// </summary>
    private void StartBackupPolling()
    {
        _backupPoll?.Stop();
        _backupPoll = new DispatcherTimer { Interval = TimeSpan.FromSeconds(2) };
        _backupPoll.Tick += async (_, _) => await PollBackupOnce();
        _backupPoll.Start();
    }

    private async Task PollBackupOnce()
    {
        try
        {
            var root = await FetchJson("/api/backup/status");
            bool running = root.TryGetProperty("running", out var r)
                           && r.ValueKind == JsonValueKind.True;

            if (root.TryGetProperty("tail", out var tail) && tail.ValueKind == JsonValueKind.Array)
            {
                // Last few lines only. Robocopy is chatty and the point here is
                // "it is still moving", not a full transcript.
                var recent = tail.EnumerateArray()
                                 .Select(x => x.GetString() ?? "")
                                 .Where(x => x.Length > 0)
                                 .TakeLast(4);
                BackupTail.Text = string.Join("\n", recent);
            }

            if (running)
            {
                BackupStatus.Text = "Backing up...";
                return;
            }

            _backupPoll?.Stop();
            _backupPoll = null;
            BackupBar.Visibility = Visibility.Collapsed;
            BackupCheckBtn.IsEnabled = true;

            bool ok = root.TryGetProperty("ok", out var o) && o.ValueKind == JsonValueKind.True;
            var summary = Str(root, "summary", "");
            var error = Str(root, "error", "");
            BackupStatus.Text = (ok ? "Finished. " : "Finished with problems. ")
                                + summary + (error.Length > 0 ? "  " + error : "");
        }
        catch (Exception ex)
        {
            _backupPoll?.Stop();
            _backupPoll = null;
            BackupBar.Visibility = Visibility.Collapsed;
            BackupCheckBtn.IsEnabled = true;
            BackupStatus.Text = "Lost contact with the agent while backing up: " + ex.Message
                                + "  The copy may still be running -- press Check to look again.";
        }
    }

    /// <summary>POST a JSON body and return the parsed response.</summary>
    private async Task<JsonElement> PostJson(string path, string body)
    {
        using var req = new HttpRequestMessage(HttpMethod.Post, BaseUrl + path)
        {
            Content = new StringContent(body, System.Text.Encoding.UTF8, "application/json"),
        };
        req.Headers.Add("X-Agent-Token", Token.Password);
        using var resp = await _http.SendAsync(req);
        var json = await resp.Content.ReadAsStringAsync();
        using var doc = JsonDocument.Parse(json);
        return doc.RootElement.Clone();
    }

    /// <summary>
    /// Read the LED brightness the Drobo reports, and whether it is in
    /// shop-display mode.
    ///
    /// On demand rather than polled: neither changes unless somebody changes
    /// it, and each read costs a connection to a command port that stops
    /// answering under sustained use.
    ///
    /// The number is shown exactly as the device sends it. The firmware names
    /// eCmdGetDimming and gives its id but documents nothing about the scale,
    /// so rendering 59 as "59%" would be inventing a unit -- the agent ships a
    /// plain-English note saying so, and it is displayed rather than dropped.
    /// </summary>
    private async void ReadLeds_Click(object sender, RoutedEventArgs e)
    {
        ReadLedsBtn.IsEnabled = false;
        DimmingNote.Text = "Asking the Drobo…";
        try
        {
            var root = await FetchJson("/api/leds");

            if (root.TryGetProperty("dimming", out var d) && d.ValueKind == JsonValueKind.Number)
            {
                DimmingValue.Text = d.GetInt32().ToString();
                DimmingNote.Text = Str(root, "scale_note", "");
            }
            else
            {
                DimmingValue.Text = "—";
                DimmingNote.Text = Str(root, "error",
                    "The Drobo didn't report a brightness this time. It sometimes "
                    + "stops answering this command for a while; try again shortly.");
            }

            // Demo mode is shown ONLY on an explicit yes. Absent means the
            // device didn't say, which is not the same as "no" -- and a warning
            // strip appearing because of a missing field would be worse than
            // useless on an ordinary array.
            // THREE states, not two. The agent sends true, false, or null --
            // and null means "the device did not tell us", which is NOT the
            // same as "no".
            //
            // This used to be a plain if/else on `== True`, so an unknown
            // answer took the else branch and silently retracted a demo-mode
            // warning that a previous read had put up on measured grounds.
            // Demo mode means the capacity figures are FABRICATED, so with-
            // drawing that warning because a command timed out tells the owner
            // their numbers are real when nothing established that. Unknown
            // now leaves whatever is on screen exactly as it is.
            bool haveAnswer = root.TryGetProperty("demo_mode", out var dm)
                              && (dm.ValueKind == JsonValueKind.True
                                  || dm.ValueKind == JsonValueKind.False);
            if (!haveAnswer) return;   // no measurement, no change

            bool inDemo = dm.ValueKind == JsonValueKind.True;
            if (inDemo)
            {
                var scale = root.TryGetProperty("scale_factor", out var sf)
                            && sf.ValueKind == JsonValueKind.Number
                    ? $" (scale factor {sf.GetInt32()})" : "";
                DemoModeText.Text =
                    "This Drobo is in shop-display (demo) mode" + scale + ". In that mode it "
                    + "reports invented capacity rather than what the drives actually hold, so "
                    + "the capacity figures in this app are not real until it is turned off.";
                DemoModeBox.Visibility = Visibility.Visible;
            }
            else
            {
                DemoModeBox.Visibility = Visibility.Collapsed;
            }
        }
        catch (Exception ex)
        {
            DimmingValue.Text = "—";
            DimmingNote.Text = "Could not ask the agent: " + ex.Message;
        }
        finally
        {
            ReadLedsBtn.IsEnabled = true;
        }
    }

    private async void IdentifyBtn_Click(object sender, RoutedEventArgs e) =>
        await SendIdentify(query: "", busyLabel: "Identifying…");

    /// <summary>
    /// Stop a blink that is already running, by sending interval 0.
    ///
    /// Identify is a MODE, not a pulse -- Drobo Dashboard's own warning says
    /// the lights blink continuously for 15 minutes, and the same button stops
    /// them. A control that starts something lasting and offers no way out is
    /// the kind of thing people don't press twice.
    ///
    /// The stop value itself is still a GUESS: no capture shows the off press,
    /// so 0 is inference from the UI being a single toggle driven by one
    /// command. It is a safe guess -- zero is exactly what an ABSENT interval
    /// already behaved like for months, so it cannot do worse than the empty
    /// Params this module used to send.
    /// </summary>
    private async void IdentifyStopBtn_Click(object sender, RoutedEventArgs e) =>
        await SendIdentify(query: "?interval=0", busyLabel: "Stopping…");

    /// <summary>
    /// The one POST both buttons make. Shared so start and stop cannot drift
    /// apart in their error handling, which is where this kind of pair usually
    /// rots -- one of them grows a fix the other never gets.
    /// </summary>
    private async Task SendIdentify(string query, string busyLabel)
    {
        // Both buttons go down together. They hit the same command port, which
        // measurably dislikes connection volume, and "Stop" racing "Identify"
        // would produce an order nobody chose.
        var startLabel = IdentifyBtn.Content;
        var stopLabel = IdentifyStopBtn.Content;
        IdentifyBtn.IsEnabled = false;
        IdentifyStopBtn.IsEnabled = false;
        if (query.Length == 0) { IdentifyBtn.Content = busyLabel; }
        else { IdentifyStopBtn.Content = busyLabel; }
        IdentifyResultBox.Visibility = Visibility.Collapsed;
        try
        {
            // A real POST, built as its own HttpRequestMessage (same
            // convention every other call in this file uses) rather than a
            // bare _http.PostAsync(url, content) -- that overload has nowhere
            // to hang the X-Agent-Token header without mutating _http's
            // shared DefaultRequestHeaders, which the 10s poll timer and this
            // click could then race on. HttpMethod.Post + SendAsync is what
            // actually goes out on the wire as POST; there is no GetAsync
            // anywhere on this path.
            using var req = new HttpRequestMessage(HttpMethod.Post, BaseUrl + "/api/identify" + query);
            req.Headers.Add("X-Agent-Token", Token.Password);
            using var resp = await _http.SendAsync(req);
            var json = await resp.Content.ReadAsStringAsync();
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;

            if (!resp.IsSuccessStatusCode)
            {
                // The agent already writes a plain-English reason into "error"
                // for both failure cases it can hit here -- no live Drobo
                // connection yet (503), or the device refused/didn't answer
                // the command (502). Show that instead of a bare status code.
                ShowIdentifyResult(Str(root, "error", $"The agent returned HTTP {(int)resp.StatusCode}."),
                    ok: false, simulated: false);
                return;
            }

            // Bool(), not a truthy/falsy read -- "simulated" is a real boolean
            // from the agent and false is exactly as meaningful as true here.
            ShowIdentifyResult(Str(root, "message", "Sent."), ok: true, simulated: Bool(root, "simulated"));
        }
        catch (Exception ex)
        {
            ShowIdentifyResult("Could not reach the agent.  (" + ex.Message + ")", ok: false, simulated: false);
        }
        finally
        {
            IdentifyBtn.IsEnabled = true;
            IdentifyStopBtn.IsEnabled = true;
            IdentifyBtn.Content = startLabel;
            IdentifyStopBtn.Content = stopLabel;
        }
    }

    private void ShowIdentifyResult(string text, bool ok, bool simulated)
    {
        IdentifyResultText.Text = text;
        IdentifyResultText.Foreground = ok ? Ok : Warning;
        IdentifySimulatedBadge.Visibility = ok && simulated ? Visibility.Visible : Visibility.Collapsed;
        IdentifyResultBox.Visibility = Visibility.Visible;
    }

    // ---------------------------------------------------------------
    // Tools tab: DroboApps -- software installed ON the Drobo (Python, SSH,
    // DroboPix), not the drives or shares covered elsewhere
    // ---------------------------------------------------------------

    private async void DroboAppsRefreshBtn_Click(object sender, RoutedEventArgs e) => await RefreshDroboApps();

    /// <summary>
    /// GET /api/droboapps on demand -- never on the 10s poll. Same reasoning
    /// as RefreshSettingsConfig: this is a second connection to the command
    /// port, which has been measured to stop answering for a while after
    /// heavy use, while the set of installed DroboApps changes about never.
    /// </summary>
    private async Task RefreshDroboApps()
    {
        DroboAppsRefreshBtn.IsEnabled = false;
        DroboAppsRefreshBtn.Content = "Reading…";
        DroboAppsError.Visibility = Visibility.Collapsed;
        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Get, BaseUrl + "/api/droboapps");
            req.Headers.Add("X-Agent-Token", Token.Password);
            using var resp = await _http.SendAsync(req);
            var json = await resp.Content.ReadAsStringAsync();
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;

            if (!resp.IsSuccessStatusCode)
            {
                // Covers both failure shapes the agent can return here: no
                // live Drobo connection yet (503, plain {"error": ...}, no
                // "apps" key at all) and a command-port failure once
                // connected (502, {"error": ..., "apps": []}). Either way,
                // show the agent's own reason and clear the list rather than
                // leaving a stale one on screen looking current.
                DroboAppsError.Text = Str(root, "error", $"The agent returned HTTP {(int)resp.StatusCode}.");
                DroboAppsError.Visibility = Visibility.Visible;
                _droboApps.Clear();
                DroboAppsSdk.Text = "";
                DroboAppsEmpty.Text = "Press Refresh to see what's installed on your Drobo.";
                DroboAppsEmpty.Visibility = Visibility.Visible;
                return;
            }

            RenderDroboApps(root);
        }
        catch (Exception ex)
        {
            DroboAppsError.Text = "Could not reach the agent.  (" + ex.Message + ")";
            DroboAppsError.Visibility = Visibility.Visible;
            _droboApps.Clear();
            DroboAppsSdk.Text = "";
            DroboAppsEmpty.Text = "Press Refresh to see what's installed on your Drobo.";
            DroboAppsEmpty.Visibility = Visibility.Visible;
        }
        finally
        {
            DroboAppsRefreshBtn.IsEnabled = true;
            DroboAppsRefreshBtn.Content = "Refresh";
        }
    }

    /// <summary>
    /// Builds the DroboApps list from a successful /api/droboapps response.
    /// "running" and "has_web_ui" are already the plain booleans a person
    /// would say out loud -- the agent flips the device's own inverted
    /// "Stopped" field before this ever sees it (see droboapps.py's
    /// parse_droboapps) -- so this reads them straight with Bool(), no
    /// re-inverting and no falsy-default shortcuts.
    /// </summary>
    private void RenderDroboApps(JsonElement root)
    {
        var sdk = Str(root, "sdk_version", "");
        DroboAppsSdk.Text = string.IsNullOrEmpty(sdk) ? "" : $"SDK {sdk}";

        var list = new List<DroboAppRow>();
        if (root.TryGetProperty("apps", out var arr) && arr.ValueKind == JsonValueKind.Array)
        {
            foreach (var a in arr.EnumerateArray())
            {
                bool running = Bool(a, "running");
                bool hasWebUi = Bool(a, "has_web_ui");
                var status = Str(a, "status", "");
                list.Add(new DroboAppRow
                {
                    Name = Str(a, "name", ""),
                    Version = Str(a, "version", ""),
                    Description = Str(a, "description", ""),
                    Status = status,
                    StatusVisibility = string.IsNullOrEmpty(status) ? Visibility.Collapsed : Visibility.Visible,
                    Running = running,
                    RunningText = running ? "RUNNING" : "STOPPED",
                    RunningBrush = running ? Ok : Muted,
                    RunningChipBackground = running ? Brush("#10281B") : Brush("#22262E"),
                    HasWebUi = hasWebUi,
                    WebUiVisibility = hasWebUi ? Visibility.Visible : Visibility.Collapsed,
                });
            }
        }

        _droboApps.Clear();
        foreach (var row in list) _droboApps.Add(row);

        DroboAppsEmpty.Text = "No DroboApps reported.";
        DroboAppsEmpty.Visibility = list.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
    }
}
