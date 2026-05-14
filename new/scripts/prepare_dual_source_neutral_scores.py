#!/usr/bin/env python
"""Create a PIT-safe neutral Dual-Source score file for paper rehearsal.

This is for service rehearsal only when connector credentials or external
feeds are unavailable. It writes neutral real-empty scores so QuantAgent active
inference can verify its feature contract without pretending that news/community
signals were observed.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.data.dataset_builder import DUAL_SOURCE_FEATURES  # noqa: E402
from src.utils.config_loader import load as config_load  # noqa: E402
from src.utils.ticker_utils import pad_ticker  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")


def _active_tickers() -> list[str]:
    cfg = config_load("universe_config.yaml") or {}
    tickers: list[str] = []
    for sector in (cfg.get("sectors") or {}).values():
        for item in sector.get("stocks", []) or []:
            if str(item.get("status", "")).lower() == "active":
                tickers.append(pad_ticker(str(item.get("ticker", ""))))
    return sorted(set(tickers))


def _neutral_row(ticker: str) -> dict[str, float | str]:
    row: dict[str, float | str] = {"ticker": pad_ticker(ticker)}
    for feature in DUAL_SOURCE_FEATURES:
        row[feature] = 1.0 if feature == "community_noise_multiplier" else 0.0
    return row


def build_payload(date_str: str, tickers: list[str]) -> dict:
    batch_date = datetime.strptime(date_str, "%Y%m%d").date()
    snapshot_ts = datetime.combine(batch_date, time(8, 30, 0), tzinfo=_KST)
    scores = [_neutral_row(ticker) for ticker in tickers]
    return {
        "batch_date": batch_date.isoformat(),
        "snapshot_ts": snapshot_ts.isoformat(),
        "generated_at": datetime.now(_KST).isoformat(),
        "ticker_count": len(scores),
        "source_stats": {
            "input_mode": "real",
            "news_mode": "unavailable_empty",
            "community_mode": "unavailable_empty",
            "neutral_rehearsal_file": True,
            "external_kis_api": False,
        },
        "scores": scores,
    }


def write_payload(payload: dict, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    batch_key = str(payload["batch_date"]).replace("-", "")
    path = output_dir / f"{batch_key}.json"
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=datetime.now(_KST).strftime("%Y%m%d"))
    parser.add_argument("--tickers", default=None)
    parser.add_argument("--output-dir", default="new/artifacts/dual_source")
    args = parser.parse_args(argv)

    tickers = (
        [pad_ticker(t.strip()) for t in str(args.tickers).split(",") if t.strip()]
        if args.tickers
        else _active_tickers()
    )
    if not tickers:
        raise RuntimeError("active tickers not found in universe_config.yaml")
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    payload = build_payload(str(args.date), tickers)
    path = write_payload(payload, output_dir)
    report = {
        "status": "PASS",
        "path": str(path),
        "path_relative": str(path.relative_to(ROOT)),
        "ticker_count": len(tickers),
        "source_stats": payload["source_stats"],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
