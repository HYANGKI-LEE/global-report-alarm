"""임시 시험: GitHub 서버에서 실제 브라우저(헤드리스 크롬)로 세인트루이스·캔자스시티 페이지가 열리는지 확인."""
import json
from playwright.sync_api import sync_playwright

URLS = [
    "https://www.kansascityfed.org/research/",
    "https://www.kansascityfed.org/research/economic-bulletin/",
    "https://www.stlouisfed.org/research/economic-conditions-survey-results",
    "https://www.stlouisfed.org/rss/page-resources/publications/review",
    "https://fred.stlouisfed.org/series/STLFSI4",
]
out = []
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page()
    for u in URLS:
        try:
            r = pg.goto(u, timeout=45000, wait_until="domcontentloaded")
            txt = pg.inner_text("body")[:300]
            out.append({"url": u, "status": r.status if r else None, "title": pg.title(), "text": " ".join(txt.split())[:200]})
        except Exception as e:
            out.append({"url": u, "error": str(e)[:200]})
    b.close()
open("probe/result.json", "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=1))
print(json.dumps(out, ensure_ascii=False, indent=1))
