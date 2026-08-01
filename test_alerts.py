#!/usr/bin/env python3
"""Tests for the attention phase. No display, no network, no sound.

    python3 test_alerts.py

Organised around the acceptance criteria in phases/phase-3-attention.md, because
every one of them is a behaviour someone will notice being wrong: an alarm that
does not fire, an alarm that will not stop, or a beep on a machine where sound
was never turned on.

The one that matters most is "answering the prompt clears it". A HUD that keeps
shouting after you have dealt with it does not get ignored once, it gets
uninstalled.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).parent
TMP = tempfile.mkdtemp(prefix="deckguy-alerts-")
os.environ["DECK_GUY_HOME"] = TMP

sys.path.insert(0, str(HERE))
import alerts as A  # noqa: E402
from alerts import Alerts  # noqa: E402
from sessions import Session  # noqa: E402

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


class FakeNotifier:
    """Records what would have left the machine. Nothing does."""

    def __init__(self):
        self.toasts = []
        self.beeps = 0

    def toast(self, title, body, urgent=False):
        self.toasts.append((title, body, urgent))
        return True

    def beep(self):
        self.beeps += 1
        return True


class Fake:
    """The two fields `Alerts` actually reads off a session."""

    def __init__(self, sid="s1", alert="", msg=""):
        self.id = sid
        self.alert = alert
        self.alert_label = msg


def store(**kw):
    return Alerts(notifier=FakeNotifier(), **kw)


# ============================================================ 1. the hook layer
# Everything below the daemon depends on notify.sh putting the right two fields
# in the file, so it is checked by actually running it.
HOOK = str(HERE / "notify.sh")
ENV = dict(os.environ, DECK_GUY_HOME=TMP + "/hook")
SID = "hooktest"
FILE = Path(TMP) / "hook" / "sessions" / f"{SID}.json"


def fire(payload, mode="working"):
    subprocess.run([HOOK, mode], input=json.dumps(payload), text=True, env=ENV,
                   check=False)
    return json.loads(FILE.read_text()) if FILE.exists() else {}


CWD = "/home/jeet/projects/thing"
BASE = {"session_id": SID, "cwd": CWD, "transcript_path": "/t/x.jsonl"}

# Shaped exactly like the real thing: Claude Code 2.1.220 builds a Notification
# payload as {...base, hook_event_name, message, title, notification_type}, with
# no tool_name and no permission_mode.
note = fire(dict(BASE, hook_event_name="Notification",
                 message="Claude needs your permission to use Bash",
                 title="Claude needs your permission",
                 notification_type="permission_prompt"), "alert")
check("Notification sets the alert kind", note.get("alert"), "permission_prompt")
check("Notification keeps the message",
      note.get("alert_msg"), "Claude needs your permission to use Bash")
check("Notification writes the alert state", note.get("state"), "alert")

# THE one. Approving in the terminal runs the tool, which fires PostToolUse.
after = fire(dict(BASE, hook_event_name="PostToolUse", tool_name="Bash",
                  tool_input={"command": "ls"}, tool_response={"stdout": "a"}))
check("the next event clears the alert", after.get("alert"), "")
check("...and says what it is doing again", after.get("state"), "working")

# Denying is you answering too, and has to clear just as fast.
fire(dict(BASE, hook_event_name="Notification", message="needs permission",
          notification_type="permission_prompt"), "alert")
denied = fire(dict(BASE, hook_event_name="PermissionDenied", tool_name="Bash",
                   tool_input={"command": "rm -rf /"}, reason="user"))
check("denying clears the alert", denied.get("alert"), "")

fail = fire(dict(BASE, hook_event_name="PostToolUseFailure", tool_name="Bash",
                 tool_input={"command": "pytest -q"},
                 error="exit code 2", is_interrupt=False), "alert")
check("a failed tool raises an alert", fail.get("alert"), "tool_failed")
ok("the failure says which tool and why",
   fail.get("alert_msg") == "Bash failed: exit code 2", fail.get("alert_msg"))

esc = fire(dict(BASE, hook_event_name="PostToolUseFailure", tool_name="Bash",
                tool_input={"command": "sleep 99"},
                error="aborted", is_interrupt=True), "alert")
check("pressing Esc is not a failure", esc.get("alert"), "")
check("...and reads as ordinary work", esc.get("state"), "working")

crash = fire(dict(BASE, hook_event_name="StopFailure",
                  error="model overloaded", error_details={}), "error")
check("a failed session gets the error posture", crash.get("state"), "error")
check("...with a kind of its own", crash.get("alert"), "session_failed")
check("...and the reason", crash.get("alert_msg"), "model overloaded")

# `message` is a common word. A tool_response carrying one must not turn into a
# warning on his shoulder about nothing.
leak = fire(dict(BASE, hook_event_name="PostToolUse", tool_name="Bash",
                 tool_input={"command": "ls"},
                 tool_response={"message": "all good"}))
check("a stray message field does not become an alert", leak.get("alert"), "")
check("...and neither does its text", leak.get("alert_msg"), "")

# A Write that failed carries the whole new file ahead of `error`, so the reason
# only exists past the first kilobyte the hook reads.
buried = fire(dict(BASE, hook_event_name="PostToolUseFailure", tool_name="Write",
                   tool_input={"file_path": "/tmp/x", "content": "x" * 4000},
                   error="permission denied", is_interrupt=False), "alert")
ok("an error hidden behind a big tool_input is still found",
   "permission denied" in buried.get("alert_msg", ""), buried.get("alert_msg"))

# ...and so is the flag right behind it. Missing this one is not a missing
# field, it is a wrong one: Esc during a big Write reads as the Write crashing.
esc_big = fire(dict(BASE, hook_event_name="PostToolUseFailure", tool_name="Write",
                    tool_input={"file_path": "/tmp/x", "content": "x" * 4000},
                    error="aborted by user", is_interrupt=True), "alert")
check("Esc behind a big tool_input is still not a failure", esc_big.get("alert"), "")
check("...and still reads as ordinary work", esc_big.get("state"), "working")

# The hook runs on every tool call, so it is allowed to be dumb but not slow.
t0 = time.time()
for _ in range(20):
    fire(dict(BASE, hook_event_name="PreToolUse", tool_name="Read",
              tool_input={"file_path": f"{CWD}/a.py"}))
per_call = (time.time() - t0) / 20 * 1000
print(f"hook cost      {per_call:.1f}ms per tool call (budget 25)")
ok(f"the hook is still fast ({per_call:.1f}ms per call)", per_call < 25)


# ====================================================== 2. the session model
prev = Session({"session_id": "s", "state": "working", "tool": "Bash",
                "input": {"command": "pytest -q"}, "ts": 1000, "prompt_ts": 900,
                "cwd": CWD, "transcript_path": "/t/x.jsonl", "heartbeat": 1000})
raised = Session({"session_id": "s", "state": "alert", "alert": "permission_prompt",
                  "alert_msg": "Claude needs your permission to use Bash",
                  "ts": 1005, "heartbeat": 1005}).inherit(prev)
check("an alert does not blank the step it is blocking", raised.label,
      "Bash pytest -q")
check("...nor restart that step's clock", raised.ts, 1000)
check("...and the alert itself survives", raised.alert, "permission_prompt")
ok("the heartbeat still advances, so the session is not reaped",
   raised.heartbeat == 1005, raised.heartbeat)

# Claude Code's permission message is the same generic sentence every time and
# names nothing. The step it is blocking is the useful half, and we still have
# it because an alert is not a posture.
def labelled(**kw):
    return Session(dict({"session_id": "s", "state": "working", "tool": "Bash",
                         "input": {"command": "pytest -q"}}, **kw)).alert_label


check("the blocked step is spliced into the generic message",
      labelled(alert="permission_prompt", alert_msg="Claude needs your permission"),
      "Bash pytest -q - needs your permission")
check("a message that already names the tool is left alone",
      labelled(alert="permission_prompt",
               alert_msg="Claude needs your permission to use Bash"),
      "Claude needs your permission to use Bash")
check("no step known, no splice",
      Session({"session_id": "s", "alert": "permission_prompt",
               "alert_msg": "Claude needs your permission"}).alert_label,
      "Claude needs your permission")
check("a failure message already says what failed",
      labelled(alert="tool_failed", alert_msg="Bash failed: exit code 2"),
      "Bash failed: exit code 2")
ok("the splice still fits above his head",
   len(labelled(alert="permission_prompt", alert_msg="Claude needs your permission",
                tool="Edit", input={"file_path": "/a/very/long/nested/module.py"}))
   <= 46)

bare = Session({"session_id": "s", "alert": "some_new_thing"})
check("an unknown kind still reads as words", bare.alert_label, "some new thing")
check("no alert, no label", Session({"session_id": "s"}).alert_label, "")

quiet = Session({"session_id": "s", "state": "working", "tool": "Bash"})
loud = Session({"session_id": "s", "state": "working", "tool": "Bash",
                "alert": "permission_prompt", "alert_msg": "needs you"})
from sessions import SessionStore  # noqa: E402
ok("the panel repaints when an alert appears",
   SessionStore._digest({"s": quiet}) != SessionStore._digest({"s": loud}))


# ================================================ 3. dedupe and clearing
a = store(first=60)
for _ in range(10):
    a.update([Fake(alert="permission_prompt", msg="needs you")], now=1000)
check("ten notifications make one badge", len(a.live), 1)
first_seen = a.current().since
a.update([Fake(alert="permission_prompt", msg="needs you")], now=1050)
check("...whose clock does not restart", a.current().since, first_seen)
ok("...so it can still reach its own escalation", a.current().waited(1050) == 50)

a.update([Fake(alert="")], now=1051)
ok("clearing the field clears the alert", a.current() is None)
check("...and leaves nothing behind", len(a.live), 0)

a.update([Fake(alert="permission_prompt", msg="again")], now=1100)
ok("a second prompt is a new alert", a.current() is not None)
check("...with its own clock", a.current().since, 1100)

multi = store()
multi.update([Fake("s1", "permission_prompt", "one"),
              Fake("s2", "permission_prompt", "two")], now=1000)
check("two sessions, two alerts", len(multi.live), 2)
multi.update([Fake("s1", "permission_prompt", "one")], now=1001)
check("one answers, the other stays", len(multi.live), 1)


# ================================================= 4. escalation and silence
a = store(first=60, repeat=300)
a.update([Fake(alert="permission_prompt", msg="needs you")], now=0)
check("a fresh alert is a badge and nothing else", len(a.notifier.toasts), 0)
a.update([Fake(alert="permission_prompt", msg="needs you")], now=59)
check("still nothing at 59s", len(a.notifier.toasts), 0)
a.update([Fake(alert="permission_prompt", msg="needs you")], now=60)
check("one toast at the threshold", len(a.notifier.toasts), 1)
a.update([Fake(alert="permission_prompt", msg="needs you")], now=120)
check("...and not a second one right after", len(a.notifier.toasts), 1)
a.update([Fake(alert="permission_prompt", msg="needs you")], now=300)
check("one repeat at five minutes", len(a.notifier.toasts), 2)
a.update([Fake(alert="permission_prompt", msg="needs you")], now=9999)
check("and then it gives up rather than nagging forever",
      len(a.notifier.toasts), 2)
check("sound is off unless you asked for it", a.notifier.beeps, 0)

# Answered inside the threshold: the whole point of the feature is that this
# path is completely silent.
a = store(first=60)
a.update([Fake(alert="permission_prompt", msg="needs you")], now=0)
a.update([Fake(alert="")], now=30)
a.update([], now=90)
check("answering in time means no toast at all", len(a.notifier.toasts), 0)

a = store(first=60, repeat=300, sound=True)
a.update([Fake(alert="permission_prompt", msg="needs you")], now=0)
check("sound on, nothing yet at the badge stage", a.notifier.beeps, 0)
a.update([Fake(alert="permission_prompt", msg="needs you")], now=60)
check("...the sound comes with the first toast, not five minutes later",
      a.notifier.beeps, 1)
a.update([Fake(alert="permission_prompt", msg="needs you")], now=300)
check("...and again with the repeat", a.notifier.beeps, 2)

check("the shipped threshold is short enough to actually fire", A.FIRST_TOAST, 10)

# The daemon used to repeat these defaults at the call site, so editing them
# here changed the tests and the documentation and nothing else.
src = (HERE / "guy.py").read_text()
ok("the daemon does not carry its own copy of the thresholds",
   "alert_secs\")" in src and "alert_secs\", 60" not in src)
d = Alerts(notifier=FakeNotifier(), first=None, repeat=None)
check("...and an absent pref means the shipped default", d.first, A.FIRST_TOAST)
check("...for both of them", d.repeat, A.REPEAT_TOAST)

# The two menu flags are two channels, not one with a master switch. A toast
# covers your screen; a sound reaches you when the screen is covered or you are
# not at it. Wanting the second without the first is an ordinary preference, and
# gating the beep behind the toast made that combination completely silent - a
# checkbox that lies about what it does.
for want_toast in (True, False):
    for want_sound in (True, False):
        a = store(first=1, toast=want_toast, sound=want_sound)
        a.update([Fake(alert="permission_prompt", msg="needs you")], now=0)
        a.update([Fake(alert="permission_prompt", msg="needs you")], now=9)
        check(f"toast={want_toast} sound={want_sound}: toasts",
              len(a.notifier.toasts), 1 if want_toast else 0)
        check(f"toast={want_toast} sound={want_sound}: beeps",
              a.notifier.beeps, 1 if want_sound else 0)
        ok(f"toast={want_toast} sound={want_sound}: badge regardless",
           a.current() is not None)

# A failed grep is a normal part of a session and must never reach the desktop.
a = store(first=1)
a.update([Fake(alert="tool_failed", msg="Grep failed: no matches")], now=0)
a.update([Fake(alert="tool_failed", msg="Grep failed: no matches")], now=500)
check("a failed tool never toasts", len(a.notifier.toasts), 0)
ok("...but it does show a red badge", a.current().frame == 1)

a = store(first=1)
a.update([Fake(alert="session_failed", msg="model overloaded")], now=0)
a.update([Fake(alert="session_failed", msg="model overloaded")], now=5)
check("a failed session does toast", len(a.notifier.toasts), 1)
ok("...loudly", a.notifier.toasts[0][2] is True)

# Two sessions blocked at once used to make two popups fight over the same
# corner of the screen. Two popups do not tell you twice as much as one.
a = store(first=1, sound=True)
many = [Fake(f"s{i}", "permission_prompt", f"Bash job{i} - needs your permission")
        for i in range(4)]
a.update(many, now=0)
a.update(many, now=5)
check("four blocked sessions make one toast", len(a.notifier.toasts), 1)
check("...and one beep", a.notifier.beeps, 1)
ok("...whose title says how many", a.notifier.toasts[0][0] == "4 sessions need you",
   a.notifier.toasts[0][0])
ok("...and whose body names some of them and counts the rest",
   "job0" in a.notifier.toasts[0][1] and "1 more" in a.notifier.toasts[0][1],
   a.notifier.toasts[0][1])
a = store(first=1)
one = [Fake("s1", "permission_prompt", "Bash pytest -q - needs your permission")]
a.update(one, now=0)
a.update(one, now=5)
ok("one session still gets its own message, not a count",
   a.notifier.toasts[0][1] == "Bash pytest -q - needs your permission",
   a.notifier.toasts[0])

# An unfamiliar notification_type is far more likely to be a new way of saying
# "you are the blocker" than a new kind of shrug.
a = store(first=1)
a.update([Fake(alert="some_future_prompt", msg="?")], now=0)
a.update([Fake(alert="some_future_prompt", msg="?")], now=5)
check("an unknown kind still escalates", len(a.notifier.toasts), 1)
ok("...and is drawn amber, not red", a.current().frame == 0)


# =========================================================== 5. acknowledging
a = store(first=60)
a.update([Fake(alert="permission_prompt", msg="needs you")], now=0)
ok("something to acknowledge", a.ack(now=1) is True)
ok("acknowledged means no badge", a.current() is None)
a.update([Fake(alert="permission_prompt", msg="needs you")], now=999)
check("...and no toast either, ever", len(a.notifier.toasts), 0)
ok("clicking again does nothing", a.ack(now=1000) is False)
a.update([Fake(alert="")], now=1001)
a.update([Fake(alert="permission_prompt", msg="needs you")], now=1002)
ok("but the next real prompt is not pre-acknowledged", a.current() is not None)

# There is no mute. Turning both channels off is the same thing, says so on the
# label, and survives a restart - which a timed mute deliberately did not, so it
# was a third way to reach a state two switches already covered.
ok("nothing named mute survives in the module",
   not any(n.startswith("mut") for n in dir(Alerts)))


# ============================================ 6. picking one, and local alerts
a = store()
a.update([Fake("s1", "permission_prompt", "older"),
          Fake("s2", "session_failed", "broken")], now=100)
check("a failure outranks a wait", a.current().kind, "session_failed")
a = store()
a.update([Fake("s1", "permission_prompt", "older")], now=100)
a.update([Fake("s1", "permission_prompt", "older"),
          Fake("s2", "idle_prompt", "newer")], now=200)
check("otherwise the oldest wins", a.current().message, "older")

a = store(first=1)
a.local("session_died", "the session stopped answering", now=0)
a.update([], now=0)
ok("the daemon can raise an alert about silence", a.current() is not None)
a.update([], now=10)
check("...and it escalates like any other", len(a.notifier.toasts), 1)
a.clear_local()
a.update([], now=11)
ok("...and clears when the session speaks again", a.current() is None)


# ============================================== 7. a machine without notify-send
real = A.Notifier()
real.send = None
real.play = real.beep_argv = None
ok("no notify-send is not an error", real.toast("t", "b") is False)
ok("no player is not an error", real.beep() is False)
ok("...and both were still recorded", len(real.sent) == 2)
broken = A.Notifier()
broken.send = "/nonexistent/notify-send"
ok("a binary that vanished is not an error either", broken.toast("t", "b") is False)

# `aplay` reads an .oga as raw 8-bit PCM and plays the file's bytes as noise, at
# exit code 0. Handing it one is worse than staying silent, so it only ever gets
# a WAV - and a player that takes a theme id gets no path at all.
picked = A.Notifier()
if picked.beep_argv:
    argv, exe = picked.beep_argv, os.path.basename(picked.beep_argv[0])
    ok(f"the sound player is used correctly ({' '.join(argv)})",
       ("-i" in argv) if exe == "canberra-gtk-play"
       else argv[-1].endswith(".wav") if exe == "aplay"
       else argv[-1].endswith((".oga", ".ogg", ".wav")))
    ok("...and whatever file it was given exists",
       "-i" in argv or os.path.exists(argv[-1]), argv)
else:
    ok("no player on this machine, which is allowed", True)

# prefs.json is a file people are told they can edit. A typo in it must not be
# the reason the daemon does not come up.
junk = Alerts(notifier=FakeNotifier(), first="sixty", repeat=None, sound="yes")
check("a hand-typed threshold falls back", junk.first, A.FIRST_TOAST)
check("...and so does the other one", junk.repeat, A.REPEAT_TOAST)
ok("a truthy string does not become a half-on flag", junk.sound is True)
junk.update([Fake(alert="permission_prompt", msg="needs you")], now=0)
junk.update([Fake(alert="permission_prompt", msg="needs you")], now=A.FIRST_TOAST)
check("...and it still escalates on the defaults", len(junk.notifier.toasts), 1)


# ======================================================== 8. the posture itself
# `Creature` needs the baked atlas but no window, so this runs without a display.
# Every check here is a way he can get *stuck*, which is the failure mode that
# matters for a thing that is supposed to sit on your desktop for days.
try:
    import guy  # noqa: E402

    def creature():
        return guy.Creature(guy.Sprites(3), 400, 300)

    def run(c, now, frames):
        for _ in range(frames):
            now += 1 / 30
            c.update(1 / 30, now)
        return now

    c, t = creature(), 1000.0
    c.set_mode("idle")
    c.cue("wave")
    t = run(c, t, 5)
    c.cue("wave")                     # a second click while the first wave plays
    check("a cue during a cue does not park itself", c._cue, "idle")
    t = run(c, t, 200)
    ok("...so he comes back to a mode that is actually driven",
       c.mode in ("idle", "patrol"), c.mode)
    c.last_activity = t - 9999
    t = run(c, t, 60)
    check("...and can still fall asleep afterwards", c.mode, "sleep")

    c, t = creature(), 1000.0
    c.set_mode("working")
    c.cue("wave")
    t = run(c, t, 3)
    c.cue("wave")
    t = run(c, t, 200)
    check("a double click while working goes back to working", c.mode, "working")

    # A failure waves *and* slumps, so `error` sits in `_cue` for a few frames.
    c, t = creature(), 1000.0
    c.set_mode("error")
    c.cue("wave")
    t = run(c, t, 200)
    check("left alone, the wave ends in the slump", c.mode, "error")

    c, t = creature(), 1000.0
    c.set_mode("error")
    c.cue("wave")
    c.leave_error()                   # clicked during those frames
    t = run(c, t, 200)
    ok("acknowledging mid-wave does not drop him back into it",
       c.mode in ("idle", "patrol"), c.mode)
    c, t = creature(), 1000.0
    c.set_mode("error")
    t = run(c, t, 30)
    c.leave_error()
    check("acknowledging a settled slump also clears it", c.mode, "idle")

    stale = {}
    c = guy.Creature(guy.Sprites(3), 400, 300, on_stale=lambda: stale.update(hit=True))
    c.set_mode("working")
    c.last_activity = 1000.0
    c.update(1 / 30, 1000.0 + guy.STALE_SECS + 1)
    check("a session that stops answering slumps", c.mode, "error")
    ok("...and tells the daemon so it can raise an alert", stale.get("hit") is True)
except Exception as exc:                     # no atlas, no GdkPixbuf
    print(f"  (skipped the posture checks: {exc})")


# ================================== 9. how fast he reacts to a real file landing
# The acceptance criterion is 250ms from the notification. The path is: hook
# writes the file, Gio.FileMonitor fires, the store coalesces for 30ms, the
# next tick draws. This measures the first two, which are the only parts that
# can be slow - the tick is a fixed 1/30s on top.
try:
    import gi

    from gi.repository import GLib
    import sessions as S

    S.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    for old in S.SESSIONS_DIR.glob("*.json"):
        old.unlink()
    result = {}
    st = S.SessionStore(metrics=False)

    def on_change(store_):
        s = store_.newest()
        if s is not None and s.alert:
            result["latency"] = time.time() - result["t0"]
            result["kind"] = s.alert
            loop.quit()

    st.on_change = on_change
    st.start()
    loop = GLib.MainLoop()

    def land():
        result["t0"] = time.time()
        now = time.time()
        (S.SESSIONS_DIR / "live.json").write_text(json.dumps({
            "session_id": "live", "state": "alert", "alert": "permission_prompt",
            "alert_msg": "Claude needs your permission to use Bash",
            "ts": now, "heartbeat": now, "session_started": now}))
        return False

    GLib.idle_add(land)
    GLib.timeout_add(2000, lambda: (loop.quit(), False)[1])
    loop.run()
    ms = result.get("latency", 99) * 1000
    print(f"alert latency  {ms:.0f}ms file to daemon (budget 250)")
    ok(f"an alert reaches the daemon in {ms:.0f}ms, budget 250", ms < 250)
    check("...and arrives intact", result.get("kind"), "permission_prompt")
except Exception as exc:                     # no inotify, no GLib, no home
    print(f"  (skipped the live file-monitor check: {exc})")


# ---------------------------------------------------------------------- report
print(f"\n{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print("  FAIL " + f)
sys.exit(1 if FAILED else 0)
