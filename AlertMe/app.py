# streamlit_app.py
import os, json, re, base64, requests
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import streamlit as st

# ============== CONFIG & THEME ==============
st.set_page_config(
    page_title="AlertMe – Dashboard",
    page_icon="🔔",
    layout="centered",
    initial_sidebar_state="collapsed"
)

# CSS MODERNE & ÉPURÉ
st.markdown("""
<style>
    /* Variables globales */
    :root {
        --primary: #4f46e5;       /* Indigo 600 */
        --primary-light: #e0e7ff; /* Indigo 100 */
        --text-dark: #1e293b;     /* Slate 800 */
        --text-gray: #64748b;     /* Slate 500 */
        --bg-card: #ffffff;
        --border-color: #e2e8f0;  /* Slate 200 */
        --shadow-sm: 0 1px 2px 0 rgb(0 0 0 / 0.05);
        --shadow-md: 0 4px 6px -1px rgb(0 0 0 / 0.1);
    }

    /* Structure globale */
    .block-container { padding-top: 2rem; padding-bottom: 3rem; max-width: 800px; }
    h1, h2, h3 { color: var(--text-dark); font-weight: 700; letter-spacing: -0.025em; }
    
    /* Stylisation des Inputs Streamlit */
    .stTextInput input, .stNumberInput input {
        border-radius: 8px; border: 1px solid var(--border-color);
    }
    .stTextInput input:focus, .stNumberInput input:focus {
        border-color: var(--primary); box-shadow: 0 0 0 2px var(--primary-light);
    }
    
    /* Tabs personnalisés */
    .stTabs [data-baseweb="tab-list"] { gap: 8px; border-bottom: 1px solid var(--border-color); padding-bottom: 8px; }
    .stTabs [data-baseweb="tab"] {
        border-radius: 6px; padding: 8px 16px; font-weight: 500; color: var(--text-gray); border: none; background: transparent;
    }
    .stTabs [aria-selected="true"] {
        background-color: var(--primary-light); color: var(--primary); font-weight: 600;
    }

    /* Cartes d'alertes */
    .alert-card {
        background: var(--bg-card);
        border: 1px solid var(--border-color);
        border-radius: 12px;
        padding: 20px;
        margin-bottom: 16px;
        box-shadow: var(--shadow-sm);
        transition: all 0.2s ease;
        position: relative;
    }
    .alert-card:hover {
        box-shadow: var(--shadow-md);
        border-color: #cbd5e1;
    }
    .alert-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
    .alert-site { font-weight: 700; font-size: 1.1rem; color: var(--text-dark); display: flex; align-items: center; gap: 8px; }
    .alert-label { background: var(--primary-light); color: var(--primary); padding: 2px 8px; border-radius: 99px; font-size: 0.75rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
    .alert-details { font-size: 0.9rem; color: var(--text-gray); line-height: 1.5; }
    .alert-details strong { color: var(--text-dark); font-weight: 600; }
    
    /* Badges pour filtres */
    .filter-tag {
        display: inline-block; background: #f1f5f9; color: #475569; 
        padding: 2px 6px; border-radius: 4px; font-size: 0.8rem; margin-right: 4px; margin-bottom: 4px; border: 1px solid #e2e8f0;
    }

    /* Boutons */
    .stButton > button {
        border-radius: 8px; font-weight: 500; transition: all 0.2s; border: none;
    }
    .stButton > button:hover { transform: translateY(-1px); }
    
    /* Aide visuelle */
    .info-box { background: #f8fafc; border-left: 4px solid var(--primary); padding: 12px; border-radius: 0 8px 8px 0; color: var(--text-gray); font-size: 0.9rem; margin-bottom: 1rem; }
</style>
""", unsafe_allow_html=True)

