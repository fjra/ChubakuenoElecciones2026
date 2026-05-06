#!/usr/bin/env python3
"""Genera un mapa HTML con el grafo de adyacencia distrital del Peru.

Cada nodo es un distrito ubicado en el centroide de ``ubigeo_centroides.csv`` y
cada arista conecta dos distritos que comparten al menos un segmento de frontera
en ``peru_distrital.geojson``.
"""

from __future__ import annotations

import json
import gzip
import base64
from collections import defaultdict
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
from plotly.utils import PlotlyJSONEncoder


BASE_DIR = Path(__file__).resolve().parent
GEOJSON_PATH = BASE_DIR / "dataviz_data" / "onpe_simplificado" / "onpe_peru_distrital_simplificado_10pct.geojson"
CENTROIDS_PATH = BASE_DIR / "ubigeo_centroides.csv"
DOCS_DIR = BASE_DIR / "docs"
DATA_DIR = BASE_DIR / "dataviz_data"
OUT_HTML = DOCS_DIR / "grafo_adyacencia_distritos.html"
OUT_EDGES = DATA_DIR / "adyacencias_distritales.csv"
OUT_NODES = DATA_DIR / "nodos_distritales.csv"
ONPE_PUNO_PROVINCE_GEOJSON = DATA_DIR / "onpe_200100.json"

GEOJSON_ID_FIELD = "UBIGEO"  # "IDDIST" para MINAM/ONPE, "UBIGEO" para INEI 2023
COORD_PRECISION = 7
SHOW_GRAPH_OVERLAY = False
FLIP_WINDING = False
FOUR_COLOR_PALETTE = {
    0: ("Azul", "#2f6fbb"),
    1: ("Naranja", "#d95f02"),
    2: ("Verde", "#1b9e77"),
    3: ("Rosa", "#cc4778"),
}

AUXILIARY_GEOMETRIES = {
    "210103": {
        "path": ONPE_PUNO_PROVINCE_GEOJSON,
        "property": "DISTRITO",
        "value": "AMANTANI",
        "source_label": "ONPE provincias/200100.json",
    },
}


def norm_ubigeo(value: Any) -> str:
    return str(value).strip().split(".")[0].zfill(6)


def coord_key(coord: list[float] | tuple[float, float]) -> tuple[float, float]:
    return (round(float(coord[0]), COORD_PRECISION), round(float(coord[1]), COORD_PRECISION))


def segment_key(a: tuple[float, float], b: tuple[float, float]) -> tuple[tuple[float, float], tuple[float, float]]:
    return (a, b) if a <= b else (b, a)


def polygon_rings(geometry: dict[str, Any] | None) -> list[list[list[float]]]:
    if not geometry:
        return []
    geom_type = geometry.get("type")
    coords = geometry.get("coordinates", [])
    if geom_type == "Polygon":
        return coords
    if geom_type == "MultiPolygon":
        rings: list[list[list[float]]] = []
        for polygon in coords:
            rings.extend(polygon)
        return rings
    return []


def _ring_signed_area(ring: list[list[float]]) -> float:
    area = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[i + 1][0], ring[i + 1][1]
        area += x1 * y2 - x2 * y1
    return area / 2.0


def fix_winding(geometry: dict[str, Any], flip: bool = False) -> dict[str, Any]:
    geom_type = geometry.get("type")
    coords = geometry.get("coordinates", [])

    def fix_ext(ring: list) -> list:
        if flip:
            return ring[::-1]
        return ring[::-1] if _ring_signed_area(ring) > 0 else ring

    def fix_hole(ring: list) -> list:
        if flip:
            return ring[::-1]
        return ring[::-1] if _ring_signed_area(ring) < 0 else ring

    if geom_type == "Polygon":
        return {**geometry, "coordinates": [fix_ext(coords[0])] + [fix_hole(r) for r in coords[1:]]}
    if geom_type == "MultiPolygon":
        return {**geometry, "coordinates": [
            [fix_ext(p[0])] + [fix_hole(r) for r in p[1:]] for p in coords
        ]}
    return geometry


