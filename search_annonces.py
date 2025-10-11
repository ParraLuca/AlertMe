#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
2ememain – Collecteur de liens d'annonces (pagination /p/2/ en suivant 'Suivant')

Exemples d’URL de recherche:
  https://www.2ememain.be/l/autos/audi/f/a1/582/#f:10882|Language:all-languages|constructionYearFrom:2019
  https://www.2ememain.be/l/autos/volkswagen/f/caddy-maxi/10653/#f:10882|constructionYearFrom:2019

Usage:
  python search_annonces.py --url "https://www.2ememain.be/l/autos/audi/f/a1/582/#f:10882|constructionYearFrom:2019" --out urls.txt
  python search_annonces.py --brand audi --model a1 --year_from 2019 --out urls.txt
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from typing import Iterable, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

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
    """Renvoie toutes les URLs d’annonce détectées sur la page."""
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

def find_next_page_url(soup: BeautifulSoup) -> Optional[str]:
    """
    Cherche le lien de pagination 'Suivant' (ou 'Next' / 'Volgende'):
      - <a rel="next" ...>
      - <a aria-label="Suivant|Next|Volgende">
    Retourne l’URL absolue ou None.
    """
    # 1) rel="next"
    a = soup.find("a", attrs={"rel": "next"})
    if a and a.get("href"): 
        return urljoin(BASE, a["href"])

    # 2) aria-label
    for pat in (r"Suivant", r"Next", r"Volgende"):
        a = soup.find("a", attrs={"aria-label": re.compile(pat, re.I)})
        if a and a.get("href"):
            return urljoin(BASE, a["href"])

    # 3) bouton primaire "Suivant" sans rel (cas vu)
    cand = soup.select_one("a.hz-Link.hz-Link--isolated.hz-Button--primary")
    if cand and cand.get("href"):
        return urljoin(BASE, cand["href"])

    return None

def collect_all_links(start_url: str, max_pages: Optional[int] = None, sleep_s: float = 0.4) -> List[str]:
    """
    Parcourt la pagination en suivant le lien 'Suivant'.
    stop si:
      - pas de 'Suivant'
      - max_pages atteint (si fourni)
      - URL de page déjà vue
    """
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

# ---------- (optionnel) construction rapide d’URL sans ID ----------
def build_search_url(brand: str, model: Optional[str], year_from: Optional[int]) -> str:
    """
    Construit une URL de recherche générique SANS l’ID numérique.
    Ex: brand='audi', model='a1', year_from=2019 =>
        https://www.2ememain.be/l/autos/audi/f/a1/#f:Language:all-languages|constructionYearFrom:2019
    """
    brand = (brand or "").strip().lower()
    model = (model or "").strip().lower() if model else None
    path = f"{BASE}/l/autos/{brand}/"
    if model:
        path += f"f/{model}/"
    frag = "Language:all-languages"
    if year_from:
        frag += f"|constructionYearFrom:{year_from}"
    return path + f"#f:{frag}"

# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser(description="Collecteur d’URLs d’annonces 2ememain (pagination /p/N/)")
    ap.add_argument("--url", help="URL de recherche 2ememain (recommandé)")
    ap.add_argument("--brand", help="Marque (ex: audi)")
    ap.add_argument("--model", help="Modèle (ex: a1)")
    ap.add_argument("--year_from", type=int, help="Année min (ex: 2019)")
    ap.add_argument("--max_pages", type=int, help="Limite de pages à parcourir")
    ap.add_argument("--out", default="search_urls.txt", help="Fichier de sortie (1 URL par ligne)")
    args = ap.parse_args()

    if not args.url and not args.brand:
        ap.error("Fournis --url OU (au moins --brand). Exemple: --brand audi --model a1 --year_from 2019")

    start_url = args.url or build_search_url(args.brand, args.model, args.year_from)
    log(f"[start] {start_url}")

    links = collect_all_links(start_url, max_pages=args.max_pages)

    if not links:
        log("[info] aucun lien trouvé.")
        sys.exit(2)

    # Impression + écriture
    for u in links:
        print(u)
    with open(args.out, "w", encoding="utf-8") as f:
        for u in links:
            f.write(u + "\n")
    log(f"[ok] {len(links)} liens écrits dans {args.out}")

if __name__ == "__main__":
    main()
