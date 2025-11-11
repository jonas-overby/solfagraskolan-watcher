#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, json, time, html
from datetime import datetime, timezone
from urllib.parse import urljoin, urlencode
import requests
from bs4 import BeautifulSoup

# --------- Konfiguration ----------
BASE = "https://sammantraden.huddinge.se/search"
QUERY = "Solfagraskolan"
PAGE_SIZE = 100
MAX_PAGES = 60

USER_AGENT = "Mozilla/5.0 (compatible; SolfagraskolanWatcher/2.3)"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.8"
}

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
        return {"items": []}
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

# ---------- Hjälp: PDF & headers ----------

def looks_like_pdf(url: str) -> bool:
    u = url.lower()
    if u.endswith(".pdf"):
        return True
    if re.search(r"pdf($|[?#])", u):
        return True
    try:
        h = requests.head(url, headers=HEADERS, allow_redirects=True, timeout=15)
        ct = (h.headers.get("Content-Type") or "").lower()
        if "application/pdf" in ct:
            return True
    except Exception:
        pass
    return False

def head_last_modified(url:str):
    try:
        h = requests.head(url, headers=HEADERS, allow_redirects=True, timeout=20)
        lm = h.headers.get("Last-Modified")
        if not lm:
            return None
        try:
            dt = datetime.strptime(lm, "%a, %d %b %Y %H:%M:%S %Z")
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone().strftime("%Y-%m-%d %H:%M")
        except Exception:
            return lm
    except Exception:
        return None

# ---------- Steg 1: plocka länkar & mötessidor från sök ----------

