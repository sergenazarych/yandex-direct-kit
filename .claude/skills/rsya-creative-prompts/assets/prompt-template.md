# Шаблон вывода: один стиль = идея + два промпта

Каждый промпт лежит в своём блоке кода (для копирования одним нажатием). Интерфейс для `scripts/check_prompts.py`: надписи пишутся как `HEADLINE (N lines): "СТРОКА / СТРОКА"`, `SUBLINE:`, `BUTTON:`, `CHIP:`, `LABEL:`; в блоке есть `CROP SAFETY` и `UPPERCASE`; формат кадра указан как `1:1`, `4:3` или `16:9`.

## Как выглядит ответ

```
Заголовок: «…» (таблица, группа «Крео N»; сегмент из паспорта «…»)

### Стиль 1: <категория, тип> · угол: <механика>
Идея: что изображено и что написано, 1–2 строки по-русски.

Основной (1:1 или 4:3):
<блок кода с промптом>

16:9:
<блок кода с промптом>

### Стиль 2 …
### Стиль 3 …

Сверить в готовых картинках: <слова из надписей>
```

## Каркас промпта (английский, надписи кириллицей)

```
ACT AS A HIGH-END ADVERTISING ART DIRECTOR. Create a <style: photo / 3D render / flat vector / engraving …> advertising image for Yandex RSYA.
- Canvas: <1:1 | 4:3 | 16:9>, full bleed, edge to edge.
- Scene and subject: <what is in the frame, pose, expression, clothing; for an expert: use the attached photo as the exact face reference, do not change the face>.
- Background: <colour with HEX, depth of field, props>.
- Light and mood: <light type, colour temperature, mood>.
- Composition and copy space: <scheme from styles.md; where the subject is, where the empty text zone is, no box behind the text unless a plate is specified>.
- HEADLINE (<N> lines): "<СТРОКА 1> / <СТРОКА 2> / <СТРОКА 3>" - <font type from type-library.md>, <colour with HEX>, left-aligned | centered, block width <45-65>% of the canvas, position <where>. Key word "<СЛОВО>" in <accent colour>.
- SUBLINE: "<ТЕКСТ>" - <font>, <colour>, <position>.   (optional)
- BUTTON: "<ТЕКСТ>" - <shape, colour, text colour, position>.   (optional)
- CHIP: "<ТЕКСТ>"   (optional, up to three)
- CROP SAFETY (MANDATORY): the headline block (and the subject's head, when a person is in the frame) sits inside <safe zone for this canvas from rules.md>, on the same horizontal band. Nothing critical in the outer 20% of the frame. The button and logo may be lost to a crop; the headline and the face may not.
- TEXT RULES: ALL TEXT IN UPPERCASE CYRILLIC ONLY (brand abbreviations from the headline allowed). NO LOWERCASE LETTERS. NO ENGLISH WORDS. Render every word exactly as quoted, no extra text.
- <standard tail from rules.md>
```

Для 16:9 в блоке Composition описана перестроенная двухколоночная схема внутри центральной полосы, а не растянутый основной кадр.
