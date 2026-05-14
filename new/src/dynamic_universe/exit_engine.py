"""C15 DynamicUniverseContract dynamic_holdings 청산 엔진. Sprint 5 S5-3.

## 동작 흐름
    1. dynamic_universe_config.yaml exit 섹션 로드:
       - ttl_sec: 1800
       - stop_loss_pct: 0.02
       - market_close_kst: "15:30"
       - spike_resolved.community_zscore_threshold: 1.5
       - spike_resolved.price_change_threshold: 0.015
       - spike_resolved.min_holding_sec: 600
    2. evaluate(current_snapshot, trigger_state, now_kst) -> list[exit_event]:
       - 매 1분 또는 이벤트 시 호출
       - holdings 각 entry 에 대해 4조건 순차 평가
       - 매칭 시 holdings_manager.remove(ticker, exit_reason) 호출
       - admission_engine.remove_from_pool(ticker) 호출 (cooldown 시작)
       - exit_event dict 리스트 반환 (HoldingsManager.remove 결과 forward)
    3. 4 조건 평가 메서드:
       - _check_market_close(now_kst) -> bool
       - _check_ttl_expiry(entry, now_kst) -> bool
       - _check_stop_loss(entry, current_price) -> bool
       - _check_spike_resolved(entry, trigger_state, now_kst) -> bool

## C15 forbidden_permissions 코드 가드
    - direct_trade_execution_bypass_pm: holdings_manager.remove 만 호출, submit_order 금지
    - mode_b_cold_path_intervention: Mode B 모듈 호출 금지
    - lightgbm/ppo import 금지 (assert)

## PIT-Safety + KST 시간대
    - 모든 시간 ZoneInfo("Asia/Seoul") 명시
    - admitted_at 파싱 시 datetime.fromisoformat + tz 명시
    - now_kst 는 datetime.now(tz=_KST)
    - UTC 혼용 절대 금지

## 에러 처리
    - holdings_entry 누락 필드 → 로그 + skip
    - dynamic_universe_config.yaml 로드 실패 → 안전한 기본값 + 경고 로그

SSOT: new/specs/api_contracts.md C15 DynamicUniverseContract
"""
from __future__ import annotations

from datetime import datetime, time
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import yaml

from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.dynamic_universe.admission_engine import AdmissionEngine
    from src.dynamic_universe.holdings_manager import HoldingsManager

logger = get_logger("exit_engine")

_KST = ZoneInfo("Asia/Seoul")

# C15 forbidden_permissions 가드: ppo 모듈 import 절대 금지
_PPO_FORBIDDEN = "ppo_allocation_for_dynamic_overlay"
assert "ppo" not in dir(), (
    f"[exit_engine] C15 forbidden_permissions 위반: {_PPO_FORBIDDEN}. "
    "exit_engine 에서 PPO 추론/임포트 금지."
)

# C15 forbidden_permissions 가드: lightgbm import 절대 금지
_LGB_FORBIDDEN = "lightgbm_inference_for_watch_universe"
assert "lightgbm" not in dir(), (
    f"[exit_engine] C15 forbidden_permissions 위반: {_LGB_FORBIDDEN}. "
    "exit_engine 에서 LightGBM 추론/임포트 금지."
)

# C15 forbidden_permissions 가드: direct_trade_execution_bypass_pm
assert "submit_order" not in dir(), (
    "[exit_engine] C15 forbidden_permissions 위반: direct_trade_execution_bypass_pm. "
    "exit_engine 은 직접 주문 발행 금지."
)

# 기본 경로
_DYNAMIC_CONFIG_PATH_DEFAULT = (
    Path(__file__).resolve().parents[2] / "config" / "dynamic_universe_config.yaml"
)


