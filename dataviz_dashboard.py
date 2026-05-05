#!/usr/bin/env python3
"""Genera dashboard HTML estático con 4 scatter plots interactivos (2x2),
switch logit/lineal, y panel lateral con tabla pobreza INEI 2018 sincronizada al hover."""

from __future__ import annotations
import json
import gzip
import base64
from pathlib import Path
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# --- rutas ---
BASE_DIR          = Path(__file__).resolve().parent
DATA_DIR          = BASE_DIR / "dataviz_data"
DOCS_DIR          = BASE_DIR / "docs"
DISTRITOS_CSV     = DATA_DIR / "pobreza_jpp_fp_distritos.csv"
POVERTY_XLSX      = DATA_DIR / "anexo_pobreza_2018.xlsx"
TOTALES_CSV       = DATA_DIR / "totales_distritos_20260430_1430.csv"
PARTICIPANTES_CSV = DATA_DIR / "participantes_distritos_20260430_1430.csv"
OUT_HTML          = DOCS_DIR / "dataviz_dashboard.html"

MIN_PROVINCE_POP_2020 = 1_000_000
MIN_VOTES_URBAN = 5_000

PROVINCE_COLORS = ["#d95f02", "#1b9e77", "#7570b3", "#e7298a", "#66a61e", "#e6ab02"]

PARTY_COLORS = {
    "jpp_share": "#1b9e77",
    "fp_share":  "#d95f02",
    "rp_share":  "#7570b3",
}

PARTY_LABELS = {
    "jpp_share": "JPP — Juntos por el Perú",
    "fp_share":  "FP — Fuerza Popular",
    "rp_share":  "RP — Renovación Popular",
}


# ── helpers ───────────────────────────────────────────────────────────────────

def norm_ubigeo_col(s: pd.Series) -> pd.Series:
    """Normaliza códigos ubigeo a texto de 6 dígitos preservando ceros iniciales."""
    return s.astype(str).str.extract(r"(\d+)")[0].str.zfill(6)


def logit_trend(x: np.ndarray, y: np.ndarray, w: np.ndarray, n: int = 300):
    mask = (y > 0) & (y < 1) & np.isfinite(x) & np.isfinite(y)
    if mask.sum() < 2:
        return None, None
    coef = np.polyfit(x[mask], np.log(y[mask] / (1 - y[mask])), deg=1,
                      w=np.sqrt(w[mask].clip(1)))
    xs = np.linspace(x[mask].min(), x[mask].max(), n)
    return xs.astype(np.float32), (100 / (1 + np.exp(-(coef[0] * xs + coef[1])))).astype(np.float32)


def linear_trend(x: np.ndarray, y: np.ndarray, w: np.ndarray, n: int = 300):
    mask = np.isfinite(x) & np.isfinite(y)
    if mask.sum() < 2:
        return None, None
    coef = np.polyfit(x[mask], y[mask] * 100, deg=1, w=np.sqrt(w[mask].clip(1)))
    xs = np.linspace(x[mask].min(), x[mask].max(), n)
    return xs.astype(np.float32), (coef[0] * xs + coef[1]).astype(np.float32)


def binned(df: pd.DataFrame, share_col: str, bins: int = 10,
           x_col: str = "pobreza_mid_2018", w_col: str = "valid_candidates"):
    out = df.dropna(subset=[x_col, share_col]).copy()
    out["bin"] = pd.qcut(out[x_col], bins, duplicates="drop")
    rows = []
    for _, grp in out.groupby("bin", observed=True):
        w = grp[w_col].clip(lower=1)
        rows.append({
            "x": np.float32(np.average(grp[x_col], weights=w)),
            "y": np.float32(np.average(grp[share_col], weights=w) * 100),
        })
    return pd.DataFrame(rows)


