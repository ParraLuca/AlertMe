# viz_scored_app.py
# -*- coding: utf-8 -*-
"""
App Streamlit — Visualisation épurée des annonces scorées (2ememain)

Lance:
  streamlit run viz_scored_app.py -- --in scored.csv

Dépendances:
  pip install streamlit pandas numpy plotly
"""

from __future__ import annotations
import argparse, json, math
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go

# =========================================================
#                     LECTURE FICHIER
# =========================================================

def read_any(path: str) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    if p.suffix.lower() == ".jsonl":
        rows = []
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return pd.DataFrame(rows)
    if p.suffix.lower() == ".csv":
        return pd.read_csv(p)
    raise ValueError("Supported: .jsonl or .csv")


# =========================================================
#                  DESIGN (épuré, light)
# =========================================================

CSS = """
<style>
:root{
  --bg: #f7f7f9;
  --panel: #ffffff;
  --text: #1f2937;
  --muted: #6b7280;
  --line: #e5e7eb;
  --accent: #8aa0b6;

  --good: #17a34a;   /* Good */
  --fair: #475569;   /* Fair */
  --over: #dc2626;   /* Overpriced */
  --great: #059669;  /* Great (vert plus vif) */
  --sus: #d97706;    /* Suspicious (orange) */

  --radius: 12px;
  --radius-sm: 8px;
  --shadow: 0 4px 16px rgba(0,0,0,.06);
}

/* Page */
.stApp { background: var(--bg); color: var(--text); }

/* Sidebar */
section[data-testid="stSidebar"]{
  background: #fafafa;
  border-right: 1px solid var(--line);
}
.sidebar-title {
  font-size: 15px; font-weight: 700;
  letter-spacing: .2px; color: var(--text);
  margin: 4px 0 8px 0;
}

/* Titres */
h1, h2, h3 { color: var(--text); margin-top: 4px; }
h1 { font-size: 28px; font-weight: 700; letter-spacing: .2px; }
h2 { font-size: 18px; font-weight: 600; color: #111827; }
h3 { font-size: 15px; font-weight: 600; color: #111827; }

/* Cartes / sections */
.block {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  padding: 14px 16px;
}

/* Petites cartes KPI */
.kpi {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  padding: 12px 14px;
}
.kpi .k-label { font-size: 12px; color: var(--muted); }
.kpi .k-value { font-size: 20px; font-weight: 700; }

/* Badges (deal) */
.badge {
  display:inline-flex; align-items:center; gap:6px;
  padding: 5px 12px; border-radius: 999px;
  border: 1px solid var(--line);
  background: #fbfbfc;
  font-size: 12px; font-weight: 700; color: #0f172a;
}
.badge .dot{width:9px;height:9px;border-radius:999px;display:inline-block;}
.badge.good .dot{background: var(--good);}
.badge.fair .dot{background: var(--fair);}
.badge.over .dot{background: var(--over);}
.badge.great .dot{background: var(--great);}
.badge.sus .dot{background: var(--sus);}

/* Tags description (plus visibles) */
.tag{
  display:inline-flex; align-items:center; gap:6px;
  padding: 4px 10px; border-radius: 999px;
  border: 1px solid var(--line);
  background: #fbfbfc; color: #111827;
  font-size: 12px; font-weight: 700; margin: 3px 6px 3px 0;
}
.tag-neg{ background: #fff5f5; border-color: #fecaca; color: #991b1b; }
.tag-pos{ background: #f4faf7; border-color: #c7f0da; color: #065f46; }

/* Chips risques (petits et visibles) */
.risks{
  display:flex; flex-wrap:wrap; gap:6px; margin-top:8px;
}
.risk-chip{
  font-size:11px; font-weight:700; padding:3px 8px;
  border-radius:999px; background:#fff7ed; color:#9a3412; border:1px solid #fed7aa;
}

/* Boutons */
.stDownloadButton button, .stLinkButton a, .stButton button{
  border-radius: var(--radius-sm)!important;
  border: 1px solid var(--line)!important;
  background: #ffffff!important;
  color: #111827!important;
  font-weight: 700!important;
  box-shadow: 0 1px 2px rgba(0,0,0,.04)!important;
}
.stDownloadButton button:hover, .stLinkButton a:hover, .stButton button:hover{
  background: #f3f4f6!important;
}

/* Plot container */
.plot-wrap{
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  padding: 6px 8px;
}

/* Tables */
.dataframe{
  border-radius: var(--radius);
  border: 1px solid var(--line);
  overflow: hidden;
}
</style>
"""

