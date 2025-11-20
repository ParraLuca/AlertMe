# streamlit_app.py
import os, json, re, base64, requests
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import streamlit as st

# ============== CONFIG & THEME ==============
st.set_page_config(
    page_title="AlertMe – Mon Tableau de Bord",
    page_icon="🔔",
    layout="centered",
    initial_sidebar_state="collapsed"
)

# CSS MODERNE & UI
st.markdown("""
<style>
    /* Variables & Thème */
    :root {
        --primary: #6366f1;       /* Indigo 500 */
        --primary-dark: #4338ca;  /* Indigo 700 */
        --bg-soft: #f8fafc;       /* Slate 50 */
        --text-main: #0f172a;     /* Slate 900 */
        --text-muted: #64748b;    /* Slate 500 */
        --card-bg: #ffffff;
        --border: #e2e8f0;
    }

    /* Global */
    .block-container { padding-top: 2rem; max-width: 850px; }
    
    /* Header "Identity" Section */
    .identity-box {
        background: linear-gradient(135deg, var(--primary), var(--primary-dark));
        padding: 1.5rem;
        border-radius: 12px;
        color: white;
        margin-bottom: 2rem;
        box-shadow: 0 4px 6px -1px rgba(79, 70, 229, 0.2);
    }
    .identity-box h2 { color: white !important; margin: 0 0 0.5rem 0; font-size: 1.5rem; }
    .identity-box p { color: #e0e7ff; margin: 0; font-size: 0.9rem; }
    .stTextInput input {
        border: 2px solid var(--border);
        border-radius: 8px;
        padding: 0.5rem 1rem;
    }
    .stTextInput input:focus {
        border-color: var(--primary);
        box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.1);
    }

    /* Section Création (Always Visible) */
    .create-section {
        background: var(--card-bg);
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 20px;
        margin-bottom: 2rem;
    }
    .create-title {
        font-weight: 700;
        color: var(--text-main);
        margin-bottom: 1rem;
        display: flex;
        align-items: center;
        gap: 8px;
    }

    /* Tabs Custom */
    .stTabs [data-baseweb="tab-list"] { gap: 10px; border-bottom: 1px solid var(--border); }
    .stTabs [data-baseweb="tab"] {
        background: transparent; border: none; font-weight: 500; color: var(--text-muted);
    }
    .stTabs [aria-selected="true"] {
        color: var(--primary); border-bottom: 2px solid var(--primary);
    }

    /* Cartes Alertes */
    .alert-card {
        background: var(--card-bg);
        border: 1px solid var(--border);
        border-left: 4px solid var(--primary);
        border-radius: 8px;
        padding: 16px;
        margin-bottom: 12px;
        transition: transform 0.2s;
    }
    .alert-card:hover { border-color: var(--primary-dark); transform: translateY(-1px); box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05); }
    
    /* Badges */
    .badge-site { 
        font-size: 0.75rem; font-weight: 700; text-transform: uppercase; 
        padding: 2px 8px; border-radius: 4px; background: #e0e7ff; color: var(--primary-dark);
    }
    .badge-filter {
        display: inline-flex; align-items: center; background: #f1f5f9; color: #475569; 
        padding: 2px 8px; border-radius: 99px; font-size: 0.75rem; margin: 2px; border: 1px solid #e2e8f0;
    }
    
    /* Empty State */
    .empty-state {
        text-align: center; padding: 3rem 1rem; color: var(--text-muted);
        background: var(--bg-soft); border-radius: 12px; border: 2px dashed var(--border);
    }
</style>
""", unsafe_allow_html=True)

# ============== CONFIG & UTILS ==============
CONFIG_PATH = os.path.join(".", "config.json")
DEFAULT_CONFIG = {
    "alerts_path": "./AlertMe/alerts.jsonl",
    "max_alerts": 200,
    "ui": { "title": "AlertMe", "show_labels": True },
    "sites": [
        {"id": "immoweb", "host_contains": "immoweb.be"},
        {"id": "marjorietome", "host_contains": "immotoma.be"},
        {"id": "immokh", "host_contains": "immo-kh.be"},
        {"id": "adhome", "host_contains": "ad-home.be"}
    ],
    "scraper_defaults": { "pages": 20, "order_keys": ["newest"] }
}

