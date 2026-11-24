"""Collect read-only Yandex Direct reports and publish a local dashboard."""

import argparse
import json
import re
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from math import isclose
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock

from campaign_goals import batched, resolve


SKILL = Path(__file__).resolve().parents[1]
DIRECT = SKILL.parent / "yandex-direct"
DEFAULT_SETTINGS = SKILL.parents[2] / "my" / "direct-monitor" / "settings.json"
sys.path[:0] = [str(DIRECT / "scripts/lib"), str(DIRECT / "scripts")]
MOSCOW = timezone(timedelta(hours=3), "MSK")
DAY = timedelta(days=1)
METRICS = ["Impressions", "Clicks", "Cost", "Bounces", "Sessions"]
SLICES = {
    "placements": ["Placement"],
    "regions": ["LocationOfPresenceId", "LocationOfPresenceName"],
    "audience": ["Gender", "Age"],
    "devices": ["Device", "MobilePlatform"],
    "ads": ["AdGroupId", "AdId"],
    "ad_formats": ["AdFormat"],
}
COMBINATION = ["Placement", "AdGroupId", "AdId", "Device", "MobilePlatform", "Gender", "Age"]


class SharedReportPace:
    def __init__(self):
        from reports import Pace

        self.pace = Pace(9, window=10)
        self.lock = Lock()

    def wait(self, cap=None):
        with self.lock:
            self.pace.wait()


def windows(today):
    yesterday = today - DAY
    yield "day", (yesterday, yesterday), (yesterday - DAY, yesterday - DAY)
    if today.weekday() == 0:
        yield "week", (today - 7 * DAY, yesterday), (today - 14 * DAY, today - 8 * DAY)
    if today.day == 1:
        last_end = yesterday
        last_start = last_end.replace(day=1)
        previous_end = last_start - DAY
        yield "month", (last_start, last_end), (previous_end.replace(day=1), previous_end)


def numeric(row, field, factor=1):
    value = row.get(field)
    return None if value in (None, "", "--") else float(value) / factor


def summarize(rows, goal_ids):
    fields = dict(impressions="Impressions", clicks="Clicks", cost="Cost",
                  bounces="Bounces", sessions="Sessions")
    values = {}
    for name, field in fields.items():
        measured = [numeric(row, field, 1_000_000 if name == "cost" else 1) for row in rows]
        values[name] = None if None in measured else sum(measured)
    values["goals"] = {}
    for goal, column in goal_ids.items():
        measured = [numeric(row, column) for row in rows
                    if row.get(column) not in (None, "", "--")
                    or any(numeric(row, field) for field in ("Impressions", "Clicks", "Cost"))]
        values["goals"][goal] = None if not measured or None in measured else sum(measured)
    return values


def divide(value, denominator):
    return value / denominator if value is not None and denominator else None


def anomaly(history, current, goal_ids):
    alerts = []
    measures = {
        "cpc": (lambda v: divide(v["cost"], v["clicks"]), lambda v: (v["clicks"] or 0) >= 30),
        **{f"cpa:{goal}": (
            lambda v, g=goal: divide(v["cost"], v["goals"].get(g)),
            lambda v, g=goal: (v["goals"].get(g) or 0) >= 5,
        ) for goal in goal_ids},
    }
    for name, (metric, sufficient) in measures.items():
        if not sufficient(current):
            continue
        baseline = [metric(v) for v in history if sufficient(v) and metric(v) is not None]
        if len(baseline) < 5:
            continue
        median = statistics.median(baseline)
        actual = metric(current)
        if not median or actual is None:
            continue
        change = (actual - median) / median
        deviation = statistics.median(abs(value - median) for value in baseline)
        if abs(change) >= 0.3 and abs(actual - median) >= 3 * deviation:
            alerts.append(dict(metric=name, value=actual, baseline=median, change=change, days=len(baseline)))
    for goal in goal_ids:
        if current["goals"].get(goal) != 0 or (current["clicks"] or 0) < 30:
            continue
        past = [divide(day["cost"], day["goals"].get(goal)) for day in history
                if (day["goals"].get(goal) or 0) >= 5 and day["cost"] is not None]
        if len(past) >= 5 and current["cost"] is not None:
            baseline = statistics.median(past)
            if baseline > 0 and current["cost"] >= 5 * baseline:
                alerts.append(dict(metric=f"zero:{goal}", value=current["cost"], baseline=baseline, days=len(past)))
    return alerts


