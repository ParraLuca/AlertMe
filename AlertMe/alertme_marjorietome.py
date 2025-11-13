#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
alertme_marjorietome.py — ImmoToma (Marjorie Tomé)

Diff logique vs version précédente:
- Lecture des filtres attendus depuis l'URL (type/ville)
- Filtrage strict des cartes DOM selon ces filtres
- Toujours: correction des clés de query mal encodées, support liens relatifs, fallback JSON-LD, seed/diff
"""

import argparse, json, logging, os, re, time, hashlib, smtplib, ssl, json as jsonlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs, parse_qsl, urlencode, urlunparse, urljoin, unquote_plus
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate
import html as htmllib

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

# ---------- Config ----------
DATA_DIR = os.path.join(".", "data")
STATE_PATH = os.path.join(DATA_DIR, "state_marjorietome.json")
DEFAULT_PAGES = 2
REQUEST_TIMEOUT = 25
POLITE_SLEEP = 1.0
SITE_HOST = "immotoma.be"

BASE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/128.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

# ---------- Logging ----------
def setup_logging():
    lvl = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, lvl, logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

# ---------- State ----------
def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def load_state() -> Dict[str, Any]:
    ensure_data_dir()
    if not os.path.isfile(STATE_PATH):
        logging.info("Aucun state (marjorietome) — création.")
        return {"alerts": {}}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.error("Lecture %s impossible (%s). Reset + .bak.", STATE_PATH, e)
        try: os.replace(STATE_PATH, STATE_PATH + ".bak")
        except Exception: pass
        return {"alerts": {}}

def save_state(state: Dict[str, Any]) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)

# ---------- Canonicalisation & pagination ----------
def _clean_param_name(name: str) -> str:
    n = (name or "").replace("%20", " ")
    n = re.sub(r"\s+", " ", n).strip()
    return n.replace(" ", "")

def canonicalize_marjorietome_url(user_url: str) -> str:
    u = urlparse(user_url)
    if SITE_HOST not in (u.netloc or ""):
        raise ValueError("URL non-immotoma.")
    pairs = parse_qsl(u.query, keep_blank_values=True)
    fixed: Dict[str, str] = {}
    for k, v in pairs:
        ck = _clean_param_name(k)
        if ck.lower() == "paged":
            continue
        if ck not in fixed:
            fixed[ck] = v
    new_q = urlencode(fixed)
    return urlunparse((u.scheme, u.netloc, u.path, u.params, new_q, u.fragment))

def with_paged(url: str, paged: int) -> str:
    u = urlparse(url)
    q = parse_qs(u.query)
    q["paged"] = [str(max(1, paged))]
    new_q = urlencode({k: v[0] for k, v in q.items()})
    return urlunparse((u.scheme, u.netloc, u.path, u.params, new_q, u.fragment))

# ---------- Lecture filtres attendus (depuis l'URL) ----------
def expected_filters_from_url(canon_url: str) -> Dict[str, Optional[str]]:
    u = urlparse(canon_url)
    q = parse_qs(u.query)
    # type (maison/appartement/…)
    ptype = None
    for k, vs in q.items():
        if k.startswith("filter_search_type"):
            if vs: ptype = unquote_plus(vs[0]).strip().lower()
    # ville
    city = (q.get("advanced_city", [""])[0] or "").strip().lower()
    return {"type": ptype, "city": city}

# ---------- HTTP ----------
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
            return (None, r.status_code, "requests")
        return (r.text, r.status_code, "requests")
    except Exception as e:
        return (None, None, f"requests_exc:{e}")

# ---------- Parsing ----------
ID_RE = re.compile(r"(?:/propert(?:y|ies)/|/biens?/|/vente/|/a-vendre/)([^/?#]+)", re.IGNORECASE)

def _stable_id_from_href(href: str) -> str:
    if not href:
        return ""
    m = ID_RE.search(href)
    if m:
        return m.group(1).lower()
    return hashlib.md5(href.encode("utf-8")).hexdigest()

def _int_price(text: str) -> Optional[int]:
    if not text: return None
    s = re.sub(r"[^\d]", "", text)
    return int(s) if s else None

def _abs(url: str) -> str:
    return url if url.startswith("http") else urljoin(f"https://{SITE_HOST}", url)

# JSON-LD (si dispo)
def _from_json_ld(soup: BeautifulSoup) -> Dict[str, Dict[str, Any]]:
    results: Dict[str, Dict[str, Any]] = {}
    for tag in soup.find_all("script", type=lambda t: t and "ld+json" in t):
        raw = tag.string or tag.get_text()
        if not (raw and raw.strip()):
            continue
        try:
            data = jsonlib.loads(raw)
        except Exception:
            continue
        def walk(x: Any):
            if isinstance(x, dict):
                tp = str(x.get("@type", "")).lower()
                if any(k in tp for k in ["offer","product","residence","house","apartment","singlefamilyresidence","realestateobject"]):
                    url = x.get("url") or x.get("mainEntityOfPage")
                    if isinstance(url, dict): url = url.get("@id") or url.get("url")
                    if isinstance(url, list) and url: url = url[0]
                    if isinstance(url, str):
                        url = _abs(url)
                        if SITE_HOST in url:
                            pid = _stable_id_from_href(url)
                            if pid:
                                title = x.get("name") or x.get("headline") or ""
                                price = None
                                off = x.get("offers")
                                if isinstance(off, dict):
                                    price = _int_price(off.get("price") or off.get("priceSpecification", {}).get("price"))
                                results[pid] = {
                                    "id": pid, "url": url, "title": title or "",
                                    "price": price, "location": "", "publication_date": None
                                }
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        walk(data)
    return results

# Filtre texte de carte selon filtres attendus
NEG_TYPE_WORDS = ["appartement", "studio", "duplex", "triplex", "flat"]
def _card_matches_filters(card_text: str, expected: Dict[str, Optional[str]]) -> bool:
    t = card_text.lower()
    # type
    etype = (expected.get("type") or "").lower()
    if etype:
        if etype == "maison":
            if "maison" not in t:
                return False
            if any(w in t for w in NEG_TYPE_WORDS):
                return False
        else:
            # pour d'autres types on exige la présence du mot brut
            if etype not in t:
                return False
    # ville
    city = (expected.get("city") or "").strip().lower()
    if city:
        # souvent en capitales dans le badge, on fait un contains simple
        if city not in t:
            return False
    return True

def extract_items_from_search_html(html: str, expected: Dict[str, Optional[str]]) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    found: Dict[str, Dict[str, Any]] = {}

    # (1) JSON-LD
    found.update(_from_json_ld(soup))

    # (2) DOM (ancres/cartes)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not (href.startswith("/") or SITE_HOST in href):
            continue
        if not ("/property/" in href.lower() or "/properties/" in href.lower()
                or "/biens" in href.lower() or "/vente/" in href.lower()
                or "/a-vendre/" in href.lower()):
            continue

        url = _abs(href)
        pid = _stable_id_from_href(url)
        if not pid:
            continue

        # remonte vers la carte
        card = a
        for _ in range(6):
            if card and card.parent:
                card = card.parent

        # texte global de carte (sert au filtrage type/ville)
        card_text = ""
        if card:
            card_text = card.get_text(" ", strip=True) or ""
        else:
            card_text = a.get_text(" ", strip=True) or ""

        if not _card_matches_filters(card_text, expected):
            continue  # << filtrage strict ici

        # Titre
        title = (a.get_text(" ", strip=True) or "").strip()
        if not title and card:
            for sel in ["h2","h3","h4",".property-title",".entry-title",".title",".card-title",".item-title","[class*=title]"]:
                node = card.select_one(sel) if ("[" in sel or "." in sel) else card.find(sel)
                if node and (node.get_text(strip=True) or "").strip():
                    title = node.get_text(" ", strip=True).strip()
                    break

        # Prix
        price = None
        if card:
            for t in card.find_all(["span","div","p"]):
                txt = (t.get_text(" ", strip=True) or "")
                if "€" in txt or "eur" in txt.lower() or "k€" in txt.lower() or "k €" in txt.lower():
                    price = _int_price(txt)
                    if price: break

        found[pid] = {
            "id": pid, "url": url, "title": title or "",
            "price": price, "location": "", "publication_date": None
        }

    return list(found.values())

# ---------- Seed & diff ----------
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
    email = alert.get("email", "")
    seen = set(alert.get("seen_codes", []))
    new_list = [it for it in items if it.get("id") and it["id"] not in seen]
    alert["seen_codes"] = sorted(seen.union({it["id"] for it in items if it.get("id")}))
    alert["last_run_utc"] = utc_now_iso()
    save_state(state)
    return new_list, email

# ---------- Emails ----------
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
        context = ssl.create_default_context()
        return smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
    return smtplib.SMTP(host, port, timeout=timeout)

def _fmt_price_eur(v: Optional[int]) -> str:
    if v is None or not isinstance(v, int) or v <= 0:
        return "—"
    return f"{v:,}€".replace(",", " ")

def _badge(text: str) -> str:
    t = htmllib.escape(text)
    return f'<span style="display:inline-block;background:#eef2ff;color:#1e3a8a;border:1px solid #c7d2fe;border-radius:999px;padding:2px 8px;margin:0 6px 6px 0;font:12px/16px system-ui, -apple-system, Segoe UI, Roboto, Arial;">{t}</span>'

def build_email(search_url: str, new_items: List[Dict[str, Any]]) -> Tuple[str, str, str]:
    subject = f"[AlertMe][ImmoToma] {len(new_items)} nouvelle(s) annonce(s)"
    # Texte
    lines = ["Site : ImmoToma", f"Recherche : {search_url}", "", "Nouvelles annonces :"]
    for it in new_items:
        pid = it.get("id") or "?"
        price = _fmt_price_eur(it.get("price"))
        title = (it.get("title") or "").strip()
        url = it.get("url") or ""
        lines.append(f"- [{pid}] {price} · {title}")
        lines.append(f"  {url}")
    lines.append("")
    lines.append(f"Voir la recherche : {search_url}")
    text_body = "\n".join(lines)

    # HTML
    rows = []
    for it in new_items:
        pid = htmllib.escape(str(it.get("id") or "?"))
        title = htmllib.escape((it.get("title") or "").strip() or "—")
        price = htmllib.escape(_fmt_price_eur(it.get("price")))
        url = htmllib.escape(it.get("url") or "")
        rows.append(f"""
        <tr>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;white-space:nowrap;color:#111827;font:14px/20px system-ui;">{pid}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;color:#111827;font:14px/20px system-ui;">{title}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;white-space:nowrap;color:#111827;font:14px/20px system-ui;">{price}</td>
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
            AlertMe – ImmoToma
          </td>
        </tr>
        <tr>
          <td style="padding:18px 24px;font:14px/20px system-ui;color:#111827;">
            <div style="margin-bottom:8px;">{len(new_items)} nouvelle(s) annonce(s) trouvée(s).</div>
            <div style="margin:6px 0 18px 0;">
              {_badge('Source: immotoma.be')}
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
                  <th align="left" style="padding:10px 12px;border-bottom:1px solid #e5e7eb;color:#374151;font:600 12px/16px system-ui;">Lien</th>
                </tr>
              </thead>
              <tbody>
                {''.join(rows)}
              </tbody>
            </table>
            <div style="margin-top:16px;color:#6b7280;font:12px/18px system-ui;">
              Généré le {datetime.now().strftime('%d/%m/%Y %H:%M')}
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
    subject, text_body, html_body = build_email(search_url, new_items)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject; msg["From"] = frm; msg["To"] = to_email; msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(text_body, "plain", _charset="utf-8"))
    msg.attach(MIMEText(html_body, "html", _charset="utf-8"))
    try:
        client = get_smtp_client(host, port)
        if port != 465:
            client.ehlo(); client.starttls(context=ssl.create_default_context()); client.ehlo()
        client.login(user, pwd); client.sendmail(frm, [to_email], msg.as_string()); client.quit()
        logging.info("✉️  Email HTML envoyé à %s (%d annonce(s))", to_email, len(new_items))
    except Exception as e:
        logging.error("Échec envoi email: %s", e)

