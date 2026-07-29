# Phase 1 — Session model + speech bubble

**Status:** **done** (2026-07-30) — built, tested and running. See *What actually got
built* at the end for the three places the design changed on contact.
**Depends on:** phase 0 (the pet, done)
**Delivers:** he stops being a mood ring and starts saying what Claude is actually doing
**Data needed:** hooks only. No transcript parsing in this phase.

---

## Why this is first

Two reasons, and the second matters more.

1. It is the thing you look at every minute. "What is it doing right now" beats every other
   readout for daily value.
2. It forces the **session model** refactor. Today there is one `state.json` and the last
   writer wins — correct for a pet, wrong for a HUD, because a HUD shows sessions, plural,
   each with its own history. Every later phase assumes that model exists. Building it under
   the smallest feature that needs it is cheaper than retrofitting it under phase 4.

---

## What you see

A single line above his head while a step is running:

```
      ┌──────────────────────────┐
      │ Edit  src/auth.py   4s   │
      └──────────────▽───────────┘
              (buddy)
```

- **Tool and target**, truncated hard: `Edit src/auth.py`, `Bash pytest -q`, `Grep "TODO"`,
  `WebFetch anthropic.com`. One line. Never a log dump, never wrapped.
- **Elapsed on the current step**, ticking. This is what tells you a Bash call has hung.
- Bubble fades out a few seconds after the step ends, so an idle desktop stays quiet.
- **Right-click → Speech bubble** turns it off for good, remembered in `prefs.json`. The
  hover panel still works when it is off, so the information is one hover away rather than
  gone. Added after living with it: an always-on line above his head is the right default
  and the wrong thing to have no escape from.

On hover or click, a small panel below it:

```
  project   deck-guy            session  4m 12s
  step      Bash pytest -q      elapsed  00:07
```

That is the whole surface for this phase. Tokens, cost and model arrive in phase 2 and
slot into the same panel.

---

## Where the data comes from

Hook payloads only. Each hook receives JSON on stdin carrying at minimum `session_id`,
`cwd`, `transcript_path` and `hook_event_name`; `PreToolUse` adds `tool_name` and
`tool_input`, `PostToolUse` adds the result.

| Hook | What phase 1 takes from it |
|---|---|
| `SessionStart` | create session file; record `cwd`, start time, `transcript_path` |
| `UserPromptSubmit` | step becomes "thinking"; stamp step start |
| `PreToolUse` | step becomes `tool_name` + target extracted from `tool_input`; stamp step start |
| `PostToolUse` | step ends; keep it in the bubble briefly, then fade |
| `Stop` | turn finished → the existing jump, then quiet |
| `SessionEnd` | remove the session file |

**Target extraction** is the only judgement call. Rules, in order: `file_path` if present,
else `command` truncated at the first pipe or newline, else `pattern`, else `url`, else
`description`, else nothing. Keep this in one function with a table — it will need new
entries as tools change, and it must never raise.

The hook writer stays a shell script that writes a file and exits 0. Extraction of the
target happens **in the daemon**, not in the hook — the hook's job is to be fast and dumb.
That means the raw `tool_input` goes into the session file as-is.

---

## Design

### On-disk layout

Moves out of the project folder, because sessions are not project state:

```
~/.deck-guy/
  sessions/<session_id>.json     one per live session, written atomically
```

Session file, everything optional except `session_id` and `heartbeat`:

```json
{
  "session_id": "abc123",
  "cwd": "/home/jeet/Desktop/deck-guy",
  "transcript_path": "/home/jeet/.claude/projects/…/abc123.jsonl",
  "state": "working",
  "tool": "Bash",
  "tool_input": {"command": "pytest -q"},
  "step_started": 1785349014.2,
  "session_started": 1785348800.0,
  "heartbeat": 1785349019.7
}
```

`heartbeat` is written on every event and is what the reaper uses. It replaces the
`STALE_SECS` timeout the pet already needed — same rule, now per session.

### Daemon changes

