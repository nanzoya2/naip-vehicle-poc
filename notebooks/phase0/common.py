"""Phase 0 共通処理（パス・設定・ROI読込）"""
from pathlib import Path
import json

import geopandas as gpd
import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
ANNOTATION_DIR = ROOT / "annotation"
OUTPUT_DIR = ROOT / "outputs" / "phase0"

WORK_CRS = "EPSG:26916"  # NAIP Tennessee (UTM 16N, NAD83)。メートル単位で処理する
GT_GPKG = ANNOTATION_DIR / "gt_nissan_smyrna_v1.gpkg"


def load_site(site_id="NISSAN_SMYRNA"):
    cfg = yaml.safe_load((CONFIG_DIR / "sites.yaml").read_text(encoding="utf-8"))
    return next(s for s in cfg["sites"] if s["site_id"] == site_id)


def load_roi(site):
    """ROIをWORK_CRSで返す（1行=1区画, 列: zone_id）"""
    return gpd.read_file(CONFIG_DIR / site["roi"]).to_crs(WORK_CRS)


def raw_image_path(site, year):
    """data/raw/{site_id}/{year}/{source_image_id}/image.tif（§11.2と同じ構成）"""
    hits = sorted((DATA_DIR / "raw" / site["site_id"] / str(year)).glob("*/image.tif"))
    if len(hits) != 1:
        raise FileNotFoundError(f"{year}: image.tif が {len(hits)} 件（01_fetch_naip.py を実行）")
    return hits[0]


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))
