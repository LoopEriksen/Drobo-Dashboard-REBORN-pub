using System.ComponentModel;
using System.Runtime.CompilerServices;

namespace DroboDashboardReborn;

/// <summary>
/// One switchable notification category on the Settings panel.
///
/// Built from <see cref="AlertNotifier.Categories"/> rather than written out in
/// XAML, so the list you can switch and the list that actually matches alert
/// keys are the same list. Two hand-maintained copies would eventually disagree,
/// and the way that failure presents is a switch labelled "Drive problems" that
/// silences nothing.
///
/// Implements INotifyPropertyChanged only for <see cref="MasterOn"/>: when the
/// master switch is toggled, every row's checkbox needs to grey out or come
/// back, and that is a change the rows have to be told about rather than one
/// they cause themselves.
/// </summary>
public sealed class NotificationCategoryRow : INotifyPropertyChanged
{
    public string Id { get; init; } = "";
    public string Label { get; init; } = "";
    public string Description { get; init; } = "";

    /// <summary>Whether this category notifies. Two-way bound; the window
    /// persists it on change.</summary>
    public bool Enabled { get; set; }

    private bool _masterOn;

    /// <summary>Mirrors the master switch, so an individual category cannot be
    /// toggled while notifications are off wholesale — the row would otherwise
    /// look armed while doing nothing.</summary>
    public bool MasterOn
    {
        get => _masterOn;
        set
        {
            if (_masterOn == value) return;
            _masterOn = value;
            Raise();
        }
    }

    public event PropertyChangedEventHandler? PropertyChanged;

    private void Raise([CallerMemberName] string? name = null) =>
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
}
