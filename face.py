"""His face: eyes that look at things, brows, a mouth, and the moods between.

The old face was two black lozenges whose only control was height, so idle,
working and failed all looked like the same creature squinting by different
amounts. This replaces it with a handful of continuous parameters, each a
spring, and a table of expressions that set their targets. Changing
expression is never a cut - every value eases toward its new target at its
own rate, so worried-to-relieved reads as a face changing its mind.

    face = Face(seed=3)
    face.update(dt, "focused", eye_h=1.0, lid=blink)   # every tick
    face.look(0.8, -0.6)                               # glance up-right
    face.draw(cr)                                      # in the 40x32 grid

Everything is drawn in the body's own 40x32 coordinates, inside the transform
the rig already set up, so the face squashes, tilts and breathes with him.
"""

import math

import cairo

from vector_motion import Saccade, Spring

# Where things sit on the 40x32 grid. Eyes match the SVG anchors.
EYE_L = (14.0, 18.0)
EYE_R = (26.0, 18.0)
EYE_W, EYE_H = 3.3, 4.1
MOUTH = (20.0, 22.6)
MOUTH_W = 3.6
BROW_Y = 14.7
BROW_LEN = 2.5

INK = (0.13, 0.07, 0.05)          # lines: brows, mouth, closed eyes
EYE_TOP = (0.08, 0.06, 0.06)
EYE_LOW = (0.24, 0.13, 0.10)      # warm bounce light at the bottom of the eye
LID = (0.843, 0.463, 0.337)       # the body colour, for lids drawn over the eye
MOUTH_IN = (0.38, 0.10, 0.08)
TONGUE = (0.93, 0.47, 0.45)
BLUSH = (0.97, 0.42, 0.45)

#            name           rest    k      damping
PARAMS = {
    "smile":      (0.30, 140.0, 0.75),   # -1 frown .. 1 grin
    "open":       (0.00, 160.0, 0.70),   # mouth open 0..1
    "mouth_w":    (1.00, 140.0, 0.75),   # width multiplier; small = "o"
    "mouth_x":    (0.00, 100.0, 0.80),   # sideways, for a thinking smirk
    "wave":       (0.00, 120.0, 0.80),   # wobbly mouth, 0..1
    "squint":     (0.00, 170.0, 0.72),   # lower the top lids, 0..1
    "lid_tilt":   (0.00, 150.0, 0.75),   # + inner corners down (cross), - sad
    "brow_y":     (0.00, 150.0, 0.62),   # + raised
    "brow_tilt":  (0.00, 150.0, 0.62),   # + inner ends up (worried), - angry
    "brow_asym":  (0.00, 120.0, 0.70),   # + right brow higher than left
    "happy":      (0.00, 150.0, 0.80),   # eyes become ^ ^
    "dizzy":      (0.00, 120.0, 0.85),   # eyes become swirls
    "blush":      (0.00,  60.0, 0.90),
    "eye_scale":  (1.00, 220.0, 0.55),   # pop wide when startled
    "gaze_x":     (0.00, 520.0, 0.82),   # -1..1, fast: eyes jump, not drift
    "gaze_y":     (0.00, 520.0, 0.82),
}

# Expression -> targets. Anything unnamed goes back to its rest value.
# `gaze` pins where he looks (None lets the idle saccades wander).
EXPRESSIONS = {
    "neutral":   {"smile": 0.30},
    "content":   {"smile": 0.55, "blush": 0.15},
    "focused":   {"smile": 0.05, "squint": 0.38, "lid_tilt": 0.35,
                  "brow_y": -0.35, "brow_tilt": -0.55, "mouth_w": 0.75},
    "thinking":  {"smile": 0.0, "mouth_w": 0.6, "mouth_x": 0.9,
                  "brow_asym": 0.9, "brow_y": 0.2, "gaze": (0.75, -0.85)},
    "writing":   {"smile": 0.4, "squint": 0.2, "gaze": (0.15, 0.85)},
    "worried":   {"smile": -0.25, "open": 0.6, "mouth_w": 0.7,
                  "brow_y": 0.55, "brow_tilt": 0.9, "lid_tilt": -0.25,
                  "eye_scale": 1.08},
    "happy":     {"smile": 1.0, "open": 0.8, "mouth_w": 1.35, "happy": 1.0, "blush": 0.85,
                  "brow_y": 0.45},
    "dizzy":     {"smile": -0.5, "wave": 1.0, "dizzy": 1.0,
                  "brow_tilt": 0.6, "brow_y": 0.2},
    "sleepy":    {"smile": 0.1, "open": 0.22, "mouth_w": 0.5, "brow_y": -0.15},
    "surprised": {"smile": 0.0, "open": 0.75, "mouth_w": 0.7,
                  "brow_y": 1.0, "eye_scale": 1.18},
    "waving":    {"smile": 0.85, "open": 0.55, "mouth_w": 1.15, "happy": 1.0, "blush": 0.5,
                  "brow_y": 0.3},
}

