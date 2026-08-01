# Phase documents

One document per phase. `../ROADMAP.md` says *why* this order; these say *what to build*.

Every phase doc has the same shape: what you see, where the data comes from, the design,
a task list, acceptance criteria you can actually check, risks, and what is explicitly
deferred. If a phase cannot state its acceptance criteria, it is not ready to start.

| Phase | Doc | Status |
|---|---|---|
| 0 | [`../PLAN.md`](../PLAN.md) — the pet itself | **done** |
| 1 | [`phase-1-session-model.md`](phase-1-session-model.md) — session model + speech bubble | **done** 2026-07-30 |
| 2 | [`phase-2-metrics.md`](phase-2-metrics.md) — tokens, context, cost, model | **done** 2026-08-01 |
| 3 | [`phase-3-attention.md`](phase-3-attention.md) — he tells you when you are the blocker | **done** 2026-08-01 |
| 4 | [`phase-4-multi-session.md`](phase-4-multi-session.md) — many sessions, one buddy | not started |
| 5 | [`phase-5-control.md`](phase-5-control.md) — act without switching to the terminal | not started, **needs a spike first** |
| 6 | [`phase-6-history.md`](phase-6-history.md) — timeline, scrollback, undo shelf | not started |
| 7 | [`phase-7-ambient.md`](phase-7-ambient.md) — git, CI, budget, personality | not started |

**v1 = phases 1 + 2 + 3, and v1 is built.** Nothing in those depends on unsupported
behaviour, and nothing in them can break a running Claude Code session.

## Conventions used across the phases

- **Read-only until phase 5.** Anything that writes to a session, blocks a hook, or signals
  a process is control, and control is phase 5 with a spike in front of it.
- **Hooks must stay fast.** The current writer costs ~2.5ms. Any phase that adds work to a
  hook has to keep that budget: write a file, exit 0. Parsing happens in the daemon.
- **Fail quiet, never fail loud.** A HUD that crashes, blocks, or spams is worse than no
  HUD. Every unknown field is optional; every missing file is a shrug.
- **He stays small.** Panels are collapsed by default. The pet is the product; the HUD is
  what he is holding.
