"""C15 Sprint 5 통합 오케스트레이터. snapshot → admission → holdings → exit lifecycle.

Sprint 5 S5-4.

## 동작 흐름
    1. gate.is_enabled() 확인 (false → 즉시 return, noop)
    2. cycle_once() 매 1분 호출 (외부 scheduler 또는 hot_runner 통합):
       a. snapshot_fetcher.fetch_once() → watch_snapshot
       b. ExitEngine.evaluate(snapshot, trigger_state) → exit_events list
       c. cycle 결과 dict 반환:
          {
            "cycle_id": "DUC-yyyymmdd-UUID8",  # Dynamic Universe Cycle
            "ts": ISO8601 KST,
            "watch_snapshot_id": str,
            "candidate_pool_size": int,
            "holdings_size": int,
            "exit_events": list,
            "enabled": bool,
          }
    3. handle_admission_event(event: dict) -> dict | None:
       - admission_engine.handle_event(event) → admission_event
       - 즉시 holdings_manager.promote_to_holdings(admission_event) → promotion 결과
    4. shutdown():
       - 마지막 holdings 상태 저장 + gate transition 로그

## C15 weight_decision_authority 준수
    - Manager 는 weight 를 직접 수정하지 않는다. HoldingsManager 에 위임.
    - FDA 호출 없음. PPO/LightGBM import 없음.

SSOT: new/specs/api_contracts.md C15 DynamicUniverseContract
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.dynamic_universe.admission_engine import AdmissionEngine
from src.dynamic_universe.exit_engine import ExitEngine
from src.dynamic_universe.gate import DynamicUniverseGate
from src.dynamic_universe.holdings_manager import HoldingsManager
from src.dynamic_universe.snapshot_fetcher import WatchSnapshotFetcher
from src.utils.logger import get_logger

logger = get_logger("manager")

_KST = ZoneInfo("Asia/Seoul")

# C15 forbidden_permissions 가드: ppo/lightgbm/fda 절대 금지
_PPO_FORBIDDEN = "ppo_allocation_for_dynamic_overlay"
assert "ppo" not in dir(), (
    f"[manager] C15 forbidden_permissions 위반: {_PPO_FORBIDDEN}. "
    "manager 에서 PPO 추론/임포트 금지."
)
assert "lightgbm" not in dir(), (
    "[manager] C15 forbidden_permissions 위반: lightgbm_inference_for_watch_universe. "
    "manager 에서 LightGBM 추론/임포트 금지."
)
assert "submit_order" not in dir(), (
    "[manager] C15 forbidden_permissions 위반: direct_trade_execution_bypass_pm. "
    "manager 는 직접 주문 발행 금지."
)

# cycle 결과 JSONL 저장 디렉토리
_CYCLES_DIR_DEFAULT = (
    Path(__file__).resolve().parents[3] / "artifacts" / "dynamic_holdings" / "cycles"
)


class DynamicUniverseManager:
    """C15 Sprint 5 통합 오케스트레이터. snapshot → admission → holdings → exit lifecycle.

    Sprint 5 S5-4.

    ## C15 weight_decision_authority 준수
        - weight 직접 수정 없음. HoldingsManager 에 위임.
        - FDA 호출 없음.
        - PPO/LightGBM import 없음.
    """

    def __init__(
        self,
        snapshot_fetcher: WatchSnapshotFetcher,
        admission_engine: AdmissionEngine,
        holdings_manager: HoldingsManager,
        exit_engine: ExitEngine,
        gate: DynamicUniverseGate,
        cycles_dir: Path | None = None,
    ) -> None:
        """DynamicUniverseManager 초기화.

        Args:
            snapshot_fetcher: WatchSnapshotFetcher 인스턴스.
            admission_engine: AdmissionEngine 인스턴스.
            holdings_manager: HoldingsManager 인스턴스.
            exit_engine: ExitEngine 인스턴스.
            gate: DynamicUniverseGate 인스턴스.
            cycles_dir: cycle JSONL 저장 디렉토리. None이면 기본값.
        """
        self._snapshot_fetcher = snapshot_fetcher
        self._admission_engine = admission_engine
        self._holdings_manager = holdings_manager
        self._exit_engine = exit_engine
        self._gate = gate
        self._cycles_dir = cycles_dir or _CYCLES_DIR_DEFAULT

        logger.info("[manager] 초기화 완료: cycles_dir=%s", self._cycles_dir)

    def cycle_once(self, trigger_state: dict | None = None) -> dict:
        """매 1분 호출 cycle. gate disabled 시 즉시 noop 반환.

        흐름:
          1. gate.is_enabled() 확인 → false 이면 즉시 return (모든 컴포넌트 미호출)
          2. snapshot_fetcher.fetch_once() → watch_snapshot
          3. exit_engine.evaluate(snapshot_map, trigger_state) → exit_events
          4. cycle 결과 dict 생성 + JSONL append
          5. 결과 반환

        Args:
            trigger_state: {ticker: {community_zscore, active_triggers}} or None.
                           ExitEngine.evaluate 로 전달. None 이면 spike_resolved 평가 skip.

        Returns:
            cycle 결과 dict:
              {
                "cycle_id": "DUC-YYYYMMDD-UUID8",
                "ts": ISO8601 KST,
                "watch_snapshot_id": str,
                "candidate_pool_size": int,
                "holdings_size": int,
                "exit_events": list[dict],
                "enabled": bool,
              }
        """
        enabled = self._gate.is_enabled()
        now_kst = datetime.now(_KST)
        ts_str = now_kst.isoformat()
        cycle_id = f"DUC-{now_kst.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"

        if not enabled:
            logger.debug(
                "[manager] cycle_once: gate disabled. noop. cycle_id=%s", cycle_id
            )
            return {
                "cycle_id": cycle_id,
                "ts": ts_str,
                "watch_snapshot_id": "",
                "candidate_pool_size": 0,
                "holdings_size": 0,
                "exit_events": [],
                "enabled": False,
            }

        # 2. watch_snapshot 수집
        try:
            watch_snapshot = self._snapshot_fetcher.fetch_once()
        except Exception as e:
            logger.error("[manager] snapshot_fetcher.fetch_once 실패: %s", e)
            watch_snapshot = {"watch_snapshot_id": "", "ts": ts_str, "snapshots": []}

        watch_snapshot_id: str = watch_snapshot.get("watch_snapshot_id", "")
        snapshots_list: list[dict] = watch_snapshot.get("snapshots", [])

        # snapshot_map: {ticker: snapshot_dict} 형태로 변환 (exit_engine 입력 형식)
        snapshot_map: dict[str, dict] = {
            s.get("ticker", ""): s for s in snapshots_list if s.get("ticker")
        }

        # 3. exit 평가
        try:
            exit_events = self._exit_engine.evaluate(snapshot_map, trigger_state, now_kst)
        except Exception as e:
            logger.error("[manager] exit_engine.evaluate 실패: %s", e)
            exit_events = []

        # 현재 holdings/pool 크기
        holdings_size = len(self._holdings_manager.get_holdings())
        candidate_pool_size = len(self._admission_engine.get_pool_state())

        # 4. 결과 dict
        result = {
            "cycle_id": cycle_id,
            "ts": ts_str,
            "watch_snapshot_id": watch_snapshot_id,
            "candidate_pool_size": candidate_pool_size,
            "holdings_size": holdings_size,
            "exit_events": exit_events,
            "enabled": True,
        }

        # JSONL append
        self._write_cycle(result, now_kst)

        logger.info(
            "[manager] cycle_once 완료: cycle_id=%s snapshot=%s pool=%d holdings=%d exit=%d",
            cycle_id,
            watch_snapshot_id,
            candidate_pool_size,
            holdings_size,
            len(exit_events),
        )
        return result

    def handle_admission_event(self, event: dict) -> dict | None:
        """외부 이벤트 → admission → holdings promote 통합 처리.

        gate disabled 시 즉시 None 반환 (noop).

        흐름:
          1. gate.is_enabled() 확인
          2. admission_engine.handle_event(event) → admission_event or None
          3. admission 성공 시 holdings_manager.promote_to_holdings(admission_event)
          4. promote 결과 반환

        Args:
            event: C2 EventNormalizeContract 이벤트 dict.
                   ticker, ts, event_type 필드 필수.

        Returns:
            promote 결과 dict (holdings entry) or None (거부/gate disabled).
        """
        if not self._gate.is_enabled():
            logger.debug("[manager] handle_admission_event: gate disabled. noop.")
            return None

        # admission 평가
        try:
            admission_event = self._admission_engine.handle_event(event)
        except Exception as e:
            logger.error("[manager] admission_engine.handle_event 실패: %s", e)
            return None

        if admission_event is None:
            return None

        # holdings promote
        # candidate_pool max 가드는 AdmissionEngine 에서 이미 처리.
        # HoldingsManager.promote_to_holdings 가 max_size/weight 가드 재검증.
        try:
            promotion = self._holdings_manager.promote_to_holdings(admission_event)
        except Exception as e:
            logger.error(
                "[manager] holdings_manager.promote_to_holdings 실패: ticker=%s err=%s",
                admission_event.get("ticker"),
                e,
            )
            return None

        if promotion is None:
            logger.info(
                "[manager] promote 거부 (max_size/weight 초과): ticker=%s",
                admission_event.get("ticker"),
            )
            return None

        logger.info(
            "[manager] handle_admission_event 완료: ticker=%s promotion_id=%s",
            promotion.get("ticker"),
            promotion.get("promotion_id"),
        )
        return promotion

    def shutdown(self) -> None:
        """종료 처리. 마지막 holdings 상태 저장 + gate transition 로그.

        외부 scheduler 또는 signal handler 에서 호출.
        """
        now_kst = datetime.now(_KST)

        # 마지막 holdings 스냅샷 저장
        try:
            holdings = self._holdings_manager.get_holdings()
            self._cycles_dir.mkdir(parents=True, exist_ok=True)
            shutdown_path = (
                self._cycles_dir / f"shutdown_{now_kst.strftime('%Y%m%d_%H%M%S')}.json"
            )
            snapshot = {
                "shutdown_ts": now_kst.isoformat(),
                "holdings_count": len(holdings),
                "holdings": holdings,
            }
            with shutdown_path.open("w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, ensure_ascii=False, indent=2)
            logger.info(
                "[manager] shutdown 스냅샷 저장 완료: path=%s holdings=%d",
                shutdown_path,
                len(holdings),
            )
        except Exception as e:
            logger.error("[manager] shutdown 스냅샷 저장 실패: %s", e)

        # gate transition 로그: enabled 상태 종료 기록
        try:
            current_enabled = self._gate.is_enabled()
            self._gate.log_transition(
                from_state=current_enabled,
                to_state=current_enabled,
                reason="manager_shutdown",
            )
        except Exception as e:
            logger.error("[manager] gate transition 로그 실패: %s", e)

        logger.info("[manager] shutdown 완료.")

    def _write_cycle(self, result: dict, now_kst: datetime) -> None:
        """artifacts/dynamic_holdings/cycles/YYYYMMDD.jsonl 에 cycle 결과 append."""
        self._cycles_dir.mkdir(parents=True, exist_ok=True)
        date_str = now_kst.strftime("%Y%m%d")
        out_path = self._cycles_dir / f"{date_str}.jsonl"

        try:
            with out_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(result, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error("[manager] cycles JSONL 쓰기 실패: path=%s err=%s", out_path, e)
