#!/usr/bin/env python3
"""Roaming: floor wraps, wall and ceiling laps, work interrupts, GTK placement.

The Creature only, no window: what is checked is the walk itself - that a
stroll crosses the edge as one continuous body, never ends half off screen, and
that work arriving mid-crossing walks him back on rather than snapping him.

    python3 test_roam.py
"""

import os
import math
import sys
import tempfile
import time

os.environ["DECK_GUY_HOME"] = tempfile.mkdtemp(prefix="deckguy-roam-")
os.environ.setdefault("GDK_BACKEND", "x11")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import guy  # noqa: E402
import vector  # noqa: E402
from roam import Border  # noqa: E402

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
    c.roam = "floor"
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
for _ in range(600):
    now = run(c, 1 / 30, now)
    if c.mode == "idle":
        break
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


# ------------------------------------------------------------- 4. whole border
def border_creature(x=500):
    c = creature(x)
    c.roam = "border"
    c.border = Border(W, 700, c.sp.w, c.sp.h)
    return c


for scale in guy.SIZES.values():
    for width, height in ((1000, 700), (1920, 1048), (800, 600)):
        b = Border(width, height, 40 * scale, 32 * scale)
        poses = [b.pose(b.length * i / 1200) for i in range(1201)]
        fits = True
        for x, y, angle, edge in poses:
            hx = (abs(math.cos(angle)) * b.body_w + abs(math.sin(angle)) * b.body_h) / 2
            hy = (abs(math.sin(angle)) * b.body_w + abs(math.cos(angle)) * b.body_h) / 2
            fits &= x - hx >= -1e-6 and x + hx <= width + 1e-6
            fits &= y - hy >= -1e-6 and y + hy <= height + 1e-6
        ok(f"rotated body stays on screen: scale {scale}, {width}x{height}", fits)
        ok("a lap visits all four edges", {p[3] for p in poses} == {"floor", "right", "ceiling", "left"})
        ok("a lap closes without a position jump", math.dist(poses[0][:2], poses[-1][:2]) < 1e-6)
        for distance in (0, b.horizontal, b.horizontal + b.arc,
                         b.horizontal + b.arc + b.vertical,
                         b.horizontal + 2 * b.arc + b.vertical,
                         2 * b.horizontal + 2 * b.arc + b.vertical,
                         2 * b.horizontal + 3 * b.arc + b.vertical,
                         2 * b.horizontal + 3 * b.arc + 2 * b.vertical):
            before, after = b.pose(distance - 1e-5), b.pose(distance + 1e-5)
            da = (after[2] - before[2] + math.pi) % (2 * math.pi) - math.pi
            ok("corner position and angle are continuous",
               math.dist(before[:2], after[:2]) < 1e-3 and abs(da) < 1e-3)

for direction in (-1, 1):
    c = border_creature()
    now = time.time()
    c.start_patrol(now, 200, direction=direction, stroll=True)
    visited = set()
    for _ in range(int(c.border.length / guy.BORDER_SPEED * 60) + 2):
        now += 1 / 60
        c.update(1 / 60, now)
        visited.add(c.border.pose(c.border_s)[3])
    ok(f"a moving buddy completes a lap in direction {direction}",
       visited == {"floor", "right", "ceiling", "left"})
    ok("border motion never draws a floor wrap copy", c.ghost_dx() == 0)

c = border_creature(x=90)
now = time.time()
before = c.x
c.start_patrol(now, 10, direction=-1, stroll=True)
ok("starting near a corner does not teleport", c.x == before)
now = run(c, 3, now)
ok("he walks onto the border from a corner home", c.border_s is not None)

