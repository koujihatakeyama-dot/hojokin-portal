"""公式ページの文章を、AIでやさしい言葉に言い直し、機械で照合して合格したものだけ data.json に入れる。

安全の考え方（ハルシネーション対策）
1. AIには、公式の文章に書いてあることだけを使って答えさせる。書いていない項目は null にさせる。
2. AIは、各項目について「公式の文章から一字一句そのまま写した根拠の文」も返す。
   その根拠が公式の文章の中に本当にあるかを、プログラムが確かめる。ないものは捨てる。
3. 言い直した文に出てくる数字（金額・割合・日付・年数など）が、公式の文章に1つでも無ければ、その項目を捨てる。
4. 合格した項目だけを d["ai"] に保存する。画面には「AI要約」と表示し、必ず公式ページを見るよう案内する。
ANTHROPIC_API_KEY が無いときは何もせずに終了する（収集の更新は止めない）。
標準ライブラリだけで動く。
"""
import datetime
import html as htmllib
import json
import os
import re
import sys
import unicodedata
import urllib.request

DATA_PATH = "data.json"
API_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("SUMMARY_MODEL", "claude-sonnet-5-5")
MAX_PER_RUN = int(os.environ.get("SUMMARY_MAX_PER_RUN", "25"))  # 1回の実行で要約する最大件数（費用の上限）
MAX_SOURCE_CHARS = 18000
JST = datetime.timezone(datetime.timedelta(hours=9))
HEADERS = {"User-Agent": "Mozilla/5.0 (subsidy-portal summarizer)", "Accept-Language": "ja"}
JG_DETAIL = "https://api.jgrants-portal.go.jp/exp/v1/public/subsidies/id/"

FIELDS = ["who_short", "who", "purpose", "how", "amount"]

PROMPT = """あなたは、補助金の公式ページの文章を、専門知識のない農家の方にも分かる言葉に言い直す係です。
次の【公式の文章】だけを使って、JSONで答えてください。

厳守するルール
- 【公式の文章】に書いていないことは、絶対に書かない。推測・一般論・補足はしない。書いていない項目は null にする。
- 数字（金額、割合、日付、年数、件数など）は、公式の文章にあるものだけを、同じ値のまま半角数字で書く。足し算・換算・言い換えはしない。
- 言い直した文に数字を使うときは、その数字を含む原文の部分を、必ず「evidence」に入れる。
- 専門用語は、やさしい言葉に直すか、かっこで短く説明する。1文は短くする。
- 各項目には、その根拠になった文を、【公式の文章】から一字一句そのまま写して「evidence」に入れる（8〜80文字）。
- 「申請できる」「採択される」「向いている」など、結果の約束や判断は書かない。

出力するJSON（これ以外の文字は出さない）
{
 "who_short": {"text": "対象を一言で（15〜35字。例：個人の農家、農業法人、JAなどの団体）", "evidence": "原文の抜き書き"} または null,
 "who":     {"text": "どんな人・団体が対象か（60〜120字）", "evidence": "原文の抜き書き"} または null,
 "purpose": {"text": "何のための支援か（40〜100字）", "evidence": "原文の抜き書き"} または null,
 "how":     {"text": "応募の方法と締切（60〜140字）", "evidence": "原文の抜き書き"} または null,
 "amount":  {"text": "補助の金額や割合（書いてあるときだけ）", "evidence": "原文の抜き書き"} または null,
 "checks":  [ {"text": "対象になるために確認したい条件（40〜80字）", "evidence": "原文の抜き書き"} ]   // 0〜3個。条件が書いてあるものだけ
}

【件名】
%(title)s

【公式の文章】
%(source)s
"""


def log(*a, **k):
    print(*a, flush=True, **k)


# ---------- 文字の正規化と照合 ----------
def norm(s):
    s = unicodedata.normalize("NFKC", str(s or ""))
    s = re.sub(r"\s+", "", s)
    return re.sub(r"(?<=\d),(?=\d)", "", s)


def numbers(s):
    return set(re.findall(r"\d+(?:\.\d+)?", norm(s)))


def verify_field(obj, source_n, source_nums):
    """text と evidence が揃い、evidence が原文にあり、text の数字がすべて原文にあるときだけ合格。"""
    if not isinstance(obj, dict):
        return None
    text, ev = (obj.get("text") or "").strip(), (obj.get("evidence") or "").strip()
    if not text or len(norm(ev)) < 8:
        return None
    if norm(ev) not in source_n:  # 根拠が原文に無い
        return None
    # 言い直した文の数字は、根拠の文（原文と一致済み）に出てくるものだけを認める。
    # 原文のどこかに同じ数字があるだけでは通さない（「第3次」の3を「3社」に使う、などを防ぐ）
    if not (numbers(text) <= numbers(ev) and numbers(text) <= source_nums):
        return None
    return text


