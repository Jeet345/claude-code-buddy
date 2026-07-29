#!/usr/bin/env python3
"""Bake the ASCII grids in sprites.py into a PNG atlas + manifest, and render a preview.

    python3 build_sheet.py            # atlas + manifest + preview
    python3 build_sheet.py --preview  # preview only
    python3 build_sheet.py --flat     # skip the automatic shading pass

Outputs:
    sprites.png    1:1 pixel atlas, upscaled nearest-neighbour by the daemon
    sprites.json   frame rects, fps, loop flags, prop anchors
    preview.png    labelled contact sheet for eyeballing the art

Three passes run before anything is drawn: parse (grid is rectangular, every
character is in the palette), shade (derive lit/shaded edges and the outline),
and resolve (costumes borrow body frames, anchor lists must match frame counts).
Art mistakes therefore surface as a line-and-column error, not as a wrong pixel.
"""

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import sprites

HERE = Path(__file__).parent
BG = (247, 245, 241)
INK = (35, 33, 30)
MUTED = (150, 143, 136)
PANEL = (252, 251, 249)


# ---------------------------------------------------------------------- parsing
def parse(grid, label):
    """ASCII grid -> list of rows. Raises with a precise location on bad input."""
    rows = [r for r in grid.strip("\n").split("\n")]
    if not rows:
        raise ValueError(f"{label}: empty frame")
    width = len(rows[0])
    for i, row in enumerate(rows):
        if len(row) != width:
            raise ValueError(
                f"{label} row {i}: width {len(row)}, expected {width}\n  {row!r}"
            )
        for j, ch in enumerate(row):
            if ch not in sprites.PALETTE:
                raise ValueError(f"{label} row {i} col {j}: unknown char {ch!r}")
    return rows


def upscale(rows, n):
    """Enlarge a grid by whole cells - one authored cell becomes an n x n block.

    The body is authored at half resolution and doubled here, so his chunky
    proportions survive while props get the finer grid they need to be readable.
    Shading runs afterwards, which is what keeps the outline one pixel thick.
    """
    return ["".join(ch * n for ch in row) for row in rows for _ in range(n)]


def shade(rows):
    """Derive lit edges, shaded edges and an outline from a flat silhouette.

    Light comes from above, so a cell with nothing above it catches the light and a
    cell with nothing below it falls into shade. The outline is painted into the
    transparent cells that touch the shape, which is why every grid keeps a margin.
    Cells that fall outside the grid count as empty - the feet get no outline
    underneath them, which is what we want, since they sit on the ground.
    """
    h, w = len(rows), len(rows[0])
    orig = [list(r) for r in rows]
    out = [list(r) for r in rows]

    def at(y, x):
        return orig[y][x] if 0 <= y < h and 0 <= x < w else "."

    for y in range(h):
        for x in range(w):
            rule = sprites.SHADE_RULES.get(orig[y][x])
            if not rule:
                continue
            if "top" in rule and at(y - 1, x) == ".":
                out[y][x] = rule["top"]
            elif "bottom" in rule and at(y + 1, x) == ".":
                out[y][x] = rule["bottom"]

    for y in range(h):
        for x in range(w):
            if orig[y][x] != ".":
                continue
            for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                rule = sprites.SHADE_RULES.get(at(y + dy, x + dx))
                if rule and "outline" in rule:
                    out[y][x] = rule["outline"]
                    break

    return ["".join(r) for r in out]


