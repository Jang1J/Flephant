"""JSONL audit logger v3 Sprint 2 S2-6 prep (2026-04-21).

두 가지 API 제공:
1. 레거시 API (Sprint 1 호환): log(event_type, payload, ts)
   기존 Hot Path + ExecutionGateway 가 사용 중. 하위 호환 유지.
2. C18 API (Sprint 2+): log_entry(AuditLogEntry), backfill_label()
   Cold Path 에이전트 + PM + ExecutionGateway 의 18-필드 decision 기록 전용.

C18 AgentPerformanceContract audit_log_schema 20 필드 (2026-05-09 P1 fix: backfill 메타 2개 추가):
    ts, decision_id, agent, event_type, ticker, reason_code,
    signal_score, anomaly_flag, target_weight, actual_weight,
    fill_price, snapshot_vwap, slippage_bps, sector,
    llm_called, llm_model, label_t5_ret, price_t5_snapshot,
    label_backfilled_at, label_backfill_source

PIT-Safety (불변 원칙 1):
    label_t5_ret / price_t5_snapshot 은 장중 null 로 기록.
    18:00 KST 이후 배치 job 만 backfill 허용.
    장중 backfill 시도 시 RuntimeError.
    backfill 시 label_backfilled_at (KST ISO8601) + label_backfill_source 메타 동시 기록.
    cause_attribution._is_label_pit_safe() 가드의 SSOT 데이터 원천.

SSOT: api_contracts.md C18 audit_log_schema (2026-05-09 backfill 메타 2필드 추가).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.utils.config_loader import load as config_load
from src.utils.label_meta import backfill_source_default
from src.utils.logger import get_logger
from src.utils.time_utils import now_kst

logger = get_logger("audit_logger")

_KST = ZoneInfo("Asia/Seoul")

_DEFAULT_LOG_PATH = (
    Path(__file__).resolve().parents[3] / "artifacts" / "audit_log.jsonl"
)


# ------------------------------------------------------------------ #
# C18 AuditLogEntry dataclass (18 필드)
# ------------------------------------------------------------------ #


@dataclass
class AuditLogEntry:
    """C18 18 필드 audit log entry.

    Required (장중 기록):
        ts, decision_id, agent, event_type, ticker
    Nullable (장중 미기록):
        label_t5_ret, price_t5_snapshot  (18:00 이후 backfill 전용)
    """

    ts: str                                    # ISO8601 KST
    decision_id: str                           # DEC-{yyyymmdd}-{UUID8}
    agent: str                                 # quant | news | risk_fast | ... | execution_gw
    event_type: str                            # signal | anomaly_trigger | veto | approve | order | fill
    ticker: str                                # 6-digit zfill

    reason_code: str | None = None             # C9 SSOT
    signal_score: float | None = None          # Quant
    anomaly_flag: bool | None = None           # Quant
    target_weight: float | None = None         # PM
    actual_weight: float | None = None         # PM
    fill_price: float | None = None            # EG
    snapshot_vwap: float | None = None         # EG (slippage 계산용)
    slippage_bps: float | None = None          # EG
    sector: str | None = None                  # PM sector breakdown
    llm_called: bool | None = None             # News/Risk/Debate
    llm_model: str | None = None               # kanana-o | gpt-4o
    label_t5_ret: float | None = None          # post-hoc, 18:00 이후 backfill
    price_t5_snapshot: float | None = None     # post-hoc
    # 2026-05-09 P1 fix (Critical MA-1): C18 backfill 메타 SSOT.
    # cause_attribution._is_label_pit_safe() 가 이 두 필드로 PIT-Safety 검증.
    # 장중 None, 18:00 KST 이후 backfill_label() 호출 시 채움.
    label_backfilled_at: str | None = None     # backfill 실행 KST ISO8601 (post-hoc)
    label_backfill_source: str | None = None   # mode_b_stage_1_rollup | synth_audit_log | manual

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ------------------------------------------------------------------ #
# AuditLogger (레거시 + C18 통합)
# ------------------------------------------------------------------ #


class AuditLogger:
    """JSON Lines append-only audit log.

    Sprint 1 레거시 API (log/read_recent/count/clear) 완전 유지.
    Sprint 2+ C18 API (log_entry/backfill_label/read_entries) 추가.
    """

    def __init__(self, log_path: Path | None = None) -> None:
        self._log_path: Path = Path(log_path) if log_path else _DEFAULT_LOG_PATH
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._record_count: int = 0
        pit_cfg = config_load("risk_config.yaml", "pit_safety") or {}
        self._pit_safe_hour: int = int(pit_cfg.get("snapshot_hour", 18))
        logger.info("[audit_logger] 초기화: path=%s", self._log_path)

    @property
    def log_path(self) -> Path:
        return self._log_path

    # ------------------------------------------------------------------ #
    # 레거시 API (Sprint 1 하위 호환)
    # ------------------------------------------------------------------ #

    def log(
        self,
        event_type: str,
        payload: dict[str, Any],
        ts: str | None = None,
    ) -> None:
        """단일 이벤트 append. ts 미지정 시 현재 UTC.

        Sprint 1 하위 호환 API. event_type + payload 구조.
        """
        record = {
            "ts": ts or datetime.now(tz=timezone.utc).isoformat(),
            "event_type": str(event_type),
            "payload": payload,
        }
        with self._log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._record_count += 1

    def read_recent(self, n: int = 100) -> list[dict[str, Any]]:
        """최근 n개 레코드 읽기. n이 전체보다 크면 전체 반환."""
        if not self._log_path.exists():
            return []
        lines: list[str] = []
        with self._log_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    lines.append(line)
        out = []
        for line in lines[-n:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                logger.warning("[audit_logger] corrupt line: %s", e)
                continue
        return out

    def count(self) -> int:
        """이 인스턴스가 기록한 record 수 (인메모리 카운터)."""
        return self._record_count

    def count_from_file(self) -> int:
        """파일 실제 line 수 (디스크 조회)."""
        if not self._log_path.exists():
            return 0
        with self._log_path.open("r", encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())

    def clear(self) -> None:
        """파일 삭제 (테스트 전용, Sprint 1 개발 단계)."""
        if self._log_path.exists():
            self._log_path.unlink()
        self._record_count = 0

    # ------------------------------------------------------------------ #
    # C18 API (Sprint 2+)
    # ------------------------------------------------------------------ #

    def log_entry(self, entry: AuditLogEntry) -> None:
        """C18 18 필드 entry 기록.

        장중 label_t5_ret/price_t5_snapshot 은 반드시 None.

        Raises:
            RuntimeError: 장중에 label_t5_ret 또는 price_t5_snapshot 채워서 호출 시
                          (PIT-Safety 위반, 불변 원칙 1).
        """
        now = now_kst()
        has_post_hoc_label = (
            entry.label_t5_ret is not None or entry.price_t5_snapshot is not None
        )
        if now.hour < self._pit_safe_hour:
            if has_post_hoc_label:
                raise RuntimeError(
                    f"PIT_SAFETY_VIOLATION: label_t5_ret/price_t5_snapshot 은 "
                    f"{self._pit_safe_hour}:00 KST 이후 backfill 전용. "
                    f"장중({now.strftime('%H:%M')}) 기록 시도 차단. 불변 원칙 1."
                )
        elif has_post_hoc_label:
            # 18:00 이후 직접 label을 기록하는 수동 보정 경로도 C18 metadata를 남긴다.
            if entry.label_backfilled_at is None:
                entry.label_backfilled_at = now.isoformat()
            if entry.label_backfill_source is None:
                entry.label_backfill_source = backfill_source_default()

        with self._log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        self._record_count += 1
        logger.debug(
            "[audit_logger] %s/%s ticker=%s decision=%s",
            entry.agent, entry.event_type, entry.ticker, entry.decision_id,
        )

    def backfill_label(
        self,
        decision_id: str,
        label_t5_ret: float,
        price_t5_snapshot: float,
        source: str = "mode_b_stage_1_rollup",
    ) -> int:
        """장마감 18:00 이후 기존 entry 에 label/price + backfill 메타 추가.

        2026-05-09 P1 fix (Critical MA-1): label_backfilled_at + label_backfill_source 동시 기록.
        cause_attribution._is_label_pit_safe() 가드가 이 두 필드를 검증한다.
        이 두 필드 없으면 cause_attribution 이 모든 entry 를 pit_violation 으로 분류.

        Args:
            decision_id: 대상 entry id
            label_t5_ret: t+5min forward return
            price_t5_snapshot: t+5min price
            source: backfill 원천 (mode_b_stage_1_rollup | synth_audit_log | manual). default 운영.

        Returns:
            업데이트된 entry 수.

        Raises:
            RuntimeError: 18:00 KST 이전 호출 (PIT-Safety 위반).
        """
        now = now_kst()
        if now.hour < self._pit_safe_hour:
            raise RuntimeError(
                f"PIT_SAFETY_VIOLATION: backfill_label 은 "
                f"{self._pit_safe_hour}:00 KST 이후 전용. "
                f"현재 {now.strftime('%H:%M')}. 불변 원칙 1."
            )

        if not self._log_path.exists():
            return 0

        # 2026-05-09 P1 fix: backfill 시각 KST ISO8601 + source 메타.
        backfilled_at_iso = now.isoformat()

        updated_lines = []
        update_count = 0
        with self._log_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                    if obj.get("decision_id") == decision_id:
                        obj["label_t5_ret"] = label_t5_ret
                        obj["price_t5_snapshot"] = price_t5_snapshot
                        # 2026-05-09 P1 fix: 두 필드 동시 기록.
                        obj["label_backfilled_at"] = backfilled_at_iso
                        obj["label_backfill_source"] = source
                        update_count += 1
                    updated_lines.append(json.dumps(obj, ensure_ascii=False) + "\n")
                except json.JSONDecodeError as e:
                    logger.warning("[audit_logger] 잘못된 JSONL 라인: %s", e)
                    updated_lines.append(line)

        tmp_path = Path(str(self._log_path) + ".tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as fh:
                fh.writelines(updated_lines)
            tmp_path.rename(self._log_path)
        except Exception as e:
            logger.error("[audit_logger] backfill_label atomic write 실패: %s", e)
            if tmp_path.exists():
                tmp_path.unlink()
            raise

        logger.info(
            "[audit_logger] backfill: decision_id=%s, 업데이트 %d 건",
            decision_id, update_count,
        )
        return update_count

    def read_entries(self) -> list[dict]:
        """전체 entry 리스트 반환 (ModeBPerformanceAggregator 입력)."""
        if not self._log_path.exists():
            return []
        entries = []
        with self._log_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError as e:
                    logger.warning("[audit_logger] 파싱 실패: %s", e)
        return entries
