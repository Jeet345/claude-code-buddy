#!/usr/bin/env python3
"""Roaming: strolls that leave home and wrap from one screen edge to the other.

The Creature only, no window: what is checked is the walk itself - that a
stroll crosses the edge as one continuous body, never ends half off screen, and
that work arriving mid-crossing walks him back on rather than snapping him.

    python3 test_roam.py
"""

import os
import sys
import tempfile
import time

os.environ["DECK_GUY_HOME"] = tempfile.mkdtemp(prefix="deckguy-roam-")
os.environ.setdefault("GDK_BACKEND", "x11")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import guy  # noqa: E402
import vector  # noqa: E402

FAILED = []
PASSED = 0


def ok(label, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(f"{label} {detail}".rstrip())


W = 1000


def creature(x=500):
    c = guy.Creature(vector.Sheet(4), x, idle_secs=10_000)
    half = c.sp.w / 2
    c.set_limits(half + guy.EDGE_MARGIN, W - half - guy.EDGE_MARGIN)
    c.wrap_w = W
    c.roam = "border"
    return c


def run(c, secs, now, dt=1 / 30):
    for _ in range(int(secs / dt)):
        now += dt
        c.update(dt, now)
    return now


# ------------------------------------------------------------ 1. the wrap itself
c = creature(x=W - 60)
now = time.time()
c.start_patrol(now, 60.0, direction=1, stroll=True)
xs, ghosts, jumps = [], [], 0
for _ in range(int(8 / (1 / 30))):
    before = c.x
    now += 1 / 30
    c.update(1 / 30, now)
    xs.append(c.x)
    ghosts.append(c.ghost_dx())
    if abs(c.x - before) > guy.PATROL_SPEED:      # more than a second's walk in one tick
        jumps += 1
ok("a stroll walks off the right edge and comes back in on the left",
   any(x < 100 for x in xs[len(xs) // 2:]), xs[-1])
ok("x stays inside the screen width the whole way", all(0 <= x < W for x in xs),
   (min(xs), max(xs)))
ok("while straddling the right edge the copy is drawn one screen to the left",
   -W in ghosts, set(ghosts))
ok("and while straddling the left edge, one screen to the right", W in ghosts, set(ghosts))
ok("the only jump in x is the wrap itself", jumps == 1, jumps)

# going left wraps the other way
c = creature(x=60)
now = time.time()
c.start_patrol(now, 60.0, direction=-1, stroll=True)
now = run(c, 6.0, now)
ok("a stroll to the left re-enters from the right", c.x > W - 300, c.x)


# ------------------------------------------------------------ 2. ending a stroll
c = creature(x=W - 30)
now = time.time()
c.start_patrol(now, 0.01, direction=1, stroll=True)
now = run(c, 0.2, now)
ok("time running out mid-crossing does not stop him there", c.mode == "patrol", c.mode)
now = run(c, 10.0, now)
ok("he walks on until all of him is back on screen, then stops",
   c.mode == "idle" and c.min_x <= c.x <= c.max_x, (c.mode, c.x))
ok("and wherever he ended up is home now", c.home_x == c.x, (c.home_x, c.x))

# work arriving mid-crossing
c = creature(x=W - 10)
now = time.time()
c.start_patrol(now, 60.0, direction=1, stroll=True)
c.set_mode("working")
xs = []
for _ in range(90):
    now += 1 / 30
    c.update(1 / 30, now)
    xs.append(c.x)
ok("work arriving mid-crossing walks him back on, not snaps him",
   max(abs(b - a) for a, b in zip(xs, xs[1:])) <= guy.SETTLE_SPEED / 30 + 1e-6)
ok("and he ends fully on screen", c.min_x <= c.x <= c.max_x, c.x)
ok("with no copy left on the far side", c.ghost_dx() == 0, c.ghost_dx())


# ------------------------------------------------------------ 3. when he strolls
c = creature()
c.roam = "off"
now = time.time()
strolls = 0
for _ in range(200):
    c._next_decision = 0
    c.mode = "idle"
    c._update_idle(0, now)
    strolls += c._stroll
ok("with roaming off he never strolls", strolls == 0, strolls)

c = creature()
strolls = 0
for _ in range(400):
    c._next_decision = 0
    c.mode, c._stroll = "idle", False
    c._update_idle(0, now)
    strolls += c._stroll
ok("with it on, some idle walks are strolls", 20 < strolls < 200, strolls)

c = creature()
c.wrap_w = None
c.start_patrol(now, 5.0, stroll=True)
ok("no screen width yet means no stroll", not c._stroll)


# ---------------------------------------------------------------------- report
print(f"\n{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print("  FAIL " + f)
sys.exit(1 if FAILED else 0)
