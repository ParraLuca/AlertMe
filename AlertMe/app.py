# streamlit_app.py
import os, json, re, base64, requests
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import streamlit as st

# ============== THEME / PAGE ==============
st.set_page_config(page_title="AlertMe – Gestion des alertes", page_icon="🔔", layout="centered")
st.markdown("""
<style>
:root {
  --pri:#4f46e5;       /* indigo */
  --pri-2:#eef2ff;     /* indigo-50 */
  --acc:#10b981;       /* emerald */
  --txt:#0f172a;       /* slate-900 */
  --mut:#64748b;       /* slate-500 */
  --bg:#ffffff;
  --card:#f8fafc;      /* slate-50 */
  --border:#e2e8f0;    /* slate-200 */
}
html, body, [class^="css"]  { color: var(--txt); }
h1, h2, h3, .stTabs [data-baseweb="tab"], .stButton>button { font-weight: 600; }
.stTabs [data-baseweb="tab-list"] { gap: 6px; }
.stTabs [data-baseweb="tab"] {
  border-radius: 10px; background: var(--card); border: 1px solid var(--border);
}
.stTabs [aria-selected="true"] {
  background: var(--pri-2) !important; border-color: var(--pri) !important; color: var(--pri) !important;
}
.stButton>button {
  background: var(--pri); color: white; border-radius: 10px; border: 0; padding: 0.5rem 0.9rem;
}
.stButton>button:hover { filter: brightness(0.95); }
div[role="group"] > div { padding: .25rem .25rem .25rem 0; }
.block-container { padding-top: 1.2rem; }
.card {
  border:1px solid var(--border); background: var(--card);
  border-radius:14px; padding:14px 16px; margin-bottom:12px;
}
.badge { display:inline-block; padding:.2rem .6rem; border-radius:9999px; background:var(--pri-2); color:var(--pri); font-size:.85rem; }
.help { color: var(--mut); font-size:.9rem; }
</style>
""", unsafe_allow_html=True)

