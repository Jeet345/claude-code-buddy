"""Costumes drawn as part of him, not pinned on top of him.

The first costumes were flat props placed at keyframed cells and drawn after
the body, in window space. So a hat hovered while he bobbed, a pencil floated
a cell away from his hand, and nothing squashed, leaned or breathed with him.

Here a costume is drawn inside the rig's own transform, in the same 40x32 grid
as the body: hats sit on the top edge of his head at y=12, tools hang from the
end of his arm wherever the arm spring has put it, and every prop is lit from
the same upper left as the clay. Each costume also *does* something - the
wrench strikes and throws sparks, the pancake flips and his eyes follow it -
and steers his arms and gaze while it does.

    wardrobe = Wardrobe()
    wardrobe.update(dt, "build", rig)    # before the rig steps its springs
    wardrobe.draw(cr, rig, skin)         # after the face, inside the transform
"""

import math
import random

import cairo

from vector_motion import Spring

NAMES = ("read", "write", "build", "scan", "cook", "listen")
BEAT = 2.0                          # Hz: 120bpm, a comfortable nod

# Body motion while each costume is on, merged into vector.ANIM_MOTION.
MOTION = {
    "read":  {"breath": 0.22, "eye": 1.00, "arm": 0.25},
    "write": {"breath": 0.35, "eye": 0.95, "stretch": 0.97},
    "build": {"breath": 0.55, "eye": 1.00, "stretch": 0.96,
              "osc": [("bob", 0.35, 1.6, "sine")]},
    "scan":  {"breath": 0.35, "eye": 1.08},
    "cook":  {"breath": 0.45, "eye": 1.00, "osc": [("bob", 0.3, 1.1, "sine")]},
}

INK = (0.20, 0.13, 0.10)            # prop outlines: warm, never pure black
BODY_INK = (0.494, 0.227, 0.141)    # his own outline, for the fist over a grip
HAND_R = (32.6, 22.0)               # end of the right arm at rest
HAND_L = (7.4, 22.0)
ARM_TRAVEL = 2.2                    # grid units per unit of arm raise (vector.py)
HEAD_TOP = 12.0


def _lighter(c, k):
    return tuple(v + (1.0 - v) * k for v in c)


def _darker(c, k):
    return tuple(v * (1.0 - k) for v in c)


def _lit(x0, y0, x1, y1, base, hi=0.35, lo=0.30):
    """The body's key light, for a prop: lit upper left, dim lower right."""
    g = cairo.LinearGradient(x0, y0, x1, y1)
    g.add_color_stop_rgb(0.0, *_lighter(base, hi))
    g.add_color_stop_rgb(0.45, *base)
    g.add_color_stop_rgb(1.0, *_darker(base, lo))
    return g


def _ink(cr, width=0.45, colour=INK):
    cr.set_source_rgb(*colour)
    cr.set_line_width(width)
    cr.stroke()


def _rrect(cr, x, y, w, h, r):
    r = min(r, w / 2.0, h / 2.0)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


def _glare(cr, x, y, w, h, alpha=0.45):
    g = cairo.RadialGradient(x, y, 0, x, y, max(w, h))
    g.add_color_stop_rgba(0, 1, 1, 1, alpha)
    g.add_color_stop_rgba(1, 1, 1, 1, 0.0)
    cr.save()
    cr.translate(x, y)
    cr.scale(w, h)
    cr.arc(0, 0, 1, 0, math.tau)
    cr.restore()
    cr.set_source(g)
    cr.fill()


def _ease(t):
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


