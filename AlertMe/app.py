# streamlit_app.py
import os, json, re, base64, time, requests
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import streamlit as st

# ---------- Chargement config ----------
CONFIG_PATH = os.path.join(".", "config.json")
DEFAULT_CONFIG = {
    # Option B : même chemin en local que dans le repo
    "alerts_path": "./AlertMe/alerts.jsonl",
    "max_alerts": 200,
    "dedupe_by_canonical_url": True,
    "ui": {
        "title": "AlertMe – Gestion des alertes Immoweb",
        "subtitle": "Ajoutez une alerte (URL + e-mail). Les données sont stockées dans GitHub (fallback local en dev).",
        "show_labels": True
    },
    "scraper_defaults": {
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
    q["orderBy"] = [ORDER_KEYS[0] if ORDER_KEYS else "newest"]  # force tri le plus récent
    q.pop("page", None)  # URL canonique sans pagination
    new_q = urlencode({k: v[0] for k, v in q.items()})
    return urlunparse((u.scheme, u.netloc, u.path, u.params, new_q, u.fragment))

def is_valid_email(s: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", s.strip()))

# ---------- Stockage GitHub (prod) + fallback local ----------
def _gh_token():
    return st.secrets.get("GH_TOKEN") or os.getenv("GH_TOKEN")

def _gh_repo_cfg():
    repo   = st.secrets.get("GH_REPO", os.getenv("GH_REPO", "ParraLuca/AlertMe"))
    # Option B : fichier dans le dossier AlertMe du repo
    path   = st.secrets.get("GH_PATH", os.getenv("GH_PATH", "AlertMe/alerts.jsonl"))
    branch = st.secrets.get("GH_BRANCH", os.getenv("GH_BRANCH", "main"))
    return repo, path, branch

def _gh_headers():
    tok = _gh_token()
    if not tok:
        raise RuntimeError("GH_TOKEN manquant.")
    return {
        "Authorization": f"token {tok}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

def gh_get_file():
    repo, path, branch = _gh_repo_cfg()
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    r = requests.get(url, headers=_gh_headers(), params={"ref": branch})
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    data = r.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    sha = data["sha"]
    return content, sha

def gh_put_file(text: str, message: str):
    """Réécrit complètement le fichier sur GitHub (edit/suppression)."""
    repo, path, branch = _gh_repo_cfg()
    _, sha = gh_get_file()  # peut être None si première écriture
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    payload = {
        "message": message,
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha
    r = requests.put(url, headers=_gh_headers(), json=payload)
    r.raise_for_status()
    return r.json()

def gh_append_line(line_text: str, message: str):
    """Append 1 JSON line to alerts.jsonl sur GitHub, en préservant le contenu existant."""
    current, sha = gh_get_file()
    if current is None:  # fichier n'existe pas encore
        new_text = line_text + "\n"
        return gh_put_file(new_text, message)
    if not current.endswith("\n"):
        current += "\n"
    new_text = current + line_text + "\n"
    repo, path, branch = _gh_repo_cfg()
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    payload = {
        "message": message,
        "content": base64.b64encode(new_text.encode("utf-8")).decode("ascii"),
        "branch": branch,
        "sha": sha,
    }
    r = requests.put(url, headers=_gh_headers(), json=payload)
    r.raise_for_status()
    return r.json()

def load_alerts():
    """Lit alerts.jsonl depuis GitHub si GH_TOKEN est présent, sinon local."""
    if _gh_token():
        try:
            content, _ = gh_get_file()
            if not content:
                return []
            return [json.loads(l) for l in content.splitlines() if l.strip()]
        except Exception as e:
            st.error(f"Lecture GitHub échouée: {e}")
            return []
    # Fallback local
    if not os.path.isfile(ALERTS_PATH):
        return []
    out = []
    with open(ALERTS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                url = obj.get("url", "").strip()
                email = obj.get("email", "").strip()
                label = obj.get("label", "").strip() if SHOW_LABELS else ""
                if url and email:
                    out.append({"url": url, "email": email, **({"label": label} if SHOW_LABELS else {})})
            except json.JSONDecodeError:
                pass
    return out

def save_alerts(alerts, commit_message: str):
    """Réécrit toute la liste (utile pour edit/suppression)."""
    text = "\n".join(json.dumps(a, ensure_ascii=False) for a in alerts)
    if _gh_token():
        try:
            return gh_put_file(text + ("\n" if text else ""), commit_message)
        except Exception as e:
            st.error(f"Écriture GitHub échouée: {e}")
            return None
    # Fallback local
    os.makedirs(os.path.dirname(ALERTS_PATH) or ".", exist_ok=True)
    tmp = ALERTS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text + ("\n" if text else ""))
    os.replace(tmp, ALERTS_PATH)
    return True

def save_alert_append(alert: dict, commit_message: str):
    """Append une seule alerte (JSONL). Fallback local en append pur."""
    line_text = json.dumps(alert, ensure_ascii=False)
    if _gh_token():
        try:
            return gh_append_line(line_text, commit_message)
        except Exception as e:
            st.error(f"Append GitHub échoué: {e}")
            return None
    # local: append
    os.makedirs(os.path.dirname(ALERTS_PATH) or ".", exist_ok=True)
    with open(ALERTS_PATH, "a", encoding="utf-8") as f:
        f.write(line_text + "\n")
    return True

def dedupe_alerts(alerts):
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
    st.session_state.alerts = dedupe_alerts(load_alerts())

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

                # existe déjà ? -> update (réécriture complète)
                updated = False
                for i, a in enumerate(st.session_state.alerts):
                    try:
                        if canonicalize_immoweb_url(a["url"]) == canon:
                            st.session_state.alerts[i] = new_alert
                            updated = True
                            break
                    except Exception:
                        if a["url"].strip() == canon:
                            st.session_state.alerts[i] = new_alert
                            updated = True
                            break

                if updated:
                    st.session_state.alerts = dedupe_alerts(st.session_state.alerts)
                    save_alerts(st.session_state.alerts, "Update alert from Streamlit")
                else:
                    st.session_state.alerts.append(new_alert)
                    save_alert_append(new_alert, "Append alert from Streamlit")

                st.success("Alerte enregistrée ✅")
            except ValueError as e:
                st.error(str(e))
            except Exception as e:
                st.error(f"Erreur inattendue: {e}")

st.divider()

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
                    save_alerts(st.session_state.alerts, "Delete alert from Streamlit")
                    st.rerun()

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
                                save_alerts(st.session_state.alerts, "Edit alert from Streamlit")
                                st.session_state[f"edit_mode_{idx}"] = False
                                st.success("Alerte mise à jour ✅")
                                st.rerun()
                        except Exception as e:
                            st.error(f"Erreur: {e}")

st.divider()
with st.expander("ℹ️ Aide"):
    st.markdown("""
- Collez l’URL **Immoweb** avec vos filtres (le tri “plus récent” est forcé).
- Entrez l’**e-mail** destinataire.
- Les alertes sont **persistées dans GitHub** (`AlertMe/alerts.jsonl`) si un **GH_TOKEN** est configuré (avec Contents: Read & Write).
- En développement local (sans GH_TOKEN), le fichier `AlertMe/alerts.jsonl` est écrit **en local**.
""")
