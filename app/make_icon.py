"""Draw the Starshelf app icon: stars standing on shelves, i.e. starred repos sorted into lists.

    python3 make_icon.py            # writes AppIcon.icns next to this script (macOS: uses iconutil)
    python3 make_icon.py --preview DIR   # also saves every size as PNG for checking

Three drawings share one look (dark slate tile, light shelves, list-coloured stars, a gold star):
  * full  (128 pt and up): two shelves; three coloured stars on top, a large gold star below
  * small (32 pt): one shelf with a gold star between a blue and a green star
  * tiny  (16 pt): one shelf with the gold star only
Point sizes are used so a 32 pt icon on a Retina screen (64 px) still gets the simple drawing.
"""
from __future__ import annotations

import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

S = 4                      # supersampling factor
W = 1024 * S
TILE = (100, 924)          # macOS icon grid: 824 px body inside a 1024 canvas
RADIUS = 186
TOP, BOTTOM = (54, 68, 108), (21, 28, 45)
SHELF, SHELF_EDGE = (238, 233, 224), (199, 190, 176)
BLUE, PINK, GREEN, GOLD = (62, 111, 216), (194, 65, 122), (46, 158, 106), (240, 184, 58)
TIP = 0.809                # a star's lower tips sit this far below its centre (times the radius)

# (file name in the .iconset, pixel size, drawing)
SIZES = [("icon_16x16.png", 16, "tiny"), ("icon_16x16@2x.png", 32, "tiny"),
         ("icon_32x32.png", 32, "small"), ("icon_32x32@2x.png", 64, "small"),
         ("icon_128x128.png", 128, "full"), ("icon_128x128@2x.png", 256, "full"),
         ("icon_256x256.png", 256, "full"), ("icon_256x256@2x.png", 512, "full"),
         ("icon_512x512.png", 512, "full"), ("icon_512x512@2x.png", 1024, "full")]


def sc(v: float) -> float:
    return v * S


def star(cx: float, cy: float, r: float, ratio: float = 0.47) -> list[tuple[float, float]]:
    return [(sc(cx + (r if i % 2 == 0 else r * ratio) * math.cos(math.radians(-90 + i * 36))),
             sc(cy + (r if i % 2 == 0 else r * ratio) * math.sin(math.radians(-90 + i * 36)))) for i in range(10)]


def blurred(draw_fn, blur: float) -> Image.Image:
    layer = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(layer))
    return layer.filter(ImageFilter.GaussianBlur(sc(blur)))


def tile() -> Image.Image:
    """The rounded-square body with a top-to-bottom gradient and a soft drop shadow."""
    grad = Image.new("RGBA", (W, W))
    gd = ImageDraw.Draw(grad)
    for y in range(W):
        t = y / (W - 1)
        gd.line([(0, y), (W, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM)) + (255,))
    mask = Image.new("L", (W, W), 0)
    ImageDraw.Draw(mask).rounded_rectangle([sc(TILE[0]), sc(TILE[0]), sc(TILE[1]), sc(TILE[1])], radius=sc(RADIUS), fill=255)
    body = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    body.paste(grad, (0, 0), mask)
    shadow = blurred(lambda d: d.rounded_rectangle([sc(TILE[0]), sc(TILE[0] + 12), sc(TILE[1]), sc(TILE[1] + 12)],
                                                   radius=sc(RADIUS), fill=(0, 0, 0, 90)), 14)
    return Image.alpha_composite(shadow, body)


def shelf(canvas: Image.Image, x0: float, x1: float, y: float, h: float) -> None:
    """A plank: light top face, darker front edge, soft shadow below."""
    canvas.alpha_composite(blurred(lambda d: d.rounded_rectangle([sc(x0), sc(y + h * 0.35), sc(x1), sc(y + h * 1.25)],
                                                                 radius=sc(h / 3), fill=(0, 0, 0, 120)), h / 4))
    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle([sc(x0), sc(y), sc(x1), sc(y + h)], radius=sc(h / 3), fill=SHELF + (255,))
    d.rounded_rectangle([sc(x0), sc(y + h * 0.58), sc(x1), sc(y + h)], radius=sc(h / 4), fill=SHELF_EDGE + (255,))


def standing_star(canvas: Image.Image, cx: float, shelf_y: float, r: float, color: tuple[int, int, int]) -> None:
    """A star whose lower tips rest on a shelf top at ``shelf_y``; lit from the upper left."""
    cy = shelf_y - TIP * r - 1
    canvas.alpha_composite(blurred(lambda d: d.polygon(star(cx, cy + r * 0.06, r), fill=(0, 0, 0, 70)), r * 0.08))
    d = ImageDraw.Draw(canvas)
    pts = star(cx, cy, r)
    d.polygon(pts, fill=color + (255,))
    light = tuple(min(255, c + 34) for c in color)
    d.polygon([(sc(cx), sc(cy))] + pts[7:10] + [pts[0]], fill=light + (255,))


def draw(kind: str) -> Image.Image:
    canvas = tile()
    if kind == "full":
        shelf(canvas, 190, 834, 420, 54)
        shelf(canvas, 190, 834, 800, 54)
        for x, color in ((300, BLUE), (512, PINK), (724, GREEN)):
            standing_star(canvas, x, 420, 96, color)
        standing_star(canvas, 512, 800, 160, GOLD)
    elif kind == "small":
        shelf(canvas, 160, 864, 692, 84)
        standing_star(canvas, 240, 692, 100, BLUE)
        standing_star(canvas, 784, 692, 100, GREEN)
        standing_star(canvas, 512, 692, 215, GOLD)
    else:  # tiny
        shelf(canvas, 180, 844, 720, 110)
        standing_star(canvas, 512, 720, 270, GOLD)
    return canvas.resize((1024, 1024), Image.LANCZOS)


def main(argv: list[str]) -> None:
    here = Path(__file__).resolve().parent
    preview = Path(argv[argv.index("--preview") + 1]) if "--preview" in argv else None
    drawings = {kind: draw(kind) for kind in ("full", "small", "tiny")}
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "AppIcon.iconset"
        iconset.mkdir()
        for name, px, kind in SIZES:
            drawings[kind].resize((px, px), Image.LANCZOS).save(iconset / name)
        if preview:
            preview.mkdir(parents=True, exist_ok=True)
            for f in iconset.iterdir():
                shutil.copy(f, preview / f.name)
        out = here / "AppIcon.icns"
        if shutil.which("iconutil"):
            subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True)
        else:  # not on macOS: Pillow derives the small sizes from the full drawing
            drawings["full"].save(out)
    print("wrote", out)


if __name__ == "__main__":
    main(sys.argv[1:])