def badge_html(lbl: str) -> str:
    m = (lbl or "").lower()
    if m == "great_deal":
        cls, txt = "badge great", "Great deal"
    elif m == "good_deal":
        cls, txt = "badge good", "Good deal"
    elif m == "overpriced":
        cls, txt = "badge over", "Overpriced"
    elif m == "suspicious":
        cls, txt = "badge sus", "Suspicious"
    else:
        cls, txt = "badge fair", "Fair"
    return f'<span class="{cls}"><span class="dot"></span>{txt}</span>'


# =========================================================
#              ENRICHISSEMENT / FEATURES
# =========================================================

DESC_FLAGS = [
    "has_export","has_ct_fail","has_accident","has_damage","has_engine_issue","has_gearbox_issue",
    "has_non_rolling","has_rust","has_oil_consumption","has_noise_smoke","has_km_not_guaranteed",
    "has_ct_ok","has_carpass","has_maintenance_history","has_first_owner",
    "has_non_smoker","has_warranty","has_no_costs","runs_perfect",
]
NEGATIVE_FLAGS = {
    "has_export":"Export",
    "has_ct_fail":"CT refusé",
    "has_accident":"Accident",
    "has_damage":"Dégâts",
    "has_engine_issue":"Moteur",
    "has_gearbox_issue":"Boîte",
    "has_non_rolling":"Non roulant",
    "has_rust":"Rouille",
    "has_oil_consumption":"Conso huile",
    "has_noise_smoke":"Bruit/Fumée",
    "has_km_not_guaranteed":"KM non garantis",
}
POSITIVE_FLAGS = {
    "has_ct_ok":"CT ok",
    "has_carpass":"Carpass",
    "has_maintenance_history":"Entretien",
    "has_first_owner":"1er propriétaire",
    "has_non_smoker":"Non-fumeur",
    "has_warranty":"Garantie",
    "has_no_costs":"Aucun frais",
    "runs_perfect":"Roule parfait",
}

# Seuils de décision (peuvent être ajustés rapidement ici)
THRESHOLDS = {
    "overpriced_max": -0.05,   # < -5% -> overpriced
    "fair_max":       0.05,    # [-5%, +5%) -> fair
    "good_max":       0.25,    # [5%, 25%) -> good
    "great_max":      0.40,    # [25%, 40%) -> great
    # >= 40% -> suspicious par défaut
}
# Pondérations de risque pour déclasser un deal
RISK_WEIGHTS = {
    "has_export": 2.0,
    "has_ct_fail": 2.0,
    "has_km_not_guaranteed": 1.5,
    "has_accident": 1.2,
    "has_damage": 1.2,
    "has_engine_issue": 1.5,
    "has_gearbox_issue": 1.5,
    "has_non_rolling": 3.0,
    "has_rust": 1.0,
    "has_oil_consumption": 1.2,
    "has_noise_smoke": 1.0,
}
RISK_AUTO_SUS_IF_OVER = 0.40   # >= 40% de remise -> suspicious
RISK_AUTO_SUS_IF_EXPORT_OVER = 0.25  # >=25% + export -> suspicious

def _risk_reasons(row) -> list[str]:
    reasons = []
    for k, label in NEGATIVE_FLAGS.items():
        if int(row.get(k, 0)) == 1:
            reasons.append(label)
    return reasons

def _base_label_from_discount(d: float) -> str:
    if not math.isfinite(d):
        return "fair"
    if d < THRESHOLDS["overpriced_max"]:
        return "overpriced"
    if d < THRESHOLDS["fair_max"]:
        return "fair"
    if d < THRESHOLDS["good_max"]:
        return "good_deal"
    if d < THRESHOLDS["great_max"]:
        return "great_deal"
    return "suspicious"