# ============== LOGIQUE METIER (INCHANGÉE) ==============
CONFIG_PATH = os.path.join(".", "config.json")
DEFAULT_CONFIG = {
    "alerts_path": "./AlertMe/alerts.jsonl",
    "max_alerts": 200,
    "ui": { "title": "AlertMe", "subtitle": "Gestionnaire d'alertes immobilières", "show_labels": True },
    "sites": [
        {"id": "immoweb", "label": "Immoweb", "host_contains": "immoweb.be"},
        {"id": "marjorietome", "label": "ImmoToma", "host_contains": "immotoma.be"},
        {"id": "immokh", "label": "Immo-KH", "host_contains": "immo-kh.be"},
        {"id": "adhome", "label": "AD-HOME", "host_contains": "ad-home.be"}
    ],
    "scraper_defaults": { "pages": 20, "order_keys": ["newest","most_recent"] }
}

def _load_cfg():
    if not os.path.isfile(CONFIG_PATH): return DEFAULT_CONFIG
    try:
        with open(CONFIG_PATH,"r",encoding="utf-8") as f: user = json.load(f)
        def merge(a,b):
            if isinstance(a,dict) and isinstance(b,dict):
                z=dict(a)
                for k,v in b.items(): z[k]=merge(a.get(k),v) if k in a else v
                return z
            return b if b is not None else a
        return merge(DEFAULT_CONFIG,user)
    except Exception: return DEFAULT_CONFIG

CFG = _load_cfg()
ALERTS_PATH = CFG["alerts_path"]
MAX_ALERTS = int(CFG["max_alerts"])
SHOW_LABELS = bool(CFG.get("ui",{}).get("show_labels",True))
SITES = CFG.get("sites",[])
ORDER_KEYS = CFG.get("scraper_defaults",{}).get("order_keys",["newest","most_recent"])
DEFAULT_PAGES = int(CFG.get("scraper_defaults",{}).get("pages",20))
IMMOWEB_HOST = "www.immoweb.be"
IMMOKH_LIST = "https://www.immo-kh.be/fr/2/chercher-bien/a-vendre"
ADHOME_LIST = "https://www.ad-home.be/fr/2/chercher-bien/a-vendre"
BROWSER_SITES = {"immokh", "adhome"}

# Github Helper
def _sec(k):
    try: return st.secrets.get(k)
    except Exception: return None

def _gh_token(): return _sec("GH_TOKEN") or os.getenv("GH_TOKEN")
def _gh_repo_cfg():
    return (
        _sec("GH_REPO") or os.getenv("GH_REPO","ParraLuca/AlertMe"),
        _sec("GH_PATH") or os.getenv("GH_PATH","AlertMe/alerts.jsonl"),
        _sec("GH_BRANCH") or os.getenv("GH_BRANCH","main")
    )
def _gh_headers():
    tok=_gh_token()
    if not tok: raise RuntimeError("GH_TOKEN manquant.")
    return {"Authorization": f"token {tok}","Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"}

def gh_get_file():
    repo,path,branch = _gh_repo_cfg()
    r=requests.get(f"https://api.github.com/repos/{repo}/contents/{path}", headers=_gh_headers(), params={"ref":branch})
    if r.status_code==404: return None,None
    r.raise_for_status()
    data=r.json()
    return base64.b64decode(data["content"]).decode("utf-8"), data["sha"]

def gh_put_file(text, message):
    repo,path,branch = _gh_repo_cfg()
    _,sha = gh_get_file()
    payload={"message":message,"content":base64.b64encode(text.encode()).decode(),"branch":branch}
    if sha: payload["sha"]=sha
    r=requests.put(f"https://api.github.com/repos/{repo}/contents/{path}", headers=_gh_headers(), json=payload)
    r.raise_for_status(); return r.json()

def gh_append_line(line_text, message):
    current,sha = gh_get_file()
    if current is None: return gh_put_file(line_text+"\n", message)
    if not current.endswith("\n"): current+="\n"
    new_text=current+line_text+"\n"
    repo,path,branch = _gh_repo_cfg()
    payload={"message":message,"content":base64.b64encode(new_text.encode()).decode(),"branch":branch,"sha":sha}
    r=requests.put(f"https://api.github.com/repos/{repo}/contents/{path}", headers=_gh_headers(), json=payload)
    r.raise_for_status(); return r.json()

# Validations
def is_valid_email(s:str)->bool: return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", s.strip()))
def utc_iso(): return datetime.now(timezone.utc).isoformat()

