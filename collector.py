"""公募情報を公式の情報源から集めて data.json を更新する（1日2回、GitHub Actions から実行）。

方針
- 公式ページ・公式APIに書いてある内容だけを写す。AIによる文章生成はしない（書いていない項目は None のまま）。
- 取得元
    1. 農林水産省「補助事業参加者の公募」ページの農業の一覧表（公告日・参加締切日・件名・リンク）
    2. デジタル庁 Jグランツ 公開API（国・自治体の補助金。農業関連のキーワードで検索）
- 新しく見つけたものは added_at を付ける。画面側は added_at から72時間（＝1日2回の更新で6回分）だけ「NEW」を出す。
- 取得に失敗したときは、前回のデータを壊さない。農水省の取得に失敗した場合は、最後に異常終了して知らせる。
- 標準ライブラリだけで動く。
"""
import datetime
import html as htmllib
import json
import re
import sys
import urllib.parse
import urllib.request
from html.parser import HTMLParser

BASE = "https://www.maff.go.jp"
LIST_URL = BASE + "/j/supply/hozyo/index.html"
JG_LIST = "https://api.jgrants-portal.go.jp/exp/v1/public/subsidies"
JG_DETAIL = "https://api.jgrants-portal.go.jp/exp/v1/public/subsidies/id/"
JG_KEYWORDS = ["農業", "農林水産", "農家", "就農", "スマート農業", "畜産", "園芸", "農地"]
JG_AGRI = re.compile("農|畜産|園芸|就農|酪農|果樹|水田|施設栽培|飼料|肥料")
JG_MAX_DETAIL_PER_RUN = 40
MIN_ROWS = 30  # 農水省の表がこれより少ないときは、ページの作りが変わったとみなす
HEADERS = {"User-Agent": "Mozilla/5.0 (subsidy-portal collector; twice daily)", "Accept-Language": "ja"}
JST = datetime.timezone(datetime.timedelta(hours=9))
DATA_PATH = "data.json"

JUNK_TITLE = re.compile(r"^(結果|詳細|一覧|こちら|ここ|PDF|様式|要領|公募要領|公募結果|採択結果|応募結果)")


def log(*a, **k):
    print(*a, flush=True, **k)


def get(url, params=None, timeout=40):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        enc = r.headers.get_content_charset() or "utf-8"
        return r.read().decode(enc, errors="replace")


# ---------- 農水省：農業の一覧表 ----------
class TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self._stack = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            self._stack.append({"rows": [], "row": None, "cell": None})
        elif not self._stack:
            return
        elif tag == "tr":
            self._stack[-1]["row"] = []
        elif tag in ("td", "th"):
            self._stack[-1]["cell"] = {"text": [], "href": None}
        elif tag == "a" and self._stack[-1]["cell"] is not None:
            c = self._stack[-1]["cell"]
            if c["href"] is None and a.get("href"):
                c["href"] = a["href"]
        elif tag == "br" and self._stack[-1]["cell"] is not None:
            self._stack[-1]["cell"]["text"].append(" ")

    def handle_endtag(self, tag):
        if not self._stack:
            return
        t = self._stack[-1]
        if tag in ("td", "th") and t["cell"] is not None:
            text = re.sub(r"\s+", "", "".join(t["cell"]["text"]))
            if t["row"] is not None:
                t["row"].append({"text": text, "href": t["cell"]["href"]})
            t["cell"] = None
        elif tag == "tr" and t["row"] is not None:
            if t["row"]:
                t["rows"].append(t["row"])
            t["row"] = None
        elif tag == "table":
            self.tables.append(self._stack.pop()["rows"])

    def handle_data(self, data):
        if self._stack and self._stack[-1]["cell"] is not None:
            self._stack[-1]["cell"]["text"].append(data)