def _apply_risk_adjustments(base: str, d: float, row) -> str:
    # Suspicious si remise extrême
    if d >= RISK_AUTO_SUS_IF_OVER:
        return "suspicious"
    # Suspicious si export + grosse remise
    if int(row.get("has_export", 0)) == 1 and d >= RISK_AUTO_SUS_IF_EXPORT_OVER:
        return "suspicious"
    # Score de risque pondéré
    score = 0.0
    for k, w in RISK_WEIGHTS.items():
        if int(row.get(k, 0)) == 1:
            score += w
    # Downgrade par paliers
    order = ["overpriced", "fair", "good_deal", "great_deal"]
    if base == "suspicious":
        return "suspicious"
    if score >= 3.0 and base == "great_deal":
        return "good_deal"
    if score >= 4.0 and base in ("great_deal", "good_deal"):
        return "fair"
    if score >= 6.0:
        return "suspicious"
    return base

def enrich(df: pd.DataFrame) -> pd.DataFrame:
    d = df.reset_index(drop=True).copy()
    d["car_id"] = (d.index + 1).astype(int)

    # Colonnes numériques utiles
    for c in ["price_eur","pred_price_fair","pred_price_lo","pred_price_hi",
              "deal_score","year","mileage_km","seller_rating","images_count","options_count"]:
        if c not in d.columns: d[c] = np.nan
        d[c] = pd.to_numeric(d[c], errors="coerce")

    # Remise
    d["discount_eur"] = d["pred_price_fair"] - d["price_eur"]
    d["discount_pct"] = (d["discount_eur"] / d["pred_price_fair"].replace(0, np.nan)).replace([np.inf, -np.inf], np.nan)

    # Champs textes
    for col in ["make","model","fuel","transmission","url"]:
        if col not in d.columns: d[col] = ""

    # Flags description (si absentes -> 0)
    for f in DESC_FLAGS:
        if f not in d.columns: d[f] = 0
        d[f] = pd.to_numeric(d[f], errors="coerce").fillna(0).astype(int)

    # Tags (affichage) + risques
    def make_tags(row):
        tags = []
        for k, label in NEGATIVE_FLAGS.items():
            if row.get(k, 0) == 1: tags.append(("neg", label))
        for k, label in POSITIVE_FLAGS.items():
            if row.get(k, 0) == 1: tags.append(("pos", label))
        return tags
    d["desc_tags"] = d.apply(make_tags, axis=1)
    d["risk_reasons"] = d.apply(_risk_reasons, axis=1)
    d["risk_count"] = d["risk_reasons"].apply(len)

    # Nouveau label (nuancé)
    def compute_label(row):
        dct = float(row.get("discount_pct", np.nan))
        base = _base_label_from_discount(dct)
        lab = _apply_risk_adjustments(base, dct, row)
        return lab
    d["deal_label"] = d.apply(compute_label, axis=1).astype(str)

    return d


# =========================================================
#                        FILTRES
# =========================================================

