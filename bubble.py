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

import cairo

FONT = "DejaVu Sans Mono"          # falls back to the default mono if absent
SIZE = 12                          # native px; ~2/3 the height of one body cell
PAD = 8                            # px between the frame and the text
GAP = 6                            # px between the label and the timer
LEAD = 3                           # px of extra leading between panel rows

PAPER = (0.941, 0.929, 0.894)      # #F0EDE4 - the chef-hat white, so it belongs
INK = (0.16, 0.11, 0.09)
DIM = (0.38, 0.30, 0.26)           # the timer, and the panel's labels
EDGE = (0.494, 0.227, 0.141)       # #7E3A24 - the body outline colour


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


def _frame(cr, w, h, px, tail=True):
    """A rectangle whose border is one `px` block thick, with a stepped tail.

    Corners are left out of the border runs, which gives the notched pixel-art
    corner instead of a hard right angle. The bottom border is drawn in two runs
    with a gap where the tail joins - running it straight across leaves the tail
    looking like two nubs stuck under a closed box.
    """
    cx = (w // (2 * px)) * px          # snap the tail to the block grid
    mouth = (cx - 3 * px, cx + 3 * px) if tail else None

    cr.set_source_rgba(*PAPER, 0.97)
    cr.rectangle(px, 0, w - 2 * px, h)
    cr.rectangle(0, px, w, h - 2 * px)
    if tail:
        cr.rectangle(cx - 2 * px, h, 4 * px, px)          # the tail's mouth
    cr.fill()

    cr.set_source_rgb(*EDGE)
    runs = [(px, 0, w - 2 * px, px),                       # top
            (0, px, px, h - 2 * px),                       # left
            (w - px, px, px, h - 2 * px)]                  # right
    if tail:
        runs += [(px, h - px, mouth[0] - px, px),
                 (mouth[1], h - px, w - px - mouth[1], px)]
    else:
        runs += [(px, h - px, w - 2 * px, px)]
    for x, y, bw, bh in runs:
        if bw > 0:
            cr.rectangle(x, y, bw, bh)
    cr.fill()
    if not tail:
        return

    # Three stepped rows: 4 blocks wide, then 2, then the point closes it off.
    cr.set_source_rgba(*PAPER, 0.97)
    cr.rectangle(cx - px, h + px, 2 * px, px)
    cr.fill()
    cr.set_source_rgb(*EDGE)
    for x, y in ((cx - 3 * px, h), (cx + 2 * px, h),
                 (cx - 2 * px, h + px), (cx + px, h + px),
                 (cx - px, h + 2 * px), (cx, h + 2 * px)):
        cr.rectangle(x, y, px, px)
    cr.fill()


def _round_up(n, px):
    return ((int(n) + px - 1) // px) * px


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

    def draw(self, cr, centre_x, bottom_y, win_w, alpha=1.0):
        if not self.visible or alpha <= 0.01:
            return None
        surf = self._build()
        w, h = surf.get_width(), surf.get_height()
        x, y = place(centre_x, bottom_y, w, h, win_w)
        cr.set_source_surface(surf, x, y)
        cr.paint_with_alpha(alpha)
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
        w = _round_up(PAD * 2 + tw + (GAP + sw if self.timer else 0), px)
        h = _round_up(PAD * 2 + TEXT_H, px)
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h + px * TAIL_ROWS)
        cr = cairo.Context(surf)
        _frame(cr, w, h, px)
        _font(cr)
        base = (h + ASCENT - DESCENT) / 2
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

    Phase 2 hangs tokens, cost and model off the same rows, so the only thing
    that changes then is what `set` is handed.
    """

    def __init__(self, scale):
        super().__init__(scale)
        self.rows = []

    def set(self, rows):
        self.rows = list(rows)

    @property
    def visible(self):
        return bool(self.rows)

    def _build(self):
        return self._cached((tuple(map(tuple, self.rows)), self.scale), self._render)

    def _render(self):
        px = self.scale
        row_h = TEXT_H + LEAD
        label_w = max((_width(r[0]) for r in self.rows), default=0)
        value_w = max((_width(r[1]) for r in self.rows), default=0)
        w = _round_up(PAD * 2 + label_w + GAP * 2 + value_w, px)
        h = _round_up(PAD * 2 + row_h * len(self.rows), px)
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        cr = cairo.Context(surf)
        _frame(cr, w, h, px, tail=False)
        _font(cr)
        for i, (label, value) in enumerate(self.rows):
            base = PAD + row_h * i + ASCENT
            cr.set_source_rgb(*DIM)
            cr.move_to(PAD, base)
            cr.show_text(str(label))
            cr.set_source_rgb(*INK)
            cr.move_to(PAD + label_w + GAP * 2, base)
            cr.show_text(str(value))
        return surf
