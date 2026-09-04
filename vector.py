"""Vector rendering for the creature and everything pinned to him.

Replaces the baked sprite atlas outright - body, props, shadow and badges are
all paths now, and nothing loads `sprites.png`. `sprites.py` stays as the
source of animation metadata (frame counts, rates, costume anchors, which tool
wears what), which is what it always really was; the atlas was a bake of it.

`Creature` still counts frames, because prop anchors are authored per frame and
that arithmetic is unchanged.

Two things change by doing it this way.

**The cadence stops being the art's.** Sprite frames ran at 2-12fps because
somebody drew each one. Here every value is a spring or an oscillator
integrated against real dt, so the same motion reads at whatever rate the tick
loop offers - 60, 30, or a stuttering 43 after a GC pause.

**The silhouette stops being pixels.** Same proportions, same palette, same
three legs, drawn as rounded paths with one vertical gradient across the whole
body so the light reads as coming from above rather than being painted per
cell.

    rig = Rig()
    rig.update(dt, creature)        # springs follow whatever he is doing
    rig.draw(cr, x, y, w, h, facing)
"""

import math

import cairo

try:
    import vector_paths as art
    import vector_prop_paths as prop_art
except ImportError as error:                      # pragma: no cover
    raise SystemExit(
        f"{error}\n\n"
        "The art has not been compiled. Run:\n"
        "  python3 build_vector.py vector_art.svg   vector_paths.py\n"
        "  python3 build_vector.py vector_props.svg vector_prop_paths.py\n"
        "(install.sh does this for you.)") from error
from vector_motion import Blink, Breath, Follow, Oscillator, Spring, squash

# Palette, taken from sprites.PALETTE so the two renderers cannot drift.
BODY = (0.843, 0.463, 0.337)      # #D77656
LIT = (0.910, 0.592, 0.478)       # #E8977A
SHADE = (0.659, 0.325, 0.220)     # #A85338
OUTLINE = (0.494, 0.227, 0.141)   # #7E3A24

OUTLINE_W = 0.9                   # in grid units; the sprite's was 1 hard pixel
BODY_PARTS = ("leg_l", "leg_m", "leg_r", "arm_l", "arm_r", "body")
LEGS = ("leg_l", "leg_m", "leg_r")

VW, VH = art.VIEWBOX[2], art.VIEWBOX[3]

# What each animation feels like, as continuous motion rather than frames.
# Rates are deliberately spread: two states that beat at the same speed read as
# the same state however different their poses are.
#
#   breath   body swell, Hz          legs   walk-cycle rate, 0 = planted
#   stretch  posture, volume preserving      eye  eyelid height multiplier
#   osc      (param, amplitude, Hz, wave)
ANIM_MOTION = {
    "idle":    {"breath": 0.25, "eye": 1.00},
    "blink":   {"breath": 0.25, "eye": 1.00},
    "patrol":  {"breath": 0.45, "eye": 1.00, "legs": 2.0, "lift": 1.6,
                "osc": [("bob", 0.45, 2.0, "sine")]},
    "working": {"breath": 0.55, "eye": 0.55, "stretch": 0.96,
                "osc": [("bob", 0.55, 1.2, "sine"),
                        ("arm_l_raise", 0.5, 1.2, "sine")]},
    "jump":    {"breath": 0.60, "eye": 1.15, "stretch": 1.05,
                "arm": 0.8},
    "sleep":   {"breath": 0.12, "eye": 0.06, "stretch": 0.90, "arm": -0.35,
                "osc": [("bob", 0.5, 0.12, "sine")]},
    "error":   {"breath": 0.30, "eye": 0.45, "stretch": 0.92, "arm": -0.3,
                "osc": [("tilt", 1.4, 0.55, "sway")]},
    "wake":    {"breath": 0.40, "eye": 1.15, "stretch": 1.07, "arm": 0.5},
    "wave":    {"breath": 0.35, "eye": 1.05,
                "osc": [("arm_r_raise", 1.5, 1.7, "sine")]},
}

