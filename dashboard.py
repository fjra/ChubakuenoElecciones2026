"""
dashboard.py
------------
Genera dashboard.html: mapa de imputaciones + grafico de evolucion
en una sola pagina con slider para navegar entre snapshots.

Uso:
    python dashboard.py
    python dashboard.py --top 5
    python dashboard.py --output mi_dashboard.html
"""

import argparse
import base64
import csv
import glob
import gzip
import json
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import datetime

# ---------------------------------------------------------------------------
# Archivos
# ---------------------------------------------------------------------------
GEOJSON_FILE    = "peru_distrital.geojson"
CENTROIDES_FILE = "ubigeo_centroides.csv"
UBIGEOS_FILE    = "ubigeos_completo.csv"
OUTPUT_HTML     = "dashboard.html"

COLORES = {
    "datos propios":            "#a8d5a2",
    "centroide":                "#f4a261",
    "provincia":                "#e63946",
    "departamento":             "#e63946",
    "perfil global extranjero": "#9b5de5",
    "sin referencia":           "#aaaaaa",
}


def step_start(label: str) -> float:
    print(f"{label}...", end="", flush=True)
    return time.perf_counter()


def step_done(t0: float) -> None:
    print(f" ({time.perf_counter() - t0:.3f} s)")

METHOD_OWN = 0
METHOD_CENTROID = 1
METHOD_PROV_DEP = 2
METHOD_FOREIGN = 3
METHOD_UNKNOWN = 4
POL_UNKNOWN = -1

D_M = 0
D_W = 1
D_A = 2
D_T = 3
D_D = 4
D_R = 5
DIST_COL_COUNT = 6
DIST_REQUIRED_COLS = 4
PCT_SCALE = 100

METHOD_META = [
    ("Datos propios", "#a8d5a2", False),
    ("Centroide", "#f4a261", True),
    ("Prov/depto", "#e63946", True),
    ("Extranjero", "#9b5de5", True),
    ("Sin referencia", "#aaaaaa", False),
]
METHOD_IDS = {
    "datos propios": METHOD_OWN,
    "centroide": METHOD_CENTROID,
    "provincia": METHOD_PROV_DEP,
    "departamento": METHOD_PROV_DEP,
    "perfil global extranjero": METHOD_FOREIGN,
    "sin referencia": METHOD_UNKNOWN,
}


# ---------------------------------------------------------------------------
# Helpers de carga
# ---------------------------------------------------------------------------

def scope_a_categoria(scope: str) -> str:
    s = scope.lower()
    if s.startswith("centroide"):           return "centroide"
    if "provincia"   in s:                  return "provincia"
    if "departamento" in s:                 return "departamento"
    if "global" in s or "extranjero" in s:  return "perfil global extranjero"
    return "sin referencia"


def load_inei_to_reniec(path: str) -> dict[str, str]:
    m = {}
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                inei   = str(row["inei"]).strip().zfill(6)
                reniec = str(row["reniec"]).strip().zfill(6)
                m[inei] = reniec
    except FileNotFoundError:
        print(f"  [warn] No se encontro {path}")
    return m


def load_todos_ubigeos(path: str) -> set[str]:
    ubigeos = set()
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if row.get("id_ambito_geografico", "1") == "1":
                    ubigeos.add(row["ubigeo_distrito"].zfill(6))
    except FileNotFoundError:
        pass
    return ubigeos


def load_imputaciones(path: str) -> dict[str, dict]:
    imp = {}
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                imp[row["ubigeo"].zfill(6)] = row
    except FileNotFoundError:
        pass
    return imp


def load_actas(path: str) -> dict[str, str]:
    actas = {}
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                ub = row.get("ubigeo_distrito", "").zfill(6)
                actas[ub] = row.get("actasContabilizadas", "")
    except FileNotFoundError:
        pass
    return actas


def load_votos_por_distrito(path: str) -> dict[str, dict[str, int]]:
    """ubigeo → {dni → votos} para ambito nacional."""
    from collections import defaultdict
    votos: dict = defaultdict(lambda: defaultdict(int))
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if row.get("error"):
                    continue
                if row.get("id_ambito_geografico", "1") != "1":
                    continue
                ub  = row.get("ubigeo_distrito", "").zfill(6)
                dni = row.get("dniCandidato", "").strip()
                try:
                    votos[ub][dni] += int(float(row.get("totalVotosValidos", 0) or 0))
                except (ValueError, TypeError):
                    pass
    except FileNotFoundError:
        pass
    return dict(votos)


def load_participantes_por_distrito(path: str) -> dict[str, dict[str, dict]]:
    """ubigeo -> dni -> metadatos + votos crudos, para ambito nacional."""
    participantes: dict[str, dict[str, dict]] = {}
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if row.get("error"):
                    continue
                if row.get("id_ambito_geografico", "1") != "1":
                    continue
                ub = row.get("ubigeo_distrito", "").zfill(6)
                dni = row.get("dniCandidato", "").strip()
                if not ub or not dni:
                    continue
                try:
                    votos = int(float(row.get("totalVotosValidos", 0) or 0))
                except (ValueError, TypeError):
                    votos = 0
                participantes.setdefault(ub, {})[dni] = {
                    "nombre": row.get("nombreCandidato", dni),
                    "partido": row.get("nombreAgrupacionPolitica", ""),
                    "votos": votos,
                }
    except FileNotFoundError:
        pass
    return participantes


def load_participantes_data(path: str) -> tuple[dict[str, dict[str, dict]], list[dict], dict[str, dict[str, int]]]:
    """Participantes nacionales por distrito + filas minimas + agregados raw por scope."""
    from collections import defaultdict
    participantes: dict[str, dict[str, dict]] = {}
    rows: list[dict] = []
    raw_scope_totals: dict[str, dict[str, int]] = {
        "todos": defaultdict(int),
        "peru": defaultdict(int),
        "extranjero": defaultdict(int),
    }
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if row.get("error"):
                    continue
                dni = row.get("dniCandidato", "").strip()
                if not dni:
                    continue
                try:
                    votos = int(float(row.get("totalVotosValidos", 0) or 0))
                except (ValueError, TypeError):
                    votos = 0
                item = {
                    "id_ambito_geografico": row.get("id_ambito_geografico", "1").strip(),
                    "dniCandidato": dni,
                    "nombreCandidato": row.get("nombreCandidato", dni),
                    "nombreAgrupacionPolitica": row.get("nombreAgrupacionPolitica", ""),
                    "totalVotosValidos": votos,
                }
                rows.append(item)
                raw_scope_totals["todos"][dni] += votos
                if item["id_ambito_geografico"] == "2":
                    raw_scope_totals["extranjero"][dni] += votos
                else:
                    raw_scope_totals["peru"][dni] += votos
                if item["id_ambito_geografico"] != "1":
                    continue
                ub = row.get("ubigeo_distrito", "").zfill(6)
                if not ub:
                    continue
                participantes.setdefault(ub, {})[dni] = {
                    "nombre": item["nombreCandidato"],
                    "partido": item["nombreAgrupacionPolitica"],
                    "votos": votos,
                }
    except FileNotFoundError:
        pass
    return participantes, rows, {scope: dict(vals) for scope, vals in raw_scope_totals.items()}


def load_totales_por_distrito(path: str) -> dict[str, dict]:
    totales: dict[str, dict] = {}
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if row.get("error"):
                    continue
                if row.get("id_ambito_geografico", "1") != "1":
                    continue
                ub = row.get("ubigeo_distrito", "").zfill(6)
                if ub:
                    totales[ub] = row
    except FileNotFoundError:
        pass
    return totales


def load_totales_y_actas_pct(path: str) -> tuple[dict[str, dict], float]:
    """Totales nacionales por distrito y % global de actas en una sola pasada."""
    totales: dict[str, dict] = {}
    total_actas = cont_actas = 0
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if row.get("error"):
                    continue
                try:
                    total_actas += int(float(row.get("totalActas", 0) or 0))
                    cont_actas += int(float(row.get("contabilizadas", 0) or 0))
                except (ValueError, TypeError):
                    pass
                if row.get("id_ambito_geografico", "1") != "1":
                    continue
                ub = row.get("ubigeo_distrito", "").zfill(6)
                if ub:
                    totales[ub] = row
    except FileNotFoundError:
        pass
    actas_pct = round(cont_actas / total_actas * 100, 2) if total_actas else 0.0
    return totales, actas_pct


def votos_desde_participantes(participantes: dict[str, dict[str, dict]]) -> dict[str, dict[str, int]]:
    return {
        ub: {dni: cand["votos"] for dni, cand in cands.items()}
        for ub, cands in participantes.items()
    }


def actas_desde_totales(totales: dict[str, dict]) -> dict[str, str]:
    return {ub: row.get("actasContabilizadas", "") for ub, row in totales.items()}


