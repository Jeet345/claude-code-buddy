# Phase 3 — He tells you when you are the blocker

**Status:** **done** (2026-08-01) — built, tested and running.
**Depends on:** phase 1. Phase 2 is not required.
**Delivers:** the highest-value feature in the whole roadmap
**Data needed:** the `Notification` hook, plus the staleness rules already built.

---

## Why this matters more than it looks

Every other feature here saves you a glance. This one saves you twenty minutes. The failure
it fixes is specific and common: **Claude sits waiting on a permission prompt while you are
in another window, and nobody tells you.** The session is not slow, it is stopped, and the
only signal today is you happening to look.

That is why it comes before multi-session and before control. It is also cheap.

---

## What you see

Four escalating states, in order of increasing rudeness:

| Trigger | What he does |
|---|---|
| Claude needs permission or input | **waves** (animation exists), badge over his head |
| Still waiting after 60s | waves bigger, desktop toast via `notify-send` |
| Still waiting after 5 min | toast repeats once, optional sound |
| Tool failed / session crashed | distinct slumped animation, red badge, stays until acknowledged |

Plus one-shot toasts, all off by default except the first:

- run finished (he already jumps; the toast is for when he is not on screen)
- tests failed
- scheduled agent fired

**Sound ships off.** It is the fastest way to turn a charming thing into an annoying one.
One config flag, default false, and a menu entry to enable it.

---

## Where the data comes from

- **`Notification` hook** — fires when Claude Code needs permission or has been idle waiting
  on input. The payload carries a `message`. This is the trigger; verify the exact event
  name and payload against the installed CLI before building, the same way the five current
  hooks were verified.
- **Waiting duration** — the daemon already tracks per-session heartbeat and step start, so
  escalation is a timer, not new data.
- **Errors** — `PostToolUse` results that indicate failure, plus sessions that go stale
  without a `Stop` (the rule already written for the pet).

No new hook cost: `Notification` fires rarely.

### Hook note — verified against Claude Code 2.1.220, 2026-08-01

Read out of the installed binary rather than assumed. `strings` on
`~/.local/share/claude/versions/2.1.220`, grepping for `hook_event_name:"..."`, gives the
full list of events the CLI can fire — **31 of them**, not the five we had wired. Four
matter here.

**`Notification`** is built as `{...base, hook_event_name, message, title,
notification_type}` and the matcher matches on `notification_type`. `base` is
`{session_id, transcript_path, cwd, prompt_id, permission_mode, agent_id, agent_type,
effort}` — and `permission_mode` is passed as `undefined` for this event, so a Notification
tells you nothing about the session's mode. The types it can send:

```
permission_prompt        a tool is waiting on your yes/no
idle_prompt              "Claude is waiting for your input"
elicitation_dialog       an MCP server is asking you something
elicitation_url_dialog
agent_needs_input        a teammate agent is blocked
worker_permission_prompt
agent_completed  auth_success  push_notification
computer_use_enter  computer_use_exit
elicitation_complete  elicitation_response
```

**Two delays are already built into the CLI, and they change the design.** A permission
dialog does not notify immediately: it re-checks every 6 s and fires only once you have not
touched the keyboard for 6 s (`Q3f = 6000`, guarded by `Date.now() - lastActivity >= 6000`).
`idle_prompt` waits 60 s (`messageIdleNotifThresholdMs: 60000`). So a notification arriving
at all is already evidence that you are not looking — the escalation clock in `alerts.py`
starts from a point Claude Code has established, not from the dialog opening. It also means
the honest ceiling on "he waves within 250 ms" is 250 ms *of the notification*, roughly 6 s
of the prompt. Nothing can close that gap without polling.

**`PostToolUseFailure`** — `{tool_name, tool_input, tool_use_id, error, is_interrupt,
duration_ms}`. Better than reading `PostToolUse` results for failure, and `is_interrupt`
separates "it broke" from "you pressed Esc", which is the difference between a badge worth
lighting and one that would fire every time you interrupt.

