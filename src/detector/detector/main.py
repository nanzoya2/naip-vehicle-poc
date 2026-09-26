"""Job #2 エントリポイント。

Cloud Run Jobs では CLOUD_RUN_TASK_INDEX で担当年を決める（§12.2）。ローカルでは --year で指定する。

    python -m detector.main --year 2018
    python -m detector.main --all-years
"""
import argparse
import os
import sys
from pathlib import Path

from . import logs
from .config import RunConfig
from .pipeline import run
from .storage import LocalStorage

REPO_ROOT = Path(__file__).resolve().parents[3]


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="NAIP 車両台数推定（面積換算）")
    ap.add_argument("--site-id", default=os.environ.get("SITE_ID", "NISSAN_SMYRNA"))
    ap.add_argument("--year", type=int, help="対象年（省略時は CLOUD_RUN_TASK_INDEX から決定）")
    ap.add_argument("--all-years", action="store_true", help="sites.yaml の対象年をすべて処理（ローカル用）")
    ap.add_argument("--config-dir", type=Path, default=Path(os.environ.get("CONFIG_DIR", REPO_ROOT / "config")))
    ap.add_argument("--data-root", type=Path, default=Path(os.environ.get("DATA_ROOT", REPO_ROOT / "data")),
                    help="バケットに見立てるローカルディレクトリ（raw/, processed/ を含む）")
    return ap.parse_args(argv)


def target_years(cfg: RunConfig, args) -> list[int]:
    years = cfg.site["target_years"]
    if args.all_years:
        return years
    if args.year is not None:
        return [args.year]
    index = int(os.environ.get("CLOUD_RUN_TASK_INDEX", "-1"))
    if not 0 <= index < len(years):
        raise SystemExit(f"--year 未指定かつ CLOUD_RUN_TASK_INDEX={index} が対象年の範囲外（{years}）")
    return [years[index]]


def main(argv=None):
    args = parse_args(argv)
    model_version = os.environ.get("MODEL_VERSION", "dev")  # コンテナではイメージタグを渡す（§13.1）
    run_execution_id = os.environ.get("CLOUD_RUN_EXECUTION", "local")
    cfg = RunConfig.load(args.config_dir, args.site_id, model_version)
    logs.bind(job_name=os.environ.get("CLOUD_RUN_JOB", "detector-local"), run_execution_id=run_execution_id,
              site_id=args.site_id, model_version=model_version, config_hash=cfg.config_hash)
    storage = LocalStorage(args.data_root)

    for year in target_years(cfg, args):
        logs.bind(naip_year=year)
        try:
            result = run(cfg, storage, year, run_execution_id)
        except Exception as e:
            logs.log("image_processing_failed", severity="ERROR", error_type=type(e).__name__, error_message=str(e))
            raise
        logs.log("image_processing_complete", observation_id=result["observation_id"],
                 vehicle_count=result["vehicle_count"], processing_seconds=result["processing_seconds"])


if __name__ == "__main__":
    sys.exit(main())
