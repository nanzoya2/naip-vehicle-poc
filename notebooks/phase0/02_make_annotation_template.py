"""Ground Truth アノテーション用の GeoPackage を作成する（§22.1）。

annotation/gt_nissan_smyrna_v1.gpkg に以下のレイヤを作る。
  grid           ROIを覆う50mグリッド全体（ベースラインのセル別集計に使用）
  cells          アノテーション対象セル（gridから層化抽出, split=train/eval）
                 done_{year}: そのセルの点入力が完了したら 1 にする（0台のセルと未作業を区別するため）
  points_{year}  1台1点の点レイヤ（空）。QGISで編集する

既存のgpkgは上書きしない（アノテーション済みの点を守るため）。
セルを作り直す場合は、点を退避してから gpkg を手動で削除すること。

    python notebooks/phase0/02_make_annotation_template.py
"""
import math

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import box

from common import GT_GPKG, WORK_CRS, load_roi, load_site

CELL_M = 50           # 設計書は100m四方だが、2018年の満車時は1セル数百台になるため50mとする
MIN_INSIDE = 0.95     # アノテーション対象はROI内が95%以上のセルのみ
CELLS_PER_ZONE = {"Z1_north": 6, "Z2_east": 3, "Z3_middle": 9, "Z4_south": 6}
BLOCK_M = 150         # 空間ホールドアウトの単位（3×3セル）
EVAL_RATIO = 1 / 3
SEED = 20260923
YEARS = load_site()["target_years"]


def make_grid(roi):
    minx, miny, maxx, maxy = roi.total_bounds
    x0, y0 = math.floor(minx / CELL_M) * CELL_M, math.floor(miny / CELL_M) * CELL_M
    rows = []
    for x in np.arange(x0, maxx, CELL_M):
        for y in np.arange(y0, maxy, CELL_M):
            cell = box(x, y, x + CELL_M, y + CELL_M)
            for zone_id, geom in zip(roi.zone_id, roi.geometry):
                inside = cell.intersection(geom).area / cell.area
                if inside > 0:
                    rows.append(dict(cell_id=f"E{int(x)}_N{int(y)}", zone_id=zone_id,
                                     inside_ratio=round(inside, 3),
                                     block_id=f"B{int(x // BLOCK_M)}_{int(y // BLOCK_M)}", geometry=cell))
    return gpd.GeoDataFrame(rows, crs=WORK_CRS)


def select_cells(grid):
    rng = np.random.default_rng(SEED)
    full = grid[grid.inside_ratio >= MIN_INSIDE]
    picked = []
    for zone_id, n in CELLS_PER_ZONE.items():
        cand = full[full.zone_id == zone_id]
        picked.append(cand.iloc[rng.choice(len(cand), size=min(n, len(cand)), replace=False)])
    cells = pd.concat(picked).copy()

    # ブロック単位で eval を割り当てる（同一ブロックのセルは必ず同じsplit）
    blocks = cells.block_id.unique().tolist()
    rng.shuffle(blocks)
    target = round(len(cells) * EVAL_RATIO)
    eval_blocks, n_eval = set(), 0
    for b in blocks:
        n = (cells.block_id == b).sum()
        if n_eval + n <= target:  # 目標数を超えるブロックは飛ばす
            eval_blocks.add(b)
            n_eval += n
    cells["split"] = np.where(cells.block_id.isin(eval_blocks), "eval", "train")
    for year in YEARS:
        cells[f"done_{year}"] = 0
    return cells.sort_values(["split", "zone_id", "cell_id"])


def main():
    if GT_GPKG.exists():
        raise SystemExit(f"{GT_GPKG} は既に存在します（アノテーション保護のため上書きしません）")
    site = load_site()
    roi = load_roi(site)
    grid = make_grid(roi)
    cells = select_cells(grid)

    GT_GPKG.parent.mkdir(parents=True, exist_ok=True)
    grid.to_file(GT_GPKG, layer="grid", driver="GPKG")
    cells.to_file(GT_GPKG, layer="cells", driver="GPKG")
    for year in site["target_years"]:
        empty = gpd.GeoDataFrame({"note": pd.Series(dtype="str")}, geometry=gpd.GeoSeries([], crs=WORK_CRS))
        empty.to_file(GT_GPKG, layer=f"points_{year}", driver="GPKG", geometry_type="Point")

    print(f"grid: {len(grid)} セル, ROI面積 {roi.area.sum() / 1e4:.1f} ha")
    print(cells.groupby(["zone_id", "split"]).size().unstack(fill_value=0))
    print(f"-> {GT_GPKG}")


if __name__ == "__main__":
    main()