**`StopFailure`** — `{error, error_details, last_assistant_message}`. Session-level failure,
which is a different thing from a tool that returned non-zero.

**`PermissionRequest`** exists and fires the moment permission is checked, with no 6 s wait —
and it is the wrong hook. It runs *inside* the permission decision, can return allow/deny,
and therefore blocks the session on us. It also fires for calls that are auto-allowed and
never prompt at all. Phase 3 is read-only; the whole point of `Notification` is that it
only fires when a human is actually being waited on.

**There is no "the prompt was answered" event.** Clearing has to come from the next thing
that happens, which is why `PostToolUse` is now wired: after you approve, the tool runs, and
its completion is the first evidence you answered. `PermissionDenied` covers the other
answer.

---

## Design

- **New art**: an `alert` badge prop (`!` in a bubble) and an `error` posture — a slumped
  version of the sleep pose with a distinct colour accent. Both go through the existing
  ASCII-grid pipeline and cost a prop grid plus anchors.
- **`alerts.py`** — new module. Owns escalation timers, dedupe (one alert per session, not
  one per event) and acknowledgement. Nothing about drawing.
- **Toasts** — `notify-send` via `subprocess`, never blocking, failure ignored. Sound the
  same way, but **not** via `paplay`: see the player note below.
- **Acknowledgement** — clicking him clears the badge. That is what the click already does
  (wave), so the badge just intercepts it first.

### Escalation must be interruptible

If the user answers the prompt in the terminal, the next hook event clears the alert
immediately. An alarm that keeps ringing after you have dealt with it is worse than no
alarm — this is the single most important behaviour in the phase.

---

## Tasks

1. Verify the `Notification` hook name and payload against the installed CLI.
2. `alert` badge and `error` posture art; rebuild the sheet.
3. `alerts.py`: state machine, escalation timers, dedupe, acknowledgement.
4. Wire `Notification` in `notify.sh` and `settings.json`.
5. `notify-send` toasts; sound behind a default-off flag.
6. Error state from failed `PostToolUse` and from stale-without-`Stop`.
7. Click-to-acknowledge; menu entry to mute for an hour.  *(built, then removed - see below)*

---

## Acceptance criteria

- A real permission prompt makes him wave within 250ms of the notification.
- Answering the prompt in the terminal clears the badge within 250ms, with no toast if it
  was answered before the 60s threshold.
- Ten notifications in a row produce one badge, not ten.
- With sound disabled (default) nothing audible ever happens.
- `notify-send` missing from the system degrades to the badge with no error.
- A failed Bash command shows the error posture, and the next successful step clears it.

---

## Risks

- **Alarm fatigue.** Every threshold in the table is a guess; make them config, and be
  willing to raise them after living with it for a week.
- **Notification hook coverage.** If it does not fire for every permission case on the
  installed version, fall back to the weaker signal: a session that is `working` with no new
  event for N seconds is *probably* waiting on you. Weaker, but better than silence.
- **Toast spam across sessions** once phase 4 lands — dedupe is per session, so cap the
  total rate too.

---

## Out of scope

Answering the permission prompt from the buddy — that is phase 5, and it is a different
kind of risk entirely. Phase 3 only ever *tells* you.

---

## What actually got built

One new file — `alerts.py` — plus `test_alerts.py` (110 checks), an `alert` prop and an
`error` posture in `sprites.py`, two fields on the session file, and five more hook events
wired. Five things came out differently from the plan above.

**An alert is not a posture, and that turned out to be the load-bearing decision.**
The `Notification` payload carries no `tool_name` and no timings, so taking it at face
value as a state would blank the very step that is blocked on you and restart its clock —
the bubble would go from `Bash pytest -q 2m 14s` to nothing at the exact moment you most
want to know what is stuck. So `Session.inherit` keeps the previous state, tool, input and
timestamps whenever the incoming state is `alert`, and the alert rides on top as two extra
fields. Everything downstream follows from that: the badge is drawn over whatever costume
he already had on, and a permission prompt during a Bash call still *looks* like a Bash
call that is stuck, which is what it is.

