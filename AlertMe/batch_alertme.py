#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
batch_alertme.py
Lance en série plusieurs alertes (URL ↔ email) définies dans un fichier JSONL.

Usage:
  python batch_alertme.py --config alerts.jsonl [--default-pages 2] [--stop-on-error]

Notes:
- Réutilise le state JSON du script principal (data/state.json).
- Si une URL existe déjà avec un autre email, l'email est mis à jour (logué).
- Le script affiche un récapitulatif en fin d’exécution.
"""

import argparse, json, logging, os, sys
from typing import List, Tuple

# Import du script principal (il doit se trouver dans le même dossier)
import alertme_immoweb as core  # utilise run_once(url, email, pages), setup_logging()

def read_jsonl(path: str) -> List[dict]:
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if "url" not in obj or "email" not in obj:
                    logging.warning("Ligne %d: champs requis manquants (url/email) -> ignorée.", i)
                    continue
                items.append(obj)
            except json.JSONDecodeError as e:
                logging.error("Ligne %d: JSON invalide (%s) -> ignorée.", i, e)
    return items

def main():
    core.setup_logging()  # unifie le format des logs
    ap = argparse.ArgumentParser(description="Batch runner pour AlertMe Immoweb (URL ↔ email)")
    ap.add_argument("--config", required=True, help="Fichier JSONL contenant les alertes")
    ap.add_argument("--default-pages", type=int, default=2, help="Nombre de pages par défaut (si non spécifié par alerte)")
    ap.add_argument("--stop-on-error", action="store_true", help="Arrêter au premier échec")
    args = ap.parse_args()

    if not os.path.isfile(args.config):
        logging.error("Fichier de config introuvable: %s", args.config)
        sys.exit(1)

    alerts = read_jsonl(args.config)
    if not alerts:
        logging.warning("Aucune alerte valide dans %s.", args.config)
        sys.exit(0)

    total = len(alerts)
    ok = 0
    fail: List[Tuple[str,str]] = []

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
