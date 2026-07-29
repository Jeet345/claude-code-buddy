# Phase 4 — Many sessions, one buddy

**Status:** not started
**Depends on:** phases 1 and 3 (session model; badges)
**Delivers:** one place to see every terminal and worktree at once
**Data needed:** already flowing. This phase is drawing and window management.

---

## What you see

The buddy stays the buddy. Live sessions line up beside him as small avatars, half his size,
each with a badge:

```
   ┌ Bash pytest -q  00:07 ┐
   └───────────▽───────────┘
      (buddy)   ▪ ▪ ▪
                │ │ └── deck-guy   ✓ done
                │ └──── api        ! waiting on you
                └────── worktree-2 ● running
```

- Hover an avatar → its bubble, project name and current step.
- Click an avatar → **focus that terminal window**.
- The main buddy mirrors whichever session is *most interesting*: blocked beats crashed
  beats running beats idle. One rule, written down, so his behaviour is predictable.
- Background tasks and subagents appear as a smaller row under their parent session, from
  `Task` tool calls and `SubagentStop`.

---

## Where the data comes from

Nothing new. Phase 1 already stores every live session; phase 1 only *drew* the newest.
This phase draws them all.

Focusing a terminal is the one genuinely new mechanism:

- The hook payload gives `cwd` and `session_id`, not a window id or a pid.
- Get the pid by having `notify.sh` record `$PPID` chain — cheap, and it is the terminal's
  process tree.
- Map pid → X window via `_NET_WM_PID`, then activate with `wmctrl -ia` or `xdotool
  windowactivate`.

**This works for X11 and XWayland windows only.** A terminal running as a native Wayland
client has no `_NET_WM_PID` we can read and cannot be focused this way. Do not fake it:
when focusing is impossible, the click copies the session's `cwd` to the clipboard and the
tooltip says so. Degrading honestly beats a button that silently does nothing.

---

## Design

- **Avatar rendering**: same atlas, drawn at half scale. At 40x32 authored cells that is
  scale 2 rather than 3 — pixels stay integer, which is why the resolution change in phase 0
  was worth doing.
- **`layout.py`** — new module. Decides where avatars sit given the buddy's position and
  the screen edges, reusing the fence from phase 0 so the row never runs off screen.
- **Priority rule** for what the main buddy reflects, in one function, unit tested.
- **`windows.py`** — pid → window id → activate, with an explicit "cannot" result that the
  UI is required to handle.
- `notify.sh` records the terminal pid alongside the session.

---

## Tasks

1. Record terminal pid in the session file.
2. `layout.py`: avatar row, spacing, screen-edge clamping, overflow ("+3 more").
3. Half-scale avatar drawing; hover bubble per avatar.
4. Priority rule for the main buddy's posture; unit tests.
5. `windows.py`: pid → window, activate, honest failure.
6. Subagent/background-task row under the parent session.
7. Input shape must now cover buddy + avatars; verify click-through everywhere else.

---

## Acceptance criteria

- Three concurrent sessions show three avatars with the correct project names.
- A session that ends removes its avatar within `STALE_SECS`.
- Clicking an avatar focuses that terminal on X11/XWayland; on a native Wayland terminal it
  copies the cwd and says so, rather than doing nothing.
- With eight sessions open the row does not run off screen and does not overlap the buddy.
- The main buddy reflects the highest-priority session, and the rule matches its tests.
- Click-through still works on every pixel that is not the buddy or an avatar.

---

## Risks

- **Screen clutter.** Eight avatars plus badges plus bubbles is a lot at the bottom of a
  1920px screen. Cap the visible row and collapse the rest into a count.
- **Wayland focusing.** Stated above; the mitigation is honesty, not effort.
- **Input shape complexity.** The shape is now a union of rectangles that changes as
  avatars come and go. Get this wrong and the desktop stops receiving clicks — it deserves
  its own test.

---

## Out of scope

Acting on a session (phase 5). Per-session history (phase 6).
