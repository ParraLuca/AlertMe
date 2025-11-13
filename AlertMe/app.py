# streamlit_app.py
import os, json, re, base64, requests
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import streamlit as st

# ---------- Chargement config ----------
CONFIG_PATH = os.path.join(".", "config.json")
DEFAULT_CONFIG = {
    "alerts_path": "./AlertMe/alerts.jsonl",
    "max_alerts": 200,
    "dedupe_by_canonical_url": True,
    "ui": {
        "title": "AlertMe – Gestion des alertes multi-sites",
        "subtitle": "Ajoutez une alerte (Site + URL + e-mail). Les données sont stockées dans GitHub (fallback local en dev).",
        "show_labels": True
    },
    # Liste des sites disponibles (clé = identifiant; host sert à valider rapidement l'URL)
    "sites": [
        {"id": "immoweb", "label": "Immoweb", "host_contains": "immoweb.be"},
        { "id": "marjorietome", "label": "ImmoToma (Marjorie Toma)", "host_contains": "immotoma.be" }
    ],
    "scraper_defaults": {
        "pages": 2,
        "order_keys": ["newest", "most_recent"],
        "path_aliases": ["/fr/recherche/", "/fr/recherche-avancee/"],
        "polite_sleep_seconds": 1.0,
        "use_selenium_fallback": False
    }
}

# in streamlit_app.py

from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

def canonicalize_by_site(site_id: str, user_url: str) -> str:
    site_id = (site_id or "").strip().lower()
    if site_id == "immoweb":
        return canonicalize_immoweb_url(user_url)

    if site_id == "marjorietome":
        # canonicalize immotoma.be: drop 'paged', keep first values
        u = urlparse(user_url)
        q = parse_qs(u.query)
        q.pop("paged", None)
        new_q = urlencode({k: v[0] for k, v in q.items()})
        return urlunparse((u.scheme, u.netloc, u.path, u.params, new_q, u.fragment))

    # default fallback
    return canonicalize_generic_url(user_url)

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
SITES = CFG.get("sites", [])

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

def canonicalize_generic_url(user_url: str) -> str:
    """Fallback canonique simple: retire 'page' et normalise query en first-values."""
    u = urlparse(user_url)
    q = parse_qs(u.query)
    q.pop("page", None)
    new_q = urlencode({k: v[0] for k, v in q.items()})
    return urlunparse((u.scheme, u.netloc, u.path, u.params, new_q, u.fragment))

def canonicalize_by_site(site_id: str, user_url: str) -> str:
    site_id = (site_id or "").strip().lower()
    if site_id == "immoweb":
        return canonicalize_immoweb_url(user_url)
    # Ajoutez ici des branches spécifiques à d’autres sites si besoin
    return canonicalize_generic_url(user_url)

def host_ok_for_site(site_id: str, user_url: str) -> bool:
    """Validation souple : l'hôte doit contenir la chaîne configurée pour le site si disponible."""
    try:
        u = urlparse(user_url)
        host = u.netloc.lower()
    except Exception:
        return False
    for s in SITES:
        if s.get("id") == site_id:
            needle = (s.get("host_contains") or "").lower().strip()
            return (needle in host) if needle else True
    # site inconnu -> on accepte pour ne pas bloquer
    return True