# ============== CONFIG ==============
CONFIG_PATH = os.path.join(".", "config.json")
DEFAULT_CONFIG = {
    "alerts_path": "./AlertMe/alerts.jsonl",
    "max_alerts": 200,
    "ui": {
        "title": "AlertMe – Gestion des alertes",
        "subtitle": "Immoweb / ImmoToma via URL; Immo-KH + AD-HOME via filtres dédiés.",
        "show_labels": True
    },
    "sites": [
        {"id": "immoweb",      "label": "Immoweb",                  "host_contains": "immoweb.be"},
        {"id": "marjorietome", "label": "ImmoToma (Marjorie Toma)", "host_contains": "immotoma.be"},
        {"id": "immokh",       "label": "Immo-KH",                  "host_contains": "immo-kh.be"},
        {"id": "adhome",       "label": "AD-HOME",                  "host_contains": "ad-home.be"}
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
    except Exception:
        return DEFAULT_CONFIG

CFG = _load_cfg()
ALERTS_PATH   = CFG["alerts_path"]
MAX_ALERTS    = int(CFG["max_alerts"])
SHOW_LABELS   = bool(CFG.get("ui",{}).get("show_labels",True))
SITES         = CFG.get("sites",[])
ORDER_KEYS    = CFG.get("scraper_defaults",{}).get("order_keys",["newest","most_recent"])
DEFAULT_PAGES = int(CFG.get("scraper_defaults",{}).get("pages",20))
IMMOWEB_HOST  = "www.immoweb.be"

# URLs fixes pour sites sans URL côté UI
IMMOKH_LIST   = "https://www.immo-kh.be/fr/2/chercher-bien/a-vendre"
ADHOME_LIST   = "https://www.ad-home.be/fr/2/chercher-bien/a-vendre"
BROWSER_SITES = {"immokh", "adhome"}  # sites avec navigateur forcé et filtres internes

# ============== GITHUB SECRETS (safe) ==============
def _sec(k):
    try: return st.secrets.get(k)  # type: ignore[attr-defined]
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
    if current is None:
        return gh_put_file(line_text+"\n", message)
    if not current.endswith("\n"): current+="\n"
    new_text=current+line_text+"\n"
    repo,path,branch = _gh_repo_cfg()
    payload={"message":message,"content":base64.b64encode(new_text.encode()).decode(),"branch":branch,"sha":sha}
    r=requests.put(f"https://api.github.com/repos/{repo}/contents/{path}", headers=_gh_headers(), json=payload)
    r.raise_for_status(); return r.json()

# ============== UTILS / CANONICALISATION ==============
def is_valid_email(s:str)->bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", s.strip()))

def utc_iso(): return datetime.now(timezone.utc).isoformat()

def canonicalize_immoweb_url(u_in:str)->str:
    u=urlparse(u_in)
    if IMMOWEB_HOST not in (u.netloc or ""): raise ValueError("URL Immoweb invalide.")
    q=parse_qs(u.query); q["orderBy"]=[ORDER_KEYS[0] if ORDER_KEYS else "newest"]; q.pop("page",None)
    return urlunparse((u.scheme,u.netloc,u.path,u.params, urlencode({k:v[0] for k,v in q.items()}), u.fragment))

def canonicalize_marjorietome_url(u_in:str)->str:
    u=urlparse(u_in); q=parse_qs(u.query); q.pop("paged",None)
    return urlunparse((u.scheme,u.netloc,u.path,u.params, urlencode({k:v[0] for k,v in q.items()}), u.fragment))

def canonicalize_generic_url(u_in:str)->str:
    u=urlparse(u_in or ""); q=parse_qs(u.query)
    for k in ("page","paged"): q.pop(k, None)
    return urlunparse((u.scheme or "https", u.netloc, u.path, u.params, urlencode({k:(v[0] if isinstance(v,list) and v else v) for k,v in q.items()}), u.fragment))

def host_ok_for_site(site_id:str, user_url:str)->bool:
    # Sites sans URL côté UI : toujours OK
    if site_id.lower() in BROWSER_SITES: 
        return True
    try: host=(urlparse(user_url).netloc or "").lower()
    except Exception: return False
    for s in SITES:
        if s.get("id")==site_id:
            needle=(s.get("host_contains") or "").lower().strip()
            return (needle in host) if needle else True
    return True

# ============== JOURNAL (alerts.jsonl) ==============
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
        # Ancien format
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
        except Exception as e:
            st.error(f"Lecture GitHub échouée: {e}"); return []
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

def filters_summary_str(filters:dict|None)->str:
    if not filters: return "—"
    parts=[]
    if filters.get("property_types"): parts.append("Types: " + ", ".join(filters["property_types"]))
    if filters.get("cities"): parts.append("Villes: " + ", ".join(filters["cities"]))
    if (filters.get("price_min") is not None) or (filters.get("price_max") is not None):
        parts.append(f"Prix: {filters.get('price_min','—')}→{filters.get('price_max','—')}")
    if filters.get("area_min") is not None: parts.append(f"≥{filters['area_min']} m²")
    if filters.get("bedrooms_min") is not None: parts.append(f"≥{filters['bedrooms_min']} ch.")
    if filters.get("bathrooms_min") is not None: parts.append(f"≥{filters['bathrooms_min']} sdb")
    return " · ".join(parts) if parts else "—"

def checkbox_grid(options:list[str], defaults:list[str], key_prefix:str)->list[str]:
    cols = st.columns(3)
    selected=set(defaults)
    for i,opt in enumerate(options):
        with cols[i%3]:
            checked = st.checkbox(opt.capitalize(), value=(opt in defaults), key=f"{key_prefix}_{i}")
            if checked: selected.add(opt)
            else: selected.discard(opt)
    return sorted(selected)

def immokh_adhome_filters_ui(default:dict|None=None):
    d = default or {}
    st.markdown("#### Filtres Immo-KH & AD-HOME")
    st.markdown('<span class="help">Aucune URL nécessaire. Le navigateur (Playwright) est utilisé automatiquement pour les deux sites.</span>', unsafe_allow_html=True)

    # Types
    default_types = d.get("property_types") or ["maison","appartement","penthouse","terrain"]
    property_types = checkbox_grid(IMMOKH_TYPES, default_types, "khadh_types")

    # Villes
    cities_txt = st.text_input("Villes (séparées par des virgules)", value=",".join(d.get("cities", [])),
                               placeholder="ex: Tamines, Aiseau-Presles, Fosses-la-Ville")

    # Min/Max — tous les 'min' initialisés à 0 (modifiable)
    colA, colB = st.columns(2)
    with colA:
        price_min     = st.number_input("Prix min (€)", min_value=0, step=1000, value=0, key="khadh_price_min")
        bedrooms_min  = st.number_input("Chambres min", min_value=0, step=1, value=0, key="khadh_bed_min")
        area_min      = st.number_input("Surface min (m²)", min_value=0, step=5,  value=0, key="khadh_area_min")
    with colB:
        price_max     = st.number_input("Prix max (€)", min_value=0, step=1000, value=int(d.get("price_max") or 0), key="khadh_price_max")
        bathrooms_min = st.number_input("Salles de bains min", min_value=0, step=1, value=0, key="khadh_bath_min")

    st.markdown('<span class="badge">Biens vendus exclus</span> <span class="help">(fixe)</span>', unsafe_allow_html=True)

    return {
        "property_types": property_types,
        "cities": [c.strip() for c in (cities_txt or "").split(",") if c.strip()],
        "price_min": int(price_min) if price_min is not None else 0,
        "price_max": int(price_max) if price_max is not None else 0,
        "bedrooms_min": int(bedrooms_min) if bedrooms_min is not None else 0,
        "bathrooms_min": int(bathrooms_min) if bathrooms_min is not None else 0,
        "area_min": int(area_min) if area_min is not None else 0,
        "include_sold": False
    }

# ============== HEADER ==============
st.title("🔔 " + CFG["ui"]["title"])
st.caption(CFG["ui"]["subtitle"])

if "alerts" not in st.session_state:
    st.session_state.alerts = load_alerts()

# ============== TABS ==============
tab_iw, tab_mt, tab_khadh = st.tabs(["🏠 Immoweb", "🏷️ ImmoToma", "🏡 Immo-KH + AD-HOME"])

# ---- Immoweb (URL obligé) ----
with tab_iw:
    with st.form("form_immoweb", clear_on_submit=True):
        st.subheader("Créer une alerte Immoweb")
        url = st.text_input("URL Immoweb (avec vos filtres)", placeholder="https://www.immoweb.be/fr/recherche/...")
        email = st.text_input("Email", placeholder="ex: prenom.nom@gmail.com")
        pages = st.number_input("Pages max à collecter", min_value=1, max_value=200, value=DEFAULT_PAGES, step=1)
        label = st.text_input("Label (facultatif)") if SHOW_LABELS else ""
        ok = st.form_submit_button("Enregistrer")
        if ok:
            if not url.strip():
                st.error("L’URL est requise.")
            elif not email.strip() or not is_valid_email(email):
                st.error("Email invalide.")
            elif not host_ok_for_site("immoweb", url.strip()):
                st.error("URL incohérente avec Immoweb.")
            else:
                try:
                    canon = canonicalize_immoweb_url(url.strip())
                    rec={"site":"immoweb","url":canon,"email":email.strip(),"pages":int(pages)}
                    if SHOW_LABELS: rec["label"]=label.strip()
                    key=f"immoweb|{canon}"
                    idx=next((i for i,a in enumerate(st.session_state.alerts) if f"{a.get('site')}|{a.get('url')}"==key), None)
                    if idx is not None:
                        st.session_state.alerts[idx]=rec
                        append_event("update", rec, "Update Immoweb")
                    else:
                        st.session_state.alerts.append(rec)
                        append_event("add", rec, "Add Immoweb")
                    st.success("Alerte Immoweb enregistrée ✅")
                except Exception as e:
                    st.error(f"Erreur: {e}")

# ---- ImmoToma (URL obligé) ----
with tab_mt:
    with st.form("form_marjorietome", clear_on_submit=True):
        st.subheader("Créer une alerte ImmoToma (Marjorie Toma)")
        url = st.text_input("URL ImmoToma (avec vos filtres)", placeholder="https://immotoma.be/advanced-search/?...")
        email = st.text_input("Email", placeholder="ex: prenom.nom@gmail.com")
        pages = st.number_input("Pages max à collecter", min_value=1, max_value=200, value=DEFAULT_PAGES, step=1)
        label = st.text_input("Label (facultatif)") if SHOW_LABELS else ""
        ok = st.form_submit_button("Enregistrer")
        if ok:
            if not url.strip():
                st.error("L’URL est requise.")
            elif not email.strip() or not is_valid_email(email):
                st.error("Email invalide.")
            elif not host_ok_for_site("marjorietome", url.strip()):
                st.error("URL incohérente avec ImmoToma.")
            else:
                try:
                    canon = canonicalize_marjorietome_url(url.strip())
                    rec={"site":"marjorietome","url":canon,"email":email.strip(),"pages":int(pages)}
                    if SHOW_LABELS: rec["label"]=label.strip()
                    key=f"marjorietome|{canon}"
                    idx=next((i for i,a in enumerate(st.session_state.alerts) if f"{a.get('site')}|{a.get('url')}"==key), None)
                    if idx is not None:
                        st.session_state.alerts[idx]=rec
                        append_event("update", rec, "Update ImmoToma")
                    else:
                        st.session_state.alerts.append(rec)
                        append_event("add", rec, "Add ImmoToma")
                    st.success("Alerte ImmoToma enregistrée ✅")
                except Exception as e:
                    st.error(f"Erreur: {e}")

# ---- Immo-KH + AD-HOME (sans URL, browser forcé, vendus exclus, mins=0) ----
with tab_khadh:
    with st.form("form_kh_adhome", clear_on_submit=True):
        st.subheader("Créer une alerte Immo-KH + AD-HOME (doublon automatique)")
        st.markdown('<span class="help">Aucune URL nécessaire. Le navigateur (Playwright) est utilisé automatiquement. Un enregistrement crée/maj 2 alertes identiques (Immo-KH & AD-HOME).</span>', unsafe_allow_html=True)

        email = st.text_input("Email", placeholder="ex: prenom.nom@gmail.com")
        pages = st.number_input("Pages / clics max (défilement)", min_value=1, max_value=200, value=DEFAULT_PAGES, step=1)
        label = st.text_input("Label (facultatif)") if SHOW_LABELS else ""

        # Filtres (mins init à 0) + vendus exclus (fixe) + use_browser True (fixe)
        filters_payload = immokh_adhome_filters_ui(default={"price_min":0,"bedrooms_min":0,"bathrooms_min":0,"area_min":0,"include_sold":False})
        use_browser = True  # forcé pour les deux

        ok = st.form_submit_button("Enregistrer")
        if ok:
            if not email.strip() or not is_valid_email(email):
                st.error("Email invalide.")
            else:
                try:
                    # 1) Immo-KH
                    rec_kh = {
                        "site":"immokh",
                        "url":IMMOKH_LIST,
                        "email":email.strip(),
                        "pages":int(pages),
                        "use_browser": True,
                        "filters": filters_payload
                    }
                    if SHOW_LABELS: rec_kh["label"]=label.strip()
                    fkey=json.dumps(filters_payload, sort_keys=True, ensure_ascii=False)
                    key_kh=f"immokh|{IMMOKH_LIST}|{fkey}"
                    idx_kh=next((i for i,a in enumerate(st.session_state.alerts)
                                 if (f"{a.get('site')}|{a.get('url')}|"+json.dumps(a.get('filters') or {}, sort_keys=True, ensure_ascii=False))==key_kh), None)
                    if idx_kh is not None:
                        st.session_state.alerts[idx_kh]=rec_kh
                        append_event("update", rec_kh, "Update Immo-KH")
                    else:
                        st.session_state.alerts.append(rec_kh)
                        append_event("add", rec_kh, "Add Immo-KH")

                    # 2) AD-HOME (copie 1:1)
                    rec_ad = {
                        "site":"adhome",
                        "url":ADHOME_LIST,
                        "email":email.strip(),
                        "pages":int(pages),
                        "use_browser": True,
                        "filters": filters_payload
                    }
                    if SHOW_LABELS: rec_ad["label"]=label.strip()
                    key_ad=f"adhome|{ADHOME_LIST}|{fkey}"
                    idx_ad=next((i for i,a in enumerate(st.session_state.alerts)
                                 if (f"{a.get('site')}|{a.get('url')}|"+json.dumps(a.get('filters') or {}, sort_keys=True, ensure_ascii=False))==key_ad), None)
                    if idx_ad is not None:
                        st.session_state.alerts[idx_ad]=rec_ad
                        append_event("update", rec_ad, "Update AD-HOME")
                    else:
                        st.session_state.alerts.append(rec_ad)
                        append_event("add", rec_ad, "Add AD-HOME")

                    st.success("Alertes Immo-KH & AD-HOME enregistrées ✅")
                except Exception as e:
                    st.error(f"Erreur: {e}")

# ============== LISTE / EDIT ==============
st.divider()
st.subheader("Mes alertes")
if "alerts" not in st.session_state: st.session_state.alerts = load_alerts()

def render_card(i:int, a:dict):
    site=a.get("site","immoweb"); url=a.get("url",""); email=a.get("email","")
    label=a.get("label","") if SHOW_LABELS else ""; filters=a.get("filters"); pages=a.get("pages")
    use_br=a.get("use_browser", None)

    with st.container():
        st.markdown('<div class="card">', unsafe_allow_html=True)
        st.markdown(f"**Site :** `{site}`  " + (f"&nbsp;&nbsp;<span class='badge'>{label}</span>" if (SHOW_LABELS and label) else ""), unsafe_allow_html=True)
        st.markdown(f"**Email :** {email}")
        if site not in BROWSER_SITES: st.markdown(f"**URL :** {url}")
        if pages: st.markdown(f"**Pages max :** {pages}")
        if site in BROWSER_SITES:
            st.markdown("**Navigateur :** toujours activé (Playwright)")
            st.markdown(f"**Filtres :** {filters_summary_str(filters)}")

        c1,c2 = st.columns([1,1])
        with c1:
            if st.button("✏️ Modifier", key=f"edit_{i}"):
                st.session_state[f"edit_{i}"]=True
        with c2:
            if st.button("🗑️ Supprimer", key=f"del_{i}"):
                payload={"site":site,"url":url}
                if site in BROWSER_SITES and filters is not None: payload["filters"]=filters
                append_event("delete", payload, "Delete alert UI")
                st.session_state.alerts=[x for j,x in enumerate(st.session_state.alerts) if j!=i]
                st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

        # EDIT
        if st.session_state.get(f"edit_{i}", False):
            with st.form(f"form_edit_{i}"):
                st.markdown("_Le site n’est pas modifiable. Supprimez puis recréez pour changer de site._")
                new_email = st.text_input("Email", value=email)
                new_pages = st.number_input("Pages max", min_value=1, max_value=200, value=int(pages or DEFAULT_PAGES), step=1)

                if site in BROWSER_SITES:
                    st.markdown("**URL :** fixée (liste du site)")
                    fixed_url = IMMOKH_LIST if site=="immokh" else ADHOME_LIST
                    new_url = fixed_url
                    new_filters = immokh_adhome_filters_ui(default=filters or {"price_min":0,"bedrooms_min":0,"bathrooms_min":0,"area_min":0})
                    new_usebr = True
                else:
                    new_url = st.text_input("URL", value=url)
                    new_filters = None
                    new_usebr = None

                new_label = st.text_input("Label", value=label) if SHOW_LABELS else ""

                save = st.form_submit_button("Sauvegarder")
                if save:
                    try:
                        if not is_valid_email(new_email):
                            st.warning("Email invalide.")
                        elif site not in BROWSER_SITES and not host_ok_for_site(site, new_url.strip()):
                            st.warning("URL incohérente avec le site.")
                        else:
                            if site in BROWSER_SITES:
                                edited={"site":site,"url":new_url,"email":new_email.strip(),"pages":int(new_pages),
                                        "filters":new_filters, "use_browser":True}
                            else:
                                canon2 = canonicalize_immoweb_url(new_url.strip()) if site=="immoweb" else canonicalize_marjorietome_url(new_url.strip())
                                edited={"site":site,"url":canon2,"email":new_email.strip(),"pages":int(new_pages)}
                            if SHOW_LABELS: edited["label"]=new_label.strip()

                            st.session_state.alerts[i]=edited
                            append_event("update", edited, "Inline edit")
                            st.session_state[f"edit_{i}"]=False
                            st.success("Alerte mise à jour ✅")
                            st.rerun()
                    except Exception as e:
                        st.error(f"Erreur: {e}")

alerts = st.session_state.alerts
if not alerts:
    st.info("Aucune alerte pour l’instant.")
else:
    for i,a in enumerate(alerts): render_card(i,a)

st.divider()
with st.expander("ℹ️ Aide"):
    st.markdown("""
- **Immoweb / ImmoToma** : collez l’URL (leurs filtres sont dans l’URL).
- **Immo-KH + AD-HOME** : **une seule configuration** de filtres → l’app crée/maintient **deux alertes** (une sur chaque site).  
  *Biens vendus **exclus**, **navigateur toujours activé**, et tous les **minimums à 0** par défaut.*
""")
