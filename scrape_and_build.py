#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, time, html
from datetime import datetime, timezone
from urllib.parse import urljoin, urlencode
import requests
from bs4 import BeautifulSoup

# --------- Konfiguration ----------
BASE = "https://sammantraden.huddinge.se/search"
QUERY = "Solfagraskolan"
PAGE_SIZE = 50
MAX_PAGES = 20

USER_AGENT = "Mozilla/5.0 (compatible; SolfagraskolanWatcher/stable-1.0)"
HEADERS = {"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8"}

# Paths
DATA_DIR   = "data"
HISTORY_FN = os.path.join(DATA_DIR, "history.json")
DOCS_DIR   = "docs"
INDEX_HTML = os.path.join(DOCS_DIR, "index.html")
STYLE_CSS  = os.path.join(DOCS_DIR, "style.css")
# ----------------------------------

def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(DOCS_DIR, exist_ok=True)

def load_history():
    ensure_dirs()
    if not os.path.exists(HISTORY_FN):
        return {"items": []}  # list of {url,title,detected_at}
    with open(HISTORY_FN, "r", encoding="utf-8") as f:
        return json.load(f)

def save_history(hist):
    ensure_dirs()
    with open(HISTORY_FN, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=2)

def fetch_page(page_index:int):
    params = {"text": QUERY, "pindex": page_index, "psize": PAGE_SIZE}
    url = f"{BASE}?{urlencode(params)}"
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.text, url

def parse_links(html_text:str, page_url:str):
    """Plocka länkar där själva länktexten innehåller 'Solfagraskolan'."""
    soup = BeautifulSoup(html_text, "html.parser")
    out = []
    for a in soup.select("a[href]"):
        title_raw = (a.get_text(" ", strip=True) or "").strip()
        if not title_raw:
            continue
        if "solfagraskolan" not in title_raw.lower():
            continue
        href = (a.get("href") or "").strip()
        if not href:
            continue
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = urljoin(page_url, href)
        elif not href.startswith("http"):
            href = urljoin(page_url, href)
        # filtrera bort pagineringslänkar
        if href.startswith(BASE) and "pindex=" in href and "psize=" in href:
            continue
        out.append((title_raw, href))
    # dedup på URL
    seen, uniq = set(), []
    for t,u in out:
        if u in seen: 
            continue
        seen.add(u)
        uniq.append((t,u))
    return uniq

def collect_all():
    """Hämta ett rimligt antal resultatsidor; bryt tidigt om det tar slut."""
    all_items = []
    for p in range(1, MAX_PAGES+1):
        html_text, url = fetch_page(p)
        items = parse_links(html_text, url)
        all_items.extend(items)
        if p == 1 and len(items) < PAGE_SIZE:
            break
        if len(items) == 0:
            break
        time.sleep(0.3)
    # dedup igen för säkerhets skull
    seen, uniq = set(), []
    for t,u in all_items:
        if u in seen: 
            continue
        seen.add(u)
        uniq.append((t,u))
    return uniq

def iso_year_week(dt: datetime):
    y, w, _ = dt.isocalendar()
    return y, w

def build_site(hist):
    # Grupp efter ISO-vecka baserat på detected_at
    groups = {}
    for it in hist["items"]:
        det = datetime.fromisoformat(it["detected_at"])
        y,w = iso_year_week(det)
        groups.setdefault((y,w), []).append(it)
    sorted_keys = sorted(groups.keys(), key=lambda k: (k[0], k[1]), reverse=True)

    # CSS
    with open(STYLE_CSS, "w", encoding="utf-8") as f:
        f.write("""
html,body{margin:0;padding:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,Inter,Arial,sans-serif;background:#f8fafc;}
header{padding:24px 16px;background:#0f172a;color:#fff;}
header h1{margin:0;font-size:24px;font-weight:700;}
main{max-width:980px;margin:0 auto;padding:16px;}
.week{margin:24px 0;padding:16px;border:1px solid #e2e8f0;border-radius:12px;background:#fff;}
.week h2{margin:0 0 12px 0;font-size:18px;}
.item{padding:10px 0;border-top:1px solid #e2e8f0;}
.item:first-child{border-top:none;}
.item a{font-weight:600;text-decoration:none;}
.item a:hover{text-decoration:underline;}
.meta{color:#475569;font-size:13px;margin-top:4px}
footer{max-width:980px;margin:32px auto 48px auto;padding:0 16px;color:#475569;font-size:13px;}
.notice{background:#f1f5f9;border:1px solid #e2e8f0;padding:10px;border-radius:8px;margin:16px 0;}
        """.strip())

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(INDEX_HTML, "w", encoding="utf-8") as f:
        f.write("<!doctype html><html lang='sv'><meta charset='utf-8'>")
        f.write("<meta name='viewport' content='width=device-width, initial-scale=1'>")
        f.write("<title>Nya dokument – Solfagraskolan</title>")
        f.write(f"<link rel='stylesheet' href='style.css'>")
        f.write("<header><h1>Nya dokument – Solfagraskolan</h1></header>")
        f.write("<main>")
        f.write("<div class='notice'>Den här stabila versionen listar länkar vars länktext innehåller ”Solfagraskolan”. (Ingen extra uppföljning av ärendesidor.)</div>")

        for (y,w) in sorted_keys:
            items = sorted(groups[(y,w)], key=lambda it: it["detected_at"], reverse=True)
            f.write(f"<section class='week'><h2>Vecka {w}, {y}</h2>")
            for it in items:
                title = html.escape(it["title"])
                url = html.escape(it["url"])
                det = html.escape(it["detected_at"].replace("T"," ")[:16])
                f.write("<div class='item'>")
                f.write(f"<a href='{url}' target='_blank' rel='noopener'>{title}</a>")
                f.write(f"<div class='meta'>Upptäckt: {det}</div>")
                f.write("</div>")
            f.write("</section>")

        f.write("</main>")
        f.write(f"<footer>Senast genererad: {now}. Källa: <a href='https://sammantraden.huddinge.se/search?text=Solfagraskolan'>MeetingPlus sök</a>.</footer>")
        f.write("</html>")

def main():
    ensure_dirs()
    hist = load_history()
    known_urls = {it["url"] for it in hist["items"]}

    all_now = collect_all()
    new = [(t,u) for (t,u) in all_now if u not in known_urls]

    now_iso = datetime.now(timezone.utc).astimezone().isoformat(timespec="minutes")
    for (title,url) in new:
        hist["items"].append({
            "url": url,
            "title": title,
            "detected_at": now_iso
        })

    save_history(hist)
    build_site(hist)

    print(f"Nya länkar denna körning: {len(new)}")
    print(f"Totalt i historik: {len(hist['items'])}")
    print(f"Genererat {INDEX_HTML}")

if __name__ == "__main__":
    main()
