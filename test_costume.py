#!/usr/bin/env python3
"""Costumes, headphones and the thinking spark, through the real rig.

No window and no display: everything here draws into an image surface. What it
guards against is the failures that would only show on the desktop - a costume
that throws mid-draw and takes the frame with it, a prop that ends up on the
wrong side of him, headphones that never come off, a pancake that leaves the
screen - checked over enough simulated seconds to cover every phase of every
loop.

    python3 test_costume.py
"""

import math
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cairo  # noqa: E402

import costume  # noqa: E402
import vector  # noqa: E402

FAILED = []
PASSED = 0


def ok(label, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(f"{label} {detail}".rstrip())


def creature(anim="idle", expression=None, **kw):
    return SimpleNamespace(anim=anim, y_off=0.0, expression=expression, x=0.0,
                           facing=1, jump_phase="", **kw)


def run(rig, c, secs, draw=False):
    """Step at 60fps, optionally drawing every frame; returns the surface."""
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 240, 240)
    cr = cairo.Context(surf)
    for _ in range(int(secs * 60)):
        rig.update(1 / 60, c)
        if draw:
            cr.set_operator(cairo.OPERATOR_CLEAR)
            cr.paint()
            cr.set_operator(cairo.OPERATOR_OVER)
            rig.draw(cr, 40, 80, 160, 128, c.facing)
    return surf


def painted(surf):
    data = bytes(surf.get_data())
    return sum(1 for i in range(3, len(data), 4) if data[i])


# ------------------------------------------------ 1. every costume, every phase
# Long enough to cover a full cycle of the slowest loop (the page turn, 5.1s).
for name in costume.NAMES:
    rig = vector.Rig(seed=1)
    if name == "listen":
        rig.listening = True
        c = creature("idle", "vibing")
    else:
        c = creature(name)
    try:
        run(rig, c, 6.0, draw=True)
        ok(f"{name}: six seconds draw without raising", True)
    except Exception as exc:                                  # pragma: no cover
        ok(f"{name}: six seconds draw without raising", False, repr(exc))
    ok(f"{name}: is the costume worn", rig.wardrobe.name == name, rig.wardrobe.name)
    ok(f"{name}: has finished putting it on", rig.wardrobe.on.x > 0.9, rig.wardrobe.on.x)

    # facing left must draw too (everything is mirrored inside the rig)
    c.facing = -1
    try:
        run(rig, c, 1.0, draw=True)
        ok(f"{name}: draws facing left", True)
    except Exception as exc:                                  # pragma: no cover
        ok(f"{name}: draws facing left", False, repr(exc))

# A costume has to change what is on screen, or it is not being drawn at all.
plain = painted(run(vector.Rig(seed=1), creature("idle"), 1.0, draw=True))
for name in ("read", "build", "cook"):
    dressed = painted(run(vector.Rig(seed=1), creature(name), 1.0, draw=True))
    ok(f"{name}: covers more of the frame than no costume", dressed > plain,
       f"{dressed} vs {plain}")


# ------------------------------------------------ 2. what turns them on and off
rig = vector.Rig(seed=2)
run(rig, creature("idle"), 0.5)
ok("idle and not listening wears nothing", rig.wardrobe.name is None, rig.wardrobe.name)

rig.listening = True
run(rig, creature("idle", "vibing"), 0.5)
ok("listening puts the headphones on", rig.wardrobe.name == "listen", rig.wardrobe.name)

run(rig, creature("patrol", "vibing"), 0.5)
ok("they stay on while he walks", rig.wardrobe.name == "listen", rig.wardrobe.name)

run(rig, creature("build"), 0.5)
ok("a work costume replaces them", rig.wardrobe.name == "build", rig.wardrobe.name)

rig.listening = False
run(rig, creature("idle"), 0.5)
ok("and nothing is left on once work and listening both end",
   rig.wardrobe.name is None, rig.wardrobe.name)

w = costume.Wardrobe(seed=1)
w.update(1 / 60, "nonsense", vector.Rig(seed=1))
ok("an unknown costume name is ignored, not drawn", w.name is None, w.name)