def reiwa_to_iso(s):
    m = re.search(r"令和\s*(\d+|元)\s*年\s*(\d+)\s*月\s*(\d+)\s*日", s)
    if not m:
        return None
    n = 1 if m.group(1) == "元" else int(m.group(1))
    return "%04d-%02d-%02d" % (2018 + n, int(m.group(2)), int(m.group(3)))


def category(t):
    # 件名のキーワードによる当サイト独自の便宜上の区分（公式の分類ではない）
    if "施設園芸" in t or "スマートグリーンハウス" in t:
        return "施設園芸・後付け環境制御"
    if re.search("スマート|農業支援サービス|ロボット|省力化", t):
        return "省力化・スマート農業機器導入"
    if re.search("肥料|飼料|資材", t):
        return "資材高騰・コスト削減対策"
    if re.search("プラスチック|脱炭素|低炭素|有機|環境", t):
        return "環境保全・脱炭素・有機質資材"
    if re.search("人材|就農|研修|教育", t):
        return "新規就農・人財育成・教育"
    return "その他"


def slug(url):
    return "auto_" + re.sub(r"\W", "", url.split("/hozyo/")[-1])


def parse_maff(page):
    p = TableParser()
    p.feed(page)
    items = []
    for rows in p.tables:
        head = None
        for i, r in enumerate(rows):
            t = [c["text"] for c in r]
            if len(t) == 3 and "公告日" in t[0] and "締切" in t[1] and "件名" in t[2]:
                head = i
                break
        if head is None:  # 「公募結果」の表などは対象外
            continue
        for r in rows[head + 1:]:
            if len(r) != 3:
                continue
            pub, dl = reiwa_to_iso(r[0]["text"]), reiwa_to_iso(r[1]["text"])
            title = re.sub(r"NEW(アイコン)?|New!?", "", r[2]["text"]).strip()
            href = r[2]["href"]
            if not (pub and dl and title and href) or JUNK_TITLE.match(title):
                continue
            url = urllib.parse.urljoin(LIST_URL, href)
            items.append({
                "id": slug(url), "title": title, "organization": "農林水産省",
                "target_roles": [], "prefecture": "全国", "category": category(title),
                "application_period": pub + " ～ " + dl, "official_url": url,
                "summary": None, "steps": None, "documents": None, "pending": True,
                "source": "maff", "pub": pub,
            })
    return items


# ---------- Jグランツ公開API ----------
def iso_date(v):
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(v or ""))
    return m.group(0) if m else None


def yen(v):
    try:
        return "{:,}円".format(int(v)) if int(v) > 0 else None
    except Exception:
        return None


def collect_jgrants(known_urls, known_ids):
    """公式APIが返した項目だけを写す。失敗しても全体は止めない。"""
    found = {}
    for kw in JG_KEYWORDS:
        try:
            body = get(JG_LIST, {"keyword": kw, "sort": "created_date", "order": "DESC", "acceptance": "1"})
            res = json.loads(body).get("result") or []
        except Exception as e:
            log("Jグランツ 一覧取得を飛ばしました（%s）: %s" % (kw, e))
            continue
        for r in res:
            if r.get("id") and r["id"] not in found:
                found[r["id"]] = r
    log("Jグランツ 一覧で見つかった件数:", len(found))
    items, fetched = [], 0
    for sid, r in found.items():
        if "jg_" + sid in known_ids:
            continue
        if fetched >= JG_MAX_DETAIL_PER_RUN:
            break
        fetched += 1
        try:
            d = json.loads(get(JG_DETAIL + sid)).get("result") or []
            d = d[0] if d else {}
        except Exception as e:
            log("Jグランツ 詳細取得を飛ばしました（%s）: %s" % (sid, e))
            continue
        url = d.get("front_subsidy_detail_page_url")
        title = (d.get("title") or r.get("title") or "").strip()
        if not url or not title or url in known_urls:
            continue
        blob = " ".join(str(d.get(k) or "") for k in ("title", "name", "subsidy_catch_phrase"))  # 業種欄は全業種が並ぶため判定に使わない
        if not JG_AGRI.search(blob):  # 農業と関係の薄いものは載せない
            continue
        start, end = iso_date(d.get("acceptance_start_datetime")), iso_date(d.get("acceptance_end_datetime"))
        area = (d.get("target_area_search") or r.get("target_area_search") or "").strip()
        who_parts = []
        if d.get("industry"):
            who_parts.append("業種：" + str(d["industry"]))
        if d.get("target_number_of_employees"):
            who_parts.append("従業員数：" + str(d["target_number_of_employees"]))
        items.append({
            "id": "jg_" + sid, "title": title,
            "organization": (d.get("institution_name") or r.get("institution_name") or None),
            "target_roles": [], "prefecture": area or "情報なし", "category": category(title),
            "application_period": (start + " ～ " + end) if (start and end) else None,
            "official_url": url,
            "who": " ／ ".join(who_parts) or None,
            "purpose": (d.get("subsidy_catch_phrase") or None),
            "max_text": yen(d.get("subsidy_max_limit") or r.get("subsidy_max_limit")),
            "rate_text": (d.get("subsidy_rate") or None),
            "summary": None, "steps": None, "documents": None, "pending": True,
            "source": "jgrants",
        })
    return items


