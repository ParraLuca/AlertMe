#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
batch_alertme.py
Lance en série plusieurs alertes (URL ↔ email) définies dans un fichier JSONL.

Compatibilité:
- Ancien format: 1 ligne = {"url": "...", "email": "...", "pages": 2}
- Nouveau format (journal d'événements append-only):
    {"ts": "...", "action": "add|update|delete", "alert": {"url": "...", "email": "...", "label": "...", "pages": 2}}

Le script fait un 'git pull' au démarrage (détection robuste du repo via git rev-parse),
puis lit le JSONL et exécute core.run_once().
"""

import argparse, json, logging, os, sys, subprocess
from typing import Dict, List, Tuple

# Import du cœur métier (doit fournir run_once(url, email, pages), setup_logging())
import alertme_immoweb as core  # type: ignore

# ---------------------- Git sync (robuste) ----------------------
def git_find_toplevel(start_dir: str) -> str | None:
    """Retourne le toplevel git en utilisant 'git rev-parse --show-toplevel', sinon None."""
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
    """Fait fetch + pull --ff-only sur le toplevel git si détecté. Ne plante pas si git absent."""
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

# ---------------------- Canonicalisation URL ----------------------
def _fallback_canonicalize(url: str) -> str:
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
    url = (url or "").strip()
    if not url:
        return url
    try:
        u = urlparse(url)
        q = parse_qs(u.query)
        q.pop("page", None)
        if "orderBy" in q:
            q["orderBy"] = [q["orderBy"][0] or "newest"]
        new_q = urlencode({k: v[0] for k, v in q.items()})
        return urlunparse((u.scheme, u.netloc, u.path, u.params, new_q, u.fragment))
    except Exception:
        return url

def canonicalize(url: str) -> str:
    fn = getattr(core, "canonicalize_immoweb_url", None)
    if callable(fn):
        try:
            return fn(url)
        except Exception:
            return _fallback_canonicalize(url)
    return _fallback_canonicalize(url)

# ---------------------- Lecture & réduction du JSONL ----------------------
def _reduce_events_to_items(lines: List[dict], default_pages: int) -> List[dict]:
    state: Dict[str, dict] = {}

    for i, row in enumerate(lines, 1):
        if not isinstance(row, dict):
            logging.warning("Ligne %d: entrée non-JSON objet -> ignorée.", i)
            continue

        # Ancien format
        if "action" not in row or "alert" not in row:
            url = (row.get("url") or "").strip()
            email = (row.get("email") or "").strip()
            if not url or not email:
                logging.warning("Ligne %d: ancien format sans url/email -> ignorée.", i)
                continue
            key = canonicalize(url)
            pages = int(row.get("pages", default_pages) or default_pages)
            state[key] = {"url": key, "email": email, "pages": pages}
            continue

        # Nouveau format (événements)
        action = (row.get("action") or "").strip().lower()
        alert = row.get("alert") or {}
        if action not in {"add", "update", "delete"}:
            logging.warning("Ligne %d: action inconnue '%s' -> ignorée.", i, action)
            continue

        url = (alert.get("url") or "").strip()
        key = canonicalize(url) if url else ""
        if action in {"add", "update"}:
            if not key:
                logging.warning("Ligne %d: %s sans URL -> ignorée.", i, action)
                continue
            email = (alert.get("email") or "").strip()
            if not email:
                logging.warning("Ligne %d: %s sans email -> ignorée.", i, action)
                continue
            pages = int(alert.get("pages", default_pages) or default_pages)
            state[key] = {"url": key, "email": email, "pages": pages}
        elif action == "delete":
            if key:
                state.pop(key, None)
            else:
                logging.warning("Ligne %d: delete sans URL -> ignorée.", i)

    return list(state.values())

def read_jsonl_effective_items(path: str, default_pages: int) -> List[dict]:
    raw: List[dict] = []
    # log pour diagnostiquer “rien dans le jsonl”
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
        url = (it.get("url") or "").strip()
        email = (it.get("email") or "").strip()
        pages = int(it.get("pages", default_pages) or default_pages)
        if url and email:
            out.append({"url": url, "email": email, "pages": pages})
        else:
            logging.warning("Enregistrement incomplet (url/email manquant) -> ignoré: %r", it)
    return out

# ---------------------- Main ----------------------
def main():
    if hasattr(core, "setup_logging"):
        core.setup_logging()
    else:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")

    # 1) Git pull au démarrage (robuste, gère worktrees et sous-dossiers)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    git_pull_repo(script_dir)

    # 2) Args
    ap = argparse.ArgumentParser(description="Batch runner pour AlertMe Immoweb (URL ↔ email)")
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
    fail: List[Tuple[str, str]] = []

    logging.info("=== Démarrage batch: %d alerte(s) ===", total)
    for idx, a in enumerate(alerts, 1):
        url = a["url"].strip()
        email = a["email"].strip()
        pages = int(a.get("pages", args.default_pages))
        logging.info("(%d/%d) URL=%s | email=%s | pages=%d", idx, total, url, email, pages)
        try:
            core.run_once(url, email, pages)
            ok += 1
        except Exception as e:
            logging.exception("Échec sur (%s -> %s): %s", url, email, e)
            fail.append((url, email))
            if args.stop_on_error:
                break

    logging.info("=== Fin batch ===")
    logging.info("Succès: %d / %d", ok, total)
    if fail:
        logging.warning("Échecs (%d):", len(fail))
        for u, m in fail:
            logging.warning(" - %s -> %s", u, m)

if __name__ == "__main__":
    main()
