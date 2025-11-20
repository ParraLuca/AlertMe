# streamlit_app.py
import os, json, re, base64, requests
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import streamlit as st

# ============== CONFIG & THEME ==============
st.set_page_config(
    page_title="AlertMe",
    page_icon="🔔",
    layout="centered",
    initial_sidebar_state="collapsed"
)

# CSS "CLEAN UI" (Flat & Moderne)
st.markdown("""
<style>
    :root { --primary: #4f46e5; --text-dark: #1e293b; --text-gray: #64748b; }
    .block-container { padding-top: 2rem; max-width: 750px; }
    
    /* Header */
    .main-title { font-size: 1.8rem; font-weight: 800; color: var(--text-dark); margin-bottom: 0; }
    .subtitle { font-size: 1rem; color: var(--text-gray); margin-bottom: 1.5rem; }
    
    /* Inputs plus doux */
    .stTextInput input, .stNumberInput input {
        border: 1px solid #e2e8f0; border-radius: 8px; padding: 10px;
    }
    .stTextInput input:focus { border-color: var(--primary); box-shadow: 0 0 0 3px rgba(79,70,229,0.1); }

    /* Badges et Tags */
    .badge-site {
        font-size: 0.7rem; font-weight: 700; text-transform: uppercase;
        padding: 3px 8px; border-radius: 6px; background: #eff6ff; color: #2563eb; margin-left: 8px;
    }
    .filter-pill {
        display: inline-block; font-size: 0.75rem; color: #475569; background: #f1f5f9;
        padding: 2px 8px; border-radius: 99px; margin-right: 4px; margin-top: 4px; border: 1px solid #e2e8f0;
    }

    /* Cartes Alertes */
    .card-container { padding: 12px 0; }
    .card-title { font-size: 1.05rem; font-weight: 600; color: var(--text-dark); display: flex; align-items: center; }
    .card-link { font-family: monospace; color: var(--text-gray); font-size: 0.8rem; text-decoration: none; }
    .card-link:hover { color: var(--primary); text-decoration: underline; }

    /* Onglets */
    .stTabs [data-baseweb="tab-list"] { gap: 12px; }
    .stTabs [aria-selected="true"] { color: var(--primary); border-bottom-color: var(--primary); }
</style>
""", unsafe_allow_html=True)

# ============== LOGIQUE METIER (INCHANGÉE) ==============
CONFIG_PATH = os.path.join(".", "config.json")
DEFAULT_CONFIG = {
    "alerts_path": "./AlertMe/alerts.jsonl",
    "ui": { "title": "AlertMe", "show_labels": True },
    "sites": [
        {"id": "immoweb", "host_contains": "immoweb.be"},
        {"id": "marjorietome", "host_contains": "immotoma.be"},
        {"id": "immokh", "host_contains": "immo-kh.be"},
        {"id": "adhome", "host_contains": "ad-home.be"}
    ],
    "scraper_defaults": { "pages": 20, "order_keys": ["newest"] }
}

def _load_cfg():
    try:
        if os.path.isfile(CONFIG_PATH):
            with open(CONFIG_PATH,"r",encoding="utf-8") as f: return json.load(f)
    except: pass
    return DEFAULT_CONFIG

CFG = _load_cfg()
ALERTS_PATH = CFG.get("alerts_path", "./AlertMe/alerts.jsonl")
SITES = CFG.get("sites", [])
BROWSER_SITES = {"immokh", "adhome"}
IMMOWEB_HOST = "www.immoweb.be"
IMMOKH_LIST = "https://www.immo-kh.be/fr/2/chercher-bien/a-vendre"
ADHOME_LIST = "https://www.ad-home.be/fr/2/chercher-bien/a-vendre"

def _sec(k):
    try: return st.secrets.get(k)
    except: return os.getenv(k)

def _gh_headers():
    tok = _sec("GH_TOKEN")
    return {"Authorization": f"token {tok}","Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"} if tok else None