# ---------- 本体 ----------
def load():
    try:
        with open(DATA_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data.get("items"), list):
            raise ValueError("items がありません")
        return data
    except FileNotFoundError:
        return {"items": []}


def main():
    now = datetime.datetime.now(JST)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S+09:00")
    data = load()
    # 題名ではないリンク文字（「結果はこちらから」など）を拾った項目は取り除く
    data["items"] = [i for i in data["items"] if not JUNK_TITLE.match(str(i.get("title") or "").replace(" ", ""))]
    # Jグランツ由来で、件名・概要に農業の語が無いもの（全業種対象の汎用補助金など）は取り除く
    data["items"] = [i for i in data["items"] if not (i.get("source") == "jgrants" and not JG_AGRI.search(str(i.get("title") or "") + str(i.get("purpose") or "")))]
    by_url = {i.get("official_url"): i for i in data["items"] if i.get("official_url")}
    ids = {i.get("id") for i in data["items"]}
    added, failed = 0, False

    # 1) 農水省
    try:
        got = parse_maff(get(LIST_URL))
        if len(got) < MIN_ROWS:
            raise RuntimeError("表から取れた件数が少なすぎます（%d件）。ページの作りが変わった可能性があります" % len(got))
        for it in got:
            old = by_url.get(it["official_url"])
            if old:  # 既にあるものは、手で整えた内容を守る。期間が空のときだけ補う
                if not old.get("application_period"):
                    old["application_period"] = it["application_period"]
                continue
            it["added_at"] = stamp
            it["last_updated"] = now.strftime("%Y-%m-%d") + "（自動取得）"
            it.pop("pub", None)
            data["items"].append(it)
            by_url[it["official_url"]] = it
            ids.add(it["id"])
            added += 1
        log("農水省: 一覧 %d件 / 新規 %d件" % (len(got), added))
    except Exception as e:
        failed = True
        log("農水省の取得に失敗しました:", e, file=sys.stderr)

    # 2) Jグランツ（失敗しても全体は止めない）
    try:
        jg = collect_jgrants(set(by_url), ids)
        for it in jg:
            it["added_at"] = stamp
            it["last_updated"] = now.strftime("%Y-%m-%d") + "（Jグランツから自動取得）"
            data["items"].append(it)
        log("Jグランツ: 新規 %d件" % len(jg))
        added += len(jg)
    except Exception as e:
        log("Jグランツ全体を飛ばしました:", e)

    data["updated_at"] = now.strftime("%Y-%m-%d %H:%M")
    data["updated"] = now.strftime("%Y-%m-%d")
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    log("新規追加の合計:", added, "／掲載合計:", len(data["items"]))
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