# Chargement Config
def _load_cfg():
    if not os.path.isfile(CONFIG_PATH): return DEFAULT_CONFIG
    try:
        with open(CONFIG_PATH,"r",encoding="utf-8") as f: return json.load(f)
    except: return DEFAULT_CONFIG

CFG = _load_cfg()
ALERTS_PATH = CFG.get("alerts_path", "./AlertMe/alerts.jsonl")
MAX_ALERTS = int(CFG.get("max_alerts", 200))
SITES = CFG.get("sites", [])
BROWSER_SITES = {"immokh", "adhome"}
IMMOWEB_HOST = "www.immoweb.be"
IMMOKH_LIST = "https://www.immo-kh.be/fr/2/chercher-bien/a-vendre"
ADHOME_LIST = "https://www.ad-home.be/fr/2/chercher-bien/a-vendre"

# Github
def _sec(k):
    try: return st.secrets.get(k)
    except: return os.getenv(k)

def _gh_headers():
    tok = _sec("GH_TOKEN")
    if not tok: return None
    return {"Authorization": f"token {tok}","Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"}

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
    payload = {
        "message": msg,
        "content": base64.b64encode(content.encode()).decode(),
        "branch": branch
    }
    if sha: payload["sha"] = sha
    requests.put(f"https://api.github.com/repos/{repo}/contents/{path}", headers=headers, json=payload).raise_for_status()
    return True

# Helpers Métier
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
    if content:
        lines = content.splitlines()
    elif os.path.isfile(ALERTS_PATH):
        with open(ALERTS_PATH, "r", encoding="utf-8") as f: lines = f.readlines()
    else: lines = []
    
    for l in lines:
        if l.strip():
            try: raw.append(json.loads(l))
            except: pass
            
    # Reduction d'état
    state = {}
    for row in raw:
        if "action" not in row: continue # Skip old format for safety if strictly needed, or adapt
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
    st.markdown("###### 🎯 Critères de recherche")
    
    types = st.multiselect("Type de bien", IMMOKH_TYPES, default=[t for t in d.get("property_types",[]) if t in IMMOKH_TYPES], key=f"{key_prefix}_types")
    cities = st.text_input("Villes (ex: Namur, Jambes)", value=",".join(d.get("cities",[])), key=f"{key_prefix}_cities")
    
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

def filters_badges(f):
    if not f: return ""
    badges = []
    for t in f.get("property_types", []): badges.append(f"🏠 {t.capitalize()}")
    for c in f.get("cities", []): badges.append(f"📍 {c}")
    if f.get("price_max"): badges.append(f"💰 Max {f['price_max']}€")
    if f.get("bedrooms_min"): badges.append(f"🛏️ {f['bedrooms_min']}+ ch")
    return " ".join([f"<span class='badge-filter'>{b}</span>" for b in badges])

# ============== APP START ==============

if "alerts" not in st.session_state:
    st.session_state.alerts = load_alerts()

# --- 1. IDENTITÉ (Header) ---
st.markdown('<div class="identity-box">', unsafe_allow_html=True)
c_id_text, c_id_input = st.columns([1.5, 2])
with c_id_text:
    st.markdown("## 👋 Bonjour")
    st.markdown("Entrez votre email pour gérer vos alertes.")
with c_id_input:
    user_email = st.text_input("", placeholder="exemple@gmail.com", label_visibility="collapsed")
st.markdown('</div>', unsafe_allow_html=True)

is_email_valid = is_valid_email(user_email)
user_alerts = [a for a in st.session_state.alerts if a.get("email") == user_email.strip()] if is_email_valid else []

# --- 2. CRÉATION (Always Visible) ---
st.markdown('<div class="create-section">', unsafe_allow_html=True)
st.markdown(f'<div class="create-title">✨ Créer une nouvelle alerte {f"pour {user_email}" if is_email_valid else ""}</div>', unsafe_allow_html=True)

tab_iw, tab_mt, tab_ka = st.tabs(["Immoweb", "ImmoToma", "Immo-KH & AD-HOME"])

