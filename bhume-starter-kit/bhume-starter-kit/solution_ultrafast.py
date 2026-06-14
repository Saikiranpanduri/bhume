from __future__ import annotations

import sys
import statistics
import numpy as np
import geopandas as gpd
import rasterio

from scipy.ndimage import gaussian_filter, label, sum as ndi_sum
from rasterio.features import rasterize
from shapely.affinity import translate
from shapely.ops import transform as shp_transform
from pyproj import Transformer

from bhume import load, write_predictions, score


def utm_for(geom):
    lon = geom.centroid.x
    return f"EPSG:{32600 + int((lon + 180)//6) + 1}"


def global_shift(village):
    utm = utm_for(village.example_truths.geometry.iloc[0])
    official = village.plots.to_crs(utm)
    truth = village.example_truths.to_crs(utm)
    dxs, dys = [], []
    for pn in truth.index:
        if pn in official.index:
            o = official.loc[pn].geometry.centroid
            t = truth.loc[pn].geometry.centroid
            dxs.append(t.x - o.x)
            dys.append(t.y - o.y)
    return statistics.median(dxs), statistics.median(dys), utm


def geom_to_raster_crs(src, geom):
    tf = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
    return shp_transform(lambda x, y, z=None: tf.transform(x, y), geom)


def score_alignment(poly, boundary_img, transform):
    mask = rasterize([(poly, 1)], out_shape=boundary_img.shape, transform=transform, fill=0, dtype=np.uint8)
    area = mask.sum()
    if area == 0:
        return 0.0
    overlap = (mask * boundary_img).sum()
    return float(overlap / area)


def clean_boundary_image(boundary_img):
    """Remove noise: small connected components < 10 pixels"""
    binary = boundary_img > 127
    labeled, num_features = label(binary)
    sizes = ndi_sum(binary, labeled, range(num_features + 1))
    small_mask = sizes < 10
    return (~small_mask[labeled]).astype(np.uint8)


def optimize_plot(poly_raster, boundary_img, transform):
    """Fast search: ±12px with 1px steps = 625 candidates"""
    best_score = -1.0
    best_poly = poly_raster
    
    for dx in range(-12, 13, 1):
        for dy in range(-12, 13, 1):
            candidate = translate(poly_raster, dx, dy)
            s = score_alignment(candidate, boundary_img, transform)
            if s > best_score:
                best_score = s
                best_poly = candidate
    
    return best_poly, best_score


def calibrate_confidence(edge_score, area_ratio):
    """Continuous confidence ranking"""
    base = edge_score
    if 0.75 <= area_ratio <= 1.25:
        area_factor = 1.0
    elif 0.65 <= area_ratio <= 1.5:
        area_factor = 0.85
    elif 0.5 <= area_ratio <= 2.0:
        area_factor = 0.60
    else:
        area_factor = 0.30
    return min(1.0, max(0.0, base * area_factor))


def run(village_dir):
    village = load(village_dir)
    if village.example_truths is None:
        raise ValueError("example_truths.geojson required")

    dx, dy, utm = global_shift(village)
    plots_u = village.plots.to_crs(utm)
    boundary_ds = rasterio.open(village.boundaries_path)
    predictions = []
    total = len(plots_u)

    for i, (_, row) in enumerate(plots_u.iterrows(), start=1):
        if i % 100 == 0:
            print(f"Processed {i}/{total}")

        try:
            poly = translate(row.geometry, dx, dy)
            poly4326 = gpd.GeoSeries([poly], crs=utm).to_crs("EPSG:4326").iloc[0]
            poly_raster = geom_to_raster_crs(boundary_ds, poly4326)

            minx, miny, maxx, maxy = poly_raster.bounds
            window = rasterio.windows.from_bounds(minx - 20, miny - 20, maxx + 20, maxy + 20, boundary_ds.transform)
            boundary_img = boundary_ds.read(1, window=window)

            if boundary_img.size == 0:
                raise ValueError("empty raster")

            boundary_img = clean_boundary_image(boundary_img)
            boundary_img = gaussian_filter(boundary_img.astype(float), sigma=0.3)
            transform = boundary_ds.window_transform(window)
            best_poly, best_score = optimize_plot(poly_raster, boundary_img, transform)

            area_ratio = 1.0
            try:
                if "recorded_area" in row.index:
                    rec_area = float(row["recorded_area"])
                    if rec_area > 0:
                        area_ratio = poly.area / rec_area
            except:
                pass

            if best_score < 0.20:
                status = "flagged"
                confidence = None
                final_geom = row.geometry
            else:
                status = "corrected"
                confidence = calibrate_confidence(best_score, area_ratio)
                tf_back = Transformer.from_crs(boundary_ds.crs, "EPSG:4326", always_xy=True)
                final_geom = shp_transform(lambda x, y, z=None: tf_back.transform(x, y), best_poly)

            predictions.append({
                "plot_number": row.plot_number,
                "status": status,
                "confidence": confidence,
                "method_note": f"global_shift=({dx:.1f},{dy:.1f});edge_score={best_score:.3f}",
                "geometry": final_geom,
            })

        except Exception as e:
            predictions.append({
                "plot_number": row.plot_number,
                "status": "flagged",
                "confidence": None,
                "method_note": f"failed:{type(e).__name__}",
                "geometry": row.geometry,
            })

    pred_gdf = gpd.GeoDataFrame(predictions, geometry="geometry", crs="EPSG:4326")
    out = village.dir / "predictions.geojson"
    write_predictions(out, pred_gdf)
    print(f"\nSaved: {out}")

    if village.example_truths is not None:
        print(score(pred_gdf, village))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python solution.py <village_folder>")
        sys.exit(1)
    run(sys.argv[1])
