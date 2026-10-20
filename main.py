"""국제기구·연준 리포트 신규 발간을 확인해 텔레그램으로 보낸다.

실행:  python main.py
환경변수:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  필수 (DRY_RUN=1 이면 불필요)
  DRY_RUN=1   텔레그램 전송·상태 저장 없이 보낼 메시지를 화면에 출력
"""
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urljoin, urlsplit

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
SOURCES_FILE = ROOT / "sources.yaml"
STATE_FILE = ROOT / "state" / "seen.json"

KST = timezone(timedelta(hours=9))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
# 일부 사이트(세인트루이스 연준 등)는 브라우저답지 않은 요청을 응답 없이 붙잡아 두므로 브라우저 헤더를 갖춰 보낸다
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/rss+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}
SEEN_KEEP = 1000          # 기관별로 기억할 최근 항목 수
MAX_ITEMS_PER_SOURCE = 20  # 한 번에 기관당 보낼 최대 건수 (넘치면 "외 N건")
FAIL_WARN_AFTER = 3        # 연속 실패가 이 횟수가 되면 경고 1회 발송
TG_LIMIT = 3900            # 텔레그램 메시지 길이 제한(4096) 여유분
MAX_AGE_DAYS = 3           # 발간일이 이보다 오래된 글은 '새 글'로 알리지 않음

DRY_RUN = os.environ.get("DRY_RUN") == "1"
MONTHS = {"January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"}


# ── 수집 ──────────────────────────────────────────────
def fetch_source(src):
    """sources.yaml 항목 하나를 읽어 [{id, title, link, series}] 목록을 돌려준다 (최신순)."""
    items = fetch_by_type(src)
    for it in items:
        # series_map: 링크 주소에 들어있는 글자 → 시리즈명 (예: economic_brief → Economic Brief)
        for key, label in (src.get("series_map") or {}).items():
            if key in it["link"]:
                it["series"] = label
                break
    return items


def fetch_by_type(src):
    kind = src.get("type", "rss")
    if kind == "rss":
        items = fetch_rss(src["urls"])
        if src.get("include"):  # 링크에 이 글자(목록이면 그중 하나)가 들어간 항목만
            keys = src["include"] if isinstance(src["include"], list) else [src["include"]]
            items = [it for it in items if any(k in it["link"] for k in keys)]
        if src.get("id_with_date"):
            # 매번 같은 주소로 갱신 공지를 올리는 항목(예: 댈러스 WEI, Trimmed Mean PCE)은 발표 시각을 붙여 구분
            for it in items:
                if not re.search(r"/(19|20)\d\d", it["id"]) and it["published"]:
                    it["id"] += "|" + it["published"]
        return items[:src.get("max_items", 100)]  # 오래된 글까지 다 들어있는 피드 대비
    if kind == "page_links":
        return fetch_page_links(src)
    if kind == "wp_api":
        return fetch_wp_api(src)
    if kind == "bis_list":
        return fetch_bis_list(src)
    if kind == "imf_elibrary":
        return fetch_imf_elibrary(src)
    if kind == "bostonfed":
        return fetch_bostonfed(src["page"])
    if kind == "nyfed_research":
        return fetch_nyfed_research(src["page"])
    if kind == "datawatch":
        return fetch_datawatch(src)
    if kind == "phillyfed_search":
        return fetch_phillyfed_search(src["page"])
    if kind == "clevelandfed_list":
        return fetch_clevelandfed_list(src["page"])
    if kind == "atlantafed_list":
        return fetch_atlantafed_list(src["page"])
    if kind == "chicagofed_series":
        return fetch_chicagofed_series(src["series_id"])
    raise ValueError(f"알 수 없는 type: {kind}")