- **`sessions.py`** — new module, no GTK. Watches `~/.deck-guy/sessions/` with a
  `Gio.FileMonitor` instead of the current 250ms poll, parses session files, reaps stale
  ones, and exposes an ordered list of live sessions. This is the only place that knows
  the on-disk format.
- **`bubble.py`** — new module. Renders the speech bubble: a rounded rect in cairo, pixel
  font or a small system font, positioned above his bounding box and clamped to the screen
  the same way his body already is.
- **`guy.py`** — `Deck.poll_state` is replaced by a callback from `sessions.py`. `Creature`
  is untouched: posture and costume already come from `state` and `tool`, which the session
  file still carries.
- **`notify.sh`** — writes to `~/.deck-guy/sessions/<session_id>.json` instead of
  `state.json`, taking `session_id` from the hook payload. Falls back to a fixed id if the
  payload has none, so an old CLI still animates him.

Backwards compatibility: keep reading `state.json` for one release so a half-updated
install still works, then delete it.

### Screen space

The bubble is drawn inside the existing 1920x200 strip, so no new window and no new
always-on-top problem. Strip height may need to grow from 200 to ~260 to fit bubble plus
jump arc; that is a one-line default change and a re-check that the input shape still only
covers him plus the bubble.

---

## Tasks

1. `~/.deck-guy/` layout, atomic write helper, session file schema.
2. `notify.sh`: extract `session_id` from the hook payload, write per-session files, keep
   the ~2.5ms budget (it is still one `printf` and one `mv`).
3. `sessions.py`: file monitor, parse, reap on heartbeat, ordered live list.
4. Target extraction table + tests for every tool in `TOOL_COSTUME` plus three unknown ones.
5. `bubble.py`: render, position above the body, clamp to screen, fade in/out.
6. Wire `Deck` to `sessions.py`; delete `poll_state` polling.
7. Panel on hover/click with project, session runtime, step elapsed.
8. Grow the strip and re-verify the input shape covers bubble + body only.

---

## Acceptance criteria

- Running `pytest` from Claude Code shows `Bash pytest -q` within 250ms of the call
  starting, and the elapsed counter ticks.
- Editing a file shows `Edit <basename>`, not the full path, not the file contents.
- A tool that is not in the extraction table shows its bare tool name and never raises —
  verified by feeding a fabricated `tool_name` through the hook.
- Killing a terminal mid-run removes the session within `STALE_SECS` and he returns to idle.
- Two sessions writing at once produce two files; the daemon does not lose either.
- `notify.sh` still costs under 5ms per call (100 calls under 0.5s).
- Idle desktop: no bubble, no repaint churn — CPU stays where phase 0 left it.

---

## Risks

- **Bubble text is the first non-pixel element in a pixel-art scene.** A crisp system font
  next to hard pixels can look wrong. Try a small bitmap-ish font first; if it fights the
  art, render the bubble at 1x and scale it with the rest.
  → *Settled by building it both ways: the 1x-and-scale route lost. See "The bubble took
  three passes" below.*
- **`session_id` may not appear in every hook payload** on older CLI versions. Verify
  against the installed version before building on it; the fallback id keeps the pet alive.
- **Screen space creep.** The panel is on hover for a reason. If it ever shows by default,
  this stops being a pet.

---

## Out of scope

Tokens, cost, model, context bar (phase 2). Notifications and alarms (phase 3). More than
one avatar on screen (phase 4) — phase 1 stores many sessions but draws only the newest.

---

## What actually got built

Files added: `sessions.py` (session model, reaper, target extraction), `bubble.py` (bubble
and panel rendering), `test_sessions.py` (98 cases, no display needed). `notify.sh` and
`guy.py` changed; `state.json` and the 250ms poll are gone.

### Confirmed against the installed CLI first

The stated risk was that `session_id` might not be in every payload. It is. A real
`PreToolUse` carries `session_id`, `transcript_path`, `cwd`, `tool_name`, `tool_input`,
`hook_event_name`, `permission_mode`, `effort`, `tool_use_id` and `prompt_id` — so
`permission_mode` fills the panel's `mode` row now, and `transcript_path` is already there
for phase 2.

