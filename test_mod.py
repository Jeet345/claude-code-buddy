#!/usr/bin/env python3
"""Tests for the mod era: the approval bridge, the avatar layout, mod sessions.

    python3 test_mod.py

No display. The bridge is exercised over a real Unix socket with the same HTTP
shapes the deck-buddy mod sends, because "fail open" is a claim about the wire,
not about a class: every path that is not a click on Allow or Deny has to come
back as `ask`, which is Claude Code's own terminal dialog.
"""

import http.client
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).parent
TMP = tempfile.mkdtemp(prefix="deckguy-mod-")
os.environ["DECK_GUY_HOME"] = TMP

sys.path.insert(0, str(HERE))
import layout  # noqa: E402
from bridge import Approvals, Bridge  # noqa: E402
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


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout=5):
        super().__init__("buddy", timeout=timeout)
        self.unix_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_path)


def post(path, route, body, timeout=5):
    c = UnixHTTP(path, timeout)
    c.request("POST", route, json.dumps(body), {"content-type": "application/json"})
    r = c.getresponse()
    out = json.loads(r.read() or b"{}")
    c.close()
    return r.status, out


ASK = {"session_id": "s1", "id": "toolu_1", "tool": "Bash",
       "input": {"command": "pytest -q"}, "reason": "", "rule": "", "agent": ""}

# ---------------------------------------------------------------- Approvals
print("approvals: the state machine")

a = Approvals(enabled=False)
check("off -> ask", a.ask(ASK)["decision"], "ask")
check("off holds nothing", a.held(), [])

a = Approvals(enabled=True, focused=lambda sid: True)
check("terminal focused -> ask", a.ask(ASK)["decision"], "ask")

a = Approvals(enabled=True, focused=lambda sid: None)
check("cannot tell -> hold", a.ask(ASK)["decision"], "hold")
check("held once", [r.id for r in a.held()], ["toolu_1"])

a = Approvals(enabled=True, focused=lambda sid: (_ for _ in ()).throw(RuntimeError("x")))
check("focus probe raising -> hold, not crash", a.ask(ASK)["decision"], "hold")

a = Approvals(enabled=True, focused=lambda sid: False)
check("no id -> ask", a.ask({**ASK, "id": ""})["decision"], "ask")
check("garbage -> ask", a.ask("nope")["decision"], "ask")
check("unknown id waits for nothing", a.wait("toolu_nope", timeout=0.1)["decision"], "ask")

a = Approvals(enabled=True, focused=lambda sid: False)
a.ask(ASK)
check("bad decision refused", a.answer("toolu_1", "yes please"), False)
check("wrong id refused", a.answer("toolu_2", "allow"), False)
check("allow accepted", a.answer("toolu_1", "allow", remember=True), True)
check("second answer refused", a.answer("toolu_1", "deny"), False)
r = a.wait("toolu_1", timeout=1)
check("allow comes back", (r["decision"], r["remember"]), ("allow", True))
check("settled prompt is gone", a.held(), [])

a = Approvals(enabled=True, focused=lambda sid: False)
a.ask(ASK)
a.answer("toolu_1", "deny", remember=True)
check("remember only rides on allow", a.wait("toolu_1", timeout=1)["remember"], False)

a = Approvals(enabled=True, focused=lambda sid: False)
a.ask(ASK)
check("timeout -> ask", a.wait("toolu_1", timeout=0.1)["decision"], "ask")

# release and sync - the paths that hand a prompt back to the terminal
t = [100.0]
a = Approvals(enabled=True, focused=lambda sid: False, clock=lambda: t[0])
a.ask(ASK)
a.ask({**ASK, "id": "toolu_9", "session_id": "s2"})
check("release one session", a.release("s1"), 1)
check("release leaves the other", [r.id for r in a.held()], ["toolu_9"])
check("released answers ask", a.wait("toolu_1", timeout=1)["decision"], "ask")

a = Approvals(enabled=True, focused=lambda sid: False, clock=lambda: t[0])
a.ask(ASK)
a.sync("s1", None)
check("sync spares a prompt younger than the mod's first write", len(a.held()), 1)
t[0] += 5
a.sync("s1", "toolu_1")
check("sync keeps the prompt the file names", len(a.held()), 1)
a.sync("s1", None)
check("sync drops one the file no longer names", a.held(), [])

