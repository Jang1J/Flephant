#!/usr/bin/env python
"""Naver DataLab attention smoke.

This script does not read .env files. Real calls use process environment
variables through AuthManager only.

DataLab ratio is a retail-attention proxy, not sentiment text.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.utils.auth import AuthManager  # noqa: E402
from src.utils.rate_limiter import RateLimiter  # noqa: E402
from src.utils.safe_cast import safe_bool  # noqa: E402
from src.utils.ticker_utils import pad_ticker  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")
_REPORT_DIR = ROOT / "artifacts" / "reports" / "naver_datalab_attention"
_UNIVERSE_PATH = SRC / "config" / "universe_config.yaml"
_DATALAB_URL = "https://openapi.naver.com/v1/datalab/search"


class _FakeNaverAuth:
    def get_naver_client(self) -> tuple[str, str]:
        return ("internal_fake_client", "internal_fake_secret")


def _repo_relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _split_csv(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _load_ticker_names() -> dict[str, str]:
    try:
        with _UNIVERSE_PATH.open("r", encoding="utf-8") as fh:
            payload = yaml.safe_load(fh) or {}
    except Exception:
        return {}
    names: dict[str, str] = {}
    for sector in (payload.get("sectors") or {}).values():
        for stock in sector.get("stocks", []) or []:
            ticker = pad_ticker(str(stock.get("ticker", "")))
            name = str(stock.get("name", "")).strip()
            if ticker and name:
                names[ticker] = name
    return names


def _keyword_groups(tickers: list[str], ticker_names: dict[str, str]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for ticker in tickers:
        name = ticker_names.get(ticker, ticker)
        keywords = list(dict.fromkeys([name, ticker, f"{name} 주식", f"{name} 실적"]))
        groups.append({"groupName": ticker, "keywords": keywords[:20]})
    return groups


def _fake_datalab_response(keyword_groups: list[dict[str, Any]], start_date: str, end_date: str) -> dict[str, Any]:
    return {
        "startDate": start_date,
        "endDate": end_date,
        "timeUnit": "date",
        "results": [
            {
                "title": group["groupName"],
                "keywords": group["keywords"],
                "data": [
                    {"period": start_date, "ratio": 35.0 + idx},
                    {"period": end_date, "ratio": 60.0 + idx},
                ],
            }
            for idx, group in enumerate(keyword_groups)
        ],
    }


def _post_datalab(
    *,
    auth: Any,
    rate_limiter: RateLimiter,
    keyword_groups: list[dict[str, Any]],
    start_date: str,
    end_date: str,
    time_unit: str,
    internal_fake_naver: bool,
) -> dict[str, Any]:
    if internal_fake_naver:
        return _fake_datalab_response(keyword_groups, start_date, end_date)
    client_id, client_secret = auth.get_naver_client()
    rate_limiter.wait_and_acquire()
    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
        "Content-Type": "application/json",
    }
    body = {
        "startDate": start_date,
        "endDate": end_date,
        "timeUnit": time_unit,
        "keywordGroups": keyword_groups,
    }
    try:
        import requests  # noqa: PLC0415

        resp = requests.post(_DATALAB_URL, headers=headers, json=body, timeout=10)
        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            preview = resp.text[:300].replace("\n", " ")
            raise ConnectionError(
                f"HTTP_{resp.status_code}:{preview}"
            ) from e
        return resp.json()
    except ImportError:
        import urllib.error  # noqa: PLC0415
        import urllib.request  # noqa: PLC0415

        req = urllib.request.Request(
            _DATALAB_URL,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise ConnectionError(f"Naver DataLab urllib 연결 실패: {e}") from e


def _flatten_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in payload.get("results", []) or []:
        ticker = pad_ticker(str(result.get("title", "")))
        for item in result.get("data", []) or []:
            rows.append({
                "ticker": ticker,
                "period": item.get("period"),
                "ratio": float(item.get("ratio", 0.0) or 0.0),
            })
    return rows


def run_naver_datalab_attention_smoke(
    *,
    tickers: list[str],
    start_date: str,
    end_date: str,
    time_unit: str,
    internal_fake_naver: bool,
    output_dir: Path,
    write_report: bool = True,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    normalized_tickers = [pad_ticker(ticker) for ticker in tickers]
    names = _load_ticker_names()
    groups = _keyword_groups(normalized_tickers, names)
    max_groups = 5
    chunks = [groups[idx:idx + max_groups] for idx in range(0, len(groups), max_groups)]
    auth = _FakeNaverAuth() if internal_fake_naver else AuthManager()
    rate_limiter = RateLimiter("naver")
    responses: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    blockers: list[str] = []

    for chunk in chunks:
        try:
            payload = _post_datalab(
                auth=auth,
                rate_limiter=rate_limiter,
                keyword_groups=chunk,
                start_date=start_date,
                end_date=end_date,
                time_unit=time_unit,
                internal_fake_naver=internal_fake_naver,
            )
        except Exception as e:
            error_text = str(e)
            blockers.append(f"datalab_request_failed:{error_text[:160]}")
            responses.append({
                "error_type": type(e).__name__,
                "error": error_text[:500],
                "keywordGroups": chunk,
            })
            continue
        responses.append(payload)
        rows.extend(_flatten_rows(payload))

    if not rows:
        blockers.append("attention_ratio_rows_zero")

    latest_ratio_by_ticker: dict[str, float] = {}
    for row in rows:
        latest_ratio_by_ticker[row["ticker"]] = row["ratio"]

    report = {
        "status": "PASS" if not blockers else "BLOCKED",
        "action": "naver_datalab_attention_smoke",
        "generated_at": datetime.now(_KST).isoformat(),
        "evidence_level": "attention_proxy_smoke",
        "deploy_quality": False,
        "is_mock": internal_fake_naver,
        "internal_fake_naver": internal_fake_naver,
        "provider": "naver_datalab_search_trend",
        "ratio_is_relative": True,
        "params": {
            "tickers": normalized_tickers,
            "start_date": start_date,
            "end_date": end_date,
            "time_unit": time_unit,
            "keyword_group_limit_per_request": max_groups,
        },
        "metrics": {
            "ticker_count": len(normalized_tickers),
            "request_count": len(chunks),
            "attention_ratio_rows": len(rows),
            "latest_ratio_by_ticker": latest_ratio_by_ticker,
        },
        "keyword_groups": groups,
        "rows": rows,
        "responses": responses,
        "blockers": blockers,
        "caveats": [
            "DataLab ratio is relative to the request window maximum, not absolute search volume.",
            "This is attention proxy evidence, not community sentiment text.",
        ],
    }
    return _write_report(report, output_dir, write_report)


def _write_report(report: dict[str, Any], output_dir: Path, write_report: bool) -> dict[str, Any]:
    if not write_report:
        return report
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"naver_datalab_attention_smoke_{ts}.json"
    report["report_path"] = str(path)
    report["report_path_relative"] = _repo_relative(path)
    with path.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--time-unit", choices=["date", "week", "month"], default="date")
    parser.add_argument("--internal-fake-naver", action="store_true")
    parser.add_argument("--no-write-report", action="store_true")
    parser.add_argument("--output-dir", default=str(_REPORT_DIR))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_naver_datalab_attention_smoke(
        tickers=_split_csv(args.tickers),
        start_date=args.start_date,
        end_date=args.end_date,
        time_unit=args.time_unit,
        internal_fake_naver=safe_bool(args.internal_fake_naver),
        output_dir=Path(args.output_dir),
        write_report=not safe_bool(args.no_write_report),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
