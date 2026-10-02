"""Extract the approved Pixel Arcade sprites and add Quarter Century.

Run once: python scripts/build_achievement_art.py
The resulting app/images/achievements/*.png files are runtime assets; the
approved design sheet is only needed to rebuild them.
"""

from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "app" / "images" / "achievements"
BOXES = (
    (55, 603, 235, 721), (268, 603, 466, 721), (500, 603, 695, 721), (735, 603, 917, 721),
    (45, 767, 239, 889), (261, 767, 465, 889), (493, 767, 698, 889), (725, 767, 920, 889),
    (44, 925, 239, 1051), (268, 925, 465, 1051), (493, 925, 698, 1051), (725, 925, 920, 1051),
    (44, 1084, 239, 1216), (263, 1084, 465, 1216), (493, 1084, 698, 1216), (725, 1084, 920, 1216),
    (325, 1242, 626, 1370),
)


def save_sprite(sprite, number):
    sprite.thumbnail((112, 112), Image.Resampling.NEAREST)
    canvas = Image.new("RGBA", (128, 128))
    canvas.paste(sprite, ((128 - sprite.width) // 2, (128 - sprite.height) // 2), sprite)
    canvas.save(OUT / f"achievement-{number:03}.png")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with Image.open(ROOT / "design-previews" / "pixel-arcade-all-achievements.png") as sheet:
        for number, box in enumerate(BOXES, 1):
            sprite = sheet.crop(box).convert("RGBA")
            # Discard the card's dark background, not a shared enclosing badge.
            pixels = [(r, g, b, 0 if max(r, g, b) < 65 else a)
                      for r, g, b, a in sprite.getdata()]
            sprite.putdata(pixels)
            if sprite.getbbox():
                sprite = sprite.crop(sprite.getbbox())
            save_sprite(sprite, number)

    cabinet = Image.new("RGBA", (32, 40))
    pen = ImageDraw.Draw(cabinet)
    purple, light, gold, cyan = "#7160b7", "#c7b4ff", "#efc65a", "#38d5cf"
    pen.polygon([(6, 1), (25, 1), (28, 6), (26, 23), (29, 29), (29, 38),
                 (3, 38), (3, 29), (6, 23), (4, 6)], fill=purple)
    pen.line([(6, 1), (25, 1), (28, 6), (26, 23), (29, 29), (29, 38),
              (3, 38), (3, 29), (6, 23), (4, 6), (6, 1)], fill=light, width=2)
    pen.rectangle((7, 6, 24, 21), fill="#17151f", outline=gold)
    pen.rectangle((8, 3, 23, 4), fill=gold)
    glyphs = ("1110111", "0010100", "0010100", "1110111", "1000001", "1000001", "1110111")
    for row, line in enumerate(glyphs):
        for col, bit in enumerate(line):
            if bit == "1":
                pen.rectangle((8 + col * 2, 7 + row * 2, 9 + col * 2, 8 + row * 2), fill=cyan)
    pen.rectangle((5, 25, 26, 29), fill="#302744")
    pen.line((11, 28, 11, 23), fill=light, width=2)
    pen.rectangle((10, 22, 12, 24), fill=gold)
    pen.rectangle((20, 26, 22, 27), fill=cyan)
    pen.rectangle((9, 33, 23, 35), fill="#17151f", outline=gold)
    save_sprite(cabinet.resize((96, 120), Image.Resampling.NEAREST), 18)
    print(f"Saved 18 achievement sprites to {OUT}")


if __name__ == "__main__":
    main()
