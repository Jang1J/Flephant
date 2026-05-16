"""PIT-safe daily exogenous feature artifacts.

The training/replay path needs the same C3 feature columns that QuantAgent
expects at inference time.  This store is intentionally file-based so Mode B can
audit exactly which daily exogenous inputs were available without reading
secrets or calling live connectors during replay.
"""
from __future__ import annotations

import json
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.utils.ticker_utils import pad_ticker

_KST = ZoneInfo("Asia/Seoul")
_NEW_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EXOGENOUS_ARTIFACT_DIR = _NEW_ROOT / "artifacts" / "exogenous"


def _normalise_date(date_str: str) -> str:
    cleaned = str(date_str).replace("-", "")
    if len(cleaned) != 8 or not cleaned.isdigit():
        raise ValueError(f"date_str must be YYYYMMDD, got {date_str!r}")
    datetime.strptime(cleaned, "%Y%m%d")
    return cleaned


def _coerce_feature_map(
    values: dict[str, Any] | None,
    feature_cols: list[str],
    defaults: dict[str, float],
) -> dict[str, float]:
    values = values or {}
    return {
        col: float(values.get(col, defaults.get(col, 0.0)))
        for col in feature_cols
    }


def _coerce_sparse_feature_map(
    values: dict[str, Any] | None,
    feature_cols: list[str],
) -> dict[str, float]:
    values = values or {}
    return {
        col: float(values[col])
        for col in feature_cols
        if col in values
    }


def is_non_neutral(
    values: dict[str, float],
    defaults: dict[str, float],
    *,
    eps: float = 1e-12,
) -> bool:
    """Return true if any feature differs from its neutral default."""
    for key, value in values.items():
        if abs(float(value) - float(defaults.get(key, 0.0))) > eps:
            return True
    return False


def load_exogenous_scores(
    date_str: str,
    *,
    feature_cols: list[str],
    defaults: dict[str, float],
    artifact_dir: Path | None = None,
) -> tuple[dict[str, dict[str, float]], dict[str, Any]]:
    """Load one daily exogenous artifact.

    Supported payload shapes:
    - {"features": {...}} or {"global": {...}} for market-wide values.
    - {"per_ticker": {"005930": {...}}} for ticker overrides.
    - {"rows": [{"ticker": "005930", ...}]} for row-oriented exports.

    The returned map uses "*" for global features and zero-padded tickers for
    ticker-specific overrides.
    """
    date_key = _normalise_date(date_str)
    root = artifact_dir or DEFAULT_EXOGENOUS_ARTIFACT_DIR
    path = root / f"{date_key}.json"
    if not path.exists():
        return {}, {
            "status": "missing",
            "date": date_key,
            "path": str(path),
            "record_count": 0,
            "non_neutral_record_count": 0,
        }

    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError(f"exogenous payload must be object: {path}")

    scores: dict[str, dict[str, float]] = {}
    global_values = payload.get("features") or payload.get("global")
    if isinstance(global_values, dict):
        scores["*"] = _coerce_feature_map(global_values, feature_cols, defaults)

    per_ticker = payload.get("per_ticker") or {}
    if isinstance(per_ticker, dict):
        for ticker, values in per_ticker.items():
            if isinstance(values, dict):
                scores[pad_ticker(str(ticker))] = _coerce_sparse_feature_map(
                    values,
                    feature_cols,
                )

    rows = payload.get("rows") or []
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            ticker = row.get("ticker")
            if ticker is None:
                scores["*"] = _coerce_feature_map(row, feature_cols, defaults)
            else:
                scores[pad_ticker(str(ticker))] = _coerce_sparse_feature_map(
                    row,
                    feature_cols,
                )

    non_neutral_count = sum(
        1 for values in scores.values() if is_non_neutral(values, defaults)
    )
    return scores, {
        "status": "found",
        "date": date_key,
        "path": str(path),
        "record_count": len(scores),
        "non_neutral_record_count": non_neutral_count,
        "batch_date": payload.get("batch_date"),
        "snapshot_ts": payload.get("snapshot_ts"),
        "generated_at": payload.get("generated_at"),
        "source_stats": payload.get("source_stats", {}),
    }


def build_neutral_payload(
    date_str: str,
    *,
    tickers: list[str],
    feature_cols: list[str],
    defaults: dict[str, float],
) -> dict[str, Any]:
    """Build a clearly marked neutral placeholder payload for rehearsal only."""
    date_key = _normalise_date(date_str)
    batch_date = datetime.strptime(date_key, "%Y%m%d").date()
    snapshot_ts = datetime.combine(batch_date, time(8, 30), tzinfo=_KST)
    rows = []
    for ticker in sorted({pad_ticker(t) for t in tickers}):
        rows.append({
            "ticker": ticker,
            **{col: float(defaults.get(col, 0.0)) for col in feature_cols},
        })
    return {
        "batch_date": batch_date.isoformat(),
        "snapshot_ts": snapshot_ts.isoformat(),
        "generated_at": datetime.now(_KST).isoformat(),
        "ticker_count": len(rows),
        "source_stats": {
            "input_mode": "real",
            "provider_mode": "unavailable_empty",
            "neutral_rehearsal_file": True,
            "external_kis_api": False,
        },
        "rows": rows,
    }


def write_exogenous_payload(payload: dict[str, Any], artifact_dir: Path | None = None) -> Path:
    """Persist a daily exogenous payload with canonical JSON formatting."""
    root = artifact_dir or DEFAULT_EXOGENOUS_ARTIFACT_DIR
    root.mkdir(parents=True, exist_ok=True)
    batch_date = str(payload["batch_date"]).replace("-", "")
    path = root / f"{batch_date}.json"
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path
