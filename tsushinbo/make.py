#!/usr/bin/env python3
"""お店の AI 通信簿（1枚のまとめ）を、avc の計測結果から作る。

使い方（main のチェックアウトで）:
  python3 tsushinbo/make.py --target example --run results/example/<日時> [--langs ja,en] [--out tsushinbo/out/example.html]

材料: results/<id>/<日時>/raw.jsonl（brand と discovery の質問）と targets/<id>/profile.yaml の brand。
各 AI の1回目（run_index 1）だけを使う。通信簿は「1回聞いた結果」であって、平均ではない。
数字を手で書かない。判定は下の grade_* だけで決める。

守ること: 結果を約束しない・「ChatGPT のアプリでこう出る」と言わない（API の答えだと必ず書く）。
"""
import argparse, collections, html, json, os, re, sys, unicodedata, urllib.parse, datetime

AI_NAMES = {"openai": "ChatGPT", "gemini": "Gemini", "anthropic": "Claude"}
AI_ORDER = ["openai", "gemini", "anthropic"]
LANG_NAMES = {"ja": "日本語", "en": "英語", "vi": "ベトナム語"}
# 出典の集計から外す（検索の中継や、元のサイトが見えないもの）
SKIP_HOSTS = {"vertexaisearch.cloud.google.com"}


def load_brand(target):
    p = os.path.join("targets", target, "profile.yaml")
    for line in open(p, encoding="utf-8"):
        m = re.match(r"brand:\s*(.+)", line)
        if m:
            return m.group(1).strip().strip('"')
    return target


def grade_brand(r):
    """名前で聞いたとき: ◎ 正しく紹介 / △ 紹介はあるがまちがいを含む / × 見つからない"""
    ev = r["eval"]
    if not ev.get("mentioned"):
        return "×", "見つからない"
    if ev.get("contradictions"):
        return "△", "まちがいあり"
    return "◎", "正しく紹介"


def grade_discovery(r):
    """探す質問: ◎ 3番目まで / ○ 4番目以降 / × 出てこない"""
    ev = r["eval"]
    if not ev.get("mentioned"):
        return "×", "出てこない"
    rank = ev.get("rank")
    if rank and rank <= 3:
        return "◎", f"{rank}番目"
    return "○", f"{rank}番目" if rank else "名前は出た"


def name_key(name):
    """店名の表記ゆれをそろえる鍵: かっこ書き・ベトナム語の声調記号・大文字小文字・記号を落とす"""
    n = re.sub(r"[（(].*", "", name).replace("đ", "d").replace("Đ", "D")
    n = "".join(c for c in unicodedata.normalize("NFKD", n) if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w\s]", " ", n.lower()).split())


def merge_keys(keys):
    """短い名前が、ちょうど1つの長い名前の頭と一致したら同じ店にまとめる（Moc Kim → Moc Kim Spa & Beauty）"""
    to = {}
    for k in keys:
        longer = [o for o in keys if o != k and o.startswith(k + " ")]
        to[k] = longer[0] if len(longer) == 1 else k
    return to


def rival_hosts(text, listed):
    """答えの文中で、各店の名前から次の店の名前までに貼られたリンクのサイトを集める。
    リンクが本文に入るのは主に ChatGPT の答え。名前が文中に見つからない店は空"""
    low = text.lower()
    pos = []
    for name in listed:
        shown = re.sub(r"[（(].*", "", name).strip().lower()
        i = low.find(shown) if shown else -1
        if i >= 0:
            pos.append((i, name))
    pos.sort()
    out = {}
    for j, (i, name) in enumerate(pos):
        end = pos[j + 1][0] if j + 1 < len(pos) else len(text)
        out[name] = {host(u) for u in re.findall(r"\]\((https?://[^)\s]+)\)", text[i:end])} - {""} - SKIP_HOSTS
    return out


def own_site(h, key):
    """サイト名に店名の言葉（3文字以上）が入っていれば、その店の公式サイトとみなす"""
    hh = h.replace("-", "").replace(".", " ")
    return any(len(w) >= 3 and w not in GENERIC and w in hh for w in key.split())


# 業種の言葉は店名の手がかりにしない（ほかのお店の「xxxspa.vn」を「spa」の一致だけで公式と見まちがえないため）
GENERIC = {"spa", "salon", "hair", "beauty", "massage", "nail", "nails", "the", "and", "head", "body",
           "wellness", "aesthetic", "clinic", "cafe", "coffee", "restaurant", "bar", "shop", "store", "hotel"}


