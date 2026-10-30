"""Rounded monitor border geometry, independent of GTK and the animation rig.

Distance increases from the floor towards the right wall, then the ceiling and
left wall. Angles turn the upright body so its feet face the nearest edge.
"""

import math


class Border:
    def __init__(self, width, height, body_w, body_h, margin=6):
        self.width, self.height = width, height
        self.body_w, self.body_h, self.margin = body_w, body_h, margin
        self.left = self.top = body_h / 2 + margin
        self.right = max(self.left + 2, width - self.left)
        self.bottom = max(self.top + 2, height - self.top)
        self.radius = min(body_w / 2, (self.right - self.left) / 2,
                          (self.bottom - self.top) / 2)
        r = self.radius
        self.horizontal = self.right - self.left - 2 * r
        self.vertical = self.bottom - self.top - 2 * r
        self.arc = math.pi * r / 2
        self.length = 2 * (self.horizontal + self.vertical) + 4 * self.arc

    def pose(self, distance):
        """Centre x/y, body rotation and supporting edge at this distance."""
        d = distance % self.length
        r, h, v, a = self.radius, self.horizontal, self.vertical, self.arc
        # Each straight is followed by a quarter circle with a continuous tangent.
        straights = (
            (h, self.left + r, self.bottom, 1, 0, 0, "floor"),
            (v, self.right, self.bottom - r, 0, -1, -math.pi / 2, "right"),
            (h, self.right - r, self.top, -1, 0, -math.pi, "ceiling"),
            (v, self.left, self.top + r, 0, 1, -3 * math.pi / 2, "left"),
        )
        centres = ((self.right - r, self.bottom - r),
                   (self.right - r, self.top + r),
                   (self.left + r, self.top + r),
                   (self.left + r, self.bottom - r))
        for i, (length, x, y, dx, dy, angle, edge) in enumerate(straights):
            if d < length:
                return x + dx * d, y + dy * d, angle, edge
            d -= length
            if d < a:
                theta = math.pi / 2 - i * math.pi / 2 - d / r
                cx, cy = centres[i]
                rotation = angle - d / r
                x, y = cx + r * math.cos(theta), cy + r * math.sin(theta)
                # A rotating rectangle is wider than either upright dimension.
                # Roll inward enough that its outside corners never hit a panel.
                hx = (abs(math.cos(rotation)) * self.body_w
                      + abs(math.sin(rotation)) * self.body_h) / 2 + self.margin
                hy = (abs(math.sin(rotation)) * self.body_w
                      + abs(math.cos(rotation)) * self.body_h) / 2 + self.margin
                x = max(hx, min(self.width - hx, x))
                y = max(hy, min(self.height - hy, y))
                return x, y, rotation, edge
            d -= a
        return self.left + r, self.bottom, 0.0, "floor"

    def floor_distance(self, x):
        return min(self.horizontal, max(0.0, x - self.left - self.radius))

    def location(self, distance):
        """Stable edge and fraction, independent of resolution and body size."""
        d = distance % self.length
        for edge, length in self.sections():
            if d < length - 1e-9:
                return edge, max(0.0, d) / length
            d -= length
        return "floor", 0.0

    def sections(self):
        return (("floor", self.horizontal + self.arc),
                ("right", self.vertical + self.arc),
                ("ceiling", self.horizontal + self.arc),
                ("left", self.vertical + self.arc))

    def distance(self, edge, fraction):
        if edge not in ("floor", "right", "ceiling", "left"):
            raise ValueError("unknown border edge")
        fraction = float(fraction)
        if not math.isfinite(fraction):
            raise ValueError("invalid border position")
        start = 0.0
        for name, length in self.sections():
            if name == edge:
                return (start + min(1.0, max(0.0, fraction)) * length) % self.length
            start += length

    def nearest(self, x, y):
        """Project onto the actual path, including its inward corner roll.

        A bounded coarse search finds the closest section; refinement also works
        across the lap seam and the clamped parts of the rounded corners.
        """
        def error(d):
            px, py, _angle, _edge = self.pose(d)
            return (px - x) ** 2 + (py - y) ** 2

        step = self.length / 256
        best = min(range(256), key=lambda i: error(i * step)) * step
        lo, hi = best - step, best + step
        for _ in range(36):
            a, b = (2 * lo + hi) / 3, (lo + 2 * hi) / 3
            if error(a) <= error(b):
                hi = b
            else:
                lo = a
        return ((lo + hi) / 2) % self.length


def readout_placement(body, edge, size, window, gap=10):
    """Upright inward readout: rect and the side whose tail points at the body."""
    bx, by, bw, bh = body
    w, h = size
    ww, wh = window
    cx, cy = bx + bw / 2, by + bh / 2
    if edge == "left":
        x, y, tail = bx + bw + gap, cy - h / 2, "left"
    elif edge == "right":
        x, y, tail = bx - w - gap, cy - h / 2, "right"
    elif edge == "ceiling" or (by - gap - h < 2 and by + bh + gap + h <= wh - 2):
        x, y, tail = cx - w / 2, by + bh + gap, "top"
    else:
        x, y, tail = cx - w / 2, by - gap - h, "bottom"
    return (int(max(2, min(ww - w - 2, x))), int(max(2, min(wh - h - 2, y))), w, h), tail


def sleep_trail(body, glyph, now, hanging, window):
    """Upright zzz drift down from a bat nap, up from an ordinary nap."""
    x, y, w, h = body
    gw, gh = glyph
    ww, wh = window
    phase = now % 3.0
    out = []
    for i in range(3):
        t = (phase - i * 0.9) / 2.4
        if not 0 < t < 1:
            continue
        size = 0.6 + t
        zx = x + w + 4 + t * 26
        if zx + gw * size > ww - 2:
            zx = x - gw * size - 4 - t * 26
        zy = y + h * (0.6 if hanging else 0.2) + t * (46 if hanging else -46)
        out.append((max(2, min(ww - gw * size - 2, zx)),
                    max(2, min(wh - gh * size - 2, zy)), size, 1.0 - t))
    return out

