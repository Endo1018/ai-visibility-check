# ai-visibility-check

お店のことを AI（ChatGPT・Claude・Gemini・Perplexity の API）に、ウェブ検索ありで聞いて、
**お店の名前が出るか・何番目に出るか・営業時間や料金が正しく書かれているか**を、言語ごとに記録する道具です。
結果を1枚にまとめた「お店の AI 通信簿」も作れます。

ホーチミンのお店の AI 対応（AIO）を手伝う [MCP365](https://team.sub.jp/mcp365/) が、自分の店を測るために作ったものを公開しています。
Claude Code や Codex などで自分でカスタマイズしたい方向けです。**サポートはしていません。**
自分で動かすのが大変な方は、MCP365 の無料の通信簿・月額のダッシュボードをご利用ください。

> **注意: API の答えは、ChatGPT などのアプリの答えと一致しません。**
> アプリには会話の履歴・位置情報・独自の設定があり、使われるモデルも違うことがあります。
> この道具の数字は「条件をそろえて比べる（前と後・言語ごと・AI ごと）」ためのものです。
> 「ChatGPT でこう出る」という主張の根拠には使わないでください。

## 費用

API の利用料は、使う人の負担です（各社に直接払います）。
1回の計測（3社 × 9問 × 3回＋判定）で、2026年11月の単価で約 $7 でした。実行の前に必ず見積もりを出して、`y` を待ちます。
単価とモデル名は `pricing.yaml` にあります。**使う前に各社の公式ページで確かめて**、変わっていたら直してください。

## 用意

```bash
python3 -m venv .venv            # Python 3.10 以上
.venv/bin/pip install -r requirements.txt
cp .env.example .env             # 使う AI のキーだけ入れる。無いものは飛ばして測る
```

`.env` は git に入れません。キーをコマンドラインやチャットに貼らないでください。

## 使い方

```bash
# 1. API を呼ばずに、流れだけ確かめる（架空の答えで集計まで。費用 0）
.venv/bin/python -m avc --target example --dry-run

# 2. 最初の4回答だけ本番で
.venv/bin/python -m avc --target example --limit 4

# 3. 全部（見積もりを見てから y）
.venv/bin/python -m avc --target example

# 4. 通信簿（1枚のまとめ）を作る
python3 tsushinbo/make.py --target example --run results/example/<日時> --out tsushinbo/out/example.html
```

結果は `results/<お店>/<日時>/` に `raw.jsonl`（全部の答えと判定）と `summary.md`（集計）が入ります。
結果には、ほかのお店の名前が入ります。公開するリポに入れないでください（`.gitignore` 済み）。

## 自分のお店で測る

`targets/example/` をコピーして、3つのファイルを書き換えます。

| ファイル | 書くこと |
|---|---|
| `profile.yaml` | お店の名前と、答えの中で「このお店」と見なす書き方（カタカナなど）・公式サイトのドメイン・検索の地域 |
| `facts.yaml` | 正しい情報（住所・営業時間・定休日・料金・言語・予約の方法）。AI の答えと食い違うかの判定に使う。分からない値は書かない |
| `queries.yaml` | 言語ごとの質問（店名を出さずに探す・店名で聞く・条件で探す）。`id` は一度使ったら変えない |

## 中身

```
avc/
  cli.py         入口（計画 → 見積もり → 確認 → 実行 → 集計）
  providers.py   各 AI の呼び出し（ウェブ検索あり）。答え・引用の URL・使用量をそろえる
  evaluate.py    名前が出たか・何番目か・公式サイトの引用・一緒に出たお店・facts との食い違い（判定は AI）
  cost.py        見積もりと実費
  report.py      raw.jsonl → summary.md
tsushinbo/make.py  計測の結果から、1枚の「お店の AI 通信簿」（HTML）を作る
pricing.yaml       各 AI のモデル名・単価（出典の URL つき）
targets/example/   架空のお店の見本
```

Claude は公式の SDK（`anthropic`）、ほかの3社は標準ライブラリで REST を直接呼んでいます（応答の JSON を丸ごと残すため）。

## ライセンス

[PolyForm Noncommercial License 1.0.0](LICENSE)。**商用での利用はできません**（個人の学び・研究・自分のお店を測るなど、商用でない使い方は自由です）。
この道具を使った診断やサービスを売りたい場合は、MCP365 にご相談ください。

Required Notice: Copyright 2026 Takahisa Endo / MCP365 (https://team.sub.jp/mcp365/)
