#!/bin/bash
# Take the buddy off this machine.
#
#   ./uninstall.sh             stop him, unwire the hooks, clear live state
#   ./uninstall.sh --purge     the above, plus his remembered position, prefs,
#                              logs and the baked art
#   ./uninstall.sh --dry-run   print what would happen and change nothing
#
# The folder itself is never deleted - `rm -rf` on a directory the script is
# running from is not something a script should decide for you, and the last
# line says so.
#
# Safe to re-run, and safe to run on a machine that was never installed: every
# step is "remove it if it is there".

set -u
D="$(cd "${0%/*}" && pwd)"
SETTINGS="$HOME/.claude/settings.json"
ROOT="${DECK_GUY_HOME:-$HOME/.deck-guy}"

purge=0
dry=0
for arg in "$@"; do
    case "$arg" in
        --purge)   purge=1 ;;
        --dry-run) dry=1 ;;
        -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
        *) printf 'unknown option: %s\n' "$arg" >&2; exit 2 ;;
    esac
done

say() { printf '%s\n' "$*"; }
run() { if [ "$dry" = 1 ]; then printf '  would: %s\n' "$*"; else "$@"; fi; }
# Summary lines are written in the past tense, so a dry run must not print them:
# the `would:` line above each one has already said it, and saying both makes the
# rehearsal read like the real thing.
done_say() { [ "$dry" = 1 ] || say "$@"; }

[ "$dry" = 1 ] && say "dry run - nothing will be changed"

# ---- 1. stop him -----------------------------------------------------------
# By pid, and wait for him to go. Deleting daemon.pid while he is alive bypasses
# the single-instance guard, so a later reinstall would draw a second creature
# on top of the first.
stopped=0
if [ -r "$D/daemon.pid" ]; then
    read -r pid < "$D/daemon.pid"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        if [ "$dry" = 1 ]; then
            say "  would: kill $pid and wait for it to exit"
        else
            kill "$pid" 2>/dev/null
            for _ in 1 2 3 4 5 6 7 8 9 10; do
                kill -0 "$pid" 2>/dev/null || break
                sleep 0.2
            done
            kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
        fi
        stopped=1
    fi
fi
# A copy started before the pidfile convention, or one whose pidfile was deleted
# by hand, is still his and still drawing. `guy\.py` is the pattern that matches
# both an absolute and a relative launch.
if pgrep -f "$D/guy.py" >/dev/null 2>&1 || pgrep -f 'python3 \./guy\.py' >/dev/null 2>&1; then
    run pkill -f "$D/guy.py"
    run pkill -f 'python3 \./guy\.py'
    stopped=1
fi
[ "$stopped" = 1 ] && done_say "stopped   the daemon" || say "stopped   nothing was running"

# ---- 2. unwire the hooks ---------------------------------------------------
# Same rule install.sh uses to recognise its own entries: the command runs a
# notify.sh, wherever it was installed from. Matching on the folder name breaks
# the moment the folder is renamed, and a rename that leaves ten dead hooks
# behind firing into nothing is exactly what this script exists to prevent.
if [ -f "$SETTINGS" ]; then
    # Only back up when there is actually something to remove. Re-running the
    # script otherwise overwrites the one useful backup with a copy of the
    # already-cleaned file, which is the same as not having taken one.
    if [ "$dry" = 0 ] && grep -q '/notify\.sh' "$SETTINGS"; then
        cp "$SETTINGS" "$SETTINGS.bak-deckguy-uninstall"
    fi
    D="$D" SETTINGS="$SETTINGS" DRY="$dry" python3 - <<'EOF'
import json, os, pathlib, sys
S = pathlib.Path(os.environ["SETTINGS"])
dry = os.environ["DRY"] == "1"
try:
    cfg = json.loads(S.read_text()) if S.read_text().strip() else {}
except ValueError:
    print("hooks     settings.json is not valid JSON - left alone", file=sys.stderr)
    sys.exit(0)

hooks = cfg.get("hooks", {})

def ours(group):
    for h in group.get("hooks", []):
        cmd = h.get("command", "").split()
        if cmd and cmd[0].endswith("/notify.sh"):
            return True
    return False

removed = 0
for event in list(hooks):
    groups = hooks[event]
    keep = [g for g in groups if not ours(g)]
    removed += len(groups) - len(keep)
    if keep:
        hooks[event] = keep
    else:
        del hooks[event]          # an event nobody else is using
if not hooks:
    cfg.pop("hooks", None)        # do not leave an empty "hooks": {} behind

if removed == 0:
    print("hooks     none of his were wired")
elif dry:
    print(f"  would: remove {removed} hook entries from {S}")
else:
    S.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"hooks     removed {removed} entries, everything else kept "
          f"(backup: {S}.bak-deckguy-uninstall)")
EOF
else
    say "hooks     no settings.json"
fi

# ---- 3. live state ---------------------------------------------------------
# Everything under here is rebuilt on the next event, so it is only ever a
# cache. The `off` marker in particular must go: it is what a reinstall would
# otherwise read as "you chose Quit", leaving the fresh install refusing to
# start him.
if [ -e "$ROOT" ]; then
    run rm -rf "$ROOT"
    done_say "state     removed $ROOT"
else
    say "state     nothing at $ROOT"
fi
run rm -f "$D/daemon.pid" "$D/state.json" "$D/prompt_ts" "$D/guy.crash.log"

# ---- 4. --purge only -------------------------------------------------------
# His position and his three switches are the only things here a person would
# miss, so they survive a plain uninstall: reinstalling puts him back where he
# was, with the sound still off. The baked art is regenerated by install.sh in
# under a second, and only goes because leaving generated files in a folder you
# have finished with is untidy.
if [ "$purge" = 1 ]; then
    run rm -f "$D/pos.json" "$D/prefs.json" "$D/guy.log" \
              "$D/sprites.png" "$D/sprites.json" \
              "$D/vector_paths.py" "$D/vector_prop_paths.py"
    done_say "purged    position, prefs, log and baked art"
else
    done_say "kept      pos.json, prefs.json, guy.log, sprites.* (--purge removes them)"
fi

say ""
say "Restart Claude Code so it stops loading the hooks."
say "The folder is still at $D - delete it yourself when you are done with it."
