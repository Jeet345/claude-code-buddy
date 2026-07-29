#!/bin/bash
# Tell the deck creature what Claude Code is doing.
#
#   notify.sh [--ensure] MODE
#
#   prompt    a prompt was submitted; stamps the task start time
#   working   still busy (refresh, keeps him awake during long tool runs)
#   jump      task finished; hop count scales with elapsed time
#   idle      nothing happening
#
# The hook payload on stdin wins over MODE when it carries `hook_event_name`,
# which is how SessionStart and SessionEnd are told apart - both are wired to
# `idle` in settings.json and only one of them ends a session.
#
# One file per session under ~/.deck-guy/sessions/, so several terminals can
# report at once. This runs on every tool call, so it must be fast and must
# never fail: bash builtins only, no forks except mkdir on the first call and
# the final atomic mv. Always exits 0.

D="${0%/*}"
ROOT="${DECK_GUY_HOME:-$HOME/.deck-guy}"
SESS="$ROOT/sessions"
STEPS="$ROOT/steps"
PIDFILE="$D/daemon.pid"

ensure=0
[ "$1" = "--ensure" ] && { ensure=1; shift; }
mode="${1:-idle}"

now=${EPOCHSECONDS:-0}
[ "$now" = 0 ] && now=$(date +%s)

# ---------------------------------------------------------------- hook payload
# Bounded read: a Write call carries the whole new file contents on stdin and we
# have no business paying for that.
#
# The bound is a cost knob, not a safety one - bash reads a pipe a byte at a
# time, so every KB is about 0.25ms. Hence two bites: 1KB covers session_id,
# transcript_path and cwd (first ~250 bytes) plus file_path for Edit and Write,
# which both put it ahead of the bulk. The second bite only happens when that
# was not enough, which in practice means a tool we have no field for anyway.
payload=""
[ -t 0 ] || IFS= read -r -N 1024 -t 0.2 payload 2>/dev/null

# Values are captured raw, still JSON-escaped, and pasted straight back into the
# file we write - so `"a\"b"` survives without this script understanding escapes.
grab() {
    local k="$1" v=""
    if [[ $payload =~ \"$k\"[[:space:]]*:[[:space:]]*\"(([^\"\\]|\\.)*)\" ]]; then
        v="${BASH_REMATCH[1]:0:300}"
        # Truncation can leave a dangling backslash that would escape our closing
        # quote and corrupt the JSON.
        while [[ $v == *\\ ]]; do v="${v%\\}"; done
    fi
    printf -v "g_$k" '%s' "$v"      # printf -v is a builtin: no subshell
}

KEYS=(session_id hook_event_name cwd transcript_path tool_name
      permission_mode file_path command pattern url description)
for key in "${KEYS[@]}"; do grab "$key"; done

# Second bite, only if the first told us nothing useful about the target: a tool
# ran but none of its fields were in the first KB.
if [ -n "$g_tool_name" ] && [ -z "$g_file_path$g_command$g_pattern$g_url$g_description" ]
then
    IFS= read -r -N 7168 -t 0.2 more 2>/dev/null
    if [ -n "$more" ]; then
        payload+="$more"
        for key in "${KEYS[@]}"; do grab "$key"; done
    fi
fi

# Anything that is not filename-safe goes, so a hostile id cannot escape the dir.
sid="${g_session_id//[^A-Za-z0-9._-]/}"
[ -z "$sid" ] && sid="default"

# ------------------------------------------------------------------- the event
# hook_event_name is authoritative when present; MODE is the fallback for a CLI
# that does not send it, and for hand-driven testing from a terminal.
case "$g_hook_event_name" in
    SessionStart)     mode=start   ;;
    UserPromptSubmit) mode=prompt  ;;
    PreToolUse)       mode=working ;;
    PostToolUse)      mode=working ;;
    Stop)             mode=jump    ;;
    SessionEnd)       mode=ended   ;;
esac

# `--ensure` with no session id is somebody starting the daemon by hand, not a
# session doing anything: start him, but do not invent a session for him to
# report on, and leave no stamps behind either. A real SessionStart carries the id.
write_session=1
[ "$ensure" = 1 ] && [ -z "$g_session_id" ] && write_session=0

