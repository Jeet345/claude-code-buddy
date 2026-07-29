# Phase 3 — He tells you when you are the blocker

**Status:** not started
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

---

## Design

- **New art**: an `alert` badge prop (`!` in a bubble) and an `error` posture — a slumped
  version of the sleep pose with a distinct colour accent. Both go through the existing
  ASCII-grid pipeline and cost a prop grid plus anchors.
- **`alerts.py`** — new module. Owns escalation timers, dedupe (one alert per session, not
  one per event) and acknowledgement. Nothing about drawing.
- **Toasts** — `notify-send` via `subprocess`, never blocking, failure ignored. Sound via
  `paplay` with the same rule.
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
7. Click-to-acknowledge; menu entry to mute for an hour.

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
