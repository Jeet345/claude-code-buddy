#!/usr/bin/env python3
"""Phase 2 tests: the price table, the tailing reader, and the acceptance criteria.

    python3 test_transcript.py

No display, no network, no Claude Code running. The synthetic-transcript tests
build JSONL by hand so a line shape can be made deliberately hostile; the
acceptance tests at the bottom run against the real transcripts on this machine
when there are any, and skip cleanly when there are not.
"""

import json
import os
import random
import shutil
import string
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import prices                                            # noqa: E402
from transcript import TranscriptReader, read            # noqa: E402

PASS = FAIL = 0
REAL = sorted((Path.home() / ".claude" / "projects").glob("*/*.jsonl"))


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {name}\n       got  {got!r}\n       want {want!r}")


def ok(name, cond):
    check(name, bool(cond), True)


def near(name, got, want, tol):
    global PASS, FAIL
    if abs(got - want) <= tol:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {name}: {got!r} not within {tol} of {want!r}")


def section(title):
    print(f"\n{title}")


# ----------------------------------------------------------------- fixtures
def assistant(model="claude-opus-5", inp=100, read_=0, create=0, out=50,
              ttl="1h", speed="standard", effort="high", sidechain=False):
    return {
        "type": "assistant",
        "isSidechain": sidechain,
        "effort": effort,
        "message": {
            "model": model,
            "role": "assistant",
            "usage": {
                "input_tokens": inp,
                "cache_read_input_tokens": read_,
                "cache_creation_input_tokens": create,
                "output_tokens": out,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": create if ttl == "5m" else 0,
                    "ephemeral_1h_input_tokens": create if ttl == "1h" else 0,
                },
                "speed": speed,
                "service_tier": "standard",
            },
        },
    }


def compaction(pre, post):
    return {"type": "system", "subtype": "compact_boundary",
            "compactMetadata": {"trigger": "manual",
                                "preTokens": pre, "postTokens": post}}


def write(path, objs, mode="w"):
    with open(path, mode) as fh:
        for o in objs:
            fh.write(json.dumps(o) + "\n" if isinstance(o, dict) else o)


# ------------------------------------------------------------------- prices
section("prices: table and formatting")
check("opus 5 input rate", prices.table("claude-opus-5")["in"], 5.00)
check("fast mode is a different rate",
      prices.table("claude-opus-5", "fast")["in"], 10.00)
check("fast mode only where it exists",
      prices.table("claude-sonnet-5", "fast")["in"], 3.00)
check("unknown model falls back", prices.table("claude-9")["in"], DEFAULT_IN := 5.00)
ok("unknown model is flagged", not prices.known("claude-9"))
ok("known model is not", prices.known("claude-sonnet-5"))

# The one arithmetic check that is not self-referential: work it out by hand.
#   1000 in @ $5/M = $0.005      2000 out @ $25/M = $0.050
#   40000 cache read @ $0.50/M = $0.020
#   8000 cache create 1h @ $10/M = $0.080          total $0.155
u = assistant(inp=1000, read_=40000, create=8000, out=2000)["message"]["usage"]
near("cost is worked out by hand", prices.cost(u, "claude-opus-5"), 0.155, 1e-9)
u5 = assistant(inp=1000, read_=40000, create=8000, out=2000, ttl="5m")["message"]["usage"]
near("5m cache writes cost less than 1h",
     prices.cost(u5, "claude-opus-5"), 0.155 - 0.080 + 0.050, 1e-9)

check("junk usage is free", prices.cost(None, "claude-opus-5"), 0.0)
check("junk fields are free",
      prices.cost({"input_tokens": "lots", "output_tokens": None}, "claude-opus-5"), 0.0)
check("negative tokens cannot make money",
      prices.cost({"input_tokens": -1e9}, "claude-opus-5"), 0.0)

check("money keeps the tilde", prices.money(3.414), "~$3.41")
check("money floors at a cent", prices.money(0.0001), "<$0.01")
check("free is free, not <$0.01", prices.money(0), "~$0.00")
check("tokens abbreviate", (prices.tokens(842), prices.tokens(3500),
                            prices.tokens(394_000), prices.tokens(3_140_000)),
      ("842", "3.5k", "394k", "3.14M"))

section("prices: the context window is a guess and says so")
check("known model, in range", prices.window("claude-opus-5", 394_000), (1_000_000, False))
check("unknown model is approximate", prices.window("claude-9", 10)[1], True)
check("a reading past the table promotes a tier",
      prices.window("claude-haiku-4-5", 350_000), (500_000, True))
ok("a reading past the whole ladder still returns something sane",
   prices.window("claude-opus-5", 5_000_000)[0] > 5_000_000)