class DirectReports:
    def __init__(self, settings, state_dir, *, network_only=False, pace=None):
        from config import DirectFailure
        from direct import Client, Journal
        import reports

        self.reports = reports
        self.reference = reports.Reference.load()
        self.recoverable_errors = (DirectFailure, OSError, ValueError, RuntimeError)
        self.account = settings["account"]
        self.network_only = network_only
        self.client = Client.from_env(account=self.account, journal=Journal(state_dir / "api-log"))
        self.pace = pace or SharedReportPace()
        self.settings = settings
        self.fixed_goals = settings.get("report_goals", {})
        self.goal_labels = dict(self.fixed_goals)
        self.goal_fields = self.conversion_columns()
        self.campaign_goals = {}
        self.campaign_goal_issues = {}
        self.warnings = {}

    def load_goals(self, rows):
        ids = {str(row["CampaignId"]) for row in rows if str(row.get("CampaignId", "")).isdigit()}
        if self.fixed_goals:
            self.campaign_goals = {campaign: list(self.fixed_goals) for campaign in ids}
            return
        self.campaign_goals, self.goal_labels, self.campaign_goal_issues = resolve(
            self.client, ids, self.warnings, self.recoverable_errors, network_only=self.network_only)
        self.goal_fields = self.conversion_columns()

    def conversion_columns(self):
        return {goal: f"Conversions_{goal}_{self.settings['attribution']}" for goal in self.goal_labels}

    def scope(self, end, now_rows):
        catalog = {}
        try:
            for row in self.client.get_all("campaigns", {
                "SelectionCriteria": {"States": ["ON", "OFF", "SUSPENDED", "ENDED", "ARCHIVED", "CONVERTED"]},
                "FieldNames": ["Id", "Name", "State"],
            }, account=self.account):
                catalog[str(row["Id"])] = dict(name=row["Name"], state=row["State"], channels=[])
        except self.recoverable_errors as exc:
            self.warnings["campaign_catalog"] = str(exc)[:500]
        try:
            history = self.request(end - 365 * DAY, end, ["CampaignId", "CampaignName", "AdNetworkType"],
                                   ["Impressions"], [], networks_only=False)
        except self.recoverable_errors as exc:
            self.warnings["campaign_history"] = str(exc)[:500]
            history = []
        for row in history + now_rows:
            entry = catalog.setdefault(row["CampaignId"], dict(name=row["CampaignName"], state=None, channels=[]))
            channel = row.get("AdNetworkType")
            if channel and channel not in entry["channels"]:
                entry["channels"].append(channel)
        return catalog

    def ad_elements(self, periods):
        import responsive

        ids = {}
        for period in periods.values():
            per_campaign = {}
            for row in period["slices"].get("ads", []):
                campaign, _, ad = row["key"]
                if ad.isdigit() and per_campaign.get(campaign, 0) < 8:
                    ids[int(ad)] = None
                    per_campaign[campaign] = per_campaign.get(campaign, 0) + 1
        elements = {}
        for batch in batched(list(ids)[:300], 100):
            try:
                rows = self.client.get_all("ads", {
                    "SelectionCriteria": {"Ids": batch}, **responsive.read_params(fields=["Id", "Type"]),
                }, account=self.account)
            except self.recoverable_errors as exc:
                self.warnings["ad_elements"] = str(exc)[:500]
                continue
            for row in rows:
                if not (row.get("ResponsiveAd") or row.get("TextAd")):
                    continue
                kit = responsive.Kit.of(row, carry=False)
                titles = list(kit.titles)
                if extra := (row.get("TextAd") or {}).get("Title2"):
                    if not row.get("ResponsiveAd") and extra not in titles:
                        titles.append(extra)
                elements[str(row["Id"])] = dict(titles=titles, texts=kit.texts,
                                                 images=len(kit.images), videos=len(kit.videos))
        return elements

    def collect(self, start, end, fields, placements=()):
        if self.fixed_goals:
            return self.request(start, end, fields, METRICS + ["Conversions"],
                                self.settings["campaign_ids"], placements, self.fixed_goals)
        rows = self.request(start, end, fields, METRICS, self.settings["campaign_ids"], placements)
        self.enrich(rows, start, end, fields, placements)
        return rows

    def enrich(self, rows, start, end, fields, placements=()):
        if self.fixed_goals or not self.goal_fields or not rows:
            return
        indexed = {tuple(row.get(field) for field in fields): row for row in rows}
        campaigns = {row.get("CampaignId") for row in rows} & self.campaign_goals.keys()
        goals = sorted({goal for campaign in campaigns for goal in self.campaign_goals[campaign]}, key=int)
        for group in batched(goals, 10):
            selected = sorted(campaign for campaign in campaigns if set(group).intersection(self.campaign_goals[campaign]))
            try:
                measured = self.request(start, end, fields, ["Conversions"], selected, placements, group)
            except self.recoverable_errors as exc:
                self.warnings[f"conversions:{end}:{','.join(fields)}:{','.join(group)}"] = str(exc)[:500]
                continue
            for result in measured:
                base = indexed.get(tuple(result.get(field) for field in fields))
                if base is None:
                    continue
                permitted = self.campaign_goals.get(result.get("CampaignId"), [])
                for goal in group:
                    column = self.goal_fields[goal]
                    if goal in permitted and column in result:
                        base[column] = result[column]

    def request(self, start, end, fields, metrics, selected, placements=(), goals=(), networks_only=None):
        if networks_only is None:
            networks_only = self.network_only
        filters = ([{"Field": "AdNetworkType", "Operator": "EQUALS", "Values": ["AD_NETWORK"]}]
                   if networks_only else [])
        if selected:
            filters.append({"Field": "CampaignId", "Operator": "IN", "Values": [str(i) for i in selected]})
        if placements:
            filters.append({"Field": "Placement", "Operator": "IN", "Values": list(placements)})
        requested = self.reports.request_params(
            self.reference, report_type="CUSTOM_REPORT", field_names=fields + metrics,
            chosen_period={"kind": "CUSTOM_DATE", "from": str(start), "to": str(end)},
            filters=filters, goals=goals, attribution=[self.settings["attribution"]],
            include_vat="YES",
        )
        problems = self.reports.check(self.reference, requested)
        if problems:
            raise ValueError("\n".join(problems))
        result = self.reports.collect(
            self.client, self.reference, requested, account=self.account, prefix="rsya-monitor",
            run=self.reports.new_run(), processing_mode="auto", use_operator_units=None,
            pace=self.pace, wait=60,
        )
        if result.truncated:
            raise ValueError(f"API вернул неполный отчёт за {start} — {end}. Сузьте период или список кампаний.")
        return result.rows