def gh_get_file():
    headers = _gh_headers()
    if not headers: return None, None
    repo, path = _sec("GH_REPO") or "ParraLuca/AlertMe", _sec("GH_PATH") or "AlertMe/alerts.jsonl"
    branch = _sec("GH_BRANCH") or "main"
    r = requests.get(f"https://api.github.com/repos/{repo}/contents/{path}", headers=headers, params={"ref":branch})
    if r.status_code == 200:
        data = r.json()
        return base64.b64decode(data["content"]).decode("utf-8"), data["sha"]
    return None, None

def gh_append_line(line, msg):
    headers = _gh_headers()
    if not headers: return None
    repo, path = _sec("GH_REPO") or "ParraLuca/AlertMe", _sec("GH_PATH") or "AlertMe/alerts.jsonl"
    branch = _sec("GH_BRANCH") or "main"
    current, sha = gh_get_file()
    content = (current + line + "\n") if current else (line + "\n")
    payload = {"message": msg, "content": base64.b64encode(content.encode()).decode(), "branch": branch}
    if sha: payload["sha"] = sha
    requests.put(f"https://api.github.com/repos/{repo}/contents/{path}", headers=headers, json=payload).raise_for_status()
    return True

def is_valid_email(s): return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", str(s).strip()))
def utc_iso(): return datetime.now(timezone.utc).isoformat()

def canonicalize_immoweb_url(u_in):
    u = urlparse(u_in)
    q = parse_qs(u.query); q["orderBy"] = ["newest"]; q.pop("page", None)
    return urlunparse((u.scheme, u.netloc, u.path, u.params, urlencode({k:v[0] for k,v in q.items()}), u.fragment))

def canonicalize_marjorietome_url(u_in):
    u = urlparse(u_in); q = parse_qs(u.query); q.pop("paged", None)
    return urlunparse((u.scheme, u.netloc, u.path, u.params, urlencode({k:v[0] for k,v in q.items()}), u.fragment))

def host_ok(site_id, url):
    if site_id in BROWSER_SITES: return True
    try: host = urlparse(url).netloc.lower()
    except: return False
    for s in SITES:
        if s["id"] == site_id: return s.get("host_contains", "") in host
    return True

def append_event(action, alert, msg):
    ev = {"ts": utc_iso(), "action": action, "alert": alert}
    line = json.dumps(ev, ensure_ascii=False)
    if _gh_headers(): 
        try: gh_append_line(line, msg)
        except Exception as e: st.error(f"GitHub Error: {e}")
    else:
        os.makedirs(os.path.dirname(ALERTS_PATH) or ".", exist_ok=True)
        with open(ALERTS_PATH, "a", encoding="utf-8") as f: f.write(line + "\n")

def load_alerts():
    raw = []
    content, _ = gh_get_file()
    if content: lines = content.splitlines()
    elif os.path.isfile(ALERTS_PATH):
        with open(ALERTS_PATH, "r", encoding="utf-8") as f: lines = f.readlines()
    else: lines = []
    
    for l in lines:
        if l.strip():
            try: raw.append(json.loads(l))
            except: pass
            
    state = {}
    for row in raw:
        if "action" not in row: continue
        action = row.get("action")
        a = row.get("alert", {})
        site = a.get("site", "immoweb")
        url = a.get("url", "")
        if site == "immokh": url = IMMOKH_LIST
        if site == "adhome": url = ADHOME_LIST
        fkey = json.dumps(a.get("filters"), sort_keys=True) if a.get("filters") else ""
        key = f"{site}|{url}|{fkey}"
        
        if action in ("add", "update"): state[key] = a
        elif action == "delete": state.pop(key, None)
            
    return list(state.values())

# UI Components
IMMOKH_TYPES = ["maison","appartement","penthouse","terrain","villa","immeuble","commerce"]

def filters_ui(default=None, key_prefix=""):
    d = default or {}
    st.markdown("###### 🎯 Vos critères")
    types = st.multiselect("Type de bien", IMMOKH_TYPES, default=[t for t in d.get("property_types",[]) if t in IMMOKH_TYPES], key=f"{key_prefix}_types")
    cities = st.text_input("Villes (séparées par virgule)", value=",".join(d.get("cities",[])), placeholder="Ex: Namur, Jambes", key=f"{key_prefix}_cities")
    
    c1, c2, c3 = st.columns(3)
    with c1: p_min = st.number_input("Prix Min", value=int(d.get("price_min") or 0), step=5000, key=f"{key_prefix}_pmin")
    with c2: p_max = st.number_input("Prix Max", value=int(d.get("price_max") or 0), step=5000, key=f"{key_prefix}_pmax")
    with c3: b_min = st.number_input("Chambres Min", value=int(d.get("bedrooms_min") or 0), key=f"{key_prefix}_bmin")
    
    return {
        "property_types": types,
        "cities": [c.strip() for c in cities.split(",") if c.strip()],
        "price_min": int(p_min), "price_max": int(p_max), "bedrooms_min": int(b_min),
        "include_sold": False
    }