**Clearing is the absence of a field, not an event.** `notify.sh` writes `alert` on an
attention event and leaves it empty on every other one. There is no timeout, no second
signal, and nothing to get out of sync: the next hook of any kind is the clear. This is
also why `PostToolUse` and `PermissionDenied` are now wired — approving and denying are the
two ways you answer, and each needs an event to land within milliseconds of it.

**A failed tool gets a badge; only a failed session gets the posture.** The doc asked for
the slumped animation on "non-zero `PostToolUse` results", and its own acceptance criteria
asked for the next successful step to clear it. Those two together would have him slumping
for half a second every time a `grep` found nothing, which is several times a minute and
means nothing. So `PostToolUseFailure` raises a red badge and never toasts, and the `error`
posture is reserved for `StopFailure` and for a working session that stops answering
altogether. `is_interrupt` is dropped on the floor: pressing Esc is the one failure you
already know about.

**The escalation ladder is shorter than specified, because Claude Code already climbs the
first rung.** The CLI waits 6 s of you not touching the keyboard before it sends a
permission notification at all, and 60 s before an idle one. So the clock here starts from a
point where you are provably not looking, and the third rung is a single repeat at 5 minutes
after which it stops — an alarm that never stops is one you learn to ignore, and by then it
has taken the badge's credibility with it.

The first rung shipped at the spec's 60 s and **that was wrong, which only showed up in
use**: every prompt got answered or acknowledged inside a minute, so the toast never fired
once in a real day. An alarm that is only theoretically an alarm is worse than none,
because you believe you have one. It is 10 s now, and the sound goes with that first toast
rather than being held back for the 5-minute repeat — holding it back meant the one signal
that reaches you when the screen is covered was the one you were least likely to ever get.
Both thresholds are `Alerts(first=, repeat=)`, read from `prefs.json` as `alert_secs` and
`alert_repeat_secs`.

**Mute for an hour was built to spec and then deleted.** The spec asked for it, and it
worked. It came out because the toast and the sound ended up as two independent switches
(see bug seven below), and *both off* is already mute: it says so on the label, you can see
its state without opening anything, and it survives a restart. A timed mute was a third
route into a state two switches already covered, and the only thing it uniquely added was a
way to be silenced without remembering you had done it. The menu is shorter and the state
space is smaller, which for an alarm is the whole point.

### Four bugs the first test pass missed

Found by going back over every new surface deliberately, after the suite was green. All
three are the same shape: a state you can only reach by doing something slightly unusual,
and then cannot get out of.

**`is_interrupt` fell off the end of the bounded read.** `notify.sh` reads 1 KB of the hook
payload and only goes back for more if that found nothing useful. The second bite re-ran
`grab` for every string field but not `grab_bool`, and `is_interrupt` sits directly behind
`error` in a `PostToolUseFailure` — so both were past the first kilobyte or neither was.
Pressing Esc during a large `Write` therefore looked exactly like that `Write` crashing and
lit a red badge for it. Not a missing number, a wrong one.

**A typo in `prefs.json` took the daemon down.** `alert_secs` is documented as hand-editable
and went straight into `int()`, so `"sixty"` was an uncaught `ValueError` at startup and he
simply never appeared. The project's own rule is *fail quiet, never fail loud*; both
thresholds now fall back to the default and the flags go through `bool()`.

**Clicking him during the wave left him slumped forever.** A failure makes him wave *and*
slump, and the wave is a cue — so `error` sits parked in `_cue` for a few frames waiting for
the wave to finish. Acknowledging inside that window cleared the badge and then dropped him
straight back into the slump with nothing left to clear it. `Creature.leave_error()` now
clears the queued mode as well as the current one.

That last one uncovered a **fourth, older bug underneath it**: `cue()` set `_cue = self.mode`
unconditionally, so a cue raised *during* a cue parked the string `"cue"` as the mode to
return to. `_enter("cue")` is a mode nothing drives — he kept the idle frames but stopped
blinking, stopped patrolling, and never fell asleep again. **Two clicks in a row was enough
to trigger it**, and it has been there since phase 0. It survived this long because any
session event calls `set_mode` and knocks him out of it, so on a machine with Claude Code
running it self-heals within seconds.

