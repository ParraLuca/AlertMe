#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
deuxiememain_pipeline.py — Pipeline complet 2ememain.be fidèle à tes scripts

Fonctions:
  - search : collecte les URLs d'annonces depuis une URL de résultats (pagination "Suivant")
  - scrape : ouvre CHAQUE annonce avec Playwright, gère les cookies, clique les onglets (Base/Technique/Éco/Options...),
             extrait meta + description + images, normalise les specs (FR/NL), et écrit CSV + JSONL
  - train  : entraîne les modèles quantiles (q10/q50/q90) + calibration conformale à partir du dump (CSV/JSONL)
  - score  : applique les modèles sur un dump et ajoute pred_price_[lo,fair,hi], deal_score, deal_label

Dépendances:
  pip install playwright==1.47.0
  python -m playwright install chromium
  pip install lxml beautifulsoup4 requests joblib numpy pandas scikit-learn

Exemples d’URL de recherche:
  https://www.2ememain.be/l/autos/audi/f/a1/582/#f:10882|Language:all-languages|constructionYearFrom:2019
  https://www.2ememain.be/l/autos/volkswagen/f/caddy-maxi/10653/#f:10882|constructionYearFrom:2019

USAGE RAPIDE
------------
# 1) Collecter les liens (pagination auto)
python deuxiememain_pipeline.py search --url "https://www.2ememain.be/l/autos/audi/f/a1/582/#f:10882|constructionYearFrom:2019" --out urls.txt

# 2) Scraper TOUTES les annonces (ouvre la page + clique les onglets)
python deuxiememain_pipeline.py scrape -i urls.txt --headless --csv vehicles.csv --jsonl vehicles.jsonl

# 3) Entraîner le modèle
python deuxiememain_pipeline.py train --in vehicles.jsonl --outdir model_artifacts