def parse_links(html_text:str, page_url:str):
    """
    Returnerar:
      direct_links: [(title, url, source)]
      agenda_pages: [url]  (mötessidor vi bör följa)
    Heuristik:
      - Om "Solfagraskolan" finns i länktexten → direkt.
      - Om det finns i blocket runt → ta PDF direkt; och
        samla även eventuella /committees/-länkar i samma block att följa.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    direct_links, agenda_pages = [], []

    def norm(s: str) -> str:
        return " ".join((s or "").split()).strip().lower()

    for a in soup.select("a[href]"):
        href = (a.get("href") or "").strip()
        if not href:
            continue

        # Absolutifiera
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

        # Samla blocktext = lite kontext 2 nivåer upp
        block = a
        block_text = title_text
        for _ in range(2):
            block = block.parent if block else None
            if not block: break
            t = norm(block.get_text(" ", strip=True))
            if t: block_text = t

        # Kännetecken för mötessidor
        def is_agenda_url(u: str) -> bool:
            return any(seg in u for seg in ["/committees/", "/agenda", "/namnder-styrelser/", "/welcome-"])

        # 1) Direktträff i länktext
        if "solfagraskolan" in title_text:
            direct_links.append((" ".join(title_raw.split()), href, "direct-title"))
            # fortsätt ändå för att kunna plocka ev. committees-länk i blocket
        # 2) Blocket matchar → PDF direkt
        if "solfagraskolan" in block_text and looks_like_pdf(href):
            ttl = " ".join(title_raw.split()) or "Bilaga"
            direct_links.append((ttl, href, "direct-block"))

        # 3) EXTRA: om blocket matchar, plocka ALLA committees-länkar i samma block
        if "solfagraskolan" in block_text:
            candidate_anchors = []
            # samla alla <a> i blocket (sista block vi sparade)
            blk = a
            for _ in range(2):
                blk = blk.parent if blk else None
            if blk:
                candidate_anchors = blk.select("a[href]")
            for ca in candidate_anchors:
                ch = (ca.get("href") or "").strip()
                if not ch:
                    continue
                if ch.startswith("//"):
                    ch = "https:" + ch
                elif ch.startswith("/"):
                    ch = urljoin(page_url, ch)
                elif not ch.startswith("http"):
                    ch = urljoin(page_url, ch)
                if is_agenda_url(ch) and not looks_like_pdf(ch):
                    agenda_pages.append(ch)

        # 4) Om länken i sig ser ut som mötessida och blocket matchar → följ
        if "solfagraskolan" in block_text and is_agenda_url(href) and not looks_like_pdf(href):
            agenda_pages.append(href)

    # Dedup
    seen = set()
    direct_links = [(t,u,s) for (t,u,s) in direct_links if not (u in seen or seen.add(u))]
    agenda_pages  = [u for u in agenda_pages  if not (u in seen or seen.add(u))]
    return direct_links, agenda_pages

# ---------- Steg 2: skörda BARA bilagor i agendapunkter som rör Solfagraskolan ----------

def extract_attachments_from_agenda(url:str):
    """
    Öppna mötessidan och returnera [(title,url,source='via-agenda')] från
    de agendapunkter vars rubrik/sektion innehåller "Solfagraskolan".
    """
    out = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        def norm(s): return " ".join((s or "").split()).strip()
        def low(s):  return norm(s).lower()

        # Hitta alla punkter/sektioner
        # Heuristik: rubriker/länkar som ser ut som "X Genomförandebeslut …"
        sections = []
        for node in soup.find_all(True):
            txt = (node.get_text(" ", strip=True) or "")
            ltxt = txt.lower()
            if ("bilagor" in ltxt) or re.search(r"\b(punkt|genomförandebeslut|inriktningsbeslut|solfagraskolan)\b", ltxt):
                sections.append(node)

        # För varje kandidatsektion: om sektionens text innehåller "Solfagraskolan",
        # samla PDF-/dokumentlänkar INOM just den sektionen.
        seen = set()
        for sec in sections:
            if "solfagraskolan" not in low(sec.get_text(" ", strip=True)):
                continue
            for a in sec.select("a[href]"):
                href = (a.get("href") or "").strip()
                if not href:
                    continue
                if href.startswith("//"):
                    href = "https:" + href
                elif href.startswith("/"):
                    href = urljoin(url, href)
                elif not href.startswith("http"):
                    href = urljoin(url, href)

                if looks_like_pdf(href) or "document" in href.lower() or "attachment" in href.lower():
                    if href in seen: 
                        continue
                    seen.add(href)
                    title = norm(a.get_text(" ", strip=True)) or "Bilaga"
                    out.append((title, href, "via-agenda"))
    except Exception:
        pass

    return out

# ---------- Orkestrering ----------

def collect_all():
    """
    Slår ihop:
      - direkta träffar (titel/block)
      - bilagor från mötessidor som rör Solfagraskolan
    """
    direct, agenda_pages = [], []

    for p in range(1, MAX_PAGES+1):
        html_text, url = fetch_page(p)
        d, a = parse_links(html_text, url)
        direct.extend(d)
        agenda_pages.extend(a)
        if p == 1 and (len(d)+len(a)) < PAGE_SIZE:
            break
        if (len(d)+len(a)) == 0:
            break
        time.sleep(0.4)

    attachments = []
    seen_agenda = set()
    for ap in agenda_pages:
        if ap in seen_agenda:
            continue
        seen_agenda.add(ap)
        attachments.extend(extract_attachments_from_agenda(ap))
        time.sleep(0.3)

    # Slå ihop + dedup
    all_items, seen = [], set()
    for t,u,s in (direct + attachments):
        if u in seen:
            continue
        seen.add(u)
        all_items.append((t,u,s))
    return all_items

# ---------- Bygg HTML ----------

def iso_year_week(dt: datetime):
    y, w, _ = dt.isocalendar()
    return y, w

def build_site(hist):
    groups = {}
    for it in hist["items"]:
        det = datetime.fromisoformat(it["detected_at"])
        y,w = iso_year_week(det)
        groups.setdefault((y,w), []).append(it)

    sorted_keys = sorted(groups.keys(), key=lambda k: (k[0], k[1]), reverse=True)

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
        f.write("<div class='notice'>Sidan uppdateras dagligen. Vi följer även mötessidor och listar bilagor endast i de agendapunkter vars rubrik innehåller ”Solfagraskolan”. PDF-länkar hittas även utan .pdf på slutet.</div>")

        for (y,w) in sorted_keys:
            items = sorted(groups[(y,w)], key=lambda it: it["detected_at"], reverse=True)
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

# ---------- Main ----------

def main():
    ensure_dirs()
    hist = load_history()
    known_urls = {it["url"] for it in hist["items"]}

    all_now = collect_all()
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
