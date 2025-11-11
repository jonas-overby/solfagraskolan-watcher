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

def extract_attachments_from_agenda(url:str):
    """
    Öppnar en ärendesida/punkt och returnerar [(title, url)] för *alla bilagor* på sidan.
    Heuristik:
      - länkar som slutar på .pdf
      - eller länkar som ligger i sektioner som innehåller ord som 'Bilaga', 'Attachments'
    Vi plockar rubriken (om någon) och annars länktexten.
    """
    out = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        def norm(s): return " ".join((s or "").split()).strip()

        # 1) Titta i sektioner som sannolikt rymmer bilagor
        candidate_sections = []
        for sec in soup.find_all(True):
            txt = (sec.get_text(" ", strip=True) or "").lower()
            if any(k in txt for k in ["bilaga", "bilagor", "attachments", "documents"]):
                candidate_sections.append(sec)

        # 2) Hitta pdf-länkar i dessa sektioner (fallback: överallt)
        def collect_links(context):
            items = []
            anchors = context.select("a[href]") if hasattr(context, "select") else soup.select("a[href]")
            for a in anchors:
                href = a.get("href", "").strip()
                if not href:
                    continue
                # Absolut-ifiera
                if href.startswith("//"):
                    href = "https:" + href
                elif href.startswith("/"):
                    href = urljoin(url, href)
                elif not href.startswith("http"):
                    href = urljoin(url, href)

                # Filtrera på dokument-liknande länkar
                if href.lower().endswith(".pdf") or "document" in href.lower() or "attachment" in href.lower():
                    title = norm(a.get_text(" ", strip=True)) or "Bilaga"
                    items.append((title, href))
            return items

        items = []
        if candidate_sections:
            for sec in candidate_sections:
                items.extend(collect_links(sec))
        else:
            # fallback: sök pdf-länkar var som helst
            items.extend(collect_links(soup))

        # dedup
        seen = set()
        for t,u in items:
            if u in seen: 
                continue
            seen.add(u)
            out.append((t,u))
    except Exception:
        pass

    return out


def parse_links(html_text:str, page_url:str):
    """
    Returnerar två listor:
      direct_links: [(title, url)] där själva länktexten innehåller 'Solfagraskolan'
      agenda_pages: [url] med ärendesidor/punkter där *blockets rubrik/kontext* matchar ordet
    Heuristik: vi tittar på föräldra-/syskonblockets text när en länk ser ut att vara en 'Punkt'/'Ärende'.
    """
    soup = BeautifulSoup(html_text, "html.parser")

    direct_links = []
    agenda_pages = []

    # Hjälp: normaliserad text
    def norm(s: str) -> str:
        return " ".join(s.split()).strip().lower()

    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        if not href:
            continue

        # Absolut URL
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = urljoin(page_url, href)
        elif not href.startswith("http"):
            href = urljoin(page_url, href)

        # Filtrera bort pagineringslänkar
        if href.startswith(BASE) and "pindex=" in href and "psize=" in href:
            continue

        title_text = norm(a.get_text(" ", strip=True))
        block_text = title_text

        # Samla lite kontext runt länken (upp till 2 nivåer upp)
        p = a.parent
        for _ in range(2):
            if not p:
                break
            block_text = norm(p.get_text(" ", strip=True)) or block_text
            p = p.parent

        # 1) Direkta dokumentlänkar: länktexten innehåller ordet
        if "solfagraskolan" in title_text:
            direct_links.append((" ".join(a.get_text(" ", strip=True).split()), href))
            continue

        # 2) Ärendesidor/Punkter: blocket innehåller ordet
        #    (t.ex. "Genomförandebeslut för Solfagraskolan …", där bilagan saknar ordet)
        #    Heuristik: länken bör peka på portalens HTML-sida (inte direkt .pdf)
        looks_like_agenda = ("/agenda" in href) or ("/namnder-styrelser/" in href) or ("/welcome-" in href)
        if "solfagraskolan" in block_text and looks_like_agenda and not href.lower().endswith(".pdf"):
            agenda_pages.append(href)

    # Rensa dubletter
    seen = set()
    direct_links = [(t,u) for (t,u) in direct_links if not (u in seen or seen.add(u))]
    agenda_pages = [u for u in agenda_pages if not (u in seen or seen.add(u))]

    return direct_links, agenda_pages

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
    """
    Hämtar alla sökresultatssidor och returnerar:
      - alla direkta länkar som matchar 'Solfagraskolan' i länktexten
      - alla bilagor från ärendesidor där *blocktexten* matchar 'Solfagraskolan'
    """
    direct = []
    agenda_pages = []

    for p in range(1, MAX_PAGES+1):
        html_text, url = fetch_page(p)
        d, a = parse_links(html_text, url)
        direct.extend(d)
        agenda_pages.extend(a)
        if p == 1 and len(d) + len(a) < PAGE_SIZE:
            break
        if (len(d) + len(a)) == 0:
            break
        time.sleep(0.5)

    # Hämta bilagor från alla ärendesidor
    attachments = []
    seen_agenda = set()
    for ap in agenda_pages:
        if ap in seen_agenda:
            continue
        seen_agenda.add(ap)
        attachments.extend(extract_attachments_from_agenda(ap))
        time.sleep(0.4)  # vara snäll mot servern

    # Slå ihop och deduplicera
    all_items = []
    seen = set()
    for t,u in (direct + attachments):
        if u in seen:
            continue
        seen.add(u)
        all_items.append((t,u))

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