def canonicalize_immoweb_url(u_in:str)->str:
    u=urlparse(u_in)
    if IMMOWEB_HOST not in (u.netloc or ""): raise ValueError("URL Immoweb invalide.")
    q=parse_qs(u.query); q["orderBy"]=[ORDER_KEYS[0] if ORDER_KEYS else "newest"]; q.pop("page",None)
    return urlunparse((u.scheme,u.netloc,u.path,u.params, urlencode({k:v[0] for k,v in q.items()}), u.fragment))

def canonicalize_marjorietome_url(u_in:str)->str:
    u=urlparse(u_in); q=parse_qs(u.query); q.pop("paged",None)
    return urlunparse((u.scheme,u.netloc,u.path,u.params, urlencode({k:v[0] for k,v in q.items()}), u.fragment))

def host_ok_for_site(site_id:str, user_url:str)->bool:
    if site_id.lower() in BROWSER_SITES: return True
    try: host=(urlparse(user_url).netloc or "").lower()
    except Exception: return False
    for s in SITES:
        if s.get("id")==site_id:
            needle=(s.get("host_contains") or "").lower().strip()
            return (needle in host) if needle else True
    return True

# Journaling
def make_event(action:str, alert:dict)->dict:
    ev={"ts":utc_iso(),"action":action,"alert":{}}
    for k in ("site","url","email","label","pages","filters","use_browser"):
        if k in alert and alert[k] not in (None,""): ev["alert"][k]=alert[k]
    return ev

def append_event(action:str, alert:dict, commit_message:str):
    ev=make_event(action,alert); line=json.dumps(ev, ensure_ascii=False)
    if _gh_token():
        try: return gh_append_line(line, commit_message)
        except Exception as e: st.error(f"Écriture GitHub échouée: {e}"); return None
    os.makedirs(os.path.dirname(ALERTS_PATH) or ".", exist_ok=True)
    with open(ALERTS_PATH,"a",encoding="utf-8") as f: f.write(line+"\n")
    return True

def _reduce_events_to_state(lines:list[dict])->list[dict]:
    state={}
    for row in lines:
        if not isinstance(row,dict): continue
        # Retro-compatibilité
        if "action" not in row or "alert" not in row:
            a=row; site=(a.get("site") or "immoweb").strip().lower()
            url=(a.get("url","") or "").strip()
            if site=="immokh": url=IMMOKH_LIST
            if site=="adhome": url=ADHOME_LIST
            key=f"{site}|{url}"
            rec={"site":site,"url":url,"email":(a.get("email","") or "").strip()}
            if SHOW_LABELS: rec["label"]=(a.get("label","") or "").strip()
            if a.get("pages") is not None: rec["pages"]=int(a["pages"])
            if a.get("use_browser") is not None: rec["use_browser"]=bool(a["use_browser"])
            if site in BROWSER_SITES and a.get("filters") is not None:
                rec["filters"]=a["filters"]; key += "|"+json.dumps(a["filters"], sort_keys=True, ensure_ascii=False)
            state[key]=rec
            continue
        # Nouveau format
        action=(row.get("action") or "").strip().lower()
        a=row.get("alert") or {}
        site=(a.get("site") or "immoweb").strip().lower()
        url=(a.get("url","") or "").strip()
        if site=="immokh": url=IMMOKH_LIST
        if site=="adhome": url=ADHOME_LIST
        filters=a.get("filters"); fkey=json.dumps(filters, sort_keys=True, ensure_ascii=False) if filters else ""
        if action in {"add","update"}:
            key=f"{site}|{url}"
            rec={"site":site,"url":url,"email":(a.get("email","") or "").strip()}
            if SHOW_LABELS: rec["label"]=(a.get("label","") or "").strip()
            if a.get("pages") is not None: rec["pages"]=int(a["pages"])
            if a.get("use_browser") is not None: rec["use_browser"]=bool(a["use_browser"])
            if filters is not None: rec["filters"]=filters; key += f"|{fkey}"
            state[key]=rec
        elif action=="delete":
            key=f"{site}|{url}"
            if fkey: key += f"|{fkey}"
            state.pop(key, None)
    return list(state.values())

