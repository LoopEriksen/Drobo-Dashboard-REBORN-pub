using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;

namespace DroboDashboardReborn;

/// <summary>
/// Talks to Windows' own SMB connection manager (the "mpr.dll" WNet* APIs)
/// so this app can connect to a Drobo share as a named user instead of the
/// guest logon Windows 11 refuses. See shares.py / docs/accessing-your-files.md
/// for the full story of why guest shares stopped working.
///
/// Why P/Invoke instead of shelling out to `net use \\host\share pass /user:name`:
/// a password passed as a command-line argument sits in that process's
/// argument list, which every other process on the machine can read for as
/// long as it runs (Task Manager, Process Explorer, WMI queries all show
/// it). WNetAddConnection2 hands the password to Windows directly, in
/// memory, for the single call -- it never touches a command line, a log,
/// or disk. We don't write it anywhere either.
/// </summary>
internal static class SmbConnections
{
    // ---------------------------------------------------------------
    // Win32 declarations (mpr.dll)
    // ---------------------------------------------------------------

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct NETRESOURCE
    {
        public int dwScope;
        public int dwType;
        public int dwDisplayType;
        public int dwUsage;
        public string? lpLocalName;
        public string? lpRemoteName;
        public string? lpComment;
        public string? lpProvider;
    }

    private const int RESOURCETYPE_DISK = 0x00000001;
    private const int RESOURCE_CONNECTED = 0x00000001;

    /// <summary>Tells Windows to remember the login in Credential Manager -- the
    /// "Remember this in Windows" checkbox. Without it, the connection lasts only
    /// for this Windows sign-in session, and we never store the password ourselves
    /// either way.</summary>
    public const int CONNECT_UPDATE_PROFILE = 0x00000001;