def is_valid_email(s: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", s.strip()))

def utc_iso():
    return datetime.now(timezone.utc).isoformat()

# ---------- Stockage GitHub (prod) + fallback local ----------
def _gh_token():
    return st.secrets.get("GH_TOKEN") or os.getenv("GH_TOKEN")

def _gh_repo_cfg():
    repo   = st.secrets.get("GH_REPO", os.getenv("GH_REPO", "ParraLuca/AlertMe"))
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
    current, sha = gh_get_file()
    if current is None:
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

# ---------- Journal d'événements (append-only) ----------
def make_event(action: str, alert: dict) -> dict:
    assert action in {"add", "update", "delete"}
    ev = {"ts": utc_iso(), "action": action, "alert": {}}
    # champs toujours permis
    for key in ("site", "url", "email", "label", "pages"):
        if key in alert and alert[key] not in (None, ""):
            ev["alert"][key] = alert[key]
    return ev

def append_event(action: str, alert: dict, commit_message: str):
    ev = make_event(action, alert)
    line_text = json.dumps(ev, ensure_ascii=False)
    if _gh_token():
        try:
            return gh_append_line(line_text, commit_message)
        except Exception as e:
            st.error(f"Append GitHub échoué: {e}")
            return None
    os.makedirs(os.path.dirname(ALERTS_PATH) or ".", exist_ok=True)
    with open(ALERTS_PATH, "a", encoding="utf-8") as f:
        f.write(line_text + "\n")
    return True

def _reduce_events_to_state(lines: list[dict]) -> list[dict]:
    """Rejoue le journal -> état courant dédupliqué par (site, URL canonique).
       Compat : anciennes lignes (sans 'action' ni 'site') => site='immoweb' + add.
    """
    state: dict[str, dict] = {}

    for row in lines:
        if not isinstance(row, dict):
            continue

        # Ancien format (ligne = alerte brute)
        if "action" not in row or "alert" not in row:
            a = row
            site = (a.get("site") or "immoweb").strip().lower()
            url_in = (a.get("url", "") or "").strip()
            if not url_in:
                continue
            try:
                canon = canonicalize_by_site(site, url_in)
            except Exception:
                canon = url_in
            key = f"{site}|{canon}"
            state[key] = {
                "site": site,
                "url": canon,
                "email": (a.get("email", "") or "").strip(),
                **({"label": (a.get("label", "") or "").strip()} if SHOW_LABELS else {})
            }
            continue

        # Nouveau format
        action = (row.get("action") or "").strip().lower()
        a = row.get("alert", {}) or {}
        site = (a.get("site") or "immoweb").strip().lower()
        url_in = (a.get("url", "") or "").strip()

        if action in {"add", "update"}:
            if not url_in:
                continue
            try:
                canon = canonicalize_by_site(site, url_in)
            except Exception:
                canon = url_in
            key = f"{site}|{canon}"
            state[key] = {
                "site": site,
                "url": canon,
                "email": (a.get("email", "") or "").strip(),
                **({"label": (a.get("label", "") or "").strip()} if SHOW_LABELS else {})
            }
        elif action == "delete":
            if not url_in:
                continue
            try:
                canon = canonicalize_by_site(site, url_in)
            except Exception:
                canon = url_in
            key = f"{site}|{canon}"
            state.pop(key, None)

    return list(state.values())

def load_alerts():
    raw_lines = []
    if _gh_token():
        try:
            content, _ = gh_get_file()
            if content:
                for l in content.splitlines():
                    l = l.strip()
                    if l:
                        try:
                            raw_lines.append(json.loads(l))
                        except json.JSONDecodeError:
                            pass
            return _reduce_events_to_state(raw_lines)
        except Exception as e:
            st.error(f"Lecture GitHub échouée: {e}")
            return []
    if not os.path.isfile(ALERTS_PATH):
        return []
    with open(ALERTS_PATH, "r", encoding="utf-8") as f:
        for l in f:
            l = l.strip()
            if l:
                try:
                    raw_lines.append(json.loads(l))
                except json.JSONDecodeError:
                    pass
    return _reduce_events_to_state(raw_lines)

# ---------- UI ----------
st.set_page_config(page_title=CFG["ui"]["title"], page_icon="🔔", layout="centered")
st.title("🔔 " + CFG["ui"]["title"])
st.caption(CFG["ui"]["subtitle"])

if "alerts" not in st.session_state:
    st.session_state.alerts = load_alerts()

with st.form("add_alert_form", clear_on_submit=True):
    st.subheader("Ajouter une alerte")
    # Sélecteur de site
    site_labels = [s["label"] for s in SITES] if SITES else ["Immoweb"]
    site_ids = [s["id"] for s in SITES] if SITES else ["immoweb"]
    site_idx = st.selectbox("Site", options=list(range(len(site_labels))), format_func=lambda i: site_labels[i], index=0)
    chosen_site = site_ids[site_idx]

    url_in = st.text_input("URL du site choisi (avec filtres)", placeholder="Collez l’URL…")
    email_in = st.text_input("Adresse e-mail", placeholder="ex: prenom.nom@gmail.com")
    label_in = st.text_input("Label (facultatif)", placeholder="ex: Brabant Wallon") if SHOW_LABELS else ""
    submitted = st.form_submit_button("Enregistrer")

    if submitted:
        if not url_in.strip():
            st.error("Merci de fournir une URL.")
        elif not host_ok_for_site(chosen_site, url_in.strip()):
            st.error("L’URL ne correspond pas au site sélectionné.")
        elif not email_in.strip() or not is_valid_email(email_in):
            st.error("Adresse e-mail invalide.")
        else:
            try:
                canon = canonicalize_by_site(chosen_site, url_in.strip())
                new_alert = {
                    "site": chosen_site,
                    "url": canon,
                    "email": email_in.strip(),
                    **({"label": label_in.strip()} if SHOW_LABELS else {})
                }

                # Existe déjà ? clé = (site, canon)
                key = f"{chosen_site}|{canon}"
                exists_idx = next((i for i, a in enumerate(st.session_state.alerts) if f"{a.get('site','immoweb')}|{a.get('url','')}" == key), None)

                if exists_idx is None and len(st.session_state.alerts) >= MAX_ALERTS:
                    st.error(f"Nombre maximum d’alertes atteint ({MAX_ALERTS}).")
                else:
                    if exists_idx is not None:
                        st.session_state.alerts[exists_idx] = new_alert
                        append_event("update", new_alert, "Update alert from Streamlit")
                    else:
                        st.session_state.alerts.append(new_alert)
                        append_event("add", new_alert, "Add alert from Streamlit")
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
        site = a.get("site", "immoweb")
        url = a.get("url", "")
        email = a.get("email", "")
        label = a.get("label", "") if SHOW_LABELS else ""
        with st.container(border=True):
            st.markdown(f"**Site :** `{site}`")
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
                    append_event("delete", {"site": site, "url": url}, "Delete alert from Streamlit")
                    st.session_state.alerts = [x for j, x in enumerate(st.session_state.alerts) if j != idx]
                    st.rerun()

            if st.session_state.get(f"edit_mode_{idx}", False):
                with st.form(f"edit_form_{idx}"):
                    # site non modifiable ici pour éviter des collisions; créer une nouvelle alerte sinon.
                    st.markdown(f"_Le site n’est pas modifiable. Supprimez puis recréez pour changer de site._")
                    new_email = st.text_input("Email", value=email)
                    new_label = st.text_input("Label", value=label) if SHOW_LABELS else ""
                    new_url = st.text_input("URL", value=url)
                    ok = st.form_submit_button("Sauvegarder")
                    if ok:
                        try:
                            if not is_valid_email(new_email):
                                st.warning("Email invalide.")
                            elif not host_ok_for_site(site, new_url.strip()):
                                st.warning("URL incohérente avec le site.")
                            else:
                                canon2 = canonicalize_by_site(site, new_url.strip())
                                edited = {"site": site, "url": canon2, "email": new_email.strip(), **({"label": new_label.strip()} if SHOW_LABELS else {})}
                                st.session_state.alerts[idx] = edited
                                append_event("update", edited, "Inline edit alert from Streamlit")
                                st.session_state[f"edit_mode_{idx}"] = False
                                st.success("Alerte mise à jour ✅")
                                st.rerun()
                        except Exception as e:
                            st.error(f"Erreur: {e}")

st.divider()
with st.expander("ℹ️ Aide"):
    st.markdown("""
- Sélectionnez le **site**, puis collez l’**URL** avec vos filtres (le tri “plus récent” est forcé quand applicable).
- Entrez l’**e-mail** destinataire.
- Les alertes sont **persistées dans GitHub** (`AlertMe/alerts.jsonl`) si un **GH_TOKEN** est configuré (Contents: Read & Write).
- Sans GH_TOKEN, le fichier `AlertMe/alerts.jsonl` est maintenu **en local**.
- Le fichier est un **journal d’événements** (append-only) : chaque ajout, modification ou suppression écrit **une ligne JSON** avec le **site**.
""")
