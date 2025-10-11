# -*- coding: utf-8 -*-
"""
App Streamlit — Deals 2ememain (UI compacte sans sidebar)
- Description sous le titre
- KPI prix en 2×2 : (Prix affiché | Prix juste) puis (Remise | Note vendeur)
- Bloc techniques en 3×3 : Kilométrage (G) | Cylindrée (D) + Transmission, Carburant, Euronorm, etc.
- Pas de sidebar : barre de filtres compacte en haut
- Badges deal: Great / Good / Fair / Overpriced / Suspicious
- Pagination propre
"""

from __future__ import annotations

import argparse, json, math, re
from pathlib import Path
from typing import Optional, List

import numpy as np
import pandas as pd
import streamlit as st

# ============================== CONFIG ========================================

APP_TITLE = "Meilleurs Deals Auto — 2ememain"
DEFAULT_PAGE_SIZE = 12

def discover_sources() -> dict[str, str]:
    """
    Retourne un dict {label: path}, où `label` est le nom du dossier sous runs/
    et `path` pointe vers runs/<label>/scored.csv. Ajoute le CSV démo si dispo.
    """
    sources: dict[str, str] = {}
    runs_dir = Path("runs")
    if runs_dir.exists():
        for p in sorted(runs_dir.glob("*/scored.csv")):
            label = p.parent.name  # ex. "audi_a1", "opel_corsa", "berlingo"
            sources[label] = str(p)
    demo = Path("/mnt/data/scored_berlingot.csv")
    if demo.exists():
        sources.setdefault("berlingo_demo", str(demo))  # nom clair pour le démo
    return sources

NEGATIVE_FLAGS = {
    "has_export": "Export",
    "has_ct_fail": "CT refusé",
    "has_accident": "Accident",
    "has_damage": "Dégâts",
    "has_engine_issue": "Moteur",
    "has_gearbox_issue": "Boîte",
    "has_non_rolling": "Non roulant",
    "has_rust": "Rouille",
    "has_oil_consumption": "Conso huile",
    "has_noise_smoke": "Bruit/Fumée",
    "has_km_not_guaranteed": "KM non garantis",
}
POSITIVE_FLAGS = {
    "has_ct_ok": "CT ok",
    "has_carpass": "Carpass",
    "has_maintenance_history": "Entretien suivi",
    "has_first_owner": "1er propriétaire",
    "has_non_smoker": "Non-fumeur",
    "has_warranty": "Garantie",
    "has_no_costs": "Aucun frais",
    "runs_perfect": "Roule bien",
}

