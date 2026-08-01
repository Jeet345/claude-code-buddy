# Phase 2 — Tokens, context, cost, model

**Status:** **done** (2026-08-01) — built, tested and running.
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

### Schema note — observed 2026-08-01

`python3 dump_transcript.py`, over 7 projects / 2718 assistant lines. Re-run it when the
numbers start looking wrong; a format change shows up there first.

**Line `type`s seen:** `assistant`, `user`, `attachment`, `last-prompt`, `mode`,
`ai-title`, `permission-mode`, `system`, `file-history-snapshot`, `file-history-delta`,
`queue-operation`. Only `assistant` and `system` matter here.

**`assistant` lines** carry the numbers, all under `message`:

```
message.model                              claude-opus-5 | claude-sonnet-5 | <synthetic>
message.usage.input_tokens                 int   uncached input this turn
message.usage.cache_read_input_tokens      int   the bulk of it once warm
message.usage.cache_creation_input_tokens  int   written this turn, priced higher
message.usage.cache_creation.ephemeral_5m_input_tokens / ephemeral_1h_input_tokens
message.usage.output_tokens                int
message.usage.iterations[]                 same four keys again, per API call
isSidechain                                bool  true = subagent, not your context
```

Four notes that changed the design:

- **`<synthetic>` is a real model value** on locally generated messages, with all-zero
  usage. It must not reach the price table or the model row.
- **`iterations` was length 1 on every one of 2706 lines**, so whether the top-level usage
  sums it or mirrors its last entry is untestable here. Top-level is treated as
  authoritative and `iterations` ignored — revisit if a multi-iteration line ever appears.
- **`isSidechain: true` is subagent traffic.** It costs money but is not in your context
  window, so it counts toward burn and cost and never toward the fill bar.
- **`session_id` (snake) drifts across resumes; `sessionId` (camel) matches the filename.**
  Neither is needed — `transcript_path` from the hook is the only thing we key on.

**`system` lines** carry `subtype`: `compact_boundary`, `turn_duration`,
`stop_hook_summary`, `local_command`, `away_summary`. `compact_boundary` is ground truth
for the fill reading —

```
compactMetadata.trigger      manual | (auto, presumably)
compactMetadata.preTokens    251264
compactMetadata.postTokens   11221
```

— and gives phase 2 its acceptance test for free: the bar must show roughly `preTokens`
just before one of these lines and drop to roughly `postTokens` just after.

**The context window is not in the transcript.** Not as a field, not in an error message.
It has to come from a local table, and the table is already wrong-ish: observed context
reached **388k on sonnet-5** and **366k on opus-5**, so the `137k / 200k` mock-up above is
fiction. Those are floors, never the window. Hence `effective_window()`: the table value,
promoted to the next tier up whenever an observed reading exceeds it, and flagged
approximate when that promotion happens. A hardcoded 200k would have read 190% full.

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

---

## What actually got built

Four new files — `dump_transcript.py`, `prices.py`, `transcript.py`, `test_transcript.py` —
plus a `Meter` row in `bubble.py`, a `metrics` field on `Session`, and five rows on the
hover panel. 164 tests, `python3 test_transcript.py`. Three things came out differently
from the plan above:

**The mock-up's `200k` window was fiction, and finding that out changed the design.**
Observed context reached 388k on sonnet-5 and 366k on opus-5 during discovery, so a
hardcoded 200k would have drawn a bar reading 190% full. The window turned out not to be
in the transcript at all, which is why `prices.window()` ended up with the
promote-and-flag behaviour rather than a plain lookup: the table is the answer until a
reading contradicts it, and then the reading wins and the number is drawn grey.

**"Seek near the end and parse backwards" became "seek near the end and parse forwards".**
Parsing JSONL backwards means finding line boundaries in reverse and buffering an unknown
number of lines to reach the last one with a `usage` block. Seeking to `size - 256KB`,
discarding the partial line you land inside, and reading forward to EOF gets the same
answer in one pass with no reverse scanning. The cost is that a tailed session's running
totals start mid-file — hence the `partial` flag and the `(partial)` suffix on the tokens
row, which is honest rather than quietly under-reporting.

**The at-a-glance cue is a badge on his shoulder, not near the readout.** It has to be
visible when the bubble is off and when nothing is hovered, so it is attached to him.

### Two bugs the tests caught, one they nearly didn't

`poll()` reopened the file every call and never seeked to the stored offset, so every read
after the first started from byte 0. That double-counted every running total and corrupted
the held-over partial line. Three separate test failures, one cause.

The second was worse because it produced a *plausible* number. `CHUNK` capped a poll at
1MB, but `FULL_READ` allowed attach to start at byte 0 of anything under 4MB — so a 3MB
transcript reported the context of whatever assistant line happened to sit around the 1MB
mark, and only crept toward the truth one poll at a time. Nothing looked wrong: a
believable token count, a believable bar. It surfaced only from cross-checking a full read
against a tailed read of the same file and finding they disagreed (91,078 vs 433,668).
A poll now drains to EOF, and there is a regression test with a fixture deliberately
spanning several chunks.

The lesson worth keeping: for a readout like this, *wrong* and *missing* are not equally
bad. A missing number gets noticed and fixed; a plausible wrong one gets believed. The
skip-and-count policy protects against missing. Only the cross-check protected against
plausible.

### Verified against the acceptance criteria

- **Context tracks a real session and drops after a compaction.** `compact_boundary` lines
  carry `preTokens`/`postTokens`, which is ground truth for free — the reader now reports
  the boundary, and the test asserts the reading is ~`preTokens` just before it and
  ~`postTokens` just after. Two real transcripts on this machine exercise it.
- **Deliberate garbage produces no crash.** Tested twice: a synthetic file of hostile
  shapes, and a *copy of the largest real transcript* with junk appended.
- **A 50MB transcript does not stall the daemon.** Fixture built at 50MB: first read pulls
  256KB in under a millisecond, subsequent polls are offset-based, a poll with nothing new
  is one `getsize` and an early return.
- **Cost within a few percent** — this one is *not* verified against Claude Code's own
  figure, because a subscription session never reports one to compare against. What is
  verified is the arithmetic: a hand-worked example checked to 1e-9, and cache reads,
  5-minute writes and 1-hour writes priced separately (lumping them into `input` is wrong
  by 10x in one direction and 2x in the other).
- **Idle CPU unchanged.** No new timer: `reload()` already ran on every hook event and on
  the phase 1 reaper tick, and metrics refresh there. Measured 0.75%, though under an
  actively-working session rather than a genuinely idle one.

### Schema notes worth carrying into phase 3

`isSidechain` marks subagent traffic: it costs money but is not in your context window, so
it counts toward burn and cost and never toward the fill bar. `<synthetic>` is a real
value of `message.model` on locally generated messages and must never reach the price
table. And an assistant line whose `usage` is the wrong shape is counted as a skipped line
rather than silently ignored the way an unrelated line type is — that is exactly the shape
a format change takes, so it should be visible in the `bad` tally.
