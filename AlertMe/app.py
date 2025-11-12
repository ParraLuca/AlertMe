# streamlit_app.py
import os, json, re
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import streamlit as st

# ---------- Chargement config ----------
CONFIG_PATH = os.path.join(".", "config.json")
DEFAULT_CONFIG = {
    "alerts_path": "./alerts.jsonl",
    "max_alerts": 200,
    "dedupe_by_canonical_url": True,
    "ui": {
        "title": "AlertMe – Gestion des alertes Immoweb",
        "subtitle": "Ajoutez une alerte (URL + e-mail). Le moteur tourne en arrière-plan.",
        "show_labels": True
    },
    "scraper_defaults": {  # purement informatif côté UI
        "pages": 2,
        "order_keys": ["newest", "most_recent"],
        "path_aliases": ["/fr/recherche/", "/fr/recherche-avancee/"],
        "polite_sleep_seconds": 1.0,
        "use_selenium_fallback": False
    }
}

def load_config():
    if not os.path.isfile(CONFIG_PATH):
        return DEFAULT_CONFIG
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            # légère fusion au cas où des clés manquent
            def deep_merge(a, b):
                if isinstance(a, dict) and isinstance(b, dict):
                    out = dict(a)
                    for k, v in b.items():
                        out[k] = deep_merge(a.get(k), v) if k in a else v
                    return out
                return b if b is not None else a
            return deep_merge(DEFAULT_CONFIG, cfg)
    except Exception:
        return DEFAULT_CONFIG

CFG = load_config()
ALERTS_PATH = CFG["alerts_path"]
MAX_ALERTS = CFG["max_alerts"]
DEDUPE_CANON = bool(CFG.get("dedupe_by_canonical_url", True))
SHOW_LABELS = bool(CFG.get("ui", {}).get("show_labels", True))

# ---------- Utilitaires ----------
IMMOWEB_HOST = "www.immoweb.be"
ORDER_KEYS = CFG.get("scraper_defaults", {}).get("order_keys", ["newest", "most_recent"])

def canonicalize_immoweb_url(user_url: str) -> str:
    u = urlparse(user_url)
    if IMMOWEB_HOST not in u.netloc:
        raise ValueError("Ce n'est pas une URL Immoweb.")
    q = parse_qs(u.query)
    # force tri "plus récent" (newest prioritaire)
    q["orderBy"] = [ORDER_KEYS[0] if ORDER_KEYS else "newest"]
    # supprime la pagination pour stocker une clé canonique stable
    q.pop("page", None)
    new_q = urlencode({k: v[0] for k, v in q.items()})
    return urlunparse((u.scheme, u.netloc, u.path, u.params, new_q, u.fragment))

