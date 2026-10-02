"""Render the app icon (same drawing as ui/static/icon.svg) to icon.png and icon.ico.

Needs Pillow:  python packaging/make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).parent


def draw(size: int = 1024) -> Image.Image:
    s = size / 64.0
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def rr(x, y, w, h, r, **kw):
        d.rounded_rectangle([x * s, y * s, (x + w) * s, (y + h) * s], radius=r * s, **kw)

    rr(4, 10, 40, 30, 5, fill="#e8662f")
    rr(20, 24, 40, 30, 5, fill="#2f5bd3")
    rr(26, 30, 16, 4, 2, fill="#ffffff")
    rr(26, 38, 24, 3, 1.5, fill=(255, 255, 255, 180))
    rr(26, 44, 20, 3, 1.5, fill=(255, 255, 255, 180))
    rr(46, 30, 9, 9, 2, outline="#ffffff", width=max(1, int(2 * s)))
    return img


if __name__ == "__main__":
    big = draw()
    big.resize((512, 512), Image.LANCZOS).save(HERE / "icon.png")
    big.save(HERE / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote icon.png, icon.ico")
