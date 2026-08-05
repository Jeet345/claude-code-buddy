#!/usr/bin/env python3
"""Tests for pid -> window -> focus.

    python3 test_windows.py

Three layers, and only the last one needs a screen:

    parsing     `_pids` on hostile input, and the ancestry walk against /proc
    contract    `notify.sh`'s bash walk agreeing with `windows.ancestry`, which
                is the seam most likely to rot: two implementations of the same
                traversal in two languages
    ranking     `find()` with the backend faked, so the "nearest ancestor, then
                the title naming the project" rule is checked without needing
                two editors open

The live X11 checks at the end are skipped, not failed, when there is no
display - the same way the transcript tests treat a machine with no transcripts.
They deliberately focus another window and put focus back, because the only
convincing test of "it raises the window" is that the active window changed.
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).parent
TMP = tempfile.mkdtemp(prefix="deckguy-win-")
os.environ["DECK_GUY_HOME"] = TMP

sys.path.insert(0, str(HERE))

PASS = 0
FAILED = []


def ok(label, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
    else:
        FAILED.append(f"{label} {detail}".rstrip())


def check(label, got, want):
    global PASS
    if got == want:
        PASS += 1
    else:
        FAILED.append(f"{label}\n     got  {got!r}\n     want {want!r}")


def section(name):
    print(f"\n{name}")


# --------------------------------------------------------------- pid parsing
section("sessions: the pids field")
from sessions import Session, _pids  # noqa: E402

check("a plain chain", _pids("36927,36079,9403"), [36927, 36079, 9403])
check("order is preserved, not sorted", _pids("9403,36927"), [9403, 36927])
check("empty is empty", _pids(""), [])
check("missing is empty", _pids(None), [])
check("a list instead of a string", _pids([1, 2]), [])
check("a number instead of a string", _pids(12345), [])
check("junk entries are dropped, good ones kept", _pids("12,abc,,34"), [12, 34])
check("pid 1 and 0 are not candidates", _pids("0,1,2"), [2])
check("whitespace", _pids(" 12 , 34 "), [12, 34])
check("negative", _pids("-5,7"), [7])

s = Session({"session_id": "a", "pids": "10,20"})
check("Session parses pids", s.pids, [10, 20])
check("a session with no pids", Session({"session_id": "a"}).pids, [])

# The hook rewrites the whole file every event. If one ever lands without the
# field, forgetting it would mean the click quietly stops working mid-session.
old = Session({"session_id": "a", "pids": "10,20", "cwd": "/tmp"})
new = Session({"session_id": "a"}).inherit(old)
check("pids survive an event that omits them", new.pids, [10, 20])
newer = Session({"session_id": "a", "pids": "77"}).inherit(old)
check("a fresh chain wins over the inherited one", newer.pids, [77])


# ------------------------------------------------------------ the ancestry walk
section("windows: the ancestry walk")
import windows  # noqa: E402

chain = windows.ancestry()
ok("our own chain is not empty", len(chain) > 0)
check("it starts with us", chain[0], os.getpid())
ok("it stops before init", 1 not in chain and 0 not in chain)
ok("it is bounded", len(chain) <= windows.MAX_DEPTH)
ok("every entry is a live pid",
   all(Path(f"/proc/{p}").exists() for p in chain), chain)
check("depth is honoured", len(windows.ancestry(depth=2)), 2)
check("a pid that cannot exist gives nothing", windows.ancestry(pid=1), [])
check("a dead pid gives just itself", windows.ancestry(pid=2 ** 30), [2 ** 30])

# comm can contain spaces and brackets: `bash -c 'exec -a "x) 1 2 3" sleep 5'`
# produces a stat line that splitting from the left gets wrong. The parse cuts
# after the last ')', so the walk has to survive it.
try:
    p = subprocess.Popen(["bash", "-c", 'exec -a "evil) 9 9 9" sleep 5'])
    time.sleep(0.3)
    got = windows.ancestry(pid=p.pid, depth=3)
    ok("a hostile process name does not break the walk",
       len(got) >= 2 and got[0] == p.pid and got[1] == os.getpid(), got)
    p.kill()
    p.wait()
except OSError as exc:                      # pragma: no cover
    print(f"  (could not spawn the hostile-name process: {exc})")


# ------------------------------------------------- notify.sh writes the same walk
section("notify.sh: the bash walk agrees with the python one")
sess_dir = Path(TMP) / "sessions"
payload = ('{"session_id":"pidtest","hook_event_name":"PreToolUse",'
           '"tool_name":"Bash","cwd":"/tmp"}')
env = dict(os.environ, DECK_GUY_HOME=TMP)
subprocess.run([str(HERE / "notify.sh"), "working"], input=payload, text=True,
               env=env, capture_output=True, timeout=10)
written = sess_dir / "pidtest.json"
if not written.exists():
    FAILED.append("notify.sh wrote no session file")
else:
    import json
    data = json.loads(written.read_text())
    got = _pids(data.get("pids"))
    ok("notify.sh recorded a chain", len(got) > 0, data.get("pids"))
    ok("it is bounded like the python one", len(got) <= windows.MAX_DEPTH)
    ok("it stops before init", 1 not in got and 0 not in got)
    # The hook's parent is the shell that ran it, which is this test process for
    # a direct exec. The two walks must agree from that point up, or the daemon
    # is matching against a chain nobody else computes the same way.
    want = windows.ancestry(pid=os.getpid())
    shared = [p for p in got if p in want]
    ok("the two walks overlap", len(shared) > 0, f"bash={got} python={want}")
    ok("the overlap is in the same order",
       shared == [p for p in want if p in got], f"bash={got} python={want}")


# -------------------------------------------------------------- ranking rules
section("windows: which window wins")


class FakeWin:
    def __init__(self, xid):
        self._xid = xid
        self.focused = 0

    def get_xid(self):
        return self._xid

    def focus(self, _ts):
        self.focused += 1


def fake(owners, stack_order):
    """Install a fake backend. `owners` is xid -> (pid, title)."""
    wins = [FakeWin(x) for x in stack_order]
    windows._backend = lambda: dict(owners)
    windows._stack = lambda: list(wins)
    return {w.get_xid(): w for w in wins}


real_backend, real_stack = windows._backend, windows._stack
try:
    # Nearest ancestor first: the terminal, not the grandparent that also has a
    # window. Getting this backwards raises a stranger's window.
    fake({11: (100, "terminal"), 22: (200, "desktop-ish")}, [11, 22])
    hit = windows.find([100, 200])
    check("nearest ancestor wins", hit[1], "terminal")
    hit = windows.find([999, 200])
    check("it falls through to the next ancestor", hit[1], "desktop-ish")
    check("no match at all", windows.find([1234]), None)
    check("an empty chain", windows.find([]), None)

    # One pid, several windows - an editor with two projects open. The title
    # naming the session's project is the only signal that tells them apart.
    fake({11: (100, "index.ts - other-project"),
          22: (100, "claude-code-buddy - Visual Studio Code")}, [11, 22])
    hit = windows.find([100], project="claude-code-buddy")
    check("the title naming the project wins", hit[1],
          "claude-code-buddy - Visual Studio Code")
    hit = windows.find([100], project="")
    check("with no project, the topmost of the stack wins", hit[1],
          "claude-code-buddy - Visual Studio Code")
    hit = windows.find([100], project="other-project")
    check("and it follows the project, not the stack", hit[1],
          "index.ts - other-project")
    hit = windows.find([100], project="NOT-OPEN-ANYWHERE")
    ok("an unmatched project still returns something", hit is not None)

    # Our own window is a dock and is never a jump target - clicking him must
    # not raise him.
    fake({11: (os.getpid(), "guy.py")}, [11])
    check("we never focus ourselves", windows.find([os.getpid()]), None)

    # A window the backend knows about but the stack does not: it is unmapped or
    # was closed between the two reads.
    fake({11: (100, "gone"), 22: (100, "here")}, [22])
    hit = windows.find([100])
    check("a window missing from the stack is not a candidate", hit[1], "here")

    # focus() on a real hit drives the window and reports the name back.
    wins = fake({11: (100, "terminal")}, [11])
    r = windows.focus([100])
    ok("focus reports success", r.ok, r.reason)
    check("focus called the window", wins[11].focused, 1)
    check("focus reports which window", r.name, "terminal")
    check("focus reports the xid", r.xid, 11)

    # ---- honest failure. Every one of these must be a Result, never a raise.
    r = windows.focus([])
    ok("no pids is a clean failure", not r.ok)
    ok("and it says why", "no window recorded" in r.reason, r.reason)

    r = windows.focus([4242])
    ok("no matching window is a clean failure", not r.ok)
    ok("and it names Wayland as a cause", "Wayland" in r.reason, r.reason)

    windows._backend = False
    r = windows.focus([100])
    ok("no backend at all is a clean failure", not r.ok)
    ok("and it says what is missing",
       "libwnck" in r.reason or "xprop" in r.reason, r.reason)

    # A backend that throws mid-call - libwnck losing the screen, xprop killed.
    def boom():
        raise RuntimeError("backend exploded")

    windows._backend = boom
    check("a backend that raises returns None, not an exception",
          windows.find([100]), None)
    r = windows.focus([100])
    ok("and focus turns that into a clean failure", not r.ok, r.reason)

    # A window that refuses to be focused.
    class BadWin(FakeWin):
        def focus(self, _ts):
            raise RuntimeError("no")

    bad = BadWin(11)
    windows._backend = lambda: {11: (100, "stubborn")}
    windows._stack = lambda: [bad]
    r = windows.focus([100])
    ok("a window that refuses is a clean failure", not r.ok)
    ok("and the reason carries the error", "could not raise" in r.reason, r.reason)
    check("and it still says which window", r.name, "stubborn")
finally:
    windows._backend, windows._stack = real_backend, real_stack


# ------------------------------------------------------ against the real screen
section("acceptance: against this machine's windows")
have_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
if not have_display:
    print("  (no display - skipped)")
else:
    backend = windows._backend_fn()
    ok("a backend is available", bool(backend),
       "neither libwnck nor xprop found")
    if backend:
        owners = backend()
        ok("the backend found windows", len(owners) > 0)
        ok("every window has a pid", all(m[0] for m in owners.values()))
        ok("our own window is not offered as a target",
           all(m[0] != os.getpid() for m in owners.values()))

        stack = windows._stack()
        ok("the stack is not empty", len(stack) > 0)

        # The real thing: find the window that owns *this* test run and raise
        # it. Focus is restored afterwards, so a passing run leaves the desktop
        # exactly as it found it.
        import gi
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk

        def active():
            try:
                out = subprocess.run(["xprop", "-root", "_NET_ACTIVE_WINDOW"],
                                     capture_output=True, text=True, timeout=2)
                return out.stdout.strip().split("#")[-1].strip()
            except (OSError, subprocess.SubprocessError):
                return ""

        before = active()
        hit = windows.find(windows.ancestry())
        if hit is None:
            print("  (this test's own terminal owns no X window - focus "
                  "test skipped)")
        else:
            target = hex(hit[0].get_xid())
            others = [w for w in stack if hex(w.get_xid()) != target]
            if not others or not before:
                print("  (only one window on screen - focus test skipped)")
            else:
                d = Gdk.Display.get_default()

                def raise_(w):
                    w.focus(0)
                    if d:
                        d.flush()
                    time.sleep(0.8)

                # If our terminal is already active, "it focused" would pass
                # without the call doing anything at all. Move focus away first,
                # so the assertion is about our code and not about where the
                # pointer happened to be.
                if before == target:
                    raise_(others[0])
                    ok("moved focus away to make the test mean something",
                       active() != target, f"active={active()}")

                r = windows.focus(windows.ancestry())
                ok("focusing our own terminal reports success", r.ok, r.reason)
                time.sleep(0.8)
                ok("the active window actually changed to it",
                   active() == target, f"active={active()} want={target}")

                # Put the desktop back exactly as we found it.
                for w in stack:
                    if hex(w.get_xid()) == before:
                        raise_(w)
                        break
                ok("and focus was restored", active() == before,
                   f"active={active()} want={before}")


# ---------------------------------------------------------------------- report
import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{PASS} passed, {len(FAILED)} failed")
for f in FAILED:
    print("  FAIL " + f)
sys.exit(1 if FAILED else 0)
