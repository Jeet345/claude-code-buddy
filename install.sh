#!/bin/bash
# Install the buddy on this machine.
#
#   ./install.sh            check deps, bake sprites, wire hooks, start him
#   ./install.sh --check    check deps only, change nothing
#
# Safe to re-run. The hook merge keeps every other key in settings.json and
# replaces only his own hook entries, so it is idempotent.

set -u
D="$(cd "${0%/*}" && pwd)"
SETTINGS="$HOME/.claude/settings.json"
PY="env -u LD_LIBRARY_PATH -u GTK_PATH -u GIO_MODULE_DIR python3"

say() { printf '%s\n' "$*"; }
die() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

# ---- 1. dependencies -------------------------------------------------------
# This exercises the exact call that fails when python3-gi-cairo is missing,
# rather than just importing cairo, which succeeds either way.
#
# Pillow and GdkPixbuf used to be required here. Neither is any more: the art is
# vector and the daemon draws it with cairo, so nothing loads a PNG. Demanding
# them would fail an install on a machine that has everything it actually needs.
$PY - <<'EOF' || die "dependencies missing - see README.md"
import sys
try:
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gtk, Gdk
    import cairo
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 4, 4)
    cairo.Context(surf)
    Gdk.cairo_region_create_from_surface(surf)   # needs python3-gi-cairo
except Exception as e:
    print(f"  {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(1)
print(f"deps OK   gtk {Gtk.get_major_version()}.{Gtk.get_minor_version()}"
      f"   pygobject {gi.__version__}   python {sys.version.split()[0]}")
EOF

# The check above proves the libraries are there; it does not prove *this code*
# imports against the versions of them on this box. It did not, once: PyGObject
# 3.48 carries the GLibUnix typelib without the signal_add that 3.50 added, and
# the daemon died at import with its traceback going to /dev/null. Importing the
# real module needs no display and catches that class of failure here, loudly,
# instead of at 3am as "he just never appeared".
D="$D" $PY - <<'EOF' || die "guy.py does not import on this machine - see the traceback above"
import os, sys
sys.path.insert(0, os.environ["D"])
import guy  # noqa: F401
print("import OK  guy.py and its modules load against these versions")
EOF

[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] || say "warn: no DISPLAY - he cannot draw over ssh"
if [ "${XDG_SESSION_TYPE:-}" = "wayland" ] && ! command -v Xwayland >/dev/null 2>&1; then
    say "warn: Wayland session and no Xwayland found - install it or he will not position"
fi

[ "${1:-}" = "--check" ] && exit 0

# ---- 2. stop any running copy, clear another machine's leftovers -----------
# Kill by pid first. Deleting daemon.pid while he is alive bypasses the
# single-instance guard and you end up with two creatures drawn on top of
# each other.
if [ -r "$D/daemon.pid" ]; then
    read -r oldpid < "$D/daemon.pid"
    if [ -n "$oldpid" ] && kill -0 "$oldpid" 2>/dev/null; then
        kill "$oldpid" 2>/dev/null
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            kill -0 "$oldpid" 2>/dev/null || break
            sleep 0.2
        done
    fi
fi
# pos.json is kept on purpose: it is only his x position, and the screen fence
# clamps a position that came from a wider monitor. state.json and prompt_ts are
# from before the session model and are removed if an old copy carried them in.
rm -f "$D/daemon.pid" "$D/state.json" "$D/prompt_ts"
rm -rf "${DECK_GUY_HOME:-$HOME/.deck-guy}/sessions" "${DECK_GUY_HOME:-$HOME/.deck-guy}/steps"

# ---- 3. compile the vector art ---------------------------------------------
# The body and props are drawn from paths now, so there is no atlas to bake.
# This compiles the two SVG sources into the path tables the daemon imports.
$PY "$D/build_vector.py" "$D/vector_art.svg" "$D/vector_paths.py" >/dev/null \
  || die "vector body build failed"
$PY "$D/build_vector.py" "$D/vector_props.svg" "$D/vector_prop_paths.py" >/dev/null \
  || die "vector prop build failed"
say "art OK    vector_paths.py + vector_prop_paths.py"

# ---- 4. wire the hooks -----------------------------------------------------
mkdir -p "$HOME/.claude"
[ -f "$SETTINGS" ] && cp "$SETTINGS" "$SETTINGS.bak-deckguy"
D="$D" SETTINGS="$SETTINGS" python3 - <<'EOF' || die "could not write settings.json"
import json, os, pathlib
D, S = os.environ["D"], pathlib.Path(os.environ["SETTINGS"])
cfg = json.loads(S.read_text()) if S.exists() and S.read_text().strip() else {}
hooks = cfg.setdefault("hooks", {})
# Every tool-scoped event gets the "*" matcher; Notification matches on
# notification_type and StopFailure on the error string, so both are left
# unmatched, which means "all of them".
want = {"SessionStart": "--ensure idle", "UserPromptSubmit": "--ensure prompt",
        "PreToolUse": "working", "PostToolUse": "working",
        "Stop": "jump", "SessionEnd": "idle",
        # phase 3 - attention
        "Notification": "alert", "PostToolUseFailure": "alert",
        "PermissionDenied": "working", "StopFailure": "error"}
SCOPED = {"PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionDenied"}

def ours(group):
    """Any entry that runs a notify.sh, wherever it was installed from.

    Matching on the folder name was wrong the moment the folder was renamed:
    a re-run then appended a second copy of every hook instead of replacing
    the first, and both fired on every tool call.
    """
    for h in group.get("hooks", []):
        cmd = h.get("command", "").split()
        if cmd and cmd[0].endswith("/notify.sh"):
            return True
    return False

for event, arg in want.items():
    groups = hooks.setdefault(event, [])
    groups[:] = [g for g in groups if not ours(g)]      # leave everyone else alone
    g = {"hooks": [{"type": "command", "command": f"{D}/notify.sh {arg}"}]}
    if event in SCOPED:
        g["matcher"] = "*"
    groups.append(g)
for event in [e for e, groups in hooks.items() if not groups]:
    del hooks[event]                                   # an event we no longer use
S.write_text(json.dumps(cfg, indent=2) + "\n")
print(f"hooks OK  {len(want)} events -> {D}/notify.sh")
EOF

# ---- 5. start him ----------------------------------------------------------
chmod +x "$D/notify.sh"
"$D/notify.sh" --ensure idle
sleep 1
if [ -r "$D/daemon.pid" ] && kill -0 "$(cat "$D/daemon.pid")" 2>/dev/null; then
    say "running   pid $(cat "$D/daemon.pid")   log: $D/guy.log"
else
    say "warn: daemon did not start - read $D/guy.crash.log, or run '$PY $D/guy.py --no-log'"
fi
say "Restart Claude Code to load the hooks."
