"""使い方は README.md。

  python -m avc --target example --dry-run     APIを呼ばずに計画・見積もり・集計の動作を確認
  python -m avc --target example --limit 4     最初の4回答だけ本番実行
  python -m avc --target example                全件(既定: 9クエリ × キーのあるAI × 3回)
  python -m avc --report results/example/2026-11-02_0900/raw.jsonl   集計だけ作り直す
"""

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

import yaml

from . import cost, evaluate, providers, report

ROOT = Path(__file__).resolve().parent.parent
DRY_NOTE = "**DRY-RUN: 回答はすべて架空。集計の動作確認用で、数字に意味はない。**"


def new_run_dir(parent, stamp, suffix=""):
    """実行ごとの結果フォルダを新しく作って返す。既存のフォルダには絶対に書かない。

    名前は「stamp + suffix」。同じ分に2回流して同じ名前が既にあれば「stamp-2 + suffix」「stamp-3 + suffix」…にする。
    suffix（例: -dryrun）は最後に付けるので、.gitignore の results/*/*-dryrun/ はそのまま効く。
    """
    parent.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        d = parent / (f"{stamp}{'' if n == 1 else f'-{n}'}{suffix}")
        try:
            d.mkdir()
            return d
        except FileExistsError:
            n += 1


def load_env(path):
    """.env を読む(既に設定済みの環境変数は上書きしない)。値は表示しない。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        if v:
            os.environ.setdefault(k.strip(), v)


def load_yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_target(target):
    d = ROOT / "targets" / target
    if not d.is_dir():
        sys.exit(f"targets/{target}/ がありません")
    return load_yaml(d / "profile.yaml"), load_yaml(d / "facts.yaml"), load_yaml(d / "queries.yaml")["queries"]


def todo_lines(target):
    """facts.yaml の todo 節と、行内の「# TODO:」コメント(YAMLとして読むと消える)を集める。"""
    path = ROOT / "targets" / target / "facts.yaml"
    lines = list(load_yaml(path).get("todo") or [])
    for l in path.read_text(encoding="utf-8").splitlines():
        if "# TODO" in l:
            lines.append(l.split("# TODO", 1)[1].lstrip(": ").strip())
    return lines


