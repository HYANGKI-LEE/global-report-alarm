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
    if src.get("exclude"):  # exclude: 링크에 이 글자(목록이면 그중 하나)가 들어간 항목은 뺌
        keys = src["exclude"] if isinstance(src["exclude"], list) else [src["exclude"]]
        items = [it for it in items if not any(k in it["link"] for k in keys)]
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
    if kind == "gmail":
        return fetch_gmail(src)
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
        if src.get("strip_query"):  # 추적용 ?cid=… 등이 붙어 같은 글이 다른 주소로 보이는 사이트
            link = link.split("?")[0]
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


# ── 메일 중계 (사이트가 클라우드 서버 접속을 막는 기관: IMF, 세인트루이스 연준) ──────────
# 봇 전용 Gmail 이 기관의 공식 이메일 알림을 구독하고, 봇은 IMAP 으로 최근 메일만 읽는다(읽음 처리 안 함).
# GitHub Secrets: GMAIL_ADDRESS, GMAIL_APP_PASSWORD (Google 앱 비밀번호)
_imap = {}
SKIP_LINK = re.compile(r"unsubscribe|preference|privacy|mailto:|view.{0,20}browser|manage|subscri|"
                       r"facebook|twitter|x\.com|linkedin|youtube|instagram|bsky", re.I)


def fetch_gmail(src):
    """from 에 적은 주소(도메인)에서 최근 max_age_days 안에 온 메일을 글 단위로 돌려준다.

    여러 글이 묶인 뉴스레터는 본문의 글 링크(링크 글자 25자 이상)를 하나씩 항목으로 만들고,
    그런 링크가 없으면 메일 제목 하나를 항목으로 만든다.
    """
    import email
    import imaplib
    from email.header import decode_header, make_header
    from email.utils import parsedate_to_datetime

    if "conn" not in _imap:
        addr, pw = os.environ.get("GMAIL_ADDRESS"), os.environ.get("GMAIL_APP_PASSWORD")
        if not addr or not pw:
            raise RuntimeError("GMAIL_ADDRESS / GMAIL_APP_PASSWORD 가 설정되지 않음")
        conn = imaplib.IMAP4_SSL("imap.gmail.com")
        conn.login(addr, pw.replace(" ", ""))
        # '전체 메일' 폴더(받은편지함·프로모션 탭 구분 없이)는 언어마다 이름이 달라서(\All 표시로 찾음)
        # 예: [Gmail]/All Mail, [Gmail]/전체보관함 — 못 찾으면 받은편지함
        folder = "INBOX"
        typ, boxes = conn.list()
        for line in boxes or []:
            line = line.decode("utf-8", "replace")
            if r"\All" in line:
                folder = line.rsplit(' "/" ', 1)[-1].strip()
                break
        typ, _ = conn.select(folder, readonly=True)  # 읽기 전용 — 메일을 읽음 처리하지 않음
        if typ != "OK":
            raise RuntimeError(f"메일함 열기 실패: {folder}")
        _imap["conn"] = conn
    conn = _imap["conn"]

    since = (datetime.now(KST) - timedelta(days=src.get("max_age_days", MAX_AGE_DAYS))).strftime("%d-%b-%Y")
    items = []
    for sender in src["from"]:
        typ, data = conn.search(None, "SINCE", since, "FROM", f'"{sender}"')
        for num in (data[0].split() if typ == "OK" and data and data[0] else []):
            typ, msg_data = conn.fetch(num, "(BODY.PEEK[])")
            if typ != "OK":
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            subject = " ".join(str(make_header(decode_header(msg.get("Subject", "")))).split())
            msg_id = msg.get("Message-ID", "").strip() or f"{sender}|{subject}"
            try:
                date = parsedate_to_datetime(msg.get("Date")).astimezone(KST).date()
            except Exception:  # noqa: BLE001
                date = None
            html_part = next((part for part in msg.walk() if part.get_content_type() == "text/html"), None)
            links = []
            if html_part is not None:
                body = html_part.get_payload(decode=True).decode(html_part.get_content_charset() or "utf-8", "replace")
                for a in BeautifulSoup(body, "html.parser").find_all("a", href=True):
                    text = " ".join(a.get_text().split())
                    if (len(text) >= 25 and a["href"].startswith("http")
                            and not text.lower().startswith(("http", "www."))      # 주소만 적힌 링크(메일 하단 공통 링크)
                            and urlsplit(a["href"]).path.strip("/") != ""            # 사이트 홈 링크
                            and not SKIP_LINK.search(a["href"] + " " + text)):
                        if a["href"] not in [l for _, l in links]:
                            links.append((text, a["href"]))
            if links:
                for text, href in links[:src.get("max_links", 15)]:
                    items.append({"id": f"{msg_id}|{text}", "title": text, "link": href,
                                  "series": subject[:60], "date": date})
            else:
                items.append({"id": msg_id, "title": subject, "link": src["page"], "series": "", "date": date})
    if not items and src.get("allow_empty", True):
        return []
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
RENDER_HOSTS = set()   # 이 호스트는 화면이 다 그려진 뒤(자바스크립트로 불러오는 목록 포함)의 내용을 읽음
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
        page = _browser["b"].new_page(user_agent=UA)
        # 글 목록만 필요하므로 이미지·글꼴·동영상은 받지 않음 (페이지가 훨씬 빨리 열림)
        page.route("**/*", lambda route: route.abort()
                   if route.request.resource_type in ("image", "font", "media") else route.continue_())
        _browser["page"] = page
    resp = _browser["page"].goto(url, timeout=60000, wait_until="domcontentloaded")
    if resp is None or resp.status >= 400:
        raise RuntimeError(f"브라우저 접속 실패 {resp.status if resp else '-'}: {url}")
    time.sleep(0.3)  # 연속 접속 간격
    if urlsplit(url).hostname in RENDER_HOSTS:
        page = _browser["page"]
        page.wait_for_load_state("networkidle", timeout=45000)
        time.sleep(2)
        return BrowserResponse(resp.status, page.content().encode("utf-8"))
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