def by_key(rows, fields, goal_fields):
    groups = {}
    for row in rows:
        key = tuple(row.get(field) or "—" for field in fields)
        groups.setdefault(key, []).append(row)
    rows = [dict(key=key, **summarize(group, goal_fields)) for key, group in groups.items()]
    return sorted(rows, key=lambda row: row["cost"] or 0, reverse=True)


def campaign_summary(api, start, end):
    fields = ["CampaignId", "CampaignName"]
    return by_key(api.collect(start, end, fields), fields, api.goal_fields)


def period_total(rows, campaigns, goals, fixed_goals):
    total = summarize(rows, {goal: goals[goal] for goal in fixed_goals})
    active = [row for row in campaigns if row["impressions"] or row["clicks"] or row["cost"]
              or any(row["goals"].values())]
    for goal in goals:
        if goal in fixed_goals:
            continue
        measured = [row["goals"].get(goal) for row in active]
        total["goals"][goal] = sum(measured) if measured and None not in measured else None
    return total


def daily_series(history, totals, goals, fixed_goals, start, end):
    by_date = {row["key"][0]: row for row in by_key(history, ["Date"], goals)}
    exact = {row["Date"]: summarize([row], {goal: goals[goal] for goal in fixed_goals}) for row in totals}
    by_campaign = {}
    for row in by_key(history, ["CampaignId", "Date"], goals):
        campaign, day = row["key"]
        by_campaign.setdefault(campaign, {})[day] = row
    result = {}
    for campaign, items in {"": by_date, **by_campaign}.items():
        series = []
        for offset in range((end - start).days + 1):
            day = str(start + offset * DAY)
            item = items.get(day) or summarize([], goals)
            if not campaign and day in exact:
                item = {**item, **exact[day], "goals": {**item["goals"], **exact[day]["goals"]}}
            series.append({**item, "key": [day]})
        result[campaign] = series
    return result


