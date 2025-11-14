#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
alertme_immokh.py — Immo-KH (À vendre) – Pagination robuste
Deux modes de collecte:
1) Mode HTTP (GET/POST AJAX) par curseur d'ID (léger, sans navigateur)
2) Mode Navigateur (Playwright Chromium non-headless) avec fin de scroll PROUVÉE:
   - Attente explicite de la réponse /List/InfiniteScroll après chaque clic
   - Stabilité du scrollHeight sur N cycles + plus de bouton 'load more'
   - Dernier sweep de sécurité

API exportée (pour batch_alertme.py):
  run_once(url: str|None, email: str, pages: int, filters: dict|None = None, **kwargs)
"""

import argparse, os, re, json, time, logging, hashlib, smtplib, ssl, html as htmllib
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qs, urlencode, unquote
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

# ---------------- Config
DATA_DIR        = os.path.join(".", "data")
STATE_PATH      = os.path.join(DATA_DIR, "state_immokh.json")
REQUEST_TIMEOUT = 25
POLITE_SLEEP    = 0.6
DEFAULT_PAGES   = 30

SITE_HOST = "www.immo-kh.be"
LIST_URL  = "https://www.immo-kh.be/fr/2/chercher-bien/a-vendre"

BASE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/128.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}
AJAX_HEADERS = dict(BASE_HEADERS)
AJAX_HEADERS.update({
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "text/html, */*; q=0.01",
})

DETAIL_PATH_RE = re.compile(r"/fr/bien/", re.I)
ID_NUM_RE      = re.compile(r"/(\d{5,})(?:$|[/?#])")
BAD_HREF_BITS  = ("InfiniteScroll", "List/InfiniteScroll?json=", "javascript:", "mailto:", "tel:", "#")

TYPE_ALIASES = {
    "maison":      ["maison","villa","house","woning"],
    "appartement": ["appartement","apartment","flat","appart"],
    "penthouse":   ["penthouse"],
    "duplex":      ["duplex"],
    "studio":      ["studio","kot"],
    "terrain":     ["terrain","terrain à bâtir","terrain a batir","grond","land"],
    "bureau":      ["bureau","office"],
    "commerce":    ["commerce","rez-commercial","retail","shop"],
    "industriel":  ["industriel","industrie","entrepôt","entrepot","warehouse"],
    "garage":      ["garage","parking","box"],
}

# ---------------- Logging / State
def setup_logging():
    lvl = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, lvl, logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

def ensure_data_dir(): os.makedirs(DATA_DIR, exist_ok=True)
def utc_now_iso() -> str: return datetime.now(timezone.utc).isoformat()

def load_state() -> Dict[str, Any]:
    ensure_data_dir()
    if not os.path.isfile(STATE_PATH): return {"alerts": {}}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f: return json.load(f)
    except Exception as e:
        try: os.replace(STATE_PATH, STATE_PATH + ".bak")
        except Exception: pass
        logging.error("Lecture %s impossible (%s). Reset.", STATE_PATH, e)
        return {"alerts": {}}

def save_state(state: Dict[str, Any]) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)

def _state_key(base_url: str, filters: Optional[Dict[str, Any]]) -> str:
    payload = json.dumps(filters or {}, sort_keys=True, ensure_ascii=False)
    h = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return f"{base_url}#filters={h}"

# ---------------- HTTP
def build_session() -> requests.Session:
    s = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
        raise_on_status=False
    )
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.mount("http://",  HTTPAdapter(max_retries=retries))
    return s

def fetch(session: requests.Session, url: str, referer: Optional[str]=None) -> Tuple[Optional[str], Optional[int]]:
    headers = dict(BASE_HEADERS)
    if referer: headers["Referer"] = referer
    r = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    logging.info("GET %s → %s (len=%s)", url, r.status_code, len(r.text) if r.text else 0)
    return (r.text if r.ok else None, r.status_code)

def fetch_inf_fragment(session: requests.Session, url_or_geturl: str, payload_json: Optional[dict],
                       referer: str) -> Tuple[Optional[str], Optional[int], str]:
    # 1) GET(AJAX)
    r = session.get(url_or_geturl, headers=AJAX_HEADERS | {"Referer": referer}, timeout=REQUEST_TIMEOUT)
    logging.info("GET(AJAX) %s → %s (len=%s)", url_or_geturl, r.status_code, len(r.text) if r.text else 0)
    if r.ok and r.text and ("/fr/bien/" in r.text or 'class="estate-card' in r.text):
        return r.text, r.status_code, "GET"

    # 2) POST(AJAX) sur le path
    parts = urlsplit(url_or_geturl)
    base = urlunsplit((parts.scheme, parts.netloc, parts.path, "", parts.fragment))
    data = {}
    if payload_json is not None:
        data["json"] = json.dumps(payload_json, separators=(",", ":"), ensure_ascii=False)
    r = session.post(base, headers=AJAX_HEADERS | {"Referer": referer}, data=data, timeout=REQUEST_TIMEOUT)
    logging.info("POST(AJAX) %s [form json] → %s (len=%s)", base, r.status_code, len(r.text) if r.text else 0)
    if r.ok and r.text:
        return r.text, r.status_code, "POST"
    return None, (r.status_code if r is not None else None), "POST"

# ---------------- Helpers extraction
def _abs(url: str) -> str:
    url = (url or "").strip()
    return url if url.startswith("http") else urljoin(f"https://{SITE_HOST}", url)

def _bad_href(href: str) -> bool:
    h = (href or "").lower()
    return any(b.lower() in h for b in BAD_HREF_BITS)

def _extract_id(href: str) -> Optional[str]:
    m = ID_NUM_RE.search(href or "")
    return m.group(1) if m else None

def _int_price(text: str) -> Optional[int]:
    s = re.sub(r"[^\d]", "", text or "")
    return int(s) if s else None

def _extract_bedrooms(text: str) -> Optional[int]:
    m = re.search(r"(\d+)\s*(?:ch|chambre|chambres|slaapkamers?|kamers?)\b", text or "", re.I)
    return int(m.group(1)) if m else None

def _classify_type(text: str) -> Optional[str]:
    t = (text or "").lower()
    for canonical, words in TYPE_ALIASES.items():
        if any(w in t for w in words): return canonical
    return None

# ---------------- Parseurs
def _parse_cards_from_html(html: str, debug_label: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")

    strict = soup.select('a.estate-card[href*="/fr/bien/"]')
    logging.info("[%s] ancres strictes .estate-card: %d", debug_label, len(strict))

    anchors = strict or [a for a in soup.find_all("a", href=True) if "/fr/bien/" in a["href"]]
    if not strict:
        logging.info("[%s] Fallback ancres href*='/fr/bien/': %d", debug_label, len(anchors))

    out: Dict[str, Dict[str, Any]] = {}
    for a in anchors:
        href = (a.get("href") or "").strip()
        if not href or _bad_href(href) or not DETAIL_PATH_RE.search(href):
            continue
        pid = _extract_id(href)
        if not pid:
            continue
        if pid in out:
            continue

        url = _abs(href)

        # remonte pour saisir texte/prix/ville
        card = a
        for _ in range(5):
            if card and card.parent: card = card.parent

        title = (a.get_text(" ", strip=True) or "").strip()
        if not title and card:
            tnode = card.select_one(".estate-card__text, .estate-card__text-details, .entry-title, [class*=title]")
            if tnode: title = (tnode.get_text(" ", strip=True) or "").strip()

        price = None
        price_node = card.select_one("span.estate-card__text-details-price") if card else None
        if price_node:
            price = _int_price(price_node.get_text(" ", strip=True))
        if price is None and card:
            for t in card.find_all(["div","span","p","strong","b"]):
                s = (t.get_text(" ", strip=True) or "")
                if "€" in s or "eur" in s.lower():
                    price = _int_price(s)
                    if price: break

        city = ""
        if card:
            loc = card.select_one(".estate-card__text-details-location")
            if loc:
                city = (loc.get_text(" ", strip=True) or "").strip()
        if not city and card:
            card_text = card.get_text(" ", strip=True)
            m_city = re.search(r"\b([A-Z][A-Za-zÀ-ÿ\- ]{2,})\b", card_text or "")
            if m_city: city = m_city.group(1).strip()

        card_text = card.get_text(" ", strip=True) if card else ""
        out[pid] = {
            "id": pid,
            "url": url,
            "title": title,
            "price": price,
            "city": city,
            "bedrooms": _extract_bedrooms(card_text),
            "type": _classify_type((title + " " + card_text)),
            "publication_date": None,
        }

    logging.info("[%s] cartes retenues (ID unique): %d", debug_label, len(out))
    return list(out.values())

def _find_infinitescroll_href(list_page_html: str) -> Optional[str]:
    soup = BeautifulSoup(list_page_html, "html.parser")
    a = soup.select_one('div.infinite-scroll a[href*="/fr/List/InfiniteScroll"]')
    href = (a.get("href") or "").strip() if a else ""
    if href:
        logging.info("InfiniteScroll anchor found: %s", href)
        return _abs(href)
    logging.warning("InfiniteScroll anchor introuvable sur la page liste.")
    return None

# ---------------- InfiniteScroll helpers (HTTP)
def _decode_inf_payload(inf_url: str) -> Tuple[str, dict]:
    parts = urlsplit(inf_url)
    q = parse_qs(parts.query)
    raw = q.get("json", ["{}"])[0]
    try:
        payload = json.loads(raw)
    except Exception:
        payload = json.loads(unquote(raw))
    base = urlunsplit((parts.scheme, parts.netloc, parts.path, "", parts.fragment))
    return base, payload

def _encode_inf_url(base: str, payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    query = urlencode({"json": raw})
    return urlunsplit(urlsplit(base)._replace(query=query))

def _payload_first_page(base_payload: Dict[str, Any], page_size: int = 12) -> Dict[str, Any]:
    p = dict(base_payload) if base_payload else {}
    p["SortParameter"] = p.get("SortParameter", 5)  # newest
    p["MaxItemsPerPage"] = page_size
    p["PageNumber"] = 0
    p["FirstPage"] = True
    p["CanGetNextPage"] = False
    p["BaseEstateID"] = p.get("BaseEstateID", 0)
    return p

def _payload_cursor(base_payload: Dict[str, Any], base_estate_id: int, page_size: int) -> Dict[str, Any]:
    p = dict(base_payload) if base_payload else {}
    p["SortParameter"] = p.get("SortParameter", 5)
    p["MaxItemsPerPage"] = page_size
    p["BaseEstateID"] = int(base_estate_id)
    p["FirstPage"] = False
    p["CanGetNextPage"] = True
    p.pop("PageNumber", None)
    return p

def _min_int_id(items: List[Dict[str, Any]]) -> Optional[int]:
    ints = []
    for it in items:
        try:
            ints.append(int(it["id"]))
        except Exception:
            pass
    return min(ints) if ints else None

# ---------------- Collecteur HTTP (cursor only)
def _collect_all_pages(session: requests.Session, first_inf_url: str, pages: int,
                       debug_save_html: bool=False, debug_dump: bool=False) -> List[Dict[str, Any]]:
    all_items: List[Dict[str, Any]] = []
    seen: set = set()
    base, base_payload = _decode_inf_payload(first_inf_url)

    # Page 1 (12 puis 48)
    for psize in (12, 48):
        p1_payload = _payload_first_page(base_payload, psize)
        p1_url = _encode_inf_url(base, p1_payload)
        html, st, meth = fetch_inf_fragment(session, p1_url, p1_payload, referer=LIST_URL)
        if not html or (st and st >= 400):
            continue
        if debug_save_html:
            ensure_data_dir()
            with open(os.path.join(DATA_DIR, f"immokh_inf_p1_sz{psize}.html"), "w", encoding="utf-8") as f: f.write(html)
        if "search404.png" in html or "aucune de nos propriétés" in html.lower():
            continue
        items = _parse_cards_from_html(html, f"InfiniteScroll p1 sz{psize} [{meth}]")
        if items:
            for it in items:
                pid = it.get("id")
                if pid and pid not in seen:
                    seen.add(pid); all_items.append(it)
            break

    if not all_items:
        logging.warning("p1: aucune carte trouvée.")
        return all_items

    logging.info("[p1] total=%d", len(all_items))
    cursor_id = _min_int_id(all_items)
    if not cursor_id:
        logging.info("Impossible d’initialiser le curseur (IDs manquants).")
        return all_items

    # Pages suivantes via curseur
    max_pages = max(1, pages)
    page_index = 1
    while page_index < max_pages:
        page_index += 1
        advanced = False
        for psize in (12, 48):
            try_cursor = cursor_id - 1
            pay = _payload_cursor(base_payload, try_cursor, psize)
            url_candidate = _encode_inf_url(base, pay)

            html2, st2, meth2 = fetch_inf_fragment(session, url_candidate, pay, referer=LIST_URL)
            if not html2 or (st2 and st2 >= 400):
                continue
            if debug_save_html:
                with open(os.path.join(DATA_DIR, f"immokh_inf_p{page_index}_sz{psize}.html"), "w", encoding="utf-8") as f: f.write(html2)
            if "search404.png" in html2 or "aucune de nos propriétés" in html2.lower():
                continue

            test_items = _parse_cards_from_html(html2, f"InfiniteScroll p{page_index} sz{psize} [{meth2}]")
            if not test_items:
                continue

            addc = 0
            new_min = None
            for it in test_items:
                pid = it.get("id")
                if not pid: continue
                try:
                    ival = int(pid)
                except Exception:
                    continue
                if ival < cursor_id and pid not in seen:
                    seen.add(pid); all_items.append(it); addc += 1
                    if new_min is None or ival < new_min:
                        new_min = ival

            if addc == 0:
                logging.info("p%d sz%d: 0 nouvelle carte (<%d) — on tente autre taille/méthode.",
                             page_index, psize, cursor_id)
                continue

            cursor_id = new_min if new_min is not None else cursor_id
            logging.info("[p%d] via CURSOR(BaseEstateID=%d) sz=%d/%s → +%d (total=%d) | new cursor=%d",
                         page_index, try_cursor, psize, meth2, addc, len(all_items), cursor_id)
            advanced = True
            break

        if not advanced:
            logging.info("p%d: aucune combinaison n’a apporté d’IDs plus petits → fin.", page_index)
            break

        time.sleep(POLITE_SLEEP)

    return all_items

# ---------------- Collecteur Navigateur (Playwright)
def _collect_with_browser(pages_max: int, debug_save_html: bool=False) -> List[Dict[str, Any]]:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except Exception:
        logging.error("Playwright non installé. pip install playwright && python -m playwright install chromium")
        return []

    N_STABLE = 3
    WAIT_BETWEEN = 0.6
    CLICK_TIMEOUT = 8000  # ms
    MAX_CLICKS = max(5, pages_max)

    def _count_cards(page) -> int:
        return page.evaluate("document.querySelectorAll('a.estate-card[href*=\"/fr/bien/\"]').length")

    def _scroll_height(page) -> int:
        return page.evaluate("document.body.scrollHeight")

    def _has_load_more(page) -> bool:
        return page.evaluate("""() => {
            const a = document.querySelector("div.infinite-scroll a[href*='/fr/List/InfiniteScroll']");
            if (!a) return false;
            const r = a.getBoundingClientRect();
            const vis = !!(r.width || r.height);
            const styles = window.getComputedStyle(a);
            return vis && styles.display !== 'none' && styles.visibility !== 'hidden' && !a.classList.contains('disabled');
        }""")

    def _click_load_more_and_wait(page) -> Tuple[bool, Optional[str]]:
        locator = page.locator("div.infinite-scroll a[href*='/fr/List/InfiniteScroll']")
        if locator.count() == 0:
            return False, None
        try:
            locator.first.wait_for(state="visible", timeout=2000)
        except PWTimeout:
            return False, None

        def _is_inf(resp):
            try:
                u = resp.url
                return ("/List/InfiniteScroll" in u) and resp.ok
            except Exception:
                return False

        locator.first.click()
        try:
            resp = page.wait_for_response(_is_inf, timeout=CLICK_TIMEOUT)
        except PWTimeout:
            return True, None
        try:
            txt = resp.text()
            return True, txt
        except Exception:
            return True, None

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=BASE_HEADERS["User-Agent"], viewport={"width":1280,"height":900})
        page = ctx.new_page()

        logging.info("Ouverture navigateur → %s", LIST_URL)
        page.goto(LIST_URL, wait_until="networkidle", timeout=60000)

        page.evaluate("window.scrollTo(0, document.body.scrollHeight/3)")
        time.sleep(0.3)
        page.evaluate("window.scrollTo(0, 0)")

        stable_cycles = 0
        last_cards = _count_cards(page)
        last_height = _scroll_height(page)
        clicks = 0
        end_by_backend_empty = False

        while clicks < MAX_CLICKS:
            clicked, fragment = _click_load_more_and_wait(page)
            if clicked:
                clicks += 1

            if fragment is not None and ('/fr/bien/' not in fragment and 'class="estate-card' not in fragment):
                logging.info("Réponse /List/InfiniteScroll sans nouvelle carte → fin backend atteinte.")
                end_by_backend_empty = True

            for _ in range(3):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                try:
                    page.wait_for_load_state("networkidle", timeout=3500)
                except Exception:
                    pass
                time.sleep(WAIT_BETWEEN)

            now_cards = _count_cards(page)
            now_height = _scroll_height(page)
            got_more_cards = now_cards > last_cards
            grew_in_height = now_height > last_height
            has_btn = _has_load_more(page)

            if got_more_cards or grew_in_height:
                stable_cycles = 0
                last_cards = now_cards
                last_height = now_height
                continue

            stable_cycles += 1
            logging.info("Cycle sans progrès (%d/%d) · cartes=%d · height=%d · load_more=%s",
                         stable_cycles, N_STABLE, now_cards, now_height, "oui" if has_btn else "non")

            if stable_cycles >= N_STABLE and (not has_btn) and (end_by_backend_empty or stable_cycles >= N_STABLE):
                logging.info("Infinite scroll confirmé terminé : cartes=%d, btn_load_more=%s, backend_empty=%s.",
                             now_cards, "non" if not has_btn else "oui", "oui" if end_by_backend_empty else "non")
                break

            time.sleep(WAIT_BETWEEN)

        # sweep final
        final_before = _count_cards(page)
        for _ in range(2):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            try:
                page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass
            time.sleep(0.5)
        final_after = _count_cards(page)
        if final_after > final_before:
            logging.info("Dernier sweep a encore trouvé %d cartes (total=%d) — on les garde.",
                         final_after - final_before, final_after)

        final_html = page.content()
        items = _parse_cards_from_html(final_html, "Browser DOM final")
        try:
            browser.close()
        except Exception:
            pass

    return items

# ---------------- Filtres
def _norm_types(ts: Optional[List[str]]) -> List[str]:
    return [str(t).strip().lower() for t in (ts or []) if str(t).strip()]

def apply_filters(items: List[Dict[str, Any]], filters: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not filters: return items
    city_q = " ".join([c.strip().lower() for c in (filters.get("cities") or [])])
    price_min = filters.get("price_min")
    price_max = filters.get("price_max")
    bedrooms_min = filters.get("bedrooms_min")
    bathrooms_min = filters.get("bathrooms_min")
    area_min = filters.get("area_min")
    types = _norm_types(filters.get("property_types"))
    include_sold = bool(filters.get("include_sold", False))

    out = []
    for it in items:
        txt_all = " ".join(str(it.get(k) or "") for k in ["title","city"]).lower()

        if city_q and not any(c in txt_all for c in city_q.split()):
            continue
        if types:
            t = (it.get("type") or "").lower() or _classify_type(txt_all) or ""
            if not t or t not in types: continue

        p = it.get("price")
        if price_min is not None and (p is None or p < price_min): continue
        if price_max is not None and (p is None or p > price_max): continue

        b = it.get("bedrooms")
        if bedrooms_min is not None and ((b or 0) < bedrooms_min): continue

        _ = bathrooms_min, area_min  # placeholders (liste ne contient pas ces infos)

        if not include_sold and ("vendu" in txt_all or "option" in txt_all):
            continue

        out.append(it)

    logging.info("Filtrage: %d -> %d items", len(items), len(out))
    return out

# ---------------- Email
def _fmt_price_eur(v: Optional[int]) -> str:
    return "—" if v is None else f"{v:,}€".replace(",", " ")

def send_email_if_configured(to_email: str, search_url: str, new_items: List[Dict[str, Any]], filters: Optional[Dict[str, Any]]) -> None:
    if os.getenv("SEND_EMAIL","0") != "1":
        logging.info("SEND_EMAIL != 1 → pas d'envoi."); return
    host = os.getenv("SMTP_HOST",""); port = int(os.getenv("SMTP_PORT","587") or "587")
    user = os.getenv("SMTP_USER",""); pwd = os.getenv("SMTP_PASS","")
    frm  = os.getenv("FROM_EMAIL", user)
    if not (host and port and user and pwd and to_email):
        logging.warning("Email non configuré (vars manquantes)."); return

    subject = f"[AlertMe][Immo-KH] {len(new_items)} nouvelle(s) annonce(s)"
    lines = ["Site : Immo-KH", f"Recherche : {search_url}", ""]
    if filters: lines += ["Filtres appliqués : " + json.dumps(filters, ensure_ascii=False), ""]
    lines.append("Nouvelles annonces :")
    for it in new_items:
        lines.append(f"- [{it.get('id')}] {it.get('title') or '—'} · {it.get('city') or '—'} · {_fmt_price_eur(it.get('price'))}")
        lines.append(f"  {it.get('url')}")
    text_body = "\n".join(lines)

    rows = []
    for it in new_items:
        rows.append(f"""
        <tr>
          <td style="padding:8px;border-bottom:1px solid #eee">{htmllib.escape(str(it.get('id')))}</td>
          <td style="padding:8px;border-bottom:1px solid #eee">{htmllib.escape(it.get('title') or '—')}</td>
          <td style="padding:8px;border-bottom:1px solid #eee">{htmllib.escape(it.get('city') or '—')}</td>
          <td style="padding:8px;border-bottom:1px solid #eee">{htmllib.escape(_fmt_price_eur(it.get('price')))}</td>
          <td style="padding:8px;border-bottom:1px solid #eee"><a href="{htmllib.escape(it.get('url') or '')}">Lien</a></td>
        </tr>
        """.strip())
    html_body = f"""<!doctype html><html><body>
  <h3>AlertMe – Immo-KH</h3>
  <p>Recherche : <a href="{htmllib.escape(search_url)}">{htmllib.escape(search_url)}</a></p>
  {'<pre style="background:#f6f6f6;padding:8px">' + htmllib.escape(json.dumps(filters, ensure_ascii=False, indent=2)) + '</pre>' if filters else ''}
  <table cellspacing="0" cellpadding="0" style="border-collapse:collapse;min-width:600px">
    <thead><tr style="background:#f3f4f6">
      <th align="left" style="padding:8px;border-bottom:1px solid #ddd">ID</th>
      <th align="left" style="padding:8px;border-bottom:1px solid #ddd">Titre</th>
      <th align="left" style="padding:8px;border-bottom:1px solid #ddd">Ville</th>
      <th align="left" style="padding:8px;border-bottom:1px solid #ddd">Prix</th>
      <th align="left" style="padding:8px;border-bottom:1px solid #ddd">Lien</th>
    </tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject; msg["From"] = frm; msg["To"] = to_email; msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(text_body, "plain", _charset="utf-8"))
    msg.attach(MIMEText(html_body, "html", _charset="utf-8"))

    try:
        if port == 465:
            client = smtplib.SMTP_SSL(host, port, timeout=30, context=ssl.create_default_context())
        else:
            client = smtplib.SMTP(host, port, timeout=30)
            client.ehlo(); client.starttls(context=ssl.create_default_context()); client.ehlo()
            client.login(user, pwd)
        client.sendmail(frm, [to_email], msg.as_string()); client.quit()
        logging.info("✉️  Email envoyé à %s (%d annonce(s))", to_email, len(new_items))
    except Exception as e:
        logging.error("SMTP: %s", e)

