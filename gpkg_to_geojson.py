#!/usr/bin/env python3
"""
Descargar data disrital del INEI 2023 (https://ide.inei.gob.pe/) y
extrarla con gpkg_to_geojson.py en un geojson.
"""
import geopandas as gpd
import fiona

INPUT = "DISTRITO.gpkg"
OUTPUT_DIR = "geojson_output"

import os
os.makedirs(OUTPUT_DIR, exist_ok=True)

layers = fiona.listlayers(INPUT)
print(f"Capas encontradas: {layers}")

for layer in layers:
    gdf = gpd.read_file(INPUT, layer=layer)
    out_path = os.path.join(OUTPUT_DIR, f"{layer}.geojson")
    gdf.to_file(out_path, driver="GeoJSON")
    print(f"  -> {out_path} ({len(gdf)} features)")

print("Listo.")