def collect(flat=False):
    """Validate and resolve everything up front, before any rendering happens."""
    finish = (lambda r: r) if flat else shade

    def prepare(spec, frames):
        n = 1 if spec.get("fine") else sprites.UPSCALE
        return [finish(upscale(f, n) if n > 1 else f) for f in frames]

    anims = {}
    for name, spec in sprites.ANIMS.items():
        frames = [parse(f, f"ANIMS[{name}][{i}]") for i, f in enumerate(spec["frames"])]
        sizes = {(len(f[0]), len(f)) for f in frames}
        if len(sizes) != 1:
            raise ValueError(f"ANIMS[{name}]: frames differ in size: {sorted(sizes)}")
        anims[name] = {
            "frames": prepare(spec, frames),
            "fps": spec.get("fps", 8),
            "loop": spec.get("loop", True),
            "attach": {},
        }

    props = {}
    for name, spec in sprites.PROPS.items():
        frames = [parse(f, f"PROPS[{name}][{i}]") for i, f in enumerate(spec["frames"])]
        sizes = {(len(f[0]), len(f)) for f in frames}
        if len(sizes) != 1:
            raise ValueError(f"PROPS[{name}]: frames differ in size: {sorted(sizes)}")
        props[name] = {
            "frames": prepare(spec, frames),
            "z": spec.get("z", "front"),
        }

    for name, spec in sprites.COSTUMES.items():
        base = spec["base"]
        if base not in anims:
            raise ValueError(f"COSTUMES[{name}]: base {base!r} is not an animation")
        src = anims[base]["frames"]
        length = spec.get("length", len(src))
        attach = {}
        for prop, anchors in spec.get("attach", {}).items():
            if prop not in props:
                raise ValueError(f"COSTUMES[{name}]: unknown prop {prop!r}")
            if len(anchors) != length:
                raise ValueError(
                    f"COSTUMES[{name}].attach[{prop}]: {len(anchors)} anchors "
                    f"for {length} frames"
                )
            attach[prop] = {"z": props[prop]["z"], "anchors": anchors}
        anims[name] = {
            "frames": [src[i % len(src)] for i in range(length)],
            "fps": spec.get("fps", 8),
            "loop": spec.get("loop", True),
            "attach": attach,
        }

    return anims, props


# --------------------------------------------------------------------- drawing
def rgba(hexcol):
    """#RRGGBB or #RRGGBBAA -> (r, g, b, a)."""
    parts = [int(hexcol[i : i + 2], 16) for i in range(1, len(hexcol) - 1, 2)]
    return tuple(parts) if len(parts) == 4 else tuple(parts) + (255,)


def render(rows, scale=1):
    w, h = len(rows[0]) * scale, len(rows) * scale
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    px = img.load()
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            hexcol = sprites.PALETTE[ch]
            if not hexcol:
                continue
            col = rgba(hexcol)
            for dy in range(scale):
                for dx in range(scale):
                    px[x * scale + dx, y * scale + dy] = col
    return img


def build_atlas(anims, props):
    """One atlas row per animation or prop, so entries may differ in size."""
    pad = 1
    entries = [(n, s["frames"]) for n, s in anims.items()]
    entries += [(f"prop:{n}", s["frames"]) for n, s in props.items()]
    width = max(sum(len(f[0]) + pad for f in frames) for _, frames in entries)
    height = sum(len(frames[0]) + pad for _, frames in entries)

    atlas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    rects, y = {}, 0
    for name, frames in entries:
        x = 0
        rects[name] = []
        for frame in frames:
            img = render(frame)
            atlas.paste(img, (x, y))
            rects[name].append([x, y, img.width, img.height])
            x += img.width + pad
        y += len(frames[0]) + pad

    idle = anims["idle"]["frames"][0]
    manifest = {
        "version": 2,
        "grid": [len(idle[0]), len(idle)],
        "anims": {
            name: {
                "fps": spec["fps"],
                "loop": spec["loop"],
                "attach": spec["attach"],
                "rects": rects[name],
            }
            for name, spec in anims.items()
        },
        "props": {
            name: {"z": spec["z"], "rects": rects[f"prop:{name}"]}
            for name, spec in props.items()
        },
    }
    return atlas, manifest


def composite(anims, props, name, index, scale):
    """Render one animation frame with its props pinned on, as the daemon will."""
    spec = anims[name]
    frame = spec["frames"][index % len(spec["frames"])]
    gw, gh = len(frame[0]), len(frame)
    pad = 12  # props may reach above and beside the body grid
    img = Image.new("RGBA", ((gw + pad * 2) * scale, (gh + pad) * scale), (0, 0, 0, 0))

    def paste(prop_name, anchor):
        pf = props[prop_name]["frames"][index % len(props[prop_name]["frames"])]
        art = render(pf, scale)
        img.alpha_composite(art, ((anchor[0] + pad) * scale, (anchor[1] + pad) * scale))

    for prop_name, info in spec["attach"].items():
        anchor = info["anchors"][index % len(info["anchors"])]
        if anchor and info["z"] == "back":
            paste(prop_name, anchor)
    img.alpha_composite(render(frame, scale), (pad * scale, pad * scale))
    for prop_name, info in spec["attach"].items():
        anchor = info["anchors"][index % len(info["anchors"])]
        if anchor and info["z"] != "back":
            paste(prop_name, anchor)
    return img


