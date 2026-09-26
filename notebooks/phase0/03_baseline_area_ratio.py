"""面積換算ベースライン（§13.2 優先1）。

  1. NDVIで植生を除外、ROI縁（縁石・境界）を数px除外
  2. 舗装面の背景色を ブロック×区画 ごとのRGB最頻値で推定
  3. 背景色からの乖離が閾値を超える画素を「車両占有」とする（車体＋影）
  4. オープニングでノイズ・区画線を除去（舗装種別で構造要素を変える）
       明るい舗装（コンクリート）: 2×2。区画線は低コントラストで閾値を超えない
       暗い舗装（アスファルト）  : 縦3×横1。白線は背景+50〜100DNと白い車と同程度に明るいが、
                                   縦1〜2pxの横線なので縦方向の厚みで車（縦3px以上）と区別する
  5. 占有面積 ÷ 1台あたり占有面積 = 台数

1台あたり占有面積は、GTがまだないため「単独車両と思われる連結成分の面積中央値」で
年×舗装種別ごとに自己校正する（影の長さが季節で変わり、暗い舗装では影が見えないため）。
GT作成後は 04_evaluate.py で train セルを使って校正係数を求め直す。

    python notebooks/phase0/03_baseline_area_ratio.py
"""
import hashlib
import json
import time

import geopandas as gpd
import numpy as np
import rasterio
from PIL import Image
from rasterio.features import rasterize
from scipy import ndimage

from common import GT_GPKG, OUTPUT_DIR, load_roi, load_site, raw_image_path

MODEL_VERSION = "baseline-area-v0"
PARAMS = dict(
    ndvi_veg=0.25,          # これを超える画素は植生として除外
    bg_block_px=80,         # 背景推定ブロック（48m）
    edge_erode_px=6,        # ROI縁から内側へ除外する幅（約3.6m）
    dark_surface_ratio=0.85,  # 区画の背景輝度 < 同年の最も明るい区画 × これ → 暗い舗装
    dev_thr=25,             # 背景色からの乖離（RGB各バンドの最大差, DN）がこれを超えたら占有
    single_area_px=[10, 60],  # 単独車両とみなす連結成分の面積範囲（車体 約22px＋影）
    single_min_n=30,          # 単独車両がこれ未満の舗装種別は、同年の全体値で代用
)


def config_hash(params, roi_version):
    s = json.dumps({"params": params, "roi_version": roi_version, "model": MODEL_VERSION}, sort_keys=True)
    return hashlib.sha256(s.encode()).hexdigest()[:8]


def band_mode(values):
    return np.array([np.bincount(values[:, b], minlength=256).argmax() for b in range(values.shape[1])])


def background(rgb, zone_map, valid, block):
    """ブロック×区画ごとのRGB最頻値を背景色とする"""
    h, w, _ = rgb.shape
    bg = np.zeros_like(rgb, dtype=np.float32)
    for y in range(0, h, block):
        for x in range(0, w, block):
            zm = zone_map[y:y + block, x:x + block]
            for z in np.unique(zm[zm > 0]):
                sel = (zm == z) & valid[y:y + block, x:x + block]
                if sel.sum() < 50:  # 画素が少なすぎるブロックは区画全体の値で代用（後で埋める）
                    continue
                bg[y:y + block, x:x + block][zm == z] = band_mode(rgb[y:y + block, x:x + block][sel])
    for z in np.unique(zone_map[zone_map > 0]):
        m = zone_map == z
        missing = m & (bg.sum(axis=2) == 0)
        if missing.any():
            bg[missing] = band_mode(rgb[m & valid])
    return bg


