"""Planetary Computer STAC から ROI+バッファ範囲の NAIP を取得し data/raw へ保存する。

Job #1（§10）のローカル版。画素値は変換せず、空間的な切り出しのみ行う（§11.3）。
ROIが複数DOQQに跨る場合はモザイクし、source_image_id はROI被覆率最大の画像とする（§5.2）。

    python notebooks/phase0/01_fetch_naip.py
"""
import json

import planetary_computer
import pystac_client
import rasterio
from rasterio.merge import merge
from shapely.geometry import box, shape
from shapely.ops import transform as shp_transform
from pyproj import Transformer

from common import DATA_DIR, WORK_CRS, load_roi, load_site

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"


def main():
    site = load_site()
    roi = load_roi(site)
    roi_union = roi.union_all()
    minx, miny, maxx, maxy = roi_union.buffer(site["buffer_m"]).bounds
    bbox_ll = roi.to_crs("EPSG:4326").total_bounds.tolist()

    catalog = pystac_client.Client.open(STAC_URL, modifier=planetary_computer.sign_inplace)
    to_work = Transformer.from_crs("EPSG:4326", WORK_CRS, always_xy=True).transform

    for year in site["target_years"]:
        items = [i for i in catalog.search(collections=["naip"], bbox=bbox_ll, datetime=str(year)).items()
                 if i.properties["gsd"] <= 0.6]
        if not items:
            print(f"{year}: 0.6m以下の画像なし"); continue

        # ROI被覆率が最大の画像を代表とする
        cover = {i.id: shp_transform(to_work, shape(i.geometry)).intersection(roi_union).area / roi_union.area
                 for i in items}
        rep = max(items, key=lambda i: cover[i.id])
        out_dir = DATA_DIR / "raw" / site["site_id"] / str(year) / rep.id
        if (out_dir / "image.tif").exists():
            print(f"{year}: skip（既存）{rep.id}"); continue

        srcs = [rasterio.open(i.assets["image"].href) for i in items]
        assert all(str(s.crs) == WORK_CRS for s in srcs), [str(s.crs) for s in srcs]
        arr, transform = merge(srcs, bounds=(minx, miny, maxx, maxy))

        out_dir.mkdir(parents=True, exist_ok=True)
        profile = dict(driver="GTiff", height=arr.shape[1], width=arr.shape[2], count=arr.shape[0],
                       dtype=arr.dtype, crs=WORK_CRS, transform=transform,
                       compress="deflate", tiled=True, blockxsize=256, blockysize=256)
        with rasterio.open(out_dir / "image.tif", "w", **profile) as dst:
            dst.write(arr)
            dst.descriptions = ("red", "green", "blue", "nir")
        rep_dict = rep.to_dict()
        for a in rep_dict["assets"].values():  # 署名付きURL（SASトークン）は保存しない
            a["href"] = a["href"].split("?")[0]
        (out_dir / "stac_item.json").write_text(json.dumps(
            {"representative": rep_dict, "source_image_ids": [i.id for i in items],
             "roi_coverage": cover}, indent=1, default=str), encoding="utf-8")
        print(f"{year}: {rep.id} {arr.shape} coverage={cover[rep.id]:.3f} "
              f"date={rep.datetime.date()} gsd={rep.properties['gsd']}")


if __name__ == "__main__":
    main()