CSS = """
<style>
:root{
  --bg:#f6f7f8; --panel:#ffffff; --panel-alt:#fafbfc; --text:#111827; --muted:#6b7280; --line:#e5e7eb;
  --ok:#15803d; --warn:#b45309; --over:#b91c1c; --fair:#374151;
  --radius:18px; --shadow:0 6px 18px rgba(0,0,0,.05);
}
html,body,.stApp{background:var(--bg);color:var(--text)}
h1{font-weight:800;letter-spacing:.2px;margin:.2rem 0 .6rem}
.small{color:var(--muted);font-size:.98rem}

.topbar{
  position:sticky; top:0; z-index:20; backdrop-filter:saturate(180%) blur(8px);
  background:rgba(246,247,248,.95); border-bottom:1px solid var(--line);
  padding:.6rem .8rem .65rem; margin:-1rem -1rem .8rem -1rem;
}
.tb-grid{
  display:grid;
  grid-template-columns: 260px 1fr 360px 130px;
  gap:.6rem; align-items:center;
}
.tb-row{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap}
.tb-label{font-size:.88rem;color:var(--muted);font-weight:700;margin-bottom:.25rem}

/* Segmented radio (tri) */
.stRadio [role="radiogroup"]{display:flex;gap:6px;flex-wrap:wrap}
.stRadio [role="radiogroup"] > label{
  border:1px solid var(--line); background:#fff; padding:6px 10px; border-radius:999px;
  font-weight:700; cursor:pointer; user-select:none;
}
.stRadio [role="radiogroup"] input:checked + div{font-weight:800}
.stRadio [role="radiogroup"] input{display:none}

/* Cartes (inchangé + zébrage) */
.card{
  background:var(--panel); border:1px solid var(--line); border-radius:var(--radius);
  box-shadow:var(--shadow); padding:14px 16px 12px; margin:10px 0;
}
.card.alt{ background:var(--panel-alt); border-color:#e9edf3; }
.card > *:first-child{margin-top:0!important}

.headerline{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:.3rem}
.title{font-size:1.18rem;font-weight:800;letter-spacing:.2px;color:var(--text)}
.badge{display:inline-flex;align-items:center;gap:8px;font-weight:800;font-size:.95rem;
  padding:6px 10px;border-radius:999px;border:1px solid var(--line);background:#fbfbfb;white-space:nowrap}
.dot{width:10px;height:10px;border-radius:999px;display:inline-block}
.badge.green .dot{background:var(--ok)} .badge.amber .dot{background:#b45309}
.badge.red .dot{background:var(--over)} .badge.gray .dot{background:var(--fair)}

.pairs{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; margin:.25rem 0 .5rem; }
.pair-row{ display:grid; grid-template-columns:1fr 1fr; gap:8px; }
.box{ border:1px solid var(--line); border-radius:12px; background:#fff; padding:8px 10px; }
.box.soft{ border-style:dashed; background:#fbfbfb; }
.box .l{font-size:.84rem;color:var(--muted);margin-bottom:2px}
.box .v{font-size:1.06rem;font-weight:800;line-height:1.2}

.grid3x3{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); grid-auto-flow:row dense; gap:8px; margin:.25rem 0 .5rem; }
.kcell{border:1px solid var(--line);border-radius:12px;background:#fff;padding:8px 10px}
.kcell .l{font-size:.84rem;color:var(--muted);margin-bottom:2px}
.kcell .v{font-size:1.06rem;font-weight:800}

.tags{margin-top:.2rem}
.tag{display:inline-flex;align-items:center;gap:6px;padding:5px 9px;border-radius:999px;border:1px solid var(--line);
  font-size:.86rem;font-weight:700;margin:3px 6px 0 0;background:#fff}
.tag.neg{background:#fff5f5;border-color:#fecaca;color:#7f1d1d}
.tag.pos{background:#f4faf7;border-color:#c7f0da;color:#065f46}
.optwrap{margin-top:.25rem}
.opt{display:inline-block;margin:3px 6px 0 0;padding:4px 9px;border:1px solid var(--line);border-radius:999px;background:#f9fafb;font-weight:700;font-size:.86rem}

.stButton>button,.stLinkButton>a{
  border-radius:12px!important;border:1px solid var(--line)!important;background:#fff!important;
  color:#111827!important;font-weight:800!important;font-size:.98rem!important;
  box-shadow:0 1px 2px rgba(0,0,0,.05)!important;padding:.45rem .8rem!important;
}
</style>
"""


# ============================== HELPERS =======================================

def _rerun():
    try:
        st.rerun()
    except AttributeError:
        # compat anciennes versions
        st.experimental_rerun()

def _money(v) -> str:
    try:
        f = float(v)
        if not math.isfinite(f): return "—"
        return f"{f:,.0f} €".replace(",", " ")
    except Exception:
        return "—"

def _safe_int(v: Optional[float|int]) -> str:
    try:
        if pd.isna(v): return ""
    except Exception:
        pass
    try:
        return f"{int(v):,}".replace(",", " ")
    except Exception:
        return ""

def _clean_desc(s: str, max_chars=420) -> str:
    s = str(s or "").strip()
    if not s: return ""
    s = re.sub(r"http[s]?://\\S+", "", s)
    s = re.sub(r"\\s+", " ", s)
    return (s[:max_chars] + "…") if len(s) > max_chars else s

def _badge(decision_label: str, decision_class: str) -> str:
    lbl = decision_label or "À considérer"
    cls = decision_class or "gray"
    return f'<span class="badge {cls}"><span class="dot"></span>{lbl}</span>'

def _opt_to_list(x):
    if isinstance(x, list): return [str(t).strip() for t in x if str(t).strip()]
    s=str(x or "").strip()
    if not s: return []
    if "|" in s: return [t.strip() for t in s.split("|") if t.strip()]
    if ";" in s: return [t.strip() for t in s.split(";") if t.strip()]
    return [s] if s else []

# ============================== DATA IO =======================================

def read_any(path: str) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    if p.suffix.lower()==".jsonl":
        rows=[]
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line=line.strip()
                if line: rows.append(json.loads(line))
        return pd.DataFrame(rows)
    if p.suffix.lower()==".csv":
        return pd.read_csv(p)
    raise ValueError("Formats acceptés : .jsonl ou .csv")