def is_valid_email(s: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", s.strip()))

def load_alerts(path: str):
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line: 
                continue
            try:
                obj = json.loads(line)
                # normalise le schéma
                url = obj.get("url", "").strip()
                email = obj.get("email", "").strip()
                label = obj.get("label", "").strip() if SHOW_LABELS else ""
                if url and email:
                    out.append({"url": url, "email": email, **({"label": label} if SHOW_LABELS else {})})
            except json.JSONDecodeError:
                # ignorer les lignes cassées
                pass
    return out

def save_alerts(path: str, alerts):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for a in alerts:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")
    os.replace(tmp, path)

def dedupe_alerts(alerts):
    """Déduplication par URL canonique (dernier gagnant)."""
    if not DEDUPE_CANON:
        return alerts
    seen = {}
    for a in alerts:
        try:
            key = canonicalize_immoweb_url(a["url"])
        except Exception:
            key = a["url"].strip()
        seen[key] = {"url": key, "email": a["email"].strip(), **({"label": a.get("label","").strip()} if SHOW_LABELS else {})}
    return list(seen.values())

# ---------- UI ----------
st.set_page_config(page_title=CFG["ui"]["title"], page_icon="🔔", layout="centered")
st.title("🔔 " + CFG["ui"]["title"])
st.caption(CFG["ui"]["subtitle"])

if "alerts" not in st.session_state:
    st.session_state.alerts = dedupe_alerts(load_alerts(ALERTS_PATH))

# Formulaire simple : URL + e-mail (+ label)
with st.form("add_alert_form", clear_on_submit=True):
    st.subheader("Ajouter une alerte")
    url_in = st.text_input("URL Immoweb (avec filtres)", placeholder="Collez l’URL depuis Immoweb…")
    email_in = st.text_input("Adresse e-mail", placeholder="ex: prenom.nom@gmail.com")
    label_in = st.text_input("Label (facultatif)", placeholder="ex: Brabant Wallon") if SHOW_LABELS else ""
    submitted = st.form_submit_button("Enregistrer")

    if submitted:
        if not url_in.strip():
            st.error("Merci de fournir une URL.")
        elif not email_in.strip() or not is_valid_email(email_in):
            st.error("Adresse e-mail invalide.")
        elif len(st.session_state.alerts) >= MAX_ALERTS:
            st.error(f"Nombre maximum d’alertes atteint ({MAX_ALERTS}).")
        else:
            try:
                canon = canonicalize_immoweb_url(url_in.strip())
                new_alert = {"url": canon, "email": email_in.strip(), **({"label": label_in.strip()} if SHOW_LABELS else {})}
                # si existe déjà -> mise à jour
                updated = False
                for i, a in enumerate(st.session_state.alerts):
                    try:
                        if canonicalize_immoweb_url(a["url"]) == canon:
                            st.session_state.alerts[i] = new_alert
                            updated = True
                            break
                    except Exception:
                        # si l'ancienne URL était non canonique, on compare brute
                        if a["url"].strip() == canon:
                            st.session_state.alerts[i] = new_alert
                            updated = True
                            break
                if not updated:
                    st.session_state.alerts.append(new_alert)

                st.session_state.alerts = dedupe_alerts(st.session_state.alerts)
                save_alerts(ALERTS_PATH, st.session_state.alerts)
                st.success("Alerte enregistrée ✅")
            except ValueError as e:
                st.error(str(e))
            except Exception as e:
                st.error(f"Erreur inattendue: {e}")

st.divider()

# Liste des alertes
st.subheader("Mes alertes")
if not st.session_state.alerts:
    st.info("Aucune alerte pour l’instant.")
else:
    for idx, a in enumerate(st.session_state.alerts):
        url = a["url"]
        email = a["email"]
        label = a.get("label","") if SHOW_LABELS else ""
        with st.container(border=True):
            if SHOW_LABELS and label:
                st.markdown(f"**Label :** {label}")
            st.markdown(f"**URL :** {url}")
            st.markdown(f"**Email :** {email}")
            cols = st.columns([1,1])
            with cols[0]:
                if st.button("✏️ Modifier", key=f"edit_{idx}"):
                    st.session_state[f"edit_mode_{idx}"] = True
            with cols[1]:
                if st.button("🗑️ Supprimer", key=f"del_{idx}"):
                    st.session_state.alerts = [x for j, x in enumerate(st.session_state.alerts) if j != idx]
                    st.session_state.alerts = dedupe_alerts(st.session_state.alerts)
                    save_alerts(ALERTS_PATH, st.session_state.alerts)
                    st.rerun()

            # Édition inline minimaliste
            if st.session_state.get(f"edit_mode_{idx}", False):
                with st.form(f"edit_form_{idx}"):
                    new_email = st.text_input("Email", value=email)
                    new_label = st.text_input("Label", value=label) if SHOW_LABELS else ""
                    new_url = st.text_input("URL Immoweb", value=url)
                    ok = st.form_submit_button("Sauvegarder")
                    if ok:
                        try:
                            if not is_valid_email(new_email):
                                st.warning("Email invalide.")
                            else:
                                canon2 = canonicalize_immoweb_url(new_url.strip())
                                st.session_state.alerts[idx] = {"url": canon2, "email": new_email.strip(), **({"label": new_label.strip()} if SHOW_LABELS else {})}
                                st.session_state.alerts = dedupe_alerts(st.session_state.alerts)
                                save_alerts(ALERTS_PATH, st.session_state.alerts)
                                st.session_state[f"edit_mode_{idx}"] = False
                                st.success("Alerte mise à jour ✅")
                                st.rerun()
                        except Exception as e:
                            st.error(f"Erreur: {e}")

st.divider()
with st.expander("ℹ️ Aide"):
    st.markdown("""
- Collez l’URL **Immoweb** avec vos filtres. Le système force automatiquement le tri **le plus récent** et supprime la pagination.
- Entrez l’**e-mail** destinataire.  
- Le fichier **alerts.jsonl** est mis à jour à chaque action (1 alerte = 1 ligne JSON).
- Le moteur batch utilisera `alerts.jsonl` pour exécuter toutes les alertes.
""")