def size_array(vals: pd.Series, lo: float = 4, hi: float = 22) -> np.ndarray:
    s = np.sqrt(vals.clip(lower=1).to_numpy())
    return lo + (hi - lo) * (s - s.min()) / (s.max() - s.min() + 1e-9)


# ── datos ─────────────────────────────────────────────────────────────────────

def load_national() -> pd.DataFrame:
    df = pd.read_csv(DISTRITOS_CSV, dtype={"UBIGEO": str, "UBIGEO_INEI": str}).dropna(
        subset=["pobreza_mid_2018", "jpp_share", "fp_share", "rp_share", "valid_candidates"]
    )
    df["UBIGEO_INEI"] = norm_ubigeo_col(df["UBIGEO_INEI"])
    return df


def load_urban() -> pd.DataFrame:
    df_pob = pd.read_excel(POVERTY_XLSX, sheet_name="Anexo1", header=None,
                           skiprows=7, usecols="A:I")
    df_pob.columns = ["UBIGEO_INEI", "DEPTO", "PROV", "DIST", "pob2020",
                      "pobreza_inf", "pobreza_sup", "grupo", "rank"]
    df_pob["UBIGEO_INEI"] = norm_ubigeo_col(df_pob["UBIGEO_INEI"])
    df_pob["pob2020"] = pd.to_numeric(df_pob["pob2020"], errors="coerce")
    df_pob["DEPTO_KEY"] = df_pob["DEPTO"].astype(str).str.strip().str.upper()
    df_pob["PROV_KEY"] = df_pob["PROV"].astype(str).str.strip().str.upper()
    province_pop = (
        df_pob.dropna(subset=["pob2020"])
        .groupby(["DEPTO_KEY", "PROV_KEY"], as_index=False)["pob2020"]
        .sum()
        .rename(columns={"pob2020": "provincia_pob2020"})
    )
    eligible_provinces = province_pop[
        province_pop["provincia_pob2020"] >= MIN_PROVINCE_POP_2020
    ].copy()

    for col in ["pobreza_inf", "pobreza_sup"]:
        df_pob[col] = pd.to_numeric(df_pob[col], errors="coerce")
    df_pob = df_pob.dropna(subset=["pobreza_inf"])
    df_pob["pobreza_mid"] = (df_pob["pobreza_inf"] + df_pob["pobreza_sup"]) / 2

    tot  = pd.read_csv(TOTALES_CSV)
    part = pd.read_csv(PARTICIPANTES_CSV, encoding="latin-1")
    tot["UBIGEO"] = norm_ubigeo_col(tot["ubigeo_distrito"])
    ubigeo_map = pd.read_csv(DISTRITOS_CSV, dtype={"UBIGEO": str, "UBIGEO_INEI": str})[
        ["UBIGEO", "UBIGEO_INEI"]
    ].drop_duplicates()
    tot = tot.merge(ubigeo_map, on="UBIGEO", how="left")

    for codigo, col in [(8, "fp_votes"), (10, "jpp_votes"), (35, "rp_votes")]:
        s = (part[part["codigoAgrupacionPolitica"] == codigo]
             .groupby("ubigeo_distrito")["totalVotosValidos"].sum().rename(col))
        tot = tot.join(s, on="ubigeo_distrito")
        tot[col] = tot[col].fillna(0)

    valid = tot["totalVotosValidos"].where(tot["totalVotosValidos"] > 0)
    for col in ["fp_votes", "jpp_votes", "rp_votes"]:
        tot[col.replace("votes", "share")] = tot[col] / valid

    merged = tot.merge(df_pob[["UBIGEO_INEI", "pobreza_mid"]], on="UBIGEO_INEI", how="inner")
    merged["DEPTO_KEY"] = merged["nombre_departamento"].str.strip().str.upper()
    merged["PROV_KEY"] = merged["nombre_provincia"].str.strip().str.upper()
    merged = merged.merge(eligible_provinces, on=["DEPTO_KEY", "PROV_KEY"], how="inner")
    merged = merged[merged["totalVotosValidos"] >= MIN_VOTES_URBAN].copy()
    merged["ciudad"] = (
        merged["nombre_provincia"].str.title() + " (" +
        (merged["provincia_pob2020"] / 1_000_000).map(lambda x: f"{x:.1f}M") + ")"
    )
    return merged


