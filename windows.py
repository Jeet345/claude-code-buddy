#!/usr/bin/env python3
"""Turning a session's process chain into a terminal window you can jump to.

A hook payload never says which window you are looking at. It says `cwd` and
`session_id`, and that is all. So the chain is built from the one thing a hook
does have - its own place in the process tree:

    hook -> zsh -> claude -> zsh -> code -> code -> systemd
                                            ^^^^
                                            owns the window

`notify.sh` walks that chain and records the pids. This module matches them
against the pids the window manager reports, nearest ancestor first, and
activates the first window it finds. Nearest-first matters: `claude`'s parent
shell owns no window, its grandparent might, and the one furthest up is
`systemd`, which owns everything and would be a nonsense answer.

Two facts about the environment shape the rest of it:

    property_get is unusable    `Gdk.property_get` cannot be called from
                                PyGObject at all - the out parameter fails to
                                marshal - so reading `_NET_WM_PID` off a foreign
                                window needs something else. Hence two backends:
                                libwnck when its typelib is installed, and a
                                fork of `xprop` when it is not. Neither is
                                assumed; both are probed at import time, in
                                keeping with everything else here.

    focus(0) is the way         `GdkX11.x11_get_server_time()` *hangs* - it waits
                                on a PropertyNotify that never arrives unless the
                                window was selecting for it. Passing CurrentTime
                                works and is not refused by the window manager,
                                so there is exactly one activation path and it is
                                the tested one.

**X11 and XWayland only.** A terminal running as a native Wayland client reports
no pid we can read and cannot be raised by any client. `focus()` says so in its
result rather than returning success and doing nothing - a button that silently
fails is worse than one that admits it, because you stop trusting the ones that
work.

    r = focus(session.pids, project=session.project)
    if not r.ok:
        ...copy the cwd instead, and say why
"""

import os
import subprocess

import gi

gi.require_version("Gdk", "3.0")
from gi.repository import Gdk  # noqa: E402

# Windows nobody means when they say "my terminal": our own creature is a dock,
# and panels and desktops own no session.
SKIP_TYPES = {"dock", "desktop", "menu", "splashscreen", "toolbar"}

# How far up the chain notify.sh is asked to record. Deep enough for
# hook -> shell -> claude -> shell -> terminal with room to spare, shallow
# enough that it always stops before init.
MAX_DEPTH = 10


class Result:
    """What happened, in a form the UI is obliged to look at.

    `ok` alone would be enough to draw a button, but not enough to tell you why
    the button did nothing, which is the whole point of degrading honestly.
    """

    __slots__ = ("ok", "reason", "name", "xid", "hint")

    def __init__(self, ok, reason, name="", xid=0, hint=""):
        self.ok = ok
        self.reason = reason      # short, and meant to be shown to a person
        self.hint = hint          # the longer version, for the log
        self.name = name          # the window's title, when we found one
        self.xid = xid

    def __repr__(self):
        return f"<Result {'ok' if self.ok else 'no'} {self.reason!r} {self.name!r}>"


# --------------------------------------------------------------------- backends
# Both answer one question - what pid owns this window - and neither is assumed
# to exist. `_backend()` is resolved once and cached, because the answer cannot
# change while the daemon is running.
_backend = None


def _wnck_pids():
    """xid -> pid, via libwnck. No forks, and it knows the window type."""
    import gi as _gi
    _gi.require_version("Wnck", "3.0")
    from gi.repository import Wnck

    screen = Wnck.Screen.get_default()
    if screen is None:
        return None
    screen.force_update()
    out = {}
    for w in screen.get_windows():
        try:
            if w.get_window_type().value_nick in SKIP_TYPES:
                continue
            out[w.get_xid()] = (w.get_pid(), w.get_name() or "")
        except Exception:
            continue
    return out


def _xprop_pids():
    """xid -> pid, by forking `xprop` once per window.

    Slower and uglier, but it needs nothing beyond `x11-utils`, which is already
    implied by running on X11 at all. Only ever called on a click, where a
    handful of forks costs less than the frame we are about to draw.
    """
    out = {}
    for w in _stack():
        xid = w.get_xid()
        try:
            raw = subprocess.run(
                ["xprop", "-id", str(xid), "_NET_WM_PID", "WM_NAME"],
                capture_output=True, text=True, timeout=2).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        pid, name = 0, ""
        for line in raw.splitlines():
            if line.startswith("_NET_WM_PID") and "=" in line:
                try:
                    pid = int(line.split("=", 1)[1].strip())
                except ValueError:
                    pass
            elif line.startswith("WM_NAME") and "=" in line:
                name = line.split("=", 1)[1].strip().strip('"')
        if pid:
            out[xid] = (pid, name)
    return out