def load_own_hosts(target):
    """profile.yaml の official_domains（自分の店のサイト）。他店の段落に混ざっても数えない"""
    out, on = set(), False
    for line in open(os.path.join("targets", target, "profile.yaml"), encoding="utf-8"):
        if re.match(r"official_domains:", line):
            on = True; continue
        if on:
            m = re.match(r"\s+-\s*(\S+)", line)
            if not m:
                break
            out.add(m.group(1).strip("'\"").removeprefix("www."))
    return out


def clean(e):
    """判定の説明から、社内の言葉（known_false・正解データ など）を消す。お客様に渡す紙なので"""
    e = re.sub(r"[（(]\s*known_false\s*[)）]", "", e)
    e = e.replace("known_false", "").replace("正解データ", "お店の正しい情報").replace("facts.yaml", "お店の正しい情報")
    return re.sub(r"\s{2,}", " ", e).strip()


def host(u):
    h = urllib.parse.urlparse(u).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True)
    ap.add_argument("--run", required=True, action="append",
                    help="results/<id>/<日時> のフォルダ（AI ごとに別の日の計測を合わせるときは複数回）")
    ap.add_argument("--label", default="", help="見出しの下に出す一言（例: 最初の計測）")
    ap.add_argument("--langs", default="ja,en")
    ap.add_argument("--out")
    a = ap.parse_args()
    langs = a.langs.split(",")
    brand = load_brand(a.target)
    rows = [json.loads(l) for d in a.run for l in open(os.path.join(d, "raw.jsonl"), encoding="utf-8")]
    rows = [r for r in rows if r.get("ok") and r.get("run_index") == 1 and r["lang"] in langs
            and r["query_type"] in ("brand", "discovery") and r["provider"] in AI_NAMES]
    if not rows:
        sys.exit("使える答えがありません（run_index 1・brand/discovery・指定の言語）")
    d0, d1 = min(r["ts"] for r in rows)[:10], max(r["ts"] for r in rows)[:10]
    day = d0 if d0 == d1 else f"{d0}〜{d1[5:]}"

    cell = {}  # (type, lang, provider) -> (mark, note)
    queries = {}  # (type, lang) -> text
    errors, sources, rivals, ahead = [], collections.Counter(), collections.Counter(), collections.Counter()
    disc = [r for r in rows if r["query_type"] == "discovery"]
    keymap = merge_keys({name_key(n) for r in disc for n in r["eval"].get("competitors") or [] if name_key(n)})
    label = collections.defaultdict(collections.Counter)  # 鍵 -> 表示名の回数（いちばん多い表記を出す）
    own_hosts = load_own_hosts(a.target)
    rsites = collections.defaultdict(collections.Counter)  # 鍵 -> その店の段落に貼られたサイト
    for r in rows:
        t, l, p = r["query_type"], r["lang"], r["provider"]
        queries[(t, l)] = r["query"]
        cell[(t, l, p)] = grade_brand(r) if t == "brand" else grade_discovery(r)
        for c in r["eval"].get("contradictions") or []:
            errors.append((p, l, c.get("quote", ""), clean(c.get("explanation", ""))))
        for u in set((r.get("cited_urls") or []) + (r.get("source_urls") or [])):
            h = host(u)
            if h and h not in SKIP_HOSTS:
                sources[h] += 1
        if t == "discovery":
            # 自分の店が何番目か（出てこなければ、紹介された店は全部「先」）
            rank = r["eval"].get("rank") if r["eval"].get("mentioned") else None
            listed = r["eval"].get("businesses_listed") or r["eval"].get("competitors") or []
            seen = set()
            hosts_of = rival_hosts(r.get("answer_text") or "", listed)
            for i, name in enumerate(listed, 1):
                k0 = name_key(name)
                if k0 not in keymap:
                    continue  # 自分の店
                k = keymap[k0]
                shown = re.sub(r"[（(].*", "", name).strip()
                label[k][shown] += 1
                if k in seen:
                    continue
                seen.add(k); rivals[k] += 1
                for h in hosts_of.get(name, set()) - own_hosts:
                    rsites[k][h] += 1
                if rank is None or i < rank:
                    ahead[k] += 1

    # 総合の点: ◎=2・○/△=1・×=0 の合計を満点で割る
    pts = {"◎": 2, "○": 1, "△": 1, "×": 0}
    marks = [v[0] for v in cell.values()]
    score = round(100 * sum(pts[m] for m in marks) / (2 * len(marks)))

    def table(t, title, legend):
        out = [f'<section class="blk"><h2>{title}</h2><p class="legend">{legend}</p><table><thead><tr><th></th>']
        out += [f"<th>{AI_NAMES[p]}</th>" for p in AI_ORDER]
        out.append("</tr></thead><tbody>")
        for l in langs:
            if (t, l) not in queries:
                continue
            out.append(f'<tr><th scope="row">{LANG_NAMES.get(l, l)}<small>「{html.escape(queries[(t, l)])}」</small></th>')
            for p in AI_ORDER:
                m, n = cell.get((t, l, p), ("—", "未計測"))
                out.append(f'<td class="m m{ {"◎":"a","○":"b","△":"c","×":"d"}.get(m,"n") }"><b>{m}</b><span>{n}</span></td>')
            out.append("</tr>")
        out.append("</tbody></table></section>")
        return "".join(out)

    # 直せそうなこと（結果から機械的に出す。約束はしない）
    todo = []
    if errors:
        todo.append("AI の答えにまちがいがあります。まちがいの元になっているサイト（口コミサイト・まとめ記事など）を探して直すと、答えが変わることがあります。")
    miss_lang = [LANG_NAMES[l] for l in langs if all(cell.get(("discovery", l, p), ("×",))[0] == "×" for p in AI_ORDER)]
    if miss_lang:
        todo.append(f"{'・'.join(miss_lang)}で探す人には、どの AI でも出てきませんでした。AI が読んでいるサイト（下の「AI が読んだサイト」）に、その言葉でお店が載っているかを確かめるのが近道です。")
    if any(cell.get(("brand", l, p), ("◎",))[0] == "×" for l in langs for p in AI_ORDER):
        todo.append("名前で聞いても見つけられない AI があります。Google マップ・Bing・公式サイトのお店の情報（名前・住所・営業時間）がそろっているかを確かめると、見つけてもらいやすくなります。")
    if not todo:
        todo.append("大きな問題は見つかりませんでした。今の状態を保てているか、ときどき確かめるのがおすすめです。")

    err_html = "".join(f'<li><b>{AI_NAMES[p]}・{LANG_NAMES.get(l, l)}</b>「{html.escape(q)}」<span>{html.escape(e)}</span></li>'
                       for p, l, q, e in errors[:6]) or '<li class="none">見つかりませんでした</li>'
    src_html = "".join(f"<li>{html.escape(h)}<span>{n}回</span></li>" for h, n in sources.most_common(8))
    def sites(k):
        hs = [f'<i>{html.escape(h)}</i>' + ("（公式サイト）" if own_site(h, k) else "") for h, _ in rsites[k].most_common(3)]
        return ('<small>AI が読んだサイト: ' + "・".join(hs) + '</small>') if hs else ""
    riv_html = "".join(f"<li><div>{html.escape(min(label[k].items(), key=lambda x: (-x[1], len(x[0])))[0])}{sites(k)}</div><span>{n}回（うち先に紹介 {ahead[k]}回）</span></li>"
                       for k, n in sorted(rivals.items(), key=lambda x: (-ahead[x[0]], -x[1]))[:6]) or '<li class="none">—</li>'
    todo_html = "".join(f"<li>{html.escape(t)}</li>" for t in todo)

    doc = TEMPLATE.format(
        brand=html.escape(brand), day=day.replace("-", "."), score=score,
        label=f'<div class="label">{html.escape(a.label)}</div>' if a.label else "",
        langs="・".join(LANG_NAMES.get(l, l) for l in langs),
        t_brand=table("brand", "名前で聞いたとき", "◎ 正しく紹介　△ 紹介はあるが、まちがいを含む　× 見つからない"),
        t_disc=table("discovery", "探している人が聞いたとき", "◎ 3番目までに紹介　○ 4番目以降　× 出てこない"),
        errors=err_html, sources=src_html, rivals=riv_html, todo=todo_html)
    out = a.out or os.path.join("tsushinbo", "out", f"{a.target}-{day}.html")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, "w", encoding="utf-8").write(doc)
    print(f"{out}  点 {score}  ◎{marks.count('◎')} ○{marks.count('○')} △{marks.count('△')} ×{marks.count('×')}  まちがい {len(errors)}")