# ── 3줄 요약·번역 (Claude API) ─────────────────────────
# GitHub Secrets 의 ANTHROPIC_API_KEY 가 있을 때만 동작. 없거나 실패하면 요약 없이 제목만 보낸다.
SUMMARY_MODEL = "claude-opus-5-5"
SUMMARY_LIMIT = 40          # 1회 실행당 최대 요약 건수 (비용 상한)
SUMMARY_SYSTEM = (
    "너는 중앙은행·국제기구 리포트를 한국 금융시장 실무자에게 소개하는 애널리스트다. "
    "주어진 글(또는 데이터 페이지)의 핵심을 한국어 불릿 정확히 3개로 요약·번역한다. "
    "각 불릿은 한 문장, 60자 안팎. 숫자·방향(상승/하락)·결론을 우선하고, 원문에 없는 내용은 쓰지 않는다. "
    "본문이 거의 없으면 제목과 설명에서 알 수 있는 범위로만 쓴다."
)


def page_text(link):
    """요약 재료: 메타 설명 + 본문 앞부분 (PDF·접속 실패는 빈 문자열)."""
    if re.search(r"\.(pdf|xlsx?|csv|zip)(\?|$)", link, re.I):
        return ""
    try:
        soup = BeautifulSoup(http_get(link).text, "html.parser")
    except Exception:  # noqa: BLE001
        return ""
    metas = [m.get("content", "") for m in soup.find_all("meta")
             if (m.get("name") or m.get("property") or "").lower() in ("description", "og:description")]
    for t in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        t.decompose()
    body = soup.find("main") or soup.find("article") or soup.body or soup
    text = " ".join(body.get_text(" ", strip=True).split())
    return ("\n".join(dict.fromkeys(m for m in metas if m)) + "\n\n" + text)[:8000]


def summarize_all(new_by_name):
    """리포트(🏛) 글마다 제목 아래 붙일 불릿을 채운다. 데이터(📊) 알림은 제목만.

    - 기본: 글 페이지의 공식 소개문(초록) 앞 1~3문장
      → DEEPL_API_KEY 가 있으면 DeepL 로 한국어 번역 (DeepL API Free: 월 50만 자 무료), 없으면 원문 그대로
    - ANTHROPIC_API_KEY 가 있으면: Claude 로 한국어 3줄 요약·번역 (유료, 현재 미사용)
    """
    items = [it for (icon, _), its in new_by_name.items() if icon != "📊" for it in its][:SUMMARY_LIMIT]
    key = os.environ.get("ANTHROPIC_API_KEY")
    client = None
    if key:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
    for it in items:
        try:
            if client:
                it["summary"] = summarize(client, it)
            else:
                it["summary"] = excerpt(it)
                if it["summary"]:
                    ko = translate_ko(it["summary"])
                    # 번역되면 한국어 기준 140자, 원문(번역 실패·키 없음)이면 220자에서 자름
                    it["summary"] = clip(ko, 140) if ko != it["summary"] else clip(ko, 220)
        except Exception as e:  # noqa: BLE001 - 요약 실패해도 알림은 보낸다
            print(f"[요약 실패] {it['title'][:40]}: {type(e).__name__}: {str(e)[:150]}", file=sys.stderr)


