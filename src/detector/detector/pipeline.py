"""1画像（1年）分の車両台数推定（§12）

  Raw取得 → [Pass 1] Tileごとに背景推定 → 舗装種別判定
          → [Pass 2] Tileごとに占有判定し Core 領域のみ全体配列へ書き戻す
          → 校正・密度 → パッチ集計 → 確認用成果物・BigQuery用レコード出力
"""
import json
import math
import time
from datetime import datetime, timezone

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import rasterize
from shapely.geometry import box

from . import area_ratio, artifacts
from .config import WORK_CRS, RunConfig
from .logs import log
from .storage import LocalStorage
from .tiling import make_tiles


def find_raw(storage: LocalStorage, site_id: str, year: int):
    """raw/{site_id}/{year}/{source_image_id}/image.tif と STACメタデータ"""
    hits = storage.glob(f"raw/{site_id}/{year}/*/image.tif")
    if len(hits) != 1:
        raise FileNotFoundError(f"raw/{site_id}/{year}/*/image.tif が {len(hits)} 件")
    image_path = hits[0]
    stac = json.loads(storage.read_text(image_path.rsplit("/", 1)[0] + "/stac_item.json"))
    return image_path, stac


def make_patches(roi: gpd.GeoDataFrame, transform, shape, patch_m):
    """ROIを覆う patch_m 四方のパッチ。patch_id はROI外接矩形左上からの行・列"""
    minx, miny, maxx, maxy = roi.total_bounds
    x0 = math.floor(minx / patch_m) * patch_m
    y1 = math.ceil(maxy / patch_m) * patch_m
    roi_union = roi.union_all()
    inv = ~transform
    rows = []
    for r, top in enumerate(np.arange(y1, miny, -patch_m)):
        for c, left in enumerate(np.arange(x0, maxx, patch_m)):
            cell = box(left, top - patch_m, left + patch_m, top)
            geom = cell.intersection(roi_union)
            if geom.is_empty:
                continue
            (col0, row0), (col1, row1) = inv @ (left, top), inv @ (left + patch_m, top - patch_m)
            rows.append(dict(patch_id=f"R{r:03d}C{c:03d}", cell=cell, geometry=geom,
                             pixel_x_min=int(np.clip(round(col0), 0, shape[1])),
                             pixel_y_min=int(np.clip(round(row0), 0, shape[0])),
                             pixel_x_max=int(np.clip(round(col1), 0, shape[1])),
                             pixel_y_max=int(np.clip(round(row1), 0, shape[0]))))
    patches = gpd.GeoDataFrame(rows, crs=WORK_CRS)
    # 集計はパッチの正方形（重複なし）で行う。ROI外の密度は0なので結果は ROI∩パッチ と同じ
    index = rasterize([(g, i + 1) for i, g in enumerate(patches.cell)], out_shape=shape,
                      transform=transform, dtype="int32")
    return patches.drop(columns="cell"), index


