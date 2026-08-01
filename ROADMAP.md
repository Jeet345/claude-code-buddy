# Deck Guy — from desktop pet to live HUD

**Status:** **v1 is built and running** — phases 1, 2 and 3. Phases 4-7 are still planning
only. The pet itself (PLAN.md phases 0-4) is done.
**Written:** 2026-07-29 · **phase 1:** 2026-07-30 · **phases 2 and 3:** 2026-08-01

The idea: he stops being a pet that reacts to Claude Code and becomes a **window into**
Claude Code — always visible, always current, and eventually a place you can act from
without switching to the terminal.

This document orders that work by what earns its keep soonest, and says plainly which
parts are blocked on things outside our control. **Each phase has its own document under
[`phases/`](phases/README.md)** with the design, task list, acceptance criteria and risks;
this file is the index and the reasoning behind the ordering.

---

## 1. What is actually knowable, and how

Everything downstream depends on this, so it goes first. There is **no streaming API for
external observers** of a Claude Code session. There are exactly two doors:

| Door | Gives you | Costs |
|---|---|---|
| **Hooks** | Event-driven truth: session start/end, prompt submitted, every tool call before and after it runs, notifications, stop. Each carries JSON on stdin: `session_id`, `cwd`, `transcript_path`, `tool_name`, `tool_input`, and for `PostToolUse` the result. | Runs on every tool call, so it must stay fast. ~4ms per call, and phase 3 wires `PostToolUse` as well, so a tool call pays for two. |
| **Transcript JSONL** | Everything else: model id, per-message token usage (input, output, cache read/write), full tool inputs and outputs, timings. One append-only `.jsonl` per session under `~/.claude/projects/<slugified-cwd>/<session_id>.jsonl`. | Parsing someone else's file format. It is stable in practice but undocumented, so treat every field as optional and never crash on a shape you do not recognise. |

The pairing that makes the HUD work: **hooks tell you *when* to look, the transcript tells
you *what happened*.** A hook fires, hands you `transcript_path`, and the daemon tails the
file from its last byte offset. No polling, no re-parsing, no guessing.

Three things are **not** cleanly knowable today and should not be promised in v1:

- **"Thinking" as distinct from "working".** You get prompt-submitted and first-tool-call.
  The gap between them is thinking, inferred, not reported.
- **Which permission prompt is on screen**, unless we intercept it ourselves (see phase 5).
- **Anything about a session started before the hooks were installed.** Hooks load at
  session start.

---

## 2. Architectural change this forces — **done in phase 1**

It used to be one `state.json`, last writer wins. That was right for a pet and wrong for a
HUD, because a HUD has to show *sessions*, plural, each with its own history.

```
~/.deck-guy/
  sessions/<session_id>.json     one file per live session, atomically replaced  [built]
  steps/<session_id>.{start,prompt}   turn timestamps the hook cannot hold in memory  [built]
  history/<date>.jsonl           append-only event log for timeline and recap    [phase 6]
  shelf/<edit_id>/               before/after copies for the undo shelf          [phase 6]
```

`notify.sh` writes the session file; the daemon watches the directory with a `Gio.FileMonitor`
instead of polling. Session files carry a heartbeat timestamp and are reaped when
stale — the same staleness rule the pet already needed when a session dies without a `Stop`.

Keep: Python, GTK3, XWayland, the ASCII-grid sprite pipeline. None of that is the
bottleneck, and the pet's charm is the product. **The HUD is a panel that appears near him,
not a replacement for him.**

---

## 3. Phases

Ordered so that each one is useful on its own, and so the risky, possibly-impossible work
lands after everything that definitely works.

### Phase 1 — Session model + speech bubble *(the foundation)* — **DONE**

> Full spec and what changed on contact:
> [`phases/phase-1-session-model.md`](phases/phase-1-session-model.md)


The smallest thing that makes him a HUD, built entirely from hook data. No transcript
parsing yet.

- Per-session state files, file-monitor instead of polling, staleness reaper.
- One line above his head: **what tool, on what target** — `Edit src/auth.py`,
  `Bash pytest -q`, `Grep "TODO"`. Truncated hard, one line, never a log dump.
- Elapsed time on the current step, and session runtime.
- Project folder name (from `cwd`) and current mode if the hook payload exposes it.
- Bubble auto-hides a few seconds after a step ends, so idle stays quiet.

Why first: it is pure hook data, it is the feature you look at every minute, and it forces
the session-model refactor that everything else needs.

### Phase 2 — Transcript reader: tokens, context, cost, model — **DONE**

> Full spec and what changed on contact: [`phases/phase-2-metrics.md`](phases/phase-2-metrics.md)


- Tail the JSONL from a stored byte offset on each hook event.
- **Context fill bar** — the honest one: last assistant message's input tokens against the
  model's context window. This is the number that actually predicts a compaction.
- Token burn and **session cost**, from usage times a local price table. The price table
  is a maintenance liability; keep it in one obvious file with a "checked on" date, and show
  cost as approximate.
- Model id and, if present, effort/mode.

Why second: it is the highest-value information that a terminal does not already show you
at a glance, and it is read-only — nothing can break a session.

What it cost to be right: the context window is not in the transcript at all, so the table
in `prices.py` has to promote itself when a reading exceeds it and grey the number out when
it does. The two bugs that mattered both produced *plausible* numbers rather than obvious
breakage — see the phase doc.