for edge, distance in (("right", 1), ("ceiling", 2), ("left", 3)):
    c = border_creature()
    b = c.border
    ds = (b.horizontal + b.arc + b.vertical / 2,
          b.horizontal * 1.5 + b.vertical + 2 * b.arc,
          2 * b.horizontal + 1.5 * b.vertical + 3 * b.arc)
    c.border_s = ds[distance - 1]
    c.pose_on_border()
    start_y = c.border_y
    c.set_mode("working")
    c.set_costume("build")
    ok(f"work on {edge} starts a fall without teleporting", c.border_y == start_y and c._dropping)
    ok("work costume waits for landing", c.anim == "idle")
    now = run(c, 2, time.time())
    ok("landing keeps the requested work and outfit", c.mode == "working" and c.anim == "build")
    ok("landing is upright and fully on the floor", c.border_y is None and c.angle == 0 and c.min_x <= c.x <= c.max_x)

c = border_creature()
b = c.border
c.border_s = b.horizontal + b.arc + b.vertical / 2
c.pose_on_border()
now = time.time()
c.start_patrol(now, 0.01, stroll=True)
now = run(c, 0.1, now)
ok("a stroll can pause on the wall", c.mode == "idle" and c.border_s is not None)
rig = vector.Rig(seed=1)
c.start_patrol(now, 10, stroll=True)
for _ in range(60):
    now += 1 / 60
    c.update(1 / 60, now)
    rig.update(1 / 60, c)
ok("wall walking drives the feet despite constant screen x", abs(rig._vx) > 40)
c.roam = "floor"
now = run(c, 2, now)
ok("disabling border roaming returns him to the floor", c.border_y is None and c.border_s is None)

for scale in (3, 6):
    c = guy.Creature(vector.Sheet(scale), 500, idle_secs=10000)
    c.wrap_w, c.roam = W, "border"
    c.set_limits(c.sp.w / 2 + 4, W - c.sp.w / 2 - 4)
    c.border = Border(W, 700, c.sp.w, c.sp.h)
    for i in range(32):
        c.border_s = c.border.length * i / 32
        c.pose_on_border()
        c._dropping = False
        c.mode = "idle"
        c.set_mode("working")
        now = time.time()
        fits = True
        for _ in range(120):
            now += 1 / 60
            c.update(1 / 60, now)
            x, y, w, h = c.bbox(694)
            fits &= x >= 0 and y >= 0 and x + w <= W and y + h <= 700
        ok(f"fall from border sample {i} at scale {scale} stays inside the screen",
           fits and not c._dropping)

# Full-screen probes must not invent a state when X11 cannot answer.
from unittest.mock import patch
from types import SimpleNamespace
import windows


class FakeWnckWindow:
    def __init__(self, xid, full):
        self.xid, self.full = xid, full

    def get_xid(self):
        return self.xid

    def is_fullscreen(self):
        return self.full


for active, expected in ((FakeWnckWindow(0x123, True), 0x123),
                         (FakeWnckWindow(0x123, False), 0), (None, 0)):
    fake = SimpleNamespace(get_active_window=lambda a=active: a)
    with patch.object(windows, "_wnck_screen", return_value=fake):
        ok("full-screen probe reads libwnck state", windows.fullscreen_window() == expected,
           windows.fullscreen_window())
with patch.object(windows, "_wnck_screen", side_effect=RuntimeError("no X")):
    ok("unavailable full-screen probe degrades quietly", windows.fullscreen_window() is None)


def trick_creature(edge="floor", scale=4):
    c = guy.Creature(vector.Sheet(scale), 150, idle_secs=10000)
    c.wrap_w, c.roam = W, "border"
    c.set_limits(c.sp.w / 2 + 4, W - c.sp.w / 2 - 4)
    c.border = Border(W, 700, c.sp.w, c.sp.h)
    b = c.border
    positions = {"floor": b.horizontal / 2,
                 "right": b.horizontal + b.arc + b.vertical / 2,
                 "ceiling": 1.5 * b.horizontal + b.vertical + 2 * b.arc,
                 "left": 2 * b.horizontal + 1.5 * b.vertical + 3 * b.arc}
    c.border_s = positions[edge]
    c.pose_on_border()
    c._next_decision = time.time() + 1000
    return c