def verify(result, source):
    sn, nums = norm(source), numbers(source)
    out = {}
    for k in FIELDS:
        v = verify_field((result or {}).get(k), sn, nums)
        if v:
            out[k] = v
    checks = []
    for c in ((result or {}).get("checks") or [])[:3]:
        v = verify_field(c, sn, nums)
        if v:
            checks.append(v)
    if checks:
        out["checks"] = checks
    return out


# ---------- 公式の文章の取得 ----------
def http_get(url, timeout=40):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        enc = r.headers.get_content_charset() or "utf-8"
        return r.read().decode(enc, errors="replace")


def page_text(raw):
    raw = re.sub(r"(?is)<(script|style|noscript|nav|header|footer)[^>]*>.*?</\1>", " ", raw)
    m = re.search(r'(?is)<(main|div)[^>]+id="main_content"[^>]*>(.*)', raw)
    if m:
        raw = m.group(2)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|li|tr|h\d|div)>", "\n", raw)
    t = htmllib.unescape(re.sub(r"<[^>]+>", " ", raw))
    t = re.sub(r"[ \t　]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t).strip()
    return t[:MAX_SOURCE_CHARS]


def source_for(item):
    if str(item.get("id", "")).startswith("jg_"):
        j = json.loads(http_get(JG_DETAIL + item["id"][3:]))
        d = (j.get("result") or [{}])[0]
        parts = [d.get(k) for k in ("title", "subsidy_catch_phrase", "detail", "use_purpose", "industry",
                                    "target_number_of_employees", "subsidy_rate")]
        parts.append("補助上限額 %s円" % d["subsidy_max_limit"] if d.get("subsidy_max_limit") else "")
        parts.append("受付期間 %s から %s" % (d.get("acceptance_start_datetime"), d.get("acceptance_end_datetime")))
        return page_text("\n".join(str(p) for p in parts if p))
    return page_text(http_get(item["official_url"]))


# ---------- AI呼び出し ----------
def ask_ai(title, source):
    body = json.dumps({
        "model": MODEL, "max_tokens": 1500,
        "messages": [{"role": "user", "content": PROMPT % {"title": title, "source": source}}],
    }).encode()
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "content-type": "application/json", "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.loads(r.read().decode("utf-8"))
    text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    return json.loads(text)


# ---------- 対象の選び方 ----------
def period_end(p):
    m = re.search(r"(\d{4}-\d{2}-\d{2})\s*[～~\-]\s*(\d{4}-\d{2}-\d{2})", str(p or ""))
    return m.group(2) if m else None


def todo(items, today):
    out = []
    for i in items:
        if i.get("ai") is not None or i.get("ai_tried") or i.get("ai_fail", 0) >= 3:
            continue
        if i.get("who") and i.get("purpose") and i.get("how"):  # 人が整えた説明があるものは対象外
            continue
        end = period_end(i.get("application_period"))
        if end and end < today:  # 終了した公募は要約しない（費用の節約）
            continue
        out.append(i)
    out.sort(key=lambda i: i.get("added_at") or "", reverse=True)  # 新しいものから
    return out[:MAX_PER_RUN]


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log("ANTHROPIC_API_KEY が設定されていないため、要約は行いません。")
        return
    with open(DATA_PATH, encoding="utf-8") as f:
        data = json.load(f)
    now = datetime.datetime.now(JST)
    today = now.date().isoformat()
    ok = rejected = failed = 0
    for it in todo(data["items"], today):
        try:
            src = source_for(it)
            if len(src) < 200:
                it["ai_tried"] = today  # 文章が取れない（PDFのみ等）。今回は諦めて、次回以降は繰り返さない
                log("文章が少ないため要約しません:", it["title"][:30])
                continue
            res = verify(ask_ai(it["title"], src), src)
            it["ai_tried"] = today
            if res:
                res["model"], res["checked_at"] = MODEL, now.strftime("%Y-%m-%d %H:%M")
                it["ai"] = res
                ok += 1
            else:
                rejected += 1
                log("照合で全項目が不合格:", it["title"][:30])
        except Exception as e:
            failed += 1
            it["ai_fail"] = it.get("ai_fail", 0) + 1  # 3回続けて失敗したものは、以後は試さない
            log("要約を飛ばしました:", it.get("title", "")[:30], "/", e)
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    log("要約 合格 %d件 / 不合格 %d件 / 失敗 %d件" % (ok, rejected, failed))


if __name__ == "__main__":
    main()
