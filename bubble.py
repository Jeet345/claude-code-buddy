#!/usr/bin/env python3
"""The speech bubble and the hover panel.

Text is the first non-pixel thing in a pixel-art scene, so the two halves are
treated differently on purpose:

    the frame   drawn as blocks of `scale` pixels - the same grid his sprite is
                on, so the border and the tail read as part of his world
    the text    drawn natively, antialiased and hinted, at a size you can read

The first version rendered everything 1-bit at 8px and scaled it up with the
creature. That magnified broken glyphs: `sheet.png` came out mangled and the
bubble was twice the width it needed to be. Text is the one thing here that has
to be legible before it is stylish.

    bubble = Bubble(scale)
    bubble.set("Bash pytest -q", "7s")
    bubble.draw(cr, centre_x, bottom_y, win_w, alpha)
"""

import math

import cairo

FONT = "DejaVu Sans Mono"          # falls back to the default mono if absent
SIZE = 12                          # native px; ~2/3 the height of one body cell
PAD = 11                           # px between the surface edge and the text
GAP = 6                            # px between the label and the timer
LEAD = 3                           # px of extra leading between panel rows

PAPER = (0.941, 0.929, 0.894)      # #F0EDE4 - the chef-hat white, so it belongs
INK = (0.16, 0.11, 0.09)
DIM = (0.38, 0.30, 0.26)           # the timer, and the panel's labels
EDGE = (0.494, 0.227, 0.141)       # #7E3A24 - the body outline colour
GHOST = (0.78, 0.75, 0.70)         # a value we are not sure of, or a stale price

# Meter fill, by how alarming the number is. The thresholds live here rather
# than at the call site so phase 7's budget bar inherits the same three colours
# and the same meaning: green is fine, amber is "start thinking about it", red
# is "this is about to happen to you".
WARN, HIGH = 0.75, 0.90
BAR_OK = (0.42, 0.55, 0.33)        # olive, close enough to the palette to belong
BAR_WARN = (0.80, 0.58, 0.18)
BAR_HIGH = (0.72, 0.24, 0.18)


def bar_colour(fill):
    return BAR_HIGH if fill >= HIGH else BAR_WARN if fill >= WARN else BAR_OK


class Meter:
    """A full-width bar row. `Panel.set([Meter(0.68), ...])`.

    Its own row rather than a widget inside the value column: at this size a
    bar with text on top of it is unreadable, and a bar squeezed beside text is
    too short to read as a proportion. Given a whole row it is honest at a
    glance, which is the only thing a fill gauge is for.
    """

    __slots__ = ("fill", "dim")

    def __init__(self, fill, dim=False):
        self.fill = min(1.0, max(0.0, float(fill or 0.0)))
        self.dim = bool(dim)          # drawn grey when the number is a guess

    def key(self):
        return ("meter", round(self.fill, 3), self.dim)


def place(centre_x, bottom_y, w, h, win_w):
    """Where a readout of this size goes: centred on him, never off an edge.

    One implementation, used by the drawing, by the damage rectangle and by the
    input shape - if those three disagreed you would get a bubble you cannot
    hover, or one that leaves smears behind when it moves.
    """
    x = max(2, min(int(centre_x - w / 2), win_w - w - 2))
    return x, max(2, int(bottom_y - h))


def _font(cr):
    cr.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    cr.set_font_size(SIZE)
    opts = cairo.FontOptions()
    opts.set_antialias(cairo.ANTIALIAS_GRAY)
    opts.set_hint_style(cairo.HINT_STYLE_FULL)      # snap stems to the pixel grid
    opts.set_hint_metrics(cairo.HINT_METRICS_ON)
    cr.set_font_options(opts)


_probe = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1))
_font(_probe)
ASCENT, DESCENT = _probe.font_extents()[0], _probe.font_extents()[1]
TEXT_H = int(ASCENT + DESCENT)


def _width(text):
    return _probe.text_extents(text).x_advance if text else 0.0


TAIL_ROWS = 3          # blocks tall; the tail is TAIL_ROWS * scale px
RADIUS = 9             # px corner radius of every readout
SHADOW = 4             # px the soft shadow reaches below a readout
INSET = 2              # px between the surface edge and the frame


