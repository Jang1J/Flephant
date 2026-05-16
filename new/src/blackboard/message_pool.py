"""C4 SharedMessagePool. publish/subscribe/ack/supersede/expire.

v3 (2026-04-21, Sprint 2 S2-1): 전체 실구현. api_contracts.md C4 SSOT 준수.

Import 경로 주의:
    이 모듈은 `from src.utils.config_loader import load` 절대 경로 사용.
    pytest 는 `new/tests/conftest.py` 에서 `sys.path.insert(0, str(Path(__file__).parent.parent))` 로
    `new/` 를 경로에 추가하므로 정상 동작.
    `new/jobs/` 에서 직접 실행 시에도 동일 conftest 적용 또는 `sys.path.insert(0, "new/")` 명시 필요.
    단독 실행 테스트: `cd new && python -m src.blackboard.message_pool` 권장 (리포 루트 기준).

C4 SharedMessagePoolContract 전체 구현 (api_contracts.md L289~329 SSOT).
- 5 operations: publish / subscribe / ack / supersede / expire
- message_schema 17 필드 validation
- delivery_semantics: at-least-once + subscriber_filtering + dependency_activation
- config SSOT: risk_config.yaml message_pool (없으면 defaults)

채널명은 api_contracts.md message_taxonomy.publish_channels 준수 (15개).
in-memory only. 영속화는 Sprint 4 S4-7 Persistent Cache.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from src.utils.config_loader import load as config_load
from src.utils.id_factory import generate_message_id
from src.utils.logger import get_logger
from src.utils.safe_cast import safe_confidence, safe_int
from src.utils.time_utils import now_kst

logger = get_logger("message_pool")

_KST = ZoneInfo("Asia/Seoul")

# C4 message_schema 17 필드 (api_contracts.md L300~320 SSOT)
_MESSAGE_REQUIRED_FIELDS: frozenset[str] = frozenset({
    "content", "cause_by", "sent_from", "priority", "confidence",
    "reasoning", "scope", "action_type", "timestamp",
})
_MESSAGE_OPTIONAL_FIELDS: frozenset[str] = frozenset({
    "message_id", "send_to", "evidence_ids", "uncertainty", "prediction",
    "risk_level", "ttl", "expires_at", "event_id", "occurred_at", "asof",
    "supersedes", "portfolio_patch_id",
})

# publish_channels SSOT (api_contracts.md message_taxonomy L57~72): 15개
_VALID_CHANNELS: frozenset[str] = frozenset({
    "news_signal",
    "dart_alert",
    "sentiment_update",
    "theme_score",
    "risk_warning",
    "regime_change",
    "veto_recommendation",
    "quant_signal",
    "quant_alert",
    "anomaly_detected",
    "investor_flow_alert",
    "debate_resolution",
    "pairwise_ranking",
    "final_decision",
    "uncertainty_signal",   # 2026-04-21 추가: api_contracts.md message_taxonomy L72
})

# action_types SSOT (api_contracts.md message_taxonomy L74~79): 5개
_VALID_ACTION_TYPES: frozenset[str] = frozenset({
    "signal",
    "alert",
    "veto_recommendation",
    "regime_change",
    "resolution",
})

# priority SSOT (api_contracts.md C4 message_schema L307)
_VALID_PRIORITIES: frozenset[str] = frozenset({"urgent", "normal", "low"})

# risk_level SSOT (api_contracts.md C4 message_schema L312): None 허용
_VALID_RISK_LEVELS: frozenset[str | None] = frozenset({"low", "medium", "high", None})


def _parse_iso_datetime_field(message: dict, field_name: str) -> datetime:
    """publish-time ISO datetime validation for C4 timestamp fields."""
    raw = message.get(field_name)
    if not isinstance(raw, str):
        raise ValueError(
            f"MESSAGE_SCHEMA_INVALID: {field_name} must be ISO datetime string"
        )
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as e:
        raise ValueError(
            f"MESSAGE_SCHEMA_INVALID: {field_name} invalid ISO datetime '{raw}'"
        ) from e
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_KST)
    return parsed


def _validate_pit_trace_fields(message: dict) -> None:
    if "occurred_at" not in message or "asof" not in message:
        return
    occurred_at = _parse_iso_datetime_field(message, "occurred_at")
    asof = _parse_iso_datetime_field(message, "asof")
    if occurred_at > asof:
        raise ValueError(
            "MESSAGE_PIT_VIOLATION: occurred_at "
            f"{occurred_at.isoformat()} > asof {asof.isoformat()}"
        )


@dataclass
class _MessageEntry:
    """내부 저장소 entry. 외부 공개 금지."""

    message_id: str
    channel: str
    message: dict
    ack_count: int = 0
    acked_by: set = field(default_factory=set)
    created_at: datetime = field(default_factory=now_kst)
    superseded_by: str | None = None


class MessagePool:
    """C4 SharedMessagePoolContract 구현.

    delivery_semantics:
      mode: at-least-once
      subscriber_filtering: filter_fn 으로 handler 별 메시지 선택.
      dependency_activation: FDA 패턴. 여러 채널 모두 publish 완료 시 callback 활성화.

    config 로드 순서:
      1. risk_config.yaml message_pool 섹션
      2. 없으면 default_ttl_sec=300, max_pool_size=10000

    expire() 는 publish 시 pool overflow 시 자동 호출. 외부에서 직접 호출도 가능.
    """

    def __init__(self, config: dict | None = None) -> None:
        cfg = (config or config_load()).get("message_pool", {})
        self._default_ttl_sec: int = int(cfg.get("default_ttl_sec", 300))
        self._max_pool_size: int = int(cfg.get("max_pool_size", 10000))

        # 저장소: message_id → entry
        self._messages: dict[str, _MessageEntry] = {}
        # 채널별 message_id 목록 (순서 보존)
        self._by_channel: dict[str, list[str]] = defaultdict(list)

        # 구독자: channel → [(handler, filter_fn)]
        self._subscribers: dict[str, list[tuple[Callable, Callable | None]]] = defaultdict(list)

        # dependency_activation 레지스트리
        # key → {"required": set, "callback": Callable, "contexts": {correlation_key: state}}
        self._dependencies: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # 5 Operations (C4 SSOT)
    # ------------------------------------------------------------------

    def publish(self, channel: str, message: dict) -> str:
        """채널에 메시지 게시. message_id 반환.

        동작:
          1. channel / 필수필드 / enum 검증
          2. message_id 자동 생성 (없을 때)
          3. expires_at 자동 계산 (ttl 경유)
          4. _messages + _by_channel 저장
          5. 구독자 at-least-once 호출
          6. dependency_activation 체크

        Raises:
          ValueError: 채널 미등록, 필수 필드 누락, enum 위반, supersede 대상 없음
          RuntimeError: pool overflow (expire 후 재시도에도 꽉 찬 경우)
        """
        # 1a. 채널 검증 (C4 errors: INVALID_SCOPE)
        if channel not in _VALID_CHANNELS:
            raise ValueError(f"INVALID_SCOPE: channel='{channel}' not in _VALID_CHANNELS")

        # 1b. 필수 필드 검증
        missing = _MESSAGE_REQUIRED_FIELDS - set(message.keys())
        if missing:
            raise ValueError(f"MESSAGE_SCHEMA_INVALID: missing fields {sorted(missing)}")

        # 1c. enum + scalar 검증
        message["confidence"] = safe_confidence(message.get("confidence", 0.5))

        priority = message.get("priority")
        if priority not in _VALID_PRIORITIES:
            raise ValueError(f"MESSAGE_SCHEMA_INVALID: priority='{priority}' not in {sorted(_VALID_PRIORITIES)}")

        action_type = message.get("action_type")
        if action_type not in _VALID_ACTION_TYPES:
            raise ValueError(f"MESSAGE_SCHEMA_INVALID: action_type='{action_type}' not in {sorted(_VALID_ACTION_TYPES)}")

        risk_level = message.get("risk_level")
        if risk_level not in _VALID_RISK_LEVELS:
            raise ValueError(f"MESSAGE_SCHEMA_INVALID: risk_level='{risk_level}' not valid")

        _parse_iso_datetime_field(message, "timestamp")
        if "expires_at" in message:
            _parse_iso_datetime_field(message, "expires_at")
        for trace_ts_field in ("occurred_at", "asof"):
            if trace_ts_field in message:
                _parse_iso_datetime_field(message, trace_ts_field)
        _validate_pit_trace_fields(message)

        # 2. message_id
        message_id = message.get("message_id") or generate_message_id()
        if message_id in self._messages:
            raise ValueError(f"MESSAGE_ID_CONFLICT: message_id='{message_id}' already exists")
        message["message_id"] = message_id

        # 3. expires_at + timestamp 자동 계산
        if "expires_at" not in message:
            ttl = safe_int(message.get("ttl", self._default_ttl_sec), default=self._default_ttl_sec, min_value=1)
            now = now_kst()
            message["expires_at"] = (now + timedelta(seconds=ttl)).isoformat()
            if "timestamp" not in message:
                message["timestamp"] = now.isoformat()
        elif "timestamp" not in message:
            message["timestamp"] = now_kst().isoformat()

        # 4. 저장 (overflow 체크)
        if len(self._messages) >= self._max_pool_size:
            removed = self.expire()
            logger.info("[message_pool] pool overflow → expire 실행: 제거=%d", removed)
            if len(self._messages) >= self._max_pool_size:
                raise RuntimeError(
                    f"POOL_OVERFLOW: pool_size={len(self._messages)} >= max_pool_size={self._max_pool_size}"
                )

        entry = _MessageEntry(message_id=message_id, channel=channel, message=message)
        self._messages[message_id] = entry
        self._by_channel[channel].append(message_id)

        # 5. subscriber at-least-once
        for handler, filter_fn in self._subscribers[channel]:
            if filter_fn is None or filter_fn(message):
                try:
                    handler(message)
                except Exception as e:
                    logger.warning(
                        "[message_pool] 구독자 handler 예외: channel=%s message_id=%s err=%s",
                        channel, message_id, e,
                    )

        # 6. dependency_activation
        self._check_dependencies(channel)

        logger.debug("[message_pool] publish: message_id=%s channel=%s", message_id, channel)
        return message_id

    def subscribe(
        self,
        channel: str,
        handler: Callable[[dict], None],
        filter_fn: Callable[[dict], bool] | None = None,
    ) -> None:
        """채널 구독 등록.

        at-least-once: 동일 메시지를 여러 handler 가 각각 수신.
        filter_fn: None 이면 전체 메시지 수신. 있으면 True 반환 메시지만 수신.

        Raises:
          ValueError: 채널 미등록 (INVALID_SCOPE)
        """
        if channel not in _VALID_CHANNELS:
            raise ValueError(f"INVALID_SCOPE: channel='{channel}' not registered")
        self._subscribers[channel].append((handler, filter_fn))
        logger.info(
            "[message_pool] 구독 등록: channel=%s handler=%s",
            channel, getattr(handler, "__name__", repr(handler)),
        )

    def remove_subscriber(self, channel: str, handler: Callable[[dict], None]) -> int:
        """채널에서 특정 handler 구독을 제거하고 제거 건수를 반환."""
        if channel not in _VALID_CHANNELS:
            raise ValueError(f"INVALID_SCOPE: channel='{channel}' not registered")
        subscribers = self._subscribers[channel]
        kept = [(hh, ff) for hh, ff in subscribers if hh is not handler]
        removed = len(subscribers) - len(kept)
        self._subscribers[channel] = kept
        if removed:
            logger.info(
                "[message_pool] 구독 해제: channel=%s handler=%s removed=%d",
                channel, getattr(handler, "__name__", repr(handler)), removed,
            )
        return removed

    def ack(self, message_id: str, ack_by: str = "unknown") -> None:
        """메시지 처리 완료 확인.

        같은 (message_id, ack_by) 조합은 중복 카운트 방지.
        ack_count 는 서로 다른 ack_by 합계.

        Raises:
          ValueError: message_id 없을 때 (ACK_TARGET_NOT_FOUND)
          ValueError: 메시지 이미 만료됐을 때 (MESSAGE_EXPIRED, C4 errors)
        """
        entry = self._messages.get(message_id)
        if entry is None:
            raise ValueError(f"ACK_TARGET_NOT_FOUND: message_id='{message_id}'")

        # 2026-04-21 감사 반영: 만료 메시지 ack 차단 (C4 errors.MESSAGE_EXPIRED)
        exp_str = entry.message.get("expires_at")
        if exp_str:
            try:
                exp = datetime.fromisoformat(exp_str)
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=_KST)
                if exp < now_kst():
                    raise ValueError(f"MESSAGE_EXPIRED: {message_id} expired at {exp_str}")
            except ValueError:
                raise
            except Exception as e:
                logger.debug("[message_pool] expires_at 파싱 실패 (ack): %s err=%s", exp_str, e)

        if ack_by not in entry.acked_by:
            entry.acked_by.add(ack_by)
            entry.ack_count += 1
        logger.debug(
            "[message_pool] ack: message_id=%s ack_by=%s total_count=%d",
            message_id, ack_by, entry.ack_count,
        )

    def supersede(self, old_id: str, new_id: str) -> None:
        """기존 메시지를 새 메시지로 대체. C4 supersede 연산.

        old 는 superseded_by 마킹 → get_active_messages() 에서 제외.
        new 의 message["supersedes"] = old_id 로 연결.

        Raises:
          ValueError: old_id 또는 new_id 미존재 (SUPERSEDE_CONFLICT)
        """
        old = self._messages.get(old_id)
        new = self._messages.get(new_id)
        if old is None:
            raise ValueError(f"SUPERSEDE_CONFLICT: old_id='{old_id}' not found")
        if new is None:
            raise ValueError(f"SUPERSEDE_CONFLICT: new_id='{new_id}' not found")
        old.superseded_by = new_id
        new.message["supersedes"] = old_id
        logger.info("[message_pool] supersede: old=%s → new=%s", old_id, new_id)

    def expire(self, now: datetime | None = None) -> int:
        """만료된 메시지 정리. 제거 건수 반환.

        expires_at 파싱 실패한 메시지는 스킵 (안전 우선).
        now 미지정 시 현재 KST 기준.
        """
        ts_now = now or now_kst()
        to_remove: list[str] = []

        for mid, entry in list(self._messages.items()):
            exp_str = entry.message.get("expires_at")
            if not exp_str:
                continue
            try:
                exp = datetime.fromisoformat(exp_str)
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=_KST)
            except Exception as e:
                logger.debug("[message_pool] expires_at 파싱 실패: %s err=%s", exp_str, e)
                continue
            if exp < ts_now:
                to_remove.append(mid)

        for mid in to_remove:
            entry = self._messages.pop(mid)
            self._by_channel[entry.channel] = [
                x for x in self._by_channel[entry.channel] if x != mid
            ]
        if to_remove:
            self._prune_dependency_contexts(set(to_remove))

        if to_remove:
            logger.info("[message_pool] expire: 제거=%d", len(to_remove))
        return len(to_remove)

    def _prune_dependency_contexts(self, removed_ids: set[str]) -> None:
        for dep in self._dependencies.values():
            contexts = dep.get("contexts", {})
            for corr_key, context in list(contexts.items()):
                latest_ids = context.get("latest_message_ids", {})
                removed_channels = {
                    channel
                    for channel, message_id in latest_ids.items()
                    if message_id in removed_ids
                }
                if not removed_channels:
                    continue
                context["seen"].difference_update(removed_channels)
                for channel in removed_channels:
                    context["latest_messages"].pop(channel, None)
                    latest_ids.pop(channel, None)
                if not context["seen"]:
                    contexts.pop(corr_key, None)

    # ------------------------------------------------------------------
    # dependency_activation (C4 delivery_semantics)
    # ------------------------------------------------------------------

    def register_dependency(
        self,
        activation_key: str,
        required_channels: set[str],
        callback: Callable[[dict], None],
    ) -> None:
        """dependency_activation 등록.

        required_channels 각각에 1개 이상 publish 발생 시 callback 호출.
        callback 인수: {channel: latest_message_dict} dict.

        FDA Cold Path 패턴 예시:
          pool.register_dependency(
              "fda_cold_activate",
              {"news_signal", "risk_warning", "quant_signal"},
              fda_cold_path_handler,
          )
        """
        self._dependencies[activation_key] = {
            "required": set(required_channels),
            "callback": callback,
            "contexts": {},
        }
        logger.info(
            "[message_pool] dependency 등록: key=%s requires=%s",
            activation_key, sorted(required_channels),
        )

    @staticmethod
    def _dependency_correlation_key(message: dict[str, Any]) -> str:
        for field_name in ("event_id", "portfolio_patch_id"):
            value = message.get(field_name)
            if value:
                return f"{field_name}:{value}"
        return f"scope:{message.get('scope', '')}"

    def _check_dependencies(self, just_published_channel: str) -> None:
        """publish 직후 호출. 모든 required channel 수신 완료 시 callback."""
        for key, dep in self._dependencies.items():
            if just_published_channel not in dep["required"]:
                continue

            # 최신 메시지 캐시
            ids = self._by_channel[just_published_channel]
            if ids:
                last_id = ids[-1]
                message = self._messages[last_id].message
                corr_key = self._dependency_correlation_key(message)
                context = dep["contexts"].setdefault(
                    corr_key,
                    {"seen": set(), "latest_messages": {}, "latest_message_ids": {}},
                )
                context["seen"].add(just_published_channel)
                context["latest_messages"][just_published_channel] = message
                context["latest_message_ids"][just_published_channel] = last_id
            else:
                continue

            if context["seen"] == dep["required"]:
                try:
                    dep["callback"](context["latest_messages"])
                    logger.info(
                        "[message_pool] dependency activated: key=%s corr=%s",
                        key,
                        corr_key,
                    )
                except Exception as e:
                    logger.warning(
                        "[message_pool] dependency callback 예외: key=%s err=%s", key, e
                    )
                # 한 사이클 완료 후 context 제거. correlation별 재활성화 지원
                dep["contexts"].pop(corr_key, None)

    # ------------------------------------------------------------------
    # 조회 helpers
    # ------------------------------------------------------------------

    def get_active_messages(self, channel: str | None = None) -> list[dict]:
        """superseded 제외 + 미만료 active 메시지 목록.

        channel 지정 시 해당 채널만, None 이면 전체.
        만료(expires_at < 현재) 메시지는 C4 at-least-once semantics 상 제외.
        """
        ts_now = now_kst()
        result: list[dict] = []
        for entry in self._messages.values():
            if entry.superseded_by is not None:
                continue
            if channel is not None and entry.channel != channel:
                continue
            # 만료 검사 (expires_at 파싱 실패 시 안전 우선으로 포함)
            exp_str = entry.message.get("expires_at")
            if exp_str:
                try:
                    exp = datetime.fromisoformat(exp_str)
                    if exp.tzinfo is None:
                        exp = exp.replace(tzinfo=_KST)
                    if exp < ts_now:
                        continue
                except Exception as e:
                    logger.debug("[message_pool] get_active expires_at 파싱 실패: %s err=%s", exp_str, e)
            result.append(entry.message)
        return result

    def pool_size(self) -> int:
        """현재 저장된 메시지 수 (superseded 포함)."""
        return len(self._messages)