def build_table_data(xlsx_path: str) -> tuple[list, dict]:
    """Lee el Excel de pobreza y devuelve (table_data, ubigeo→idx) para construcción JS."""
    df = pd.read_excel(xlsx_path, sheet_name="Anexo1", header=None,
                       skiprows=7, usecols="A:I")
    df.columns = ["UBIGEO_INEI", "DEPTO", "PROV", "DIST", "pob2020",
                  "pobreza_inf", "pobreza_sup", "grupo", "rank"]
    df["UBIGEO_INEI"] = norm_ubigeo_col(df["UBIGEO_INEI"])
    for col in ["pobreza_inf", "pobreza_sup"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["UBIGEO_INEI", "pobreza_inf"]).reset_index(drop=True)

    ubigeo_to_idx: dict[str, int] = {}
    table_data: list = []

    for i, row in df.iterrows():
        ubigeo_to_idx[row["UBIGEO_INEI"]] = int(i)
        table_data.append([
            row["UBIGEO_INEI"],
            row["DEPTO"],
            row["PROV"],
            row["DIST"],
            round(float(row["pobreza_inf"]), 1),
            round(float(row["pobreza_sup"]), 1),
        ])

    return table_data, ubigeo_to_idx


# ── construcción de trazos ────────────────────────────────────────────────────

def add_party_panel(fig, df, share_col, color, label, row, col,
                    trend_indices: dict):
    sizes = size_array(df["valid_candidates"])
    short = label.split("—")[0].strip()

    # customdata: [dmeta_index, votos] — strings via DMETA JS lookup
    custom = np.column_stack([df.index.to_numpy(), df["valid_candidates"].to_numpy()])

    fig.add_trace(go.Scatter(
        x=df["pobreza_mid_2018"].to_numpy(dtype=np.float32),
        y=(df[share_col] * 100).to_numpy(dtype=np.float32),
        mode="markers",
        marker=dict(size=sizes.astype(np.float32), color=color, opacity=0.35, line=dict(width=0)),
        customdata=custom, hovertemplate="<extra></extra>",
        name=label, showlegend=True,
    ), row=row, col=col)

    x = df["pobreza_mid_2018"].to_numpy()
    y = df[share_col].to_numpy()
    w = df["valid_candidates"].to_numpy()

    xs_l, ys_l = logit_trend(x, y, w)
    idx_logit = len(fig.data)
    if xs_l is not None:
        fig.add_trace(go.Scatter(
            x=xs_l, y=ys_l, mode="lines",
            line=dict(color=color, width=2.5),
            name=f"{short} tendencia logit",
            showlegend=False, hoverinfo="skip", visible=True,
        ), row=row, col=col)

    xs_n, ys_n = linear_trend(x, y, w)
    idx_linear = len(fig.data)
    if xs_n is not None:
        fig.add_trace(go.Scatter(
            x=xs_n, y=ys_n, mode="lines",
            line=dict(color=color, width=2.5),
            name=f"{short} tendencia lineal",
            showlegend=False, hoverinfo="skip", visible=False,
        ), row=row, col=col)

    trend_indices.setdefault("logit", []).append(idx_logit)
    trend_indices.setdefault("linear", []).append(idx_linear)

    dec = binned(df, share_col)
    fig.add_trace(go.Scatter(
        x=dec["x"], y=dec["y"], mode="lines+markers",
        line=dict(color=color, width=1.8, dash="dot"),
        marker=dict(size=5, color=color),
        name=f"{short} deciles", showlegend=False, hoverinfo="skip",
    ), row=row, col=col)


