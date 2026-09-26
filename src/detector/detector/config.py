"""設定の読込と config_hash / observation_id の算出（§21）"""
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import yaml

WORK_CRS = "EPSG:26916"  # NAIP Tennessee（UTM 16N, NAD83）。メートル単位で処理する


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


@dataclass
class RunConfig:
    site: dict
    params: dict
    roi_geojson: dict
    model_version: str

    @classmethod
    def load(cls, config_dir: Path, site_id: str, model_version: str) -> "RunConfig":
        sites = yaml.safe_load((config_dir / "sites.yaml").read_text(encoding="utf-8"))["sites"]
        site = next(s for s in sites if s["site_id"] == site_id)
        params = yaml.safe_load((config_dir / "detector.yaml").read_text(encoding="utf-8"))
        roi = json.loads((config_dir / site["roi"]).read_text(encoding="utf-8"))
        cfg = cls(site=site, params=params, roi_geojson=roi, model_version=model_version)
        cfg.validate()
        return cfg

    def validate(self):
        block = self.params["area_ratio"]["bg_block_px"]
        for key in ("core_px", "overlap_px"):
            if self.params["tiling"][key] % block:
                # 背景推定ブロックの格子とTileを揃えないと、Tile分割で結果が変わる
                raise ValueError(f"tiling.{key} は area_ratio.bg_block_px（{block}）の倍数にすること")

    @property
    def run_config(self) -> dict:
        """run_config.json の内容。config_hash の元データ"""
        return {"site_id": self.site["site_id"], "roi_version": self.site["roi_version"],
                "roi": self.roi_geojson, "params": self.params}

    @property
    def config_hash(self) -> str:
        return _sha8(json.dumps(self.run_config, sort_keys=True, ensure_ascii=False))

    def roi(self) -> gpd.GeoDataFrame:
        return gpd.GeoDataFrame.from_features(self.roi_geojson["features"], crs="EPSG:4326").to_crs(WORK_CRS)

    def observation_id(self, year: int, source_image_ids: list[str]) -> str:
        """{site_id}_{naip_year}_{source_key}_{roi_version}_{model_version}_{config_hash}"""
        source_key = _sha8("".join(sorted(source_image_ids)))
        return "_".join([self.site["site_id"], str(year), source_key, self.site["roi_version"],
                         self.model_version, self.config_hash])