def fetch_datawatch(src):
    """데이터 업데이트 감지. 'watch' 방식에 따라 지문(fingerprint)을 만들어 항목으로 돌려준다.

      head: [url, ...]           파일의 Last-Modified/ETag 헤더가 바뀌면 업데이트 (큰 파일용)
      files: [url, ...]          파일 내용이 바뀌면 업데이트 (여러 개면 합쳐서 하나의 지문)
                                 주소에 {quarter}/{prev_quarter} 를 쓰면 현재·직전 분기로 자동 치환
      regex: {url, pattern}      페이지에서 처음 일치하는 글자가 바뀌면 업데이트 (예: 'second quarter of 2026')
      links: {url, pattern}      페이지에 패턴과 일치하는 새 링크가 생기면 업데이트 (링크마다 1건)
    """
    w, title, page = src["watch"], src["title"], src["page"]
    if "head" in w:
        # 파일을 받지 않고 서버가 알려주는 수정 시각(Last-Modified)/ETag 로만 판별 — 큰 파일용
        marks = []
        for url in w["head"]:
            r = requests.head(url, headers=HEADERS, timeout=60, allow_redirects=True)
            r.raise_for_status()
            mark = r.headers.get("Last-Modified") or r.headers.get("ETag")
            if not mark:
                raise ValueError(f"Last-Modified/ETag 헤더 없음: {url}")
            marks.append(mark)
        return [{"id": "head:" + " | ".join(marks), "title": f"{title} 업데이트",
                 "link": page, "series": ""}]
    if "files" in w:
        h, got = hashlib.sha256(), 0
        for url in expand_quarters(w["files"]):
            r = requests.get(url, headers=HEADERS, timeout=60)
            if r.status_code == 404 and "{" in str(w["files"]):
                continue  # 분기 이름으로 만든 주소는 아직 파일이 없을 수 있음
            r.raise_for_status()
            h.update(r.content)
            got += 1
        if not got:
            raise ValueError("받은 파일 0개")
        return [{"id": "sha256:" + h.hexdigest()[:20], "title": f"{title} 업데이트",
                 "link": page, "series": ""}]
    if "regex" in w:
        # 태그 사이에 끼어 있는 글자도 잡히도록 화면에 보이는 글자만 모아서 검사
        visible = " ".join(BeautifulSoup(http_get(w["regex"]["url"]).text, "html.parser")
                           .get_text(" ", strip=True).split())
        m = re.search(w["regex"]["pattern"], visible, re.I)
        if not m:
            raise ValueError("업데이트 표시 문구를 찾지 못함 (페이지 구조 변경?)")
        found = " ".join(m.group(0).split())
        return [{"id": "text:" + found, "title": f"{title} 업데이트 ({found})",
                 "link": page, "series": ""}]
    if "links" in w:
        url = w["links"]["url"]
        hrefs = re.findall(r'href="([^"]*' + w["links"]["pattern"] + r'[^"]*)"', http_get(url).text, re.I)
        links = list(dict.fromkeys(urljoin(url, html.unescape(x)) for x in hrefs))
        if not links:
            raise ValueError("일치하는 링크 0건 (페이지 구조 변경?)")
        return [{"id": l, "title": f"{title}: {l.rstrip('/').rsplit('/', 1)[-1]}",
                 "link": l, "series": ""} for l in links]
    raise ValueError("watch 에 files / regex / links 중 하나가 필요")


def fetch_phillyfed_search(page):
    """필라델피아 연준 검색 목록 페이지.

    검색 결과가 HTML 안의 FRBP.extend(... 'search-results' ...) 스크립트에 JSON(data: {...})으로
    들어있다. 기본 정렬이 최신순이고 pageSize 로 건수를 늘릴 수 있다.
    """
    sep = "&" if "?" in page else "?"
    text = http_get(page + sep + "pageSize=30").text
    start = text.find("data: {", text.find("'search-results'"))
    if start < 0:
        raise ValueError("검색 결과 데이터를 찾지 못함 (페이지 구조 변경?)")
    data, _ = json.JSONDecoder().raw_decode(text, start + len("data: "))
    items = []
    for r in data.get("results") or []:
        a = r.get("attributes") or {}
        if r.get("type") == "About" or not a.get("url"):  # 소개 페이지 등 발간물이 아닌 것 제외
            continue
        link = urljoin("https://www.philadelphiafed.org", a["url"])
        items.append({"id": r.get("id") or link, "title": " ".join((a.get("title") or "").split()),
                      "link": link, "series": r.get("type") or ""})
    if not items:
        raise ValueError("항목 0건 (페이지 구조 변경?)")
    return items


