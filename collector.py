"""農水省「補助事業参加者の公募」一覧から新着を集め、data.json に足す。
- 公式ページに書いてある文字だけを写す（AIによる文章生成はしない）
- 書いていない項目は None のまま。新しいものは pending=True（人の確認前）
- 注意: 農水省ページの作りが変わると取れなくなる。最初の数日は結果を目で確認すること
"""
import json, re, datetime, html, urllib.request

BASE = "https://www.maff.go.jp"
LIST_URL = BASE + "/j/supply/hozyo/index.html"
HEADERS = {"User-Agent": "Mozilla/5.0 (subsidy-portal daily collector)"}


def get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def strip(t):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", t))).strip()


def reiwa(m):
    return "%04d-%02d-%02d" % (int(m[0]) + 2018, int(m[1]), int(m[2]))


def parse_period(page):
    text = strip(page)
    i = text.find("公募期間")
    if i < 0:
        return None
    dates = re.findall(r"令和(\d+)年(\d+)月(\d+)日", text[i:i + 150])
    if len(dates) >= 2:
        return reiwa(dates[0]) + " ～ " + reiwa(dates[1])
    return None


def main():
    try:
        with open("data.json", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {"updated": "", "items": []}
    known = {i.get("official_url") for i in data["items"]}
    today = datetime.date.today().isoformat()

    page = get(LIST_URL)
    links = re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', page, re.S)
    added = 0
    for href, label in links:
        if not re.search(r"/j/supply/hozyo/[^\"]*/?\d{6}_[^\"]*\.html", href):
            continue
        url = href if href.startswith("http") else BASE + href
        title = strip(label)
        if not title or url in known:
            continue
        try:
            period = parse_period(get(url))
        except Exception:
            period = None
        data["items"].append({
            "id": "auto_" + re.sub(r"\W", "", url.split("/hozyo/")[-1]),
            "title": title, "organization": "農林水産省", "target_roles": [],
            "prefecture": "全国", "category": "その他",
            "application_period": period, "official_url": url,
            "summary": None, "steps": None, "documents": None,
            "last_updated": today + "（自動収集）", "pending": True,
        })
        known.add(url)
        added += 1
    data["updated"] = today
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print("added:", added)


if __name__ == "__main__":
    main()
