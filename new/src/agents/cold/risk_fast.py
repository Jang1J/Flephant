"""Cold Path Risk Fast Agent. trigger_catalog 기반 비LLM 규칙 평가.

S2-8 실구현. Cold Path 이벤트 진입 시 즉시 규칙 매칭 (<50ms, LLM 미호출).
결과: C5 risk_warning publish.

역할 분리:
  - hot/risk_fast.py: Hot Path bar_buffer 기반 가격/거래량 이상 탐지 sidecar
  - cold/risk_fast.py (이 파일): Cold Path 이벤트 payload 기반 커뮤니티/수급/공시 규칙 평가

trigger_catalog는 risk_config.yaml에서 로드 (하드코딩 금지, 불변 원칙 5).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from src.agents._base import AgentBase
from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger
from src.utils.pit_guard import PITViolationError, assert_pit_safe
from src.utils.ticker_utils import normalize_ticker

logger = get_logger("risk_fast_cold")

_KST = ZoneInfo("Asia/Seoul")


class RiskAgentFast(AgentBase):
    """Cold Path 규칙 기반 빠른 리스크 평가 에이전트.

    trigger_catalog 6개 규칙 순차 평가. 비LLM, <50ms 목표.
    임계값은 risk_config.yaml trigger_catalog / risk_fast 섹션 경유.
    """

    ALLOWED_PUBLISH_CHANNELS: frozenset[str] = frozenset(
        {"risk_warning", "uncertainty_signal"}
    )

    def __init__(self, pubsub: Any | None = None) -> None:
        """
        pubsub: PubSubBroker (optional). None이면 publish skip.
        """
        self._pubsub = pubsub
        self._rules = self._load_trigger_rules()
        self._thresholds = self._load_thresholds()
        # trigger id → risk_level / stance: yaml trigger_catalog에서 동적 로드
        self._trigger_risk_level: dict[str, str] = {}
        self._trigger_stance: dict[str, str] = {}
        for rule in self._rules:
            rid = rule.get("id", "")
            self._trigger_risk_level[rid] = rule.get("risk_level", "medium")
            self._trigger_stance[rid] = rule.get("stance", "neutral")
        # SLA: risk_config.yaml risk_fast.sla_ms 경유 (불변 원칙 5)
        self._sla_ms = self._load_sla_ms()

    def _publish_to_bus(self, channel: str, payload: dict[str, Any]) -> dict[str, Any]:
        """AgentBase 메시지를 만들고, PubSubBroker가 있으면 실제 MessagePool에 발행."""
        msg = self.publish(channel, payload)
        if self._pubsub is None:
            return msg
        try:
            msg["message_id"] = self._pubsub.publish(channel, msg)
        except Exception as e:
            msg["publish_error"] = str(e)
            logger.warning(
                "[risk_fast_cold] pubsub publish 실패: channel=%s error=%s",
                channel,
                e,
            )
        return msg

    def _load_trigger_rules(self) -> list[dict[str, Any]]:
        """trigger_catalog.rules 로드. trigger_loader 에 위임 (S5-2 DRY refactor).

        filter 없이 전체 rules 반환. admit_candidate action rule 은
        evaluate loop 에서 action 필드 확인 후 skip.
        """
        from src.utils.trigger_loader import load_trigger_rules
        return load_trigger_rules()

    def _load_sla_ms(self) -> float:
        """risk_config.yaml risk_fast.sla_ms 로드 (불변 원칙 5)."""
        try:
            rf = config_load("risk_config.yaml", "risk_fast")
            return float(rf.get("sla_ms", 50))
        except Exception as e:
            logger.warning("[risk_fast_cold] sla_ms 로드 실패, 기본값 50ms 사용: %s", e)
            return 50.0

    def _load_thresholds(self) -> dict[str, float]:
        """risk_config.yaml risk_fast.cold_path 섹션에서 임계값 로드.

        불변 원칙 5: 모든 수치는 yaml SSOT 경유. 하드코딩 금지.
        yaml 로드 실패 시 KeyError / RuntimeError 전파 (시스템 설정 오류 명확화).
        foreign_net_sell_krw: 음수 컨벤션 (-100B 이하 = 대규모 순매도).
        """
        rf = config_load("risk_config.yaml", "risk_fast")
        cold_cfg = rf.get("cold_path")
        if cold_cfg is None:
            raise KeyError(
                "[risk_fast_cold] risk_config.yaml risk_fast.cold_path 섹션 없음. "
                "설정 파일 점검 필요."
            )
        keys = [
            "comm_volume_zscore",
            "comm_sentiment_delta",
            "intraday_return_zscore",
            "foreign_net_sell_krw",
            "news_comm_divergence",
        ]
        result: dict[str, float] = {}
        for key in keys:
            if key not in cold_cfg:
                raise KeyError(
                    f"[risk_fast_cold] risk_fast.cold_path.{key} 누락. "
                    "risk_config.yaml 점검 필요."
                )
            result[key] = float(cold_cfg[key])
        return result

    @staticmethod
    def _event_trace(event: dict[str, Any]) -> dict[str, str]:
        """Cold event trace를 PIT-safe publish payload용으로 정규화한다."""
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        ticker = normalize_ticker(event.get("ticker") or payload.get("ticker"))
        raw_scope = str(event.get("scope") or "").strip()
        if raw_scope.startswith("ticker:"):
            scoped_ticker = normalize_ticker(raw_scope.split(":", 1)[1])
            scope = f"ticker:{scoped_ticker}" if scoped_ticker else "market"
        elif raw_scope:
            scope = raw_scope
        elif ticker:
            scope = f"ticker:{ticker}"
        else:
            scope = "market"
        return {"ticker": ticker, "scope": scope}

    def evaluate(
        self,
        event: dict,
        context: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """Cold Path 이벤트 + 컨텍스트 수치에 대해 trigger_catalog 규칙 평가.

        Args:
            event: C2 EventNormalizeContract 이벤트
                   (event_type, payload, ticker, occurred_at 등)
            context: 부가 수치 딕셔너리 (선택).
              - comm_volume_zscore: 커뮤니티 게시글 z-score
              - comm_sentiment_delta: 감성 점수 30분 변화율
              - intraday_return_zscore: 분봉 수익률 z-score
              - foreign_net_sell_krw: 외국인 순매도 금액 (원)
              - news_comm_divergence: 뉴스-커뮤니티 방향 불일치

        Returns:
            C5 risk_warning 호환 딕셔너리:
              risk_level, stance, fast_rule_match, triggered_rules,
              recommended_action, latency_ms
        """
        t0 = time.perf_counter()
        ctx = context or {}
        triggered: list[dict[str, Any]] = []
        uncertainty_score: float | None = None

        event_type = event.get("event_type", "")
        occurred_at = event.get("occurred_at")
        asof = event.get("asof")
        if occurred_at and not asof:
            raise PITViolationError("[risk_fast_cold] asof_required: occurred_at 이벤트는 asof가 필요합니다.")
        if occurred_at:
            assert_pit_safe(occurred_at, snapshot_ts=asof)
        trace = self._event_trace(event)
        now_iso = datetime.now(timezone.utc).isoformat()

        # admit_candidate action rule 은 AdmissionEngine 담당. risk_fast 는 skip.
        # _load_trigger_rules() 가 전체 rule 을 반환하므로 action 필드 체크 필요.
        # 아래 6개 규칙은 명시적 rule_id 기반 평가 (action=review_risk/tighten_stop 등).
        # action=admit_candidate 인 rule 은 이 evaluate loop 에 진입하지 않음.

        # 규칙 1: 커뮤니티 게시글 급증
        comm_z = ctx.get("comm_volume_zscore", 0.0)
        if comm_z > self._thresholds["comm_volume_zscore"]:
            triggered.append({"rule_id": "comm_volume_spike", "matched_at": now_iso})
            logger.info(
                "[risk_fast_cold] rule_comm_volume_spike 발동: zscore=%.2f", comm_z
            )

        # 규칙 2: 커뮤니티 감성 급변
        sent_delta = ctx.get("comm_sentiment_delta", 0.0)
        if abs(sent_delta) > self._thresholds["comm_sentiment_delta"]:
            triggered.append({"rule_id": "comm_sentiment_delta", "matched_at": now_iso})
            logger.info(
                "[risk_fast_cold] rule_comm_sentiment_delta 발동: delta=%.2f", sent_delta
            )

        # 규칙 3: 분봉 급락 이상치
        ret_z = ctx.get("intraday_return_zscore", 0.0)
        if ret_z < self._thresholds["intraday_return_zscore"]:
            triggered.append({"rule_id": "intraday_drop_anomaly", "matched_at": now_iso})
            logger.info(
                "[risk_fast_cold] rule_intraday_drop_anomaly 발동: zscore=%.2f", ret_z
            )

        # 규칙 4: 외국인 대규모 순매도 (음수 컨벤션: -100B 이하 = 대규모 순매도)
        frg_sell = ctx.get("foreign_net_sell_krw", 0.0)
        if frg_sell < self._thresholds["foreign_net_sell_krw"]:
            triggered.append(
                {"rule_id": "foreign_net_sell_critical", "matched_at": now_iso}
            )
            logger.info(
                "[risk_fast_cold] rule_foreign_net_sell_critical 발동: sell=%.0f KRW", frg_sell
            )

        # 규칙 5: DART 중요 공시 (C2 event_type="dart" + priority 기반)
        # C2 EventNormalizeContract event_type enum: news|dart|macro|us_market|community|regime|investor_flow
        # "dart_alert"는 C4 publish_channel 이름이지 C2 event_type이 아님.
        # priority는 event 최상위 필드 (payload 내부가 아님).
        if (
            event_type == "dart"
            and event.get("priority", "") in ("critical", "urgent")
        ):
            triggered.append(
                {"rule_id": "dart_critical_disclosure", "matched_at": now_iso}
            )
            logger.info("[risk_fast_cold] rule_dart_critical_disclosure 발동")

        # 규칙 6: 뉴스-커뮤니티 방향 불일치
        divergence = ctx.get("news_comm_divergence", 0.0)
        try:
            divergence_value = float(divergence)
        except (TypeError, ValueError):
            divergence_value = 0.0
        if abs(divergence_value) > self._thresholds["news_comm_divergence"]:
            uncertainty_score = max(0.0, min(1.0, abs(divergence_value)))
            triggered.append(
                {"rule_id": "news_comm_divergence_strong", "matched_at": now_iso}
            )
            logger.info(
                "[risk_fast_cold] rule_news_comm_divergence_strong 발동: div=%.2f",
                divergence_value,
            )
            # uncertainty_signal publish (Dual-Source divergence → FDA 연계)
            self._publish_to_bus(
                "uncertainty_signal",
                {
                    "source": "risk_fast_cold",
                    "trigger": "news_comm_divergence_strong",
                    "uncertainty_score": uncertainty_score,
                    "confidence": uncertainty_score,
                    "scope": trace["scope"],
                    "event_id": event.get("event_id"),
                    "ticker": trace["ticker"] or None,
                    "occurred_at": occurred_at,
                    "asof": asof,
                    "ts": datetime.now(_KST).isoformat(),
                    "reasoning": "뉴스-커뮤니티 방향 불일치로 불확실성 신호 발행",
                },
            )

        # risk_level / stance 결정
        if not triggered:
            risk_level = "low"
            stance = "neutral"
            recommended_action = "pass"
        else:
            # 가장 높은 risk_level 선택 (high > medium > low)
            levels = [self._trigger_risk_level.get(t["rule_id"], "low") for t in triggered]
            risk_level = "high" if "high" in levels else "medium"
            # 가장 강한 stance 선택
            stances = [self._trigger_stance.get(t["rule_id"], "neutral") for t in triggered]
            if "veto_recommendation" in stances:
                stance = "veto_recommendation"
                recommended_action = "halt"
            elif "risk_reduce" in stances:
                stance = "risk_reduce"
                recommended_action = "reduce"
            else:
                stance = "neutral"
                recommended_action = "pass"

        latency_ms = (time.perf_counter() - t0) * 1000.0

        if latency_ms > self._sla_ms:
            logger.warning(
                "[risk_fast_cold] SLA 초과: %.2fms > %.0fms", latency_ms, self._sla_ms
            )

        result = {
            "risk_level": risk_level,
            "stance": stance,
            "fast_rule_match": triggered if triggered else None,
            "triggered_rules": [t["rule_id"] for t in triggered],
            "recommended_action": recommended_action,
            # P1-1 fix (2026-05-09): fast_rule_count 제거. C5 schema 미정의 extra 필드.
            # consumer 가 len(triggered_rules) 로 동등 계산 가능.
            "latency_ms": round(latency_ms, 2),
        }
        if uncertainty_score is not None:
            result["uncertainty_score"] = uncertainty_score
        return result

    def report(self, report_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """C5 risk_warning 리포트 생성."""
        if report_type != "risk_warning":
            raise ValueError(
                f"[risk_fast_cold] report_type={report_type} 미지원. "
                "RiskAgentFast(Cold)는 risk_warning만 발행."
            )
        msg = self._publish_to_bus("risk_warning", payload)
        msg["report_type"] = report_type
        msg["ts"] = datetime.now(timezone.utc).isoformat()
        return msg
