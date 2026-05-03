"""C15 DynamicUniverseContract dynamic_holdings 비중 관리. Sprint 5 S5-2.

## 동작 흐름
    1. dynamic_universe_config.yaml 로드
       (holdings.max_size=5, per_stock_max_weight=0.03,
        total_max_weight=0.10, allocator_mode=fixed_rule_only)
    2. promote_to_holdings(admission_event: dict) -> dict | None:
       - max_size 가드
       - per_stock_max_weight 적용
       - total_max_weight 가드
       - holdings entry 생성 + 반환
    3. get_holdings() -> list[dict]
    4. remove(ticker, exit_reason) -> dict | None: exit_event 반환

## weight_decision_authority (C15 정식)
    - HoldingsManager 는 weight 를 제안만 한다.
    - PortfolioManager 가 order_delta 로 변환한다.
    - HoldingsManager 는 직접 주문 발행 금지 (C15 forbidden direct_trade_execution_bypass_pm).
    - FDA 는 admission_event 에 대해서만 veto. exit_event 는 자동 청산.

## C15 forbidden_permissions 코드 가드
    - ppo_allocation_for_dynamic_overlay: ppo 모듈 import 금지
    - fda_weight_modification: FDA 호출 금지
    - direct_trade_execution_bypass_pm: submit_order 호출 금지
    - trade_universe_ssot_mutation: universe_config.yaml write 금지

SSOT: new/specs/api_contracts.md C15 DynamicUniverseContract
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from src.utils.logger import get_logger

logger = get_logger("holdings_manager")

_KST = ZoneInfo("Asia/Seoul")

# C15 forbidden_permissions 가드: ppo 모듈 import 절대 금지
_PPO_FORBIDDEN = "ppo_allocation_for_dynamic_overlay"
assert "ppo" not in dir(), (
    f"[holdings_manager] C15 forbidden_permissions 위반: {_PPO_FORBIDDEN}. "
    "holdings_manager 에서 PPO 추론/임포트 금지."
)

# C15 forbidden_permissions 가드: direct_trade_execution_bypass_pm
# submit_order 는 이 모듈에 없음. 실수로 임포트되면 즉시 탐지.
_SUBMIT_ORDER_FORBIDDEN = "direct_trade_execution_bypass_pm"
assert "submit_order" not in dir(), (
    f"[holdings_manager] C15 forbidden_permissions 위반: {_SUBMIT_ORDER_FORBIDDEN}. "
    "holdings_manager 는 직접 주문 발행 금지."
)

# 기본 경로
_DYNAMIC_CONFIG_PATH_DEFAULT = (
    Path(__file__).resolve().parents[3] / "config" / "dynamic_universe_config.yaml"
)
_ARTIFACTS_DIR_DEFAULT = (
    Path(__file__).resolve().parents[3] / "artifacts" / "dynamic_holdings"
)


def _load_yaml(path: Path) -> dict:
    """yaml 파일 로드. 실패 시 빈 dict 반환."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception as e:
        logger.error("[holdings_manager] yaml 로드 실패: path=%s error=%s", path, e)
        return {}