def add_urban_panel(fig, urb, trend_indices: dict):
    max_v = urb["totalVotosValidos"].clip(lower=1).pipe(np.sqrt).max()

    for i, (ciudad, grp) in enumerate(urb.groupby("ciudad", sort=False)):
        color = PROVINCE_COLORS[i % len(PROVINCE_COLORS)]
        sizes = (4 + 18 * np.sqrt(grp["totalVotosValidos"].clip(lower=1).to_numpy()) / max_v).astype(np.float32)
        # customdata urban: [dist_name, depto, prov, votos, ubigeo]
        custom = grp[["nombre_distrito", "nombre_departamento",
                       "nombre_provincia", "totalVotosValidos", "UBIGEO_INEI"]].values
        fig.add_trace(go.Scatter(
            x=grp["pobreza_mid"].to_numpy(dtype=np.float32),
            y=(grp["fp_share"] * 100).to_numpy(dtype=np.float32),
            mode="markers",
            marker=dict(size=sizes, color=color,
                        opacity=0.7,
                        line=dict(width=0.5, color="white")),
            customdata=custom,
            hovertemplate="<extra></extra>", name=ciudad, showlegend=True,
        ), row=2, col=2)

    x = urb["pobreza_mid"].to_numpy()
    y = urb["fp_share"].to_numpy()
    w = urb["totalVotosValidos"].to_numpy()

    xs, ys = logit_trend(x, y, w)
    idx_logit_conj = len(fig.data)
    if xs is not None:
        fig.add_trace(go.Scatter(
            x=xs, y=ys, mode="lines",
            line=dict(color="#1b9e77", width=2.5),
            name="Tendencia conjunta (logit)",
            showlegend=True, hoverinfo="skip", visible=True,
        ), row=2, col=2)

    xs_n, ys_n = linear_trend(x, y, w)
    idx_linear_conj = len(fig.data)
    if xs_n is not None:
        fig.add_trace(go.Scatter(
            x=xs_n, y=ys_n, mode="lines",
            line=dict(color="#1b9e77", width=2.5),
            name="Tendencia conjunta (lineal)",
            showlegend=True, hoverinfo="skip", visible=False,
        ), row=2, col=2)

    lima = urb[urb["PROV_KEY"] == "LIMA"]
    xl = lima["pobreza_mid"].to_numpy()
    yl = lima["fp_share"].to_numpy()
    wl = lima["totalVotosValidos"].to_numpy()

    xs_l, ys_l = logit_trend(xl, yl, wl)
    idx_logit_lima = len(fig.data)
    if xs_l is not None:
        fig.add_trace(go.Scatter(
            x=xs_l, y=ys_l, mode="lines",
            line=dict(color="#d95f02", width=2.5),
            name="Tendencia Lima (logit)",
            showlegend=True, hoverinfo="skip", visible=True,
        ), row=2, col=2)

    xs_ln, ys_ln = linear_trend(xl, yl, wl)
    idx_linear_lima = len(fig.data)
    if xs_ln is not None:
        fig.add_trace(go.Scatter(
            x=xs_ln, y=ys_ln, mode="lines",
            line=dict(color="#d95f02", width=2.5),
            name="Tendencia Lima (lineal)",
            showlegend=True, hoverinfo="skip", visible=False,
        ), row=2, col=2)

    trend_indices.setdefault("logit",  []).extend([idx_logit_conj,  idx_logit_lima])
    trend_indices.setdefault("linear", []).extend([idx_linear_conj, idx_linear_lima])


# ── dashboard ─────────────────────────────────────────────────────────────────

