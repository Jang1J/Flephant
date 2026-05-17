"""C11 EventAdmissionContract. 중복 제거 / stale drop / dead letter 로그.

PIT-Safety(불변 원칙 1): expires_at 비교는 KST timezone 일관 사용.
하드코딩 금지(불변 원칙 5): 모든 임계값은 risk_config.yaml event_admission 에서 로드.
"""
from __future__ import annotations

import heapq
import json
import time as time_module
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger
from src.utils.safe_cast import safe_bool, safe_int

logger = get_logger("event_admission")

_KST = ZoneInfo("Asia/Seoul")

# config fallback 전용 상수. 본 로직에서 직접 사용 금지.
_DEFAULT_DEDUPE_TTL_SEC = 300

# comparator rank 매핑 (C11 spec SSOT)
_PRIORITY_RANK: dict[str, int] = {"urgent": 0, "normal": 1, "low": 2}
_TRIGGER_RANK: dict[str, int] = {
    "vol_spike": 0,
    "dart_alert": 1,
    "news_detected": 2,
    "regime_change": 3,
    "anomaly": 4,
}
_SCOPE_RANK: dict[str, int] = {"market": 0, "sector": 1, "ticker": 2}

# event_type → trigger_type 변환 (C2 event_type → C11 trigger_order)
# S2-8 Risk Fast sidecar 구현 시 risk_config.yaml trigger_catalog 로 이관 예정.
# 현재는 C11 comparator.trigger_order 매핑 하드 코딩 (공식 trigger_order enum).
_EVENT_TYPE_TO_TRIGGER: dict[str, str] = {
    "investor_flow": "vol_spike",   # 수급 급변은 vol_spike 수준
    "dart": "dart_alert",
    "news": "news_detected",
    "regime": "regime_change",
    "macro": "regime_change",
    "us_market": "regime_change",
    "community": "anomaly",
    "price_snapshot": "vol_spike",  # S5-1 C16 WatchUniverseSnapshot 편입 판정용
}


@dataclass(order=True)
class _PrioritizedEvent:
    """Priority Queue 내부 entry. heapq 용 order key."""

    sort_key: tuple  # (priority_rank, trigger_rank, scope_rank, -ts_epoch)
    event: dict = field(compare=False)


