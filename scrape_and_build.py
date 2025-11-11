#!/usr/bin/env python3
import os, re, json, time, html
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlencode
import requests
from bs4 import BeautifulSoup

BASE = "https://sammantraden.huddinge.se/search"
QUERY = "Solfagraskolan"
PAGE_SIZE = 100
MAX_PAGES = 60

USER_AGENT = "Mozilla/5.0 (compatible; SolfagraskolanWatcher/2.0)"
HEADERS = {"User-Agent": USER_AGENT}

# Paths
DATA_DIR   = os.path.join("data")
HISTORY_FN = os.path.join(DATA_DIR, "history.json")
DOCS_DIR   = "docs"
INDEX_HTML = os.path.join(DOCS_DIR, "index.html")
STYLE_CSS  = os.path.join(DOCS_DIR, "style.css")

def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(DOCS_DIR, exist_ok=True)

def load_history():
    ensure_dirs()
    if not os.path.exists(HISTORY_FN):
        return {"items": []}  # list of {url,title,detected_at,last_modified}
    with open(HISTORY_FN, "r", encoding="utf-8") as f:
        return json.load(f)

def save_history(hist):
    ensure_dirs()
    with open(HISTORY_FN, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=2)

def fetch_page(page_index:int):
    params = {"text": QUERY, "pindex": page_index, "psize": PAGE_SIZE}
    url = f"{BASE}?{urlencode(params)}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text, url

def parse_links(html_text:str, page_url:str):
    soup = BeautifulSoup(html_text, "html.parser")
    out = []
    for a in soup.select("a[href]"):
        title = " ".join(a.get_text(" ", strip=True).split())
        if not title:
            continue
        if "solfagraskolan" not in title.lower():
            continue
        href = a.get("href").strip()
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = urljoin(page_url, href)
        elif not href.startswith("http"):
            href = urljoin(page_url, href)
        # hoppa över pagineringslänkar
        if href.startswith(BASE) and "pindex=" in href and "psize=" in href:
            continue
        out.append((title, href))
    # dedup by URL
    seen = set()
    uniq = []
    for t,u in out:
        if u in seen: 
            continue
        seen.add(u)
        uniq.append((t,u))
    return uniq

def head_last_modified(url:str):
    try:
        h = requests.head(url, headers=HEADERS, allow_redirects=True, timeout=20)
        lm = h.headers.get("Last-Modified")
        if not lm:
            return None
        # försök parsa (RFC1123), fallback: returnera strängen
        try:
            dt = datetime.strptime(lm, "%a, %d %b %Y %H:%M:%S %Z")
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone().strftime("%Y-%m-%d %H:%M")
        except Exception:
            return lm
    except Exception:
        return None

def collect_all():
    all_items = []
    for p in range(1, MAX_PAGES+1):
        html_text, url = fetch_page(p)
        items = parse_links(html_text, url)
        all_items.extend(items)
        if p == 1 and len(items) < PAGE_SIZE:
            break
        if len(items) == 0:
            break
        time.sleep(0.5)
    return all_items

def iso_year_week(dt: datetime):
    # ISO-vecka (svensk praxis)
    y, w, _ = dt.isocalendar()
    return y, w

def build_site(hist):
    # Grupp enligt ISO-vecka på detected_at (svensk kontext)
    groups = {}  # (year, week) -> [items]
    for it in hist["items"]:
        det = datetime.fromisoformat(it["detected_at"])
        y,w = iso_year_week(det)
        groups.setdefault((y,w), []).append(it)

    # sortera veckor nyast först
    sorted_keys = sorted(groups.keys(), key=lambda k: (k[0], k[1]), reverse=True)

    # enkel CSS
    with open(STYLE_CSS, "w", encoding="utf-8") as f:
        f.write("""
html,body{margin:0;padding:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,Inter,Arial,sans-serif;}
header{padding:24px 16px;background:#0f172a;color:#fff;}
header h1{margin:0;font-size:24px;font-weight:700;}
main{max-width:900px;margin:0 auto;padding:16px;}
.week{margin:24px 0;padding:16px;border:1px solid #e2e8f0;border-radius:12px;background:#fff;}
.week h2{margin:0 0 12px 0;font-size:18px;}
.item{padding:10px 0;border-top:1px solid #e2e8f0;}
.item:first-child{border-top:none;}
.item a{font-weight:600;text-decoration:none;}
.item a:hover{text-decoration:underline;}
.meta{color:#475569;font-size:13px;margin-top:4px;}
footer{max-width:900px;margin:32px auto 48px auto;padding:0 16px;color:#475569;font-size:13px;}
.notice{background:#f8fafc;border:1px solid #e2e8f0;padding:10px;border-radius:8px;margin:16px 0;}
        """.strip())

    # HTML
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(INDEX_HTML, "w", encoding="utf-8") as f:
        f.write("<!doctype html><html lang='sv'><meta charset='utf-8'>")
        f.write("<meta name='viewport' content='width=device-width, initial-scale=1'>")
        f.write("<title>Nya dokument – Solfagraskolan</title>")
        f.write(f"<link rel='stylesheet' href='style.css'>")
        f.write("<header><h1>Nya dokument – Solfagraskolan</h1></header>")
        f.write("<main>")
        f.write("<div class='notice'>Den här sidan uppdateras dagligen och visar nya/upptäckta dokument där söktermen ”Solfagraskolan” förekommer. Länkarna är direkta till källan.</div>")

        for (y,w) in sorted_keys:
            items = groups[(y,w)]
            # sortera inom veckan: nyast först på detected_at
            items = sorted(items, key=lambda it: it["detected_at"], reverse=True)
            f.write(f"<section class='week'><h2>Vecka {w}, {y}</h2>")
            for it in items:
                title = html.escape(it["title"])
                url = html.escape(it["url"])
                det = html.escape(it["detected_at"].replace("T"," ")[:16])
                lm  = it.get("last_modified")
                lm  = html.escape(lm) if lm else None
                meta = f"Upptäckt: {det}" + (f" • Last-Modified: {lm}" if lm else "")
                f.write("<div class='item'>")
                f.write(f"<a href='{url}' target='_blank' rel='noopener'>{title}</a>")
                f.write(f"<div class='meta'>{meta}</div>")
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

    # Nya länkar = de vi inte sett tidigare
    new = [(t,u) for (t,u) in all_now if u not in known_urls]

    # Berika + lägg i historik
    now_iso = datetime.now(timezone.utc).astimezone().isoformat(timespec="minutes")
    for (title,url) in new:
        lm = head_last_modified(url) if (url.lower().endswith(".pdf") or "/protocol/" in url) else None
        hist["items"].append({
            "url": url,
            "title": title,
            "detected_at": now_iso,
            "last_modified": lm
        })

    # Spara historik och bygg sajt
    save_history(hist)
    build_site(hist)

    print(f"Upptäckta nya länkar denna körning: {len(new)}")
    print(f"Totalt i historik: {len(hist['items'])}")
    print(f"Genererat {INDEX_HTML}")

if __name__ == "__main__":
    main()
