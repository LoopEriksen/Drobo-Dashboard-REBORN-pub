"""
The agent's built-in web dashboard.

One self-contained page -- no frameworks, no CDN, no build step -- served at /.
It works in any browser on the LAN, which means you get a usable Drobo UI on
the iPhone today, through Safari, without Xcode or a developer account.

It also doubles as the layout prototype for the WinUI 3 app: if a panel doesn't
work here, it won't work there either.

The HTML shell itself needs no token (it contains no data). Every piece of
information on the page arrives from the authenticated JSON API, so the page
asks for the token once and keeps it in sessionStorage.
"""

from __future__ import annotations

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>Drobo Agent</title>
<style>
:root {
  --bg: #f4f5f7; --panel: #ffffff; --ink: #16181d; --muted: #666d7a;
  --line: #dfe3e9; --accent: #2f6feb;
  --ok: #1a7f47; --warn: #a76b00; --crit: #c22f2f; --unknown: #6b7280;
  --ok-bg: #e6f4ec; --warn-bg: #fdf3e0; --crit-bg: #fbeaea; --unknown-bg: #eef0f3;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #111317; --panel: #191c22; --ink: #e8eaee; --muted: #9aa3b0;
    --line: #2a2f38; --accent: #5c8dff;
    --ok: #4ec98a; --warn: #e0a94a; --crit: #ff6b6b; --unknown: #8b93a1;
    --ok-bg: #14301f; --warn-bg: #33260f; --crit-bg: #3a1b1b; --unknown-bg: #22262e;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 1.25rem; background: var(--bg); color: var(--ink);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 62rem; margin: 0 auto; }
header { display: flex; flex-wrap: wrap; gap: .75rem; align-items: baseline;
         justify-content: space-between; margin-bottom: 1.25rem; }
h1 { font-size: 1.35rem; margin: 0; letter-spacing: -.01em; }
h2 { font-size: .8rem; text-transform: uppercase; letter-spacing: .06em;
     color: var(--muted); margin: 0 0 .6rem; font-weight: 600; }
.sub { color: var(--muted); font-size: .85rem; }
.panel { background: var(--panel); border: 1px solid var(--line);
         border-radius: 12px; padding: 1rem 1.1rem; margin-bottom: 1rem; }
.pill { display: inline-flex; align-items: center; gap: .4rem; padding: .2rem .65rem;
        border-radius: 999px; font-size: .8rem; font-weight: 600; }
.pill::before { content: ""; width: .5rem; height: .5rem; border-radius: 50%;
                background: currentColor; }
.s-ok { color: var(--ok); background: var(--ok-bg); }
.s-warning { color: var(--warn); background: var(--warn-bg); }
.s-failed, .s-critical { color: var(--crit); background: var(--crit-bg); }
.s-unknown, .s-empty { color: var(--unknown); background: var(--unknown-bg); }
/* The network-mismatch banner. Warning-coloured rather than critical: the
   array is almost certainly fine, and the thing that needs attention is this
   PC. Painting it red would say the opposite. */
.netbanner { border-color: var(--warn); background: var(--warn-bg); }
.netbanner h2 { color: var(--warn); }
.netbanner p { margin: 0 0 .4rem; }
.netbanner p:last-child { margin-bottom: 0; }
.netheadline { font-weight: 600; }
/* Informational, not a warning: nothing here needs fixing and the one action
   that would "fix" it is the dangerous one. Accent-bordered, not amber. */
.fwnote { border-color: var(--accent); }
.fwnote h2 { color: var(--accent); }
.fwnote p { margin: 0 0 .4rem; }
.fwnote p:last-child { margin-bottom: 0; }
.bar { height: 12px; border-radius: 999px; background: var(--unknown-bg);
       overflow: hidden; margin: .55rem 0 .4rem; }
.bar > i { display: block; height: 100%; background: var(--accent); transition: width .4s; }
.bar.warn > i { background: var(--warn); }
.bar.crit > i { background: var(--crit); }
.figures { display: flex; flex-wrap: wrap; gap: 1rem; margin-top: .6rem; }
.figures div { min-width: 5rem; }
.figures b { display: block; font-size: 1.05rem; font-variant-numeric: tabular-nums; }
.figures span { color: var(--muted); font-size: .72rem; }
/* Compact on purpose. All five bays should be readable at once without
   scrolling, on a laptop and on a phone -- so the cards are dense rather than
   airy, and rows that would say nothing (no errors, full SSD life, a
   temperature the device doesn't measure) are omitted rather than shown blank. */
.bays { display: grid; gap: .45rem;
        grid-template-columns: repeat(auto-fill, minmax(11.5rem, 1fr)); }
.bay { border: 1px solid var(--line); border-radius: 8px; padding: .45rem .55rem; }
.bay.empty { opacity: .55; }
.bay .top { display: flex; justify-content: space-between; align-items: center;
            gap: .4rem; margin-bottom: .3rem; }
.bay .name { font-weight: 600; font-size: .82rem; }
.bay dl { margin: 0; font-size: .74rem; color: var(--muted);
          display: grid; grid-template-columns: auto 1fr; gap: .02rem .45rem; }
/* Wrap rather than truncate. These cards are narrow by design, but a serial
   number cut off halfway is useless precisely when you need it -- ordering a
   replacement drive. Better a card one line taller than a fact you have to
   hover to read. */
.bay dd { margin: 0; color: var(--ink); font-variant-numeric: tabular-nums;
          overflow-wrap: anywhere; }
.bay .note { margin-top: .3rem; font-size: .72rem; color: var(--warn); }
.panel { padding: .8rem .9rem; }
h2 { font-size: .72rem; }
ul.alerts { list-style: none; margin: 0; padding: 0; }
ul.alerts li { display: flex; gap: .6rem; align-items: flex-start;
               padding: .55rem 0; border-bottom: 1px solid var(--line); }
ul.alerts li:last-child { border-bottom: 0; }
.quiet { color: var(--muted); }
.activity-note { font-size: .74rem; margin: .5rem 0 0; line-height: 1.45; }
.vol-intro { font-size: .78rem; margin: 0 0 .6rem; line-height: 1.45; }
.vols { display: grid; gap: .45rem;
        grid-template-columns: repeat(auto-fill, minmax(13rem, 1fr)); }
.vol { border: 1px solid var(--line); border-radius: 8px; padding: .5rem .6rem; }
.vol .name { font-weight: 600; font-size: .84rem; }
.vol dl { margin: .3rem 0 0; font-size: .74rem; color: var(--muted);
          display: grid; grid-template-columns: auto 1fr; gap: .02rem .45rem; }
.vol dd { margin: 0; color: var(--ink); overflow-wrap: anywhere; }
.toolrow { display: flex; align-items: center; gap: .7rem; flex-wrap: wrap; }
.toolrow .quiet { font-size: .78rem; flex: 1 1 16rem; }
.btn { font: inherit; font-size: .82rem; padding: .4rem .85rem; border-radius: 6px;
       border: 1px solid var(--accent); background: var(--accent); color: #fff;
       cursor: pointer; }
.btn:hover { filter: brightness(1.1); }
.btn:disabled { opacity: .55; cursor: default; filter: none; }
.btn:focus-visible { outline: 2px solid var(--ink); outline-offset: 2px; }
.btn-quiet { background: transparent; color: var(--ink); border-color: var(--line); }
.apps { display: grid; gap: .4rem; }
.app { border: 1px solid var(--line); border-radius: 6px; padding: .5rem .6rem;
       display: grid; grid-template-columns: auto 1fr auto; gap: .1rem .6rem;
       align-items: baseline; }
.app .nm { font-weight: 600; font-size: .84rem; }
.app .ver { color: var(--muted); font-size: .72rem; font-variant-numeric: tabular-nums; }
.app .desc { grid-column: 1 / -1; color: var(--muted); font-size: .74rem; }
.run { font-size: .68rem; text-transform: uppercase; letter-spacing: .05em;
       padding: .1rem .45rem; border-radius: 999px; }
.run.on { color: var(--ok); background: var(--ok-bg); }
.run.off { color: var(--muted); background: var(--unknown-bg); }
.activity-idle { font-size: .9rem; color: var(--muted); margin: 0; }
/* Resolved alerts are history, not news -- present and readable, but visibly
   quieter than something still wrong. */
.resolved-head { margin-top: 1rem; }
ul.alerts.resolved li { opacity: .6; }
svg.spark { width: 100%; height: 70px; display: block; }
dl.cfg { margin: 0; display: grid; grid-template-columns: auto 1fr;
         gap: .15rem .9rem; font-size: .85rem; }
dl.cfg dt { color: var(--muted); }
dl.cfg dd { margin: 0; color: var(--ink); font-variant-numeric: tabular-nums; }
.cfg-shares { margin-top: .8rem; color: var(--muted); font-size: .78rem;
              text-transform: uppercase; letter-spacing: .05em; }
ul.shares { list-style: none; margin: .35rem 0 0; padding: 0;
            display: flex; flex-wrap: wrap; gap: .4rem; }
ul.shares li { border: 1px solid var(--line); border-radius: 8px;
               padding: .3rem .6rem; font-size: .85rem;
               display: flex; align-items: center; gap: .45rem; }
.tag { font-size: .7rem; color: var(--accent); border: 1px solid var(--accent);
       border-radius: 999px; padding: .05rem .4rem; }
/* The path to type into Explorer or a phone's file app. Selectable on purpose:
   copying it is the whole point. */
.unc { font-family: ui-monospace, Consolas, monospace; font-size: .78rem;
       color: var(--muted); user-select: all; }
/* Something worth reading, but not a fault. Used for the Windows guest-logon
   explanation and the link-speed warning -- neither means the Drobo is broken,
   so neither gets the red treatment. */
.notice { margin-top: .8rem; border-radius: 8px; padding: .55rem .7rem;
          font-size: .82rem; line-height: 1.45;
          color: var(--warn); background: var(--warn-bg);
          border: 1px solid var(--warn); }
.notice b { display: block; margin-bottom: .15rem; }
button { font: inherit; padding: .35rem .7rem; border-radius: 8px; cursor: pointer;
         border: 1px solid var(--line); background: var(--panel); color: var(--ink); }
button:hover { border-color: var(--accent); }
input[type=password], input[type=text] { font: inherit; padding: .45rem .6rem;
  border-radius: 8px; border: 1px solid var(--line);
  background: var(--panel); color: var(--ink); min-width: 16rem; }
.mock { display: flex; flex-wrap: wrap; gap: .4rem; }
.live { font-size: .78rem; color: var(--muted); }
.live.on { color: var(--ok); }
footer { margin-top: 1.5rem; font-size: .78rem; color: var(--muted); }
#gate { max-width: 26rem; margin: 4rem auto; }
[hidden] { display: none !important; }
</style>
</head>
<body>

<div class="wrap">

  <!-- shown until a token is supplied -->
  <div id="gate" class="panel" hidden>
    <h1>Drobo Agent</h1>
    <p class="sub">Paste the access token from <code>agent/config.json</code>.</p>
    <p><input type="password" id="tokenInput" placeholder="access token" autocomplete="off"></p>
    <p><button id="tokenSave">Connect</button>
       <span id="tokenError" class="sub" style="color:var(--crit)"></span></p>
  </div>

  <div id="app" hidden>
    <header>
      <div>
        <h1 id="devName">Drobo</h1>
        <div class="sub" id="devSub">&nbsp;</div>
      </div>
      <div style="text-align:right">
        <span class="pill s-unknown" id="statePill">connecting</span>
        <div class="live" id="liveFlag">offline</div>
      </div>
    </header>

    <!-- Why the Drobo can't be reached, when the answer is "this PC is on the
         wrong network". Sits ABOVE capacity because when it is showing, every
         number below it is stale and this is the only thing worth reading.
         Hidden entirely otherwise -- see network_hint in monitor.py, which is
         null unless the device is unreachable AND we can actually explain it. -->
    <section class="panel netbanner" id="netPanel" hidden>
      <h2>Network</h2>
      <p id="netHeadline" class="netheadline"></p>
      <p id="netDetail"></p>
      <p id="netCaveat" class="sub"></p>
    </section>

    <!-- Standing note about the running firmware. Shown only for the two builds
         Drobo withdrew, and placed BELOW the network banner because it is
         information rather than a problem to fix — see firmware.py, which
         explains at length why this is not an alert and why it never suggests
         changing firmware. -->
    <section class="panel fwnote" id="fwPanel" hidden>
      <h2>Firmware</h2>
      <p id="fwHeadline" class="netheadline"></p>
      <p id="fwDetail" class="sub"></p>
    </section>

    <section class="panel" id="capPanel">
      <h2>Capacity</h2>
      <div class="bar" id="capBar"><i style="width:0"></i></div>
      <div class="figures">
        <div><b id="capUsed">–</b><span>used</span></div>
        <div><b id="capFree">–</b><span>free</span></div>
        <div><b id="capUsable">–</b><span>usable</span></div>
        <div><b id="capProt">–</b><span>protection</span></div>
        <div><b id="capRaw">–</b><span>raw</span></div>
      </div>
    </section>

    <section class="panel" id="rebuildPanel" hidden>
      <h2>Rebuild</h2>
      <p id="rebuildNote"></p>
    </section>

    <section class="panel">
      <h2>Drive bays</h2>
      <div class="bays" id="bays"></div>
    </section>

    <!-- Software installed ON the Drobo. Fetched on demand, never polled:
         the list changes when someone installs something, which is roughly
         never, and each fetch costs a command-port connection. -->
    <section class="panel" id="appsPanel" hidden>
      <h2>Installed on the Drobo</h2>
      <div id="appsList"></div>
      <p class="quiet activity-note" id="appsSdk"></p>
    </section>

    <!-- The only control on this page that changes anything on the device.
         Kept beside the tools it belongs with, and labelled so you know what
         it does before you press it rather than after. -->
    <section class="panel" id="toolsPanel">
      <h2>Tools</h2>
      <div class="toolrow">
        <button type="button" id="identifyBtn" class="btn">Blink the lights</button>
        <button type="button" id="identifyStopBtn" class="btn btn-quiet">Stop</button>
        <!-- "It stops on its own" until 2026-08-06, which described a brief
             flash. Dashboard's own warning says the lights blink CONTINUOUSLY
             for 15 minutes and the same button stops them, so there is a Stop
             here now. -->
        <span class="quiet" id="identifyMsg">Makes the Drobo blink its front lights, so you
          can tell which box is which. The original Dashboard blinked for 15
          minutes -- press Stop once you have found it.</span>
      </div>
      <div class="toolrow" style="margin-top:.6rem">
        <button type="button" id="appsBtn" class="btn btn-quiet">Show installed software</button>
        <span class="quiet">Asks the Drobo what apps are on it.</span>
      </div>
    </section>

    <!-- What the pack is carved into. Hidden when the device reports none,
         which is what a driver that has never seen a real greeting does. -->
    <section class="panel" id="volumesPanel" hidden>
      <h2>Volumes</h2>
      <p class="quiet vol-intro" id="volumesIntro"></p>
      <div class="vols" id="volumes"></div>
    </section>

    <!-- Deliberately called "recent activity", not "throughput". The device's
         counters lag real use by about 25 seconds, and the agent only re-asks
         every two minutes, so this is never a live rate. See the note under
         it, which exists so that a 0 during a big copy reads as normal rather
         than as a bug. -->
    <section class="panel" id="activityPanel" hidden>
      <h2>Recent activity</h2>
      <div class="figures" id="activityFigures"></div>
      <p class="quiet activity-note" id="activityNote"></p>
    </section>

    <!-- Hidden entirely when nothing is wrong. An always-present panel saying
         "Nothing to report" costs a screenful of space to tell you the thing
         you already know; when it DOES appear, it means something. -->
    <section class="panel" id="alertsPanel" hidden>
      <h2>Active alerts</h2>
      <ul class="alerts" id="alerts"></ul>
      <div id="alertsResolvedWrap" hidden>
        <h2 class="resolved-head">Resolved</h2>
        <ul class="alerts resolved" id="alertsResolved"></ul>
      </div>
    </section>

    <section class="panel" id="configPanel" hidden>
      <h2>Network &amp; shares</h2>
      <div id="configBody" class="sub">Loading&hellip;</div>
    </section>

    <section class="panel">
      <h2>Last 24 hours &mdash; warmest drive</h2>
      <svg class="spark" id="sparkTemp" viewBox="0 0 600 70" preserveAspectRatio="none"></svg>
      <div class="sub" id="sparkLabel">&nbsp;</div>
    </section>

    <section class="panel" id="mockPanel" hidden>
      <h2>Simulator controls</h2>
      <p class="sub">The mock driver is running, so nothing here touches real
         hardware. Use it to prove the alert path works.</p>
      <div class="mock" id="mockButtons"></div>
    </section>

    <footer>
      <span id="agentVersion"></span> &middot;
      <a href="#" id="forget">forget token</a>
    </footer>
  </div>
</div>

<script>
(function () {
  "use strict";

  var KEY = "drobo-agent-token";
  var token = "";

  // Accept ?token=... once, stash it, then scrub it out of the address bar so
  // it doesn't sit in history or get shared by accident.
  var fromUrl = new URLSearchParams(location.search).get("token");
  if (fromUrl) {
    sessionStorage.setItem(KEY, fromUrl);
    history.replaceState(null, "", location.pathname);
  }
  token = sessionStorage.getItem(KEY) || "";

  var $ = function (id) { return document.getElementById(id); };

  function api(path) {
    return fetch(path, { headers: { "X-Agent-Token": token } }).then(function (r) {
      if (r.status === 401) throw new Error("unauthorised");
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  function post(path) {
    return fetch(path, { method: "POST", headers: { "X-Agent-Token": token } });
  }

  function bytes(n) {
    if (n === null || n === undefined) return "–";
    var units = ["B", "KB", "MB", "GB", "TB", "PB"], i = 0;
    while (Math.abs(n) >= 1000 && i < units.length - 1) { n /= 1000; i++; }
    return (i === 0 ? n : n.toFixed(n < 10 ? 2 : 1)) + " " + units[i];
  }

  function ago(ts) {
    if (!ts) return "never";
    var s = Math.max(0, Math.round(Date.now() / 1000 - ts));
    if (s < 60) return s + "s ago";
    if (s < 3600) return Math.round(s / 60) + "m ago";
    return Math.round(s / 3600) + "h ago";
  }

  // How long the Drobo has been switched on. Days matter, seconds don't.
  function uptime(sec) {
    var d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600);
    if (d) return d + (d === 1 ? " day" : " days") + (h ? " " + h + "h" : "");
    if (h) return h + "h";
    return Math.max(1, Math.floor(sec / 60)) + "m";
  }

  // -- rendering -----------------------------------------------------------

  function renderSnapshot(s) {
    if (!s) return;
    var dev = s.device || {}, cap = s.capacity || {};

    $("devName").textContent = dev.name || dev.model || "Drobo";
    var bits = [];
    if (dev.model) bits.push(dev.model);
    if (dev.firmware) bits.push("firmware " + dev.firmware);
    if (dev.ip) bits.push(dev.ip);
    if (s.source) bits.push(s.source + " driver");
    // Chassis temperature, always with its age. The Drobo answers this
    // command only sometimes, so a reading can be a few minutes old -- shown
    // without the age it would look live, which would be a lie.
    if (dev.temperature_c != null) {
      bits.push(dev.temperature_c + " °C"
                + (dev.temperature_at ? " (" + ago(dev.temperature_at) + ")" : ""));
    }
    if (dev.uptime_seconds != null) bits.push("up " + uptime(dev.uptime_seconds));
    $("devSub").textContent = bits.join(" · ");

    var state = s.reachable ? (s.overall_state || "unknown") : "unknown";
    var pill = $("statePill");
    pill.className = "pill s-" + state;
    pill.textContent = s.reachable ? state : "unreachable";
    $("liveFlag").textContent = "updated " + ago(s.taken_at);

    // "unreachable" on its own reads as "your NAS is broken". When the real
    // reason is that this PC has wandered onto a different Wi-Fi network, the
    // agent says so in network_hint and we show it instead of leaving someone
    // to go and check on a healthy array. Present only when the agent can
    // actually explain the failure, so its presence is the whole test.
    // Firmware note: `notable` is true only for the two builds Drobo pulled,
    // so one field decides this and the UI never has to know the version rules.
    var fw = s.firmware_advisory;
    $("fwPanel").hidden = !(fw && fw.notable);
    if (fw && fw.notable) {
      $("fwHeadline").textContent = fw.headline || "";
      $("fwDetail").textContent = fw.detail || "";
    }

    var net = s.network_hint;
    $("netPanel").hidden = !net;
    if (net) {
      $("netHeadline").textContent = net.headline || "";
      // detail is written to follow the headline (it opens with "It is on..."),
      // so the two are always rendered together -- see Diagnosis in netcheck.py.
      $("netDetail").textContent = net.detail || "";
      $("netCaveat").textContent = net.caveat || "";
    }

    var frac = cap.used_fraction || 0;
    var bar = $("capBar");
    bar.className = "bar" + (frac >= 0.95 ? " crit" : frac >= 0.85 ? " warn" : "");
    bar.firstElementChild.style.width = Math.min(100, frac * 100).toFixed(1) + "%";
    $("capUsed").textContent = bytes(cap.used_bytes);
    $("capFree").textContent = bytes(cap.free_bytes);
    $("capUsable").textContent = bytes(cap.usable_bytes);
    $("capProt").textContent = bytes(cap.protection_bytes);
    $("capRaw").textContent = bytes(cap.raw_bytes);

    var host = $("bays");
    host.textContent = "";
    (s.drives || []).forEach(function (d) {
      var card = document.createElement("div");
      card.className = "bay" + (d.present ? "" : " empty");

      var top = document.createElement("div");
      top.className = "top";
      var name = document.createElement("span");
      name.className = "name";
      name.textContent = "Bay " + d.bay;
      var badge = document.createElement("span");
      badge.className = "pill s-" + (d.state || "unknown");
      badge.textContent = d.present ? (d.state || "unknown") : "empty";
      top.appendChild(name); top.appendChild(badge);
      card.appendChild(top);

      if (d.present) {
        var dl = document.createElement("dl");
        // The model string arrives with runs of internal padding spaces
        // ("WDC      WD30EFRX-68A"); collapse them so the row stays narrow.
        var rows = [["Model", (d.model || "–").replace(/\s+/g, " ").trim()],
         ["Size", bytes(d.capacity_bytes)],
         ["Serial", d.serial || "–"]];
        // Only worth a row if the device actually measures it. A 5N reports
        // null here, and a permanently blank "Temp –" is just noise -- the
        // chassis reading is shown beside the device name instead.
        if (d.temperature_c != null) rows.push(["Temp", d.temperature_c.toFixed(1) + " °C"]);
        if (d.firmware_rev) rows.push(["Firmware", d.firmware_rev]);
        // A healthy array should look calm -- only show these when there's
        // something to see. 0 errors and 100% life stay invisible.
        if (d.error_count) rows.push(["Errors", String(d.error_count)]);
        if (d.ssd_life_remaining != null && d.ssd_life_remaining < 100)
          rows.push(["SSD life", d.ssd_life_remaining + "%"]);
        rows.forEach(function (pair) {
          var dt = document.createElement("dt"); dt.textContent = pair[0];
          var dd = document.createElement("dd"); dd.textContent = pair[1];
          dd.title = pair[1];
          if (pair[0] === "Errors" || pair[0] === "SSD life")
            dd.style.color = "var(--warn)";
          dl.appendChild(dt); dl.appendChild(dd);
        });
        card.appendChild(dl);
        if (d.note) {
          var note = document.createElement("div");
          note.className = "note"; note.textContent = d.note;
          card.appendChild(note);
        }
      }
      host.appendChild(card);
    });

    // Calm by default: the rebuild panel only appears when there's actually
    // something rebuilding or double-degraded, per pack_health.
    var ph = s.pack_health || {};
    var rebuildPanel = $("rebuildPanel");
    if (ph.double_degraded_count) {
      rebuildPanel.hidden = false;
      $("rebuildNote").textContent =
        "Double-degraded count is " + ph.double_degraded_count +
        " -- two drives were degraded at once; the array's protection may have been exhausted.";
    } else if (ph.relayout_count) {
      rebuildPanel.hidden = false;
      $("rebuildNote").textContent =
        "Relayout count is " + ph.relayout_count +
        " -- rebuilding, this is normal after a drive change.";
    } else {
      rebuildPanel.hidden = true;
    }

    renderActivity(s.performance);
    renderVolumes(s.volumes, s.max_volumes, cap);
  }

  // The volumes the pack is carved into -- what Windows shows as drive letters.
  //
  // The honesty problem here is max_size_bytes. The device reports 70 TB, which
  // is the CEILING a volume may grow to, not its size. This array holds 8.76 TB
  // usable, so printing "70 TB" beside a volume would be flatly misleading --
  // it reads as free space that does not exist. So it is labelled as a limit,
  // and the real number people want (how full the array is) stays in Capacity
  // where it belongs.
  function renderVolumes(vols, maxVols, cap) {
    var panel = $("volumesPanel");
    if (!vols || !vols.length) { panel.hidden = true; return; }
    panel.hidden = false;

    var n = vols.length;
    var intro = n === 1
      ? "Your Drobo presents its storage as a single volume"
      : "Your Drobo presents its storage as " + n + " volumes";
    if (maxVols) intro += ", out of " + maxVols + " it could hold";
    intro += ". A volume is what appears in Windows as a drive letter; "
           + "it grows as you add drives rather than being a fixed slice.";
    $("volumesIntro").textContent = intro;

    var host = $("volumes");
    host.textContent = "";
    vols.forEach(function (v, i) {
      var card = document.createElement("div");
      card.className = "vol";
      var name = document.createElement("div");
      name.className = "name";
      // The device leaves the name empty on a single-volume pack. Say
      // "Volume 1" rather than inventing a name the Drobo never gave it.
      name.textContent = v.name || ("Volume " + (v.lun + 1));
      card.appendChild(name);

      var dl = document.createElement("dl");
      var rows = [];
      if (v.partition_count != null)
        rows.push(["Partitions", String(v.partition_count)]);
      if (v.max_size_bytes)
        rows.push(["Can grow to", bytes(v.max_size_bytes)]);
      if (cap && cap.usable_bytes)
        rows.push(["Pack holds", bytes(cap.usable_bytes)]);
      if (v.unique_id) rows.push(["ID", v.unique_id]);

      rows.forEach(function (pair) {
        var dt = document.createElement("dt"); dt.textContent = pair[0];
        var dd = document.createElement("dd"); dd.textContent = pair[1];
        dl.appendChild(dt); dl.appendChild(dd);
      });
      card.appendChild(dl);
      host.appendChild(card);
    });
  }

  // The Drobo's own read/write counters.
  //
  // Three states, and telling them apart is the whole job:
  //   measured false  -> the device has never answered. NOT the same as idle.
  //   all zeros       -> genuinely idle, and worth saying so in words.
  //   anything else   -> show the figures.
  //
  // Note the explicit `== null` checks rather than `||` defaults: 0 is a real
  // reading here, and a falsy-default lookup would erase it. That trap has
  // already bitten this file once, in the alert severity sort.
  // The one control here that changes anything on the device. Disabled while
  // in flight so an impatient double-click can't send it twice.
  // Both buttons share one function so start and stop cannot drift apart in
  // their error handling, and both go down together while either is in
  // flight: they hit the same command port, which measurably dislikes
  // connection volume, and a Stop racing a Blink would land in an order
  // nobody chose.
  function sendIdentify(query, working) {
    var btn = $("identifyBtn"), stop = $("identifyStopBtn"), msg = $("identifyMsg");
    btn.disabled = true;
    stop.disabled = true;
    msg.textContent = working;
    fetch("/api/identify" + query, { method: "POST", headers: { "X-Agent-Token": token } })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok || res.j.error) {
          msg.textContent = res.j.error || "The Drobo didn't accept that.";
          return;
        }
        // The agent writes a message that already reflects what was sent --
        // blink, stop, or the no-interval control case. Never let a simulated
        // blink be mistaken for a real one.
        msg.textContent = (res.j.message || "Sent.")
          + (res.j.simulated ? "  (Simulated -- nothing real flashed.)" : "");
      })
      .catch(function (e) { msg.textContent = "Couldn't reach the agent: " + e.message; })
      .then(function () { btn.disabled = false; stop.disabled = false; });
  }

  $("identifyBtn").addEventListener("click", function () {
    sendIdentify("", "Asking the Drobo…");
  });
  // interval=0 -- see identify.py's STOP_INTERVAL for why this is the stop,
  // and why it is still a guess rather than a captured fact.
  $("identifyStopBtn").addEventListener("click", function () {
    sendIdentify("?interval=0", "Stopping…");
  });

  $("appsBtn").addEventListener("click", function () {
    var btn = $("appsBtn");
    btn.disabled = true;
    api("/api/droboapps")
      .then(renderApps)
      .catch(function (e) {
        $("appsPanel").hidden = false;
        $("appsList").textContent = "";
        $("appsSdk").textContent = "Couldn't ask the Drobo: " + e.message;
      })
      .then(function () { btn.disabled = false; });
  });

  function renderApps(d) {
    var panel = $("appsPanel"), host = $("appsList");
    panel.hidden = false;
    host.textContent = "";
    var apps = (d && d.apps) || [];
    if (!apps.length) {
      $("appsSdk").textContent = d && d.error
        ? "Couldn't ask the Drobo: " + d.error
        : "The Drobo reports nothing installed.";
      return;
    }
    var wrap = document.createElement("div");
    wrap.className = "apps";
    apps.forEach(function (a) {
      var row = document.createElement("div");
      row.className = "app";
      var nm = document.createElement("span");
      nm.className = "nm"; nm.textContent = a.name;
      var ver = document.createElement("span");
      ver.className = "ver"; ver.textContent = a.version ? "v" + a.version : "";
      var run = document.createElement("span");
      // `running` is already the right way round -- the agent flips the
      // device's inverted "Stopped" field. Don't re-invert it here.
      run.className = "run " + (a.running ? "on" : "off");
      run.textContent = a.running ? "running" : "stopped";
      row.appendChild(nm); row.appendChild(ver); row.appendChild(run);
      var extra = [a.description, a.status].filter(Boolean).join(" ");
      if (a.has_web_ui) extra += (extra ? "  " : "") + "Has a web interface.";
      if (extra) {
        var d2 = document.createElement("span");
        d2.className = "desc"; d2.textContent = extra;
        row.appendChild(d2);
      }
      wrap.appendChild(row);
    });
    host.appendChild(wrap);
    $("appsSdk").textContent =
      (d.sdk_version ? "DroboApps SDK " + d.sdk_version + ". " : "") +
      (d.simulated ? "This is the simulated Drobo." : "");
  }

  function renderActivity(p) {
    var panel = $("activityPanel");
    if (!p || p.measured !== true) {
      // Nothing measured yet. Say nothing rather than implying zero activity.
      panel.hidden = true;
      return;
    }
    panel.hidden = false;

    var read = p.read_mb_per_s == null ? 0 : p.read_mb_per_s;
    var write = p.write_mb_per_s == null ? 0 : p.write_mb_per_s;
    var iops = p.iops == null ? 0 : p.iops;
    var figures = $("activityFigures");
    figures.textContent = "";

    if (!read && !write && !iops) {
      var idle = document.createElement("p");
      idle.className = "activity-idle";
      idle.textContent = "Idle — nothing reading or writing.";
      figures.appendChild(idle);
    } else {
      [["Read", read + " MB/s"], ["Write", write + " MB/s"],
       ["Operations", iops + "/s"]].forEach(function (pair) {
        var d = document.createElement("div");
        var b = document.createElement("b");
        b.textContent = pair[1];
        var s2 = document.createElement("span");
        s2.textContent = pair[0].toLowerCase();
        d.appendChild(b); d.appendChild(s2);
        figures.appendChild(d);
      });
    }

    $("activityNote").textContent =
      "This is recent activity, not a live reading. The Drobo's own counters " +
      "lag by around half a minute and this is refreshed every couple of " +
      "minutes, so a big copy can be well underway before the numbers move — " +
      "seeing zero here while the drive light is busy is normal.";
  }

  // Note: rank lookups must not use `||` for the default -- critical is 0,
  // which is falsy, and would sort to the bottom instead of the top.
  var RANK = { critical: 0, warning: 1, info: 2 };
  function rank(sev) {
    return Object.prototype.hasOwnProperty.call(RANK, sev) ? RANK[sev] : 3;
  }

  function alertRow(a) {
    var li = document.createElement("li");
    var badge = document.createElement("span");
    badge.className = "pill s-" + a.severity;
    badge.textContent = a.severity;
    var text = document.createElement("div");
    text.textContent = a.message;
    var when = document.createElement("div");
    when.className = "sub";
    when.textContent = "since " + ago(a.first_seen)
                     + (a.active ? "" : " · cleared " + ago(a.cleared_at));
    text.appendChild(when);
    li.appendChild(badge); li.appendChild(text);
    return li;
  }

  // `list` is the FULL history -- resolved entries included -- so a problem
  // that came and went overnight leaves a trace instead of vanishing. Active
  // ones stay in the panel; resolved ones move to a quieter list below, which
  // is the same split the Windows app shows.
  function renderAlerts(list) {
    var host = $("alerts"), past = $("alertsResolved");
    host.textContent = ""; past.textContent = "";
    list = list || [];

    var active = list.filter(function (a) { return a.active !== false; });
    var resolved = list.filter(function (a) { return a.active === false; });

    var bySeverity = function (a, b) {
      var d = rank(a.severity) - rank(b.severity);
      return d !== 0 ? d : (a.first_seen || 0) - (b.first_seen || 0);
    };
    active.sort(bySeverity);
    // Most recently cleared first -- old news sinks.
    resolved.sort(function (a, b) { return (b.cleared_at || 0) - (a.cleared_at || 0); });

    // Nothing at all, ever -> the whole panel goes away, and the "ok" pill in
    // the header already says so.
    if (!active.length && !resolved.length) {
      $("alertsPanel").hidden = true;
      document.title = "Drobo Agent";
      return;
    }
    $("alertsPanel").hidden = false;

    // Only ACTIVE problems belong in the tab title -- a resolved one must not
    // keep nagging from a background tab.
    document.title = active.length
      ? "(" + active.length + ") Drobo Agent" : "Drobo Agent";

    if (active.length) {
      active.forEach(function (a) { host.appendChild(alertRow(a)); });
    } else {
      var calm = document.createElement("li");
      calm.className = "quiet";
      calm.textContent = "Nothing active right now.";
      host.appendChild(calm);
    }

    $("alertsResolvedWrap").hidden = !resolved.length;
    resolved.forEach(function (a) { past.appendChild(alertRow(a)); });
  }

  function renderSpark(points) {
    var svg = $("sparkTemp");
    svg.textContent = "";
    var vals = points.map(function (p) { return p.max_temp_c; })
                     .filter(function (v) { return v != null; });
    if (vals.length < 2) {
      // Distinguish "no data yet" from "this device never reports it". A real
      // Drobo 5N sends mTemperature=0 for every bay, so a permanently blank
      // graph is expected and shouldn't look like a fault.
      var haveReadings = points.length >= 2;
      $("sparkLabel").textContent = haveReadings
        ? "This Drobo doesn't measure each drive separately. Its chassis "
          + "temperature is shown beside the device name above."
        : "Not enough readings yet.";
      return;
    }
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    if (hi - lo < 1) { hi = lo + 1; }
    var step = 600 / (vals.length - 1);
    var d = vals.map(function (v, i) {
      var y = 65 - ((v - lo) / (hi - lo)) * 58;
      return (i ? "L" : "M") + (i * step).toFixed(1) + " " + y.toFixed(1);
    }).join(" ");

    var fill = document.createElementNS("http://www.w3.org/2000/svg", "path");
    fill.setAttribute("d", d + " L600 70 L0 70 Z");
    fill.setAttribute("fill", "currentColor");
    fill.setAttribute("opacity", "0.12");
    var line = document.createElementNS("http://www.w3.org/2000/svg", "path");
    line.setAttribute("d", d);
    line.setAttribute("fill", "none");
    line.setAttribute("stroke", "currentColor");
    line.setAttribute("stroke-width", "2");
    line.setAttribute("vector-effect", "non-scaling-stroke");
    svg.style.color = "var(--accent)";
    svg.appendChild(fill); svg.appendChild(line);
    $("sparkLabel").textContent =
      vals.length + " readings · " + lo.toFixed(1) + " °C to " + hi.toFixed(1) + " °C"
      + " · now " + vals[vals.length - 1].toFixed(1) + " °C";
  }

  // -- simulator controls --------------------------------------------------

  function buildMockControls(drives) {
    var host = $("mockButtons");
    host.textContent = "";
    drives.forEach(function (d) {
      [["fail", "Fail bay " + d.bay], ["pull", "Pull bay " + d.bay]].forEach(function (pair) {
        var b = document.createElement("button");
        b.textContent = pair[1];
        b.onclick = function () { post("/api/mock/" + pair[0] + "/" + d.bay).then(refresh); };
        host.appendChild(b);
      });
    });
    var reset = document.createElement("button");
    reset.textContent = "Reset all";
    reset.onclick = function () { post("/api/mock/reset").then(refresh); };
    host.appendChild(reset);
  }

  // -- wiring --------------------------------------------------------------

  var mockReady = false;

  function refresh() {
    return Promise.all([
      api("/api/status"),
      // ?all=1 is the full history, resolved alerts included. The Windows app
      // shows both; this page used to show only what was still active, so a
      // problem that came and went overnight left no trace here at all.
      api("/api/alerts?all=1"),
      api("/api/history?limit=1440")
    ]).then(function (r) {
      renderSnapshot(r[0]);
      renderAlerts(r[1].alerts);
      renderSpark(r[2].points);
      if (r[0].source === "drobo5n") loadConfig();
      if (!mockReady && r[0].source === "mock") {
        $("mockPanel").hidden = false;
        buildMockControls(r[0].drives || []);
        mockReady = true;
      }
    });
  }

  // Configuration comes from the device's COMMAND port, not the status push --
  // a separate connection, so it is fetched once rather than on every tick.
  // Configuration changes rarely, and a failure here must not disturb the rest
  // of the page: the panel simply stays hidden.
  var configLoaded = false;

  // Append an informational strip. Everything goes in via textContent -- the
  // wording comes from the device's own configuration, so it is never trusted
  // as markup.
  function notice(host, src, headKey, bodyKey) {
    if (!src || !src[bodyKey]) return;
    var box = document.createElement("div");
    box.className = "notice";
    var b = document.createElement("b");
    b.textContent = src[headKey] || "";
    var p = document.createElement("span");
    p.textContent = src[bodyKey];
    box.appendChild(b); box.appendChild(p);
    host.appendChild(box);
  }

  function loadConfig() {
    if (configLoaded) return;
    configLoaded = true;
    Promise.all([
      api("/api/droboconfig?section=network").catch(function () { return null; }),
      api("/api/shares").catch(function () { return null; })
    ]).then(function (r) {
      var net = r[0] && r[0].config &&
                r[0].config.DRINASConfig &&
                r[0].config.DRINASConfig.DRINasNetworkConfig;
      var sh = r[1] || null;                       // {shares, advice, link}
      var list = (sh && sh.shares) || [];
      if (!net && !list.length) return;   // command port unavailable; stay quiet

      var host = $("configBody");
      host.textContent = "";

      if (net) {
        var ip = net.IPConfig || {};
        var rows = [
          ["Address", ip.IP],
          ["Subnet", ip.Subnet],
          ["Gateway", ip.Gateway],
          ["DNS", [ip.DNS1, ip.DNS2].filter(Boolean).join(", ")],
          ["Workgroup", net.NasWorkgroup]
        ].filter(function (kv) { return kv[1]; });
        var dl = document.createElement("dl");
        dl.className = "cfg";
        rows.forEach(function (kv) {
          var dt = document.createElement("dt"); dt.textContent = kv[0];
          var dd = document.createElement("dd"); dd.textContent = kv[1];
          dl.appendChild(dt); dl.appendChild(dd);
        });
        host.appendChild(dl);
      }

      if (list.length) {
        var h = document.createElement("div");
        h.className = "cfg-shares";
        h.textContent = list.length + (list.length === 1 ? " share" : " shares");
        host.appendChild(h);
        var ul = document.createElement("ul");
        ul.className = "shares";
        list.forEach(function (s) {
          var li = document.createElement("li");
          var nm = document.createElement("b");
          nm.textContent = s.name || "(unnamed)";
          li.appendChild(nm);
          if (s.time_machine) {
            var tm = document.createElement("span");
            tm.className = "tag";
            tm.textContent = "Time Machine";
            li.appendChild(tm);
          }
          // The path itself, so it can be copied straight into Explorer, a
          // phone's file app, or a Finder "Connect to Server" box.
          if (s.unc) {
            var p = document.createElement("span");
            p.className = "unc";
            p.textContent = s.unc;
            li.appendChild(p);
          }
          ul.appendChild(li);
        });
        host.appendChild(ul);
      }

      // Two things worth saying out loud, both explained by the agent rather
      // than invented here, so the wording stays identical everywhere.
      notice(host, sh && sh.advice && sh.advice.guest_shares &&
                   sh.advice.guest_shares.length ? sh.advice : null,
             "headline", "detail");
      if (sh && sh.link && sh.link.degraded && sh.link.note) {
        notice(host, {h: "This Drobo's network is running slowly.",
                      d: sh.link.note}, "h", "d");
      }
      $("configPanel").hidden = false;
    });
  }

  function connectStream() {
    var es = new EventSource("/api/events?token=" + encodeURIComponent(token));
    es.addEventListener("snapshot", function (e) {
      renderSnapshot(JSON.parse(e.data).data);
      $("liveFlag").classList.add("on");
    });
    es.addEventListener("alert", function () {
      // ?all=1 here too. renderAlerts() splits the full history into active
      // and resolved, so handing it the active-only list would wipe the
      // resolved section every time a new alert fired.
      api("/api/alerts?all=1").then(function (r) { renderAlerts(r.alerts); });
    });
    es.onerror = function () {
      $("liveFlag").classList.remove("on");
      $("liveFlag").textContent = "reconnecting…";
    };
  }

  function start() {
    $("gate").hidden = true;
    $("app").hidden = false;
    refresh().then(function () {
      connectStream();
      // The stream carries snapshots; poll the slower things occasionally.
      setInterval(function () {
        api("/api/history?limit=1440").then(function (r) { renderSpark(r.points); });
      }, 60000);
      return fetch("/api/ping").then(function (r) { return r.json(); });
    }).then(function (p) {
      if (p) $("agentVersion").textContent = "Drobo Agent " + p.version;
    }).catch(showGate);
  }

  function showGate(err) {
    $("app").hidden = true;
    $("gate").hidden = false;
    if (err && String(err.message).indexOf("unauthorised") >= 0) {
      $("tokenError").textContent = "That token was rejected.";
    }
  }

  $("tokenSave").onclick = function () {
    token = $("tokenInput").value.trim();
    if (!token) return;
    sessionStorage.setItem(KEY, token);
    $("tokenError").textContent = "";
    start();
  };
  $("tokenInput").addEventListener("keydown", function (e) {
    if (e.key === "Enter") $("tokenSave").click();
  });
  $("forget").onclick = function (e) {
    e.preventDefault();
    sessionStorage.removeItem(KEY);
    location.reload();
  };

  if (token) { start(); } else { showGate(); }
})();
</script>
</body>
</html>
"""