def enrich(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy().reset_index(drop=True)

    for c in [
        "url","make","model","year","mileage_km","fuel","transmission","doors",
        "power_kw","power_hp","co2_gkm","seller_rating","price_eur",
        "pred_price_fair","pred_price_lo","pred_price_hi",
        "deal_label","description","engine_liters","euro_norm",
        "body_type","color","options"
    ]:
        if c not in d.columns: d[c]=np.nan

    numeric_cols = [
        "year","mileage_km","power_kw","power_hp","co2_gkm","doors","seller_rating",
        "engine_liters","price_eur","pred_price_fair","pred_price_lo","pred_price_hi"
    ]
    for c in numeric_cols:
        d[c] = pd.to_numeric(d[c], errors="coerce")

    for f in list(NEGATIVE_FLAGS.keys()) + list(POSITIVE_FLAGS.keys()):
        if f not in d.columns: d[f]=0
        d[f] = pd.to_numeric(d[f], errors="coerce").fillna(0).astype(int)

    d["discount_eur"] = d["pred_price_fair"] - d["price_eur"]
    d["discount_pct"] = d["discount_eur"] / d["pred_price_fair"].replace(0,np.nan)

    # Décision simple
    def _decision(p):
        dp = p.get("discount_pct")
        if pd.isna(dp): return ("À considérer","gray")
        pct = float(dp)*100
        if pct >= 25: return ("Great deal","green")
        if pct >= 10: return ("Good deal","amber")
        if -5 <= pct < 10: return ("Fair","gray")
        if pct < -5: return ("Overpriced","red")
        return ("Fair","gray")
    dec = d.apply(_decision, axis=1, result_type="expand")
    d["decision_label"] = dec[0]
    d["decision_class"] = dec[1]

    d["options_list"] = d["options"].apply(_opt_to_list)
    return d

# ============================== FILTERS (NO SIDEBAR) ==========================

def _rerun():
    try: st.rerun()
    except AttributeError: st.experimental_rerun()  # compat anciennes versions

def top_filters(df: pd.DataFrame, sources: dict[str, str], current_src: str) -> dict:
    st.markdown('<div class="topbar">', unsafe_allow_html=True)

    labels = list(sources.keys())
    paths = list(sources.values())
    try:
        current_idx = paths.index(current_src)
    except ValueError:
        current_idx = 0 if labels else None

    # Ligne 1 : Source | Tri (segmented) | Réinitialiser / Ordre
    c1, c2, c3 = st.columns([2.6, 6.0, 1.8])
    with c1:
        st.markdown('<div class="tb-label">Source</div>', unsafe_allow_html=True)
        chosen_label = st.selectbox("Source", options=labels, index=current_idx, label_visibility="collapsed")
        chosen_src = sources.get(chosen_label)

    with c2:
        st.markdown('<div class="tb-label">Trier par</div>', unsafe_allow_html=True)
        sort_label = st.radio(
            "Trier par",
            ["Remise (%)","Remise (€)","Prix (€)","Année","Kilométrage","Prix juste (€)"],
            horizontal=True,
            label_visibility="collapsed",
            key="sort_radio",
        )

    with c3:
        asc = st.toggle("Ordre croissant", value=st.session_state.get("asc_init", False))
        if st.button("Réinitialiser"):
            # reinit des filtres clés dans session_state
            st.session_state.pop("sort_radio", None)
            st.session_state.pop("asc_init", None)
            st.session_state.pop("price_rg", None)
            st.session_state.pop("km_rg", None)
            st.session_state.pop("year_rg", None)
            _rerun()

    # Ligne 2 : 3 sliders (plus de checkbox Great/Good)
    c6, c7, c8 = st.columns([3.6, 3.6, 3.0])

    def _robust(col, default_min, default_max):
        if df[col].notna().any():
            return int(np.nanmin(df[col])), int(np.nanmax(df[col]))
        return default_min, default_max

    pmin,pmax = _robust("price_eur",0,100_000)
    ymin,ymax = _robust("year",1995,2030)
    kmin,kmax = _robust("mileage_km",0,400_000)

    with c6:
        price_rg = st.slider("Prix (€)", pmin, pmax, st.session_state.get("price_rg", (pmin, pmax)), step=500, key="price_rg")
    with c7:
        km_rg = st.slider("Kilométrage (km)", kmin, kmax, st.session_state.get("km_rg", (kmin, kmax)), step=1000, key="km_rg")
    with c8:
        year_rg = st.slider("Année", ymin, ymax, st.session_state.get("year_rg", (ymin, ymax)), step=1, key="year_rg")

    st.session_state["asc_init"] = asc
    st.markdown('</div>', unsafe_allow_html=True)

    return {
        "chosen_src": chosen_src,
        # plus de 'q'
        "price": price_rg, "year": year_rg, "km": km_rg,
        # plus de 'only_green'
        "sort": sort_label, "asc": asc
    }

def apply_filters(df: pd.DataFrame, f: dict) -> pd.DataFrame:
    d = df.copy()

    # plus de filtre texte 'q'

    # Filtres numériques
    d = d[d["price_eur"].between(f["price"][0], f["price"][1])]
    if d["year"].notna().any():
        d = d[d["year"].between(f["year"][0], f["year"][1])]
    if d["mileage_km"].notna().any():
        d = d[d["mileage_km"].between(f["km"][0], f["km"][1])]

    # plus de filtre 'only_green'

    # Tri
    key = f["sort"]
    if key=="Remise (%)": col="discount_pct"
    elif key=="Remise (€)": col="discount_eur"
    elif key=="Prix (€)": col="price_eur"
    elif key=="Année": col="year"
    elif key=="Kilométrage": col="mileage_km"
    elif key=="Prix juste (€)": col="pred_price_fair"
    else: col="discount_pct"

    d = d.sort_values(col, ascending=f["asc"], na_position="last")
    return d

# ============================== RENDER ========================================

def _tags(row: pd.Series, max_each=3) -> str:
    negs, poss = [], []
    for k, label in NEGATIVE_FLAGS.items():
        if int(row.get(k,0))==1: negs.append(label)
    for k, label in POSITIVE_FLAGS.items():
        if int(row.get(k,0))==1: poss.append(label)
    negs = negs[:max_each]; poss = poss[:max_each]
    parts=[]
    for t in negs: parts.append(f'<span class="tag neg">{t}</span>')
    for t in poss: parts.append(f'<span class="tag pos">{t}</span>')
    return '<div class="tags">'+"".join(parts)+"</div>" if parts else ""

def render_kpi_pairs(row: pd.Series):
    """2×2 : (Prix | Juste) puis (Remise | Note)."""
    prix = _money(row["price_eur"])
    juste = _money(row["pred_price_fair"])
    rem_pct = f"{row['discount_pct']*100:.1f}%" if pd.notna(row["discount_pct"]) else "—"
    note = f"{row['seller_rating']:.1f}/5" if pd.notna(row["seller_rating"]) else "—"

    st.markdown('<div class="pairs">', unsafe_allow_html=True)
    st.markdown(
        '<div class="pair-row">'
        f'  <div class="box soft"><div class="l">Prix affiché</div><div class="v">{prix}</div></div>'
        f'  <div class="box soft"><div class="l">Prix juste</div><div class="v">{juste}</div></div>'
        '</div>', unsafe_allow_html=True
    )
    st.markdown(
        '<div class="pair-row">'
        f'  <div class="box soft"><div class="l">Remise vs juste</div><div class="v">{rem_pct}</div></div>'
        f'  <div class="box soft"><div class="l">Note vendeur</div><div class="v">{note}</div></div>'
        '</div>', unsafe_allow_html=True
    )
    st.markdown('</div>', unsafe_allow_html=True)

def _kcell_html(label:str, value:str) -> str:
    return f'<div class="kcell"><div class="l">{label}</div><div class="v">{value}</div></div>'

def render_info_grid_3x3(row: pd.Series):
    # Valeurs formatées
    km   = _safe_int(row.get("mileage_km"))
    cyl  = f"{float(row['engine_liters']):.1f} L" if pd.notna(row.get("engine_liters", np.nan)) else ""
    carb = str(row.get("fuel") or "")
    trans= str(row.get("transmission") or "")
    euro = str(row.get("euro_norm") or "")
    co2  = _safe_int(row.get("co2_gkm"))
    hp   = _safe_int(row.get("power_hp"))
    doors= _safe_int(row.get("doors"))
    body = str(row.get("body_type") or "") or str(row.get("color") or "")

    # Construction de la liste SANS trous
    cells: list[tuple[str,str]] = []
    if km:  cells.append(("Kilométrage", km))
    if cyl: cells.append(("Cylindrée", cyl))   # à droite si les deux existent
    if carb: cells.append(("Carburant", carb))
    if trans: cells.append(("Transmission", trans))
    if euro:  cells.append(("Euronorm", euro))
    if co2:   cells.append(("CO₂ (g/km)", co2))
    if hp:    cells.append(("Puissance (hp)", hp))
    if doors: cells.append(("Portes", doors))
    if body:  cells.append(("Carrosserie/Couleur", body))
    if not cells: return

    html = ['<div class="grid3x3">'] + [_kcell_html(lbl, val) for (lbl, val) in cells] + ['</div>']
    st.markdown("".join(html), unsafe_allow_html=True)

def render_card(row: pd.Series, idx: int):
    wrapper_cls = "card alt" if (idx % 2 == 1) else "card"
    st.markdown(f'<div class="{wrapper_cls}">', unsafe_allow_html=True)

    # En-tête
    title_parts = [
        str(row.get("make","") or "").strip(),
        str(row.get("model","") or "").strip(),
        str(int(row["year"])) if pd.notna(row["year"]) else ""
    ]
    title = " ".join([p for p in title_parts if p]).strip() or "Annonce"
    st.markdown(
        f'<div class="headerline"><div class="title">{title}</div>'
        f'<div>{_badge(row.get("decision_label",""), row.get("decision_class",""))}</div></div>',
        unsafe_allow_html=True
    )

    # Description
    desc = _clean_desc(row.get("description",""), max_chars=420)
    if desc: st.markdown(desc)

    # KPI prix 2×2
    render_kpi_pairs(row)

    # Bloc techniques 3×3
    render_info_grid_3x3(row)

    # Explication du deal
    if pd.notna(row["price_eur"]) and pd.notna(row["pred_price_fair"]):
        lo, fair, hi = row.get("pred_price_lo"), row.get("pred_price_fair"), row.get("pred_price_hi")
        txt = f"Listé **{_money(row['price_eur'])}** vs prix juste **{_money(fair)}**."
        if pd.notna(lo) and pd.notna(hi):
            txt += f" Intervalle attendu : [{_money(lo)} – {_money(hi)}]."
        if pd.notna(row["discount_pct"]):
            d = float(row["discount_pct"])*100
            txt += f" ≈ **{d:.1f}%** sous le juste prix." if d>=0 else f" ≈ **{abs(d):.1f}%** au-dessus du juste prix."
        st.markdown(txt)

    # Tags / options
    neg_pos_html = _tags(row)
    if neg_pos_html: st.markdown(neg_pos_html, unsafe_allow_html=True)

    # Action (seulement le lien vers l'annonce)
    url = str(row.get("url") or "")
    if url.startswith("http"):
        st.link_button("Voir l’annonce", url)

    st.markdown('</div>', unsafe_allow_html=True)

# ============================== APP ===========================================

def main():
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)

    st.title(APP_TITLE)

    # CLI arg
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--in", dest="in_path", required=False, default="")
    args, _ = parser.parse_known_args()

    # Découverte des sources
    sources = discover_sources()
    if not sources:
        st.error("Aucune source trouvée. Place un 'scored.csv' dans un dossier sous runs/<nom>.")
        st.stop()

    # Choix initial de la source : --in a priorité, sinon 1ère trouvée
    src = args.in_path.strip()
    if not src:
        src = list(sources.values())[0]

    # Lecture
    try:
        df_raw = read_any(src)
    except Exception as e:
        st.error(f"Impossible de lire {src} : {e}")
        st.stop()
    df = enrich(df_raw)

    # Barre haute (filtres) avec sélection de la source
    fcfg = top_filters(df, sources=sources, current_src=src)

    # Changement de source depuis l'UI
    new_src = fcfg.get("chosen_src") or src
    if new_src != src and Path(new_src).exists():
        src = new_src
        df = enrich(read_any(src))



    dff = apply_filters(df, fcfg)

    # Bandeau résultats (sans export)
    nb = len(dff)
    st.subheader(f"{nb} annonce(s) trouvée(s)")
    st.divider()

    # Pagination
    if "page" not in st.session_state:
        st.session_state.page = 1

    page_size = DEFAULT_PAGE_SIZE
    total = len(dff)
    total_pages = max(1, math.ceil(total / page_size))

    if total == 0:
        st.info("Aucune annonce avec ces critères. Élargis un peu les filtres.")
        st.stop()

    page = max(1, min(st.session_state.page, total_pages))
    start = (page - 1) * page_size
    end = start + page_size
    subset = dff.iloc[start:end].reset_index(drop=True)

    # Cartes
    for i, row in subset.iterrows():
        render_card(row, idx=start+i)

    # Contrôles de page
    col_a, col_b, col_c = st.columns([0.2, 0.6, 0.2])
    with col_a:
        if st.button("◀ Précédent", disabled=(page <= 1)):
            st.session_state.page = max(1, page - 1)
            st.rerun()
    with col_b:
        st.markdown(f"<div style='text-align:center' class='small'>Page {page} / {total_pages}</div>", unsafe_allow_html=True)
    with col_c:
        if st.button("Suivant ▶", disabled=(page >= total_pages)):
            st.session_state.page = min(total_pages, page + 1)
            st.rerun()

    if page < total_pages:
        if st.button("Afficher plus"):
            st.session_state.page = min(total_pages, page + 1)
            st.rerun()

if __name__ == "__main__":
    main()
