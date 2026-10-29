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
import random

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
import costume
from face import Face, expression_for
from vector_motion import Blink, Breath, Follow, Oscillator, Spring, squash

# Palette, taken from sprites.PALETTE so the two renderers cannot drift.
BODY = (0.843, 0.463, 0.337)      # #D77656
LIT = (0.910, 0.592, 0.478)       # #E8977A
SHADE = (0.659, 0.325, 0.220)     # #A85338
OUTLINE = (0.494, 0.227, 0.141)   # #7E3A24
CORE = (0.380, 0.140, 0.075)      # the side turned away from the light
BOUNCE = (1.000, 0.640, 0.440)    # warm light thrown back up off the ground
SCREEN = (0.640, 0.800, 1.000)    # laptop screen light on his face
SPARK = (0.960, 0.560, 0.380)     # the thinking spark, Claude Code's spinner orange

OUTLINE_W = 0.6                   # in grid units; thin enough to read as an edge, not ink
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
    "working": {"breath": 0.55, "eye": 0.95, "stretch": 0.96,
                "osc": [("bob", 0.55, 1.2, "sine"),
                        ("arm_l_raise", 0.5, 1.2, "sine")]},
    "jump":    {"breath": 0.60, "eye": 1.15, "stretch": 1.05,
                "arm": 0.8},
    "sleep":   {"breath": 0.12, "eye": 0.06, "stretch": 0.90, "arm": -0.35,
                "osc": [("bob", 0.5, 0.12, "sine")]},
    "error":   {"breath": 0.30, "eye": 1.00, "stretch": 0.92, "arm": -0.3,
                "osc": [("tilt", 1.4, 0.55, "sway")]},
    "wake":    {"breath": 0.40, "eye": 1.15, "stretch": 1.07, "arm": 0.5},
    "wave":    {"breath": 0.35, "eye": 1.05,
                "osc": [("arm_r_raise", 1.5, 1.7, "sine")]},
}

ANIM_MOTION.update(costume.MOTION)

THINK_DELAY = 1.5     # secs of thinking before the cloud shows

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
    # Stacked at growing offsets and falling alpha, so the edge is soft: a
    # held thing a little off the surface, not a decal with a drop shadow.
    if shadow and name not in _FLAT_PROPS:
        for dx, dy, a in ((0.35, 0.5, 0.14), (0.6, 0.85, 0.09), (0.9, 1.2, 0.05)):
            cr.save()
            cr.translate(dx, dy)
            for part in parts:
                if prop_art.STYLE[part]["fill"] is None:
                    continue
                prop_art.path(cr, part)
            cr.set_source_rgba(0.19, 0.09, 0.05, a * alpha)
            cr.fill()
            cr.restore()

    # clip first: an unclipped group is the size of the whole window
    cr.rectangle(bx - 1.0, by - 1.0, bw + 2.0, bh + 2.0)
    cr.clip()
    cr.push_group()
    for part in parts:
        style = prop_art.STYLE[part]
        fill = style["fill"]
        if fill is None:
            continue
        prop_art.path(cr, part)
        cr.set_source_rgba(*fill, style["fill_opacity"])
        cr.fill()
    # the same key light as his body: lit upper left, falling off lower right
    if name not in _FLAT_PROPS:
        cr.set_operator(cairo.OPERATOR_ATOP)
        g = cairo.LinearGradient(bx, by, bx + bw, by + bh)
        g.add_color_stop_rgba(0.0, 1, 1, 1, 0.22)
        g.add_color_stop_rgba(0.45, 1, 1, 1, 0.0)
        g.add_color_stop_rgba(0.6, 0, 0, 0, 0.0)
        g.add_color_stop_rgba(1.0, 0, 0, 0, 0.22)
        cr.set_source(g)
        cr.paint()
    cr.pop_group_to_source()
    cr.paint_with_alpha(alpha)
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
    "sparkle": {"spin": 120.0},
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


