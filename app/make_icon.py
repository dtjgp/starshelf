"""Draw the Starshelf app icon: stars standing on shelves, i.e. starred repos sorted into lists.

    python3 make_icon.py            # writes AppIcon.icns and AppIcon.icon next to this script
    python3 make_icon.py --preview DIR   # also saves every size / appearance as PNG for checking

Two outputs:
  * AppIcon.icns - classic icon for macOS 14/15 (Pillow drawings below, packed with iconutil).
  * AppIcon.icon - layered Icon Composer document for macOS 26+: vector layers (gold star, shelf
    stars, shelves) on a slate fill with a darker fill in Dark mode. The system renders the Default,
    Dark, Clear and Tinted appearances from it; build_app.sh compiles it into Assets.car with actool.

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


# --------------------------------------------------------------------------- layered icon (macOS 26+)
ICTOOL = Path("/Applications/Xcode.app/Contents/Applications/Icon Composer.app/Contents/Executables/ictool")
APPEARANCES = ("Default", "Dark", "ClearLight", "ClearDark", "TintedLight", "TintedDark")


def _hex(c: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % c


def _srgb(c: tuple[int, int, int]) -> str:
    return "srgb:" + ",".join(f"{v / 255:.5f}" for v in c) + ",1.00000"


def _svg_star(cx: float, cy: float, r: float, color: tuple[int, int, int]) -> str:
    pts = " ".join(f"{x / S:.1f},{y / S:.1f}" for x, y in star(cx, cy, r))
    return f'<polygon points="{pts}" fill="{_hex(color)}"/>'


def _svg(body: str) -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">{body}</svg>\n'


def write_icon_document(out: Path) -> None:
    """The full drawing as an Icon Composer document. Its canvas is the whole 1024 square (the system
    applies the rounded shape), so the 824 px tile layout is scaled up by 1.1 around the centre."""
    k = 1.1
    f = lambda v: 512 + (v - 512) * k  # noqa: E731
    shelf_h = 54 * k
    shelves = "".join(f'<rect x="{f(190):.1f}" y="{f(y):.1f}" width="{(834 - 190) * k:.1f}" height="{shelf_h:.1f}" '
                      f'rx="{shelf_h / 3:.1f}" fill="{_hex(SHELF)}"/>' for y in (420, 800))
    top = "".join(_svg_star(f(x), f(420) - TIP * 96 * k - 1, 96 * k, c) for x, c in ((300, BLUE), (512, PINK), (724, GREEN)))
    gold = _svg_star(512, f(800) - TIP * 160 * k - 1, 160 * k, GOLD)
    if out.exists():
        shutil.rmtree(out)
    (out / "Assets").mkdir(parents=True)
    for name, body in (("gold-star.svg", gold), ("shelf-stars.svg", top), ("shelves.svg", shelves)):
        (out / "Assets" / name).write_text(_svg(body), encoding="utf-8")
    vertical = {"start": {"x": 0.5, "y": 0}, "stop": {"x": 0.5, "y": 1}}
    doc = {
        "fill": {"linear-gradient": [_srgb(TOP), _srgb(BOTTOM)], "orientation": vertical},
        "fill-specializations": [{"appearance": "dark", "value": {
            "linear-gradient": [_srgb((30, 36, 54)), _srgb((10, 12, 20))], "orientation": vertical}}],
        # Groups are listed front to back. In Dark mode the shelves stay light (glass translucency off,
        # so the dark fill cannot show through); in Tinted mode stars and shelves turn near-white, so
        # after tinting they are the brightest parts and keep their contrast.
        "groups": [
            {"name": "Gold star", "layers": [{"image-name": "gold-star.svg", "name": "gold-star",
                                              "fill-specializations": [{"appearance": "tinted", "value": {"solid": _srgb((255, 255, 255))}}]}],
             "shadow": {"kind": "layer-color", "opacity": 0.5}, "translucency": {"enabled": True, "value": 0.2}},
            {"name": "Shelf stars", "layers": [{"image-name": "shelf-stars.svg", "name": "shelf-stars",
                                                "fill-specializations": [{"appearance": "tinted", "value": {"solid": _srgb((236, 236, 236))}}]}],
             "shadow": {"kind": "layer-color", "opacity": 0.5}, "translucency": {"enabled": True, "value": 0.2}},
            {"name": "Shelves", "layers": [{"image-name": "shelves.svg", "name": "shelves",
                                            "fill-specializations": [
                                                {"appearance": "dark", "value": {"solid": _srgb((206, 210, 222))}},
                                                {"appearance": "tinted", "value": {"solid": _srgb((215, 215, 215))}}]}],
             "shadow": {"kind": "neutral", "opacity": 0.5}, "translucency": {"enabled": False, "value": 0.5}},
        ],
        "supported-platforms": {"squares": ["macOS"]},
    }
    import json
    (out / "icon.json").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def render_appearances(doc: Path, out_dir: Path, sizes: tuple[int, ...] = (1024, 64, 32)) -> None:
    """PNG previews of every appearance, rendered by Icon Composer's own ictool (if installed)."""
    if not ICTOOL.exists():
        print("ictool not found (needs Xcode with Icon Composer); skipped appearance previews")
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    for look in APPEARANCES:
        for px in sizes:
            extra = ["--tint-color", "0.6", "--tint-strength", "0.8"] if look.startswith("Tinted") else []
            subprocess.run([str(ICTOOL), str(doc), "--export-image", "--output-file", str(out_dir / f"{look}-{px}.png"),
                            "--platform", "macOS", "--rendition", look, "--width", str(px), "--height", str(px),
                            "--scale", "1", *extra], check=True, capture_output=True)


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
    doc = here / "AppIcon.icon"
    write_icon_document(doc)
    print("wrote", doc)
    if preview:
        render_appearances(doc, preview / "appearances")


if __name__ == "__main__":
    main(sys.argv[1:])
