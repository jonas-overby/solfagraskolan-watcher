#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, time, html, re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlencode
import requests
from bs4 import BeautifulSoup

# --------- Konfiguration ----------
BASE = "https://sammantraden.huddinge.se/search"
QUERY = "Solfagraskolan"

PAGE_SIZE = 50     # poster per söksida
MAX_PAGES = 10     # söksidor att hämta (ofta räcker 1–3)

# Följ få mötessidor för bilagor (anti-häng):
MAX_AGENDA_PAGES = 2
MAX_ATTACH_TOTAL = 6

USER_AGENT = "Mozilla/5.0 (compatible; SolfagraskolanWatcher/date-sort-plus-1.0)"
HEADERS = {"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8"}
TIMEOUT = 8  # sek per request
SLEEP   = 0.4
# ----------------------------------

# Paths
DATA_DIR   = "data"
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
        return {"items": []}  # list of {url,title,detected_at,document_date,source}
    with open(HISTORY_FN, "r", encoding="utf-8") as f:
        return json.load(f)

def save_history(hist):
    ensure_dirs()
    with open(HISTORY_FN, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=2)

def _abs_href(href: str, base_url: str) -> str:
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return urljoin(base_url, href)
    if not href.startswith("http"):
        return urljoin(base_url, href)
    return href

def _looks_like_pdf(url: str) -> bool:
    u = url.lower()
    if u.endswith(".pdf"):
        return True
    # fångar "…pdf" utan punkt, ev. med query/fragment
    return re.search(r"pdf($|[?#])", u) is not None

def fetch_page(page_index:int):
    params = {"text": QUERY, "pindex": page_index, "psize": PAGE_SIZE}
    url = f"{BASE}?{urlencode(params)}"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text, url

def parse_links(html_text:str, page_url:str):
    """
    Plockar direkta träffar + kandidat-mötessidor från sökresultatet
    baserat på div.resultitem:
      - Länk (<a>) vars text innehåller "Solfagraskolan" => direct
      - Datum i samma block (<span class="date">) => document_date
      - Mötesside-länkar (~/committees, /agenda, /namnder-styrelser, /welcome-) sparas i agenda_candidates
        tillsammans med det datum som visas i resultatblocket.
    Returnerar:
      direct_items: [(title, url, document_date, "direct")]
      agenda_candidates: [(agenda_url, document_date)]
    """
    soup = BeautifulSoup(html_text, "html.parser")
    direct_items = []
    agenda_candidates = []

    for item in soup.select("div.resultitem"):
        a = item.select_one("a[href]")
        if not a:
            continue
        title = (a.get_text(" ", strip=True) or "").strip()
        href = _abs_href((a.get("href") or "").strip(), page_url)
        # datum i samma block
        date_el = item.select_one(".date")
        doc_date = date_el.get_text(strip=True) if date_el else None

        if "solfagraskolan" in title.lower():
            direct_items.append((title, href, doc_date, "direct"))

        # markera mötessidor att följa (ej pdf)
        if any(seg in href for seg in ["/committees/", "/agenda", "/namnder-styrelser/", "/welcome-"]) and not _looks_like_pdf(href):
            # även om a-texten inte hade Solfagraskolan kan blocket vara relevant
            block_txt = (item.get_text(" ", strip=True) or "").lower()
            if "solfagraskolan" in block_txt:
                agenda_candidates.append((href, doc_date))

    # dedup
    seen = set()
    direct_items = [(t,u,d,s) for (t,u,d,s) in direct_items if not (u in seen or seen.add(u))]
    agenda_candidates = [(u,d) for (u,d) in agenda_candidates if not (u in seen or seen.add(u))]
    return direct_items, agenda_candidates