def load_snapshot_inputs(ts: str) -> dict:
    totales, actas_pct = load_totales_y_actas_pct(f"data/totales_distritos_{ts}.csv")
    participantes, participantes_rows, raw_scope_totals = load_participantes_data(f"data/participantes_distritos_{ts}.csv")
    return {
        "ts": ts,
        "imputaciones": load_imputaciones(f"data/imputaciones_{ts}.csv"),
        "totales": totales,
        "actas_pct": actas_pct,
        "participantes": participantes,
        "participantes_rows": participantes_rows,
        "raw_scope_totals": raw_scope_totals,
        "votos_dist": votos_desde_participantes(participantes),
        "actas_dict": actas_desde_totales(totales),
    }


def load_snapshot_inputs_many(timestamps: list[str], workers: int = 1) -> list[dict]:
    if workers == -1:
        workers = os.cpu_count() or 1
    elif workers < -1:
        workers = 1

    progress_cols = 4
    progress_width = max((len(ts) for ts in timestamps), default=0) + 6
    progress_rows = (len(timestamps) + progress_cols - 1) // progress_cols

    def progress_pos(idx: int) -> tuple[int, int]:
        row = idx % progress_rows
        col = idx // progress_rows
        return row, col

    def progress_cell(idx: int, done: bool) -> str:
        mark = "x" if done else " "
        return f"  [{mark}] {timestamps[idx]}".ljust(progress_width)

    def print_pending() -> None:
        for row in range(progress_rows):
            parts = []
            for col in range(progress_cols):
                idx = col * progress_rows + row
                if idx < len(timestamps):
                    parts.append(progress_cell(idx, False))
            print("".join(parts).rstrip())

    def mark_done(idx: int) -> None:
        row, col = progress_pos(idx)
        lines_up = progress_rows - row
        col_pos = col * progress_width
        sys.stdout.write(
            f"\033[{lines_up}A\r"
            f"\033[{col_pos + 1}G"
            f"{progress_cell(idx, True)}"
            f"\033[{lines_up}B\r"
        )
        sys.stdout.flush()

    if workers <= 1:
        inputs = []
        interactive = sys.stdout.isatty()
        if interactive:
            print_pending()
        for idx, ts in enumerate(timestamps):
            inputs.append(load_snapshot_inputs(ts))
            if interactive:
                mark_done(idx)
            else:
                print(f"  [x] {ts}")
        return inputs

    print(f"  usando {workers} workers")
    interactive = sys.stdout.isatty()
    if interactive:
        print_pending()
    results: list[dict | None] = [None] * len(timestamps)

    try:
        ex_ctx = ProcessPoolExecutor(max_workers=workers)
        executor_name = "procesos"
    except (OSError, PermissionError) as exc:
        print(f"  [warn] No se pudo usar ProcessPoolExecutor ({exc}); usando threads.")
        ex_ctx = ThreadPoolExecutor(max_workers=workers)
        executor_name = "threads"

    if not interactive:
        print(f"  modo: {executor_name}")

    with ex_ctx as ex:
        futures = {
            ex.submit(load_snapshot_inputs, ts): idx
            for idx, ts in enumerate(timestamps)
        }
        for fut in as_completed(futures):
            idx = futures[fut]
            results[idx] = fut.result()
            if interactive:
                mark_done(idx)
            else:
                print(f"  [x] {timestamps[idx]}")
    return [r for r in results if r is not None]


