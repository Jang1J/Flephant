"""C5 AgentReportContract 공통 부모 클래스 + SSOT 정렬.

api_contracts.md 공통 규약 message_taxonomy 1:1 매핑:
  - VALID_PUBLISH_CHANNELS: L53-67 publish_channels (15종 Message Pool 채널)
  - VALID_REPORT_TYPES: L76-81 report_types (5종 C5 AgentReportContract type)

Agent별:
  - ALLOWED_PUBLISH_CHANNELS: 각 Agent가 publish 가능한 subset (하위 클래스 override 필수)
  - report(report_type, payload): C5 AgentReport 생성. 하위 클래스 구현.

설계 근거: architect 판단 2026-04-20 (옵션 C, 2단계 검증). publish_channels는
Message Pool 채널(실시간 broadcast), report_types는 에이전트 산출물 유형 (KB 저장/재사용).
두 개념이 다르므로 별도 상수로 관리.
"""
from __future__ import annotations

from typing import Any

from src.utils.time_utils import now_kst

_CHANNEL_ACTION_TYPE = {
    "news_signal": "signal",
    "dart_alert": "alert",
    "sentiment_update": "signal",
    "theme_score": "signal",
    "risk_warning": "alert",
    "regime_change": "regime_change",
    "veto_recommendation": "veto_recommendation",
    "quant_signal": "signal",
    "quant_alert": "alert",
    "anomaly_detected": "alert",
    "investor_flow_alert": "alert",
    "debate_resolution": "resolution",
    "pairwise_ranking": "resolution",
    "final_decision": "resolution",
    "uncertainty_signal": "signal",
}

_VALID_MESSAGE_RISK_LEVELS = {"low", "medium", "high", None}


class AgentBase:
    """모든 에이전트의 공통 부모.

    Subclass 책임:
      1. `ALLOWED_PUBLISH_CHANNELS`를 class-level frozenset으로 override
         (api_contracts.md의 Agent 항목 publishes와 1:1 대응)
      2. `report(report_type, payload) -> dict` 구현

    Subclass가 `ALLOWED_PUBLISH_CHANNELS`를 override하지 않으면 publish 시
    ValueError 발생 (guard).
    """

    # api_contracts.md L53-67 message_taxonomy.publish_channels (SSOT, 15종)
    VALID_PUBLISH_CHANNELS: frozenset[str] = frozenset(
        {
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
            "uncertainty_signal",
        }
    )

    # api_contracts.md L76-81 message_taxonomy.report_types (SSOT, 5종 C5 AgentReport type)
    VALID_REPORT_TYPES: frozenset[str] = frozenset(
        {
            "news_signal",
            "risk_warning",
            "quant_signal",
            "investor_flow_alert",
            "theme_score",
        }
    )

    # Subclass override 필수. 비어있으면 publish() 금지.
    ALLOWED_PUBLISH_CHANNELS: frozenset[str] = frozenset()

    def report(self, report_type: str, payload: dict) -> dict:
        """C5 AgentReport 생성. 하위 클래스에서 구현.

        report_type은 VALID_REPORT_TYPES 또는 ALLOWED_PUBLISH_CHANNELS 내에서만.
        실제 검증 경로는 하위 클래스 구현에 위임.
        """
        raise NotImplementedError("하위 클래스에서 구현 필요")

    def publish(self, channel: str, payload: dict) -> dict:
        """Message Pool publish 헬퍼. ALLOWED_PUBLISH_CHANNELS subset 검증.

        Returns: C4 MessagePool 필수 필드를 포함한 publishable dict.
        """
        cls_name = type(self).__name__
        # 1. Subclass가 ALLOWED_PUBLISH_CHANNELS 선언했는가
        if not self.ALLOWED_PUBLISH_CHANNELS:
            raise ValueError(
                f"{cls_name}.ALLOWED_PUBLISH_CHANNELS 미선언. override 필수."
            )
        # 2. Subclass가 전역 subset 유지하는가
        if not self.ALLOWED_PUBLISH_CHANNELS.issubset(self.VALID_PUBLISH_CHANNELS):
            extra = self.ALLOWED_PUBLISH_CHANNELS - self.VALID_PUBLISH_CHANNELS
            raise ValueError(
                f"{cls_name}.ALLOWED_PUBLISH_CHANNELS SSOT 위반. "
                f"publish_channels 외 채널: {sorted(extra)}"
            )
        # 3. 호출된 channel이 Agent publishes 내인가
        if channel not in self.ALLOWED_PUBLISH_CHANNELS:
            raise ValueError(
                f"{cls_name}.publish: channel={channel} not allowed. "
                f"허용: {sorted(self.ALLOWED_PUBLISH_CHANNELS)}"
            )
        message = {
            "channel": channel,
            "agent": cls_name,
            "payload": payload,
            "content": self._content_from_payload(payload),
            "cause_by": cls_name,
            "sent_from": cls_name,
            "priority": str(payload.get("priority", "normal")),
            "confidence": float(payload.get("confidence", 0.5)),
            "reasoning": str(
                payload.get("reasoning")
                or payload.get("narrative")
                or payload.get("description")
                or ""
            ),
            "scope": str(payload.get("scope") or payload.get("ticker") or "market"),
            "action_type": _CHANNEL_ACTION_TYPE.get(channel, "signal"),
            "timestamp": str(payload.get("timestamp") or payload.get("ts") or now_kst().isoformat()),
        }
        risk_level = payload.get("risk_level")
        if risk_level in _VALID_MESSAGE_RISK_LEVELS:
            message["risk_level"] = risk_level
        return message

    @staticmethod
    def _content_from_payload(payload: dict[str, Any]) -> str:
        """MessagePool `content`를 payload에서 안정적으로 구성."""
        for key in ("content", "narrative", "reasoning", "title", "summary"):
            value = payload.get(key)
            if value:
                return str(value)
        return str(payload)