def stub_answer(provider, q, run_index, profile):
    """--dry-run 用の架空の回答。集計の経路を通すためだけのもので、実データではない。"""
    hit = (run_index + len(q["id"]) + len(provider)) % 2 == 0
    lines = ["[DRY-RUN の架空の回答]", "1. **Sample Spa A**: 架空の競合", "2. **Sample Spa B**: 架空の競合"]
    if hit:
        lines.append(f"3. **{profile['brand']}**: 架空の記述。年中無休で営業。")
    return {"text": "\n".join(lines),
            "cited_urls": [f"https://{profile['official_domains'][0]}/"] if hit and profile.get("official_domains") else ["https://example.com/"],
            "source_urls": [], "searches": 0, "input_tokens": 0, "output_tokens": 0,
            "reported_cost": None, "served_model": "dry-run", "raw": None, "latency_s": 0}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m avc", description="AI回答での店舗の言及・正確性を定点観測する")
    ap.add_argument("--target", default="example", help="targets/ 以下のフォルダ名")
    ap.add_argument("--runs", type=int, default=3, help="クエリ×AIごとの実行回数(既定3)")
    ap.add_argument("--providers", default="openai,anthropic,gemini,perplexity", help="カンマ区切りで絞る")
    ap.add_argument("--lang", default=None, help="ja,en,vi などで絞る")
    ap.add_argument("--limit", type=int, default=None, help="回答の呼び出し回数の上限(費用の上限に使う)")
    ap.add_argument("--dry-run", action="store_true", help="APIを呼ばない。見積もりと集計の動作確認だけ")
    ap.add_argument("--no-judge", action="store_true", help="判定モデル(矛盾チェック・競合抽出)を使わない")
    ap.add_argument("--yes", action="store_true", help="見積もり後の確認を省く(定期実行用)")
    ap.add_argument("--report", metavar="RAW_JSONL", help="既存の raw.jsonl から summary.md だけ作り直す")
    ap.add_argument("--rejudge", metavar="RAW_JSONL",
                    help="保存済みの回答を、今の facts / profile / 判定モデルで評価し直す(AIには聞き直さない)")
    a = ap.parse_args(argv)

    load_env(ROOT / ".env")
    pricing = load_yaml(ROOT / "pricing.yaml")
    judge_cfg = {"provider": pricing["judge"]["provider"], "model": pricing["judge"]["model"]}

    if a.rejudge:
        raw = Path(a.rejudge)
        records = report.load(raw)
        profile, facts, queries = load_target(records[0]["target"])
        if not providers.available(judge_cfg["provider"]):
            sys.exit(f"判定モデルのキー {providers.ENV_KEYS[judge_cfg['provider']]} が未設定")
        ok = [r for r in records if r["ok"]]
        print(f"評価し直す回答: {len(ok)}件(判定 {judge_cfg['model']})")
        outdir = new_run_dir(raw.parent, f"rejudged-{dt.datetime.now():%Y-%m-%d_%H%M}")
        qmap = {q["id"]: q for q in queries}
        with open(outdir / "raw.jsonl", "w", encoding="utf-8") as f:
            for n, r in enumerate(records, 1):
                if r["ok"]:
                    ans = {"text": r["answer_text"], "cited_urls": r["cited_urls"], "source_urls": r["source_urls"]}
                    r["eval"] = evaluate.evaluate(ans, qmap.get(r["query_id"], {"text": r["query"]}), profile, facts, judge_cfg)
                    print(f"[{n}/{len(records)}] {r['provider']} {r['query_id']} #{r['run_index']}"
                          + (f": 判定失敗 {r['eval']['judge_error'][:60]}" if r["eval"]["judge_error"] else ""))
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        md = report.build(report.load(outdir / "raw.jsonl"), profile, pricing,
                          f"保存済みの回答({raw.parent.name})を {dt.date.today()} に評価し直したもの。", [q["id"] for q in queries])
        (outdir / "summary.md").write_text(md, encoding="utf-8")
        print(f"書き出し: {outdir / 'summary.md'}")
        return

    if a.report:
        raw = Path(a.report)
        records = report.load(raw)
        profile, _, queries = load_target(records[0]["target"])
        note = DRY_NOTE if records[0].get("dry_run") else ""
        md = report.build(records, profile, pricing, note, [q["id"] for q in queries])
        (raw.parent / "summary.md").write_text(md, encoding="utf-8")
        print(f"書き出し: {raw.parent / 'summary.md'}")
        return

    profile, facts, queries = load_target(a.target)

    if a.lang:
        langs = set(a.lang.split(","))
        queries = [q for q in queries if q["lang"] in langs]
    wanted = [p.strip() for p in a.providers.split(",") if p.strip()]
    unknown = [p for p in wanted if p not in providers.ASK]
    if unknown:
        sys.exit(f"不明なAI: {unknown}")

    if a.dry_run:
        active, skipped = wanted, []
    else:
        active = [p for p in wanted if providers.available(p)]
        skipped = [p for p in wanted if p not in active]
    use_judge = not a.no_judge and (a.dry_run or providers.available(judge_cfg["provider"]))

    plan = [(p, q, i) for q in queries for p in active for i in range(1, a.runs + 1)]
    if a.limit is not None:
        plan = plan[:a.limit]

    print(f"対象: {profile['brand']}  クエリ {len(queries)}件 × AI {len(active)}社 × {a.runs}回 → 回答 {len(plan)}回")
    for p in skipped:
        print(f"スキップ: {p}({providers.ENV_KEYS[p]} が未設定)")
    if not use_judge:
        print("判定モデル: 使わない(順位はリストの行頭から推定・矛盾チェックなし)")
    rows, total = cost.estimate(plan, pricing, use_judge)
    print(cost.format_estimate(rows, total))
    todos = todo_lines(a.target)
    if todos:
        print(f"\nfacts.yaml の TODO {len(todos)}件(判定には使わない):")
        for t in todos:
            print(f"  - {t}")

    if not plan:
        print("\n実行する回答がありません(.env のキーを確認)")
        return
    if not a.dry_run and not a.yes:
        if input(f"\n約 {total:.2f} USD かかる見込みです。実行しますか? [y/N] ").strip().lower() != "y":
            print("中止しました")
            return

    # 1回の実行ごとに別フォルダ(同じ分に --limit の試し打ちと本番を続けて流しても上書きしない)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
    outdir = new_run_dir(ROOT / "results" / a.target, stamp, "-dryrun" if a.dry_run else "")
    raw_path = outdir / "raw.jsonl"
    judge_model = None if (a.dry_run or not use_judge) else judge_cfg

    with open(raw_path, "w", encoding="utf-8") as f:
        for n, (p, q, i) in enumerate(plan, 1):
            model = pricing["providers"][p]["model"]
            rec = {"ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                   "target": a.target, "provider": p, "model": model,
                   "query_id": q["id"], "lang": q["lang"], "query_type": q["type"], "query": q["text"],
                   "run_index": i, "dry_run": a.dry_run}
            try:
                ans = stub_answer(p, q, i, profile) if a.dry_run else providers.ask(p, q["text"], model, profile.get("user_location"))
                ev = evaluate.evaluate(ans, q, profile, facts, judge_model)
                if a.dry_run:  # 架空の回答で矛盾判定の経路を通す
                    ev["contradictions"] = [{"quote": "年中無休で営業", "fact_key": "hours.closed", "verdict": "contradiction",
                                             "explanation": "DRY-RUN の架空の判定"}] if ev["mentioned"] else []
                rec.update(ok=True, error=None, answer_text=ans["text"], cited_urls=ans["cited_urls"],
                           source_urls=ans["source_urls"], searches=ans["searches"],
                           input_tokens=ans["input_tokens"], output_tokens=ans["output_tokens"],
                           reported_cost=ans["reported_cost"], served_model=ans["served_model"],
                           latency_s=ans["latency_s"], eval=ev, raw_response=ans["raw"])
                mark = "言及あり" if ev["mentioned"] else "言及なし"
            except Exception as e:  # 1件の失敗(タイムアウト・不正なJSON等)で全体を止めない
                rec.update(ok=False, error=f"{type(e).__name__}: {e}")
                mark = f"失敗: {str(e)[:80]}"
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            print(f"[{n}/{len(plan)}] {p} {q['id']} #{i}: {mark}")

    note = DRY_NOTE if a.dry_run else ""
    md = report.build(report.load(raw_path), profile, pricing, note, [q["id"] for q in queries])
    (outdir / "summary.md").write_text(md, encoding="utf-8")
    print(f"\n書き出し: {raw_path}\n          {outdir / 'summary.md'}")