def load_alerts():
    raw=[]
    if _gh_token():
        try:
            content,_=gh_get_file()
            if content:
                for line in content.splitlines():
                    t=line.strip()
                    if not t: continue
                    try: raw.append(json.loads(t))
                    except json.JSONDecodeError: pass
            return _reduce_events_to_state(raw)
        except Exception as e: st.error(f"Lecture GitHub échouée: {e}"); return []
    if not os.path.isfile(ALERTS_PATH): return []
    with open(ALERTS_PATH,"r",encoding="utf-8") as f:
        for line in f:
            t=line.strip()
            if not t: continue
            try: raw.append(json.loads(t))
            except json.JSONDecodeError: pass
    return _reduce_events_to_state(raw)

# ============== UI HELPERS ==============
IMMOKH_TYPES = [
    "maison","appartement","duplex","penthouse","terrain",
    "villa","studio","immeuble","commerce","bureau","industriel","garage"
]

def filters_summary_html(filters:dict|None)->str:
    """Génère des badges HTML pour les filtres"""
    if not filters: return "<span class='text-muted'>Aucun filtre spécifique</span>"
    parts=[]
    if filters.get("property_types"): 
        for t in filters["property_types"]: parts.append(f"<span class='filter-tag'>{t.capitalize()}</span>")
    if filters.get("cities"): 
        for c in filters["cities"]: parts.append(f"<span class='filter-tag'>📍 {c}</span>")
    
    p_min = filters.get("price_min", 0)
    p_max = filters.get("price_max", 0)
    if p_min > 0 or p_max > 0:
        txt_price = f"{p_min}€ → {p_max if p_max > 0 else '∞'}"
        parts.append(f"<span class='filter-tag'>💰 {txt_price}</span>")
        
    if filters.get("area_min"): parts.append(f"<span class='filter-tag'>📐 ≥{filters['area_min']} m²</span>")
    if filters.get("bedrooms_min"): parts.append(f"<span class='filter-tag'>🛏️ ≥{filters['bedrooms_min']} ch.</span>")
    if filters.get("bathrooms_min"): parts.append(f"<span class='filter-tag'>🚿 ≥{filters['bathrooms_min']} sdb</span>")
    
    return "".join(parts) if parts else "<span class='text-muted'>—</span>"

def immokh_adhome_filters_ui(default:dict|None=None):
    """UI optimisée pour les filtres"""
    d = default or {}
    
    st.markdown("##### 🛠️ Configuration des critères")
    
    # 1. Types de biens (Multiselect au lieu de cases à cocher)
    default_types = d.get("property_types") or ["maison","appartement","penthouse","terrain"]
    # Nettoyage si des types inconnus sont dans la config
    valid_defaults = [t for t in default_types if t in IMMOKH_TYPES]
    
    selected_types = st.multiselect(
        "Types de biens recherchés",
        options=IMMOKH_TYPES,
        default=valid_defaults,
        format_func=lambda x: x.capitalize(),
        key="khadh_types_multi"
    )
    
    # 2. Localisation
    cities_txt = st.text_input(
        "Villes / Communes (séparées par virgule)", 
        value=",".join(d.get("cities", [])),
        placeholder="Ex: Tamines, Aiseau-Presles...",
        help="Laissez vide pour toute la zone couverte par l'agence."
    )
    
    st.markdown("---")
    
    # 3. Critères numériques regroupés
    c1, c2 = st.columns(2)
    with c1:
        price_min = st.number_input("Prix Min (€)", 0, step=5000, value=int(d.get("price_min",0)))
        area_min = st.number_input("Surface Min (m²)", 0, step=10, value=int(d.get("area_min",0)))
        bedrooms_min = st.number_input("Chambres Min", 0, step=1, value=int(d.get("bedrooms_min",0)))
    with c2:
        price_max = st.number_input("Prix Max (€)", 0, step=5000, value=int(d.get("price_max",0)), help="0 = Pas de limite")
        # Placeholder pour alignement ou autre
        st.write("") # Spacer
        bathrooms_min = st.number_input("Salles de bain Min", 0, step=1, value=int(d.get("bathrooms_min",0)))

    return {
        "property_types": selected_types,
        "cities": [c.strip() for c in (cities_txt or "").split(",") if c.strip()],
        "price_min": int(price_min),
        "price_max": int(price_max),
        "bedrooms_min": int(bedrooms_min),
        "bathrooms_min": int(bathrooms_min),
        "area_min": int(area_min),
        "include_sold": False # Fixe
    }

