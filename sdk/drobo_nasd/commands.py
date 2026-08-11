"""
nasd command enum -- TMCmd::ECommands, transcribed from docs/protocol-map.md.

Source of truth: the "Full ID table (103 commands)" in
docs/protocol-map.md, itself recovered from a 103-entry table of `char*`
pointers in `nasd`'s `.data.rel.ro` at 0x00191ccc (firmware 4.2.1). The array
index *is* the command's numeric id -- `FIRMWARE`-grade evidence, not a guess.
What IS a guess (`GUESS` in protocol-map.md): that the enum starts at 0, and
whether the wire actually carries this integer or the string name instead
(still open -- see docs/protocol-map.md "Still open").

Do not add, remove, renumber, or reorder entries by hand. If protocol-map.md's
table changes, regenerate this dict from it and re-run the self-check below.
"""

from __future__ import annotations

# name -> numeric id (== array index in the recovered firmware table)
COMMANDS: dict[str, int] = {
    "eCmdSetAdmin": 0,
    "eCmdGetAdmin": 1,
    "eCmdCreateLUN": 2,
    "eCmdDeleteLUN": 3,
    "eCmdRenameLUN": 4,
    "eCmdGetNextUniqueLUNID": 5,
    "eCmdSetCHAP": 6,
    "eCmdGetVersion": 7,
    "eCmdSetDimming": 8,
    "eCmdGetDimming": 9,
    "eCmdDeviceLogin": 10,
    "eCmdDeviceLogout": 11,
    "eCmdMount": 12,
    "eCmdUnmount": 13,
    "eCmdForceUpdate": 14,
    "eCmdGetDiskPackId": 15,
    "eCmdRepair": 16,
    "eCmdSetFlashConfig": 17,
    "eCmdSendTestEmail": 18,
    "eCmdSendDashboardTestEmail": 19,
    "eCmdGetKernelInterfaceInfo": 20,
    "eCmdRestart": 21,
    "eCmdStandby": 22,
    "eCmdShutdown": 23,
    "eCmdReset": 24,
    "eCmdRename": 25,
    "eCmdIdentify": 26,
    "eCmdInstallFirmware": 27,
    "eCmdRevertFirmware": 28,
    "eCmdUploadDiags": 29,
    "eCmdGetConfig": 30,
    "eCmdSetConfig": 31,
    "eCmdGetFirmwareInfo": 32,
    "eCmdFormatDevice": 33,
    "eCmdFormatLUN": 34,
    "eCmdGetDroboSyncSettings": 35,
    "eCmdSetDroboSyncSettings": 36,
    "eCmdGetDroboSyncSummaryLog": 37,
    "eCmdGetDroboSyncDetailedLog": 38,
    "eCmdGetDroboSyncLog": 39,
    "eCmdSyncNow": 40,
    "eCmdStopSync": 41,
    "eCmdGetReservedDriveLetters": 42,
    "eCmdGetDemoModeInfo": 43,
    "eCmdSetDemoModeInfo": 44,
    "eCmdUpdateUserEmailSetting": 45,
    "eCmdGetUserEmailSettings": 46,
    "eCmdGetDevices": 47,
    "eCmdGetUpdate": 48,
    "eCmdInsertDrive": 49,
    "eCmdRemoveDrive": 50,
    "eCmdRunESACommand": 51,
    "eCmdSendTMCommand": 52,
    "eCmdEnableAutoDiscovery": 53,
    "eCmdIsAutoDiscoveryEnabled": 54,
    "eCmdAddManualDiscoveryIP": 55,
    "eCmdRemoveManualDiscoveryIP": 56,
    "eCmdGetManualDiscoveryIPList": 57,
    "eCmdModeSense": 58,
    "eCmdIsSCSIConnAvailable": 59,
    "eCmdAddVolume": 60,
    "eCmdGetSysInfo": 61,
    "eCmdGetPerformance": 62,
    "eCmdGetFanInfo": 63,
    "eCmdGetPowerInfo": 64,
    "eCmdGetControllerCard": 65,
    "eCmdGetExpanderCard": 66,
    "eCmdCacheBattery": 67,
    "eCmdForceSingleInitiator": 68,
    "eCmdGetOSInfo": 69,
    "eCmdReadHostBuffer": 70,
    "eCmdWriteHostBuffer": 71,
    "eCmdGetTMDiagFilePath": 72,
    "eCmdForceRepair": 73,
    "eCmdGetLunInitatorExtraInfo": 74,  # sic -- Drobo's own typo, kept verbatim
    "eCmdDoPoll": 75,
    "eCmdToggleNightMode": 76,
    "eCmdGetDroboAppsStatus": 77,
    "eCmdDroboAppsAction": 78,
    "eCmdAuthenticateUser": 79,
    "eCmdGetAdminConfig": 80,
    "eCmdGetSharesConfig": 81,
    "eCmdSetTime": 82,
    "eCmdGetEventLogs": 83,
    "eCmdGetSessionIPAddress": 84,
    "eCmdGetAllDevicesStatusXML": 85,
    "eCmdUploadFileToSupport": 86,
    "eCmdCancelUploadToSupport": 87,
    "eCmdGetRegisteredEmail": 88,
    "eCmdRunCommand": 89,
    "eCmdGetUserGroupList": 90,
    "eCmdGetUserEventLog": 91,
    "eCmdUpdateUserGroupList": 92,
    "eCmdGetDeviceNamesToShutdown": 93,
    "eCmdGetSavedFileContent": 94,
    "eCmdDownloadConfigFile": 95,
    "eCmdDownloadAllConfigFiles": 96,
    "eCmdGetDASDeviceNamesWithMountedVolumes": 97,
    "eCmdGetDeviceMountedVolumesCount": 98,
    "eCmdPostUserEvent": 99,
    "eCmdDownloadFTPFileToSharedPath": 100,
    "eCmdGetAppDataFile": 101,
    "eCmdGetNASUsernamePassword": 102,
}