def translate_ko(texts):
    """DeepL 로 영어 문장들을 한국어로. 키가 없거나 한도 초과(456)·오류면 원문 그대로 돌려준다."""
    key = os.environ.get("DEEPL_API_KEY", "").strip()
    if not key or not texts:
        return texts
    host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"  # 무료 키는 :fx 로 끝남
    try:
        r = requests.post(f"https://{host}/v2/translate",
                          headers={"Authorization": f"DeepL-Auth-Key {key}"},
                          data=[("text", t) for t in texts] + [("source_lang", "EN"), ("target_lang", "KO")],
                          timeout=30)
        if r.status_code == 456:
            print("[번역] DeepL 월 한도 초과 — 이번 달은 원문으로 보냄", file=sys.stderr)
            return texts
        r.raise_for_status()
        out = [t.get("text", "") for t in r.json().get("translations", [])]
        return out if len(out) == len(texts) and all(out) else texts
    except Exception as e:  # noqa: BLE001
        print(f"[번역 실패] {type(e).__name__}: {str(e)[:150]}", file=sys.stderr)
        return texts


def excerpt(it):
    """무료 방식: 페이지 메타 설명(초록)의 앞 1~3문장. 메타 설명이 없으면 None."""
    if re.search(r"\.(pdf|xlsx?|csv|zip)(\?|$)", it["link"], re.I):
        return None
    try:
        soup = BeautifulSoup(http_get(it["link"]).text, "html.parser")
    except Exception:  # noqa: BLE001
        return None
    desc = ""
    for m in soup.find_all("meta"):
        if (m.get("name") or m.get("property") or "").lower() in ("description", "og:description"):
            c = " ".join(BeautifulSoup(m.get("content") or "", "html.parser").get_text(" ").split())  # <cite> 등 태그 제거
            if len(c) > len(desc):
                desc = c
    # 메타 설명이 없거나 너무 짧으면 본문의 첫 '긴 문단'(초록·도입부)을 쓴다
    if len(desc) < 60 or desc.lower().startswith(it["title"].lower()[:40]):
        desc = ""
        body = soup.find("main") or soup.find("article") or soup.find(id="content") or soup.body or soup
        for p in body.find_all("p"):
            t = " ".join(p.get_text(" ").split())
            if len(t) >= 120 and not re.search(r"cookie|subscribe|javascript|privacy|copyright|\.gov website|"
                                               r"official website|secure websites", t, re.I):
                desc = t
                break
    if len(desc) < 60:
        return None
    # 약어(U.S., e.g. 등)의 마침표에서 문장이 잘리지 않게 잠시 바꿔 두고 나눈다
    protected = re.sub(r"\b([A-Z])\.(?=[A-Z]\.)|\b(U\.S|U\.K|e\.g|i\.e|vs|Dr|Mr|Ms|No|St)\.",
                       lambda m: m.group(0).replace(".", "§"), desc)
    sentences = [s.replace("§", ".").strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9“\"(])", protected) if s.strip()]
    bullets = []
    for s in sentences[:3]:
        bullets.append(s[:600])   # 길이 자르기는 번역 뒤에 (잘린 문장을 번역하면 어색해짐) — clip()
    return bullets or None


def clip(texts, limit):
    """불릿마다 limit 자를 넘으면 자르고 … 를 붙인다."""
    return [t if len(t) <= limit else t[:limit - 1].rstrip() + "…" for t in texts]


def summarize(client, it):
    content = page_text(it["link"])
    prompt = (f"분류: {it.get('series') or '-'}\n제목: {it['title']}\n링크: {it['link']}\n\n"
              f"본문:\n{content or '(본문을 가져오지 못함)'}")
    response = client.beta.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=4000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",                     # 안전 분류기가 거절하면 서버에서 다른 모델로 재시도
        system=SUMMARY_SYSTEM,
        output_config={
            "effort": "low",
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {"bullets": {"type": "array", "items": {"type": "string"}}},
                    "required": ["bullets"],
                    "additionalProperties": False,
                },
            },
        },
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "refusal":
        return None
    text = next((b.text for b in response.content if b.type == "text"), "")
    bullets = [b.strip() for b in json.loads(text).get("bullets", []) if b.strip()]
    return bullets[:3] or None


