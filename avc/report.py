"""raw.jsonl から summary.md を作る。raw.jsonl だけあれば何度でも作り直せる。"""

import json
from collections import Counter, defaultdict

from .cost import call_cost

TYPE_LABEL = {"discovery": "発見系", "brand": "指名系", "attribute": "属性系"}


def _pct(a, b):
    return f"{a}/{b} ({a / b:.0%})" if b else "—"


def _norm(name):
    return " ".join(name.lower().replace("’", "'").split())


def load(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def build(records, profile, pricing, title_note="", query_order=None):
    ok = [r for r in records if r["ok"]]
    providers = sorted({r["provider"] for r in records})
    langs = sorted({r["lang"] for r in records}, key=lambda x: ["ja", "en", "vi"].index(x) if x in ("ja", "en", "vi") else 9)
    out = [f"# AI可視性レポート: {profile['brand']}", ""]
    if title_note:
        out += [title_note, ""]
    dates = sorted({r["ts"][:10] for r in records})
    models = sorted({f"{r['provider']}={r.get('served_model') or r['model']}" for r in ok})
    out += [f"- 実行日: {', '.join(dates)}",
            f"- 回答数: 成功 {len(ok)} / 全 {len(records)}",
            f"- モデル: {', '.join(models) or '—'}",
            "- 注意: APIの回答は ChatGPT などの消費者向けアプリの回答とは一致しない(README参照)", ""]

    # 1. 言語 × AI の言及率
    out += ["## 言及率(言語 × AI)", "", "セルは「言及あり / 成功した回答数」。", ""]
    out.append("| 言語 | " + " | ".join(providers) + " |")
    out.append("|---|" + "---|" * len(providers))
    for lang in langs:
        cells = []
        for p in providers:
            rs = [r for r in ok if r["lang"] == lang and r["provider"] == p]
            cells.append(_pct(sum(r["eval"]["mentioned"] for r in rs), len(rs)))
        out.append(f"| {lang} | " + " | ".join(cells) + " |")
    out.append("")

    # 2. クエリ別の詳細
    out += ["## クエリ別", "", "言及率 / 平均順位(言及されたリスト内) / 公式サイトの引用率。指名系は言及されて当然なので、正確性を見る。", ""]
    out.append("| クエリ | 種類 | AI | 言及 | 平均順位 | 公式サイト引用 |")
    out.append("|---|---|---|---|---|---|")
    groups = defaultdict(list)
    for r in ok:
        groups[(r["query_id"], r["provider"])].append(r)
    order = {q: i for i, q in enumerate(query_order or [])}
    for (qid, p), rs in sorted(groups.items(), key=lambda kv: (order.get(kv[0][0], 99), kv[0][0], kv[0][1])):
        ranks = [r["eval"]["rank"] for r in rs if r["eval"]["rank"]]
        avg = f"{sum(ranks) / len(ranks):.1f}位(n={len(ranks)})" if ranks else "—"
        out.append(f"| {qid}: {rs[0]['query'][:40]} | {TYPE_LABEL.get(rs[0]['query_type'], rs[0]['query_type'])} | {p} | "
                   f"{_pct(sum(r['eval']['mentioned'] for r in rs), len(rs))} | {avg} | "
                   f"{_pct(sum(r['eval']['official_cited'] for r in rs), len(rs))} |")
    out.append("")

    # 3. 正確性の問題
    issues = [(r, c) for r in ok for c in r["eval"]["contradictions"]]
    judged = sum(1 for r in ok if r["eval"]["judge"] is not None)
    out += ["## 正確性の問題(facts.yaml と矛盾する記述)", "",
            f"判定できた回答: {judged} / {len(ok)}(判定モデルが無い・失敗した回答は対象外)", ""]
    if issues:
        out.append("| AI | クエリ | 回 | 該当箇所 | 項目 | 理由 |")
        out.append("|---|---|---|---|---|---|")
        for r, c in issues:
            q = c["quote"].replace("|", "\\|").replace("\n", " ")
            out.append(f"| {r['provider']} | {r['query_id']} | {r['run_index']} | {q} | {c['fact_key']} | {c['explanation']} |")
    else:
        out.append("矛盾は見つからなかった。" if judged else "判定していないため不明。")
    out.append("")

    # 4. 頻出する競合
    cnt, label = Counter(), {}
    for r in ok:
        for n in set(_norm(x) for x in r["eval"]["competitors"]):
            cnt[n] += 1
        for x in r["eval"]["competitors"]:
            label.setdefault(_norm(x), x)
    out += ["## 一緒に挙がった競合(上位20)", "", "数字は「その店が挙がった回答の数」。", ""]
    if cnt:
        out.append("| 店名 | 回答数 |")
        out.append("|---|---|")
        for n, c in cnt.most_common(20):
            out.append(f"| {label[n]} | {c} |")
    else:
        out.append("なし")
    out.append("")

    # 5. 実測の使用量と実費(pricing.yaml の estimate を直すための材料)
    out += ["## 実測の使用量と実費", "", "平均は成功した回答1回あたり。pricing.yaml の estimate をこの値に近づけると見積もりが正確になる。", ""]
    out.append("| AI | 回答数 | 平均入力tok | 平均出力tok | 平均検索回数 | 実費USD(推計) |")
    out.append("|---|---|---|---|---|---|")
    grand = 0.0
    for p in providers:
        rs = [r for r in ok if r["provider"] == p]
        if not rs:
            continue
        pr = pricing["providers"][p]
        cost = sum(r["reported_cost"] if r.get("reported_cost") is not None
                   else call_cost(pr, r["input_tokens"], r["output_tokens"], r["searches"]) for r in rs)
        grand += cost
        avg = lambda k: sum((r[k] or 0) for r in rs) / len(rs)
        out.append(f"| {p} | {len(rs)} | {avg('input_tokens'):,.0f} | {avg('output_tokens'):,.0f} | {avg('searches'):.1f} | {cost:.2f} |")
    ju = [r["eval"]["judge"]["_usage"] for r in ok if r["eval"]["judge"]]
    if ju:
        jc = sum(call_cost(pricing["judge"], u["input_tokens"], u["output_tokens"]) for u in ju)
        grand += jc
        out.append(f"| 判定 | {len(ju)} | {sum(u['input_tokens'] for u in ju) / len(ju):,.0f} | "
                   f"{sum(u['output_tokens'] for u in ju) / len(ju):,.0f} | — | {jc:.2f} |")
    out += ["", f"合計 約 {grand:.2f} USD(Perplexity は応答に載った実費、他は使用量×pricing.yaml の単価)", ""]
    alt = [r for r in ok if r.get("served_model") and r["served_model"] != r["model"]
           and not r["served_model"].startswith(r["model"]) and r["served_model"] != "dry-run"]
    if alt:
        out += [f"※ {len(alt)}件は設定と別のモデルが回答した(例: {alt[0]['provider']}={alt[0]['served_model']})。"
                "その分の実費は設定モデルの単価で計算しているので、ずれがある。", ""]

    # 6. 失敗
    bad = [r for r in records if not r["ok"]]
    jerr = [r for r in ok if r["eval"]["judge_error"]]
    if bad or jerr:
        out += ["## 失敗・スキップ", ""]
        for r in bad:
            out.append(f"- {r['provider']} / {r['query_id']} / {r['run_index']}回目: {r['error']}")
        for r in jerr:
            out.append(f"- 判定失敗 {r['provider']} / {r['query_id']} / {r['run_index']}回目: {r['eval']['judge_error']}")
        out.append("")
    return "\n".join(out)