def slim_feature_for_html(feature: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {GEOJSON_ID_FIELD: feature["properties"][GEOJSON_ID_FIELD]},
        "geometry": fix_winding(feature["geometry"], flip=FLIP_WINDING),
    }


def patch_auxiliary_geometries(geojson: dict[str, Any]) -> dict[str, Any]:
    patched = 0
    by_ubigeo = {
        norm_ubigeo(feature["properties"][GEOJSON_ID_FIELD]): feature
        for feature in geojson["features"]
    }

    for ubigeo, source in AUXILIARY_GEOMETRIES.items():
        target = by_ubigeo.get(ubigeo)
        if not target or target.get("geometry"):
            continue
        source_path = source["path"]
        if not source_path.exists():
            print(f"  Aviso: no se encontro geometria auxiliar {source_path}")
            continue

        aux_geojson = json.loads(source_path.read_text(encoding="utf-8-sig"))
        aux_feature = next(
            (
                feature
                for feature in aux_geojson.get("features", [])
                if str(feature.get("properties", {}).get(source["property"], "")).upper()
                == str(source["value"]).upper()
            ),
            None,
        )
        if not aux_feature or not aux_feature.get("geometry"):
            print(f"  Aviso: no se encontro {source['value']} en {source_path}")
            continue

        target["geometry"] = aux_feature["geometry"]
        target["properties"]["GEOM_SOURCE"] = source["source_label"]
        patched += 1

    if patched:
        print(f"  {patched:,} geometria auxiliar aplicada")
    return geojson