# ── 텔레그램 ──────────────────────────────────────────
def send(text):
    """보내고 텔레그램 메시지 번호를 돌려준다 (나중에 한국어 요약을 채워 넣을 때 사용)."""
    return telegram("sendMessage", {"text": text})


def edit(message_id, text):
    """이미 보낸 메시지의 내용을 바꾼다. 내용이 같으면(이미 반영됨) 조용히 넘어간다."""
    try:
        telegram("editMessageText", {"message_id": message_id, "text": text})
    except RuntimeError as e:
        if "message is not modified" not in str(e):
            raise


def telegram(method, payload):
    if DRY_RUN:
        print("─" * 60 + f" [{method}]\n" + payload["text"])
        return None
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    for attempt in range(3):
        r = requests.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json={"chat_id": chat_id, "parse_mode": "HTML", "disable_web_page_preview": True, **payload},
            timeout=30,
        )
        if r.status_code == 429:  # 전송 속도 제한 → 안내된 시간만큼 대기 후 재시도
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        if not r.ok:
            raise RuntimeError(f"텔레그램 {method} 실패 {r.status_code}: {r.text[:200]}")
        time.sleep(3)  # 채널 전송 속도 제한(분당 약 20건) 대비 — 글마다 따로 보내므로 간격을 둔다
        result = r.json().get("result")
        return result.get("message_id") if isinstance(result, dict) else None
    raise RuntimeError(f"텔레그램 {method} 실패: 속도 제한 반복")


# ── 한국어 3줄 요약 (Claude Code 예약 작업과 연동, API 요금 없음) ──────────
# 1) 봇이 리포트 알림을 보내면 state/pending_summaries.json 에 '요약 대기'로 기록
# 2) Claude Code 예약 작업(매시간)이 대기 목록을 읽고 한국어 3줄 요약을 만들어
#    claude/summaries 브랜치의 state/summaries.json 에 저장 (텔레그램 토큰 불필요)
# 3) 봇이 다음 실행 때 그 요약으로 이미 보낸 메시지를 수정 (영어 초록 → 한국어 요약)
PENDING_FILE = STATE_FILE.parent / "pending_summaries.json"
SUMMARIES_URL = ("https://raw.githubusercontent.com/{repo}/claude/summaries/state/summaries.json")
PENDING_KEEP_DAYS = 3
ROUTINE_SUMMARY = False   # 2026-10-21: 구독 사용량 절약을 위해 끔 → DeepL 번역(translate_ko)으로 대체. 다시 쓰려면 True + 예약 작업 켜기
_pending_new = []


def apply_summaries():
    """예약 작업이 만든 한국어 요약을 대기 중인 메시지에 반영하고, 반영된 건 대기 목록에서 뺀다."""
    if not PENDING_FILE.exists():
        return
    pending = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
    repo = os.environ.get("GITHUB_REPOSITORY", "HYANGKI-LEE/global-report-alarm")
    try:
        r = requests.get(SUMMARIES_URL.format(repo=repo), headers=HEADERS, timeout=30,
                         params={"t": int(time.time())})  # 캐시 회피
        summaries = r.json() if r.ok else {}
    except Exception:  # noqa: BLE001 - 요약 브랜치가 아직 없거나 일시 오류면 다음에
        summaries = {}
    cutoff = (datetime.now(KST) - timedelta(days=PENDING_KEEP_DAYS)).isoformat()
    keep = []
    for p in pending:
        s = summaries.get(str(p["message_id"]))
        if s and s.get("bullets"):
            it = {"title": p["title"], "link": p["link"], "series": p.get("series", ""),
                  "summary": s["bullets"][:3]}
            try:
                edit(p["message_id"], f"{p['icon']} <b>{html.escape(p['name'])}</b>\n{render_item(it)}"[:TG_LIMIT])
                print(f"[요약 반영] {p['title'][:50]}")
                continue
            except Exception as e:  # noqa: BLE001
                print(f"[요약 반영 실패] {p['title'][:40]}: {e}", file=sys.stderr)
        if p.get("sent_at", "") >= cutoff:   # 너무 오래된 대기 건은 버림
            keep.append(p)
    save_pending(keep)