PARAMS = {
    #  name            rest    k     damping
    "stretch":        (1.0, 130.0, 0.68),
    "lean":           (0.0,  70.0, 0.80),
    "tilt":           (0.0, 110.0, 0.65),
    "bob":            (0.0, 120.0, 0.62),
    "eye_h":          (1.0, 300.0, 0.70),
    "arm_l_raise":    (0.0, 120.0, 0.50),
    "arm_r_raise":    (0.0, 120.0, 0.50),
    "leg_lift":       (0.0,  90.0, 0.85),
}


# prop name -> the shape ids that make it up, in draw order
_PROP_PARTS = {}
for _id in prop_art.DRAW_ORDER:
    if "__" in _id:
        _PROP_PARTS.setdefault(_id.split("__", 1)[0], []).append(_id)


# base name -> frame keys, e.g. "sparkle" -> ["sparkle0", "sparkle1"]
_PROP_FRAMES = {}
for _key in _PROP_PARTS:
    _base = _key.rstrip("0123456789")
    if _base != _key:
        _PROP_FRAMES.setdefault(_base, []).append(_key)
for _v in _PROP_FRAMES.values():
    _v.sort()


def prop_names():
    return sorted(_PROP_PARTS)


def prop_key(name, frame=0):
    """Resolve (prop, frame index) to a drawable key, wrapping like the atlas
    did. Single-frame props ignore the index."""
    variants = _PROP_FRAMES.get(name)
    if variants:
        return variants[int(frame) % len(variants)]
    return name if name in _PROP_PARTS else None


def prop_frame_count(name):
    return len(_PROP_FRAMES.get(name, ())) or (1 if name in _PROP_PARTS else 0)


def draw_prop(cr, name, x, y, scale, alpha=1.0, rotate=0.0, shadow=True):
    """Draw a prop with the top-left of its footprint at (x, y).

    `x`/`y` are device pixels and `scale` is the sprite scale, so this is a
    drop-in for the pixbuf blit it replaces: the footprint recorded in
    `box_<name>` is exactly the ASCII grid the anchors were authored against.
    """
    parts = _PROP_PARTS.get(name)
    if not parts:
        return False
    bx, by, bw, bh = prop_art.BOXES[name]
    cr.save()
    cr.translate(x, y)
    cr.scale(scale, scale)
    if rotate:
        # Spin about the prop's own middle. The pivot has to be expressed in
        # the space *after* the -bx,-by shift below, so it is (bw/2, bh/2) --
        # using the atlas coordinates here instead displaces the pivot by the
        # prop's position on the shared sheet and throws it off screen.
        cr.translate(bw / 2.0, bh / 2.0)
        cr.rotate(math.radians(rotate))
        cr.translate(-bw / 2.0, -bh / 2.0)
    cr.translate(-bx, -by)
    # Rings (the glasses lenses) are an outer path with an inner one cut out of
    # it, which only reads as a hole under even-odd.
    cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)

    # A soft offset silhouette under the prop. Without it a prop sits flat
    # against the body it is pinned to and reads as painted on rather than held.
    if shadow and name not in _FLAT_PROPS:
        cr.save()
        cr.translate(0.5, 0.7)
        for part in parts:
            if prop_art.STYLE[part]["fill"] is None:
                continue
            prop_art.path(cr, part)
        cr.set_source_rgba(0.19, 0.09, 0.05, 0.28 * alpha)
        cr.fill()
        cr.restore()

    for part in parts:
        style = prop_art.STYLE[part]
        fill = style["fill"]
        if fill is None:
            continue
        prop_art.path(cr, part)
        cr.set_source_rgba(*fill, style["fill_opacity"] * alpha)
        cr.fill()
    cr.restore()
    return True


# Props that are already shadows or light, and must not cast one of their own.
_FLAT_PROPS = {"shadow0", "shadow1", "shadow2", "puff0", "puff1", "puff2",
               "sparkle0", "sparkle1"}


def prop_size(name):
    """(w, h) of a prop's footprint in grid units, or None if unknown."""
    box = prop_art.BOXES.get(name)
    return (box[2], box[3]) if box else None