def fetch_clevelandfed_list(page):
    """클리블랜드 연준 출판물 목록 페이지 (Economic Commentary, Working Paper 등).

    최신순 결과 10건이 <search-results data-initialize="{...}"> 속성에 JSON 으로 들어있다.
    """
    soup = BeautifulSoup(http_get(page).text, "html.parser")
    tag = soup.find("search-results", attrs={"data-initialize": True})
    if tag is None:
        raise ValueError("<search-results> 데이터를 찾지 못함 (페이지 구조 변경?)")
    results = json.loads(tag["data-initialize"]).get("results") or []
    items = []
    for r in results:
        if not r.get("url"):
            continue
        link = urljoin("https://www.clevelandfed.org", r["url"])
        items.append({"id": link, "title": " ".join((r.get("title") or "").split()),
                      "link": link, "series": r.get("issue") or r.get("publication") or "",
                      "date": parse_date(r.get("sortDate"))})
    if not items:
        raise ValueError("항목 0건 (페이지 구조 변경?)")
    return items


def fetch_atlantafed_list(page):
    """애틀랜타 연준 출판물 시리즈 페이지 (Working Papers, Macroblog 등).

    목록이 HTML 안의 'var feed_<해시> = [ {Title, Date, Url, ...}, ... ]' 스크립트 변수에 JSON 으로 들어있다.
    (애틀랜타 RSS 는 일부가 몇 달씩 갱신이 멈춰 있어 페이지를 직접 읽는다)
    """
    text = http_get(page).text
    items = []
    for m in re.finditer(r"var feed_\w+\s*=\s*", text):
        arr, _ = json.JSONDecoder().raw_decode(text, m.end())
        for r in arr:
            link = r.get("Url") or ""
            if not link:
                continue
            link = urljoin("https://www.atlantafed.org", link)
            items.append({"id": link, "title": " ".join((r.get("Title") or "").split()),
                          "link": link, "series": "", "date": parse_date(r.get("Date"))})
    if not items:
        raise ValueError("목록 데이터(var feed_...)를 찾지 못함 (페이지 구조 변경?)")
    return items


def fetch_chicagofed_series(series_id):
    """시카고 연준 출판물 시리즈 (Chicago Fed Letter, Working Papers 등).

    목록 페이지가 POST /forms/NewsFeed/Index?seriesIds=<시리즈ID>&page=1 로 최신 10건을 JSON 으로 받아온다.
    시리즈ID 는 각 시리즈 목록 페이지 HTML 의 seriesIds 값. (시카고 RSS 는 2023~24년에 멈춰 있음)
    """
    r = requests.post("https://www.chicagofed.org/forms/NewsFeed/Index",
                      params={"seriesIds": series_id, "page": 1},
                      headers={**HEADERS, "X-Requested-With": "XMLHttpRequest"}, timeout=30)
    r.raise_for_status()
    items = []
    for x in r.json() or []:
        if not x.get("Url"):
            continue
        title = BeautifulSoup(x.get("Title") or "", "html.parser").get_text()  # 제목에 <i> 태그가 섞여 있음
        items.append({"id": x.get("Id") or x["Url"], "title": " ".join(title.split()),
                      "link": x["Url"], "series": (x.get("Series") or {}).get("Name") or ""})
    if not items:
        raise ValueError("항목 0건 (시리즈ID 변경?)")
    return items


def fetch_page_links(src):
    """범용: 서버에서 완성된 HTML 로 오는 목록 페이지에서, 주소가 link_pattern(정규식)과 맞는 링크를 글로 본다.

    link_pattern 은 href 에 대해 검사하고, 글자 수가 min_title(기본 15)자 미만인 링크(사람 이름·'더보기' 등)는 버린다.
    """
    page = src["page"]
    soup = BeautifulSoup(http_get(page).text, "html.parser")
    root = soup.find("main") or soup
    items, seen = [], set()
    for a in root.find_all(["a", "mnt-link"], href=True):  # mnt-link: 캔자스시티 연준 사이트의 링크 태그
        title = " ".join(a.get_text().split())
        if not re.search(src["link_pattern"], a["href"]) or len(title) < src.get("min_title", 15):
            continue
        link = urljoin(page, a["href"]).split("#")[0]
        if link in seen:
            continue
        seen.add(link)
        card = a.find_parent(["mnt-card", "article", "li"])
        t = card.find("time") if card else None
        items.append({"id": link, "title": title, "link": link, "series": "",
                      "date": parse_date((t.get("datetime") or t.get_text()) if t else None)})
    if not items:
        raise ValueError("일치하는 링크 0건 (페이지 구조 변경?)")
    return items