def save_pending(pending):
    if DRY_RUN:
        return
    pending = pending + _pending_new
    _pending_new.clear()
    PENDING_FILE.write_text(json.dumps(pending, ensure_ascii=False, indent=1), encoding="utf-8")


def flush_pending():
    """이번 실행에서 새로 보낸 리포트 글을 요약 대기 목록에 추가해 저장."""
    if _pending_new:
        existing = json.loads(PENDING_FILE.read_text(encoding="utf-8")) if PENDING_FILE.exists() else []
        save_pending(existing)


def render_item(it):
    """• [분류] 제목(링크) + 그 아래 요약 불릿."""
    tag = f"<i>[{html.escape(it['series'])}]</i> " if it.get("series") else ""
    lines = [f"• {tag}<a href=\"{html.escape(it['link'], quote=True)}\">{html.escape(it['title'])}</a>"]
    for b in it.get("summary") or []:
        lines.append(f"   - {html.escape(b)}")
    return "\n".join(lines)


def send_new(new_by_name):
    """글 1건당 메시지 1개. 각 메시지는 기관명 줄 + [분류] 제목(링크) + 요약 불릿."""
    for (icon, name), items in new_by_name.items():
        head = f"{icon} <b>{html.escape(name)}</b>"
        shown = items[:MAX_ITEMS_PER_SOURCE]
        for it in shown:
            mid = send(f"{head}\n{render_item(it)}"[:TG_LIMIT])
            if ROUTINE_SUMMARY and mid and icon != "📊":   # (예약 작업 요약을 쓸 때만) 요약 대기 목록에 올림
                _pending_new.append({"message_id": mid, "icon": icon, "name": name, "title": it["title"],
                                     "link": it["link"], "series": it.get("series", ""),
                                     "summary_en": it.get("summary") or [],
                                     # 요약 예약 작업의 클라우드 환경은 일부 사이트에 접속하지 못하므로
                                     # 봇이 미리 초록·본문 앞부분을 담아 둔다
                                     "content": page_text(it["link"])[:4000],
                                     "sent_at": datetime.now(KST).isoformat()})
        if len(items) > len(shown):
            send(f"{head}\n… 외 {len(items) - len(shown)}건 (목록: {html.escape(items[0]['link'])})")


# ── 상태 ──────────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_timing(t_start, timings):
    """실행 시간 기록 (state/timing.json) — 확인 주기를 정할 때 참고용."""
    if DRY_RUN:
        return
    job_start = float(os.environ.get("JOB_START", 0)) or None
    now = time.time()
    data = {
        "run_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        "setup_seconds": round(t_start - job_start, 1) if job_start else None,   # 체크아웃·설치 등
        "check_seconds": round(now - t_start, 1),                                # 전체 대상 확인
        "slowest": dict(sorted(timings.items(), key=lambda kv: -kv[1])[:15]),
        "sources": len(timings),
    }
    (STATE_FILE.parent / "timing.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def save_state(state):
    if DRY_RUN:
        return
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


# ── 메인 ──────────────────────────────────────────────
def source_hosts(src):
    """대상이 접속하는 호스트들 (page, urls, watch 안의 주소)."""
    urls = [src.get("page", "")] + list(src.get("urls") or [])
    for v in (src.get("watch") or {}).values():
        urls += v if isinstance(v, list) else [v.get("url", "")] if isinstance(v, dict) else []
    return {urlsplit(u).hostname for u in urls if u}


def fetch_all(sources):
    """모든 대상을 미리 수집해 {id: (items, 오류, 걸린초)} 로 돌려준다.

    - 실제 브라우저·메일(한 연결을 공유)이 필요한 대상은 순서대로 한 줄로
    - 나머지는 사이트(호스트)별로 묶어, 서로 다른 사이트끼리만 동시에 — 한 사이트에 동시 접속이 몰리지 않게
    """
    from concurrent.futures import ThreadPoolExecutor

    def run(src):
        t0 = time.time()
        try:
            return src["id"], (fetch_source(src), None, round(time.time() - t0, 1))
        except Exception as e:  # noqa: BLE001
            return src["id"], (None, e, round(time.time() - t0, 1))

    def run_group(group):
        return [run(src) for src in group]

    serial, groups = [], {}
    for src in sources:
        hosts = source_hosts(src)
        if src.get("type") == "gmail" or hosts & BROWSER_HOSTS:
            serial.append(src)
        else:
            groups.setdefault(sorted(hosts)[0] if hosts else src["id"], []).append(src)

    results = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(run_group, g) for g in groups.values()]
        results.update(run_group(serial))           # 브라우저·메일은 메인 스레드에서 (동시에 진행)
        for f in futures:
            results.update(f.result())
    return results


