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
from collections import defaultdict
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.connectors.community import CommunityCrawler, CommunityPost
from src.connectors.naver_rest import NaverNewsClient, NaverNewsItem
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


def _load_active_universe() -> list[dict[str, Any]]:
    """universe_config.yaml 에서 active 종목 메타데이터 로드.

    과거 구조(cfg["active"])와 현행 sectors 구조를 모두 지원한다.
    """
    cfg = config_load("universe_config.yaml")
    universe: list[dict[str, Any]] = []

    for item in cfg.get("active", []) or []:
        ticker = str(item.get("ticker", "")).zfill(6)
        if ticker and ticker != "000000":
            universe.append({
                "ticker": ticker,
                "name": item.get("name") or ticker,
                "aliases": list(item.get("aliases", []) or []),
            })

    if universe:
        return universe

    sectors = cfg.get("sectors", {}) or {}
    for sector in sectors.values():
        if not isinstance(sector, dict) or sector.get("status") != "confirmed":
            continue
        for stock in sector.get("stocks", []) or []:
            if not isinstance(stock, dict) or stock.get("status") != "active":
                continue
            ticker = str(stock.get("ticker", "")).zfill(6)
            if ticker and ticker != "000000":
                universe.append({
                    "ticker": ticker,
                    "name": stock.get("name") or ticker,
                    "aliases": list(stock.get("aliases", []) or []),
                })

    return universe


def _load_active_tickers() -> list[str]:
    """universe_config.yaml 에서 active 20종목 코드 로드."""
    return [item["ticker"] for item in _load_active_universe()]


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


def _as_kst(dt: datetime) -> datetime:
    """datetime을 KST tz-aware로 정규화."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_KST)
    return dt.astimezone(_KST)


def _load_news_window_hours() -> int:
    """dual_source.yaml news.score_window_hours 로드."""
    cfg = config_load("dual_source.yaml") or {}
    return int((cfg.get("news", {}) or {}).get("score_window_hours", 12))


def _news_text(item: NaverNewsItem) -> str:
    """NaverNewsItem을 scorer 입력 텍스트로 변환."""
    return f"{item.title} {item.description}".strip()


def _post_text(post: CommunityPost) -> str:
    """CommunityPost를 scorer 입력 텍스트로 변환."""
    return f"{post.title} {post.content}".strip()


def _collect_real_inputs(
    universe_meta: list[dict[str, Any]],
    snapshot_ts: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Naver/Community 실 커넥터로 Dual-Source 입력을 구성한다.

    Community는 공식 API가 없어 현재 실 스크래핑이 꺼져 있을 수 있다. 그 경우
    mock 데이터를 섞지 않고 빈 커뮤니티 입력으로 둔다. 이렇게 해야 실데이터 학습
    metadata가 mock 커뮤니티에 오염되지 않는다.
    """
    news_client = NaverNewsClient()
    community = CommunityCrawler()
    window_hours = _load_news_window_hours()
    news_window_start = snapshot_ts - timedelta(hours=window_hours)
    tickers = [item["ticker"] for item in universe_meta]

    posts_by_ticker: dict[str, list[CommunityPost]] = defaultdict(list)
    community_mode = "real"
    community_error = ""
    if getattr(community, "_is_mock", False):
        community_mode = "unavailable_empty"
        community_error = "COMMUNITY_SCRAPE_ENABLED is not enabled; mock community data was not used"
    else:
        try:
            posts = community.poll(tickers, window_minutes=5)
            for post in posts:
                post_ts = _as_kst(post.timestamp)
                if post_ts <= snapshot_ts:
                    posts_by_ticker[str(post.ticker).zfill(6)].append(post)
        except NotImplementedError as e:
            community_mode = "unavailable_empty"
            community_error = str(e)
        except Exception as e:
            community_mode = "error_empty"
            community_error = str(e)

    rows: list[dict[str, Any]] = []
    news_mode = "unavailable_empty" if getattr(news_client, "_is_mock", False) else "real"
    news_error = (
        "NAVER_CLIENT_ID/SECRET not available; mock news data was not used"
        if news_mode == "unavailable_empty" else ""
    )

    source_stats: dict[str, Any] = {
        "news_mode": news_mode,
        "news_error": news_error,
        "community_mode": community_mode,
        "community_error": community_error,
        "news_window_hours": window_hours,
        "per_ticker": {},
    }

    for item in universe_meta:
        ticker = item["ticker"]
        name = str(item.get("name") or ticker)
        query = name

        news_texts: list[str] = []
        news_timestamps: list[datetime] = []
        if news_mode == "real":
            try:
                news_items = news_client.search_news(query)
                for news_item in news_items:
                    pub_dt = _as_kst(news_item.pub_date)
                    if not (news_window_start <= pub_dt <= snapshot_ts):
                        continue
                    text = _news_text(news_item)
                    if text:
                        news_texts.append(text)
                        news_timestamps.append(pub_dt)
            except Exception as e:
                source_stats["per_ticker"].setdefault(ticker, {})["news_error"] = str(e)

        ticker_posts = posts_by_ticker.get(ticker, [])
        comm_texts = [_post_text(post) for post in ticker_posts if _post_text(post)]
        comm_timestamps = [_as_kst(post.timestamp) for post in ticker_posts]

        data_ts_candidates = news_timestamps + comm_timestamps
        data_ts = max(data_ts_candidates) if data_ts_candidates else snapshot_ts
        assert_pit_safe(data_ts, snapshot_ts)

        rows.append({
            "ticker": ticker,
            "news_texts": news_texts,
            "comm_texts_t1": comm_texts,
            "comm_texts_t2": [],
            "current_volume": float(len(comm_texts)),
            "historical_volumes": [],
            "data_ts": data_ts.isoformat(),
        })
        source_stats["per_ticker"].setdefault(ticker, {}).update({
            "query": query,
            "news_count": len(news_texts),
            "community_count": len(comm_texts),
        })

    return rows, source_stats


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
    snap_dt = _as_kst(snap_dt)
    batch_date = snap_dt.date()

    logger.info(
        "[dual_source] 배치 시작: batch_date=%s snapshot_ts=%s use_mock=%s",
        batch_date.isoformat(),
        snap_dt.isoformat(),
        use_mock,
    )

    # active 20종목 로드
    universe_meta = _load_active_universe()
    tickers = [item["ticker"] for item in universe_meta]
    if not tickers:
        logger.warning("[dual_source] active 종목 없음. universe_config.yaml 확인 필요.")
        return []

    logger.info("[dual_source] active 종목 %d개 처리 시작", len(tickers))

    # 종목별 입력 데이터 구성
    source_stats: dict[str, Any]
    if use_mock:
        universe: list[dict] = []
        for ticker in tickers:
            item = _build_mock_inputs(ticker, snap_dt)
            universe.append(item)
        source_stats = {"input_mode": "mock"}
    else:
        universe, source_stats = _collect_real_inputs(universe_meta, snap_dt)
        source_stats["input_mode"] = "real"

    # DualSourceScorer 5피처 생성
    scorer = DualSourceScorer()
    results = scorer.score_universe(universe=universe, snapshot_ts=snap_dt)

    # 결과 저장 (new/artifacts/dual_source/YYYYMMDD.json)
    _ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _ARTIFACT_DIR / f"{batch_date.strftime('%Y%m%d')}.json"

    payload = {
        "batch_date": batch_date.isoformat(),
        "snapshot_ts": snap_dt.isoformat(),
        "generated_at": now_kst.isoformat(),
        "ticker_count": len(results),
        "source_stats": source_stats,
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