def reconcile_zero_conversions(parts, total):
    """Resolve suppressed zeros only when clicks and known goal counts reconcile."""
    if total.get("clicks") is None or any(row.get("clicks") is None for row in parts):
        return
    if not isclose(sum(row["clicks"] for row in parts), total["clicks"], rel_tol=0, abs_tol=1e-7):
        return
    for goal, expected in total["goals"].items():
        if expected is None:
            continue
        reported = [row["goals"].get(goal) for row in parts if row["goals"].get(goal) is not None]
        if any(value < 0 for value in reported):
            continue
        if isclose(sum(reported), expected, rel_tol=0, abs_tol=1e-7):
            for row in parts:
                if row["clicks"] > 0 and row["goals"].get(goal) is None:
                    row["goals"][goal] = 0


def reconcile_trend(trends, account_total, campaigns, start, end):
    totals = {"": account_total, **{row["key"][0]: row for row in campaigns}}
    for campaign, rows in trends.items():
        if campaign in totals:
            reconcile_zero_conversions([row for row in rows if start <= row["key"][0] <= end], totals[campaign])


def reconcile_segments(slices, campaigns):
    for name, rows in slices.items():
        if name == "combos":
            continue  # This subdivision includes only selected placements.
        groups = {}
        for row in rows:
            groups.setdefault(row["key"][0], []).append(row)
        for campaign in campaigns:
            reconcile_zero_conversions(groups.get(campaign["key"][0], []), campaign)


def period_report(api, pair, previous, history, daily_totals, current_day, trend_start):
    start, end = pair
    earlier, earlier_end = previous
    here = [row for row in history if str(start) <= row["Date"] <= str(end)]
    before = [row for row in history if str(earlier) <= row["Date"] <= str(earlier_end)]
    by_campaign = campaign_summary(api, start, end) if here else []
    old_campaigns = campaign_summary(api, earlier, earlier_end) if before else []
    total = api.collect(start, end, []) if here else []
    old_total = api.collect(earlier, earlier_end, []) if before else []
    totals = period_total(total, by_campaign, api.goal_fields, api.fixed_goals)
    previous_totals = period_total(old_total, old_campaigns, api.goal_fields, api.fixed_goals)
    had_activity = bool(here)
    trend_rows = [row for row in history if str(trend_start) <= row["Date"] <= str(end)]
    trend = daily_series(trend_rows, daily_totals, api.goal_fields, api.fixed_goals, trend_start, end)
    previous_trend = daily_series(before, daily_totals, api.goal_fields, api.fixed_goals, earlier, earlier_end)
    if trend_start < start and api.goal_fields:
        try:
            trend_campaigns = campaign_summary(api, trend_start, end)
            trend_total = period_total(api.collect(trend_start, end, []), trend_campaigns, api.goal_fields, api.fixed_goals)
            reconcile_trend(trend, trend_total, trend_campaigns, str(trend_start), str(end))
        except api.recoverable_errors as exc:
            api.warnings["trend_check"] = str(exc)[:500]
    reconcile_trend(trend, totals, by_campaign, str(start), str(end))
    reconcile_trend(previous_trend, previous_totals, old_campaigns, str(earlier), str(earlier_end))
    slices, errors = {}, {}
    for name, dimensions in SLICES.items():
        if not had_activity:
            continue
        fields = ["CampaignId", *dimensions]
        try:
            data = api.collect(start, end, fields)
            slices[name] = by_key(data, fields, api.goal_fields)
        except api.recoverable_errors as exc:
            errors[name] = str(exc)[:1000]
    placements = list(dict.fromkeys(
        row["key"][1] for row in slices.get("placements", []) if row["key"][1] != "—"
    ))[:10]
    if placements:
        fields = ["CampaignId", *COMBINATION]
        try:
            slices["combos"] = by_key(api.collect(start, end, fields, placements), fields, api.goal_fields)
        except api.recoverable_errors as exc:
            errors["combos"] = str(exc)[:1000]
    reconcile_segments(slices, by_campaign)
    return dict(
        start=str(start), end=str(end), previous=list(map(str, previous)),
        total=totals, baseline=previous_totals,
        daily=trend[""], trend=trend, previous_trend=previous_trend, campaigns=by_campaign,
        previous_campaigns=old_campaigns, slices=slices, errors=errors,
        activity=had_activity, preliminary=end >= current_day - 2 * DAY, completed_on=str(current_day),
        goal_labels=api.goal_labels, campaign_goals=api.campaign_goals, goal_issues=api.campaign_goal_issues,
    )