changes = []
a = Approvals(enabled=True, focused=lambda sid: False, on_change=lambda: changes.append(1))
a.ask(ASK)
a.answer("toolu_1", "allow")
ok("on_change fires on hold and on answer", len(changes) >= 2, str(changes))

# -------------------------------------------------------------- over the wire
print("bridge: over a real socket")

sock = os.path.join(TMP, "run", "buddy.sock")
prefs_seen = []
appr = Approvals(enabled=True, focused=lambda sid: False)
br = Bridge(sock, appr,
            prefs=lambda d: (prefs_seen.append(d), {"approve": appr.enabled})[1]).start()
ok("bridge started", br is not None)
mode = stat.S_IMODE(os.stat(sock).st_mode)
check("socket is 0600", oct(mode), oct(0o600))
check("socket dir is 0700", oct(stat.S_IMODE(os.stat(os.path.dirname(sock)).st_mode)), oct(0o700))

status, first = post(sock, "/ask", ASK)
check("/ask holds", (status, first["decision"], first["id"]), (200, "hold", "toolu_1"))

result = {}
waiter = threading.Thread(target=lambda: result.update(post(sock, "/wait", {"id": "toolu_1"}, 10)[1]))
waiter.start()
time.sleep(0.2)
ok("/wait blocks until answered", waiter.is_alive())
status, out = post(sock, "/answer", {"id": "toolu_1", "decision": "allow"})
check("/answer accepted", out, {"ok": True})
waiter.join(5)
check("/wait gets the answer for exactly that id",
      (result.get("id"), result.get("decision")), ("toolu_1", "allow"))

status, out = post(sock, "/nope", {})
check("unknown route is 404", status, 404)
c = UnixHTTP(sock)
c.request("POST", "/ask", "{not json", {"content-type": "application/json"})
check("bad json is 400", c.getresponse().status, 400)
c.close()
status, out = post(sock, "/prefs", {"approve": False})
check("/prefs reaches the daemon", prefs_seen[-1], {"approve": False})

second = Bridge(sock, Approvals()).start()
check("a second daemon does not steal a live socket", second, None)

# Fail open, proven by injury: a prompt held when the daemon stops comes back ask.
appr.ask({**ASK, "id": "toolu_2"})
result.clear()
waiter = threading.Thread(target=lambda: result.update(post(sock, "/wait", {"id": "toolu_2"}, 10)[1]))
waiter.start()
time.sleep(0.2)
br.stop()
waiter.join(5)
check("daemon stopping releases held prompts", result.get("decision"), "ask")
ok("socket removed on stop", not os.path.exists(sock))

# A stale socket file left by a crash is replaced, not refused.
open(sock, "w").close()
br2 = Bridge(sock, Approvals()).start()
ok("stale socket file replaced", br2 is not None)
if br2:
    br2.stop()

# --------------------------------------------------------------------- layout
print("layout: avatars and sidekicks")

row = layout.avatar_row(cx=800, body_w=120, ground_y=394, size=(60, 48), n=3, win_w=1920)
check("three avatars to his right", (len(row.rects), row.side, row.more), (3, 1, 0))
ok("first avatar clears his body", row.rects[0][0] >= 800 + 60, str(row.rects))
ok("feet on the ground", all(r[1] + r[3] == 394 for r in row.rects))
xs = [r[0] for r in row.rects]
ok("no overlap", all(b - a >= 60 for a, b in zip(xs, xs[1:])), str(xs))

row = layout.avatar_row(cx=1850, body_w=120, ground_y=394, size=(60, 48), n=3, win_w=1920)
check("no room on the right -> left", row.side, -1)
ok("left row stays on screen", min(r[0] for r in row.rects) >= layout.EDGE)
ok("left row clears his body", max(r[0] + r[2] for r in row.rects) <= 1850 - 60)

row = layout.avatar_row(cx=800, body_w=120, ground_y=394, size=(60, 48), n=9, win_w=1920)
check("cap and overflow", (len(row.rects), row.more), (layout.AVATAR_CAP, 3))
ok("+n label placed", row.more_at is not None)

row = layout.avatar_row(cx=200, body_w=120, ground_y=394, size=(60, 48), n=8, win_w=420)
ok("narrow strip never runs off screen",
   all(layout.EDGE <= r[0] and r[0] + r[2] <= 420 - layout.EDGE for r in row.rects), str(row.rects))
check("narrow strip counts what it dropped", len(row.rects) + row.more, 8)

