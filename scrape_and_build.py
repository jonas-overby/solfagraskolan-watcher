#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, json, time, html
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlencode
import requests
from bs4 import BeautifulSoup

# --------- Konfiguration ----------
BASE = "https://sammantraden.huddinge.se/search"
QUERY = "Solfagraskolan"
PAGE_SIZE = 100
MAX_PAGES = 60

USER_AGENT = "Mozilla/5.0 (compatible; SolfagraskolanWatcher/2.1)"
HEADERS = {"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.8"}

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
        return {"items": []}  # list of {url,title,detected_at,last_modified,source}
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

def looks_like_pdf(url: str) -> bool:
    """Identifiera PDF-länkar även om .pdf saknas på slutet."""
    u = url.lower()
    if u.endswith(".pdf"):
        return True
    # Fånga udda varianter som slutar med "...pdf" utan punkt, eller har ?-query efter
    if re.search(r"pdf($|\?|#)", u):
        return True
    # Fallback: kolla content-type via HEAD (kan vara lite långsammare)
    try:
        h = requests.head(url, headers=HEADERS, allow_redirects=True, timeout=15)
        ct = (h.headers.get("Content-Type") or "").lower()
        if "application/pdf" in ct:
            return True
    except Exception:
        pass
    return False

def head_last_modified(url:str):
    """Hämta Last-Modified om möjligt; returnera läsbar sträng eller None."""
    try:
        h = requests.head(url, headers=HEADERS, allow_redirects=True, timeout=20)
        lm = h.headers.get("Last-Modified")
        if not lm:
            return None
        # Försök parsa RFC1123 -> lokal tid
        try:
            dt = datetime.strptime(lm, "%a, %d %b %Y %H:%M:%S %Z")
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone().strftime("%Y-%m-%d %H:%M")
        except Exception:
            return lm
    except Exception:
        return None

def parse_links(html_text:str, page_url:str):
    """
    Returnerar två listor:
      direct_links: [(title, url, source)] där länktexten eller blocket matchar 'Solfagraskolan'
      agenda_pages: [url] med ärendesidor/punkter som vi ska följa för att skörda bilagor
    source = 'direct-title' (träff i länktext) eller 'direct-block' (PDF i block som matchar).
    """
    soup = BeautifulSoup(html_text, "html.parser")
    direct_links = []
    agenda_pages = []

    def norm(s: str) -> str:
        return " ".join((s or "").split()).strip().lower()

    for a in soup.select("a[href]"):
        href = (a.get("href") or "").strip()
        if not href:
            continue

        # Absolut URL
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = urljoin(page_url, href)
        elif not href.startswith("http"):
            href = urljoin(page_url, href)

        # Hoppa paginering
        if href.startswith(BASE) and "pindex=" in href and "psize=" in href:
            continue

        title_raw = a.get_text(" ", strip=True)
        title_text = norm(title_raw)

        # Bygg blocktext (lite kontext runt länken)
        block_text = title_text
        p = a.parent
        for _ in range(2):
            if not p:
                break
            t = norm(p.get_text(" ", strip=True))
            if t:
                block_text = t
            p = p.parent

        looks_like_agenda = ("/agenda" in href) or ("/namnder-styrelser/" in href) or ("/welcome-" in href)

        # 1) Direkta träffar: länktexten innehåller ordet
        if "solfagraskolan" in title_text:
            direct_links.append((" ".join(title_raw.split()), href, "direct-title"))
            continue

        # 2) Block matchar 'Solfagraskolan' → ta PDF direkt, eller följ ärendesida
        if "solfagraskolan" in block_text:
            if looks_like_pdf(href):
                ttl = " ".join(title_raw.split()) or "Bilaga"
                direct_links.append((ttl, href, "direct-block"))
            elif looks_like_agenda and not looks_like_pdf(href):
                agenda_pages.append(href)

    # Dedup
    seen = set()
    direct_links = [(t,u,s) for (t,u,s) in direct_links if not (u in seen or seen.add(u))]
    agenda_pages = [u for u in agenda_pages if not (u in seen or seen.add(u))]
    return direct_links, agenda_pages

