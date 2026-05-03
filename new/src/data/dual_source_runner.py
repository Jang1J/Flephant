"""08:00~08:30 KST Dual-Source 5피처 배치 실행기.

Sprint 4 S4-1. Mode B 사이클(18:00~22:00)과 완전 분리된 장전 배치 전용.

## 실행 흐름

    1. 08:00~08:30 KST 시간대 검증 (범위 이탈 시 경고 후 계속)
    2. universe_config.yaml 에서 active 20종목 로드
    3. 종목별 뉴스 텍스트 + 커뮤니티 텍스트 수집 (mock 또는 커넥터)
    4. DualSourceScorer.score_universe() 호출
    5. 결과 JSON 저장 (new/artifacts/dual_source/YYYYMMDD.json)
    6. PIT-Safety: snapshot_ts = 오늘 08:30 KST (장전 배치 기준)

## PIT-Safety

    배치 기준 snapshot_ts = 오늘 08:30 KST.
    이 시각 이후 데이터는 접근 금지 (assert_pit_safe 강제).

## 하드코딩 금지

    - universe: universe_config.yaml 로드
    - 시간 창: dual_source.yaml score_build_window (또는 risk_config.yaml 기준)
    - 파라미터: DualSourceScorer 내부에서 dual_source.yaml 로드

SSOT: new/specs/api_contracts.md C3A DualSourceScoreContract
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from src.data.dual_source_scorer import DualSourceScorer
from src.utils.config_loader import load as config_load
from src.utils.pit_guard import PITViolationError, assert_pit_safe

logger = logging.getLogger(__name__)

_KST = ZoneInfo("Asia/Seoul")

# 결과 저장 경로: new/artifacts/dual_source/
_ARTIFACT_DIR = Path(__file__).resolve().parents[2] / "artifacts" / "dual_source"

def _load_batch_window() -> tuple[int, int, int, int]:
    """risk_config.yaml dual_source.score_build_window 에서 배치 창 로드.

    Returns:
        (start_hour, start_minute, end_hour, end_minute)

    기본값: 08:00~08:30 (risk_config.yaml에 섹션 없을 때 fallback).
    """
    cfg = config_load("risk_config.yaml", "dual_source")
    win = cfg.get("score_build_window", {})
    start_str: str = win.get("start", "08:00")
    end_str: str = win.get("end", "08:30")
    sh, sm = map(int, start_str.split(":"))
    eh, em = map(int, end_str.split(":"))
    return sh, sm, eh, em


def _is_in_batch_window(now: datetime) -> bool:
    """현재 시각이 배치 창 내부인지 확인 (risk_config.yaml 동적 로드)."""
    sh, sm, eh, em = _load_batch_window()
    start = time(sh, sm)
    end = time(eh, em)
    return start <= now.timetz().replace(tzinfo=None) <= end


def _load_active_tickers() -> list[str]:
    """universe_config.yaml 에서 active 20종목 코드 로드.

    반환값: 6자리 zero-padded 종목코드 리스트.
    """
    cfg = config_load("universe_config.yaml")
    active = cfg.get("active", [])
    return [str(item.get("ticker", "")).zfill(6) for item in active if item.get("ticker")]


def _build_mock_inputs(ticker: str, today: datetime) -> dict:
    """테스트/mock 환경용 더미 입력 데이터 생성.

    실 환경(Sprint 4 S4-6 이후)에서는 NaverNewsClient + CommunityCrawler 로 교체.
    """
    return {
        "ticker": ticker,
        "news_texts": [
            f"{ticker} 주가 상승 기대감 지속",
            f"{ticker} 실적 호조 예상",
        ],
        "comm_texts_t1": [
            f"{ticker} 떡상 각인가",
            f"{ticker} 매수 타이밍 체크",
        ],
        "comm_texts_t2": [
            f"{ticker} 외국인 매수 급증",
        ],
        "current_volume": 120.0,
        "historical_volumes": [80.0, 90.0, 100.0, 85.0, 95.0, 110.0, 88.0],
        "data_ts": today.replace(hour=7, minute=50).isoformat(),  # 07:50 KST (배치 전 수집)
    }


def run_dual_source_batch(
    snapshot_ts: str | datetime | None = None,
    use_mock: bool = True,
) -> list[dict]:
    """C3A DualSourceScoreContract 배치 실행.

    Args:
        snapshot_ts: PIT-Safety 기준 시각. None 이면 오늘 08:30 KST 사용.
        use_mock   : True 이면 mock 데이터 사용. False 이면 실 커넥터 (Sprint 4 S4-6+).

    Returns:
        list[dict]: C3A 출력 스키마 리스트 (종목별 5피처).

    Raises:
        PITViolationError: data_ts > snapshot_ts 위반 시.
    """
    now_kst = datetime.now(_KST)
    today = now_kst.date()

    # 배치 창 검증 (이탈 시 경고, 강제 중단 없음 — 수동 재실행 허용)
    if not _is_in_batch_window(now_kst):
        logger.warning(
            "[dual_source] 배치 창 이탈 (현재: %s KST). 권장 창: 08:00~08:30 KST. 계속 실행.",
            now_kst.strftime("%H:%M"),
        )

    # snapshot_ts: 오늘 08:30 KST (장전 배치 기준)
    if snapshot_ts is None:
        snap_dt = datetime.combine(today, time(8, 30, 0), tzinfo=_KST)
    else:
        if isinstance(snapshot_ts, str):
            from datetime import datetime as _dt  # noqa: PLC0415
            snap_dt = _dt.fromisoformat(snapshot_ts)
        else:
            snap_dt = snapshot_ts

    logger.info(
        "[dual_source] 배치 시작: today=%s snapshot_ts=%s use_mock=%s",
        today.isoformat(),
        snap_dt.isoformat(),
        use_mock,
    )

    # active 20종목 로드
    tickers = _load_active_tickers()
    if not tickers:
        logger.warning("[dual_source] active 종목 없음. universe_config.yaml 확인 필요.")
        return []

    logger.info("[dual_source] active 종목 %d개 처리 시작", len(tickers))

    # 종목별 입력 데이터 구성
    universe: list[dict] = []
    for ticker in tickers:
        if use_mock:
            item = _build_mock_inputs(ticker, now_kst)
        else:
            # 실 커넥터 경로 (Sprint 4 S4-6 이후 구현)
            # from src.connectors.naver_rest import NaverNewsClient
            # from src.connectors.community import CommunityCrawler
            raise NotImplementedError(
                "실 커넥터 경로는 Sprint 4 S4-6 이후 구현. use_mock=True 사용 권장."
            )
        universe.append(item)

    # DualSourceScorer 5피처 생성
    scorer = DualSourceScorer()
    results = scorer.score_universe(universe=universe, snapshot_ts=snap_dt)

    # 결과 저장 (new/artifacts/dual_source/YYYYMMDD.json)
    _ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _ARTIFACT_DIR / f"{today.strftime('%Y%m%d')}.json"

    payload = {
        "batch_date": today.isoformat(),
        "snapshot_ts": snap_dt.isoformat(),
        "generated_at": now_kst.isoformat(),
        "ticker_count": len(results),
        "scores": results,
    }
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    logger.info(
        "[dual_source] 배치 완료: %d종목 → %s",
        len(results),
        out_path,
    )
    return results


def load_latest_scores(date_str: str | None = None) -> list[dict]:
    """저장된 C3A 점수 로드.

    Args:
        date_str: 'YYYYMMDD' 형식. None 이면 오늘 날짜.

    Returns:
        list[dict]: C3A 출력 스키마 리스트.
    """
    if date_str is None:
        date_str = datetime.now(_KST).strftime("%Y%m%d")

    path = _ARTIFACT_DIR / f"{date_str}.json"
    if not path.exists():
        logger.warning("[dual_source] 점수 파일 없음: %s", path)
        return []

    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)

    return payload.get("scores", [])


if __name__ == "__main__":
    # 직접 실행: python -m src.data.dual_source_runner
    logging.basicConfig(level=logging.INFO)
    scores = run_dual_source_batch(use_mock=True)
    print(f"[dual_source] 완료: {len(scores)}종목 처리")
