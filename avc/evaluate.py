"""1回分の回答を評価する。

機械的に決めるもの(再現性が要るのでLLMに任せない):
  言及の有無 / 引用URLに公式ドメインがあるか
LLM(安価なモデル)に任せるもの:
  回答に挙がった店名の順序付きリスト(→順位・競合) / facts.yaml との矛盾
判定モデルが使えないときは、番号付きリストの行から順位をおおまかに推定する。
"""

import json
import re
from urllib.parse import urlparse

import yaml


def _mask_excluded(text, profile):
    for pat in profile.get("exclude_patterns") or []:
        text = re.sub(pat, " ", text, flags=re.I)
    return text


def find_alias(text, profile):
    """最初に一致した表記を返す。無ければ None。"""
    t = _mask_excluded(text or "", profile)
    for a in profile["aliases"]:
        if re.search(re.escape(a), t, flags=re.I):
            return a
    return None


def matches_target(name, profile):
    return find_alias(name, profile) is not None


def is_official(url, profile):
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in profile["official_domains"])


def is_owned(url, profile):
    u = url.lower()
    return any(o.lower() in u for o in profile.get("owned_urls") or [])


# 字下げなしの番号・箇条書きの行だけを見る(見出しや「- Price:」のような子項目は店名にしない)
LIST_LINE = re.compile(r"^(?:\d+[.)]|[-*•])\s+(.+)$")
BOLD = re.compile(r"\*\*(.+?)\*\*")
SPLIT = re.compile(r"\s[-–—]\s|[:：(（]")


def heuristic_listing(text):
    """判定モデルが無いときの代替。番号・箇条書き・見出しの行頭を店名候補として順に返す。"""
    names = []
    for line in (text or "").splitlines():
        m = LIST_LINE.match(line)
        if not m:
            continue
        body = m.group(1)
        b = BOLD.search(body)
        if b and (b.group(1).rstrip().endswith((":", "：")) or body[b.end():b.end() + 1] in (":", "：")):
            continue  # 「**住所:** …」のような項目名の行
        n = b.group(1) if b else SPLIT.split(body, 1)[0]
        n = n.strip(" *:：")
        if 1 < len(n) <= 60 and n not in names:
            names.append(n)
    return names


JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "businesses_listed": {
            "type": "array", "items": {"type": "string"},
            "description": "回答が推薦・紹介している店舗やサービスの名前。回答に出た順。",
        },
        "claims_about_target": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "quote": {"type": "string"},
                    "fact_key": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["consistent", "contradiction", "unverifiable"]},
                    "explanation": {"type": "string"},
                },
                "required": ["quote", "fact_key", "verdict", "explanation"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["businesses_listed", "claims_about_target"],
    "additionalProperties": False,
}

JUDGE_PROMPT = """あなたはAI検索の回答を監査する担当者です。次の「質問」に対するAIの「回答」を読み、2つの作業をしてください。

1. businesses_listed: 回答が推薦・紹介している店舗(スパ・サロンなど)の名前を、回答に出てきた順にすべて書き出す。
   表記は回答のまま。店舗でないもの(地名・ホテル・予約サイト・記事名)は含めない。
2. claims_about_target: 対象店舗「{brand}」(表記ゆれ: {aliases})について回答が述べている事実の主張を1つずつ取り出し、
   「正解データ」と照らして判定する。
   - quote: 回答から該当箇所をそのまま抜き出す(150字以内)
   - fact_key: 照合した正解データの項目名(例: hours, pricing.range, address)。該当が無ければ "none"
   - verdict: 正解データと食い違う → contradiction / 合っている → consistent / 正解データに無く判断できない → unverifiable
   - explanation: 判定の理由を日本語で1文
   正解データの menu は全メニュー。menu に無い品目名だけを理由に contradiction にしない(名称の言い換えはよくある)。
   正解データの known_false に当たる記述は contradiction。価格の小さな丸め(約・〜)やレビュー件数の多少の差は contradiction にしない。
   対象店舗に触れていなければ空の配列にする。

# 正解データ
{facts}

# 質問
{query}

# 回答
{answer}
"""


def _parse_json(text):
    """```json の囲みや前置きがあっても、最初の { から最後の } までを JSON として読む。"""
    s, e = text.find("{"), text.rfind("}")
    data = json.loads(text[s:e + 1])
    if not isinstance(data.get("businesses_listed"), list) or not isinstance(data.get("claims_about_target"), list):
        raise ValueError("判定結果に必要な項目がない")
    return data


def _judge_gemini(prompt, model):
    # Interactions API は構造化出力の指定方法を確認できていないので、JSONだけを返すよう指示して読む
    from .providers import _post
    import os
    resp = _post(
        "https://generativelanguage.googleapis.com/v1beta/interactions",
        {"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
        {"model": model, "store": False,
         "input": prompt + "\n\n# 出力形式\n次のJSONスキーマに合うJSONだけを返す。説明文は書かない。\n"
                  + json.dumps(JUDGE_SCHEMA, ensure_ascii=False)},
    )
    text = "".join(c.get("text", "") for s in resp.get("steps", []) if s.get("type") == "model_output"
                   for c in s.get("content", []))
    data = _parse_json(text)
    u = resp.get("usage") or {}
    data["_usage"] = {"input_tokens": u.get("total_input_tokens") or 0,
                      "output_tokens": (u.get("total_output_tokens") or 0) + (u.get("total_thought_tokens") or 0)}
    return data


def judge(answer_text, query, profile, facts, cfg):
    prompt = JUDGE_PROMPT.format(
        brand=profile["brand"], aliases=", ".join(profile["aliases"]),
        facts=yaml.safe_dump({k: v for k, v in facts.items() if k != "todo"}, allow_unicode=True, sort_keys=False),
        query=query, answer=answer_text,
    )
    if cfg["provider"] == "gemini":
        return _judge_gemini(prompt, cfg["model"])

    import anthropic

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=cfg["model"], max_tokens=4000,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": JUDGE_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("判定モデルが拒否した")
    data = json.loads(next(b.text for b in resp.content if b.type == "text"))
    data["_usage"] = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
    return data


def evaluate(answer, query, profile, facts, judge_cfg=None):
    text = answer["text"]
    urls = answer["cited_urls"] + [u for u in answer["source_urls"] if u not in answer["cited_urls"]]
    result = {
        "mentioned": find_alias(text, profile) is not None,
        "matched_alias": find_alias(text, profile),
        "official_cited": any(is_official(u, profile) for u in answer["cited_urls"]),
        "official_in_sources": any(is_official(u, profile) for u in urls),
        "owned_in_sources": any(is_owned(u, profile) for u in urls),
        "judge": None,
        "judge_error": None,
    }
    listed = None
    if judge_cfg and text.strip():
        try:
            j = judge(text, query["text"], profile, facts, judge_cfg)
            result["judge"] = j
            listed = j["businesses_listed"]
        except Exception as e:  # 判定の失敗で計測全体を止めない
            result["judge_error"] = f"{type(e).__name__}: {e}"
    result["listing_method"] = "judge" if listed is not None else "heuristic"
    if listed is None:
        listed = heuristic_listing(text)
    rank = next((i + 1 for i, n in enumerate(listed) if matches_target(n, profile)), None)
    result["businesses_listed"] = listed
    result["rank"] = rank
    result["list_size"] = len(listed)
    result["competitors"] = [n for n in listed if not matches_target(n, profile)]
    claims = (result["judge"] or {}).get("claims_about_target", [])
    result["contradictions"] = [c for c in claims if c["verdict"] == "contradiction"]
    return result