def extract_attachments_from_agenda(url:str):
    """
    Öppna ärendesidan/punkten och returnera [(title,url,source='via-agenda')] för alla bilagor.
    Vi letar primärt efter PDF-länkar, men tar även länkar i sektioner som heter Bilaga/Attachments.
    """
    out = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        def norm(s): return " ".join((s or "").split()).strip()

        # Kandidatsektioner där bilagor brukar ligga
        candidate_sections = []
        for sec in soup.find_all(True):
            txt = (sec.get_text(" ", strip=True) or "").lower()
            if any(k in txt for k in ["bilaga", "bilagor", "attachments", "documents"]):
                candidate_sections.append(sec)

        def collect_links(context):
            items = []
            anchors = context.select("a[href]") if hasattr(context, "select") else soup.select("a[href]")
            for a in anchors:
                href = (a.get("href") or "").strip()
                if not href:
                    continue
                # Absolutifiera
                if href.startswith("//"):
                    href = "https:" + href
                elif href.startswith("/"):
                    href = urljoin(url, href)
                elif not href.startswith("http"):
                    href = urljoin(url, href)

                # Dokument-liknande
                if looks_like_pdf(href) or "document" in href.lower() or "attachment" in href.lower():
                    title = norm(a.get_text(" ", strip=True)) or "Bilaga"
                    items.append((title, href))
            return items

        items = []
        if candidate_sections:
            for sec in candidate_sections:
                items.extend(collect_links(sec))
        else:
            items.extend(collect_links(soup))

        # Dedup
        seen = set()
        for t,u in items:
            if u in seen:
                continue
            seen.add(u)
            out.append((t,u,"via-agenda"))
    except Exception:
        # svälj och gå vidare – hellre ofullständigt än stopp
        pass

    return out

def collect_all():
    """
    Hämtar alla sökresultatssidor och returnerar [(title,url,source)] där:
      - source='direct-title'  (länktext matchar)
      - source='direct-block'  (block matchar och länken är PDF)
      - source='via-agenda'    (hämtad bilaga från ärendesida som matchar)
    """
    direct = []
    agenda_pages = []

    for p in range(1, MAX_PAGES+1):
        html_text, url = fetch_page(p)
        d, a = parse_links(html_text, url)
        direct.extend(d)
        agenda_pages.extend(a)

        # Avsluta tidigare om det verkar slut
        if p == 1 and (len(d) + len(a)) < PAGE_SIZE:
            break
        if (len(d) + len(a)) == 0:
            break
        time.sleep(0.5)

    attachments = []
    seen_agenda = set()
    for ap in agenda_pages:
        if ap in seen_agenda:
            continue
        seen_agenda.add(ap)
        attachments.extend(extract_attachments_from_agenda(ap))
        time.sleep(0.4)

    # Slå ihop + dedup
    all_items = []
    seen = set()
    for t,u,s in (direct + attachments):
        if u in seen:
            continue
        seen.add(u)
        all_items.append((t,u,s))

    return all_items

def iso_year_week(dt: datetime):
    y, w, _ = dt.isocalendar()
    return y, w

def build_site(hist):
    # Grupp enligt ISO-vecka på detected_at (svensk praxis)
    groups = {}  # (year, week) -> [items]
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
.badge{display:inline-block;padding:2px 6px;border-radius:6px;background:#eef2ff;color:#3730a3;font-size:12px;margin-left:6px}
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
        f.write("<div class='notice'>Sidan uppdateras dagligen och visar nyupptäckta dokument där 'Solfagraskolan' förekommer i sökträffen eller i ärendets rubrik. PDF-länkar kan upptäckas även om URL:en inte slutar på .pdf.</div>")

        for (y,w) in sorted_keys:
            items = groups[(y,w)]
            items = sorted(items, key=lambda it: it["detected_at"], reverse=True)
            f.write(f"<section class='week'><h2>Vecka {w}, {y}</h2>")
            for it in items:
                title = html.escape(it["title"])
                url = html.escape(it["url"])
                det = html.escape(it["detected_at"].replace("T"," ")[:16])
                lm  = html.escape(it.get("last_modified") or "")
                src = it.get("source", "")
                badge = ""
                if src == "via-agenda":
                    badge = "<span class='badge'>via ärendepunkt</span>"
                elif src == "direct-block":
                    badge = "<span class='badge'>träff i block</span>"
                elif src == "direct-title":
                    badge = "<span class='badge'>träff i länktext</span>"

                f.write("<div class='item'>")
                f.write(f"<a href='{url}' target='_blank' rel='noopener'>{title}</a> {badge}")
                meta = f"Upptäckt: {det}"
                if lm:
                    meta += f" • Last-Modified: {lm}"
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

    # Nya länkar = inte tidigare sedda
    new = [(t,u,s) for (t,u,s) in all_now if u not in known_urls]

    now_iso = datetime.now(timezone.utc).astimezone().isoformat(timespec="minutes")
    for (title,url,source) in new:
        lm = head_last_modified(url) if looks_like_pdf(url) else None
        hist["items"].append({
            "url": url,
            "title": title,
            "detected_at": now_iso,
            "last_modified": lm,
            "source": source
        })

    save_history(hist)
    build_site(hist)

    print(f"Upptäckta nya länkar denna körning: {len(new)}")
    print(f"Totalt i historik: {len(hist['items'])}")
    print(f"Genererat {INDEX_HTML}")

if __name__ == "__main__":
    main()