# reverse lookup: numeric id -> name
ID_TO_NAME: dict[int, str] = {v: k for k, v in COMMANDS.items()}

# eCmd* names that appear in the binary's strings but NOT in the recovered
# 103-entry numeric table, so they have no confirmed id. Tracked here so the
# gap is documented rather than silently dropped. `eCmdRouter` is the one in the
# DO-NOT-SEND group below; the iSCSI/shutdown three are ordinary reads that
# simply never got a table slot. See protocol-map.md "Commands".
NAMES_WITHOUT_CONFIRMED_ID: frozenset[str] = frozenset({
    "eCmdRouter",
    "eCmdGetiSCSIAdmin",
    "eCmdSetiSCSIAdmin",
    "eCmdIsSafeToShutdown",
})

# ---------------------------------------------------------------------------
# ESCAPE HATCHES: arbitrary-execution / raw-memory paths.
#
# protocol-map.md ("Escape hatches"): "Treat this group as dangerous. They
# appear to be arbitrary command execution paths on a device with no security
# updates. The agent must never expose them, and nothing should be sent to
# them during exploration."
#
# eCmdRouter has no numeric id (see NAMES_WITHOUT_CONFIRMED_ID above) -- it is
# still listed by name so any code path that resolves a command by name catches
# it too.
# ---------------------------------------------------------------------------
ESCAPE_HATCHES: frozenset[str] = frozenset(
    {
        "eCmdRunESACommand",
        "eCmdRunCommand",
        "eCmdSendTMCommand",
        "eCmdRouter",
        "eCmdReadHostBuffer",
        "eCmdWriteHostBuffer",
    }
)

# ---------------------------------------------------------------------------
# DESTRUCTIVE: anything that changes the device, its data, or its firmware.
#
# Added 2026-07-26 after an audit noticed the omission. Until then DO_NOT_SEND
# held only the six escape hatches above -- so eCmdFormatDevice,
# eCmdInstallFirmware, eCmdRevertFirmware, eCmdReset and eCmdRepair were NOT
# refused by the guard, on a project whose first rule is "never write to the
# pack, never flash firmware". Nothing had ever sent one, but the guard that is
# supposed to make that impossible would not have stopped it.
#
# Grouped by what going wrong costs you:
#
#   irreversible data loss   format, repair, reset, LUN delete
#   bricks the box           firmware install/revert
#   interrupts the array     restart, shutdown, standby, mount/unmount,
#                            insert/remove drive, sync
#   changes configuration    every eCmdSet*, rename, dimming, night mode,
#                            time, discovery IPs, user/group edits
#   has a side effect        test emails, diagnostics upload, support upload,
#                            FTP fetch, LUN id allocation (it ALLOCATES despite
#                            the "Get" prefix), eCmdForceSingleInitiator (the
#                            name reads like a query; it is a write)
#
# Naming is not a safe guide here, which is the whole reason this list is
# explicit rather than derived from a prefix rule.
# ---------------------------------------------------------------------------
DESTRUCTIVE: frozenset[str] = frozenset(
    {
        # irreversible
        "eCmdFormatDevice", "eCmdFormatLUN", "eCmdRepair", "eCmdForceRepair",
        "eCmdReset", "eCmdDeleteLUN",
        # firmware
        "eCmdInstallFirmware", "eCmdRevertFirmware", "eCmdForceUpdate",
        # availability
        "eCmdRestart", "eCmdShutdown", "eCmdStandby",
        "eCmdMount", "eCmdUnmount", "eCmdInsertDrive", "eCmdRemoveDrive",
        "eCmdSyncNow", "eCmdStopSync",
        # configuration changes
        "eCmdSetAdmin", "eCmdSetCHAP", "eCmdSetConfig", "eCmdSetFlashConfig",
        "eCmdSetDimming", "eCmdSetTime", "eCmdSetDemoModeInfo",
        "eCmdSetDroboSyncSettings", "eCmdSetiSCSIAdmin",
        "eCmdToggleNightMode", "eCmdRename", "eCmdRenameLUN",
        "eCmdCreateLUN", "eCmdAddVolume", "eCmdGetNextUniqueLUNID",
        "eCmdEnableAutoDiscovery", "eCmdAddManualDiscoveryIP",
        "eCmdRemoveManualDiscoveryIP",
        "eCmdUpdateUserEmailSetting", "eCmdUpdateUserGroupList",
        "eCmdDroboAppsAction",
        "eCmdForceSingleInitiator",
        # side effects that leave the machine
        "eCmdSendTestEmail", "eCmdSendDashboardTestEmail",
        "eCmdUploadDiags", "eCmdUploadFileToSupport",
        "eCmdCancelUploadToSupport", "eCmdDownloadFTPFileToSharedPath",
        "eCmdPostUserEvent",
    }
)

