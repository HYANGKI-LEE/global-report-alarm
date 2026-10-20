"""임시 시험: GitHub 서버에서 실제 브라우저로 IMF 페이지·RSS 가 열리는지."""
import json
from playwright.sync_api import sync_playwright
URLS = ["https://www.imf.org/en/publications",
        "https://www.imf.org/en/Publications/RSS?language=eng",
        "https://www.imf.org/en/Publications/RSS?language=eng&series=IMF%20Working%20Papers",
        "https://www.imf.org/en/research/commodity-prices"]
out = []
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
    for u in URLS:
        try:
            r = pg.goto(u, timeout=60000, wait_until="networkidle")
            body = r.body()[:400].decode("utf-8", "replace") if r else ""
            links = pg.eval_on_selector_all("a[href*='/issues/']", "els => els.slice(0,8).map(e => e.textContent.trim().slice(0,60) + ' | ' + e.getAttribute('href'))")
            out.append({"url": u, "status": r.status if r else None, "title": pg.title(), "body": " ".join(body.split())[:250], "issues_links": links})
        except Exception as e:
            out.append({"url": u, "error": str(e)[:250]})
    b.close()
open("probe/result.json", "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=1))