def _load_yaml(path: Path) -> dict:
    """yaml 파일 로드. 실패 시 빈 dict 반환."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception as e:
        logger.error("[exit_engine] yaml 로드 실패: path=%s error=%s", path, e)
        return {}


def _parse_kst_time(time_str: str) -> time:
    """'HH:MM' 문자열 → time 객체 파싱. 실패 시 ValueError."""
    try:
        parts = time_str.strip().split(":")
        return time(int(parts[0]), int(parts[1]))
    except Exception as e:
        raise ValueError(
            f"[exit_engine] market_close_kst 파싱 실패: '{time_str}': {e}"
        ) from e


def _parse_admitted_at(admitted_at_str: str) -> datetime:
    """admitted_at ISO8601 문자열 → KST-aware datetime.

    KST timezone 이 없으면 Asia/Seoul 로 부여.
    """
    dt = datetime.fromisoformat(admitted_at_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_KST)
    return dt


class ExitEngine:
    """C15 dynamic_holdings 청산 엔진. 4 exit policy 통합.

    Sprint 5 S5-3.

    C15 forbidden_permissions 준수:
      - direct_trade_execution_bypass_pm: submit_order 호출 없음
      - mode_b_cold_path_intervention: Mode B 모듈 호출 없음
      - ppo_allocation_for_dynamic_overlay: ppo 모듈 import 없음
    """

    def __init__(
        self,
        holdings_manager: "HoldingsManager",
        admission_engine: "AdmissionEngine",
        dynamic_config_path: Path | None = None,
    ) -> None:
        """ExitEngine 초기화.

        Args:
            holdings_manager: HoldingsManager 인스턴스.
            admission_engine: AdmissionEngine 인스턴스.
            dynamic_config_path: dynamic_universe_config.yaml 경로. None이면 기본값.
        """
        self._holdings_manager = holdings_manager
        self._admission_engine = admission_engine
        self._dynamic_cfg_path = dynamic_config_path or _DYNAMIC_CONFIG_PATH_DEFAULT

        # dynamic_universe_config.yaml exit 섹션 로드 (하드코딩 금지 원칙)
        dynamic_cfg = _load_yaml(self._dynamic_cfg_path)
        exit_cfg = dynamic_cfg.get("exit", {})

        self._ttl_sec: int = int(exit_cfg.get("ttl_sec", 1800))
        self._stop_loss_pct: float = float(exit_cfg.get("stop_loss_pct", 0.02))

        market_close_str: str = exit_cfg.get("market_close_kst", "15:30")
        try:
            self._market_close_time: time = _parse_kst_time(market_close_str)
        except ValueError:
            logger.warning(
                "[exit_engine] market_close_kst 파싱 실패: '%s'. 기본값 15:30 사용.",
                market_close_str,
            )
            self._market_close_time = time(15, 30)

        spike_cfg = exit_cfg.get("spike_resolved", {})
        self._spike_zscore_threshold: float = float(
            spike_cfg.get("community_zscore_threshold", 1.5)
        )
        self._spike_price_threshold: float = float(
            spike_cfg.get("price_change_threshold", 0.015)
        )
        self._spike_min_holding_sec: int = int(
            spike_cfg.get("min_holding_sec", 600)
        )

        logger.info(
            "[exit_engine] 초기화 완료: ttl_sec=%d stop_loss_pct=%.3f "
            "market_close=%s spike_zscore_threshold=%.2f "
            "spike_price_threshold=%.3f spike_min_holding_sec=%d",
            self._ttl_sec,
            self._stop_loss_pct,
            self._market_close_time.strftime("%H:%M"),
            self._spike_zscore_threshold,
            self._spike_price_threshold,
            self._spike_min_holding_sec,
        )

    # ----------------------------------------------------------------
    # 4 조건 평가 메서드
    # ----------------------------------------------------------------

    def _check_market_close(self, now_kst: datetime) -> bool:
        """장 마감 후 여부 확인.

        Returns:
            now_kst.time() >= market_close_kst 이면 True.
        """
        return now_kst.time() >= self._market_close_time

    def _check_ttl_expiry(self, entry: dict, now_kst: datetime) -> bool:
        """TTL 만료 여부 확인.

        Returns:
            admitted_at 으로부터 ttl_sec 이 경과했으면 True.
        """
        admitted_at_str: str = entry.get("admitted_at", "")
        if not admitted_at_str:
            logger.warning(
                "[exit_engine] ttl 체크: ticker=%s admitted_at 필드 없음 → skip",
                entry.get("ticker", "?"),
            )
            return False

        try:
            admitted_at = _parse_admitted_at(admitted_at_str)
        except Exception as e:
            logger.warning(
                "[exit_engine] ttl 체크: admitted_at 파싱 실패: '%s' err=%s → skip",
                admitted_at_str,
                e,
            )
            return False

        elapsed = (now_kst - admitted_at).total_seconds()
        return elapsed > self._ttl_sec

    def _check_stop_loss(self, entry: dict, current_price: float) -> bool:
        """손절 여부 확인.

        entry_price 는 entry.get("entry_price") 또는 entry.get("last_price") 기준.
        entry_price 없으면 False 반환 (안전 fallback).

        Returns:
            (current_price - entry_price) / entry_price < -stop_loss_pct 이면 True.
        """
        entry_price: float | None = entry.get("entry_price") or entry.get("last_price")
        if entry_price is None:
            logger.warning(
                "[exit_engine] stop_loss 체크: ticker=%s entry_price 없음 → skip",
                entry.get("ticker", "?"),
            )
            return False

        try:
            entry_price = float(entry_price)
        except Exception as e:
            logger.warning(
                "[exit_engine] stop_loss 체크: entry_price 파싱 실패: %s err=%s → skip",
                entry_price,
                e,
            )
            return False

        if entry_price <= 0:
            logger.warning(
                "[exit_engine] stop_loss 체크: entry_price <= 0 (ticker=%s). skip.",
                entry.get("ticker", "?"),
            )
            return False

        pnl_pct = (current_price - entry_price) / entry_price
        return pnl_pct < -self._stop_loss_pct

    def _check_spike_resolved(
        self,
        entry: dict,
        trigger_state: dict[str, dict] | None,
        now_kst: datetime,
    ) -> bool:
        """spike_resolved 여부 확인.

        조건:
          1. 최소 보유 시간(min_holding_sec) 경과
          2. community z-score < zscore_threshold
          3. |price_change_pct| <= price_change_threshold
          4. trigger_state[ticker].active_triggers 비어 있음 (trigger 소멸)

        trigger_state 가 None 이면 False 반환 (안전).

        Returns:
            4개 조건 모두 충족 시 True.
        """
        if trigger_state is None:
            return False

        ticker: str = entry.get("ticker", "")
        if not ticker:
            return False

        # 최소 보유 시간
        admitted_at_str: str = entry.get("admitted_at", "")
        if not admitted_at_str:
            return False

        try:
            admitted_at = _parse_admitted_at(admitted_at_str)
        except Exception as e:
            logger.warning(
                "[exit_engine] spike_resolved 체크: admitted_at 파싱 실패: %s err=%s → skip",
                admitted_at_str,
                e,
            )
            return False

        elapsed = (now_kst - admitted_at).total_seconds()
        if elapsed < self._spike_min_holding_sec:
            return False

        # trigger_state 에서 해당 ticker 상태 조회
        ticker_state: dict = trigger_state.get(ticker, {})

        # community z-score 기준
        community_zscore: float | None = ticker_state.get("community_zscore")
        if community_zscore is None:
            return False
        if abs(float(community_zscore)) >= self._spike_zscore_threshold:
            return False

        # price change 기준 (절댓값)
        # trigger_state 에서 day_change_pct 또는 price_change_pct 사용
        price_change: float | None = ticker_state.get("day_change_pct") or ticker_state.get(
            "price_change_pct"
        )
        if price_change is None:
            return False
        if abs(float(price_change)) > self._spike_price_threshold:
            return False

        # active_triggers 소멸 확인
        active_triggers: list = ticker_state.get("active_triggers", [])
        if active_triggers:
            return False

        return True

    # ----------------------------------------------------------------
    # 핵심 public 메서드
    # ----------------------------------------------------------------

    def evaluate(
        self,
        current_snapshot: dict[str, dict],
        trigger_state: dict[str, dict] | None = None,
        now_kst: datetime | None = None,
    ) -> list[dict]:
        """4 exit policy 순차 평가 → 청산 실행.

        매 1분 또는 이벤트 시 호출.

        Args:
            current_snapshot: {ticker: {last_price: float, day_change_pct: float, ts: ISO8601}}
            trigger_state: {ticker: {community_zscore: float, active_triggers: list[str]}}
                           None 이면 spike_resolved 평가 skip.
            now_kst: 현재 KST datetime. None 이면 datetime.now(_KST) 사용.

        Returns:
            청산된 종목별 exit_event dict 리스트.
        """
        if now_kst is None:
            now_kst = datetime.now(tz=_KST)

        # now_kst timezone 보장
        if now_kst.tzinfo is None:
            now_kst = now_kst.replace(tzinfo=_KST)

        holdings: list[dict] = self._holdings_manager.get_holdings()
        exit_events: list[dict] = []

        for entry in holdings:
            ticker: str = entry.get("ticker", "")
            if not ticker:
                logger.warning("[exit_engine] holdings entry ticker 없음 → skip")
                continue

            exit_reason: str | None = None

            # 조건 1: market_close
            if self._check_market_close(now_kst):
                exit_reason = "market_close"

            # 조건 2: ttl_expiry
            elif self._check_ttl_expiry(entry, now_kst):
                exit_reason = "ttl_expiry"

            # 조건 3: stop_loss
            elif ticker in current_snapshot:
                last_price = current_snapshot[ticker].get("last_price")
                if last_price is not None:
                    if self._check_stop_loss(entry, float(last_price)):
                        exit_reason = "stop_loss"

            # 조건 4: spike_resolved
            if exit_reason is None:
                if self._check_spike_resolved(entry, trigger_state, now_kst):
                    exit_reason = "spike_resolved"

            if exit_reason is None:
                continue

            # 청산 실행
            exit_event = self._holdings_manager.remove(ticker, exit_reason)
            if exit_event is not None:
                # cooldown 시작: candidate_pool 에서도 제거
                self._admission_engine.remove_from_pool(ticker)
                exit_events.append(exit_event)

                logger.info(
                    "[exit_engine] 청산 완료: ticker=%s reason=%s",
                    ticker,
                    exit_reason,
                )

        if exit_events:
            logger.info(
                "[exit_engine] evaluate 결과: %d건 청산 (now_kst=%s)",
                len(exit_events),
                now_kst.isoformat(),
            )

        return exit_events