# 4) Scorer un nouveau dump et marquer les sous-évaluées
python deuxiememain_pipeline.py score --in vehicles.jsonl --artifacts model_artifacts --out scored.csv
"""

from __future__ import annotations

import argparse, csv, json, os, re, sys, time, warnings
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin

# ───────────────────────────────────────────────────────────────────────────────
# Import scraping (requests/bs4) — fidèle au script 1 (search_annonces.py)
# ───────────────────────────────────────────────────────────────────────────────
import requests
from bs4 import BeautifulSoup

# ───────────────────────────────────────────────────────────────────────────────
# Helpers chemins: tout par dossier de run
# ───────────────────────────────────────────────────────────────────────────────
def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p

def run_paths(run_dir: str | Path) -> Dict[str, Path]:
    d = ensure_dir(Path(run_dir))
    return {
        "dir": d,
        "urls": d / "urls.txt",
        "vehicles_csv": d / "vehicles.csv",
        "vehicles_jsonl": d / "vehicles.jsonl",
        "artifacts": d / "model_artifacts",
        "scored_csv": d / "scored.csv",
        "scored_jsonl": d / "scored.jsonl",
    }


BASE = "https://www.2ememain.be"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/122.0.0.0 Safari/537.36"),
    "Accept-Language": "fr-BE,fr;q=0.9,nl;q=0.6,en;q=0.5",
}
TIMEOUT = 25
MAX_RETRIES = 4
BACKOFF = 1.7

# Lien d’annonce : /v/autos/…/m123456… (id m*)
AD_HREF_RE = re.compile(r"^/v/autos/.*/m\d+[^/]*$", re.I)


def fit_transform_tfidf_vocab(pre: ColumnTransformer, df: pd.DataFrame) -> Dict[str, Any]:
    _ = pre.fit_transform(df)
    tfidf = None
    for name, trans, cols in pre.transformers_:
        if name == "txt":
            for stepname, step in trans.steps:
                if stepname == "tfidf":
                    tfidf = step
    if tfidf is not None and getattr(tfidf, "vocabulary_", None):
        # cast explicite en int Python
        vocab = {k: int(v) for k, v in tfidf.vocabulary_.items()}
        return {"tfidf_vocab": vocab}
    return {}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)

def dedup(seq: Iterable[str]) -> List[str]:
    seen, out = set(), []
    for x in seq:
        if x and x not in seen:
            seen.add(x); out.append(x)
    return out

def fetch(url: str) -> BeautifulSoup:
    last = None
    for i in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code in (429, 403):
                raise requests.HTTPError(r.status_code)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return BeautifulSoup(r.text, "lxml")
        except Exception as e:
            last = e
            wait = BACKOFF ** i
            log(f"[warn] fetch fail try {i}/{MAX_RETRIES}: {e} → sleep {wait:.1f}s")
            time.sleep(wait)
    raise RuntimeError(f"Failed to fetch {url}: {last}")

def extract_links_on_page(soup: BeautifulSoup) -> List[str]:
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if AD_HREF_RE.match(href):
            out.append(urljoin(BASE, href))
    # fallback data-href si présent
    for tag in soup.find_all(attrs={"data-href": True}):
        href = str(tag.get("data-href") or "").strip()
        if AD_HREF_RE.match(href):
            out.append(urljoin(BASE, href))
    return dedup(out)

def find_next_page_url(soup: BeautifulSoup, cur_url: str, cur_page_idx: int) -> Optional[str]:
    # 1) tentatives DOM (ancien comportement + sélecteurs supplémentaires)
    a = soup.find("a", attrs={"rel": "next"})
    if a and a.get("href"):
        return urljoin(BASE, a["href"])

    for pat in (r"Suivant", r"Next", r"Volgende", r"Page suivante", r"Volgende pagina"):
        a = soup.find("a", attrs={"aria-label": re.compile(pat, re.I)})
        if a and a.get("href"):
            return urljoin(BASE, a["href"])

    # variantes de pagination classiques
    # boutons/chevrons
    for sel in [
        "a.hz-Pagination-next",
        "a[aria-label*='Suivant']",
        "a[aria-label*='Next']",
        "a[aria-label*='Volgende']",
        "nav .hz-Pagination a[rel='next']",
        "nav .hz-Pagination a[aria-label*='Suivant']",
        "nav .hz-Pagination a[aria-label*='Next']",
        "nav .hz-Pagination a[aria-label*='Volgende']",
    ]:
        tag = soup.select_one(sel)
        if tag and tag.get("href"):
            return urljoin(BASE, tag["href"])

    # 2) Fallback URL : on fabrique /p/<n+1>/ en conservant le fragment #f:
    # Exemple:
    #  - page 1: https://.../10775/#f:10882
    #  - page 2: https://.../10775/p/2/#f:10882|Language:...
    m = re.match(r"^(?P<base>https?://[^#]+?)(?P<frag>#.*)?$", cur_url)
    if not m:
        return None
    base = m.group("base")
    frag = m.group("frag") or ""

    # si /p/<n>/ existe déjà -> on incrémente, sinon on insère avant le fragment
    if re.search(r"/p/\d+/?$", base):
        next_base = re.sub(r"/p/(\d+)/?$", lambda mm: f"/p/{int(mm.group(1)) + 1}/", base)
    else:
        # insérer /p/<n+1>/ juste avant le fragment (ou fin)
        # attention à un éventuel / à la fin
        if not base.endswith("/"):
            base += "/"
        next_base = base + f"p/{cur_page_idx + 1}/"

    return next_base + frag
    # 1) rel="next"
    a = soup.find("a", attrs={"rel": "next"})
    if a and a.get("href"):
        return urljoin(BASE, a["href"])
    # 2) aria-label
    for pat in (r"Suivant", r"Next", r"Volgende"):
        a = soup.find("a", attrs={"aria-label": re.compile(pat, re.I)})
        if a and a.get("href"):
            return urljoin(BASE, a["href"])
    # 3) bouton primaire sans rel
    cand = soup.select_one("a.hz-Link.hz-Link--isolated.hz-Button--primary")
    if cand and cand.get("href"):
        return urljoin(BASE, cand["href"])
    return None

def collect_all_links(start_url: str, max_pages: Optional[int] = None, sleep_s: float = 0.4) -> List[str]:
    all_links: List[str] = []
    visited_pages = set()
    cur_url = start_url
    page_idx = 1

    while True:
        if max_pages and page_idx > max_pages:
            break
        if cur_url in visited_pages:
            log("[stop] boucle évitée: page déjà vue")
            break
        visited_pages.add(cur_url)

        log(f"[page {page_idx}] {cur_url}")
        soup = fetch(cur_url)
        links = extract_links_on_page(soup)
        log(f"[found] {len(links)} ad links on page {page_idx}")
        all_links.extend(links)

        # essaie DOM puis fallback URL builder
        next_url = find_next_page_url(soup, cur_url, page_idx)

        if not next_url:
            log("[done] pas de page suivante détectée (DOM & fallback).")
            break

        # sécurité : si la page suivante ne contient pas de nouvelles annonces -> stop
        # (évite boucle infinie si le site répond la même page pour /p/<grand n>/)
        if next_url in visited_pages:
            log("[stop] prochaine page déjà visitée")
            break

        # Test rapide de validité : si la page suivante ne renvoie aucun lien, on s'arrête
        try:
            soup_next = fetch(next_url)
            links_next = extract_links_on_page(soup_next)
            if not links_next:
                log("[stop] page suivante sans annonces → fin de pagination")
                break
        except Exception as e:
            log(f"[stop] échec chargement page suivante: {e}")
            break

        # Tout va bien → on passe réellement à la prochaine page
        cur_url = next_url
        page_idx += 1
        time.sleep(sleep_s)

    return dedup(all_links)
    all_links: List[str] = []
    visited_pages = set()
    cur_url = start_url
    page_idx = 1
    while True:
        if max_pages and page_idx > max_pages:
            break
        if cur_url in visited_pages:
            log("[stop] boucle évitée: page déjà vue")
            break
        visited_pages.add(cur_url)

        log(f"[page {page_idx}] {cur_url}")
        soup = fetch(cur_url)
        links = extract_links_on_page(soup)
        log(f"[found] {len(links)} ad links on page {page_idx}")
        all_links.extend(links)

        next_url = find_next_page_url(soup)
        if not next_url:
            log("[done] pas de page suivante détectée.")
            break

        cur_url = next_url
        page_idx += 1
        time.sleep(sleep_s)
    return dedup(all_links)

def build_search_url(brand: str, model: Optional[str], year_from: Optional[int]) -> str:
    brand = (brand or "").strip().lower()
    model = (model or "").strip().lower() if model else None
    path = f"{BASE}/l/autos/{brand}/"
    if model:
        path += f"f/{model}/"
    frag = "Language:all-languages"
    if year_from:
        frag += f"|constructionYearFrom:{year_from}"
    return path + f"#f:{frag}"

# ───────────────────────────────────────────────────────────────────────────────
# Playwright scraper — fidèle au script 2 (scrape_2ememain_ml.py v9.0)
# ───────────────────────────────────────────────────────────────────────────────
from playwright.sync_api import sync_playwright

TAB_NAMES = [
    "Base","Technique","Éco","Eco","Historique","Options",
    "Basis","Techniek","Eco","Geschiedenis","Opties",
]

CSV_COLUMNS = [
    # core
    "url","ad_id","language","title","price_eur","currency",
    "seller_name","seller_rating","seller_reviews_count",
    "description",
    "images_count","images",
    # normalised ML fields
    "make","model","year","mileage_km","fuel","transmission","engine_liters",
    "power_hp","power_kw","co2_gkm","consumption_l_100","body_type","color",
    "doors","drivetrain_raw","turbo","euro_norm",
    # derived
    "age_years","price_per_km",
    # raw specs / options (pour features engineering)
    "options_count","options","specs_raw"
]

def clean(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def to_int_eur(s: str) -> Optional[int]:
    if not s: return None
    raw = re.sub(r"[^\d\.,\s]", "", s)
    raw = raw.replace(" ", "").replace(".", "").replace(",", ".")
    m = re.search(r"\d+(?:\.\d+)?", raw)
    if not m: return None
    try:
        return int(round(float(m.group(0))))
    except:
        return None

def to_int(s: Optional[str]) -> Optional[int]:
    if not s: return None
    t = re.sub(r"[^\d\-]", "", s.replace(".", "").replace(" ", ""))
    if not t: return None
    try:
        return int(t)
    except:
        m = re.search(r"\d+", s)
        return int(m.group(0)) if m else None

def to_float(s: Optional[str]) -> Optional[float]:
    if not s: return None
    s = s.replace(" ", "").replace(",", ".")
    m = re.search(r"[-+]?\d*\.?\d+", s)
    try:
        return float(m.group(0)) if m else None
    except:
        return None

def extract_ad_id(url: str) -> Optional[str]:
    m = re.search(r"/m(\d+)-", url)
    return m.group(1) if m else None

# Cookies (robuste) — inchangé
def click_if_exists(page, selector: str, timeout=1200) -> bool:
    try:
        loc = page.locator(selector)
        if loc.count() > 0:
            loc.first.click(timeout=timeout)
            page.wait_for_timeout(300)
            return True
    except Exception:
        pass
    return False

def try_sourcepoint_iframes(page, accept=True) -> bool:
    try:
        for f in page.frames:
            fid, furl = (f.name or ""), (f.url or "")
            if ("sp_message" in fid) or ("sp_message" in furl) or ("sourcepoint" in furl):
                patt = (r"(Tout accepter|Accepter|J.?accepte|Alles accepteren|Akkoord|Accepteren)"
                        if accept else r"(Continuer sans accepter|Tout refuser|Refuser|Weigeren|Afwijzen|Reject)")
                loc = f.get_by_role("button", name=re.compile(patt, re.I))
                if loc.count() > 0:
                    loc.first.click(timeout=1500); page.wait_for_timeout(300); return True
    except Exception:
        pass
    return False

def try_onetrust(page, accept=True) -> bool:
    if accept:
        if click_if_exists(page, "#onetrust-accept-btn-handler"): return True
        if click_if_exists(page, "button#onetrust-accept-btn-handler"): return True
    else:
        if click_if_exists(page, "#onetrust-reject-all-handler"): return True
    return False

def try_google_fc(page, accept=True) -> bool:
    try:
        for f in page.frames:
            if "fc" in (f.name or "") or "fundingchoices" in (f.url or ""):
                btns = [r"(Agree|Allow all|I agree)", r"(Accepter|J.?accepte|Tout accepter)", r"(Alles accepteren|Akkoord|Accepteren)"] if accept else [r"(Reject|Refuse|Tout refuser|Weigeren|Afwijzen)"]
                for pattern in btns:
                    loc = f.get_by_role("button", name=re.compile(pattern, re.I))
                    if loc.count() > 0:
                        loc.first.click(timeout=1500); page.wait_for_timeout(300); return True
    except Exception:
        pass
    return False

def try_preact_modal(page, accept=True) -> bool:
    if accept:
        patterns = ["button:has-text('Tout accepter')","button:has-text('Accepter')","button[aria-label*='Accepter']","button.message-button.primary"]
    else:
        patterns = ["button:has-text('Continuer sans accepter')","button:has-text('Refuser')","button[aria-label*='Refuser']"]
    for sel in patterns:
        if click_if_exists(page, sel): return True
    for sel in ["text=Continuer sans accepter","text=Tout accepter","text=Accepter","text=Alles accepteren","text=Akkoord"]:
        if click_if_exists(page, sel): return True
    return False

def accept_or_reject_cookies(page, accept=True) -> None:
    page.wait_for_timeout(600)
    if try_preact_modal(page, accept): return
    if try_sourcepoint_iframes(page, accept): return
    if try_onetrust(page, accept): return
    if try_google_fc(page, accept): return
    for pat in [r"(Tout accepter|Accepter|J.?accepte|Alles accepteren|Akkoord|Accepteren)",
                r"(Continuer sans accepter|Tout refuser|Refuser|Weigeren|Afwijzen|Reject)"]:
        try:
            loc = page.get_by_role("button", name=re.compile(pat, re.I))
            if loc.count() > 0:
                loc.first.click(timeout=1500); page.wait_for_timeout(300); return
        except Exception:
            pass

# Onglets — inchangé
def wait_dom_stable(page, ms: int = 350) -> None:
    page.wait_for_timeout(ms)

def get_visible_tab_ids(page) -> List[Tuple[str, str]]:
    tabs = []
    loc = page.get_by_role("tab")
    try:
        count = loc.count()
    except Exception:
        count = 0
    for i in range(count):
        try:
            el = loc.nth(i)
            name = clean(el.inner_text()) or clean(el.get_attribute("aria-label") or "")
            controls = el.get_attribute("aria-controls") or ""
            if name and controls:
                tabs.append((name, controls))
        except Exception:
            continue
    return tabs

def click_tab_by_name(page, want_name: str) -> bool:
    want = want_name.lower()
    try:
        page.get_by_role("tab", name=re.compile(rf"^{re.escape(want_name)}$", re.I)).first.click(timeout=1500)
        wait_dom_stable(page); return True
    except Exception:
        pass
    try:
        page.get_by_role("tab", name=re.compile(re.escape(want), re.I)).first.click(timeout=1500)
        wait_dom_stable(page); return True
    except Exception:
        pass
    for (name, _cid) in get_visible_tab_ids(page):
        if want in name.lower():
            try:
                page.get_by_role("tab", name=re.compile(re.escape(name), re.I)).first.click(timeout=1500)
                wait_dom_stable(page); return True
            except Exception:
                continue
    return False

def extract_active_tab_content(page) -> Dict[str, List]:
    pairs, values = [], []
    label_nodes = page.locator("div.CarAttributesTabs-itemLabel")
    try:
        label_count = label_nodes.count()
    except Exception:
        label_count = 0
    for i in range(label_count):
        try:
            label = clean(label_nodes.nth(i).inner_text())
            value_node = label_nodes.nth(i).locator(
                "xpath=following-sibling::div[contains(@class,'CarAttributesTabs-itemValue')]"
            ).first
            value = clean(value_node.inner_text()) if value_node else ""
            if label and value:
                pairs.append({"label": label, "value": value})
        except Exception:
            continue

    li_nodes = page.locator("li.CarAttributesTabs-valueWithoutLabel")
    try:
        vcount = li_nodes.count()
    except Exception:
        vcount = 0
    for i in range(vcount):
        try:
            txt = clean(li_nodes.nth(i).inner_text())
            if txt: values.append(txt)
        except Exception:
            continue

    if not pairs and not values:
        try:
            panel = page.locator("section.hz-TabPanelNext div.CarAttributesTabs-panelContainer").first
            all_divs = panel.locator("div").all()
            pending_label = None
            for d in all_divs:
                cls = d.get_attribute("class") or ""
                txt = clean(d.inner_text())
                if not txt: continue
                if "itemLabel" in cls:
                    pending_label = txt
                elif "itemValue" in cls and pending_label:
                    pairs.append({"label": pending_label, "value": txt})
                    pending_label = None
        except Exception:
            pass
    return {"pairs": pairs, "values": values}

# Meta/Description/Images — inchangé
def extract_meta(page) -> Dict[str, Any]:
    meta = {
        "language": None,
        "title": None, "price_eur": None, "currency": "EUR",
        "seller_name": None, "seller_rating": None, "seller_reviews_count": None,
        "description": None,
        "images_count": None, "images": [],
    }

    try:
        lang = page.locator("html").first.get_attribute("lang") or ""
        meta["language"] = lang.lower()[:2] if lang else None
    except Exception:
        pass

    try:
        t = clean(page.locator("h1.ListingHeader-title").first.inner_text())
        if t: meta["title"] = t
    except Exception:
        pass

    try:
        ptxt = clean(page.locator(".ListingHeader-price").first.inner_text())
        val = to_int_eur(ptxt)
        if val is not None:
            meta["price_eur"] = val
            meta["currency"] = "EUR"
    except Exception:
        pass

    try:
        name_loc = page.locator("#seller-info-expanded-root .SellerInfo-name a, #seller-info-expanded-root .SellerInfo-name")
        if name_loc.count() > 0:
            nm = clean(name_loc.first.inner_text())
            if nm: meta["seller_name"] = nm
    except Exception:
        pass

    try:
        items = page.locator("#seller-info-expanded-root ul.SellerTrustIndicators-root li.SellerTrustIndicator-root")
        for i in range(items.count()):
            it = items.nth(i)
            title_el = it.locator(".SellerTrustIndicator-title")
            body_el  = it.locator(".SellerTrustIndicator-body")
            title = clean(title_el.first.inner_text()) if title_el.count() > 0 else ""
            if re.search(r"^(Note|Beoordeling|Rating)$", title, re.I):
                body = clean(body_el.first.inner_text()) if body_el.count() > 0 else ""
                m = re.search(r"\d+(?:[.,]\d+)?", body)
                if m:
                    try: meta["seller_rating"] = float(m.group(0).replace(",", "."))
                    except: pass
                a = it.locator("a[href*='/experiences/user-reviews/']")
                if a.count() > 0:
                    link_txt = clean(a.first.inner_text())
                    m2 = re.search(r"(\d+)", link_txt)
                    if m2:
                        try: meta["seller_reviews_count"] = int(m2.group(1))
                        except: pass
                else:
                    m3 = re.search(r"(\d+)\s+(avis|beoordelingen?)", body, re.I)
                    if m3:
                        try: meta["seller_reviews_count"] = int(m3.group(1))
                        except: pass
                break
    except Exception:
        pass

    try:
        btn = page.locator("button:has-text('En savoir plus'), button:has-text('Meer lezen')")
        if btn.count() > 0:
            try: btn.first.click(timeout=1200)
            except Exception: pass
            page.wait_for_timeout(200)
        desc_node = page.locator("div.Description-description")
        if desc_node.count() > 0:
            desc = clean(desc_node.first.inner_text())
            if desc: meta["description"] = desc
    except Exception:
        pass

    try:
        # 1) "1/10"
        idx = page.locator("span.Gallery-carouselIndex")
        total_a = None
        if idx.count() > 0:
            txt = clean(idx.first.inner_text())
            m = re.search(r"/\s*(\d+)", txt)
            if m: total_a = int(m.group(1))
        # 2) miniatures
        thumbs = page.locator(".Thumbnails-root img")
        total_b = thumbs.count() if thumbs else None
        # 3) URLs API images
        imgs = []
        allimg = page.locator("img")
        for i in range(min(allimg.count(), 300)):
            try:
                src = (allimg.nth(i).get_attribute("src") or "").strip()
                if src.startswith("//"): src = "https:" + src
                if src.startswith("https://images.2dehands.com/api/"):
                    imgs.append(src)
            except Exception:
                continue
        imgs = dedup(imgs)
        total_c = len(imgs) if imgs else None

        cands = [x for x in [total_a, total_b, total_c] if x]
        meta["images_count"] = (max(cands) if cands else None)
        meta["images"] = imgs
    except Exception:
        pass

    return meta

# Normalisation — inchangée
def liters_from_value(val: str) -> Optional[float]:
    v = val.lower()
    if "liter" in v or " l" in v:
        return to_float(val)
    if "cm" in v:
        cc = to_int(val)
        return round(cc/1000.0, 3) if cc else None
    return to_float(val)

def normalise_from_pairs(pairs: List[Dict[str,str]]) -> Tuple[Dict[str,Any], Dict[str,str]]:
    d: Dict[str,str] = {}
    for p in pairs:
        L = clean(p.get("label","")); V = clean(p.get("value",""))
        if L and V and L not in d:
            d[L] = V

    def get_any(keys: List[str]) -> Optional[str]:
        for k in keys:
            if k in d: return d[k]
        for k in d:
            if any(re.search(fr"\b{re.escape(x)}\b", k, re.I) for x in keys):
                return d[k]
        return None

    out = {
        "make":None,"model":None,"year":None,"mileage_km":None,"fuel":None,"transmission":None,
        "engine_liters":None,"power_hp":None,"power_kw":None,"co2_gkm":None,"consumption_l_100":None,
        "body_type":None,"color":None,"doors":None,"drivetrain_raw":None,"turbo":None,"euro_norm":None,
    }

    mm = get_any(["Marque & Modèle","Merk & Model","Marque et modèle"])
    if mm:
        parts = clean(mm).split()
        if parts:
            out["make"] = parts[0]
            out["model"] = clean(mm[len(parts[0]):]).strip() or None

    y = get_any(["Année de fabrication","Bouwjaar"])
    if y:
        yi = to_int(y)
        if yi and 1900 <= yi <= datetime.now().year + 1:
            out["year"] = yi

    km = get_any(["Kilométrage","Kilometerstand"])
    out["mileage_km"] = to_int(km) if km else None

    out["fuel"] = get_any(["Carburant","Brandstof"])
    out["transmission"] = get_any(["Transmission","Transmissie"])

    eng = get_any(["Cylindrée","Cilinderinhoud","Moteur","Motor"])
    out["engine_liters"] = liters_from_value(eng) if eng else None

    out["euro_norm"] = get_any(["Euronorm","Euro norm","Emissions"])
    out["body_type"] = get_any(["Carrosserie","Carrosserievorm"])
    out["color"] = get_any(["Couleur","Kleur","Garniture","Intérieur"])

    ddoors = get_any(["Portes","Deuren"]); out["doors"] = to_int(ddoors) if ddoors else None
    out["drivetrain_raw"] = get_any(["Propulsion ou traction","Aandrijving"])

    powhp = get_any(["Puissance (ch)","Vermogen (pk)","Puissance (hp)"])
    powkw = get_any(["Puissance (kW)","Vermogen (kW)"])
    out["power_hp"] = to_int(powhp) if powhp else None
    out["power_kw"] = to_int(powkw) if powkw else (round(out["power_hp"]*0.7355) if isinstance(out["power_hp"], int) else None)

    out["co2_gkm"] = to_int(get_any(["CO2 (g/km)","CO2-uitstoot (g/km)"]) or "")
    out["consumption_l_100"] = to_float(get_any(["Consommation (l/100 km)","Verbruik (l/100 km)"]) or "")

    tb = get_any(["Turbo"])
    if tb:
        out["turbo"] = 1 if re.search(r"\b(oui|ja)\b", tb, re.I) else 0 if re.search(r"\b(non|nee)\b", tb, re.I) else None

    return out, d

from concurrent.futures import ProcessPoolExecutor, as_completed

def _safe_scrape(args):
    """Appel isolé dans un process: ne touche PAS à ta logique."""
    idx, url, headless, accept_cookies = args
    try:
        rec = scrape_one(url, headless=headless, accept_cookies=accept_cookies)
        return (idx, rec, None)
    except Exception as e:
        return (idx, None, f"{e}")


def scrape_one(url: str, headless: bool, accept_cookies: bool) -> Dict[str, Any]:
    rec: Dict[str, Any] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=["--no-sandbox","--disable-dev-shm-usage","--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"),
            locale="fr-FR",
        )
        page = ctx.new_page()
        page.set_default_timeout(8000)

        page.goto(url, wait_until="domcontentloaded")
        wait_dom_stable(page, 700)
        accept_or_reject_cookies(page, accept=accept_cookies)
        wait_dom_stable(page, 300)

        # Meta
        meta = extract_meta(page)

        # Onglets (on CLIQUE réellement chaque tab visible selon ta logique)
        specs_pairs: List[Dict[str,str]] = []
        options_values: List[str] = []

        visible_tabs = get_visible_tab_ids(page)
        seen = set()
        for name in TAB_NAMES:
            lname = name.lower()
            if lname in seen: continue
            if click_tab_by_name(page, name):
                seen.add(lname)
                wait_dom_stable(page, 250)
                content = extract_active_tab_content(page)
                specs_pairs += content["pairs"]
                options_values += content["values"]

        # fallback: clique tous les onglets visibles restants
        if not specs_pairs and not options_values:
            for (nm, _cid) in visible_tabs:
                if nm.lower() in seen: continue
                if click_tab_by_name(page, nm):
                    wait_dom_stable(page, 250)
                    content = extract_active_tab_content(page)
                    specs_pairs += content["pairs"]
                    options_values += content["values"]

        # Normalisation
        norm, specs_map = normalise_from_pairs(specs_pairs)
        options_values = dedup(options_values)

        # Record final
        rec.update({
            "url": url,
            "ad_id": extract_ad_id(url),
            "language": meta.get("language"),
            "title": meta.get("title"),
            "price_eur": meta.get("price_eur"),
            "currency": meta.get("currency"),
            "seller_name": meta.get("seller_name"),
            "seller_rating": meta.get("seller_rating"),
            "seller_reviews_count": meta.get("seller_reviews_count"),
            "description": meta.get("description"),
            "images_count": meta.get("images_count"),
            "images": meta.get("images") or [],
            # normalisé
            "make": norm["make"], "model": norm["model"], "year": norm["year"],
            "mileage_km": norm["mileage_km"], "fuel": norm["fuel"], "transmission": norm["transmission"],
            "engine_liters": norm["engine_liters"], "power_hp": norm["power_hp"], "power_kw": norm["power_kw"],
            "co2_gkm": norm["co2_gkm"], "consumption_l_100": norm["consumption_l_100"],
            "body_type": norm["body_type"], "color": norm["color"], "doors": norm["doors"],
            "drivetrain_raw": norm["drivetrain_raw"], "turbo": norm["turbo"], "euro_norm": norm["euro_norm"],
            # dérivées
            "age_years": (datetime.now().year - norm["year"]) if isinstance(norm["year"], int) else None,
            "price_per_km": (round(meta["price_eur"]/norm["mileage_km"], 6)
                             if isinstance(meta.get("price_eur"), int) and isinstance(norm["mileage_km"], int) and norm["mileage_km"]>0 else None),
            # brut pour features
            "options_count": len(options_values),
            "options": options_values,
            "specs_raw": specs_map,
        })

        ctx.close(); browser.close()
    return rec

def write_outputs(records: List[Dict[str, Any]], csv_path="vehicles.csv", jsonl_path="vehicles.jsonl"):
    # JSONL
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # CSV (listes en pipe, dicts en json compact)
    def cell(v):
        if isinstance(v, list): return "|".join(map(str, v))
        if isinstance(v, dict): return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
        return v

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for r in records:
            w.writerow({k: cell(r.get(k)) for k in CSV_COLUMNS})

def read_urls_from_file(p: str) -> List[str]:
    out = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"): out.append(line)
    return out

# ───────────────────────────────────────────────────────────────────────────────
# ML — fidèle au script 3 (train_deal_model.py)
# ───────────────────────────────────────────────────────────────────────────────
import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.impute import SimpleImputer
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, FunctionTransformer, StandardScaler
from sklearn.ensemble import GradientBoostingRegressor

import hashlib
from collections import defaultdict

FOLD_K = 5  # par défaut 5 folds

def row_key(row: dict) -> str:
    """Identifie une annonce. Priorité ad_id, sinon url."""
    k = str(row.get("ad_id") or "").strip()
    if not k:
        k = str(row.get("url") or "").strip()
    return k

def stable_hash_to_fold(key: str, k: int = FOLD_K, salt: str = "2ememain_v1") -> int:
    """Renvoie un fold id [0..k-1] stable pour une clé donnée."""
    if not key:
        # clé vide => répartir quand même de façon stable
        key = "NA"
    h = hashlib.md5((salt + "§" + key).encode("utf-8")).hexdigest()
    return int(h[:8], 16) % k


warnings.filterwarnings("ignore", category=UserWarning)

CURRENT_YEAR = pd.Timestamp.today().year

NUMERIC_BOUNDS = {
    "price_eur": (500, 200_000),
    "mileage_km": (0, 600_000),
    "year": (1995, CURRENT_YEAR + 1),
    "images_count": (0, 200),
    "power_hp": (20, 1000),
    "power_kw": (10, 750),
    "co2_gkm": (0, 1000),
    "consumption_l_100": (1.5, 25),
}

CATS = ["make","model","fuel","transmission","body_type","color"]
TOP_CAT_MAX = 60
# APRES
TEXT_FEATURES = ["text_all"]  # options + description concaténées

NUM_FEATURES = [
    "year","mileage_km","age_years","km_per_year",
    "power_kw","co2_gkm","consumption_l_100","images_count",
    "seller_rating","seller_reviews_count","engine_liters","doors",
    # --- Nouveaux signaux issus de la description ---
    "desc_len", "cond_pos", "cond_neg", "cond_score",
    "has_export","has_ct_fail","has_accident","has_damage",
    "has_engine_issue","has_gearbox_issue","has_non_rolling",
    "has_rust","has_oil_consumption","has_noise_smoke","has_km_not_guaranteed",
    "has_ct_ok","has_carpass","has_maintenance_history","has_first_owner",
    "has_non_smoker","has_warranty","has_no_costs","runs_perfect",
]


def read_any(path: str) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    if p.suffix.lower() == ".jsonl":
        rows = []
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line: continue
                rows.append(json.loads(line))
        return pd.DataFrame(rows)
    elif p.suffix.lower() == ".csv":
        return pd.read_csv(p)
    else:
        raise ValueError("Supported: .jsonl or .csv")

def write_jsonl(df: pd.DataFrame, path: str):
    with open(path, "w", encoding="utf-8") as f:
        for _, row in df.iterrows():
            f.write(json.dumps(row.to_dict(), ensure_ascii=False) + "\n")

def list_to_text(lst) -> str:
    if isinstance(lst, list):
        return " ".join(str(x) for x in lst if isinstance(x, str))
    return ""

def to_topn(series: pd.Series, topn=TOP_CAT_MAX) -> pd.Series:
    vc = series.value_counts(dropna=True)
    keep = set(vc.head(topn).index.tolist())
    return series.apply(lambda x: x if x in keep else "__OTHER__")

# ─────────── Analyse description (flags FR/NL/EN) ───────────

DESC_NEG_PATTERNS = {
    "has_export": r"\b(export|voor\s+export|alleen\s+export|exportvoertuig|destin[ée]e?\s+à\s+l'?export)\b",
    "has_ct_fail": r"(contr[ôo]le?\s+technique|keuring).{0,60}\b(refus[ée]e?|afgekeurd|herkeuring|rode?\s+kaart)",
    "has_accident": r"\b(accident[ée]?|sinistr[ée]?|ongeval|botsing)\b",
    "has_damage": r"\b(cass[ée]e?s?|cassure?s?|griffes?|rayures?|krassen?|deuk(en)?|bosse?s?)\b",
    "has_engine_issue": r"(moteur|motor).{0,40}\b(hs|panne|defect|defekt|kapot|à\s+remplacer)",
    "has_gearbox_issue": r"(bo[iî]te|bo[îi]te\s+de\s+vitesses|versnellingsbak).{0,40}\b(hs|panne|defect|schakelt\s+slecht|à\s+remplacer)",
    "has_non_rolling": r"\b(non\s+roulant|ne\s+roule\s+pas|niet\s+rijdend)\b",
    "has_rust": r"\b(rouille|roest)\b",
    "has_oil_consumption": r"(consommation\s+d['’]huile|olieverbruik)\b",
    "has_noise_smoke": r"\b(fum[ée]e?s?|rook|bruit|geluid)\b",
    "has_km_not_guaranteed": r"(kilom[ée]trage\s+non\s+garanti|km-?stand\s+niet\s+gegarandeerd)"
}

DESC_POS_PATTERNS = {
    "has_ct_ok": r"(contr[ôo]le?\s+technique|keuring).{0,40}\b(ok|valide|goedgekeurd)",
    "has_carpass": r"\b(carpass)\b",
    "has_maintenance_history": r"(entretien|onderhoud|carnet|factures?)\b",
    "has_first_owner": r"(premi[èe]re?\s+main|eerste\s+eigenaar)",
    "has_non_smoker": r"(non[-\s]?fumeur|rookvrij)",
    "has_warranty": r"(garantie|waarborg)\b",
    "has_no_costs": r"(aucun\s+frais\s+[àa]\s+pr[ée]voir|zonder\s+kosten|nothing\s+to\s+do)",
    "runs_perfect": r"(roule|rijdt).{0,20}(parfait|perfect)"
}

def _desc_clean(text: str) -> str:
    text = str(text or "")
    text = text.lower()
    text = re.sub(r"http[s]?://\S+|www\.\S+", " ", text)         # URLs
    text = re.sub(r"\+?\d[\d\s\-]{6,}", " ", text)               # numéros tel
    text = re.sub(r"\s+", " ", text).strip()
    return text

def extract_desc_flags(text: str) -> Dict[str, int | float]:
    t = _desc_clean(text)
    out = {}
    # négatifs
    for k, pat in DESC_NEG_PATTERNS.items():
        out[k] = 1 if re.search(pat, t, flags=re.I) else 0
    # positifs
    for k, pat in DESC_POS_PATTERNS.items():
        out[k] = 1 if re.search(pat, t, flags=re.I) else 0

    out["cond_pos"] = int(sum(out[k] for k in DESC_POS_PATTERNS.keys()))
    out["cond_neg"] = int(sum(out[k] for k in DESC_NEG_PATTERNS.keys()))
    out["cond_score"] = float(out["cond_pos"] - 2 * out["cond_neg"])  # poids négatifs plus forts
    out["desc_len"] = float(len(t))

    return out


def preprocess(df: pd.DataFrame, for_training=True) -> pd.DataFrame:
    df = df.copy()
    if "ad_id" in df.columns:
        df = df.drop_duplicates(subset=["ad_id"])
    df = df.drop_duplicates(subset=["url"])

    needed = ["price_eur","year","mileage_km","make","model"]
    for col in needed:
        if col not in df.columns:
            df[col] = np.nan
    df = df[ df["price_eur"].notna() & df["year"].notna() & df["mileage_km"].notna() ]

    for k in ["price_eur","year","mileage_km","power_hp","power_kw","co2_gkm",
              "consumption_l_100","images_count","seller_rating","seller_reviews_count","doors","engine_liters"]:
        if k in df.columns:
            df[k] = pd.to_numeric(df[k], errors="coerce")

    for k, (lo, hi) in NUMERIC_BOUNDS.items():
        if k in df.columns:
            df = df[(df[k].isna()) | ((df[k] >= lo) & (df[k] <= hi))]

    df["age_years"] = CURRENT_YEAR - df["year"]
    df.loc[df["age_years"] < 0, "age_years"] = np.nan
    df["km_per_year"] = df["mileage_km"] / df["age_years"].replace(0, np.nan)
    df["km_per_year"] = df["km_per_year"].clip(0, 100_000)

    if "power_kw" in df.columns:
        miss = df["power_kw"].isna() & df["power_hp"].notna()
        df.loc[miss, "power_kw"] = df.loc[miss, "power_hp"] * 0.7355

    df["price_per_km"] = df["price_eur"] / df["mileage_km"].replace(0, np.nan)

    df["options_text"] = df.get("options", pd.Series([""]*len(df))).apply(list_to_text)
    df["desc_text"] = df.get("description", pd.Series([""]*len(df))).astype(str).fillna("")
    df["desc_text"] = df["desc_text"].str.slice(0, 2000)
    # 1) Flags qualité issus de la description
    flags_df = df["desc_text"].apply(extract_desc_flags).apply(pd.Series)

    # Assure présence de toutes les colonnes même si aucune occurrence
    for col in [
        "desc_len","cond_pos","cond_neg","cond_score",
        "has_export","has_ct_fail","has_accident","has_damage",
        "has_engine_issue","has_gearbox_issue","has_non_rolling",
        "has_rust","has_oil_consumption","has_noise_smoke","has_km_not_guaranteed",
        "has_ct_ok","has_carpass","has_maintenance_history","has_first_owner",
        "has_non_smoker","has_warranty","has_no_costs","runs_perfect",
    ]:
        if col not in flags_df.columns:
            flags_df[col] = 0

    df = pd.concat([df, flags_df], axis=1)

    # 2) Texte global pour TF-IDF (options + description)
    df["text_all"] = (df["options_text"].astype(str) + " " + df["desc_text"].astype(str)).str.strip()


    for c in CATS:
        if c not in df.columns:
            df[c] = "__NA__"
        df[c] = df[c].astype(str).fillna("__NA__")
        df[c] = to_topn(df[c], topn=TOP_CAT_MAX)

    if for_training:
        df = df.dropna(subset=["price_eur","year","mileage_km","age_years"])
        df = df[(df["km_per_year"].isna()) | (df["km_per_year"] <= 80_000)]
    return df

from inspect import signature

def _make_ohe():
    # Compatibilité sklearn 1.0 → 1.6+
    params = signature(OneHotEncoder).parameters
    if "sparse_output" in params:  # nouvelles versions
        return OneHotEncoder(handle_unknown="ignore", sparse_output=True)
    else:                          # anciennes versions
        return OneHotEncoder(handle_unknown="ignore", sparse=True)

def build_preprocessor(vocab: Dict[str, Any] | None = None) -> ColumnTransformer:
    numeric = Pipeline(steps=[
        ("imp", SimpleImputer(strategy="median")),
        ("sc", StandardScaler(with_mean=False)),  # ok pour sparse
    ])
    categorical = Pipeline(steps=[
        ("imp", SimpleImputer(strategy="most_frequent")),
        ("oh", _make_ohe()),
    ])
    if vocab is None:
        text = Pipeline(steps=[
            ("tfidf", TfidfVectorizer(max_features=700, ngram_range=(1,2), min_df=5))
        ])
    else:
        text = Pipeline(steps=[
            ("tfidf", TfidfVectorizer(vocabulary=vocab["tfidf_vocab"]))
        ])
    return ColumnTransformer(
        transformers=[
            ("num", numeric, NUM_FEATURES),
            ("cat", categorical, CATS),
            ("txt", text, "text_all"),
        ],
        remainder="drop",
        sparse_threshold=0.3,
    )


def fit_transform_tfidf_vocab(pre: ColumnTransformer, df: pd.DataFrame) -> Dict[str, Any]:
    _ = pre.fit_transform(df)
    tfidf = None
    for name, trans, cols in pre.transformers_:
        if name == "txt":
            for stepname, step in trans.steps:
                if stepname == "tfidf":
                    tfidf = step
    vocab = {"tfidf_vocab": dict(tfidf.vocabulary_)} if tfidf is not None else {}
    return vocab

import math
import numpy as np

def json_safe(x):
    """Convertit récursivement les types NumPy vers des types Python JSON-safe."""
    if isinstance(x, np.generic):          # np.int32, np.float64, etc.
        return x.item()
    if isinstance(x, np.ndarray):
        return x.tolist()
    if isinstance(x, dict):
        return {str(k): json_safe(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [json_safe(v) for v in x]
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    return x


def train_models(df: pd.DataFrame, outdir: str, n_folds: int = FOLD_K) -> Dict[str, Any]:
    os.makedirs(outdir, exist_ok=True)
    base = preprocess(df, for_training=True).reset_index(drop=True)

    # --- clés & folds déterministes
    keys = base.apply(lambda r: row_key(r), axis=1)
    folds = keys.apply(lambda k: stable_hash_to_fold(k, k=n_folds)).values
    base["__fold__"] = folds
    base["__key__"]  = keys

    y = base["price_eur"].astype(float).values
    X = base

    # --- Vocab TF-IDF "figé" une seule fois (évite vocab différent par fold)
    gbr_med = GradientBoostingRegressor(
        loss="quantile", alpha=0.5, n_estimators=500, max_depth=3,
        learning_rate=0.05, subsample=0.8, random_state=42
    )
    pre_tmp = build_preprocessor()
    pipe_tmp = Pipeline(steps=[("pre", pre_tmp), ("reg", gbr_med)])
    pipe_tmp.fit(X, y)
    vocab = fit_transform_tfidf_vocab(pipe_tmp.named_steps["pre"], X)
    pre_fixed = build_preprocessor(vocab=vocab)

    # --- Entraînement CV: K modèles par quantile + OOF preds
    cv_dir = Path(outdir) / "cv_models"
    cv_dir.mkdir(parents=True, exist_ok=True)

    def fit_on(mask_train, alpha):
        reg = GradientBoostingRegressor(
            loss="quantile", alpha=alpha, n_estimators=500, max_depth=3,
            learning_rate=0.05, subsample=0.8, random_state=42
        )
        pipe = Pipeline(steps=[("pre", pre_fixed), ("reg", reg)])
        pipe.fit(X[mask_train], y[mask_train])
        return pipe

    oof_q10 = np.zeros(len(X), dtype=float)
    oof_q50 = np.zeros(len(X), dtype=float)
    oof_q90 = np.zeros(len(X), dtype=float)

    models_q10, models_q50, models_q90 = [], [], []

    for f in range(n_folds):
        mask_val = (folds == f)
        mask_tr  = ~mask_val
        m10 = fit_on(mask_tr, 0.10)
        m50 = fit_on(mask_tr, 0.50)
        m90 = fit_on(mask_tr, 0.90)

        # sauvegarde des modèles du fold
        joblib.dump(m10, cv_dir / f"model_q10_fold{f}.joblib", compress=3)
        joblib.dump(m50, cv_dir / f"model_q50_fold{f}.joblib", compress=3)
        joblib.dump(m90, cv_dir / f"model_q90_fold{f}.joblib", compress=3)

        # préd OOF sur le fold f (jamais vu à l'entraînement)
        oof_q10[mask_val] = m10.predict(X[mask_val])
        oof_q50[mask_val] = m50.predict(X[mask_val])
        oof_q90[mask_val] = m90.predict(X[mask_val])

        models_q10.append(m10); models_q50.append(m50); models_q90.append(m90)

    # --- Calibration conformale sur résidus OOF (aucune fuite)
    cover = 0.90
    a_lo = np.maximum(oof_q10 - y, 0.0)
    a_hi = np.maximum(y - oof_q90, 0.0)
    c_lo = float(np.quantile(a_lo, cover))
    c_hi = float(np.quantile(a_hi, cover))

    # --- Métriques OOF
    mae  = float(mean_absolute_error(y, oof_q50))
    mape = float(np.median(np.abs((y - oof_q50) / np.clip(y, 1.0, None))))
    r2   = float(r2_score(y, oof_q50))
    metrics = {
        "mae": mae, "mape_median": mape, "r2": r2,
        "n_rows": int(len(X)), "n_folds": int(n_folds)
    }
    print("[OOF] MAE=%.1f € | MedMAPE=%.3f | R2=%.3f" % (mae, mape, r2))

    # --- Modèles "full-data" (pour scorer des annonces nouvelles/inconnues)
    full_q10 = fit_on(np.ones(len(X), dtype=bool), 0.10)
    full_q50 = fit_on(np.ones(len(X), dtype=bool), 0.50)
    full_q90 = fit_on(np.ones(len(X), dtype=bool), 0.90)
    joblib.dump(full_q10, Path(outdir) / "model_q10.joblib", compress=3)
    joblib.dump(full_q50, Path(outdir) / "model_q50.joblib", compress=3)
    joblib.dump(full_q90, Path(outdir) / "model_q90.joblib", compress=3)

    # --- Persistences: calibration, vocab, mapping fold
    calib = {"c_lo": c_lo, "c_hi": c_hi, "tfidf_vocab": vocab.get("tfidf_vocab", {})}
    with open(Path(outdir) / "calibration.json", "w", encoding="utf-8") as f:
        json.dump(json_safe({"calibration": calib, "metrics": metrics}), f, ensure_ascii=False, indent=2)

    # mapping clé -> fold pour scoring OOF auto
    with open(Path(outdir) / "fold_map.jsonl", "w", encoding="utf-8") as f:
        for k, fo in zip(base["__key__"], base["__fold__"]):
            f.write(json.dumps({"key": k, "fold": int(fo)}, ensure_ascii=False) + "\n")

    return {"metrics": metrics, "calibration": calib}
    os.makedirs(outdir, exist_ok=True)

    base = preprocess(df, for_training=True)
    y = base["price_eur"].astype(float).values
    X = base

    X_train, X_tmp, y_train, y_tmp = train_test_split(X, y, test_size=0.20, random_state=42)
    X_val,   X_test, y_val, y_test = train_test_split(X_tmp, y_tmp, test_size=0.50, random_state=42)

    # median(q50) init
    gbr_med = GradientBoostingRegressor(
        loss="quantile", alpha=0.5, n_estimators=500, max_depth=3,
        learning_rate=0.05, subsample=0.8, random_state=42
    )
    pre_tmp = build_preprocessor()
    pipe_tmp = Pipeline(steps=[("pre", pre_tmp), ("reg", gbr_med)])
    pipe_tmp.fit(X_train, y_train)

    vocab = fit_transform_tfidf_vocab(pipe_tmp.named_steps["pre"], X_train)
    pre_fixed = build_preprocessor(vocab=vocab)

    def refit(alpha):
        reg = GradientBoostingRegressor(
            loss="quantile", alpha=alpha, n_estimators=500, max_depth=3,
            learning_rate=0.05, subsample=0.8, random_state=42
        )
        pipe = Pipeline(steps=[("pre", pre_fixed), ("reg", reg)])
        pipe.fit(X_train, y_train)
        return pipe

    model_q50 = refit(0.50)
    model_q10 = refit(0.10)
    model_q90 = refit(0.90)

    q10_val = model_q10.predict(X_val)
    q50_val = model_q50.predict(X_val)
    q90_val = model_q90.predict(X_val)

    cover = 0.90
    a_lo = np.maximum(q10_val - y_val, 0.0)
    a_hi = np.maximum(y_val - q90_val, 0.0)
    c_lo = np.quantile(a_lo, cover)
    c_hi = np.quantile(a_hi, cover)

    calib = {"c_lo": float(c_lo), "c_hi": float(c_hi), "tfidf_vocab": vocab.get("tfidf_vocab", {})}

    q50_test = model_q50.predict(X_test)
    mae = mean_absolute_error(y_test, q50_test)
    mape = float(np.median(np.abs((y_test - q50_test) / np.clip(y_test, 1.0, None))))
    r2 = r2_score(y_test, q50_test)
    metrics = {"mae": float(mae), "mape_median": mape, "r2": float(r2), "n_train": int(len(X_train)), "n_val": int(len(X_val)), "n_test": int(len(X_test))}
    print("[Eval] MAE=%.1f € | MedMAPE=%.3f | R2=%.3f" % (mae, mape, r2))

    joblib.dump(model_q50, Path(outdir) / "model_q50.joblib", compress=3)
    joblib.dump(model_q10, Path(outdir) / "model_q10.joblib", compress=3)
    joblib.dump(model_q90, Path(outdir) / "model_q90.joblib", compress=3)
    payload = {"calibration": calib, "metrics": metrics}
    with open(Path(outdir) / "calibration.json", "w", encoding="utf-8") as f:
        json.dump(json_safe(payload), f, ensure_ascii=False, indent=2)


    return {"models": (model_q10, model_q50, model_q90), "calibration": calib, "metrics": metrics}

def classify_deal(price, fair, lo, hi) -> str:
    if np.isfinite(lo) and price < lo and (fair - price) / max(fair, 1.0) >= 0.10:
        return "good_deal"
    if np.isfinite(hi) and price > hi and (fair - price) / max(fair, 1.0) <= -0.10:
        return "overpriced"
    return "fair"

def load_fold_map(artifacts_dir: str) -> Dict[str, int]:
    p = Path(artifacts_dir) / "fold_map.jsonl"
    mp: Dict[str, int] = {}
    if p.exists():
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip(): continue
                d = json.loads(line)
                mp[str(d.get("key"))] = int(d.get("fold"))
    return mp

def score_file(df: pd.DataFrame, artifacts_dir: str) -> pd.DataFrame:
    # calibration
    calib_json = json.loads(Path(artifacts_dir, "calibration.json").read_text(encoding="utf-8"))
    calib = calib_json["calibration"]
    c_lo, c_hi = calib["c_lo"], calib["c_hi"]

    # modèles "full"
    full_q50 = joblib.load(Path(artifacts_dir) / "model_q50.joblib")
    full_q10 = joblib.load(Path(artifacts_dir) / "model_q10.joblib")
    full_q90 = joblib.load(Path(artifacts_dir) / "model_q90.joblib")

    # modèles CV (si dispo)
    cv_dir = Path(artifacts_dir) / "cv_models"
    has_cv = cv_dir.exists()
    cv_q50, cv_q10, cv_q90 = {}, {}, {}
    if has_cv:
        for f in range(FOLD_K):
            if (cv_dir / f"model_q50_fold{f}.joblib").exists():
                cv_q50[f] = joblib.load(cv_dir / f"model_q50_fold{f}.joblib")
                cv_q10[f] = joblib.load(cv_dir / f"model_q10_fold{f}.joblib")
                cv_q90[f] = joblib.load(cv_dir / f"model_q90_fold{f}.joblib")

    fold_map = load_fold_map(artifacts_dir)

    X = preprocess(df, for_training=False).reset_index(drop=True)
    # prépare les clés dans le même ordre que X
    keys = X.apply(lambda r: row_key(r), axis=1)

    fair = np.zeros(len(X), dtype=float)
    qlo  = np.zeros(len(X), dtype=float)
    qhi  = np.zeros(len(X), dtype=float)

    for i in range(len(X)):
        k = keys.iloc[i]
        # si l'annonce faisait partie du train et qu'on a un modèle de fold => OOF
        if has_cv and (k in fold_map) and (fold_map[k] in cv_q50):
            f = fold_map[k]
            fair[i] = cv_q50[f].predict(X.iloc[[i]])[0]
            qlo[i]  = cv_q10[f].predict(X.iloc[[i]])[0] - c_lo
            qhi[i]  = cv_q90[f].predict(X.iloc[[i]])[0] + c_hi
        else:
            # annonce nouvelle ou pas de CV disponible -> modèle full
            fair[i] = full_q50.predict(X.iloc[[i]])[0]
            qlo[i]  = full_q10.predict(X.iloc[[i]])[0] - c_lo
            qhi[i]  = full_q90.predict(X.iloc[[i]])[0] + c_hi

    out = X.copy()
    out["pred_price_fair"] = np.round(fair, 0)
    out["pred_price_lo"]   = np.round(np.maximum(0, qlo), 0)
    out["pred_price_hi"]   = np.round(np.maximum(out["pred_price_lo"], qhi), 0)
    out["deal_score"]      = (out["pred_price_fair"] - out["price_eur"]) / (np.clip(out["pred_price_fair"], 1.0, None))
    out["deal_label"]      = [
        classify_deal(p, f, l, h) for p, f, l, h in zip(out["price_eur"], out["pred_price_fair"], out["pred_price_lo"], out["pred_price_hi"])
    ]

    cols_first = [
        "url","ad_id","make","model","year","mileage_km","fuel","transmission","body_type","age_years",
        "price_eur","pred_price_lo","pred_price_fair","pred_price_hi","deal_score","deal_label",
        "seller_name","seller_rating","seller_reviews_count","images_count","options_count"
    ]
    for c in cols_first:
        if c not in out.columns:
            out[c] = np.nan
    ordered = cols_first + [c for c in out.columns if c not in cols_first]
    return out[ordered]
    model_q50 = joblib.load(Path(artifacts_dir) / "model_q50.joblib")
    model_q10 = joblib.load(Path(artifacts_dir) / "model_q10.joblib")
    model_q90 = joblib.load(Path(artifacts_dir) / "model_q90.joblib")
    calib_json = json.loads(Path(artifacts_dir, "calibration.json").read_text(encoding="utf-8"))
    calib = calib_json["calibration"]
    c_lo, c_hi = calib["c_lo"], calib["c_hi"]

    X = preprocess(df, for_training=False)

    fair = model_q50.predict(X)
    qlo = model_q10.predict(X) - c_lo
    qhi = model_q90.predict(X) + c_hi

    out = X.copy()
    out["pred_price_fair"] = fair.round(0)
    out["pred_price_lo"] = np.maximum(0, qlo).round(0)
    out["pred_price_hi"] = np.maximum(out["pred_price_lo"], qhi).round(0)
    out["deal_score"] = (out["pred_price_fair"] - out["price_eur"]) / (np.clip(out["pred_price_fair"], 1.0, None))
    out["deal_label"] = [
        classify_deal(p, f, l, h) for p, f, l, h in zip(out["price_eur"], out["pred_price_fair"], out["pred_price_lo"], out["pred_price_hi"])
    ]

    cols_first = [
        "url","ad_id","make","model","year","mileage_km","fuel","transmission","body_type","age_years",
        "price_eur","pred_price_lo","pred_price_fair","pred_price_hi","deal_score","deal_label",
        "seller_name","seller_rating","seller_reviews_count","images_count","options_count"
    ]
    for c in cols_first:
        if c not in out.columns:
            out[c] = np.nan
    ordered = cols_first + [c for c in out.columns if c not in cols_first]
    return out[ordered]

# ───────────────────────────────────────────────────────────────────────────────
# Sous-commandes CLI
# ───────────────────────────────────────────────────────────────────────────────

def cmd_search(args):
    if not args.dir:
        print("❗ Utilise --dir pour indiquer le dossier de run (ex: runs/audi_a5_2010).", file=sys.stderr)
        sys.exit(1)
    P = run_paths(args.dir)

    start_url = args.url or build_search_url(args.brand, args.model, args.year_from)
    log(f"[start] {start_url}")
    links = collect_all_links(start_url, max_pages=args.max_pages)
    if not links:
        log("[info] aucun lien trouvé.")
        sys.exit(2)

    # sortie forcée dans <dir>/urls.txt sauf si --out explicite
    out_file = Path(args.out) if args.out else P["urls"]
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with out_file.open("w", encoding="utf-8") as f:
        for u in links:
            f.write(u + "\n")
    log(f"[ok] {len(links)} liens écrits dans {out_file}")


def cmd_scrape(args):
    if not args.dir:
        print("❗ Utilise --dir pour indiquer le dossier de run (ex: runs/audi_a5_2010).", file=sys.stderr)
        sys.exit(1)
    P = run_paths(args.dir)

    # sources d'URLs
    urls: List[str] = []
    if args.input:
        urls += read_urls_from_file(args.input)
    elif args.urls:
        urls += args.urls
    else:
        # défaut: <dir>/urls.txt
        if P["urls"].exists():
            urls += read_urls_from_file(str(P["urls"]))
        else:
            print("❗ Aucune URL fournie et aucun urls.txt trouvé dans le dossier de run.", file=sys.stderr)
            sys.exit(1)
    if not urls:
        print("❗ Aucune URL à scraper.", file=sys.stderr)
        sys.exit(1)

    # sorties par défaut dans le dossier
    csv_path  = Path(args.csv)  if args.csv  else P["vehicles_csv"]
    jsonl_path= Path(args.jsonl) if args.jsonl else P["vehicles_jsonl"]
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    # exécution
    tasks = [(i, u, args.headless, (not args.reject)) for i, u in enumerate(urls)]
    results_ordered: List[Optional[Dict[str, Any]]] = [None] * len(tasks)

    workers = getattr(args, "workers", 2)
    print(f"[run] scraping parallèle avec {workers} worker(s)")

    nb_ok, nb_err = 0, 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_safe_scrape, t): t for t in tasks}
        for fut in as_completed(futs):
            idx, rec, err = fut.result()
            url = urls[idx]
            if err is None and isinstance(rec, dict):
                results_ordered[idx] = rec
                nb_ok += 1
                print(f"[✓] OK: {url}")
            else:
                nb_err += 1
                print(f"[×] ERREUR {url}: {err}")

    records = [r for r in results_ordered if r is not None]
    if not records:
        print("Aucun enregistrement", file=sys.stderr)
        sys.exit(2)

    write_outputs(records, csv_path=str(csv_path), jsonl_path=str(jsonl_path))
    print(f"[✓] Écrits: {jsonl_path} et {csv_path} | OK={nb_ok} | ERR={nb_err}")


def cmd_train(args):
    if not args.dir:
        print("❗ Utilise --dir pour indiquer le dossier de run (ex: runs/audi_a5_2010).", file=sys.stderr)
        sys.exit(1)
    P = run_paths(args.dir)

    in_path = Path(args.in_path) if args.in_path else P["vehicles_jsonl"]
    if not in_path.exists():
        print(f"❗ Fichier d'entrée introuvable: {in_path}", file=sys.stderr)
        sys.exit(1)
    outdir = Path(args.outdir) if args.outdir else P["artifacts"]
    outdir.mkdir(parents=True, exist_ok=True)

    df = read_any(str(in_path))
    print(f"[load] {len(df)} rows from {in_path}")
    n_folds = getattr(args, "cv", FOLD_K)
    train_models(df, str(outdir), n_folds=n_folds)
    print(f"[done] artifacts → {outdir}")


def cmd_score(args):
    if not args.dir:
        print("❗ Utilise --dir pour indiquer le dossier de run (ex: runs/audi_a5_2010).", file=sys.stderr)
        sys.exit(1)
    P = run_paths(args.dir)

    in_path = Path(args.in_path) if args.in_path else P["vehicles_jsonl"]
    artifacts = Path(args.artifacts) if args.artifacts else P["artifacts"]
    out_path = Path(args.out) if args.out else P["scored_csv"]  # par défaut CSV

    if not in_path.exists():
        print(f"❗ Fichier d'entrée introuvable: {in_path}", file=sys.stderr)
        sys.exit(1)
    if not artifacts.exists():
        print(f"❗ Dossier d'artefacts introuvable: {artifacts}", file=sys.stderr)
        sys.exit(1)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    df = read_any(str(in_path))
    print(f"[load] {len(df)} rows from {in_path}")
    scored = score_file(df, str(artifacts))
    print(f"[scored] {len(scored)} rows")

    if out_path.suffix.lower() == ".csv":
        scored.to_csv(out_path, index=False)
    else:
        write_jsonl(scored, str(out_path))
    print(f"[done] wrote {out_path}")


def main():
    ap = argparse.ArgumentParser(description="2ememain.be pipeline (search→scrape→train→score) — orienté dossier de run")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # search
    ap_s = sub.add_parser("search", help="Collecte d'URLs d'annonces (pagination Suivant)")
    ap_s.add_argument("--dir", required=True, help="Dossier de run (ex: runs/audi_a5_2010)")
    ap_s.add_argument("--url", help="URL de recherche 2ememain (recommandé)")
    ap_s.add_argument("--brand", help="Marque (ex: audi)")
    ap_s.add_argument("--model", help="Modèle (ex: a1)")
    ap_s.add_argument("--year_from", type=int, help="Année min (ex: 2019)")
    ap_s.add_argument("--max_pages", type=int, help="Limite de pages à parcourir")
    ap_s.add_argument("--out", help="(optionnel) Chemin de sortie custom, sinon <dir>/urls.txt")
    ap_s.set_defaults(func=cmd_search)

    # scrape
    ap_c = sub.add_parser("scrape", help="Scraper les annonces (ouvre la page + clique les onglets)")
    ap_c.add_argument("--dir", required=True, help="Dossier de run (ex: runs/audi_a5_2010)")
    ap_c.add_argument("urls", nargs="*", help="URLs d'annonces (optionnel si <dir>/urls.txt existe)")
    ap_c.add_argument("-i", "--input", help="Fichier texte avec 1 URL par ligne (optionnel)")
    ap_c.add_argument("--csv", help="(optionnel) chemin CSV sortie, sinon <dir>/vehicles.csv")
    ap_c.add_argument("--jsonl", help="(optionnel) chemin JSONL sortie, sinon <dir>/vehicles.jsonl")
    ap_c.add_argument("--headless", action="store_true", help="Lancer Chromium en headless")
    ap_c.add_argument("--reject", action="store_true", help="Rejeter les cookies (par défaut: accepter)")
    ap_c.add_argument("--workers", type=int, default=2, help="Nb de workers parallèles (default: 2)")
    ap_c.set_defaults(func=cmd_scrape)

    # train
    ap_tr = sub.add_parser("train", help="Entraîner modèles quantiles + calibration conformale (CV OOF auto)")
    ap_tr.add_argument("--dir", required=True, help="Dossier de run (ex: runs/audi_a5_2010)")
    ap_tr.add_argument("--in", dest="in_path", help="(optionnel) chemin input, sinon <dir>/vehicles.jsonl")
    ap_tr.add_argument("--outdir", dest="outdir", help="(optionnel) artefacts, sinon <dir>/model_artifacts")
    ap_tr.add_argument("--cv", dest="cv", type=int, default=FOLD_K, help="nb de folds CV (default 5)")
    ap_tr.set_defaults(func=cmd_train)

    # score
    ap_sc = sub.add_parser("score", help="Scorer un dump avec les artefacts")
    ap_sc.add_argument("--dir", required=True, help="Dossier de run (ex: runs/audi_a5_2010)")
    ap_sc.add_argument("--in", dest="in_path", help="(optionnel) input, sinon <dir>/vehicles.jsonl")
    ap_sc.add_argument("--artifacts", help="(optionnel) artefacts, sinon <dir>/model_artifacts")
    ap_sc.add_argument("--out", help="(optionnel) sortie, sinon <dir>/scored.csv")
    ap_sc.set_defaults(func=cmd_score)

    args = ap.parse_args()
    args.func(args)


from multiprocessing import freeze_support

if __name__ == "__main__":
    freeze_support()
    main()