def try_parse_date(s: str):
    """Tolka 2025-11-26, 26/11/2025 etc. Fallback: plocka YYYY-MM-DD via regex."""
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    m = re.search(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d")
        except Exception:
            return None
    return None

def _extract_any_date_from_soup(soup: BeautifulSoup):
    """
    Försök hitta ett mötesdatum på mötessidan.
    Vi scannar texten efter ett YYYY-MM-DD (t.ex. i sidhuvud, metadata).
    """
    txt = soup.get_text(" ", strip=True)
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", txt)
    return m.group(1) if m else None

def extract_bilagor_from_agenda(url:str, fallback_doc_date:str, remaining:int):
    """
    Hämtar bilagor (pdf/dokument) från agendapunkter som nämner "Solfagraskolan".
    - Försöker hämta ett datum från sidan; annars används fallback_doc_date från sökresultatet.
    - Avbryter när 'remaining' bilagor har samlats.
    Returnerar: [(title, url, document_date, "via-agenda")]
    """
    out = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        agenda_date = _extract_any_date_from_soup(soup) or fallback_doc_date

        # hitta sektioner/rubriker som nämner Solfagraskolan
        candidate_sections = []
        for node in soup.find_all(True):
            txt = (node.get_text(" ", strip=True) or "")
            if "solfagraskolan" in txt.lower():
                candidate_sections.append(node)

        seen = set()
        for sec in candidate_sections:
            for a in sec.select("a[href]"):
                if remaining <= 0:
                    return out
                href = a.get("href") or ""
                if not href:
                    continue
                href = _abs_href(href.strip(), url)
                if href in seen:
                    continue
                # ta dokument-liknande länkar
                if _looks_like_pdf(href) or "document" in href.lower() or "attachment" in href.lower():
                    seen.add(href)
                    title = (a.get_text(" ", strip=True) or "").strip() or "Bilaga"
                    out.append((title, href, agenda_date, "via-agenda"))
                    remaining -= 1
            if remaining <= 0:
                break
    except Exception:
        # Låt bli att krascha – hellre få länkar än stoppad körning
        pass
    return out

def collect_all():
    """
    1) Hämta sökresultat → direkta träffar + mötessidor (med deras datum).
    2) Följ högst MAX_AGENDA_PAGES mötessidor (prioritera första sidans kandidater)
       och plocka max MAX_ATTACH_TOTAL bilagor.
    3) Returnera [(title, url, document_date, source)]
    """
    all_direct = []
    all_agenda = []
    first_page_agendas = []

    for p in range(1, MAX_PAGES+1):
        html_text, url = fetch_page(p)
        direct, agendas = parse_links(html_text, url)
        all_direct.extend(direct)
        all_agenda.extend(agendas)
        if p == 1:
            first_page_agendas = agendas[:]

        # bryt tidigt om det är lite träffar
        if p == 1 and (len(direct) + len(agendas)) < PAGE_SIZE:
            break
        if len(direct) + len(agendas) == 0:
            break
        time.sleep(SLEEP)

    # välj vilka mötessidor som ska följas (prioritera första sidans kandidater)
    # agenda-kandidater är tuples (url, doc_date)
    prioritized = first_page_agendas + [ad for ad in all_agenda if ad not in first_page_agendas]
    agendas_to_follow = prioritized[:MAX_AGENDA_PAGES]

    # hämta bilagor
    remaining = MAX_ATTACH_TOTAL
    attachments = []
    for (ap_url, ap_date) in agendas_to_follow:
        if remaining <= 0:
            break
        attachments.extend(extract_bilagor_from_agenda(ap_url, ap_date, remaining))
        remaining = MAX_ATTACH_TOTAL - len(attachments)
        time.sleep(SLEEP)

    # slå ihop + dedup på URL
    seen = set()
    merged = []
    for t,u,d,s in (all_direct + attachments):
        if u in seen:
            continue
        seen.add(u)
        merged.append((t,u,d,s))
    return merged

def build_site(hist):
    """
    En enda lista, sorterad fallande på document_date (fallback: detected_at).
    """
    items = hist["items"]

    def sort_key(it):
        d = try_parse_date(it.get("document_date"))
        if d:
            return d
        try:
            return datetime.fromisoformat(it["detected_at"])
        except Exception:
            return datetime.min

    items_sorted = sorted(items, key=sort_key, reverse=True)

    # CSS
    with open(STYLE_CSS, "w", encoding="utf-8") as f:
        f.write("""
html,body{margin:0;padding:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,Inter,Arial,sans-serif;background:#f8fafc;}
header{padding:24px 16px;background:#0f172a;color:#fff;}
header h1{margin:0;font-size:24px;font-weight:700;}
main{max-width:980px;margin:0 auto;padding:16px;}
.item{padding:10px 0;border-bottom:1px solid #e2e8f0;}
.item a{font-weight:600;text-decoration:none;}
.item a:hover{text-decoration:underline;}
.meta{color:#475569;font-size:13px;margin-top:4px}
.badge{display:inline-block;padding:2px 6px;border-radius:6px;background:#eef2ff;color:#3730a3;font-size:12px;margin-left:6px}
.notice{background:#f1f5f9;border:1px solid #e2e8f0;padding:10px;border-radius:8px;margin:16px 0;}
footer{max-width:980px;margin:32px auto 48px auto;padding:0 16px;color:#475569;font-size:13px;}
        """.strip())

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(INDEX_HTML, "w", encoding="utf-8") as f:
        f.write("<!doctype html><html lang='sv'><meta charset='utf-8'>")
        f.write("<meta name='viewport' content='width=device-width, initial-scale=1'>")
        f.write("<title>Dokument – Solfagraskolan</title>")
        f.write("<link rel='stylesheet' href='style.css'>")
        f.write("<header><h1>Dokument – Solfagraskolan</h1></header>")
        f.write("<main>")
        f.write("<div class='notice'>Listan sorteras efter datumet som visas på Huddinges sida (oftast mötesdatum). Nyaste datum överst. Bilagor från agendapunkter som nämner ”Solfagraskolan” inkluderas i begränsad mängd.</div>")

        for it in items_sorted:
            title = html.escape(it["title"])
            url = html.escape(it["url"])
            doc_date = html.escape(it.get("document_date") or "")
            det = html.escape(it["detected_at"].replace("T"," ")[:16])
            src = it.get("source", "")
            badge = ""
            if src == "via-agenda":
                badge = "<span class='badge'>via ärendepunkt</span>"
            elif src == "direct":
                badge = "<span class='badge'>träff i länktext</span>"

            f.write("<div class='item'>")
            f.write(f"<a href='{url}' target='_blank' rel='noopener'>{title}</a> {badge}")
            if doc_date:
                f.write(f"<div class='meta'>Datum: {doc_date}</div>")
            else:
                f.write(f"<div class='meta'>Upptäckt: {det}</div>")
            f.write("</div>")

        f.write("</main>")
        f.write(f"<footer>Senast genererad: {now}. Källa: <a href='{BASE}?text={QUERY}'>MeetingPlus sök</a>.</footer>")
        f.write("</html>")

def main():
    ensure_dirs()
    hist = load_history()
    known_urls = {it["url"] for it in hist["items"]}
    now_iso = datetime.now(timezone.utc).astimezone().isoformat(timespec="minutes")

    all_now = collect_all()  # [(title,url,document_date,source)]
    new = [(t,u,d,s) for (t,u,d,s) in all_now if u not in known_urls]

    for (title,url,doc_date,source) in new:
        hist["items"].append({
            "url": url,
            "title": title,
            "detected_at": now_iso,
            "document_date": doc_date,
            "source": source
        })

    save_history(hist)
    build_site(hist)
    print(f"Nya länkar: {len(new)}")
    print(f"Totalt i historik: {len(hist['items'])}")
    print(f"Genererat {INDEX_HTML}")

if __name__ == "__main__":
    main()
