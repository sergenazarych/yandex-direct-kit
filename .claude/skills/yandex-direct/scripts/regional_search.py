"""Региональная органическая выдача через настроенное подключение Yandex Cloud."""

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date

from wordstat import DirectFailure, settings


def documents(data):
    root = ET.fromstring(base64.b64decode(data["rawData"], validate=True))
    error = root.find(".//response/error")
    if error is not None:
        raise DirectFailure(f"Ответ Яндекса: {error.text}")
    rows = []
    for doc in root.findall(".//results/grouping/group/doc"):
        title = doc.find("title")
        rows.append({"url": doc.findtext("url"), "title": "".join(title.itertext()) if title is not None else ""})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True)
    parser.add_argument("--region", required=True, type=int)
    args = parser.parse_args()
    key, folder = settings()
    body = {"query": {"queryText": args.query, "searchType": "SEARCH_TYPE_RU"},
            "region": str(args.region), "responseFormat": "FORMAT_XML",
            "groupSpec": {"groupMode": "GROUP_MODE_FLAT", "groupsOnPage": "10", "docsInGroup": "1"}}
    if folder:
        body["folderId"] = folder
    request = urllib.request.Request("https://searchapi.api.cloud.yandex.net/v2/web/search",
                                     data=json.dumps(body).encode(), headers={
                                         "Authorization": f"Api-Key {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        rows = documents(json.load(response))
    print(json.dumps({"date": date.today().isoformat(), "query": args.query,
                      "region": args.region, "results": rows}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (DirectFailure, OSError, ValueError, KeyError, ET.ParseError) as exc:
        sys.exit(f"Региональную выдачу получить не удалось: {exc}")