def fetch_imf_elibrary(src):
    """IMF eLibrary 최신 발간 목록 (imf.org 의 Latest Releases 와 같은 내용, 최신순 50건/쪽).

    imf.org 는 자동 접속을 막고 공식 RSS 도 없어져서(404) eLibrary 를 쓴다.
    책(WEO·GFSR 등)은 장(chapter)·번역판이 따로 올라오므로 영문 책 본문만 남기고,
    exclude_title(정규식)에 맞는 제목은 버린다 (예: IMF Terminology Bulletin).
    """
    items, seen = [], set()
    for pg in range(1, src.get("pages", 2) + 1):
        url = f"https://www.elibrary.imf.org/search?sort=datedescending&pageSize=50&page={pg}"
        soup = BeautifulSoup(http_get(url).text, "html.parser")
        for a in soup.select("h2 a[href*='result=']"):
            path = a["href"].split("?")[0]
            title = " ".join(a.get_text().split())
            if path in seen:
                continue
            seen.add(path)
            if "/display/book/" in path:
                isbn = path.split("/display/book/")[1].split("/")[0]
                if not path.endswith(f"/{isbn}.xml"):   # 책의 장(chapter) 단위 항목
                    continue
                if re.search(r"[À-ÿ]|\b(de 20\d\d|Octubre|Outubro|Octobre|Abril|Avril|Enero|Janvier|Janeiro)\b", title, re.I):
                    continue  # 스페인어·포르투갈어·프랑스어 번역판 (보고서류는 주소가 -en.xml 이라 영문만 옴)
            if re.search(r"[^\x00-\u024F\u2000-\u206F\u20AC]", title):  # 라틴 문자 외(번역판) 제외
                continue
            if src.get("exclude_title") and re.search(src["exclude_title"], title):
                continue
            link = urljoin("https://www.elibrary.imf.org", path)
            items.append({"id": link, "title": title, "link": link, "series": ""})
    if not items:
        raise ValueError("항목 0건 (페이지 구조 변경?)")
    return items


def fetch_bis_list(src):
    """BIS 'All publications' 목록 (연구물 + BCBS·CPMI 등 기준서·보고서). 카드마다 유형 배지·제목·날짜가 있다.

    한 페이지 약 10건이라 pages 쪽수(기본 2)만큼 함께 본다.
    """
    items, seen = [], set()
    for n in range(src.get("pages", 2)):
        url = src["page"] + (f"?page={n}" if n else "")
        soup = BeautifulSoup(http_get(url).text, "html.parser")
        for a in soup.select("a.card-link[href]"):
            head = a.select_one(".card-heading")
            if not head or not a.select_one(".card-date"):  # 날짜 없는 카드는 안내 페이지(저자 소개 등)
                continue
            link = urljoin("https://www.bis.org", a["href"])
            if link in seen:
                continue
            seen.add(link)
            badges = [" ".join(b.get_text().split()) for b in a.select(".card-header .badge")]
            series = " ".join(badges[1:3]) if len(badges) > 1 else ""  # 예: "BIS Working Paper No 1384"
            items.append({"id": link, "title": " ".join(head.get_text().split()), "link": link, "series": series,
                          "date": parse_date(a.select_one(".card-date").get_text())})
    if not items:
        raise ValueError("항목 0건 (페이지 구조 변경?)")
    return items


def fetch_wp_api(src):
    """워드프레스 공개 REST API (/wp-json/wp/v2/<post_type>) — 샌프란시스코 연준 등.

      mode: new       새 글(발행일 최신순) — 글 하나가 항목 하나
      mode: modified  글의 수정 시각이 바뀌면 업데이트 — 데이터 페이지용 (항목 id = 글id|수정시각)
      series_after: 링크에서 이 경로 다음 단어를 시리즈명으로 (예: /publications/economic-letter/… → Economic Letter)
    """
    modified = src.get("mode") == "modified"
    r = requests.get(f"{src['api']}/wp-json/wp/v2/{src['post_type']}", headers=HEADERS, timeout=40,
                     params={"per_page": src.get("per_page", 30), "order": "desc",
                             "orderby": "modified" if modified else "date",
                             "_fields": "id,date,modified,link,title"})
    r.raise_for_status()
    items = []
    for x in r.json():
        title = " ".join(BeautifulSoup(x["title"]["rendered"], "html.parser").get_text().split())
        series = ""
        if src.get("series_after") and src["series_after"] in x["link"]:
            seg = x["link"].split(src["series_after"], 1)[1].split("/")[0]
            series = seg.replace("-", " ").title()
        if modified:
            items.append({"id": f"{x['id']}|{x['modified']}", "title": f"{title} 업데이트",
                          "link": x["link"], "series": series})
        else:
            items.append({"id": str(x["id"]), "title": title, "link": x["link"], "series": series,
                          "date": parse_date(x.get("date"))})
    if not items:
        raise ValueError("항목 0건")
    return items