check("synthetic never reaches the table", prices.window("<synthetic>", 0)[1], True)
ok("<synthetic> is recognised", prices.synthetic("<synthetic>"))
ok("empty model is treated as synthetic", prices.synthetic(""))
ok("a real model is not", not prices.synthetic("claude-opus-5"))
ok("the table has a checked date this decade", prices.CHECKED.startswith("202"))

# ---------------------------------------------------------------- transcript
tmp = Path(tempfile.mkdtemp(prefix="deckguy-t2-"))
try:
    section("transcript: reading, tailing, and not breaking")
    p = tmp / "a.jsonl"
    write(p, [assistant(inp=1000, read_=40000, create=8000, out=2000)])
    m = read(p)
    check("context is the latest turn's input", m.context, 49000)
    check("model is picked up", m.model, "claude-opus-5")
    check("effort is picked up", m.effort, "high")
    check("window is the table's", m.window, 1_000_000)
    near("cost matches the hand calculation", m.cost, 0.155, 1e-9)
    check("turns counted", m.turns, 1)
    ok("not partial on a small file", not m.partial)
    near("fill is context over window", m.fill, 0.049, 1e-6)

    r = TranscriptReader(p)
    r.poll()
    check("a second poll with nothing new returns None", r.poll(), None)
    write(p, [assistant(inp=10, read_=90000, create=0, out=100)], mode="a")
    m2 = r.poll()
    ok("an appended line is picked up", m2 is not None)
    check("context follows the newest turn", m2.context, 90010)
    check("totals accumulate", m2.turns, 2)
    check("only the new bytes were read", r.offset, os.path.getsize(p))

    section("transcript: hostile input is skipped, not raised")
    p = tmp / "junk.jsonl"
    write(p, [assistant(inp=500, out=10)])
    with open(p, "a") as fh:
        fh.write("this is not json at all\n")
        fh.write('{"type": "assistant", "message": null}\n')
        fh.write('{"type": "assistant", "message": {"usage": "a string"}}\n')
        fh.write('[1, 2, 3]\n')
        fh.write('{"type": "assistant", "message": {"model": 7, "usage": {'
                 '"input_tokens": {"nested": "nonsense"}}}}\n')
        fh.write("\n")
        fh.write('{"type": "brand_new_line_type_from_the_future"}\n')
        fh.write('{"type": "assistant", "message": {"model": "claude-opus-5",'
                 ' "usage": {"input_tokens": 700, "output_tokens": 1}}}\n')
    m = read(p)
    ok("garbage did not raise", m is not None)
    check("the good line after the garbage still counted", m.context, 700)
    ok("bad lines were tallied", m.bad >= 3)

    p = tmp / "empty.jsonl"
    p.write_text("")
    m = read(p)
    ok("an empty file reads as nothing", m is not None and not m.ok)
    ok("a missing file is None, not an exception", read(tmp / "nope.jsonl") is None)
    ok("an empty path is None", read("") is None)

    section("transcript: a line split across two reads")
    p = tmp / "split.jsonl"
    blob = json.dumps(assistant(inp=1234, out=5))
    p.write_text(blob[:20])                       # half a line on disk
    r = TranscriptReader(p)
    r.poll()
    check("half a line counts as nothing yet", r.m.context, 0)
    check("half a line is not garbage either", r.m.bad, 0)
    with open(p, "a") as fh:
        fh.write(blob[20:] + "\n")
    r.poll()
    check("the line lands once it is whole", r.m.context, 1234)

    section("transcript: subagents and synthetic messages")
    p = tmp / "side.jsonl"
    write(p, [assistant(inp=1000, out=100),
              assistant(inp=500_000, out=9, sidechain=True),
              assistant(model="<synthetic>", inp=0, out=0)])
    m = read(p)
    check("a subagent turn does not move the fill bar", m.context, 1000)
    check("but it does count toward burn", m.input, 501_000)
    check("<synthetic> does not become the model", m.model, "claude-opus-5")
    ok("<synthetic> did not blank the reading", m.ok)

    section("transcript: truncation and reuse")
    p = tmp / "trunc.jsonl"
    write(p, [assistant(inp=9000, out=1)])
    r = TranscriptReader(p)
    r.poll()
    write(p, [assistant(inp=42, out=1)])          # rewritten shorter
    m = r.poll()
    check("a shrunken file is re-read from the top", m.context, 42)
    check("and its totals reset", m.turns, 1)

    section("transcript: compaction is visible")
    p = tmp / "compact.jsonl"
    write(p, [assistant(inp=10, read_=233_661, out=1),
              compaction(233_671, 15_022),
              assistant(inp=15_000, read_=22, out=1)])
    m = read(p)
    check("the boundary is captured", m.compacted, (233_671, 15_022))
    ok("the reading after a compaction is far below the reading before",
       m.context < 20_000)

    r = TranscriptReader(tmp / "live.jsonl")
    write(tmp / "live.jsonl", [assistant(inp=10, read_=233_661, out=1)])
    before = r.poll().context
    write(tmp / "live.jsonl", [compaction(233_671, 15_022),
                               assistant(inp=15_000, read_=22, out=1)], mode="a")
    after = r.poll().context
    near("the bar reads roughly preTokens just before", before, 233_671, 100)
    near("and roughly postTokens just after", after, 15_022, 100)

    section("transcript: a poll drains to EOF, not to one chunk")
    # Regression: a file bigger than CHUNK but smaller than FULL_READ used to
    # stop after the first chunk, so the context reading came from the middle
    # of the session. Wrong, and plausible enough to go unnoticed.
    import transcript as T
    p = tmp / "multichunk.jsonl"
    mid = assistant(inp=111, out=1)
    mid["pad"] = "x" * 3000
    with open(p, "w") as fh:
        while fh.tell() < T.CHUNK * 2.5:
            fh.write(json.dumps(mid) + "\n")
        fh.write(json.dumps(assistant(inp=98765, read_=1, out=1)) + "\n")
    size = os.path.getsize(p)
    ok(f"the fixture spans several chunks ({size // 1024}KB)", size > T.CHUNK * 2)
    ok("and is small enough to be read whole", size < T.FULL_READ)
    m = read(p)
    check("the reading comes from the last line, not the first chunk",
          m.context, 98766)
    ok("the whole file was read", not m.partial)
    check("nothing was skipped", m.bad, 0)

    section("transcript: a big file is bounded, not read whole")
    p = tmp / "big.jsonl"
    filler = assistant(inp=1, read_=1, out=1)
    filler["pad"] = "".join(random.choice(string.ascii_letters) for _ in range(4000))
    line = json.dumps(filler) + "\n"
    target = 50 * 1024 * 1024
    with open(p, "w") as fh:
        for _ in range(target // len(line)):
            fh.write(line)
        fh.write(json.dumps(assistant(inp=777, read_=333, out=9)) + "\n")
    size = os.path.getsize(p)
    ok("the fixture really is ~50MB", size > 45 * 1024 * 1024)

    r = TranscriptReader(p)
    t0 = time.time()
    m = r.poll()
    first = time.time() - t0
    check("the latest turn is still found", m.context, 1110)
    ok("the first read is flagged partial", m.partial)
    ok(f"the first read is bounded ({first * 1000:.0f}ms)", first < 1.0)
    ok(f"it pulled {r.bytes_read // 1024}KB off a {size // 1024 // 1024}MB file",
       r.bytes_read <= 300 * 1024)
    ok("and it is sitting at the end, ready to tail", size - r.offset < len(line))

    t0 = time.time()
    r.poll()
    ok(f"a poll with nothing new is free ({(time.time() - t0) * 1000:.2f}ms)",
       time.time() - t0 < 0.01)

    with open(p, "a") as fh:
        fh.write(json.dumps(assistant(inp=1, read_=1, out=1)) + "\n")
    t0 = time.time()
    m = r.poll()
    ok(f"a later read is offset-based and cheap ({(time.time() - t0) * 1000:.2f}ms)",
       time.time() - t0 < 0.05)
    check("and picks up the appended line", m.context, 2)

    section("transcript: deliberate garbage appended to a copy of a real one")
    if REAL:
        src = max(REAL, key=lambda q: q.stat().st_size)
        dst = tmp / "real-plus-garbage.jsonl"
        shutil.copy(src, dst)
        with open(dst, "a") as fh:
            fh.write("}{ not json\n")
            fh.write('{"type":"assistant","message":{"usage":[]}}\n')
            fh.write("\x00\x01\x02 binary noise\n")
            fh.write('{"type":"assistant","message":{"model":"claude-opus-5",'
                     '"usage":{"input_tokens":123,"output_tokens":4}}}\n')
        m = read(dst)
        ok("a real transcript plus garbage still reads", m is not None and m.ok)
        check("the clean line after the garbage wins", m.context, 123)
        ok("the garbage was counted", m.bad >= 3)
    else:
        print("  (no real transcripts on this machine - skipped)")

finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ------------------------------------------------------- acceptance criteria
section("acceptance: against the real transcripts on this machine")
if not REAL:
    print("  (none found - skipped)")
else:
    checked = compactions = 0
    for path in REAL:
        m = read(path)
        if m is None:
            continue
        checked += 1
        ok(f"{path.name[:8]} reads without raising", True)
        ok(f"{path.name[:8]} fill is a proportion", 0.0 <= m.fill <= 1.0)
        ok(f"{path.name[:8]} cost is not negative", m.cost >= 0)
        ok(f"{path.name[:8]} no lines were skipped", m.bad == 0)
        if m.compacted:
            compactions += 1
            pre, post = m.compacted
            ok(f"{path.name[:8]} compaction dropped the context", post < pre)
    ok(f"read {checked} real transcripts", checked > 0)
    print(f"  ({checked} transcripts, {compactions} with a compaction boundary)")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
