#!/usr/bin/env python3
"""Имитация центральных обрезок Яндекса (1:1, 4:3, 3:4, 16:9) для готового креатива.

Из картинки вырезается максимальная центральная область каждого соотношения,
результат складывается в одну контрольную картинку `<имя>_crops.png` рядом
с файлом (или в --out). Реальные смарт-центры Яндекса могут сместить кадр:
скрипт показывает худший случай обрезки строго по центру.

Нужен Pillow (pip install pillow). Выход: 0 — готово, 1 — ошибка чтения.
"""

import argparse
import os
import sys

RATIOS = {"1:1": (1, 1), "4:3": (4, 3), "3:4": (3, 4), "16:9": (16, 9)}


def center_crop(img, ratio):
    w, h = img.size
    rw, rh = ratio
    target = rw / rh
    if w / h > target:
        nw, nh = round(h * target), h
    else:
        nw, nh = w, round(w / target)
    left, top = (w - nw) // 2, (h - nh) // 2
    return img.crop((left, top, left + nw, top + nh))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("image", help="путь к картинке (png, jpg, webp)")
    p.add_argument("--out", help="папка результата; по умолчанию рядом с картинкой")
    p.add_argument("--ratios", default="1:1,4:3,3:4,16:9", help="соотношения через запятую")
    p.add_argument("--height", type=int, default=420, help="высота превью каждой обрезки в пикселях")
    a = p.parse_args()

    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("Нужен Pillow: pip install pillow", file=sys.stderr)
        return 1
    try:
        img = Image.open(a.image).convert("RGB")
    except OSError as e:
        print("Не удалось открыть %s: %s" % (a.image, e), file=sys.stderr)
        return 1

    tiles = []
    for name in [r.strip() for r in a.ratios.split(",") if r.strip()]:
        if name not in RATIOS:
            print("Неизвестное соотношение %s (есть: %s)" % (name, ", ".join(RATIOS)), file=sys.stderr)
            return 1
        c = center_crop(img, RATIOS[name])
        k = a.height / c.size[1]
        c = c.resize((round(c.size[0] * k), a.height))
        tiles.append((name, c))

    pad, label = 16, 28
    width = sum(t.size[0] for _, t in tiles) + pad * (len(tiles) + 1)
    sheet = Image.new("RGB", (width, a.height + label + pad * 2), (245, 245, 245))
    draw = ImageDraw.Draw(sheet)
    x = pad
    for name, t in tiles:
        draw.text((x, pad // 2), name, fill=(20, 20, 20))
        sheet.paste(t, (x, label + pad // 2))
        x += t.size[0] + pad

    out_dir = a.out or os.path.dirname(os.path.abspath(a.image))
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(a.image))[0]
    path = os.path.join(out_dir, base + "_crops.png")
    sheet.save(path)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