def filter_df(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    st.sidebar.markdown('<div class="sidebar-title">🔎 Filtres</div>', unsafe_allow_html=True)

    labels = ["any","great_deal","good_deal","fair","overpriced","suspicious"]
    label = st.sidebar.selectbox("Label du deal", labels, index=0)

    dis_min = st.sidebar.slider("Remise minimale (%)", -50.0, 60.0, -50.0, 1.0)

    pmin = int(np.nanmin(df["price_eur"])) if df["price_eur"].notna().any() else 0
    pmax = int(np.nanmax(df["price_eur"])) if df["price_eur"].notna().any() else 100000
    ymin = int(np.nanmin(df["year"])) if df["year"].notna().any() else 1990
    ymax = int(np.nanmax(df["year"])) if df["year"].notna().any() else 2030

    prix_rg = st.sidebar.slider("Prix (€)", pmin, pmax, (pmin, pmax), 500)
    an_rg   = st.sidebar.slider("Année", ymin, ymax, (ymin, ymax), 1)

    with st.sidebar.expander("Description (qualité)"):
        only_neg = st.checkbox("Filtrer par signaux négatifs", value=False)
        sel_neg = st.multiselect("Négatifs", [NEGATIVE_FLAGS[k] for k in NEGATIVE_FLAGS], []) if only_neg else []
        only_pos = st.checkbox("Filtrer par signaux positifs", value=False)
        sel_pos = st.multiselect("Positifs", [POSITIVE_FLAGS[k] for k in POSITIVE_FLAGS], []) if only_pos else []
        no_risk = st.checkbox("Exclure annonces avec risques", value=False)

    d = df.copy()

    if label != "any": d = d[d["deal_label"] == label]
    d = d[d["discount_pct"].fillna(-1.0) >= (dis_min/100.0)]
    d = d[d["price_eur"].between(prix_rg[0], prix_rg[1])]
    if d["year"].notna().any():
        d = d[d["year"].between(an_rg[0], an_rg[1])]
    if only_neg and len(sel_neg):
        keys = [k for k,v in NEGATIVE_FLAGS.items() if v in sel_neg]
        for k in keys: d = d[d[k] == 1]
    if only_pos and len(sel_pos):
        keys = [k for k,v in POSITIVE_FLAGS.items() if v in sel_pos]
        for k in keys: d = d[d[k] == 1]
    if no_risk:
        d = d[d["risk_count"] == 0]

    filt = {
        "label": label,
        "discount_min_pct": dis_min,
        "price_range": prix_rg,
        "year_range": an_rg,
        "neg": sel_neg, "pos": sel_pos,
        "no_risk": no_risk
    }
    return d, filt


# =========================================================
#                         KPIs
# =========================================================

def kpi(label: str, value: str):
    st.markdown(f"""
    <div class="kpi">
      <div class="k-label">{label}</div>
      <div class="k-value">{value}</div>
    </div>
    """, unsafe_allow_html=True)

def kpis(df_all: pd.DataFrame, dff: pd.DataFrame):
    c1, c2, c3, c4 = st.columns(4)
    with c1: kpi("Annonces (total)", f"{len(df_all)}")
    with c2: kpi("Après filtres", f"{len(dff)}")
    if len(dff):
        counts = dff["deal_label"].value_counts()
        order = ["great_deal","good_deal","fair","overpriced","suspicious"]
        txt = " / ".join([f"{k.split('_')[0].capitalize()}: {int(counts.get(k,0))}" for k in order if counts.get(k,0)>0])
    else:
        txt = "—"
    with c3: kpi("Répartition deals", txt)
    med = float(dff["discount_pct"].median()) if len(dff) else math.nan
    with c4: kpi("Remise médiane", f"{med*100:.1f}%" if math.isfinite(med) else "—")


# =========================================================
#                    GRAPHIQUES PROPRES
# =========================================================

COLORS = {
    "great_deal":"#059669",
    "good_deal":"#17a34a",
    "fair":"#475569",
    "overpriced":"#dc2626",
    "suspicious":"#d97706",
}

def fig_hist_discount(dff: pd.DataFrame, dis_min: float):
    if not len(dff):
        st.info("Aucune donnée après filtres.")
        return
    st.markdown("##### Distribution des remises (%)")
    fig = px.histogram(
        dff, x="discount_pct", nbins=36, color="deal_label",
        color_discrete_map=COLORS,
        labels={"discount_pct":"Remise (%)","deal_label":"Label"},
        category_orders={"deal_label":["overpriced","fair","good_deal","great_deal","suspicious"]}
    )
    fig.update_layout(
        margin=dict(l=12,r=12,t=6,b=8),
        paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
        font=dict(color="#1f2937"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    fig.update_xaxes(tickformat=".0%", title_text="Remise (pourcentage)", showline=True, linewidth=1, linecolor="#e5e7eb", gridcolor="#eef0f3")
    fig.update_yaxes(title_text="Fréquence", showline=True, linewidth=1, linecolor="#e5e7eb", gridcolor="#eef0f3")
    fig.add_vline(x=dis_min/100.0, line_dash="dash", line_color="#6b7280",
                  annotation_text=f"Seuil {dis_min:.0f}%", annotation_position="top left")
    st.markdown('<div class="plot-wrap">', unsafe_allow_html=True)
    st.plotly_chart(fig, use_container_width=True, theme=None)
    st.markdown('</div>', unsafe_allow_html=True)

def _apply_common_hover(fig):
    fig.update_traces(
        hovertemplate=(
            "<b>ID</b>=%{customdata[0]}<br>"
            "Année=%{customdata[5]}<br>"
            "Km=%{customdata[3]:,.0f}<br>"
            "Modèle=%{customdata[4]}<br>"
            "Prix listé=%{customdata[1]:,.0f} €<br>"
            "Prix prédit=%{customdata[2]:,.0f} €<br>"
            "<extra></extra>"
        )
    )
    return fig

def fig_scatter_parity(dff: pd.DataFrame):
    if not len(dff):
        st.info("Aucune donnée après filtres.")
        return
    st.markdown("##### Prix listé vs Prix juste (parité)")
    cd = ["car_id","price_eur","pred_price_fair","mileage_km","model","year","url"]
    fig = px.scatter(
        dff, x="pred_price_fair", y="price_eur",
        color="deal_label", color_discrete_map=COLORS,
        custom_data=cd, hover_data=None,
        labels={"pred_price_fair":"Prix juste (€)","price_eur":"Prix listé (€)","deal_label":"Label"},
        category_orders={"deal_label":["overpriced","fair","good_deal","great_deal","suspicious"]}
    )
    fig = _apply_common_hover(fig)
    x0, x1 = float(np.nanmin(dff["pred_price_fair"])), float(np.nanmax(dff["pred_price_fair"]))
    fig.add_trace(go.Scatter(
        x=[x0, x1], y=[x0, x1], mode="lines", name="Parité",
        line=dict(color="#9ca3af", dash="dash"), hoverinfo="skip", showlegend=False
    ))
    fig.update_layout(
        margin=dict(l=12,r=12,t=6,b=8),
        paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
        font=dict(color="#1f2937"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    fig.update_xaxes(title_text="Prix juste prédit (€)", showline=True, linewidth=1, linecolor="#e5e7eb", gridcolor="#eef0f3")
    fig.update_yaxes(title_text="Prix listé (€)", showline=True, linewidth=1, linecolor="#e5e7eb", gridcolor="#eef0f3")
    st.markdown('<div class="plot-wrap">', unsafe_allow_html=True)
    st.plotly_chart(fig, use_container_width=True, theme=None)
    st.markdown('</div>', unsafe_allow_html=True)

# ---- Similarité (année + km) ----

def _pairwise_similarity_distance(df: pd.DataFrame) -> np.ndarray:
    n = len(df)
    if n == 0: return np.zeros((0,0), dtype=float)
    year = pd.to_numeric(df["year"], errors="coerce").fillna(-1).values.astype(float)
    km   = pd.to_numeric(df["mileage_km"], errors="coerce")
    km = km.fillna(km.median() if km.notna().any() else 0).values.astype(float)
    logkm = np.log1p(np.clip(km, 0, None))
    w_year, w_km = 1.0, 1.0
    D = np.zeros((n, n), dtype=float)
    for i in range(n):
        dy  = np.abs(year[i] - year)
        dkm = np.abs(logkm[i] - logkm)
        dist = w_year * (dy / 10.0) + w_km * (dkm / 5.0)
        D[i, :] = dist; D[:, i] = dist
    np.fill_diagonal(D, 0.0)
    return D

def _classical_mds(D: np.ndarray, ndim: int = 2) -> np.ndarray:
    if D.size == 0: return np.zeros((0, ndim))
    n = D.shape[0]
    J = np.eye(n) - np.ones((n, n))/n
    D2 = D**2
    B = -0.5 * J @ D2 @ J
    vals, vecs = np.linalg.eigh(B)
    idx = np.argsort(vals)[::-1]
    vals = vals[idx]; vecs = vecs[:, idx]
    keep = [k for k in range(len(vals)) if vals[k] > 1e-9][:ndim]
    if not keep: return np.zeros((n, ndim))
    L = np.diag(np.sqrt(vals[keep]))
    X = vecs[:, keep] @ L
    if len(keep) < ndim:
        X = np.concatenate([X, np.zeros((n, ndim-len(keep)))], axis=1)
    return X[:, :ndim]

def fig_similarity_map(dff: pd.DataFrame):
    if len(dff) < 3:
        st.info("Trop peu d’annonces pour la carte de similarité.")
        return
    st.markdown("##### Carte de similarité (année & km)")
    MAX_N = 1200
    data = dff.copy()
    if len(data) > MAX_N:
        data = data.sample(MAX_N, random_state=42)
    D = _pairwise_similarity_distance(data)
    X = _classical_mds(D, ndim=2)
    plot = data.copy()
    plot["sim_x"] = X[:, 0]; plot["sim_y"] = X[:, 1]
    cd = ["car_id","price_eur","pred_price_fair","mileage_km","model","year","url"]
    fig = px.scatter(
        plot, x="sim_x", y="sim_y", color="deal_label",
        color_discrete_map=COLORS, custom_data=cd, hover_data=None,
        labels={"deal_label":"Label"},
        category_orders={"deal_label":["overpriced","fair","good_deal","great_deal","suspicious"]}
    )
    fig = _apply_common_hover(fig)
    fig.update_layout(
        margin=dict(l=12,r=12,t=6,b=8),
        paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
        font=dict(color="#1f2937"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    fig.update_xaxes(visible=False); fig.update_yaxes(visible=False)
    st.markdown('<div class="plot-wrap">', unsafe_allow_html=True)
    st.plotly_chart(fig, use_container_width=True, theme=None)
    st.caption("Voitures proches = millésime & kilométrage comparables.")
    st.markdown('</div>', unsafe_allow_html=True)


# =========================================================
#                 CARTES TOP + TABLE SHORTLIST
# =========================================================

def _tags_html(tags):
    if not tags: return ""
    parts = []
    for t in tags:
        cls = "tag-pos" if t[0] == "pos" else "tag-neg"
        parts.append(f'<span class="tag {cls}">{t[1]}</span>')
    return "".join(parts)

def _risks_html(reasons):
    if not reasons: return ""
    chips = "".join([f'<span class="risk-chip">{r}</span>' for r in reasons])
    return f'<div class="risks">{chips}</div>'

def render_top_cards(dff: pd.DataFrame, n=3):
    st.markdown("##### Top opportunités")
    if not len(dff):
        st.info("Aucune annonce à afficher."); return

    # 1) Priorité: vrais bons plans (good/great) sans risque
    pri = dff[(dff["deal_label"].isin(["great_deal","good_deal"])) & (dff["risk_count"] == 0)].copy()
    # 2) Fallback: good/great avec peu de risques (<=1)
    fallback = dff[(dff["deal_label"].isin(["great_deal","good_deal"])) & (dff["risk_count"] <= 1)].copy()
    # 3) Fallback2: meilleurs fair sans risque
    fallback2 = dff[(dff["deal_label"]=="fair") & (dff["risk_count"]==0)].copy()

    if len(pri) == 0:
        pri = fallback
    if len(pri) == 0:
        pri = fallback2
    if len(pri) == 0:
        st.info("Aucune opportunité sans risque à mettre en avant."); return

    top = pri.sort_values(["discount_pct","pred_price_fair"], ascending=[False, False]).head(n).copy()
    cols = st.columns(min(n, len(top)))
    for i, (_, row) in enumerate(top.iterrows()):
        col = cols[i] if i < len(cols) else st
        with col:
            st.markdown('<div class="block">', unsafe_allow_html=True)
            year_txt = int(row["year"]) if math.isfinite(row.get("year", np.nan)) else ""
            title = f"#{int(row['car_id'])} — {row.get('make','')} {row.get('model','')} {year_txt}"
            st.markdown(f"**{title}**")
            st.markdown(badge_html(row.get("deal_label","fair")), unsafe_allow_html=True)

            st.markdown(
                f"Prix: **{row['price_eur']:,.0f} €**  ·  "
                f"Juste: **{row['pred_price_fair']:,.0f} €**  ·  "
                f"Remise: **{(row['discount_pct']*100):.1f}%**  ·  "
                f"Km: **{row.get('mileage_km',np.nan):,.0f}**"
            )

            # Tags + risques plus visibles
            st.markdown(_tags_html(row["desc_tags"]), unsafe_allow_html=True)
            if row["risk_count"] > 0:
                st.markdown(_risks_html(row["risk_reasons"]), unsafe_allow_html=True)

            url = str(row.get("url") or "")
            if url and url.startswith("http"):
                st.link_button("Voir l’annonce", url)
            st.markdown('</div>', unsafe_allow_html=True)

def shortlist_table(dff: pd.DataFrame):
    st.markdown("##### Shortlist (triée par remise %) — plus lisible")
    if not len(dff):
        st.info("Aucune annonce à afficher."); return

    # Colonnes choisies (compact + utiles)
    cols = [c for c in [
        "car_id","deal_label","risk_count","discount_pct","discount_eur",
        "price_eur","pred_price_fair",
        "make","model","year","mileage_km","fuel","transmission","seller_rating","images_count","url"
    ] if c in dff.columns]

    show = dff.sort_values("discount_pct", ascending=False).copy()
    show = show[cols].head(400)

    # Formats
    show["discount_pct"] = (show["discount_pct"]*100).round(1)
    show["discount_eur"] = show["discount_eur"].round(0)
    if "seller_rating" in show.columns:
        show["seller_rating"] = pd.to_numeric(show["seller_rating"], errors="coerce").round(2)

    # Configuration Streamlit dataframe (si version récente)
    try:
        st.dataframe(
            show,
            use_container_width=True,
            column_config={
                "car_id": st.column_config.NumberColumn("ID", help="Identifiant interne"),
                "deal_label": st.column_config.TextColumn("Label"),
                "risk_count": st.column_config.NumberColumn("Risks", help="Nombre d’indices négatifs"),
                "discount_pct": st.column_config.ProgressColumn(
                    "Remise (%)", format="%.1f", min_value=-50, max_value=60, help="Remise par rapport au prix juste"
                ),
                "discount_eur": st.column_config.NumberColumn("Remise (€)", format="%.0f"),
                "price_eur": st.column_config.NumberColumn("Prix (€)", format="%.0f"),
                "pred_price_fair": st.column_config.NumberColumn("Juste (€)", format="%.0f"),
                "year": st.column_config.NumberColumn("Année"),
                "mileage_km": st.column_config.NumberColumn("Km", format="%.0f"),
                "seller_rating": st.column_config.NumberColumn("Note vendeur"),
                "images_count": st.column_config.NumberColumn("Photos"),
                "url": st.column_config.LinkColumn("Annonce"),
            }
        )
    except Exception:
        # Fallback si version Streamlit plus ancienne
        st.dataframe(show, use_container_width=True)

    csv_bytes = show.to_csv(index=False).encode("utf-8")
    st.download_button("Exporter la shortlist (CSV)", data=csv_bytes,
                       file_name="shortlist.csv", mime="text/csv")


# =========================================================
#                         APP
# =========================================================

def main():
    st.set_page_config(page_title="2ememain — Vue épurée", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)

    # Args
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--in", dest="in_path", required=False, default="scored_berlingot.csv")
    args, _ = parser.parse_known_args()

    # En-tête
    st.markdown("<h1>2ememain — Décision éclair</h1>", unsafe_allow_html=True)
    st.caption("Interface épurée. Filtre, compare, shortlist. Labellisation nuancée (vrais bons plans vs. deals suspects).")

    # Chargement
    try:
        df_raw = read_any(args.in_path)
        st.info(f"Fichier chargé : **{args.in_path}**")
    except Exception as e:
        st.error(f"Impossible de charger {args.in_path} : {e}")
        st.stop()

    df_all = enrich(df_raw)
    dff, filt = filter_df(df_all)

    # KPIs
    kpis(df_all, dff)

    st.divider()

    # Ligne de 2 graphes équilibrés
    c1, c2 = st.columns((1,1))
    with c1:
        fig_hist_discount(dff, dis_min=filt["discount_min_pct"])
    with c2:
        fig_scatter_parity(dff)

    st.divider()

    # Carte similarité
    fig_similarity_map(dff)

    st.divider()

    # Top + table
    render_top_cards(dff, n=3)
    shortlist_table(dff)

    st.caption("Astuce : utilise l’ID (#car_id) pour recouper un point du graphe avec la table et l’URL.")

if __name__ == "__main__":
    main()