def _flt(value, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except (ValueError, TypeError):
        return default


def pct_to_int(value) -> int | str:
    """Porcentaje a entero con dos decimales; conserva vacios como ''."""
    if value in ("", None):
        return ""
    try:
        return round(float(value) * PCT_SCALE)
    except (ValueError, TypeError):
        return ""


def _project_profile(ubigeo: str, participantes: dict, totales: dict) -> dict[str, float]:
    """Proyecta votos distritales con ratio simple de actas contabilizadas."""
    cands = participantes.get(ubigeo, {})
    if not cands:
        return {}
    actas_pct = _flt(totales.get(ubigeo, {}).get("actasContabilizadas", 0))
    factor = 100.0 / actas_pct if actas_pct > 0 else 1.0
    return {dni: meta["votos"] * factor for dni, meta in cands.items()}


def build_district_chart_data(timestamps: list[str], geojson_iddists: set[str],
                              inei_to_reniec: dict[str, str],
                              top_n: int | None,
                              snapshot_inputs: list[dict] | None = None) -> dict[str, dict]:
    """Payload compacto por distrito para armar plots proyectados y crudos en JS."""
    if not timestamps:
        return {}
    if snapshot_inputs is None:
        snapshot_inputs = [load_snapshot_inputs(ts) for ts in timestamps]

    per_ts: list[dict] = []
    for inputs in snapshot_inputs:
        participantes = inputs["participantes"]
        totales = inputs["totales"]
        imputaciones = inputs["imputaciones"]

        projected_by_dist = {ub: _project_profile(ub, participantes, totales) for ub in participantes}

        snap: dict[str, dict] = {}
        for iddist in geojson_iddists:
            reniec = inei_to_reniec.get(iddist, iddist)
            projected = projected_by_dist.get(reniec, {})

            if reniec in imputaciones:
                donor_ubs = [
                    u.strip().zfill(6)
                    for u in imputaciones[reniec].get("donor_ubigeo", "").split("+")
                    if u.strip() and not u.strip().startswith("PERFIL")
                ]
                if donor_ubs:
                    target_actas = _flt(totales.get(reniec, {}).get("totalActas", 0))
                    blended: dict[str, float] = {}
                    for donor_ub in donor_ubs:
                        donor_profile = projected_by_dist.get(donor_ub, {})
                        donor_actas = _flt(totales.get(donor_ub, {}).get("totalActas", 0))
                        scale = (target_actas / donor_actas) if donor_actas > 0 else 1.0
                        weight = 1.0 / len(donor_ubs)
                        for dni, votos in donor_profile.items():
                            blended[dni] = blended.get(dni, 0.0) + votos * scale * weight
                    if blended:
                        projected = blended

            snap[iddist] = {"projected": projected}
        per_ts.append(snap)

    final_idx = len(timestamps) - 1
    district_data: dict[str, dict] = {}

    for iddist in geojson_iddists:
        final_projected = per_ts[final_idx].get(iddist, {}).get("projected", {})
        if not final_projected:
            continue
        dnis = sorted(final_projected, key=lambda d: final_projected[d], reverse=True)
        if top_n:
            dnis = dnis[:top_n]

        totals = []
        for snap in per_ts:
            projected = snap.get(iddist, {}).get("projected", {})
            totals.append(round(sum(projected.values())))

        rows = [totals]
        for dni in dnis:
            proj_votes = []
            for snap in per_ts:
                item = snap.get(iddist, {})
                projected = item.get("projected", {})
                votos_proy = projected.get(dni, 0.0)
                proj_votes.append(round(votos_proy))

            pol_idx = POL_INDEX.get(dni, POL_UNKNOWN)
            if pol_idx == POL_UNKNOWN:
                continue
            rows.append([pol_idx, proj_votes])
        district_data[iddist] = rows

    return district_data


def ganador_distrito(votos: dict[str, int]) -> tuple[str, str, float]:
    if not votos:
        return "", "Sin datos", 0.0
    total = sum(votos.values())
    if total == 0:
        return "", "Sin datos", 0.0
    dni_win = max(votos, key=lambda d: votos[d])
    pct = votos[dni_win] / total * 100
    return dni_win, NOMBRES_CORTOS.get(dni_win, dni_win[:8]), pct


def load_actas_pct_global(path: str) -> float:
    """Calcula el % global de actas contabilizadas: sum(contabilizadas)/sum(totalActas)*100."""
    total = cont = 0
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                try:
                    total += int(float(row.get("totalActas", 0) or 0))
                    cont  += int(float(row.get("contabilizadas", 0) or 0))
                except (ValueError, TypeError):
                    pass
    except FileNotFoundError:
        pass
    return round(cont / total * 100, 2) if total else 0.0


# ---------------------------------------------------------------------------
# Snapshots disponibles
# ---------------------------------------------------------------------------

def encontrar_timestamps(solo_peru: bool = False, solo_extranjero: bool = False) -> list[str]:
    if solo_peru:
        patron = re.compile(r"imputaciones_peru_(\d{8}_\d{4})\.csv")
        return sorted(
            m.group(1)
            for f in glob.glob("data/imputaciones_peru_*.csv")
            if (m := patron.match(os.path.basename(f)))
        )
    if solo_extranjero:
        patron = re.compile(r"imputaciones_extranjero_(\d{8}_\d{4})\.csv")
        return sorted(
            m.group(1)
            for f in glob.glob("data/imputaciones_extranjero_*.csv")
            if (m := patron.match(os.path.basename(f)))
        )
    patron = re.compile(r"imputaciones_(\d{8}_\d{4})\.csv")
    return sorted(
        m.group(1)
        for f in glob.glob("data/imputaciones_*.csv")
        if (m := patron.match(os.path.basename(f)))
    )


def ts_to_label(ts: str) -> str:
    return datetime.strptime(ts, "%Y%m%d_%H%M").strftime("%d-%b %H:%M")

def ts_to_iso(ts: str) -> str:
    return datetime.strptime(ts, "%Y%m%d_%H%M").strftime("%Y-%m-%dT%H:%M:00")

def ts_to_unix(ts: str) -> int:
    return int(datetime.strptime(ts, "%Y%m%d_%H%M").timestamp())


# ---------------------------------------------------------------------------
# Datos por snapshot
# ---------------------------------------------------------------------------

def build_snapshot(ts: str, inei_to_reniec: dict, todos_ubigeos: set,
                   geojson_iddists: set, inputs: dict | None = None) -> dict:
    """Devuelve snapshots compactos por distrito.
    Por distrito: [m, w, a, t, d, r]
    m=metodo id, w=ganador id, a=actas, t=% ganador, d=donor, r=razon.
    Los campos finales se omiten cuando estan vacios."""
    if inputs is None:
        inputs = load_snapshot_inputs(ts)
    imputaciones = inputs["imputaciones"]
    actas_dict   = inputs["actas_dict"]
    votos_dist   = inputs["votos_dist"]

    data = {}
    for iddist in geojson_iddists:
        reniec = inei_to_reniec.get(iddist, iddist)

        if reniec in imputaciones:
            imp      = imputaciones[reniec]
            cat      = scope_a_categoria(imp.get("scope", ""))
            donor_ub = imp.get("donor_ubigeo", "").split("+")[0].strip().zfill(6)
            votos    = votos_dist.get(donor_ub, {})
            dni_win, _nom_win, pct_win = ganador_distrito(votos)
            row = [
                METHOD_IDS.get(cat, METHOD_IDS["sin referencia"]),
                POL_INDEX.get(dni_win, POL_UNKNOWN),
                pct_to_int(imp.get("actas_pct", "")),
                pct_to_int(pct_win if pct_win else ""),
            ]
            donor = imp.get("donor_nombre", "")
            razon = imp.get("razon", "")
            if donor or razon:
                row.extend([donor, razon])
            data[iddist] = row
        elif reniec in todos_ubigeos:
            votos = votos_dist.get(reniec, {})
            dni_win, _nom_win, pct_win = ganador_distrito(votos)
            data[iddist] = [
                METHOD_IDS["datos propios"],
                POL_INDEX.get(dni_win, POL_UNKNOWN),
                pct_to_int(actas_dict.get(reniec, "")),
                pct_to_int(pct_win if pct_win else ""),
            ]
        else:
            data[iddist] = [METHOD_IDS["sin referencia"], POL_UNKNOWN]

    actas_pct = inputs["actas_pct"]
    return {"ts": ts, "label": ts_to_label(ts), "iso": ts_to_iso(ts), "unix": ts_to_unix(ts),
            "actas_pct": actas_pct, "data": data}


def pack_snapshots_columnar(snapshots: list[dict], geojson_iddists: set[str]) -> dict:
    """Convierte snapshots por timestamp a series por distrito.
    Salida: {"m": metadata_por_timestamp, "d": {iddist: [mRle, wRle, a[], t[], dRle, rRle]}}."""
    metadata = [
        {
            "ts": s["ts"],
            "label": s["label"],
            "iso": s["iso"],
            "unix": s["unix"],
            "actas_pct": s["actas_pct"],
        }
        for s in snapshots
    ]
    data: dict[str, list[list]] = {}
    empty = [METHOD_IDS["sin referencia"], POL_UNKNOWN]

    def rle(values: list) -> list:
        if not values:
            return []
        encoded = []
        prev = values[0]
        run_len = 1
        for value in values[1:]:
            if value == prev:
                run_len += 1
            else:
                encoded.extend([prev, run_len])
                prev = value
                run_len = 1
        encoded.extend([prev, run_len])
        return encoded

    for iddist in geojson_iddists:
        cols = [[] for _ in range(DIST_COL_COUNT)]
        any_donor = False
        any_reason = False
        for snap in snapshots:
            row = snap["data"].get(iddist, empty)
            cols[D_M].append(row[D_M] if len(row) > D_M else METHOD_IDS["sin referencia"])
            cols[D_W].append(row[D_W] if len(row) > D_W else POL_UNKNOWN)
            cols[D_A].append(row[D_A] if len(row) > D_A else "")
            cols[D_T].append(row[D_T] if len(row) > D_T else "")
            donor = row[D_D] if len(row) > D_D else ""
            reason = row[D_R] if len(row) > D_R else ""
            cols[D_D].append(donor)
            cols[D_R].append(reason)
            any_donor = any_donor or bool(donor)
            any_reason = any_reason or bool(reason)

        used_cols = DIST_REQUIRED_COLS
        if any_reason:
            used_cols = D_R + 1
        elif any_donor:
            used_cols = D_D + 1
        cols[D_M] = rle(cols[D_M])
        cols[D_W] = rle(cols[D_W])
        if any_donor:
            cols[D_D] = rle(cols[D_D])
        if any_reason:
            cols[D_R] = rle(cols[D_R])
        data[iddist] = cols[:used_cols]

    return {"m": metadata, "d": data}


# ---------------------------------------------------------------------------
# Colores fijos por candidato (DNI como clave)
# ---------------------------------------------------------------------------
COLORES_CANDIDATOS: dict[str, str] = {}
NOMBRES_CORTOS:    dict[str, str] = {}
PARTIDOS_CANDIDATOS: dict[str, str] = {}
POL_INDEX: dict[str, int] = {}
COLOR_OTROS_POL = "#9ca3af"

_PALETTE = [
    "#f97316", "#dc2626", "#1d4ed8", "#0d9488", "#ca8a04",
    "#7c3aed", "#0ea5e9", "#db2777", "#65a30d", "#92400e",
    "#84cc16", "#06b6d4", "#f59e0b", "#10b981", "#6366f1",
]

# Overrides manuales: DNI → color hex. Tienen prioridad sobre _PALETTE.
# Los DNIs correctos se imprimen al correr el script.
_COLOR_OVERRIDES: dict[str, str] = {
    "10001088": "#FF8000",   # Keiko Fujimori     — naranja
    "16002918": "#1a7a1a",   # Roberto Sanchez    — verde
    "07845838": "#00AEEF",   # Rafael Lopez Aliaga — celeste
    "06506278": "#FFD700",   # Jorge Nieto        — amarillo
    "09177250": "#76C442",   # Ricardo Belmont
}

# Overrides manuales: DNI → nombre a mostrar en mapa y leyenda.
_NAME_OVERRIDES: dict[str, str] = {
    "10001088": "KEIKO FUJIMORI",
    "16002918": "ROBERTO SANCHEZ",
    "07845838": "RAFAEL LOPEZ ALIAGA",
    "07552706": "JORGE NIETO",
    "09177250": "RICARDO BELMONT",
}

def build_candidatos_meta_from_rows(rows: list[dict], top_n: int = 12) -> None:
    """Puebla COLORES_CANDIDATOS y NOMBRES_CORTOS desde filas de participantes."""
    from collections import defaultdict
    totales: dict[str, int] = defaultdict(int)
    nombres: dict[str, str] = {}
    partidos: dict[str, str] = {}
    for row in rows:
        dni = row.get("dniCandidato", "").strip()
        nom = row.get("nombreCandidato", "").strip()
        if not dni:
            continue
        nombres[dni] = nom
        partidos[dni] = row.get("nombreAgrupacionPolitica", "")
        try:
            totales[dni] += int(float(row.get("totalVotosValidos", 0) or 0))
        except (ValueError, TypeError):
            pass

    if not totales:
        return

    todos = sorted(totales, key=lambda d: -totales[d])
    top   = todos[:top_n]
    COLORES_CANDIDATOS.clear()
    NOMBRES_CORTOS.clear()
    PARTIDOS_CANDIDATOS.clear()
    POL_INDEX.clear()
    palette_idx = 0
    for idx, dni in enumerate(todos):
        POL_INDEX[dni] = idx
        if dni in _COLOR_OVERRIDES:
            COLORES_CANDIDATOS[dni] = _COLOR_OVERRIDES[dni]
        else:
            COLORES_CANDIDATOS[dni] = _PALETTE[palette_idx % len(_PALETTE)]
            palette_idx += 1
        if dni in _NAME_OVERRIDES:
            NOMBRES_CORTOS[dni] = _NAME_OVERRIDES[dni]
        else:
            NOMBRES_CORTOS[dni] = nombres.get(dni, dni)
        PARTIDOS_CANDIDATOS[dni] = partidos.get(dni, "")
    print(f"  {len(top)} candidatos con color propio:"
          + "".join(f"\n    {NOMBRES_CORTOS[d]} ({totales[d]:,})  {COLORES_CANDIDATOS[d]}" for d in top))


def build_candidatos_meta(path: str, top_n: int = 12) -> None:
    """Puebla COLORES_CANDIDATOS y NOMBRES_CORTOS desde el CSV de participantes."""
    try:
        _participantes, rows, _raw_scope_totals = load_participantes_data(path)
    except FileNotFoundError:
        return
    build_candidatos_meta_from_rows(rows, top_n=top_n)


# ---------------------------------------------------------------------------
# Datos para el gráfico Plotly
# ---------------------------------------------------------------------------

def build_chart_traces(timestamps: list[str], top_n: int | None,
                       sufijo: str = "") -> list[dict]:
    if not timestamps:
        return []

    # Meta del ultimo snapshot
    last = f"data/proyeccion_final{sufijo}_{timestamps[-1]}.csv"
    if not os.path.exists(last):
        return []

    meta = {}
    with open(last, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            dni = row.get("dniCandidato", "").strip()
            meta[dni] = {
                "nombre":  row.get("nombreCandidato", dni),
                "partido": row.get("nombreAgrupacionPolitica", ""),
                "votos":   int(float(row.get("votos_proyectados", 0) or 0)),
            }

    top_dnis = sorted(meta, key=lambda d: meta[d]["votos"], reverse=True)
    if top_n:
        top_dnis = top_dnis[:top_n]

    # Series temporales: porcentaje y votos absolutos
    xs = [ts_to_iso(ts) for ts in timestamps]
    series_pct:   dict[str, list] = {dni: [] for dni in top_dnis}
    series_votos: dict[str, list] = {dni: [] for dni in top_dnis}

    for ts in timestamps:
        path = f"data/proyeccion_final{sufijo}_{ts}.csv"
        snap_pct   = {}
        snap_votos = {}
        if os.path.exists(path):
            with open(path, newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    dni = row.get("dniCandidato", "").strip()
                    snap_pct[dni]   = float(row.get("porcentaje_proyectado", 0) or 0)
                    snap_votos[dni] = int(float(row.get("votos_proyectados", 0) or 0))
        for dni in top_dnis:
            series_pct[dni].append(snap_pct.get(dni, None))
            series_votos[dni].append(snap_votos.get(dni, None))

    # Ordenar por porcentaje final descendente
    top_dnis_sorted = sorted(
        top_dnis,
        key=lambda d: (series_pct[d][-1] or 0),
        reverse=True,
    )

    traces = []
    for dni in top_dnis_sorted:
        m        = meta[dni]
        last_pct   = series_pct[dni][-1]   or 0
        last_votos = series_votos[dni][-1] or 0
        color = COLORES_CANDIDATOS.get(dni)
        trace: dict = {
            "type": "scatter",
            "mode": "lines+markers",
            "name": f"{m['nombre']} ({last_pct:.2f}% · {last_votos:,})",
            "x": xs,
            "y": series_pct[dni],
            "customdata": series_votos[dni],
            "hovertemplate": (
                f"<b>{m['nombre']}</b><br>"
                f"{m['partido']}<br>"
                "%{y:.3f}%  ·  %{customdata:,} votos"
                "<extra></extra>"
            ),
            "line":   {"width": 2},
            "marker": {"size": 5},
        }
        if color:
            trace["line"]["color"]   = color
            trace["marker"]["color"] = color
        traces.append(trace)

    return traces


# ---------------------------------------------------------------------------
# Trazas de votos reales (conteo sin proyectar)
# ---------------------------------------------------------------------------

def _scope_match_row(row: dict, scope: str) -> bool:
    if scope == "todos":
        return True
    ambito = row.get("id_ambito_geografico", "").strip()
    return ambito == ("2" if scope == "extranjero" else "1")


def build_all_raw_traces(timestamps: list[str], top_n: int | None,
                         scopes: list[str],
                         snapshot_inputs: list[dict] | None = None) -> dict[str, list[dict]]:
    """Devuelve trazas raw para todos los scopes usando participantes ya cargados."""
    from collections import defaultdict
    if not timestamps:
        return {}
    if snapshot_inputs is None:
        snapshot_inputs = [load_snapshot_inputs(ts) for ts in timestamps]
    if not snapshot_inputs:
        return {}

    totales_last: dict[str, dict[str, int]] = {s: defaultdict(int) for s in scopes}
    nombres:  dict[str, str] = {}
    partidos: dict[str, str] = {}
    for row in snapshot_inputs[-1]["participantes_rows"]:
        dni = row.get("dniCandidato", "").strip()
        if not dni:
            continue
        nombres[dni]  = row.get("nombreCandidato", dni)
        partidos[dni] = row.get("nombreAgrupacionPolitica", "")
        try:
            v = int(float(row.get("totalVotosValidos", 0) or 0))
        except (ValueError, TypeError):
            continue
        for s in scopes:
            if _scope_match_row(row, s):
                totales_last[s][dni] += v

    top_dnis: dict[str, list[str]] = {}
    top_sets: dict[str, set[str]] = {}
    for s in scopes:
        ranked = sorted(totales_last[s], key=lambda d: -totales_last[s][d])
        top_dnis[s] = ranked[:top_n] if top_n else ranked
        top_sets[s] = set(top_dnis[s])
    all_top = set().union(*top_sets.values())

    xs = [ts_to_iso(ts) for ts in timestamps]
    series: dict[str, dict[str, tuple[list, list]]] = {
        s: {d: ([], []) for d in top_dnis[s]} for s in scopes
    }

    for inputs in snapshot_inputs:
        snap: dict[str, dict[str, int]] = {s: {} for s in scopes}
        raw_scope_totals = inputs["raw_scope_totals"]
        for s in scopes:
            source = raw_scope_totals.get(s, {})
            snap[s] = {dni: source.get(dni, 0) for dni in top_dnis[s] if dni in source}
        for s in scopes:
            total = sum(snap[s].values())
            for dni in top_dnis[s]:
                v = snap[s].get(dni)
                pcts, abss = series[s][dni]
                if total > 0 and v is not None:
                    pcts.append(round(v / total * 100, 4))
                    abss.append(v)
                else:
                    pcts.append(None)
                    abss.append(None)

    result: dict[str, list[dict]] = {}
    for s in scopes:
        top_sorted = sorted(top_dnis[s], key=lambda d: (series[s][d][0][-1] or 0), reverse=True)
        traces = []
        for dni in top_sorted:
            pcts, abss = series[s][dni]
            last_pct = pcts[-1] or 0
            last_abs = totales_last[s].get(dni, 0)
            color = COLORES_CANDIDATOS.get(dni)
            trace: dict = {
                "type": "scatter",
                "mode": "lines+markers",
                "name": f"{nombres.get(dni, dni)} ({last_pct:.2f}% · {last_abs:,})",
                "x": xs,
                "y": pcts,
                "customdata": abss,
                "hovertemplate": (
                    f"<b>{nombres.get(dni, dni)}</b><br>"
                    f"{partidos.get(dni, '')}<br>"
                    "%{y:.3f}%  ·  %{customdata:,} votos"
                    "<extra></extra>"
                ),
                "line":   {"width": 2},
                "marker": {"size": 5},
            }
            if color:
                trace["line"]["color"] = color
                trace["marker"]["color"] = color
            traces.append(trace)
        if traces:
            result[s] = traces
    return result


# ---------------------------------------------------------------------------
# Generar HTML
# ---------------------------------------------------------------------------

SCOPE_LABELS = {
    "todos":      "Todos",
    "peru":       "Per\u00fa",
    "extranjero": "PEX",
}


def generate_html(slim_geojson: dict, snapshots: dict,
                  all_traces: dict[str, list[dict]],
                  all_raw_traces: dict[str, list[dict]],
                  district_data: dict[str, dict],
                  pol_order: list[str] | None = None) -> str:
    """all_traces / all_raw_traces: dict con claves "todos", "peru", "extranjero"."""
    def gz_b64(obj) -> str:
        raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return base64.b64encode(gzip.compress(raw, compresslevel=9)).decode("ascii")

    geojson_gz        = gz_b64(slim_geojson)
    snapshots_gz      = gz_b64(snapshots)
    all_traces_gz     = gz_b64(all_traces)
    all_raw_traces_gz = gz_b64(all_raw_traces)
    district_data_gz  = gz_b64(district_data)

    # Orden para la leyenda política: proyección > votos brutos
    ordered_dnis = pol_order if pol_order else list(COLORES_CANDIDATOS.keys())
    pol_order_js = json.dumps(ordered_dnis, ensure_ascii=False)
    method_meta_js = json.dumps(METHOD_META, ensure_ascii=False, separators=(",", ":"))
    pol_meta = [
        [dni, NOMBRES_CORTOS.get(dni, dni), PARTIDOS_CANDIDATOS.get(dni, ""), COLORES_CANDIDATOS.get(dni, COLOR_OTROS_POL)]
        for dni in COLORES_CANDIDATOS.keys()
    ]
    pol_meta_js = json.dumps(pol_meta, ensure_ascii=False, separators=(",", ":"))

    snapshot_meta = snapshots["m"]
    n          = len(snapshot_meta)
    init_idx   = n - 1
    unix_first = snapshot_meta[0]["unix"]
    unix_last  = snapshot_meta[-1]["unix"]
    unix_init  = snapshot_meta[init_idx]["unix"]

    scopes        = list(all_traces.keys())
    default_scope = scopes[0]
    multi_scope   = len(scopes) > 1

    # Tabs HTML (solo si hay más de un scope)
    if multi_scope:
        tabs_html = '<div id="scope-tabs">' + "".join(
            f'<label class="scope-tab{"  scope-active" if s == default_scope else ""}">'
            f'<input type="radio" name="scope" value="{s}"'
            f'{" checked" if s == default_scope else ""}> {SCOPE_LABELS.get(s, s)}</label>'
            for s in scopes
        ) + "</div>"
        tabs_css = """
#scope-tabs { display: flex; gap: 4px; flex-shrink: 0; }
.scope-tab {
  font-size: 12px; padding: 4px 10px; border-radius: 4px; cursor: pointer;
  border: 1px solid #ccc; color: #555; background: #f8f8f8; white-space: nowrap;
}
.scope-tab input { display: none; }
.scope-active { background: #1a1a2e; color: white; border-color: #1a1a2e; }"""
        tabs_js = f"""
const scopeTabs = document.querySelectorAll('.scope-tab');
scopeTabs.forEach(label => {{
  label.querySelector('input').addEventListener('change', e => {{
    scopeTabs.forEach(l => l.classList.remove('scope-active'));
    label.classList.add('scope-active');
    setScope(e.target.value);
  }});
}});"""
    else:
        tabs_html = ""
        tabs_css  = ""
        tabs_js   = ""

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dashboard Electoral Peru 2026</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://cdn.plot.ly/plotly-2.32.0.min.js"></script>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: sans-serif; background: #f0f0f0; display: flex; flex-direction: column; }}

#header {{ background: #1a1a2e; color: white; padding: 10px 20px; flex-shrink: 0; }}
#header h1 {{ font-size: 17px; font-weight: 600; letter-spacing: 0.3px; }}

#controls {{
  background: white;
  padding: 8px 14px;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px 14px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.1);
  flex-shrink: 0;
}}
#controls > label {{ font-size: 12px; color: #666; white-space: nowrap; }}
#slider {{ flex: 1; min-width: 120px; accent-color: #1a1a2e; cursor: pointer; }}
#ts-label {{ font-size: 14px; font-weight: 700; color: #1a1a2e; white-space: nowrap; }}
{tabs_css}

