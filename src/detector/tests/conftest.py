"""合成シーン: 舗装（明るい区画・暗い区画）に既知台数の車両を置いた4バンド画像"""
import json

import numpy as np
import pytest
import rasterio
import yaml
from rasterio.transform import from_origin
from shapely.geometry import box, mapping
from shapely.ops import transform as shp_transform
from pyproj import Transformer

H, W = 700, 1000
ORIGIN = (547000.0, 3980000.0)  # UTM 16N
RES = 0.6
YEAR = 2020
SITE_ID = "TEST_SITE"

PARAMS = {
    "estimation_method": "area_ratio",
    "tiling": {"core_px": 240, "overlap_px": 80},
    "patch_m": 50,
    "area_ratio": {"ndvi_veg": 0.25, "bg_block_px": 80, "edge_erode_px": 6, "dark_surface_ratio": 0.85,
                   "dev_thr": 25, "single_area_px": [10, 60], "single_min_n": 30},
}


def _scene(rng):
    img = np.zeros((4, H, W), np.uint8)
    img[:3] = 180                              # 明るい舗装（左）
    img[:3, :, 520:] = 90                      # 暗い舗装（右）
    # 暗い舗装の区画線（縦1px, 横8px の横線）
    for y in range(40, H - 40, 5):
        for x in range(560, W - 40, 12):
            img[:3, y, x:x + 8] = 170
    cars, occupied = 0, np.zeros((H, W), bool)
    for _ in range(4000):
        y, x = rng.integers(30, H - 40), rng.integers(30, W - 40)
        if 500 < x < 560:  # 区画境界付近は置かない
            continue
        if occupied[y - 3:y + 6, x - 3:x + 11].any():
            continue
        occupied[y:y + 3, x:x + 8] = True
        img[:3, y:y + 3, x:x + 8] = 240 if rng.random() < 0.6 else 30
        cars += 1
        if cars >= 600:
            break
    # センサーノイズは最後に全体へ（単一値の線が背景の最頻値になるのを防ぐ。実画像と同じ条件）
    img[:3] = np.clip(img[:3] + rng.normal(0, 3, (3, H, W)), 0, 255).astype(np.uint8)
    img[3] = img[0] // 2                       # NIR < Red → NDVI < 0（植生なし）
    return img, cars


@pytest.fixture
def scene(tmp_path):
    rng = np.random.default_rng(0)
    img, n_cars = _scene(rng)
    transform = from_origin(*ORIGIN, RES, RES)

    raw = tmp_path / "data" / "raw" / SITE_ID / str(YEAR) / "item_a"
    raw.mkdir(parents=True)
    with rasterio.open(raw / "image.tif", "w", driver="GTiff", height=H, width=W, count=4, dtype="uint8",
                       crs="EPSG:26916", transform=transform) as dst:
        dst.write(img)
    (raw / "stac_item.json").write_text(json.dumps({
        "representative": {"id": "item_a", "properties": {"datetime": f"{YEAR}-06-01T00:00:00Z", "gsd": RES}},
        "source_image_ids": ["item_a"]}))

    to_ll = Transformer.from_crs("EPSG:26916", "EPSG:4326", always_xy=True).transform
    x0, y0 = ORIGIN
    zones = {"A_light": box(x0 + 20 * RES, y0 - (H - 20) * RES, x0 + 505 * RES, y0 - 20 * RES),
             "B_dark": box(x0 + 555 * RES, y0 - (H - 20) * RES, x0 + (W - 20) * RES, y0 - 20 * RES)}
    config = tmp_path / "config"
    (config / "roi").mkdir(parents=True)
    (config / "roi" / "test_v1.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"zone_id": k}, "geometry": mapping(shp_transform(to_ll, g))}
        for k, g in zones.items()]}))
    (config / "sites.yaml").write_text(yaml.safe_dump({"sites": [
        {"site_id": SITE_ID, "roi_version": "v1", "roi": "roi/test_v1.geojson", "target_years": [YEAR]}]}))
    (config / "detector.yaml").write_text(yaml.safe_dump(PARAMS))
    return {"config_dir": config, "data_root": tmp_path / "data", "n_cars": n_cars}