# ---------------- State helpers
def seed_if_needed(state: Dict[str, Any], state_key: str, email: str, items: List[Dict[str, Any]]) -> bool:
    alerts = state.setdefault("alerts", {})
    if state_key in alerts:
        if alerts[state_key].get("email") != email:
            alerts[state_key]["email"] = email
            save_state(state)
        return False
    created_at = utc_now_iso()
    codes = sorted({it["id"] for it in items if it.get("id")})
    alerts[state_key] = {
        "created_at_utc": created_at,
        "seen_codes": codes,
        "last_run_utc": created_at,
        "email": email,
    }
    save_state(state)
    logging.info("Seed initial: %d code(s) enregistrés.", len(codes))
    return True

def detect_new_items(state: Dict[str, Any], state_key: str, items: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], str]:
    alert = state["alerts"][state_key]
    seen = set(alert.get("seen_codes", []))
    new_list = [it for it in items if it.get("id") and it["id"] not in seen]
    alert["seen_codes"] = sorted(seen.union({it["id"] for it in items if it.get("id")}))
    alert["last_run_utc"] = utc_now_iso()
    save_state(state)
    return new_list, alert.get("email","")

# ---------------- Canonicalize pour batch
def canonicalize_list_url(user_url: Optional[str]) -> str:
    """
    Canonicalisation souple pour Immo-KH liste "à vendre".
    - Force le chemin liste principal (on ignore la pagination).
    - Conserve le schéma/host si valides sinon base LIST_URL.
    - Si user_url est vide -> LIST_URL.
    """
    if not user_url:
        return LIST_URL
    try:
        parts = urlsplit(user_url or "")
        host = parts.netloc or SITE_HOST
        scheme = parts.scheme or "https"
        return urlunsplit((scheme, host, "/fr/2/chercher-bien/a-vendre", "", ""))
    except Exception:
        return LIST_URL

