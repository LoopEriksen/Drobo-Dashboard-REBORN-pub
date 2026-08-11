# Drobo Agent

The always-on piece. It watches the Drobo, keeps history, raises alerts,
receives photo backups from the phone, and serves all of it as plain JSON.

The Windows app and the iOS app are both just clients of this. Neither of them
knows the Drobo protocol exists — that lives in exactly one file,
[`drobo_agent/drivers.py`](drobo_agent/drivers.py).

Python 3.9+, standard library only. Nothing to install.

---

## Run it

```bash
py run_agent.py
```

First run generates `config.json` with an access token and prints a URL. Open
that URL and you get the dashboard.

## The web dashboard

Served at `/`. One self-contained page — no frameworks, no CDN, no build step.
Device header with live state, capacity bar, a card per drive bay with
temperatures, active alerts sorted worst-first, and a 24-hour temperature
sparkline. Updates arrive over Server-Sent Events, so it changes the moment the
Drobo does rather than on a refresh timer. Light and dark follow the system
setting.

**This is the fastest route to a Drobo UI on your iPhone**: open the agent's
address in Safari and it works today — no Xcode, no Mac, no developer account.
It also serves as the layout prototype for the WinUI 3 app; if a panel doesn't
work here it won't work there.

The page shell carries no data and needs no token, so you can bookmark it. It
asks for the token once and keeps it in `sessionStorage`. If you pass
`?token=...` in the URL it's saved and then scrubbed out of the address bar so
it doesn't linger in history.

When the mock driver is running, a simulator panel appears with buttons to fail
or pull any bay — the quickest way to see the whole alert path work end to end.

```bash
py run_agent.py --once
```

Takes a single reading, prints it as JSON, exits. Useful for checking a driver.

```bash
py test_agent.py
```

Starts a real agent on a spare port, exercises every endpoint, checks the
answers, and tidies up. 138 checks as of 2026-07-26 (count it yourself with
`py test_agent.py | grep -c "  PASS "` -- this number drifts as the API
grows), no hardware needed. Run it after any change.

This is one of twenty-one test suites in the project: six under
`agent/test_*.py` (`test_agent`, `test_backup`, `test_drivers`,
`test_identify_api`, `test_notify`, `test_updates`), eleven under
`sdk/tests/` (the protocol: esatm, discovery, config, shares, sysinfo,
identify and friends), and four under `tools/`. Every suite runs standalone
-- `py <file>` from its own directory -- and none needs real hardware.

---

## Two drivers: fake data for testing, real data for the actual Drobo

`Drobo5NDriver` is real and works against real hardware — it reads the status
greeting the device pushes on TCP 5000, discovers the device by mDNS when its
address changes, and tops up chassis temperature/uptime from the command
channel on TCP 5001. Point `config.json` at `"driver": "drobo5n"` and every
layer above it (API, alerts, notifications, both apps) runs on the real
array, nothing further to build.

The `mock` driver still exists, and is still useful: it simulates a Drobo 5N
(five bays, four populated, drifting temperatures, capacity creeping upward)
so the whole stack can be built and tested without a physical device on hand,
and so failure paths can be triggered on demand rather than waited for.

You can break the fake Drobo on demand to prove the alert path works:

```bash
curl -X POST -H "X-Agent-Token: YOUR_TOKEN" http://127.0.0.1:7420/api/mock/fail/3
curl -X POST -H "X-Agent-Token: YOUR_TOKEN" http://127.0.0.1:7420/api/mock/reset
```

`fail/<bay>`, `warn/<bay>`, `pull/<bay>`, `reset`.

**Still simulated, even against the real driver:** cache battery, fan, PSU,
and the device event log. That's a hardware/protocol limit, not a gap in this
codebase: on a real 5N those commands are accepted and then answer empty or
time out, measured repeatedly, and the chassis plausibly has no such sensors
to report. The UI labels these fields as simulated rather than passing fake
readings off as measurements.

---

## API

Everything except `/api/ping` needs the `X-Agent-Token` header.
`/api/events` also accepts `?token=` because browsers can't set headers on an
EventSource.

