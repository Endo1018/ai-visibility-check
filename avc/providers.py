"""各AIにWeb検索を有効にして質問し、回答・引用URL・使用量をそろえた形で返す。

リクエストの形は 2026-11-02 に各社の公式ドキュメントで確認した:
  OpenAI     Responses API + {"type": "web_search"}
             https://developers.openai.com/api/docs/guides/tools-web-search
  Anthropic  Messages API + web_search_20260318(公式SDK)
             https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool
  Gemini     Interactions API + {"type": "google_search"}
             https://ai.google.dev/gemini-api/docs/google-search
  Perplexity Agent API + {"type": "web_search"}
             https://docs.perplexity.ai/docs/agent-api/tools/web-search.md
Gemini と Perplexity は応答の全項目がドキュメントに載っていないため、
本文とURLは応答JSONを走査して拾う。応答は raw.jsonl にそのまま残すので、後から読み直せる。
"""

import json
import os
import time
import urllib.error
import urllib.request

ENV_KEYS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
}

TIMEOUT = 180


class ProviderError(Exception):
    pass


def available(name):
    return bool(os.environ.get(ENV_KEYS[name]))


def _post(url, headers, body):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:800]
        raise ProviderError(f"HTTP {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise ProviderError(f"接続エラー: {e.reason}") from None


def _walk(obj):
    """dict/list を深さ優先でたどり、すべての dict を返す。"""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _urls_in(obj):
    urls = []
    for d in _walk(obj):
        for k in ("url", "uri"):
            v = d.get(k)
            if isinstance(v, str) and v.startswith("http") and v not in urls:
                urls.append(v)
    return urls


def _location(loc, keys):
    if not loc:
        return None
    out = {k: loc[k] for k in keys if loc.get(k)}
    return out or None


# ---------------------------------------------------------------- OpenAI

def ask_openai(query, model, location):
    tool = {"type": "web_search"}
    loc = _location(location, ("country", "city", "region", "timezone"))
    if loc:
        tool["user_location"] = {"type": "approximate", **loc}
    resp = _post(
        "https://api.openai.com/v1/responses",
        {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        {"model": model, "input": query, "tools": [tool],
         "include": ["web_search_call.action.sources"]},
    )
    text, cited = [], []
    searches = 0
    for item in resp.get("output", []):
        if item.get("type") == "web_search_call":
            searches += 1
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    text.append(c.get("text", ""))
                    for a in c.get("annotations", []):
                        if a.get("url") and a["url"] not in cited:
                            cited.append(a["url"])
    u = resp.get("usage") or {}
    return {
        "text": "\n".join(text),
        "cited_urls": cited,
        "source_urls": _urls_in([i for i in resp.get("output", []) if i.get("type") == "web_search_call"]),
        "searches": searches,
        "input_tokens": u.get("input_tokens"),
        "output_tokens": u.get("output_tokens"),
        "reported_cost": None,
        "served_model": resp.get("model"),
        "raw": resp,
    }


# ---------------------------------------------------------------- Anthropic

def ask_anthropic(query, model, location):
    import anthropic  # 公式SDK。キーが無いときは呼ばれないので遅延 import

    client = anthropic.Anthropic()
    # direct: 動的フィルタリング(コード実行で検索結果を絞る)を使わない。
    # 使うと回答の本文に出典(citations)が付かず、公式サイトの引用を測れなかった(2026-11-02 実測)
    tool = {"type": "web_search_20260318", "name": "web_search", "max_uses": 5, "allowed_callers": ["direct"]}
    # 国コードは対応国だけ受け付ける(VN は 400 になった・2026-11-02 実測)。都市・地域・タイムゾーンだけ渡す
    loc = _location(location, ("city", "region", "timezone"))
    if loc:
        tool["user_location"] = {"type": "approximate", **loc}
    messages = [{"role": "user", "content": query}]
    blocks, raws = [], []
    usage = {"input": 0, "output": 0, "searches": 0}
    served = None
    # pause_turn(サーバー側の検索ループが上限に達した)は応答をそのまま返して続けてもらう
    for _ in range(4):
        try:
            # 拒否されたときに別モデルで答え直す server-side fallback を既定で有効にしている
            resp = client.beta.messages.create(
                model=model, max_tokens=16000, messages=messages, tools=[tool],
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            )
        except anthropic.RateLimitError as e:
            raise ProviderError(f"レート制限: {e}") from None
        except anthropic.APIStatusError as e:
            raise ProviderError(f"HTTP {e.status_code}: {e.message}") from None
        except anthropic.APIConnectionError as e:
            raise ProviderError(f"接続エラー: {e}") from None
        raws.append(resp.model_dump(mode="json"))
        served = resp.model
        blocks.extend(resp.content)
        usage["input"] += resp.usage.input_tokens or 0
        usage["output"] += resp.usage.output_tokens or 0
        stu = getattr(resp.usage, "server_tool_use", None)
        usage["searches"] += (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
        if resp.stop_reason != "pause_turn":
            break
        # 再開するときは、それまでの全パスのアシスタント内容をまとめて送り直す
        messages = [{"role": "user", "content": query},
                    {"role": "assistant", "content": list(blocks)}]
    if resp.stop_reason == "pause_turn":
        raise ProviderError("pause_turn のまま再開の上限(4回)に達した")
    if resp.stop_reason == "refusal":
        raise ProviderError("拒否された(stop_reason=refusal)")

    text, cited = [], []
    for b in blocks:
        if b.type == "text":
            text.append(b.text)
            for c in (b.citations or []):
                url = getattr(c, "url", None)
                if url and url not in cited:
                    cited.append(url)
    sources = _urls_in([d for r in raws for d in r.get("content", [])
                        if d.get("type") == "web_search_tool_result"])
    return {
        "text": "".join(text),
        "cited_urls": cited,
        "source_urls": sources,
        "searches": usage["searches"],
        "input_tokens": usage["input"],
        "output_tokens": usage["output"],
        "reported_cost": None,
        "served_model": served,
        "raw": raws,
    }


# ---------------------------------------------------------------- Gemini

def _resolve_gemini(a):
    """引用URLが Google の中継URL(vertexaisearch…/grounding-api-redirect)なら、
    title に入っているドメイン名から https://<ドメイン>/ を作る(ドキュメントの例では title=ドメイン)。
    実際に中継URLが返るかは未確認。元のURLは raw_response に残る。"""
    url, title = a.get("url"), (a.get("title") or "").strip()
    if url and "grounding-api-redirect" in url and title and " " not in title and "." in title:
        return f"https://{title}/"
    return url


def ask_gemini(query, model, location):
    resp = _post(
        "https://generativelanguage.googleapis.com/v1beta/interactions",
        {"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
        {"model": model, "input": query, "tools": [{"type": "google_search"}], "store": False},
    )
    steps = resp.get("steps") or resp.get("outputs") or []
    text, cited, searches = [], [], 0
    for s in steps if isinstance(steps, list) else []:
        t = s.get("type")
        if t == "google_search_call":
            q = (s.get("arguments") or {}).get("queries") or []
            searches += len(q) or 1
        if t == "model_output":
            for c in s.get("content", []):
                if c.get("text"):
                    text.append(c["text"])
                for a in c.get("annotations", []) or []:
                    url = _resolve_gemini(a)
                    if url and url not in cited:
                        cited.append(url)
    if not text and resp.get("output_text"):
        text.append(resp["output_text"])
    u = resp.get("usage") or resp.get("usage_metadata") or {}
    return {
        "text": "\n".join(text),
        "cited_urls": cited,
        "source_urls": [x for x in _urls_in(steps) if x not in cited],
        "searches": searches,
        "input_tokens": u.get("total_input_tokens") or u.get("input_tokens") or u.get("prompt_token_count"),
        "output_tokens": u.get("total_output_tokens") or u.get("output_tokens") or u.get("candidates_token_count"),
        "reported_cost": None,
        "served_model": resp.get("model"),
        "raw": resp,
    }


# ---------------------------------------------------------------- Perplexity

def ask_perplexity(query, model, location):
    tool = {"type": "web_search"}
    loc = _location(location, ("country", "region", "city"))
    if loc:
        tool["user_location"] = loc
    resp = _post(
        "https://api.perplexity.ai/v1/agent",
        {"Authorization": f"Bearer {os.environ['PERPLEXITY_API_KEY']}"},
        {"model": model, "input": query, "tools": [tool], "max_output_tokens": 2000},
    )
    text, cited, sources, searches = [], [], [], 0
    for item in resp.get("output", []):
        if item.get("type") == "search_results":
            searches += 1
            sources.extend(r["url"] for r in item.get("results", []) if r.get("url"))
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    text.append(c.get("text", ""))
                for a in c.get("annotations", []) or []:
                    if a.get("url") and a["url"] not in cited:
                        cited.append(a["url"])
    u = resp.get("usage") or {}
    cost = (u.get("cost") or {}).get("total_cost")
    return {
        "text": "\n".join(text) or resp.get("output_text", ""),
        "cited_urls": cited,
        "source_urls": [x for x in dict.fromkeys(sources) if x not in cited],
        "searches": searches,
        "input_tokens": u.get("input_tokens"),
        "output_tokens": u.get("output_tokens"),
        "reported_cost": cost,
        "served_model": resp.get("model"),
        "raw": resp,
    }


ASK = {
    "openai": ask_openai,
    "anthropic": ask_anthropic,
    "gemini": ask_gemini,
    "perplexity": ask_perplexity,
}


def ask(name, query, model, location):
    t0 = time.time()
    out = ASK[name](query, model, location)
    out["latency_s"] = round(time.time() - t0, 1)
    return out
