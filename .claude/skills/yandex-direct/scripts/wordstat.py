#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Вордстат через Search API Yandex Cloud: популярные запросы и динамика.

Токен Директа Вордстат не открывает. Нужны сервисный аккаунт Yandex Cloud с ролью
`search-api.webSearch.user` и API-ключ; ключ кладётся в config/.env как
YANDEX_WORDSTAT_API_KEY (каталог — YANDEX_WORDSTAT_FOLDER_ID, если нужен).
Подробнее: config/STUDENT_GUIDE.md, раздел «Вордстат».

    uv run scripts/wordstat.py top --phrase "директолог" --limit 50 --region 225
    uv run scripts/wordstat.py dynamics --phrase "директолог" --from 2025-11-01 --period month
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

from config import (  # noqa: E402
    ENV_FILE,
    DirectFailure,
    env_value,
    excerpt,
    keep_secret,
    load_env_file,
    redact,
)

BASE = "https://searchapi.api.cloud.yandex.net/v2/wordstat"
DEVICES = {"all": "DEVICE_ALL", "desktop": "DEVICE_DESKTOP", "phone": "DEVICE_PHONE",
           "tablet": "DEVICE_TABLET"}
PERIODS = {"month": "PERIOD_MONTHLY", "week": "PERIOD_WEEKLY", "day": "PERIOD_DAILY"}

NO_KEY = (
    "Ключ Вордстата не задан: YANDEX_WORDSTAT_API_KEY нет в config/.env.\n"
    "Токен Директа Вордстат не открывает. Нужны: аккаунт Yandex Cloud, сервисный аккаунт с ролью\n"
    "search-api.webSearch.user и API-ключ (aistudio.yandex.ru → Search API). Ключ вставьте в\n"
    "config/.env в редакторе, не в чат. Подробнее: config/STUDENT_GUIDE.md, раздел «Вордстат»."
)


def settings():
    from_file = load_env_file(ENV_FILE, strict=False) if ENV_FILE.is_file() else {}
    key = env_value("YANDEX_WORDSTAT_API_KEY", from_file)
    if not key:
        raise DirectFailure(NO_KEY)
    keep_secret(key)
    return key, env_value("YANDEX_WORDSTAT_FOLDER_ID", from_file)


def call(method: str, body: dict) -> dict:
    key, folder = settings()
    if folder:
        body = dict(body, folderId=folder)
    request = urllib.request.Request(
        f"{BASE}/{method}",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Api-Key {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as answer:
            return json.loads(answer.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        text = error.read().decode("utf-8", "replace")
        hint = {
            401: "ключ неверный или отозван",
            403: "у сервисного аккаунта нет роли search-api.webSearch.user или ключ без нужной области действия",
            429: "превышена квота запросов; подождите и повторите",
        }.get(error.code, "")
        raise DirectFailure(redact(f"Вордстат ответил {error.code}" + (f" ({hint})" if hint else "")
                                   + f": {excerpt(text, 400)}")) from None
    except urllib.error.URLError as error:
        raise DirectFailure(redact(f"Нет связи с Вордстатом: {error.reason}")) from None


def regions(values):
    return [str(v) for v in values] or None


def show_rows(rows, header, csv_path):
    if csv_path:
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle, delimiter=";")
            writer.writerow(header)
            writer.writerows(rows)
        print(f"Сохранено в {csv_path}: {len(rows)} строк")
        return
    for row in rows:
        print("  " + " · ".join(str(x) for x in row))


def top(args) -> int:
    body = {"phrase": args.phrase, "numPhrases": str(args.limit),
            "devices": [DEVICES[args.devices]]}
    if args.region:
        body["regions"] = regions(args.region)
    data = call("topRequests", body)
    print(f"Фраза «{args.phrase}» · всего запросов со всеми словами: {data.get('totalCount', '—')} "
          f"(последние 30 дней)")
    print("\nПопулярные запросы:")
    show_rows([(r.get("phrase"), r.get("count")) for r in data.get("results", [])],
              ("фраза", "запросов"), args.csv)
    similar = data.get("associations", [])
    if similar and not args.csv:
        print("\nПохожие запросы:")
        show_rows([(r.get("phrase"), r.get("count")) for r in similar], ("фраза", "запросов"), None)
    return 0


def dynamics(args) -> int:
    body = {"phrase": args.phrase, "period": PERIODS[args.period],
            "fromDate": f"{args.date_from}T00:00:00Z", "devices": [DEVICES[args.devices]]}
    if args.date_to:
        body["toDate"] = f"{args.date_to}T00:00:00Z"
    if args.region:
        body["regions"] = regions(args.region)
    data = call("dynamics", body)
    print(f"Фраза «{args.phrase}» · динамика ({args.period})")
    def share(value):
        try:
            return f"{float(value):.7f}"
        except (TypeError, ValueError):
            return value

    show_rows([(str(r.get("date", ""))[:10], r.get("count"), share(r.get("share")))
               for r in data.get("results", [])],
              ("дата", "запросов", "доля от всех запросов"), args.csv)
    return 0


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, redact(f"{self.prog}: {message}\n"))


def main() -> int:
    parser = Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--phrase", required=True, help="фраза, до 400 символов")
    common.add_argument("--region", action="append", default=[], metavar="ID",
                        help="ID региона (225 — Россия); можно несколько")
    common.add_argument("--devices", choices=list(DEVICES), default="all")
    common.add_argument("--csv", metavar="ФАЙЛ", help="сохранить в CSV вместо вывода")
    one = sub.add_parser("top", parents=[common], help="популярные запросы за 30 дней")
    one.add_argument("--limit", type=int, default=50, help="сколько фраз, 1–2000")
    two = sub.add_parser("dynamics", parents=[common], help="динамика частоты запросов")
    two.add_argument("--from", dest="date_from", required=True, metavar="ГГГГ-ММ-ДД")
    two.add_argument("--to", dest="date_to", metavar="ГГГГ-ММ-ДД")
    two.add_argument("--period", choices=list(PERIODS), default="month")
    args = parser.parse_args()
    try:
        return top(args) if args.action == "top" else dynamics(args)
    except DirectFailure as error:
        print(redact(str(error)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
