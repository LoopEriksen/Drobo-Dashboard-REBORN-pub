using DroboDashboardReborn;

// Tests for AlertNotifier -- when this app is allowed to interrupt somebody.
//
// The owner reported twice that this app was intrusive, and the rules here are
// the whole of the promise made when notifications were allowed back in:
// alerts only, each type switchable, everything off until switched on, and
// never a second time for something you already know about.
//
//     dotnet run --project windows/DroboDashboardReborn.Tests

var fails = new List<string>();

void Check(string label, bool cond, object? extra = null)
{
    Console.WriteLine((cond ? "  PASS  " : "  FAIL  ") + label
        + (!cond && extra is not null ? "  " + extra : ""));
    if (!cond) fails.Add(label);
}

static (string, string, string) Alert(string key, string sev = "critical", string msg = "something happened")
    => (key, sev, msg);

static Dictionary<string, bool> AllOn()
{
    var d = AlertNotifier.DefaultEnabled();
    foreach (var k in d.Keys.ToList()) d[k] = true;
    return d;
}

// ---------------------------------------------------------------------------
Console.WriteLine("\n== a fresh install is silent ==");
var defaults = AlertNotifier.DefaultEnabled();
Check("every category defaults to off", defaults.Values.All(v => !v),
      string.Join(",", defaults.Where(kv => kv.Value).Select(kv => kv.Key)));
Check("every listed category has a default", AlertNotifier.Categories.All(c => defaults.ContainsKey(c.Id)));
Check("categories all have a label and a description",
      AlertNotifier.Categories.All(c => c.Label.Length > 0 && c.Description.Length > 0));

// ---------------------------------------------------------------------------
Console.WriteLine("\n== RULE 2: opening the app never announces what you came to look at ==");
// Three standing alerts must produce zero notifications. You are already
// looking at the screen that lists them.
var n = new AlertNotifier();
var first = n.Evaluate(
    new[] { Alert("drive-failed-3"), Alert("capacity-critical"), Alert("unreachable") },
    masterEnabled: true, enabled: AllOn());
Check("first pass notifies about nothing", first.Count == 0, first.Count);

// ...but it did LEARN them, so they stay quiet next time too.
var second = n.Evaluate(
    new[] { Alert("drive-failed-3"), Alert("capacity-critical"), Alert("unreachable") },
    masterEnabled: true, enabled: AllOn());
Check("-- and they stay quiet on the next pass", second.Count == 0, second.Count);

// ---------------------------------------------------------------------------
Console.WriteLine("\n== a NEW alert after that does notify ==");
var third = n.Evaluate(
    new[] { Alert("drive-failed-3"), Alert("capacity-critical"), Alert("unreachable"),
            Alert("battery-failed") },
    masterEnabled: true, enabled: AllOn());
Check("exactly the new alert notifies", third.Count == 1, third.Count);
Check("-- and it is the right one", third.Count == 1 && third[0].Key == "battery-failed", third.FirstOrDefault());
Check("-- carrying its category", third.Count == 1 && third[0].Category == "battery", third.FirstOrDefault());
Check("-- and the message the agent wrote",
      third.Count == 1 && third[0].Message == "something happened", third.FirstOrDefault());

// ---------------------------------------------------------------------------
Console.WriteLine("\n== RULE 3: never twice for the same standing problem ==");
// The agent re-sends long-running alerts on its own timer so they are not
// forgotten. That is right for a phone push and wrong for a desktop toast
// every hour about a drive you already know is dead.
var repeat = n.Evaluate(
    new[] { Alert("drive-failed-3"), Alert("battery-failed") },
    masterEnabled: true, enabled: AllOn());
Check("a still-active alert does not notify again", repeat.Count == 0, repeat.Count);

// ---------------------------------------------------------------------------
Console.WriteLine("\n== RULE 4: but again if it clears and comes back ==");
n.Evaluate(new[] { Alert("drive-failed-3") }, true, AllOn());          // battery cleared
var returned = n.Evaluate(
    new[] { Alert("drive-failed-3"), Alert("battery-failed") }, true, AllOn());
Check("an alert that returned is new information", returned.Count == 1, returned.Count);
Check("-- and it is the one that came back",
      returned.Count == 1 && returned[0].Key == "battery-failed", returned.FirstOrDefault());

// ---------------------------------------------------------------------------
Console.WriteLine("\n== RULE 1: the switches actually switch ==");
var m = new AlertNotifier();
m.Evaluate(Array.Empty<(string, string, string)>(), true, AllOn());     // seed

var masterOff = m.Evaluate(new[] { Alert("drive-failed-1") }, masterEnabled: false, enabled: AllOn());
Check("the master switch off silences everything", masterOff.Count == 0, masterOff.Count);

var onlyDrives = AlertNotifier.DefaultEnabled();
onlyDrives["drives"] = true;
var m2 = new AlertNotifier();
m2.Evaluate(Array.Empty<(string, string, string)>(), true, onlyDrives);
var filtered = m2.Evaluate(
    new[] { Alert("drive-failed-1"), Alert("capacity-critical"), Alert("battery-failed") },
    masterEnabled: true, enabled: onlyDrives);
