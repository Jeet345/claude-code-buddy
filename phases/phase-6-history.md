# Phase 6 — Timeline, scrollback, undo shelf

**Status:** not started
**Depends on:** phases 1 and 2
**Delivers:** answers to "what did it just do to my repo"
**Data needed:** hooks for the events, transcript for the detail, plus our own file copies.

---

## The sleeper feature

Most of this phase is nice-to-have. **The undo shelf is not.** It is small, it is entirely
within our control, it depends on nothing undocumented, and it turns "Claude touched 30
files" from a worry into a list you can walk back one item at a time.

If this phase gets cut down, keep the shelf.

---

## What you see

A panel, opened from the buddy's menu, with three tabs:

**Timeline** — this session, newest first:

```
  14:22:07  Edit    src/auth.py         +12 −3
  14:22:01  Bash    pytest -q           exit 1
  14:21:44  Read    src/auth.py
```

**Scrollback** — the last N tool calls across sessions. Click a row to open `file:line` in
the editor (`$EDITOR`, or `code -g`, or `xdg-open` — configurable, detected once).

**Shelf** — every file edit Claude made, with a Revert button per entry:

```
  src/auth.py        14:22:07   [ revert ]   [ diff ]
  tests/test_auth.py 14:19:52   [ revert ]   [ diff ]
```

Plus a **daily recap**, generated on demand: what ran, what it cost, which files changed.

---

## Where the data comes from

| Need | Source |
|---|---|
| Event stream | hooks, appended to `~/.deck-guy/history/<date>.jsonl` |
| Tool detail | the transcript (phase 2's reader already tails it) |
| Diffs | our own before/after copies |
| Cost per day | phase 2's metrics, summed |

### How the shelf works

`PreToolUse` on `Edit`/`Write`/`NotebookEdit` receives `tool_input` containing the target
path **before the write happens**. That is the whole trick:

1. On `PreToolUse` for a writing tool, copy the current file to
   `~/.deck-guy/shelf/<edit_id>/before`.
2. On `PostToolUse`, copy the new contents to `.../after` and record the metadata.
3. Revert = copy `before` back, after showing the diff and confirming.

Constraints that make it safe:

- **Copy, never move.** The shelf is additive; it can be deleted wholesale at any time.
- **Size cap and TTL** — skip files over a threshold, keep N days, prune on start. A shelf
  that fills the disk is a bug that costs more than the feature is worth.
- **Refuse to revert a file that changed since the copy** unless explicitly confirmed —
  you may have edited it yourself after Claude did.
- The copy happens in the hook, so it must stay fast: one `cp` on a bounded file size.

---

## Design

- **`history.py`** — append-only event log writer and reader, one JSONL per day.
- **`shelf.py`** — copy, list, diff, revert, prune. Pure file operations, unit testable.
- **`panel.py`** — the tabbed window. This is the first piece of real GTK UI in the project;
  everything so far is a custom-drawn strip. Keep it an ordinary window, not a transparent
  overlay, and let the window manager handle it.
- Editor detection: `$VISUAL`, `$EDITOR`, `code -g`, `xdg-open`, in that order, resolved once
  and stored.

---

## Tasks

1. `history.py` and the daily JSONL format.
2. Shelf copy on `PreToolUse` / `PostToolUse` for writing tools; size cap and TTL.
3. `shelf.py`: list, diff, revert with the changed-since-copy guard.
4. `panel.py` with the three tabs.
5. Click-to-open `file:line`, with editor detection.
6. Daily recap generation.
7. Prune job on daemon start.

---

## Acceptance criteria

- Every file Claude edits appears on the shelf within a second of the edit.
- Reverting restores the exact prior bytes, verified by hash.
- Editing a file yourself after Claude did, then reverting, warns before overwriting.
- A 200MB file is skipped by the size cap, and the skip is visible in the shelf rather than
  silently absent.
- The shelf prunes to the TTL on start and never exceeds its cap.
- Hook cost with shelf copying stays under 10ms for ordinary source files.
- Clicking a scrollback row opens the right file at the right line.

---

## Risks

- **Disk usage.** Cap, TTL, prune. Stated three times because it is the way this feature
  goes wrong.
- **False confidence in revert.** It restores a file, not a state — if Claude ran a
  migration or a shell command, reverting the file does not undo that. Say so in the UI.
- **Panel scope creep.** This is a list with buttons, not a diff viewer, not an IDE.

---

## Out of scope

Cross-session search, undo of shell commands (not possible), git integration (phase 7).