    [DllImport("mpr.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern int WNetAddConnection2(
        ref NETRESOURCE lpNetResource, string? lpPassword, string? lpUsername, int dwFlags);

    [DllImport("mpr.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern int WNetCancelConnection2(
        string lpName, int dwFlags, [MarshalAs(UnmanagedType.Bool)] bool fForce);

    [DllImport("mpr.dll", SetLastError = true)]
    private static extern int WNetOpenEnum(
        int dwScope, int dwType, int dwUsage, IntPtr lpNetResource, out IntPtr lphEnum);

    [DllImport("mpr.dll")]
    private static extern int WNetCloseEnum(IntPtr hEnum);

    [DllImport("mpr.dll", CharSet = CharSet.Unicode)]
    private static extern int WNetEnumResource(
        IntPtr hEnum, ref int lpcCount, IntPtr lpBuffer, ref int lpBufferSize);

    private const int NO_ERROR = 0;

    // ---------------------------------------------------------------
    // Reading Windows' current connections
    // ---------------------------------------------------------------

    /// <summary>One currently-active Windows connection to a network share. LocalName
    /// is null when the share is connected without consuming a drive letter.</summary>
    public readonly record struct ActiveConnection(string RemoteUnc, string? LocalName);

    /// <summary>
    /// Every network share Windows currently has open, drive-lettered or
    /// not. Read fresh each time -- this is Windows' own live state, not
    /// something we cache, so it stays correct even if the share was mapped
    /// from Explorer instead of from this app.
    /// </summary>
    public static List<ActiveConnection> GetActiveConnections()
    {
        var results = new List<ActiveConnection>();
        if (WNetOpenEnum(RESOURCE_CONNECTED, RESOURCETYPE_DISK, 0, IntPtr.Zero, out var handle) != NO_ERROR)
            return results; // nothing connected, or enumeration unavailable -- an empty list is the safe default

        try
        {
            // 64KB comfortably holds the handful of shares a home NAS offers plus
            // whatever else Windows has mapped. If there were ever more than that,
            // WNetEnumResource returns an error and we simply stop -- a partial
            // list beats crashing the Shares tab over an edge case this app will
            // never actually see on a home network.
            const int bufferSize = 65536;
            var buffer = Marshal.AllocHGlobal(bufferSize);
            try
            {
                while (true)
                {
                    int count = -1;
                    int size = bufferSize;
                    int rc = WNetEnumResource(handle, ref count, buffer, ref size);
                    if (rc != NO_ERROR) break; // NO_ERROR is the only "keep going" result

                    int itemSize = Marshal.SizeOf<NETRESOURCE>();
                    for (int i = 0; i < count; i++)
                    {
                        var res = Marshal.PtrToStructure<NETRESOURCE>(buffer + i * itemSize);
                        if (!string.IsNullOrEmpty(res.lpRemoteName))
                            results.Add(new ActiveConnection(res.lpRemoteName!, res.lpLocalName));
                    }
                }
            }
            finally
            {
                Marshal.FreeHGlobal(buffer);
            }
        }
        finally
        {
            WNetCloseEnum(handle);
        }
        return results;
    }

    /// <summary>The first unused drive letter from Z down to D, or null if none are
    /// free. D upward is skipped to stay clear of A/B (floppy-era conventions some
    /// software still assumes) and C (the system drive).</summary>
    public static string? NextFreeDriveLetter()
    {
        var used = new HashSet<char>(
            DriveInfo.GetDrives().Select(d => char.ToUpperInvariant(d.Name[0])));
        for (char c = 'Z'; c >= 'D'; c--)
            if (!used.Contains(c))
                return c + ":";
        return null;
    }

    // ---------------------------------------------------------------
    // Connect / disconnect
    // ---------------------------------------------------------------

    /// <summary>
    /// Connects to <paramref name="uncPath"/> as a named user.
    ///
    /// <paramref name="driveLetter"/> may be null/empty to connect without
    /// consuming a drive letter -- Explorer can still open the UNC path
    /// directly once Windows has a session to it.
    ///
    /// Returns 0 on success, or a Win32 error code -- pass that to
    /// <see cref="DescribeError"/> to turn it into plain English.
    /// </summary>
    public static int Connect(string uncPath, string? driveLetter, string username, string password, bool remember)
    {
        var nr = new NETRESOURCE
        {
            dwType = RESOURCETYPE_DISK,
            lpRemoteName = uncPath,
            lpLocalName = string.IsNullOrWhiteSpace(driveLetter) ? null : driveLetter,
        };
        int flags = remember ? CONNECT_UPDATE_PROFILE : 0;
        return WNetAddConnection2(ref nr, password, username, flags);
    }

    /// <summary>Disconnects a drive letter ("Z:") or a UNC-only session (pass the UNC
    /// path itself). Returns 0 on success, or a Win32 error code.</summary>
    public static int Disconnect(string nameOrUnc, bool force = false) =>
        WNetCancelConnection2(nameOrUnc, 0, force);

    // ---------------------------------------------------------------
    // Error codes -> plain English
    // ---------------------------------------------------------------

    public const int ERROR_ACCESS_DENIED = 5;
    public const int ERROR_BAD_NETPATH = 53;
    public const int ERROR_ALREADY_ASSIGNED = 85;
    public const int ERROR_EXTENDED_ERROR = 1208; // 0x4B8
    public const int ERROR_SESSION_CREDENTIAL_CONFLICT = 1219;
    public const int ERROR_LOGON_FAILURE = 1326;

    /// <summary>
    /// Turns a Win32 error code from Connect/Disconnect into a sentence the
    /// owner can act on instead of a bare number. <paramref name="guestAdvice"/>
    /// is the explanation the agent already produced for this exact device
    /// (shares.py's access_advice.detail) -- reused here because
    /// ERROR_ACCESS_DENIED is usually that same guest-logon story.
    /// </summary>
    public static string DescribeError(int code, string? guestAdvice)
    {
        switch (code)
        {
            case NO_ERROR:
                return "";
            // WNetAddConnection2 commonly returns 1208 (ERROR_EXTENDED_ERROR) for a
            // rejected SMB logon rather than 1326 -- Windows is saying "ask the
            // provider for the real reason" (WNetGetLastError), but a wrong Drobo
            // username/password is the overwhelmingly common cause on this device,
            // so both codes get the same plain-English message.
            case ERROR_EXTENDED_ERROR:
            case ERROR_LOGON_FAILURE:
                return "Windows says the username or password is wrong. Double-check " +
                       "both and try again -- this is your Drobo login, not your Windows login.";
            case ERROR_BAD_NETPATH:
                return "The Drobo didn't answer. Check that it's powered on and connected to the network.";
            case ERROR_SESSION_CREDENTIAL_CONFLICT:
                return "Windows already has a different connection open to this Drobo, " +
                       "under a different login. Disconnect it first, then try again.";
            case ERROR_ACCESS_DENIED:
                return !string.IsNullOrWhiteSpace(guestAdvice)
                    ? guestAdvice!
                    : "Windows was refused access to this share. Check the username and " +
                      "password, and that this user has permission to this share on the Drobo.";
            case ERROR_ALREADY_ASSIGNED:
                return "That drive letter is already in use for something else. Pick a different one.";
            default:
                return $"Windows reported an error connecting: {new Win32Exception(code).Message} (code {code}).";
        }
    }
}
