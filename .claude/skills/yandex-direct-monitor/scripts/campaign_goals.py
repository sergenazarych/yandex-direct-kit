"""Resolve configured goals for the network side of Direct campaigns."""

def batched(values, size):
    for index in range(0, len(values), size):
        yield values[index:index + size]


READ_FIELDS = {"BiddingStrategy", "PriorityGoals", "PackageBiddingStrategy"}
PACKAGE_FIELDS = (
    "MaximumConversionRate", "AverageCpa", "PayForConversion",
    "AverageCrr", "PayForConversionCrr",
)


def configured_goals(body, strategy=None):
    if strategy is None:
        bidding = body.get("BiddingStrategy") or {}
        strategy = bidding.get("Network") or {}
        if strategy.get("BiddingStrategyType") == "NETWORK_DEFAULT":
            strategy = bidding.get("Search") or {}
    if strategy.get("BiddingStrategyType") == "SERVING_OFF":
        return []
    explicit = [item.get("GoalId") for item in strategy.values() if isinstance(item, dict)]
    explicit = [str(goal) for goal in explicit if goal and str(goal) != "13"]
    if explicit:
        return list(dict.fromkeys(explicit))
    priority = (body.get("PriorityGoals") or {}).get("Items") or []
    return list(dict.fromkeys(str(item["GoalId"]) for item in priority
                              if item.get("GoalId") and str(item["GoalId"]) != "13"))


def resolve(client, campaign_ids, warnings, recoverable_errors, *, network_only=True):
    from campaigns import TYPE_FIELDS, type_body

    typed = {key: [value for value in values if value in READ_FIELDS]
             for key, values in TYPE_FIELDS.items()}
    records = {}
    for ids in batched(sorted(campaign_ids, key=int), 1000):
        try:
            result = client.get_all("campaigns", {
                "SelectionCriteria": {"Ids": list(map(int, ids))},
                "FieldNames": ["Id", "Type"], **typed,
            })
            records.update({str(row["Id"]): type_body(row) for row in result})
        except recoverable_errors as exc:
            warnings["campaign_settings"] = str(exc)[:500]
    packages = {}
    strategy_ids = {info["StrategyId"] for body in records.values()
                    if (info := body.get("PackageBiddingStrategy")) and info.get("StrategyId")}
    for ids in batched(sorted(strategy_ids), 1000):
        try:
            result = client.get_all("strategies", {
                "SelectionCriteria": {"Ids": list(ids)}, "FieldNames": ["Id", "PriorityGoals"],
                **{f"Strategy{field}FieldNames": ["GoalId"] for field in PACKAGE_FIELDS},
            })
            packages.update({row["Id"]: row for row in result})
        except recoverable_errors as exc:
            warnings["package_settings"] = str(exc)[:500]

    assigned, issues = {}, {}
    for campaign_id in campaign_ids:
        body = records.get(campaign_id)
        if body is None:
            issues[campaign_id] = "settings_unavailable"
            continue
        package = (body.get("PackageBiddingStrategy") or {}).get("StrategyId")
        if package and package not in packages:
            issues[campaign_id] = "package_unavailable"
            continue
        goals = configured_goals(packages[package], packages[package]) if package else configured_goals(body)
        if not package and not network_only and (search := (body.get("BiddingStrategy") or {}).get("Search")):
            goals = list(dict.fromkeys([*goals, *configured_goals(body, search)]))
        if goals:
            assigned[campaign_id] = goals
        else:
            issues[campaign_id] = "no_configured_goal"

    names = {"12": "Вовлечённые сессии"}
    for ids in batched(sorted(assigned, key=int), 50):
        try:
            details = client.stat_goals(campaign_ids=ids)
        except recoverable_errors as exc:
            warnings["goal_names"] = str(exc)[:500]
            continue
        for item in details:
            if item.get("GoalID") and item.get("Name"):
                names[str(item["GoalID"])] = item["Name"]
    labels = {goal: names.get(goal, f"Цель {goal}")
              for goal in dict.fromkeys(goal for goals in assigned.values() for goal in goals)}
    return assigned, labels, issues