def run(cfg: RunConfig, storage: LocalStorage, year: int, run_execution_id: str) -> dict:
    t0 = time.time()
    site_id, p = cfg.site["site_id"], cfg.params["area_ratio"]
    roi = cfg.roi()
    roi_geoms = list(roi.geometry)
    n_zones = len(roi_geoms)

    image_path, stac = find_raw(storage, site_id, year)
    rep = stac["representative"]
    source_image_ids = stac["source_image_ids"]
    observation_id = cfg.observation_id(year, source_image_ids)
    log("image_processing_start", observation_id=observation_id, source_image_id=rep["id"])

    with rasterio.open(storage.uri(image_path)) as src:
        if str(src.crs) != WORK_CRS:
            raise ValueError(f"CRS {src.crs} は想定外（{WORK_CRS}）")
        shape, transform, crs = (src.height, src.width), src.transform, src.crs
        tiles = make_tiles(*shape, cfg.params["tiling"]["core_px"], cfg.params["tiling"]["overlap_px"])
        log("roi_created", zones=list(roi.zone_id), image_shape=list(shape), tiles=len(tiles))

        def tile_result(tile):
            img = src.read(window=tile.window)
            return area_ratio.prepare_tile(img, src.window_transform(tile.window), roi_geoms, p)

        # Pass 1: 舗装種別判定用に区画ごとの背景輝度を Core 領域で集計
        lum_sums, lum_counts = np.zeros(n_zones + 1), np.zeros(n_zones + 1)
        active = []
        for tile in tiles:
            t = tile_result(tile)
            rs, cs = tile.core_in_tile
            core = area_ratio.TileResult(t.zone_map[rs, cs], t.valid[rs, cs], t.bg[rs, cs], t.dev[rs, cs])
            if not (core.zone_map > 0).any():
                continue  # Core に ROI を含まない Tile は以後処理しない
            active.append(tile)
            s, n = area_ratio.zone_luminance_sums(core, n_zones)
            lum_sums += s
            lum_counts += n
        dark_zones = area_ratio.dark_zones_from_luminance(lum_sums, lum_counts, p["dark_surface_ratio"])

        # Pass 2: 占有判定。Core 領域のみ全体配列へ書き戻す（二重計上防止, §15）
        occ = np.zeros(shape, bool)
        zone_map = np.zeros(shape, np.uint8)
        for tile in active:
            log("tile_processing_start", tile_index=tile.index)
            t = tile_result(tile)
            tile_occ = area_ratio.occupancy(t, dark_zones, p)
            rs, cs = tile.core_in_tile
            occ[tile.core_in_image] = tile_occ[rs, cs]
            zone_map[tile.core_in_image] = t.zone_map[rs, cs]
            log("tile_processing_complete", tile_index=tile.index, occupied_px=int(tile_occ[rs, cs].sum()))

        rgb = np.moveaxis(src.read((1, 2, 3)), 0, -1)  # 確認用成果物の生成にのみ使用

    dark = np.isin(zone_map, dark_zones)
    calib = area_ratio.calibrate(occ, dark, p)
    dens = area_ratio.density(occ, dark, calib)
    total = float(dens.sum())
    zone_counts = {z: round(float(dens[zone_map == i + 1].sum()), 1) for i, z in enumerate(roi.zone_id)}
    log("density_estimation_complete", vehicle_count_raw=round(total, 1), zone_counts=zone_counts)

    patches, patch_index = make_patches(roi, transform, shape, cfg.params["patch_m"])
    per_patch = np.bincount(patch_index.ravel(), weights=dens.ravel(), minlength=len(patches) + 1)[1:]
    patches["estimated_count"] = np.round(per_patch, 3)

    # 確認用成果物（§11.2 processed/）
    prefix = (f"processed/{site_id}/{year}/{cfg.site['roi_version']}/"
              f"{cfg.model_version}/{cfg.config_hash}")
    storage.write_bytes(f"{prefix}/overview_density.png", artifacts.density_heatmap(rgb, dens, zone_map, total))
    for name, data in artifacts.crops(rgb, occ, patches):
        storage.write_bytes(f"{prefix}/{name}", data)
    storage.write_text(f"{prefix}/patches.geojson", artifacts.patches_geojson(patches))
    storage.write_bytes(f"{prefix}/density.tif", artifacts.density_geotiff(dens, transform, crs))
    storage.write_text(f"{prefix}/run_config.json", artifacts.run_config_json(cfg.run_config, cfg.config_hash))
    diagnostics = dict(zone_counts=zone_counts,
                       surface={z: ("dark" if i + 1 in dark_zones else "light") for i, z in enumerate(roi.zone_id)},
                       area_per_vehicle_px={k: v[0] for k, v in calib.items()},
                       single_blob_n={k: v[1] for k, v in calib.items()},
                       occupied_ratio=round(float(occ.sum() / max((zone_map > 0).sum(), 1)), 4),
                       tiles_total=len(tiles), tiles_processed=len(active))
    storage.write_text(f"{prefix}/diagnostics.json", json.dumps(diagnostics, indent=1, ensure_ascii=False))
    log("processed_artifacts_uploaded", processed_uri_prefix=storage.uri(prefix))

    # BigQuery 用レコード（§16.2, §16.3）。Phase 4 で Staging テーブルへ Load する
    observation = dict(
        observation_id=observation_id, site_id=site_id, naip_year=year,
        image_date=rep["properties"]["datetime"][:10], source_image_ids=source_image_ids,
        gsd_m=rep["properties"]["gsd"], roi_version=cfg.site["roi_version"],
        roi_geog=roi.to_crs("EPSG:4326").union_all().wkt,
        estimation_method=cfg.params["estimation_method"], model_version=cfg.model_version,
        config_hash=cfg.config_hash, vehicle_count=int(round(total)), vehicle_count_raw=round(total, 3),
        raw_image_uris=[storage.uri(image_path)], processed_uri_prefix=storage.uri(prefix),
        processing_timestamp=datetime.now(timezone.utc).isoformat(),
        processing_seconds=round(time.time() - t0, 2), run_execution_id=run_execution_id,
    )
    patch_rows = [dict(observation_id=observation_id, patch_id=r.patch_id, patch_geog=g.wkt,
                       pixel_x_min=r.pixel_x_min, pixel_y_min=r.pixel_y_min,
                       pixel_x_max=r.pixel_x_max, pixel_y_max=r.pixel_y_max,
                       estimated_count=float(r.estimated_count))
                  for r, g in zip(patches.itertuples(), patches.to_crs("EPSG:4326").geometry)]
    storage.write_text(f"{prefix}/bq/vehicle_observation.ndjson", json.dumps(observation, ensure_ascii=False) + "\n")
    storage.write_text(f"{prefix}/bq/vehicle_density_patch.ndjson",
                       "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in patch_rows))
    return {**observation, **diagnostics}