def write_text(path, value):
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as file:
        file.write(value)
        temporary = Path(file.name)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load_settings(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise ValueError(f"Создайте {path} по примеру из assets/settings.example.json и впишите логин Директа.") from None
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), list) or not data["accounts"]:
        raise ValueError("В настройках нужен непустой список accounts с кабинетами Директа.")
    attribution = data.get("attribution", "AUTO")
    if attribution not in ("AUTO", "LC", "LSCCD", "FCCD"):
        raise ValueError("Модель attribution должна быть AUTO, LC, LSCCD или FCCD.")

    result = []
    logins = set()
    for entry in data["accounts"]:
        login = entry.get("account") if isinstance(entry, dict) else None
        if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", login) or login.endswith("."):
            raise ValueError("В каждом кабинете укажите логин Директа в поле account.")
        if login.casefold() in logins:
            raise ValueError("В accounts повторяется логин кабинета.")
        logins.add(login.casefold())
        name = entry.get("account_name", login)
        currency = entry.get("currency", "RUB")
        if not isinstance(name, str) or not name.strip() or not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError(f"Проверьте название и трёхбуквенный код валюты кабинета {login}.")
        ids = entry.get("campaign_ids", [])
        if not isinstance(ids, list) or any(type(value) is not int or value <= 0 for value in ids):
            raise ValueError(f"campaign_ids для {login} должен содержать целые положительные ID.")
        goals = entry.get("report_goals", {})
        if not isinstance(goals, dict) or len(goals) > 10 or any(
            not re.fullmatch(r"[1-9][0-9]*", goal) or not isinstance(label, str) or not label.strip()
            for goal, label in goals.items()
        ):
            raise ValueError(f"report_goals для {login} задаётся как до десяти пар «ID цели → название».")
        cheap, expensive = entry.get("cheap_cpc", 10), entry.get("expensive_cpc", 100)
        if any(type(value) not in (int, float) for value in (cheap, expensive)) or not 0 <= cheap < expensive < float("inf"):
            raise ValueError(f"Порог дешёвого CPC должен быть неотрицательным и ниже дорогого для {login}.")
        result.append(dict(account=login, account_name=name, currency=currency,
                           campaign_ids=list(dict.fromkeys(ids)), report_goals=goals,
                           cheap_cpc=cheap, expensive_cpc=expensive))
    return {"accounts": result, "attribution": attribution}


def capture_balances(configs, state_dir):
    from accounts import balances
    from config import DirectFailure
    from direct import Client, Journal

    directory = state_dir / "balances"
    directory.mkdir(parents=True, exist_ok=True)
    output = {}
    for settings in configs:
        login = settings["account"]
        try:
            client = Client.from_env(account=login, journal=Journal(state_dir / "accounts" / login / "api-log"))
            amounts, issues = balances(client, [login])
            now = datetime.now(MOSCOW)
            file = directory / f"{now.date()}.json"
            sampled = json.loads(file.read_text(encoding="utf-8")) if file.exists() else {}
            account = sampled.setdefault(login, {})
            if login not in amounts:
                reason = str(issues[0]) if issues else "API не вернул остаток общего счёта"
                account["unavailable"] = reason[:300]
                write_text(file, json.dumps(sampled, ensure_ascii=False))
                output[login] = reason
                continue
            measurement = dict(amount=amounts[login] / 1_000_000, at=now.isoformat(timespec="seconds"))
            account.pop("unavailable", None)
            account["latest"] = measurement
            if now.hour == 0 and now.minute < 15:
                account.setdefault("opening", measurement)
            write_text(file, json.dumps(sampled, ensure_ascii=False))
            output[login] = measurement["at"]
        except (DirectFailure, OSError, ValueError) as exc:
            output[login] = str(exc)[:500]
    return output


