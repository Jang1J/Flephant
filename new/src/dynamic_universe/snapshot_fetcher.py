"""C16 WatchUniverseSnapshotContract 구현. KOSPI200 watch universe 60초 snapshot.

Sprint 5 S5-1.

## 동작 흐름
    1. watch_universe_kospi200.yaml 로드 → tickers 200개
    2. exclude_trade_universe=true 이면 universe_config.yaml active 종목 제외 → 실효 watch list (~181)
    3. dynamic_universe_config.yaml 에서 snapshot_cache.ttl_sec 로드
    4. fetch_once() 호출:
       - PIT-Safety 가드 (ELEPHANT_TEST_PIT_SKIP 환경변수 분기)
       - 종목별 PersistentCache hit 우선 (TTL=ttl_sec)
       - miss 시 KIS get_price_snapshot 호출
       - C16 output dict 반환: {watch_snapshot_id, ts, snapshots}
       - artifacts/watch_snapshots/YYYYMMDD.jsonl append

## C16 forbidden_permissions 코드 레벨 가드
    - direct_trade_execution: submit_order 호출 금지 (코드 자체에 호출 없음)
    - trade_universe_mutation: universe_config.yaml write 금지 (read 전용)
    - lightgbm_inference_for_watch_universe: lightgbm import 금지 (assert)

## 에러 처리
    - SNAPSHOT_MISSING: KIS 응답 None 또는 빈 dict → 로그 + ticker skip
    - RATE_LIMIT_EXCEEDED: KIS 429 응답 → logger.warning + 다음 cycle 대기
    - TICKER_NOT_IN_WATCH_UNIVERSE: 요청 ticker 가 watch list 에 없음 → 로그 + skip
    - STALE_SNAPSHOT: ts > now + interval_sec*2 → DROP

SSOT: new/specs/api_contracts.md C16 WatchUniverseSnapshotContract
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import yaml

from src.utils.logger import get_logger
from src.utils.pit_guard import PITViolationError, is_pit_safe

if TYPE_CHECKING:
    from src.cache.persistent_cache import PersistentCache
    from src.connectors.kis_rest import KISRestClient

logger = get_logger("snapshot_fetcher")

_KST = ZoneInfo("Asia/Seoul")

# C16 forbidden_permissions 가드: lightgbm import 절대 금지
_LIGHTGBM_FORBIDDEN = "lightgbm_inference_for_watch_universe"
assert "lightgbm" not in dir(), (
    f"[snapshot_fetcher] C16 forbidden_permissions 위반: {_LIGHTGBM_FORBIDDEN}. "
    "snapshot_fetcher 에서 LightGBM 추론 금지."
)

# 기본 경로 (new/artifacts/watch_snapshots/)
_DEFAULT_SNAPSHOT_DIR = (
    Path(__file__).resolve().parents[3] / "artifacts" / "watch_snapshots"
)

# universe_config.yaml 경로 (read-only, write 금지 — C16 trade_universe_mutation)
_UNIVERSE_CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "universe_config.yaml"
)

# watch_universe_kospi200.yaml 경로
_WATCH_UNIVERSE_PATH_DEFAULT = (
    Path(__file__).resolve().parents[3] / "config" / "watch_universe_kospi200.yaml"
)

# dynamic_universe_config.yaml 경로
_DYNAMIC_CONFIG_PATH_DEFAULT = (
    Path(__file__).resolve().parents[3] / "config" / "dynamic_universe_config.yaml"
)


def _load_yaml(path: Path) -> dict:
    """yaml 파일 로드. 실패 시 빈 dict 반환."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception as e:
        logger.error("[snapshot_fetcher] yaml 로드 실패: path=%s error=%s", path, e)
        return {}


