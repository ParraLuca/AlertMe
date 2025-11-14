# streamlit_app.py
import os, json, re, base64, requests
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import streamlit as st

# ====================== CONFIG GÉNÉRALE ======================
st.set_page_config(page_title="AlertMe – Gestion des alertes", page_icon="🔔", layout="centered")

CONFIG_PATH = os.path.join(".", "config.json")
DEFAULT_CONFIG = {
    "alerts_path": "./AlertMe/alerts.jsonl",
    "max_alerts": 200,
    "ui": {
        "title": "AlertMe – Gestion des alertes",
        "subtitle": "Immoweb/ImmoToma via URL; Immo-KH via filtres dédiés.",
        "show_labels": True
    },
    "sites": [
        {"id": "immoweb",      "label": "Immoweb",                  "host_contains": "immoweb.be"},
        {"id": "marjorietome", "label": "ImmoToma (Marjorie Toma)", "host_contains": "immotoma.be"},
        {"id": "immokh",       "label": "Immo-KH",                  "host_contains": "immo-kh.be"}
    ],
    "scraper_defaults": {
        "pages": 20,
        "order_keys": ["newest", "most_recent"]
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
MAX_ALERTS = int(CFG["max_alerts"])
SHOW_LABELS = bool(CFG.get("ui", {}).get("show_labels", True))
SITES = CFG.get("sites", [])
ORDER_KEYS = CFG.get("scraper_defaults", {}).get("order_keys", ["newest", "most_recent"])
DEFAULT_PAGES = int(CFG.get("scraper_defaults", {}).get("pages", 20))
IMMOWEB_HOST = "www.immoweb.be"

# ====================== SECRETS GITHUB (SÛRS) ======================
def _safe_secret(key: str):
    try:
        return st.secrets.get(key)  # type: ignore[attr-defined]
    except Exception:
        return None

def _gh_token():
    return _safe_secret("GH_TOKEN") or os.getenv("GH_TOKEN")

def _gh_repo_cfg():
    repo   = _safe_secret("GH_REPO")   or os.getenv("GH_REPO",  "ParraLuca/AlertMe")
    path   = _safe_secret("GH_PATH")   or os.getenv("GH_PATH",  "AlertMe/alerts.jsonl")
    branch = _safe_secret("GH_BRANCH") or os.getenv("GH_BRANCH","main")
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

# ====================== UTILS & CANONICALISATION ======================
def is_valid_email(s: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", s.strip()))

def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def canonicalize_immoweb_url(user_url: str) -> str:
    u = urlparse(user_url)
    if IMMOWEB_HOST not in (u.netloc or ""):
        raise ValueError("Ce n'est pas une URL Immoweb.")
    q = parse_qs(u.query)
    q["orderBy"] = [ORDER_KEYS[0] if ORDER_KEYS else "newest"]
    q.pop("page", None)
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
    s = (site_id or "").strip().lower()
    if s == "immoweb":
        return canonicalize_immoweb_url(user_url)
    if s == "marjorietome":
        return canonicalize_marjorietome_url(user_url)
    return canonicalize_generic_url(user_url)

def host_ok_for_site(site_id: str, user_url: str) -> bool:
    if (site_id or "").lower().strip() == "immokh" and not (user_url or "").strip():
        return True  # URL optionnelle pour Immo-KH
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

# ====================== JOURNAL (alerts.jsonl) ======================
def make_event(action: str, alert: dict) -> dict:
    assert action in {"add","update","delete"}
    ev = {"ts": utc_iso(), "action": action, "alert": {}}
    for key in ("site","url","email","label","pages","filters","use_browser"):
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
    """Déduplique par (site|URL) + (Immo-KH: filtres JSON)."""
    state: dict[str, dict] = {}
    for row in lines:
        if not isinstance(row, dict):
            continue

        # Ancien format
        if "action" not in row or "alert" not in row:
            a = row
            site = (a.get("site") or "immoweb").strip().lower()
            url_in = (a.get("url","") or "").strip()
            if site == "immokh" and not url_in:
                url_in = "https://www.immo-kh.be/fr/2/chercher-bien/a-vendre"
            try:
                canon = canonicalize_by_site(site, url_in)
            except Exception:
                canon = url_in
            key = f"{site}|{canon}"
            rec = {"site": site, "url": canon, "email": (a.get("email","") or "").strip()}
            if SHOW_LABELS: rec["label"] = (a.get("label","") or "").strip()
            if site == "immokh" and a.get("filters") is not None:
                rec["filters"] = a["filters"]
                key += "|" + json.dumps(a["filters"], sort_keys=True, ensure_ascii=False)
            if a.get("pages") is not None: rec["pages"] = int(a["pages"])
            if a.get("use_browser") is not None: rec["use_browser"] = bool(a["use_browser"])
            state[key] = rec
            continue

        # Nouveau format
        action = (row.get("action") or "").strip().lower()
        a = row.get("alert", {}) or {}
        site = (a.get("site") or "immoweb").strip().lower()
        url_in = (a.get("url","") or "").strip()
        if site == "immokh" and not url_in:
            url_in = "https://www.immo-kh.be/fr/2/chercher-bien/a-vendre"
        filters = a.get("filters")
        filt_key = json.dumps(filters, sort_keys=True, ensure_ascii=False) if filters else ""

        if action in {"add","update"}:
            try:
                canon = canonicalize_by_site(site, url_in)
            except Exception:
                canon = url_in
            key = f"{site}|{canon}"
            rec = {"site": site, "url": canon, "email": (a.get("email","") or "").strip()}
            if SHOW_LABELS: rec["label"] = (a.get("label","") or "").strip()
            if filters is not None:
                rec["filters"] = filters
                key += f"|{filt_key}"
            if a.get("pages") is not None: rec["pages"] = int(a["pages"])
            if a.get("use_browser") is not None: rec["use_browser"] = bool(a["use_browser"])
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

# ====================== UI HELPERS ======================
IMMOKH_TYPES = [
    "maison","appartement","duplex","penthouse","terrain",
    "villa","studio","immeuble","commerce","bureau","industriel","garage"
]

def filters_summary_str(filters: dict | None) -> str:
    if not filters: return "—"
    parts = []
    if filters.get("property_types"): parts.append("Types: " + ", ".join(filters["property_types"]))
    if filters.get("cities"): parts.append("Villes: " + ", ".join(filters["cities"]))
    if (filters.get("price_min") is not None) or (filters.get("price_max") is not None):
        parts.append(f"Prix: {filters.get('price_min','—')}→{filters.get('price_max','—')}")
    if filters.get("area_min") is not None: parts.append(f"≥{filters['area_min']} m²")
    if filters.get("bedrooms_min") is not None: parts.append(f"≥{filters['bedrooms_min']} ch.")
    if filters.get("bathrooms_min") is not None: parts.append(f"≥{filters['bathrooms_min']} sdb")
    if filters.get("include_sold"): parts.append("incl. vendus")
    return " · ".join(parts) if parts else "—"

def ui_checkbox_matrix(title: str, options: list[str], defaults: list[str]) -> list[str]:
    st.markdown(f"**{title}**")
    cols = st.columns(3)
    selected = set(defaults)
    for i, opt in enumerate(options):
        with cols[i % 3]:
            if st.checkbox(opt.capitalize(), value=(opt in defaults), key=f"type_{title}_{i}"):
                selected.add(opt)
            else:
                selected.discard(opt)
    return sorted(selected)

def immokh_filters_ui(default=None):
    d = default or {}
    st.markdown("### Filtres Immo-KH")
    st.caption("Immo-KH ne met pas les filtres dans l’URL. Ils sont stockés ici et appliqués par le scraper.")
    # Types (cases à cocher)
    default_types = d.get("property_types") or ["maison","appartement","penthouse","terrain"]
    property_types = ui_checkbox_matrix("Types de biens", IMMOKH_TYPES, default_types)

    # Villes
    cities_txt = st.text_input(
        "Villes (séparées par des virgules)",
        value=",".join(d.get("cities", [])),
        placeholder="ex: Tamines, Aiseau-Presles, Fosses-la-Ville"
    )
    cities = [c.strip() for c in (cities_txt or "").split(",") if c.strip()]

    # Grilles numériques min/max
    colA, colB = st.columns(2)
    with colA:
        price_min = st.number_input("Prix min (€)", min_value=0, step=1000, value=int(d.get("price_min") or 0))
        bedrooms_min = st.number_input("Chambres min", min_value=0, step=1, value=int(d.get("bedrooms_min") or 0))
        area_min = st.number_input("Surface min (m²)", min_value=0, step=5, value=int(d.get("area_min") or 0))
    with colB:
        price_max = st.number_input("Prix max (€)", min_value=0, step=1000, value=int(d.get("price_max") or 0))
        bathrooms_min = st.number_input("Salles de bains min", min_value=0, step=1, value=int(d.get("bathrooms_min") or 0))
        include_sold = st.checkbox("Inclure les biens vendus ?", value=bool(d.get("include_sold") or False))

    return {
        "property_types": property_types or [],
        "cities": cities,
        "price_min": int(price_min) if price_min else None,
        "price_max": int(price_max) if price_max else None,
        "bedrooms_min": int(bedrooms_min) if bedrooms_min else None,
        "bathrooms_min": int(bathrooms_min) if bathrooms_min else None,
        "area_min": int(area_min) if area_min else None,
        "include_sold": bool(include_sold),
    }

# ====================== UI PRINCIPALE (TABS) ======================
st.title("🔔 " + CFG["ui"]["title"])
st.caption(CFG["ui"]["subtitle"])

if "alerts" not in st.session_state:
    st.session_state.alerts = load_alerts()

tab_iw, tab_mt, tab_kh = st.tabs(["🏠 Immoweb", "🏷️ ImmoToma", "🏡 Immo-KH"])

# -------- TAB IMMOWEB (URL-based) --------
with tab_iw:
    with st.form("form_immoweb", clear_on_submit=True):
        st.subheader("Créer une alerte Immoweb")
        url = st.text_input("URL Immoweb (avec vos filtres)", placeholder="https://www.immoweb.be/fr/recherche/...")
        email = st.text_input("Email de notification", placeholder="ex: prenom.nom@gmail.com")
        pages = st.number_input("Pages max à collecter", min_value=1, max_value=200, value=DEFAULT_PAGES, step=1)
        label = st.text_input("Label (facultatif)") if SHOW_LABELS else ""
        submitted = st.form_submit_button("Enregistrer")
        if submitted:
            if not url.strip():
                st.error("L’URL est requise.")
            elif not email.strip() or not is_valid_email(email):
                st.error("Email invalide.")
            else:
                try:
                    canon = canonicalize_immoweb_url(url.strip())
                    rec = {"site":"immoweb","url":canon,"email":email.strip(),"pages":int(pages)}
                    if SHOW_LABELS: rec["label"] = label.strip()
                    # clé dédup simple (site|url)
                    key = f"immoweb|{canon}"
                    exists = next((i for i,a in enumerate(st.session_state.alerts) if f"{a.get('site')}|{a.get('url')}"==key), None)
                    if exists is not None:
                        st.session_state.alerts[exists] = rec
                        append_event("update", rec, "Update alert Immoweb")
                    else:
                        st.session_state.alerts.append(rec)
                        append_event("add", rec, "Add alert Immoweb")
                    st.success("Alerte Immoweb enregistrée ✅")
                except Exception as e:
                    st.error(f"Erreur: {e}")

# -------- TAB IMMO TOMA (URL-based) --------
with tab_mt:
    with st.form("form_marjorietome", clear_on_submit=True):
        st.subheader("Créer une alerte ImmoToma (Marjorie Toma)")
        url = st.text_input("URL ImmoToma (avec vos filtres)", placeholder="https://immotoma.be/advanced-search/?...")
        email = st.text_input("Email de notification", placeholder="ex: prenom.nom@gmail.com")
        pages = st.number_input("Pages max à collecter", min_value=1, max_value=200, value=DEFAULT_PAGES, step=1)
        label = st.text_input("Label (facultatif)") if SHOW_LABELS else ""
        submitted = st.form_submit_button("Enregistrer")
        if submitted:
            if not url.strip():
                st.error("L’URL est requise.")
            elif not email.strip() or not is_valid_email(email):
                st.error("Email invalide.")
            else:
                try:
                    canon = canonicalize_marjorietome_url(url.strip())
                    rec = {"site":"marjorietome","url":canon,"email":email.strip(),"pages":int(pages)}
                    if SHOW_LABELS: rec["label"] = label.strip()
                    key = f"marjorietome|{canon}"
                    exists = next((i for i,a in enumerate(st.session_state.alerts) if f"{a.get('site')}|{a.get('url')}"==key), None)
                    if exists is not None:
                        st.session_state.alerts[exists] = rec
                        append_event("update", rec, "Update alert ImmoToma")
                    else:
                        st.session_state.alerts.append(rec)
                        append_event("add", rec, "Add alert ImmoToma")
                    st.success("Alerte ImmoToma enregistrée ✅")
                except Exception as e:
                    st.error(f"Erreur: {e}")

# -------- TAB IMMO-KH (Filters-based) --------
with tab_kh:
    with st.form("form_immokh", clear_on_submit=True):
        st.subheader("Créer une alerte Immo-KH")
        st.caption("L’URL est optionnelle (liste par défaut). Les filtres ci-dessous seront appliqués par le scraper.")
        url = st.text_input(
            "URL Immo-KH (optionnelle)",
            placeholder="(laisser vide pour https://www.immo-kh.be/fr/2/chercher-bien/a-vendre)"
        )
        email = st.text_input("Email de notification", placeholder="ex: prenom.nom@gmail.com")
        pages = st.number_input("Pages/clics max (navigateur ou HTTP)", min_value=1, max_value=200, value=DEFAULT_PAGES, step=1)
        use_browser = st.checkbox("Préférer le navigateur (Playwright)", value=True, help="Recommandé pour un défilement ‘load more’ fiable.")
        label = st.text_input("Label (facultatif)") if SHOW_LABELS else ""

        filters_payload = immokh_filters_ui()

        submitted = st.form_submit_button("Enregistrer")
        if submitted:
            if not email.strip() or not is_valid_email(email):
                st.error("Email invalide.")
            elif not host_ok_for_site("immokh", url.strip()):
                st.error("URL incohérente avec Immo-KH.")
            else:
                try:
                    canon = "https://www.immo-kh.be/fr/2/chercher-bien/a-vendre" if not url.strip() else canonicalize_generic_url(url.strip())
                    rec = {
                        "site":"immokh",
                        "url":canon,
                        "email":email.strip(),
                        "pages":int(pages),
                        "use_browser": bool(use_browser),
                        "filters": filters_payload
                    }
                    if SHOW_LABELS: rec["label"] = label.strip()

                    # clé dédup: site|url|filtersJSON
                    fkey = json.dumps(filters_payload or {}, sort_keys=True, ensure_ascii=False)
                    key = f"immokh|{canon}|{fkey}"
                    exists = next((i for i,a in enumerate(st.session_state.alerts)
                                   if (f"{a.get('site')}|{a.get('url')}|"+json.dumps(a.get('filters') or {}, sort_keys=True, ensure_ascii=False))==key), None)
                    if exists is not None:
                        st.session_state.alerts[exists] = rec
                        append_event("update", rec, "Update alert Immo-KH")
                    else:
                        st.session_state.alerts.append(rec)
                        append_event("add", rec, "Add alert Immo-KH")
                    st.success("Alerte Immo-KH enregistrée ✅")
                except Exception as e:
                    st.error(f"Erreur: {e}")

# ====================== LISTE / ÉDITION DES ALERTES ======================
st.divider()
st.subheader("Mes alertes")

def render_card(idx: int, a: dict):
    site    = a.get("site","immoweb")
    url     = a.get("url","")
    email   = a.get("email","")
    label   = a.get("label","") if SHOW_LABELS else ""
    filters = a.get("filters")
    pages   = a.get("pages")
    use_br  = a.get("use_browser", None)

    with st.container(border=True):
        st.markdown(f"**Site :** `{site}`")
        if SHOW_LABELS and label:
            st.markdown(f"**Label :** {label}")
        st.markdown(f"**URL :** {url or '—'}")
        st.markdown(f"**Email :** {email}")
        if pages: st.markdown(f"**Pages max :** {pages}")
        if site == "immokh":
            if use_br is not None:
                st.markdown(f"**Préférer navigateur :** {'oui' if use_br else 'non'}")
            st.markdown(f"**Filtres :** {filters_summary_str(filters)}")

        c1, c2 = st.columns([1,1])
        with c1:
            if st.button("✏️ Modifier", key=f"edit_{idx}"):
                st.session_state[f"edit_mode_{idx}"] = True
        with c2:
            if st.button("🗑️ Supprimer", key=f"del_{idx}"):
                payload = {"site": site, "url": url}
                if site == "immokh" and filters is not None:
                    payload["filters"] = filters
                append_event("delete", payload, "Delete alert from UI")
                st.session_state.alerts = [x for j, x in enumerate(st.session_state.alerts) if j != idx]
                st.rerun()

        # Édition inline
        if st.session_state.get(f"edit_mode_{idx}", False):
            with st.form(f"form_edit_{idx}"):
                st.markdown("_Le site n’est pas modifiable. Supprimez puis recréez pour changer de site._")
                new_email = st.text_input("Email", value=email)
                new_label = st.text_input("Label", value=label) if SHOW_LABELS else ""
                new_url   = st.text_input("URL", value=url)
                new_pages = st.number_input("Pages max", min_value=1, max_value=200, value=int(pages or DEFAULT_PAGES), step=1)
                new_usebr = st.checkbox("Préférer le navigateur (Immo-KH)", value=bool(use_br) if use_br is not None else (site=="immokh"))

                new_filters = filters
                if site == "immokh":
                    new_filters = immokh_filters_ui(default=filters)

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
                                # immoweb/marjorietome conservent leur canonicaliseur dédié
                                canon2 = canonicalize_by_site(site, new_url.strip() or "")
                            edited = {
                                "site": site,
                                "url": canon2,
                                "email": new_email.strip(),
                                "pages": int(new_pages),
                                **({"label": new_label.strip()} if SHOW_LABELS else {})
                            }
                            if site == "immokh":
                                edited["filters"] = new_filters
                                edited["use_browser"] = bool(new_usebr)

                            # Remplace et journalise
                            st.session_state.alerts[idx] = edited
                            append_event("update", edited, "Inline edit alert")
                            st.session_state[f"edit_mode_{idx}"] = False
                            st.success("Alerte mise à jour ✅")
                            st.rerun()
                    except Exception as e:
                        st.error(f"Erreur: {e}")

# Affichage de toutes les alertes
if not st.session_state.alerts:
    st.info("Aucune alerte pour l’instant.")
else:
    for idx, a in enumerate(st.session_state.alerts):
        render_card(idx, a)

st.divider()
with st.expander("ℹ️ Aide"):
    st.markdown("""
- **Immoweb / ImmoToma** : collez l’**URL** (leurs filtres restent dans l’URL).
- **Immo-KH** : définissez les **filtres dédiés** (types par cases à cocher, villes, prix min/max, surface min, chambres min, SDB min).  
  L’option **“Inclure vendus”** est décochée par défaut.
- **Pages max** et **Préférer le navigateur** sont stockés avec l’alerte (Immo-KH).
- Les alertes sont persistées dans **GitHub** si `GH_TOKEN` est présent, sinon **localement** dans `alerts.jsonl`.
- Déduplication :  
  - Immoweb/ImmoToma → `site|url canonique`  
  - Immo-KH → `site|url canonique|JSON(filters)`
""")
