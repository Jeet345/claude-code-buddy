#!/usr/bin/env python3
"""Tests for the session model. No GTK, no display, no main loop.

    python3 test_sessions.py

Covers every tool in TOOL_COSTUME plus tools that do not exist, because the
bubble must degrade to a bare name rather than raise on a tool Claude Code
grows next month.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).parent
TMP = tempfile.mkdtemp(prefix="deckguy-test-")
os.environ["DECK_GUY_HOME"] = TMP

sys.path.insert(0, str(HERE))
import sessions  # noqa: E402
from sessions import Session, SessionStore, clock, describe, elide  # noqa: E402
from sprites import TOOL_COSTUME  # noqa: E402

FAILED = []


def check(label, got, want):
    if got != want:
        FAILED.append(f"{label}\n     got  {got!r}\n     want {want!r}")


def ok(label, cond, detail=""):
    if not cond:
        FAILED.append(f"{label} {detail}")


# ------------------------------------------------------------ target extraction
CWD = "/home/jeet/projects/thing"
CASES = [
    # tool,          tool_input,                                    expected target
    ("Read",         {"file_path": f"{CWD}/src/auth.py"},           "src/auth.py"),
    ("Read",         {"file_path": "/etc/hosts"},                   "hosts"),
    ("Grep",         {"pattern": "TODO", "path": "src"},            '"TODO"'),
    ("Glob",         {"pattern": "**/*.py"},                        '"**/*.py"'),
    ("NotebookRead", {"file_path": f"{CWD}/nb.ipynb"},              "nb.ipynb"),
    ("Edit",         {"file_path": f"{CWD}/a/b.py"},                "a/b.py"),
    ("Write",        {"file_path": f"{CWD}/new.py"},                "new.py"),
    ("MultiEdit",    {"file_path": f"{CWD}/x.py"},                  "x.py"),
    ("NotebookEdit", {"file_path": f"{CWD}/n.ipynb"},               "n.ipynb"),
    ("Bash",         {"command": "pytest -q", "description": "t"},  "pytest -q"),
    ("Bash",         {"command": "pytest -q | tee out.log"},        "pytest -q"),
    ("Bash",         {"command": "make && ./run"},                  "make"),
    ("Bash",         {"command": "cd /tmp; ls"},                    "cd /tmp"),
    ("Bash",         {"command": "ls \\\n  -la"},                   "ls \\"),
    ("BashOutput",   {"bash_id": "abc"},                            ""),
    ("KillShell",    {"shell_id": "abc"},                           ""),
    ("WebFetch",     {"url": "https://docs.anthropic.com/x/y"},     "docs.anthropic.com"),
    ("WebSearch",    {"query": "gtk3 input shape"},                 ""),
    ("Task",         {"description": "Explore the repo",
                      "subagent_type": "Explore"},                  "Explore the repo"),
    ("Agent",        {"description": "Review diff"},                "Review diff"),
    # Tools that do not exist. None of these may raise.
    ("Telepathy",    {"thought": "..."},                            ""),
    ("Frobnicate",   {},                                            ""),
    ("Quantum",      {"file_path": None},                           ""),
]

for tool, inp, want in CASES:
    try:
        check(f"describe({tool}, {inp})", describe(tool, inp, CWD), want)
    except Exception as exc:
        FAILED.append(f"describe({tool}) raised {type(exc).__name__}: {exc}")

# Every tool that has a costume must survive an empty and a junk input.
for tool in TOOL_COSTUME:
    for junk in ({}, None, [], {"file_path": 42}, {"command": ""}):
        try:
            describe(tool, junk, CWD)
        except Exception as exc:
            FAILED.append(f"describe({tool}, {junk!r}) raised {type(exc).__name__}: {exc}")

check("elide short", elide("Bash pytest -q"), "Bash pytest -q")
check("elide long", elide("Bash " + "x" * 80), "Bash " + "x" * 40 + "…")
check("elide newline", elide("a\n  b"), "a b")
check("clock secs", clock(7), "7s")
check("clock mins", clock(252), "4m 12s")
check("clock hours", clock(3780), "1h 03m")
check("clock negative", clock(-5), "0s")


# ------------------------------------------------------------------- session
now = time.time()
s = Session({
    "session_id": "abc", "state": "working", "tool": "Bash",
    "cwd": CWD, "input": {"command": "pytest -q"},
    "ts": now - 7, "prompt_ts": now - 40, "session_started": now - 252,
    "heartbeat": now,
})
check("label", s.label, "Bash pytest -q")
check("project", s.project, "thing")
check("elapsed", int(s.elapsed(now)), 7)
check("runtime", int(s.runtime(now)), 252)
check("task_secs", int(s.task_secs), 33)
ok("fresh session is not stale", not s.stale(now))
ok("old session is stale", Session({"session_id": "x", "heartbeat": now - 99}).stale(now))

check("no tool while working reads as thinking",
      Session({"session_id": "a", "state": "working"}).label, "thinking")
check("jump reads as done",
      Session({"session_id": "a", "state": "jump", "tool": "Bash"}).label, "done")
check("idle with no tool is silent",
      Session({"session_id": "a", "state": "idle"}).label, "")
check("garbage session does not raise", Session("not a dict").id, "?")
check("missing numbers default to zero", Session({"session_id": "a", "ts": "soon"}).ts, 0.0)

old = Session({"session_id": "a", "cwd": CWD, "transcript_path": "/t.jsonl"})
new = Session({"session_id": "a"}).inherit(old)
check("inherits cwd when an event omits it", new.cwd, CWD)
check("inherits transcript path", new.transcript_path, "/t.jsonl")


# ------------------------------------------------------------- store on disk
def write_session(sid, **kw):
    d = {"session_id": sid, "state": "working", "tool": "Bash",
         "input": {"command": "sleep 1"}, "ts": time.time(),
         "heartbeat": time.time()}
    d.update(kw)
    sessions.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    (sessions.SESSIONS_DIR / f"{sid}.json").write_text(json.dumps(d))


write_session("one", ts=time.time() - 5)
write_session("two")
store = SessionStore()
check("two sessions, two files", len(store.live()), 2)
check("newest first", store.newest().id, "two")

write_session("dead", heartbeat=time.time() - 999, ts=time.time() - 999)
store.reload()
check("stale session reaped", sorted(s.id for s in store.live()), ["one", "two"])
ok("stale file deleted", not (sessions.SESSIONS_DIR / "dead.json").exists())

write_session("bye", state="ended")
store.reload()
ok("ended session removed", "bye" not in store.sessions)

(sessions.SESSIONS_DIR / "half.json").write_text('{"session_id": "half", "st')
store.reload()
ok("truncated file ignored, others survive", len(store.live()) == 2)

seen = []
store2 = SessionStore(on_change=lambda st: seen.append(len(st.live())))
write_session("three")
store2.reload(notify=True)
ok("on_change fired for a new session", seen == [3], f"got {seen}")
store2.reload(notify=True)
ok("on_change silent when nothing moved", seen == [3], f"got {seen}")


# -------------------------------------------------- end to end through the hook
env = dict(os.environ, DECK_GUY_HOME=TMP + "/hook")
payloads = [
    {"session_id": "e2e", "hook_event_name": "SessionStart", "cwd": CWD,
     "transcript_path": "/t/e2e.jsonl", "permission_mode": "acceptEdits"},
    {"session_id": "e2e", "hook_event_name": "UserPromptSubmit", "cwd": CWD},
    # Shaped like a real PreToolUse: the CLI repeats cwd and permission_mode on
    # every event, which is why the file can be rewritten whole each time.
    {"session_id": "e2e", "hook_event_name": "PreToolUse", "cwd": CWD,
     "permission_mode": "acceptEdits", "tool_name": "Edit",
     "tool_input": {"file_path": f"{CWD}/src/auth.py"}},
]
for p in payloads:
    subprocess.run([str(HERE / "notify.sh"), "working"],
                   input=json.dumps(p), text=True, env=env)

hook_file = Path(TMP) / "hook" / "sessions" / "e2e.json"
ok("hook wrote a session file", hook_file.exists())
if hook_file.exists():
    hs = Session(json.loads(hook_file.read_text()))
    check("hook e2e label", hs.label, "Edit src/auth.py")
    check("hook e2e state", hs.state, "working")
    check("hook e2e project", hs.project, "thing")
    check("hook e2e permission mode", hs.permission_mode, "acceptEdits")
    ok("hook stamped the turn start", 0 <= hs.task_secs < 5, f"{hs.task_secs}")

end = dict(payloads[0], hook_event_name="SessionEnd")
subprocess.run([str(HERE / "notify.sh"), "idle"], input=json.dumps(end), text=True, env=env)
state = json.loads(hook_file.read_text())["state"]
check("SessionEnd marks the session ended", state, "ended")

# A payload with no session_id at all still animates him.
subprocess.run([str(HERE / "notify.sh"), "working"], input="{}", text=True, env=env)
ok("payload without a session id falls back",
   (Path(TMP) / "hook" / "sessions" / "default.json").exists())

# ...but starting the daemon by hand is not a session and must not invent one.
env2 = dict(os.environ, DECK_GUY_HOME=TMP + "/hand", DISPLAY="")
subprocess.run([str(HERE / "notify.sh"), "--ensure", "idle"],
               input="", text=True, env=env2)
ok("hand-started daemon writes no phantom session",
   not (Path(TMP) / "hand" / "sessions" / "default.json").exists())

# A session that predates the hooks still accumulates runtime: the first event
# we see stamps the start, and later events must not reset it.
env3 = dict(os.environ, DECK_GUY_HOME=TMP + "/old")
# sessions/ already exists because the daemon makes it for its file monitor - the
# hook still has to create steps/ for itself.
(Path(TMP) / "old" / "sessions").mkdir(parents=True, exist_ok=True)
mid = {"session_id": "old", "hook_event_name": "PreToolUse", "cwd": CWD,
       "tool_name": "Bash", "tool_input": {"command": "ls"}}
subprocess.run([str(HERE / "notify.sh"), "working"], input=json.dumps(mid),
               text=True, env=env3)
ok("the hook creates its own steps dir even when sessions/ already exists",
   (Path(TMP) / "old" / "steps" / "old.start").exists())
first = json.loads((Path(TMP) / "old" / "sessions" / "old.json").read_text())
time.sleep(1.1)
subprocess.run([str(HERE / "notify.sh"), "working"], input=json.dumps(mid),
               text=True, env=env3)
second = json.loads((Path(TMP) / "old" / "sessions" / "old.json").read_text())
check("session start is stamped once, not per event",
      second["session_started"], first["session_started"])
check("turn start is stamped once too, so a finished task is not always instant",
      second["prompt_ts"], first["prompt_ts"])
ok("runtime climbs between events", second["ts"] > first["ts"],
   f'{second["ts"]} vs {first["ts"]}')
ok("a finished turn has a measurable length",
   Session(second).task_secs >= 1, f'{Session(second).task_secs}')

# A new prompt restarts the turn clock without touching the session clock.
subprocess.run([str(HERE / "notify.sh"), "prompt"], text=True, env=env3,
               input=json.dumps(dict(mid, hook_event_name="UserPromptSubmit")))
third = json.loads((Path(TMP) / "old" / "sessions" / "old.json").read_text())
check("a new prompt resets the turn clock", third["prompt_ts"], third["ts"])
check("but not the session clock", third["session_started"], first["session_started"])

# The daemon owns cleanup: reaping a session takes its stamps with it.
sessions.STEPS_DIR.mkdir(parents=True, exist_ok=True)
(sessions.STEPS_DIR / "gone.start").write_text("1")
(sessions.STEPS_DIR / "gone.prompt").write_text("1")
write_session("gone", heartbeat=time.time() - 999, ts=time.time() - 999)
store.reload()
ok("stamps reaped with the session",
   not (sessions.STEPS_DIR / "gone.start").exists()
   and not (sessions.STEPS_DIR / "gone.prompt").exists())


# ---------------------------------------------------------------------- report
print(f"{len(CASES) + len(TOOL_COSTUME) * 5} extraction cases, "
      f"{len(FAILED)} failures")
for f in FAILED:
    print("  FAIL " + f)
sys.exit(1 if FAILED else 0)
