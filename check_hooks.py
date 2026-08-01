#!/usr/bin/env python3
"""Are the hooks wired, are they real, and do they actually fire?

    python3 check_hooks.py            audit the wiring and exercise every event
    python3 check_hooks.py --trace    start recording real events, then re-run
    python3 check_hooks.py --off      stop recording and delete the log

Three different questions, and only the third one is hard:

1. **Is it wired?** `~/.claude/settings.json` versus what we expect. Catches a
   moved folder, a half-finished install, a hand-edit.
2. **Is the event name real?** Checked against the installed `claude` binary, not
   against memory. A hook wired to an event the CLI has renamed is silent and
   looks exactly like a hook that is working and has nothing to say.
3. **Does it fire?** The session file holds only the *last* event, so an event
   that fires and is overwritten a moment later leaves no evidence. `--trace`
   drops a marker file that makes `notify.sh` append one line per call, which
   turns the question into arithmetic.

Question 2 is the one that rots. Hooks are configuration pointing at a moving
target: nothing tells you when an event you depend on stops existing, because
"never fired" and "fired and did nothing" look identical from here.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
SETTINGS = Path.home() / ".claude/settings.json"
ROOT = Path(os.environ.get("DECK_GUY_HOME") or Path.home() / ".deck-guy")
TRACE = ROOT / "trace"
EVENTS = ROOT / "events.log"

# What install.sh writes, and what each one is for. Keep in step with the `want`
# table in install.sh - the audit below fails loudly if they drift.
WANT = {
    "SessionStart":       ("--ensure idle", "starts the daemon"),
    "UserPromptSubmit":   ("--ensure prompt",
                       "stamps the turn; also revives a daemon that died"),
    "PreToolUse":         ("working", "he starts working, puts the costume on"),
    "PostToolUse":        ("working", "clears an alert the moment you approve"),
    "Stop":               ("jump", "he celebrates"),
    "SessionEnd":         ("idle", "reaps the session"),
    "Notification":       ("alert", "you are the blocker - badge, toast, sound"),
    "PostToolUseFailure": ("alert", "red badge, no noise"),
    "PermissionDenied":   ("working", "clears an alert the moment you deny"),
    "StopFailure":        ("error", "the session itself failed - he slumps"),
}

GREEN, RED, DIM, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = DIM = OFF = ""

problems = []


def line(state, name, note=""):
    mark = {"ok": f"{GREEN}ok  {OFF}", "bad": f"{RED}FAIL{OFF}",
            "warn": f"{RED}warn{OFF}", "-": f"{DIM}--  {OFF}"}[state]
    print(f"  {mark} {name:<20} {DIM}{note}{OFF}" if note else f"  {mark} {name}")
    if state == "bad":
        problems.append(f"{name}: {note}")


# ------------------------------------------------------- 1. is it wired at all
def audit_settings():
    print("\nwiring — ~/.claude/settings.json")
    if not SETTINGS.exists():
        line("bad", "settings.json", "does not exist; run ./install.sh")
        return {}
    try:
        cfg = json.loads(SETTINGS.read_text())
    except ValueError as exc:
        line("bad", "settings.json", f"is not valid JSON: {exc}")
        return {}
    hooks = cfg.get("hooks", {})
    mine = {}
    for event, (arg, why) in WANT.items():
        entries = [h.get("command", "")
                   for g in hooks.get(event, []) for h in g.get("hooks", [])
                   if h.get("command", "").split()[:1]
                   and h["command"].split()[0].endswith("/notify.sh")]
        if not entries:
            line("bad", event, f"not wired — {why}")
            continue
        if len(entries) > 1:
            line("bad", event, f"wired {len(entries)} times; it will fire twice")
            continue
        cmd = entries[0]
        script = Path(cmd.split()[0])
        got = " ".join(cmd.split()[1:])
        if not script.exists():
            line("bad", event, f"points at {script}, which is gone")
        elif not os.access(script, os.X_OK):
            line("bad", event, f"{script} is not executable")
        elif script.resolve() != (HERE / "notify.sh").resolve():
            line("warn", event, f"points at another copy: {script}")
        elif got != arg:
            line("bad", event, f"argument is {got!r}, expected {arg!r}")
        else:
            line("ok", event, why)
            mine[event] = cmd
    others = {e: len(g) for e, g in hooks.items() if e not in WANT}
    if others:
        print(f"  {DIM}--   other hooks present and untouched: "
              f"{', '.join(sorted(others))}{OFF}")
    return mine


# ------------------------------- 2. does the installed CLI still have the event
def audit_cli():
    print("\nevent names — against the installed claude binary")
    exe = None
    for cand in (Path.home() / ".local/bin/claude", Path("/usr/local/bin/claude")):
        if cand.exists():
            exe = cand.resolve()
            break
    if exe is None or not exe.is_file():
        line("warn", "claude binary", "not found; cannot verify the event names")
        return
    try:
        blob = exe.read_bytes()
    except OSError as exc:
        line("warn", "claude binary", f"unreadable: {exc}")
        return
    known = {m.decode() for m in
             re.findall(rb'hook_event_name:"([A-Za-z]+)"', blob)}
    ver = re.search(rb'VERSION:"([0-9.]+)"', blob)
    # The binary is named after its version, so printing both says it twice.
    print(f"  {DIM}claude {ver.group(1).decode() if ver else exe.name} advertises "
          f"{len(known)} hook events{OFF}")
    for event in WANT:
        if event in known:
            line("ok", event)
        else:
            line("bad", event, "the installed CLI no longer sends this event")
    unused = sorted(known - set(WANT))
    if unused:
        print(f"  {DIM}--   not used by the buddy: {', '.join(unused)}{OFF}")


# -------------------------------------- 3. does the script do the right thing
def exercise():
    """Feed notify.sh a realistic payload per event and read back the result."""
    print("\nbehaviour — every event, driven through notify.sh")
    tmp = tempfile.mkdtemp(prefix="deckguy-check-")
    env = dict(os.environ, DECK_GUY_HOME=tmp)
    sid = "check"
    out = Path(tmp) / "sessions" / f"{sid}.json"
    base = {"session_id": sid, "cwd": str(HERE),
            "transcript_path": "/tmp/x.jsonl", "permission_mode": "default"}

    def fire(arg, **payload):
        subprocess.run([str(HERE / "notify.sh"), arg], text=True, env=env,
                       input=json.dumps(dict(base, **payload)), check=False)
        return json.loads(out.read_text()) if out.exists() else {}

    tool = {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}}
    cases = [
        ("SessionStart", "--ensure idle", {"hook_event_name": "SessionStart",
                                           "source": "startup"},
         lambda r: r.get("state") == "idle"),
        ("UserPromptSubmit", "--ensure prompt", {"hook_event_name": "UserPromptSubmit"},
         lambda r: r.get("state") == "working"),
        ("PreToolUse", "working", dict(hook_event_name="PreToolUse", **tool),
         lambda r: r.get("state") == "working" and r.get("tool") == "Bash"),
        ("Notification", "alert",
         {"hook_event_name": "Notification", "message": "Claude needs your permission",
          "notification_type": "permission_prompt"},
         lambda r: r.get("alert") == "permission_prompt" and r.get("alert_msg")),
        ("PostToolUse", "working",
         dict(hook_event_name="PostToolUse", tool_response={"ok": 1}, **tool),
         lambda r: r.get("alert") == "" and r.get("state") == "working"),
        ("PermissionDenied", "working",
         dict(hook_event_name="PermissionDenied", reason="user", **tool),
         lambda r: r.get("alert") == ""),
        ("PostToolUseFailure", "alert",
         dict(hook_event_name="PostToolUseFailure", error="exit code 2",
              is_interrupt=False, **tool),
         lambda r: r.get("alert") == "tool_failed" and "exit code 2" in r.get("alert_msg", "")),
        ("PostToolUseFailure/esc", "alert",
         dict(hook_event_name="PostToolUseFailure", error="aborted",
              is_interrupt=True, **tool),
         lambda r: r.get("alert") == ""),
        ("StopFailure", "error",
         {"hook_event_name": "StopFailure", "error": "model overloaded"},
         lambda r: r.get("state") == "error" and r.get("alert") == "session_failed"),
        ("Stop", "jump", {"hook_event_name": "Stop"},
         lambda r: r.get("state") == "jump"),
        ("SessionEnd", "idle", {"hook_event_name": "SessionEnd"},
         lambda r: r.get("state") == "ended"),
    ]
    for name, arg, payload, want in cases:
        try:
            res = fire(arg, **payload)
            line("ok" if want(res) else "bad", name,
                 "" if want(res) else f"state={res.get('state')!r} "
                                      f"alert={res.get('alert')!r}")
        except Exception as exc:
            line("bad", name, f"raised {type(exc).__name__}: {exc}")

    # Cost, on the payload shape that actually hurts: a Write carries the whole
    # new file on stdin.
    big = dict(base, hook_event_name="PreToolUse", tool_name="Write",
               tool_input={"file_path": "/tmp/x.py", "content": "x" * 60000})
    t0 = time.time()
    for _ in range(20):
        subprocess.run([str(HERE / "notify.sh"), "working"], text=True, env=env,
                       input=json.dumps(big), check=False)
    ms = (time.time() - t0) / 20 * 1000
    line("ok" if ms < 25 else "warn", "cost", f"{ms:.1f}ms on a 60KB payload "
                                              f"(x2 per tool call: Pre + Post)")


# --------------------------------------------- 4. what has actually fired here
def report_trace():
    print("\nreal events — recorded on this machine")
    if not TRACE.exists():
        print(f"  {DIM}--   not recording. `python3 check_hooks.py --trace`, use "
              f"Claude Code for a bit, then re-run.{OFF}")
        return
    if not EVENTS.exists() or not EVENTS.stat().st_size:
        print(f"  {DIM}--   recording, but nothing has fired yet.{OFF}")
        return
    seen, first, last = Counter(), None, None
    for row in EVENTS.read_text().splitlines():
        parts = row.split()
        if len(parts) < 2:
            continue
        seen[parts[1]] += 1
        first = first or parts[0]
        last = parts[0]
    span = (float(last) - float(first)) / 60 if first and last else 0
    print(f"  {DIM}{sum(seen.values())} events over {span:.0f} min{OFF}")
    # Nothing here fires on a clock, so "has not fired" is only evidence of a
    # problem for the two that fire on *every tool call*. The rest are waiting
    # for a situation - and a trace started in the middle of a turn legitimately
    # has no UserPromptSubmit and no Stop in it.
    EVERY_CALL = ("PreToolUse", "PostToolUse")
    PER_TURN = ("UserPromptSubmit", "Stop")
    for event in WANT:
        n = seen[event]
        if n:
            line("ok", event, f"{n}x")
        elif event in EVERY_CALL:
            line("warn", event, "never fired, and every tool call should have")
        elif event in PER_TURN:
            line("-", event, "once per turn — none has finished since tracing began")
        else:
            line("-", event, "not seen yet — only fires on that situation")
    pre, post = seen["PreToolUse"], seen["PostToolUse"]
    if pre or post:
        # The pairing is the thing worth checking. PostToolUse is what clears an
        # alert the moment you approve, so a shortfall here is a badge that
        # stays lit; a Post with no Pre would mean the two are wired unevenly.
        gap = pre - post
        line("ok" if abs(gap) <= 1 else "warn", "Pre/Post pairing",
             f"{pre} in, {post} out"
             + ("" if abs(gap) <= 1 else f" — {abs(gap)} unmatched"))
    stray = sorted(set(seen) - set(WANT) - {"(none)"})
    if stray:
        print(f"  {DIM}--   also seen: {', '.join(stray)}{OFF}")


def main(argv):
    if "--off" in argv:
        TRACE.unlink(missing_ok=True)
        EVENTS.unlink(missing_ok=True)
        print("tracing off, log deleted")
        return 0
    if "--trace" in argv:
        ROOT.mkdir(parents=True, exist_ok=True)
        TRACE.touch()
        print(f"tracing on — every hook call appends one line to {EVENTS}\n"
              f"use Claude Code normally, then re-run this. `--off` to stop.")
        return 0
    audit_settings()
    audit_cli()
    exercise()
    report_trace()
    print()
    if problems:
        print(f"{RED}{len(problems)} problem(s){OFF}")
        for p in problems:
            print(f"  - {p}")
        print("\n./install.sh fixes wiring; a failing event name means the CLI "
              "changed under us.")
        return 1
    print(f"{GREEN}all good{OFF}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
