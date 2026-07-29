#!/usr/bin/env python3
"""Live Claude Code sessions, read from ~/.deck-guy/sessions/.

One file per session, written by notify.sh on every hook event. This module is
the only place that knows that format. It has no GTK in it and no drawing: it
parses, it reaps the dead, and it hands out an ordered list.

    store = SessionStore(on_change=redraw)
    store.start()          # file monitor + reaper, needs a GLib main loop
    store.newest()         # the session the creature reacts to

`describe()` is the other half of the file and is deliberately dumb: the hook
captures whatever fields it recognised without ranking them, and the ranking
happens here, where changing it does not cost a tool call.
"""

import json
import os
import time
from pathlib import Path

from gi.repository import Gio, GLib

ROOT = Path(os.environ.get("DECK_GUY_HOME") or Path.home() / ".deck-guy")
SESSIONS_DIR = ROOT / "sessions"
STEPS_DIR = ROOT / "steps"          # notify.sh keeps its turn timestamps here

STALE_SECS = 45       # no heartbeat this long and the session is presumed dead
REAP_SECS = 5         # how often we look for those
LABEL_MAX = 46        # characters of "Tool target" that fit above his head


# --------------------------------------------------------------- target naming
def _rel(path, cwd):
    """`src/auth.py`, not `/home/jeet/projects/thing/src/auth.py`.

    Relative to the session's own directory when it sits underneath it, because
    that is the form you would have typed. Bare basename otherwise.
    """
    try:
        p = Path(path)
        if cwd:
            try:
                return str(p.relative_to(cwd))
            except ValueError:
                pass
        return p.name or str(p)
    except (TypeError, ValueError):
        return str(path)


def _head(text):
    """First line of a command, up to the first pipe - `pytest -q`, not the tee."""
    for cut in ("\n", "|", "&&", ";"):
        text = text.split(cut, 1)[0]
    return text.strip()


def _host(url):
    rest = url.split("://", 1)[-1]
    return rest.split("/", 1)[0] or url


def describe(tool, inp, cwd=""):
    """One short phrase naming what the tool is acting on. Never raises.

    Order matters and is the judgement call in this phase: a Bash call has both
    `command` and `description`, and the command is what you actually want to
    see. An unknown tool falls through to nothing, which renders as the bare
    tool name rather than as an error.
    """
    if not isinstance(inp, dict):
        return ""
    try:
        if inp.get("file_path"):
            return _rel(inp["file_path"], cwd)
        if inp.get("command"):
            return _head(inp["command"])
        if inp.get("pattern"):
            return '"%s"' % inp["pattern"]
        if inp.get("url"):
            return _host(inp["url"])
        if inp.get("description"):
            return inp["description"]
    except Exception:                      # a shape we have never seen
        return ""
    return ""


def elide(text, limit=LABEL_MAX):
    text = " ".join(str(text).split())     # collapse any newline that survived
    return text if len(text) <= limit else text[: limit - 1] + "…"


def clock(secs):
    """`07s`, `4m 12s`, `1h 03m` - always two fields, always the same width."""
    secs = max(0, int(secs))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60:02d}s"
    return f"{secs // 3600}h {secs % 3600 // 60:02d}m"