for side in (-1, 1):
    c = trick_creature()
    c.x = 150 if side < 0 else W - 150
    c.border_s = c.border_y = None
    now = time.time()
    ok("a floor buddy can peek", c.start_roam_action("peek", now))
    saw_hold, saw_return, half_visible = False, False, True
    for _ in range(1200):
        now += 1 / 60
        c.update(1 / 60, now)
        if c.roam_phase == "hold":
            saw_hold = True
            half_visible &= c.x == (0 if side < 0 else W) and c.ghost_dx() == 0
        saw_return |= c.roam_phase == "return"
        if not c.roam_action:
            break
    ok("peeking pauses, then walks fully back in", saw_hold and saw_return
       and c.min_x <= c.x <= c.max_x and c.mode == "idle")
    ok("a peek exposes exactly half the body on only one edge", saw_hold and half_visible)

for edge, action in (("floor", "peek"), ("right", "slide"), ("left", "slide"), ("ceiling", "drop")):
    for delay in (0.0, 0.2, 0.7):
        c = trick_creature(edge)
        now = time.time()
        c.start_roam_action(action, now)
        now = run(c, delay, now)
        c.set_mode("working")
        c.set_costume("write")
        now = run(c, 3, now)
        ok(f"work interrupts {action} at {delay}s without losing the requested outfit",
           c.mode == "working" and c.anim == "write" and c.roam_action is None
           and not c._dropping and c.border_y is None and c.min_x <= c.x <= c.max_x)
    c = trick_creature(edge)
    c.start_roam_action(action, time.time())
    c.set_mode("sleep")
    now = run(c, 3, time.time())
    ok("sleep cancels an active trick without stranding him", c.mode == "sleep" and not c.roam_action)
    c = trick_creature(edge)
    c.start_roam_action(action, time.time())
    c.cue("wave")
    now = run(c, 2, time.time())
    ok("a click wave cancels the trick and settles normally", c.mode == "idle" and not c.roam_action)

for edge in ("right", "left"):
    for scale in (3, 6):
        c = trick_creature(edge, scale)
        now = time.time()
        start_y = c.border_y
        c.start_roam_action("slide", now)
        rig = vector.Rig(seed=1)
        fits = True
        for _ in range(1200):
            now += 1 / 60
            c.update(1 / 60, now)
            rig.update(1 / 60, c)
            x, y, w, h = c.bbox(694)
            fits &= x >= 0 and y >= 0 and x + w <= W and y + h <= 700
            if not c.roam_action:
                break
        ok("wall slides roll through the corner and stop upright on the floor",
           fits and not c.roam_action and c.mode == "idle" and c.angle == 0
           and c.border_y > start_y and c.border.pose(c.border_s)[3] == "floor")
        ok("a wall slide produces one landing with dust", c.landings == 1
           and any(p[6] == "dust" for p in rig.particles))
        rig.update(1 / 60, c)
        ok("landing dust is not emitted again every frame", len(rig.particles) == 6)

for side in (-1, 1):
    for delay in (3.0, 5.0):
        c = trick_creature()
        c.x = 100 if side < 0 else W - 100
        c.border_s = c.border_y = None
        now = time.time()
        c.start_roam_action("peek", now)
        now = run(c, delay, now)
        c.set_mode("working")
        c.set_costume("build")
        smooth = True
        for _ in range(90):
            before = c.x
            now += 1 / 30
            c.update(1 / 30, now)
            smooth &= abs(c.x - before) <= guy.SETTLE_SPEED / 30 + 1e-6
        ok("work during a peek hold or return walks him fully back without a snap",
           smooth and c.min_x <= c.x <= c.max_x and c.anim == "build" and not c.roam_action)