av = layout.avatar_row(cx=800, body_w=120, ground_y=394, size=(60, 48), n=2, win_w=1920)
sk = layout.sidekick_row(cx=800, body_w=120, ground_y=394, size=(40, 32), n=2,
                         win_w=1920, avoid=av.side)
check("sidekicks take the other side", sk.side, -av.side)
check("zero of anything is an empty row", layout.avatar_row(800, 120, 394, (60, 48), 0, 1920).rects, [])
check("hit test", layout.hit([(10, 10, 5, 5), (20, 10, 5, 5)], 22, 12), 1)
check("hit test miss", layout.hit([(10, 10, 5, 5)], 0, 0), -1)

# ----------------------------------------------------------- mod sessions
print("sessions: what the mod writes")

MOD = {"source": "mod", "session_id": "abc", "state": "working", "event": "PreToolUse",
       "tool": "Bash", "cwd": "/home/x/api", "ts": 100, "prompt_ts": 90,
       "session_started": 50, "heartbeat": 100, "alert": "", "alert_msg": "",
       "pids": "10,9,8", "input": {"command": "pytest -q"}, "phase": "tool",
       "metrics": {"model": "claude-opus-5-5", "effort": "high", "context": 42000,
                   "window": 200000, "cost": 1.25, "turns": 3},
       "pending": None, "agents": [{"id": "a1", "type": "Explore", "description": "scan"}]}

s = Session(MOD)
check("source", s.source, "mod")
check("tool phase shows the tool", s.label, "Bash pytest -q")
check("thinking phase", Session({**MOD, "phase": "thinking"}).label, "thinking")
check("responding phase", Session({**MOD, "phase": "responding"}).label, "writing")
check("hook sessions keep their label", Session({**MOD, "source": "", "phase": ""}).label,
      "Bash pytest -q")
m = s.metrics
ok("metrics come from the file", m is not None and m.live)
check("metric values", (m.context, m.window, m.cost, m.model, m.window_approx),
      (42000, 200000, 1.25, "claude-opus-5-5", False))
check("fill", round(m.fill, 2), 0.21)
check("subagents", [a["id"] for a in s.agents], ["a1"])
check("junk agents dropped", Session({**MOD, "agents": [1, "x", {"id": "ok"}]}).agents, [{"id": "ok"}])
check("agents not a list", Session({**MOD, "agents": "nope"}).agents, [])
check("metrics junk", Session({**MOD, "metrics": "nope"}).metrics, None)

old = Session(MOD)
new = Session({**MOD, "metrics": {**MOD["metrics"], "cost": 2.0}})
new.inherit(old)
check("mod metrics are not overwritten by the old reading", new.metrics.cost, 2.0)

check("priority: held prompt", Session({**MOD, "pending": {"id": "t"}}).priority, 4)
check("priority: permission alert", Session({**MOD, "alert": "permission_prompt"}).priority, 4)
check("priority: failed", Session({**MOD, "state": "error"}).priority, 3)
check("priority: running", s.priority, 2)
check("priority: done", Session({**MOD, "state": "jump"}).priority, 1)
check("priority: idle", Session({**MOD, "state": "idle"}).priority, 0)

# notify.sh steps aside for a session the mod owns, and still writes others.
print("notify.sh: steps aside for the mod")
sess_dir = Path(TMP) / "sessions"
sess_dir.mkdir(parents=True, exist_ok=True)
(Path(TMP) / "steps").mkdir(exist_ok=True)
owned = sess_dir / "owned.json"
owned.write_text(json.dumps(MOD) + "\n")
before = owned.read_text()
env = {**os.environ, "DECK_GUY_HOME": TMP, "DISPLAY": ""}
env.pop("WAYLAND_DISPLAY", None)
payload = json.dumps({"session_id": "owned", "hook_event_name": "PreToolUse",
                      "tool_name": "Read", "cwd": "/x"})
subprocess.run([str(HERE / "notify.sh"), "working"], input=payload, text=True, env=env,
               timeout=10)
check("mod-owned file untouched", owned.read_text(), before)
payload = json.dumps({"session_id": "plain", "hook_event_name": "PreToolUse",
                      "tool_name": "Read", "cwd": "/x"})
subprocess.run([str(HERE / "notify.sh"), "working"], input=payload, text=True, env=env,
               timeout=10)
plain = sess_dir / "plain.json"
ok("hook session still written", plain.exists() and '"tool":"Read"' in plain.read_text())

print()
print(f"{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print("  FAIL " + f)
sys.exit(1 if FAILED else 0)