class Wardrobe:
    def __init__(self, seed=None):
        self.name = None
        self.t = 0.0
        self.on = Spring(0.0, k=95.0, damping=0.55)      # 0 off .. 1 worn
        self.hat = Spring(0.0, k=150.0, damping=0.30)    # degrees of hat wobble
        self.particles = []      # [x, y, vx, vy, life, max_life, kind, spin]
        self.ink = []            # pencil strokes on the pad, as point lists
        self._rand = random.Random(seed)
        self._struck = False
        self._line = -1
        self._flip_seen = 0

    # -- motion ------------------------------------------------------------

    def update(self, dt, name, rig):
        if name not in NAMES:
            name = None
        if name != self.name:
            self.name = name
            self.t = 0.0
            self.on.x, self.on.v = 0.0, 0.0
            self.particles = []
            self.ink = []
            self._line = -1
            self._flip_seen = 0
        self.on.target = 1.0 if name else 0.0
        self.on.step(dt)
        # the hat rides his bob a beat late, and rings on a landing
        self.hat.nudge(-rig.p["bob"].v * 0.05 + rig.jiggle.v * 0.04)
        self.hat.step(dt)
        self._step_particles(dt)
        if name is None:
            return
        self.t += dt
        getattr(self, "_move_" + name)(dt, rig)

    def _offset(self, rig, param, value):
        rig.offsets[param] = rig.offsets.get(param, 0.0) + value

    def _move_read(self, dt, rig):
        # eyes run along a line, snap back to the start of the next
        line = (self.t / 1.7) % 1.0
        rig.face.look(-0.7 + 1.4 * line, 0.75, hold=0.15)

    def _move_write(self, dt, rig):
        wx, wy = self._pencil_tip()
        self._offset(rig, "arm_r_raise", -(wy - HAND_R[1]) * 0.22)
        rig.face.look(0.35 + (wx - 26.0) * 0.08, 0.85, hold=0.15)
        line = int(self.t / self.LINE_SECS) % (self.LINES + 1)
        if line != self._line:
            self._line = line
            if line == 0:
                self.ink = []
            if line < self.LINES:
                self.ink.append([])
        if self._line < self.LINES and self.ink:
            self.ink[-1].append((wx, wy))

    def _move_build(self, dt, rig):
        p = (self.t * 1.5) % 1.0
        up = _ease(p / 0.7) if p < 0.7 else 1.0 - _ease((p - 0.7) / 0.12)
        self._offset(rig, "arm_r_raise", 0.2 + up * 0.7)
        struck = p >= 0.82
        if struck and not self._struck:
            hx, hy = self._wrench_head(rig)
            for _ in range(7):
                a = self._rand.uniform(-math.pi * 0.95, -math.pi * 0.05)
                v = self._rand.uniform(10.0, 22.0)
                self.particles.append([hx, hy, math.cos(a) * v, math.sin(a) * v,
                                       0.35, 0.35, "spark", 0.0])
            rig.p["bob"].nudge(4.0)
        self._struck = struck
        rig.face.look(0.6, -0.2, hold=0.15)

    def _move_scan(self, dt, rig):
        self._offset(rig, "arm_r_raise", 0.9)
        a = self._scan_angle()
        rig.face.look(0.1 + (a + 52.0) / 30.0 * 0.8, -0.3, hold=0.15)

    def _move_cook(self, dt, rig):
        p, flip = self._flip_phase()
        if flip != self._flip_seen and p >= 0.86:
            self._flip_seen = flip
            rig.p["bob"].nudge(3.0)
        jerk = 1.0 - _ease((p - 0.22) / 0.1) if 0.18 <= p < 0.32 else 0.0
        self._offset(rig, "arm_r_raise", 0.15 + jerk * 0.6)
        if 0.25 <= p < 0.86:
            _, fy, _ = self._pancake(p)
            rig.face.look(0.0, max(-1.0, (fy - 18.0) / 9.0), hold=0.15)
        else:
            rig.face.look(0.0, 0.8, hold=0.15)
        if self._rand.random() < dt * 5.0:
            self.particles.append([self._rand.uniform(16.5, 23.5), 23.6, self._rand.uniform(-0.4, 0.4),
                                   -self._rand.uniform(2.5, 4.0), 1.4, 1.4, "steam",
                                   self._rand.uniform(0, math.tau)])

    def _move_listen(self, dt, rig):
        # nod on every beat, sway across two, a toe-tap of the arms
        beat = self.t * BEAT
        nod = abs(math.sin(math.pi * beat))
        self._offset(rig, "bob", nod * 0.75)
        self._offset(rig, "tilt", math.sin(math.pi * beat / 2.0) * 2.4)
        self._offset(rig, "arm_l_raise", (1.0 - nod) * 0.25)
        self._offset(rig, "arm_r_raise", (1.0 - nod) * 0.25)
        rig.face.look(0.0, -0.15, hold=0.15)
        if int(beat) != self._flip_seen:                 # a note per beat
            self._flip_seen = int(beat)
            side = -1 if self._flip_seen % 2 else 1
            self.particles.append([20.0 + side * 12.4, 14.5, side * self._rand.uniform(0.8, 1.8),
                                   -self._rand.uniform(3.0, 4.2), 1.8, 1.8,
                                   "note" if self._rand.random() < 0.7 else "note2",
                                   self._rand.uniform(0, math.tau)])

    def _step_particles(self, dt):
        alive = []
        for p in self.particles:
            p[4] -= dt
            if p[4] <= 0:
                continue
            p[0] += p[2] * dt
            p[1] += p[3] * dt
            if p[6] == "spark":
                p[3] += 60.0 * dt
            else:
                p[7] += dt * 2.0
                p[0] += math.sin(p[7]) * dt * 0.8
            alive.append(p)
        self.particles = alive

    # -- geometry shared by motion and drawing ------------------------------

    LINES = 3
    LINE_SECS = 1.5

    def _pencil_tip(self):
        # wobbly handwriting along a ruled line on the pad, then the next line
        line = int(self.t / self.LINE_SECS) % (self.LINES + 1)
        f = (self.t % self.LINE_SECS) / self.LINE_SECS
        if line >= self.LINES:                       # pause, lift, tear the page
            return 27.0, 24.0 - math.sin(f * math.pi) * 1.5
        x = 22.6 + f * 6.2
        y = 25.3 + line * 1.25 + math.sin(self.t * 17.0) * 0.32 * (0.6 + 0.4 * math.sin(self.t * 5.3))
        return x, y

    @staticmethod
    def _hand(rig):
        return HAND_R[0], HAND_R[1] - rig.arm_r.x * ARM_TRAVEL

    def _wrench_angle(self):
        p = (self.t * 1.5) % 1.0
        if p < 0.7:
            return -10.0 - 45.0 * _ease(p / 0.7)          # wind up, slowly
        if p < 0.82:
            return -55.0 + 85.0 * _ease((p - 0.7) / 0.12)  # strike, fast
        return 30.0 - 40.0 * _ease((p - 0.82) / 0.18)     # recoil to rest

    def _wrench_head(self, rig):
        hx, hy = self._hand(rig)
        a = math.radians(self._wrench_angle())
        return hx + math.sin(a) * 8.6, hy - math.cos(a) * 8.6

    def _scan_angle(self):
        return -52.0 + 30.0 * math.sin(self.t * 1.1)

    FLIP_SECS = 1.9

    def _flip_phase(self):
        n = self.t / self.FLIP_SECS
        return n % 1.0, int(n)

    @staticmethod
    def _pancake(p):
        """(x, y, turn) of the pancake: resting, then a flight, then caught."""
        rest_y = 23.4
        if p < 0.25 or p >= 0.86:
            return 20.0, rest_y, 0.0
        f = (p - 0.25) / 0.61
        y = rest_y - 4.0 * 25.0 * f * (1.0 - f)     # clears the hat, never rests on it
        return 20.0 + math.sin(f * math.pi) * 1.2, y, f * math.pi * 2.0

    # -- drawing -------------------------------------------------------------

    def draw(self, cr, rig, skin):
        if self.name is None:
            return
        on = max(0.0, min(1.2, self.on.x))
        if on < 0.02:
            return
        cr.save()
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_fill_rule(cairo.FILL_RULE_WINDING)
        getattr(self, "_draw_" + self.name)(cr, rig, skin, on)
        self._draw_particles(cr)
        cr.restore()

    def _hat_space(self, cr, on):
        """Drop onto the head from above, then wobble about its front edge."""
        cr.translate(20.0, HEAD_TOP)
        cr.rotate(math.radians(self.hat.x))
        cr.translate(-20.0, -HEAD_TOP - (1.0 - min(1.0, on)) * 9.0)

    def _pop(self, cr, x, y, on):
        cr.translate(x, y)
        cr.scale(on, on)
        cr.translate(-x, -y)

    def _fist(self, cr, x, y, skin):
        cr.arc(x, y, 1.45, 0, math.tau)
        cr.set_source(skin)
        cr.fill_preserve()
        _ink(cr, 0.5, BODY_INK)

    # read: round glasses and an open book, eyes running along its lines
    def _draw_read(self, cr, rig, skin, on):
        cr.save()
        cr.translate(0.0, (1.0 - min(1.0, on)) * -4.0)
        frame = (0.30, 0.18, 0.12)
        for ex in (14.0, 26.0):
            cr.arc(ex, 18.0, 2.85, 0, math.tau)
            cr.set_source_rgba(0.85, 0.92, 1.0, 0.06)   # a hint of glass, not a tint
            cr.fill_preserve()
            _ink(cr, 0.55, frame)
            cr.save()
            cr.arc(ex, 18.0, 2.6, 0, math.tau)
            cr.clip()
            cr.move_to(ex - 2.2, 17.6)
            cr.line_to(ex - 0.6, 15.6)
            cr.set_source_rgba(1, 1, 1, 0.5)
            cr.set_line_width(0.45)
            cr.stroke()
            cr.restore()
        cr.move_to(16.85, 17.7)
        cr.curve_to(18.4, 16.6, 21.6, 16.6, 23.15, 17.7)
        _ink(cr, 0.5, frame)
        for x0, x1 in ((11.15, 10.1), (28.85, 29.9)):
            cr.move_to(x0, 17.4)
            cr.line_to(x1, 16.9)
            _ink(cr, 0.5, frame)
        cr.restore()

        cr.save()
        self._pop(cr, 20.0, 27.0, min(1.0, on))
        cover = (0.62, 0.20, 0.18)
        cr.move_to(11.6, 22.6)
        cr.line_to(20.0, 23.6)
        cr.line_to(28.4, 22.6)
        cr.line_to(28.6, 29.0)
        cr.line_to(20.0, 29.8)
        cr.line_to(11.4, 29.0)
        cr.close_path()
        cr.set_source(_lit(11, 22, 29, 30, cover))
        cr.fill_preserve()
        _ink(cr, 0.45)
        paper = (0.97, 0.94, 0.86)
        for side in (-1, 1):
            cr.move_to(20.0, 24.0)
            cr.curve_to(20.0 + side * 3.0, 22.9, 20.0 + side * 6.0, 22.9, 20.0 + side * 7.9, 23.2)
            cr.line_to(20.0 + side * 7.9, 28.5)
            cr.curve_to(20.0 + side * 6.0, 28.2, 20.0 + side * 3.0, 28.2, 20.0, 29.2)
            cr.close_path()
            cr.set_source(_lit(12, 23, 28, 29, paper, hi=0.2, lo=0.12))
            cr.fill_preserve()
            _ink(cr, 0.35)
            cr.set_source_rgba(0.35, 0.33, 0.40, 0.55)
            cr.set_line_width(0.28)
            for i in range(4):
                y = 24.6 + i * 1.0
                cr.move_to(20.0 + side * 1.4, y + 0.15)
                cr.line_to(20.0 + side * (6.8 - (i == 3) * 2.0), y)
                cr.stroke()
        # a page turning over, every few lines
        turn = (self.t % 5.1) / 0.6
        if turn < 1.0:
            f = _ease(turn)
            edge = 20.0 + 7.9 * math.cos(f * math.pi)
            lift = math.sin(f * math.pi) * 1.6
            cr.move_to(20.0, 24.0)
            cr.curve_to((20.0 + edge) / 2, 23.0 - lift, edge, 23.2 - lift, edge, 23.2 - lift * 0.5)
            cr.line_to(edge, 28.5 - lift * 0.5)
            cr.curve_to(edge, 28.4 - lift, (20.0 + edge) / 2, 28.4 - lift, 20.0, 29.2)
            cr.close_path()
            cr.set_source_rgb(*_darker(paper, 0.08 + 0.1 * math.sin(f * math.pi)))
            cr.fill_preserve()
            _ink(cr, 0.35)
        cr.restore()

    # write: a notepad, a pencil in his hand, ink appearing as he goes
    def _draw_write(self, cr, rig, skin, on):
        cr.save()
        self._pop(cr, 25.5, 27.0, min(1.0, on))
        pad = (0.99, 0.93, 0.55)
        _rrect(cr, 21.4, 23.2, 9.0, 6.6, 0.5)
        cr.set_source(_lit(21, 23, 30, 30, pad, hi=0.25, lo=0.15))
        cr.fill_preserve()
        _ink(cr, 0.45)
        _rrect(cr, 21.4, 23.2, 9.0, 0.9, 0.4)            # glued top strip
        cr.set_source_rgb(0.78, 0.30, 0.26)
        cr.fill()
        cr.set_line_width(0.2)
        cr.set_source_rgba(0.40, 0.55, 0.80, 0.55)
        for i in range(4):
            y = 25.6 + i * 1.25
            cr.move_to(21.9, y)
            cr.line_to(29.9, y)
            cr.stroke()
        cr.set_source_rgba(0.85, 0.30, 0.30, 0.5)
        cr.move_to(22.7, 24.2)
        cr.line_to(22.7, 29.6)
        cr.stroke()
        cr.set_source_rgb(0.22, 0.20, 0.30)
        cr.set_line_width(0.24)
        for stroke in self.ink:
            if len(stroke) > 1:
                cr.move_to(*stroke[0])
                for pt in stroke[1:]:
                    cr.line_to(*pt)
                cr.stroke()
        cr.restore()

        # pencil: tip on the paper, shaft back through his fist
        tx, ty = self._pencil_tip()
        hx, hy = self._hand(rig)
        dx, dy = hx - tx, hy - ty
        dist = math.hypot(dx, dy) or 1.0
        length = max(8.5, dist + 1.8)
        cr.save()
        cr.translate(tx, ty)
        cr.rotate(math.atan2(dy, dx))
        self._pop(cr, 0.0, 0.0, min(1.0, on))
        cr.move_to(0.0, 0.0)                              # graphite
        cr.line_to(0.9, -0.25)
        cr.line_to(0.9, 0.25)
        cr.close_path()
        cr.set_source_rgb(0.25, 0.25, 0.28)
        cr.fill()
        cr.move_to(0.9, -0.25)                            # shaved wood
        cr.line_to(2.2, -0.62)
        cr.line_to(2.2, 0.62)
        cr.line_to(0.9, 0.25)
        cr.close_path()
        cr.set_source_rgb(0.93, 0.78, 0.58)
        cr.fill_preserve()
        _ink(cr, 0.25)
        _rrect(cr, 2.2, -0.62, length - 3.6, 1.24, 0.15)  # yellow body
        g = cairo.LinearGradient(0, -0.62, 0, 0.62)
        g.add_color_stop_rgb(0.0, 1.0, 0.86, 0.35)
        g.add_color_stop_rgb(0.5, 0.97, 0.74, 0.16)
        g.add_color_stop_rgb(1.0, 0.80, 0.55, 0.10)
        cr.set_source(g)
        cr.fill_preserve()
        _ink(cr, 0.3)
        cr.rectangle(length - 1.4, -0.62, 0.6, 1.24)      # ferrule
        cr.set_source_rgb(0.74, 0.76, 0.80)
        cr.fill()
        _rrect(cr, length - 0.85, -0.62, 1.1, 1.24, 0.45)  # eraser
        cr.set_source_rgb(0.95, 0.55, 0.60)
        cr.fill_preserve()
        _ink(cr, 0.3)
        cr.restore()
        self._fist(cr, hx, hy, skin)

    # build: a hard hat, and a wrench struck down hard enough to spark
    def _draw_build(self, cr, rig, skin, on):
        worn = min(1.0, on)
        # the brim's shadow on his forehead, fading in as the hat lands
        cr.save()
        _rrect(cr, 10.0, 12.0, 20.0, 16.0, 1.6)
        cr.clip()
        g = cairo.LinearGradient(0, 12.0, 0, 15.2)
        g.add_color_stop_rgba(0.0, 0.25, 0.10, 0.05, 0.45 * worn ** 3)
        g.add_color_stop_rgba(1.0, 0.25, 0.10, 0.05, 0.0)
        cr.set_source(g)
        cr.paint()
        cr.restore()

        cr.save()
        self._hat_space(cr, on)
        yellow = (0.99, 0.77, 0.12)
        shade = (0.86, 0.55, 0.06)

        # dome: a little flatter on top than a half-ellipse, as real ones are
        def dome():
            cr.move_to(11.0, 12.2)
            cr.curve_to(10.6, 7.2, 13.6, 4.2, 20.0, 4.2)
            cr.curve_to(26.4, 4.2, 29.4, 7.2, 29.0, 12.2)
            cr.close_path()

        dome()
        g = cairo.RadialGradient(16.0, 6.0, 0.5, 20.0, 9.0, 11.0)
        g.add_color_stop_rgb(0.0, *_lighter(yellow, 0.45))
        g.add_color_stop_rgb(0.45, *yellow)
        g.add_color_stop_rgb(1.0, *shade)
        cr.set_source(g)
        cr.fill_preserve()
        _ink(cr, 0.5)

        # side ribs and the centre ridge, moulded into the shell
        cr.save()
        dome()
        cr.clip()
        for x, bend in ((15.2, -1.6), (24.8, 1.6)):
            cr.move_to(x + bend * 0.2, 4.8)
            cr.curve_to(x + bend, 7.0, x + bend, 9.8, x + bend * 0.8, 12.2)
            cr.set_source_rgba(*_darker(shade, 0.25), 0.55)
            cr.set_line_width(0.4)
            cr.stroke()
            cr.move_to(x + bend * 0.2 - 0.35, 4.9)
            cr.curve_to(x + bend - 0.35, 7.0, x + bend - 0.35, 9.8, x + bend * 0.8 - 0.35, 12.1)
            cr.set_source_rgba(1, 1, 1, 0.3)
            cr.set_line_width(0.25)
            cr.stroke()
        cr.restore()
        cr.move_to(18.6, 12.0)                            # ridge, narrowing at the top
        cr.curve_to(18.6, 8.0, 18.9, 5.0, 20.0, 4.0)
        cr.curve_to(21.1, 5.0, 21.4, 8.0, 21.4, 12.0)
        cr.close_path()
        cr.set_source(_lit(18.6, 4, 21.4, 12, _lighter(yellow, 0.12), hi=0.4))
        cr.fill_preserve()
        _ink(cr, 0.35)

        # emblem: a little spark badge on the front
        cr.arc(20.0, 9.6, 1.25, 0, math.tau)
        cr.set_source_rgb(0.98, 0.95, 0.88)
        cr.fill_preserve()
        _ink(cr, 0.3)
        cr.set_source_rgb(0.86, 0.42, 0.28)
        cr.set_line_width(0.3)
        for k in range(4):
            ang = k * math.pi / 4.0
            cr.move_to(20.0 - math.cos(ang) * 0.85, 9.6 - math.sin(ang) * 0.85)
            cr.line_to(20.0 + math.cos(ang) * 0.85, 9.6 + math.sin(ang) * 0.85)
        cr.stroke()

        # curved highlight following the shell, and a small hot spot
        cr.move_to(12.6, 9.6)
        cr.curve_to(12.8, 6.6, 15.0, 5.2, 17.4, 5.0)
        cr.set_source_rgba(1, 1, 1, 0.55)
        cr.set_line_width(0.6)
        cr.stroke()
        _glare(cr, 14.6, 6.6, 0.8, 0.5, 0.7)

        # brim: wider at the front and dipping in the middle, with its edge
        cr.move_to(8.0, 12.0)
        cr.curve_to(12.0, 11.3, 28.0, 11.3, 32.0, 12.0)
        cr.curve_to(32.6, 12.3, 32.4, 13.0, 31.6, 13.0)
        cr.curve_to(27.0, 13.9, 13.0, 13.9, 8.4, 13.0)
        cr.curve_to(7.6, 13.0, 7.4, 12.3, 8.0, 12.0)
        cr.close_path()
        cr.set_source(_lit(8, 11.3, 32, 13.9, yellow, hi=0.3, lo=0.35))
        cr.fill_preserve()
        _ink(cr, 0.5)
        cr.move_to(9.0, 12.15)                            # the brim's lit top edge
        cr.curve_to(13.0, 11.6, 27.0, 11.6, 31.0, 12.15)
        cr.set_source_rgba(1, 1, 1, 0.4)
        cr.set_line_width(0.3)
        cr.stroke()
        cr.restore()

        hx, hy = self._hand(rig)
        cr.save()
        cr.translate(hx, hy)
        cr.rotate(math.radians(self._wrench_angle()))
        cr.scale(min(1.0, on), min(1.0, on))
        steel = (0.70, 0.73, 0.78)
        _rrect(cr, -0.95, -7.4, 1.9, 8.6, 0.9)
        cr.set_source(_lit(-1, -8, 1, 1, steel))
        cr.fill_preserve()
        _ink(cr, 0.45)
        cr.arc(0.0, -8.6, 2.4, 0, math.tau)
        cr.rectangle(-0.8, -11.6, 1.6, 2.8)               # the open jaw
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        cr.set_source(_lit(-2, -10.5, 2, -6.4, steel))
        cr.fill_preserve()
        cr.set_fill_rule(cairo.FILL_RULE_WINDING)
        _ink(cr, 0.4)
        cr.restore()
        self._fist(cr, hx, hy, skin)

    # scan: a magnifying glass swept across the view, eyes following it
    def _draw_scan(self, cr, rig, skin, on):
        hx, hy = self._hand(rig)
        cr.save()
        cr.translate(hx, hy)
        cr.rotate(math.radians(self._scan_angle()))
        cr.scale(min(1.0, on), min(1.0, on))
        _rrect(cr, -0.65, -5.2, 1.3, 6.2, 0.6)             # handle
        cr.set_source(_lit(-1, -5, 1, 1, (0.45, 0.25, 0.15)))
        cr.fill_preserve()
        _ink(cr, 0.4)
        cr.arc(0.0, -9.4, 4.1, 0, math.tau)               # rim
        cr.arc_negative(0.0, -9.4, 3.3, math.tau, 0)
        cr.set_source(_lit(-4, -13.5, 4, -5.3, (0.80, 0.66, 0.32)))
        cr.fill()
        cr.arc(0.0, -9.4, 3.3, 0, math.tau)               # glass
        g = cairo.RadialGradient(-1.0, -10.6, 0.2, 0.0, -9.4, 3.3)
        g.add_color_stop_rgba(0.0, 0.92, 0.97, 1.0, 0.45)
        g.add_color_stop_rgba(1.0, 0.55, 0.75, 0.95, 0.22)
        cr.set_source(g)
        cr.fill()
        cr.arc(0.0, -9.4, 4.1, 0, math.tau)
        _ink(cr, 0.45)
        cr.arc(0.0, -9.4, 3.3, 0, math.tau)
        _ink(cr, 0.3)
        cr.arc(0.0, -9.4, 2.5, math.pi * 1.05, math.pi * 1.45)
        cr.set_source_rgba(1, 1, 1, 0.75)
        cr.set_line_width(0.4)
        cr.stroke()
        cr.restore()
        self._fist(cr, hx, hy, skin)

    # listen: over-ear headphones, the band clear of his head, cups at his sides
    def _draw_listen(self, cr, rig, skin, on):
        cr.save()
        cr.translate(0.0, -(1.0 - min(1.0, on)) * 9.0)
        shell = (0.24, 0.25, 0.30)
        accent = (0.30, 0.78, 0.72)
        band = ((9.6, 14.4), (9.6, 4.6), (30.4, 4.6), (30.4, 14.4))

        def arc():
            cr.move_to(*band[0])
            cr.curve_to(*band[1], *band[2], *band[3])

        arc()
        _ink(cr, 2.0)
        arc()
        cr.set_source_rgb(*shell)
        cr.set_line_width(1.25)
        cr.stroke()
        cr.move_to(10.2, 12.6)                            # sheen along the top
        cr.curve_to(10.6, 6.0, 18.0, 5.3, 21.0, 5.6)
        cr.set_source_rgba(1, 1, 1, 0.28)
        cr.set_line_width(0.35)
        cr.stroke()
        _rrect(cr, 16.0, 6.0, 8.0, 1.9, 0.9)              # padded crown
        cr.set_source(_lit(16, 6, 24, 8, accent))
        cr.fill_preserve()
        _ink(cr, 0.4)

        for side in (-1, 1):
            cx = 20.0 + side * 10.4
            x0 = cx - 1.7
            _rrect(cr, x0 + side * 0.9, 14.0, 2.6, 6.0, 1.1)   # cushion, against him
            cr.set_source(_lit(x0, 14, x0 + 3.4, 20, (0.42, 0.42, 0.46), hi=0.25))
            cr.fill_preserve()
            _ink(cr, 0.4)
            _rrect(cr, x0 - side * 0.5, 13.4, 3.4, 7.2, 1.4)   # shell
            cr.set_source(_lit(x0 - 1, 13, x0 + 3, 21, shell, hi=0.3))
            cr.fill_preserve()
            _ink(cr, 0.45)
            _rrect(cr, cx - side * 0.5 - 0.55, 15.0, 1.1, 4.0, 0.55)   # accent stripe
            cr.set_source(_lit(cx - 1, 15, cx + 1, 19, accent))
            cr.fill()
            _glare(cr, cx - side * 0.5 - 0.6, 14.4, 0.9, 0.5, 0.45)
        cr.restore()

    # cook: a chef's hat and a pan, flipping a pancake his eyes follow up
    def _draw_cook(self, cr, rig, skin, on):
        cr.save()
        self._hat_space(cr, on)
        white = (0.97, 0.96, 0.94)
        for cx, cy, r in ((15.4, 7.0, 3.2), (24.6, 7.0, 3.2), (20.0, 5.4, 3.9)):
            cr.new_sub_path()
            cr.arc(cx, cy, r, 0, math.tau)
        _rrect(cr, 13.0, 7.2, 14.0, 5.2, 0.6)
        cr.set_source(_lit(11, 1.5, 29, 12.5, white, hi=0.2, lo=0.16))
        cr.fill()
        for cx, cy, r in ((15.4, 7.0, 3.2), (24.6, 7.0, 3.2), (20.0, 5.4, 3.9)):
            cr.arc(cx, cy, r, math.pi * 0.95, math.pi * 2.05)
            _ink(cr, 0.45)
        _rrect(cr, 12.6, 9.0, 14.8, 3.5, 0.7)             # band
        cr.set_source(_lit(12, 9, 28, 12.5, _darker(white, 0.03), hi=0.2, lo=0.18))
        cr.fill_preserve()
        _ink(cr, 0.45)
        cr.set_source_rgba(*INK, 0.25)
        cr.set_line_width(0.25)
        for x in (16.2, 19.0, 21.8, 24.6):
            cr.move_to(x, 9.5)
            cr.line_to(x, 12.0)
            cr.stroke()
        _glare(cr, 18.2, 3.6, 1.8, 0.9, 0.55)
        cr.restore()

        p, _ = self._flip_phase()
        jerk = math.sin(min(1.0, max(0.0, (p - 0.18) / 0.14)) * math.pi) * 1.0
        hx, hy = self._hand(rig)
        cr.save()
        self._pop(cr, 20.0, 25.0, min(1.0, on))
        cr.translate(0.0, -jerk)
        cr.move_to(hx - 0.4, hy + 0.2)                    # handle
        cr.line_to(25.2, 24.6)
        cr.set_source_rgb(*INK)
        cr.set_line_width(1.5)
        cr.stroke()
        cr.move_to(hx - 0.4, hy + 0.2)
        cr.line_to(25.2, 24.6)
        cr.set_source_rgb(0.30, 0.30, 0.34)
        cr.set_line_width(0.85)
        cr.stroke()
        cr.save()                                         # bowl
        cr.translate(20.0, 24.6)
        cr.scale(5.6, 1.3)
        cr.arc(0, 0, 1, 0, math.pi)
        cr.restore()
        cr.line_to(14.4, 24.6)
        cr.close_path()
        cr.set_source(_lit(14, 24, 26, 26.5, (0.28, 0.28, 0.32), hi=0.25, lo=0.3))
        cr.fill_preserve()
        _ink(cr, 0.45)
        cr.save()                                         # rim, seen from above
        cr.translate(20.0, 24.3)
        cr.scale(5.6, 0.75)
        cr.arc(0, 0, 1, 0, math.tau)
        cr.restore()
        cr.set_source_rgb(0.16, 0.16, 0.19)
        cr.fill_preserve()
        _ink(cr, 0.4)
        cr.restore()

        fx, fy, turn = self._pancake(p)
        cr.save()
        cr.translate(fx, fy - jerk)
        squash = 1.0 + (0.25 if p >= 0.86 and p < 0.92 else 0.0)
        cr.scale(squash, 1.0)
        cr.save()
        cr.scale(3.0, 0.45 + 0.85 * abs(math.sin(turn)))
        cr.arc(0, 0, 1, 0, math.tau)
        cr.restore()
        top = math.cos(turn) >= 0
        gold = (0.93, 0.66, 0.30) if top else (0.80, 0.48, 0.18)
        cr.set_source(_lit(-3, -1, 3, 1, gold, hi=0.3, lo=0.25))
        cr.fill_preserve()
        _ink(cr, 0.35)
        cr.restore()
        self._fist(cr, hx, hy - jerk, skin)

    def _draw_particles(self, cr):
        for x, y, _vx, _vy, life, max_life, kind, spin in self.particles:
            f = life / max_life
            if kind == "spark":
                cr.set_source_rgba(1.0, 0.85, 0.35, f)
                cr.set_line_width(0.35)
                cr.move_to(x, y)
                cr.line_to(x - _vx * 0.025, y - _vy * 0.025)
                cr.stroke()
            elif kind.startswith("note"):
                self._draw_note(cr, x, y, f, spin, kind == "note2")
            else:
                r = 0.6 + (1.0 - f) * 1.4
                cr.set_source_rgba(1, 1, 1, 0.28 * f * (1.0 - f) * 4.0 * 0.5)
                cr.arc(x, y, r, 0, math.tau)
                cr.fill()

    @staticmethod
    def _draw_note(cr, x, y, f, spin, double):
        """A quaver (or a pair of them), cream with a dark edge so it shows on
        light and dark desktops alike."""
        alpha = min(1.0, f * 2.5) * min(1.0, (1.0 - f) * 6.0 + 0.2)
        cr.save()
        cr.translate(x, y)
        cr.rotate(math.sin(spin) * 0.3)
        heads = ((0.0, 0.0), (2.0, -0.5)) if double else ((0.0, 0.0),)
        for width, colour in ((0.75, INK), (0.32, (1.0, 0.96, 0.88))):
            cr.set_line_width(width)
            cr.set_source_rgba(*colour, alpha)
            for hx, hy in heads:
                cr.move_to(hx + 0.7, hy)
                cr.line_to(hx + 0.7, hy - 2.6)
            if double:
                cr.move_to(0.7, -2.6)
                cr.line_to(2.7, -3.1)
            else:
                cr.move_to(0.7, -2.6)
                cr.curve_to(1.6, -2.1, 1.9, -1.5, 1.5, -0.9)
            cr.stroke()
        for hx, hy in heads:
            cr.save()
            cr.translate(hx, hy)
            cr.rotate(-0.35)
            cr.scale(0.85, 0.62)
            cr.arc(0, 0, 1, 0, math.tau)
            cr.restore()
            cr.set_source_rgba(1.0, 0.96, 0.88, alpha)
            cr.fill_preserve()
            cr.set_source_rgba(*INK, alpha)
            cr.set_line_width(0.3)
            cr.stroke()
        cr.restore()