# -------------------------------------------------------------------- session
class Session:
    """One live Claude Code session. Every field is optional but the id."""

    def __init__(self, data):
        d = data if isinstance(data, dict) else {}
        self.id = str(d.get("session_id") or "?")
        self.state = d.get("state") or "idle"
        self.event = d.get("event") or ""
        self.tool = d.get("tool") or ""
        self.cwd = d.get("cwd") or ""
        self.transcript_path = d.get("transcript_path") or ""
        self.permission_mode = d.get("permission_mode") or ""
        self.input = d.get("input") if isinstance(d.get("input"), dict) else {}
        self.ts = _num(d.get("ts"))
        self.prompt_ts = _num(d.get("prompt_ts")) or self.ts
        self.started = _num(d.get("session_started")) or self.ts
        self.heartbeat = _num(d.get("heartbeat")) or self.ts

    # The hook rewrites the whole file each event, and not every event carries
    # every field, so keep the last non-empty value we were told.
    def inherit(self, old):
        for field in ("cwd", "transcript_path", "permission_mode"):
            if not getattr(self, field) and getattr(old, field, ""):
                setattr(self, field, getattr(old, field))
        return self

    @property
    def project(self):
        return Path(self.cwd).name if self.cwd else ""

    @property
    def target(self):
        return describe(self.tool, self.input, self.cwd)

    @property
    def label(self):
        """What goes in the bubble: `Bash pytest -q`, `Edit src/auth.py`."""
        if self.state == "jump":
            return "done"
        if not self.tool:
            return "thinking" if self.state == "working" else ""
        return elide(f"{self.tool} {self.target}".strip())

    def elapsed(self, now=None):
        return max(0.0, (now or time.time()) - self.ts)

    def runtime(self, now=None):
        return max(0.0, (now or time.time()) - self.started)

    @property
    def task_secs(self):
        """How long the turn has been going - what the jump height scales on."""
        return max(0.0, self.ts - self.prompt_ts)

    def stale(self, now=None, limit=STALE_SECS):
        return (now or time.time()) - self.heartbeat > limit

    def __repr__(self):
        return f"<Session {self.id[:8]} {self.state} {self.label!r}>"


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------- store
class SessionStore:
    """Watches the sessions directory. Calls `on_change` when anything moves.

    A `Gio.FileMonitor` replaces the 250ms stat poll the pet used: with several
    sessions writing on every tool call, polling would be both slower to react
    and more work at idle. The reaper is the only timer left, and it runs every
    few seconds regardless of what the monitor says, because a session that dies
    with its terminal never writes anything again.
    """

    def __init__(self, on_change=None, stale=STALE_SECS):
        self.on_change = on_change
        self.stale = stale
        self.sessions = {}
        self._monitor = None
        self._pending = 0
        self.reload()

    def start(self):
        self._watch()
        GLib.timeout_add_seconds(REAP_SECS, self._reap_tick)
        return self

    def _watch(self):
        try:
            SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
            gfile = Gio.File.new_for_path(str(SESSIONS_DIR))
            self._monitor = gfile.monitor_directory(Gio.FileMonitorFlags.NONE, None)
            self._monitor.connect("changed", self._on_event)
        except Exception as exc:           # no inotify, read-only home, ...
            print(f"session monitor unavailable ({exc}); polling instead", flush=True)
            self._monitor = None
            GLib.timeout_add(250, lambda: (self.reload(True), True)[1])

    def _on_event(self, _mon, _f, _other, _event):
        # Several files can change in one burst; coalesce into one reload so a
        # busy tool call does not cause four repaints.
        if self._pending:
            return
        self._pending = GLib.timeout_add(30, self._flush)

    def _flush(self):
        self._pending = 0
        self.reload(True)
        return False

    def _reap_tick(self):
        # A monitor whose directory was deleted never fires again, so re-arm it
        # here rather than going quiet for the rest of the daemon's life.
        if self._monitor is not None and not SESSIONS_DIR.exists():
            self._watch()
        self.reload(True)
        return True

    def reload(self, notify=False):
        now = time.time()
        found = {}
        try:
            paths = list(SESSIONS_DIR.glob("*.json"))
        except OSError:
            paths = []
        for path in paths:
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue                   # half-written; the next event brings it back
            s = Session(data)
            old = self.sessions.get(s.id)
            if old:
                s.inherit(old)
            if s.state == "ended" or s.stale(now, self.stale):
                _reap(path, s.id)
                continue
            found[s.id] = s

        changed = self._digest(found) != self._digest(self.sessions)
        self.sessions = found
        if notify and changed and self.on_change:
            self.on_change(self)
        return found

    @staticmethod
    def _digest(sessions):
        return {k: (s.state, s.tool, s.label, s.ts) for k, s in sessions.items()}

    def live(self):
        """Most recently active first. Phase 1 draws only the first."""
        return sorted(self.sessions.values(), key=lambda s: s.ts, reverse=True)

    def newest(self):
        live = self.live()
        return live[0] if live else None


def _reap(path, session_id):
    """Drop a dead session's file and the stamps notify.sh left beside it.

    The daemon owns cleanup because the hook cannot: a terminal that dies takes
    the session with it and never runs another line of shell.
    """
    for p in (path, STEPS_DIR / f"{session_id}.start",
              STEPS_DIR / f"{session_id}.prompt"):
        try:
            p.unlink()
        except OSError:
            pass
