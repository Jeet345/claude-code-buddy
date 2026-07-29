# Phase 2 — Tokens, context, cost, model

**Status:** not started
**Depends on:** phase 1 (session model, `transcript_path` stored per session)
**Delivers:** the numbers a terminal does not show you at a glance
**Data needed:** the transcript JSONL. This is the phase where we start parsing it.

---

## Why this is second

Everything here is **read-only**. Nothing can break a session, nothing blocks a hook. And
the context fill bar answers a question you currently cannot answer without guessing: *how
close am I to a compaction.*

---

## What you see

Added to the hover panel from phase 1:

```
  context  ███████████████░░░░░░░  68%   137k / 200k
  session  1.24M in · 38k out            ~$3.41
  model    claude-opus-5                 fast
```

Plus one always-visible cue when it matters: the context bar turns amber past ~75% and red
past ~90%, and that state is allowed to leak onto the buddy himself (a small icon, not a
costume). Everything else stays inside the panel.

---

## Where the data comes from

`~/.claude/projects/<slugified-cwd>/<session_id>.jsonl` — the path arrives in every hook
payload as `transcript_path`, so it never has to be guessed or globbed.

Append-only JSONL, one JSON object per line. **Task 1 of this phase is a discovery script**
that dumps the distinct shapes actually present in a real transcript on this machine, so
the parser is written against observed reality rather than an assumption. Expect to find,
per assistant message, a `usage` object with input, output and cache-read/write token
counts, and a model identifier. Treat every field as optional.

**Tail, do not re-read.** Store a byte offset per session; on each hook event, open, seek,
read to EOF, update the offset. A hook event is the signal that there is something new —
that is the whole reason phases 1 and 2 are in this order.

### The numbers

- **Context fill** = the most recent assistant message's *input* tokens (including cache
  reads) against that model's context window. Not a running total of the session — the
  input token count of the latest turn is what actually approaches the limit.
- **Token burn** = running sums of input and output across the session, for interest.
- **Cost** = tokens × a local price table, split by cache-read / cache-write / input /
  output, since those price differently. Displayed with a `~` and never presented as
  authoritative.
- **Model and mode** from the transcript, falling back to "unknown" rather than a guess.

### The price table is a liability

Put it in `prices.py`, one dict, with a `CHECKED = "2026-07-29"` constant printed in the
panel's tooltip. When the date is over ~90 days old, show the cost in grey with a "may be
stale" tooltip rather than silently drifting. Same for context-window sizes.

---

## Design

- **`transcript.py`** — new module, pure Python, no GTK, no I/O beyond reading the file.
  `TranscriptReader(path)` with `.poll() -> Metrics | None`, holding the byte offset.
  Every field access is defensive; an unparseable line is skipped and counted, not raised.
- **`prices.py`** — price table, context windows, `CHECKED` date, and one function that
  turns a usage dict into an approximate dollar figure.
- **`sessions.py`** — grows a `metrics` field per session, refreshed on hook events.
- **`bubble.py`** — grows the meter widget. One reusable horizontal bar with a threshold
  colour, since phase 7's budget meter wants the same thing.

---

## Tasks

1. **Discovery script**: dump distinct line `type`s and key paths from a real transcript,
   commit the findings into this doc as a short schema note.
2. `transcript.py` with byte-offset tailing and a skip-and-count policy for bad lines.
3. `prices.py` with the table, context windows and `CHECKED` date.
4. Metrics into the session model, refreshed on hook events only.
5. Context bar, token line, cost line, model line in the panel.
6. Threshold colours, plus the small at-a-glance icon for high context.
7. Staleness handling for the price table.

---

## Acceptance criteria

- Context percentage tracks a real session and rises across a long conversation; after a
  compaction it visibly drops.
- A transcript containing a line shape the parser has never seen produces **no crash** and
  no visible breakage — verified by appending deliberate garbage to a copy.
- Cost is within a few percent of the figure Claude Code itself reports for the same session.
- Reading a 50MB transcript does not stall the daemon: first read is bounded, subsequent
  reads are offset-based and effectively free.
- Idle CPU is unchanged from phase 1 — polling must not have crept in.

---

## Risks

- **Undocumented format.** It changes when it changes. The mitigation is the skip-and-count
  policy plus a visible "metrics unavailable" state, so a format change degrades to missing
  numbers rather than a broken HUD.
- **Cost is approximate and people treat numbers as facts.** The `~`, the grey and the
  stale-table warning are not decoration; keep them.
- **First read of a huge transcript.** Bound it: on first attach, seek near the end and
  parse backwards for the last usage block rather than reading the whole file.

---

## Out of scope

Per-tool cost attribution, historical cost across sessions (phase 6's recap), budget
thresholds and warnings (phase 7).
