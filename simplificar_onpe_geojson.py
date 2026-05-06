#!/usr/bin/env python3
"""Simplifica el GeoJSON ONPE manteniendo topologia y valida adyacencias.

Requiere mapshaper. Si no esta instalado globalmente, el script lo ejecuta via:
``npx --yes mapshaper``.
Descargar data disrital del INEI 2023 (https://ide.inei.gob.pe/) y
extrarla con gpkg_to_geojson.py en dataviz_data/onpe_peru_distrital.geojson.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
#seleccionar el geojson generado por gpkg_to_geojson.py a partir del GPKG distrital de INEI 2023
IN_GEOJSON = BASE_DIR / "dataviz_data" / "onpe_peru_distrital.geojson"
OUT_DIR = BASE_DIR / "dataviz_data" / "onpe_simplificado"

COORD_PRECISION = 7
ORIG_FEATURE_FIELD = "__orig_feature_index"
ORIG_PART_FIELD = "__orig_part_index"
PRESERVED_MULTIPOLYGON_FIELD = "__preserved_multipolygon"
DEFAULT_PRESERVE_MULTIPOLYGON_UBIGEOS = ["150124"]
DEFAULT_PRESERVE_PART_MIN_AREA_M2 = 10_000.0
AUTHALIC_EARTH_RADIUS_M = 6371007.180918475


def norm_id(value: Any) -> str:
    return str(value or "").strip()


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


def load_geojson(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def write_geojson(path: Path, geojson: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    features = geojson.get("features")
    if not isinstance(features, list):
        path.write_text(json_compact(geojson), encoding="utf-8")
        return

    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("{\n")
        first_key = True
        for key, value in geojson.items():
            if key == "features":
                continue
            if not first_key:
                fh.write(",\n")
            fh.write(f"  {json.dumps(key, ensure_ascii=False)}:{json_compact(value)}")
            first_key = False

        if not first_key:
            fh.write(",\n")
        fh.write('  "features":[\n')
        for index, feature in enumerate(features):
            suffix = "," if index < len(features) - 1 else ""
            fh.write(f"    {json_compact(feature)}{suffix}\n")
        fh.write("  ]\n")
        fh.write("}\n")


def ring_area_m2(ring: list[list[float]]) -> float:
    if not ring:
        return 0.0
    points = ring if ring[0] == ring[-1] else ring + [ring[0]]
    total = 0.0
    for point_a, point_b in zip(points, points[1:]):
        lon1 = math.radians(point_a[0])
        lat1 = math.radians(point_a[1])
        lon2 = math.radians(point_b[0])
        lat2 = math.radians(point_b[1])
        dlon = lon2 - lon1
        if dlon > math.pi:
            dlon -= 2 * math.pi
        elif dlon < -math.pi:
            dlon += 2 * math.pi
        total += dlon * (math.sin(lat1) + math.sin(lat2))
    return abs(total * AUTHALIC_EARTH_RADIUS_M * AUTHALIC_EARTH_RADIUS_M / 2.0)


def polygon_area_m2(polygon_coords: list[list[list[float]]]) -> float:
    if not polygon_coords:
        return 0.0
    return ring_area_m2(polygon_coords[0]) - sum(ring_area_m2(ring) for ring in polygon_coords[1:])


def selected_polygon_parts(
    polygons: list[list[list[list[float]]]],
    min_area_m2: float,
) -> list[tuple[int, list[list[list[float]]]]]:
    selected = [
        (part_index, polygon_coords)
        for part_index, polygon_coords in enumerate(polygons)
        if polygon_area_m2(polygon_coords) >= min_area_m2
    ]
    if selected:
        return selected
    if not polygons:
        return []
    return [max(enumerate(polygons), key=lambda item: polygon_area_m2(item[1]))]


def split_selected_multipolygons(
    geojson: dict[str, Any],
    ubigeos: set[str],
    min_area_m2: float,
) -> dict[str, Any]:
    split_geojson = dict(geojson)
    split_features = []

    for feature_index, feature in enumerate(geojson.get("features", [])):
        props = dict(feature.get("properties", {}))
        geometry = feature.get("geometry")
        geom_type = geometry.get("type") if geometry else None
        coords = geometry.get("coordinates", []) if geometry else []
        should_split = geom_type == "MultiPolygon" and norm_id(props.get("UBIGEO")) in ubigeos

        if not should_split:
            split_feature = dict(feature)
            props[ORIG_FEATURE_FIELD] = feature_index
            props[ORIG_PART_FIELD] = 0
            props[PRESERVED_MULTIPOLYGON_FIELD] = False
            split_feature["properties"] = props
            split_features.append(split_feature)
            continue

        for part_index, polygon_coords in selected_polygon_parts(coords, min_area_m2):
            split_feature = dict(feature)
            part_props = dict(props)
            part_props[ORIG_FEATURE_FIELD] = feature_index
            part_props[ORIG_PART_FIELD] = part_index
            part_props[PRESERVED_MULTIPOLYGON_FIELD] = True
            split_feature["properties"] = part_props
            split_feature["geometry"] = {
                "type": "Polygon",
                "coordinates": polygon_coords,
            }
            split_features.append(split_feature)

    split_geojson["features"] = split_features
    return split_geojson


def clean_internal_props(props: dict[str, Any]) -> dict[str, Any]:
    clean_props = dict(props)
    clean_props.pop(ORIG_FEATURE_FIELD, None)
    clean_props.pop(ORIG_PART_FIELD, None)
    clean_props.pop(PRESERVED_MULTIPOLYGON_FIELD, None)
    return clean_props


def geometry_polygons(geometry: dict[str, Any] | None) -> list[list[list[list[float]]]]:
    if not geometry:
        return []
    geom_type = geometry.get("type")
    coords = geometry.get("coordinates", [])
    if geom_type == "Polygon":
        return [coords]
    if geom_type == "MultiPolygon":
        return coords
    return []


def recombine_selected_multipolygons(geojson: dict[str, Any]) -> dict[str, Any]:
    recombined_geojson = dict(geojson)
    grouped: dict[int, dict[str, Any]] = {}

    for fallback_index, feature in enumerate(geojson.get("features", [])):
        props = feature.get("properties", {})
        group_index = props.get(ORIG_FEATURE_FIELD, fallback_index)
        try:
            group_index = int(group_index)
        except (TypeError, ValueError):
            group_index = fallback_index

        group = grouped.setdefault(
            group_index,
            {
                "template": feature,
                "props": clean_internal_props(props),
                "parts": [],
                "preserved_multipolygon": False,
            },
        )
        group["preserved_multipolygon"] = group["preserved_multipolygon"] or bool(
            props.get(PRESERVED_MULTIPOLYGON_FIELD)
        )

        part_index = props.get(ORIG_PART_FIELD, len(group["parts"]))
        try:
            part_index = int(part_index)
        except (TypeError, ValueError):
            part_index = len(group["parts"])

        for polygon_coords in geometry_polygons(feature.get("geometry")):
            group["parts"].append((part_index, polygon_coords))

    features = []
    for _, group in sorted(grouped.items()):
        feature = dict(group["template"])
        feature["properties"] = group["props"]

        parts = [coords for _, coords in sorted(group["parts"], key=lambda item: item[0])]
        if not parts:
            feature["geometry"] = None
        elif group["preserved_multipolygon"]:
            feature["geometry"] = {"type": "MultiPolygon", "coordinates": parts}
        elif len(parts) == 1:
            feature["geometry"] = {"type": "Polygon", "coordinates": parts[0]}
        else:
            feature["geometry"] = {"type": "MultiPolygon", "coordinates": parts}
        features.append(feature)

    recombined_geojson["features"] = features
    return recombined_geojson


def build_adjacencies(geojson: dict[str, Any]) -> set[tuple[str, str]]:
    segment_owners: dict[
        tuple[tuple[float, float], tuple[float, float]], set[str]
    ] = defaultdict(set)

    for feature in geojson.get("features", []):
        props = feature.get("properties", {})
        feature_id = norm_id(props.get("IDDIST_RENIEC") or props.get("IDDIST") or props.get("ONPE_ID_RENIEC"))
        if not feature_id:
            continue

        for ring in polygon_rings(feature.get("geometry")):
            if len(ring) < 2:
                continue
            points = [coord_key(pt) for pt in ring]
            for a, b in zip(points, points[1:]):
                if a != b:
                    segment_owners[segment_key(a, b)].add(feature_id)

    adjacency: set[tuple[str, str]] = set()
    for owners in segment_owners.values():
        if len(owners) < 2:
            continue
        sorted_owners = sorted(owners)
        for i, source in enumerate(sorted_owners):
            for target in sorted_owners[i + 1 :]:
                adjacency.add((source, target))
    return adjacency


def feature_ids(geojson: dict[str, Any]) -> set[str]:
    ids = set()
    for feature in geojson.get("features", []):
        props = feature.get("properties", {})
        feature_id = norm_id(props.get("IDDIST_RENIEC") or props.get("IDDIST") or props.get("ONPE_ID_RENIEC"))
        if feature_id:
            ids.add(feature_id)
    return ids


def geometry_stats(geojson: dict[str, Any]) -> tuple[int, int, int]:
    features = geojson.get("features", [])
    null_geoms = sum(1 for feature in features if not feature.get("geometry"))
    empty_rings = 0
    for feature in features:
        rings = polygon_rings(feature.get("geometry"))
        if feature.get("geometry") and not rings:
            empty_rings += 1
    return len(features), null_geoms, empty_rings


def run_mapshaper(input_path: Path, output_path: Path, percent: str, precision: str, clean: bool) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    npx = shutil.which("npx.cmd") or shutil.which("npx")
    if not npx:
        raise RuntimeError("No se encontro npx en PATH. Instala Node.js o mapshaper globalmente.")
    command = [
        npx,
        "--yes",
        "mapshaper",
        str(input_path),
        "-snap",
    ]
    if clean:
        command.append("-clean")
    command.extend([
        "-simplify",
        "weighted",
        percent,
        "keep-shapes",
        "-o",
        f"format=geojson",
        f"precision={precision}",
        str(output_path),
    ])
    subprocess.run(command, check=True)


def simplify_geojson(
    input_path: Path,
    output_path: Path,
    percent: str,
    precision: str,
    clean: bool,
    preserve_multipolygon_ubigeos: set[str],
    preserve_part_min_area_m2: float,
) -> None:
    if not preserve_multipolygon_ubigeos:
        run_mapshaper(input_path, output_path, percent, precision, clean=clean)
        write_geojson(output_path, load_geojson(output_path))
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    safe_level = percent.replace("%", "pct").replace(".", "_")
    tmp_path = output_path.parent / f"_mapshaper_preserve_{safe_level}"
    tmp_path.mkdir(parents=True, exist_ok=True)
    split_input = tmp_path / "input_split.geojson"
    split_output = tmp_path / "output_split.geojson"

    original = load_geojson(input_path)
    write_geojson(
        split_input,
        split_selected_multipolygons(
            original,
            preserve_multipolygon_ubigeos,
            preserve_part_min_area_m2,
        ),
    )
    run_mapshaper(split_input, split_output, percent, precision, clean=clean)
    write_geojson(output_path, recombine_selected_multipolygons(load_geojson(split_output)))

    for path in (split_input, split_output):
        path.unlink(missing_ok=True)
    tmp_path.rmdir()


def compare_variant(
    original_ids: set[str],
    original_edges: set[tuple[str, str]],
    path: Path,
) -> dict[str, Any]:
    geojson = load_geojson(path)
    ids = feature_ids(geojson)
    edges = build_adjacencies(geojson)
    features, null_geoms, empty_rings = geometry_stats(geojson)

    return {
        "file": path.name,
        "size_mb": path.stat().st_size / 1024 / 1024,
        "features": features,
        "missing_features": len(original_ids - ids),
        "extra_features": len(ids - original_ids),
        "null_geoms": null_geoms,
        "empty_geoms": empty_rings,
        "edges": len(edges),
        "lost_edges": len(original_edges - edges),
        "new_edges": len(edges - original_edges),
    }


def print_report(rows: list[dict[str, Any]]) -> None:
    headers = [
        "file",
        "size_mb",
        "features",
        "missing_features",
        "extra_features",
        "null_geoms",
        "empty_geoms",
        "edges",
        "lost_edges",
        "new_edges",
    ]
    print("\t".join(headers))
    for row in rows:
        values = []
        for header in headers:
            value = row[header]
            if header == "size_mb":
                value = f"{value:.2f}"
            values.append(str(value))
        print("\t".join(values))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Genera variantes simplificadas del GeoJSON ONPE y valida adyacencias."
    )
    parser.add_argument("--input", type=Path, default=IN_GEOJSON)
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    parser.add_argument(
        "--levels",
        nargs="+",
        default=["20%", "10%", "5%", "2%"],
        help="porcentajes mapshaper para -simplify, por ejemplo 10%%",
    )
    parser.add_argument("--precision", default="0.000001")
    parser.add_argument("--clean", action="store_true", help="aplica mapshaper -clean antes de simplificar")
    parser.add_argument(
        "--preserve-multipolygon-ubigeos",
        nargs="*",
        default=DEFAULT_PRESERVE_MULTIPOLYGON_UBIGEOS,
        help="ubigeos cuyos MultiPolygon se protegen separando sus partes antes de mapshaper",
    )
    parser.add_argument(
        "--preserve-part-min-area-m2",
        type=float,
        default=DEFAULT_PRESERVE_PART_MIN_AREA_M2,
        help="area minima en m2 para conservar partes de los MultiPolygon protegidos",
    )
    parser.add_argument("--skip-existing", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    original = load_geojson(args.input)
    preserve_multipolygon_ubigeos = {norm_id(ubigeo) for ubigeo in args.preserve_multipolygon_ubigeos}
    original_ids = feature_ids(original)
    original_edges = build_adjacencies(original)
    original_stats = geometry_stats(original)

    print(f"Original: {args.input}")
    print(
        f"  features={original_stats[0]:,} null_geoms={original_stats[1]:,} "
        f"empty_geoms={original_stats[2]:,} edges={len(original_edges):,} "
        f"size={args.input.stat().st_size / 1024 / 1024:.2f} MB"
    )

    rows = []
    for level in args.levels:
        safe_level = level.replace("%", "pct").replace(".", "_")
        out_path = args.out_dir / f"onpe_peru_distrital_simplificado_{safe_level}.geojson"
        if out_path.exists() and args.skip_existing:
            print(f"Usando existente: {out_path}")
        else:
            print(f"Simplificando {level} -> {out_path}")
            simplify_geojson(
                args.input,
                out_path,
                level,
                args.precision,
                clean=args.clean,
                preserve_multipolygon_ubigeos=preserve_multipolygon_ubigeos,
                preserve_part_min_area_m2=args.preserve_part_min_area_m2,
            )
        rows.append(compare_variant(original_ids, original_edges, out_path))

    print_report(rows)


if __name__ == "__main__":
    main()
