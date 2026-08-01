#!/usr/bin/env python3
"""Reading the numbers out of a Claude Code transcript, cheaply and defensively.

The transcript is append-only JSONL at the path every hook payload hands us, so
this module never has to guess or glob. It holds a byte offset and reads only
what arrived since last time - a hook event is the signal that there is
something new, which is the whole reason phases 1 and 2 are in this order.

Two rules run through all of it:

    skip and count   an unparseable or unfamiliar line is skipped and tallied,
                     never raised. The format is undocumented and will change;
                     when it does, the HUD should lose numbers, not break.
    bound the first  a 50MB transcript is not read on attach. Under FULL_READ
                     we read it all and the totals are exact; over it we seek
                     near the end, take the context reading from there, and
                     mark the session's running totals `partial`.

    reader = TranscriptReader(path)
    m = reader.poll()            # None until there is something new
    m.context, m.window, m.cost  # what the panel draws

`python3 transcript.py <file.jsonl>` dumps a reading for one transcript.
"""

import json
import os
import sys

import prices

FULL_READ = 4_000_000      # bytes; under this the first read is the whole file
TAIL_BYTES = 262_144       # over it, this much of the tail instead
CHUNK = 1_000_000          # bytes per read() call; a poll drains as many as it needs


class Metrics:
    """One reading. Every field is optional in the sense that it may be zero."""

    def __init__(self):
        self.model = ""
        self.speed = ""            # "standard" | "fast"
        self.effort = ""
        self.context = 0           # input tokens of the latest turn
        self.window = 0
        self.window_approx = True
        self.input = 0             # running sums across everything we have read
        self.output = 0
        self.cost = 0.0
        self.turns = 0
        self.partial = False       # totals started mid-file, so they under-count
        self.bad = 0               # lines skipped
        self.compacted = None      # (pre, post) of the last compaction we saw

    @property
    def fill(self):
        """0.0-1.0. Clamped, because an over-full reading is a table problem."""
        if self.window <= 0:
            return 0.0
        return min(1.0, max(0.0, self.context / self.window))

    @property
    def ok(self):
        """Whether there is anything worth drawing."""
        return self.context > 0 or self.output > 0

    def __repr__(self):
        return (f"<Metrics {self.model or '?'} {self.context}/{self.window}"
                f" {prices.money(self.cost)}{' partial' if self.partial else ''}>")