c = trick_creature("ceiling")
now = time.time()
c.start_roam_action("drop", now)
before = c.x, c.border_y, c.angle
now = run(c, 0.3, now)
ok("a ceiling drop has an anticipation beat", before == (c.x, c.border_y, c.angle) and not c._dropping)
rig = vector.Rig(seed=1)
for _ in range(120):
    now += 1 / 60
    c.update(1 / 60, now)
    rig.update(1 / 60, c)
    if c.landings:
        break
ok("a deliberate ceiling drop lands with a squash and dust", c.landings == 1 and c.border_y is None
   and rig.p["stretch"].v < 0 and any(p[6] == "dust" for p in rig.particles))

c = trick_creature("ceiling")
c.set_mode("sleep")
before = c.x, c.border_y, c.border_s
rig = vector.Rig(seed=1)
for _ in range(90):
    c.update(1 / 30, time.time())
    rig.update(1 / 30, c)
ok("a bat nap stays attached to the ceiling", c.hanging_sleep and before == (c.x, c.border_y, c.border_s))
ok("hanging sleep sways gently without bobbing away from the ceiling",
   abs(rig.offsets["tilt"]) <= 1.5 and rig.offsets["bob"] == 0)
c.set_mode("working")
c.set_costume("build")
now = run(c, 3, time.time())
ok("work wakes a bat nap and dresses him on the floor", c.mode == "working" and c.anim == "build"
   and not c.hanging_sleep and c.border_y is None)
for hanging in (False, True):
    first = guy.roam.sleep_trail((400, 200, 160, 128), (32, 32), 0.5, hanging, (1000, 700))[0]
    later = guy.roam.sleep_trail((400, 200, 160, 128), (32, 32), 0.7, hanging, (1000, 700))[0]
    ok("zzz drifts down for bat naps and up for floor naps", (later[1] > first[1]) == hanging)
    trail = guy.roam.sleep_trail((900, 6, 100, 128), (32, 32), 2.3, hanging, (1000, 700))
    ok("sleep effects stay on screen beside corners", all(x >= 0 and y >= 0 and x + 32 * s <= W
       and y + 32 * s <= 700 for x, y, s, a in trail))
for roam_mode in ("off", "floor"):
    c = trick_creature("ceiling")
    c.roam = roam_mode
    ok("border tricks respect the roam setting", not c.start_roam_action("drop", time.time()))
for edge, expected in (("floor", "peek"), ("right", "slide"), ("left", "slide"), ("ceiling", "drop")):
    c = trick_creature(edge)
    if edge == "floor":
        c.x, c.border_s, c.border_y = 150, None, None
    c._next_decision = 0
    c._next_blink = time.time() + 100
    with patch.object(guy.random, "random", return_value=0.0):
        c._update_idle(1 / 60, time.time())
    ok("idle decisions occasionally choose the trick for the current edge", c.roam_action == expected)
c = trick_creature("ceiling")
c.return_to_floor()
c.start_patrol(time.time(), 10, stroll=True)
ok("another stroll cannot take over a falling buddy", c.mode == "idle" and not c._stroll)

# Drawing, pointer geometry and mode switches through the actual GTK window.
for width, height in ((1000, 700), (1920, 1048)):
    b = Border(width, height, 160, 128)
    for i in range(80):
        distance = b.length * i / 80
        x, y, angle, edge = b.pose(distance)
        projected = b.pose(b.nearest(x, y))
        ok("projection preserves points on straights and rounded corners",
           math.dist((x, y), projected[:2]) < 0.01)
        new = Border(width + 400, height + 200, 240, 192)
        location = b.location(distance)
        restored = new.location(new.distance(*location))
        ok("saved edge position survives screen and body size changes",
           location[0] == restored[0] and abs(location[1] - restored[1]) < 1e-8)
    for x, y, edge in ((width / 2, height - 5, "floor"), (5, height / 2, "left"),
                       (width - 5, height / 2, "right"), (width / 2, 5, "ceiling")):
        ok("free positions snap to the nearest edge", b.pose(b.nearest(x, y))[3] == edge)