def build_dashboard(df: pd.DataFrame, urb: pd.DataFrame) -> go.Figure:
    fig = make_subplots(
        rows=2, cols=2,
        subplot_titles=[
            "JPP — Juntos por el Perú",
            "FP — Fuerza Popular",
            "RP — Renovación Popular",
            "FP en provincias > 1M hab.",
        ],
        horizontal_spacing=0.08,
        vertical_spacing=0.14,
    )

    trend_indices: dict[str, list[int]] = {}

    add_party_panel(fig, df, "jpp_share", PARTY_COLORS["jpp_share"],
                    PARTY_LABELS["jpp_share"], 1, 1, trend_indices)
    add_party_panel(fig, df, "fp_share",  PARTY_COLORS["fp_share"],
                    PARTY_LABELS["fp_share"],  1, 2, trend_indices)
    add_party_panel(fig, df, "rp_share",  PARTY_COLORS["rp_share"],
                    PARTY_LABELS["rp_share"],  2, 1, trend_indices)
    add_urban_panel(fig, urb, trend_indices)

    n_traces = len(fig.data)
    logit_idx  = set(trend_indices["logit"])
    linear_idx = set(trend_indices["linear"])

    vis_logit  = [True] * n_traces
    vis_linear = [True] * n_traces
    for i in range(n_traces):
        if i in logit_idx:
            vis_linear[i] = False
        if i in linear_idx:
            vis_logit[i]  = False

    fig.update_layout(
        updatemenus=[dict(
            type="buttons",
            direction="left",
            x=-0.08, xanchor="left",
            y=1.16, yanchor="top",
            pad={"r": 10, "t": 5},
            showactive=True,
            buttons=[
                dict(label="Tendencia logit",
                     method="restyle",
                     args=[{"visible": vis_logit}]),
                dict(label="Tendencia lineal",
                     method="restyle",
                     args=[{"visible": vis_linear}]),
            ],
        )],
        title=dict(
            text=(
                "<b>Pobreza monetaria distrital 2018 vs votos presidenciales 2026</b><br>"
                "<sup>Fuente pobreza: "
                "<a href='https://www.gob.pe/institucion/inei/informes-publicaciones/3204872-mapa-de-pobreza-provincial-y-distrital-2018' target='_blank'>"
                "INEI 2018 (Anexo estadístico)</a> · Fuente votos: "
                "<a href='https://resultadoelectoral.onpe.gob.pe/main/resumen' target='_blank'>"
                "ONPE 2026 primera vuelta</a> · "
                "Tamaño de punto proporcional a votos válidos</sup>"
            ),
            x=0.5, xanchor="center",
        ),
        height=860,
        margin=dict(t=95, r=20, b=95, l=55),
        paper_bgcolor="white", plot_bgcolor="white",
        legend=dict(orientation="h", yanchor="bottom", y=-0.145,
                    xanchor="center", x=0.5, font=dict(size=10)),
        font=dict(family="Arial, sans-serif", size=11),
        hoverlabel=dict(bgcolor="white", font_size=12),
    )

    axis_x = "Pobreza monetaria 2018, punto medio IC95 (%)"
    axis_y = "Share sobre votos válidos (%)"
    for row, col in [(1, 1), (1, 2), (2, 1), (2, 2)]:
        fig.update_xaxes(title_text=axis_x, row=row, col=col, gridcolor="#eeeeee")
        fig.update_yaxes(title_text=axis_y, row=row, col=col, gridcolor="#eeeeee")

    return fig


