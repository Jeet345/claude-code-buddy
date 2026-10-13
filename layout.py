#!/usr/bin/env python3
"""Where the other sessions stand. Pure geometry: no GTK, no cairo.

The buddy mirrors one session - the one that most wants you. Every other live
session gets a small avatar in a row beside him, and the featured session's
subagents trail behind him as sidekicks, smaller still:

      ° °  (buddy)  ▪ ▪ ▪ +2
      sidekicks      avatars

Both rows go on whichever side has room, prefer opposite sides so they never
overlap, and stay inside the strip. Overflow collapses into a count rather than
running off the screen.

    row = avatar_row(cx=800, body_w=120, ground_y=394, size=(60, 48), n=4, win_w=1920)
    row.rects      [(x, y, w, h), ...] left to right, one per shown avatar
    row.more       how many did not fit
    row.more_at    (x, y) for the "+n" label, or None
    row.side       +1 right of him, -1 left
"""

AVATAR_CAP = 6        # avatars shown before the rest become "+n"
SIDEKICK_CAP = 4
GAP = 8               # px between neighbours
EDGE = 4              # px of strip kept clear at either end
MORE_W = 28           # px reserved for the "+n" label


class Row:
    __slots__ = ("rects", "more", "more_at", "side")

    def __init__(self, rects, more, more_at, side):
        self.rects, self.more, self.more_at, self.side = rects, more, more_at, side

    def __repr__(self):
        return f"<Row {len(self.rects)} side={self.side} more={self.more}>"


def _fits(start, step, count, lo, hi, w):
    """Room for `count` boxes of width `w`, stepping `step` from `start`?"""
    if count <= 0:
        return True
    first, last = start, start + step * (count - 1)
    return min(first, last) >= lo and max(first, last) + w <= hi


def row(cx, body_w, ground_y, size, n, win_w, cap, prefer=1, avoid=0):
    """One row of `n` boxes of `size` beside a body centred on `cx`.

    `prefer` is the side to try first; `avoid` is a side already taken (so the
    avatars and the sidekicks never share one) - it is only used when nothing
    else fits.
    """
    w, h = size
    if n <= 0 or w <= 0:
        return Row([], 0, None, prefer)
    lo, hi = EDGE, win_w - EDGE
    sides = [prefer, -prefer]
    if avoid in sides:
        sides.remove(avoid)
        sides.append(avoid)
    shown = min(n, cap)
    while True:
        for side in sides:
            more = n - shown
            reserve = MORE_W if more else 0
            if side > 0:
                start = cx + body_w / 2 + GAP
                ok = _fits(start, w + GAP, shown, lo, hi - reserve, w)
            else:
                start = cx - body_w / 2 - GAP - w
                ok = _fits(start, -(w + GAP), shown, lo + reserve, hi, w)
            if ok:
                rects = [(int(start + side * (w + GAP) * i), int(ground_y - h), w, h)
                         for i in range(shown)]
                rects.sort()
                more_at = None
                if more:
                    if side > 0:
                        more_at = (rects[-1][0] + w + GAP // 2, int(ground_y - h / 2))
                    else:
                        more_at = (rects[0][0] - MORE_W + GAP // 2, int(ground_y - h / 2))
                return Row(rects, more, more_at, side)
        if shown == 0:
            return Row([], n, None, prefer)
        shown -= 1


def avatar_row(cx, body_w, ground_y, size, n, win_w, cap=AVATAR_CAP):
    return row(cx, body_w, ground_y, size, n, win_w, cap, prefer=1)


def sidekick_row(cx, body_w, ground_y, size, n, win_w, avoid, cap=SIDEKICK_CAP):
    return row(cx, body_w, ground_y, size, n, win_w, cap, prefer=-avoid or -1, avoid=avoid)


def hit(rects, x, y):
    """Index of the rect containing (x, y), or -1."""
    for i, (rx, ry, rw, rh) in enumerate(rects):
        if rx <= x < rx + rw and ry <= y < ry + rh:
            return i
    return -1
