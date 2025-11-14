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
    "sites": [
        {"id": "immoweb",      "label": "Immoweb",                  "host_contains": "immoweb.be"},
        {"id": "marjorietome", "label": "ImmoToma (Marjorie Toma)", "host_contains": "immotoma.be"},
        {"id": "immokh",       "label": "Immo-KH",                  "host_contains": "immo-kh.be"}
    ],
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
SITES = CFG.get("sites", [])

# ---------- Utilitaires ----------
IMMOWEB_HOST = "www.immoweb.be"
ORDER_KEYS = CFG.get("scraper_defaults", {}).get("order_keys", ["newest", "most_recent"])

def canonicalize_immoweb_url(user_url: str) -> str:
    u = urlparse(user_url)
    if IMMOWEB_HOST not in u.netloc:
        raise ValueError("Ce n'est pas une URL Immoweb.")
    q = parse_qs(u.query)
    q["orderBy"] = [ORDER_KEYS[0] if ORDER_KEYS else "newest"]  # force tri récent
    q.pop("page", None)  # URL canonique sans pagination
    new_q = urlencode({k: v[0] for k, v in q.items()})
    return urlunparse((u.scheme, u.netloc, u.path, u.params, new_q, u.fragment))

def canonicalize_marjorietome_url(user_url: str) -> str:
    u = urlparse(user_url)
    q = parse_qs(u.query)
    q.pop("paged", None)
    new_q = urlencode({k: v[0] for k, v in q.items()})
    return urlunparse((u.scheme, u.netloc, u.path, u.params, new_q, u.fragment))

def canonicalize_generic_url(user_url: str) -> str:
    u = urlparse(user_url or "")
    q = parse_qs(u.query)
    for k in ("page", "paged"):
        q.pop(k, None)
    new_q = urlencode({k: (v[0] if isinstance(v, list) and v else v) for k, v in q.items()})
    return urlunparse((u.scheme or "https", u.netloc, u.path, u.params, new_q, u.fragment))

def canonicalize_by_site(site_id: str, user_url: str) -> str:
    site_id = (site_id or "").strip().lower()
    if site_id == "immoweb":
        return canonicalize_immoweb_url(user_url)
    if site_id == "marjorietome":
        return canonicalize_marjorietome_url(user_url)
    # immokh et autres: fallback générique
    return canonicalize_generic_url(user_url)

def host_ok_for_site(site_id: str, user_url: str) -> bool:
    # Pour Immo-KH on autorise URL vide (elle sera forcée côté batch/immokh)
    if (site_id or "").lower().strip() == "immokh" and not (user_url or "").strip():
        return True
    try:
        u = urlparse(user_url)
        host = (u.netloc or "").lower()
    except Exception:
        return False
    for s in SITES:
        if s.get("id") == site_id:
            needle = (s.get("host_contains") or "").lower().strip()
            return (needle in host) if needle else True
    return True