class WatchSnapshotFetcher:
    """C16 WatchUniverseSnapshotContract 구현체.

    watch_universe_kospi200.yaml + universe_config.yaml 기준으로
    실효 watch list 를 결정하고 60초 주기로 현재가 스냅샷을 수집한다.

    C16 forbidden_permissions 준수:
      - direct_trade_execution: submit_order 호출 없음
      - trade_universe_mutation: universe_config.yaml read 전용
      - lightgbm_inference_for_watch_universe: lightgbm import 없음
    """

    def __init__(
        self,
        kis_client: "KISRestClient",
        watch_universe_path: Path | None = None,
        dynamic_config_path: Path | None = None,
        cache: "PersistentCache | None" = None,
        snapshot_dir: Path | None = None,
    ) -> None:
        """WatchSnapshotFetcher 초기화.

        Args:
            kis_client: KIS REST 클라이언트 (mock/virtual/real 모드).
            watch_universe_path: watch_universe_kospi200.yaml 경로. None이면 기본값.
            dynamic_config_path: dynamic_universe_config.yaml 경로. None이면 기본값.
            cache: PersistentCache 인스턴스. None이면 캐시 미사용.
            snapshot_dir: 스냅샷 jsonl 저장 디렉토리. None이면 artifacts/watch_snapshots/.
        """
        self._kis = kis_client
        self._watch_path = watch_universe_path or _WATCH_UNIVERSE_PATH_DEFAULT
        self._dynamic_cfg_path = dynamic_config_path or _DYNAMIC_CONFIG_PATH_DEFAULT
        self._cache = cache
        self._snapshot_dir = snapshot_dir or _DEFAULT_SNAPSHOT_DIR

        # dynamic_universe_config.yaml 로드 → snapshot_cache.ttl_sec (하드코딩 금지)
        dynamic_cfg = _load_yaml(self._dynamic_cfg_path)
        snap_cache_cfg = dynamic_cfg.get("snapshot_cache", {})
        self._ttl_sec: int = int(snap_cache_cfg.get("ttl_sec", 55))

        # exit.ttl_sec 은 종목 보유 TTL, 여기서는 snapshot_cache.ttl_sec 만 사용
        self._interval_sec: int = 60  # C16 polling 주기 (고정 60초 — 계약서 spec)

        # 실효 watch tickers 로드
        self._watch_tickers: list[str] = self._load_watch_tickers()

        logger.info(
            "[snapshot_fetcher] 초기화 완료: watch_tickers=%d ttl_sec=%d snapshot_dir=%s",
            len(self._watch_tickers),
            self._ttl_sec,
            self._snapshot_dir,
        )

    def _load_watch_tickers(self) -> list[str]:
        """watch_universe_kospi200.yaml tickers + universe_config.yaml active 제외.

        watch_rules.exclude_trade_universe=true 이면
        universe_config.yaml의 active 종목을 watch list 에서 제외한다.

        Returns:
            6자리 zero-padded 종목코드 리스트.
        """
        watch_cfg = _load_yaml(self._watch_path)
        raw_tickers: list[str] = [
            str(item.get("ticker", "")).zfill(6)
            for item in watch_cfg.get("tickers", [])
            if item.get("ticker")
        ]

        watch_rules = watch_cfg.get("watch_rules", {})
        exclude_trade = watch_rules.get("exclude_trade_universe", False)

        if not exclude_trade:
            logger.info(
                "[snapshot_fetcher] exclude_trade_universe=false. watch list 그대로 사용: %d종목",
                len(raw_tickers),
            )
            return raw_tickers

        # C16 trade_universe_mutation 금지: read 전용
        trade_tickers: set[str] = set()
        try:
            universe_cfg = _load_yaml(_UNIVERSE_CONFIG_PATH)
            for sector_data in universe_cfg.get("sectors", {}).values():
                for stock in sector_data.get("stocks", []):
                    t = stock.get("ticker")
                    if t:
                        trade_tickers.add(str(t).zfill(6))
        except Exception as e:
            logger.warning(
                "[snapshot_fetcher] universe_config.yaml 로드 실패 (read-only). 제외 없이 계속: %s",
                e,
            )

        result = [t for t in raw_tickers if t not in trade_tickers]
        logger.info(
            "[snapshot_fetcher] trade_universe 제외: watch=%d trade=%d 실효=%d",
            len(raw_tickers),
            len(trade_tickers),
            len(result),
        )
        return result

    def fetch_once(self, snapshot_id_prefix: str = "WS") -> dict:
        """C16 WatchUniverseSnapshotContract 출력 생성.

        1. PIT-Safety 가드 (ELEPHANT_TEST_PIT_SKIP env 분기)
        2. 종목별 cache hit 우선 → miss 시 KIS 호출
        3. STALE_SNAPSHOT, SNAPSHOT_MISSING, RATE_LIMIT_EXCEEDED 처리
        4. artifacts/watch_snapshots/YYYYMMDD.jsonl append
        5. C16 output dict 반환

        Returns:
            {
                watch_snapshot_id: str,  # WS-{yyyymmdd}-{uuid8}
                ts: str,                 # ISO8601 KST
                snapshots: list[dict]    # 종목별 현재가 스냅샷
            }

        Raises:
            PITViolationError: ELEPHANT_TEST_PIT_SKIP unset 상태에서 미래 ts 감지 시.
        """
        now_kst = datetime.now(_KST)
        ts_str = now_kst.isoformat()

        # PIT-Safety: ELEPHANT_TEST_PIT_SKIP=true 이면 우회
        pit_skip = os.getenv("ELEPHANT_TEST_PIT_SKIP", "").lower() in ("true", "1")
        if not pit_skip:
            if not is_pit_safe(ts_str):
                raise PITViolationError(
                    f"[snapshot_fetcher] PIT-Safety 위반: ts={ts_str}"
                )

        watch_snapshot_id = (
            f"{snapshot_id_prefix}-{now_kst.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"
        )

        snapshots: list[dict[str, Any]] = []
        kis_miss_tickers: list[str] = []

        for ticker in self._watch_tickers:
            cached = self._get_from_cache(ticker)
            if cached is not None:
                snapshots.append(cached)
            else:
                kis_miss_tickers.append(ticker)

        # miss 종목은 KIS bulk 호출
        if kis_miss_tickers:
            fetched = self._fetch_from_kis(kis_miss_tickers, now_kst)
            snapshots.extend(fetched)

        output = {
            "watch_snapshot_id": watch_snapshot_id,
            "ts": ts_str,
            "snapshots": snapshots,
        }

        # artifacts/watch_snapshots/YYYYMMDD.jsonl append
        self._write_jsonl(output, now_kst)

        logger.info(
            "[snapshot_fetcher] fetch_once 완료: id=%s ts=%s snapshots=%d",
            watch_snapshot_id,
            ts_str,
            len(snapshots),
        )
        return output

    def _get_from_cache(self, ticker: str) -> dict | None:
        """캐시 조회. miss 또는 cache=None 이면 None 반환."""
        if self._cache is None:
            return None
        key = f"watch_snapshot:{ticker}"
        return self._cache.get(key)

    def _set_to_cache(self, ticker: str, value: dict) -> None:
        """캐시 저장. cache=None 이면 무시."""
        if self._cache is None:
            return
        key = f"watch_snapshot:{ticker}"
        self._cache.set(key, value, ttl_seconds=self._ttl_sec)

    def _fetch_from_kis(
        self, tickers: list[str], now_kst: datetime
    ) -> list[dict[str, Any]]:
        """KIS get_price_snapshot 호출 + 에러 처리.

        에러 종류:
          - SNAPSHOT_MISSING: 응답 None 또는 빈 list → skip
          - RATE_LIMIT_EXCEEDED: KIS 429 → warning + 빈 list 반환
          - TICKER_NOT_IN_WATCH_UNIVERSE: 응답 ticker 가 watch list 에 없음 → skip
          - STALE_SNAPSHOT: ts > now + interval_sec*2 → DROP
        """
        results: list[dict[str, Any]] = []
        watch_set = set(self._watch_tickers)

        try:
            raw_list = self._kis.get_price_snapshot(tickers)
        except Exception as e:
            err_str = str(e).lower()
            if "429" in err_str or "rate" in err_str:
                logger.warning(
                    "[snapshot_fetcher] RATE_LIMIT_EXCEEDED: KIS 429 응답. 다음 cycle 대기. err=%s",
                    e,
                )
                return []
            logger.error("[snapshot_fetcher] KIS get_price_snapshot 실패: %s", e)
            return []

        # SNAPSHOT_MISSING: None 또는 빈 응답
        if not raw_list:
            logger.warning(
                "[snapshot_fetcher] SNAPSHOT_MISSING: KIS 응답 빈 list. tickers=%d skip.",
                len(tickers),
            )
            return []

        stale_threshold_sec = self._interval_sec * 2

        for item in raw_list:
            if not item:
                logger.warning("[snapshot_fetcher] SNAPSHOT_MISSING: 빈 dict. skip.")
                continue

            ticker = item.get("ticker", "")

            # TICKER_NOT_IN_WATCH_UNIVERSE
            if ticker not in watch_set:
                logger.warning(
                    "[snapshot_fetcher] TICKER_NOT_IN_WATCH_UNIVERSE: ticker=%s watch list 미포함. skip.",
                    ticker,
                )
                continue

            # STALE_SNAPSHOT: ts > now + interval_sec*2
            item_ts_str = item.get("ts", "")
            if item_ts_str:
                try:
                    item_dt = datetime.fromisoformat(item_ts_str)
                    if item_dt.tzinfo is None:
                        item_dt = item_dt.replace(tzinfo=_KST)
                    delta_sec = (item_dt - now_kst).total_seconds()
                    if delta_sec > stale_threshold_sec:
                        logger.warning(
                            "[snapshot_fetcher] STALE_SNAPSHOT: ticker=%s ts=%s delta=%.1fs > %ds. DROP.",
                            ticker,
                            item_ts_str,
                            delta_sec,
                            stale_threshold_sec,
                        )
                        continue
                except Exception as e:
                    logger.debug(
                        "[snapshot_fetcher] ts 파싱 실패 (skip stale check): ticker=%s err=%s",
                        ticker,
                        e,
                    )

            results.append(item)
            # 캐시 저장
            self._set_to_cache(ticker, item)

        return results

    def _write_jsonl(self, output: dict, now_kst: datetime) -> None:
        """artifacts/watch_snapshots/YYYYMMDD.jsonl 에 한 줄 append."""
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        date_str = now_kst.strftime("%Y%m%d")
        out_path = self._snapshot_dir / f"{date_str}.jsonl"

        try:
            with out_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(output, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error("[snapshot_fetcher] jsonl 쓰기 실패: path=%s err=%s", out_path, e)
