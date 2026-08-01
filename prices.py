#!/usr/bin/env python3
"""What a token costs and how many fit - the two numbers nobody publishes in the transcript.

This file is a liability and is meant to look like one. Every number here was
true on CHECKED and will drift silently: no error, no warning, just a cost
readout that quietly stops matching reality. So the panel greys the figure out
once the table is STALE_DAYS old rather than presenting a stale guess in the
same black text as a live one.

The context window is the worse half. It is not in the transcript at all - not
as a field, not in an error message - so `window()` is a table lookup with one
escape hatch: an observed reading larger than the table promotes to the next
tier up and marks itself approximate. A hardcoded number that is too small
reads 190% full and looks like a bug in the bar; this at least degrades to
"bigger than we thought".

    prices.cost(usage, "claude-opus-5")   # -> dollars, approximate
    prices.window("claude-opus-5", 366242) # -> (1_000_000, False)
"""

import time

CHECKED = "2026-08-01"       # when a human last compared this to the pricing page
STALE_DAYS = 90              # after this the panel greys the cost out

# Dollars per million tokens. `write_1h` is the 1-hour cache TTL, which costs
# more to create than the 5-minute one and is what Claude Code actually uses.
#
#   read  = 0.1x input     write_5m = 1.25x input     write_1h = 2x input
#
# Kept as literals rather than multipliers so a future price change that breaks
# those ratios does not need the shape of this file to change.
PRICES = {
    "claude-opus-5":   {"in": 5.00,  "out": 25.00, "read": 0.50, "write_5m": 6.25,  "write_1h": 10.00},
    "claude-opus-4-8": {"in": 5.00,  "out": 25.00, "read": 0.50, "write_5m": 6.25,  "write_1h": 10.00},
    "claude-opus-4-7": {"in": 5.00,  "out": 25.00, "read": 0.50, "write_5m": 6.25,  "write_1h": 10.00},
    "claude-fable-5":  {"in": 10.00, "out": 50.00, "read": 1.00, "write_5m": 12.50, "write_1h": 20.00},
    "claude-sonnet-5": {"in": 3.00,  "out": 15.00, "read": 0.30, "write_5m": 3.75,  "write_1h": 6.00},
    "claude-sonnet-4-6": {"in": 3.00, "out": 15.00, "read": 0.30, "write_5m": 3.75, "write_1h": 6.00},
    "claude-haiku-4-5": {"in": 1.00, "out": 5.00,  "read": 0.10, "write_5m": 1.25,  "write_1h": 2.00},
}

# Fast mode is the same model at a premium, and the transcript tells us which
# was used: `message.usage.speed`.
FAST = {
    "claude-opus-5":   {"in": 10.00, "out": 50.00, "read": 1.00, "write_5m": 12.50, "write_1h": 20.00},
    "claude-opus-4-8": {"in": 10.00, "out": 50.00, "read": 1.00, "write_5m": 12.50, "write_1h": 20.00},
}

DEFAULT = PRICES["claude-opus-5"]      # an unknown model prices as the common one

WINDOWS = {
    "claude-opus-5": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-fable-5": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-haiku-4-5": 200_000,
}
DEFAULT_WINDOW = 200_000               # the smallest thing it could plausibly be
TIERS = (200_000, 500_000, 1_000_000, 2_000_000)


def _num(v):
    """Anything unparseable is zero. A transcript field is never trusted."""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return 0.0
    return n if n == n and n not in (float("inf"), float("-inf")) else 0.0


def synthetic(model):
    """`<synthetic>` is a real model value on locally generated messages.

    It carries all-zero usage and must not reach the price table or the model
    row - it is not a model the user picked, it is Claude Code talking to
    itself.
    """
    return not model or (model.startswith("<") and model.endswith(">"))


def table(model, speed=""):
    if synthetic(model):
        return DEFAULT
    if speed == "fast" and model in FAST:
        return FAST[model]
    return PRICES.get(model, DEFAULT)


def known(model):
    """Whether the cost figure is a lookup or a guess. The panel says so."""
    return not synthetic(model) and model in PRICES


def cost(usage, model="", speed=""):
    """Approximate dollars for one usage block. Never raises, never negative.

    Cache reads and writes price differently enough that lumping them into
    `input` is wrong by an order of magnitude in both directions - a warm turn
    is ten times cheaper than it looks, a cold one twice as expensive.
    """
    if not isinstance(usage, dict):
        return 0.0
    p = table(model, speed)
    creation = usage.get("cache_creation")
    creation = creation if isinstance(creation, dict) else {}

    w5 = _num(creation.get("ephemeral_5m_input_tokens"))
    w1 = _num(creation.get("ephemeral_1h_input_tokens"))
    total_write = _num(usage.get("cache_creation_input_tokens"))
    # The breakdown should sum to the total; when it does not (a shape we have
    # not seen), price the remainder at the cheaper 5m rate rather than losing
    # it or double-counting.
    if w5 + w1 < total_write:
        w5 += total_write - (w5 + w1)

    dollars = (
        _num(usage.get("input_tokens")) * p["in"]
        + _num(usage.get("output_tokens")) * p["out"]
        + _num(usage.get("cache_read_input_tokens")) * p["read"]
        + w5 * p["write_5m"]
        + w1 * p["write_1h"]
    ) / 1_000_000
    return max(0.0, dollars)


def window(model, observed=0):
    """`(size, approximate)` - the context window, and whether we are guessing.

    A reading larger than the table means the table is wrong, not that the
    session is over 100% full. Promote to the next tier and say so; the panel
    renders an approximate window differently so nobody quotes the number.
    """
    size = WINDOWS.get(model, DEFAULT_WINDOW) if not synthetic(model) else DEFAULT_WINDOW
    approx = not known(model)
    observed = _num(observed)
    if observed > size:
        approx = True
        for tier in TIERS:
            if tier > observed:
                size = tier
                break
        else:
            size = int(observed * 1.2)       # off the end of the ladder entirely
    return size, approx


def stale(now=None):
    """True once the table is old enough that the cost should be greyed out."""
    try:
        checked = time.mktime(time.strptime(CHECKED, "%Y-%m-%d"))
    except ValueError:
        return True
    return ((now or time.time()) - checked) > STALE_DAYS * 86400


def money(dollars):
    """`~$3.41`, `~$0.08`, `<$0.01` - always with the tilde. It is an estimate."""
    dollars = max(0.0, _num(dollars))
    if dollars and dollars < 0.01:
        return "<$0.01"
    if dollars < 100:
        return f"~${dollars:.2f}"
    return f"~${dollars:,.0f}"


def tokens(n):
    """`137k`, `1.2M`, `842` - the context bar has no room for digit grouping."""
    n = int(max(0.0, _num(n)))
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.0f}k" if n >= 10_000 else f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.2f}M"