# ---------------- Runner
def run_once(url: Optional[str], email: str, pages: int, filters: Optional[Dict[str, Any]] = None,
             debug_save_html: bool=False, debug_dump: bool=False, use_browser: bool=False):
    setup_logging()
    session = build_session()

    base_list_url = canonicalize_list_url(url or LIST_URL)
    logging.info("URL liste: %s", base_list_url)
    logging.info("Filtres: %s", json.dumps(filters or {}, ensure_ascii=False))
    if use_browser or os.getenv("IMMO_KH_USE_BROWSER","0") == "1":
        logging.info("Mode navigateur activé (Chromium non-headless).")
        items = _collect_with_browser(pages, debug_save_html=debug_save_html)
    else:
        list_html, status = fetch(session, base_list_url, referer=base_list_url)
        if not list_html or (status and status >= 400):
            logging.error("Impossible de charger la page liste."); return
        if debug_save_html:
            ensure_data_dir()
            with open(os.path.join(DATA_DIR, "immokh_list.html"), "w", encoding="utf-8") as f: f.write(list_html)

        base_inf = _find_infinitescroll_href(list_html)
        if not base_inf:
            items = _parse_cards_from_html(list_html, "Liste brute")
        else:
            items = _collect_all_pages(session, base_inf, pages,
                                       debug_save_html=debug_save_html, debug_dump=debug_dump)

    if not items:
        logging.warning("Aucune annonce trouvée (avant filtrage)."); return

    filtered = apply_filters(items, filters)

    state = load_state()
    skey = _state_key(base_list_url, filters)
    if seed_if_needed(state, skey, email, filtered): return

    new_items, to_email = detect_new_items(state, skey, filtered)
    if not new_items:
        logging.info("— Aucun nouveau résultat."); return

    logging.info("✅ %d nouvelle(s) annonce(s):", len(new_items))
    for it in new_items:
        logging.info(" • [%s] %s | %s | %s",
                     it["id"], _fmt_price_eur(it.get("price")), it.get("city","—"), it["url"])
    if to_email:
        send_email_if_configured(to_email, base_list_url, new_items, filters)