CLAY_PX = 4          # texture pixels per grid unit; 1:1 at the default size
CLAY_PAD = 4         # grid units of texture beyond the 40x32 box, for squash
_clay = None
_form = None


def _grid_pattern(surface):
    """A texture made at CLAY_PX per grid unit, placed back on the grid."""
    pattern = cairo.SurfacePattern(surface)
    pattern.set_filter(cairo.FILTER_BILINEAR)
    m = cairo.Matrix()
    m.scale(CLAY_PX, CLAY_PX)
    m.translate(CLAY_PAD, CLAY_PAD)
    pattern.set_matrix(m)
    return pattern


def _form_texture():
    """Soft darkening just inside the body's edge, which rounds the block
    off, and the warm bounce light along its underside. Many faint rings so
    no band shows - too many to stroke every frame, so done once here."""
    global _form
    if _form is not None:
        return _form
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, int((VW + CLAY_PAD * 2) * CLAY_PX),
                              int((VH + CLAY_PAD * 2) * CLAY_PX))
    cr = cairo.Context(surf)
    cr.scale(CLAY_PX, CLAY_PX)
    cr.translate(CLAY_PAD, CLAY_PAD)
    art.path(cr, "body")
    cr.clip()
    cr.set_source_rgba(*CORE, 0.022)
    for step in range(12):
        art.path(cr, "body")
        cr.set_line_width(6.0 - step * 0.45)
        cr.stroke()
    _, by, _, bh = art.BOXES["body"]
    g = cairo.LinearGradient(0.0, by + bh - 3.2, 0.0, by + bh)
    g.add_color_stop_rgba(0.0, *BOUNCE, 0.0)
    g.add_color_stop_rgba(1.0, *BOUNCE, 0.22)
    cr.set_source(g)
    cr.paint()
    _form = surf
    return surf