### The fifth bug: the sound never worked, and said it did

Found by the only method that could have found it — being asked "why am I not getting a
sound". The player was picked as `shutil.which("paplay") or shutil.which("aplay")` and
handed the freedesktop theme's `message.oga`. This machine is PipeWire and has no `paplay`,
so it fell through to `aplay` — which is **ALSA's WAV player**. Given an Ogg it does not
refuse:

```
$ aplay /usr/share/sounds/freedesktop/stereo/message.oga
Playing raw data '.../message.oga' : Unsigned 8 bit, Rate 8000 Hz, Mono
```

It reads the compressed file as raw 8-bit 8 kHz PCM, plays the bytes as noise, and exits 0.
Every layer above it was satisfied: `which` found a binary, `Popen` succeeded, the return
code was fine. Nothing anywhere had to be wrong for the feature to be completely absent.

The order is `canberra-gtk-play -i message` (plays a theme *id*, needs no path, and is the
correct thing on a GNOME desktop), then `pw-play`, then `paplay`, and `aplay` last and only
ever with a WAV. `python3 alerts.py` now prints the resolved command and sends one of each,
which is the diagnostic that should have existed from the start:

```
notify-send  /usr/bin/notify-send
sound        /usr/bin/canberra-gtk-play -i message
thresholds   first toast 10s unanswered, one repeat at 300s; the sound goes with both
```

The lesson is the phase 2 one again, from the other direction. Phase 2's trap was a
*plausible wrong number*; this is a **plausible success** — an exit code of 0 from a tool
that did not do the thing. Checking that a binary exists is not checking that it can read
the file you are about to give it.

### The sixth: the threshold that was configured in two places

Lowering `FIRST_TOAST` to 10 changed the tests, the `alerts.py` self-check and every
document. It did not change the daemon, which was still toasting at 60 s, because `guy.py`
had written its own copy of the default at the call site:

```python
first=prefs.get("alert_secs", 60)        # <- the 60 that actually won
```

A default repeated at the call site is not a default, it is a second source of truth that
agrees with the first exactly until someone edits one of them. `Alerts` falls back on its
own constants when handed `None`, so the call site now passes `prefs.get("alert_secs")` and
nothing else. A test asserts `guy.py` does not carry a copy.

Two log lines exist now because none of this was visible from outside the process — the
badge and the toast are separate channels and either can be the one that failed:

```
alerts: toast=True sound=True first=10s repeat=300s
toast sent + beep sent after 10s: permission_prompt
```

### The seventh: a checkbox that lied

Found by being asked the right question — *why are there two options, is either of them
useful?* Working out the answer meant enumerating all four states, and one of them did
nothing:

| Notifications | Sound | Actually happened |
|---|---|---|
| on | off | toast |
| on | on | toast + beep |
| **off** | **on** | **nothing** |
| off | off | nothing (correct) |

`_speak` opened with `if not self.toast: return`, so the sound was gated behind the toast.
Turning notifications off silently disabled the sound switch while leaving it ticked.

The two flags are two *channels*, not one with a master switch. A toast covers a corner of
your screen, so it is for when you are looking at the screen and looking at something else.
A sound reaches you when the screen is covered or you are not at it — which is the case this
entire phase exists for. Wanting the second without the first is an ordinary preference:
*nothing over my work, just tell me out loud.* Each channel checks its own flag now, and a
test asserts all four combinations, because the answer to "is this option useful" turned out
to be "yes, and it was the one that did not work".

### Three more, from testing it the way a person uses it

`test_interactions.py` drives the real `Deck` the way a hand does - click, drag, right-click,
hover - at every point in the state machine, 341 checks. Every scenario ends on the same two
assertions, and they are the whole design of the suite:

    drivable   a session event can still move him
    sleepy     leave him alone long enough and he still naps