# Nobody naps sideways: on a wall he slides down (or drops) before sleeping.
for edge in ("left", "right"):
    c = trick_creature(edge)
    c.idle_secs = 1
    c.last_activity = time.time() - 10
    now = time.time()
    c.update(1 / 30, now)
    ok(f"sleep due on the {edge} wall starts a slide, not a sideways nap",
       c.mode != "sleep" and (c.roam_action == "slide" or c.falling), (c.mode, c.roam_action))
    now = run(c, 12, now)
    ok(f"and once down from the {edge} wall he naps on the floor",
       c.mode == "sleep" and not c.on_wall and math.cos(c.angle) > 0.9, (c.mode, c.angle))
for edge in ("floor", "ceiling"):
    c = trick_creature(edge)
    c.idle_secs = 1
    c.last_activity = time.time() - 10
    c.update(1 / 30, time.time())
    ok(f"on the {edge} he simply falls asleep", c.mode == "sleep", c.mode)
ok("the ceiling nap hangs by the feet", c.hanging_sleep)

# Letting go: near an edge he grabs it, out in the open he falls.
c = trick_creature()
c.grab(free=True)
c.x, c.border_y = W / 2, 350
c.release(stick=c.sp.h)
ok("released mid-screen he falls instead of snapping to an edge",
   c.falling and c.border_s is None and c.border_y == 350, (c.falling, c.border_y))
now = run(c, 3, time.time())
ok("and lands on the floor", not c.falling and c.border_y is None)
c = trick_creature()
c.grab(free=True)
c.x, c.border_y = W / 2, c.border.top + 20
c.release(stick=c.sp.h)
ok("released close to the ceiling he holds on to it",
   c.border_s is not None and c.border.pose(c.border_s)[3] == "ceiling")