# ============== MAIN APP ==============
if "alerts" not in st.session_state:
    st.session_state.alerts = load_alerts()

# HEADER
c_title, c_stat = st.columns([3, 1])
with c_title:
    st.title("🔔 AlertMe")
    st.caption("Tableau de bord de surveillance immobilière")
with c_stat:
    nb = len(st.session_state.alerts)
    st.metric("Alertes Actives", f"{nb}", delta=f"{MAX_ALERTS - nb} slots restants", delta_color="normal")

# SECTION: CRÉATION D'ALERTE
with st.expander("➕ Créer une nouvelle alerte", expanded=(len(st.session_state.alerts) == 0)):
    tab_iw, tab_mt, tab_khadh = st.tabs(["🏠 Immoweb", "🏷️ ImmoToma", "🚀 Immo-KH + AD-HOME"])

    # ---- Immoweb ----
    with tab_iw:
        with st.form("form_immoweb", clear_on_submit=True):
            st.markdown("#### Nouvelle surveillance Immoweb")
            st.markdown("<div class='info-box'>Rendez-vous sur Immoweb, faites votre recherche avec vos filtres, puis copiez l'URL ici.</div>", unsafe_allow_html=True)
            
            col_u, col_e = st.columns([2, 1])
            with col_u:
                url = st.text_input("URL de recherche", placeholder="https://www.immoweb.be/fr/recherche/...")
            with col_e:
                email = st.text_input("Email de notification", placeholder="vous@email.com")
                
            c3, c4 = st.columns(2)
            with c3:
                label = st.text_input("Libellé (Optionnel)", placeholder="Ex: Maisons Bruxelles")
            with c4:
                pages = st.number_input("Profondeur (Pages max)", 1, 200, DEFAULT_PAGES)
            
            if st.form_submit_button("✨ Activer cette alerte", use_container_width=True):
                if not url.strip(): st.error("L'URL est obligatoire.")
                elif not email.strip() or not is_valid_email(email): st.error("Email invalide.")
                elif not host_ok_for_site("immoweb", url): st.error("L'URL ne correspond pas à Immoweb.")
                else:
                    try:
                        canon = canonicalize_immoweb_url(url.strip())
                        rec = {"site":"immoweb","url":canon,"email":email.strip(),"pages":int(pages), "label":label.strip()}
                        # Logic update/add
                        key=f"immoweb|{canon}"
                        idx=next((i for i,a in enumerate(st.session_state.alerts) if f"{a.get('site')}|{a.get('url')}"==key), None)
                        if idx is not None:
                            st.session_state.alerts[idx]=rec
                            append_event("update", rec, "Update Immoweb")
                            st.toast("Alerte Immoweb mise à jour !", icon="🔄")
                        else:
                            st.session_state.alerts.append(rec)
                            append_event("add", rec, "Add Immoweb")
                            st.toast("Alerte Immoweb créée !", icon="✅")
                    except Exception as e: st.error(f"Erreur: {e}")

    # ---- ImmoToma ----
    with tab_mt:
        with st.form("form_marjorietome", clear_on_submit=True):
            st.markdown("#### Nouvelle surveillance ImmoToma")
            st.markdown("<div class='info-box'>Copiez l'URL de recherche depuis le site ImmoToma.</div>", unsafe_allow_html=True)
            
            col_u, col_e = st.columns([2, 1])
            with col_u:
                url = st.text_input("URL de recherche", placeholder="https://immotoma.be/advanced-search/...")
            with col_e:
                email = st.text_input("Email", placeholder="vous@email.com")
            
            c3, c4 = st.columns(2)
            with c3:
                label = st.text_input("Libellé", placeholder="Ex: Projets Toma")
            with c4:
                pages = st.number_input("Profondeur", 1, 200, DEFAULT_PAGES)
                
            if st.form_submit_button("✨ Activer cette alerte", use_container_width=True):
                if not url.strip(): st.error("L'URL est obligatoire.")
                elif not email.strip() or not is_valid_email(email): st.error("Email invalide.")
                elif not host_ok_for_site("marjorietome", url): st.error("L'URL ne correspond pas à ImmoToma.")
                else:
                    try:
                        canon = canonicalize_marjorietome_url(url.strip())
                        rec = {"site":"marjorietome","url":canon,"email":email.strip(),"pages":int(pages), "label":label.strip()}
                        # Logic update/add similar to Immoweb
                        key=f"marjorietome|{canon}"
                        idx=next((i for i,a in enumerate(st.session_state.alerts) if f"{a.get('site')}|{a.get('url')}"==key), None)
                        if idx is not None:
                            st.session_state.alerts[idx]=rec
                            append_event("update", rec, "Update ImmoToma")
                            st.toast("Alerte ImmoToma mise à jour !", icon="🔄")
                        else:
                            st.session_state.alerts.append(rec)
                            append_event("add", rec, "Add ImmoToma")
                            st.toast("Alerte ImmoToma créée !", icon="✅")
                    except Exception as e: st.error(f"Erreur: {e}")

    # ---- KH + AD-HOME ----
    with tab_khadh:
        with st.form("form_kh_adhome", clear_on_submit=True):
            st.markdown("#### Mode Multi-Agence (Immo-KH & AD-HOME)")
            st.info("💡 Ce formulaire crée automatiquement **deux alertes distinctes** (une pour chaque agence) avec les mêmes critères. Le navigateur interne sera utilisé pour récupérer les données dynamiques.")
            
            ce1, ce2, ce3 = st.columns([2, 1, 1])
            with ce1: email = st.text_input("Email de notification", placeholder="vous@email.com")
            with ce2: label = st.text_input("Libellé global", placeholder="Ex: Biens Namur")
            with ce3: pages = st.number_input("Profondeur (Clics)", 1, 200, DEFAULT_PAGES)
            
            filters_payload = immokh_adhome_filters_ui(default={"price_min":0,"bedrooms_min":0,"bathrooms_min":0,"area_min":0,"include_sold":False})
            
            st.write("")
            if st.form_submit_button("✨ Créer les 2 alertes synchronisées", use_container_width=True):
                if not email.strip() or not is_valid_email(email):
                    st.error("Email invalide.")
                else:
                    try:
                        # Logic logic logic... loop for both sites
                        targets = [("immokh", IMMOKH_LIST), ("adhome", ADHOME_LIST)]
                        for s_id, s_url in targets:
                            rec = {
                                "site": s_id, "url": s_url, "email": email.strip(),
                                "pages": int(pages), "use_browser": True, "filters": filters_payload,
                                "label": label.strip()
                            }
                            fkey=json.dumps(filters_payload, sort_keys=True, ensure_ascii=False)
                            key=f"{s_id}|{s_url}|{fkey}"
                            # Check existence
                            idx=next((i for i,a in enumerate(st.session_state.alerts)
                                      if (f"{a.get('site')}|{a.get('url')}|"+json.dumps(a.get('filters') or {}, sort_keys=True, ensure_ascii=False))==key), None)
                            
                            msg_action = "Update" if idx is not None else "Add"
                            if idx is not None: st.session_state.alerts[idx] = rec
                            else: st.session_state.alerts.append(rec)
                            append_event(msg_action.lower(), rec, f"{msg_action} {s_id}")
                        
                        st.toast("Configuration appliquée aux deux agences avec succès !", icon="🚀")
                    except Exception as e: st.error(f"Erreur: {e}")