def estimate(site, year, roi, grid, chash):
    t0 = time.time()
    path = raw_image_path(site, year)
    with rasterio.open(path) as src:
        img = src.read()
        transform, shape = src.transform, (src.height, src.width)
    rgb = np.moveaxis(img[:3], 0, -1)
    red, nir = img[0].astype(np.float32), img[3].astype(np.float32)
    ndvi = (nir - red) / np.maximum(nir + red, 1)

    zone_ids = list(roi.zone_id)
    zone_map = rasterize([(g, i + 1) for i, g in enumerate(roi.geometry)], out_shape=shape,
                         transform=transform, dtype="uint8")
    inner = np.zeros(shape, bool)
    for i in range(len(zone_ids)):
        inner |= ndimage.binary_erosion(zone_map == i + 1, iterations=PARAMS["edge_erode_px"])
    zone_map = np.where(inner, zone_map, 0).astype("uint8")
    valid = inner & (ndvi <= PARAMS["ndvi_veg"])

    bg = background(rgb, zone_map, valid, PARAMS["bg_block_px"])
    dev = np.abs(rgb.astype(np.float32) - bg).max(axis=2)

    # 舗装種別（区画ごと）
    lum = {i + 1: float(bg[zone_map == i + 1].mean()) for i in range(len(zone_ids))}
    dark_zones = [z for z, v in lum.items() if v < max(lum.values()) * PARAMS["dark_surface_ratio"]]
    dark = np.isin(zone_map, dark_zones)
    surface = {zone_ids[z - 1]: ("dark" if z in dark_zones else "light") for z in lum}

    # 区画ごとの分布統計（中央値+MAD）は満車区画で閾値が車両側に寄るため使わない
    raw = valid & (dev > PARAMS["dev_thr"])
    occ = (ndimage.binary_opening(raw & ~dark, structure=np.ones((2, 2), bool))
           | ndimage.binary_opening(raw & dark, structure=np.ones((3, 1), bool)))

    # 1台あたり占有面積の自己校正（年×舗装種別）
    labels, _ = ndimage.label(occ, structure=np.ones((3, 3)))
    areas = np.bincount(labels.ravel())
    comp_dark = np.bincount(labels.ravel(), weights=dark.ravel()) > areas / 2
    lo, hi = PARAMS["single_area_px"]
    is_single = (areas >= lo) & (areas <= hi)
    is_single[0] = False
    calib = {"all": (float(np.median(areas[is_single])), int(is_single.sum()))}
    for name, sel in (("light", ~comp_dark), ("dark", comp_dark)):
        a = areas[is_single & sel]
        calib[name] = (float(np.median(a)), len(a)) if len(a) >= PARAMS["single_min_n"] else calib["all"]
    apv = np.where(dark, calib["dark"][0], calib["light"][0])  # 画素ごとの1台あたり面積

    per_px = np.where(occ, 1.0 / apv, 0.0)  # 画素ごとの台数寄与
    zone_counts = {z: float(per_px[zone_map == i + 1].sum()) for i, z in enumerate(zone_ids)}
    total = sum(zone_counts.values())

    # セル別推定（grid × zone の交差ごと）
    cell_est = []
    for _, c in grid.iterrows():
        zi = zone_ids.index(c.zone_id) + 1
        cm = rasterize([(c.geometry, 1)], out_shape=shape, transform=transform, dtype="uint8").astype(bool)
        cell_est.append(float(per_px[cm & (zone_map == zi)].sum()))
    cells = grid.copy()
    cells["estimated_count"] = np.round(cell_est, 2)

    out = OUTPUT_DIR / "baseline" / str(year)
    out.mkdir(parents=True, exist_ok=True)
    save_visuals(rgb, occ, zone_map, out)
    cells.to_crs("EPSG:4326").to_file(out / "cells_estimate.geojson", driver="GeoJSON")
    result = dict(
        site_id=site["site_id"], naip_year=year, source_image_id=path.parent.name,
        roi_version=site["roi_version"], estimation_method="area_ratio", model_version=MODEL_VERSION,
        config_hash=chash, vehicle_count=int(round(total)), vehicle_count_raw=round(total, 1),
        zone_counts={k: round(v, 1) for k, v in zone_counts.items()},
        surface=surface, area_per_vehicle_px={k: v[0] for k, v in calib.items()},
        single_blob_n={k: v[1] for k, v in calib.items()},
        occupied_ratio=round(float(occ.sum() / (zone_map > 0).sum()), 4),
        processing_seconds=round(time.time() - t0, 1),
    )
    (out / "result.json").write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    return result


def save_visuals(rgb, occ, zone_map, out):
    """確認用成果物（§11.4）: 全体重畳と、4倍拡大の原画像｜占有マスク比較"""
    over = rgb.copy()
    over[zone_map == 0] = (over[zone_map == 0] * 0.4).astype(np.uint8)
    over[occ] = (0.4 * over[occ] + 0.6 * np.array([255, 0, 0])).astype(np.uint8)
    Image.fromarray(over).save(out / "overview_occupancy.png")

    ys, xs = np.nonzero(zone_map)
    rng = np.random.default_rng(0)
    for k in range(4):
        i = rng.integers(len(ys))
        y, x = max(0, ys[i] - 60), max(0, xs[i] - 90)
        a = Image.fromarray(rgb[y:y + 120, x:x + 180]).resize((720, 480), Image.NEAREST)
        b = Image.fromarray(over[y:y + 120, x:x + 180]).resize((720, 480), Image.NEAREST)
        pair = Image.new("RGB", (1450, 480), "white")
        pair.paste(a, (0, 0)); pair.paste(b, (730, 0))
        (out / "crops").mkdir(exist_ok=True)
        pair.save(out / "crops" / f"crop_{k:02d}.png")


def main():
    site = load_site()
    roi = load_roi(site)
    grid = gpd.read_file(GT_GPKG, layer="grid")
    chash = config_hash(PARAMS, site["roi_version"])
    (OUTPUT_DIR / "baseline").mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "baseline" / "run_config.json").write_text(
        json.dumps({"model_version": MODEL_VERSION, "config_hash": chash, "params": PARAMS,
                    "roi_version": site["roi_version"]}, indent=1), encoding="utf-8")
    rows = [estimate(site, y, roi, grid, chash) for y in site["target_years"]]
    for r in rows:
        print(f"{r['naip_year']}: {r['vehicle_count']}台  occ={r['occupied_ratio'] * 100:.1f}%  "
              f"zones={r['zone_counts']}\n      surface={r['surface']}  "
              f"A1px={r['area_per_vehicle_px']}  n={r['single_blob_n']}")


if __name__ == "__main__":
    main()