# > Immoweb
with tab_iw:
    with st.form("new_iw", clear_on_submit=True):
        u = st.text_input("URL de recherche Immoweb", placeholder="https://www.immoweb.be/fr/recherche/...")
        l = st.text_input("Nom de l'alerte (ex: Maison Bruxelles)", placeholder="Optionnel")
        submitted = st.form_submit_button("Ajouter l'alerte", use_container_width=True)
        
        if submitted:
            if not is_email_valid: st.error("Veuillez entrer un email valide en haut de page.")
            elif not host_ok("immoweb", u): st.warning("L'URL ne semble pas venir d'Immoweb.")
            else:
                try:
                    clean_url = canonicalize_immoweb_url(u)
                    rec = {"site":"immoweb","url":clean_url,"email":user_email.strip(),"label":l.strip(),"pages":20}
                    st.session_state.alerts.append(rec) # Optimistic UI
                    append_event("add", rec, "Add IW")
                    st.toast("Alerte Immoweb ajoutée !", icon="✅")
                    st.rerun()
                except Exception as e: st.error(f"Erreur URL: {e}")

# > Toma
with tab_mt:
    with st.form("new_mt", clear_on_submit=True):
        u = st.text_input("URL de recherche ImmoToma", placeholder="https://immotoma.be/advanced-search/...")
        l = st.text_input("Nom de l'alerte", placeholder="Optionnel")
        submitted = st.form_submit_button("Ajouter l'alerte", use_container_width=True)
        
        if submitted:
            if not is_email_valid: st.error("Veuillez entrer un email valide en haut de page.")
            elif not host_ok("marjorietome", u): st.warning("L'URL ne semble pas venir d'ImmoToma.")
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
        st.info("Crée deux alertes simultanées (Immo-KH et AD-HOME).")
        l = st.text_input("Nom global", placeholder="Ex: Terrains Namur")
        f_data = filters_ui(key_prefix="new_ka")
        submitted = st.form_submit_button("Ajouter les 2 alertes", use_container_width=True)
        
        if submitted:
            if not is_email_valid: st.error("Veuillez entrer un email valide en haut de page.")
            else:
                for s_id, s_url in [("immokh", IMMOKH_LIST), ("adhome", ADHOME_LIST)]:
                    rec = {"site":s_id,"url":s_url,"email":user_email.strip(),"label":l.strip(),"filters":f_data,"use_browser":True,"pages":20}
                    st.session_state.alerts.append(rec)
                    append_event("add", rec, f"Add {s_id}")
                st.toast("Alertes créées avec succès !", icon="🚀")
                st.rerun()

st.markdown('</div>', unsafe_allow_html=True)

# --- 3. LISTE FILTRÉE ---
st.subheader(f"📋 Mes Alertes Actives ({len(user_alerts)})")

if not is_email_valid:
    st.info("👆 Entrez votre email ci-dessus pour voir vos alertes.")
elif not user_alerts:
    st.markdown(f"""
    <div class="empty-state">
        <h3>Aucune alerte trouvée pour {user_email}</h3>
        <p>Utilisez le formulaire ci-dessus pour créer votre première surveillance.</p>
    </div>
    """, unsafe_allow_html=True)
else:
    for i, alert in enumerate(user_alerts):
        # Recherche de l'index réel dans la liste globale pour suppression correcte
        real_index = next((idx for idx, a in enumerate(st.session_state.alerts) 
                          if a == alert), None)
        
        if real_index is None: continue

        site = alert.get("site")
        label = alert.get("label") or "Sans titre"
        url = alert.get("url")
        filters = alert.get("filters")
        
        # Render Card
        with st.container():
            st.markdown('<div class="alert-card">', unsafe_allow_html=True)
            c1, c2 = st.columns([5, 1])
            
            with c1:
                st.markdown(f"**{label}** <span class='badge-site'>{site}</span>", unsafe_allow_html=True)
                if site in BROWSER_SITES:
                    st.markdown(filters_badges(filters), unsafe_allow_html=True)
                else:
                    st.caption(f"🔗 {url[:60]}...")
            
            with c2:
                if st.button("🗑️", key=f"del_{i}_{real_index}"):
                    st.session_state.alerts.pop(real_index)
                    # Reconstruction pour delete event
                    payload = {"site":site,"url":url}
                    if filters: payload["filters"] = filters
                    append_event("delete", payload, "User delete")
                    st.rerun()
                    
            st.markdown("</div>", unsafe_allow_html=True)