Nothing that checks *"is the right animation playing"* would have caught the stuck-`cue`
bug, because the right animation **was** playing. Checking *"can he still be driven"* does.
Three things fell out of running it:

**A session file with a wrong-typed field crashed the tick.** `{"alert": ["not","a",
"string"]}` reached `set.add((session_id, kind))` and raised `TypeError: unhashable type`
out of the timeout callback. Every field being *optional* was handled everywhere; a field
being the wrong *type* was not, and `Path(cwd)` on a list would have gone the same way.
`Session` now coerces every string field through `_text()`, and `Alerts.update` checks the
type again because it accepts anything session-shaped.

**A long notification message silently lost the alert.** `notification_type` sits behind
`message` in the payload, and `notify.sh` reads the first kilobyte. An MCP server asking a
900-character question pushes the type past that boundary, so the field came out empty and
the badge for the thing actually blocking you never appeared at all. The second bite now
triggers on a `Notification` with no type, the same way it already did for a failure with
no `error`.

**The slump was erased if the dead session got reaped mid-wave.** A failure makes him wave
*and* slump, so for the length of the wave the slump is parked in `_cue` and `mode` reads
`cue`. The guard protecting it asked `mode == "error"`, which is false for exactly the
half-second in which the reap almost always lands - so the only evidence a session had died
vanished with the session. There is a `Creature.slumped` property now, and the two places
that ask "is he showing a failure" both use it.

### Verifying the hooks, and what that turned up

`check_hooks.py` exists because *"is my hook firing"* had no answer. It asks three separate
questions and only the third is hard:

1. **Wired?** settings.json against the `want` table — moved folder, half-finished install,
   the same event wired twice.
2. **Real?** The event names are grepped out of the installed `claude` binary rather than
   trusted. This is the one that rots: hooks are configuration pointing at a moving target,
   and a hook wired to an event the CLI has renamed is *silent*, which is indistinguishable
   from a hook that works and has nothing to say.
3. **Firing?** The session file holds only the last event, so one that fires and is
   overwritten a moment later leaves no evidence. `--trace` drops a marker that makes
   `notify.sh` append a line per call — `[ -e ]` and `>>` are both builtins, so the cost
   when it is off is one stat.

Against the live session: all ten wired, all ten still present in 2.1.220, all eleven
behaviours correct, and the trace showed **11 `PreToolUse` and 11 `PostToolUse`** — the
pairing is the number that matters, because `PostToolUse` is what clears an alert the moment
you approve, so a shortfall there is a badge that stays lit.

Two things came out of writing it:

**`--ensure` was only on `SessionStart`, so a daemon that died stayed dead** until you
opened a *new* Claude Code session — possibly the next day. It runs on `UserPromptSubmit`
too now. The check is `[ -r ]`, `read` and `kill -0`, all bash builtins, so it costs
**0.13 ms** and never forks.

That immediately broke **Quit**, which would have undone itself within a minute. So Quit
leaves `~/.deck-guy/off` and a hook-driven ensure respects it, while running `notify.sh
--ensure idle` **by hand** clears it. The two are told apart by something already in the
payload: a hook always carries a `session_id` and a person typing never does. Arguably this
is how Quit should always have behaved — it used to silently come back on your next session.

`SessionStart` also turns out to carry `source`, `model` and `session_title`, all of which
we discard. Left alone deliberately: `model` arrives from the transcript within a turn
anyway, and `session_title` at *start* is a placeholder — Claude Code generates the real one
later. Worth revisiting only with a captured payload in hand rather than on the strength of
a field name.

### Polish, after living with it

Three rough edges, all found by using it rather than by testing it.

**The alert text said nothing.** Claude Code's permission message is the same generic
sentence whatever is blocked — `"Claude needs your permission"`, with no tool name in it —
so the badge told you *that* you were the blocker and never *what* for. The fix was already
lying around: an alert is not a posture, so `inherit` had kept the step underneath it.
Splicing the two gives `Bash pytest -q - needs your permission`, and it only splices when
the message does not already name the tool, so a future CLI that includes it wins rather
than being duplicated.

