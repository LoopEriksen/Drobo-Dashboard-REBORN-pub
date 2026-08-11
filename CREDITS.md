# Credits

> File names in backticks (`ROADMAP.md`, `docs/protocol-map.md`, ...)
> refer to internal research and planning notes that are not part of this
> repository.

This project is an independent reimplementation of a Drobo's network protocol,
built for hardware whose vendor (StorCentric, Drobo's parent) filed for
bankruptcy and was liquidated in 2023. Nobody at Drobo or Data Robotics wrote,
reviewed, or endorses any of this. Where this project stood on someone else's
shoulders, this file says so, by name, and explains why the borrowing was
trusted rather than just copied. Where it did not — which is most of the
codebase — it does not claim otherwise: most of what's here came out of this
project's own capture-and-test process against a real Drobo 5N, documented in
`docs/protocol-map.md`, and that work isn't "credited"
here because nothing was borrowed to produce it.

Three categories, kept separate on purpose: actual borrowed knowledge, public
standards implemented from specification, and reverse-engineering sources that
were read but not borrowed from.

## Category A — borrowed knowledge

### drobo-utils (partition-scheme enumeration)

**What was taken:** the partition-scheme table
`{0: "No partitions", 1: "MBR", 2: "APM", 3: "GPT"}`, used by
`Volume._SCHEMES` / `Volume.partition_scheme` in
[`sdk/drobo_nasd/models.py`](sdk/drobo_nasd/models.py).

**From whom:** [petersilva/drobo-utils](https://github.com/petersilva/drobo-utils),
by Peter Silva, and its Python 3 fork,
[n3gwg/drobo-utils](https://github.com/n3gwg/drobo-utils). An independent
reverse-engineering of Drobo's SCSI/USB "Drobo Management Protocol Rev A.0",
dating from 2008.

**Why it was trusted:** drobo-utils drives a different transport — it drives
direct-attached Drobos over SCSI/USB, where this project speaks `nasd` over
TCP to a network Drobo. Different interface, but Drobo had no reason to
renumber its own constants between its own interfaces, so the table was a
testable hypothesis rather than a certainty to adopt on sight. It earned its
place only because an independent argument agreed: the volume this project
reads back from a real 5N reports a 64 TiB ceiling, and MBR cannot address
past 2 TiB, so scheme 3 could not be anything but GPT regardless of what
drobo-utils said. Two unrelated lines of evidence agreeing is what got this
table into the code — not the source's reputation, and not convenience.

**Tested and rejected, honestly:** two of drobo-utils' other tables were tried
against this project's own readings from a real, healthy Drobo 5N and did not
survive contact with them:

- Its unit-status bitfield (`0x0002 Red alert`, `0x0004 Yellow alert`, and so
  on) decodes this array's `DNASStatus = 6` as "Red alert | Yellow alert" —
  on an array that is provably healthy. A healthy array decoding as a red
  alert is disqualifying on its face, so `mStatus` / `DNASStatus` stay
  undecoded in this project rather than being forced to fit a table that
  doesn't.
- Its partition-format table (NO FORMAT / NTFS / HFS / EXT3 / FAT32) has no
  entry for 64, which is exactly what this project's device reports for
  `partition_format`. A neighbouring table being right was not treated as
  evidence for guessing this one, so `partition_format` is carried as a raw
  number and nothing more.

The full account — the specific values, and why each verdict was reached — is
in `docs/patents.md`. The rejections are listed here
alongside the one table that was kept for a reason: it shows the enumeration
that made it into the code was tested against real data, not copied on faith
because the name of the source sounded authoritative.

## Category B — public standards, implemented from specification

Nobody's code was taken for any of the following. Each implements a published,
public standard, so naming the standard here is for a future reader's benefit
— it is not a credit to a person or a project, because no person or project's
work went into it.

- [`agent/drobo_agent/qr.py`](agent/drobo_agent/qr.py) implements QR Code
  encoding per **ISO/IEC 18004**, including Reed–Solomon error correction over
  GF(256) with primitive polynomial `0x11D`. That polynomial and the GF
  arithmetic belong to the standard's own mathematics, not to any particular
  implementation of it. Standard library only — deliberately no `qrcode` or
  `pillow` dependency.
- [`sdk/drobo_nasd/discovery.py`](sdk/drobo_nasd/discovery.py) implements a
  minimal mDNS/DNS client. DNS message format and name compression are
  **RFC 1035**; multicast DNS is **RFC 6762**. Written from the RFCs, stdlib
  only, no `dnspython` or `zeroconf` dependency.
- [`tools/nasd_dissect.py`](tools/nasd_dissect.py) parses pcap and pcapng
  capture files. Both are public format specifications — pcapng is documented
  by the IETF opsawg draft and by Wireshark's own published format page.
  Written by hand, stdlib only, no `scapy` or `dpkt` dependency.

## Category C — reverse-engineering sources (not open source)

These are information sources this project read in order to understand the
hardware, not code or projects to credit. Neither of them is open source, and
framing them as such would misrepresent what they are.

- **Drobo / Data Robotics firmware.** Recovered by this project's own
  analysis of the official Drobo Dashboard and firmware images: the
  discovery mechanism, the `nasd` daemon, the message format, the connection
  model, and all 103 protocol command names and numeric IDs. See
  `docs/protocol-map.md`.
- **US Patent 7,873,782** (Data Robotics Inc, "Filesystem-aware block storage
  system"). Read as public documentation, which is exactly what a patent is
  for — a disclosure made in exchange for legal protection. It confirmed the
  four slot-state model (`STATE_EMPTY` / `STATE_OK` / `STATE_WARNING` /
  `STATE_FAILED`) this project had already inferred, and its LED semantics,
  including that "amber" describes the whole array's remaining capacity
  headroom rather than one drive's health — correcting a plausible
  misreading. It contains no numeric status codes, so it did not, and could
  not, resolve the `mStatus` / `DNASStatus` decode. See
  `docs/patents.md` for the full read.

## Category D — inspiration and prior art

The three categories above are about code, specifications and documents. This
one is about ideas, which are the harder thing to be honest about, because
nothing is copied and there is therefore nothing to point at unless the project
says so itself.

The relationship with Drobo needs stating plainly, because it is unusual: this
project exists to replace software made by a company that no longer exists, and
the software it replaces is simultaneously the thing being made obsolete **and**
the best available source of good ideas about how the job should be done. The
original Drobo Dashboard and DroboPix are the competition, the reference
implementation, and the cautionary tale, all at once. Data Robotics spent years
learning what a Drobo owner actually needs and shipped the answers; the company
folding does not make those answers wrong. Where this project reached the same
conclusion by copying rather than by thinking, it says so below rather than
quietly presenting the idea as its own. This is also the owner's stated
instruction on the matter, recorded in
`ROADMAP.md`: *"If there is any inspiration you are taking from the
firmware of software for the original drobo stuff, then go for it but ask for
permission to include additional features … and put them in our markdown."*

None of this is a claim of endorsement, and none of it involved reading Drobo's
source code. What follows is design influence: behaviour observed in a shipped
product, judged good, and deliberately reproduced.

### The original Drobo Dashboard (Data Robotics / StorCentric, final Windows release 3.5.0)

**The shape of the whole application.** Phase 1 originally aimed at "show me the
Drobo's health". After reading what the original client actually did, the
finish line was moved to parity with it — `ROADMAP.md` records
that "the new bar is 'match what Drobo Dashboard actually did'", and
[`README.md`](README.md) repeats it, both concluding that "a replacement that
only reads is a monitor, not a dashboard". The original product, not this
project's own initial ambition, is what defines done. That is the single largest
influence on scope in the repository.

**How the device is found.** [`sdk/drobo_nasd/discovery.py`](sdk/drobo_nasd/discovery.py)
could have required a static DHCP lease and been finished in an afternoon.
Instead its docstring says it "finds the device the same ways the real Drobo
Dashboard would" — mDNS browse, hostname resolution, a remembered address, an
optional sweep — because making the owner fight their router was judged the
wrong burden. The same reasoning picks the diagnostic channel in
[`tools/drobo_probe.py`](tools/drobo_probe.py): "This is how Drobo Dashboard
finds a Drobo, so the Drobo should answer." Copying the original's discovery
path is what makes silence meaningful evidence rather than an inconclusive
result. What was *not* copied is the identity check layered on top — esa_id
pinning, MAC corroboration, subnet limits — which is this project's own, on the
grounds that "found a Drobo" is not "found *my* Drobo".

**Where the capacity warnings fire.** [`agent/drobo_agent/monitor.py`](agent/drobo_agent/monitor.py)
prefers the device's own `mRedThreshold` / `mYellowThreshold` over this
project's configured defaults, so that "we'd then be alerting on exactly the
same levels the original Dashboard does". Inventing thresholds would have been
easier; matching the levels an owner is already used to was judged better.

**The vocabulary for share permissions.** The mapping from `ShareUserAccess`
integers to human meanings is computed at runtime and cannot be recovered from
the firmware, and this project refuses to guess — calling a writable share
"read-only" is worse than showing a raw number.
[`tools/access_codes.py`](tools/access_codes.py) resolves it by deferring to the
old client outright: "The original Drobo Dashboard 3.5.0 still installs, still
talks to the device, and shows permissions in plain words. So it becomes the
reference." The original is also used as the *writing* half of the experiment,
so this project never sends a write of its own — "the changing is done by
Dashboard, which is software Drobo shipped for the job."
[`tools/capture-session.ps1`](tools/capture-session.ps1) makes the same point to
the operator, instructing them to write down the literal words the old UI
offered because the word is "the half of this we cannot recover from the bytes".

**Features carried forward because the original had them.** Recorded in
`ROADMAP.md` as candidates, each justified by the original's
behaviour rather than by a fresh idea: scheduled LED dimming and night mode
("The original could dim its lights on a schedule"), SMTP email alerts
alongside the agent's ntfy push ("The original could send SMTP alerts … email is
a good second channel"), and Drobo-to-Drobo replication ("The original could
mirror one Drobo to another — a genuine backup answer to the BeyondRAID lock-in
problem").

**And one thing taken as a warning rather than a model.** The auto-updater is
core scope, not an optional extra, and the machinery in
[`agent/drobo_agent/updates.py`](agent/drobo_agent/updates.py) was built early
and shipped dormant. The reason given in both that module and
`BLUEPRINT.md` is the original's own end: "an app that cannot
ship itself a fix is the exact failure mode that killed the original." That is
influence too — a design decision made by looking at how a predecessor failed
and deliberately doing the opposite.

### DroboPix (Data Robotics' phone-to-NAS photo backup app)

DroboPix is what Phase 4 sets out to replace, and it is also the design this
phase is built on. Both halves of that are true at once and the repository says
so in the same breath.

**Pairing by QR code.** The agent shows a code carrying URL and token; the phone
scans it. [`agent/drobo_agent/qr.py`](agent/drobo_agent/qr.py) credits the
source and the reasoning together: "DroboPix paired exactly this way -- a QR
code shown in Drobo Dashboard that you scanned with the phone. It was the right
call then and it still is: nobody should be typing a 32-character token on a
phone keyboard." `ios/README.md` repeats it at the endpoint
that serves the code. (The QR *encoder* is implemented from ISO/IEC 18004 and is
credited under Category B; what is credited here is the decision to have a QR
code at all.)

**No cloud relay.** The phone pairs locally and then talks straight to the NAS
over Wi-Fi. `ios/README.md` cites DroboPix as precedent that
this shape works: "DroboPix worked the same way — it paired by scanning a QR
code off the Dashboard screen and then talked straight to the NAS over Wi-Fi.
The cloud relay belonged to DroboAccess, a different app." A design that never
depended on vendor infrastructure is a design that survives the vendor's
liquidation, which is the whole reason it transfers intact.

**Geofence and SSID together as the wake trigger.** iOS gives third-party apps
no continuous background execution, so something has to wake the app up.
DroboPix's answer is adopted without modification —
`ios/README.md`: "DroboPix used geofence **and** SSID together —
it wasn't a gimmick, it was the only dependable way to get woken up. Worth
copying exactly." It is step 4 of the build order there, and the same judgement
is recorded independently in `BLUEPRINT.md` and
`docs/reality-check.md`, both noting that none of it
depended on Drobo's cloud.

**Wi-Fi only by default.** Listed flatly in the iOS design notes as a behaviour
to take rather than decide afresh: "**Wi-Fi only by default**, with an explicit
opt-in for cellular. Copy the DroboPix behaviour."

**The split of responsibilities.** The agent's receiving half is labelled
"DroboPix-style photo backup" in
[`agent/drobo_agent/photos.py`](agent/drobo_agent/photos.py) and in the package
index: the phone notices new photos and offers them, the agent decides what it
already has and files what it doesn't. That division is DroboPix's, and Phase 4
in `ROADMAP.md` is defined as "Make an app mimicking Drobo Pix
given the functionality of our new program".

**The still-installed copy as a starting point.** DroboPix is one of the apps
that answers `eCmdGetDroboAppsStatus` on this unit, and
[`sdk/drobo_nasd/droboapps.py`](sdk/drobo_nasd/droboapps.py) treats that as
useful rather than merely amusing: the app, its version and its web interface
"are all discoverable from here, which is a better starting point for Phase 4
than guessing." With the vendor gone and no documentation surviving, reading the
real thing off the device is the same evidence-over-invention discipline applied
everywhere else in this project.

### Tailscale — an intended approach, not yet built

**Nothing here is implemented.** `ROADMAP.md` lists Phase 5 as
"Approach settled (Tailscale), nothing built", and it is recorded in this
section because the approach was chosen by reference to how Tailscale works, not
because any code depends on it.

The plan is Tailscale on the machine running the agent and on the phone: "The
Drobo keeps its ordinary LAN address; the phone reaches it from anywhere; no
ports are ever opened." It replaces Drobo's dead myDrobo relay without
reproducing it. It cannot run on the Drobo itself — the unit's kernel 3.2.96
predates WireGuard's mainline merge — so it would run on a PC or a Raspberry Pi
acting as a subnet router. If that ever ships, this entry should be rewritten to
describe what was actually built.

### Apple iCloud Photos

The owner's brief for Phase 4 sets it up as a replacement and competitor for
iCloud Photos, and the project reasons from iCloud's actual behaviour to work
out what a faithful replacement has to store. The concrete consequence in
`ROADMAP.md` is a backup requirement that would otherwise have
been missed: albums, favourites, keywords and non-destructive edits live in the
Photos database rather than in the image file, so "A true iCloud replacement
backs up the original **plus** the adjustment data." An app that saved only the
original file would look correct and quietly lose every edit the owner ever
made. That requirement comes from looking at what Apple's product does, not from
first principles.

## What's not here

Everything else in this repository — the `nasd` protocol client, the XML
parsing, the alerting logic, the Windows app, the web dashboard, and the
capture tooling not listed above — is this project's own work, arrived at by
capturing real traffic to and from a real Drobo 5N and testing hypotheses
against it. It has nothing to credit because nothing was taken. If that ever
changes, this file changes with it.
