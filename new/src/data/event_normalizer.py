"""C2 EventNormalizeContract 구현. 이기종 소스(6개) → 통일 이벤트 스키마 변환.

PIT-Safety(불변 원칙 1) 강제: 모든 출력에 pit_safe 태그.
source별 TTL/priority/llm_required는 risk_config.yaml event_normalizer 섹션에서 로드 (불변 원칙 5).
kis_bar/kis_event는 C2를 거치지 않음. Quant Agent가 bar_buffer 직접 consume (C1 bypass).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from src.utils.config_loader import load as config_load
from src.utils.id_factory import generate_event_id
from src.utils.pit_guard import PITViolationError, is_pit_safe
from src.utils.time_utils import now_kst
from src.utils.logger import get_logger
from src.utils.ticker_utils import normalize_ticker

logger = get_logger("event_normalizer")

_KST = ZoneInfo("Asia/Seoul")

# source → event_type 매핑 (C2 SSOT: news|dart|macro|us_market|community|regime|investor_flow)
# enum 성격이므로 코드에 유지. kis_bar/kis_event는 C1 bypass이므로 포함하지 않음.
# S5-1: price_snapshot 추가 (C16 WatchUniverseSnapshotContract, AdmissionEngine 편입 트리거 전용)
_EVENT_TYPE_MAP: dict[str, str] = {
    "dart": "dart",
    "krx_investor_flow": "investor_flow",
    "naver_news": "news",
    "community": "community",
    "ecos": "macro",
    "us_market": "us_market",
    "price_snapshot": "price_snapshot",
}


class ValidationError(Exception):
    """C2 필수 필드 누락 또는 타입 불일치."""


# PITViolationError: src.utils.pit_guard SSOT. 이 파일에서 직접 import해 사용.
# (하위 호환: from src.data.event_normalizer import PITViolationError 도 동작)


class UnknownSourceError(ValueError):
    """지원하지 않는 source."""


def _require(raw: dict[str, Any], field: str, source: str) -> Any:
    """필수 필드 체크. 없으면 ValidationError."""
    if field not in raw or raw[field] is None:
        raise ValidationError(f"source={source}: 필수 필드 '{field}' 누락")
    return raw[field]


def _parse_ts_to_kst_str(ts_value: Any, field: str, source: str) -> str:
    """문자열 또는 datetime → KST ISO 8601 문자열로 정규화.

    입력이 이미 KST aware이면 그대로 사용.
    naive이면 KST 가정.
    """
    if isinstance(ts_value, datetime):
        dt = ts_value
    elif isinstance(ts_value, str):
        try:
            dt = datetime.fromisoformat(ts_value)
        except ValueError as e:
            raise ValidationError(
                f"source={source}: 필드 '{field}' 시간 파싱 실패: {ts_value!r}"
            ) from e
    else:
        raise ValidationError(
            f"source={source}: 필드 '{field}' 타입 불일치 (str 또는 datetime 필요)"
        )

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_KST)
    else:
        dt = dt.astimezone(_KST)

    return dt.isoformat()


def _compute_expires_at(occurred_at_str: str, ttl: int) -> str:
    """occurred_at + ttl(초) → expires_at ISO 8601 KST 문자열."""
    dt = datetime.fromisoformat(occurred_at_str)
    result = dt + timedelta(seconds=ttl)
    if result.tzinfo is None:
        result = result.replace(tzinfo=_KST)
    return result.isoformat()


class EventNormalizer:
    """C2 EventNormalizeContract 구현. 6개 source → 통일 이벤트 구조.

    불변 원칙 1 (PIT-Safety) enforcement: 모든 정규화 출력에 pit_safe 태그.
    pit_safe=False 시 PITViolationError 발생. 미래 데이터는 즉시 차단.

    kis_bar/kis_event는 C1 bypass. Quant Agent가 bar_buffer 직접 consume.
    이 클래스에 kis_bar/kis_event를 전달하면 UnknownSourceError 발생.
    """

    SUPPORTED_SOURCES: frozenset[str] = frozenset({
        "dart", "krx_investor_flow", "naver_news", "community",
        "ecos", "us_market",
        "price_snapshot",  # S5-1 C16 WatchUniverseSnapshot
    })

    def __init__(self) -> None:
        # source별 TTL/priority/llm_required는 risk_config.yaml에서 로드 (불변 원칙 5)
        cfg = config_load("risk_config.yaml", "event_normalizer")
        self.default_ttl: dict[str, int] = cfg["default_ttl"]
        self.priority: dict[str, str] = cfg["priority"]
        self.llm_required: dict[str, bool] = cfg["llm_required"]
        # market_hours.close 캐시: _market_close_time_str() 매 호출 yaml I/O 방지
        self._market_close_str: str = self._load_market_close_str()

    @staticmethod
    def _load_market_close_str() -> str:
        """risk_config.yaml market_hours.close 최초 로드 헬퍼 (캐시 초기화 전용)."""
        mh = config_load("risk_config.yaml", "market_hours")
        return str(mh["close"])

    def normalize(
        self,
        raw_event: dict[str, Any],
        source: str,
        *,
        asof: datetime | str | None = None,
    ) -> dict[str, Any]:
        """raw_event를 C2 스키마로 정규화.

        Args:
            raw_event: 커넥터가 반환한 원본 dict.
            source: 소스 이름 (SUPPORTED_SOURCES 중 하나).
            asof: 장중 Cold Path 판단 시점. 주입되면 occurred_at <= asof 만 허용.

        Returns:
            C2 EventNormalizeContract output 스키마 dict.

        Raises:
            UnknownSourceError: source가 SUPPORTED_SOURCES 아닌 경우.
            ValidationError: source별 필수 필드 누락 시.
            PITViolationError: occurred_at이 미래인 경우 (불변 원칙 1).
        """
        if source not in self.SUPPORTED_SOURCES:
            raise UnknownSourceError(
                f"지원하지 않는 source: '{source}'. "
                f"지원 목록: {sorted(self.SUPPORTED_SOURCES)}"
            )

        dispatch = {
            "dart": self._normalize_dart,
            "krx_investor_flow": self._normalize_krx_investor_flow,
            "naver_news": self._normalize_naver_news,
            "community": self._normalize_community,
            "ecos": self._normalize_ecos,
            "us_market": self._normalize_us_market,
            "price_snapshot": self._normalize_price_snapshot,  # S5-1 C16
        }

        partial = dispatch[source](raw_event)

        # common 필드 주입
        ingest_ts = now_kst().isoformat()
        event_id = generate_event_id()
        ttl = partial.get("ttl", self.default_ttl[source])
        occurred_at = partial["occurred_at"]
        expires_at = _compute_expires_at(occurred_at, ttl)

        snapshot_ts = _parse_ts_to_kst_str(asof, "asof", "event_normalizer") if asof else None
        pit_safe_result = is_pit_safe(occurred_at, snapshot_ts=snapshot_ts)
        if not pit_safe_result:
            raise PITViolationError(
                f"[event_normalizer] PIT-Safety 위반: source={source}, "
                f"occurred_at={occurred_at} 가 snapshot/asof={snapshot_ts or '18:00 KST'} 이후입니다. "
                "불변 원칙 1 위반. 이 이벤트는 처리 불가."
            )

        result: dict[str, Any] = {
            "event_id": event_id,
            "source": source,
            "event_type": partial.get("event_type", _EVENT_TYPE_MAP[source]),
            "scope": partial.get("scope", "market"),
            "title": partial.get("title", ""),
            "summary": partial.get("summary", ""),
            "occurred_at": occurred_at,
            "ingest_ts": ingest_ts,
            "priority": partial.get("priority", self.priority[source]),
            "llm_required": partial.get("llm_required", self.llm_required[source]),
            "ttl": ttl,
            "expires_at": expires_at,
            "supersedes": partial.get("supersedes", None),
            "pit_safe": pit_safe_result,
            "payload": partial.get("payload", {}),
        }
        payload_ticker = result["payload"].get("ticker")
        scope = str(result["scope"])
        if payload_ticker:
            ticker = normalize_ticker(payload_ticker)
            if ticker:
                result["ticker"] = ticker
                result["scope"] = f"ticker:{ticker}"
        elif scope.startswith("ticker:"):
            ticker = normalize_ticker(scope.split(":", 1)[1])
            if ticker:
                result["ticker"] = ticker
                result["scope"] = f"ticker:{ticker}"

        logger.info(
            "이벤트 정규화 완료: source=%s event_id=%s event_type=%s occurred_at=%s",
            source, event_id, result["event_type"], occurred_at,
        )

        return result

    # ------------------------------------------------------------------ #
    # source별 private 정규화 메서드
    # ------------------------------------------------------------------ #

    def _normalize_dart(self, raw: dict[str, Any]) -> dict[str, Any]:
        """DART 공시 정규화.

        필수: title, corp_name, disclosure_time
        선택: summary, event_type (기본 "dart")
        """
        title = _require(raw, "title", "dart")
        corp_name = _require(raw, "corp_name", "dart")
        disclosure_time = _require(raw, "disclosure_time", "dart")

        occurred_at = _parse_ts_to_kst_str(disclosure_time, "disclosure_time", "dart")
        summary = raw.get("summary", "")
        ticker = normalize_ticker(raw.get("ticker", None))
        scope = f"ticker:{ticker}" if ticker else "market"

        return {
            "event_type": "dart",
            "scope": scope,
            "title": title,
            "summary": summary,
            "occurred_at": occurred_at,
            "priority": "urgent",
            "llm_required": True,
            "payload": {
                "corp_name": corp_name,
                "ticker": ticker or None,
                "original_time": str(disclosure_time),
                **{k: v for k, v in raw.items()
                   if k not in ("title", "corp_name", "disclosure_time", "summary", "ticker")},
            },
        }

    def _normalize_krx_investor_flow(self, raw: dict[str, Any]) -> dict[str, Any]:
        """KRX 투자자별 수급 정규화.

        필수: ticker, date
        선택: foreign_net_buy, institutional_net_buy, retail_net_buy
              (C3 SSOT api_contracts.md feature_manifest.investor_flow 필드명)
        """
        ticker = _require(raw, "ticker", "krx_investor_flow")
        date_val = _require(raw, "date", "krx_investor_flow")

        # 장 마감 시각은 risk_config.yaml market_hours.close 경유 (불변 원칙 5)
        market_close_str = self._market_close_time_str()

        # date가 날짜 문자열이면 market_close(15:30 기본) KST로 변환 (수급은 장 마감 후 발표)
        if isinstance(date_val, str) and len(date_val) == 10:
            occurred_at = _parse_ts_to_kst_str(
                f"{date_val}T{market_close_str}+09:00", "date", "krx_investor_flow"
            )
        else:
            occurred_at = _parse_ts_to_kst_str(date_val, "date", "krx_investor_flow")

        ticker_str = normalize_ticker(ticker)
        if not ticker_str:
            raise ValidationError("source=krx_investor_flow: ticker 형식 오류")

        # 레거시 키(institutional/retail) → SSOT 키(institutional_net_buy/retail_net_buy) 호환
        institutional_val = raw.get(
            "institutional_net_buy", raw.get("institutional")
        )
        retail_val = raw.get(
            "retail_net_buy", raw.get("retail")
        )

        return {
            "event_type": "investor_flow",
            "scope": f"ticker:{ticker_str}",
            "title": f"투자자별 수급 - {ticker_str}",
            "summary": (
                f"외국인 순매수: {raw.get('foreign_net_buy', 'N/A')}, "
                f"기관: {institutional_val if institutional_val is not None else 'N/A'}, "
                f"개인: {retail_val if retail_val is not None else 'N/A'}"
            ),
            "occurred_at": occurred_at,
            "priority": "normal",
            "llm_required": False,
            "payload": {
                "ticker": ticker_str,
                "foreign_net_buy": raw.get("foreign_net_buy"),
                "institutional_net_buy": institutional_val,
                "retail_net_buy": retail_val,
                "date": str(date_val),
                **{k: v for k, v in raw.items()
                   if k not in (
                       "ticker", "date",
                       "foreign_net_buy",
                       "institutional", "institutional_net_buy",
                       "retail", "retail_net_buy",
                   )},
            },
        }

    def _market_close_time_str(self) -> str:
        """market_hours.close 문자열 반환. __init__ 에서 캐시된 값 사용 (yaml I/O 없음)."""
        return self._market_close_str

    def _normalize_naver_news(self, raw: dict[str, Any]) -> dict[str, Any]:
        """네이버 뉴스 정규화.

        필수: title, published_at
        선택: summary, link
        """
        title = _require(raw, "title", "naver_news")
        published_at = _require(raw, "published_at", "naver_news")

        occurred_at = _parse_ts_to_kst_str(published_at, "published_at", "naver_news")
        summary = raw.get("summary", "")
        ticker = normalize_ticker(raw.get("ticker", None))
        scope = f"ticker:{ticker}" if ticker else "market"

        return {
            "event_type": "news",
            "scope": scope,
            "title": title,
            "summary": summary,
            "occurred_at": occurred_at,
            "priority": "normal",
            "llm_required": True,
            "payload": {
                "link": raw.get("link", ""),
                "ticker": ticker or None,
                **{k: v for k, v in raw.items()
                   if k not in ("title", "summary", "published_at", "link", "ticker")},
            },
        }

    def _normalize_community(self, raw: dict[str, Any]) -> dict[str, Any]:
        """커뮤니티 게시물 정규화.

        필수: post_title, posted_at
        선택: body, ticker_mentioned, spam_flag
        """
        post_title = _require(raw, "post_title", "community")
        posted_at = _require(raw, "posted_at", "community")

        occurred_at = _parse_ts_to_kst_str(posted_at, "posted_at", "community")
        body = raw.get("body", "")
        ticker_mentioned = normalize_ticker(raw.get("ticker_mentioned", None))
        spam_flag = raw.get("spam_flag", False)
        scope = f"ticker:{ticker_mentioned}" if ticker_mentioned else "market"

        return {
            "event_type": "community",
            "scope": scope,
            "title": post_title,
            "summary": body[:200] if body else "",
            "occurred_at": occurred_at,
            "priority": "low",
            "llm_required": False,  # 1차 규칙 필터 통과 후 일부만 LLM 호출
            "payload": {
                "body": body,
                "ticker_mentioned": ticker_mentioned or None,
                "spam_flag": spam_flag,
                **{k: v for k, v in raw.items()
                   if k not in ("post_title", "posted_at", "body", "ticker_mentioned", "spam_flag")},
            },
        }

    def _normalize_ecos(self, raw: dict[str, Any]) -> dict[str, Any]:
        """ECOS 거시지표 정규화.

        필수: indicator, value, date
        """
        indicator = _require(raw, "indicator", "ecos")
        value = _require(raw, "value", "ecos")
        date_val = _require(raw, "date", "ecos")

        if isinstance(date_val, str) and len(date_val) == 10:
            occurred_at = _parse_ts_to_kst_str(f"{date_val}T00:00:00+09:00", "date", "ecos")
        else:
            occurred_at = _parse_ts_to_kst_str(date_val, "date", "ecos")

        return {
            "event_type": "macro",
            "scope": "market",
            "title": f"거시지표 갱신: {indicator}",
            "summary": f"{indicator} = {value}",
            "occurred_at": occurred_at,
            "priority": "low",
            "llm_required": False,
            "payload": {
                "indicator": indicator,
                "value": value,
                "date": str(date_val),
                **{k: v for k, v in raw.items()
                   if k not in ("indicator", "value", "date")},
            },
        }

    def _normalize_us_market(self, raw: dict[str, Any]) -> dict[str, Any]:
        """미국 시장 정규화.

        필수: close_time_utc
        선택: sp500_change, nasdaq_change, vix, soxx_change
        """
        close_time_utc = _require(raw, "close_time_utc", "us_market")

        occurred_at = _parse_ts_to_kst_str(close_time_utc, "close_time_utc", "us_market")
        sp500 = raw.get("sp500_change")
        nasdaq = raw.get("nasdaq_change")
        vix = raw.get("vix")

        summary_parts = []
        if sp500 is not None:
            summary_parts.append(f"S&P500 {sp500 * 100:+.2f}%")
        if nasdaq is not None:
            summary_parts.append(f"NASDAQ {nasdaq * 100:+.2f}%")
        if vix is not None:
            summary_parts.append(f"VIX {vix:.1f}")

        return {
            "event_type": "us_market",
            "scope": "market",
            "title": "미국 시장 마감",
            "summary": " | ".join(summary_parts) if summary_parts else "미국 시장 데이터",
            "occurred_at": occurred_at,
            "priority": "normal",
            "llm_required": False,
            "payload": {
                "sp500_change": sp500,
                "nasdaq_change": nasdaq,
                "vix": vix,
                "soxx_change": raw.get("soxx_change"),
                "close_time_utc": str(close_time_utc),
                **{k: v for k, v in raw.items()
                   if k not in ("sp500_change", "nasdaq_change", "vix", "soxx_change", "close_time_utc")},
            },
        }

    def _normalize_price_snapshot(self, raw: dict[str, Any]) -> dict[str, Any]:
        """C16 WatchUniverseSnapshot 정규화. S5-1.

        필수: watch_snapshot_id, ts
        선택: snapshots (종목별 현재가 list)

        AdmissionEngine 편입 판정을 위해 EventGateway 를 경유해야 할 때만 이 경로를 사용한다.
        snapshot 자체는 WatchSnapshotFetcher.fetch_once() 가 직접 저장하므로,
        여기서는 이벤트 정규화 스키마만 구성한다.
        """
        watch_snapshot_id = _require(raw, "watch_snapshot_id", "price_snapshot")
        ts_val = _require(raw, "ts", "price_snapshot")

        occurred_at = _parse_ts_to_kst_str(ts_val, "ts", "price_snapshot")
        snapshots = raw.get("snapshots", [])
        ticker_count = len(snapshots) if isinstance(snapshots, list) else 0
        ticker = normalize_ticker(raw.get("ticker", ""))
        return_pct = raw.get("return_pct", raw.get("day_change_pct"))

        if not ticker or ticker == "000000":
            if isinstance(snapshots, list) and len(snapshots) == 1 and isinstance(snapshots[0], dict):
                ticker = normalize_ticker(snapshots[0].get("ticker", ""))
                return_pct = snapshots[0].get(
                    "return_pct",
                    snapshots[0].get("day_change_pct", return_pct),
                )

        scope = f"ticker:{ticker}" if ticker and ticker != "000000" else "market"
        payload = {
            "watch_snapshot_id": watch_snapshot_id,
            "ticker_count": ticker_count,
            "snapshots": snapshots,
            **{k: v for k, v in raw.items()
               if k not in ("watch_snapshot_id", "ts", "snapshots")},
        }
        if ticker and ticker != "000000":
            payload["ticker"] = ticker
        if return_pct is not None:
            payload["return_pct"] = return_pct

        return {
            "event_type": "price_snapshot",
            "scope": scope,
            "title": f"Watch Universe 스냅샷: {ticker_count}종목",
            "summary": f"watch_snapshot_id={watch_snapshot_id} ticker_count={ticker_count}",
            "occurred_at": occurred_at,
            "priority": "normal",
            "llm_required": False,
            "payload": payload,
        }
