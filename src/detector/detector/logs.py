"""構造化ログ（§20.1）。1行1JSONで標準出力へ出し、Cloud Logging が jsonPayload として取り込む"""
import json
import sys
from datetime import datetime, timezone

_context: dict = {}


def bind(**fields):
    """以降のログに共通項目を付与する"""
    _context.update({k: v for k, v in fields.items() if v is not None})


def log(event: str, severity: str = "INFO", **fields):
    entry = {"event": event, "severity": severity, **_context, **fields,
             "timestamp": datetime.now(timezone.utc).isoformat()}
    print(json.dumps(entry, ensure_ascii=False, default=str), file=sys.stdout, flush=True)
