"""面積換算による密度推定（§13.2 優先1）。Phase 0 の baseline-area-v0 と同じアルゴリズム。

  Tile単位（Overlap付き）
    1. ROI区画をラスタ化し、縁を edge_erode_px だけ除外。NDVIで植生を除外
    2. 舗装面の背景色を ブロック×区画 ごとのRGB最頻値で推定
    3. 背景色からの乖離が dev_thr を超える画素を「車両占有」とする（車体＋影）
    4. オープニングでノイズ・区画線を除去（舗装種別で構造要素を変える）
         明るい舗装: 2×2。区画線は低コントラストで閾値を超えない
         暗い舗装  : 縦3×横1。白線は白い車と同程度に明るいが、縦1〜2pxの横線なので
                     縦方向の厚みで車（縦3px以上）と区別する
  画像全体（全TileのCoreを結合後）
    5. 舗装種別: 区画の背景輝度 < 同年の最も明るい区画 × dark_surface_ratio → 暗い舗装
    6. 1台あたり占有面積: 単独車両と思われる連結成分の面積中央値（舗装種別ごと）
    7. 密度 = 占有画素 ÷ 1台あたり占有面積（画素ごとの台数寄与。総和が台数）
"""
import numpy as np
from rasterio.features import rasterize
from scipy import ndimage


def zone_raster(roi_geoms, transform, shape, erode_px):
    """区画番号（1始まり, 0=ROI外）のラスタ。区画ごとに縁を erode_px だけ除外する"""
    zone_map = rasterize([(g, i + 1) for i, g in enumerate(roi_geoms)], out_shape=shape,
                         transform=transform, dtype="uint8")
    inner = np.zeros(shape, bool)
    for z in range(1, len(roi_geoms) + 1):
        inner |= ndimage.binary_erosion(zone_map == z, iterations=erode_px)
    return np.where(inner, zone_map, 0).astype("uint8")


def ndvi(img):
    red, nir = img[0].astype(np.float32), img[3].astype(np.float32)
    return (nir - red) / np.maximum(nir + red, 1)


def _band_mode(values):
    return np.array([np.bincount(values[:, b], minlength=256).argmax() for b in range(values.shape[1])])


def background(rgb, zone_map, valid, block):
    """ブロック×区画ごとのRGB最頻値を背景色とする。

    ブロック格子は配列の原点基準。Tileの原点をブロック格子に揃えることで、
    Tile分割の有無に関わらず同じブロックで背景を推定する。
    """
    h, w, _ = rgb.shape
    bg = np.zeros_like(rgb, dtype=np.float32)
    for y in range(0, h, block):
        for x in range(0, w, block):
            zm = zone_map[y:y + block, x:x + block]
            for z in np.unique(zm[zm > 0]):
                sel = (zm == z) & valid[y:y + block, x:x + block]
                if sel.sum() < 50:  # 画素が少なすぎるブロックは区画全体の値で代用（後で埋める）
                    continue
                bg[y:y + block, x:x + block][zm == z] = _band_mode(rgb[y:y + block, x:x + block][sel])
    for z in np.unique(zone_map[zone_map > 0]):
        m = zone_map == z
        missing = m & (bg.sum(axis=2) == 0)
        if missing.any() and (m & valid).any():
            bg[missing] = _band_mode(rgb[m & valid])
    return bg


class TileResult:
    """1 Tile分の中間結果（Core領域のみ保持）"""

    def __init__(self, zone_map, valid, bg, dev):
        self.zone_map, self.valid, self.bg, self.dev = zone_map, valid, bg, dev


def prepare_tile(img, transform, roi_geoms, p):
    """手順1〜3（舗装種別に依存しない部分）"""
    rgb = np.moveaxis(img[:3], 0, -1)
    zone_map = zone_raster(roi_geoms, transform, rgb.shape[:2], p["edge_erode_px"])
    valid = (zone_map > 0) & (ndvi(img) <= p["ndvi_veg"])
    bg = background(rgb, zone_map, valid, p["bg_block_px"])
    dev = np.abs(rgb.astype(np.float32) - bg).max(axis=2)
    return TileResult(zone_map, valid, bg, dev)


def zone_luminance_sums(t: TileResult, n_zones):
    """舗装種別判定用: 区画ごとの背景輝度の (合計, 画素数)"""
    lum = t.bg.mean(axis=2)
    sums = np.bincount(t.zone_map.ravel(), weights=lum.ravel(), minlength=n_zones + 1)
    counts = np.bincount(t.zone_map.ravel(), minlength=n_zones + 1)
    return sums, counts


def dark_zones_from_luminance(sums, counts, ratio):
    mean = {z: sums[z] / counts[z] for z in range(1, len(sums)) if counts[z] > 0}
    top = max(mean.values())
    return sorted(z for z, v in mean.items() if v < top * ratio)


def occupancy(t: TileResult, dark_zones, p):
    """手順4"""
    dark = np.isin(t.zone_map, dark_zones)
    raw = t.valid & (t.dev > p["dev_thr"])
    return (ndimage.binary_opening(raw & ~dark, structure=np.ones((2, 2), bool))
            | ndimage.binary_opening(raw & dark, structure=np.ones((3, 1), bool)))


def calibrate(occ, dark, p):
    """手順6: 舗装種別ごとの1台あたり占有面積 {name: (面積px, 単独成分数)}"""
    labels, _ = ndimage.label(occ, structure=np.ones((3, 3)))
    areas = np.bincount(labels.ravel())
    comp_dark = np.bincount(labels.ravel(), weights=dark.ravel()) > areas / 2
    lo, hi = p["single_area_px"]
    is_single = (areas >= lo) & (areas <= hi)
    is_single[0] = False
    if not is_single.any():
        raise ValueError("単独車両と思われる連結成分が0件のため校正できない")
    calib = {"all": (float(np.median(areas[is_single])), int(is_single.sum()))}
    for name, sel in (("light", ~comp_dark), ("dark", comp_dark)):
        a = areas[is_single & sel]
        calib[name] = (float(np.median(a)), int(len(a))) if len(a) >= p["single_min_n"] else calib["all"]
    return calib


def density(occ, dark, calib):
    """手順7: 画素ごとの台数寄与（float32）"""
    apv = np.where(dark, calib["dark"][0], calib["light"][0])
    return np.where(occ, 1.0 / apv, 0.0).astype(np.float32)
