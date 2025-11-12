#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse, json, logging, os, re, time, smtplib, ssl
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Iterable, Tuple
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from email.mime.text import MIMEText
from email.utils import formatdate

from email.mime.multipart import MIMEMultipart
import html as htmllib


import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

# ---------- Config ----------
DATA_DIR = os.path.join(".", "data")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
IMMOWEB_HOST = "www.immoweb.be"
DEFAULT_PAGES = 2
REQUEST_TIMEOUT = 25
POLITE_SLEEP = 1.0
ORDER_KEYS = ["newest", "most_recent"]
PATH_ALIASES = ["/fr/recherche/", "/fr/recherche-avancee/"]

BASE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/128.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

USE_SELENIUM = os.getenv("USE_SELENIUM", "0") == "1"  # dernier recours

# ---------- Helpers: résumé des filtres & nom du site ----------
def human_filters_from_url(url: str) -> dict:
    u = urlparse(url); q = parse_qs(u.query)
    def _get(name, cast=str):
        if name not in q: return None
        try: return cast(q[name][0])
        except Exception: return q[name][0]
    def _list(name): return q[name][0].split(",") if name in q else []
    return {
        "pays": q.get("countries", ["BE"])[0],
        "prix_min": _get("minPrice", int),
        "prix_max": _get("maxPrice", int),
        "chambres_min": _get("minBedroomCount", int),
        "codes_postaux": _list("postalCodes"),
        "disponible_immediat": _get("isImmediatelyAvailable", str),
        "meublé": _get("isFurnished", str),
        "viager": _get("isALifeAnnuitySale", str),
        "tri": q.get("orderBy", ["newest"])[0],
    }

def site_name_from_url(url: str) -> str:
    host = urlparse(url).netloc
    if "immoweb" in host: return "Immoweb"
    return host or "site"

# ---------- Logging ----------
def setup_logging():
    lvl = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, lvl, logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

# ---------- State JSON ----------
def ensure_data_dir():
    if not os.path.isdir(DATA_DIR):
        os.makedirs(DATA_DIR, exist_ok=True)
        logging.info("Création du dossier de données: %s", DATA_DIR)

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def load_state() -> Dict[str, Any]:
    ensure_data_dir()
    if not os.path.isfile(STATE_PATH):
        logging.info("Aucun state.json — création.")
        return {"alerts": {}}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.error("Lecture %s impossible (%s). Reset + .bak.", STATE_PATH, e)
        try: os.replace(STATE_PATH, STATE_PATH + ".bak")
        except Exception as e2: logging.warning("Échec .bak: %s", e2)
        return {"alerts": {}}

def save_state(state: Dict[str, Any]) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)

# ---------- URL helpers ----------
def normalize_immoweb_url(user_url: str) -> str:
    u = urlparse(user_url)
    if IMMOWEB_HOST not in u.netloc:
        raise ValueError("URL non-Immoweb.")
    q = parse_qs(u.query)
    q["orderBy"] = [ORDER_KEYS[0]]   # force “plus récent”
    q.pop("page", None)              # URL canonique sans page
    new_q = urlencode({k: v[0] for k, v in q.items()})
    return urlunparse((u.scheme, u.netloc, u.path, u.params, new_q, u.fragment))

def with_page(url: str, page: int) -> str:
    u = urlparse(url); q = parse_qs(u.query); q["page"] = [str(page)]
    new_q = urlencode({k: v[0] for k, v in q.items()})
    return urlunparse((u.scheme, u.netloc, u.path, u.params, new_q, u.fragment))

def url_variants(canon_url: str, page: int) -> Iterable[Tuple[str, str, str]]:
    u = urlparse(canon_url); q = parse_qs(u.query)
    for order in ORDER_KEYS:
        q["orderBy"] = [order]; q["page"] = [str(page)]
        query = urlencode({k: v[0] for k, v in q.items()})
        for base in PATH_ALIASES:
            path = u.path
            for p in PATH_ALIASES:
                if path.startswith(p):
                    path = path.replace(p, base); break
            new_url = urlunparse((u.scheme, u.netloc, path, u.params, query, u.fragment))
            ref_q = parse_qs(u.query); ref_q["orderBy"] = [order]; ref_q.pop("page", None)
            referer = urlunparse((u.scheme, u.netloc, path, u.params, urlencode({k:v[0] for k,v in ref_q.items()}), u.fragment))
            yield f"{order}|{base.strip('/')}", new_url, referer