def _int(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _usage_of(obj):
    """The usage dict of an assistant line, or None for anything else.

    `iterations` is deliberately ignored: it was length 1 on every one of the
    2706 lines in the discovery run, so whether the top-level usage sums it or
    mirrors its last entry is untestable. Top-level is treated as authoritative
    until a multi-iteration line turns up.
    """
    if obj.get("type") != "assistant":
        return None
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return None
    usage = msg.get("usage")
    return usage if isinstance(usage, dict) else None


def _context_of(usage):
    """What the model actually saw this turn: fresh + cached + newly cached.

    Not a running total of the session - the input token count of the latest
    turn is the thing that approaches the limit, and it drops when Claude Code
    compacts.
    """
    return (_int(usage.get("input_tokens"))
            + _int(usage.get("cache_read_input_tokens"))
            + _int(usage.get("cache_creation_input_tokens")))


class TranscriptReader:
    """Tails one transcript. Cheap to poll, safe to poll when nothing changed."""

    def __init__(self, path):
        self.path = str(path or "")
        self.offset = 0
        self.bytes_read = 0        # how much we have actually pulled off disk
        self.m = Metrics()
        self._attached = False
        self._tail = b""           # a line split across two reads

    # ---------------------------------------------------------------- reading
    def poll(self):
        """Read whatever arrived since last time. `None` if nothing did."""
        if not self.path:
            return None
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return None

        if size < self.offset:
            # Truncated, or the path was reused by a different session.
            self._reset()
        if self._attached and size == self.offset:
            return None

        try:
            with open(self.path, "rb") as fh:
                if self._attached:
                    # The file is reopened every poll, so the stored offset is
                    # the only thing that stops us re-reading it from the top -
                    # which double-counts every total and corrupts the held-over
                    # partial line.
                    fh.seek(self.offset)
                else:
                    self._attach(fh, size)
                # Drain to EOF rather than stopping after one chunk. The
                # context reading is only meaningful if it comes from the last
                # assistant line in the file; a poll that stops short reports a
                # number from the middle of the session and looks plausible
                # while being wrong.
                read_any = False
                while True:
                    data = fh.read(CHUNK)
                    if not data:
                        break
                    read_any = True
                    self.offset = fh.tell()
                    self.bytes_read += len(data)
                    self._consume(data)
        except OSError:
            return None

        if not read_any and self._attached:
            return None
        self._attached = True
        self.m.window, self.m.window_approx = prices.window(self.m.model, self.m.context)
        return self.m

    def _attach(self, fh, size):
        """Position for the first read: whole file, or a bounded tail of it."""
        if size <= FULL_READ:
            fh.seek(0)
            return
        fh.seek(max(0, size - TAIL_BYTES))
        fh.readline()              # drop the partial line we landed inside
        self.m.partial = True

    def _reset(self):
        self.offset = 0
        self.m = Metrics()
        self._attached = False
        self._tail = b""

    # --------------------------------------------------------------- parsing
    def _consume(self, data):
        if not data:
            return
        data = self._tail + data
        # Everything after the last newline is an incomplete line; hold it for
        # the next poll rather than counting half a JSON object as garbage.
        cut = data.rfind(b"\n")
        if cut < 0:
            self._tail = data
            return
        self._tail = data[cut + 1:]
        for raw in data[:cut].split(b"\n"):
            if raw.strip():
                self._line(raw)

    def _line(self, raw):
        try:
            obj = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            self.m.bad += 1
            return
        if not isinstance(obj, dict):
            self.m.bad += 1
            return
        try:
            self._apply(obj)
        except Exception:          # a shape we have never seen
            self.m.bad += 1

    def _apply(self, obj):
        if obj.get("type") == "system":
            meta = obj.get("compactMetadata")
            if isinstance(meta, dict):
                # Ground truth: the bar should read ~pre just before this line
                # and ~post just after.
                self.m.compacted = (_int(meta.get("preTokens")),
                                    _int(meta.get("postTokens")))
            return

        if obj.get("type") != "assistant":
            return                 # user, attachment, mode, ai-title, ... - not ours

        usage = _usage_of(obj)
        if usage is None:
            # An assistant line we cannot read is the shape a format change
            # takes, so it is counted rather than silently ignored the way an
            # unrelated line type is.
            self.m.bad += 1
            return
        msg = obj["message"]
        model = msg.get("model") or ""
        speed = usage.get("speed") or ""

        # Subagent traffic costs money but is not in your context window, so it
        # counts toward burn and cost and never toward the fill bar.
        self.m.input += _context_of(usage)
        self.m.output += _int(usage.get("output_tokens"))
        self.m.cost += prices.cost(usage, model, speed)
        self.m.turns += 1

        if obj.get("isSidechain"):
            return
        if prices.synthetic(model):
            return                 # locally generated, all-zero usage
        self.m.model = model
        self.m.speed = speed
        self.m.effort = obj.get("effort") or self.m.effort
        self.m.context = _context_of(usage)


def read(path):
    """One-shot convenience for scripts and tests."""
    return TranscriptReader(path).poll()


if __name__ == "__main__":
    for arg in sys.argv[1:] or ["-"]:
        m = read(arg)
        if m is None:
            print(f"{arg}: unreadable")
            continue
        pct = f"{m.fill * 100:.0f}%"
        approx = "~" if m.window_approx else ""
        print(f"{arg}\n"
              f"  model    {m.model or 'unknown'} {m.speed} {m.effort}\n"
              f"  context  {prices.tokens(m.context)} / {approx}"
              f"{prices.tokens(m.window)}  {pct}\n"
              f"  session  {prices.tokens(m.input)} in · "
              f"{prices.tokens(m.output)} out over {m.turns} turns"
              f"{' (partial)' if m.partial else ''}\n"
              f"  cost     {prices.money(m.cost)}"
              f"{'  [stale table]' if prices.stale() else ''}\n"
              f"  skipped  {m.bad} lines"
              + (f"\n  compact  {m.compacted[0]:,} -> {m.compacted[1]:,}"
                 if m.compacted else ""))