| Method | Path | What it does |
|---|---|---|
| GET | `/` | The web dashboard. No auth — the shell holds no data. |
| GET | `/api/ping` | Liveness and version. No auth — safe for discovery. |
| GET | `/api/status` | Everything: device, capacity, drives, overall state |
| GET | `/api/device` | Identity and firmware |
| GET | `/api/drives` | Per-bay detail |
| GET | `/api/capacity` | Capacity numbers, including `used_fraction` |
| GET | `/api/alerts?all=1` | Active alerts, or the full history |
| GET | `/api/history?limit=N` | Trimmed series for graphing |
| GET | `/api/netcheck?host=` | Whether this PC is even on the Drobo's network — the usual reason one "vanishes". `/api/status` already carries this as `network_hint`; this is for a device picker with an address and no snapshot. |
| GET | `/api/events` | Server-sent events — live snapshots and alerts |
| GET | `/api/photos/stats` | How much has been backed up |
| POST | `/api/photos/have` | `{"hashes":[...]}` → `{"missing":[...]}` |
| POST | `/api/photos` | Raw file body; `X-Filename` + `X-Taken-At` headers |
| POST | `/api/photos/begin` | Start a chunked upload (videos) |
| POST | `/api/photos/chunk` | Append a chunk; `X-Upload-Id` + `X-Offset` |
| POST | `/api/photos/finish` | Verify hash and file it; `X-Upload-Id` |
| GET | `/api/photos/session?id=` | How many bytes of an upload we already hold |
| GET | `/api/pair` | URL + token for the phone, in one payload |
| GET | `/api/pair.svg` | The same as a scannable QR code |
| POST | `/api/mock/<action>/<bay>` | Fault injection, mock driver only |

---

## Photo backup

The DroboPix replacement, receiving half. It talks to the Drobo purely as a
file share, so **it works today with no protocol reverse engineering at all.**

Set `photos.target_dir` to a share on the Drobo — a mapped drive (`Z:\Photos`)
or a UNC path (`\\drobo\Photos`).

The upload flow is two steps, and the first one is what makes it cheap:

1. The phone sends the SHA-256 hashes of photos it's holding to
   `/api/photos/have`. The agent replies with just the ones it has never seen.
2. The phone uploads only those, one at a time, to `/api/photos`.

That means an interrupted backup resumes for free, and reinstalling the app
doesn't re-upload your entire camera roll.

Files are filed as `target_dir/YYYY/YYYY-MM/name`, using the `X-Taken-At` date
the phone supplies. Duplicates are detected by content, so the same photo sent
twice is stored once — but two *different* photos that happen to share a name
(`IMG_0042.JPG` is not unique across devices) are both kept, with `~1`
appended. Uploads are written to a `.part` file and renamed into place, so a
dropped connection can't leave a half-written photo in your library.

Filenames from the phone are stripped to a safe basename before use — a
filename like `../../../escape.jpg` lands inside the target directory, not
outside it. Only image and video extensions are accepted; this is not a
general-purpose file drop.

---

## Alerts

Alerts are sticky: they stay active until the condition clears, so the apps can
show "here's what's wrong right now" rather than a stream of one-off messages.
Long-running problems are re-sent every `resend_seconds` so they don't get
forgotten, and a "Resolved:" message goes out when a condition clears.

Rules that fire today: drive failed, drive reporting a problem, drive over the
temperature thresholds, **chassis temperature over threshold** (warning at
50 °C, critical at 60 °C, from the real `eCmdGetSysInfo` reading), array over
the capacity thresholds, and Drobo unreachable.

To get alerts on your phone, add `"ntfy"` to `notify.backends` and set a topic:

```json
"notify": {
  "backends": ["console", "ntfy"],
  "ntfy": { "server": "https://ntfy.sh", "topic": "drobo-<pick-a-long-random-name>" }
}
```

Install the ntfy app, subscribe to that topic, done. **Pick a long random topic
name and treat it like a password** — on the public ntfy.sh server, anyone who
knows the topic can read it.

This is the stopgap until the iOS app has proper push notifications, and it's
worth having regardless: it works whether or not the app is installed.

---

## Security

- **Binds to `127.0.0.1` by default** — this PC only. Change `agent.bind` to
  `0.0.0.0` when you want the phone to reach it, and only ever on your own LAN.
- **Never port-forward this.** For access away from home, put Tailscale on the
  machine running the agent.
- The token is a shared secret in plain HTTP. That's fine on a home LAN;
  it is not fine on an untrusted network.
- `config.json` contains the token and, later, your Drobo password. It's in
  `.gitignore` — keep it that way.