# How each prop carries itself beyond simply following its anchor.
#
#   swing  degrees of tilt taken from how fast the anchor is falling, so a tool
#          being swung leans into the swing instead of staying rigid
#   spin   degrees over one loop, for things that tumble rather than swing
#   lag    spring stiffness for the prop trailing its anchor; lower is heavier
PROP_MOTION = {
    "wrench":  {"swing": 16.0, "lag": 150.0},
    "pencil":  {"swing": 12.0, "lag": 200.0},
    "pan":     {"swing": 9.0,  "lag": 120.0},
    "food":    {"spin": 300.0, "lag": 260.0},
    "book":    {"swing": 4.0,  "lag": 190.0},
    "sparkle": {"spin": 120.0},
    "hardhat": {"lag": 240.0},
    "chefhat": {"lag": 210.0},
    "glasses": {"lag": 300.0},
    "binoculars": {"lag": 260.0},
}

_CATMULL_TIGHT = 0.5


def sample_anchors(anchors, phase):
    """Anchor position at a continuous frame phase, or None when hidden.

    The arrays in COSTUMES were authored as keyframes for a 4-8 fps flipbook.
    Sampled at 60fps they teleport - the thrown food in `cook` jumps 11 grid
    units between two frames, which is a quarter of his body in one tick. So
    the keyframes are run through a Catmull-Rom spline instead of being
    stepped: the authored positions are still hit exactly, and the path between
    them curves the way a tossed thing actually travels.

    A `None` keyframe means the prop is not on screen for that frame, so any
    span touching one is hidden rather than interpolated out of nowhere.
    """
    count = len(anchors)
    if count == 0:
        return None
    if count == 1:
        return tuple(anchors[0]) if anchors[0] else None

    phase = phase % count
    index = int(phase)
    t = phase - index

    p1, p2 = anchors[index], anchors[(index + 1) % count]
    if p1 is None or p2 is None:
        return tuple(p1) if (p1 and t < 0.5) else (tuple(p2) if p2 and t >= 0.5 else None)

    p0 = anchors[(index - 1) % count] or p1
    p3 = anchors[(index + 2) % count] or p2

    tt = t * t
    ttt = tt * t
    out = []
    for axis in (0, 1):
        a, b, c, d = p0[axis], p1[axis], p2[axis], p3[axis]
        out.append(_CATMULL_TIGHT * (
            2 * b
            + (c - a) * t
            + (2 * a - 5 * b + 4 * c - d) * tt
            + (-a + 3 * b - 3 * c + d) * ttt))
    return (out[0], out[1])


def prop_rotation(name, anchors, phase):
    """Degrees to tilt a prop, derived from how its anchor is moving."""
    motion = PROP_MOTION.get(name)
    if not motion:
        return 0.0
    if "spin" in motion:
        return (phase / max(1, len(anchors))) * motion["spin"]
    swing = motion.get("swing")
    if not swing:
        return 0.0
    look = 0.08
    here = sample_anchors(anchors, phase)
    ahead = sample_anchors(anchors, phase + look)
    if here is None or ahead is None:
        return 0.0
    # Vertical speed drives the lean, so a tool swung downward leads with its
    # head. Normalised by the lookahead to give units-per-frame, or the tilt
    # comes out proportional to how far ahead we happened to peek.
    speed = (ahead[1] - here[1]) / look
    return max(-1.0, min(1.0, speed / 6.0)) * swing


def body_inset(scale):
    """`(top, left, right)` empty margin around the body, in pixels.

    The old version scanned a pixbuf's alpha to find where the creature
    actually sat inside its 40x32 cell. The vector art declares that outright
    in `box_all`, so the answer is arithmetic instead of an 11k-pixel scan.
    """
    bx, by, bw, _ = art.BOXES["all"]
    return (int(by * scale), int(bx * scale),
            int((VW - (bx + bw)) * scale))