#tab-group {{ display: flex; gap: 4px; flex-wrap: nowrap; align-items: center; flex-shrink: 0; }}
#view-tabs {{ display: flex; gap: 4px; flex-shrink: 0; }}
.view-tab {{
  font-size: 12px; padding: 4px 10px; border-radius: 4px; cursor: pointer;
  border: 1px solid #ccc; color: #555; background: #f8f8f8; white-space: nowrap;
}}
.view-tab input {{ display: none; }}
.view-active {{ background: #1a1a2e; color: white; border-color: #1a1a2e; }}

#main {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  padding: 8px;
  flex: 1;
  min-height: 0;
}}

.panel {{
  background: white;
  border-radius: 6px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.1);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-height: 0;
}}

.panel-title {{
  padding: 7px 14px;
  font-size: 12px;
  font-weight: 600;
  color: #555;
  border-bottom: 1px solid #eee;
  flex-shrink: 0;
}}

#map {{ flex: 1; min-height: 0; }}
#chart {{ flex: 1; min-height: 0; }}
#chart2 {{ flex: 1; min-height: 0; }}

#right-col {{
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 0;
}}
#right-col .panel:first-child {{ flex: 3; min-height: 0; }}
#right-col .panel:last-child  {{ flex: 2; min-height: 0; }}

