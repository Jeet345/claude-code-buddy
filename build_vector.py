#!/usr/bin/env python3
"""Compile vector_art.svg into vector_paths.py.

Every path is normalised to move / cubic / close, so the renderer has exactly
three cases to handle and any two shapes with the same command sequence can be
interpolated. Ellipses and circles become four cubics.

Usage:  python3 build_vector.py [svg] [out]
"""

import math
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

SVG_NS = "{http://www.w3.org/2000/svg}"
KAPPA = 0.5522847498307936
NUMBER = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
TOKEN = re.compile(r"([MmLlHhVvCcSsQqTtAaZz])|([-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?)")


# -- colour --------------------------------------------------------------

def parse_colour(value):
    if not value or value == "none":
        return None
    value = value.strip()
    if not value.startswith("#"):
        raise ValueError(f"only hex colours are supported, got {value!r}")
    digits = value[1:]
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    if len(digits) != 6:
        raise ValueError(f"bad hex colour {value!r}")
    return tuple(int(digits[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


# -- path parsing --------------------------------------------------------

class PathParser:
    """Reduces an SVG `d` string to ('M', x, y) / ('C', x1,y1,x2,y2,x,y) / ('Z',)."""

    def __init__(self, d):
        self.tokens = [(c, n) for c, n in TOKEN.findall(d)]
        self.index = 0
        self.out = []
        self.cx = self.cy = 0.0
        self.sx = self.sy = 0.0
        self.prev_c2 = None   # for S
        self.prev_q = None    # for T

    def _number(self):
        while self.index < len(self.tokens):
            command, number = self.tokens[self.index]
            self.index += 1
            if number:
                return float(number)
            raise ValueError(f"expected a number, found command {command!r}")
        raise ValueError("path ended mid-command")

    def _peek_is_number(self):
        return self.index < len(self.tokens) and bool(self.tokens[self.index][1])

    def _line_to(self, x, y):
        # a straight line as a cubic keeps the segment vocabulary at one entry
        x0, y0 = self.cx, self.cy
        self.out.append(("C",
                         x0 + (x - x0) / 3.0, y0 + (y - y0) / 3.0,
                         x0 + 2.0 * (x - x0) / 3.0, y0 + 2.0 * (y - y0) / 3.0,
                         x, y))
        self.cx, self.cy = x, y
        self.prev_c2 = self.prev_q = None

    def _curve_to(self, x1, y1, x2, y2, x, y):
        self.out.append(("C", x1, y1, x2, y2, x, y))
        self.cx, self.cy = x, y
        self.prev_c2 = (x2, y2)
        self.prev_q = None

    def _quad_to(self, qx, qy, x, y):
        x0, y0 = self.cx, self.cy
        self.out.append(("C",
                         x0 + 2.0 / 3.0 * (qx - x0), y0 + 2.0 / 3.0 * (qy - y0),
                         x + 2.0 / 3.0 * (qx - x), y + 2.0 / 3.0 * (qy - y),
                         x, y))
        self.cx, self.cy = x, y
        self.prev_q = (qx, qy)
        self.prev_c2 = None

    def parse(self):
        command = None
        while self.index < len(self.tokens):
            token_command, _ = self.tokens[self.index]
            if token_command:
                command = token_command
                self.index += 1
            elif command is None:
                raise ValueError("path data starts with a number")
            elif command in "Mm":
                command = "L" if command == "M" else "l"

            relative = command.islower()
            upper = command.upper()
            ox, oy = (self.cx, self.cy) if relative else (0.0, 0.0)

            if upper == "M":
                x, y = self._number() + ox, self._number() + oy
                self.out.append(("M", x, y))
                self.cx = self.sx = x
                self.cy = self.sy = y
                self.prev_c2 = self.prev_q = None
            elif upper == "L":
                self._line_to(self._number() + ox, self._number() + oy)
            elif upper == "H":
                self._line_to(self._number() + ox, self.cy)
            elif upper == "V":
                self._line_to(self.cx, self._number() + oy)
            elif upper == "C":
                self._curve_to(self._number() + ox, self._number() + oy,
                               self._number() + ox, self._number() + oy,
                               self._number() + ox, self._number() + oy)
            elif upper == "S":
                if self.prev_c2 is None:
                    rx, ry = self.cx, self.cy
                else:
                    rx = 2.0 * self.cx - self.prev_c2[0]
                    ry = 2.0 * self.cy - self.prev_c2[1]
                self._curve_to(rx, ry,
                               self._number() + ox, self._number() + oy,
                               self._number() + ox, self._number() + oy)
            elif upper == "Q":
                self._quad_to(self._number() + ox, self._number() + oy,
                              self._number() + ox, self._number() + oy)
            elif upper == "T":
                if self.prev_q is None:
                    qx, qy = self.cx, self.cy
                else:
                    qx = 2.0 * self.cx - self.prev_q[0]
                    qy = 2.0 * self.cy - self.prev_q[1]
                self._quad_to(qx, qy, self._number() + ox, self._number() + oy)
            elif upper == "Z":
                self.out.append(("Z",))
                self.cx, self.cy = self.sx, self.sy
                self.prev_c2 = self.prev_q = None
            elif upper == "A":
                raise ValueError(
                    "arc commands are not supported -- redraw the segment as a "
                    "cubic in the SVG source")
            else:
                raise ValueError(f"unknown path command {command!r}")

            if upper == "Z" and not self._peek_is_number():
                command = None
        return self.out


def rounded_rect_segments(x, y, w, h, rx, ry):
    """Rounded rectangle as move/cubic/close.

    The corner radius is what turns the sprite's hard 40x32 blocks into
    something that reads as drawn rather than pixelated, so it is the one shape
    this compiler cares about most.
    """
    rx = max(0.0, min(rx, w / 2.0))
    ry = max(0.0, min(ry, h / 2.0))
    if rx <= 0.0 or ry <= 0.0:
        return rect_segments(x, y, w, h)
    k = KAPPA
    ox, oy = rx * k, ry * k
    x1, y1 = x + w, y + h
    return [
        ("M", x + rx, y),
        ("C", x + w - rx + ox, y, x1, y + ry - oy, x1, y + ry),
        ("C", x1, y + ry, x1, y1 - ry, x1, y1 - ry),
        ("C", x1, y1 - ry + oy, x + w - rx + ox, y1, x + w - rx, y1),
        ("C", x + w - rx, y1, x + rx, y1, x + rx, y1),
        ("C", x + rx - ox, y1, x, y1 - ry + oy, x, y1 - ry),
        ("C", x, y1 - ry, x, y + ry, x, y + ry),
        ("C", x, y + ry - oy, x + rx - ox, y, x + rx, y),
        ("Z",),
    ]


def rect_segments(x, y, w, h):
    """Axis-aligned rectangle as move/cubic/close, so rects share the segment
    vocabulary with everything else and can be morphed like any other shape."""
    corners = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    segments = [("M", *corners[0])]
    for index in range(1, 5):
        px, py = corners[index - 1]
        qx, qy = corners[index % 4]
        segments.append(("C",
                         px + (qx - px) / 3.0, py + (qy - py) / 3.0,
                         px + 2.0 * (qx - px) / 3.0, py + 2.0 * (qy - py) / 3.0,
                         qx, qy))
    segments.append(("Z",))
    return segments


def ellipse_segments(cx, cy, rx, ry):
    ox, oy = rx * KAPPA, ry * KAPPA
    return [
        ("M", cx, cy - ry),
        ("C", cx + ox, cy - ry, cx + rx, cy - oy, cx + rx, cy),
        ("C", cx + rx, cy + oy, cx + ox, cy + ry, cx, cy + ry),
        ("C", cx - ox, cy + ry, cx - rx, cy + oy, cx - rx, cy),
        ("C", cx - rx, cy - oy, cx - ox, cy - ry, cx, cy - ry),
        ("Z",),
    ]


# -- extraction ----------------------------------------------------------

def style_of(element):
    def number(name, default):
        value = element.get(name)
        return float(value) if value is not None else default

    return {
        "fill": parse_colour(element.get("fill", "#000000")),
        "fill_opacity": number("fill-opacity", 1.0),
        "stroke": parse_colour(element.get("stroke")),
        "stroke_opacity": number("stroke-opacity", 1.0),
        "stroke_width": number("stroke-width", 1.0),
        "line_cap": element.get("stroke-linecap", "butt"),
    }


def extract(svg_path):
    root = ET.parse(svg_path).getroot()
    viewbox = tuple(float(v) for v in root.get("viewBox").split())

    shapes, styles, order, anchors, boxes = {}, {}, [], {}, {}

    def walk(node, hidden):
        for child in node:
            tag = child.tag.replace(SVG_NS, "")
            child_hidden = hidden or child.get("display") == "none"
            if tag == "g":
                walk(child, child_hidden)
                continue

            name = child.get("id")
            if not name:
                continue

            if tag == "path":
                segments = PathParser(child.get("d")).parse()
            elif tag == "circle":
                r = float(child.get("r"))
                segments = ellipse_segments(float(child.get("cx")),
                                            float(child.get("cy")), r, r)
            elif tag == "rect":
                rx = float(child.get("rx") or 0.0)
                ry = float(child.get("ry") or rx)
                segments = rounded_rect_segments(
                    float(child.get("x")), float(child.get("y")),
                    float(child.get("width")), float(child.get("height")),
                    rx, ry)
            elif tag == "ellipse":
                segments = ellipse_segments(float(child.get("cx")),
                                            float(child.get("cy")),
                                            float(child.get("rx")),
                                            float(child.get("ry")))
            else:
                continue

            if name.startswith("anchor_"):
                anchors[name[len("anchor_"):]] = (float(child.get("cx")),
                                                  float(child.get("cy")))
                continue

            if name.startswith("box_"):
                # a named rectangle used only for measurement, never drawn
                x, y = float(child.get("x")), float(child.get("y"))
                boxes[name[len("box_"):]] = (x, y, float(child.get("width")),
                                             float(child.get("height")))
                continue

            shapes[name] = segments
            styles[name] = style_of(child)
            if not child_hidden:
                order.append(name)

    walk(root, False)
    return viewbox, shapes, styles, order, anchors, boxes


def check_morphs(shapes):
    """Morph targets must share a command sequence or interpolation is garbage."""
    groups = {}
    for name, segments in shapes.items():
        if "_" not in name:
            continue
        prefix = name.split("_", 1)[0]
        groups.setdefault(prefix, []).append((name, [s[0] for s in segments]))
    problems = []
    for prefix, members in groups.items():
        if prefix != "mouth" or len(members) < 2:
            continue
        reference = members[0][1]
        for name, commands in members[1:]:
            if commands != reference:
                problems.append(
                    f"{name} has command sequence {commands} but "
                    f"{members[0][0]} has {reference}")
    return problems


# -- emission ------------------------------------------------------------

def fmt(value):
    return repr(round(value, 4)) if isinstance(value, float) else repr(value)


def fmt_value(value):
    """Render a style value as a Python literal, not as a quoted string."""
    if isinstance(value, tuple):
        return repr(tuple(round(c, 4) for c in value))
    if isinstance(value, float):
        return repr(round(value, 4))
    return repr(value)


def emit(viewbox, shapes, styles, order, anchors, boxes):
    lines = [
        '"""Buddy geometry, in the 40x32 fine-grid coordinates of the sprite.',
        "",
        "GENERATED by build_vector.py from vector_art.svg -- do not edit by hand.",
        "Edit the SVG and regenerate.",
        "",
        "Every shape is a list of ('M', x, y), ('C', x1, y1, x2, y2, x, y) and",
        "('Z',) tuples. Shapes sharing a command sequence can be morphed.",
        '"""',
        "",
        f"VIEWBOX = {tuple(round(v, 4) for v in viewbox)!r}",
        "",
        "ANCHORS = {",
    ]
    for name, (x, y) in anchors.items():
        lines.append(f"    {name!r}: ({round(x, 4)}, {round(y, 4)}),")
    lines += ["}", "", "BOXES = {"]
    for name, (x, y, w, h) in boxes.items():
        lines.append(f"    {name!r}: ({round(x, 4)}, {round(y, 4)}, "
                     f"{round(w, 4)}, {round(h, 4)}),")
    lines += ["}", "", "DRAW_ORDER = ["]
    for name in order:
        lines.append(f"    {name!r},")
    lines += ["]", "", "SHAPES = {"]
    for name, segments in shapes.items():
        lines.append(f"    {name!r}: [")
        for segment in segments:
            body = ", ".join(fmt(v) for v in segment)
            lines.append(f"        ({body},),".replace(",),", ",),")
                         if len(segment) == 1 else f"        ({body}),")
        lines.append("    ],")
    lines += ["}", "", "STYLE = {"]
    for name, style in styles.items():
        parts = ", ".join(f"{k!r}: {fmt_value(v)}" for k, v in style.items())
        lines.append(f"    {name!r}: {{{parts}}},")
    lines += ["}", "", "", RUNTIME]
    return "\n".join(lines) + "\n"


RUNTIME = '''def emit(cr, segments):
    """Lay down a segment list in viewbox coordinates."""
    for segment in segments:
        kind = segment[0]
        if kind == "C":
            cr.curve_to(*segment[1:])
        elif kind == "M":
            cr.move_to(*segment[1:])
        else:
            cr.close_path()


def path(cr, name):
    emit(cr, SHAPES[name])


def blend(a, b, t):
    """Interpolate two same-topology segment lists. t is clamped, so a spring
    value that overshot is safe to pass straight in."""
    if t <= 0.0:
        return a
    if t >= 1.0:
        return b
    out = []
    for first, second in zip(a, b):
        if first[0] == "Z":
            out.append(first)
            continue
        out.append((first[0], *[p + (q - p) * t for p, q in zip(first[1:], second[1:])]))
    return out


def blend3(low, mid, high, t):
    """Blend across three segment lists with t in -1..1, mid at zero."""
    return blend(mid, high, t) if t >= 0.0 else blend(mid, low, -t)


def morph(cr, a, b, t):
    emit(cr, blend(SHAPES[a], SHAPES[b], t))


def bounds(segments):
    """Rough extents from on-curve and control points. Good enough for the
    contact sheet and for sizing the input shape."""
    xs, ys = [], []
    for segment in segments:
        values = segment[1:]
        xs.extend(values[0::2])
        ys.extend(values[1::2])
    return (min(xs), min(ys), max(xs), max(ys)) if xs else (0.0, 0.0, 0.0, 0.0)
'''


def preview(out_path="preview.png"):
    """Contact sheet of what the daemon actually draws.

    README.md embeds preview.png, so it has to show the vector art rather than
    the pixel atlas it replaced - a documentation image of art that no longer
    ships is worse than none.
    """
    import cairo

    import sprites
    import vector

    S, PAD = 5, 16
    anims = list(sprites.ANIMS)
    props = vector.prop_names()
    costumes = list(sprites.COSTUMES)

    cell_w, cell_h = 40 * S + PAD, 32 * S + PAD + 16
    cols = 7
    anim_rows = (len(anims) + cols - 1) // cols
    cost_rows = (len(costumes) + cols - 1) // cols
    prop_cols = 7
    prop_rows = (len(props) + prop_cols - 1) // prop_cols
    prop_h = 30 * S // 2 + 34

    width = cols * cell_w + PAD
    height = (60 + anim_rows * cell_h + 40 + cost_rows * cell_h
              + 40 + prop_rows * prop_h + PAD)
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    cr = cairo.Context(surface)
    cr.set_source_rgb(0.96, 0.95, 0.93)
    cr.paint()

    def label(text, x, y, size=13, grey=0.35):
        cr.set_source_rgb(grey, grey, grey)
        cr.select_font_face("monospace")
        cr.set_font_size(size)
        cr.move_to(x, y)
        cr.show_text(text)

    label("deck guy - vector art", PAD, 30, 20, 0.15)
    label("body and props are drawn from paths at 60fps; no sprite atlas",
          PAD, 48, 12, 0.45)

    class Fake:
        def __init__(self, anim):
            self.anim, self.y_off = anim, 0.0

    y = 60
    for i, name in enumerate(anims):
        col, row = i % cols, i // cols
        x = PAD + col * cell_w
        cy = y + row * cell_h
        rig = vector.Rig(seed=i + 3)
        fake = Fake(name)
        for _ in range(80):
            rig.update(1.0 / 60.0, fake)
        rig.draw(cr, x, cy, 40 * S, 32 * S)
        label(name, x, cy + 32 * S + 13)

    y += anim_rows * cell_h + 24
    label("costumes", PAD, y, 15, 0.2)
    y += 16
    for i, name in enumerate(costumes):
        spec = sprites.COSTUMES[name]
        col, row = i % cols, i // cols
        x = PAD + col * cell_w
        cy = y + row * cell_h
        rig = vector.Rig(seed=i + 9)
        fake = Fake(spec["base"])
        for _ in range(50):
            rig.update(1.0 / 60.0, fake)
        back, front = [], []
        for prop, anchors in spec.get("attach", {}).items():
            anchor = anchors[0]
            if not anchor:
                continue
            key = vector.prop_key(prop, 0)
            z = sprites.PROPS[prop].get("z", "front")
            (back if z == "back" else front).append((key, anchor))
        for key, (ax, ay) in back:
            vector.draw_prop(cr, key, x + ax * S, cy + ay * S, S)
        rig.draw(cr, x, cy, 40 * S, 32 * S)
        for key, (ax, ay) in front:
            vector.draw_prop(cr, key, x + ax * S, cy + ay * S, S)
        label(name, x, cy + 32 * S + 13)

    y += cost_rows * cell_h + 24
    label("props", PAD, y, 15, 0.2)
    y += 16
    for i, name in enumerate(props):
        col, row = i % prop_cols, i // prop_cols
        x = PAD + col * cell_w
        cy = y + row * prop_h
        vector.draw_prop(cr, name, x, cy, S)
        pw, ph = vector.prop_size(name)
        label(f"{name} {int(pw)}x{int(ph)}", x, cy + prop_h - 14, 11, 0.45)

    surface.write_to_png(out_path)
    print(f"{out_path}  {width}x{height}  "
          f"{len(anims)} animations, {len(costumes)} costumes, {len(props)} props")
    return 0


def main():
    if "--preview" in sys.argv[1:]:
        return preview()
    svg = Path(sys.argv[1] if len(sys.argv) > 1 else "vector_art.svg")
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "vector_paths.py")

    viewbox, shapes, styles, order, anchors, boxes = extract(svg)

    problems = check_morphs(shapes)
    if problems:
        print("morph targets do not match:", file=sys.stderr)
        for problem in problems:
            print("  " + problem, file=sys.stderr)
        return 1

    out.write_text(emit(viewbox, shapes, styles, order, anchors, boxes))
    print(f"{svg} -> {out}")
    print(f"  {len(shapes)} shapes, {len(anchors)} anchors, {len(boxes)} boxes, "
          f"{sum(len(s) for s in shapes.values())} segments")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