def _backend_fn():
    """Pick a backend once. `None` means this machine can do neither."""
    global _backend
    if _backend is not None:
        return _backend
    try:
        import gi as _gi
        _gi.require_version("Wnck", "3.0")
        from gi.repository import Wnck  # noqa: F401
        _backend = _wnck_pids
        return _backend
    except (ImportError, ValueError):
        pass
    try:
        ok = subprocess.run(["xprop", "-version"], capture_output=True,
                            timeout=2).returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    _backend = _xprop_pids if ok else False
    return _backend


# ------------------------------------------------------------------- the lookup
def _stack():
    """Every managed toplevel, bottom of the stack first."""
    screen = Gdk.Screen.get_default()
    if screen is None:
        return []
    return list(screen.get_window_stack() or [])


def ancestry(pid=None, depth=MAX_DEPTH):
    """The pid chain above `pid`, nearest first, stopping before init.

    The same walk `notify.sh` does, in Python, so the tests can check the two
    against each other rather than trusting that the bash version is right.
    """
    p = int(pid if pid is not None else os.getpid())
    chain = []
    for _ in range(depth):
        if p <= 1:
            break
        chain.append(p)
        try:
            with open(f"/proc/{p}/stat") as fh:
                stat = fh.read()
        except OSError:
            break
        # The comm field is parenthesised and may itself contain spaces and
        # brackets, so split after the *last* ')' and never before it.
        tail = stat[stat.rfind(")") + 2:].split()
        if len(tail) < 2:
            break
        try:
            p = int(tail[1])
        except ValueError:
            break
    return chain


def find(pids, project=""):
    """The window to raise for this session, or `None`.

    Ranked, because one pid can own several windows and picking wrong is worse
    than picking none: an editor with two projects open reports the same pid for
    both, and raising the other one moves you further from what you asked for.

        1. nearest ancestor wins        the terminal, not its grandparent
        2. then a title naming the      "claude-code-buddy - Visual Studio Code"
           project                      over "index.ts - other-project"
        3. then whichever is highest    the one you used last
           in the stacking order
    """
    fn = _backend_fn()
    if not fn:
        return None
    try:
        owners = fn()
    except Exception:
        return None
    if not owners:
        return None

    mine = os.getpid()
    stack = _stack()
    order = {w.get_xid(): i for i, w in enumerate(stack)}
    by_xid = {w.get_xid(): w for w in stack}
    project = (project or "").lower()

    for rank, pid in enumerate(pids):
        hits = [(xid, meta) for xid, meta in owners.items()
                if meta[0] == pid and meta[0] != mine and xid in by_xid]
        if not hits:
            continue
        hits.sort(key=lambda h: (
            0 if project and project in h[1][1].lower() else 1,
            -order.get(h[0], 0),
        ))
        xid, (_, name) = hits[0]
        return by_xid[xid], name
    return None


# Names that are never the thing owning a window: the shell the hook ran in,
# the runtime above it, and the session plumbing at the top of the chain.
_NOT_A_TERMINAL = {
    "sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh",
    "node", "claude", "python3", "python", "env", "su", "sudo", "login",
    "systemd", "init", "tmux", "screen",
}


def _likely_terminal(pids):
    """The nearest ancestor that looks like an application rather than a shell.

    Used only to explain a failure. When no window matched, the useful thing to
    say is *which* program is holding the session, because on Wayland the answer
    is nearly always that it is a Wayland-native terminal.
    """
    for pid in pids:
        try:
            with open(f"/proc/{pid}/comm") as handle:
                name = handle.read().strip()
        except OSError:
            continue
        if name and name not in _NOT_A_TERMINAL:
            return name
    return ""


def focus(pids, project=""):
    """Raise this session's terminal. Never raises; always explains itself."""
    if not pids:
        return Result(False, "no window recorded for this session")
    if not _backend_fn():
        return Result(False, "needs libwnck or xprop to find windows")

    hit = find(pids, project)
    if hit is None:
        # Name the program instead of shrugging. "no window found" is true and
        # useless; "ghostty is a Wayland window" tells you both why it failed
        # and that it will never succeed until that changes.
        term = _likely_terminal(pids)
        if term:
            return Result(
                False, f"{term} is a Wayland window - can't raise it",
                hint=(f"{term} has no X11 window, and Wayland does not let one "
                      f"client raise another's. Start it with GDK_BACKEND=x11 "
                      f"(it runs on XWayland and gains a raisable window), or "
                      f"use an X11 terminal."))
        return Result(False, "no window found - Wayland terminal, or it closed")

    window, name = hit
    try:
        # CurrentTime, deliberately: see the note at the top of the file about
        # x11_get_server_time hanging.
        window.focus(0)
        display = Gdk.Display.get_default()
        if display is not None:
            display.flush()
    except Exception as exc:
        return Result(False, f"could not raise it: {exc}", name)
    return Result(True, "focused", name, window.get_xid())
