#!/bin/bash
# Tell the deck creature what Claude Code is doing.
#
#   notify.sh [--ensure] MODE
#
#   prompt    a prompt was submitted; stamps the task start time
#   working   still busy (refresh, keeps him awake during long tool runs)
#   jump      task finished; hop count scales with elapsed time
#   idle      nothing happening
#   alert     the session is blocked on you, or a tool failed  (phase 3)
#   error     the session itself failed                        (phase 3)
#
# The hook payload on stdin wins over MODE when it carries `hook_event_name`,
# which is how SessionStart and SessionEnd are told apart - both are wired to
# `idle` in settings.json and only one of them ends a session.
#
# `alert` is not a posture. It says "something wants your attention" and carries
# no information about what the session was doing, because the Notification hook
# does not carry any: no tool_name, no timings. The daemon keeps the previous
# state underneath it and clears the alert on the next event of any other kind,
# which is what makes an answered prompt stop shouting immediately.
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

# Same idea for a JSON boolean, which `grab` cannot see: it only matches quoted
# values, and `is_interrupt` is a bare true/false.
grab_bool() {
    local k="$1" v=""
    [[ $payload =~ \"$k\"[[:space:]]*:[[:space:]]*(true|false) ]] && v="${BASH_REMATCH[1]}"
    printf -v "g_$k" '%s' "$v"
}

KEYS=(session_id hook_event_name cwd transcript_path tool_name
      permission_mode file_path command pattern url description
      notification_type message error source)
for key in "${KEYS[@]}"; do grab "$key"; done
grab_bool is_interrupt

# Second bite, only if the first told us nothing useful about the target: a tool
# ran but none of its fields were in the first KB. A failure carries `error`
# behind the whole of `tool_input`, so a Write that blew up needs the second bite
# to say why - and a failure is rare enough to pay for it.
more_needed=0
[ -n "$g_tool_name" ] && [ -z "$g_file_path$g_command$g_pattern$g_url$g_description" ] &&
    more_needed=1
[ "$g_hook_event_name" = PostToolUseFailure ] && [ -z "$g_error" ] && more_needed=1
# `notification_type` sits behind `message`, and an MCP server can ask you a
# question a thousand characters long. Without this the type falls off the end
# of the first kilobyte, the alert field comes out empty, and the badge for the
# thing actually blocking you never appears at all.
[ "$g_hook_event_name" = Notification ] && [ -z "$g_notification_type" ] && more_needed=1
if [ "$more_needed" = 1 ]; then
    IFS= read -r -N 7168 -t 0.2 more 2>/dev/null
    if [ -n "$more" ]; then
        payload+="$more"
        for key in "${KEYS[@]}"; do grab "$key"; done
        # `is_interrupt` sits directly behind `error`, so if the first bite was
        # too short for one it was too short for both. Forgetting this one is
        # not a missing field, it is a wrong one: Esc during a big Write looked
        # exactly like that Write crashing, and lit a red badge for it.
        grab_bool is_interrupt
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
    # Phase 3. PermissionDenied is here for one reason: denying is you answering,
    # so it has to clear the alert as fast as approving does.
    Notification)     mode=alert   ;;
    PermissionDenied) mode=working ;;
    StopFailure)
        mode=error
        g_notification_type=session_failed
        g_message="${g_error:-the session failed}"
        ;;
    PostToolUseFailure)
        mode=alert
        g_notification_type=tool_failed
        g_message="$g_tool_name failed"
        [ -n "$g_error" ] && g_message="$g_tool_name failed: $g_error"
        # You pressing Esc is not a failure and must never light a badge - it is
        # the one "error" you already know about.
        [ "$g_is_interrupt" = true ] && { mode=working; g_notification_type=""; }
        ;;
esac

# The two alert fields belong to alert events only. `message` is a common enough
# word that a tool_response can carry one, and a stale value here would show up
# on his shoulder as a warning about nothing.
[ -n "$g_notification_type" ] || g_message=""

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
"session_started":%s,"heartbeat":%s,"alert":"%s","alert_msg":"%s",
"input":{"file_path":"%s","command":"%s","pattern":"%s","url":"%s","description":"%s"}}\n' \
    "$sid" "$mode" "$g_hook_event_name" "$g_tool_name" "$g_cwd" \
    "$g_transcript_path" "$g_permission_mode" "$now" "$prompt_ts" \
    "$started" "$now" "$g_notification_type" "$g_message" \
    "$g_file_path" "$g_command" "$g_pattern" "$g_url" "$g_description" \
    > "$tmp" 2>/dev/null && mv -f "$tmp" "$SESS/$sid.json" 2>/dev/null

# ---------------------------------------------------------------------- trace
# "Did my hook actually fire?" is otherwise unanswerable: the session file holds
# only the *last* event, so an event that fires once and is immediately
# overwritten leaves no trace at all - which is exactly the shape of a hook that
# is wired wrong. `check_hooks.py --trace` creates the marker; `[ -e ]` and `>>`
# are both builtins, so the cost when it is off is one stat and nothing else.
if [ -e "$ROOT/trace" ]; then
    printf '%s %s %s %s\n' "$now" "${g_hook_event_name:-(none)}" "$mode" \
        "${g_tool_name:-${g_notification_type:--}}" >> "$ROOT/events.log" 2>/dev/null
fi

# Start the daemon if it isn't up. Skipped over ssh, where there's no display.
#
# This is on every prompt, not only on SessionStart, so a daemon that died at
# 11am is back by your next prompt instead of staying gone until you open a new
# session tomorrow. It costs nothing to ask: `[ -r ]`, `read` and `kill -0` are
# all bash builtins, so the whole check is zero forks.
#
# Which makes "Quit" from his menu a problem - it would undo itself. So Quit
# leaves a marker, and a hook-driven ensure respects it. Running this script by
# hand clears it, because that is a person explicitly asking for him back, and
# a hook always carries a session_id while a person typing does not.
#
# Opening Claude Code again clears it too. Quit means "not for the rest of this
# session", not "never again until you find the CLI incantation" - launching the
# tool afresh is as clear a request as typing the command. SessionStart also
# fires on /clear and on an auto-compact, which are the *same* sitting and must
# not revive him; only startup and resume are a new one.
if [ "$ensure" = 1 ] && { [ -n "$DISPLAY" ] || [ -n "$WAYLAND_DISPLAY" ]; }; then
    if [ -z "$g_session_id" ]; then
        rm -f "$ROOT/off" 2>/dev/null      # started by hand: he is wanted again
    elif [ "$g_hook_event_name" = SessionStart ] &&
         { [ "$g_source" = startup ] || [ "$g_source" = resume ]; }; then
        rm -f "$ROOT/off" 2>/dev/null      # new sitting: quit does not carry over
    fi
    alive=0
    [ -e "$ROOT/off" ] && alive=1          # quit on purpose; leave him quit
    if [ "$alive" = 0 ] && [ -r "$PIDFILE" ]; then
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
        # Output goes to guy.crash.log, not /dev/null: guy.py redirects its own
        # output to guy.log as its first act, so anything that lands here is a
        # failure from *before* that - an import error on a PyGObject this code
        # has not met, a missing typelib - which otherwise dies silently and is
        # indistinguishable from "nothing has happened yet". The file is
        # truncated, not appended, so it always describes the latest attempt.
        nohup setsid --fork env -u LD_LIBRARY_PATH -u GTK_PATH -u GIO_MODULE_DIR \
            GDK_BACKEND=x11 python3 "$D/guy.py" >"$D/guy.crash.log" 2>&1 </dev/null &
        disown 2>/dev/null
    fi
fi

exit 0