def _clay_texture():
    """Grain and lumps for the clay finish, made once and reused every frame.

    Two layers of noise: soft blotches a few units across (thumb-pressed
    unevenness) and per-pixel grain. Stored as light and dark speckles with
    their own alpha, so it can be laid over any colour with ATOP."""
    global _clay
    if _clay is not None:
        return _clay
    rnd = random.Random(7)
    w = int((VW + CLAY_PAD * 2) * CLAY_PX)
    h = int((VH + CLAY_PAD * 2) * CLAY_PX)
    cell = 3 * CLAY_PX
    gw, gh = w // cell + 2, h // cell + 2
    lumps = [[rnd.uniform(-1.0, 1.0) for _ in range(gw)] for _ in range(gh)]

    def smooth(t):
        return t * t * (3.0 - 2.0 * t)

    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    stride = surf.get_stride()
    data = surf.get_data()
    for y in range(h):
        gy, fy = divmod(y, cell)
        ty = smooth(fy / cell)
        row0, row1 = lumps[gy], lumps[gy + 1]
        for x in range(w):
            gx, fx = divmod(x, cell)
            tx = smooth(fx / cell)
            top = row0[gx] + (row0[gx + 1] - row0[gx]) * tx
            bot = row1[gx] + (row1[gx + 1] - row1[gx]) * tx
            n = (top + (bot - top) * ty) * 0.55 + rnd.uniform(-1.0, 1.0) * 0.45
            a = int(min(1.0, abs(n)) * 0.10 * 255)
            c = a if n > 0 else 0                  # premultiplied: white or black
            i = y * stride + x * 4
            data[i:i + 4] = bytes((c, c, c, a))
    surf.mark_dirty()
    _clay = surf
    return surf


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
        self.face = Face(seed=seed)
        self.wardrobe = costume.Wardrobe(seed=seed)
        self.listening = False       # set by the daemon: headphones on, waiting for you
        self._rand = random.Random(seed)
        self.particles = []          # [x, y, vx, vy, life, max_life, kind, colour, spin]
        # Weight. He leans into his walk and carries the lean a beat past a
        # stop; a turn pinches him narrow for an instant, the way a body turns
        # through its thin side; and landing or stopping sets him wobbling.
        self.drift = Spring(0.0, k=60.0, damping=0.55)     # degrees forward
        self.turn = Spring(1.0, k=180.0, damping=0.55)     # width multiplier
        self.jiggle = Spring(0.0, k=260.0, damping=0.22)   # + wide, - tall
        self._last_x = None
        self._vx = 0.0               # smoothed travel speed, px/s
        self._stride = 0.0           # grid units a foot covers per step
        self._facing = None
        self._moving = False
        self._phase = ""
        self._fidget_in = self._rand.uniform(12.0, 25.0)
        self.thinking = 0.0          # 0..1 fade of the thought cloud
        self._think_t = 0.0          # how long the face has been thinking
        self.writing = 0.0           # 0..1 fade of the laptop

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
            if creature.anim == "jump" or (creature.anim == "wake" and self.anim == "sleep"):
                self.p["stretch"].nudge(-2.5)            # a little startle squash
            self._adopt(creature.anim)
        self._motion_extras(dt, creature)

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

        if self.writing > 0.3:
            # typing: quick alternating taps, out of phase hand to hand
            tap = self.writing * 0.55
            self.offsets["arm_l_raise"] = self.offsets.get("arm_l_raise", 0.0) + tap * max(0.0, math.sin(self.t * 15.0))
            self.offsets["arm_r_raise"] = self.offsets.get("arm_r_raise", 0.0) + tap * max(0.0, math.sin(self.t * 15.0 + math.pi))

        # the costume steers arms and gaze, so it goes before the springs step
        self.wardrobe.update(dt, self.anim if self.anim in costume.NAMES else
                             ("listen" if self.listening else None), self)

        for spring in self.p.values():
            spring.step(dt)

        self.breath_v = self.breath.step(dt)
        self.lid = self.blink.step(dt)
        if self.leg_rate > 0.0:
            self.leg_phase = (self.leg_phase + dt * self.leg_rate) % 1.0

        # The face picks its own expression from the posture unless someone
        # who knows more (the daemon: thinking, writing, waiting on you) says.
        expression = getattr(creature, "expression", None) or expression_for(creature.anim)
        self.face.update(dt, expression, eye_h=self.value("eye_h"), lid=self.lid)

        self.arm_l.step(self.value("arm_l_raise"), dt)
        self.arm_r.step(self.value("arm_r_raise"), dt)
        self.squash_x, self.squash_y = squash(-self._y_speed, gain=0.0012)

    def _motion_extras(self, dt, creature):
        """Weight, jump anticipation and landing, particles, idle fidgets."""
        x = float(getattr(creature, "x", 0.0) or 0.0)
        vx = (x - self._last_x) / dt if (self._last_x is not None and dt > 0) else 0.0
        self._last_x = x
        if abs(vx) < 400.0:                 # a teleport is not a walk
            self._vx += (vx - self._vx) * min(1.0, dt * 10.0)
        facing = getattr(creature, "facing", 1) or 1
        if self._facing is not None and facing != self._facing:
            self.turn.x = 0.62
        self._facing = facing
        # forward is whichever way he faces; a drag backwards leans him back
        self.drift.target = max(-6.0, min(6.0, vx * facing * 0.08))
        moving = abs(vx) > 1.0
        if self._moving and not moving:
            self.jiggle.nudge(8.0)                           # settling after a stop
        self._moving = moving
        for spring in (self.drift, self.turn, self.jiggle):
            spring.step(dt)

        phase = getattr(creature, "jump_phase", "") or ""
        if phase != self._phase:
            if phase == "crouch":
                self.p["stretch"].target = 0.80              # anticipation
            elif phase == "air":
                self.p["stretch"].target = ANIM_MOTION["jump"].get("stretch", 1.0)
                self.p["stretch"].nudge(4.0)                 # launch
                if self._phase in ("crouch", ""):
                    self._confetti(10 + int(getattr(creature, "y_off", 0) or 0) // 3)
            elif phase == "land":
                self.p["stretch"].nudge(-4.5)                # squash on impact
                self.jiggle.nudge(14.0)
                self._dust()
            self._phase = phase

        if creature.anim == "idle" and not phase:
            self._fidget_in -= dt
            if self._fidget_in <= 0:
                self._fidget()
                self._fidget_in = self._rand.uniform(15.0, 35.0)

        # thought dots fade with the face's own expression
        # The cloud waits a beat: thinking between two quick tool calls lasts
        # a fraction of a second, and a cloud flashing on and off at that rate
        # read as a glitch rather than as thought.
        self._think_t = self._think_t + dt if self.face.expression == "thinking" else 0.0
        want = 1.0 if self._think_t > THINK_DELAY else 0.0
        self.thinking += (want - self.thinking) * min(1.0, dt * (7.0 if want else 9.0))
        # a costume brings its own props; the laptop is for replies only
        want = 1.0 if self.face.expression == "writing" and not self.wardrobe.name else 0.0
        self.writing += (want - self.writing) * min(1.0, dt * 6.0)

        alive = []
        for p in self.particles:
            p[4] -= dt
            if p[4] <= 0:
                continue
            p[0] += p[2] * dt
            p[1] += p[3] * dt
            p[3] += (18.0 if p[6] == "confetti" else -2.0) * dt   # gravity / drift up
            p[2] *= (1.0 - 1.8 * dt) if p[6] == "dust" else 1.0
            p[8] += dt * 6.0
            alive.append(p)
        self.particles = alive

    def _fidget(self):
        pick = self._rand.choice(("look", "look", "stretch", "bounce", "blink"))
        if pick == "look":
            self.face.look(self._rand.choice((-1.0, 1.0)), self._rand.uniform(-0.5, 0.3), hold=1.1)
        elif pick == "stretch":
            self.p["stretch"].nudge(3.0)
        elif pick == "bounce":
            self.p["bob"].nudge(-14.0)
        else:
            self.blink.force(double=True)

    def _dust(self):
        for side in (-1, 1):
            for i in range(3):
                self.particles.append([20.0 + side * (6 + i * 2.5), 31.0,
                                       side * self._rand.uniform(6, 14), -self._rand.uniform(1, 4),
                                       0.45, 0.45, "dust", None, 0.0])

    def _confetti(self, n):
        colours = ((0.95, 0.75, 0.25), (0.42, 0.62, 0.90), (0.55, 0.78, 0.45),
                   (0.92, 0.45, 0.50), (0.75, 0.55, 0.88))
        for _ in range(n):
            self.particles.append([20.0 + self._rand.uniform(-6, 6), 12.0,
                                   self._rand.uniform(-16, 16), -self._rand.uniform(14, 26),
                                   1.3, 1.3, "confetti", self._rand.choice(colours),
                                   self._rand.uniform(0, math.tau)])

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

    def foot_shift(self, index, count=3):
        """Grid units to slide one leg forward, so a planted foot holds still
        on the ground while the body travels over it.

        The lift half of the cycle (sin > 0) is the swing: the foot eases from
        a stride behind to a stride ahead. The other half is the stance: it
        runs backward at exactly the body's speed, which is what keeps it from
        skating. Standing still, the stride is zero and this does nothing."""
        if self.leg_rate <= 0.0 or self._stride <= 0.01:
            return 0.0
        phase = (self.leg_phase + index / float(count)) % 1.0
        half = self._stride / 2.0
        if phase < 0.5:
            eased = 0.5 - 0.5 * math.cos(math.pi * phase / 0.5)
            return -half + self._stride * eased
        return half - self._stride * (phase - 0.5) / 0.5

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
                shift = self.foot_shift(index)
                if shift:
                    cr.translate(shift, 0.0)
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
        # A stance lasts half a cycle; the ground moves under him by speed x
        # that long, in grid units at the size he is drawn.
        if self.leg_rate > 0.0 and w > 0:
            self._stride = min(6.0, abs(self._vx) / (w / VW) * 0.5 / self.leg_rate)
        else:
            self._stride = 0.0
        cr.save()
        cr.translate(x, y)
        cr.scale(w / VW, h / VH)

        # The outline straddles the path, so half its width falls outside the
        # 40x32 box and the window clips it. Inset the drawing by that half.
        inset = OUTLINE_W / 2.0
        cr.translate(inset, inset)
        cr.scale((VW - inset * 2.0) / VW, (VH - inset * 2.0) / VH)

        self._draw_particles(cr, "dust")

        if facing < 0:
            cr.translate(VW, 0.0)
            cr.scale(-1.0, 1.0)

        fx, fy = art.ANCHORS["feet"]
        cr.translate(fx, fy)
        stretch = max(0.5, self.value("stretch"))
        j = max(-0.5, min(0.5, self.jiggle.x * 0.06))
        cr.scale(self.squash_x / stretch * (1.0 + j) * max(0.3, self.turn.x),
                 self.squash_y * stretch * (1.0 - j))
        tilt = self.value("tilt") + self.drift.x
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

        # Fill and lighting go into one group, and the face's lids are filled
        # with that same group, so a squint shows the shaded skin behind it.
        cr.save()
        ax, ay, aw, ah = art.BOXES["all"]
        cr.rectangle(ax - 1.5, ay - 1.5, aw + 3.0, ah + 3.0)    # every paint below
        cr.clip()                                            # covers only this
        cr.push_group()
        cr.set_source(self._gradient())
        for _ in self._parts(cr):
            cr.fill()
        self._draw_form(cr)
        self._draw_sheen(cr)
        skin = cr.pop_group()
        cr.set_source(skin)
        cr.paint()
        cr.restore()

        self.face.draw(cr, skin=skin)
        self.wardrobe.draw(cr, self, skin)
        self._draw_thought(cr, facing)
        self._draw_laptop(cr)
        cr.restore()
        cr.save()
        cr.translate(x, y)
        cr.scale(w / VW, h / VH)
        self._draw_particles(cr, "confetti")
        cr.restore()

    def _draw_form(self, cr):
        """Light from the upper left, painted over the flat fill. Every layer
        uses ATOP, so it only lands where body is already drawn.

        Key and core give the direction; the darkened rim is what rounds the
        block off; bounce and contact shadow are what put him on a floor."""
        bx, by, bw, bh = art.BOXES["body"]
        cr.save()
        cr.set_operator(cairo.OPERATOR_ATOP)

        # key to core: lit upper left, falling off to the far lower right
        g = cairo.LinearGradient(bx - 2.0, by - 2.0, bx + bw + 2.0, VH)
        g.add_color_stop_rgba(0.00, 1, 1, 1, 0.14)
        g.add_color_stop_rgba(0.40, 1, 1, 1, 0.0)
        g.add_color_stop_rgba(0.62, *CORE, 0.0)
        g.add_color_stop_rgba(1.00, *CORE, 0.34)
        cr.set_source(g)
        cr.paint()

        # rounded edges and the floor's bounce light: fixed to the body, so
        # drawn once into a texture rather than stroked every frame
        cr.set_source(_grid_pattern(_form_texture()))
        cr.paint()

        # contact shadow: where the legs tuck under the body, and the arms'
        # inner ends where they meet its sides
        # (clipped to outside the body: limbs run on under it, and shading
        # those hidden ends would show through as dark patches on the torso)
        cr.save()
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        cr.rectangle(-4.0, -4.0, VW + 8.0, VH + 8.0)
        art.path(cr, "body")
        cr.clip()
        cr.set_fill_rule(cairo.FILL_RULE_WINDING)
        under = cairo.LinearGradient(0.0, by + bh - 0.4, 0.0, by + bh + 2.2)
        under.add_color_stop_rgba(0.0, *CORE, 0.45)
        under.add_color_stop_rgba(1.0, *CORE, 0.0)
        for name in self._parts(cr):
            if name in LEGS:
                cr.set_source(under)
                cr.fill()
            elif name in ("arm_l", "arm_r"):
                edge = bx if name == "arm_l" else bx + bw
                out = -2.6 if name == "arm_l" else 2.6
                g = cairo.LinearGradient(edge, 0.0, edge + out, 0.0)
                g.add_color_stop_rgba(0.0, *CORE, 0.30)
                g.add_color_stop_rgba(1.0, *CORE, 0.0)
                cr.set_source(g)
                cr.fill()
            else:
                cr.new_path()
        cr.restore()
        cr.restore()

    def _draw_sheen(self, cr):
        """The clay finish: a broad, dull sheen where the light lands - matte
        surfaces spread a highlight instead of focusing it - over a fixed grain
        that rides with the body, so it reads as the surface, not the screen."""
        bx, by, bw, bh = art.BOXES["body"]
        cr.save()
        art.path(cr, "body")
        cr.clip()
        cr.translate(bx + bw * 0.36, by + 3.0)
        cr.scale(bw * 0.55, 5.5)
        g = cairo.RadialGradient(0, 0, 0, 0, 0, 1)
        g.add_color_stop_rgba(0, 1, 0.96, 0.90, 0.16)
        g.add_color_stop_rgba(1, 1, 0.96, 0.90, 0.0)
        cr.set_source(g)
        cr.arc(0, 0, 1, 0, math.tau)
        cr.fill()
        cr.restore()

        cr.save()
        cr.set_operator(cairo.OPERATOR_ATOP)
        cr.set_source(_grid_pattern(_clay_texture()))
        cr.paint()
        cr.restore()

    def _draw_thought(self, cr, facing):
        """Claude Code's own thinking spark, spinning over his head.

        The terminal shows a little star that turns and changes shape while
        Claude thinks; he shows the same one, so the two read as one signal.
        Eight rays, alternate ones long and short, the ratio breathing so it
        morphs between a plus, an asterisk and a star the way the CLI glyphs
        cycle. Motes rise from his head into it: the thought coming from him.
        """
        a = max(0.0, min(1.0, self.thinking))
        if a < 0.02:
            return
        grow = 0.4 + 0.6 * (1.0 - (1.0 - a) ** 3)       # ease-out pop
        hat = self.wardrobe.name in ("build", "cook")
        cx = 20.0
        cy = (-3.4 if hat else 3.6) + math.sin(self.t * 1.6) * 0.4
        top = 5.0 if hat else 11.6
        cr.save()
        cr.push_group()

        # motes drifting up from his head into the spark
        for i in range(3):
            f = (self.t * 0.9 + i / 3.0) % 1.0
            x = cx + math.sin(f * 5.0 + i * 2.1) * 0.9
            y = top + (cy + 2.2 - top) * f
            cr.arc(x, y, 0.32 * (1.0 - f * 0.5) * grow, 0, math.tau)
            cr.set_source_rgba(*SPARK, 0.85 * math.sin(f * math.pi))
            cr.fill()

        # glow
        r = 5.6 * grow
        g = cairo.RadialGradient(cx, cy, 0, cx, cy, r)
        g.add_color_stop_rgba(0.0, *SPARK, 0.45 * (0.8 + 0.2 * math.sin(self.t * 3.2)))
        g.add_color_stop_rgba(1.0, *SPARK, 0.0)
        cr.set_source(g)
        cr.arc(cx, cy, r, 0, math.tau)
        cr.fill()

        # the spark: rays from the centre, outline under, fill over
        morph = 0.5 + 0.5 * math.sin(self.t * 2.4)
        spin = self.t * 1.3 * (1 if facing >= 0 else -1)
        long_r = (3.0 + 0.4 * math.sin(self.t * 4.8)) * grow
        short_r = long_r * (0.35 + 0.55 * morph)
        for width, colour in ((1.25 * grow, OUTLINE), (0.6 * grow, SPARK)):
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.set_line_width(width)
            cr.set_source_rgb(*colour)
            for k in range(8):
                ang = spin + k * math.pi / 4.0
                length = long_r if k % 2 == 0 else short_r
                cr.move_to(cx, cy)
                cr.line_to(cx + math.cos(ang) * length, cy + math.sin(ang) * length)
            cr.stroke()
        cr.arc(cx, cy, 0.55 * grow, 0, math.tau)
        cr.set_source_rgb(1.0, 0.95, 0.88)
        cr.fill()

        cr.pop_group_to_source()
        cr.paint_with_alpha(a)
        cr.restore()

    def _draw_laptop(self, cr):
        """The back of a small laptop in front of him while he writes a reply."""
        a = self.writing
        if a < 0.02:
            return
        rise = (1.0 - a) * 3.0                     # slides up into place
        x0, x1, top, base = 13.6, 26.4, 23.6 + rise, 29.4 + rise
        # The screen faces him: a cool glow up his front and across his face,
        # flickering faintly as the text under it changes.
        flicker = 0.85 + 0.15 * math.sin(self.t * 9.0) * math.sin(self.t * 3.7)
        cr.save()
        art.path(cr, "body")
        cr.clip()
        g = cairo.RadialGradient(20.0, top - 1.0, 0.0, 20.0, top - 1.0, 11.0)
        g.add_color_stop_rgba(0.0, *SCREEN, 0.42 * a * flicker)
        g.add_color_stop_rgba(0.5, *SCREEN, 0.14 * a * flicker)
        g.add_color_stop_rgba(1.0, *SCREEN, 0.0)
        cr.set_source(g)
        cr.paint()
        cr.restore()
        cr.save()
        cr.push_group()
        art_round = 1.0
        # lid, seen from behind
        cr.new_sub_path()
        cr.arc(x1 - art_round, top + art_round, art_round, -math.pi / 2, 0)
        cr.line_to(x1, base)
        cr.line_to(x0, base)
        cr.arc(x0 + art_round, top + art_round, art_round, math.pi, 1.5 * math.pi)
        cr.close_path()
        g = cairo.LinearGradient(0, top, 0, base)
        g.add_color_stop_rgb(0, 0.80, 0.82, 0.86)
        g.add_color_stop_rgb(1, 0.58, 0.60, 0.66)
        cr.set_source(g)
        cr.fill_preserve()
        cr.set_source_rgb(0.25, 0.26, 0.30)
        cr.set_line_width(0.55)
        cr.stroke()
        # glowing logo
        lx, ly = (x0 + x1) / 2, (top + base) / 2
        glow = cairo.RadialGradient(lx, ly, 0, lx, ly, 2.2)
        glow.add_color_stop_rgba(0, 1.0, 1.0, 1.0, 0.9)
        glow.add_color_stop_rgba(1, 1.0, 1.0, 1.0, 0.0)
        cr.set_source(glow)
        cr.arc(lx, ly, 2.2, 0, math.tau)
        cr.fill()
        # base, a thin slab in front
        cr.rectangle(x0 - 1.2, base, (x1 - x0) + 2.4, 0.9)
        cr.set_source_rgb(0.38, 0.40, 0.45)
        cr.fill()
        cr.pop_group_to_source()
        cr.paint_with_alpha(a)
        cr.restore()

    def _draw_particles(self, cr, kind):
        for x, y, _vx, _vy, life, max_life, k, colour, spin in self.particles:
            if k != kind:
                continue
            fade = max(0.0, min(1.0, life / max_life * 1.6))
            if k == "dust":
                grow = 1.0 - life / max_life
                cr.set_source_rgba(0.85, 0.80, 0.74, 0.55 * fade)
                cr.arc(x, y, 0.8 + grow * 1.4, 0, math.tau)
                cr.fill()
            else:
                cr.save()
                cr.translate(x, y)
                cr.rotate(spin)
                cr.scale(1.0, abs(math.cos(spin * 1.3)) * 0.9 + 0.1)
                cr.set_source_rgba(*colour, fade)
                cr.rectangle(-0.55, -0.35, 1.1, 0.7)
                cr.fill()
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
