using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using System.Windows;

namespace DroboDashboardReborn;

/// <summary>
/// "Connect as..." -- asks for a Drobo username and password and hands them
/// straight to Windows via SmbConnections.Connect.
///
/// The password lives only in PasswordInput.Password (the PasswordBox's own
/// property) and a single local variable for the moment it takes to make the
/// P/Invoke call. It is never bound to a view-model string, never logged,
/// and never written to disk, config.json, or the agent -- see the
/// P/Invoke-not-`net use` note on SmbConnections for why that matters.
/// </summary>
public partial class ConnectShareDialog : Window
{
    private readonly string _uncPath;
    private readonly string? _guestAdviceDetail;

    /// <param name="shareName">Shown in the dialog header.</param>
    /// <param name="uncPath">The share to connect to, e.g. \\10.0.0.5\Documents.</param>
    /// <param name="suggestedDriveLetter">Pre-selected in the drive-letter list; null if
    /// none are free.</param>
    /// <param name="guestAdviceDetail">The agent's explanation of the guest-logon
    /// problem for this device, reused if Windows comes back with access denied.</param>
    public ConnectShareDialog(string shareName, string uncPath, string? suggestedDriveLetter, string? guestAdviceDetail)
    {
        InitializeComponent();
        _uncPath = uncPath;
        _guestAdviceDetail = guestAdviceDetail;

        Header.Text = "Connect to " + shareName;
        Sub.Text = uncPath;

        DriveLetterBox.Items.Add("(none -- open without a drive letter)");
        var used = new HashSet<char>(
            DriveInfo.GetDrives().Select(d => char.ToUpperInvariant(d.Name[0])));
        for (char c = 'Z'; c >= 'D'; c--)
            if (!used.Contains(c))
                DriveLetterBox.Items.Add(c + ":");

        DriveLetterBox.SelectedItem = !string.IsNullOrEmpty(suggestedDriveLetter)
            && DriveLetterBox.Items.Contains(suggestedDriveLetter)
                ? suggestedDriveLetter
                : DriveLetterBox.Items[0];

        Loaded += (_, _) => UsernameBox.Focus();
    }

    private async void Connect_Click(object sender, RoutedEventArgs e)
    {
        var username = UsernameBox.Text.Trim();
        var password = PasswordInput.Password;

        if (string.IsNullOrEmpty(username))
        {
            ShowError("Enter the username for your Drobo account.");
            return;
        }

        var letterChoice = DriveLetterBox.SelectedItem as string;
        string? driveLetter = letterChoice is { Length: 2 } && letterChoice[1] == ':' ? letterChoice : null;
        bool remember = Remember.IsChecked == true;

        SetBusy(true);
        try
        {
            // Off the UI thread: WNetAddConnection2 is a blocking network call and
            // can take several seconds against a slow or unreachable device.
            int code = await Task.Run(
                () => SmbConnections.Connect(_uncPath, driveLetter, username, password, remember));

            if (code == 0)
            {
                DialogResult = true;
                Close();
                return;
            }
            ShowError(SmbConnections.DescribeError(code, _guestAdviceDetail));
        }
        finally
        {
            SetBusy(false);
        }
    }

    private void SetBusy(bool busy)
    {
        ConnectBtn.IsEnabled = !busy;
        CancelBtn.IsEnabled = !busy;
        ConnectBtn.Content = busy ? "Connecting…" : "Connect";
        if (busy) ErrorText.Visibility = Visibility.Collapsed;
    }

    private void ShowError(string message)
    {
        ErrorText.Text = message;
        ErrorText.Visibility = Visibility.Visible;
    }

    private void Cancel_Click(object sender, RoutedEventArgs e)
    {
        DialogResult = false;
        Close();
    }
}