def filters_badges_html(f):
    if not f: return ""
    html = ""
    if f.get("property_types"):
        for t in f["property_types"]: html += f"<span class='filter-pill'>🏠 {t.capitalize()}</span>"
    if f.get("cities"):
        for c in f["cities"]: html += f"<span class='filter-pill'>📍 {c}</span>"
    p_max = int(f.get("price_max") or 0)
    if p_max > 0: html += f"<span class='filter-pill'>💰 Max {p_max}€</span>"
    b_min = int(f.get("bedrooms_min") or 0)
    if b_min > 0: html += f"<span class='filter-pill'>🛏️ {b_min}+ ch</span>"
    return html

# ============== APP START ==============

if "alerts" not in st.session_state:
    st.session_state.alerts = load_alerts()

# --- TITRE ---
st.markdown('<div class="main-title">🔔 AlertMe</div>', unsafe_allow_html=True)
st.markdown('<div class="subtitle">Gestion de vos surveillances immobilières</div>', unsafe_allow_html=True)

# --- ONBOARDING (Explication minimale et clean) ---
with st.expander("📚 Comment utiliser l'application ?", expanded=False):
    st.markdown("""
    1. **Identifiez-vous** : Entrez votre email ci-dessous. C'est la clé pour retrouver vos alertes.
    2. **Ajoutez une alerte** :
        - **Immoweb / ImmoToma** : Faites votre recherche sur leur site, copiez l'URL, et collez-la ici.
        - **Immo-KH / AD-HOME** : Pas d'URL nécessaire. Configurez vos filtres (Ville, Prix...) directement ici.
    3. **Dormez tranquille** : Vous recevrez un email dès qu'un nouveau bien correspond.
    """)

# --- IDENTIFICATION ---
user_email = st.text_input(
    "Votre adresse email", 
    placeholder="exemple@gmail.com", 
    help="Nécessaire pour associer les alertes à votre compte et vous envoyer les notifications."
)

is_email_valid = is_valid_email(user_email)
user_alerts = [a for a in st.session_state.alerts if a.get("email") == user_email.strip()] if is_email_valid else []

st.divider()

# --- AJOUT ALERTE ---
st.subheader("✨ Créer une nouvelle alerte")

tab_iw, tab_mt, tab_ka = st.tabs(["🏠 Immoweb", "🏷️ ImmoToma", "🤝 KH & AD-HOME"])

# > Immoweb
with tab_iw:
    st.markdown("👉 [Ouvrir Immoweb](https://www.immoweb.be/fr/recherche) pour faire votre recherche.")
    with st.form("new_iw", clear_on_submit=True):
        c1, c2 = st.columns([3, 2])
        with c1: u = st.text_input("Collez l'URL Immoweb ici", placeholder="https://www.immoweb.be/fr/recherche/...")
        with c2: l = st.text_input("Nom (Optionnel)", placeholder="Ex: Bruxelles Sud")
        submitted = st.form_submit_button("Activer l'alerte")
        
        if submitted:
            if not is_email_valid: st.error("❌ Veuillez entrer votre email en haut de la page.")
            elif not host_ok("immoweb", u): st.error("❌ L'URL doit provenir d'Immoweb.")
            else:
                try:
                    clean_url = canonicalize_immoweb_url(u)
                    rec = {"site":"immoweb","url":clean_url,"email":user_email.strip(),"label":l.strip(),"pages":20}
                    st.session_state.alerts.append(rec)
                    append_event("add", rec, "Add IW")
                    st.toast("Alerte Immoweb ajoutée !", icon="✅")
                    st.rerun()
                except Exception as e: st.error(f"Erreur URL: {e}")

