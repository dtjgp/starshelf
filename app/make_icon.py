"""Draw the Starshelf app icon: a gold star joined to three list-coloured stars, like a star chart."""
from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

S = 4  # supersampling factor
W = 1024 * S


def star_points(cx: float, cy: float, r_out: float, ratio: float = 0.46, rot: float = -90) -> list[tuple[float, float]]:
    pts = []
    for i in range(10):
        r = r_out if i % 2 == 0 else r_out * ratio
        a = math.radians(rot + i * 36)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def scale(v: float) -> float:
    return v * S


def main(out_dir: Path) -> None:
    # body: macOS icon grid (824 px squircle-ish rounded square inside a 1024 canvas)
    body = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    grad = Image.new("RGBA", (W, W))
    top, bottom = (46, 59, 92), (17, 23, 36)
    gd = ImageDraw.Draw(grad)
    for y in range(W):
        t = y / (W - 1)
        gd.line([(0, y), (W, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,))
    mask = Image.new("L", (W, W), 0)
    ImageDraw.Draw(mask).rounded_rectangle([scale(100), scale(100), scale(924), scale(924)], radius=scale(186), fill=255)
    body.paste(grad, (0, 0), mask)

    # soft drop shadow under the body
    shadow = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle([scale(100), scale(112), scale(924), scale(936)], radius=scale(186), fill=(0, 0, 0, 110))
    shadow = shadow.filter(ImageFilter.GaussianBlur(scale(14)))
    canvas = Image.alpha_composite(shadow, body)

    draw = ImageDraw.Draw(canvas)
    # faint background stars (clipped to body)
    dots = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    dd = ImageDraw.Draw(dots)
    for x, y, r in [(250, 640, 5), (330, 800, 4), (690, 820, 5), (820, 470, 4), (430, 210, 4), (600, 180, 3), (190, 420, 3), (760, 700, 3)]:
        dd.ellipse([scale(x - r), scale(y - r), scale(x + r), scale(y + r)], fill=(220, 228, 245, 120))
    canvas.alpha_composite(Image.composite(dots, Image.new("RGBA", (W, W)), mask))

    big = (470, 560, 235)
    small = [((300, 285, 64), (194, 65, 122)),   # magenta
             ((735, 300, 76), (62, 111, 216)),   # blue
             ((775, 650, 58), (46, 158, 106))]   # green
    # constellation lines
    lines = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lines)
    for (x, y, _), _c in small:
        ld.line([scale(big[0]), scale(big[1]), scale(x), scale(y)], fill=(205, 215, 235, 95), width=scale(7))
    ld.line([scale(735), scale(300), scale(775), scale(650)], fill=(205, 215, 235, 70), width=scale(5))
    canvas.alpha_composite(lines)

    # glow + gold star
    glow = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    ImageDraw.Draw(glow).polygon([(scale(x), scale(y)) for x, y in star_points(*big)], fill=(226, 176, 72, 150))
    glow = glow.filter(ImageFilter.GaussianBlur(scale(28)))
    canvas.alpha_composite(glow)
    draw = ImageDraw.Draw(canvas)
    draw.polygon([(scale(x), scale(y)) for x, y in star_points(*big)], fill=(232, 182, 64, 255))
    # light facet on the upper-left half for a little depth
    facet = star_points(*big)
    cx, cy = scale(big[0]), scale(big[1])
    half = [(cx, cy)] + [(scale(x), scale(y)) for x, y in facet[7:10]] + [(scale(facet[0][0]), scale(facet[0][1]))]
    draw.polygon(half, fill=(244, 205, 110, 255))
    for (x, y, r), color in small:
        draw.polygon([(scale(px), scale(py)) for px, py in star_points(x, y, r)], fill=color + (255,))

    icon = canvas.resize((1024, 1024), Image.LANCZOS)
    out_dir.mkdir(parents=True, exist_ok=True)
    icon.save(out_dir / "AppIcon-1024.png")
    icon.save(out_dir / "AppIcon.icns")
    print("wrote", out_dir / "AppIcon.icns")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "."))
