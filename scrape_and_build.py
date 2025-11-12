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

# Håll nere anropen:
PAGE_SIZE = 50      # sökresultat per sida
MAX_PAGES = 10      # hur många resultatsidor vi hämtar (ofta räcker 1–3)

# MÖTESSIDOR (agenda) – strikt cap så vi inte hänger
MAX_AGENDA_PAGES = 2      # följ som mest 2 mötessidor per körning
MAX_ATTACH_TOTAL = 6      # samla som mest 6 bilagelänkar per körning

USER_AGENT = "Mozilla/5.0 (compatible; SolfagraskolanWatcher/stable-plus-1.0)"
HEADERS = {"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8"}
TIMEOUT = 8  # sek per request (get)
SLEEP   = 0.4

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
        return {"items": []}  # list of {url,title,detected_at,source}
    with open(HISTORY_FN, "r", encoding="utf-8") as f:
        return json.load(f)

def save_history(hist):
    ensure_dirs()
    with open(HISTORY_FN, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=2)

def fetch_page(page_index:int):
    params = {"text": QUERY, "pindex": page_index, "psize": PAGE_SIZE}
    url = f"{BASE}?{urlencode(params)}"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text, url

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
    # fånga "…pdf" utan punkt och med ev query/fragment
    return re.search(r"pdf($|[?#])", u) is not None

def parse_links(html_text:str, page_url:str):
    """
    Returnerar:
      direct_links: [(title,url,'direct')] där länktexten innehåller 'Solfagraskolan'
      agenda_candidates: [agenda_url] – mötessidor vi ev. följer
    """
    soup = BeautifulSoup(html_text, "html.parser")
    direct_links = []
    agenda_candidates = []

    def norm(s): return " ".join((s or "").split()).strip().lower()

    for a in soup.select("a[href]"):
        title_raw = (a.get_text(" ", strip=True) or "").strip()
        if not title_raw:
            continue

        href = a.get("href") or ""
        if not href: 
            continue
        href = _abs_href(href.strip(), page_url)

        # hoppa pagineringslänkar
        if href.startswith(BASE) and "pindex=" in href and "psize=" in href:
            continue

        # samla lite blocktext kring länken (2 nivåer upp)
        block = a
        block_text = title_raw
        for _ in range(2):
            block = block.parent if block else None
            if not block: break
            t = (block.get_text(" ", strip=True) or "").strip()
            if t: block_text = t

        # 1) Direkta träffar: länktext innehåller ordet
        if "solfagraskolan" in title_raw.lower():
            direct_links.append((title_raw, href, "direct"))
            continue

        # 2) Kandidat till mötessida om blocket matchar
        if "solfagraskolan" in norm(block_text):
            if any(seg in href for seg in ["/committees/", "/agenda", "/namnder-styrelser/", "/welcome-"]) and not _looks_like_pdf(href):
                agenda_candidates.append(href)

    # dedup
    seen = set()
    direct_links = [(t,u,s) for (t,u,s) in direct_links if not (u in seen or seen.add(u))]
    agenda_candidates = [u for u in agenda_candidates if not (u in seen or seen.add(u))]
    return direct_links, agenda_candidates

def extract_bilagor_from_agenda(url:str, remaining:int):
    """
    Hämta bilagor från en mötessida, men håll hård cap via 'remaining'.
    Vi tar bara länkar inom sektioner som för sin egen text innehåller 'Solfagraskolan'
    (rubrik/sektion/agenda-punkt) och som ser ut som dokument (pdf i URL).
    Returnerar [(title,url,'via-agenda')].
    """
    out = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        def norm(s): return " ".join((s or "").split()).strip()
        def low(s):  return norm(s).lower()

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
                if _looks_like_pdf(href) or "document" in href.lower() or "attachment" in href.lower():
                    seen.add(href)
                    title = norm(a.get_text(" ", strip=True)) or "Bilaga"
                    out.append((title, href, "via-agenda"))
                    remaining -= 1
            if remaining <= 0:
                break
    except Exception:
        # svälj fel – hellre få träffar än att hänga
        pass
    return out

def collect_all():
    """Huvudinsamling: först direkta träffar, sedan max 2 mötessidor för bilagor."""
    all_direct = []
    all_agenda = []
    first_page_agendas = []

    for p in range(1, MAX_PAGES+1):
        html_text, url = fetch_page(p)
        direct, agenda = parse_links(html_text, url)
        all_direct.extend(direct)
        all_agenda.extend(agenda)
        # spara kandidat-agendas från första sidan som högst prioritet
        if p == 1:
            first_page_agendas = agenda[:]

        # bryt tidigt om lite träffar
        if p == 1 and (len(direct) + len(agenda)) < PAGE_SIZE:
            break
        if len(direct) + len(agenda) == 0:
            break
        time.sleep(SLEEP)

    # välj vilka mötessidor att följa (prioritera första resultatsidan)
    agendas_to_follow = (first_page_agendas + [u for u in all_agenda if u not in first_page_agendas])[:MAX_AGENDA_PAGES]

    # hämta bilagor, men cap:a totala antalet
    remaining = MAX_ATTACH_TOTAL
    attachments = []
    for ap in agendas_to_follow:
        if remaining <= 0:
            break
        attachments.extend(extract_bilagor_from_agenda(ap, remaining))
        remaining = MAX_ATTACH_TOTAL - len(attachments)
        time.sleep(SLEEP)

    # slå ihop + dedup
    seen = set()
    merged = []
    for t,u,s in (all_direct + attachments):
        if u in seen:
            continue
        seen.add(u)
        merged.append((t,u,s))
    return merged

def iso_year_week(dt: datetime):
    y, w, _ = dt.isocalendar()
    return y, w

def build_site(hist):
    # Grupp efter ISO-vecka
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
        f.write("<div class='notice'>Stabilt läge med begränsad agenda-uppföljning (max 2 sidor & 6 bilagor per körning). PDF hittas även om URL inte slutar på .pdf.</div>")

        for (y,w) in sorted_keys:
            items = sorted(groups[(y,w)], key=lambda it: it["detected_at"], reverse=True)
            f.write(f"<section class='week'><h2>Vecka {w}, {y}</h2>")
            for it in items:
                title = html.escape(it["title"])
                url = html.escape(it["url"])
                det = html.escape(it["detected_at"].replace("T"," ")[:16])
                src = it.get("source","")
                badge = ""
                if src == "via-agenda":
                    badge = "<span class='badge'>via ärendepunkt</span>"
                elif src == "direct":
                    badge = "<span class='badge'>träff i länktext</span>"
                f.write("<div class='item'>")
                f.write(f"<a href='{url}' target='_blank' rel='noopener'>{title}</a> {badge}")
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
    new = [(t,u,s) for (t,u,s) in all_now if u not in known_urls]

    now_iso = datetime.now(timezone.utc).astimezone().isoformat(timespec="minutes")
    for (title,url,source) in new:
        hist["items"].append({
            "url": url,
            "title": title,
            "detected_at": now_iso,
            "source": source
        })

    save_history(hist)
    build_site(hist)

    print(f"Nya länkar denna körning: {len(new)}")
    print(f"Totalt i historik: {len(hist['items'])}")
    print(f"Genererat {INDEX_HTML}")

if __name__ == "__main__":
    main()
