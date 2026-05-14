#!/usr/bin/env python
"""Print read-only service readiness status for BE integration."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.ops.service_readiness_status import build_service_status  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")
_REPORT_DIR = ROOT / "artifacts" / "reports" / "service_readiness"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--output-dir", default=str(_REPORT_DIR))
    parser.add_argument("--no-write-report", action="store_true")
    args = parser.parse_args(argv)
    payload = build_service_status(bundle_id=str(args.bundle_id), root=ROOT)
    if not bool(args.no_write_report):
        output_dir = Path(str(args.output_dir))
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / (
            f"service_readiness_{args.bundle_id}_{datetime.now(_KST).strftime('%Y%m%d_%H%M%S')}.json"
        )
        payload["report_path"] = str(path)
        try:
            payload["report_path_relative"] = str(path.relative_to(ROOT))
        except ValueError:
            payload["report_path_relative"] = str(path)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ssot_readiness") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
