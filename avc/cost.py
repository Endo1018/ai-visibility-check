"""実行前の見積もりと、実行後の実費の計算。単価は pricing.yaml が正。"""


def call_cost(p, input_tokens, output_tokens, searches=0):
    return ((input_tokens or 0) * p["input_per_mtok"] + (output_tokens or 0) * p["output_per_mtok"]) / 1e6 \
        + (searches or 0) * p.get("search_per_call", 0)


def estimate(plan, pricing, use_judge):
    """plan: [(provider, query, run_index), ...] → 行ごとの見積もりと合計"""
    rows, total = [], 0.0
    counts = {}
    for name, _, _ in plan:
        counts[name] = counts.get(name, 0) + 1
    for name, n in counts.items():
        p = pricing["providers"][name]
        e = p["estimate"]
        cost = n * call_cost(p, e["input_tokens"], e["output_tokens"], e["searches"])
        rows.append({"name": f"{name} ({p['model']})", "calls": n,
                     "input_tokens": n * e["input_tokens"], "output_tokens": n * e["output_tokens"],
                     "searches": n * e["searches"], "cost": cost})
        total += cost
    if use_judge and plan:
        j = pricing["judge"]
        e = j["estimate"]
        n = len(plan)
        cost = n * call_cost(j, e["input_tokens"], e["output_tokens"])
        rows.append({"name": f"判定 ({j['model']})", "calls": n,
                     "input_tokens": n * e["input_tokens"], "output_tokens": n * e["output_tokens"],
                     "searches": 0, "cost": cost})
        total += cost
    return rows, total


def format_estimate(rows, total):
    lines = [f"{'対象':<34}{'回数':>6}{'入力tok':>11}{'出力tok':>10}{'検索':>6}{'推定USD':>10}"]
    for r in rows:
        lines.append(f"{r['name']:<34}{r['calls']:>6}{r['input_tokens']:>11,}{r['output_tokens']:>10,}"
                     f"{r['searches']:>6}{r['cost']:>10.2f}")
    lines.append(f"{'合計':<34}{'':>33}{total:>10.2f}")
    lines.append("※トークン数は pricing.yaml の estimate による推定。実測ではない。Geminiの検索無料枠(月5,000回)は差し引いていない")
    return "\n".join(lines)