class HoldingsManager:
    """C15 dynamic_holdings 비중 관리. fixed_rule_only sizing.

    Sprint 5 S5-2.

    C15 forbidden_permissions 준수:
      - ppo_allocation_for_dynamic_overlay: ppo 모듈 import 없음
      - fda_weight_modification: FDA 직접 호출 없음
      - direct_trade_execution_bypass_pm: submit_order 없음
      - trade_universe_ssot_mutation: universe_config.yaml write 없음
    """

    def __init__(
        self,
        dynamic_config_path: Path | None = None,
        artifacts_dir: Path | None = None,
    ) -> None:
        """HoldingsManager 초기화.

        Args:
            dynamic_config_path: dynamic_universe_config.yaml 경로. None이면 기본값.
            artifacts_dir: exit_events.jsonl / YYYYMMDD.json 저장 디렉토리.
                           None이면 기본값.
        """
        self._dynamic_cfg_path = dynamic_config_path or _DYNAMIC_CONFIG_PATH_DEFAULT
        self._artifacts_dir = artifacts_dir or _ARTIFACTS_DIR_DEFAULT

        # dynamic_universe_config.yaml 로드 (하드코딩 금지 원칙)
        dynamic_cfg = _load_yaml(self._dynamic_cfg_path)
        holdings_cfg = dynamic_cfg.get("holdings", {})
        self._max_size: int = int(holdings_cfg.get("max_size", 5))
        self._per_stock_max_weight: float = float(
            holdings_cfg.get("per_stock_max_weight", 0.03)
        )
        self._total_max_weight: float = float(
            holdings_cfg.get("total_max_weight", 0.10)
        )
        allocator_mode: str = holdings_cfg.get("allocator_mode", "fixed_rule_only")

        # allocator_mode=fixed_rule_only 강제 확인
        if allocator_mode != "fixed_rule_only":
            logger.warning(
                "[holdings_manager] allocator_mode=%s. "
                "fixed_rule_only 만 지원. PPO 사용 금지 (C15 forbidden).",
                allocator_mode,
            )

        # exit 정책 로드 (exit_event dict 구성에 사용)
        exit_cfg = dynamic_cfg.get("exit", {})
        self._ttl_sec: int = int(exit_cfg.get("ttl_sec", 1800))
        self._stop_loss_pct: float = float(exit_cfg.get("stop_loss_pct", 0.02))

        # 내부 상태: {ticker: holdings entry dict}
        self._holdings: dict[str, dict] = {}

        logger.info(
            "[holdings_manager] 초기화 완료: max_size=%d per_stock=%.3f total=%.2f "
            "allocator_mode=%s",
            self._max_size,
            self._per_stock_max_weight,
            self._total_max_weight,
            allocator_mode,
        )

    @property
    def _current_total_weight(self) -> float:
        """현재 holdings 합산 비중."""
        return sum(h["weight"] for h in self._holdings.values())

    def promote_to_holdings(self, admission_event: dict) -> dict | None:
        """admission_event → holdings entry 생성.

        흐름:
          1. max_size 가드
          2. total_max_weight 가드 (현재 합계 + per_stock_max_weight > total_max_weight)
          3. holdings entry 생성 + 반환

        Args:
            admission_event: AdmissionEngine.handle_event() 반환 dict.
                             ticker, admitted_at, trigger_ids 필드 필수.

        Returns:
            holdings entry dict 또는 None (거부).
        """
        ticker = str(admission_event.get("ticker", "")).zfill(6)
        admitted_at: str = admission_event.get("admitted_at", datetime.now(_KST).isoformat())

        if not ticker or ticker == "000000":
            logger.warning("[holdings_manager] promote_to_holdings: ticker 비어 있음 → 거부")
            return None

        # 이미 holdings 에 있으면 skip
        if ticker in self._holdings:
            logger.debug(
                "[holdings_manager] ticker=%s 이미 holdings 에 존재. skip.", ticker
            )
            return None

        # max_size 가드
        if len(self._holdings) >= self._max_size:
            logger.warning(
                "[holdings_manager] max_size 초과: %d/%d. ticker=%s promote 거부.",
                len(self._holdings),
                self._max_size,
                ticker,
            )
            return None

        # total_max_weight 가드
        projected_total = self._current_total_weight + self._per_stock_max_weight
        if projected_total > self._total_max_weight:
            logger.warning(
                "[holdings_manager] total_max_weight 초과: 현재=%.3f + 신규=%.3f > %.3f. "
                "ticker=%s promote 거부.",
                self._current_total_weight,
                self._per_stock_max_weight,
                self._total_max_weight,
                ticker,
            )
            return None

        # holdings entry 생성
        now_kst = datetime.now(_KST)
        promotion_id = f"PRM-{now_kst.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"

        entry: dict[str, Any] = {
            "ticker": ticker,
            "weight": self._per_stock_max_weight,
            "admitted_at": admitted_at,
            "exit_policy": {
                "ttl_sec": self._ttl_sec,
                "stop_loss_pct": self._stop_loss_pct,
                "spike_resolved": True,
                "market_close": True,
            },
            "promotion_id": promotion_id,
        }

        self._holdings[ticker] = entry

        # artifacts/dynamic_holdings/YYYYMMDD.json 스냅샷 저장
        self._write_snapshot(now_kst)

        logger.info(
            "[holdings_manager] promote 완료: id=%s ticker=%s weight=%.3f "
            "holdings=%d/%d total_weight=%.3f",
            promotion_id,
            ticker,
            self._per_stock_max_weight,
            len(self._holdings),
            self._max_size,
            self._current_total_weight,
        )
        return entry

    def get_holdings(self) -> list[dict]:
        """현재 dynamic_holdings 상태 반환."""
        return list(self._holdings.values())

    def remove(self, ticker: str, exit_reason: str) -> dict | None:
        """청산 호출. exit_event dict 반환.

        Args:
            ticker: 청산할 종목코드.
            exit_reason: 청산 원인.
                         "market_close" | "ttl_expiry" | "stop_loss" | "spike_resolved"

        Returns:
            exit_event dict 또는 None (ticker 미존재).
        """
        ticker = str(ticker).zfill(6)
        if ticker not in self._holdings:
            logger.debug(
                "[holdings_manager] remove: ticker=%s holdings 미존재.", ticker
            )
            return None

        entry = self._holdings.pop(ticker)
        now_kst = datetime.now(_KST)
        exit_id = f"EXT-{now_kst.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"

        exit_event: dict[str, Any] = {
            "exit_event_id": exit_id,
            "ticker": ticker,
            "exit_reason": exit_reason,
            "exit_ts": now_kst.isoformat(),
            "weight_freed": entry["weight"],
        }

        # artifacts/dynamic_holdings/exit_events.jsonl append
        self._write_exit_event(exit_event)

        # 스냅샷 업데이트 (청산 후 상태)
        self._write_snapshot(now_kst)

        logger.info(
            "[holdings_manager] remove 완료: id=%s ticker=%s reason=%s "
            "weight_freed=%.3f holdings=%d/%d",
            exit_id,
            ticker,
            exit_reason,
            entry["weight"],
            len(self._holdings),
            self._max_size,
        )
        return exit_event

    def _write_exit_event(self, event: dict) -> None:
        """artifacts/dynamic_holdings/exit_events.jsonl 에 append."""
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)
        out_path = self._artifacts_dir / "exit_events.jsonl"
        try:
            with out_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error("[holdings_manager] exit_events.jsonl 쓰기 실패: %s", e)

    def _write_snapshot(self, now_kst: datetime) -> None:
        """artifacts/dynamic_holdings/YYYYMMDD.json 현재 상태 스냅샷 저장."""
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)
        date_str = now_kst.strftime("%Y%m%d")
        out_path = self._artifacts_dir / f"{date_str}.json"

        snapshot: dict[str, Any] = {
            "snapshot_ts": now_kst.isoformat(),
            "holdings_count": len(self._holdings),
            "total_weight": self._current_total_weight,
            "holdings": self.get_holdings(),
        }
        try:
            with out_path.open("w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("[holdings_manager] snapshot json 쓰기 실패: %s", e)