# ---------- HTTP & Selenium ----------
def build_session() -> requests.Session:
    s = requests.Session()
    retries = Retry(total=3, backoff_factor=0.7,
                    status_forcelist=(429,500,502,503,504),
                    allowed_methods=("GET",), raise_on_status=False)
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.mount("http://", HTTPAdapter(max_retries=retries))
    return s

def fetch_html(session: requests.Session, url: str, referer: Optional[str]=None) -> Tuple[Optional[str], Optional[int], str]:
    headers = dict(BASE_HEADERS)
    if referer: headers["Referer"] = referer
    try:
        r = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        if r.status_code >= 400:
            logging.warning("HTTP %s sur %s", r.status_code, url)
            try:
                import cloudscraper  # optional
                logging.info("Fallback via cloudscraper…")
                sc = cloudscraper.create_scraper()
                r2 = sc.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
                return (r2.text if r2.ok else None, r2.status_code, "cloudscraper")
            except Exception as e:
                return (None, r.status_code, f"requests:{e}")
        return (r.text, r.status_code, "requests")
    except Exception as e:
        return (None, None, f"requests_exc:{e}")

def selenium_get(url: str) -> Optional[str]:
    try:
        import undetected_chromedriver as uc
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        opts = uc.ChromeOptions()
        opts.add_argument("--headless=new"); opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox"); opts.add_argument("--lang=fr-FR")
        driver = uc.Chrome(options=opts)
        driver.set_page_load_timeout(40)
        driver.get(url)
        try:
            WebDriverWait(driver, 8).until(EC.presence_of_all_elements_located((By.TAG_NAME, "button")))
            for b in driver.find_elements(By.TAG_NAME, "button"):
                txt = (b.text or "").strip().lower()
                if txt in ("ok", "j'accepte", "accepter tout"):
                    try: b.click(); break
                    except Exception: pass
        except Exception:
            pass
        time.sleep(2.0)
        html = driver.page_source
        driver.quit()
        return html
    except Exception as e:
        logging.warning("Selenium indisponible/échec: %s", e)
        return None

# ---------- Parsing ----------
def fetch_next_data_from_variants(session: requests.Session, canon_url: str, page: int) -> Optional[Dict[str, Any]]:
    for name, variant_url, referer in url_variants(canon_url, page):
        html, status, engine = fetch_html(session, variant_url, referer=referer)
        logging.info("Variante %s → engine=%s status=%s", name, engine, status)
        if not html or (status and status >= 400): 
            continue
        soup = BeautifulSoup(html, "html.parser")
        node = soup.find("script", id="__NEXT_DATA__", type="application/json") or soup.find("script", id="__NEXT_DATA__")
        if not node:
            continue
        try:
            return json.loads(node.get_text(strip=True))
        except Exception as e:
            logging.debug("JSON __NEXT_DATA__ illisible (var %s): %s", name, e)
            continue
    return None

def _to_int_price(v: Any) -> Optional[int]:
    if v is None: return None
    if isinstance(v, (int, float)): return int(v)
    s = re.sub(r"[^\d]", "", str(v))
    return int(s) if s else None

def _maybe_iso(dt: Any) -> Optional[str]:
    if not dt: return None
    s = str(dt).strip()
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
        return s if ("Z" in s or "+" in s) else s + "+00:00"
    except Exception:
        pass
    if s.isdigit():
        try:
            ts = int(s)
            d = datetime.fromtimestamp(ts/1000 if ts > 10_000_000_000 else ts, tz=timezone.utc)
            return d.isoformat()
        except Exception:
            return None
    return None

