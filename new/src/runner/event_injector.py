"""S4-3 EventInjector. E2E 시나리오용 합성 이벤트 주입기.

EventGateway (S2-0)로 이벤트를 push한다.
실 KIS/Naver 없이 mock fixtures 기반 시뮬레이션 전용.

지원 이벤트 타입:
  news      : C2 event_type=news. ticker + headline + sentiment.
  dart      : C2 event_type=dart. ticker + disclosure_type.
  community : C2 event_type=community. ticker + post_text + score.
  macro     : C2 event_type=macro. indicator + value (usd_krw / interest_rate 등).

불변 원칙:
  - PIT-Safety: inject_* 호출 시 ts가 현재 시각 이전인지 검증하지 않음.
    시나리오 시뮬이므로 caller가 올바른 ts를 전달할 책임.
  - 하드코딩 금지 (불변 원칙 5): source 필드 등 고정 문자열은 EventNormalizer에서 처리.
  - LLM 미호출: injector는 gateway 입력 생성만. 처리는 gateway + 에이전트.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.orchestration.event_gateway import EventGateway
from src.utils.id_factory import generate_event_id
from src.utils.logger import get_logger
from src.utils.ticker_utils import pad_ticker as normalize_ticker

logger = get_logger("event_injector")
_KST = ZoneInfo("Asia/Seoul")

_AUDIT_DIR = Path(__file__).resolve().parents[3] / "artifacts" / "audit"


class EventInjector:
    """E2E 시나리오용 이벤트 주입기. EventGateway로 합성 이벤트를 push한다.

    Args:
        gateway: 이벤트를 받을 EventGateway 인스턴스.
        audit_log_path: 주입 이벤트 JSONL 기록 경로. None이면 기본 경로 사용.
    """

    def __init__(
        self,
        gateway: EventGateway,
        audit_log_path: Path | None = None,
    ) -> None:
        self._gateway = gateway
        self._log_path: Path = audit_log_path or (
            _AUDIT_DIR / "injected_events.jsonl"
        )
        self._injected: list[dict[str, Any]] = []
        logger.info("[event_injector] 초기화 완료. audit=%s", self._log_path)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def inject_news(
        self,
        ticker: str,
        headline: str,
        ts: datetime,
        sentiment: str = "neutral",
        source: str = "naver",
    ) -> dict[str, Any]:
        """뉴스 이벤트 주입. Cold Path NewsAgent 입력.

        Args:
            ticker: 6자리 종목코드 (내부에서 zero-pad).
            headline: 뉴스 제목.
            ts: 이벤트 발생 시각 (KST aware datetime).
            sentiment: positive | negative | neutral.
            source: naver | community | dart.

        Returns:
            EventGateway.ingest() 반환값 + injected event raw dict.
        """
        raw = {
            "event_id": generate_event_id(),
            "published_at": ts.isoformat(),
            "ticker": normalize_ticker(ticker),
            "title": headline,
            "sentiment": sentiment,
            "source": source,
            "category": "single_company",
        }
        return self._ingest(raw, source="naver_news", event_type="news")

    def inject_dart(
        self,
        ticker: str,
        disclosure_type: str,
        ts: datetime,
        title: str = "",
        corp_name: str = "",
    ) -> dict[str, Any]:
        """DART 공시 이벤트 주입.

        Args:
            ticker: 6자리 종목코드.
            disclosure_type: 공시 종류 (예: 주요사항보고서, 조회공시).
            ts: 이벤트 발생 시각.
            title: 공시 제목 (선택).
            corp_name: 법인명 (선택). 미제공 시 ticker 기반 기본값 사용.

        Returns:
            EventGateway.ingest() 반환값.
        """
        padded_ticker = normalize_ticker(ticker)
        raw = {
            "event_id": generate_event_id(),
            "disclosure_time": ts.isoformat(),
            "ticker": padded_ticker,
            "corp_name": corp_name if corp_name else padded_ticker,
            "disclosure_type": disclosure_type,
            "title": title if title else disclosure_type,
            "source": "dart",
        }
        return self._ingest(raw, source="dart", event_type="dart")

    def inject_community(
        self,
        ticker: str,
        post_text: str,
        ts: datetime,
        score: float = 0.5,
    ) -> dict[str, Any]:
        """커뮤니티 감성 이벤트 주입. Dual-Source divergence 테스트용.

        Args:
            ticker: 6자리 종목코드.
            post_text: 게시글 텍스트.
            ts: 이벤트 발생 시각.
            score: 감성 점수 (0.0~1.0, 높을수록 긍정).

        Returns:
            EventGateway.ingest() 반환값.
        """
        raw = {
            "event_id": generate_event_id(),
            "posted_at": ts.isoformat(),
            "ticker_mentioned": normalize_ticker(ticker),
            "post_title": post_text,
            "body": post_text,
            "score": float(score),
            "source": "community",
        }
        return self._ingest(raw, source="community", event_type="community")

    def inject_macro(
        self,
        indicator: str,
        value: float,
        ts: datetime,
    ) -> dict[str, Any]:
        """거시 지표 이벤트 주입. 종목 없음 (market-wide).

        Args:
            indicator: 지표명 (예: usd_krw, interest_rate).
            value: 지표 값.
            ts: 이벤트 발생 시각.

        Returns:
            EventGateway.ingest() 반환값.
        """
        raw = {
            "event_id": generate_event_id(),
            "date": ts.strftime("%Y-%m-%d"),
            "indicator": indicator,
            "value": float(value),
            "source": "ecos",
        }
        return self._ingest(raw, source="ecos", event_type="macro")

    def injected_count(self) -> int:
        """지금까지 주입한 이벤트 수."""
        return len(self._injected)

    def injected_events(self) -> list[dict[str, Any]]:
        """주입된 이벤트 사본 반환."""
        return list(self._injected)

    def flush_audit_log(self) -> Path:
        """주입된 이벤트 전체를 JSONL로 기록. 경로 반환."""
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_path.open("w", encoding="utf-8") as f:
            for entry in self._injected:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info(
            "[event_injector] audit log flush: %d 건 → %s",
            len(self._injected),
            self._log_path,
        )
        return self._log_path

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    def _ingest(
        self,
        raw: dict[str, Any],
        source: str,
        event_type: str,
    ) -> dict[str, Any]:
        """EventGateway.ingest() 호출 + audit 기록."""
        result = self._gateway.ingest(raw, source=source)
        record = {
            "event_type": event_type,
            "raw": raw,
            "gateway_result": result,
        }
        self._injected.append(record)
        logger.info(
            "[event_injector] 주입: event_type=%s event_id=%s status=%s",
            event_type,
            raw.get("event_id"),
            result.get("status"),
        )
        return result
