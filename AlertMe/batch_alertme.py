#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
batch_alertme.py
Exécute en série les alertes (Site ↔ URL ↔ Email) définies dans un JSONL (journal d'événements).

Adaptations:
- Support explicite d'AD-HOME (mêmes facilités qu'Immo-KH) :
  * URL vide autorisée → on force l'URL liste canonique via alertme_adhome.canonicalize_list_url(None)
  * Canonicalisation site-spécifique si disponible dans le module alertme_{site}
"""

import argparse, json, logging, os, sys, subprocess, importlib, inspect
from typing import Dict, List, Tuple, Optional

# ---------- Git sync (robuste) ----------
def git_find_toplevel(start_dir: str) -> Optional[str]:
    try:
        cp = subprocess.run(
            ["git", "-C", start_dir, "rev-parse", "--show-toplevel"],
            check=True, capture_output=True, text=True
        )
        root = (cp.stdout or "").strip()
        return root or None
    except Exception:
        return None

def git_pull_repo(start_dir: str) -> None:
    top = git_find_toplevel(start_dir)
    if not top:
        logging.info("Git: repo non détecté (ni .git dir, ni rev-parse). Skip pull.")
        return
    try:
        logging.info("Git: pull début sur %s ...", top)
        subprocess.run(["git", "-C", top, "fetch", "--all"], check=True)
        subprocess.run(["git", "-C", top, "pull", "--ff-only"], check=True)
        logging.info("Git: pull ok.")
    except FileNotFoundError:
        logging.warning("Git: binaire 'git' introuvable dans le PATH. Skip pull.")
    except subprocess.CalledProcessError as e:
        logging.error("Git: pull a échoué (code %s). On continue avec l'état local.", e.returncode)

# ---------- Canonicalisation URL ----------
SITES_ALLOW_EMPTY_URL = {"immokh", "adhome"}  # sites où on accepte URL vide et on force l’URL liste

def _fallback_canonicalize(url: str) -> str:
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
    url = (url or "").strip()
    if not url:
        return url
    try:
        u = urlparse(url)
        q = parse_qs(u.query)
        q.pop("page", None)
        new_q = urlencode({k: v[0] for k, v in q.items()})
        return urlunparse((u.scheme, u.netloc, u.path, u.params, new_q, u.fragment))
    except Exception:
        return url

def _canon_site_if_empty(site: str, url: str) -> str:
    """
    Pour les sites listés dans SITES_ALLOW_EMPTY_URL, si URL vide → on appelle canonicalize_list_url(None)
    du module alertme_{site}, sinon on renvoie l'URL telle quelle.
    """
    if (site or "").lower().strip() not in SITES_ALLOW_EMPTY_URL or (url or "").strip():
        return url
    try:
        mod = importlib.import_module(f"alertme_{site}")
        return getattr(mod, "canonicalize_list_url")(None)
    except Exception:
        # garde-fous explicites pour immokh/adhome si le module n'est pas importable
        if site == "immokh":
            return "https://www.immo-kh.be/fr/2/chercher-bien/a-vendre"
        if site == "adhome":
            return "https://www.ad-home.be/fr/2/chercher-bien/a-vendre"
        return url

def canonicalize(site: str, url: str) -> str:
    """
    Essaie d’appeler une fonction de canonicalisation spécifique au module du site, sinon fallback.
    Sites autorisant URL vide → on remplit d’abord via _canon_site_if_empty().
    """
    site = (site or "immoweb").strip().lower()
    url = _canon_site_if_empty(site, url)

    try:
        mod = importlib.import_module(f"alertme_{site}")
    except Exception:
        return _fallback_canonicalize(url)

    # Cherche une fonction de canonicalisation explicite dans le module (ex: canonicalize_list_url)
    for name in dir(mod):
        if name.startswith("canonicalize") and "url" in name:
            fn = getattr(mod, name)
            if callable(fn):
                try:
                    return fn(url)
                except Exception:
                    break
    return _fallback_canonicalize(url)

# ---------- Lecture & réduction JSONL ----------
def _reduce_events_to_items(lines: List[dict], default_pages: int) -> List[dict]:
    """
    Rejoue le journal -> état courant.
    Clé de dédup: (site, canon_url) + éventuellement filters_JSON.
    Pour sites 'immokh' et 'adhome', l’URL peut être vide → on injecte l’URL canonique.
    """
    state: Dict[str, dict] = {}

    for i, row in enumerate(lines, 1):
        if not isinstance(row, dict):
            logging.warning("Ligne %d: non-objet JSON -> ignorée.", i)
            continue

        # Ancien format (sans filtres)
        if "action" not in row or "alert" not in row:
            site = (row.get("site") or "immoweb").strip().lower()
            url = (row.get("url") or "").strip()
            email = (row.get("email") or "").strip()
            if not email:
                logging.warning("Ligne %d: ancien format sans email -> ignorée.", i)
                continue
            # autorise URL vide pour sites spéciaux
            url = _canon_site_if_empty(site, url)
            key_url = canonicalize(site, url) if url else ""
            if not key_url:
                logging.warning("Ligne %d: ancien format sans URL exploitable -> ignorée.", i)
                continue
            pages = int(row.get("pages", default_pages) or default_pages)
            key = f"{site}|{key_url}"
            state[key] = {"site": site, "url": key_url, "email": email, "pages": pages}
            continue

        # Nouveau format
        action = (row.get("action") or "").strip().lower()
        alert = row.get("alert") or {}
        if action not in {"add", "update", "delete"}:
            logging.warning("Ligne %d: action inconnue '%s' -> ignorée.", i, action)
            continue

        site = (alert.get("site") or "immoweb").strip().lower()
        url = (alert.get("url") or "").strip()
        # autorise URL vide pour sites spéciaux
        url = _canon_site_if_empty(site, url)
        key_url = canonicalize(site, url) if url else ""

        filters = alert.get("filters") or {}
        use_browser = alert.get("use_browser")
        filters_json_key = json.dumps(filters, sort_keys=True, ensure_ascii=False) if filters else ""

        if action in {"add", "update"}:
            if not key_url:
                logging.warning("Ligne %d: %s sans URL -> ignorée.", i, action)
                continue
            email = (alert.get("email") or "").strip()
            if not email:
                logging.warning("Ligne %d: %s sans email -> ignorée.", i, action)
                continue
            pages = int(alert.get("pages", default_pages) or default_pages)

            key = f"{site}|{key_url}"
            if filters_json_key:
                key += f"|{filters_json_key}"

            item = {"site": site, "url": key_url, "email": email, "pages": pages}
            if filters_json_key:
                item["filters"] = filters
            if use_browser is not None:
                item["use_browser"] = bool(use_browser)
            state[key] = item

        elif action == "delete":
            if not key_url:
                logging.warning("Ligne %d: delete sans URL -> ignorée.", i)
                continue
            key = f"{site}|{key_url}"
            if filters_json_key:
                key += f"|{filters_json_key}"
            state.pop(key, None)

    return list(state.values())

def read_jsonl_effective_items(path: str, default_pages: int) -> List[dict]:
    raw: List[dict] = []
    try:
        size = os.path.getsize(path)
        logging.info("Lecture '%s' (taille %d octets)...", path, size)
    except OSError:
        logging.info("Lecture '%s'...", path)

    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                raw.append(json.loads(line))
            except json.JSONDecodeError as e:
                logging.error("Ligne %d: JSON invalide (%s) -> ignorée.", i, e)

    logging.info("Lignes JSON valides lues: %d", len(raw))
    items = _reduce_events_to_items(raw, default_pages)
    logging.info("Alertes effectives après réduction: %d", len(items))

    out: List[dict] = []
    for it in items:
        site = (it.get("site") or "immoweb").strip().lower()
        url = (it.get("url") or "").strip()
        email = (it.get("email") or "").strip()
        pages = int(it.get("pages", default_pages) or default_pages)
        filters = it.get("filters")
        use_browser = it.get("use_browser")

        # autorise URL vide pour les sites spéciaux
        url = _canon_site_if_empty(site, url)

        if site and url and email:
            rec = {"site": site, "url": url, "email": email, "pages": pages}
            if filters is not None:
                rec["filters"] = filters
            if use_browser is not None:
                rec["use_browser"] = bool(use_browser)
            out.append(rec)
        else:
            logging.warning("Enregistrement incomplet -> ignoré: %r", it)
    return out

# ---------- Dispatch ----------
def dispatch_run(site: str, url: str, email: str, pages: int, **extra):
    site = (site or "immoweb").strip().lower()
    mod_name = f"alertme_{site}"
    try:
        mod = importlib.import_module(mod_name)
    except Exception as e:
        logging.error("Site '%s' non supporté: import '%s' impossible (%s).", site, mod_name, e)
        raise

    run_fn = getattr(mod, "run_once", None)
    if not callable(run_fn):
        raise RuntimeError(f"Le module {mod_name} n’expose pas run_once(url,email,pages).")

    logging.info("Dispatch: site=%s -> module=%s.run_once", site, mod_name)

    # on ne passe au module que les paramètres qu’il accepte
    sig = inspect.signature(run_fn)
    kwargs = {k: v for k, v in extra.items() if k in sig.parameters}
    return run_fn(url, email, pages, **kwargs)

# ---------- Main ----------
def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
    script_dir = os.path.dirname(os.path.abspath(__file__))
    git_pull_repo(script_dir)

    ap = argparse.ArgumentParser(description="Batch runner multi-sites (URL ↔ email)")
    ap.add_argument("--config", required=True, help="Fichier JSONL (ancien format OU journal d'événements)")
    ap.add_argument("--default-pages", type=int, default=2, help="Nombre de pages par défaut si non spécifié")
    ap.add_argument("--stop-on-error", action="store_true", help="Arrêter au premier échec")
    args = ap.parse_args()

    if not os.path.isfile(args.config):
        logging.error("Fichier de config introuvable: %s", args.config)
        sys.exit(1)

    alerts = read_jsonl_effective_items(args.config, args.default_pages)
    if not alerts:
        logging.warning("Aucune alerte exploitable dans %s.", args.config)
        sys.exit(0)

    total = len(alerts)
    ok = 0
    fail: List[Tuple[str, str, str]] = []

    logging.info("=== Démarrage batch: %d alerte(s) ===", total)
    for idx, a in enumerate(alerts, 1):
        site = a["site"]
        url = a["url"]
        email = a["email"]
        pages = int(a.get("pages", args.default_pages))
        filters = a.get("filters")
        use_browser = a.get("use_browser")

        logging.info("(%d/%d) site=%s | URL=%s | email=%s | pages=%d", idx, total, site, url, email, pages)
        try:
            dispatch_run(site, url, email, pages, filters=filters, use_browser=use_browser)
            ok += 1
        except Exception as e:
            logging.exception("Échec sur (site=%s, %s -> %s): %s", site, url, email, e)
            fail.append((site, url, email))
            if args.stop_on_error:
                break

    logging.info("=== Fin batch ===")
    logging.info("Succès: %d / %d", ok, total)
    if fail:
        logging.warning("Échecs (%d):", len(fail))
        for s, u, m in fail:
            logging.warning(" - site=%s | %s -> %s", s, u, m)

if __name__ == "__main__":
    main()