# a swap starts the new outfit from nothing, so it visibly goes on
rig = vector.Rig(seed=3)
run(rig, creature("cook"), 2.0)
had = len(rig.wardrobe.particles)
run(rig, creature("read"), 1 / 60)
ok("swapping costumes restarts the put-on", rig.wardrobe.on.x < 0.2, rig.wardrobe.on.x)
ok("and drops the old costume's particles", not rig.wardrobe.particles, f"had {had}")


# ------------------------------------------------ 3. geometry stays where it belongs
w = costume.Wardrobe(seed=4)
heights = []
for i in range(int(costume.Wardrobe.FLIP_SECS * 60 * 2)):
    p = (i / 60.0 / costume.Wardrobe.FLIP_SECS) % 1.0
    heights.append(w._pancake(p)[1])
ok("the pancake rests in the pan between flips",
   w._pancake(0.1)[1] == w._pancake(0.95)[1], (w._pancake(0.1), w._pancake(0.95)))
ok("and clears the chef's hat at the top of the flip", min(heights) < 1.5, min(heights))
ok("without leaving the strip he is drawn in", min(heights) > -12.0, min(heights))

w = costume.Wardrobe(seed=5)
tips = []
for _ in range(int(costume.Wardrobe.LINE_SECS * (costume.Wardrobe.LINES + 1) * 60)):
    w.t += 1 / 60
    tips.append(w._pencil_tip())
ok("the pencil stays over the notepad",
   all(21.0 <= x <= 31.0 and 21.5 <= y <= 30.0 for x, y in tips),
   [t for t in tips if not (21.0 <= t[0] <= 31.0 and 21.5 <= t[1] <= 30.0)][:3])

w = costume.Wardrobe(seed=6)
angles = []
for _ in range(240):
    w.t += 1 / 60
    angles.append(w._wrench_angle())
ok("the wrench winds back and strikes forward",
   min(angles) < -40.0 and max(angles) > 20.0, (min(angles), max(angles)))

# sparks fly off each strike and fall away again
rig = vector.Rig(seed=7)
c = creature("build")
sparks = 0
for _ in range(120):
    rig.update(1 / 60, c)
    sparks = max(sparks, sum(1 for p in rig.wardrobe.particles if p[6] == "spark"))
ok("striking throws sparks", sparks >= 3, sparks)
rig = vector.Rig(seed=8)
rig.listening = True
run(rig, creature("idle", "vibing"), 3.0)
notes = [p for p in rig.wardrobe.particles if p[6].startswith("note")]
ok("the headphones play notes", len(notes) >= 2, len(notes))
ok("which float up, not down", all(p[3] < 0 for p in notes))


# ------------------------------------------------ 4. what the costume does to him
rig = vector.Rig(seed=9)
run(rig, creature("write", "writing"), 2.0)
ok("the writing costume brings its own pad, not the laptop",
   rig.writing < 0.05, rig.writing)
rig = vector.Rig(seed=9)
run(rig, creature("idle", "writing"), 2.0)
ok("a reply with no costume still gets the laptop", rig.writing > 0.9, rig.writing)

rig = vector.Rig(seed=10)
bobs = []
rig.listening = True
c = creature("idle", "vibing")
for _ in range(120):
    rig.update(1 / 60, c)
    bobs.append(rig.offsets.get("bob", 0.0))
ok("listening nods on the beat", max(bobs) - min(bobs) > 0.4, (min(bobs), max(bobs)))

# the thinking spark draws over a hat as well as a bare head
for anim in ("working", "build", "cook"):
    rig = vector.Rig(seed=11)
    c = creature(anim, "thinking")
    try:
        run(rig, c, vector.THINK_DELAY + 1.0, draw=True)
        ok(f"thinking spark draws over {anim}", rig.thinking > 0.9, rig.thinking)
    except Exception as exc:                                  # pragma: no cover
        ok(f"thinking spark draws over {anim}", False, repr(exc))


# ---------------------------------------------------------------------- report
print(f"\n{PASSED} passed, {len(FAILED)} failed")
for f in FAILED:
    print("  FAIL " + f)
sys.exit(1 if FAILED else 0)
