"""Ground Truth と推定値を比較し、Gate 1 を判定する（§22）。

入力
  annotation/gt_nissan_smyrna_v1.gpkg  cells（done_{year}=1 のセルのみ使用）, points_{year}
  outputs/phase0/baseline/{year}/cells_estimate.geojson

処理
  1. セル内の点を数えて GT とする（点がセルとROI区画の両方に含まれるもの）
  2. 生の推定値で評価（自己校正のまま）
  3. 年ごとに train セルで校正係数 k = ΣGT / Σ推定 を求め、同年の eval セルに適用
  4. Gate 1: 校正後 eval セルを全年プールした Count Error% が ±15% 以内か

出力
  outputs/phase0/evaluation/cell_level.csv      セル別の GT / 推定
  outputs/phase0/evaluation/ground_truth.csv    vehicle_ground_truth（§16.4）形式
  outputs/phase0/evaluation/summary.json

    python notebooks/phase0/04_evaluate.py
"""
import json
import os
from datetime import datetime, timezone

import geopandas as gpd
import numpy as np
import pandas as pd

from common import GT_GPKG, OUTPUT_DIR, WORK_CRS, load_roi, load_site

GATE1_MAX_ABS_ERROR_PCT = 15.0


def count_error_pct(est, gt):
    return float((est - gt) / gt * 100) if gt > 0 else float("nan")


def metrics(df, col):
    err = df[col] - df.gt_count
    return dict(n_cells=int(len(df)), gt_total=int(df.gt_count.sum()), est_total=round(float(df[col].sum()), 1),
                count_error_pct=round(count_error_pct(df[col].sum(), df.gt_count.sum()), 2),
                patch_mae=round(float(err.abs().mean()), 2), patch_rmse=round(float(np.sqrt((err ** 2).mean())), 2))


def load_year(site, roi, cells, year):
    done = cells[cells[f"done_{year}"] == 1]
    if done.empty:
        return None
    points = gpd.read_file(GT_GPKG, layer=f"points_{year}").to_crs(WORK_CRS)
    # 点は「セル × ROI区画」に含まれるものだけ数える（ROI外の車両は対象外）
    targets = gpd.overlay(done[["cell_id", "zone_id", "split", "geometry"]],
                          roi[["zone_id", "geometry"]].rename(columns={"zone_id": "roi_zone"}), how="intersection")
    targets = targets[targets.zone_id == targets.roi_zone].drop(columns="roi_zone")
    joined = gpd.sjoin(points, targets, predicate="within")
    gt = joined.groupby(["cell_id", "zone_id"]).size().rename("gt_count")

    est = gpd.read_file(OUTPUT_DIR / "baseline" / str(year) / "cells_estimate.geojson")
    df = (targets.drop(columns="geometry")
          .merge(gt.reset_index(), on=["cell_id", "zone_id"], how="left")
          .merge(est[["cell_id", "zone_id", "estimated_count"]], on=["cell_id", "zone_id"], how="left"))
    df["gt_count"] = df.gt_count.fillna(0).astype(int)
    df["naip_year"] = year
    df["geometry_wkt"] = targets.to_crs("EPSG:4326").geometry.to_wkt().values
    return df


def main():
    site = load_site()
    roi = load_roi(site)
    cells = gpd.read_file(GT_GPKG, layer="cells").to_crs(WORK_CRS)
    frames = [f for y in site["target_years"] if (f := load_year(site, roi, cells, y)) is not None]
    if not frames:
        raise SystemExit("done_{year}=1 のセルがありません。QGISでアノテーション後、cells の done_{year} を 1 にしてください")
    df = pd.concat(frames, ignore_index=True)

    # 校正係数は年ごと（影の長さが季節で変わるため）。Gate 1 は校正後の eval を全年プールして判定する
    summary = {"estimation_method": "area_ratio", "gate1_max_abs_error_pct": GATE1_MAX_ABS_ERROR_PCT,
               "by_year": {}}
    df["calibrated_count"] = np.nan
    for year, sub in df.groupby("naip_year"):
        train, ev = sub[sub.split == "train"], sub[sub.split == "eval"]
        k = float(train.gt_count.sum() / train.estimated_count.sum()) if train.estimated_count.sum() > 0 else np.nan
        df.loc[sub.index, "calibrated_count"] = sub.estimated_count * k
        summary["by_year"][str(year)] = {
            "calibration_k": round(k, 4),
            "raw": {s: metrics(g, "estimated_count") for s, g in (("train", train), ("eval", ev)) if len(g)},
            "calibrated_eval": metrics(df.loc[ev.index], "calibrated_count") if len(ev) else None,
        }
    ev_all = df[(df.split == "eval") & df.calibrated_count.notna()]
    if len(ev_all):
        m = metrics(ev_all, "calibrated_count")
        summary["gate1"] = {**m, "pass": bool(abs(m["count_error_pct"]) <= GATE1_MAX_ABS_ERROR_PCT)}

    out = OUTPUT_DIR / "evaluation"
    out.mkdir(parents=True, exist_ok=True)
    df.to_csv(out / "cell_level.csv", index=False)

    # vehicle_ground_truth（§16.4）形式
    now = datetime.now(timezone.utc).isoformat()
    gt_rows = df.assign(site_id=site["site_id"], roi_version=site["roi_version"],
                        patch_id=df.cell_id + "_" + df.zone_id, patch_geog=df.geometry_wkt,
                        annotator=os.environ.get("ANNOTATOR", ""), annotated_at=now)
    gt_rows[["site_id", "naip_year", "roi_version", "patch_id", "patch_geog", "split", "gt_count",
             "annotator", "annotated_at"]].to_csv(out / "ground_truth.csv", index=False)
    (out / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(summary, indent=1, ensure_ascii=False))
    if "gate1" in summary:
        g = summary["gate1"]
        print(f"\nGate 1（eval, 年別校正後, 全年プール）: Count Error% = {g['count_error_pct']:+.1f}% "
              f"→ {'PASS' if g['pass'] else 'FAIL'}")


if __name__ == "__main__":
    main()