`hook_event_name` turned out to matter more than expected: `SessionStart` and `SessionEnd`
are both wired to `notify.sh idle` in `settings.json`, and only one of them ends a session.
The event name in the payload disambiguates them, so **no `settings.json` change was needed**
and an existing install keeps working.

### Three deviations from the design above

1. **The hook captures bounded fields, not the raw `tool_input`.** The design said to put
   `tool_input` in the session file as-is and let the daemon rank the fields. A `Write`
   payload is the whole file — 235KB in testing — so the hook now regex-captures the five
   fields it recognises, each capped at 300 characters, and reads stdin in two bites: 1KB,
   then 7KB more only if the first found nothing. The *ranking* still happens in the daemon,
   which was the point of the rule. Measured: 3.6ms typical, 4.0ms on a 235KB `Write`,
   5.8ms in the rare case that needs the second bite.
2. **The hover panel replaces the bubble instead of sitting under it.** The bubble's one
   line is already the panel's `step` row, and stacking them needed a strip half again as
   tall for no new information.
3. **No `state.json` compatibility window.** The design said keep reading it for one
   release. There is no window to cover: `install.sh` replaces `notify.sh` and `guy.py`
   together and restarts the daemon, and hooks exec the script fresh on every event.

### Bugs found while building

- **A hand-run `notify.sh --ensure idle` invented a phantom session.** With no hook payload
  on stdin the session id falls back to `default`, so starting the daemon by hand wrote a
  session file with nothing in it, which the daemon then dutifully mirrored. `--ensure`
  with no `session_id` in the payload now starts him and writes nothing.
- **Session runtime never climbed.** The turn stamps under `~/.deck-guy/steps/` were only
  written by `SessionStart`, so a session that predated the hooks re-stamped itself as
  "started now" on every event. The first event of any kind now stamps the start if it is
  missing.
- **A sparkle survived the jump it belonged to.** Only a completed jump cleared it, so a
  session ending mid-hop left him sparkling at nothing. `_enter()` clears it now.
- The `case` that handles the event arm needed `;&` to fall through, or the start stamp
  never ran for a `prompt`.

### The bubble took three passes

Only the third one looked like anything. Worth recording, because the first two were both
defensible on paper and both wrong on screen.

1. **Everything 1-bit at 8px, scaled 3x with the creature.** The reasoning was that text
   drawn on the same pixel grid as the art must belong to it. What it actually did was
   magnify broken glyphs — at 8px with hinting off, `sheet.png` renders as mush, and
   tripling mush gives you bigger mush. It was also enormous: 279x57 for one short line.
2. **Parked above the tallest possible hop.** So a jump could never collide with the
   bubble. That bought collision-safety at the price of ~130px of permanent empty sky
   between him and his own speech, which read as an unrelated desktop notification. It now
   anchors to his head and rides up with him through a jump. A few more repaints per hop,
   and it looks like he is talking.
3. **The split that worked:** the frame and tail stay pixel-art blocks on his 3px grid, and
   the *text* is drawn natively at 12px, antialiased and hinted. 135x42 — half the width,
   fully legible. Legibility is the one thing here that has to win before style does.

One detail that is easy to get wrong: the frame's bottom border must be drawn in two runs
with a gap where the tail joins. Running it straight across leaves the tail looking like
two nubs stuck underneath a closed box.

### Verified

- All eight acceptance criteria above.
- Three concurrent sessions, none lost; a session with a dead heartbeat reaped in under 12s
  along with its stamp files.
- A fabricated `Frobnicate` tool logged `costume build -> None` and drew a bare tool name —
  no traceback.
- Input shape checked point by point: his body and the readout catch clicks, the gap
  between them and everything either side stays click-through.
- Idle CPU 0.18s per 30s wall clock, below where phase 0 left it.
- Hostile payloads (embedded quotes, backslashes, newlines, a `../../etc/passwd` session id,
  a 200KB `Write`) all produce valid JSON inside `sessions/`.