/* ── Desktop: ocupa toda la pantalla sin scroll ─────────────────────────── */
@media (min-width: 769px) {{
  body {{ height: 100vh; overflow: hidden; }}
  #main {{ height: 0; }}
}}

/* ── Mobile: columna única, scroll vertical ─────────────────────────────── */
@media (max-width: 768px) {{
  #main {{ grid-template-columns: 1fr; }}
  .panel {{ height: 72vw; min-height: 320px; max-height: 500px; }}
  .panel:first-child {{ min-height: 320px; max-height: none; }}
  #right-col {{ display: contents; }}
}}
</style>
</head>
<body>

<div id="header"><h1>Dashboard Electoral &mdash; Peru 2026</h1></div>

<div id="controls">
  <div id="tab-group">
    {tabs_html}
    <div id="view-tabs">
      <label class="view-tab"><input type="radio" name="viewmode" value="imp"> Imputaciones</label>
      <label class="view-tab view-active"><input type="radio" name="viewmode" value="pol" checked> Ganador</label>
    </div>
  </div>
  <input type="range" id="slider" min="{unix_first}" max="{unix_last}" value="{unix_init}" step="60">
  <span id="ts-label"></span>
  <span id="actas-pct" style="font-size:14px;font-weight:700;color:#1a1a2e;"></span>
</div>

<div id="main">
  <div class="panel">
    <div class="panel-title" id="map-panel-title">Ganador por distrito</div>
    <div id="map"></div>
  </div>
  <div id="right-col">
    <div class="panel">
      <div class="panel-title" id="projected-title">Evolucion de la proyeccion electoral</div>
      <div id="chart"></div>
    </div>
    <div class="panel">
      <div class="panel-title" id="raw-title">Conteo actual de votos (sin proyectar)</div>
      <div id="chart2"></div>
    </div>
  </div>
</div>