# SECTION: LISTE DES ALERTES
st.markdown("### 📡 Vos surveillances actives")
st.markdown("---")

if not st.session_state.alerts:
    st.info("Aucune alerte configurée pour le moment. Utilisez le panneau ci-dessus pour commencer.")

alerts = st.session_state.alerts

# AFFICHAGE DES CARTES
for i, a in enumerate(alerts):
    site = a.get("site","immoweb")
    url = a.get("url","")
    email = a.get("email","")
    label = a.get("label","")
    filters = a.get("filters")
    pages = a.get("pages")
    
    # Icônes et noms stylisés
    site_map = {
        "immoweb": ("🏠", "Immoweb"),
        "marjorietome": ("🏷️", "ImmoToma"),
        "immokh": ("🏡", "Immo-KH"),
        "adhome": ("🔑", "AD-HOME")
    }
    icon, site_nice = site_map.get(site, ("🌐", site))
    
    # Container de la carte
    with st.container():
        st.markdown(f"""
        <div class="alert-card">
            <div class="alert-header">
                <div class="alert-site">
                    <span>{icon} {site_nice}</span>
                    {f'<span class="alert-label">{label}</span>' if label else ''}
                </div>
                <div style="color:var(--text-gray); font-size:0.8rem;">Max {pages} pages</div>
            </div>
            <div class="alert-details">
                <div style="margin-bottom:4px;">📧 <strong>{email}</strong></div>
        """, unsafe_allow_html=True)
        
        if site in BROWSER_SITES:
            # Affichage des badges de filtres
            html_filters = filters_summary_html(filters)
            st.markdown(f"<div style='margin-top:8px;'>{html_filters}</div>", unsafe_allow_html=True)
        else:
            # Affichage URL tronquée
            short_url = (url[:60] + '...') if len(url) > 60 else url
            st.markdown(f"<div style='font-family:monospace; font-size:0.8rem; color:#64748b; word-break:break-all;' title='{url}'>{short_url}</div>", unsafe_allow_html=True)

        st.markdown("</div>", unsafe_allow_html=True) # Fin contenu HTML statique
        
        # Actions (Edit/Delete)
        c_edit, c_del = st.columns([1, 4]) # Colonnes étroites pour boutons
        
        # EDIT MODE (Expander intégré à la "carte" visuellement)
        with st.expander("⚙️ Modifier / Détails"):
            with st.form(f"edit_form_{i}"):
                st.caption("Certains paramètres (comme le site) ne sont pas modifiables.")
                new_email = st.text_input("Email", value=email)
                new_pages = st.number_input("Pages", 1, 200, int(pages or DEFAULT_PAGES))
                new_label = st.text_input("Libellé", value=label)
                
                new_filters = None
                new_url = url
                
                if site in BROWSER_SITES:
                    st.markdown("**Filtres actifs :**")
                    new_filters = immokh_adhome_filters_ui(default=filters)
                else:
                    new_url = st.text_input("URL", value=url)
                
                if st.form_submit_button("💾 Enregistrer les modifications"):
                    try:
                        if not is_valid_email(new_email): st.warning("Email invalide.")
                        elif site not in BROWSER_SITES and not host_ok_for_site(site, new_url): st.warning("URL invalide.")
                        else:
                            edited = dict(a)
                            edited.update({"email": new_email, "pages": int(new_pages), "label": new_label})
                            
                            if site in BROWSER_SITES:
                                edited["filters"] = new_filters
                            else:
                                edited["url"] = canonicalize_immoweb_url(new_url) if site=="immoweb" else canonicalize_marjorietome_url(new_url)
                            
                            st.session_state.alerts[i] = edited
                            append_event("update", edited, "Inline Edit UI")
                            st.toast("Modification enregistrée !", icon="💾")
                            st.rerun()
                    except Exception as e: st.error(f"Erreur: {e}")

        # DELETE BUTTON (Extérieur à l'expander pour accès rapide)
        # On utilise un petit hack visuel pour placer le bouton supprimer proprement
        st.markdown("<div style='margin-top:-45px; float:right; position:relative; z-index:2;'>", unsafe_allow_html=True)
        if st.button("🗑️", key=f"del_btn_{i}", help="Supprimer cette alerte"):
            payload={"site":site,"url":url}
            if site in BROWSER_SITES and filters: payload["filters"]=filters
            append_event("delete", payload, "Delete alert UI")
            st.session_state.alerts.pop(i)
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)
        
        st.markdown("</div>", unsafe_allow_html=True) # Fin carte wrapper