class EventAdmission:
    """C11 EventAdmissionContract 집행자.

    3 필터 + 백로그 + dead_letter_log:
      1. dedupe (event_id + supersedes TTL)
      2. stale_drop (expires_at < admission asof/now)
      3. priority comparator 정렬 후 backlog 상한 초과 시 lowest drop

    config SSOT: risk_config.yaml event_admission.
    모든 임계값(TTL, max_backlog, jobs_per_minute) yaml 경유. 하드코딩 금지.
    """

    def __init__(
        self,
        config: dict | None = None,
        dead_letter_path: Path | None = None,
    ) -> None:
        cfg = (config or config_load()).get("event_admission", {})
        self._max_backlog: int = int(cfg.get("max_backlog", 3))
        self._max_jobs_per_min: int = int(cfg.get("max_cold_path_jobs_per_minute", 10))
        self._jobs_window_sec: int = int(cfg.get("jobs_per_minute_window_sec", 60))
        self._stale_drop: bool = safe_bool(cfg.get("stale_drop", True), default=True)
        self._dedupe_ttl_sec: int = int(cfg.get("dedupe_ttl_sec", _DEFAULT_DEDUPE_TTL_SEC))
        self._comparator_sort_key: list[str] = list(
            cfg.get("comparator_sort_key", ["priority", "trigger_type", "scope", "recency"])
        )
        self._dead_letter_path: Path = Path(
            dead_letter_path or cfg.get("dead_letter_path", "artifacts/dead_letter.jsonl")
        )
        self._dead_letter_retention_days: int = safe_int(
            cfg.get("dead_letter_retention_days", cfg.get("retention_days")),
            default=30,
            min_value=0,
        )
        self._dead_letter_path.parent.mkdir(parents=True, exist_ok=True)

        # In-memory dedupe cache: {event_id: expiry_epoch}
        self._seen: dict[str, float] = {}
        # supersedes cache: {event_id: expiry_epoch}
        self._seen_supersedes: dict[str, float] = {}

        # Priority Queue (heapq, lowest sort_key = highest priority)
        self._backlog: list[_PrioritizedEvent] = []

        # jobs_per_minute rolling window: 최근 admitted ts 목록 (epoch)
        self._recent_admitted_ts: list[float] = []

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def admit(self, event: dict) -> bool:
        """이벤트 수용 여부 판정.

        Returns:
            True: 수용 (backlog 삽입).
            False: 거부 (dead_letter_log 기록).

        거부 사유:
          - DUPLICATE_EVENT_ID: 이미 dedupe cache 에 존재
          - SUPERSEDED: supersedes 참조가 이미 처리됨
          - STALE: expires_at < now
          - BACKLOG_OVERFLOW: max_backlog 초과 + 본인이 lowest priority
          - JOBS_PER_MINUTE_CAP: 최근 60초 처리 수 >= max
        """
        now_epoch = time_module.time()
        self._clean_expired_seen(now_epoch)

        event_id: str = event.get("event_id", "")

        # --- 필터 1: dedupe (event_id) ---
        if event_id and event_id in self._seen:
            self._write_dead_letter(event, "DUPLICATE_EVENT_ID")
            return False

        # --- 필터 1b: supersedes 체크 ---
        supersedes: str | None = event.get("supersedes")
        if supersedes and supersedes in self._seen_supersedes:
            self._write_dead_letter(event, "SUPERSEDED")
            return False

        # --- 필터 2: stale drop ---
        if self._stale_drop:
            expires_at_str: str | None = event.get("expires_at")
            if expires_at_str:
                try:
                    expires_dt = self._parse_kst_datetime(expires_at_str)
                    if expires_dt is None:
                        raise ValueError("expires_at parse returned None")
                    reference_dt = (
                        self._parse_kst_datetime(event.get("asof")) or datetime.now(_KST)
                    )
                    if expires_dt < reference_dt:
                        self._write_dead_letter(event, "STALE")
                        return False
                except Exception as e:
                    logger.warning("expires_at 파싱 실패, stale 체크 생략: %s err=%s", expires_at_str, e)

        # --- 필터 3: jobs_per_minute cap ---
        cutoff = now_epoch - self._jobs_window_sec
        self._recent_admitted_ts = [ts for ts in self._recent_admitted_ts if ts > cutoff]
        if len(self._recent_admitted_ts) >= self._max_jobs_per_min:
            self._write_dead_letter(event, "JOBS_PER_MINUTE_CAP")
            return False

        # --- 필터 4: backlog overflow ---
        sort_key = self._compute_sort_key(event)
        entry = _PrioritizedEvent(sort_key=sort_key, event=event)

        if len(self._backlog) >= self._max_backlog:
            # 현재 backlog 최저 우선순위 entry 찾기 (heapq 최솟값 = 최고 우선순위)
            # max(sort_key)가 lowest priority
            lowest = max(self._backlog, key=lambda x: x.sort_key)
            if entry.sort_key >= lowest.sort_key:
                # 신규 이벤트가 backlog 최저보다 우선순위 낮거나 같음 → drop
                self._write_dead_letter(event, "BACKLOG_OVERFLOW")
                return False
            else:
                # 신규 이벤트가 더 높은 우선순위 → 최저 entry 제거 후 삽입
                self._backlog.remove(lowest)
                heapq.heapify(self._backlog)
                self._write_dead_letter(lowest.event, "BACKLOG_OVERFLOW")

        heapq.heappush(self._backlog, entry)

        # dedupe cache 등록
        if event_id:
            expiry_epoch = now_epoch + self._dedupe_ttl_sec
            self._seen[event_id] = expiry_epoch
            self._seen_supersedes[event_id] = expiry_epoch

        self._recent_admitted_ts.append(now_epoch)
        logger.info("허용: event_id=%s priority=%s", event_id, event.get("priority", "unknown"))
        return True

    def pop_next(self) -> dict | None:
        """우선순위 최상위 이벤트를 backlog 에서 pop.

        Returns:
            이벤트 dict, 또는 backlog 빔 시 None.
        """
        if not self._backlog:
            return None
        entry = heapq.heappop(self._backlog)
        return entry.event

    def backlog_size(self) -> int:
        """현재 backlog 길이."""
        return len(self._backlog)

    def flush_dead_letter(self) -> int:
        """dead letter 버퍼 강제 flush. 기록 건수 반환.

        현 구현은 admit() 시 즉시 append-only 로 파일 기록하므로 0 반환.
        """
        return 0

    # ------------------------------------------------------------------
    # private helpers
    # ------------------------------------------------------------------

    def _clean_expired_seen(self, now_epoch: float) -> None:
        """dedupe cache 에서 TTL 만료 entry 제거."""
        expired = [eid for eid, exp in self._seen.items() if exp <= now_epoch]
        for eid in expired:
            del self._seen[eid]
        expired_supersedes = [
            eid for eid, exp in self._seen_supersedes.items() if exp <= now_epoch
        ]
        for eid in expired_supersedes:
            del self._seen_supersedes[eid]

    @staticmethod
    def _parse_kst_datetime(value: object) -> datetime | None:
        """ISO datetime을 KST aware datetime으로 정규화."""
        if value is None:
            return None
        try:
            dt = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=_KST)
        return dt.astimezone(_KST)

    def _compute_sort_key(self, event: dict) -> tuple:
        """comparator sort key 생성.

        C11 spec:
          priority_rank: urgent=0, normal=1, low=2 (낮을수록 우선)
          trigger_rank: vol_spike=0 > dart_alert=1 > news_detected=2 > regime_change=3 > anomaly=4
          scope_rank: market=0 > sector=1 > ticker=2
          recency: -ts_epoch (최신일수록 낮은 값)

        Returns:
            tuple of 4 elements for heapq ordering.
        """
        priority_str: str = event.get("priority", "low")
        priority_rank = _PRIORITY_RANK.get(priority_str, 2)

        # trigger_type 은 event 직접 제공 or event_type → 변환
        trigger_type: str = event.get("trigger_type", "")
        if not trigger_type:
            etype = event.get("event_type", "")
            trigger_type = _EVENT_TYPE_TO_TRIGGER.get(etype, "anomaly")
        trigger_rank = _TRIGGER_RANK.get(trigger_type, 4)

        # scope: "market" | "sector:name" | "ticker:code"
        scope_str: str = event.get("scope", "ticker")
        scope_prefix = scope_str.split(":")[0] if ":" in scope_str else scope_str
        scope_rank = _SCOPE_RANK.get(scope_prefix, 2)

        # recency: occurred_at → epoch. 없으면 0 (낮은 recency)
        recency_neg = 0.0
        occurred_at_str: str | None = event.get("occurred_at")
        if occurred_at_str:
            try:
                dt = datetime.fromisoformat(occurred_at_str)
                recency_neg = -dt.timestamp()
            except Exception as e:
                logger.debug("[event_admission] occurred_at 파싱 실패, recency=0 fallback: ts=%s err=%s", occurred_at_str, e)

        return (priority_rank, trigger_rank, scope_rank, recency_neg)

    def _prune_dead_letter(self) -> None:
        """C11 retention_days 초과 dead_letter JSONL 라인 제거."""
        if self._dead_letter_retention_days <= 0 or not self._dead_letter_path.exists():
            return
        cutoff = datetime.now(_KST) - timedelta(days=self._dead_letter_retention_days)
        kept: list[str] = []
        pruned = 0
        try:
            lines = self._dead_letter_path.read_text(encoding="utf-8").splitlines()
            for line in lines:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    ts = datetime.fromisoformat(str(entry.get("timestamp") or ""))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=_KST)
                except Exception as e:
                    _ = e
                    kept.append(line)
                    continue
                if ts >= cutoff:
                    kept.append(line)
                else:
                    pruned += 1
            if pruned:
                payload = "\n".join(kept)
                if payload:
                    payload += "\n"
                self._dead_letter_path.write_text(payload, encoding="utf-8")
        except Exception as e:
            logger.warning("dead_letter retention 정리 실패: err=%s", e)

    def _write_dead_letter(self, event: dict, reason: str) -> None:
        """C11 format 준수: {timestamp, event_id, drop_reason, original_event_ref}
        """
        entry = {
            "timestamp": datetime.now(_KST).isoformat(),
            "event_id": event.get("event_id", "UNKNOWN"),
            "drop_reason": reason,
            "original_event_ref": event.get("raw_payload_ref") or event.get("event_id"),
        }
        try:
            self._prune_dead_letter()
            with open(self._dead_letter_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("dead_letter 기록 실패: reason=%s err=%s", reason, e)
        logger.info("거부 기록: %s event_id=%s", reason, entry["event_id"])