class Sheet:
    """Animation metadata, with no atlas behind it.

    Stands in for the old `Sprites`: same lookups, no PNG and no pixbufs.
    Frame counts, rates, loop flags and costume anchors all come straight from
    sprites.py, which was always their source - `sprites.png` was only ever a
    bake of it, and nothing needs the bake once the body is drawn from paths.
    """

    def __init__(self, scale):
        import sprites

        self.scale = scale
        self.grid_w = int(VW)
        self.grid_h = int(VH)
        self.w = self.grid_w * scale
        self.h = self.grid_h * scale

        prop_z = {n: spec.get("z", "front") for n, spec in sprites.PROPS.items()}
        anims = {}
        for name, spec in sprites.ANIMS.items():
            anims[name] = {
                "count": len(spec["frames"]),
                "fps": spec["fps"],
                "loop": spec["loop"],
                "attach": {},
            }
        for name, spec in sprites.COSTUMES.items():
            base = anims[spec["base"]]
            length = spec.get("length", base["count"])
            anims[name] = {
                "count": length,
                "fps": spec.get("fps", 8),
                "loop": spec.get("loop", True),
                "attach": {prop: {"z": prop_z[prop], "anchors": anchors}
                           for prop, anchors in spec.get("attach", {}).items()},
            }
        self.anims = anims

    def __getitem__(self, name):
        return self.anims[name]

    def has(self, name):
        return name in self.anims


