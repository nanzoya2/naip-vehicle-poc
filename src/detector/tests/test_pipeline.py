import json

import pytest
import yaml

from detector.config import RunConfig
from detector.pipeline import run
from detector.storage import LocalStorage

from conftest import SITE_ID, YEAR


def _run(scene, core_px=None):
    if core_px is not None:
        p = yaml.safe_load((scene["config_dir"] / "detector.yaml").read_text())
        p["tiling"]["core_px"] = core_px
        (scene["config_dir"] / "detector.yaml").write_text(yaml.safe_dump(p))
    cfg = RunConfig.load(scene["config_dir"], SITE_ID, "test")
    return cfg, run(cfg, LocalStorage(scene["data_root"]), YEAR, "exec-1")


def test_count_close_to_truth(scene):
    _, r = _run(scene)
    assert r["surface"] == {"A_light": "light", "B_dark": "dark"}
    # ROI縁の除外で数台は落ちうるが、区画線（暗い区画）は数えない
    assert r["vehicle_count"] == pytest.approx(scene["n_cars"], rel=0.05)


def test_tiling_does_not_change_result(scene):
    """Tile分割しても1枚で処理しても同じ結果（Core集計・ブロック格子の整合）"""
    _, tiled = _run(scene, core_px=240)
    _, single = _run(scene, core_px=1040)
    assert tiled["tiles_processed"] > 1 and single["tiles_processed"] == 1
    assert tiled["vehicle_count_raw"] == pytest.approx(single["vehicle_count_raw"], abs=1e-6)
    assert tiled["zone_counts"] == single["zone_counts"]


def test_outputs_are_consistent(scene):
    cfg, r = _run(scene)
    prefix = scene["data_root"] / f"processed/{SITE_ID}/{YEAR}/v1/test/{cfg.config_hash}"
    for name in ("overview_density.png", "patches.geojson", "density.tif", "run_config.json",
                 "diagnostics.json", "crops/crop_00.png"):
        assert (prefix / name).exists(), name
    obs = json.loads((prefix / "bq/vehicle_observation.ndjson").read_text())
    patches = [json.loads(line) for line in (prefix / "bq/vehicle_density_patch.ndjson").read_text().splitlines()]
    assert obs["observation_id"] == r["observation_id"]
    assert obs["image_date"] == f"{YEAR}-06-01" and obs["run_execution_id"] == "exec-1"
    assert sum(p["estimated_count"] for p in patches) == pytest.approx(obs["vehicle_count_raw"], abs=0.05)
    assert len({p["patch_id"] for p in patches}) == len(patches)


def test_observation_id_format(scene):
    cfg, r = _run(scene)
    site, year, source_key, roi_v, model_v, chash = r["observation_id"].rsplit("_", 5)
    assert (site, year, roi_v, model_v, chash) == (SITE_ID, str(YEAR), "v1", "test", cfg.config_hash)
    assert len(source_key) == 8


def test_config_hash_changes_with_params(scene):
    cfg_a, _ = _run(scene)
    cfg_b, _ = _run(scene, core_px=480)
    assert cfg_a.config_hash != cfg_b.config_hash


def test_rejects_unaligned_tiling(scene):
    with pytest.raises(ValueError, match="倍数"):
        _run(scene, core_px=250)