def source_icon(src):
    """텔레그램 메시지 첫 줄 마크: 📊 데이터 / 💡 민간 리서치(kind: private) / 🏛 공공기관 리포트."""
    if src.get("type") == "datawatch" or src.get("kind") == "data":
        return "📊"
    if src.get("kind") == "private":
        return "💡"
    return "🏛"


TEST_FILE = ROOT / "test_request.txt"


def run_test(sources):
    """시험 발송: test_request.txt 에 적은 대상 id(한 줄에 하나, 뒤에 숫자를 쓰면 그 건수만큼 — 예: 'xxx 3')의
    최근 글을 실제 양식(요약 포함)으로 보낸다. 상태(seen)는 건드리지 않고, 끝나면 파일을 지운다(워크플로가 커밋)."""
    wanted = [l.split() for l in TEST_FILE.read_text(encoding="utf-8").splitlines()
              if l.strip() and not l.startswith("#")]
    by_id = {s["id"]: s for s in sources}
    new_by_name = {}
    for parts in wanted:
        sid, count = parts[0], int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
        src = by_id.get(sid)
        if not src:
            print(f"[시험] 없는 대상: {sid}", file=sys.stderr)
            continue
        items = fetch_source(src)
        if items:
            icon = source_icon(src)
            new_by_name.setdefault((icon, src["name"]), []).extend(items[:count])
    if new_by_name:
        summarize_all(new_by_name)
        send_new(new_by_name)
        flush_pending()
        if not DRY_RUN:  # 시험 결과(보낸 제목·불릿)를 기록 — 텔레그램을 못 보는 쪽에서도 확인용
            (STATE_FILE.parent / "test_result.json").write_text(json.dumps(
                [{"name": n, "title": it["title"], "summary": it.get("summary")}
                 for (_, n), its in new_by_name.items() for it in its],
                ensure_ascii=False, indent=1), encoding="utf-8")
    TEST_FILE.unlink()


def main():
    config = yaml.safe_load(SOURCES_FILE.read_text(encoding="utf-8"))
    BROWSER_HOSTS.update(config.get("browser_hosts") or [])
    RENDER_HOSTS.update(config.get("render_hosts") or [])
    BROWSER_HOSTS.update(RENDER_HOSTS)
    sources = [s for s in config["sources"] if s.get("enabled", True)]
    if TEST_FILE.exists():
        run_test(sources)
        return
    try:
        apply_summaries()   # 예약 작업이 만든 한국어 요약을 이미 보낸 메시지에 반영
    except Exception as e:  # noqa: BLE001 - 요약 반영이 실패해도 새 글 확인은 계속
        print(f"[요약 반영 오류] {e}", file=sys.stderr)
    state = load_state()

    seeded, failed, warnings = [], [], []
    t_start, timings = time.time(), {}
    results = fetch_all(sources)
    new_by_name = {}  # 표시 이름별 신규 항목 (같은 이름의 여러 대상을 한 블록으로 묶음)
    pending = {}  # 전송 성공 후에 seen 에 반영할 id

    for src in sources:
        sid = src["id"]
        name = src["name"] + (f" · {src['title']}" if src.get("title") else "")
        st = state.setdefault(sid, {"seen": [], "fails": 0})
        items, err, timings[sid] = results[sid]
        try:
            if err:
                raise err
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
            st["seen"] = ids[:SEEN_KEEP] or ["__connected__"]  # 0건이어도 연결 완료로 기록 (메일 중계 등)
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
            icon = source_icon(src)
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
        summarize_all(new_by_name)
        send_new(new_by_name)
        flush_pending()

    # 전송까지 끝난 것만 seen 에 반영 (전송 실패 시 다음 실행에서 다시 보냄)
    for sid, new_ids in pending.items():
        state[sid]["seen"] = (new_ids + state[sid]["seen"])[:SEEN_KEEP]
    save_state(state)
    save_timing(t_start, timings)


if __name__ == "__main__":
    try:
        main()
    finally:
        close_browser()