TEMPLATE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>お店の AI 通信簿｜{brand}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Zen+Kaku+Gothic+New:wght@400;500;700;900&display=swap">
<style>
:root{{--bg:#f7f8fa;--surface:#fff;--ink:#0f172a;--muted:#4a5468;--line:#e3e7ee;--accent:#2f4fff;--soft:#eef1ff;
--a:#16a34a;--a-bg:#e8f6ee;--b:#2f4fff;--b-bg:#eef1ff;--c:#b45309;--c-bg:#fdf2e2;--d:#dc2626;--d-bg:#fdecec}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font-family:"Zen Kaku Gothic New","Hiragino Sans",system-ui,sans-serif;line-height:1.7}}
.wrap{{max-width:760px;margin:0 auto;padding:24px 16px 40px}}
header{{background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:22px 20px;display:grid;grid-template-columns:1fr auto;gap:12px;align-items:center}}
.kick{{font-size:.8rem;font-weight:700;color:var(--accent);letter-spacing:.04em}}
h1{{margin:2px 0 4px;font-size:1.35rem;line-height:1.4}}
.meta{{font-size:.82rem;color:var(--muted)}}
.label{{display:inline-block;margin-top:6px;font-size:.78rem;font-weight:700;color:var(--c);background:var(--c-bg);border-radius:999px;padding:2px 10px}}
.score{{text-align:center;background:var(--soft);border-radius:14px;padding:10px 16px}}
.score b{{display:block;font-size:2.2rem;font-weight:900;color:var(--accent);line-height:1.1}}
.score span{{font-size:.75rem;color:var(--muted)}}
.blk{{background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:18px 16px;margin-top:14px}}
h2{{margin:0 0 4px;font-size:1.05rem}}
.legend{{margin:0 0 10px;font-size:.78rem;color:var(--muted)}}
table{{width:100%;border-collapse:collapse;table-layout:fixed}}
th,td{{border-top:1px solid var(--line);padding:8px 4px;vertical-align:middle}}
thead th{{border-top:0;font-size:.82rem;color:var(--muted);font-weight:700}}
tbody th{{text-align:left;font-size:.9rem;width:34%}}
tbody th small{{display:block;font-weight:400;font-size:.72rem;color:var(--muted);line-height:1.4;overflow-wrap:anywhere}}
td.m{{text-align:center}}
td.m b{{display:inline-grid;place-items:center;width:2.1em;height:2.1em;border-radius:50%;font-size:1.05rem}}
td.m span{{display:block;font-size:.7rem;color:var(--muted);line-height:1.3;margin-top:2px}}
.ma b{{background:var(--a-bg);color:var(--a)}} .mb b{{background:var(--b-bg);color:var(--b)}}
.mc b{{background:var(--c-bg);color:var(--c)}} .md b{{background:var(--d-bg);color:var(--d)}}
ul.list{{list-style:none;margin:0;padding:0}}
ul.list li{{border-top:1px solid var(--line);padding:8px 0;font-size:.9rem;display:flex;justify-content:space-between;gap:12px}}
ul.list li:first-child{{border-top:0}}
ul.list li span{{color:var(--muted);font-size:.8rem;white-space:nowrap}}
ul.list li small{{display:block;color:var(--muted);font-size:.75rem;margin-top:2px}}
ul.list li small i{{font-style:normal;white-space:nowrap}}
ul.list li > div{{flex:1;min-width:0}}
ul.list li > span{{flex-shrink:0}}
ul.err li{{display:block}} ul.err li span{{display:block;white-space:normal}}
.none{{color:var(--muted)}}
.two{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}}
.two .blk{{margin-top:14px}}
ol.todo{{margin:0;padding-left:1.3em}} ol.todo li{{margin:6px 0;font-size:.92rem}}
.note{{font-size:.75rem;color:var(--muted);margin-top:16px}}
@media (max-width:480px){{header{{grid-template-columns:1fr}} .score{{justify-self:start}}}}
</style></head>
<body><div class="wrap">
<header>
  <div><div class="kick">お店の AI 通信簿</div><h1>{brand}</h1>
  <div class="meta">ChatGPT・Gemini・Claude に {langs}で聞きました（{day}）</div>{label}</div>
  <div class="score"><b>{score}</b><span>100点中</span></div>
</header>
{t_brand}
{t_disc}
<section class="blk"><h2>AI の答えのまちがい</h2><p class="legend">お店の正しい情報と食い違っていた部分</p><ul class="list err">{errors}</ul></section>
<div class="two">
<section class="blk"><h2>AI が読んだサイト</h2><p class="legend">答えの出典に入っていたサイト（回数）</p><ul class="list">{sources}</ul></section>
<section class="blk"><h2>代わりに出てきたお店</h2><p class="legend">探す質問で一緒に紹介されたお店（回数・あなたのお店より先に紹介された回数）。あなたにだけお渡しする情報です</p><ul class="list">{rivals}</ul></section>
</div>
<section class="blk"><h2>直せそうなこと</h2><ol class="todo">{todo}</ol></section>

<p class="note">この結果は各社の API に、条件をそろえて1回ずつ聞いた答えです。ChatGPT などのアプリで聞いた答えとは一致しません。AI の答えは聞くたびに変わることがあります。点数は ◎=2・○△=1・×=0 の合計を満点で割ったものです。</p>
</div></body></html>
"""

if __name__ == "__main__":
    main()