def _rounded(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


def _frame(cr, w, h, px, tail=True):
    """A soft card: rounded, a warm paper gradient, a hairline edge, a shadow
    that falls below it, and - for the bubble - a tail curving down to him.

    This replaced a frame drawn in `px` blocks from the pixel-sprite days. The
    body has been smooth vector art for a while, and a pixel frame beside it
    read as two different games stuck together.
    """
    x, y = INSET, 1
    fw, fh = w - INSET * 2, h - 1 - SHADOW
    cx = w / 2
    tail_h = px * TAIL_ROWS

    def outline():
        _rounded(cr, x, y, fw, fh, RADIUS)
        if tail:
            cr.new_sub_path()
            cr.move_to(cx - 9, y + fh - 1)
            cr.curve_to(cx - 4, y + fh + tail_h * 0.35, cx - 2, y + fh + tail_h * 0.75,
                        cx, y + fh + tail_h)
            cr.curve_to(cx + 2, y + fh + tail_h * 0.6, cx + 6, y + fh + tail_h * 0.25,
                        cx + 10, y + fh - 1)
            cr.close_path()

    # shadow: a few widening, fading copies - a blur without a blur
    for i, a in enumerate((0.10, 0.07, 0.04)):
        cr.save()
        cr.translate(0, 1.5 + i)
        _rounded(cr, x - i * 0.5, y + i * 0.5, fw + i, fh, RADIUS + i)
        cr.set_source_rgba(0.12, 0.06, 0.04, a)
        cr.fill()
        cr.restore()

    cr.set_fill_rule(cairo.FILL_RULE_WINDING)
    outline()
    g = cairo.LinearGradient(0, y, 0, y + fh)
    g.add_color_stop_rgba(0.0, 1.0, 0.99, 0.96, 0.98)
    g.add_color_stop_rgba(1.0, *PAPER, 0.98)
    cr.set_source(g)
    cr.fill_preserve()
    cr.set_source_rgba(*EDGE, 0.55)
    cr.set_line_width(1.0)
    cr.stroke()
    # re-fill the seam where the tail joins, so no stroke crosses it
    if tail:
        cr.rectangle(cx - 8, y + fh - 2.5, 17, 2.2)
        cr.set_source_rgba(*PAPER, 1.0)
        cr.fill()


def _round_up(n, px):
    return ((int(n) + px - 1) // px) * px


BAR_H = 9              # px tall including the 1px track outline
BAR_MIN = 150          # px; a bar shorter than this cannot show a proportion


def _bar(cr, x, y, w, meter):
    """A track with a fill. Drawn in flat px, not blocks - a meter quantised to
    the sprite grid jumps in 3% steps and reads as broken rather than stylised."""
    x, y, w = int(x), int(y), int(w)
    _rounded(cr, x, y, w, BAR_H, BAR_H / 2)
    cr.set_source_rgba(*DIM, 0.18)
    cr.fill()
    filled = (w - 2) * meter.fill
    if filled <= 1:
        return
    _rounded(cr, x + 1, y + 1, max(BAR_H - 2, filled), BAR_H - 2, (BAR_H - 2) / 2)
    cr.set_source_rgb(*(GHOST if meter.dim else bar_colour(meter.fill)))
    cr.fill()


class _Readout:
    """Shared caching and blitting. Subclasses only build the surface."""

    def __init__(self, scale):
        self.scale = scale
        self._surface = None
        self._key = None
        self._size = (0, 0)

    def _cached(self, key, build):
        if key != self._key or self._surface is None:
            self._surface = build()
            self._key = key
            self._size = (self._surface.get_width(), self._surface.get_height())
        return self._surface

    def size(self):
        self._build()
        return self._size

    def draw(self, cr, centre_x, bottom_y, win_w, alpha=1.0, scale=1.0):
        """`scale` grows it from its bottom centre - the pop-in on arrival.
        The returned rect is the resting one; the caller pads its damage."""
        if not self.visible or alpha <= 0.01:
            return None
        surf = self._build()
        w, h = surf.get_width(), surf.get_height()
        x, y = place(centre_x, bottom_y, w, h, win_w)
        cr.save()
        if abs(scale - 1.0) > 0.002:
            cr.translate(x + w / 2, y + h)
            cr.scale(scale, scale)
            cr.translate(-(x + w / 2), -(y + h))
        cr.set_source_surface(surf, x, y)
        cr.paint_with_alpha(alpha)
        cr.restore()
        return (x, y, w, h)


class Bubble(_Readout):
    """One line above his head. Rebuilt only when the text changes."""

    def __init__(self, scale):
        super().__init__(scale)
        self.text = ""
        self.timer = ""

    def set(self, text, timer=""):
        self.text, self.timer = text or "", timer or ""

    @property
    def visible(self):
        return bool(self.text)

    def _build(self):
        return self._cached((self.text, self.timer, self.scale), self._render)

    def _render(self):
        px = self.scale
        tw, sw = _width(self.text), _width(self.timer)
        w = _round_up(PAD * 2 + INSET * 2 + tw + (GAP + sw if self.timer else 0), px)
        h = _round_up(PAD * 2 + TEXT_H + SHADOW, px)
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h + px * TAIL_ROWS)
        cr = cairo.Context(surf)
        _frame(cr, w, h, px)
        cr.translate(INSET, 0)
        _font(cr)
        base = (h - SHADOW + ASCENT - DESCENT) / 2
        cr.set_source_rgb(*INK)
        cr.move_to(PAD, base)
        cr.show_text(self.text)
        if self.timer:
            cr.set_source_rgb(*DIM)
            cr.move_to(PAD + tw + GAP, base)
            cr.show_text(self.timer)
        return surf


class Panel(_Readout):
    """The hover readout: label/value rows in the same frame, no tail.

    A row is either a `(label, value)` pair, optionally with a third truthy
    element meaning "draw this greyed out", or a `Meter`, which takes the full
    inner width.
    """

    def __init__(self, scale):
        super().__init__(scale)
        self.rows = []

    def set(self, rows):
        self.rows = list(rows)

    @property
    def visible(self):
        return bool(self.rows)

    def key(self):
        """What the panel currently shows, for the repaint check upstream.

        The ticking clocks live in here, so this has to be part of the draw key
        or the panel would only refresh when the creature happened to move.
        """
        return tuple(r.key() if isinstance(r, Meter) else tuple(map(str, r))
                     for r in self.rows)

    def _build(self):
        return self._cached((self.key(), self.scale), self._render)

    def _text_rows(self):
        return [r for r in self.rows if not isinstance(r, Meter)]

    def _render(self):
        px = self.scale
        row_h = TEXT_H + LEAD
        text_rows = self._text_rows()
        label_w = max((_width(str(r[0])) for r in text_rows), default=0)
        value_w = max((_width(str(r[1])) for r in text_rows), default=0)
        w = _round_up(PAD * 2 + INSET * 2 + label_w + GAP * 2 + value_w, px)
        w = max(w, _round_up(PAD * 2 + BAR_MIN, px))
        h = _round_up(PAD * 2 + row_h * len(self.rows) + SHADOW, px)
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        cr = cairo.Context(surf)
        _frame(cr, w, h, px, tail=False)
        cr.translate(INSET, 0)
        _font(cr)
        for i, row in enumerate(self.rows):
            top = PAD + row_h * i
            if isinstance(row, Meter):
                _bar(cr, PAD, top + (row_h - BAR_H) / 2, w - PAD * 2 - INSET * 2, row)
                continue
            label, value = row[0], row[1]
            faded = len(row) > 2 and row[2]
            base = top + ASCENT
            cr.set_source_rgb(*DIM)
            cr.move_to(PAD, base)
            cr.show_text(str(label))
            cr.set_source_rgb(*(GHOST if faded else INK))
            cr.move_to(PAD + label_w + GAP * 2, base)
            cr.show_text(str(value))
        return surf


BTN_PAD = 10           # px of text inset inside a button
BTN_GAP = 6            # px between buttons
BTN_FILL = {"allow": BAR_OK, "deny": BAR_HIGH}


class Card(_Readout):
    """A held permission prompt: a title, a few lines, a row of buttons.

    The buttons are the only clickable thing the buddy has ever drawn, so the
    card answers where they are: `buttons_at(origin)` gives screen rects keyed
    by the button's id, from the same layout the drawing used.

        card.set("Bash  pytest -q", ["api · needs your permission"],
                 [("allow", "Allow"), ("deny", "Deny")], timer="0:12")
    """

    def __init__(self, scale):
        super().__init__(scale)
        self.title = ""
        self.lines = []
        self.buttons = []
        self.timer = ""
        self.hot = ""                  # the button under the pointer
        self.danger = False            # the command matched a guard pattern
        self._rects = []

    def set(self, title, lines=(), buttons=(), timer="", hot="", danger=False):
        self.danger = bool(danger)
        self.title = title or ""
        self.lines = [str(x) for x in lines if x]
        self.buttons = [(str(k), str(v)) for k, v in buttons]
        self.timer = timer or ""
        self.hot = hot or ""

    @property
    def visible(self):
        return bool(self.title)

    def key(self):
        return (self.title, tuple(self.lines), tuple(self.buttons), self.timer, self.hot,
                self.danger)

    def _build(self):
        return self._cached((self.key(), self.scale), self._render)

    def _layout(self):
        """(w, h, [(id, label, x, y, bw, bh)]) - shared by draw and hit test."""
        px = self.scale
        row_h = TEXT_H + LEAD
        bh = _round_up(TEXT_H + BTN_PAD * 2, px)
        widths = [_round_up(_width(label) + BTN_PAD * 2, px) for _, label in self.buttons]
        row_w = sum(widths) + BTN_GAP * max(0, len(widths) - 1)
        title_w = _width(self.title) + (GAP + _width(self.timer) if self.timer else 0)
        text_w = max([title_w] + [_width(x) for x in self.lines])
        w = _round_up(PAD * 2 + INSET * 2 + max(text_w, row_w), px)
        top = PAD + row_h * (1 + len(self.lines)) + LEAD
        h = _round_up(top + bh + PAD + SHADOW, px)
        rects, x = [], PAD
        for (bid, label), bw in zip(self.buttons, widths):
            rects.append((bid, label, x, top, bw, bh))
            x += bw + BTN_GAP
        return w, h, rects

    def _render(self):
        px = self.scale
        w, h, rects = self._layout()
        self._rects = rects
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        cr = cairo.Context(surf)
        _frame(cr, w, h, px, tail=False)
        if self.danger:
            # a red edge and a red band behind the title: read before clicking
            _rounded(cr, INSET + 0.5, 1.5, w - INSET * 2 - 1, h - SHADOW - 2, RADIUS)
            cr.set_source_rgb(*BAR_HIGH)
            cr.set_line_width(2.0)
            cr.stroke()
            cr.rectangle(INSET + 2, PAD - 4, w - INSET * 2 - 4, TEXT_H + 8)
            cr.set_source_rgba(*BAR_HIGH, 0.14)
            cr.fill()
        cr.translate(INSET, 0)
        _font(cr)
        row_h = TEXT_H + LEAD
        cr.set_source_rgb(*(BAR_HIGH if self.danger else INK))
        cr.move_to(PAD, PAD + ASCENT)
        cr.show_text(self.title)
        if self.timer:
            cr.set_source_rgb(*DIM)
            cr.move_to(PAD + _width(self.title) + GAP, PAD + ASCENT)
            cr.show_text(self.timer)
        cr.set_source_rgb(*DIM)
        for i, line in enumerate(self.lines):
            cr.move_to(PAD, PAD + row_h * (i + 1) + ASCENT)
            cr.show_text(line)
        for bid, label, x, y, bw, bh in rects:
            fill = BTN_FILL.get(bid)
            hot = bid == self.hot
            lift = -1 if hot else 0                  # hovered buttons rise a pixel
            # drop shadow, then the pill
            _rounded(cr, x, y + 1.5, bw, bh, bh / 2)
            cr.set_source_rgba(0.12, 0.06, 0.04, 0.16)
            cr.fill()
            _rounded(cr, x, y + lift, bw, bh, bh / 2)
            if fill:
                top = [min(1.0, c * (1.18 if hot else 1.08)) for c in fill]
                g = cairo.LinearGradient(0, y + lift, 0, y + lift + bh)
                g.add_color_stop_rgb(0, *top)
                g.add_color_stop_rgb(1, *fill)
                cr.set_source(g)
                cr.fill()
            else:
                cr.set_source_rgb(*((0.93, 0.90, 0.85) if hot else (1.0, 0.99, 0.97)))
                cr.fill_preserve()
                cr.set_source_rgba(*EDGE, 0.45)
                cr.set_line_width(1.0)
                cr.stroke()
            cr.set_source_rgb(*((1, 1, 1) if fill else INK))
            cr.move_to(x + BTN_PAD, y + lift + (bh + ASCENT - DESCENT) / 2)
            cr.show_text(label)
        return surf

    def buttons_at(self, origin):
        """{id: (x, y, w, h)} in the coordinates `origin` (the card's x, y) is in."""
        ox, oy = origin
        _, _, rects = self._layout()
        return {bid: (ox + INSET + x, oy + y, bw, bh) for bid, _l, x, y, bw, bh in rects}