# > Toma
with tab_mt:
    st.markdown("👉 [Ouvrir ImmoToma](https://immotoma.be/advanced-search/) pour faire votre recherche.")
    with st.form("new_mt", clear_on_submit=True):
        c1, c2 = st.columns([3, 2])
        with c1: u = st.text_input("Collez l'URL ImmoToma ici", placeholder="https://immotoma.be/advanced-search/...")
        with c2: l = st.text_input("Nom (Optionnel)", placeholder="Ex: Investissement")
        submitted = st.form_submit_button("Activer l'alerte")
        
        if submitted:
            if not is_email_valid: st.error("❌ Veuillez entrer votre email en haut de la page.")
            elif not host_ok("marjorietome", u): st.error("❌ L'URL doit provenir d'ImmoToma.")
            else:
                try:
                    clean_url = canonicalize_marjorietome_url(u)
                    rec = {"site":"marjorietome","url":clean_url,"email":user_email.strip(),"label":l.strip(),"pages":20}
                    st.session_state.alerts.append(rec)
                    append_event("add", rec, "Add MT")
                    st.toast("Alerte ImmoToma ajoutée !", icon="✅")
                    st.rerun()
                except: st.error("URL invalide")

# > KH/AD
with tab_ka:
    with st.form("new_ka", clear_on_submit=True):
        st.info("💡 Crée automatiquement une alerte pour **Immo-KH** ET **AD-HOME** avec ces critères.")
        l = st.text_input("Nom de la recherche", placeholder="Ex: Maison 3 chambres")
        f_data = filters_ui(key_prefix="new_ka")
        submitted = st.form_submit_button("Activer la surveillance double")
        
        if submitted:
            if not is_email_valid: st.error("❌ Veuillez entrer votre email en haut de la page.")
            else:
                for s_id, s_url in [("immokh", IMMOKH_LIST), ("adhome", ADHOME_LIST)]:
                    rec = {"site":s_id,"url":s_url,"email":user_email.strip(),"label":l.strip(),"filters":f_data,"use_browser":True,"pages":20}
                    st.session_state.alerts.append(rec)
                    append_event("add", rec, f"Add {s_id}")
                st.toast("Double alerte activée avec succès !", icon="🚀")
                st.rerun()

st.divider()

# --- LISTE ---
st.subheader(f"📋 Vos surveillances ({len(user_alerts)})")

if not is_email_valid:
    st.info("👆 Entrez votre adresse email ci-dessus pour voir vos alertes.")
elif not user_alerts:
    st.caption("Aucune alerte active pour cet email. Utilisez le formulaire ci-dessus pour commencer.")
else:
    for i, alert in enumerate(user_alerts):
        real_index = next((idx for idx, a in enumerate(st.session_state.alerts) if a == alert), None)
        if real_index is None: continue

        site = alert.get("site", "N/A")
        label = alert.get("label") or "Alerte sans nom"
        url = alert.get("url", "")
        filters = alert.get("filters")
        
        with st.container():
            c_content, c_action = st.columns([0.9, 0.1])
            with c_content:
                # HTML Content Clean
                site_badge = f"<span class='badge-site'>{site}</span>"
                if site in BROWSER_SITES:
                    details = filters_badges_html(filters)
                else:
                    disp_url = (url[:75] + '...') if len(url) > 75 else url
                    details = f"<a href='{url}' target='_blank' class='card-link'>🔗 {disp_url}</a>"
                
                st.markdown(f"""
                    <div class='card-title'>{label} {site_badge}</div>
                    <div style='margin-top:4px;'>{details}</div>
                """, unsafe_allow_html=True)
                
            with c_action:
                st.write("") 
                if st.button("🗑️", key=f"del_{i}_{real_index}", help="Supprimer l'alerte"):
                    st.session_state.alerts.pop(real_index)
                    payload = {"site":site,"url":url}
                    if filters: payload["filters"] = filters
                    append_event("delete", payload, "User delete")
                    st.rerun()
            
            st.markdown("<div style='border-bottom:1px solid #f1f5f9; margin: 8px 0;'></div>", unsafe_allow_html=True)