def save_account(data, state_dir):
    history = state_dir / "history"
    previous = json.loads((state_dir / "latest.json").read_text(encoding="utf-8")) if (state_dir / "latest.json").exists() else {}
    if previous.get("settings") == data["settings"] and previous.get("scope") == data["scope"]:
        for kind, report in previous.get("periods", {}).items():
            data["periods"].setdefault(kind, report)
        if previous.get("network") and data.get("network"):
            for kind, report in previous["network"]["periods"].items():
                data["network"]["periods"].setdefault(kind, report)
    history.mkdir(parents=True, exist_ok=True)
    write_text(history / f"{data['as_of']}.json", json.dumps(data, ensure_ascii=False))
    write_text(state_dir / "latest.json", json.dumps(data, ensure_ascii=False))


def render(state_dir):
    latest = json.loads((state_dir / "latest.json").read_text(encoding="utf-8"))
    archives = {}
    for account, snapshot in latest["accounts"].items():
        history = state_dir / "accounts" / account / "history"
        files = sorted(history.glob("*.json"), reverse=True)[:35]
        archive = {file.stem: compact(json.loads(file.read_text(encoding="utf-8"))) for file in files}
        archive.pop(snapshot["as_of"], None)
        archives[account] = archive
    notes_file = state_dir / "notes.json"
    notes = json.loads(notes_file.read_text(encoding="utf-8")) if notes_file.exists() else {}
    balance_files = sorted((state_dir / "balances").glob("*.json"), reverse=True)[:35]
    payload = dict(accounts=latest["accounts"],
                   archives=archives, failures=latest.get("failures", {}), notes=notes,
                   balances={file.stem: json.loads(file.read_text(encoding="utf-8")) for file in balance_files})
    safe_json = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    template = (SKILL / "assets" / "panel-template.html").read_text(encoding="utf-8")
    dashboard = state_dir / "dashboard.html"
    write_text(dashboard, template.replace("__DASHBOARD_DATA__", safe_json))
    return dashboard


def compact(snapshot):
    if snapshot.get("network"):
        compact(snapshot["network"])
    for period in snapshot["periods"].values():
        for name, rows in period["slices"].items():
            groups = {}
            for row in rows:
                groups.setdefault(row["key"][0], []).append(row)
            kept = []
            for campaign_rows in groups.values():
                selected = campaign_rows[:75]
                for goal in period.get("goal_labels", {}):
                    ranked = sorted(
                        (row for row in campaign_rows if (row["goals"].get(goal) or 0) >= 3),
                        key=lambda row: row["cost"] / row["goals"][goal] if row["cost"] is not None else float("inf"),
                    )
                    selected.extend(ranked[:8] + ranked[-8:])
                kept.extend({tuple(row["key"]): row for row in selected}.values())
            period["slices"][name] = sorted(kept, key=lambda row: row["cost"] or 0, reverse=True)
    return snapshot


def collect_view(settings, state_dir, today, backfill, pace, *, network_only=False, catalog=None):
    api = DirectReports(settings, state_dir, network_only=network_only, pace=pace)
    selected = {kind: (pair, previous) for kind, pair, previous in windows(today)}
    if backfill:
        monday = today - today.weekday() * DAY
        selected.update({kind: (pair, before) for kind, pair, before in windows(monday) if kind == "week"})
        selected.update({kind: (pair, before) for kind, pair, before in windows(today.replace(day=1)) if kind == "month"})
    history_start = min(today - 29 * DAY, *(previous[0] for pair, previous in selected.values()))
    daily_fields = ["Date", "CampaignId", "CampaignName"]
    raw = api.collect(history_start, today - DAY, daily_fields)
    api.load_goals(raw)
    api.enrich(raw, history_start, today - DAY, daily_fields)
    if catalog is None:
        catalog = api.scope(today - DAY, raw)
    totals = api.collect(history_start, today - DAY, ["Date"])
    periods = {kind: period_report(api, pair, before, raw, totals, today,
                                   today - 14 * DAY if kind == "day" else pair[0])
               for kind, (pair, before) in selected.items()}
    by_date = {row["key"][0]: row for row in by_key(raw, ["Date"], api.goal_fields)}
    for row in totals:
        if row["Date"] in by_date:
            for goal in api.fixed_goals:
                by_date[row["Date"]]["goals"][goal] = numeric(row, api.goal_fields[goal])
    baseline = [value for day, value in by_date.items() if str(today - 29 * DAY) <= day < str(today - DAY)]
    alerts = anomaly(baseline, periods["day"]["total"], api.goal_labels)
    for campaign in periods["day"]["campaigns"]:
        campaign_id = campaign["key"][0]
        detailed = [row for row in raw if row["CampaignId"] == campaign_id and str(today - 29 * DAY) <= row["Date"] < str(today - DAY)]
        observations = by_key(detailed, ["Date"], api.goal_fields)
        alerts.extend(dict(alert, campaign_id=campaign_id) for alert in anomaly(
            observations, campaign, api.campaign_goals.get(campaign_id, [])))
    data = dict(as_of=str(today), generated=datetime.now(MOSCOW).isoformat(timespec="seconds"),
                settings=settings, periods=periods, alerts=alerts, warnings=api.warnings,
                catalog=catalog, elements=api.ad_elements(periods), scope="network" if network_only else "all")
    return data, len(api.campaign_goals), len(api.campaign_goal_issues)


