import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
from copy import deepcopy
import json, os, io, requests

# ─────────────────────────────────────────────
#  CONFIG PAGE
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="SimulSalaires · La Mandallaz",
    page_icon="🏅",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────
#  STYLE
# ─────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Serif+Display:ital@0;1&family=DM+Sans:wght@300;400;500;600&display=swap');

html, body, [class*="css"] { font-family: 'DM Sans', sans-serif; }

h1, h2, h3 { font-family: 'DM Serif Display', serif; }

.main { background: #f7f6f2; }

.kpi-card {
    background: white;
    border-radius: 12px;
    padding: 20px 24px;
    border-left: 5px solid #2d6a4f;
    box-shadow: 0 1px 4px rgba(0,0,0,0.07);
    margin-bottom: 12px;
}
.kpi-card.warning { border-left-color: #e07b39; }
.kpi-card.danger  { border-left-color: #c1121f; }
.kpi-card.ok      { border-left-color: #2d6a4f; }

.kpi-label { font-size: 12px; font-weight: 600; color: #888; text-transform: uppercase; letter-spacing: .05em; }
.kpi-value { font-size: 28px; font-family: 'DM Serif Display', serif; color: #1a1a2e; margin: 4px 0; }
.kpi-sub   { font-size: 12px; color: #666; }

.section-header {
    background: #1a1a2e;
    color: white;
    padding: 10px 18px;
    border-radius: 8px;
    font-family: 'DM Serif Display', serif;
    font-size: 18px;
    margin: 24px 0 12px 0;
}
.delta-positive { color: #2d6a4f; font-weight: 600; }
.delta-negative { color: #c1121f; font-weight: 600; }

.stTabs [data-baseweb="tab-list"] { gap: 8px; }
.stTabs [data-baseweb="tab"] {
    font-family: 'DM Sans', sans-serif;
    font-weight: 500;
    background: white;
    border-radius: 8px 8px 0 0;
    border: 1px solid #e0e0e0;
}
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
#  CHARGEMENT & CACHE DES DONNÉES
# ─────────────────────────────────────────────
COEFF_CHARGES = 1.45   # Coefficient charges patronales (taux horaire brut → coût total)

@st.cache_data(show_spinner=False)
def download_from_drive(file_id: str) -> bytes:
    """Télécharge un fichier Excel depuis Google Drive en mémoire."""
    session = requests.Session()
    # Première requête
    url = "https://drive.google.com/uc"
    params = {"id": file_id, "export": "download"}
    response = session.get(url, params=params, stream=True)
    # Récupération du token de confirmation si fichier volumineux
    token = None
    for key, value in response.cookies.items():
        if key.startswith("download_warning"):
            token = value
    if token:
        params["confirm"] = token
        response = session.get(url, params=params, stream=True)
    # Lecture complète du contenu
    content = b"".join(response.iter_content(chunk_size=32768))
    # Vérification signature XLSX (fichier ZIP)
    if content[:2] != b"PK":
        raise ValueError(
            f"Le fichier Google Drive ({file_id}) n'est pas un Excel valide. "
            "Vérifiez que le partage est bien en mode 'Toute personne ayant le lien'."
        )
    return content


@st.cache_data(show_spinner=False)
def load_data_from_bytes(vol_bytes: bytes, act_bytes: bytes, cout_bytes: bytes):
    """Charge les DataFrames depuis les 3 fichiers :
    - VolumeEtTauxHoraire.xlsx → salariés
    - Activites.xlsx           → activités
    - CoutHoraire.xlsx         → coût horaire de référence
    """
    salaries_df  = pd.read_excel(io.BytesIO(vol_bytes))
    salaries_df['Tarif horaire ou mensuel'] = salaries_df['Tarif horaire ou mensuel'].astype(float)
    activites_df = pd.read_excel(io.BytesIO(act_bytes))
    cout_df      = pd.read_excel(io.BytesIO(cout_bytes), sheet_name='Feuil1')
    cout_df.columns = ['NOM', 'cout_horaire_ref']
    return salaries_df, activites_df, cout_df



def compute_cout_salarial(salaries_df: pd.DataFrame, coeff: float = COEFF_CHARGES) -> pd.DataFrame:
    """
    Calcule le coût salarial annuel total par ligne salarié.
    Volume annuel = Durée hebdo × nb semaines + 1h entretien annuel (sur ligne principale du salarié).
    Cette logique est intégrée dans la colonne Volume annuel du fichier source.
    coût = taux_horaire_brut × Volume annuel × coeff_charges
    """
    df = salaries_df.copy()
    df['Volume annuel'] = df['Durée hebdo'] * df['nb semaines']
    df['cout_annuel'] = df['Tarif horaire ou mensuel'] * df['Volume annuel'] * coeff
    return df


def compute_recettes_activites(activites_df: pd.DataFrame) -> pd.DataFrame:
    """
    Calcule la recette max par activité (si tous les participants inscrits).
    recette_max = tarif * max_participants
    """
    df = activites_df.copy()
    df['recette_max'] = df['Tarif'] * df['Max participants']
    return df


def repartir_maud(salaries_df: pd.DataFrame, activites_df: pd.DataFrame, mode: str = 'nb_cours') -> pd.DataFrame:
    """
    Répartit le coût de Maud BOCHATON (Assist. Adm.) sur toutes les activités.
    mode: 'nb_cours' (pondération égale par cours unique) ou 'volume_horaire'
    Retourne activites_df enrichi d'une colonne 'quote_part_maud'
    """
    maud = salaries_df[salaries_df['Cours'] == 'Assist. Adm.'].iloc[0]
    cout_maud = maud['Tarif horaire ou mensuel'] * maud['Volume annuel'] * COEFF_CHARGES

    df = activites_df.copy()

    if mode == 'nb_cours':
        # Répartition égale par nombre d'activités (codes)
        n = len(df)
        df['quote_part_maud'] = cout_maud / n
    else:
        # Pondération par volume horaire hebdo * nb semaines
        df['vol_total'] = df['Volume horaire hebdo'] * df['nb semaines']
        total_vol = df['vol_total'].sum()
        df['quote_part_maud'] = cout_maud * (df['vol_total'] / total_vol)
        df.drop(columns=['vol_total'], inplace=True)

    return df, cout_maud


def build_equilibre(salaries_df: pd.DataFrame, activites_df: pd.DataFrame,
                    mode_maud: str = 'nb_cours') -> pd.DataFrame:
    """
    Construit le tableau d'équilibre par cours (Cours = lien entre Salariés et Activités).
    """
    # 1. Coût salarial par cours (hors Maud)
    sal = compute_cout_salarial(salaries_df)
    sal_no_maud = sal[sal['Cours'] != 'Assist. Adm.']
    cout_par_cours = sal_no_maud.groupby('Cours')['cout_annuel'].sum().reset_index()
    cout_par_cours.columns = ['Cours', 'cout_salarial']

    # 2. Recettes par cours
    act = compute_recettes_activites(activites_df)
    act_with_maud, cout_maud = repartir_maud(salaries_df, act, mode_maud)
    recette_par_cours = act_with_maud.groupby('Cours').agg(
        recette_max=('recette_max', 'sum'),
        quote_part_maud=('quote_part_maud', 'sum'),
        nb_activites=('Code', 'count'),
    ).reset_index()

    # 3. Merge
    df = recette_par_cours.merge(cout_par_cours, on='Cours', how='left')
    df['cout_salarial'] = df['cout_salarial'].fillna(0)
    df['cout_total'] = df['cout_salarial'] + df['quote_part_maud']
    df['solde'] = df['recette_max'] - df['cout_total']
    df['taux_couverture'] = (df['recette_max'] / df['cout_total'] * 100).round(1)

    return df.sort_values('solde')


def build_equilibre_activite(salaries_df: pd.DataFrame, activites_df: pd.DataFrame,
                              mode_maud: str = 'nb_cours') -> pd.DataFrame:
    """Équilibre au niveau de chaque code activité."""
    act_with_maud, _ = repartir_maud(salaries_df, activites_df, mode_maud)
    act = compute_recettes_activites(act_with_maud)

    # Coût salarial par cours, réparti au prorata du volume horaire des activités
    sal = compute_cout_salarial(salaries_df)
    sal_no_maud = sal[sal['Cours'] != 'Assist. Adm.']
    cout_par_cours = sal_no_maud.groupby('Cours')['cout_annuel'].sum()

    # Volume horaire total par cours dans les activités
    vol_par_cours = act.groupby('Cours').apply(
        lambda g: (g['Volume horaire hebdo'] * g['nb semaines']).sum()
    )

    def get_cout_activite(row):
        cours = row['Cours']
        if cours not in cout_par_cours.index or vol_par_cours.get(cours, 0) == 0:
            return 0
        vol_act = row['Volume horaire hebdo'] * row['nb semaines']
        return cout_par_cours[cours] * vol_act / vol_par_cours[cours]

    act['cout_salarial'] = act.apply(get_cout_activite, axis=1)
    act['cout_total'] = act['cout_salarial'] + act['quote_part_maud']
    act['recette_max'] = act['Tarif'] * act['Max participants']
    act['solde'] = act['recette_max'] - act['cout_total']
    act['taux_couverture'] = (act['recette_max'] / act['cout_total'] * 100).round(1)
    act['cout_par_participant'] = (act['cout_total'] / act['Max participants']).round(2)
    act['min_participants'] = (act['cout_total'] / act['Tarif']).apply(
        lambda x: int(x) + (1 if x % 1 > 0 else 0))

    return act.sort_values('solde')


# ─────────────────────────────────────────────
#  DASHBOARD PROJETÉ : fonctions
# ─────────────────────────────────────────────

def build_equilibre_projete(salaries_df: pd.DataFrame, activites_df: pd.DataFrame,
                             mode_maud: str = 'nb_cours') -> pd.DataFrame:
    """Comme build_equilibre mais utilise Projection participants au lieu de Max participants."""
    sal = compute_cout_salarial(salaries_df)
    sal_no_maud = sal[sal['Cours'] != 'Assist. Adm.']
    cout_par_cours = sal_no_maud.groupby('Cours')['cout_annuel'].sum().reset_index()
    cout_par_cours.columns = ['Cours', 'cout_salarial']

    act = activites_df.copy()
    act['recette_projetee'] = act['Tarif'] * act['Projection participants']
    act_with_maud, cout_maud = repartir_maud(salaries_df, act, mode_maud)
    # repartir_maud utilise recette_max — on l'ajoute temporairement
    act_with_maud['recette_max'] = act_with_maud['Tarif'] * act_with_maud['Max participants']

    recette_par_cours = act_with_maud.groupby('Cours').agg(
        recette_projetee=('recette_projetee', 'sum'),
        recette_max=('recette_max', 'sum'),
        quote_part_maud=('quote_part_maud', 'sum'),
        nb_activites=('Code', 'count'),
    ).reset_index()

    df = recette_par_cours.merge(cout_par_cours, on='Cours', how='left')
    df['cout_salarial'] = df['cout_salarial'].fillna(0)
    df['cout_total'] = df['cout_salarial'] + df['quote_part_maud']
    df['solde'] = df['recette_projetee'] - df['cout_total']
    df['taux_couverture'] = (df['recette_projetee'] / df['cout_total'] * 100).round(1)
    df['taux_remplissage'] = (df['recette_projetee'] / df['recette_max'] * 100).round(1)
    return df.sort_values('solde')


def build_equilibre_activite_projete(salaries_df: pd.DataFrame, activites_df: pd.DataFrame,
                                      mode_maud: str = 'nb_cours') -> pd.DataFrame:
    """Équilibre par code activité avec participants projetés."""
    act = activites_df.copy()
    act['recette_max'] = act['Tarif'] * act['Max participants']
    act['recette_projetee'] = act['Tarif'] * act['Projection participants']
    act_with_maud, _ = repartir_maud(salaries_df, act, mode_maud)

    sal = compute_cout_salarial(salaries_df)
    sal_no_maud = sal[sal['Cours'] != 'Assist. Adm.']
    cout_par_cours = sal_no_maud.groupby('Cours')['cout_annuel'].sum()
    vol_par_cours = act.groupby('Cours').apply(
        lambda g: (g['Volume horaire hebdo'] * g['nb semaines']).sum()
    )

    def get_cout_activite(row):
        cours = row['Cours']
        if cours not in cout_par_cours.index or vol_par_cours.get(cours, 0) == 0:
            return 0
        vol_act = row['Volume horaire hebdo'] * row['nb semaines']
        return cout_par_cours[cours] * vol_act / vol_par_cours[cours]

    act_with_maud['cout_salarial'] = act_with_maud.apply(get_cout_activite, axis=1)
    act_with_maud['cout_total'] = act_with_maud['cout_salarial'] + act_with_maud['quote_part_maud']
    act_with_maud['solde'] = act_with_maud['recette_projetee'] - act_with_maud['cout_total']
    act_with_maud['taux_couverture'] = (act_with_maud['recette_projetee'] / act_with_maud['cout_total'] * 100).round(1)
    act_with_maud['taux_remplissage'] = (act_with_maud['Projection participants'] / act_with_maud['Max participants'] * 100).round(1)
    act_with_maud['cout_par_participant'] = (act_with_maud['cout_total'] / act_with_maud['Projection participants']).round(2)
    act_with_maud['min_participants'] = (act_with_maud['cout_total'] / act_with_maud['Tarif']).apply(
        lambda x: int(x) + (1 if x % 1 > 0 else 0))
    return act_with_maud.sort_values('solde')


# ─────────────────────────────────────────────
#  INITIALISATION SESSION STATE
# ─────────────────────────────────────────────
if 'salaries_df' not in st.session_state:
    st.session_state['salaries_df'] = None
    st.session_state['activites_df'] = None
    st.session_state['recap_df'] = None
    st.session_state['sim_salaries'] = None
    st.session_state['sim_activites'] = None


def init_simulation():
    """Copie les données de base dans les DataFrames de simulation."""
    st.session_state['sim_salaries']  = st.session_state['salaries_df'].copy()
    st.session_state['sim_activites'] = st.session_state['activites_df'].copy()


# ─────────────────────────────────────────────
#  SIDEBAR : CHARGEMENT FICHIERS
# ─────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 🏅 La Mandallaz")
    st.markdown("### Chargement des données")

    vol_file  = st.file_uploader("📊 VolumeEtTauxHoraire.xlsx", type=['xlsx'], key='fu_vol')
    act_file  = st.file_uploader("🏃 Activites.xlsx",            type=['xlsx'], key='fu_act')
    cout_file = st.file_uploader("💶 CoutHoraire.xlsx",          type=['xlsx'], key='fu_cout')

    vol_bytes = act_bytes = cout_bytes = None
    source = None

    if vol_file and act_file and cout_file:
        # Priorité 1 : fichiers uploadés manuellement
        vol_bytes  = vol_file.read()
        act_bytes  = act_file.read()
        cout_bytes = cout_file.read()
        source = "upload"
        load_data_from_bytes.clear()

    else:
        # Priorité 2 : Google Drive via secrets Streamlit
        try:
            vol_id  = st.secrets["VOL_TAUX_ID"]
            act_id  = st.secrets["ACTIVITES_ID"]
            cout_id = st.secrets["COUT_HORAIRE_ID"]
            vol_bytes  = download_from_drive(vol_id)
            act_bytes  = download_from_drive(act_id)
            cout_bytes = download_from_drive(cout_id)
            source = "drive"
        except (KeyError, FileNotFoundError):
            pass

        if vol_bytes is None:
            # Priorité 3 : fichiers locaux (dev local)
            base = os.path.dirname(__file__)
            f_vol  = os.path.join(base, 'VolumeEtTauxHoraire.xlsx')
            f_act  = os.path.join(base, 'Activites.xlsx')
            f_cout = os.path.join(base, 'CoutHoraire.xlsx')
            if os.path.exists(f_vol) and os.path.exists(f_act) and os.path.exists(f_cout):
                with open(f_vol,  'rb') as f: vol_bytes  = f.read()
                with open(f_act,  'rb') as f: act_bytes  = f.read()
                with open(f_cout, 'rb') as f: cout_bytes = f.read()
                source = "local"

    if vol_bytes and act_bytes and cout_bytes:
        try:
            s, a, r = load_data_from_bytes(vol_bytes, act_bytes, cout_bytes)
            st.session_state['salaries_df']  = s
            st.session_state['activites_df'] = a
            st.session_state['recap_df']     = r
            if st.session_state['sim_salaries'] is None:
                init_simulation()
            icons = {"drive": "☁️", "upload": "📂", "local": "💾"}
            st.success(f"{icons.get(source, '')} Données chargées")
            st.caption(f"{len(s)} entrées salariés · {len(a)} activités")
        except Exception as e:
            st.error(f"Erreur chargement : {e}")
    elif not vol_file and not act_file and not cout_file and source is None:
        st.warning("Aucune source de données disponible.")

    st.divider()
    st.markdown("### Paramètres globaux")
    coeff_charges = st.number_input(
        "Coefficient charges patronales",
        min_value=1.0, max_value=2.0, value=COEFF_CHARGES, step=0.01,
        help="Multiplicateur appliqué au taux horaire brut pour obtenir le coût employeur."
    )
    COEFF_CHARGES = coeff_charges

    mode_maud = st.selectbox(
        "Répartition coût Maud (Assist. Adm.)",
        options=['nb_cours', 'volume_horaire'],
        format_func=lambda x: "Égale par activité" if x == 'nb_cours' else "Pondérée par volume horaire",
    )

    if st.session_state['sim_salaries'] is not None:
        if st.button("🔄 Réinitialiser la simulation", use_container_width=True):
            init_simulation()
            st.session_state['journal'] = []
            st.session_state['global_hausse_type'] = 'Pourcentage (%)'
            st.session_state['global_hausse_val']  = 0.0
            st.rerun()


# ─────────────────────────────────────────────
#  GUARD : données non chargées
# ─────────────────────────────────────────────
if st.session_state['salaries_df'] is None:
    st.title("SimulSalaires · La Mandallaz 🏅")
    st.info("👈 Chargez vos 3 fichiers Excel dans la barre latérale pour commencer : VolumeEtTauxHoraire, Activités, CoutHoraire.")
    st.stop()

# Raccourcis
sal_base  = st.session_state['salaries_df']
act_base  = st.session_state['activites_df']
sal_sim   = st.session_state['sim_salaries']
act_sim   = st.session_state['sim_activites']

# ─────────────────────────────────────────────
#  HEADER
# ─────────────────────────────────────────────
st.markdown("""
<div style="display:flex; align-items:center; gap:16px; margin-bottom:8px;">
  <div>
    <h1 style="margin:0; font-family:'DM Serif Display',serif; font-size:2.2rem; color:#1a1a2e;">
      SimulSalaires · La Mandallaz
    </h1>
    <p style="margin:0; color:#666; font-size:14px;">Simulateur d'équilibre financier Inscriptions / Coûts salariaux</p>
  </div>
</div>
""", unsafe_allow_html=True)

tab_max, tab_proj, tab_sim, tab_data = st.tabs([
    "📊 Dashboard effectif maximisé",
    "📈 Dashboard effectif projeté",
    "🔧 Simulateur",
    "📋 Données",
])

# ─────────────────────────────────────────────
#  TAB 1 : DASHBOARD
# ─────────────────────────────────────────────
with tab_max:
    eq_cours = build_equilibre(sal_sim, act_sim, mode_maud)
    eq_act   = build_equilibre_activite(sal_sim, act_sim, mode_maud)

    total_recettes  = eq_cours['recette_max'].sum()
    total_couts     = eq_cours['cout_total'].sum()
    solde_global    = total_recettes - total_couts
    taux_global     = total_recettes / total_couts * 100 if total_couts > 0 else 0
    nb_deficit      = (eq_cours['solde'] < 0).sum()

    # ── KPIs ──
    c1, c2, c3, c4 = st.columns(4)
    def kpi(col, label, value, sub="", status="ok"):
        col.markdown(f"""
        <div class="kpi-card {status}">
          <div class="kpi-label">{label}</div>
          <div class="kpi-value">{value}</div>
          <div class="kpi-sub">{sub}</div>
        </div>""", unsafe_allow_html=True)

    kpi(c1, "Recettes max (inscriptions)", f"{total_recettes:,.0f} €",
        "Si tous les créneaux sont pleins", "ok")
    kpi(c2, "Coût salarial total", f"{total_couts:,.0f} €",
        f"Dont Maud répartie", "ok")
    kpi(c3, "Solde global", f"{solde_global:+,.0f} €",
        f"Taux de couverture : {taux_global:.1f} %",
        "ok" if solde_global >= 0 else "danger")
    kpi(c4, "Cours en déficit", f"{nb_deficit} / {len(eq_cours)}",
        "Cours dont les inscriptions ne couvrent pas le coût", 
        "ok" if nb_deficit == 0 else ("warning" if nb_deficit <= 2 else "danger"))

    st.divider()

    # ── Graphique équilibre par cours ──
    col_g1, col_g2 = st.columns([3, 2])

    with col_g1:
        st.markdown("#### Solde par cours (Recettes − Coûts)")
        colors = ['#c1121f' if v < 0 else '#2d6a4f' for v in eq_cours['solde']]
        fig_bar = go.Figure(go.Bar(
            x=eq_cours['Cours'],
            y=eq_cours['solde'],
            marker_color=colors,
            text=[f"{v:+,.0f} €" for v in eq_cours['solde']],
            textposition='outside',
            hovertemplate="<b>%{x}</b><br>Solde : %{y:,.0f} €<extra></extra>",
        ))
        fig_bar.add_hline(y=0, line_dash="dash", line_color="#888", line_width=1)
        fig_bar.update_layout(
            height=380, margin=dict(t=20, b=40, l=0, r=0),
            plot_bgcolor='white', paper_bgcolor='rgba(0,0,0,0)',
            yaxis=dict(title="Solde (€)", gridcolor='#f0f0f0'),
            xaxis=dict(tickangle=-30),
            font=dict(family='DM Sans'),
        )
        st.plotly_chart(fig_bar, use_container_width=True)

    with col_g2:
        st.markdown("#### Taux de couverture par cours")
        fig_cov = go.Figure(go.Bar(
            x=eq_cours['taux_couverture'],
            y=eq_cours['Cours'],
            orientation='h',
            marker_color=['#c1121f' if v < 100 else '#2d6a4f' for v in eq_cours['taux_couverture']],
            text=[f"{v:.0f} %" for v in eq_cours['taux_couverture']],
            textposition='outside',
        ))
        fig_cov.add_vline(x=100, line_dash="dash", line_color="#e07b39", line_width=2)
        fig_cov.update_layout(
            height=380, margin=dict(t=20, b=20, l=0, r=60),
            plot_bgcolor='white', paper_bgcolor='rgba(0,0,0,0)',
            xaxis=dict(title="Taux de couverture (%)", gridcolor='#f0f0f0'),
            font=dict(family='DM Sans'),
        )
        st.plotly_chart(fig_cov, use_container_width=True)

    # ── Tableau de synthèse par cours ──
    st.markdown("#### Détail par cours")
    display_cours = eq_cours.copy()
    display_cours = display_cours.rename(columns={
        'Cours': 'Cours', 'recette_max': 'Recettes max (€)',
        'quote_part_maud': 'Quote-part Maud (€)', 'cout_salarial': 'Coût salarial (€)',
        'cout_total': 'Coût total (€)', 'solde': 'Solde (€)',
        'taux_couverture': 'Taux couverture (%)', 'nb_activites': 'Nb activités',
    })
    for col in ['Recettes max (€)', 'Quote-part Maud (€)', 'Coût salarial (€)', 'Coût total (€)', 'Solde (€)']:
        display_cours[col] = display_cours[col].apply(lambda x: f"{x:,.0f}")

    def highlight_solde(row):
        try:
            v = float(row['Solde (€)'].replace(',', '').replace(' ', ''))
            color = '#fde8e8' if v < 0 else ('#e8f5e9' if v > 0 else 'white')
        except:
            color = 'white'
        return [f'background-color: {color}'] * len(row)

    st.dataframe(
        display_cours[['Cours', 'Nb activités', 'Recettes max (€)', 'Coût salarial (€)',
                        'Quote-part Maud (€)', 'Coût total (€)', 'Solde (€)', 'Taux couverture (%)']],
        use_container_width=True, hide_index=True,
    )

    # ── Vue par activité ──
    st.markdown("#### Détail par activité (code)")
    with st.expander("Voir le détail par code activité", expanded=False):
        has_proj_col = 'Projection participants' in eq_act.columns
        cols_show = ['Code', 'Activité', 'Type', 'Public', 'Cours',
                     'Tarif', 'Max participants', 'recette_max',
                     'cout_total', 'solde', 'taux_couverture', 'min_participants']
        col_labels = ['Code', 'Activité', 'Type', 'Public', 'Cours',
                      'Tarif (€)', 'Max. participants', 'Recettes max (€)',
                      'Coût total (€)', 'Solde (€)', 'Taux cov. (%)', 'Min. participants']
        if has_proj_col:
            cols_show.insert(7, 'Projection participants')
            col_labels.insert(7, 'Part. projetés')
        disp_act = eq_act[cols_show].copy()
        disp_act.columns = col_labels
        for c in ['Recettes max (€)', 'Coût total (€)', 'Solde (€)']:
            disp_act[c] = disp_act[c].apply(lambda x: f"{x:,.0f}")
        st.dataframe(disp_act, use_container_width=True, hide_index=True)

    # ── Vue par Type / Public ──
    st.markdown("#### Vue synthétique par catégorie")
    col_t1, col_t2 = st.columns(2)

    with col_t1:
        eq_type = eq_act.groupby('Type').agg(
            recette_max=('recette_max','sum'),
            cout_total=('cout_total','sum'),
        ).reset_index()
        eq_type['solde'] = eq_type['recette_max'] - eq_type['cout_total']
        fig_type = go.Figure(data=[
            go.Bar(name='Recettes', x=eq_type['Type'], y=eq_type['recette_max'],
                   marker_color='#2d6a4f'),
            go.Bar(name='Coûts', x=eq_type['Type'], y=eq_type['cout_total'],
                   marker_color='#e07b39'),
        ])
        fig_type.update_layout(
            barmode='group', title="Par type (Sport / Art)",
            height=280, margin=dict(t=40,b=20,l=0,r=0),
            plot_bgcolor='white', paper_bgcolor='rgba(0,0,0,0)',
            font=dict(family='DM Sans'),
        )
        st.plotly_chart(fig_type, use_container_width=True)

    with col_t2:
        eq_pub = eq_act.groupby('Public').agg(
            recette_max=('recette_max','sum'),
            cout_total=('cout_total','sum'),
        ).reset_index()
        fig_pub = go.Figure(data=[
            go.Bar(name='Recettes', x=eq_pub['Public'], y=eq_pub['recette_max'],
                   marker_color='#2d6a4f'),
            go.Bar(name='Coûts', x=eq_pub['Public'], y=eq_pub['cout_total'],
                   marker_color='#e07b39'),
        ])
        fig_pub.update_layout(
            barmode='group', title="Par public (Adulte / Enfant)",
            height=280, margin=dict(t=40,b=20,l=0,r=0),
            plot_bgcolor='white', paper_bgcolor='rgba(0,0,0,0)',
            font=dict(family='DM Sans'),
        )
        st.plotly_chart(fig_pub, use_container_width=True)



# ─────────────────────────────────────────────
#  TAB 2 : DASHBOARD PROJETÉ
# ─────────────────────────────────────────────
with tab_proj:
    # Vérification que la colonne Projection participants existe
    if 'Projection participants' not in act_sim.columns:
        st.error("⚠️ La colonne 'Projection participants' est absente du fichier SimulSalaires.xlsx. "
                 "Veuillez recharger le fichier mis à jour.")
        st.stop()

    eq_proj_cours = build_equilibre_projete(sal_sim, act_sim, mode_maud)
    eq_proj_act   = build_equilibre_activite_projete(sal_sim, act_sim, mode_maud)

    total_rec_proj  = eq_proj_cours['recette_projetee'].sum()
    total_rec_max   = eq_proj_cours['recette_max'].sum()
    total_couts_p   = eq_proj_cours['cout_total'].sum()
    solde_proj      = total_rec_proj - total_couts_p
    taux_proj       = total_rec_proj / total_couts_p * 100 if total_couts_p > 0 else 0
    taux_rempl_glob = total_rec_proj / total_rec_max * 100 if total_rec_max > 0 else 0
    nb_deficit_p    = (eq_proj_cours['solde'] < 0).sum()

    # ── KPIs ──
    cp1, cp2, cp3, cp4 = st.columns(4)
    kpi(cp1, "Recettes projetées", f"{total_rec_proj:,.0f} €",
        f"Taux de remplissage global : {taux_rempl_glob:.1f} %", "ok")
    kpi(cp2, "Coût salarial total", f"{total_couts_p:,.0f} €",
        "Dont Maud répartie", "ok")
    kpi(cp3, "Solde projeté", f"{solde_proj:+,.0f} €",
        f"Taux de couverture : {taux_proj:.1f} %",
        "ok" if solde_proj >= 0 else "danger")
    kpi(cp4, "Cours en déficit", f"{nb_deficit_p} / {len(eq_proj_cours)}",
        "Avec les participants projetés",
        "ok" if nb_deficit_p == 0 else ("warning" if nb_deficit_p <= 2 else "danger"))

    st.divider()

    # ── Graphique solde projeté par cours ──
    col_pg1, col_pg2 = st.columns([3, 2])

    with col_pg1:
        st.markdown("#### Solde par cours (Recettes projetées − Coûts)")
        colors_p = ["#c1121f" if v < 0 else "#2d6a4f" for v in eq_proj_cours["solde"]]
        fig_pbar = go.Figure(go.Bar(
            x=eq_proj_cours["Cours"],
            y=eq_proj_cours["solde"],
            marker_color=colors_p,
            text=[f"{v:+,.0f} €" for v in eq_proj_cours["solde"]],
            textposition="outside",
            hovertemplate="<b>%{x}</b><br>Solde projeté : %{y:,.0f} €<extra></extra>",
        ))
        fig_pbar.add_hline(y=0, line_dash="dash", line_color="#888", line_width=1)
        fig_pbar.update_layout(
            height=380, margin=dict(t=20, b=40, l=0, r=0),
            plot_bgcolor="white", paper_bgcolor="rgba(0,0,0,0)",
            yaxis=dict(title="Solde (€)", gridcolor="#f0f0f0"),
            xaxis=dict(tickangle=-30),
            font=dict(family="DM Sans"),
        )
        st.plotly_chart(fig_pbar, use_container_width=True)

    with col_pg2:
        st.markdown("#### Taux de remplissage par cours")
        fig_rempl = go.Figure(go.Bar(
            x=eq_proj_cours["taux_remplissage"],
            y=eq_proj_cours["Cours"],
            orientation="h",
            marker_color=["#c1121f" if v < 70 else ("#e07b39" if v < 90 else "#2d6a4f")
                          for v in eq_proj_cours["taux_remplissage"]],
            text=[f"{v:.0f} %" for v in eq_proj_cours["taux_remplissage"]],
            textposition="outside",
        ))
        fig_rempl.add_vline(x=100, line_dash="dash", line_color="#888", line_width=1)
        fig_rempl.add_vline(x=80, line_dash="dot", line_color="#e07b39", line_width=1)
        fig_rempl.update_layout(
            height=380, margin=dict(t=20, b=20, l=0, r=60),
            plot_bgcolor="white", paper_bgcolor="rgba(0,0,0,0)",
            xaxis=dict(title="Taux de remplissage (%)", gridcolor="#f0f0f0"),
            font=dict(family="DM Sans"),
        )
        st.plotly_chart(fig_rempl, use_container_width=True)

    # ── Comparaison Max vs Projeté ──
    st.markdown("#### Comparaison Maximisé vs Projeté par cours")
    eq_max_cours = build_equilibre(sal_sim, act_sim, mode_maud)
    merged_p = eq_max_cours[["Cours","recette_max","solde"]].merge(
        eq_proj_cours[["Cours","recette_projetee","solde","taux_remplissage"]],
        on="Cours"
    )
    merged_p.columns = ["Cours","Recettes max","Solde max","Recettes projetées","Solde projeté","Remplissage (%)"]
    fig_comp_p = go.Figure()
    fig_comp_p.add_trace(go.Bar(
        name="Solde maximisé", x=merged_p["Cours"], y=merged_p["Solde max"],
        marker_color="#adb5bd", opacity=0.7,
    ))
    fig_comp_p.add_trace(go.Bar(
        name="Solde projeté", x=merged_p["Cours"], y=merged_p["Solde projeté"],
        marker_color=["#2d6a4f" if v >= 0 else "#c1121f" for v in merged_p["Solde projeté"]],
    ))
    fig_comp_p.add_hline(y=0, line_dash="dash", line_color="#555")
    fig_comp_p.update_layout(
        barmode="group", height=360,
        margin=dict(t=20, b=40, l=0, r=0),
        plot_bgcolor="white", paper_bgcolor="rgba(0,0,0,0)",
        font=dict(family="DM Sans"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    st.plotly_chart(fig_comp_p, use_container_width=True)

    # ── Tableau de synthèse par cours ──
    st.markdown("#### Détail par cours")
    disp_proj = eq_proj_cours[["Cours","nb_activites","recette_projetee","recette_max",
                                "cout_salarial","quote_part_maud","cout_total",
                                "solde","taux_couverture","taux_remplissage"]].copy()
    disp_proj.columns = ["Cours","Nb activités","Recettes projetées (€)","Recettes max (€)",
                          "Coût salarial (€)","Quote-part Maud (€)","Coût total (€)",
                          "Solde (€)","Taux couverture (%)","Taux remplissage (%)"]
    for c in ["Recettes projetées (€)","Recettes max (€)","Coût salarial (€)",
              "Quote-part Maud (€)","Coût total (€)","Solde (€)"]:
        disp_proj[c] = disp_proj[c].apply(lambda x: f"{x:,.0f}")
    st.dataframe(disp_proj, use_container_width=True, hide_index=True)

    # ── Détail par activité ──
    st.markdown("#### Détail par activité (code)")
    with st.expander("Voir le détail par code activité", expanded=False):
        cols_p = ["Code","Activité","Type","Public","Cours","Tarif",
                  "Max participants","Projection participants",
                  "recette_projetee","cout_total","solde","taux_couverture",
                  "taux_remplissage","min_participants","cout_par_participant"]
        disp_pa = eq_proj_act[cols_p].copy()
        disp_pa.columns = ["Code","Activité","Type","Public","Cours","Tarif (€)",
                            "Max part.","Part. projetés",
                            "Recettes proj. (€)","Coût total (€)","Solde (€)",
                            "Taux cov. (%)","Remplissage (%)","Min. participants","Coût/participant (€)"]
        for c in ["Recettes proj. (€)","Coût total (€)","Solde (€)"]:
            disp_pa[c] = disp_pa[c].apply(lambda x: f"{x:,.0f}")
        st.dataframe(disp_pa, use_container_width=True, hide_index=True)

    # ── Vue par Type / Public ──
    st.markdown("#### Vue synthétique par catégorie")
    col_pt1, col_pt2 = st.columns(2)
    with col_pt1:
        eq_ptype = eq_proj_act.groupby("Type").agg(
            recette_projetee=("recette_projetee","sum"),
            cout_total=("cout_total","sum"),
        ).reset_index()
        fig_ptype = go.Figure(data=[
            go.Bar(name="Recettes proj.", x=eq_ptype["Type"], y=eq_ptype["recette_projetee"],
                   marker_color="#2d6a4f"),
            go.Bar(name="Coûts", x=eq_ptype["Type"], y=eq_ptype["cout_total"],
                   marker_color="#e07b39"),
        ])
        fig_ptype.update_layout(
            barmode="group", title="Par type (Sport / Art)",
            height=280, margin=dict(t=40,b=20,l=0,r=0),
            plot_bgcolor="white", paper_bgcolor="rgba(0,0,0,0)",
            font=dict(family="DM Sans"),
        )
        st.plotly_chart(fig_ptype, use_container_width=True)

    with col_pt2:
        eq_ppub = eq_proj_act.groupby("Public").agg(
            recette_projetee=("recette_projetee","sum"),
            cout_total=("cout_total","sum"),
        ).reset_index()
        fig_ppub = go.Figure(data=[
            go.Bar(name="Recettes proj.", x=eq_ppub["Public"], y=eq_ppub["recette_projetee"],
                   marker_color="#2d6a4f"),
            go.Bar(name="Coûts", x=eq_ppub["Public"], y=eq_ppub["cout_total"],
                   marker_color="#e07b39"),
        ])
        fig_ppub.update_layout(
            barmode="group", title="Par public (Adulte / Enfant)",
            height=280, margin=dict(t=40,b=20,l=0,r=0),
            plot_bgcolor="white", paper_bgcolor="rgba(0,0,0,0)",
            font=dict(family="DM Sans"),
        )
        st.plotly_chart(fig_ppub, use_container_width=True)

# ─────────────────────────────────────────────
#  TAB 2 : SIMULATEUR
# ─────────────────────────────────────────────
with tab_sim:

    # ── Réinitialisation ──
    col_rst1, col_rst2 = st.columns([4, 1])
    with col_rst1:
        st.markdown("### Modifier les paramètres et observer l'impact en temps réel")
        st.caption("L'augmentation globale s'applique sur les taux de base. "
                   "Les modifications individuelles s'y cumulent. "
                   "Le journal liste toutes les modifications individuelles actives.")
    with col_rst2:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("🔄 Réinitialiser", key='btn_reset_sim', use_container_width=True):
            init_simulation()
            st.session_state['journal'] = []
            st.session_state['global_hausse_type'] = 'Pourcentage (%)'
            st.session_state['global_hausse_val']  = 0.0
            st.rerun()

    # ── Initialisation session state simulation ──
    if 'journal' not in st.session_state:
        st.session_state['journal'] = []
    if 'global_hausse_type' not in st.session_state:
        st.session_state['global_hausse_type'] = 'Pourcentage (%)'
    if 'global_hausse_val' not in st.session_state:
        st.session_state['global_hausse_val'] = 0.0

    def appliquer_simulation():
        """Recalcule sim_salaries depuis la base en appliquant :
        1. L'augmentation globale (% ou €/h) sur tous les salariés
        2. Les modifications individuelles du journal par-dessus
        """
        df = st.session_state['salaries_df'].copy()
        df['Tarif horaire ou mensuel'] = df['Tarif horaire ou mensuel'].astype(float)

        # 1. Augmentation globale
        htype = st.session_state['global_hausse_type']
        hval  = st.session_state['global_hausse_val']
        if hval != 0:
            if htype == 'Pourcentage (%)':
                df['Tarif horaire ou mensuel'] *= (1 + hval / 100)
            else:
                df['Tarif horaire ou mensuel'] += hval

        # 2. Modifications individuelles du journal (salariés)
        for entry in st.session_state['journal']:
            if entry['type'] == 'salarié':
                mask = df['NOM'] == entry['nom']
                df.loc[mask, 'Tarif horaire ou mensuel'] = float(entry['new_taux'])
                df.loc[mask, 'nb semaines'] = entry['new_sem']
                for idx in df[mask].index:
                    df.at[idx, 'Volume annuel'] = df.at[idx, 'Durée hebdo'] * entry['new_sem']

        df['Volume annuel'] = df['Durée hebdo'] * df['nb semaines']
        st.session_state['sim_salaries'] = df

        # Modifications individuelles activités
        df_act = st.session_state['activites_df'].copy()
        for entry in st.session_state['journal']:
            if entry['type'] == 'activité':
                idx = df_act[df_act['Code'] == entry['code']].index[0]
                df_act.at[idx, 'Tarif']               = entry['new_tarif']
                df_act.at[idx, 'Max participants']     = entry['new_max_p']
                df_act.at[idx, 'Volume horaire hebdo'] = entry['new_vol_h']
                df_act.at[idx, 'nb semaines']          = entry['new_sem_a']
                if entry.get('new_proj_p') is not None and 'Projection participants' in df_act.columns:
                    df_act.at[idx, 'Projection participants'] = entry['new_proj_p']
        st.session_state['sim_activites'] = df_act

    # ── Section A : Augmentation globale ──
    st.markdown('<div class="section-header">A · Augmentation globale des salaires (tous salariés)</div>',
                unsafe_allow_html=True)

    col_a1, col_a2, col_a3 = st.columns([2, 2, 1])
    with col_a1:
        hausse_type = st.radio(
            "Type d'augmentation",
            ["Pourcentage (%)", "Montant fixe (€/h)"],
            index=0 if st.session_state['global_hausse_type'] == 'Pourcentage (%)' else 1,
            horizontal=True, key='hausse_type'
        )
    with col_a2:
        if hausse_type == "Pourcentage (%)":
            hausse_val = st.number_input(
                "Augmentation (%)",
                min_value=-50.0, max_value=100.0, step=0.5,
                value=st.session_state['global_hausse_val'] if st.session_state['global_hausse_type'] == 'Pourcentage (%)' else 0.0,
                key='hausse_pct',
                help="Valeur appliquée sur les taux de base originaux"
            )
        else:
            hausse_val = st.number_input(
                "Augmentation (€/h)",
                min_value=-20.0, max_value=50.0, step=0.5,
                value=st.session_state['global_hausse_val'] if st.session_state['global_hausse_type'] == 'Montant fixe (€/h)' else 0.0,
                key='hausse_eur',
                help="Valeur appliquée sur les taux de base originaux"
            )
    with col_a3:
        st.markdown("<br>", unsafe_allow_html=True)
        apply_global = st.button("▶ Appliquer", key='btn_global', use_container_width=True)

    if apply_global:
        st.session_state['global_hausse_type'] = hausse_type
        st.session_state['global_hausse_val']  = hausse_val
        appliquer_simulation()
        st.rerun()

    # Affichage du statut global actuel
    hv = st.session_state['global_hausse_val']
    ht = st.session_state['global_hausse_type']
    if hv != 0:
        unite = "%" if ht == "Pourcentage (%)" else "€/h"
        signe = "+" if hv > 0 else ""
        st.info(f"📌 Augmentation globale active : **{signe}{hv} {unite}** sur tous les taux de base")
    else:
        st.caption("Aucune augmentation globale active.")

    # ── Section B : Modification individuelle salarié ──
    st.markdown('<div class="section-header">B · Modifier un salarié</div>', unsafe_allow_html=True)

    # On affiche les taux de base + augmentation globale pour que l'utilisateur
    # voit le point de départ réel avant son ajustement individuel
    df_base_with_global = st.session_state['salaries_df'].copy()
    df_base_with_global['Tarif horaire ou mensuel'] = df_base_with_global['Tarif horaire ou mensuel'].astype(float)
    hv = st.session_state['global_hausse_val']
    ht = st.session_state['global_hausse_type']
    if hv != 0:
        if ht == 'Pourcentage (%)':
            df_base_with_global['Tarif horaire ou mensuel'] *= (1 + hv / 100)
        else:
            df_base_with_global['Tarif horaire ou mensuel'] += hv

    noms_uniques = sorted(sal_base['NOM'].unique().tolist())
    col_b1, col_b2, col_b3 = st.columns([3, 2, 2])
    with col_b1:
        sel_salarie = st.selectbox("Salarié", noms_uniques, key='sel_salarie')

    lignes_base_global = df_base_with_global[df_base_with_global['NOM'] == sel_salarie]
    taux_apres_global  = float(lignes_base_global['Tarif horaire ou mensuel'].iloc[0])
    sem_base           = int(lignes_base_global['nb semaines'].iloc[0])

    # Valeurs du journal UNIQUEMENT si elles concernent ce salarié
    entry_exist = next((e for e in st.session_state['journal']
                        if e['type'] == 'salarié' and e['nom'] == sel_salarie), None)
    # Les keys incluent le nom du salarié → Streamlit recrée les widgets à chaque changement
    taux_default = float(entry_exist['new_taux']) if entry_exist else taux_apres_global
    sem_default  = int(entry_exist['new_sem'])    if entry_exist else sem_base

    with col_b2:
        new_taux = st.number_input(
            f"Taux horaire brut (€/h) — base{'+global' if hv != 0 else ''} : {taux_apres_global:.2f} €",
            min_value=0.0, max_value=200.0, value=taux_default, step=0.5,
            key=f'new_taux_{sel_salarie}'
        )
    with col_b3:
        new_sem = st.number_input(
            f"Nb semaines — base : {sem_base}",
            min_value=1, max_value=52, value=sem_default, step=1,
            key=f'new_sem_sal_{sel_salarie}'
        )

    apply_sal = st.button("▶ Appliquer modification salarié", key='btn_sal')
    if apply_sal:
        # Mise à jour ou ajout dans le journal
        taux_base_orig = float(sal_base[sal_base['NOM'] == sel_salarie]['Tarif horaire ou mensuel'].iloc[0])
        sem_base_orig  = int(sal_base[sal_base['NOM'] == sel_salarie]['nb semaines'].iloc[0])
        label = (f"Salarié · {sel_salarie} — "
                 f"taux : {taux_base_orig:.2f} → {new_taux:.2f} €/h"
                 + (f", semaines : {sem_base_orig} → {new_sem}" if new_sem != sem_base_orig else ""))
        new_entry = {'type': 'salarié', 'nom': sel_salarie,
                     'new_taux': new_taux, 'new_sem': new_sem, 'label': label}
        journal = [e for e in st.session_state['journal']
                   if not (e['type'] == 'salarié' and e['nom'] == sel_salarie)]
        journal.append(new_entry)
        st.session_state['journal'] = journal
        appliquer_simulation()
        st.rerun()

    # ── Section C : Modifier une activité ──
    st.markdown('<div class="section-header">C · Modifier une activité</div>', unsafe_allow_html=True)

    has_proj_sim = 'Projection participants' in act_base.columns
    codes_uniques = sorted(act_base['Code'].unique().tolist())
    ncols = 6 if has_proj_sim else 5
    if has_proj_sim:
        col_c0, col_c1, col_c2, col_c3, col_c4, col_c5 = st.columns([1, 2, 2, 2, 2, 2])
    else:
        col_c0, col_c1, col_c2, col_c3, col_c4 = st.columns([1, 2, 2, 2, 2])
    with col_c0:
        sel_code = st.selectbox("Code", codes_uniques, key='sel_code')

    ligne_act_base = act_base[act_base['Code'] == sel_code].iloc[0]
    # Journal : valeurs existantes pour ce code uniquement
    entry_act = next((e for e in st.session_state['journal']
                      if e['type'] == 'activité' and e['code'] == sel_code), None)

    with col_c1:
        new_tarif = st.number_input(
            f"Tarif (€) — base : {ligne_act_base['Tarif']:.0f}",
            min_value=0.0, max_value=2000.0, step=5.0,
            value=float(entry_act['new_tarif'] if entry_act else ligne_act_base['Tarif']),
            key=f'new_tarif_{sel_code}'
        )
    with col_c2:
        new_max_p = st.number_input(
            f"Max participants — base : {ligne_act_base['Max participants']}",
            min_value=1, max_value=200, step=1,
            value=int(entry_act['new_max_p'] if entry_act else ligne_act_base['Max participants']),
            key=f'new_max_p_{sel_code}'
        )
    if has_proj_sim:
        with col_c3:
            new_proj_p = st.number_input(
                f"Part. projetés — base : {int(ligne_act_base['Projection participants'])}",
                min_value=0, max_value=200, step=1,
                value=int(entry_act['new_proj_p'] if entry_act else ligne_act_base['Projection participants']),
                key=f'new_proj_p_{sel_code}'
            )
        with col_c4:
            new_vol_h = st.number_input(
                f"Volume hebdo (h) — base : {ligne_act_base['Volume horaire hebdo']}",
                min_value=0.25, max_value=20.0, step=0.25,
                value=float(entry_act['new_vol_h'] if entry_act else ligne_act_base['Volume horaire hebdo']),
                key=f'new_vol_h_{sel_code}'
            )
        with col_c5:
            new_sem_a = st.number_input(
                f"Nb semaines — base : {ligne_act_base['nb semaines']}",
                min_value=1, max_value=52, step=1,
                value=int(entry_act['new_sem_a'] if entry_act else ligne_act_base['nb semaines']),
                key=f'new_sem_act_{sel_code}'
            )
    else:
        new_proj_p = None
        with col_c3:
            new_vol_h = st.number_input(
                f"Volume hebdo (h) — base : {ligne_act_base['Volume horaire hebdo']}",
                min_value=0.25, max_value=20.0, step=0.25,
                value=float(entry_act['new_vol_h'] if entry_act else ligne_act_base['Volume horaire hebdo']),
                key=f'new_vol_h_{sel_code}'
            )
        with col_c4:
            new_sem_a = st.number_input(
                f"Nb semaines — base : {ligne_act_base['nb semaines']}",
                min_value=1, max_value=52, step=1,
                value=int(entry_act['new_sem_a'] if entry_act else ligne_act_base['nb semaines']),
                key=f'new_sem_act_{sel_code}'
            )

    apply_act = st.button("▶ Appliquer modification activité", key='btn_act')
    if apply_act:
        parts = []
        if new_tarif != ligne_act_base['Tarif']:
            parts.append(f"tarif : {ligne_act_base['Tarif']:.0f} → {new_tarif:.0f} €")
        if new_max_p != ligne_act_base['Max participants']:
            parts.append(f"max : {ligne_act_base['Max participants']} → {new_max_p}")
        if has_proj_sim and new_proj_p != int(ligne_act_base['Projection participants']):
            parts.append(f"projetés : {int(ligne_act_base['Projection participants'])} → {new_proj_p}")
        if new_vol_h != ligne_act_base['Volume horaire hebdo']:
            parts.append(f"vol : {ligne_act_base['Volume horaire hebdo']} → {new_vol_h} h")
        if new_sem_a != ligne_act_base['nb semaines']:
            parts.append(f"sem : {ligne_act_base['nb semaines']} → {new_sem_a}")
        label = f"Activité · {sel_code} ({ligne_act_base['Activité']}) — " + (", ".join(parts) if parts else "aucun changement")
        new_entry = {'type': 'activité', 'code': sel_code,
                     'new_tarif': new_tarif, 'new_max_p': new_max_p,
                     'new_proj_p': new_proj_p, 'new_vol_h': new_vol_h,
                     'new_sem_a': new_sem_a, 'label': label}
        journal = [e for e in st.session_state['journal']
                   if not (e['type'] == 'activité' and e['code'] == sel_code)]
        journal.append(new_entry)
        st.session_state['journal'] = journal
        appliquer_simulation()
        st.rerun()

    # ── Journal des modifications ──
    st.markdown('<div class="section-header">📋 Journal des modifications individuelles</div>',
                unsafe_allow_html=True)

    if not st.session_state['journal']:
        st.caption("Aucune modification individuelle enregistrée.")
    else:
        st.caption("Cochez les modifications à annuler puis cliquez sur le bouton.")
        to_cancel = []
        for i, entry in enumerate(st.session_state['journal']):
            checked = st.checkbox(entry['label'], key=f'journal_{i}')
            if checked:
                to_cancel.append(i)

        if to_cancel:
            if st.button(f"🗑 Annuler les {len(to_cancel)} modification(s) sélectionnée(s)",
                         key='btn_cancel_selected'):
                st.session_state['journal'] = [
                    e for i, e in enumerate(st.session_state['journal'])
                    if i not in to_cancel
                ]
                appliquer_simulation()
                st.rerun()

    # ── Impact de la simulation ──
    st.divider()
    st.markdown("### 📈 Impact de la simulation vs données de base")

    eq_base_cours = build_equilibre(sal_base, act_base, mode_maud)
    eq_sim_cours  = build_equilibre(sal_sim, act_sim, mode_maud)

    has_proj = 'Projection participants' in act_sim.columns
    if has_proj:
        eq_base_proj = build_equilibre_projete(sal_base, act_base, mode_maud)
        eq_sim_proj  = build_equilibre_projete(sal_sim, act_sim, mode_maud)

    cout_base     = eq_base_cours['cout_total'].sum()
    cout_sim      = eq_sim_cours['cout_total'].sum()
    solde_base_max = eq_base_cours['solde'].sum()
    solde_sim_max  = eq_sim_cours['solde'].sum()

    def kpi_delta(col, label, base, sim, unit="€"):
        delta = sim - base
        sign = "+" if delta >= 0 else ""
        is_solde = "Solde" in label
        if is_solde:
            color = "ok" if sim >= 0 else "danger"
            span_class = "delta-positive" if delta >= 0 else "delta-negative"
        else:
            color = "ok" if delta <= 0 else "warning"
            span_class = "delta-positive" if delta <= 0 else "delta-negative"
        col.markdown(f"""
        <div class="kpi-card {color}">
          <div class="kpi-label">{label}</div>
          <div class="kpi-value">{sim:,.0f} {unit}</div>
          <div class="kpi-sub">Base : {base:,.0f} {unit} &nbsp;|&nbsp;
            <span class="{span_class}">{sign}{delta:,.0f} {unit}</span>
          </div>
        </div>""", unsafe_allow_html=True)

    st.markdown("#### Effectif maximisé")
    ci1, ci2, ci3 = st.columns(3)
    kpi_delta(ci1, "Coût salarial total", cout_base, cout_sim)
    kpi_delta(ci2, "Recettes max", eq_base_cours['recette_max'].sum(), eq_sim_cours['recette_max'].sum())
    kpi_delta(ci3, "Solde maximisé", solde_base_max, solde_sim_max)

    if has_proj:
        st.markdown("#### Effectif projeté")
        cp1, cp2, cp3 = st.columns(3)
        kpi_delta(cp1, "Coût salarial total", cout_base, cout_sim)
        kpi_delta(cp2, "Recettes projetées",
                  eq_base_proj['recette_projetee'].sum(),
                  eq_sim_proj['recette_projetee'].sum())
        kpi_delta(cp3, "Solde projeté",
                  eq_base_proj['solde'].sum(),
                  eq_sim_proj['solde'].sum())

    # Graphique comparatif
    merged_max = eq_base_cours[['Cours','solde']].merge(
        eq_sim_cours[['Cours','solde']], on='Cours', suffixes=('_base','_sim')
    )
    if has_proj:
        merged_proj = eq_base_proj[['Cours','solde']].merge(
            eq_sim_proj[['Cours','solde']], on='Cours', suffixes=('_base','_sim')
        )
        fig_comp = go.Figure()
        fig_comp.add_trace(go.Bar(name='Base — maximisé', x=merged_max['Cours'],
            y=merged_max['solde_base'], marker_color='#adb5bd', opacity=0.6))
        fig_comp.add_trace(go.Bar(name='Simulation — maximisé', x=merged_max['Cours'],
            y=merged_max['solde_sim'], marker_color='#52b788', opacity=0.85))
        fig_comp.add_trace(go.Bar(name='Base — projeté', x=merged_proj['Cours'],
            y=merged_proj['solde_base'], marker_color='#e9c46a', opacity=0.6))
        fig_comp.add_trace(go.Bar(name='Simulation — projeté', x=merged_proj['Cours'],
            y=merged_proj['solde_sim'],
            marker_color=['#2d6a4f' if v >= 0 else '#c1121f' for v in merged_proj['solde_sim']]))
    else:
        fig_comp = go.Figure()
        fig_comp.add_trace(go.Bar(name='Base', x=merged_max['Cours'],
            y=merged_max['solde_base'], marker_color='#adb5bd', opacity=0.7))
        fig_comp.add_trace(go.Bar(name='Simulation', x=merged_max['Cours'],
            y=merged_max['solde_sim'],
            marker_color=['#2d6a4f' if v >= 0 else '#c1121f' for v in merged_max['solde_sim']]))

    fig_comp.add_hline(y=0, line_dash="dash", line_color="#555")
    fig_comp.update_layout(
        barmode='group', height=400,
        title="Comparaison Solde par cours : Base vs Simulation",
        margin=dict(t=40, b=40, l=0, r=0),
        plot_bgcolor='white', paper_bgcolor='rgba(0,0,0,0)',
        font=dict(family='DM Sans'),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    st.plotly_chart(fig_comp, use_container_width=True)

    # Tableau modifications salariés
    st.markdown("#### Détail modifications salariés (Base vs Simulation)")
    comp_sal = sal_base[['NOM','Cours','Tarif horaire ou mensuel','nb semaines']].merge(
        sal_sim[['NOM','Cours','Tarif horaire ou mensuel','nb semaines']],
        on=['NOM','Cours'], suffixes=('_base','_sim')
    )
    comp_sal['Δ taux'] = (comp_sal['Tarif horaire ou mensuel_sim']
                          - comp_sal['Tarif horaire ou mensuel_base']).round(2)
    comp_sal_mod = comp_sal[comp_sal['Δ taux'] != 0]
    if comp_sal_mod.empty:
        st.info("Aucune modification salariale dans la simulation actuelle.")
    else:
        st.dataframe(comp_sal_mod[['NOM','Cours',
            'Tarif horaire ou mensuel_base','Tarif horaire ou mensuel_sim','Δ taux',
            'nb semaines_base','nb semaines_sim']].rename(columns={
                'Tarif horaire ou mensuel_base': 'Taux base (€/h)',
                'Tarif horaire ou mensuel_sim':  'Taux sim (€/h)',
                'nb semaines_base': 'Semaines base',
                'nb semaines_sim':  'Semaines sim',
            }), use_container_width=True, hide_index=True)


# ─────────────────────────────────────────────
#  TAB 3 : DONNÉES
# ─────────────────────────────────────────────
with tab_data:
    st.markdown("### Données brutes et coûts calculés")

    st.markdown("#### Salariés (simulation en cours)")
    sal_display = sal_sim.copy()
    sal_display['Coût annuel estimé (€)'] = (
        sal_display['Tarif horaire ou mensuel'] * sal_display['Volume annuel'] * COEFF_CHARGES
    ).round(0)
    st.dataframe(sal_display, use_container_width=True, hide_index=True)

    st.markdown("#### Activités (simulation en cours)")
    act_display = act_sim.copy()
    act_display['Recette max (€)'] = (act_display['Tarif'] * act_display['Max participants']).round(0)
    st.dataframe(act_display, use_container_width=True, hide_index=True)

    st.markdown("#### Référence coûts horaires (CoutHoraire.xlsx)")
    st.dataframe(st.session_state['recap_df'], use_container_width=True, hide_index=True)


# ─────────────────────────────────────────────
#  FOOTER
# ─────────────────────────────────────────────
st.markdown("---")
st.caption(
    "Association La Mandallaz · SimulSalaires · "
    f"Coefficient charges : {COEFF_CHARGES} · "
    "Développé avec Streamlit & Plotly"
)