# ---------------------------------------------------------------------------
# SAFE_WRITES: commands that change something, but nothing that matters.
#
# This project was read-only against the hardware for its whole life, which was
# the right default while the protocol was still being worked out. This set is
# the deliberate, narrow exception -- opened 2026-07-27 at the owner's request,
# starting with the least consequential write in the entire 103-command table.
#
# The bar for entry is strict, and all four must hold:
#
#   1. It cannot touch the disk pack, the filesystem, or any file.
#   2. It cannot affect availability -- no reboot, no unmount, no spin-down.
#   3. It is reversible, or it undoes itself.
#   4. Getting it wrong costs the owner nothing worse than mild confusion.
#
# eCmdIdentify qualifies on all four: it blinks the front lights so you can
# tell which box is which in a cupboard, and it stops on its own. There is no
# state to corrupt and nothing to undo.
#
# NOTHING is added here without the owner saying so explicitly, and anything
# that fails even one of the four tests above stays in DESTRUCTIVE. In
# particular eCmdSetDimming and eCmdRename are plausible future members but are
# NOT here yet -- they persist a setting, so they fail test 3 without a
# read-back-and-restore path that does not exist.
# ---------------------------------------------------------------------------
SAFE_WRITES: frozenset[str] = frozenset({
    "eCmdIdentify",
})

# The guard every caller checks. Everything dangerous, minus nothing --
# SAFE_WRITES is deliberately NOT subtracted here, because it never overlapped:
# a command is either in the refused set or in the safe set, never both.
DO_NOT_SEND: frozenset[str] = ESCAPE_HATCHES | DESTRUCTIVE

assert DO_NOT_SEND.isdisjoint(SAFE_WRITES), (
    "a command cannot be both refused and a safe write: "
    f"{sorted(DO_NOT_SEND & SAFE_WRITES)}"
)


def is_safe_write(name: str) -> bool:
    """True if `name` is one of the narrow, explicitly-approved writes."""
    return name in SAFE_WRITES


def is_dangerous(name_or_id) -> bool:
    """True if `name_or_id` (a command name or numeric id) is in DO_NOT_SEND."""
    if isinstance(name_or_id, str):
        return name_or_id in DO_NOT_SEND
    name = ID_TO_NAME.get(name_or_id)
    return name in DO_NOT_SEND if name else False


def _self_check() -> None:
    """
    Cheap internal consistency check, run at import time. This is NOT a
    substitute for test_nasd.py -- it just guarantees the module never loads
    in a silently-corrupted state (dropped entry, typo'd id, duplicate id).
    """
    assert len(COMMANDS) == 103, f"expected 103 commands, got {len(COMMANDS)}"

    ids = sorted(COMMANDS.values())
    assert ids == list(range(103)), "command ids must be exactly 0..102, each used once"

    # spot-check a handful of ids against docs/protocol-map.md by hand
    spot_checks = {
        "eCmdSetAdmin": 0,
        "eCmdGetVersion": 7,
        "eCmdDeviceLogin": 10,
        "eCmdRunESACommand": 51,
        "eCmdGetSysInfo": 61,
        "eCmdGetAllDevicesStatusXML": 85,
        "eCmdRunCommand": 89,
        "eCmdGetNASUsernamePassword": 102,
    }
    for name, expected_id in spot_checks.items():
        assert COMMANDS[name] == expected_id, (
            f"{name} expected id {expected_id}, got {COMMANDS.get(name)}"
        )

    for name in DO_NOT_SEND:
        assert name in COMMANDS or name in NAMES_WITHOUT_CONFIRMED_ID, (
            f"DO_NOT_SEND entry {name!r} is neither a known command nor a "
            f"documented no-id exception"
        )


_self_check()