def parse_date(raw):
    """여러 사이트의 날짜 표기를 date 로. 일(day)까지 없으면(예: 2026년 10월) None — 판단에서 빼기 위함."""
    if not raw:
        return None
    raw = " ".join(str(raw).split())
    for fmt, n in (("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m-%d", 10), ("%B %d, %Y", None), ("%b %d, %Y", None),
                   ("%d %b %Y", None), ("%d %B %Y", None)):
        try:
            return datetime.strptime(raw[:n] if n else raw, fmt).date()
        except ValueError:
            continue
    return None


def expand_quarters(urls):
    """주소 안의 {quarter} / {prev_quarter} 를 현재·직전 분기(예: 2026Q4 / 2026Q3)로 바꾼다."""
    today = datetime.now(KST)
    q = (today.month - 1) // 3 + 1
    cur = f"{today.year}Q{q}"
    prev = f"{today.year}Q{q - 1}" if q > 1 else f"{today.year - 1}Q4"
    return [u.replace("{quarter}", cur).replace("{prev_quarter}", prev) for u in urls]


def http_get(url):
    if urlsplit(url).hostname in BROWSER_HOSTS:
        return browser_get(url)
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r


# ── 실제 브라우저(헤드리스 크롬)로 여는 사이트 ─────────────────
# 캔자스시티 연준처럼 단순 요청은 붙잡아 두고 실제 브라우저만 통과시키는 사이트용 (robots.txt 로 수집이 허용된 페이지만).
# sources.yaml 의 browser_hosts 에 적은 호스트만 이 방식으로 연다.
BROWSER_HOSTS = set()
_browser = {}


class BrowserResponse:
    def __init__(self, status, content):
        self.status_code, self.content = status, content
        self.text = content.decode("utf-8", "replace")


def browser_get(url):
    if "page" not in _browser:
        from playwright.sync_api import sync_playwright  # 필요할 때만 불러옴 (로컬엔 없어도 됨)
        pw = sync_playwright().start()
        _browser["pw"], _browser["b"] = pw, pw.chromium.launch()
        _browser["page"] = _browser["b"].new_page(user_agent=UA)
    resp = _browser["page"].goto(url, timeout=60000, wait_until="domcontentloaded")
    if resp is None or resp.status >= 400:
        raise RuntimeError(f"브라우저 접속 실패 {resp.status if resp else '-'}: {url}")
    time.sleep(1)  # 연속 접속 간격
    try:
        body = resp.body()
    except Exception:  # noqa: BLE001 - 리다이렉트 등으로 원 응답 본문을 못 읽으면 다 그려진 화면 내용을 쓴다
        page = _browser["page"]
        page.wait_for_load_state("networkidle", timeout=30000)
        body = page.content().encode("utf-8")
    return BrowserResponse(resp.status, body)


def close_browser():
    if "b" in _browser:
        _browser["b"].close()
        _browser["pw"].stop()


def fetch_rss(urls):
    """urls 를 차례로 시도해 처음 성공한 피드의 항목 목록을 돌려준다."""
    errors = []
    for url in urls:
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            feed = feedparser.parse(r.content)
            if not feed.entries:
                raise ValueError("항목 0건")
            return [to_item(e) for e in feed.entries]
        except Exception as e:  # noqa: BLE001 - 기관 하나 실패해도 나머지는 계속
            errors.append(f"{url} → {type(e).__name__}: {str(e)[:120]}")
    raise RuntimeError(" | ".join(errors))