def build_full_html(fig: go.Figure, table_data: list, ubigeo_to_idx: dict,
                    dmeta: list) -> str:
    """Post-procesa el HTML generado por Plotly para añadir panel lateral."""
    import io
    buf = io.StringIO()
    fig.write_html(
        buf,
        include_plotlyjs="cdn",
        full_html=True,
        div_id="dataviz-dashboard-plot",
        config={"displayModeBar": True, "scrollZoom": True},
    )
    html = buf.getvalue()

    def gzip_json_b64(obj) -> str:
        raw = json.dumps(
            obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        return base64.b64encode(gzip.compress(raw, compresslevel=9, mtime=0)).decode("ascii")

    ubigeo_to_idx_gz = gzip_json_b64(ubigeo_to_idx)
    table_data_gz = gzip_json_b64(table_data)
    dmeta_gz      = gzip_json_b64(dmeta)

    # CSS en <head> (sin dependencia de Plotly)
    extra_head = """  <style>
    body { display: flex !important; height: 100vh; overflow: hidden; margin: 0; }
    body > div:first-child { flex: 1; min-width: 0; overflow: hidden; width: 0; visibility: hidden; }
    #table-wrap {
      width: 480px; flex-shrink: 0; height: 100vh;
      border-left: 2px solid #ccc; background: #fafafa;
      display: flex; flex-direction: column; font-family: Arial, sans-serif;
    }
    #table-title {
      padding: 7px 10px; font-size: 12px; font-weight: bold;
      background: #e4e4e4; border-bottom: 1px solid #ccc; flex-shrink: 0;
    }
    #table-scroll { overflow-y: auto; flex: 1; }
    table { border-collapse: collapse; font-size: 10px; width: 100%; }
    thead th {
      background: #ececec; position: sticky; top: 0; z-index: 9;
      padding: 4px 6px; text-align: left; border-bottom: 1px solid #bbb; white-space: nowrap;
    }
    td {
      padding: 2px 6px; border-bottom: 1px solid #eee;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 90px;
    }
    td.num { text-align: right; }
    tr.highlighted td { background-color: #fff3b0 !important; }
  </style>
"""

    table_panel = """  <div id="chover" style="position:fixed;display:none;z-index:999;background:white;\
border:1px solid #ccc;border-radius:4px;padding:6px 10px;font-family:Arial,sans-serif;\
font-size:12px;pointer-events:none;box-shadow:2px 2px 6px rgba(0,0,0,.15);line-height:1.6"></div>
  <div id="table-wrap">
    <div id="table-title">Anexo Pobreza INEI 2018</div>
    <div id="table-scroll">
      <table>
        <thead><tr><th>Ubigeo</th><th>Depto</th><th>Prov</th><th>Distrito</th><th>Inf %</th><th>Sup %</th></tr></thead>
        <tbody id="tbod"></tbody>
      </table>
    </div>
  </div>
"""

    # Al final del body: conocemos el ancho real → inicializamos Plotly con él
    init_js = f"""  <script>
    (async function() {{
      var wrap = document.querySelector('body > div:first-child');
      var layout = Object.assign({{}}, _pendingPlot[2], {{width: wrap.offsetWidth}});
      _origNewPlot(_pendingPlot[0], _pendingPlot[1], layout, _pendingPlot[3]);
      requestAnimationFrame(function() {{ wrap.style.visibility = 'visible'; }});

      async function gunzipJson(b64) {{
        var bin = atob(b64);
        var bytes = new Uint8Array(bin.length);
        for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);

        if ('DecompressionStream' in window) {{
          var stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'));
          return JSON.parse(await new Response(stream).text());
        }}
        throw new Error('Este navegador no soporta DecompressionStream para gzip.');
      }}

      var DMETA      = await gunzipJson('{dmeta_gz}');
      var TABLE_DATA = await gunzipJson('{table_data_gz}');
      var UBIGEO_TO_IDX = await gunzipJson('{ubigeo_to_idx_gz}');
      var plotDiv    = document.querySelector('.plotly-graph-div');

      // Construir tabla de una sola vez con innerHTML
      document.getElementById('tbod').innerHTML = TABLE_DATA.map(function(r, i) {{
        return '<tr id="pob-' + i + '"><td>' + r[0] + '</td><td>' + r[1] + '</td><td>' +
          r[2] + '</td><td>' + r[3] + '</td><td class="num">' + r[4] + '</td><td class="num">' + r[5] + '</td></tr>';
      }}).join('');
      var chover    = document.getElementById('chover');
      var highlighted = null;

      plotDiv.on('plotly_hover', function(evtData) {{
        var pt = evtData.points[0];
        if (!pt.customdata) return;
        var cd = pt.customdata, dist, depto, prov, votes, ubigeo;

        if (cd.length === 2 && typeof cd[0] === 'number') {{
          var m = DMETA[cd[0]];
          dist = m[0]; depto = m[1]; prov = m[2]; ubigeo = m[3]; votes = cd[1];
        }} else if (cd.length >= 5) {{
          dist = cd[0]; depto = cd[1]; prov = cd[2]; votes = cd[3]; ubigeo = cd[4];
        }} else {{ return; }}

        var short = (pt.data.name || '').split('—')[0].trim() || '';
        chover.innerHTML = '<b>' + dist + '</b><br>' +
          depto + ', ' + prov + '<br>' +
          'Pobreza: ' + pt.x.toFixed(1) + '%<br>' +
          (short ? 'Share ' + short + ': ' + pt.y.toFixed(1) + '%<br>' : '') +
          'Votos: ' + Number(votes).toLocaleString('es-PE');
        var e = evtData.event;
        chover.style.left = (e.clientX + 14) + 'px';
        chover.style.top  = (e.clientY - 50) + 'px';
        chover.style.display = 'block';

        var idx = UBIGEO_TO_IDX[String(ubigeo).padStart(6, '0')];
        if (highlighted) highlighted.classList.remove('highlighted');
        if (idx !== undefined) {{
          var row = document.getElementById('pob-' + idx);
          if (row) {{
            row.classList.add('highlighted');
            row.scrollIntoView({{ behavior: 'instant', block: 'center' }});
            highlighted = row;
          }}
        }}
      }});

      plotDiv.on('plotly_unhover', function() {{
        chover.style.display = 'none';
        if (highlighted) {{ highlighted.classList.remove('highlighted'); highlighted = null; }}
      }});
    }})().catch(function(err) {{
      var tbod = document.getElementById('tbod');
      if (tbod) {{
        tbod.innerHTML = '<tr><td colspan="6" style="white-space:normal;color:#900;padding:8px">' +
          'No se pudo cargar la tabla: ' + err.message + '</td></tr>';
      }}
      console.error(err);
    }});
  </script>
"""

    intercept = """<script>
    var _origNewPlot = Plotly.newPlot.bind(Plotly);
    var _pendingPlot = null;
    Plotly.newPlot = function(div, data, layout, config) {
      _pendingPlot = [div, data, layout, config];
    };
  </script>"""
    import re
    # El CDN lo inyecta write_html con versión y SRI hash; matcheamos por patrón
    html = html.replace("</head>", extra_head + "</head>")
    html = re.sub(
        r'(<script\b[^>]*cdn\.plot\.ly[^>]*></script>)',
        r'\1\n' + intercept,
        html,
    )
    html = html.replace("</body>", table_panel + init_js + "</body>")
    return html


def main() -> None:
    print("Cargando datos nacionales...")
    df = load_national()
    print(f"  {len(df)} distritos")

    print("Cargando datos urbanos (merge por ubigeo)...")
    urb = load_urban()
    print(f"  {len(urb)} distritos en provincias >1M hab. y >= {MIN_VOTES_URBAN:,} votos")

    print("Cargando tabla de pobreza INEI...")
    table_data, ubigeo_to_idx = build_table_data(POVERTY_XLSX)
    print(f"  {len(table_data)} filas en tabla")

    df = df.reset_index(drop=True)
    dmeta = df[["DISTRITO", "DEPARTAMENTO", "PROVINCIA", "UBIGEO_INEI"]].values.tolist()

    print("Construyendo dashboard...")
    fig = build_dashboard(df, urb)
    html = build_full_html(fig, table_data, ubigeo_to_idx, dmeta)

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    Path(OUT_HTML).write_text(html, encoding="utf-8")
    size_kb = Path(OUT_HTML).stat().st_size / 1024
    print(f"Guardado: {OUT_HTML} ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
