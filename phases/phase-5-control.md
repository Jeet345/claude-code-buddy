# Phase 5 — Act without switching to the terminal

**Status:** not started. **Do not start building — start with the spike below.**
**Depends on:** phases 1, 3, 4
**Delivers:** the buddy stops being a display and becomes an input
**Data needed:** hook *output*, which is a different contract from everything before it.

---

## Read this before anything else

Every phase up to here is read-only. This one writes: it can block a session, deny a tool
call, or signal a process. **The failure modes are no longer cosmetic.** A HUD bug so far
means a wrong number on screen. A bug here means a session that hangs, or a tool that gets
denied without you knowing why.

So this phase is gated on a **spike**: a throwaway prototype on a scratch project that
answers the open questions below. If the spike fails, the phase still ships value — as
*notify me and take me there* rather than *act from here*. That decision gets made after
the spike, not now.

---

## Ordered by descending confidence

### 5a. Approve / deny a permission prompt *(most valuable, plausible)*

A `PreToolUse` hook can return a permission decision, which means the approve-from-buddy
flow is not obviously impossible:

```
PreToolUse fires
  → hook writes request file, then waits for an answer file
  → daemon shows Approve / Deny on the buddy
  → you click
  → daemon writes the answer, hook returns the decision, session continues
```

**Open questions the spike must answer:**

- What exactly does the installed CLI accept as a permission decision from a hook, and does
  it match the docs for that version?
- What is the hook timeout, and what happens to the session when it is hit?
- Does a blocking hook stall anything else in the session while it waits?
- If the daemon is not running, does the flow **fail open** — falling back to the normal
  terminal prompt — under every failure it can have? *This is non-negotiable.* Silently
  denying tool calls because a GUI daemon crashed is the worst outcome in this document.

**Design constraints if it proceeds:** short wait (a few seconds, not minutes), fail open on
timeout, and a visible mode indicator so you always know the buddy is intercepting prompts.
Ship it behind a flag that defaults to off.

### 5b. Interrupt / stop a session *(plausible, blunt)*

No API. The approximation is signalling the `claude` process from the pid recorded in
phase 4. The spike must establish what SIGINT actually does to an in-flight tool call, and
whether the session survives it in a usable state. If it corrupts the session, drop it.

### 5c. Quick prompt box *(constrained — be honest in the UI)*

Sending text into an **already-running** session is not supported. Two honest options:

- **tmux**: `tmux send-keys` works reliably when the session runs inside tmux. Scope the
  feature to exactly that, detect it, and hide the box otherwise.
- **New headless run**: `claude -p …` starts a *different* session. Useful, but it must be
  labelled as starting something new, never dressed up as talking to the session on screen.

Typing into an arbitrary terminal with `xdotool` is rejected: it depends on focus, races
with the user's keyboard, and fails silently in the worst way.

### 5d. Drag a file onto him → attach the path

Only meaningful once 5c exists, and inherits its constraint. Cheap once the box is there.

### 5e. Slash-command launcher for skills

Same mechanism as 5c, same constraint. A menu of your skills (`/stock`, `/map`) that fills
the prompt box.

---

## Tasks

1. **Spike** on a scratch project answering every open question in 5a and 5b. Time-box it.
2. Write the findings back into this document before writing production code.
3. If 5a proceeds: request/answer protocol, fail-open guarantees, mode indicator, flag.
4. Approve/Deny UI on the buddy, with the tool and target visible in the prompt.
5. 5b if the spike says the session survives it.
6. tmux detection, prompt box, and the "starts a new session" labelling for the fallback.
7. 5d and 5e once the box exists.

---

## Acceptance criteria

- **Fail open, proven by injury:** kill the daemon mid-prompt, corrupt the answer file,
  fill the disk — in every case the terminal prompt appears as normal and no tool is denied
  by accident.
- Approving from the buddy allows exactly the call that was shown, never a different one.
- The intercept mode is visible whenever it is on, and off by default.
- The prompt box either targets a tmux session or clearly says it is starting a new run.
- Interrupt leaves the session usable, or it does not ship.

---

## Risks

- **This phase can hurt.** Everything above is about containing that.
- **Version coupling.** Hook output contracts change between CLI versions; anything built
  here needs a version check and a graceful "not supported on this version" path.
- **Scope creep into a full client.** The buddy is a glance and a click. If a feature needs
  a keyboard and a scrollback, it belongs in the terminal.