**Two blocked sessions made two popups** fighting over the same corner of the screen.
`_escalate` now collects everything that crossed a threshold in the pass and announces it
once: `4 sessions need you`, three named and the rest counted, one beep. A cap across
*time* rather than within a pass is still phase 4's problem.

**The badges were invisible in `preview.png`.** They are the only art the daemon positions
itself — not a costume, not an animation — so nothing on the contact sheet showed them; the
raw-frames dump at the bottom had the prop, but with no body under it and no caption it was
two coloured pills. There is a captioned row now, amber on an idle body and red on a
slumped one, anchored exactly where the daemon puts them.

### The bug that was in the box before we opened it

`install.sh` dropped its own previous hook entries by matching `"deck-guy" in command` —
which stopped matching anything the moment the folder was renamed to `claude-buddy`. Every
re-run since then would have *appended* a second copy of all five hooks instead of
replacing them, and both copies would fire on every tool call. It never showed up because
nobody re-ran the installer after the rename. It matches on a `notify.sh` path now.

`build_sheet.py` had a quieter one in the same family: the preview canvas budgeted height
for `len(anims)` raw-frame rows but drew `len(anims) + len(props)`, so `preview.png` had
been silently truncated partway through the props for some time. The README calls that file
"the reference for what each pose means", so a reference that stops early is worth the
one-line fix.

### Verified against the acceptance criteria

- **A permission prompt makes him wave.** Measured file-to-daemon at **31 ms** against a
  250 ms budget (`Gio.FileMonitor` plus the store's 30 ms coalesce), with one 30 Hz tick on
  top. The honest caveat is above: the CLI itself sits on the notification for ~6 s first,
  and nothing short of polling can change that.
- **Answering clears it, with no toast if you were quick.** Tested at both levels: through
  `notify.sh` end to end (`Notification` then `PostToolUse` → `alert` is `""`), and in the
  state machine (raise at 0 s, clear at 30 s, nothing sent by 90 s).
- **Ten notifications in a row produce one badge.** One entry, and `since` does not move —
  which matters more than the count, because an alarm whose clock restarts on every repeat
  never reaches its own escalation and stays a badge forever.
- **Sound off by default.** No beep on any path unless `sound` is turned on, and even then
  only on the 5-minute repeat, never the first toast.
- **`notify-send` missing degrades to the badge.** Tested with the binary absent and with a
  path that exists in `which` but not on disk; both return `False` and neither raises.
- **A failed tool shows red and the next step clears it.** Plus the `is_interrupt` case,
  which shows nothing at all.
- **Hook cost.** The four new fields cost **+0.17 ms** per call (3.79 → 3.96 ms on a 3 KB
  payload). Wiring `PostToolUse` is the real change: a tool call now runs the script twice
  instead of once, so roughly 8 ms per tool call total. That buys the clear-on-answer
  behaviour, which is the feature.
- **Idle CPU.** No new timer — escalation is checked on the tick that was already running.
  Measured 0.71% over a 228 s window, against phase 2's 0.75%. The badge's pulse does add
  repaints, but only while a badge is showing.
- **Live, against real permission dialogs.** Seven real `permission_prompt` notifications
  are in `guy.log` from the session that built this, plus a real `PostToolUseFailure`
  rendered as `alert tool_failed: Bash failed: Exit code 1 Traceback (most rece…`. Worth
  knowing: the message Claude Code sends for a permission prompt is the bare string
  `"Claude needs your permission"` with **no tool name in it** — the tool is on the `step`
  row directly above, which is why the alert row is placed there and not somewhere tidier.
- **The installer is idempotent against a settings file that is not ours.** Merged twice
  over a config carrying a foreign `PreToolUse` hook: 10 events, no duplicates, the foreign
  hook still there, all 8 top-level keys kept.

### Left for phase 4

Toast rate-limiting across sessions. Dedupe is per session and `current()` picks exactly
one alert to draw, because he has one shoulder. Once every session has its own avatar,
both of those need revisiting — and a global cap on toasts per minute becomes necessary
rather than theoretical.
