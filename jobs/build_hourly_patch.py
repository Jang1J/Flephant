"""
HourlyMarketPatch 빌더
- DailyMarketPacket(backbone) 위에 장중 1시간 delta를 생성
- 장중 가격 변화, 신규 뉴스/공시, market stress 업데이트

Usage:
    python jobs/build_hourly_patch.py 20260322 1030
    python jobs/build_hourly_patch.py 20260322 1430
"""

import sys
import json
from datetime import datetime
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE_DIR))

from connectors import now_kst, make_snapshot_dt

HMP_DIR = _BASE_DIR / "artifacts" / "hourly_market_patch"
HMP_DIR.mkdir(parents=True, exist_ok=True)
DMP_DIR = _BASE_DIR / "artifacts" / "daily_market_packet"


def build_hourly_patch(target_date: str, hour_str: str, use_mock: bool = True) -> dict:
    """
    HourlyMarketPatch 생성

    Args:
        target_date: YYYYMMDD
        hour_str: HHMM (e.g., "1030", "1430")
        use_mock: True면 mock 데이터, False면 실시간 API 호출

    Returns:
        HourlyMarketPatch dict
    """
    snapshot_dt = make_snapshot_dt(target_date, int(hour_str[:2]))

    # base DMP 확인
    dmp_path = DMP_DIR / f"DMP-{target_date}.json"
    if not dmp_path.exists():
        raise FileNotFoundError(f"Base DMP not found: {dmp_path}")

    with open(dmp_path) as f:
        dmp = json.load(f)

    base_dmp_id = dmp.get("snapshot_id", f"DMP-{target_date}")

    if use_mock:
        patch = _build_mock_patch(target_date, hour_str, snapshot_dt, base_dmp_id, dmp)
    else:
        patch = _build_live_patch(target_date, hour_str, snapshot_dt, base_dmp_id, dmp)

    return patch


def _build_mock_patch(target_date: str, hour_str: str, snapshot_dt: str,
                      base_dmp_id: str, dmp: dict) -> dict:
    """Mock HourlyMarketPatch 생성 (테스트/데모용)"""
    import random
    random.seed(int(target_date + hour_str))

    tickers = dmp.get("tickers", [])
    market_data = dmp.get("market_data", {})

    # 무작위로 5~10개 종목에 가격 변화
    n_changed = random.randint(5, min(10, len(tickers)))
    changed_tickers = random.sample(tickers, n_changed)

    price_patch = {}
    for ticker in changed_tickers:
        md = market_data.get(ticker, {})
        ohlcv = md.get("ohlcv", {})
        base_close = ohlcv.get("close", 50000)
        chg_pct = round(random.uniform(-3.0, 3.0), 2)
        last = round(base_close * (1 + chg_pct / 100))
        volume_intraday = random.randint(10000, 500000)

        price_patch[ticker] = {
            "last": last,
            "chg_pct": chg_pct,
            "volume_intraday": volume_intraday,
        }

    # mock 뉴스 evidence
    new_evidence_ids = [
        f"NEWS-MOCK-{target_date}-{hour_str}-{i}"
        for i in range(random.randint(0, 3))
    ]

    # market stress mock
    macro = dmp.get("macro_snapshot", {})
    base_vix = macro.get("vix_proxy")
    base_breadth = macro.get("market_breadth")

    stress_update = None
    if base_vix is not None or base_breadth is not None:
        stress_update = {
            "vix_proxy": round(base_vix + random.uniform(-2, 2), 2) if base_vix else None,
            "market_breadth": round(base_breadth + random.uniform(-0.05, 0.05), 4) if base_breadth else None,
        }

    return {
        "patch_id": f"HMP-{target_date}-{hour_str}00",
        "snapshot_dt": snapshot_dt,
        "artifact_version": "v1.0",
        "base_dmp_id": base_dmp_id,
        "changed_tickers": changed_tickers,
        "price_patch": price_patch,
        "new_evidence_ids": new_evidence_ids,
        "market_stress_update": stress_update,
    }


def _build_live_patch(target_date: str, hour_str: str, snapshot_dt: str,
                      base_dmp_id: str, dmp: dict) -> dict:
    """Live HourlyMarketPatch 생성 (실시간 API 기반) — Phase 2"""
    # Phase 2: pykrx 실시간 / KRX Open API / Naver 실시간 시세 연동
    raise NotImplementedError("Live HourlyMarketPatch는 Phase 2에서 구현 예정")


def save_patch(patch: dict) -> Path:
    """HourlyMarketPatch 저장"""
    path = HMP_DIR / f"{patch['patch_id']}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(patch, f, ensure_ascii=False, indent=2)
    return path


if __name__ == "__main__":
    date = sys.argv[1] if len(sys.argv) > 1 else now_kst().strftime("%Y%m%d")
    hour = sys.argv[2] if len(sys.argv) > 2 else "1030"

    print(f"\n[HourlyMarketPatch] {date} {hour}")
    patch = build_hourly_patch(date, hour, use_mock=True)
    path = save_patch(patch)
    print(f"  → changed: {len(patch['changed_tickers'])}종목")
    print(f"  → new evidence: {len(patch['new_evidence_ids'])}건")
    print(f"  ✅ 저장: {path}")
