#!/usr/bin/env python3
"""Проверка черновика промптов для креативов РСЯ по формальным признакам.

Читает текст ответа (файл или stdin). Каждый блок кода ``` считается одним
промптом. Проверяет: надписи заглавными, заголовок в 2-4 строки по 8-18
символов, нет лишней латиницы, в каждом блоке есть формат кадра, CROP SAFETY
и UPPERCASE, число блоков и пар «основной + 16:9».

Выход: 0 — замечаний нет, 1 — есть замечания (список в stdout).
"""

import argparse
import re
import sys

LABELS = r"(?:HEADLINE|SUBLINE|BUTTON|CHIP|LABEL|CAPTION)"
TEXT_RE = re.compile(LABELS + r"\s*(?:\((\d+)\s*lines?\))?[^:\n\"]*:\s*\"([^\"]+)\"", re.I)
READING_RE = re.compile(r"text reading\s*\"([^\"]+)\"", re.I)
ASPECT_RE = re.compile(r"\b(1:1|4:3|16:9)\b")
FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.S)


def split_blocks(text):
    return [b for b in FENCE_RE.findall(text) if b.strip()]


def is_upper(s):
    letters = [c for c in s if c.isalpha()]
    return all(c == c.upper() for c in letters)


def check_block(i, block, allow_latin, min_line, max_line):
    issues = []
    tag = "промпт %d" % i
    for needle in ("CROP SAFETY", "UPPERCASE"):
        if needle not in block:
            issues.append("%s: нет блока %s" % (tag, needle))
    canvas = re.search(r"Canvas\s*:\s*(1:1|4:3|16:9)", block)
    aspects = {canvas.group(1)} if canvas else set(ASPECT_RE.findall(block))
    if not aspects:
        issues.append("%s: не указан формат кадра (1:1, 4:3 или 16:9)" % tag)
    texts = [(m.group(0).split(":")[0].split("(")[0].strip().upper(), m.group(1), m.group(2))
             for m in TEXT_RE.finditer(block)]
    texts += [("TEXT", None, m.group(1)) for m in READING_RE.finditer(block)]
    if not texts:
        issues.append("%s: не найдено ни одной надписи в кавычках" % tag)
    has_headline = False
    for label, declared, value in texts:
        if not is_upper(value):
            issues.append('%s: есть строчные буквы в «%s»' % (tag, value))
        for word in re.findall(r"[A-Za-z][A-Za-z0-9]*", value):
            if word.upper() not in allow_latin:
                issues.append('%s: латиница «%s» в «%s» (разрешены: %s)' % (
                    tag, word, value, ", ".join(sorted(allow_latin)) or "нет"))
        if label == "HEADLINE":
            has_headline = True
            lines = [ln.strip() for ln in value.split("/") if ln.strip()]
            if not 2 <= len(lines) <= 4:
                issues.append("%s: заголовок в %d строк, нужно 2-4" % (tag, len(lines)))
            if declared and int(declared) != len(lines):
                issues.append("%s: заявлено %s строк, в кавычках %d" % (tag, declared, len(lines)))
            for pos, ln in enumerate(lines):
                if len(ln) > max_line:
                    issues.append("%s: строка «%s» %d символов, максимум %d" % (tag, ln, len(ln), max_line))
                elif len(ln) < min_line and pos < len(lines) - 1:
                    issues.append("%s: строка «%s» %d символов, меньше %d" % (tag, ln, len(ln), min_line))
    if texts and not has_headline:
        issues.append("%s: нет HEADLINE" % tag)
    return issues, aspects


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("file", nargs="?", help="файл с ответом; без аргумента читается stdin")
    p.add_argument("--expect-blocks", type=int, default=6, help="сколько промптов ожидается (по умолчанию 6; 0 — не проверять)")
    p.add_argument("--allow-latin", default="", help="разрешённая латиница через запятую, например SEO,B2B,LTV")
    p.add_argument("--min-line", type=int, default=8, help="минимум символов в строке заголовка кроме последней")
    p.add_argument("--max-line", type=int, default=18, help="максимум символов в строке заголовка")
    a = p.parse_args()

    text = open(a.file, encoding="utf-8").read() if a.file else sys.stdin.read()
    allow = {w.strip().upper() for w in a.allow_latin.split(",") if w.strip()}
    blocks = split_blocks(text)

    issues = []
    if a.expect_blocks and len(blocks) != a.expect_blocks:
        issues.append("блоков кода %d, ожидалось %d" % (len(blocks), a.expect_blocks))
    wide = other = 0
    for i, b in enumerate(blocks, 1):
        found, aspects = check_block(i, b, allow, a.min_line, a.max_line)
        issues += found
        if "16:9" in aspects:
            wide += 1
        elif aspects:
            other += 1
    if blocks and wide != other:
        issues.append("пары не сходятся: 16:9 — %d, основных — %d" % (wide, other))

    if issues:
        print("Замечания (%d):" % len(issues))
        for s in issues:
            print("- " + s)
        return 1
    print("Замечаний нет: блоков %d, пар 16:9 — %d." % (len(blocks), wide))
    return 0


if __name__ == "__main__":
    sys.exit(main())