Check("only the enabled category notifies", filtered.Count == 1, filtered.Count);
Check("-- and it is the enabled one",
      filtered.Count == 1 && filtered[0].Category == "drives", filtered.FirstOrDefault());

var m3 = new AlertNotifier();
m3.Evaluate(Array.Empty<(string, string, string)>(), true, AlertNotifier.DefaultEnabled());
var allOff = m3.Evaluate(new[] { Alert("drive-failed-1"), Alert("unreachable") },
                         true, AlertNotifier.DefaultEnabled());
Check("defaults (all off) notify about nothing", allOff.Count == 0, allOff.Count);

// ---------------------------------------------------------------------------
Console.WriteLine("\n== every alert key the agent raises maps to a category ==");
// Taken from monitor.py's _evaluate. A key that maps nowhere is silent, which
// is the safe direction to be wrong -- but these are the ones we know about
// and they should all land somewhere.
var known = new (string Key, string Expected)[]
{
    ("drive-failed-3", "drives"),
    ("drive-warning-2", "drives"),
    ("drive-errors-1", "drives"),
    ("temp-critical-4", "temperature"),
    ("temp-warning-4", "temperature"),
    ("chassis-temp-critical", "temperature"),
    ("chassis-temp-warning", "temperature"),
    ("capacity-critical", "capacity"),
    ("capacity-warning", "capacity"),
    ("battery-failed", "battery"),
    ("battery-warning", "battery"),
    ("unreachable", "unreachable"),
    ("double-degraded", "pack"),
    ("relayout", "pack"),
    ("pack-status-dnas_status", "pack"),
};
foreach (var (key, expected) in known)
    Check($"{key} -> {expected}", AlertNotifier.CategoryFor(key) == expected, AlertNotifier.CategoryFor(key));

Console.WriteLine("\n== an unknown key is silent, not a surprise ==");
// If a future alert type is added to the agent and nobody updates the list,
// the failure mode must be a missing notification rather than an unexpected
// one -- given what this app has already been told off for.
Check("an unrecognised key has no category", AlertNotifier.CategoryFor("brand-new-thing") == "");
Check("an empty key has no category", AlertNotifier.CategoryFor("") == "");
var u = new AlertNotifier();
u.Evaluate(Array.Empty<(string, string, string)>(), true, AllOn());
var unknown = u.Evaluate(new[] { Alert("brand-new-thing") }, true, AllOn());
Check("-- and it notifies about nothing even with everything switched on",
      unknown.Count == 0, unknown.Count);

// A "-cleared" key is the agent's RESOLUTION notice, carrying text like
// "Resolved: Drive in bay 3 has failed". Matched on prefix alone it would land
// in "drives" and pop up an alarm about a drive you have just replaced. It
// never reaches here today (monitor.py doesn't list cleared alerts as active),
// but that must stay impossible by rule rather than by luck.
Check("a resolution notice maps to no category at all",
      AlertNotifier.CategoryFor("drive-failed-3-cleared") == "",
      AlertNotifier.CategoryFor("drive-failed-3-cleared"));
var c = new AlertNotifier();
c.Evaluate(Array.Empty<(string, string, string)>(), true, AllOn());
var cleared = c.Evaluate(new[] { Alert("drive-failed-3-cleared", "info", "Resolved: ...") },
                         true, AllOn());
Check("-- so a resolved problem can never raise an alarm", cleared.Count == 0, cleared.Count);

// ---------------------------------------------------------------------------
Console.WriteLine("\n== switching Drobos starts over ==");
// The alerts of the device you just left say nothing about the one you just
// opened. Carrying them across would either suppress a real notification or
// invent one.
var s = new AlertNotifier();
s.Evaluate(new[] { Alert("drive-failed-3") }, true, AllOn());          // seeded, silent
s.Reset();
var afterReset = s.Evaluate(new[] { Alert("drive-failed-3") }, true, AllOn());
Check("the first pass after a reset is silent again", afterReset.Count == 0, afterReset.Count);
var thenNew = s.Evaluate(new[] { Alert("drive-failed-3"), Alert("unreachable") }, true, AllOn());
Check("-- and normal behaviour resumes after that", thenNew.Count == 1, thenNew.Count);

// ---------------------------------------------------------------------------
Console.WriteLine("\n== empty and malformed input ==");
var e = new AlertNotifier();
e.Evaluate(Array.Empty<(string, string, string)>(), true, AllOn());
var none = e.Evaluate(Array.Empty<(string, string, string)>(), true, AllOn());
Check("no alerts means no notices", none.Count == 0, none.Count);
var blank = e.Evaluate(new[] { Alert("") }, true, AllOn());
Check("a blank key is ignored", blank.Count == 0, blank.Count);
var missingCat = e.Evaluate(new[] { Alert("drive-failed-9") }, true,
                            new Dictionary<string, bool>());  // category absent entirely
Check("a category missing from settings counts as off", missingCat.Count == 0, missingCat.Count);

Console.WriteLine();
Console.WriteLine(fails.Count == 0 ? "ALL PASS" : $"{fails.Count} FAILURES: {string.Join(", ", fails)}");
return fails.Count == 0 ? 0 : 1;