class Rig:
    def __init__(self, seed=None):
        self.p = {n: Spring(v, k=k, damping=d) for n, (v, k, d) in PARAMS.items()}
        self.arm_l = Follow(0.0, k=58.0, damping=0.45)
        self.arm_r = Follow(0.0, k=49.0, damping=0.42)
        self.blink = Blink(seed=seed)
        self.breath = Breath(rate=0.25)

        self.anim = None
        self.gestures = ()
        self.t = 0.0
        self.offsets = {}
        self.leg_phase = 0.0
        self.leg_rate = 0.0
        self.breath_v = 0.0
        self.lid = 0.0
        self.squash_x = 1.0
        self.squash_y = 1.0
        self._last_y = 0.0
        self._y_speed = 0.0

    # -- state ------------------------------------------------------------

    def _adopt(self, anim):
        spec = ANIM_MOTION.get(anim) or ANIM_MOTION["idle"]
        self.anim = anim
        self.breath.rate = spec.get("breath", 0.25)
        self.leg_rate = spec.get("legs", 0.0)
        self.p["stretch"].target = spec.get("stretch", 1.0)
        self.p["eye_h"].target = spec.get("eye", 1.0)
        self.p["leg_lift"].target = spec.get("lift", 0.0)
        arm = spec.get("arm", 0.0)
        self.p["arm_l_raise"].target = arm
        self.p["arm_r_raise"].target = arm
        self.gestures = tuple(
            Oscillator(p, a, r, w) for p, a, r, w in spec.get("osc", ()))
        self.t = 0.0

    def update(self, dt, creature):
        if creature.anim != self.anim:
            self._adopt(creature.anim)

        # Vertical travel is the jump arc's job, not ours. What we take from it
        # is the speed, because that is what squash and stretch are made of.
        y = float(getattr(creature, "y_off", 0.0))
        self._y_speed = (y - self._last_y) / dt if dt > 0 else 0.0
        self._last_y = y

        self.t += dt
        self.offsets = {}
        for gesture in self.gestures:
            self.offsets[gesture.param] = (self.offsets.get(gesture.param, 0.0)
                                           + gesture.value(self.t))

        for spring in self.p.values():
            spring.step(dt)

        self.breath_v = self.breath.step(dt)
        self.lid = self.blink.step(dt)
        if self.leg_rate > 0.0:
            self.leg_phase = (self.leg_phase + dt * self.leg_rate) % 1.0

        self.arm_l.step(self.value("arm_l_raise"), dt)
        self.arm_r.step(self.value("arm_r_raise"), dt)
        self.squash_x, self.squash_y = squash(-self._y_speed, gain=0.0012)

    def value(self, name):
        return self.p[name].x + self.offsets.get(name, 0.0)

    def leg_offset(self, index, count=3):
        """0..1 lift for one leg. Legs are staggered so he is never fully
        airborne while walking."""
        amount = self.value("leg_lift")
        if amount <= 0.001 or self.leg_rate <= 0.0:
            return 0.0
        phase = (self.leg_phase + index / float(count)) % 1.0
        return max(0.0, math.sin(phase * math.tau)) * amount

    # -- rendering --------------------------------------------------------

    @staticmethod
    def _gradient():
        """One vertical ramp across the whole creature. The sprite shaded per
        cell; a single gradient is what makes the light read as one source."""
        top, bottom = art.bounds(art.SHAPES["body"])[1], VH
        g = cairo.LinearGradient(0.0, top, 0.0, bottom)
        g.add_color_stop_rgb(0.00, *LIT)
        g.add_color_stop_rgb(0.22, *BODY)
        g.add_color_stop_rgb(0.75, *BODY)
        g.add_color_stop_rgb(1.00, *SHADE)
        return g

    def _parts(self, cr):
        """Lay every body part down in draw order, with its own transform."""
        for name in BODY_PARTS:
            cr.save()
            if name in LEGS:
                index = LEGS.index(name)
                lift = self.leg_offset(index)
                if lift > 0.001:
                    _, y0, _, y1 = art.bounds(art.SHAPES[name])
                    length = y1 - y0
                    cr.translate(0.0, y0)
                    cr.scale(1.0, max(0.15, 1.0 - lift * 0.42))
                    cr.translate(0.0, -y0)
            elif name == "arm_l":
                cr.translate(0.0, -self.arm_l.x * 2.2)
            elif name == "arm_r":
                cr.translate(0.0, -self.arm_r.x * 2.2)
            art.path(cr, name)
            yield name
            cr.restore()

    def draw(self, cr, x, y, w, h, facing=1):
        cr.save()
        cr.translate(x, y)
        cr.scale(w / VW, h / VH)

        # The outline straddles the path, so half its width falls outside the
        # 40x32 box and the window clips it. Inset the drawing by that half.
        inset = OUTLINE_W / 2.0
        cr.translate(inset, inset)
        cr.scale((VW - inset * 2.0) / VW, (VH - inset * 2.0) / VH)

        if facing < 0:
            cr.translate(VW, 0.0)
            cr.scale(-1.0, 1.0)

        fx, fy = art.ANCHORS["feet"]
        cr.translate(fx, fy)
        stretch = max(0.5, self.value("stretch"))
        cr.scale(self.squash_x / stretch, self.squash_y * stretch)
        tilt = self.value("tilt")
        if abs(tilt) > 0.02:
            cr.rotate(math.radians(tilt))
        cr.translate(-fx, -fy)

        cr.translate(self.value("lean") * 1.2, self.value("bob"))

        breath = 1.0 + self.breath_v * 0.018
        bx, by = art.ANCHORS["body"]
        cr.translate(bx, by)
        cr.scale(1.0 / breath, breath)
        cr.translate(-bx, -by)

        # Outlines first, fills second. Stroking after filling would draw each
        # limb's outline across the torso it is joined to; doing every stroke
        # before any fill leaves the seams covered and only the silhouette
        # outlined.
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.set_line_width(OUTLINE_W * 2.0)
        cr.set_source_rgb(*OUTLINE)
        for _ in self._parts(cr):
            cr.stroke()

        cr.set_source(self._gradient())
        for _ in self._parts(cr):
            cr.fill()

        self._draw_eyes(cr)
        cr.restore()

    def _draw_eyes(self, cr):
        lid = max(0.0, min(1.0, self.lid))
        height = max(0.0, self.value("eye_h")) * (1.0 - lid * 0.92)
        if height <= 0.02:
            return
        for name, shine in (("eye_l", "shine_l"), ("eye_r", "shine_r")):
            ex, ey = art.ANCHORS[name]
            cr.save()
            cr.translate(ex, ey)
            cr.scale(1.0, height)
            cr.translate(-ex, -ey)
            art.path(cr, name)
            cr.set_source_rgb(0.063, 0.063, 0.063)
            cr.fill()
            if height > 0.45:
                art.path(cr, shine)
                cr.set_source_rgba(1, 1, 1, 0.85 * min(1.0, height))
                cr.fill()
            cr.restore()
