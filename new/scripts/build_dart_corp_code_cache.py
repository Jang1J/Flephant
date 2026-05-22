#!/usr/bin/env python
"""Build DART corp_code mapping cache for active KOSPI trade-universe tickers.

DART Open API의 corpCode.xml endpoint (zip)에서 전체 회사 코드를 1회 다운로드한 후
universe_config.yaml active ticker만 추출해 캐시 JSON으로 저장한다.
한 번 실행 후 캐시 파일을 build_news_dart_archive.py가 참조한다.

산출: artifacts/cache/dart_corp_code_kospi20.json
주의: 파일명은 과거 20종목 시절 이름을 유지하지만, 내용은 현재 active universe
전체를 반영해야 한다.
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.utils.auth import AuthManager  # noqa: E402
from src.utils.config_loader import load as config_load  # noqa: E402
from src.utils.ticker_utils import pad_ticker  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")
_DART_CORPCODE_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
_DEFAULT_CACHE_PATH = ROOT / "artifacts" / "cache" / "dart_corp_code_kospi20.json"


def _active_tickers() -> list[dict[str, Any]]:
    cfg = config_load("universe_config.yaml") or {}
    out: list[dict[str, Any]] = []
    for sector_name, sector in (cfg.get("sectors") or {}).items():
        for stock in (sector.get("stocks") or []):
            if not isinstance(stock, dict):
                continue
            if str(stock.get("status", "")).lower() != "active":
                continue
            out.append({
                "ticker": pad_ticker(str(stock.get("ticker", ""))),
                "name": str(stock.get("name", "")),
                "aliases": list(stock.get("aliases", []) or []),
                "sector": str(sector_name),
            })
    return out


def _download_corpcode_zip(api_key: str, *, timeout_sec: int = 30) -> bytes:
    resp = requests.get(
        _DART_CORPCODE_URL,
        params={"crtfc_key": api_key},
        timeout=timeout_sec,
    )
    resp.raise_for_status()
    return resp.content


def _parse_corpcode_zip(zip_bytes: bytes) -> dict[str, dict[str, str]]:
    """Parse zip stream into {stock_code(6-padded): {corp_code, corp_name, modify_date}}."""
    out: dict[str, dict[str, str]] = {}
    with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
        xml_name = next((n for n in zf.namelist() if n.endswith(".xml")), None)
        if not xml_name:
            raise RuntimeError("CORPCODE.xml not found inside DART zip")
        with zf.open(xml_name) as fh:
            tree = ET.parse(fh)
    root = tree.getroot()
    for node in root.findall("list"):
        stock_code = (node.findtext("stock_code") or "").strip()
        # DART는 비상장 회사도 포함. stock_code 비어있으면 skip (장외 / 비상장).
        if not stock_code or stock_code == " ":
            continue
        try:
            ticker = pad_ticker(stock_code)
        except Exception:
            continue
        out[ticker] = {
            "corp_code": (node.findtext("corp_code") or "").strip(),
            "corp_name": (node.findtext("corp_name") or "").strip(),
            "modify_date": (node.findtext("modify_date") or "").strip(),
        }
    return out


def build_cache(output_path: Path) -> dict[str, Any]:
    tickers = _active_tickers()
    if not tickers:
        raise RuntimeError("universe_config.yaml에서 active ticker를 찾지 못함")

    auth = AuthManager()
    try:
        api_key = auth.get_dart_key()
    except (EnvironmentError, KeyError) as e:
        raise RuntimeError(
            f"DART_API_KEY 미설정: {e}. .env source 후 재실행 필요."
        ) from e

    zip_bytes = _download_corpcode_zip(api_key)
    full_mapping = _parse_corpcode_zip(zip_bytes)

    mapping: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for meta in tickers:
        info = full_mapping.get(meta["ticker"])
        if info is None:
            missing.append(meta["ticker"])
            continue
        mapping[meta["ticker"]] = {
            **info,
            "name": meta["name"],
            "aliases": meta["aliases"],
            "sector": meta["sector"],
        }

    payload: dict[str, Any] = {
        "generated_at": datetime.now(_KST).isoformat(),
        "source_url": _DART_CORPCODE_URL,
        "total_active_tickers": len(tickers),
        "matched_count": len(mapping),
        "missing_tickers": missing,
        "mapping": mapping,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-path", default=str(_DEFAULT_CACHE_PATH))
    args = parser.parse_args(argv)

    payload = build_cache(Path(str(args.output_path)))
    summary = {
        "status": "PASS" if not payload["missing_tickers"] else "PARTIAL",
        "output_path": str(args.output_path),
        "matched": payload["matched_count"],
        "total": payload["total_active_tickers"],
        "missing": payload["missing_tickers"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not payload["missing_tickers"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
