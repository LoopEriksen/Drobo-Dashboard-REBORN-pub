namespace DroboDashboardReborn;

/// <summary>
/// One volume (Drobo calls them LUNs) as the Status tab shows it.
///
/// A volume is what Windows presents as a drive letter. A Drobo carves its
/// pack into up to sixteen of them, and each grows as drives are added rather
/// than being a fixed slice of the array.
///
/// <para>
/// <b>Ceiling and PackHolds exist as a pair, and must stay that way.</b> The
/// device reports a maximum volume size of about 70 TB — that is a ceiling the
/// volume may grow to, not its size and not free space. On a pack holding
/// under 9 TB usable, showing 70 TB on its own reads as sixty-odd terabytes of
/// headroom that does not exist. So the pack's real capacity is rendered
/// directly beneath it, and neither line should be shown without the other.
/// </para>
/// </summary>
public sealed class VolumeRow
{
    /// <summary>The device leaves this empty on a single-volume pack, in which
    /// case the caller substitutes "Volume 1" rather than inventing a name.</summary>
    public string Name { get; set; } = "";

    /// <summary>Partition count and identifier — the small print under the name.</summary>
    public string Detail { get; set; } = "";

    /// <summary>"Can grow to 70.4 TB". Never shown without <see cref="PackHolds"/>.</summary>
    public string Ceiling { get; set; } = "";

    /// <summary>"pack holds 8.76 TB" — the number that is actually true today.</summary>
    public string PackHolds { get; set; } = "";
}