def collect_account(settings, state_dir, today, backfill, pace):
    data, configured, unavailable = collect_view(settings, state_dir, today, backfill, pace)
    try:
        data["network"], _, _ = collect_view(settings, state_dir, today, backfill, pace, network_only=True,
                                              catalog=data["catalog"])
    except Exception as exc:
        data["warnings"]["network"] = str(exc)[:500]
    fresh = list(data["periods"])
    save_account(data, state_dir)
    return data, dict(periods=fresh, alerts=data["alerts"], goal_campaigns=configured,
                      unavailable_settings=unavailable, warnings=data["warnings"],
                      partial={kind: v["errors"] for kind, v in data["periods"].items() if v["errors"]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", type=date.fromisoformat, default=datetime.now(MOSCOW).date())
    parser.add_argument("--backfill", action="store_true", help="also refresh the last completed week and month")
    parser.add_argument("--render", action="store_true", help="render stored data without calling Yandex")
    parser.add_argument("--balance-only", action="store_true", help="capture shared-account balances without generating reports")
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS, help="JSON с кабинетами и целями")
    parser.add_argument("--state-dir", type=Path, help="папка панели, истории и журналов, по умолчанию рядом с настройками")
    args = parser.parse_args()
    args.state_dir = (args.state_dir or args.settings.parent).resolve()
    if args.render and not (args.state_dir / "latest.json").is_file():
        parser.error("Сначала соберите данные обычным запуском или с --backfill.")
    try:
        settings = None if args.render else load_settings(args.settings)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    args.state_dir.mkdir(parents=True, exist_ok=True)
    if args.render:
        print(json.dumps({"dashboard": str(render(args.state_dir))}, ensure_ascii=False))
        return
    balances = capture_balances(settings["accounts"], args.state_dir)
    if args.balance_only:
        print(json.dumps(dict(balances=balances), ensure_ascii=False))
        return
    accounts, reports, failures = {}, {}, {}
    pace = SharedReportPace()
    with ThreadPoolExecutor(max_workers=len(settings["accounts"])) as pool:
        pending = {}
        for config in settings["accounts"]:
            account = config["account"]
            directory = args.state_dir / "accounts" / account
            pending[account] = (directory, pool.submit(collect_account,
                dict(config, attribution=settings["attribution"]), directory, args.as_of, args.backfill, pace))
        for account, (directory, task) in pending.items():
            try:
                accounts[account], reports[account] = task.result()
            except Exception as exc:
                failures[account] = str(exc)[:500]
                previous = directory / "latest.json"
                if previous.exists():
                    accounts[account] = json.loads(previous.read_text(encoding="utf-8"))
    if not reports:
        raise SystemExit(json.dumps({"failed": failures}, ensure_ascii=False))
    write_text(args.state_dir / "latest.json", json.dumps(dict(accounts=accounts, failures=failures), ensure_ascii=False))
    dashboard = render(args.state_dir)
    print(json.dumps(dict(date=str(args.as_of), accounts=reports, failed=failures, dashboard=str(dashboard)), ensure_ascii=False))


if __name__ == "__main__":
    main()