def extract_items_from_next(next_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    results: Dict[str, Dict[str, Any]] = {}
    def maybe_register(raw: Dict[str, Any]):
        pid = raw.get("id") or raw.get("propertyId") or raw.get("code") or raw.get("nid")
        url = raw.get("url") or raw.get("detailUrl") or raw.get("propertyUrl") or raw.get("link")
        if not (pid and url and isinstance(url, str) and "immoweb.be" in url): return
        title = raw.get("title") or raw.get("propertyTitle") or raw.get("heading") or ""
        price = _to_int_price(raw.get("price") or raw.get("priceValue") or raw.get("salePrice"))
        location = raw.get("city") or raw.get("location") or raw.get("propertyLocation") or ""
        pub = (raw.get("publicationDate") or raw.get("postedAt") or raw.get("creationDate")
               or raw.get("date") or raw.get("updateDate"))
        results[str(pid)] = {
            "id": str(pid), "url": url, "title": (title or "").strip(),
            "price": price, "location": location, "publication_date": _maybe_iso(pub),
        }
    def walk(x: Any):
        if isinstance(x, dict):
            maybe_register(x)
            for v in x.values(): walk(v)
        elif isinstance(x, list):
            for v in x: walk(v)
    walk(next_data)
    return list(results.values())

def extract_items_from_html(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    items: Dict[str, Dict[str, Any]] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "immoweb.be" not in href or "/annonce/" not in href:
            continue
        m = re.search(r"/(\d{5,})", href)
        if not m:
            continue
        pid = m.group(1)
        title = (a.get_text(strip=True) or "")
        url = href if href.startswith("http") else f"https://{IMMOWEB_HOST}{href}"
        item = items.setdefault(pid, {"id": pid, "url": url, "title": title, "price": None, "location": "", "publication_date": None})

        # remonte au conteneur pour choper prix/ville si possible
        card = a
        for _ in range(4):
            if card and card.parent: card = card.parent
        if card:
            price_text = None
            for sel in ["span", "div"]:
                for t in card.find_all(sel):
                    s = (t.get_text(" ", strip=True) or "").lower()
                    if "€" in s or "eur" in s:
                        price_text = s; break
                if price_text: break
            if price_text:
                digits = re.sub(r"[^\d]", "", price_text)
                if digits: item["price"] = int(digits)

            loc_text = None
            for t in card.find_all(["span","div"]):
                s = (t.get_text(" ", strip=True) or "")
                if re.search(r"[A-Za-zÀ-ÿ-]+\s*\(\d{4}\)", s) or re.search(r"\d{4}\s+[A-Za-zÀ-ÿ-]+", s):
                    loc_text = s; break
            if loc_text: item["location"] = loc_text

    return list(items.values())

# ---------- Alert logic (avec email associé) ----------
def seed_if_needed(state: Dict[str, Any], canon_url: str, email: str, items: List[Dict[str, Any]]) -> bool:
    alerts = state.setdefault("alerts", {})
    if canon_url in alerts:
        if alerts[canon_url].get("email") != email:
            alerts[canon_url]["email"] = email
            save_state(state)
            logging.info("Email mis à jour pour cette alerte: %s", email)
        return False
    created_at = utc_now_iso()
    codes = sorted({it["id"] for it in items if it.get("id")})
    alerts[canon_url] = {
        "created_at_utc": created_at,
        "seen_codes": codes,
        "last_run_utc": created_at,
        "email": email,
    }
    save_state(state)
    logging.info("Seed initial: %d code(s) enregistrés pour %s (aucune notif).", len(codes), email)
    return True

def detect_new_items(state: Dict[str, Any], canon_url: str, items: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], str]:
    alert = state["alerts"][canon_url]
    created_at = alert["created_at_utc"]
    email = alert.get("email", "")
    seen = set(alert.get("seen_codes", []))
    try:
        created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except Exception:
        created_dt = None

    new_list: List[Dict[str, Any]] = []
    for it in items:
        pid = it.get("id")
        if not pid:
            continue
        pub_ok = False
        if it.get("publication_date") and created_dt:
            try:
                pub_dt = datetime.fromisoformat(it["publication_date"].replace("Z", "+00:00"))
                pub_ok = pub_dt >= created_dt
            except Exception:
                pub_ok = False
        if pub_ok or (pid not in seen):
            new_list.append(it)

    alert["seen_codes"] = sorted(seen.union({it["id"] for it in items if it.get("id")}))
    alert["last_run_utc"] = utc_now_iso()
    save_state(state)
    return new_list, email

# ---------- Email: config/diagnostics ----------
def mask(s: Optional[str], keep: int = 2) -> str:
    if not s: return ""
    s = str(s)
    if len(s) <= keep: return "*" * len(s)
    return s[:keep] + "…" + "*" * max(0, len(s) - keep - 1)

def log_email_config():
    cfg = {
        "SEND_EMAIL": os.getenv("SEND_EMAIL"),
        "SMTP_HOST": os.getenv("SMTP_HOST"),
        "SMTP_PORT": os.getenv("SMTP_PORT"),
        "SMTP_USER": os.getenv("SMTP_USER"),
        "FROM_EMAIL": os.getenv("FROM_EMAIL"),
        "SMTP_PASS": mask(os.getenv("SMTP_PASS")),
    }
    logging.info("SMTP config: %s", cfg)

def get_smtp_client(host: str, port: int):
    timeout = 30
    if port == 465:
        logging.info("SMTP: connexion SSL directe à %s:%s", host, port)
        context = ssl.create_default_context()
        client = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
    else:
        logging.info("SMTP: connexion TCP à %s:%s", host, port)
        client = smtplib.SMTP(host, port, timeout=timeout)
    return client


def _fmt_price_eur(v: Optional[int]) -> str:
    if v is None or not isinstance(v, int) or v <= 0:
        return "—"
    return f"{v:,}€".replace(",", " ")

def _badge(text: str) -> str:
    t = htmllib.escape(text)
    return f'<span style="display:inline-block;background:#eef2ff;color:#1e3a8a;border:1px solid #c7d2fe;border-radius:999px;padding:2px 8px;margin:0 6px 6px 0;font:12px/16px system-ui, -apple-system, Segoe UI, Roboto, Arial;">{t}</span>'

def _email_subject(site: str, n: int, filters: dict) -> str:
    cps = ",".join(filters.get("codes_postaux") or [])
    cap = f"≤{filters['prix_max']}€" if filters.get("prix_max") else ""
    tail = " · ".join([p for p in [f"CP: {cps}" if cps else "", cap] if p])
    return f"[AlertMe][{site}] {n} nouvelle(s) annonce(s)" + (f" — {tail}" if tail else "")

def build_email_bodies(search_url: str, new_items: List[Dict[str, Any]]) -> Tuple[str, str, str]:
    """Retourne (subject, text_body, html_body)."""
    filters = human_filters_from_url(search_url)
    site = site_name_from_url(search_url)
    subject = _email_subject(site, len(new_items), filters)

    # ---------- TEXTE ALTERNATIF ----------
    text_lines = [
        f"Site : {site}",
        f"Recherche : {search_url}",
        "",
        "Filtres :",
        f"- Pays: {filters.get('pays') or '—'}",
        f"- Prix: {filters.get('prix_min') or '—'} → {filters.get('prix_max') or '—'}",
        f"- Min chambres: {filters.get('chambres_min') or '—'}",
        f"- Codes postaux: {', '.join(filters.get('codes_postaux') or []) or '—'}",
        f"- Disponible immédiat: {filters.get('disponible_immediat') if filters.get('disponible_immediat') is not None else '—'}",
        f"- Meublé: {filters.get('meublé') if filters.get('meublé') is not None else '—'}",
        f"- Viager: {filters.get('viager') if filters.get('viager') is not None else '—'}",
        f"- Tri: {filters.get('tri') or 'newest'}",
        "",
        "Nouvelles annonces :",
    ]
    for it in new_items:
        pid = it.get("id") or "?"
        price = _fmt_price_eur(it.get("price"))
        loc = (it.get("location") or "").strip() or "—"
        url = it.get("url") or ""
        title = (it.get("title") or "").strip()
        line = f"- [{pid}] {price} · {loc}"
        if title:
            line += f" · {title}"
        text_lines.append(line)
        text_lines.append(f"  {url}")
    text_lines.append("")
    text_lines.append(f"Voir la recherche : {search_url}")
    text_body = "\n".join(text_lines)

    # ---------- HTML ----------
    # badges filtres
    badges = []
    if filters.get("pays"): badges.append(_badge(f"Pays: {filters['pays']}"))
    if filters.get("prix_min") is not None or filters.get("prix_max") is not None:
        badges.append(_badge(f"Prix: {filters.get('prix_min','—')} → {filters.get('prix_max','—')}"))
    if filters.get("chambres_min") is not None: badges.append(_badge(f"≥ {filters['chambres_min']} ch."))
    if filters.get("codes_postaux"): badges.append(_badge("CP: " + ",".join(filters['codes_postaux'])))
    if filters.get("disponible_immediat") is not None: badges.append(_badge(f"Immédiat: {filters['disponible_immediat']}"))
    if filters.get("meublé") is not None: badges.append(_badge(f"Meublé: {filters['meublé']}"))
    if filters.get("viager") is not None: badges.append(_badge(f"Viager: {filters['viager']}"))
    if filters.get("tri"): badges.append(_badge(f"Tri: {filters['tri']}"))

    # lignes tableau
    rows = []
    for it in new_items:
        pid = htmllib.escape(str(it.get("id") or "?"))
        title = htmllib.escape((it.get("title") or "").strip() or "—")
        price = htmllib.escape(_fmt_price_eur(it.get("price")))
        loc = htmllib.escape((it.get("location") or "").strip() or "—")
        url = htmllib.escape(it.get("url") or "")
        rows.append(f"""
        <tr>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;white-space:nowrap;color:#111827;font:14px/20px system-ui;">{pid}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;color:#111827;font:14px/20px system-ui;">{title}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;white-space:nowrap;color:#111827;font:14px/20px system-ui;">{price}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;color:#374151;font:14px/20px system-ui;">{loc}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;">
            <a href="{url}" style="display:inline-block;background:#111827;color:#ffffff;text-decoration:none;border-radius:6px;padding:8px 12px;font:13px/16px system-ui;">Voir l’annonce</a>
          </td>
        </tr>
        """.strip())

    html_body = f"""
<!doctype html>
<html lang="fr">
  <body style="margin:0;padding:0;background:#f8fafc;">
    <div style="max-width:720px;margin:0 auto;padding:24px;">
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;overflow:hidden;">
        <tr>
          <td style="padding:20px 24px;background:#111827;color:#ffffff;font:600 16px/20px system-ui;">
            AlertMe – {htmllib.escape(site)}
          </td>
        </tr>
        <tr>
          <td style="padding:18px 24px;font:14px/20px system-ui;color:#111827;">
            <div style="margin-bottom:8px;">{len(new_items)} nouvelle(s) annonce(s) trouvée(s).</div>
            <div style="margin:8px 0 14px 0;">
              {''.join(badges)}
            </div>
            <div style="margin:6px 0 18px 0;">
              <a href="{htmllib.escape(search_url)}" style="color:#2563eb;text-decoration:none;">Voir la recherche</a>
            </div>
            <table width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;">
              <thead>
                <tr style="background:#f3f4f6;">
                  <th align="left" style="padding:10px 12px;border-bottom:1px solid #e5e7eb;color:#374151;font:600 12px/16px system-ui;">ID</th>
                  <th align="left" style="padding:10px 12px;border-bottom:1px solid #e5e7eb;color:#374151;font:600 12px/16px system-ui;">Titre</th>
                  <th align="left" style="padding:10px 12px;border-bottom:1px solid #e5e7eb;color:#374151;font:600 12px/16px system-ui;">Prix</th>
                  <th align="left" style="padding:10px 12px;border-bottom:1px solid #e5e7eb;color:#374151;font:600 12px/16px system-ui;">Localisation</th>
                  <th align="left" style="padding:10px 12px;border-bottom:1px solid #e5e7eb;color:#374151;font:600 12px/16px system-ui;">Lien</th>
                </tr>
              </thead>
              <tbody>
                {''.join(rows)}
              </tbody>
            </table>
            <div style="margin-top:16px;color:#6b7280;font:12px/18px system-ui;">
              Requête triée par <strong>{htmllib.escape(filters.get('tri') or 'newest')}</strong> · Généré le {datetime.now().strftime('%d/%m/%Y %H:%M')}
            </div>
          </td>
        </tr>
      </table>
      <div style="color:#9ca3af;font:12px/18px system-ui;margin-top:10px;text-align:center;">
        Vous recevez cet e-mail car vous avez créé une alerte sur AlertMe.
      </div>
    </div>
  </body>
</html>
""".strip()

    return subject, text_body, html_body

# ---------- Email (envoi complet avec logs détaillés) ----------
def send_email_if_configured(to_email: str, search_url: str, new_items: List[Dict[str, Any]]) -> None:
    if os.getenv("SEND_EMAIL","0") != "1":
        logging.info("SEND_EMAIL != 1 → pas d'envoi.")
        return

    host = os.getenv("SMTP_HOST",""); port = int(os.getenv("SMTP_PORT","587") or "587")
    user = os.getenv("SMTP_USER",""); pwd = os.getenv("SMTP_PASS","")
    frm  = os.getenv("FROM_EMAIL", user)
    if not (host and port and user and pwd and to_email):
        logging.warning("Email non configuré (vars manquantes). host=%s port=%s user=%s to=%s", host, port, user, to_email)
        return

    subject, text_body, html_body = build_email_bodies(search_url, new_items)

    # Multipart/alternative
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = frm
    msg["To"] = to_email
    msg["Date"] = formatdate(localtime=True)

    msg.attach(MIMEText(text_body, "plain", _charset="utf-8"))
    msg.attach(MIMEText(html_body, "html", _charset="utf-8"))

    log_email_config()
    try:
        client = get_smtp_client(host, port)
        if port != 465:
            logging.info("SMTP: EHLO + STARTTLS…")
            client.ehlo()
            context = ssl.create_default_context()
            client.starttls(context=context)
            client.ehlo()
        logging.info("SMTP: login user=%s", user)
        client.login(user, pwd)
        logging.info("SMTP: envoi du message à %s", to_email)
        client.sendmail(frm, [to_email], msg.as_string())
        client.quit()
        logging.info("✉️  Email HTML envoyé à %s (%d annonce(s))", to_email, len(new_items))
    except smtplib.SMTPAuthenticationError as e:
        logging.error("SMTP auth error: %s. Vérifie le mot de passe d'application Gmail et l'adresse SMTP_USER.", e)
    except smtplib.SMTPException as e:
        logging.error("SMTPException: %s", e)
    except Exception as e:
        logging.error("Échec envoi email (exception): %s", e)

# ---------- Email: test dédié ----------
def send_test_email(to_email: str) -> None:
    """Envoie un mail de test avec la config actuelle (sans scraper)."""
    if os.getenv("SEND_EMAIL","0") != "1":
        logging.warning("SEND_EMAIL != 1 → test annulé.")
        return
    host = os.getenv("SMTP_HOST",""); port = int(os.getenv("SMTP_PORT","587") or "587")
    user = os.getenv("SMTP_USER",""); pwd = os.getenv("SMTP_PASS","")
    frm  = os.getenv("FROM_EMAIL", user)
    log_email_config()
    if not (host and port and user and pwd and to_email):
        logging.error("Config SMTP incomplète pour test."); return
    msg = MIMEText("Ceci est un test AlertMe (configuration SMTP OK).", _charset="utf-8")
    msg["Subject"] = "[AlertMe] Test e-mail"
    msg["From"] = frm; msg["To"] = to_email; msg["Date"] = formatdate(localtime=True)
    try:
        client = get_smtp_client(host, port)
        if port != 465:
            client.ehlo(); client.starttls(context=ssl.create_default_context()); client.ehlo()
        client.login(user, pwd)
        client.sendmail(frm, [to_email], msg.as_string())
        client.quit()
        logging.info("✅ Test e-mail envoyé à %s", to_email)
    except Exception as e:
        logging.error("❌ Test e-mail échec: %s", e)

# ---------- Runner ----------
def run_once(user_url: str, email: str, pages: int = DEFAULT_PAGES) -> None:
    try:
        canon = normalize_immoweb_url(user_url)
    except ValueError as e:
        logging.error("URL invalide: %s", e); return
    logging.info("URL canonique: %s", canon)
    logging.info("Email associé à l'alerte: %s", email)

    session = build_session()
    all_items: List[Dict[str, Any]] = []
    for page in range(1, max(1, pages) + 1):
        logging.info("Fetching page %d…", page)
        next_data = fetch_next_data_from_variants(session, canon, page)
        items: List[Dict[str, Any]] = []
        if next_data:
            items = extract_items_from_next(next_data)
            logging.info("Page %d via __NEXT_DATA__: %d annonce(s).", page, len(items))
        if not items:
            for name, variant_url, referer in url_variants(canon, page):
                html, status, engine = fetch_html(session, variant_url, referer=referer)
                logging.info("Variante %s (fallback HTML) → engine=%s status=%s", name, engine, status)
                if not html or (status and status >= 400):
                    continue
                items = extract_items_from_html(html)
                if items:
                    logging.info("Page %d via HTML fallback: %d annonce(s).", page, len(items))
                    break
        if not items and USE_SELENIUM:
            page_url = with_page(canon, page)
            logging.info("Selenium fallback: %s", page_url)
            html = selenium_get(page_url)
            if html:
                items = extract_items_from_html(html)
                logging.info("Page %d via Selenium: %d annonce(s).", page, len(items))
        if not items:
            if page == 1:
                logging.warning("Page 1: aucune annonce (après tous les fallbacks). Stop.")
            else:
                logging.info("Arrêt pagination: page vide.")
            break
        all_items.extend(items)
        time.sleep(POLITE_SLEEP)

    if not all_items:
        logging.warning("Aucune annonce trouvée.")
        return

    state = load_state()
    if seed_if_needed(state, canon, email, all_items):
        return

    new_items, to_email = detect_new_items(state, canon, all_items)
    if not new_items:
        logging.info("— Aucun nouveau résultat.")
        return

    logging.info("✅ %d nouvelle(s) annonce(s) (alerte: %s):", len(new_items), to_email or "—")
    for it in new_items:
        p = f"{it['price']:,}€".replace(",", " ") if it.get("price") else "—"
        logging.info(" • [%s] %s | %s | %s", it["id"], p, it.get("location",""), it["url"])

    if to_email:
        send_email_if_configured(to_email, canon, new_items)

def main():
    setup_logging()
    ap = argparse.ArgumentParser(description="AlertMe Immoweb – URL ↔ email (JSON + fallbacks)")
    ap.add_argument("--url", help="URL de recherche Immoweb (collée depuis le site)")
    ap.add_argument("--email", help="Adresse email à associer à cette alerte")
    ap.add_argument("--pages", type=int, default=DEFAULT_PAGES, help="Pages à scanner (défaut=2)")
    ap.add_argument("--send-test-email", action="store_true", help="Envoie un e-mail de test à --email, sans scraper")
    args = ap.parse_args()

    # Mode test e-mail (sans scraping)
    if args.send_test_email:
        if not args.email:
            logging.error("--send-test-email nécessite --email")
            return
        send_test_email(args.email)
        return

    if not (args.url and args.email):
        logging.error("Il faut --url et --email (ou bien --send-test-email).")
        return

    try:
        run_once(args.url, args.email, args.pages)
    except Exception as e:
        logging.exception("Erreur fatale: %s", e)

if __name__ == "__main__":
    main()