# ---------- Runner ----------
def run_once(user_url: str, email: str, pages: int = DEFAULT_PAGES) -> None:
    try:
        canon = canonicalize_marjorietome_url(user_url)
    except ValueError as e:
        logging.error("URL invalide: %s", e); return

    expected = expected_filters_from_url(canon)
    logging.info("Site=ImmoToma | URL canonique: %s", canon)
    logging.info("Filtres attendus: type=%s | ville=%s", expected.get("type"), expected.get("city"))
    logging.info("Email associé à l'alerte: %s", email)

    session = build_session()
    all_items: List[Dict[str, Any]] = []

    for p in range(1, max(1, pages) + 1):
        page_url = with_paged(canon, p)
        html, status, engine = fetch_html(session, page_url, referer=canon)
        logging.info("paged=%d → engine=%s status=%s", p, engine, status)
        if not html or (status and status >= 400):
            if p == 1:
                logging.warning("Page 1: aucune annonce (HTTP). Stop.")
            break

        items = extract_items_from_search_html(html, expected)
        logging.info("Page %d: %d annonce(s) détectée(s) après filtrage.", p, len(items))
        if not items:
            if p == 1:
                logging.warning("Page 1 vide (parsing/filtrage). Stop.")
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
        logging.info(" • [%s] %s | %s", it["id"], _fmt_price_eur(it.get("price")), it["url"])
    if to_email:
        send_email_if_configured(to_email, canon, new_items)

def main():
    setup_logging()
    ap = argparse.ArgumentParser(description="AlertMe ImmoToma – URL ↔ email")
    ap.add_argument("--url", help="URL de recherche ImmoToma (advanced-search)")
    ap.add_argument("--email", help="Adresse email à associer à cette alerte")
    ap.add_argument("--pages", type=int, default=DEFAULT_PAGES, help="Pages 'paged' à scanner (défaut=2)")
    args = ap.parse_args()
    if not (args.url and args.email):
        logging.error("Il faut --url et --email."); return
    try:
        run_once(args.url, args.email, args.pages)
    except Exception as e:
        logging.exception("Erreur fatale: %s", e)

if __name__ == "__main__":
    main()