# ---------------- CLI
def _load_filters(filters: Optional[str], filters_file: Optional[str]) -> Optional[Dict[str, Any]]:
    if filters_file:
        if not os.path.isfile(filters_file):
            logging.error("Fichier de filtres introuvable: %s", filters_file); return None
        with open(filters_file, "r", encoding="utf-8") as f: return json.load(f)
    if not filters: return None
    s = filters.strip()
    try:
        return json.loads(s)
    except Exception:
        try:
            import ast
            obj = ast.literal_eval(s)
            if isinstance(obj, dict): return obj
        except Exception:
            pass
        s2 = re.sub(r"^--%\s*", "", s)
        try:
            return json.loads(s2)
        except Exception:
            logging.error("JSON --filters invalide."); return None

def main():
    setup_logging()
    ap = argparse.ArgumentParser(description="AlertMe Immo-KH — Collecte robuste (HTTP ou navigateur)")
    ap.add_argument("--url", type=str, default=LIST_URL, help="(Ignorée/normalisée) URL de la liste Immo-KH")
    ap.add_argument("--email", required=True, help="Adresse email pour l’alerte")
    ap.add_argument("--pages", type=int, default=DEFAULT_PAGES, help="Nombre max de fenêtres/clics")
    ap.add_argument("--filters", type=str, help="JSON des filtres")
    ap.add_argument("--filters-file", type=str, help="Chemin d’un fichier JSON de filtres")
    ap.add_argument("--debug-save-html", action="store_true")
    ap.add_argument("--debug-dump-items", action="store_true")
    ap.add_argument("--use-browser", action="store_true", help="Forcer le mode navigateur Playwright")
    args = ap.parse_args()

    filters = _load_filters(args.filters, args.filters_file)

    try:
        run_once(args.url, args.email, args.pages, filters=filters,
                 debug_save_html=args.debug_save_html, debug_dump=args.debug_dump_items,
                 use_browser=args.use_browser)
    except Exception as e:
        logging.exception("Erreur fatale: %s", e)

if __name__ == "__main__":
    main()