def fetch_bostonfed(page):
    """보스턴 연준 'Publications & Data' 검색 페이지.

    화면은 자바스크립트로 그려지고, 실제 목록은 /api/pubsanddata/publications 에서 온다.
    page 주소의 '#/search?...' 필터를 그대로 넘겨 최신순 50건을 받는다.
    """
    fragment = urlsplit(page).fragment            # "/search?siteTpc=...&topic=..."
    params = {k: v for k, v in parse_qsl(fragment.partition("?")[2]) if v}
    params.update({"yrFrom": "", "yrTo": "", "d": "false", "dt": "false",
                   "srt": "0", "pgSz": "50", "pgN": "1"})  # srt=0: Newest
    r = requests.post("https://www.bostonfed.org/api/pubsanddata/publications",
                      params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    pubs = r.json().get("publications") or []
    if not pubs:
        raise ValueError("항목 0건")
    return [{
        "id": p["id"],
        "title": " ".join((p.get("title") or "(제목 없음)").split()),
        "link": urljoin("https://www.bostonfed.org", p.get("url") or ""),
        "series": ((p.get("series") or {}).get("name") or "").strip(),
    } for p in pubs]


def fetch_nyfed_research(page):
    """뉴욕 연준 Research 메인의 'Recent Publications' 칸 (Staff Reports, EPR 등 최근 8건).

    항목 아래 줄 예: "저자1 and 저자2, Staff Report 1211, October 2026" → 시리즈 "Staff Report"
    """
    r = requests.get(page, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    head = next((h for h in soup.select("div.ts-page-head-b")
                 if "Recent Publications" in h.get_text()), None)
    if head is None:
        raise ValueError("'Recent Publications' 칸을 찾지 못함 (페이지 구조 변경?)")
    box = head.find_parent("div", class_="frb-feature-content-wrapper")
    items = []
    for h in box.select("div.ts-story-headline"):
        a = h.find("a", href=True)
        if not a:
            continue
        meta = h.find_next_sibling("div", class_="catItalic")
        found = [s for s in re.findall(r"[,.]\s*([A-Z][A-Za-z’' ]+?)\s+\d",
                                       meta.get_text(" ", strip=True) if meta else "")
                 if s not in MONTHS]  # ", October 2026" 같은 날짜 부분 제외
        link = urljoin(page, a["href"])
        items.append({"id": link, "title": " ".join(a.get_text().split()),
                      "link": link, "series": found[0] if found else ""})
    if not items:
        raise ValueError("항목 0건 (페이지 구조 변경?)")
    return items


def to_item(entry):
    link = entry.get("link", "")
    series = ""
    # Fed in Print 는 bibo:series / ebucore:publicationChannel 에 시리즈명이 들어있다
    for key, val in entry.items():
        if isinstance(val, str) and ("series" in key or "publicationchannel" in key.lower()):
            series = val.strip()
            break
    return {
        "id": entry.get("id") or link,
        "title": " ".join(entry.get("title", "(제목 없음)").split()),
        "link": link,
        "series": series,
        "published": entry.get("published", ""),
        "date": (datetime(*(entry.get("published_parsed") or entry.get("updated_parsed"))[:6]).date()
                 if (entry.get("published_parsed") or entry.get("updated_parsed")) else None),
    }


# ── 텔레그램 ──────────────────────────────────────────
def send(text):
    if DRY_RUN:
        print("─" * 60 + "\n" + text)
        return
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    for attempt in range(3):
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=30,
        )
        if r.status_code == 429:  # 전송 속도 제한 → 안내된 시간만큼 대기 후 재시도
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        if not r.ok:
            raise RuntimeError(f"텔레그램 전송 실패 {r.status_code}: {r.text[:200]}")
        time.sleep(1.5)
        return
    raise RuntimeError("텔레그램 전송 실패: 속도 제한 반복")


def send_blocks(header, blocks):
    """여러 블록을 길이 제한에 맞춰 나눠서 보낸다."""
    msg = header
    for block in blocks:
        if len(msg) + len(block) + 2 > TG_LIMIT and msg != header:
            send(msg)
            msg = header + " (계속)"
        msg += "\n\n" + block
    if msg not in (header, header + " (계속)"):
        send(msg)


def render_source(name, items, icon="🏛"):
    lines = [f"{icon} <b>{html.escape(name)}</b>"]
    for it in items[:MAX_ITEMS_PER_SOURCE]:
        tag = f"<i>[{html.escape(it['series'])}]</i> " if it["series"] else ""
        lines.append(f"• {tag}<a href=\"{html.escape(it['link'], quote=True)}\">"
                     f"{html.escape(it['title'])}</a>")
    if len(items) > MAX_ITEMS_PER_SOURCE:
        lines.append(f"  … 외 {len(items) - MAX_ITEMS_PER_SOURCE}건")
    return "\n".join(lines)


# ── 상태 ──────────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    if DRY_RUN:
        return
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


# ── 메인 ──────────────────────────────────────────────
def main():
    config = yaml.safe_load(SOURCES_FILE.read_text(encoding="utf-8"))
    BROWSER_HOSTS.update(config.get("browser_hosts") or [])
    sources = [s for s in config["sources"] if s.get("enabled", True)]
    state = load_state()

    seeded, failed, warnings = [], [], []
    new_by_name = {}  # 표시 이름별 신규 항목 (같은 이름의 여러 대상을 한 블록으로 묶음)
    pending = {}  # 전송 성공 후에 seen 에 반영할 id

    for src in sources:
        sid = src["id"]
        name = src["name"] + (f" · {src['title']}" if src.get("title") else "")
        st = state.setdefault(sid, {"seen": [], "fails": 0})
        try:
            items = fetch_source(src)
        except Exception as e:  # noqa: BLE001
            st["fails"] = st.get("fails", 0) + 1
            st["last_error"] = str(e)[:300]  # GitHub 서버에서만 실패할 때 원인을 seen.json 에서 확인용
            failed.append(f"{name}: {e}")
            print(f"[실패] {name} ({st['fails']}회 연속) {e}", file=sys.stderr)
            if st["fails"] == FAIL_WARN_AFTER:
                warnings.append(f"⚠️ <b>{html.escape(name)}</b> 피드가 {FAIL_WARN_AFTER}회 연속 "
                                f"수집에 실패했어요.\n<code>{html.escape(str(e)[:300])}</code>")
            continue

        st["fails"] = 0
        st.pop("last_error", None)
        for it in items:  # sources.yaml 에 series 를 적어두면 시리즈명이 없는 항목에 붙인다
            it["series"] = it["series"] or src.get("series", "")
        ids = [it["id"] for it in items]
        if not st["seen"]:
            # 처음 연결된 기관은 기존 항목을 '본 것'으로만 기록하고 보내지 않는다
            st["seen"] = ids[:SEEN_KEEP]
            seeded.append(name if src.get("type") == "datawatch" and len(items) == 1
                          else f"{name} ({len(items)}건)")
            print(f"[초기화] {name}: {len(items)}건")
            continue

        seen = set(st["seen"])
        new = [it for it in items if it["id"] not in seen]
        # '최근 올라온 글만' — 발간일을 아는 항목은 max_age_days(기본 3)일보다 오래됐으면 알리지 않고 기록만 한다.
        # (사이트가 캐시된 옛 목록을 잠깐 내려줘서 지난 글이 새 글처럼 보이는 경우 등)
        cutoff = datetime.now(KST).date() - timedelta(days=src.get("max_age_days", MAX_AGE_DAYS))
        stale = [it for it in new if it.get("date") and it["date"] < cutoff]
        if stale:
            print(f"[무시] {name}: 발간일 {cutoff} 이전 글 {len(stale)}건")
            st["seen"] = ([it["id"] for it in stale] + st["seen"])[:SEEN_KEEP]
            new = [it for it in new if it not in stale]
        print(f"[확인] {name}: 신규 {len(new)}건 / 피드 {len(items)}건")
        if new:
            icon = "📊" if src.get("type") == "datawatch" or src.get("kind") == "data" else "🏛"
            new_by_name.setdefault((icon, src["name"]), []).extend(new)
            pending[sid] = [it["id"] for it in new]

    now = datetime.now(KST).strftime("%m/%d %H:%M")

    if seeded:
        text = (f"📡 <b>구독 연결 완료</b> ({now} KST)\n"
                "지금부터 새로 올라오는 리포트를 알려드릴게요.\n\n"
                + "\n".join(f"✅ {html.escape(s)}" for s in seeded))
        if failed:
            text += "\n\n" + "\n".join(f"❌ {html.escape(f[:200])}" for f in failed)
        send(text)

    for w in warnings:
        send(w)

    if new_by_name:
        blocks = [render_source(name, items, icon) for (icon, name), items in new_by_name.items()]
        send_blocks(f"🌐 <b>새 리포트·데이터</b> ({now} KST)", blocks)

    # 전송까지 끝난 것만 seen 에 반영 (전송 실패 시 다음 실행에서 다시 보냄)
    for sid, new_ids in pending.items():
        state[sid]["seen"] = (new_ids + state[sid]["seen"])[:SEEN_KEEP]
    save_state(state)


if __name__ == "__main__":
    try:
        main()
    finally:
        close_browser()
