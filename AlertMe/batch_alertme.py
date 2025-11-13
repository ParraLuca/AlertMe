#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
batch_alertme.py
Exécute en série les alertes (Site ↔ URL ↔ Email) définies dans un JSONL (journal d'événements).

Compat:
- Ancien format: {"url","email","pages"?} -> site="immoweb" par défaut.
- Nouveau format: {"ts","action","alert":{"site","url","email","label"?, "pages"?, ...}}

Fonctionnement:
1) git pull (robuste, gère worktrees) pour être à jour.
2) lecture JSONL, replay du journal, dédup par (site, url canonique).
3) dispatch dynamique vers le module Python correspondant:
   - module = f"alertme_{site}". Exemple: site=immoweb -> alertme_immoweb.py
   - le module doit exposer: run_once(url:str, email:str, pages:int) et (optionnel) canonicalize_...()
Log complet du module choisi et des exécutions.
"""

import argparse, json, logging, os, sys, subprocess, importlib
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

def canonicalize(site: str, url: str) -> str:
    """Essaie d’appeler une fonction de canonicalisation spécifique au module du site, sinon fallback."""
    site = (site or "immoweb").strip().lower()
    try:
        mod = importlib.import_module(f"alertme_{site}")
    except Exception:
        return _fallback_canonicalize(url)

    # cherche une fonction candidate dans le module (nom souple)
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
    """Rejoue le journal -> état courant. Clé de dédup: (site, canon_url)."""
    state: Dict[str, dict] = {}

    for i, row in enumerate(lines, 1):
        if not isinstance(row, dict):
            logging.warning("Ligne %d: non-objet JSON -> ignorée.", i)
            continue

        # Ancien format
        if "action" not in row or "alert" not in row:
            site = (row.get("site") or "immoweb").strip().lower()
            url = (row.get("url") or "").strip()
            email = (row.get("email") or "").strip()
            if not url or not email:
                logging.warning("Ligne %d: ancien format sans url/email -> ignorée.", i)
                continue
            key_url = canonicalize(site, url)
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
        key_url = canonicalize(site, url) if url else ""

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
            state[key] = {"site": site, "url": key_url, "email": email, "pages": pages}
        elif action == "delete":
            if not key_url:
                logging.warning("Ligne %d: delete sans URL -> ignorée.", i)
                continue
            key = f"{site}|{key_url}"
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
        if site and url and email:
            out.append({"site": site, "url": url, "email": email, "pages": pages})
        else:
            logging.warning("Enregistrement incomplet -> ignoré: %r", it)
    return out

# ---------- Dispatch vers module par site ----------
def dispatch_run(site: str, url: str, email: str, pages: int):
    """Importe alertme_{site} et appelle run_once(). Logge le module choisi."""
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
    return run_fn(url, email, pages)

# ---------- Main ----------
def main():
    # logging de base
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")

    # 1) Git pull (robuste)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    git_pull_repo(script_dir)

    # 2) Args
    ap = argparse.ArgumentParser(description="Batch runner multi-sites (URL ↔ email)")
    ap.add_argument("--config", required=True, help="Fichier JSONL (ancien format OU journal d'événements)")
    ap.add_argument("--default-pages", type=int, default=2, help="Nombre de pages par défaut si non spécifié")
    ap.add_argument("--stop-on-error", action="store_true", help="Arrêter au premier échec")
    args = ap.parse_args()

    if not os.path.isfile(args.config):
        logging.error("Fichier de config introuvable: %s", args.config)
        sys.exit(1)

    # 3) Lecture + exécution
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
        logging.info("(%d/%d) site=%s | URL=%s | email=%s | pages=%d", idx, total, site, url, email, pages)
        try:
            dispatch_run(site, url, email, pages)
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