def is_valid_email(s: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", s.strip()))

def utc_iso():
    return datetime.now(timezone.utc).isoformat()

def filters_summary_str(filters: dict | None) -> str:
    if not filters: return "—"
    parts = []
    if filters.get("property_types"):
        parts.append("Types: " + ", ".join(filters["property_types"]))
    if filters.get("cities"):
        parts.append("Villes: " + ", ".join(filters["cities"]))
    if filters.get("price_min") is not None or filters.get("price_max") is not None:
        parts.append(f"Prix: {filters.get('price_min','—')}→{filters.get('price_max','—')}")
    if filters.get("bedrooms_min") is not None:
        parts.append(f"≥{filters['bedrooms_min']} ch.")
    if filters.get("bathrooms_min") is not None:
        parts.append(f"≥{filters['bathrooms_min']} sdb")
    if filters.get("area_min") is not None:
        parts.append(f"≥{filters['area_min']} m²")
    if filters.get("include_sold"):
        parts.append("incl. vendus")
    return " · ".join(parts) if parts else "—"

# ---------- Secrets sûrs (GitHub) ----------
def _safe_secret(key: str) -> str | None:
    # Évite l'exception StreamlitSecretNotFoundError si aucun secrets.toml
    try:
        return st.secrets.get(key)  # type: ignore[attr-defined]
    except Exception:
        return None

def _gh_token():
    return _safe_secret("GH_TOKEN") or os.getenv("GH_TOKEN")

def _gh_repo_cfg():
    repo   = _safe_secret("GH_REPO")  or os.getenv("GH_REPO",  "ParraLuca/AlertMe")
    path   = _safe_secret("GH_PATH")  or os.getenv("GH_PATH",  "AlertMe/alerts.jsonl")
    branch = _safe_secret("GH_BRANCH") or os.getenv("GH_BRANCH", "main")
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
    _, sha = gh_get_file()
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

# ---------- Journal d'événements ----------
def make_event(action: str, alert: dict) -> dict:
    assert action in {"add", "update", "delete"}
    ev = {"ts": utc_iso(), "action": action, "alert": {}}
    for key in ("site", "url", "email", "label", "pages", "filters"):
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
    """Rejoue le journal -> état courant dédupliqué.
       Clé de dédup:
       - par défaut: (site, URL canonique)
       - immokh avec 'filters': (site, URL canonique, json.dumps(filters, sort_keys=True))
    """
    state: dict[str, dict] = {}

    for row in lines:
        if not isinstance(row, dict):
            continue

        # Ancien format
        if "action" not in row or "alert" not in row:
            a = row
            site = (a.get("site") or "immoweb").strip().lower()
            url_in = (a.get("url","") or "").strip()
            # Immo-KH: URL vide autorisée → forcée à la liste canonique
            if site == "immokh" and not url_in:
                url_in = "https://www.immo-kh.be/fr/2/chercher-bien/a-vendre"
            try:
                canon = canonicalize_by_site(site, url_in)
            except Exception:
                canon = url_in
            key = f"{site}|{canon}"
            state[key] = {
                "site": site,
                "url": canon,
                "email": (a.get("email","") or "").strip(),
                **({"label": (a.get("label","") or "").strip()} if SHOW_LABELS else {})
            }
            continue

        # Nouveau format
        action = (row.get("action") or "").strip().lower()
        a = row.get("alert", {}) or {}
        site = (a.get("site") or "immoweb").strip().lower()
        url_in = (a.get("url","") or "").strip()
        filters = a.get("filters")
        if site == "immokh" and not url_in:
            url_in = "https://www.immo-kh.be/fr/2/chercher-bien/a-vendre"

        filt_key = json.dumps(filters, sort_keys=True, ensure_ascii=False) if filters else ""

        if action in {"add", "update"}:
            try:
                canon = canonicalize_by_site(site, url_in)
            except Exception:
                canon = url_in
            key = f"{site}|{canon}"
            if filt_key:
                key += f"|{filt_key}"
            rec = {
                "site": site,
                "url": canon,
                "email": (a.get("email","") or "").strip(),
                **({"label": (a.get("label","") or "").strip()} if SHOW_LABELS else {})
            }
            if filters is not None:
                rec["filters"] = filters
            state[key] = rec

        elif action == "delete":
            try:
                canon = canonicalize_by_site(site, url_in) if url_in else url_in
            except Exception:
                canon = url_in
            if site == "immokh" and not canon:
                canon = "https://www.immo-kh.be/fr/2/chercher-bien/a-vendre"
            key = f"{site}|{canon}"
            if filt_key:
                key += f"|{filt_key}"
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
    site_labels = [s["label"] for s in SITES] if SITES else ["Immoweb"]
    site_ids = [s["id"] for s in SITES] if SITES else ["immoweb"]
    site_idx = st.selectbox("Site", options=list(range(len(site_labels))), format_func=lambda i: site_labels[i], index=0)
    chosen_site = site_ids[site_idx]

    # Pour Immo-KH, l’URL est optionnelle (toujours la même liste)
    url_placeholder = ("(optionnel) Laisser vide pour la liste par défaut Immo-KH"
                       if chosen_site == "immokh" else "Collez l’URL…")
    url_in = st.text_input("URL du site choisi", placeholder=url_placeholder)
    email_in = st.text_input("Adresse e-mail", placeholder="ex: prenom.nom@gmail.com")
    label_in = st.text_input("Label (facultatif)", placeholder="ex: Brabant Wallon") if SHOW_LABELS else ""

    # ----- Filtres spécifiques Immo-KH -----
    filters_payload = None
    if chosen_site == "immokh":
        st.markdown("**Filtres (Immo-KH)**")
        col1, col2 = st.columns(2)
        with col1:
            cities_txt = st.text_input("Villes (séparées par des virgules)", placeholder="ex: Fosses-la-Ville, Jemeppe-sur-Sambre")
            price_min = st.number_input("Prix min (€)", min_value=0, step=1000, value=0)
            bedrooms_min = st.number_input("Chambres min", min_value=0, step=1, value=0)
            area_min = st.number_input("Surface min (m²)", min_value=0, step=5, value=0)
        with col2:
            price_max = st.number_input("Prix max (€)", min_value=0, step=1000, value=0)
            bathrooms_min = st.number_input("Salles de bains min", min_value=0, step=1, value=0)
            include_sold = st.checkbox("Inclure les biens vendus ?", value=False)

        TYPES = ["maison","appartement","duplex","penthouse","terrain","villa","studio","immeuble","commerce"]
        property_types = st.multiselect("Types de biens", options=TYPES, default=["maison"])

        filters_payload = {
            "cities": [c.strip() for c in (cities_txt or "").split(",") if c.strip()],
            "price_min": int(price_min) if price_min else None,
            "price_max": int(price_max) if price_max else None,
            "bedrooms_min": int(bedrooms_min) if bedrooms_min else None,
            "bathrooms_min": int(bathrooms_min) if bathrooms_min else None,
            "area_min": int(area_min) if area_min else None,
            "include_sold": bool(include_sold),
            "property_types": property_types or []
        }

    submitted = st.form_submit_button("Enregistrer")

    if submitted:
        if not email_in.strip() or not is_valid_email(email_in):
            st.error("Adresse e-mail invalide.")
        elif not host_ok_for_site(chosen_site, url_in.strip()):
            st.error("L’URL ne correspond pas au site sélectionné.")
        else:
            try:
                # Canonicalisation: pour Immo-KH, si vide -> on met la liste par défaut
                if chosen_site == "immokh" and not url_in.strip():
                    canon = "https://www.immo-kh.be/fr/2/chercher-bien/a-vendre"
                else:
                    canon = canonicalize_by_site(chosen_site, url_in.strip() or "")
                new_alert = {
                    "site": chosen_site,
                    "url": canon,
                    "email": email_in.strip(),
                    **({"label": label_in.strip()} if SHOW_LABELS else {})
                }
                if chosen_site == "immokh":
                    new_alert["filters"] = filters_payload or {}

                # Clé de dédup
                key = f"{chosen_site}|{canon}"
                if chosen_site == "immokh":
                    key += "|" + json.dumps(new_alert.get("filters") or {}, sort_keys=True, ensure_ascii=False)

                exists_idx = next((i for i, a in enumerate(st.session_state.alerts)
                                   if (f"{a.get('site','immoweb')}|{a.get('url','')}"
                                       + ("|" + json.dumps(a.get('filters') or {}, sort_keys=True, ensure_ascii=False)
                                          if a.get('site') == 'immokh' else "")
                                      ) == key), None)

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
        filters = a.get("filters")

        with st.container(border=True):
            st.markdown(f"**Site :** `{site}`")
            if SHOW_LABELS and label:
                st.markdown(f"**Label :** {label}")
            st.markdown(f"**URL :** {url or '—'}")
            st.markdown(f"**Email :** {email}")
            if site == "immokh":
                st.markdown(f"**Filtres :** {filters_summary_str(filters)}")

            cols = st.columns([1,1])
            with cols[0]:
                if st.button("✏️ Modifier", key=f"edit_{idx}"):
                    st.session_state[f"edit_mode_{idx}"] = True
            with cols[1]:
                if st.button("🗑️ Supprimer", key=f"del_{idx}"):
                    payload = {"site": site, "url": url}
                    if site == "immokh":
                        payload["filters"] = filters
                    append_event("delete", payload, "Delete alert from Streamlit")
                    st.session_state.alerts = [x for j, x in enumerate(st.session_state.alerts) if j != idx]
                    st.rerun()

            if st.session_state.get(f"edit_mode_{idx}", False):
                with st.form(f"edit_form_{idx}"):
                    st.markdown("_Le site n’est pas modifiable. Supprimez puis recréez pour changer de site._")
                    new_email = st.text_input("Email", value=email)
                    new_label = st.text_input("Label", value=label) if SHOW_LABELS else ""
                    new_url = st.text_input("URL", value=url)

                    new_filters = filters
                    if site == "immokh":
                        st.markdown("**Filtres (Immo-KH)**")
                        col1, col2 = st.columns(2)
                        cities_txt = ",".join((filters or {}).get("cities", []))
                        with col1:
                            ef_cities = st.text_input("Villes (séparées par virgules)", value=cities_txt)
                            ef_price_min = st.number_input("Prix min (€)", min_value=0, step=1000, value=int((filters or {}).get("price_min") or 0))
                            ef_bed_min = st.number_input("Chambres min", min_value=0, step=1, value=int((filters or {}).get("bedrooms_min") or 0))
                            ef_area_min = st.number_input("Surface min (m²)", min_value=0, step=5, value=int((filters or {}).get("area_min") or 0))
                        with col2:
                            ef_price_max = st.number_input("Prix max (€)", min_value=0, step=1000, value=int((filters or {}).get("price_max") or 0))
                            ef_bath_min = st.number_input("Salles de bains min", min_value=0, step=1, value=int((filters or {}).get("bathrooms_min") or 0))
                            ef_include_sold = st.checkbox("Inclure les biens vendus ?", value=bool((filters or {}).get("include_sold") or False))
                        TYPES = ["maison","appartement","duplex","penthouse","terrain","villa","studio","immeuble","commerce"]
                        ef_types = st.multiselect("Types de biens", options=TYPES, default=(filters or {}).get("property_types") or [])
                        new_filters = {
                            "cities": [c.strip() for c in (ef_cities or "").split(",") if c.strip()],
                            "price_min": int(ef_price_min) if ef_price_min else None,
                            "price_max": int(ef_price_max) if ef_price_max else None,
                            "bedrooms_min": int(ef_bed_min) if ef_bed_min else None,
                            "bathrooms_min": int(ef_bath_min) if ef_bath_min else None,
                            "area_min": int(ef_area_min) if ef_area_min else None,
                            "include_sold": bool(ef_include_sold),
                            "property_types": ef_types or []
                        }

                    ok = st.form_submit_button("Sauvegarder")
                    if ok:
                        try:
                            if not is_valid_email(new_email):
                                st.warning("Email invalide.")
                            elif not host_ok_for_site(site, new_url.strip()):
                                st.warning("URL incohérente avec le site.")
                            else:
                                if site == "immokh" and not new_url.strip():
                                    canon2 = "https://www.immo-kh.be/fr/2/chercher-bien/a-vendre"
                                else:
                                    canon2 = canonicalize_by_site(site, new_url.strip() or "")
                                edited = {
                                    "site": site,
                                    "url": canon2,
                                    "email": new_email.strip(),
                                    **({"label": new_label.strip()} if SHOW_LABELS else {})
                                }
                                if site == "immokh":
                                    edited["filters"] = new_filters

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
- Sélectionnez le **site**, puis collez l’**URL** (avec filtres si le site en propose).
- Pour **Immo-KH**, **l’URL est optionnelle** (la liste par défaut est utilisée) et **les filtres ci-dessus** sont enregistrés dans `alerts.jsonl`.
- Les alertes sont **persistées dans GitHub** (`AlertMe/alerts.jsonl`) si un **GH_TOKEN** est configuré (Contents: Read & Write). Sinon, stockage local.
- Le fichier est un **journal d’événements** append-only; la déduplication tient compte du **site**, de l’**URL canonique** et, pour **Immo-KH**, des **filtres**.
""")