# Guard on the steps dir, not the sessions dir: the daemon creates sessions/ for
# its own file monitor, so testing that one would leave steps/ missing and every
# turn timestamp would silently fail to write.
[ "$write_session" = 1 ] && { [ -d "$STEPS" ] || mkdir -p "$SESS" "$STEPS" 2>/dev/null; }

started=$now
prompt_ts=$now
[ "$write_session" = 1 ] &&
case "$mode" in
    start)
        printf '%s' "$now" > "$STEPS/$sid.start" 2>/dev/null
        printf '%s' "$now" > "$STEPS/$sid.prompt" 2>/dev/null
        mode=idle
        ;;
    ended)
        rm -f "$STEPS/$sid.start" "$STEPS/$sid.prompt" 2>/dev/null
        ;;
    prompt)
        printf '%s' "$now" > "$STEPS/$sid.prompt" 2>/dev/null
        mode=working
        ;&
    *)
        # A session that was already running when the hooks were installed never
        # saw its own SessionStart or UserPromptSubmit, so date both from the
        # first event we do see. Without this they read "now" on every event:
        # the session runtime never climbs past zero and every finished task
        # scores as instant, which is a one-hop jump instead of a celebration.
        # UserPromptSubmit overwrites the prompt stamp each turn, as it should.
        [ -e "$STEPS/$sid.start" ]  || printf '%s' "$now" > "$STEPS/$sid.start" 2>/dev/null
        [ -e "$STEPS/$sid.prompt" ] || printf '%s' "$now" > "$STEPS/$sid.prompt" 2>/dev/null
        ;;
esac
[ -r "$STEPS/$sid.start" ]  && read -r started   < "$STEPS/$sid.start"
[ -r "$STEPS/$sid.prompt" ] && read -r prompt_ts < "$STEPS/$sid.prompt"
[ -z "$started" ] && started=$now
[ -z "$prompt_ts" ] && prompt_ts=$now

# ---------------------------------------------------------------- session file
# `input` holds whatever was recognised, unranked. Deciding which field is the
# target, and how to shorten it, is the daemon's job - the hook stays dumb.
tmp="$SESS/$sid.json.$$"
[ "$write_session" = 1 ] &&
printf '{"session_id":"%s","state":"%s","event":"%s","tool":"%s","cwd":"%s",
"transcript_path":"%s","permission_mode":"%s","ts":%s,"prompt_ts":%s,
"session_started":%s,"heartbeat":%s,
"input":{"file_path":"%s","command":"%s","pattern":"%s","url":"%s","description":"%s"}}\n' \
    "$sid" "$mode" "$g_hook_event_name" "$g_tool_name" "$g_cwd" \
    "$g_transcript_path" "$g_permission_mode" "$now" "$prompt_ts" \
    "$started" "$now" \
    "$g_file_path" "$g_command" "$g_pattern" "$g_url" "$g_description" \
    > "$tmp" 2>/dev/null && mv -f "$tmp" "$SESS/$sid.json" 2>/dev/null

# Start the daemon if it isn't up. Skipped over ssh, where there's no display.
if [ "$ensure" = 1 ] && { [ -n "$DISPLAY" ] || [ -n "$WAYLAND_DISPLAY" ]; }; then
    alive=0
    if [ -r "$PIDFILE" ]; then
        read -r pid < "$PIDFILE"
        [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && alive=1
    fi
    if [ "$alive" = 0 ]; then
        # --fork is required: plain setsid is a no-op when the caller is already a
        # process group leader, which leaves the daemon exposed to SIGHUP.
        # The env -u strip works around snap breaking GTK's linking - see PLAN.md.
        # GDK_BACKEND=x11 forces XWayland on Wayland sessions: GTK3's move(),
        # keep_above() and input_shape_combine_region() are all no-ops on the
        # native Wayland backend.
        nohup setsid --fork env -u LD_LIBRARY_PATH -u GTK_PATH -u GIO_MODULE_DIR \
            GDK_BACKEND=x11 python3 "$D/guy.py" >/dev/null 2>&1 </dev/null &
        disown 2>/dev/null
    fi
fi

exit 0