def build_adjacencies(geojson: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    features = geojson["features"]
    segment_owners: dict[
        tuple[tuple[float, float], tuple[float, float]], set[str]
    ] = defaultdict(set)
    feature_rows = []

    for feature in features:
        props = feature["properties"]
        ubigeo = norm_ubigeo(props[GEOJSON_ID_FIELD])
        feature_rows.append(
            {
                "ubigeo": ubigeo,
                "departamento_geo": props.get("NOMBDEP") or props.get("DEPARTAMEN", ""),
                "provincia_geo": props.get("NOMBPROV") or props.get("PROVINCIA", ""),
                "distrito_geo": props.get("NOMBDIST") or props.get("DISTRITO", ""),
                "tiene_geometria": bool(feature.get("geometry")),
                "fuente_geometria": props.get("GEOM_SOURCE", "peru_distrital.geojson"),
            }
        )

        for ring in polygon_rings(feature["geometry"]):
            if len(ring) < 2:
                continue
            points = [coord_key(pt) for pt in ring]
            for a, b in zip(points, points[1:]):
                if a != b:
                    segment_owners[segment_key(a, b)].add(ubigeo)

    adjacency: set[tuple[str, str]] = set()
    for owners in segment_owners.values():
        if len(owners) < 2:
            continue
        sorted_owners = sorted(owners)
        for i, source in enumerate(sorted_owners):
            for target in sorted_owners[i + 1 :]:
                adjacency.add((source, target))

    nodes = pd.DataFrame(feature_rows).drop_duplicates("ubigeo")
    edges = pd.DataFrame(sorted(adjacency), columns=["source", "target"])
    return nodes, edges


def load_nodes_with_centroids(nodes: pd.DataFrame, edges: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    out = nodes.copy()
    degree = pd.concat([edges["source"], edges["target"]]).value_counts()
    out["grado"] = out["ubigeo"].map(degree).fillna(0).astype(int)
    out["nombre"] = out["distrito_geo"].str.title()
    out["departamento_label"] = out["departamento_geo"].str.title()
    out["provincia_label"] = out["provincia_geo"].str.title()
    out["pct_pobreza_total"] = float("nan")
    out["latitude"] = float("nan")
    out["longitude"] = float("nan")
    return out, edges


def four_color_nodes(nodes: pd.DataFrame, edges: pd.DataFrame) -> pd.DataFrame:
    """Colorea el grafo con hasta 4 colores y valida el resultado."""
    import networkx as nx

    graph = nx.Graph()
    graph.add_nodes_from(nodes["ubigeo"])
    graph.add_edges_from(edges[["source", "target"]].itertuples(index=False, name=None))
    color_by_ubigeo = nx.coloring.greedy_color(
        graph,
        strategy="largest_first",
        interchange=True,
    )

    color_count = max(color_by_ubigeo.values(), default=-1) + 1
    if color_count > len(FOUR_COLOR_PALETTE):
        raise RuntimeError(f"La coloracion uso {color_count} colores; se esperaban como maximo 4.")

    conflicts = [
        (source, target)
        for source, target in edges[["source", "target"]].itertuples(index=False)
        if color_by_ubigeo[source] == color_by_ubigeo[target]
    ]
    if conflicts:
        sample = ", ".join(f"{a}-{b}" for a, b in conflicts[:5])
        raise RuntimeError(f"Coloracion invalida: {len(conflicts)} conflictos. Ejemplos: {sample}")

    out = nodes.copy()
    out["color4"] = out["ubigeo"].map(color_by_ubigeo).astype(int)
    out["color4_label"] = out["color4"].map(lambda color: FOUR_COLOR_PALETTE[color][0])
    out["color4_hex"] = out["color4"].map(lambda color: FOUR_COLOR_PALETTE[color][1])
    return out


def edge_coordinates(nodes: pd.DataFrame, edges: pd.DataFrame) -> tuple[list[float], list[float]]:
    by_ubigeo = nodes.set_index("ubigeo")[["longitude", "latitude"]].to_dict("index")
    lons: list[float] = []
    lats: list[float] = []
    for source, target in edges[["source", "target"]].itertuples(index=False):
        a = by_ubigeo[source]
        b = by_ubigeo[target]
        lons.extend([a["longitude"], b["longitude"], None])
        lats.extend([a["latitude"], b["latitude"], None])
    return lons, lats


def build_figure(geojson: dict[str, Any], nodes: pd.DataFrame, edges: pd.DataFrame) -> go.Figure:
    valid = set(nodes["ubigeo"])
    mapped_features = [
        slim_feature_for_html(feature)
        for feature in geojson["features"]
        if feature.get("geometry") and norm_ubigeo(feature["properties"][GEOJSON_ID_FIELD]) in valid
    ]
    missing_geometry = int((~nodes["tiene_geometria"]).sum())
    isolated = int((nodes["grado"] == 0).sum())
    island_names = nodes[(nodes["grado"] == 0) & nodes["tiene_geometria"]]["nombre"].tolist()
    island_note = ""
    if island_names:
        island_word = "isla" if len(island_names) == 1 else "islas"
        island_note = f"; {len(island_names):,} {island_word}: {', '.join(island_names)}"

    fig = go.Figure()
    node_info = nodes.set_index("ubigeo", drop=False)
    feature_colors = node_info["color4"].to_dict()
    mapped_geojson = {**geojson, "features": mapped_features}
    mapped_locations = [feature["properties"][GEOJSON_ID_FIELD] for feature in mapped_features]
    district_hover = node_info.loc[
        mapped_locations,
        [
            "ubigeo",
            "departamento_label",
            "provincia_label",
            "grado",
            "pct_pobreza_total",
            "color4_label",
        ],
    ]

    fig.add_trace(
        go.Choropleth(
            geojson=mapped_geojson,
            locations=mapped_locations,
            z=[feature_colors[ubigeo] for ubigeo in mapped_locations],
            customdata=district_hover.values,
            text=node_info.loc[mapped_locations, "nombre"].tolist(),
            featureidkey=f"properties.{GEOJSON_ID_FIELD}",
            colorscale=[
                [0.00, FOUR_COLOR_PALETTE[0][1]],
                [0.25, FOUR_COLOR_PALETTE[0][1]],
                [0.25, FOUR_COLOR_PALETTE[1][1]],
                [0.50, FOUR_COLOR_PALETTE[1][1]],
                [0.50, FOUR_COLOR_PALETTE[2][1]],
                [0.75, FOUR_COLOR_PALETTE[2][1]],
                [0.75, FOUR_COLOR_PALETTE[3][1]],
                [1.00, FOUR_COLOR_PALETTE[3][1]],
            ],
            zmin=-0.5,
            zmax=3.5,
            marker_line_color="#d5d9df",
            marker_line_width=0.35,
            showscale=False,
            hovertemplate=(
                "<b>%{text}</b><br>"
                "Ubigeo: %{customdata[0]}<br>"
                "%{customdata[1]} / %{customdata[2]}<br>"
                "Vecinos: %{customdata[3]}<br>"
                "4-coloracion: %{customdata[5]}"
                "<extra></extra>"
            ),
            name="Distritos 4-coloreados",
            showlegend=False,
        )
    )

    if SHOW_GRAPH_OVERLAY:
        edge_lons, edge_lats = edge_coordinates(nodes, edges)
        fig.add_trace(
            go.Scattergeo(
                lon=edge_lons,
                lat=edge_lats,
                mode="lines",
                line=dict(width=0.45, color="rgba(40, 71, 94, 0.32)"),
                hoverinfo="skip",
                name=f"Fronteras compartidas ({len(edges):,})",
            )
        )

        for color, (label, hex_color) in FOUR_COLOR_PALETTE.items():
            color_nodes = nodes[nodes["color4"] == color]
            fig.add_trace(
                go.Scattergeo(
                    lon=color_nodes["longitude"],
                    lat=color_nodes["latitude"],
                    mode="markers",
                    marker=dict(
                        size=(color_nodes["grado"].clip(lower=1) ** 0.5 * 2.4 + 2).astype(float),
                        color=hex_color,
                        opacity=0.9,
                        line=dict(width=0.45, color="white"),
                    ),
                    customdata=color_nodes[
                        [
                            "ubigeo",
                            "departamento_label",
                            "provincia_label",
                            "grado",
                            "pct_pobreza_total",
                            "color4_label",
                        ]
                    ],
                    hovertemplate=(
                        "<b>%{text}</b><br>"
                        "Ubigeo: %{customdata[0]}<br>"
                        "%{customdata[1]} / %{customdata[2]}<br>"
                        "Vecinos: %{customdata[3]}<br>"
                        "4-coloracion: %{customdata[5]}<br>"
                        "Pobreza total: %{customdata[4]:.1f}%"
                        "<extra></extra>"
                    ),
                    text=color_nodes["nombre"],
                    name=f"{label} ({len(color_nodes):,})",
                    showlegend=True,
                ),
            )
    else:
        for color, (label, hex_color) in FOUR_COLOR_PALETTE.items():
            fig.add_trace(
                go.Scattergeo(
                    lon=[None],
                    lat=[None],
                    mode="markers",
                    marker=dict(size=10, color=hex_color),
                    hoverinfo="skip",
                    name=f"{label} ({int((nodes['color4'] == color).sum()):,})",
                )
            )

    fig.update_geos(
        fitbounds="locations",
        domain=dict(y=[0.06, 1.0]),
        visible=False,
        projection_type="mercator",
        showcountries=False,
        showcoastlines=False,
        showland=False,
        lataxis_showgrid=False,
        lonaxis_showgrid=False,
    )
    fig.update_layout(
        title=dict(
            text=(
                "Grafo de adyacencia distrital del Peru<br>"
                f"<sup>4-coloracion validada · {len(edges):,} aristas por frontera compartida · "
                f"{isolated:,} distritos aislados ({missing_geometry:,} sin geometria{island_note}) · "
                "Chucuito y Acora son discontinuo</sup>"
            ),
            x=0.5,
            xanchor="center",
        ),
        height=920,
        margin=dict(t=75, r=15, b=15, l=15),
        paper_bgcolor="white",
        font=dict(family="Arial, sans-serif", size=12),
        legend=dict(orientation="h", yanchor="bottom", y=0.01, xanchor="left", x=0.01),
    )
    return fig


def gzip_json_b64(obj: Any) -> str:
    raw = json.dumps(
        obj,
        ensure_ascii=False,
        separators=(",", ":"),
        cls=PlotlyJSONEncoder,
    ).encode("utf-8")
    return base64.b64encode(gzip.compress(raw, compresslevel=9, mtime=0)).decode("ascii")


def write_compressed_html(fig: go.Figure, out_html: Path) -> None:
    fig_json = fig.to_plotly_json()
    geojson = fig_json["data"][0].pop("geojson")
    data_json = json.dumps(
        fig_json["data"],
        ensure_ascii=False,
        separators=(",", ":"),
        cls=PlotlyJSONEncoder,
    )
    layout_json = json.dumps(
        fig_json["layout"],
        ensure_ascii=False,
        separators=(",", ":"),
        cls=PlotlyJSONEncoder,
    )
    geojson_gz = gzip_json_b64(geojson)

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <script charset="utf-8" src="https://cdn.plot.ly/plotly-3.5.0.min.js"
    integrity="sha256-fHbNLP+GlIXN+efbQec78UkemUz3NJp7UmfGxC1tNxs="
    crossorigin="anonymous"></script>
  <style>
    html, body {{ margin: 0; height: 100%; }}
    #grafo-adyacencia-distritos {{ height: 920px; width: 100%; }}
  </style>
</head>
<body>
  <div id="grafo-adyacencia-distritos"></div>
  <script>
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

    (async function() {{
      var data = {data_json};
      var layout = {layout_json};
      data[0].geojson = await gunzipJson('{geojson_gz}');
      await Plotly.newPlot(
        'grafo-adyacencia-distritos',
        data,
        layout,
        {{displayModeBar: true, scrollZoom: true, responsive: true}}
      );
    }})().catch(function(err) {{
      document.getElementById('grafo-adyacencia-distritos').innerHTML =
        '<div style="font-family:Arial,sans-serif;padding:16px;color:#900">' +
        'No se pudo cargar el mapa: ' + err.message + '</div>';
      console.error(err);
    }});
  </script>
</body>
</html>
"""
    out_html.write_text(html, encoding="utf-8")


def main() -> None:
    print("Cargando GeoJSON distrital...")
    geojson = json.loads(GEOJSON_PATH.read_text(encoding="utf-8"))
    geojson = patch_auxiliary_geometries(geojson)
    print(f"  {len(geojson['features']):,} poligonos distritales")

    print("Calculando aristas por frontera compartida...")
    geo_nodes, edges = build_adjacencies(geojson)
    print(f"  {len(edges):,} aristas topologicas")

    nodes, edges = load_nodes_with_centroids(geo_nodes, edges)
    print(f"  {len(nodes):,} nodos; {len(edges):,} aristas")

    print("Calculando 4-coloracion...")
    nodes = four_color_nodes(nodes, edges)
    color_counts = nodes["color4_label"].value_counts().sort_index()
    print("  " + ", ".join(f"{label}: {count:,}" for label, count in color_counts.items()))

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    nodes.to_csv(OUT_NODES, index=False, encoding="utf-8")
    edges.to_csv(OUT_EDGES, index=False, encoding="utf-8")

    print("Construyendo mapa...")
    fig = build_figure(geojson, nodes, edges)
    write_compressed_html(fig, OUT_HTML)
    print(f"Guardado: {OUT_HTML} ({OUT_HTML.stat().st_size / 1024:.0f} KB)")
    print(f"Aristas CSV: {OUT_EDGES}")
    print(f"Nodos CSV: {OUT_NODES}")


if __name__ == "__main__":
    main()
