"""Загрузчик: таблица кампании (xlsx) -> кабинет Яндекс Директа.

По умолчанию ничего не пишет: показывает план («что будет создано»).
Запись включает --apply. Повторный запуск не плодит дубли: существующие
группы, фразы и объявления определяются по выгрузке кампании.

Использует готовые команды скилла yandex-direct (scripts/*.py).
Кнопку объявления («Смотреть») API комбинаторных объявлений не принимает —
загрузчик печатает такие строки отдельным списком для ручной настройки.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter

from openpyxl import load_workbook

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # каталог скилла
NARROW = ".,:;!'|"
REGIONS = {"россия": 225, "республика крым": 977, "крым": 977,
           "северный кавказ": 102444, "москва": 213, "санкт-петербург": 2}
MAX_ADS_PER_GROUP = 3
PLACEHOLDER = "ваш-сайт.ru"  # заглушка в учебном шаблоне


def narrow_ok(text, base=81, extra=15):
    n = sum(text.count(c) for c in NARROW)
    return len(text) - n <= base and n <= extra


def cell(v):
    return "" if v is None else str(v).strip()


def run(script, *args, account, apply=False, json_out=False):
    cmd = [sys.executable, os.path.join(SKILL, "scripts", script), "--account", account]
    if json_out:
        cmd.append("--json")
    cmd += list(args)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    p = subprocess.run(cmd, cwd=SKILL, capture_output=True, text=True,
                       encoding="utf-8-sig", env=env)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def region_ids(text, problems, where):
    ids = []
    for part in [x.strip() for x in text.replace(",", ";").split(";") if x.strip()]:
        if part.lstrip("-").isdigit():
            ids.append(int(part))
        elif part.lower() in REGIONS:
            ids.append(REGIONS[part.lower()])
        else:
            problems.append(f"{where}: неизвестный регион «{part}» (впишите ID региона)")
    return ids


class Table:
    def __init__(self, path):
        self.path = path
        self.dir = os.path.dirname(os.path.abspath(path))
        wb = load_workbook(path, data_only=True)
        self.wb = wb

    def rows(self, sheet):
        ws = self.wb[sheet]
        head = [cell(c.value) for c in ws[1]]
        for r in ws.iter_rows(min_row=2, values_only=True):
            if not any(cell(x) for x in r):
                continue
            yield dict(zip(head, [cell(x) for x in r]))

    def campaign(self):
        return {r["Параметр"]: r["Значение"] for r in self.rows("Кампания")}


def col(row, prefix):
    for k, v in row.items():
        if k.startswith(prefix):
            return v
    return ""


def cols(row, prefix):
    return [v for k, v in row.items() if k.startswith(prefix) and v]


def existing_targets(account, group_ids):
    """Условия ретаргетинга, уже привязанные к группам: {группа: {id условия}}."""
    if not group_ids:
        return {}
    sys.path[:0] = [os.path.join(SKILL, "scripts"), os.path.join(SKILL, "scripts", "lib")]
    from direct import Client
    c = Client.from_env(account=account)
    r = c.call("AudienceTargets", "get",
               {"SelectionCriteria": {"AdGroupIds": list(group_ids)},
                "FieldNames": ["AdGroupId", "RetargetingListId"]},
               account=account, raise_on_error=False)
    found = {}
    for t in (r.result or {}).get("AudienceTargets", []) or []:
        found.setdefault(t["AdGroupId"], set()).add(t["RetargetingListId"])
    return found


def norm_site(x):
    """Площадка в виде, в котором её хранит Директ: кириллица — punycode."""
    x = x.strip()
    if any(ord(c) > 127 for c in x):
        try:
            return x.encode("idna").decode()
        except UnicodeError:
            return x
    return x


def add_sites(a, cid, sites, flag):
    """Площадки пакетами: при отказе пакета делим его, чтобы найти негодную."""
    def batch(items):
        args = list(flag) + ["sites", "--campaign", cid, "--add"]
        for s in items:
            args += ["--site", s]
        rc, o = run("campaign_write.py", *args, account=a.account)
        return "записано 1" in o

    def reread():
        rc, out = run("campaigns.py", "--campaign", cid, "--no-cache", account=a.account, json_out=True)
        return {norm_site(x).lower() for x in (last_json(out)["campaign"]["raw"].get("ExcludedSites") or {}).get("Items") or []}

    def push(items, size):
        for i in range(0, len(items), size):
            part = items[i:i + size]
            if not batch(part) and size > 1:
                have = reread()
                rest = [x for x in part if x.lower() not in have]
                if rest:
                    push(rest, max(1, size // 5))
    push(sites, 100)
    have = reread()
    return sum(1 for x in sites if x.lower() in have)


def last_json(out):
    return json.loads(out[out.index("{"):])


def ids_in(out):
    """ID дополнений из ответа: «Принятые API ID: N» или «существующее … N:»."""
    import re
    got = re.findall(r"Принятые API ID:\s*([\d, ]+)", out)
    got += re.findall(r"существующ\w+\s+\w+\s+(\d+)", out)
    return [int(x) for g in got for x in re.findall(r"\d+", g)]


def post_stage(a, t, camp, campaign_id):
    """Запрещённые площадки, персонализация, возраст, быстрые ссылки, уточнения.

    Каждый шаг повторяемый: сначала читается состояние, пишется только нужное.
    """
    import re
    flag = ["--apply"] if a.apply else []
    cid = str(campaign_id)
    print("\n== Настройки кампании ==")

    # персонализация (Нейро, альтернативные тексты)
    want = camp.get("Персонализация (Яндекс Нейро / альтернативные тексты)", "").lower()
    if want.startswith("выкл"):
        rc, out = run("campaigns.py", "--campaign", cid, "--no-cache", account=a.account, json_out=True)
        state = last_json(out)["campaign"].get("settings", {}).get("ALTERNATIVE_TEXTS_ENABLED")
        if state == "NO":
            print("  = персонализация уже выключена")
        else:
            rc, out = run("campaign_write.py", *flag, "personalization", "--campaign", cid, "--off", account=a.account)
            print(f"  + персонализация: {'выключена' if a.apply else 'будет выключена'}")

    # корректировки по возрасту
    wanted = []
    for label, age in (("Корректировка: возраст до 18, %", "AGE_0_17"), ("Корректировка: возраст 55+, %", "AGE_55")):
        v = camp.get(label, "")
        if v:
            wanted.append((age, max(0, round((100 + float(v)) / 100, 2))))
    if wanted:
        rc, out = run("bids.py", "modifier", "list", "--campaign", cid, account=a.account)
        for age, k in wanted:
            if f"Age={age}" in out:
                print(f"  = корректировка {age}: уже есть")
                continue
            rc, o = run("bids.py", *flag, "modifier", "add", "--campaign", cid,
                        "--demographics", f"{age}={k:g}", account=a.account)
            if a.apply and "записано 1" not in o:      # Директ иногда отвечает «неизвестно»
                rc, chk = run("bids.py", "modifier", "list", "--campaign", cid, account=a.account)
                if f"Age={age}" not in chk:
                    rc, o = run("bids.py", *flag, "modifier", "add", "--campaign", cid,
                                "--demographics", f"{age}={k:g}", account=a.account)
            print(f"  + корректировка {age}: коэффициент {k:g} {'записано' if a.apply else 'будет записано'}")

    # запрещённые площадки
    names = [x.strip() for x in re.split(r"[;\n]+", camp.get("Запрещённые площадки", "")) if x.strip()]
    if names:
        sites, lost = [], []
        for fname in names:
            path = os.path.join(t.dir, fname)
            if not os.path.exists(path):
                print(f"  ✗ файл площадок «{fname}» не найден рядом с таблицей")
                continue
            # в файле площадки через запятую и/или по одной в строке
            for x in re.split(r"[,\n\r]+", open(path, encoding="utf-8-sig").read()):
                x = x.strip()
                if x and norm_site(x) not in {norm_site(y) for y in sites}:
                    sites.append(x)
        if len(sites) > 1000:
            print(f"  ✗ площадок {len(sites)}, Директ принимает не больше 1000")
        elif sites:
            rc, out = run("campaigns.py", "--campaign", cid, "--no-cache", account=a.account, json_out=True)
            have = {norm_site(x) for x in (last_json(out)["campaign"]["raw"].get("ExcludedSites") or {}).get("Items") or []}
            missing = [norm_site(x) for x in sites if norm_site(x) not in have]
            if not missing:
                print(f"  = запрещённые площадки: все {len(sites)} уже есть")
            elif not a.apply:
                print(f"  + запрещённые площадки: {len(missing)} из {len(sites)} будет добавлено")
            else:
                done = add_sites(a, cid, missing, flag)
                print(f"  + запрещённые площадки: добавлено {done} из {len(missing)}")
                if done < len(missing):
                    print("  ✗ часть площадок Директ не принял — смотрите журнал; повторный запуск догрузит остаток")

    # быстрые ссылки и уточнения
    print("\n== Быстрые ссылки и уточнения ==")
    links = [r for r in t.rows("Быстрые ссылки") if r.get("Заголовок (до 30)") and r.get("Адрес")]
    callouts = [r["Текст уточнения (до 25)"] for r in t.rows("Уточнения") if r.get("Текст уточнения (до 25)")]
    for r in links:
        if len(r["Заголовок (до 30)"]) > 30 or len(r.get("Описание (до 60)", "")) > 60:
            print("  ✗ быстрая ссылка длиннее лимита:", r["Заголовок (до 30)"]); return
    if any(len(c) > 25 for c in callouts):
        print("  ✗ уточнение длиннее 25 символов"); return

    rc, out = run("campaign_dump.py", "--campaign", cid, "--no-cache", account=a.account)
    with open(os.path.join(SKILL, "cache", a.account, f"campaign-dump-{cid}.json"), encoding="utf-8-sig") as fh:
        ads = [x for x in json.load(fh)["ads"] if x.get("state") != "ARCHIVED"]
    need = [x["id"] for x in ads if not x.get("sitelink_set_id") or not x.get("ad_extensions")]
    if not need:
        print(f"  = у всех {len(ads)} объявлений уже есть быстрые ссылки и уточнения")
        return
    state_file = os.path.join(SKILL, "settings", "extensions_state.json")
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    try:
        state = json.load(open(state_file, encoding="utf-8"))
    except Exception:
        state = {}
    set_key = a.account + "|" + json.dumps([[r["Заголовок (до 30)"], r.get("Описание (до 60)", ""), r["Адрес"]] for r in links], ensure_ascii=False)
    sitelink_id = state.get(set_key)
    if links and not sitelink_id:
        tmp = os.path.join(os.environ.get("TEMP", "."), "sitelinks_load.json")
        json.dump([{"Title": r["Заголовок (до 30)"], "Description": r.get("Описание (до 60)", ""), "Href": r["Адрес"]} for r in links],
                  open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
        rc, o = run("extensions.py", "sitelinks", "create", "--file", tmp, *flag, account=a.account)
        ids = ids_in(o)
        sitelink_id = ids[0] if ids else None
        if a.apply and not sitelink_id:
            print("  ✗ не удалось получить ID набора быстрых ссылок:\n" + o[-400:]); return
        if sitelink_id:
            state[set_key] = sitelink_id
        print(f"  + набор быстрых ссылок ({len(links)}): {'создан' if a.apply else 'будет создан'}")
    callout_ids = []
    for text in callouts:
        rc, o = run("extensions.py", "callouts", "create", "--text", text, *flag, account=a.account)
        ids = ids_in(o)
        if ids:
            callout_ids.append(ids[0])
        elif a.apply:
            print(f"  ✗ уточнение «{text}»: ID не получен:\n" + o[-300:]); return
    print(f"  + уточнений: {len(callouts)} {'готово' if a.apply else 'будет создано/использовано'}")
    if a.apply:
        json.dump(state, open(state_file, "w", encoding="utf-8"), ensure_ascii=False)
        args = ["bind", "--apply"]
        for i in need:
            args += ["--ad", str(i)]
        if sitelink_id:
            args += ["--sitelink-set", str(sitelink_id)]
        for c in callout_ids:
            args += ["--callout-id", str(c)]
        rc, o = run("extensions.py", *args, account=a.account)
        ok = o.count("привязки подтверждены")
        print(f"  + привязано к объявлениям: {ok} из {len(need)}")
    else:
        print(f"  + будет привязано к объявлениям: {len(need)}")


def rsya_checks(t, camp, groups, ads):
    """Замечания по эталону РСЯ (references/RSYA.md). Ничего не блокируют."""
    notes = []
    def num(key):
        try:
            return float(str(camp.get(key, "")).replace(" ", "").replace(",", "."))
        except ValueError:
            return None
    budget, cpa = num("Недельный бюджет, ₽"), num("Ориентир цены конверсии, ₽")
    conv = num("Конверсий в неделю (минимум 10, лучше 12–15)")
    if budget and cpa and budget < cpa * 10:
        notes.append(f"бюджет {budget:g} ₽ в неделю меньше {cpa * 10:g} ₽ (10 конверсий × {cpa:g} ₽): кампании не хватит данных для обучения")
    elif budget and cpa and conv and budget < cpa * conv:
        notes.append(f"бюджет {budget:g} ₽ меньше {cpa * conv:g} ₽ ({conv:g} конверсий × {cpa:g} ₽)")
    cap = num("Потолок ставки, ₽")
    if not cap:
        notes.append("не задан потолок ставки (в эталоне 400 ₽: защита от залётных кликов из приложений)")
    if len(groups) != 3:
        notes.append(f"групп {len(groups)}, в эталоне 3 полных дубля с разными картинками")
    per, sig = {}, {}
    for r in ads:
        g = r["Группа"]
        per[g] = per.get(g, 0) + 1
        sig.setdefault(g, []).append((tuple(cols(r, "Заголовок")), tuple(cols(r, "Текст"))))
        if len(cols(r, "Заголовок")) > 2:
            notes.append(f"{g} №{r.get('№')}: заголовков {len(cols(r, 'Заголовок'))}; на новом проекте эталон — 1–2 близких по смыслу")
    if per and len(set(per.values())) > 1:
        notes.append("в группах разное число объявлений: эталон — одинаковые группы-дубли по 3 объявления")
    if len({tuple(sorted(v)) for v in sig.values()}) > 1:
        notes.append("тексты объявлений в группах различаются: в эталоне группы — полные дубли, меняются только картинки")
    for label, want in (("Персонализация (Яндекс Нейро / альтернативные тексты)", "выкл"),):
        if not camp.get(label, "").lower().startswith(want):
            notes.append("персонализация не выключена (в эталоне выключена)")
    if not (camp.get("Корректировка: возраст до 18, %") and camp.get("Корректировка: возраст 55+, %")):
        notes.append("нет корректировок возраста до 18 и 55+ (−100%); оставьте так только если проект для этой аудитории")
    for key in ("Корректировка: возраст до 18, %", "Корректировка: возраст 55+, %"):
        v = camp.get(key, "")
        if v and v not in ("-100", "−100"):
            notes.append(f"«{key}» = {v}: частичная корректировка меняет цену конверсии, а не объём аудитории; эталон — только −100%")
    links = [r for r in t.rows("Быстрые ссылки") if r.get("Заголовок (до 30)")]
    if len(links) < 4:
        notes.append(f"быстрых ссылок {len(links)}: эталон — 4, связанные между собой по воронке")
    if not [r for r in t.rows("Уточнения") if r.get("Текст уточнения (до 25)")]:
        notes.append("нет уточнений («для кого это»)")
    promo = [r for r in t.rows("Промоакции") if r.get("Название в библиотеке")]
    if not promo:
        notes.append("нет промоакции («бесплатно», «в подарок»)")
    if not camp.get("UTM-метки", "").strip():
        notes.append("не задан UTM: эталон — единый формат всегда")
    return notes


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("table")
    ap.add_argument("--account", required=True)
    ap.add_argument("--campaign", type=int, help="ID существующей кампании; без него кампания создаётся")
    ap.add_argument("--apply", action="store_true", help="выполнить запись (без флага — только план)")
    a = ap.parse_args()

    t = Table(a.table)
    problems, manual, plan = [], [], []

    # ---------- чтение и проверка таблицы
    camp = t.campaign()
    groups = list(t.rows("Группы"))
    phrases = list(t.rows("Фразы"))
    all_ads = [r for r in t.rows("Объявления") if r.get("№")]
    ads = [r for r in all_ads if col(r, "Проверено").lower() == "да"]
    creatives = {r["Имя файла (в папке рядом)"]: r for r in t.rows("Креативы") if r.get("Имя файла (в папке рядом)")}
    skipped = len(all_ads) - len(ads)

    groups = [g for g in groups if any(v for k, v in g.items() if k != "Группа")]
    gnames = [g["Группа"] for g in groups]
    if len(set(gnames)) != len(gnames):
        problems.append("Группы: имена групп повторяются")
    per_group = Counter(r["Группа"] for r in ads)
    for g, n in per_group.items():
        if g not in gnames:
            problems.append(f"Объявления: группа «{g}» не описана на листе «Группы»")
        if n > MAX_ADS_PER_GROUP:
            problems.append(f"Объявления: в группе «{g}» {n} объявлений, максимум {MAX_ADS_PER_GROUP}")
    for i, r in enumerate(ads, 1):
        who = f"Объявление {r['Группа']} №{r.get('№')}"
        titles = cols(r, "Заголовок")
        texts = cols(r, "Текст")
        if not titles:
            problems.append(f"{who}: нет заголовка")
        if not texts:
            problems.append(f"{who}: нет текста")
        for x in titles:
            if len(x) > 56:
                problems.append(f"{who}: заголовок {len(x)} > 56 символов: «{x[:40]}…»")
        for x in texts:
            if not narrow_ok(x):
                problems.append(f"{who}: текст длиннее лимита (81 + 15 узких): «{x[:40]}…»")
        if not r.get("Ссылка", "").startswith("http"):
            problems.append(f"{who}: нет ссылки (http…)")
        d = col(r, "Отобр. ссылка")
        if len(d) > 20:
            problems.append(f"{who}: отображаемая ссылка длиннее 20 символов")
        if col(r, "Кнопка: текст"):
            manual.append(f"{who}: кнопка «{col(r, 'Кнопка: текст')}» ({col(r, 'Кнопка: действие')}) — добавить вручную в интерфейсе")
        img = col(r, "Картинка")
        if img and img not in creatives:
            problems.append(f"{who}: картинка «{img}» не описана на листе «Креативы»")
    for g in groups:
        g["_regions"] = region_ids(g.get("Регионы (через ;)", ""), problems, f"Группа {g['Группа']}")
        ex = region_ids(g.get("Исключить регионы", ""), problems, f"Группа {g['Группа']}")
        g["_regions"] += [-x for x in ex]
        if not g["_regions"]:
            problems.append(f"Группа {g['Группа']}: не заданы регионы")
    for p in phrases:
        if p["Группа"] not in gnames:
            problems.append(f"Фразы: группа «{p['Группа']}» не описана на листе «Группы»")
    if not a.campaign:
        for key in ("Название кампании", "Дата начала", "Недельный бюджет, ₽", "Потолок ставки, ₽",
                    "Цель для обучения (ID цели Метрики)", "Счётчик Метрики"):
            if not str(camp.get(key, "")).strip():
                problems.append(f"Кампания: не заполнено «{key}»")
        if camp.get("Дата начала") and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(camp["Дата начала"])[:10]):
            problems.append("Кампания: дата начала не в формате ГГГГ-ММ-ДД")
        for key in ("Цель для обучения (ID цели Метрики)", "Счётчик Метрики"):
            if str(camp.get(key, "")).strip() and not str(camp[key]).strip().isdigit():
                problems.append(f"Кампания: «{key}» — нужен числовой ID")
    links = [(f"Объявление {r['Группа']} №{r.get('№')}", r.get("Ссылка", "")) for r in ads]
    links += [(f"Быстрая ссылка «{r.get('Заголовок (до 30)', '')}»", col(r, "Адрес"))
              for r in t.rows("Быстрые ссылки")]
    for who, url in links:
        if PLACEHOLDER in url:
            problems.append(f"{who}: ссылка-заглушка {PLACEHOLDER}, впишите адрес своей страницы")

    # хеши картинок: из таблицы или файл рядом
    hashes = {}
    for name, r in creatives.items():
        h = r.get("Хеш в Директе\n(заполняет загрузчик)", "") or col(r, "Хеш в Директе")
        if h:
            hashes[name] = h
        elif not os.path.exists(os.path.join(t.dir, name)):
            used = any(col(x, "Картинка") == name for x in ads)
            if used:
                problems.append(f"Креативы: у «{name}» нет хеша и нет файла рядом с таблицей")

    print(f"Таблица: {a.table}")
    print(f"Групп {len(groups)} · фраз {len(phrases)} · объявлений с отметкой «да» {len(ads)} (без отметки пропущено {skipped})")
    if problems:
        print("\nОШИБКИ В ТАБЛИЦЕ — исправьте и запустите снова:")
        for p in problems:
            print("  ✗", p)
        sys.exit(2)
    print("Проверка таблицы: ошибок нет.")
    notes = rsya_checks(t, camp, groups, ads)
    if notes:
        print("\nЗамечания по эталону РСЯ (не блокируют, см. references/RSYA.md):")
        for n in notes:
            print("  •", n)

    # ---------- кампания
    campaign_id = a.campaign
    if not campaign_id:
        name = camp["Название кампании"]
        cmd = ["create", "--name", name, "--type", "unified",
               "--start", camp["Дата начала"], "--search-strategy", "SERVING_OFF",
               "--network-strategy", "WB_MAXIMUM_CONVERSION_RATE",
               "--network-param", f"GoalId={camp['Цель для обучения (ID цели Метрики)']}",
               "--network-param", f"BidCeiling={camp['Потолок ставки, ₽']}",
               "--weekly-budget", camp["Недельный бюджет, ₽"],
               "--counter", camp["Счётчик Метрики"]]
        utm = camp.get("UTM-метки", "").strip()
        cmd += ["--tracking", utm] if utm else ["--utm-scheme", "cabinet"]
        if a.apply:
            cmd.insert(0, "--apply")
        rc, out = run("campaign_write.py", *cmd, account=a.account)
        print("\n== Кампания ==\n" + "\n".join(out.splitlines()[-6:]))
        if not a.apply:
            print("\nБез --campaign группы и объявления можно проверить только после создания кампании. План дальше — по тем же правилам.")
            return
        import re
        m = re.search(r"campaigns-ids=(\d+)", out)
        if not m:
            print("Не удалось определить ID созданной кампании — проверьте кабинет, повторять запись нельзя.")
            sys.exit(3)
        campaign_id = int(m.group(1))

    # ---------- текущее состояние кампании
    rc, out = run("campaign_dump.py", "--campaign", str(campaign_id), "--no-cache", account=a.account)
    try:
        with open(os.path.join(SKILL, "cache", a.account, f"campaign-dump-{campaign_id}.json"), encoding="utf-8-sig") as fh:
            dump = json.load(fh)
    except Exception:
        print("Не удалось прочитать кампанию:\n" + out[-600:])
        sys.exit(3)
    by_name = {g["name"]: g["id"] for g in dump["groups"]}
    have_kw = Counter()
    for k in dump["keywords"]:
        if not k.get("autotargeting"):
            have_kw[(k["group_id"], k["keyword"])] += 1
    have_ads = Counter()
    for ad in dump["ads"]:
        if ad.get("state") == "ARCHIVED":
            continue
        p = ad["composition"]["parts"]
        key = (ad["group_id"], tuple(x["value"] for x in p["titles"]), tuple(x["value"] for x in p["texts"]))
        have_ads[key] += 1

    flag = ["--apply"] if a.apply else []
    # ---------- группы
    print("\n== Группы ==")
    for g in groups:
        if g["Группа"] in by_name:
            print(f"  = {g['Группа']}: уже есть ({by_name[g['Группа']]})")
            continue
        args = flag + ["group", "create", "--campaign", str(campaign_id), "--name", g["Группа"], "--name-scheme", "other"]
        for r in g["_regions"]:
            args += ["--region", str(r)]
        rc, out = run("ads_write.py", *args, account=a.account)
        print(f"  + {g['Группа']}: {'создана' if a.apply else 'будет создана'}")
        if a.apply:
            rc, o2 = run("adgroups.py", "--campaign", str(campaign_id), "--no-cache", account=a.account, json_out=True)
            by_name = {x["name"]: x["id"] for x in json.loads(o2[o2.index("{"):])["groups"]}
    # ---------- условие ретаргетинга групп
    wanted_targets = [(g["Группа"], g.get("Условие ретаргетинга (ID)", "")) for g in groups
                      if g.get("Условие ретаргетинга (ID)", "").isdigit()]
    if wanted_targets:
        print("\n== Ретаргетинг ==")
        have_targets = existing_targets(a.account, [by_name[n] for n, _ in wanted_targets if n in by_name])
        for gname, cond in wanted_targets:
            gid = by_name.get(gname)
            if not gid:
                print(f"  + {gname}: условие {cond} будет привязано после создания группы")
                continue
            if int(cond) in have_targets.get(gid, set()):
                print(f"  = {gname}: условие {cond} уже привязано")
                continue
            rc, o = run("ads_write.py", *flag, "group", "targets", "add", "--group", str(gid),
                        "--condition", cond, account=a.account)
            ok = (not a.apply) or "записано 1" in o
            print(f"  {'+' if ok else '✗'} {gname}: условие {cond} {'привязано' if a.apply and ok else ('будет привязано' if ok else 'ОШИБКА — смотрите журнал')}")
    # ---------- фразы
    print("\n== Фразы ==")
    new_kw = {}
    for p in phrases:
        gid = by_name.get(p["Группа"])
        if gid and have_kw[(gid, p["Ключевая фраза"])]:
            continue
        new_kw.setdefault(p["Группа"], []).append(p["Ключевая фраза"])
    for gname, ph in new_kw.items():
        gid = by_name.get(gname, 0)
        args = flag + ["keyword", "add", "--group", str(gid)]
        for x in ph:
            args += ["--phrase", x]
        if gid or not a.apply:
            run("keywords_write.py", *args, account=a.account) if a.apply else None
        print(f"  + {gname}: {len(ph)} фраз {'добавлено' if a.apply else 'будет добавлено'}")
    if not new_kw:
        print("  = все фразы уже есть")
    # ---------- объявления
    print("\n== Объявления ==")
    for r in ads:
        gid = by_name.get(r["Группа"])
        titles, texts = cols(r, "Заголовок"), cols(r, "Текст")
        key = (gid, tuple(titles), tuple(texts))
        if have_ads[key] > 0:
            have_ads[key] -= 1
            print(f"  = {r['Группа']} №{r['№']}: уже есть")
            continue
        img = col(r, "Картинка")
        h = hashes.get(img)
        if img and not h:
            args = flag + ["upload", "--file", os.path.join(t.dir, img)]
            rc, o = run("images.py", *args, account=a.account)
            import re
            m = re.search(r"AdImageHash\W+([\w-]{16,})", o) or re.search(r"\b([A-Za-z0-9_-]{22})\b", o)
            h = m.group(1) if (m and a.apply) else "<хеш после загрузки>"
            hashes[img] = h
            print(f"  ↑ картинка {img}: {'загружена' if a.apply else 'будет загружена'}")
        args = flag + ["ad", "create", "--group", str(gid or 0)]
        for x in titles:
            args += ["--title", x]
        for x in texts:
            args += ["--text", x]
        if h and not h.startswith("<"):
            args += ["--image", h]
        args += ["--href", r["Ссылка"]]
        if col(r, "Отобр. ссылка"):
            args += ["--display-url-path", col(r, "Отобр. ссылка")]
        if a.apply or gid:
            rc, o = run("ads_write.py", *args, account=a.account)
            ok = "записано 1" in o if a.apply else rc == 0
        else:
            ok = True
        print(f"  {'+' if ok else '✗'} {r['Группа']} №{r['№']}: {titles[0][:50]}… "
              f"{'создано' if a.apply and ok else ('будет создано' if ok else 'ОШИБКА — смотрите журнал')}")
    # ---------- настройки кампании и дополнения
    post_stage(a, t, camp, campaign_id)
    # ---------- итог
    print("\n== Вручную в интерфейсе ==")
    for m_ in manual:
        print("  •", m_)
    print("  • Промоакция (лист «Промоакции»): Библиотека → Промоакции, затем привязать к кампании — в API её нет")
    print("  • Быстрые ссылки и уточнения загружены на объявления. Чтобы они стояли «на всю кампанию»:")
    print("    настройки кампании → Дополнительные элементы → включить чипы, ввести те же значения, нажать «Сбросить» у «переопределены в N объявлениях»")
    print("  • Офферный ретаргетинг групп (если нужен) — в настройках группы, команды в скилле нет")
    print("  • Кнопка объявления остаётся по умолчанию («Узнать больше»)")
    print(f"\nКампания: https://direct.yandex.ru/dna/grid/groups?ulogin={a.account}&campaigns-ids={campaign_id}")
    if not a.apply:
        print("Это был план. Для записи добавьте --apply.")


if __name__ == "__main__":
    main()

