using System.Windows;

namespace DroboDashboardReborn;

/// <summary>
/// "Copy diagnostics" -> asks once whether the result should go to the
/// clipboard or to a file, with an optional "remember my choice" that
/// CopyDiagnostics_Click persists via DroboSettings so the owner isn't
/// asked again every time -- see the owner's note on MainWindow.xaml.cs
/// about not wanting to be nagged for something this small.
/// </summary>
public partial class DiagnosticsChoiceDialog : Window
{
    public enum Choice { Cancelled, Clipboard, File }

    public Choice Result { get; private set; } = Choice.Cancelled;
    public bool RememberChoice => Remember.IsChecked == true;

    public DiagnosticsChoiceDialog()
    {
        InitializeComponent();
    }

    private void Clipboard_Click(object sender, RoutedEventArgs e)
    {
        Result = Choice.Clipboard;
        DialogResult = true;
        Close();
    }

    private void File_Click(object sender, RoutedEventArgs e)
    {
        Result = Choice.File;
        DialogResult = true;
        Close();
    }

    private void Cancel_Click(object sender, RoutedEventArgs e)
    {
        Result = Choice.Cancelled;
        DialogResult = false;
        Close();
    }
}
