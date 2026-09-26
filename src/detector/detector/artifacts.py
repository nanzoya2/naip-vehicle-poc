"""確認用成果物（§11.4）の生成。すべて bytes / str で返し、保存は呼び出し側で行う"""
import io
import json

import numpy as np
import rasterio
from PIL import Image, ImageDraw
from rasterio.io import MemoryFile
from scipy import ndimage

# 密度ヒートマップの配色（低→高: 透明寄りの青 → 黄 → 赤）
_LUT_STOPS = np.array([[0.0, 40, 70, 200], [0.5, 250, 220, 40], [1.0, 230, 30, 30]])
CROP_SIZE_PX = 120   # 拡大クロップの切り出し範囲（72m四方）
CROP_SCALE = 4       # 最近傍補間での拡大率


def _colormap(v):
    """0〜1 の配列を RGB へ"""
    return np.stack([np.interp(v, _LUT_STOPS[:, 0], _LUT_STOPS[:, i]) for i in (1, 2, 3)], axis=-1)


def _png(arr) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


def density_heatmap(rgb, dens, zone_map, total_count) -> bytes:
    """overview_density.png: ROI全体の密度分布を原画像に重畳（ROI外は暗く）"""
    smooth = ndimage.gaussian_filter(dens, sigma=6)  # 約3.6m。台/画素 → 見やすい連続量へ
    in_roi = zone_map > 0
    vmax = np.percentile(smooth[in_roi], 99) if in_roi.any() else 1.0
    v = np.clip(smooth / max(vmax, 1e-9), 0, 1)
    alpha = (0.15 + 0.55 * v)[..., None] * in_roi[..., None]
    out = rgb.astype(np.float32) * (1 - alpha) + _colormap(v) * alpha
    out[~in_roi] *= 0.4
    img = Image.fromarray(out.astype(np.uint8))
    ImageDraw.Draw(img).text((8, 8), f"estimated vehicles: {total_count:,.0f}", fill=(255, 255, 255))
    return _png(np.asarray(img))


def crops(rgb, occ, patches, n_top=2, n_random=2, seed=0) -> list[tuple[str, bytes]]:
    """crops/crop_{nn}.png: 推定台数の多いパッチと無作為パッチの中心を4倍拡大（左: 原画像, 右: 占有画素）"""
    nonempty = patches[patches.estimated_count > 0]
    if nonempty.empty:
        return []
    top = nonempty.nlargest(n_top, "estimated_count")
    rest = nonempty.drop(top.index)
    rnd = rest.sample(min(n_random, len(rest)), random_state=seed) if len(rest) else rest
    over = rgb.copy()
    over[occ] = (0.4 * over[occ] + 0.6 * np.array([255, 0, 0])).astype(np.uint8)
    out = []
    for k, (_, p) in enumerate(list(top.iterrows()) + list(rnd.iterrows())):
        cy = (p.pixel_y_min + p.pixel_y_max) // 2
        cx = (p.pixel_x_min + p.pixel_x_max) // 2
        y0 = int(np.clip(cy - CROP_SIZE_PX // 2, 0, max(0, rgb.shape[0] - CROP_SIZE_PX)))
        x0 = int(np.clip(cx - CROP_SIZE_PX // 2, 0, max(0, rgb.shape[1] - CROP_SIZE_PX)))
        size = (CROP_SIZE_PX * CROP_SCALE, CROP_SIZE_PX * CROP_SCALE)
        a = Image.fromarray(rgb[y0:y0 + CROP_SIZE_PX, x0:x0 + CROP_SIZE_PX]).resize(size, Image.NEAREST)
        b = Image.fromarray(over[y0:y0 + CROP_SIZE_PX, x0:x0 + CROP_SIZE_PX]).resize(size, Image.NEAREST)
        pair = Image.new("RGB", (size[0] * 2 + 10, size[1] + 24), "white")
        pair.paste(a, (0, 24)); pair.paste(b, (size[0] + 10, 24))
        ImageDraw.Draw(pair).text((4, 4), f"{p.patch_id}  estimated={p.estimated_count:.1f}", fill=(0, 0, 0))
        out.append((f"crops/crop_{k:02d}.png", _png(np.asarray(pair))))
    return out


def density_geotiff(dens, transform, crs) -> bytes:
    """density.tif: 画素ごとの台数寄与（float32, 総和が台数）"""
    profile = dict(driver="GTiff", height=dens.shape[0], width=dens.shape[1], count=1, dtype="float32",
                   crs=crs, transform=transform, compress="deflate", predictor=3, tiled=True,
                   blockxsize=256, blockysize=256)
    with MemoryFile() as mem:
        with mem.open(**profile) as dst:
            dst.write(dens, 1)
            dst.set_band_description(1, "vehicles_per_pixel")
        return mem.read()


def patches_geojson(patches) -> str:
    """patches.geojson: パッチ別推定台数（EPSG:4326）"""
    return patches.to_crs("EPSG:4326").to_json(drop_id=True, ensure_ascii=False)


def run_config_json(run_config: dict, config_hash: str) -> str:
    return json.dumps({**run_config, "config_hash": config_hash}, indent=1, ensure_ascii=False, sort_keys=True)