### Phase 3 — Attention: he tells you when you are the blocker — **DONE**

> Full spec and what changed on contact:
> [`phases/phase-3-attention.md`](phases/phase-3-attention.md)


- Hook the `Notification` event: Claude needs permission or input → he **waves**, a badge
  appears, optional `notify-send` toast and a sound.
- **Idle-waiting alarm**: waiting on you for more than N minutes escalates — bigger
  animation, then a toast. This is the single most valuable feature in the whole list,
  because the failure it fixes (session sat blocked for 20 minutes while you did something
  else) costs real time.
- Error/crash state: a distinct animation, from non-zero `PostToolUse` results and from
  sessions that go stale without a `Stop`.
- Toast on: run finished, tests failed, scheduled agent fired.

Sound stays off by default. It is the fastest way to make a charming thing annoying.

What it cost to be right: the CLI turned out to have 31 hook events, not the five that
were wired, and two of the new ones (`PostToolUseFailure`, `StopFailure`) are better
failure signals than reading tool results. It also turned out that there is **no event for
"the prompt was answered"** — clearing has to be the absence of an alert on the next hook
of any kind, which is why `PostToolUse` is now wired and why the whole design hangs on an
alert being orthogonal to the state rather than being one.

### Phase 4 — Multi-session

> Full spec: [`phases/phase-4-multi-session.md`](phases/phase-4-multi-session.md)


- One buddy, plus a small avatar per live session standing near him.
- Per-avatar badge: blocked on permission, finished, crashed, running.
- Background task and subagent list with progress, from `Task` tool calls and
  `SubagentStop`.
- **Click a session to focus its terminal.** Doable on X11/XWayland via the window's
  `_NET_WM_PID` and `wmctrl`/`xdotool`. Native Wayland terminal windows cannot be focused
  this way — accept the limitation and degrade to "copy the session's cwd" rather than
  fake it.

### Phase 5 — Control *(the first phase that can fail)*

> Full spec: [`phases/phase-5-control.md`](phases/phase-5-control.md) — **spike before building**


Ordered by descending confidence:

1. **Approve/deny a permission prompt from the buddy.** Genuinely possible: a `PreToolUse`
   hook can return a permission decision, so the hook writes a request file, blocks on the
   daemon's answer, and returns allow or deny. Risks that must be tested before committing:
   the hook blocks the session while it waits, hook timeouts are finite, and a daemon that
   is not running must fail **open with the normal prompt**, never deny silently. Prototype
   this behind a flag on a throwaway project first.
2. **Interrupt / stop a session.** No API. Approximated by signalling the `claude` process
   from its pid. Test what SIGINT actually does to an in-flight tool call before shipping.
3. **Quick prompt box.** Sending text into a *running* session is not supported. The honest
   options are: `tmux send-keys` when the session runs inside tmux (reliable, and worth
   scoping to exactly that), or launching a **new** headless run — which is a different
   thing and should be labelled as such in the UI.
4. **Drag a file onto him to attach its path.** Only meaningful once a prompt box exists,
   so it inherits the same constraint.
5. Slash-command launcher for skills — same mechanism, same constraint.

If 1 proves unworkable against the installed CLI, the phase still delivers value as
*notify-and-jump-me-there* rather than *act-from-here*. Decide after the prototype, not now.

### Phase 6 — Memory and history

> Full spec: [`phases/phase-6-history.md`](phases/phase-6-history.md)


- Timeline per session: files touched, commands run, diffs.
- Scrollback of the last N tool calls; click one to open `file:line` in the editor.
- **Undo shelf.** `PreToolUse` on Edit/Write gives the target path before the write —
  copy the file to the shelf, and every edit becomes revertible one at a time. This is
  the sleeper feature in the whole list; it is small, it is entirely within our control,
  and it turns "Claude touched 30 files" from a worry into a list.
- Daily recap: what ran, what it cost, what changed.

### Phase 7 — Ambient and personality

> Full spec: [`phases/phase-7-ambient.md`](phases/phase-7-ambient.md)


Cheap, independent, do them whenever:

- Git state of the watched repo: branch, dirty count, unpushed commits.
- CI/PR status dots via `gh`.
- Cost budget meter with a warning threshold.
- Mood tied to metrics (tests green → happy).
- Skins and themes; caveman-mode buddy speaks caveman.
- Levels/XP by sessions completed.

Speech bubbles summarising an action in one line belong to phase 1, not here — that is
information, not personality.

---

## 4. v1 — built

**Phases 1 + 2 + 3.** He shows what is happening, what it is costing, and shouts when you
are the one holding things up. That is a complete, coherent product with no dependency on
anything Claude Code does not already support, and nothing in it can break a session.

Phase 4 is the natural v1.1. Phase 5 is v2 and needs a prototype before it gets a date.

---

## 5. Risks worth stating once

- **The transcript format is undocumented.** Treat every field as optional. A HUD that
  crashes on an unfamiliar message shape is worse than no HUD.
- **The price table will drift.** Show cost as approximate, date-stamp the table.
- **Blocking hooks are dangerous.** Anything that makes Claude Code wait on our daemon must
  have a short timeout and must fail open.
- **Screen real estate.** Every feature here wants pixels. The pet works because he is small
  and ignorable. The HUD should stay collapsed by default and expand on hover or click.
- **This is a second product.** The pet is finished and pleasant. The HUD is bigger than
  everything built so far combined. Phase 1 alone is worth more than phases 4-7 rushed.
