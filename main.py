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
        if src.get("include"):  # 링크에 이 글자가 들어간 항목만 (예: Fed in Print 에서 Working Paper 만)
            items = [it for it in items if src["include"] in it["link"]]
        return items[:src.get("max_items", 100)]  # 오래된 글까지 다 들어있는 피드 대비
    if kind == "page_links":
        return fetch_page_links(src)
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
                      "link": link, "series": r.get("issue") or r.get("publication") or ""})
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
                          "link": link, "series": ""})
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

    link_pattern 은 href 에 대해 검사하고, 글자 수가 15자 미만인 링크(사람 이름·'더보기' 등)는 버린다.
    """
    page = src["page"]
    soup = BeautifulSoup(http_get(page).text, "html.parser")
    root = soup.find("main") or soup
    items, seen = [], set()
    for a in root.find_all("a", href=True):
        title = " ".join(a.get_text().split())
        if not re.search(src["link_pattern"], a["href"]) or len(title) < 15:
            continue
        link = urljoin(page, a["href"]).split("#")[0]
        if link in seen:
            continue
        seen.add(link)
        items.append({"id": link, "title": title, "link": link, "series": ""})
    if not items:
        raise ValueError("일치하는 링크 0건 (페이지 구조 변경?)")
    return items


def expand_quarters(urls):
    """주소 안의 {quarter} / {prev_quarter} 를 현재·직전 분기(예: 2026Q4 / 2026Q3)로 바꾼다."""
    today = datetime.now(KST)
    q = (today.month - 1) // 3 + 1
    cur = f"{today.year}Q{q}"
    prev = f"{today.year}Q{q - 1}" if q > 1 else f"{today.year - 1}Q4"
    return [u.replace("{quarter}", cur).replace("{prev_quarter}", prev) for u in urls]


def http_get(url):
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r


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
    sources = [s for s in yaml.safe_load(SOURCES_FILE.read_text(encoding="utf-8"))["sources"]
               if s.get("enabled", True)]
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
        print(f"[확인] {name}: 신규 {len(new)}건 / 피드 {len(items)}건")
        if new:
            icon = "📊" if src.get("type") == "datawatch" else "🏛"
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
    main()