<script>
async function _gz(b64) {{
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const ds = new DecompressionStream('gzip');
  const writer = ds.writable.getWriter();
  writer.write(bytes); writer.close();
  const chunks = []; const reader = ds.readable.getReader();
  while (true) {{ const {{done, value}} = await reader.read(); if (done) break; chunks.push(value); }}
  const out = new Uint8Array(chunks.reduce((n,c) => n+c.length, 0));
  let off = 0; for (const c of chunks) {{ out.set(c, off); off += c.length; }}
  return JSON.parse(new TextDecoder().decode(out));
}}
(async () => {{
const GEOJSON        = await _gz('{geojson_gz}');
const SNAPSHOTS      = await _gz('{snapshots_gz}');
const ALL_TRACES     = await _gz('{all_traces_gz}');
const ALL_RAW_TRACES = await _gz('{all_raw_traces_gz}');
const DISTRICT_DATA  = await _gz('{district_data_gz}');
let currentScope  = '{default_scope}';
let viewMode      = 'pol';
let hoveredDistrict = null;
let selectedDistrict = null;
let _clickedLayer = false;
const SNAP_META = SNAPSHOTS.m;
const SNAP_DATA = SNAPSHOTS.d;

const METHOD_META = {method_meta_js};
const POL_META = {pol_meta_js};
const POL_BY_DNI = Object.fromEntries(POL_META.map((p, i) => [p[0], i]));
const POL_OTROS  = '{COLOR_OTROS_POL}';
const POL_ORDER  = {pol_order_js};

// SNAP_DATA[iddist] = [mRle, wRle, a[], t[], dRle, rRle]
// RLE: [valor, largo, ...]. a: actas %, t: % ganador.
const D_M = 0, D_W = 1, D_A = 2, D_T = 3, D_D = 4, D_R = 5;
const METHOD_OWN = 0;
const METHOD_CENTROID = 1;
const METHOD_PROV_DEP = 2;
const METHOD_FOREIGN = 3;
const METHOD_UNKNOWN = METHOD_META.length - 1;
const POL_UNKNOWN = -1;
const PCT_SCALE = {PCT_SCALE};

function methodMeta(id) {{
  return METHOD_META[id] || METHOD_META[METHOD_META.length - 1];
}}

function methodColor(id) {{
  return methodMeta(id)[1];
}}

function isImputed(d) {{
  return !!methodMeta(d[D_M])[2];
}}

function polMeta(id) {{
  return POL_META[id] || null;
}}

function polColor(id) {{
  const p = polMeta(id);
  return p ? p[3] : POL_OTROS;
}}

function polName(id) {{
  const p = polMeta(id);
  return p ? p[1] : '—';
}}

function rleValueAt(rle, idx, fallback) {{
  if (!rle) return fallback;
  let offset = 0;
  for (let i = 0; i < rle.length; i += 2) {{
    const value = rle[i];
    const len = rle[i + 1] || 0;
    if (idx < offset + len) return value;
    offset += len;
  }}
  return fallback;
}}

function pctText(value) {{
  return value !== undefined && value !== '' ? (value / PCT_SCALE).toFixed(2) + '%' : '—';
}}

// ── Mapa ──────────────────────────────────────────────────────────────────
const isMobile = window.innerWidth <= 768;
const map = L.map('map', {{ zoomControl: true }}).setView([-9.2, -75.0], isMobile ? 5 : 6);
L.tileLayer('https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
  attribution: '&copy; OpenStreetMap &copy; CARTO',
  subdomains: 'abcd', maxZoom: 19,
}}).addTo(map);

// Leyenda dinamica
const IMP_LEGEND = [
  [METHOD_META[METHOD_OWN][1], METHOD_META[METHOD_OWN][0]],
  [METHOD_META[METHOD_CENTROID][1], 'Imputado — ' + METHOD_META[METHOD_CENTROID][0].toLowerCase()],
  [METHOD_META[METHOD_PROV_DEP][1], 'Imputado — ' + METHOD_META[METHOD_PROV_DEP][0].toLowerCase()],
  [METHOD_META[METHOD_FOREIGN][1], 'Imputado — ' + METHOD_META[METHOD_FOREIGN][0].toLowerCase()],
  [METHOD_META[METHOD_UNKNOWN][1], METHOD_META[METHOD_UNKNOWN][0] + ' / sin poligono'],
];
const POL_LEGEND = POL_ORDER.slice(0, 5).map(dni => {{
  const p = POL_META[POL_BY_DNI[dni]];
  return [p ? p[3] : POL_OTROS, p ? p[1] : dni];
}})
;

function legendHTML(entries, title) {{
  return `<b style="font-size:13px;display:block;margin-bottom:2px">${{title}}</b>`
    + entries.map(([c,l]) =>
        `<span style="display:inline-block;width:12px;height:12px;background:${{c}};border-radius:3px;margin-right:6px;vertical-align:middle"></span>${{l}}<br>`
      ).join('');
}}

const legend = L.control({{ position: 'bottomleft' }});
legend.onAdd = () => {{
  const div = L.DomUtil.create('div');
  const mobile = window.innerWidth <= 768;
  div.id = 'map-legend';
  div.style.cssText = `background:white;padding:${{mobile ? '5px 8px' : '10px 14px'}};border-radius:6px;box-shadow:0 2px 8px rgba(0,0,0,.25);font-size:${{mobile ? '10px' : '12px'}};line-height:${{mobile ? '1.7' : '2'}}`;
  div.innerHTML = legendHTML(POL_LEGEND, 'Ganador por distrito');
  return div;
}};
legend.addTo(map);

function updateLegend() {{
  const div = document.getElementById('map-legend');
  if (!div) return;
  if (viewMode === 'pol') {{
    div.innerHTML = legendHTML(POL_LEGEND, 'Ganador por distrito');
  }} else {{
    div.innerHTML = legendHTML(IMP_LEGEND, 'Metodo de imputacion');
  }}
}}

// ── Patron SVG para distritos imputados ──────────────────────────────────
// Se inyecta en el SVG de Leaflet la primera vez que se agrega una capa.
let hatchInjected = false;
function ensureHatchDefs() {{
  if (hatchInjected) return;
  const svgEl = document.querySelector('#map svg');
  if (!svgEl) return;
  const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
  defs.innerHTML = `
    <pattern id="imp-hatch" patternUnits="userSpaceOnUse" width="7" height="7" patternTransform="rotate(45)">
      <rect width="7" height="7" fill="transparent"/>
      <line x1="0" y1="0" x2="0" y2="7" stroke="rgba(0,0,0,0.30)" stroke-width="8"/>
    </pattern>`;
  svgEl.insertBefore(defs, svgEl.firstChild);
  hatchInjected = true;
}}
map.on('layeradd', ensureHatchDefs);

// Capa GeoJSON
let currentIdx = {init_idx};
const layerByDist = {{}};
const hatchByDist = {{}};
let geoLayer, hatchLayer;

function distData(idx, iddist) {{
  const cols = SNAP_DATA[iddist];
  if (!cols) return [METHOD_UNKNOWN, POL_UNKNOWN];
  return [
    rleValueAt(cols[D_M], idx, METHOD_UNKNOWN),
    rleValueAt(cols[D_W], idx, POL_UNKNOWN),
    cols[D_A] ? cols[D_A][idx] : '',
    cols[D_T] ? cols[D_T][idx] : '',
    rleValueAt(cols[D_D], idx, ''),
    rleValueAt(cols[D_R], idx, ''),
  ];
}}

function makeStyle(idx, iddist) {{
  const d = distData(idx, iddist);
  return {{ fillColor: viewMode === 'pol' ? polColor(d[D_W]) : methodColor(d[D_M]), color: '#555', weight: 0.4, fillOpacity: 0.75 }};
}}

function makeSelectedStyle(idx, iddist) {{
  const style = makeStyle(idx, iddist);
  return {{ ...style, color: '#111', weight: 2.2, fillOpacity: 0.95 }};
}}

function makeHatchStyle(idx, iddist) {{
  const d = distData(idx, iddist);
  const visible = viewMode === 'pol' && isImputed(d);
  return {{ fillColor: 'url(#imp-hatch)', fillOpacity: visible ? 1 : 0, color: 'none', weight: 0 }};
}}

function actasText(d) {{
  return pctText(d[D_A]);
}}

function makeTooltip(idx, feat) {{
  const d = distData(idx, feat.properties.id);
  const p = feat.properties;
  const header = `<b>${{p.dist}}</b><br><span style="color:#777">${{p.prov}} — ${{p.dep}}</span><br>`;
  if (viewMode === 'pol') {{
    return header
      + `<b>Ganador:</b> ${{polName(d[D_W])}}<br>`
      + (d[D_T] !== undefined && d[D_T] !== '' ? `<b>% del total:</b> ${{pctText(d[D_T])}}<br>` : '')
      + `<b>Actas:</b> ${{actasText(d)}}`
      + (isImputed(d) && d[D_D] ? `<br><i style="color:#888">via donor: ${{d[D_D]}}</i>` : '');
  }}
  return header
    + `<b>Metodo:</b> ${{methodMeta(d[D_M])[0]}}<br>`
    + `<b>Actas:</b> ${{actasText(d)}}`
    + (d[D_D] ? `<br><b>Donor:</b> ${{d[D_D]}}` : '')
    + (d[D_R] ? `<br><b>Razon:</b> ${{d[D_R]}}` : '');
}}

function districtTitle(feat) {{
  const p = feat.properties;
  return `Evolucion distrital - ${{p.dist}}, ${{p.prov}}`;
}}

geoLayer = L.geoJSON(GEOJSON, {{
  style: feat => makeStyle(currentIdx, feat.properties.id),
  onEachFeature(feat, layer) {{
    layerByDist[feat.properties.id] = layer;
    layer.bindTooltip(makeTooltip(currentIdx, feat), {{ sticky: true, maxWidth: 260 }});
    layer.on('mouseover', () => {{
      const iddist = feat.properties.id;
      layer.setStyle(selectedDistrict === iddist ? makeSelectedStyle(currentIdx, iddist) : {{ weight: 1.5, color: '#000', fillOpacity: 0.95 }});
      if (!selectedDistrict) showDistrictChart(iddist, districtTitle(feat));
    }});
    layer.on('mouseout',  () => {{
      const iddist = feat.properties.id;
      layer.setStyle(selectedDistrict === iddist ? makeSelectedStyle(currentIdx, iddist) : makeStyle(currentIdx, iddist));
      if (!selectedDistrict) restoreScopeChart();
    }});
    layer.on('click', e => {{
      _clickedLayer = true;
      if (e && e.originalEvent) L.DomEvent.stopPropagation(e.originalEvent);
      toggleDistrictSelection(feat.properties.id, districtTitle(feat));
    }});
  }},
}}).addTo(map);

// Capa overlay de hatch (encima de geoLayer)
hatchLayer = L.geoJSON(GEOJSON, {{
  style: feat => makeHatchStyle(currentIdx, feat.properties.id),
  onEachFeature(feat, layer) {{
    hatchByDist[feat.properties.id] = layer;
    // Redirigir eventos al layerByDist correspondiente para que el tooltip funcione
    layer.on('mouseover', () => {{
      const base = layerByDist[feat.properties.id];
      if (base) base.fire('mouseover');
    }});
    layer.on('mouseout', () => {{
      const base = layerByDist[feat.properties.id];
      if (base) base.fire('mouseout');
    }});
    layer.on('click', e => {{
      _clickedLayer = true;
      if (e && e.originalEvent) L.DomEvent.stopPropagation(e.originalEvent);
      toggleDistrictSelection(feat.properties.id, districtTitle(feat));
    }});
  }},
}}).addTo(map);

function updateMap(idx, prevIdx = null) {{
  const forceAll = prevIdx === null;
  for (const [iddist, layer] of Object.entries(layerByDist)) {{
    const d    = distData(idx, iddist);
    const dOld = forceAll ? null : distData(prevIdx, iddist);
    const styleChanged = forceAll || (viewMode === 'pol' ? d[D_W] !== dOld[D_W] : d[D_M] !== dOld[D_M]);
    if (styleChanged) layer.setStyle(selectedDistrict === iddist ? makeSelectedStyle(idx, iddist) : makeStyle(idx, iddist));
    layer.setTooltipContent(makeTooltip(idx, layer.feature));
  }}
  for (const [iddist, layer] of Object.entries(hatchByDist)) {{
    const d    = distData(idx, iddist);
    const dOld = forceAll ? null : distData(prevIdx, iddist);
    if (forceAll || isImputed(d) !== isImputed(dOld)) layer.setStyle(makeHatchStyle(idx, iddist));
  }}
}}

// ── Plotly ────────────────────────────────────────────────────────────────
function markerShape(label) {{
  return {{
    type: 'line', xref: 'x', yref: 'paper',
    x0: label, x1: label, y0: 0, y1: 1,
    line: {{ color: '#1a1a2e', width: 1.5, dash: 'dot' }},
  }};
}}

function imputationBandShapes(iddist) {{
  if (!iddist) return [];
  const methods = (SNAP_DATA[iddist] && SNAP_DATA[iddist][D_M]) || [];
  if (!methodMeta(rleValueAt(methods, 0, METHOD_UNKNOWN))[2]) return [];
  let firstOwnIdx = -1;
  let offset = 0;
  for (let i = 0; i < methods.length; i += 2) {{
    const method = methods[i];
    const len = methods[i + 1] || 0;
    if (!methodMeta(method)[2]) {{
      firstOwnIdx = offset;
      break;
    }}
    offset += len;
  }}
  const start = SNAP_META[0].iso;
  const end = firstOwnIdx >= 0 ? SNAP_META[firstOwnIdx].iso : SNAP_META[SNAP_META.length - 1].iso;
  const shapes = [
    {{
      type: 'rect', xref: 'x', yref: 'paper',
      x0: start, x1: end, y0: 0, y1: 1,
      fillcolor: 'rgba(230, 57, 70, 0.10)',
      line: {{ width: 0 }},
      layer: 'below',
    }},
  ];
  if (firstOwnIdx >= 0) {{
    shapes.push({{
      type: 'line', xref: 'x', yref: 'paper',
      x0: end, x1: end, y0: 0, y1: 1,
      line: {{ color: '#e63946', width: 1.2, dash: 'dash' }},
    }});
  }}
  return shapes;
}}

function chartShapes() {{
  return [
    ...imputationBandShapes(hoveredDistrict),
    markerShape(SNAP_META[currentIdx].iso),
  ];
}}

function chartLayout() {{
  const legendY = currentScope === 'extranjero' ? 0.75 : 0.90;
  return {{
    xaxis: {{ type: 'date', showgrid: true, gridcolor: '#eee', tickangle: -30, tickfont: {{ size: 11 }}, tickformat: '%d-%b %H:%M' }},
    yaxis: {{ title: '%', showgrid: true, gridcolor: '#eee', ticksuffix: '%', tickfont: {{ size: 11 }}, autorange: true }},
    hovermode: 'x unified',
    legend: {{
      orientation: 'v',
      x: 0.99, xanchor: 'right',
      y: legendY, yanchor: 'top',
      bgcolor: 'rgba(255,255,255,0.85)',
      bordercolor: '#ddd', borderwidth: 1,
      font: {{ size: 11 }},
    }},
    plot_bgcolor: 'white', paper_bgcolor: 'white',
    margin: {{ l: 45, r: 20, t: 8, b: 60 }},
    shapes: chartShapes(),
    autosize: true,
  }};
}}

function chart2Layout() {{
  return {{
    xaxis: {{ type: 'date', showgrid: true, gridcolor: '#eee', tickangle: -30, tickfont: {{ size: 11 }}, tickformat: '%d-%b %H:%M' }},
    yaxis: {{ title: '%', showgrid: true, gridcolor: '#eee', ticksuffix: '%', tickfont: {{ size: 11 }}, autorange: true }},
    hovermode: 'x unified',
    showlegend: false,
    plot_bgcolor: 'white', paper_bgcolor: 'white',
    margin: {{ l: 45, r: 20, t: 8, b: 60 }},
    shapes: chartShapes(),
    autosize: true,
  }};
}}

function setChartTitles(projected, raw) {{
  document.getElementById('projected-title').textContent = projected;
  document.getElementById('raw-title').textContent = raw;
}}

function setRawPanelVisible(visible) {{
  const panel = document.getElementById('chart2').closest('.panel');
  panel.style.display = visible ? 'flex' : 'none';
  setTimeout(() => Plotly.Plots.resize('chart'), 0);
}}

const chartXs = SNAP_META.map(s => s.iso);

function districtProjectedTraces(iddist) {{
  const pack = DISTRICT_DATA[iddist];
  if (!pack || pack.length < 2) return [];
  const totals = pack[0] || [];
  return pack.slice(1).map(row => {{
    const [polId, proy] = row;
    const pct = proy.map((v, i) => totals[i] > 0 ? v / totals[i] * 100 : null);
    const pol = POL_META[polId] || ['', '—', '', POL_OTROS];
    const nombre = pol[1];
    const partido = pol[2];
    const lastPct = [...pct].reverse().find(v => v !== null) || 0;
    const lastProy = [...proy].reverse().find(v => v) || 0;
    const color = pol[3];
    const trace = {{
      type: 'scatter',
      mode: 'lines+markers',
      name: `${{nombre}} (${{lastPct.toFixed(2)}}% · ${{lastProy.toLocaleString()}} proy.)`,
      x: chartXs,
      y: pct,
      customdata: proy,
      hovertemplate: `<b>${{nombre}}</b><br>${{partido}}<br>%{{y:.3f}}% proy. · %{{customdata:,}} votos proy.<extra></extra>`,
      line: {{ width: 2 }},
      marker: {{ size: 5 }},
    }};
    if (color) {{
      trace.line.color = color;
      trace.marker.color = color;
    }}
    return trace;
  }});
}}

function setScope(scope) {{
  currentScope = scope;
  if (selectedDistrict && layerByDist[selectedDistrict]) {{
    layerByDist[selectedDistrict].setStyle(makeStyle(currentIdx, selectedDistrict));
  }}
  selectedDistrict = null;
  hoveredDistrict = null;
  setRawPanelVisible(true);
  setChartTitles('Evolucion de la proyeccion electoral', 'Conteo actual de votos (sin proyectar)');
  Plotly.react('chart', ALL_TRACES[scope], chartLayout());
  Plotly.react('chart2', ALL_RAW_TRACES[scope] || [], chart2Layout());
}}

function showDistrictChart(iddist, title) {{
  const projected = districtProjectedTraces(iddist);
  if (!projected.length) return;
  hoveredDistrict = iddist;
  setRawPanelVisible(false);
  setChartTitles(`${{title}} - proyeccion`, 'Conteo actual de votos (sin proyectar)');
  Plotly.react('chart', projected, chartLayout());
}}

function restoreScopeChart(force = false) {{
  if (selectedDistrict && !force) return;
  if (!hoveredDistrict && !force) return;
  hoveredDistrict = null;
  setRawPanelVisible(true);
  setChartTitles('Evolucion de la proyeccion electoral', 'Conteo actual de votos (sin proyectar)');
  Plotly.react('chart', ALL_TRACES[currentScope], chartLayout());
  Plotly.react('chart2', ALL_RAW_TRACES[currentScope] || [], chart2Layout());
}}

function toggleDistrictSelection(iddist, title) {{
  if (selectedDistrict === iddist) {{
    selectedDistrict = null;
    const layer = layerByDist[iddist];
    if (layer) layer.setStyle(makeStyle(currentIdx, iddist));
    restoreScopeChart(true);
    return;
  }}
  if (selectedDistrict && layerByDist[selectedDistrict]) {{
    layerByDist[selectedDistrict].setStyle(makeStyle(currentIdx, selectedDistrict));
  }}
  selectedDistrict = iddist;
  showDistrictChart(iddist, title);
  const layer = layerByDist[iddist];
  if (layer) layer.setStyle(makeSelectedStyle(currentIdx, iddist));
}}

Plotly.newPlot('chart', ALL_TRACES[currentScope], chartLayout(), {{ responsive: true }});
Plotly.newPlot('chart2', ALL_RAW_TRACES[currentScope] || [], chart2Layout(), {{ responsive: true }});

// ── Tabs de ambito ────────────────────────────────────────────────────────
{tabs_js}

// ── Slider ────────────────────────────────────────────────────────────────
function updateLabel(idx) {{
  document.getElementById('ts-label').textContent = SNAP_META[idx].label;
  const _pct = SNAP_META[idx].actas_pct.toFixed(3);
  const _label = window.innerWidth <= 768 ? 'cont.' : 'contabilizadas';
  document.getElementById('actas-pct').textContent = `(${{_label}} ${{_pct}}%)`;
}}

document.getElementById('slider').addEventListener('input', e => {{
  const unix = +e.target.value;
  const prevIdx = currentIdx;
  currentIdx = SNAP_META.reduce((best, s, i) =>
    Math.abs(s.unix - unix) < Math.abs(SNAP_META[best].unix - unix) ? i : best, 0);
  updateLabel(currentIdx);
  updateMap(currentIdx, prevIdx);
  const shape = {{ shapes: chartShapes() }};
  Plotly.relayout('chart', shape);
  Plotly.relayout('chart2', shape);
}});

// ── Toggle imputaciones / ganador ────────────────────────────────────────
document.querySelectorAll('#view-tabs input').forEach(input => {{
  input.addEventListener('change', e => {{
    document.querySelectorAll('.view-tab').forEach(l => l.classList.remove('view-active'));
    e.target.closest('.view-tab').classList.add('view-active');
    viewMode = e.target.value;
    document.getElementById('map-panel-title').textContent =
      viewMode === 'pol' ? 'Ganador por distrito' : 'Metodo de imputacion por distrito';
    updateLegend();
    updateMap(currentIdx);
  }});
}});

// Init
updateLabel({init_idx});

map.on('click', () => {{
  if (_clickedLayer) {{ _clickedLayer = false; return; }}
  if (!selectedDistrict) return;
  const old = selectedDistrict;
  selectedDistrict = null;
  if (layerByDist[old]) layerByDist[old].setStyle(makeStyle(currentIdx, old));
  restoreScopeChart(true);
}});

// Redibuja Leaflet si el contenedor cambia de tamaño (orientacion, resize)
new ResizeObserver(() => map.invalidateSize()).observe(document.getElementById('map'));

// En mobile: reparte el espacio restante entre mapa (60%) y grafico (40%)
function ajustarAlturasMobile() {{
  if (window.innerWidth > 768) return;
  const main      = document.getElementById('main');
  const top       = main.getBoundingClientRect().top;
  const gap       = 8;   // gap entre paneles
  const padding   = 8;   // padding de #main
  const available = window.innerHeight - top - padding * 2 - gap;
  const mapPanel   = main.querySelector('.panel:first-child');
  const chartPanel = document.getElementById('chart').closest('.panel');
  mapPanel.style.height   = Math.round(available * 0.60) + 'px';
  chartPanel.style.height = Math.round(available * 0.40) + 'px';
}}

ajustarAlturasMobile();
window.addEventListener('resize', ajustarAlturasMobile);
}})();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Genera dashboard electoral interactivo.")
    parser.add_argument("--output", default=None,
                        help="Archivo HTML de salida (default: dashboard.html o dashboard_peru.html)")
    parser.add_argument("--top", type=int, default=5,
                        help="Mostrar solo los N candidatos con mas votos en el grafico (default: 5)")
    parser.add_argument("--solo-peru", action="store_true",
                        help="Usar proyecciones solo-peru (proyeccion_final_peru_*.csv)")
    parser.add_argument("--solo-extranjero", action="store_true",
                        help="Usar proyecciones solo-extranjero (proyeccion_final_extranjero_*.csv)")
    parser.add_argument("--workers", type=int, default=-1,
                        help="Workers para cargar inputs por snapshot en paralelo (default: -1 = todos los procesadores; usar 1 para desactivar)")
    args = parser.parse_args()

    # Determinar modo y output
    if args.solo_peru:
        sufijo = "_peru"
    elif args.solo_extranjero:
        sufijo = "_extranjero"
    else:
        sufijo = None   # combinado

    output = args.output or (f"dashboard{sufijo}.html" if sufijo else "dashboard.html")

    # Timestamps base: siempre desde imputaciones_*.csv (mapa siempre es completo)
    timestamps = encontrar_timestamps()
    if not timestamps:
        print("No se encontraron snapshots con timestamp (imputaciones_*.csv).")
        return
    print(f"{len(timestamps)} snapshots: {', '.join(timestamps)}")

    print("Cargando inputs por snapshot...")
    snapshot_inputs = load_snapshot_inputs_many(timestamps, workers=args.workers)

    # Construir metadatos de candidatos desde el ultimo snapshot
    print("Construyendo metadatos de candidatos...")
    build_candidatos_meta_from_rows(snapshot_inputs[-1]["participantes_rows"])

    # Orden de candidatos segun ultima proyeccion
    proy_order: list[str] = []
    proy_path = f"data/proyeccion_final_{timestamps[-1]}.csv"
    if os.path.exists(proy_path):
        with open(proy_path, newline="", encoding="utf-8-sig") as f:
            rows_proy = sorted(csv.DictReader(f),
                               key=lambda r: float(r.get("votos_proyectados", 0) or 0),
                               reverse=True)
        proy_order = [r["dniCandidato"].strip() for r in rows_proy
                      if r.get("dniCandidato", "").strip() in COLORES_CANDIDATOS]

    # GeoJSON
    print("Cargando GeoJSON...")
    with open(GEOJSON_FILE, encoding="utf-8") as f:
        geojson = json.load(f)

    inei_to_reniec = load_inei_to_reniec(CENTROIDES_FILE)
    todos_ubigeos  = load_todos_ubigeos(UBIGEOS_FILE)

    # Slim GeoJSON: solo geometría + campos mínimos
    geojson_iddists = set()
    slim_features   = []
    for feat in geojson["features"]:
        props  = feat["properties"]
        iddist = str(props.get("IDDIST", "")).zfill(6)
        geojson_iddists.add(iddist)
        slim_features.append({
            "type": "Feature",
            "geometry": feat["geometry"],
            "properties": {
                "id":   iddist,
                "dist": props.get("NOMBDIST", ""),
                "prov": props.get("NOMBPROV", ""),
                "dep":  props.get("NOMBDEP",  ""),
            },
        })
    slim_geojson = {"type": "FeatureCollection", "features": slim_features}
    print(f"  {len(slim_features)} poligonos")

    # Snapshots (el mapa siempre usa imputaciones completas)
    t_step = step_start("Procesando snapshots")
    snapshots = []
    for inputs in snapshot_inputs:
        snapshots.append(build_snapshot(inputs["ts"], inei_to_reniec, todos_ubigeos, geojson_iddists, inputs=inputs))
    snapshots_packed = pack_snapshots_columnar(snapshots, geojson_iddists)
    step_done(t_step)

    # Trazas del grafico
    t_step = step_start("Construyendo trazas del grafico")
    if sufijo is not None:
        # Modo único (--solo-peru o --solo-extranjero)
        all_traces = {"todos": build_chart_traces(timestamps, args.top, sufijo=sufijo)}
        trace_counts = [f"{len(all_traces['todos'])} candidatos ({sufijo})"]
    else:
        # Modo combinado: los tres ámbitos
        all_traces = {}
        trace_counts = []
        for key, suf in [("todos", ""), ("peru", "_peru"), ("extranjero", "_extranjero")]:
            top_n = 8 if key == "extranjero" else args.top
            traces = build_chart_traces(timestamps, top_n, sufijo=suf)
            if traces:
                all_traces[key] = traces
                trace_counts.append(f"{len(traces)} candidatos ({key})")
            else:
                trace_counts.append(f"sin datos '{key}'")
    step_done(t_step)
    for msg in trace_counts:
        print(f"  {msg}")

    if not all_traces:
        print("Sin trazas disponibles, abortando.")
        return

    t_step = step_start("Construyendo trazas de votos reales")
    all_raw_traces = build_all_raw_traces(
        timestamps, args.top, scopes=list(all_traces.keys()), snapshot_inputs=snapshot_inputs
    )
    step_done(t_step)
    for key, traces in all_raw_traces.items():
        print(f"  {len(traces)} candidatos raw ({key})")
    if not all_raw_traces:
        all_raw_traces = {"todos": []}

    t_step = step_start("Construyendo datos distritales para hover")
    district_data = build_district_chart_data(
        timestamps, geojson_iddists, inei_to_reniec, args.top, snapshot_inputs=snapshot_inputs
    )
    step_done(t_step)
    print(f"  {len(district_data)} distritos con serie temporal")

    # HTML
    print(f"Generando {output}...")
    html = generate_html(slim_geojson, snapshots_packed, all_traces, all_raw_traces, district_data, pol_order=proy_order)
    with open(output, "w", encoding="utf-8") as f:
        f.write(html)

    size_mb = os.path.getsize(output) / 1024 / 1024
    print(f"Dashboard guardado -> {output}  ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