# What each animation looks like when nobody has said otherwise. Costumes are
# all work, so anything not listed and not idle-ish reads as focused.
ANIM_EXPRESSION = {
    "idle": "neutral", "blink": "neutral", "patrol": "content",
    "working": "focused", "jump": "happy", "sleep": "sleepy",
    "error": "dizzy", "wake": "surprised", "wave": "waving",
}


def expression_for(anim):
    return ANIM_EXPRESSION.get(anim, "focused" if anim else "neutral")


class Face:
    def __init__(self, seed=None):
        self.p = {n: Spring(v, k=k, damping=d) for n, (v, k, d) in PARAMS.items()}
        self.saccade = Saccade(box=(0.7, 0.45), seed=seed)
        self.expression = "neutral"
        self.t = 0.0
        self._look = None            # (x, y, seconds left) from look()
        self._skin = None
        self.eye_h = 1.0
        self.lid = 0.0

    def value(self, name):
        return self.p[name].x

    def look(self, x, y, hold=0.8):
        """Glance somewhere for `hold` seconds; -1..1 on both axes."""
        self._look = (max(-1.0, min(1.0, x)), max(-1.0, min(1.0, y)), hold)

    def update(self, dt, expression=None, eye_h=1.0, lid=0.0):
        self.t += dt
        self.eye_h, self.lid = eye_h, lid
        if expression and expression in EXPRESSIONS:
            self.expression = expression
        spec = EXPRESSIONS[self.expression]
        for name, spring in self.p.items():
            if name in ("gaze_x", "gaze_y"):
                continue
            spring.target = spec.get(name, PARAMS[name][0])

        if self._look is not None:
            x, y, left = self._look
            gx, gy = x, y
            left -= dt
            self._look = (x, y, left) if left > 0 else None
        elif spec.get("gaze") is not None:
            gx, gy = spec["gaze"]
            # a pinned gaze still twitches a little, or it reads as a stare
            sx, sy = self.saccade.step(dt, spread=0.25)
            gx, gy = gx + sx, gy + sy
        else:
            spread = 0.35 if self.expression in ("focused", "sleepy") else 1.0
            gx, gy = self.saccade.step(dt, spread=spread)
        self.p["gaze_x"].target = gx
        self.p["gaze_y"].target = gy
        for spring in self.p.values():
            spring.step(dt)

    # ------------------------------------------------------------------ draw
    def draw(self, cr, skin=None):
        """`skin` is the body's own fill, so lids drawn over an eye match the
        gradient behind them instead of showing as a flat-coloured cap."""
        self._skin = skin
        v = self.value
        gx = max(-1.2, min(1.2, v("gaze_x")))
        gy = max(-1.2, min(1.2, v("gaze_y")))
        cr.save()
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        self._blush(cr)
        for side, (ex, ey) in ((-1, EYE_L), (1, EYE_R)):
            # Eyes travel a little with the gaze; the whole face leans after
            # them by a fraction, which is what makes a glance read at 40px.
            cx, cy = ex + gx * 0.75, ey + gy * 0.5
            self._eye(cr, cx, cy, side)
            self._brow(cr, ex + gx * 0.3, side)
        self._mouth(cr, gx * 0.35, gy * 0.2)
        cr.restore()

    def _openness(self):
        lid = max(0.0, min(1.0, self.lid))
        return max(0.0, self.eye_h) * (1.0 - lid * 0.95)

    def _eye(self, cr, cx, cy, side):
        v = self.value
        scale = v("eye_scale")
        happy = _ramp(v("happy"), 0.35, 0.65)
        dizzy = _ramp(v("dizzy"), 0.3, 0.6)
        solid = (1.0 - _ramp(v("happy"), 0.3, 0.55)) * (1.0 - _ramp(v("dizzy"), 0.25, 0.5))
        open_ = self._openness()
        w, h = EYE_W * scale, EYE_H * scale

        if solid > 0.02 and open_ > 0.04:
            hh = h * min(1.0, open_)
            cr.save()
            # a rounded pill: soft at this size, and it squashes cleanly
            _pill(cr, cx, cy + (h - hh) * 0.18, w, hh)
            grad = cairo.LinearGradient(0, cy - hh / 2, 0, cy + hh / 2)
            grad.add_color_stop_rgba(0.0, *EYE_TOP, solid)
            grad.add_color_stop_rgba(1.0, *EYE_LOW, solid)
            cr.set_source(grad)
            cr.fill_preserve()
            cr.clip()
            # Shines stay put relative to the eye: the light is above-left,
            # not on the pupil, so they ride with the eye as it looks around.
            if hh > h * 0.45:
                a = solid * min(1.0, (hh / h - 0.45) / 0.3)
                cr.set_source_rgba(1, 1, 1, 0.92 * a)
                cr.arc(cx - w * 0.2, cy - hh * 0.2, 0.62 * scale, 0, math.tau)
                cr.fill()
                cr.set_source_rgba(1, 1, 1, 0.55 * a)
                cr.arc(cx + w * 0.2, cy + hh * 0.22, 0.28 * scale, 0, math.tau)
                cr.fill()
            # Lids: body colour drawn over the top of the eye, tilted at the
            # inner corner. This is the squint, the frown and the sad droop.
            squint = max(0.0, v("squint"))
            tilt = v("lid_tilt")
            if squint > 0.01 or abs(tilt) > 0.01:
                top = cy - hh / 2
                inner = top + hh * (squint + max(0.0, tilt) * 0.35)
                outer = top + hh * (squint + max(0.0, -tilt) * 0.35)
                left, right = (outer, inner) if side < 0 else (inner, outer)
                cr.move_to(cx - w, top - 1)
                cr.line_to(cx + w, top - 1)
                cr.line_to(cx + w, right)
                cr.line_to(cx - w, left)
                cr.close_path()
                if self._skin is not None and solid > 0.98:
                    cr.set_source(self._skin)
                else:
                    cr.set_source_rgba(*LID, solid)
                cr.fill_preserve()
                cr.set_source_rgba(*INK, 0.55 * solid)
                cr.set_line_width(0.35)
                cr.new_path()
                cr.move_to(cx - w, left)
                cr.line_to(cx + w, right)
                cr.stroke()
            cr.restore()
        elif solid > 0.02:
            # closed: a soft downward curve, the sleeping / blinking eye
            cr.set_source_rgba(*INK, solid)
            cr.set_line_width(0.6)
            cr.move_to(cx - w * 0.45, cy)
            cr.curve_to(cx - w * 0.2, cy + 0.7, cx + w * 0.2, cy + 0.7, cx + w * 0.45, cy)
            cr.stroke()

        if happy > 0.02 and dizzy < 0.98:
            # ^ ^ : an upward arch, thick and round-capped
            cr.set_source_rgba(*INK, happy * (1.0 - dizzy))
            cr.set_line_width(0.75)
            cr.move_to(cx - w * 0.5, cy + 0.6)
            cr.curve_to(cx - w * 0.25, cy - 1.3, cx + w * 0.25, cy - 1.3, cx + w * 0.5, cy + 0.6)
            cr.stroke()

        if dizzy > 0.02:
            cr.save()
            cr.translate(cx, cy)
            cr.rotate(self.t * 5.0 * side)
            cr.set_source_rgba(*INK, dizzy)
            cr.set_line_width(0.45)
            turns, r = 2.2, 1.55
            steps = 40
            for i in range(steps + 1):
                u = i / steps
                ang = u * turns * math.tau
                rad = r * u
                x, y = math.cos(ang) * rad, math.sin(ang) * rad
                (cr.move_to if i == 0 else cr.line_to)(x, y)
            cr.stroke()
            cr.restore()

    def _brow(self, cr, ex, side):
        v = self.value
        raise_ = v("brow_y") + v("brow_asym") * 0.8 * side
        tilt = v("brow_tilt")
        y = BROW_Y - raise_ * 0.85
        half = BROW_LEN / 2
        inner_x, outer_x = ex - side * half, ex + side * half
        inner_y = y - tilt * 0.55
        outer_y = y + tilt * 0.25
        cr.set_source_rgba(*INK, 0.82)
        cr.set_line_width(0.62)
        cr.move_to(outer_x, outer_y)
        mid_x, mid_y = (inner_x + outer_x) / 2, min(inner_y, outer_y) - 0.25
        cr.curve_to(mid_x, mid_y, mid_x, mid_y, inner_x, inner_y)
        cr.stroke()

    def _mouth(self, cr, dx, dy):
        v = self.value
        smile = max(-1.0, min(1.0, v("smile")))
        open_ = max(0.0, min(1.0, v("open")))
        width = MOUTH_W * max(0.3, v("mouth_w"))
        wave = max(0.0, min(1.0, v("wave")))
        mx = MOUTH[0] + dx + v("mouth_x") * 0.9
        my = MOUTH[1] + dy
        half = width / 2
        bend = smile * 1.15                       # how far the middle dips

        if open_ > 0.06:
            depth = 0.6 + open_ * 2.4
            # top lip follows the smile; bottom lip hangs below it
            cr.move_to(mx - half, my - bend * 0.25)
            cr.curve_to(mx - half * 0.4, my + bend * 0.35, mx + half * 0.4, my + bend * 0.35,
                        mx + half, my - bend * 0.25)
            cr.curve_to(mx + half * 0.6, my + depth + bend * 0.4,
                        mx - half * 0.6, my + depth + bend * 0.4, mx - half, my - bend * 0.25)
            cr.close_path()
            cr.set_source_rgb(*MOUTH_IN)
            cr.fill_preserve()
            cr.save()
            cr.clip()
            cr.set_source_rgb(*TONGUE)
            cr.arc(mx, my + depth + bend * 0.25, half * 0.62, 0, math.tau)
            cr.fill()
            cr.restore()
            cr.set_source_rgb(*INK)
            cr.set_line_width(0.5)
            cr.stroke()
            return

        cr.set_source_rgb(*INK)
        cr.set_line_width(0.55)
        if wave > 0.05:
            # a wobbly line: unsure, queasy, dizzy
            steps = 16
            for i in range(steps + 1):
                u = i / steps
                x = mx - half + width * u
                y = my - bend * 0.3 * (1 - (2 * u - 1) ** 2) + math.sin(u * math.tau * 2) * 0.35 * wave
                (cr.move_to if i == 0 else cr.line_to)(x, y)
            cr.stroke()
            return
        cr.move_to(mx - half, my - bend * 0.3)
        cr.curve_to(mx - half * 0.4, my + bend * 0.45, mx + half * 0.4, my + bend * 0.45,
                    mx + half, my - bend * 0.3)
        cr.stroke()

    def _blush(self, cr):
        a = max(0.0, min(1.0, self.value("blush")))
        if a < 0.02:
            return
        for bx in (11.9, 28.1):
            cr.save()
            cr.translate(bx, 21.4)
            cr.scale(1.8, 0.9)
            grad = cairo.RadialGradient(0, 0, 0, 0, 0, 1)
            grad.add_color_stop_rgba(0.0, *BLUSH, 0.8 * a)
            grad.add_color_stop_rgba(1.0, *BLUSH, 0.0)
            cr.set_source(grad)
            cr.arc(0, 0, 1, 0, math.tau)
            cr.fill()
            cr.restore()


def _ramp(x, lo, hi):
    """0 below `lo`, 1 above `hi`, smooth between."""
    u = max(0.0, min(1.0, (x - lo) / (hi - lo)))
    return u * u * (3 - 2 * u)


def _pill(cr, cx, cy, w, h):
    """A rounded rectangle whose corners are as round as its short side allows."""
    r = min(w, h) * 0.45
    x0, y0, x1, y1 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
    cr.new_path()
    cr.arc(x1 - r, y0 + r, r, -math.pi / 2, 0)
    cr.arc(x1 - r, y1 - r, r, 0, math.pi / 2)
    cr.arc(x0 + r, y1 - r, r, math.pi / 2, math.pi)
    cr.arc(x0 + r, y0 + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()
