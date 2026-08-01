#!/usr/bin/env python3
"""Adversarial interaction tests: click him at the worst possible moment.

    python3 test_interactions.py

The other two suites test the pieces. This one drives the whole daemon the way a
person does - click, drag, right-click, hover, at every point in the state
machine - and looks for the one failure that matters for a thing that sits on
your desktop all day: **he gets stuck.**

Every scenario ends with the same two assertions, because they are what a stuck
creature fails:

    drivable   a session event can still move him
    sleepy     leave him alone long enough and he still naps

That pair is the whole point. The real bug this suite exists for was a mode
called `cue` that nothing drove: he kept the idle frames, so he *looked* fine,
but he never blinked, never patrolled and never slept again - and two clicks in
a row was enough to get there. Nothing that checks "is the animation right"
would have caught it. Checking "can he still be driven" does.

No display is needed for `Creature`; the `Deck` sections skip themselves if
there is no X.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).parent
TMP = tempfile.mkdtemp(prefix="deckguy-inter-")
os.environ["DECK_GUY_HOME"] = TMP
os.environ.setdefault("GDK_BACKEND", "x11")

sys.path.insert(0, str(HERE))

FAILED = []
PASSED = 0


def ok(label, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(f"{label} {detail}".rstrip())


def check(label, got, want):
    ok(label, got == want, f"\n     got  {got!r}\n     want {want!r}")


try:
    import gi

    gi.require_version("Gtk", "3.0")
    import guy
    from guy import Creature, Deck, Sprites
    HAVE_GTK = True
except Exception as exc:                                     # pragma: no cover
    print(f"cannot import the daemon ({exc}) - nothing to test")
    sys.exit(1)


# --------------------------------------------------------------------- helpers
class Ev:
    """A GDK button/motion event, as much of one as the handlers touch."""

    def __init__(self, button=1, x_root=0.0):
        self.button = button
        self.x_root = x_root


# Modes the tick loop actually drives. `cue` and `wake` are legal *transiently*
# - they are one-shots that end - so a scenario is only allowed to leave him
# there if the animation still has frames to play.
SETTLED = {"idle", "patrol", "working", "jump", "sleep", "error"}


def deck(demo=True):
    d = Deck(guy.argparse.Namespace(demo=demo, scale=3, height=400, idle_secs=300,
                                    no_shape=True, no_log=True))
    d.hide()
    # Park him mid-screen. His real home comes from pos.json and is usually hard
    # against a fence, where a drag is indistinguishable from a clamp.
    d.creature.x = d.creature.home_x = (d.creature.min_x + d.creature.max_x) / 2
    return d


def tick(d, now, frames=1, hover=False):
    """Drive the real per-frame path: creature, alerts, readout, draw."""
    for _ in range(frames):
        now += 1 / 30
        d.creature.update(1 / 30, now)
        d.on_alerts(now)
        d._hover = hover
        d.update_readout(now)
        d.on_draw(None, CR)
    return now


def click(d, x=None):
    """Press and release without moving - the gesture that means 'wave'."""
    x = d.creature.x if x is None else x
    d.on_press(None, Ev(1, x))
    d._dragged = False
    d.on_release(None, Ev(1, x))


def drag(d, dx, steps=4):
    start = d.creature.x
    d.on_press(None, Ev(1, start))
    for i in range(1, steps + 1):
        d.on_motion(None, Ev(1, start + dx * i / steps))
    d.on_release(None, Ev(1, start + dx))


def right_click(d):
    """Open the menu. `popup_at_pointer` is stubbed so nothing appears."""
    d.on_press(None, Ev(3, d.creature.x))


def settles(d, now, label):
    """The two assertions every scenario ends with."""
    now = tick(d, now, 400)                # ~13s, longer than any one-shot
    ok(f"{label}: settles in a mode the tick loop drives",
       d.creature.mode in SETTLED, d.creature.mode)
    # Drivable: a session event still moves him.
    d.creature.set_mode("working")
    ok(f"{label}: still drivable", d.creature.mode == "working", d.creature.mode)
    # Sleepy: the nap timer still reaches him. This is the invariant that a
    # stuck mode fails while looking completely normal on screen.
    d.creature.set_mode("idle")
    d.creature.last_activity = now - 99999
    now = tick(d, now, 60)
    ok(f"{label}: can still fall asleep", d.creature.mode == "sleep",
       d.creature.mode)
    d.creature.last_activity = now
    d.creature.set_mode("idle")
    return now


import cairo  # noqa: E402

CR = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1920, 400))

# The menu must never actually pop up during a test run.
from gi.repository import Gtk  # noqa: E402

MENU_LABELS = []
Gtk.Menu.popup_at_pointer = lambda self, ev: MENU_LABELS.__setitem__(
    slice(None), [(c.get_label() or "-") for c in self.get_children()])


# ============================================ 1. click him in every single mode
# The state machine has eight modes and a click is legal in all of them. Each of
# these was a chance to park an undrivable mode.
print("1. clicking him in every mode")
STARTS = [
    ("idle", lambda c: c.set_mode("idle")),
    ("patrol", lambda c: c.start_patrol(time.time(), 5)),
    ("working", lambda c: c.set_mode("working")),
    ("working+costume", lambda c: (c.set_mode("working"), c.set_costume("build"))),
    ("jump", lambda c: c.set_mode("jump", duration=45)),
    ("sleep", lambda c: c.set_mode("sleep")),
    ("error", lambda c: c.set_mode("error")),
    ("mid-wave", lambda c: (c.set_mode("idle"), c.cue("wave"))),
    ("mid-wake", lambda c: (c.set_mode("sleep"), c.set_mode("idle"))),
]
for name, setup in STARTS:
    d = deck()
    now = time.time()
    setup(d.creature)
    now = tick(d, now, 2)
    click(d)
    now = settles(d, now, f"click during {name}")

# Clicking twice, three times, ten times - the original stuck-mode bug needed
# exactly two, so the count matters.
for n in (2, 3, 10):
    d = deck()
    now = time.time()
    d.creature.set_mode("idle")
    for _ in range(n):
        click(d)
        now = tick(d, now, 2)          # inside the previous wave every time
    now = settles(d, now, f"{n} clicks in a row")

# A click landing on the exact frame a one-shot finishes.
for frames in range(1, 14):
    d = deck()
    now = time.time()
    d.creature.set_mode("idle")
    d.creature.cue("wave")
    now = tick(d, now, frames)
    click(d)
    now = settles(d, now, f"click {frames} frames into the wave")


# ============================================== 2. drag, and drag while busy
print("2. dragging")
for name, setup in STARTS:
    d = deck()
    now = time.time()
    setup(d.creature)
    now = tick(d, now, 2)
    before = d.creature.home_x
    drag(d, -220)
    now = tick(d, now, 2)
    ok(f"drag during {name}: he moved", abs(d.creature.home_x - before) > 100,
       f"{before} -> {d.creature.home_x}")
    ok(f"drag during {name}: no wave", d.creature.anim != "wave" or name == "mid-wave")
    now = settles(d, now, f"drag during {name}")

# A drag that goes nowhere is a click, and a click that wobbles is still a click.
d = deck()
now = time.time()
d.creature.set_mode("idle")
x = d.creature.x
d.on_press(None, Ev(1, x))
d.on_motion(None, Ev(1, x + 2))          # under the 3px threshold
d.on_release(None, Ev(1, x + 2))
ok("a 2px wobble is still a click", d.creature.anim == "wave", d.creature.anim)
now = settles(d, now, "wobble click")

# Dragged hard off both edges.
for target in (-5000, 5000):
    d = deck()
    now = time.time()
    drag(d, target)
    inside = d.creature.min_x <= d.creature.x <= d.creature.max_x
    ok(f"drag to {target} stays fenced", inside,
       f"{d.creature.x} not in {d.creature.min_x}..{d.creature.max_x}")
    now = settles(d, now, f"drag to {target}")


# ================================== 3. clicking while a permission prompt waits
print("3. clicking while a permission prompt is waiting")
ALERT = ("permission_prompt", "Claude needs your permission to use Bash")

d = deck()
now = time.time()
d.alerts.local(*ALERT, now)
now = tick(d, now, 2)
ok("prompt waiting: badge is up", d.alerts.current() is not None)
ok("prompt waiting: he waved", d.creature.anim == "wave", d.creature.anim)
click(d)
ok("clicking acknowledges it", d.alerts.current() is None)
now = tick(d, now, 60)
ok("...and it stays acknowledged", d.alerts.current() is None)
now = settles(d, now, "click during a permission prompt")

# Clicking at each frame of the wave the alert itself triggered - the window in
# which `error` or the previous mode is parked behind a one-shot.
for frames in (0, 1, 3, 6, 10, 13, 20):
    d = deck()
    now = time.time()
    d.creature.set_mode("working")
    d.creature.set_costume("build")
    d.alerts.local(*ALERT, now)
    now = tick(d, now, frames)
    click(d)
    now = tick(d, now, 5)
    ok(f"ack {frames} frames into the alert wave: badge gone",
       d.alerts.current() is None)
    now = settles(d, now, f"ack {frames} frames in")

# Same, for the failure path, which also changes posture.
for frames in (0, 1, 5, 12, 30):
    d = deck()
    now = time.time()
    d.creature.set_mode("error")
    d.alerts.local("session_failed", "model overloaded", now)
    now = tick(d, now, frames)
    click(d)
    now = tick(d, now, 90)
    ok(f"ack {frames} frames into the slump: badge gone", d.alerts.current() is None)
    ok(f"ack {frames} frames into the slump: not still slumped",
       d.creature.mode != "error", d.creature.mode)
    now = settles(d, now, f"ack slump at {frames}")

# Acknowledge, then it clears on its own, then a *new* prompt arrives.
d = deck()
now = time.time()
d.alerts.local(*ALERT, now)
now = tick(d, now, 2)
click(d)
ok("acked", d.alerts.current() is None)
d.alerts.clear_local()
now = tick(d, now, 2)
d.alerts.local(*ALERT, now)
now = tick(d, now, 2)
ok("a new prompt after an ack is not pre-acknowledged", d.alerts.current() is not None)
now = settles(d, now, "ack then re-raise")

# Dragging him while blocked must not acknowledge - you moved him, you did not
# say you had seen it.
d = deck()
now = time.time()
d.alerts.local(*ALERT, now)
now = tick(d, now, 2)
drag(d, 150)
ok("dragging does not acknowledge", d.alerts.current() is not None)
now = settles(d, now, "drag while blocked")

# Right-click while blocked: the menu opens, the alert survives.
d = deck()
now = time.time()
d.alerts.local(*ALERT, now)
now = tick(d, now, 2)
right_click(d)
ok("right-click does not acknowledge", d.alerts.current() is not None)
ok("...and the menu still builds", "Speech bubble" in MENU_LABELS, MENU_LABELS)
ok("...with no mute item", not any("ute" in x for x in MENU_LABELS), MENU_LABELS)
now = settles(d, now, "right-click while blocked")


# ==================================== 4. an alert arriving at the worst moment
print("4. alerts arriving mid-everything")
for name, setup in STARTS:
    d = deck()
    now = time.time()
    setup(d.creature)
    now = tick(d, now, 2)
    d.alerts.local(*ALERT, now)
    now = tick(d, now, 4)
    ok(f"alert during {name}: badge is up", d.alerts.current() is not None)
    now = settles(d, now, f"alert during {name}")

# Arriving while he is being dragged - the pointer is down the whole time.
d = deck()
now = time.time()
x = d.creature.x
d.on_press(None, Ev(1, x))
d.on_motion(None, Ev(1, x - 60))
d.alerts.local(*ALERT, now)
now = tick(d, now, 4)
d.on_motion(None, Ev(1, x - 120))
d.on_release(None, Ev(1, x - 120))
ok("alert mid-drag: badge is up", d.alerts.current() is not None)
ok("alert mid-drag: the drag still finished", abs(d.creature.x - (x - 120)) < 2,
   f"{x} -> {d.creature.x}")
now = settles(d, now, "alert mid-drag")

# Arriving while asleep should wake him rather than be missed.
d = deck()
now = time.time()
d.creature.set_mode("sleep")
now = tick(d, now, 4)
d.alerts.local(*ALERT, now)
now = tick(d, now, 30)
ok("an alert wakes him", d.creature.mode != "sleep", d.creature.mode)
now = settles(d, now, "alert while asleep")


# ================================================= 5. hovering, in and out
print("5. hovering")
d = deck()
now = time.time()
d.alerts.local(*ALERT, now)
now = tick(d, now, 4, hover=True)
labels = [r[0] for r in d.panel.rows if not hasattr(r, "fill")]
ok("the panel names the wait", "waiting" in labels, labels)
now = tick(d, now, 4, hover=False)
check("leaving the hover empties the panel", d.panel.rows, [])
now = tick(d, now, 4, hover=True)
ok("hovering again refills it", len(d.panel.rows) > 0)
# Flap the hover every single frame.
for i in range(80):
    now = tick(d, now, 1, hover=bool(i % 2))
ok("flapping the hover 80 times does not raise", True)
now = settles(d, now, "hover flapping")

# The readout rectangle has to exist whenever something is drawn, or you get a
# panel you cannot hover and a bubble that leaves smears.
d = deck()
now = time.time()
d.alerts.local(*ALERT, now)
now = tick(d, now, 4, hover=True)
rect = d.readout_rect(now)
ok("a visible panel has a hit rectangle", rect is not None)
if rect:
    ok("...that is on screen", 0 <= rect[0] and rect[0] + rect[2] <= d.win_w + 2, rect)
now = settles(d, now, "readout rect")


# ================================== 6. the menu, toggled at the worst moment
print("6. menu toggles")


class Item:
    def __init__(self, v):
        self.v = v

    def get_active(self):
        return self.v


saved = (HERE / "prefs.json").read_text() if (HERE / "prefs.json").exists() else None
try:
    d = deck()
    now = time.time()
    d.alerts.local(*ALERT, now)
    now = tick(d, now, 2)
    for toast in (False, True):
        for sound in (False, True):
            d.on_toggle_toast(Item(toast))
            d.on_toggle_sound(Item(sound))
            check(f"toast={toast} survives the round trip", d.alerts.toast, toast)
            check(f"sound={sound} survives the round trip", d.alerts.sound, sound)
            now = tick(d, now, 3)
    ok("toggling both flags mid-alert keeps the badge", d.alerts.current() is not None)
    d.on_toggle_bubble(Item(False))
    now = tick(d, now, 3)
    check("bubble off means no bubble even during an alert", d.bubble.text, "")
    ok("...but the badge is untouched", d.alerts.current() is not None)
    d.on_toggle_bubble(Item(True))
    now = tick(d, now, 3)
    ok("bubble back on says what is waiting", "permission" in d.bubble.text)
    now = settles(d, now, "menu toggles mid-alert")
finally:
    if saved is not None:
        (HERE / "prefs.json").write_text(saved)


# ========================================= 7. the hook, in hostile sequences
print("7. hook event sequences")
HOOK = str(HERE / "notify.sh")
ENV = dict(os.environ, DECK_GUY_HOME=TMP + "/hook")
SID = "seq"
FILE = Path(TMP) / "hook" / "sessions" / f"{SID}.json"
BASE = {"session_id": SID, "cwd": "/home/jeet/x", "transcript_path": "/t/x.jsonl"}


def fire(**payload):
    p = dict(BASE, **payload)
    mode = {"Notification": "alert", "PostToolUseFailure": "alert",
            "StopFailure": "error", "Stop": "jump",
            "SessionEnd": "idle"}.get(p.get("hook_event_name"), "working")
    subprocess.run([HOOK, mode], input=json.dumps(p), text=True, env=ENV, check=False)
    return json.loads(FILE.read_text()) if FILE.exists() else {}


NOTE = dict(hook_event_name="Notification", message="needs permission",
            notification_type="permission_prompt")
WORK = dict(hook_event_name="PreToolUse", tool_name="Bash",
            tool_input={"command": "pytest -q"})

# Every event type, immediately after a Notification. Each must clear it.
CLEARERS = [
    ("PreToolUse", WORK),
    ("PostToolUse", dict(hook_event_name="PostToolUse", tool_name="Bash",
                         tool_input={"command": "ls"}, tool_response={"ok": 1})),
    ("PermissionDenied", dict(hook_event_name="PermissionDenied", tool_name="Bash",
                              tool_input={"command": "ls"}, reason="user")),
    ("Stop", dict(hook_event_name="Stop")),
    ("UserPromptSubmit", dict(hook_event_name="UserPromptSubmit")),
]
for name, ev in CLEARERS:
    fire(**WORK)
    raised = fire(**NOTE)
    check(f"Notification then {name}: raised", raised.get("alert"), "permission_prompt")
    cleared = fire(**ev)
    check(f"Notification then {name}: cleared", cleared.get("alert"), "")

# Ten notifications back to back, then one clear.
fire(**WORK)
for _ in range(10):
    out = fire(**NOTE)
check("ten notifications still one alert field", out.get("alert"), "permission_prompt")
check("...and one clear finishes it", fire(**WORK).get("alert"), "")

# Two different kinds in a row: the newer one wins the field.
fire(**WORK)
fire(**NOTE)
second = fire(hook_event_name="Notification", message="Claude is waiting for your input",
              notification_type="idle_prompt")
check("a different kind replaces the first", second.get("alert"), "idle_prompt")

# A Notification as the very first thing a session ever says.
solo_env = dict(os.environ, DECK_GUY_HOME=TMP + "/solo")
Path(TMP, "solo", "sessions").mkdir(parents=True, exist_ok=True)
subprocess.run([HOOK, "alert"], text=True, env=solo_env,
               input=json.dumps(dict(BASE, session_id="solo", **NOTE)), check=False)
solo = json.loads((Path(TMP) / "solo" / "sessions" / "solo.json").read_text())
check("a first-ever Notification still writes the alert", solo.get("alert"),
      "permission_prompt")

# Hostile message content must not break the JSON the daemon has to parse.
for bad in ['a "quoted" thing', "back\\slash", "trailing\\", "nul\x01ctl",
            "x" * 900, "emoji 🔥 and 中文", "}{\"alert\":\"pwned\""]:
    out = fire(hook_event_name="Notification", message=bad,
               notification_type="permission_prompt")
    ok(f"payload survives {bad[:18]!r}", out.get("alert") == "permission_prompt",
       out.get("alert"))
    ok(f"...and does not smuggle a field {bad[:18]!r}",
       set(out) >= {"session_id", "state", "alert", "alert_msg"})

# `--ensure` runs on every prompt now, so a daemon that died at 11am is back by
# your next prompt. Which makes "Quit" mean nothing unless it leaves a marker.
OFFROOT = Path(TMP) / "hook"
subprocess.run([HOOK, "--ensure", "prompt"], text=True, env=ENV, check=False,
               input=json.dumps(dict(BASE, hook_event_name="UserPromptSubmit")))
ok("a hook-driven ensure writes no marker", not (OFFROOT / "off").exists())
(OFFROOT / "off").touch()
subprocess.run([HOOK, "--ensure", "prompt"], text=True, env=ENV, check=False,
               input=json.dumps(dict(BASE, hook_event_name="UserPromptSubmit")))
ok("a hook respects the quit marker", (OFFROOT / "off").exists())
subprocess.run([HOOK, "--ensure", "idle"], text=True, env=ENV, check=False, input="")
ok("running it by hand clears the marker", not (OFFROOT / "off").exists())

# A session that ends while blocked.
fire(**WORK)
fire(**NOTE)
ended = fire(hook_event_name="SessionEnd")
check("ending while blocked marks it ended", ended.get("state"), "ended")
check("...and drops the alert", ended.get("alert"), "")


# ====================== 8. the daemon reading those files off disk, for real
print("8. the daemon against real session files")
import sessions as S  # noqa: E402

S.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def write(sid, **kw):
    now = time.time()
    data = dict(session_id=sid, state="working", tool="Bash",
                input={"command": "pytest -q"}, cwd="/home/jeet/x",
                ts=now, prompt_ts=now, session_started=now, heartbeat=now,
                alert="", alert_msg="")
    data.update(kw)
    (S.SESSIONS_DIR / f"{sid}.json").write_text(json.dumps(data))


for f in S.SESSIONS_DIR.glob("*.json"):
    f.unlink()
d = deck(demo=False)
now = time.time()

write("a")
d.store.reload(True)
now = tick(d, now, 3)
ok("a working session drives him", d.creature.mode == "working", d.creature.mode)

write("a", state="alert", alert="permission_prompt", alert_msg="needs permission")
d.store.reload(True)
now = tick(d, now, 3)
ok("the alert lands", d.alerts.current() is not None)
s = d.store.newest()
check("the step underneath it survived", s.label, "Bash pytest -q")
click(d)
ok("clicking clears it", d.alerts.current() is None)

write("a")                                       # you answered in the terminal
d.store.reload(True)
now = tick(d, now, 3)
ok("answering leaves nothing behind", d.alerts.current() is None)

# Two blocked sessions, acknowledge once.
write("a", state="alert", alert="permission_prompt", alert_msg="one")
write("b", state="alert", alert="permission_prompt", alert_msg="two")
d.store.reload(True)
now = tick(d, now, 3)
check("two blocked sessions, two alerts", len(d.alerts.live), 2)
ok("...but only one badge", d.alerts.current() is not None)
click(d)
ok("one click clears both", d.alerts.current() is None)

# A failure outranks a wait for the one badge he has.
for f in S.SESSIONS_DIR.glob("*.json"):
    f.unlink()
d.store.reload(True)
now = tick(d, now, 3)
write("a", state="alert", alert="permission_prompt", alert_msg="waiting")
write("b", state="alert", alert="session_failed", alert_msg="broken")
d.store.reload(True)
now = tick(d, now, 3)
cur = d.alerts.current()
check("the failure gets the badge", cur.kind if cur else None, "session_failed")
check("...and it is the red frame", cur.frame if cur else None, 1)

# Half-written and hostile files must not stop him.
(S.SESSIONS_DIR / "torn.json").write_text('{"session_id": "torn", "alert": "per')
(S.SESSIONS_DIR / "weird.json").write_text(json.dumps(
    {"session_id": "weird", "state": "alert", "alert": ["not", "a", "string"],
     "alert_msg": {"nested": True}, "ts": "soon", "heartbeat": time.time()}))
d.store.reload(True)
now = tick(d, now, 5)
ok("a torn file and a hostile file do not stop him", True)
now = settles(d, now, "hostile session files")

# The session dies while he is slumped about it: the slump must survive being
# reaped, or the only evidence disappears with the thing that caused it.
for f in S.SESSIONS_DIR.glob("*.json"):
    f.unlink()
d.store.reload(True)
d.creature.set_mode("error")
d.alerts.local("session_died", "the session stopped answering", now)
now = tick(d, now, 5)
# The reap almost always lands *during* the wave a failure triggers, which is
# exactly when `mode` says "cue" and the slump is parked behind it.
ok("mid-wave, he still counts as slumped", d.creature.slumped, d.creature.mode)
d.on_sessions(d.store)
now = tick(d, now, 60)
ok("a slump outlives the session that caused it",
   d.creature.mode == "error", d.creature.mode)
click(d)
now = tick(d, now, 60)
ok("...and a click ends it", d.creature.mode != "error", d.creature.mode)
now = settles(d, now, "slump after the session is gone")


# ============================== 9. a full session, start to finish, with clicks
print("9. a whole session with a person interfering")
for f in S.SESSIONS_DIR.glob("*.json"):
    f.unlink()
d = deck(demo=False)
now = time.time()
script = [
    ("idle", {}), ("working", {"tool": "Read"}), ("working", {"tool": "Edit"}),
    ("alert", {"alert": "permission_prompt", "alert_msg": "needs permission"}),
    ("working", {"tool": "Bash"}),
    ("alert", {"alert": "tool_failed", "alert_msg": "Bash failed: exit 2"}),
    ("working", {"tool": "Bash"}), ("jump", {}), ("idle", {}),
]
for i, (state, extra) in enumerate(script):
    write("live", state=state, **extra)
    d.store.reload(True)
    now = tick(d, now, 6, hover=bool(i % 3 == 0))
    if i % 2:
        click(d)                       # a person poking him throughout
        now = tick(d, now, 4)
    ok(f"step {i} ({state}) left him drivable",
       d.creature.mode in SETTLED | {"cue", "wake"}, d.creature.mode)
now = settles(d, now, "a whole session with clicking")

for f in S.SESSIONS_DIR.glob("*.json"):
    f.unlink()


# ---------------------------------------------------------------------- report
print(f"\n{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print("  FAIL " + f)
sys.exit(1 if FAILED else 0)