if guy.Gdk.Display.get_default():
    import cairo
    from pathlib import Path
    with patch.object(guy.Deck, "_load_prefs", return_value={"roam": "border", "toast": False}), \
            patch.object(guy, "PREFS", Path(tempfile.mkdtemp()) / "prefs.json"), \
            patch.object(guy, "POS", Path(tempfile.mkdtemp()) / "pos.json"):
        d = guy.Deck(SimpleNamespace(demo=True, scale=4, height=460, idle_secs=10000,
                                    no_shape=False, no_log=True))
        d.hide()
        guy.GLib.source_remove(d._tick_id)
        c, b = d.creature, d.creature.border
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, d.win_w, d.win_h)
        cr = cairo.Context(surf)
        for distance in (b.horizontal / 2, b.horizontal + b.arc + b.vertical / 2,
                         1.5 * b.horizontal + 2 * b.arc + b.vertical,
                         2 * b.horizontal + 3 * b.arc + 1.5 * b.vertical):
            c.border_s = distance
            c.pose_on_border()
            c.mode = "idle"
            d.rig.update(1 / 60, c)
            d.on_draw(None, cr)
            bbox = c.bbox(d.ground_y)
            d._update_input_shape(bbox)
            ok("rotated body hitbox stays on screen", bbox[0] >= 0 and bbox[1] >= 0
               and bbox[0] + bbox[2] <= d.win_w and bbox[1] + bbox[3] <= d.win_h)
            for widget in (d.bubble, d.panel, d.card):
                d.bubble.set("")
                d.panel.set([])
                d.card.set("")
                if widget is d.bubble:
                    widget.set("Hello from the border")
                    d._bubble_until = time.time() + 10
                elif widget is d.panel:
                    widget.set([("project", "border test"), ("step", "idle")])
                else:
                    widget.set("Permission", ["Bash pytest"], [("ask", "In terminal")])
                d.on_draw(None, cr)
                rect = d.readout_rect(time.time())
                ok("edge readout is inside the screen", rect is not None and rect[0] >= 0
                   and rect[1] >= 0 and rect[0] + rect[2] <= d.win_w
                   and rect[1] + rect[3] <= d.win_h)
                edge = b.pose(distance)[3]
                if widget is d.bubble:
                    ok("bubble tail faces its supporting edge", widget.tail ==
                       {"floor": "bottom", "ceiling": "top", "left": "left", "right": "right"}[edge])
                if widget is d.card:
                    bid, button = next(iter(d.card.buttons_at(rect[:2]).items()))
                    bx, by, bw, bh = button
                    ok("card buttons follow the inward card origin", d._card_button(bx + bw / 2, by + bh / 2) == bid)

        for edge, action, label in (("floor", "peek", "Peek at edge"),
                                    ("right", "slide", "Slide down wall"),
                                    ("ceiling", "drop", "Drop from ceiling")):
            d.bubble.set("")
            d.panel.set([])
            d.card.set("")
            c.mode, c._cue, c._cue_into = "idle", None, None
            c._dropping = False
            c.cancel_roam_action()
            c.border_s = c.border.distance(edge, 0.4)
            c.pose_on_border()
            if edge == "floor":
                c.border_s = c.border_y = None
                c.x, c.angle = 100, 0
            labels = []
            with patch.object(guy.Gtk.Menu, "popup_at_pointer",
                              lambda menu, event: labels.extend(it.get_label() for it in menu.get_children())):
                d.menu(SimpleNamespace(button=3, time=0))
            ok("the menu offers a trick appropriate to the current edge", label in labels)
            d.roam_trick(action)
            now = time.time()
            for _ in range(180 if action == "peek" else 30):
                now += 1 / 60
                c.update(1 / 60, now)
                d.update_face(now)
                d.rig.update(1 / 60, c)
            d.on_draw(None, cr)
            d._update_input_shape(c.bbox(d.ground_y))
            if action == "peek":
                data = bytes(surf.get_data())
                stride = surf.get_stride()
                near = sum(data[row * stride + col * 4 + 3]
                           for row in range(d.ground_y - d.sp.h, d.ground_y)
                           for col in range(d.sp.w))
                far = sum(data[row * stride + col * 4 + 3]
                          for row in range(d.ground_y - d.sp.h, d.ground_y)
                          for col in range(d.win_w - d.sp.w, d.win_w))
                ok("a rendered peek stays visible and does not appear on the opposite edge", near > 0 and far == 0)
                point = SimpleNamespace(button=1, x=8, y=d.ground_y-40, x_root=8, y_root=400)
                d.click_focus = False
                d.on_press(None, point)
                d.on_release(None, point)
                ok("the visible half of a peeking buddy still responds to clicks", c.mode == "cue" and not c.roam_action)
        c.cancel_roam_action()
        c._dropping = False
        c.border_s = c.border.distance("ceiling", 0.4)
        c.pose_on_border()
        c.set_mode("sleep")
        d.rig.update(1 / 30, c)
        d.on_draw(None, cr)
        ok("the overlay renders a hanging nap", c.hanging_sleep)

        def drag_to(x, y, work=False, asleep=False):
            d.bubble.set("")
            d.panel.set([])
            d.card.set("")
            c.mode, c._dropping = "idle", False
            c.y_off = 0
            c.last_activity = time.time()
            if asleep:
                c.set_mode("sleep")
            bx, by, bw, bh = c.body_bbox(d.ground_y)
            cx, cy = bx + bw / 2, by + bh / 2
            start = SimpleNamespace(button=1, x_root=1000, y_root=1000, x=cx, y=cy)
            end = SimpleNamespace(button=1, x_root=1000 + x - cx, y_root=1000 + y - cy, x=x, y=y)
            d.on_press(None, start)
            d.on_motion(None, end)
            held = c.x, c.border_y
            if work:
                c.set_mode("working")
                c.set_costume("build")
            c.update(1 / 30, time.time() + 1 / 30)
            ok("held buddy stays at the dragged position while events arrive",
               c.dragging and held == (c.x, c.border_y))
            d.on_release(None, end)
            return held

        c.mode = "idle"
        c.border_s = c.border.floor_distance(d.win_w / 2)
        c.pose_on_border()
        c.start_roam_action("peek", time.time())
        drag_to(d.win_w / 2, 70)
        ok("dragging cancels an active trick and sticks to the selected edge",
           not c.roam_action and c.border_s is not None and c.border.pose(c.border_s)[3] == "ceiling")
        c.border_s = c.border.floor_distance(d.win_w / 2)
        c.pose_on_border()
        for x, y, edge in ((d.win_w / 2, 70, "ceiling"), (70, d.win_h / 2, "left"),
                           (d.win_w - 70, d.win_h / 2, "right"),
                           (d.win_w / 2, d.win_h - 70, "floor")):
            drag_to(x, y)
            ok("releasing a two-axis drag sticks to the nearest edge", c.border.pose(c.border_s)[3] == edge)
            saved = c.border.location(c.border_s)
            restored = guy.Deck(SimpleNamespace(demo=True, scale=4, height=460, idle_secs=10000,
                                               no_shape=True, no_log=True))
            restored.hide()
            guy.GLib.source_remove(restored._tick_id)
            rc = restored.creature
            ok("a new daemon restores the dragged edge and position",
               rc.border_s is not None and rc.border.location(rc.border_s)[0] == saved[0]
               and abs(rc.border.location(rc.border_s)[1] - saved[1]) < 1e-7)
            d.set_scale(6)
            ok("changing size preserves edge and position",
               c.border.location(c.border_s)[0] == saved[0]
               and abs(c.border.location(c.border_s)[1] - saved[1]) < 1e-7)
            d.set_scale(4)
        drag_to(d.win_w / 2, 70, asleep=True)
        now = run(c, 1.5, time.time())
        ok("dragging a sleeping buddy wakes him on the selected edge",
           c.mode == "idle" and c.border_s is not None and c.border.pose(c.border_s)[3] == "ceiling")
        d.click_focus = False
        before = (c.x, c.border_y, c.border_s)
        bx, by, bw, bh = c.body_bbox(d.ground_y)
        press = SimpleNamespace(button=1, x_root=1000, y_root=1000, x=bx+bw/2, y=by+bh/2)
        jitter = SimpleNamespace(button=1, x_root=1001, y_root=1001, x=bx+bw/2+1, y=by+bh/2+1)
        d.on_press(None, press)
        d.on_motion(None, jitter)
        d.on_release(None, jitter)
        ok("pointer jitter stays a click and does not move him", before == (c.x, c.border_y, c.border_s))
        drag_to(d.win_w / 2 + 100, 70, work=True)
        now = run(c, 3, time.time())
        ok("work during a drag survives the release and returns to the floor",
           c.mode == "working" and c.anim == "build" and c.border_s is None and not c.dragging and not c._dropping)
        c.set_mode("idle")
        bx, by, bw, bh = c.body_bbox(d.ground_y)
        press = SimpleNamespace(button=1, x_root=1000, y_root=1000, x=bx+bw/2, y=by+bh/2)
        move = SimpleNamespace(button=1, x_root=1100, y_root=900, x=bx+bw/2+100, y=by+bh/2-100)
        d.on_press(None, press)
        d.on_motion(None, move)
        d.set_roam("floor")
        now = run(c, 2, time.time())
        ok("changing roam mode during a drag cannot leave him held",
           d._drag is None and not c.dragging and c.border_y is None)
        d.set_roam("border")
        # Old position files and corrupt/new position fields must be harmless.
        for text in ('{"home_x": 123}', '{"home_x": "bad", "edge": "left", "position": "bad"}',
                     '{"home_x": 123, "edge": "nowhere", "position": 0.5}',
                     '{"home_x": 123, "edge": "left", "position": NaN}', '[]', 'broken'):
            guy.POS.write_text(text)
            c.border_s = None
            d._restore_border_home()
            ok("old or invalid saved positions fall back to the floor", c.border_s is None)
        # Mode switches must preserve a valid floor pose even during a fall.
        for target in ("floor", "border", "off", "border"):
            d.set_roam(target)
            now = run(c, 2, time.time())
            ok("roam mode switch remains drivable", c.border_s is None and not c._dropping)
        # Finish an asynchronous guard probe through the real GLib main context.
        d.args.demo = False
        d.approvals.enabled = True
        d.approvals.focused = lambda sid: False
        d.approvals.ask({"id": "fullscreen-test", "session_id": "test", "tool": "Bash"})
        with patch.object(guy.windows, "fullscreen_window", return_value=0x123), \
                patch.object(d, "_is_session_terminal", return_value=False):
            d.poll_fullscreen(time.time())
        ok("full-screen guard hides the buddy", d._fullscreen_app and not d.get_visible())
        ok("full-screen guard releases held prompts", d.approvals.wait("fullscreen-test", timeout=0)["decision"] == "ask")
        ok("hidden buddy leaves new prompts in the terminal", d._focused("unknown") is True)
        with patch.object(guy.windows, "fullscreen_window", return_value=0):
            d.poll_fullscreen(time.time() + 2)
        ok("full-screen guard restores the buddy", not d._fullscreen_app and d.get_visible())
        with patch.object(guy.windows, "fullscreen_window", return_value=0x456), \
                patch.object(d, "_is_session_terminal", return_value=True):
            d.poll_fullscreen(time.time() + 4)
        ok("a full-screen session terminal keeps him on screen",
           not d._fullscreen_app and d.get_visible())
        calls = []
        with patch.object(guy.windows, "fullscreen_window", side_effect=lambda: calls.append(1) or 0):
            c.mode = "sleep"
            t0 = time.time() + 10
            for i in range(8):
                d.poll_fullscreen(t0 + i * 0.5)
            c.mode = "idle"
        ok("asleep, the full-screen check runs every few seconds", len(calls) == 1, len(calls))
        # A contact shadow under his feet on every edge, none while held.
        shadows = []
        real = guy.vector.draw_prop

        def spy(cr_, key, *a, **kw):
            if key.startswith("shadow"):
                shadows.append(cr_.user_to_device(a[0], a[1]))
            return real(cr_, key, *a, **kw)

        c.mode = "idle"
        with patch.object(guy.vector, "draw_prop", side_effect=spy):
            for edge, dist in (("floor", b.horizontal / 2),
                               ("right", b.horizontal + b.arc + b.vertical / 2),
                               ("ceiling", 1.5 * b.horizontal + 2 * b.arc + b.vertical),
                               ("left", 2 * b.horizontal + 3 * b.arc + 1.5 * b.vertical)):
                shadows.clear()
                c.border_s = dist
                c.pose_on_border()
                d.on_draw(None, cr)
                ok(f"he casts a contact shadow on the {edge}", len(shadows) == 1, shadows)
                if shadows:
                    sx, sy = shadows[0]
                    near = {"floor": d.win_h - sy, "ceiling": sy,
                            "left": sx, "right": d.win_w - sx}[edge]
                    ok(f"and it lies against the {edge} edge", near < 40, (sx, sy))
            c.grab(free=True)
            shadows.clear()
            d.on_draw(None, cr)
            ok("held in the air he casts none", not shadows)
            c.dragging = False
        d.hide()
else:
    print("No display: skipped GTK border drawing and full-screen guard checks")

# ---------------------------------------------------------------------- report
print(f"\n{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print("  FAIL " + f)
sys.exit(1 if FAILED else 0)