# --------------------------------------------------------------------- preview
def font(size, bold=False):
    stem = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(f"/usr/share/fonts/truetype/dejavu/{stem}", size)
    except OSError:
        return ImageFont.load_default()


def build_key(img, d, anims, props, x, y, width):
    """The 'when does he do what' table, drawn from MEANING and TOOL_COSTUME.

    Both live in sprites.py next to the art, so this cannot describe a costume the
    daemon does not actually wear. Returns the y to carry on drawing at.
    """
    scale, row_h, thumb_w = 2, 78, 150
    # Which frame of each animation shows the pose best - mid-flip for the pan, the
    # apex for the jump, the raised nub for the wave.
    HERO_FRAME = {"cook": 3, "jump": 2, "wave": 0, "patrol": 2, "wake": 1}
    by_costume = {}
    for tool, costume in sprites.TOOL_COSTUME.items():
        by_costume.setdefault(costume, []).append(tool)

    d.text((x, y), "when he does what", font=font(19, True), fill=INK)
    d.text((x, y + 26), "every line is a real trigger, generated from the same table "
                        "the daemon reads", font=font(13), fill=MUTED)
    y += 54

    for name, meaning in sprites.MEANING:
        if name not in anims:
            continue
        d.rectangle([x, y, x + width, y + row_h - 8], fill=PANEL)
        art = composite(anims, props, name, HERO_FRAME.get(name, 0), scale)
        img.paste(art, (x + (thumb_w - art.width) // 2, y + row_h - 12 - art.height), art)

        tx = x + thumb_w + 16
        d.text((tx, y + 14), name, font=font(16, True), fill=INK)
        d.text((tx + 92, y + 16), meaning, font=font(14), fill=(70, 66, 62))
        tools = by_costume.get(name)
        if tools:
            d.text((tx + 92, y + 40), "  ·  ".join(tools), font=font(12), fill=MUTED)
        y += row_h
    return y + 18


def build_preview(anims, props):
    scale, gap, pad = 4, 14, 28
    label_w, hero_h = 150, 250
    cw = max(len(a["frames"][0][0]) for a in anims.values()) * scale
    ch = max(len(a["frames"][0]) for a in anims.values()) * scale
    cols = max(len(a["frames"]) for a in anims.values())
    costumes = list(sprites.COSTUMES)

    row_h = ch + gap + 22
    W = pad * 2 + label_w + cols * (cw + gap)
    key_h = 72 + 78 * len([n for n, _ in sprites.MEANING if n in anims])
    H = pad + hero_h + key_h + 250 + len(costumes) * (ch + 74) + len(anims) * row_h + pad
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # --- hero: creature standing on a mock dock, like the reference ---
    d.text((pad, pad), "deck guy", font=font(30, True), fill=INK)
    d.text((pad, pad + 38), "sprite sheet - body, costumes, props",
           font=font(15), fill=MUTED)

    dock_y = pad + hero_h - 54
    dock_w, dock_x = 430, pad
    d.rounded_rectangle(
        [dock_x, dock_y, dock_x + dock_w, dock_y + 46], radius=12,
        fill=(255, 255, 255), outline=(228, 224, 217),
    )
    for i in range(7):
        ix = dock_x + 12 + i * 60
        d.rounded_rectangle(
            [ix, dock_y + 7, ix + 46, dock_y + 39], radius=9,
            fill=[(90, 170, 245), (60, 130, 230), (232, 105, 70), (150, 175, 205),
                  (140, 140, 145), (95, 190, 250), (240, 240, 242)][i],
        )
    hero = render(anims["idle"]["frames"][0], 5)
    img.paste(hero, (dock_x + 40, dock_y - hero.height), hero)

    total = sum(len(a["frames"]) for a in anims.values())
    for i, line in enumerate([
        f"body {sprites.PALETTE['O']}",
        f"{len(anims['idle']['frames'][0][0])} x {len(anims['idle']['frames'][0])} px grid",
        f"{total} frames, {len(anims)} animations, {len(props)} props",
    ]):
        d.text((dock_x + dock_w + 40, dock_y - 96 + i * 24), line,
               font=font(15), fill=INK if i == 0 else MUTED)

    # --- the key: what each behaviour means, straight from sprites.MEANING ---
    y = build_key(img, d, anims, props, pad, pad + hero_h + 16, W - pad * 2)

    # --- jump arc: poses plus the vertical travel the daemon supplies ---
    d.text((pad, y), "jump arc — daemon supplies the travel, frames are pose only",
           font=font(14), fill=MUTED)
    y += 26
    s, ground, arc = 3, y + 176, [0, 22, 78, 40, 0]
    for i, frame in enumerate(anims["jump"]["frames"]):
        f = render(frame, s)
        fx = pad + 30 + i * (f.width + 26)
        sh = render(props["shadow"]["frames"][0 if arc[i] < 10 else (1 if arc[i] < 50 else 2)], s)
        img.paste(sh, (fx + (f.width - sh.width) // 2, ground - sh.height), sh)
        img.paste(f, (fx, ground - f.height - arc[i]), f)
        if i == 2:
            sp = render(props["sparkle"]["frames"][0], s)
            img.paste(sp, (fx - 22, ground - f.height - arc[i] + 6), sp)
            img.paste(sp, (fx + f.width + 6, ground - f.height - arc[i] + 18), sp)
    strip_w = 5 * (len(anims["jump"]["frames"][0][0]) * s + 26)
    d.line([pad + 20, ground + 2, pad + 30 + strip_w, ground + 2],
           fill=(226, 222, 215), width=2)

    sx = pad + 30 + strip_w + 40
    sleeper = render(anims["sleep"]["frames"][0], s)
    img.paste(sleeper, (sx, ground - sleeper.height), sleeper)
    for dx, dy, zs in [(0, 0, 3), (16, -26, 4), (36, -58, 5)]:
        z = render(props["zzz"]["frames"][0], zs)
        img.paste(z, (sx + sleeper.width + dx, ground - 40 + dy), z)
    d.text((sx, ground + 12), "asleep after 5 min", font=font(13), fill=MUTED)

    # --- costumes: body + props composited, exactly as the daemon draws them ---
    y = ground + 50
    d.text((pad, y), "costumes — body animation + props pinned per frame",
           font=font(14), fill=MUTED)
    y += 26
    for name in costumes:
        spec = anims[name]
        d.text((pad, y + ch // 2 - 16), name, font=font(17, True), fill=INK)
        d.text((pad, y + ch // 2 + 4),
               f"{spec['fps']}fps · {len(spec['attach'])} props",
               font=font(13), fill=MUTED)
        for c in range(len(spec["frames"])):
            fx = pad + label_w + c * (cw + gap)
            d.rectangle([fx - 4, y - 4, fx + cw + 4, y + ch + 4], fill=PANEL)
            f = composite(anims, props, name, c, scale)
            img.paste(f, (fx + (cw - f.width) // 2, y + ch - f.height + 30), f)
        y += ch + 74

    # --- every raw animation and prop ---
    d.text((pad, y - 8), "raw frames", font=font(14), fill=MUTED)
    y += 22
    rows = [(n, s["frames"], f"{s['fps']}fps · {'loop' if s['loop'] else 'once'}")
            for n, s in anims.items()]
    rows += [(n, s["frames"], f"prop · {s['z']}") for n, s in props.items()]
    for name, frames, tag in rows:
        d.text((pad, y + ch // 2 - 16), name, font=font(17, True), fill=INK)
        d.text((pad, y + ch // 2 + 4), tag, font=font(13), fill=MUTED)
        for c, frame in enumerate(frames):
            fx = pad + label_w + c * (cw + gap)
            d.rectangle([fx - 4, y - 4, fx + cw + 4, y + ch + 4], fill=PANEL)
            f = render(frame, scale)
            img.paste(f, (fx + (cw - f.width) // 2, y + (ch - f.height)), f)
        y += row_h
    return img


def main():
    anims, props = collect(flat="--flat" in sys.argv)
    if "--preview" not in sys.argv:
        atlas, manifest = build_atlas(anims, props)
        atlas.save(HERE / "sprites.png")
        (HERE / "sprites.json").write_text(json.dumps(manifest, indent=1))
        print(f"sprites.png   {atlas.width}x{atlas.height}")
        print(f"sprites.json  {len(manifest['anims'])} animations, "
              f"{len(manifest['props'])} props")
    build_preview(anims, props).save(HERE / "preview.png")
    print("preview.png   ok")
    total = sum(len(a["frames"]) for a in anims.values())
    total += sum(len(p["frames"]) for p in props.values())
    print(f"validated     {total} frames, {len(anims)} animations, {len(props)} props")


if __name__ == "__main__":
    main()
