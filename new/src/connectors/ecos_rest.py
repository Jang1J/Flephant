"""ECOS 경제통계 REST 커넥터 v3 Sprint 2 S2-5 (2026-04-21).

한국은행 ECOS OpenAPI. 거시 지표 일별 수집 (장전 08:00 배치).

## 사용 패턴
    client = ECOSRestClient()
    rate_data = client.get_stat_data(
        "722Y001", start_date="20260101", end_date="20260421", item_codes=("0101000",)
    )
    pack = client.get_macro_pack("20260421")
    # {'interest_rate': 3.50, 'usd_krw': 1340.5}

## 수집 대상 (C3 macro 2 피처)
- 기준금리 (ECOS stat_code: 722Y001, item_code: 0101000)
- 원/달러 환율 (ECOS stat_code: 731Y001, item_code: 0000001)

## Mock 모드
ECOS_API_KEY 미설정 시 mock 자동 분기. Sprint 2 구현 + Sprint 4 실 API 통합.

## PIT-Safety
end_date <= snapshot_cutoff. 미래 데이터 참조 금지.

SSOT: api_contracts.md v3.5 C2 + C3 macro 피처.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from src.connectors.base import BaseConnector
from src.data.event_normalizer import EventNormalizer
from src.utils.auth import AuthManager
from src.utils.logger import get_logger
from src.utils.pit_guard import PITViolationError, is_pit_safe
from src.utils.rate_limiter import RateLimiter

logger = get_logger("ecos_rest")

_ECOS_BASE_URL = "https://ecos.bok.or.kr/api/StatisticSearch"
_KST = ZoneInfo("Asia/Seoul")

# ECOS 공식 stat_code/item_code (C3 macro 2 피처)
_STAT_CODES = {
    "interest_rate": "722Y001",    # 한국은행 기준금리 및 여수신금리
    "usd_krw": "731Y001",          # 주요국 통화의 대원화환율
}
_STAT_ITEM_CODES = {
    "interest_rate": ("0101000",),  # 한국은행 기준금리
    "usd_krw": ("0000001",),        # 원/미국달러(매매기준율)
}


@dataclass
class ECOSDatapoint:
    """ECOS API 단일 observation."""

    stat_code: str        # 722Y001 등
    stat_name: str        # 기준금리
    date_str: str         # YYYYMMDD (ECOS 원본 형식)
    date: datetime        # tz-aware KST
    value: float          # 관찰값
    unit: str             # % / 원


class ECOSRestClient(BaseConnector):
    """ECOS 거시 지표 수집 클라이언트.

    DART/Naver 패턴 3중 통합: auth/rate/normalizer.
    Mock 자동 분기 (ECOS_API_KEY 미설정 시).
    """

    def __init__(
        self,
        auth: AuthManager | None = None,
        rate_limiter: RateLimiter | None = None,
        normalizer: EventNormalizer | None = None,
    ) -> None:
        super().__init__()  # BaseConnector: timeout_sec / max_retries / backoff_base 로드
        self._timeout_sec = self.timeout_sec
        self._max_retries = self.max_retries
        self._backoff_base = self.backoff_base

        self._auth = auth or AuthManager()
        self._rate_limiter = rate_limiter or RateLimiter("ecos")
        self._normalizer = normalizer or EventNormalizer()

        # Mock 분기 (DART/Naver 패턴 통일). ECOS_API_KEY 미설정 시 mock 자동 전환.
        try:
            self._api_key: str | None = self._auth.get_ecos_key()
            self._is_mock = self._api_key is None
        except EnvironmentError:
            self._api_key = None
            self._is_mock = True

        if self._is_mock:
            logger.info("[ecos_rest] ECOS_API_KEY 미설정. Mock 모드로 동작.")

    def get_stat_data(
        self,
        stat_code: str,
        start_date: str,
        end_date: str,
        interval: str = "D",
        item_codes: tuple[str, ...] | None = None,
    ) -> list[ECOSDatapoint]:
        """ECOS 통계 데이터 조회.

        PIT-Safety: end_date <= snapshot_cutoff (18:00 KST 기준일).
        미래 데이터 참조 금지. 호출자가 end_date 를 현재 날짜 이하로 설정해야 함.

        Args:
            stat_code: 통계표 코드 (예: '722Y001' 기준금리)
            start_date: 시작일 'YYYYMMDD'
            end_date: 종료일 'YYYYMMDD'
            interval: D(일) | M(월) | Q(분기) | A(연)
            item_codes: 통계항목 코드. ECOS 일부 표는 item_code 없이 호출하면
                여러 항목이 섞이거나 빈 row를 반환한다.

        Returns:
            ECOSDatapoint 리스트.
        """
        if interval not in {"D", "M", "Q", "A"}:
            raise ValueError(f"interval D/M/Q/A 만 허용. 입력값: {interval}")

        # PIT-Safety 능동 체크 (2026-04-21 감사 반영). 불변 원칙 1.
        try:
            end_dt = datetime.strptime(end_date, "%Y%m%d").replace(tzinfo=_KST)
        except ValueError as e:
            raise ValueError(
                f"[ecos_rest] end_date 파싱 실패: {end_date}. YYYYMMDD 형식 필요."
            ) from e
        if not is_pit_safe(end_dt.isoformat()):
            raise PITViolationError(
                f"[ecos_rest] end_date={end_date} 가 snapshot(18:00 KST) 이후. 불변 원칙 1 위반."
            )

        if self._is_mock:
            return self._mock_data(stat_code, start_date, end_date, interval)

        self._rate_limiter.wait_and_acquire()
        item_path = ""
        if item_codes:
            item_path = "/" + "/".join(str(code) for code in item_codes)
        url = (
            f"{_ECOS_BASE_URL}/{self._api_key}/json/kr/1/100"
            f"/{stat_code}/{interval}/{start_date}/{end_date}{item_path}"
        )
        resp_json = self._http_get_with_retry(url)
        if resp_json is None:
            return []

        # ECOS 응답: {"StatisticSearch": {"list_total_count": N, "row": [{...}, ...]}}
        search = resp_json.get("StatisticSearch")
        if not isinstance(search, dict):
            result = resp_json.get("RESULT", {})
            logger.warning(
                "[ecos_rest] StatisticSearch 없음. stat_code=%s item_codes=%s "
                "result_code=%s message=%s",
                stat_code,
                item_codes,
                result.get("CODE"),
                result.get("MESSAGE"),
            )
            return []
        rows = search.get("row", [])
        return [self._parse_row(r, stat_code) for r in rows]

    def get_macro_pack(self, as_of_date: str) -> dict[str, float]:
        """C3 macro 2 피처 동시 수집. 장전 08:00 배치용.

        PIT-Safety: as_of_date 는 현재 날짜 이하. 최근 30일 범위로 조회 후 최신값 사용.

        Args:
            as_of_date: 'YYYYMMDD' (보통 오늘 날짜)

        Returns:
            {'interest_rate': 3.50, 'usd_krw': 1340.5}
        """
        end_date = as_of_date
        start_dt = datetime.strptime(as_of_date, "%Y%m%d") - timedelta(days=30)
        start_date = start_dt.strftime("%Y%m%d")

        result: dict[str, float] = {}
        for feature_name, stat_code in _STAT_CODES.items():
            item_codes = _STAT_ITEM_CODES.get(feature_name)
            points = self.get_stat_data(
                stat_code,
                start_date,
                end_date,
                item_codes=item_codes,
            )
            if points:
                latest = max(points, key=lambda p: p.date_str)
                result[feature_name] = latest.value
            else:
                logger.warning(
                    "[ecos_rest] %s (%s/%s) 조회 실패. 0 fallback.",
                    feature_name,
                    stat_code,
                    ",".join(item_codes or ()),
                )
                result[feature_name] = 0.0
        return result

    # ------------------------------------------------------------------ #
    # 내부 helper
    # ------------------------------------------------------------------ #

    def to_normalizer_input(self, datapoint: ECOSDatapoint) -> dict:
        """ECOSDatapoint → EventNormalizer raw dict 변환.

        필수 필드 매핑:
            stat_name → indicator  (EventNormalizer._normalize_ecos 요구)
            value     → value
            date      → date (ISO format, YYYY-MM-DD 10자)
        선택 필드:
            stat_code, unit  (payload 보존용)
        """
        date_iso: str
        if hasattr(datapoint.date, "isoformat"):
            # tz-aware datetime → YYYY-MM-DD 10자로 자름 (normalizer date 파싱 호환)
            date_iso = datapoint.date.strftime("%Y-%m-%d")
        else:
            # 이미 문자열이면 YYYYMMDD → YYYY-MM-DD 변환 시도
            raw_str = str(datapoint.date)
            if len(raw_str) == 8 and raw_str.isdigit():
                date_iso = f"{raw_str[:4]}-{raw_str[4:6]}-{raw_str[6:]}"
            else:
                date_iso = raw_str

        return {
            "indicator": datapoint.stat_name,     # 필수 매핑 (normalizer 요구)
            "value": datapoint.value,
            "date": date_iso,
            "stat_code": datapoint.stat_code,     # 선택 (payload 보존)
            "unit": datapoint.unit,
        }

    def poll_and_normalize(
        self,
        stat_code: str,
        start_date: str,
        end_date: str,
    ) -> list[dict]:
        """ECOS 수집 + EventNormalizer 경유 C2 event 리스트 반환.

        **경고**: EventGateway bypass (편의 메서드).
        Cold Path 운영은 get_stat_data() + to_normalizer_input() +
        EventGateway.ingest() 조합을 사용할 것.
        """
        points = self.get_stat_data(stat_code, start_date, end_date)
        events = []
        for pt in points:
            raw = self.to_normalizer_input(pt)
            try:
                event = self._normalizer.normalize(raw, source="ecos")
                events.append(event)
            except Exception as e:
                logger.warning("[ecos_rest] 정규화 실패: stat_code=%s err=%s", stat_code, e)
        return events

    def _parse_row(self, raw: dict, stat_code: str) -> ECOSDatapoint:
        """ECOS 응답 row 파싱."""
        date_str = raw.get("TIME", "")
        try:
            if len(date_str) == 8:
                date = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=_KST)
            elif len(date_str) == 6:
                date = datetime.strptime(date_str, "%Y%m").replace(tzinfo=_KST)
            else:
                raise ValueError(f"unsupported TIME={date_str}")
        except ValueError:
            logger.debug("[ecos_rest] TIME 파싱 실패: %s", date_str)
            date = datetime.now(_KST)

        try:
            value = float(raw.get("DATA_VALUE", 0))
        except (TypeError, ValueError):
            value = 0.0

        return ECOSDatapoint(
            stat_code=stat_code,
            stat_name=raw.get("STAT_NAME", ""),
            date_str=date_str,
            date=date,
            value=value,
            unit=raw.get("UNIT_NAME", ""),
        )

    def _http_get_with_retry(self, url: str) -> dict | None:
        """HTTP GET 재시도 (DART/Naver 패턴 통일)."""
        for attempt in range(self._max_retries):
            try:
                resp = requests.get(url, timeout=self._timeout_sec)
                resp.raise_for_status()
                return resp.json()
            except requests.Timeout as e:
                logger.warning("[ecos_rest] timeout (attempt %d): %s", attempt + 1, e)
            except requests.HTTPError as e:
                status = getattr(e.response, "status_code", None)
                if status == 429 or (status and 500 <= status < 600):
                    logger.warning("[ecos_rest] %s (attempt %d)", status, attempt + 1)
                else:
                    logger.warning("[ecos_rest] HTTP error: %s", e)
                    return None
            except requests.RequestException as e:
                logger.warning("[ecos_rest] request err (attempt %d): %s", attempt + 1, e)

            if attempt < self._max_retries - 1:
                time.sleep(self._backoff_base ** attempt)

        return None

    def _mock_data(
        self,
        stat_code: str,
        start_date: str,
        end_date: str,
        interval: str,
    ) -> list[ECOSDatapoint]:
        """Mock: 3개 datapoint 반환 (최근 3 영업일 대체)."""
        now = datetime.now(_KST)
        mock_values: dict[str, float] = {
            "098Y001": 3.50,    # 기준금리 3.5%
            "036Y001": 1340.5,  # 원/달러 1340.5원
            "722Y001": 3.50,    # 한국은행 기준금리
            "731Y001": 1340.5,  # 원/미국달러(매매기준율)
        }
        default_val = mock_values.get(stat_code, 100.0)
        return [
            ECOSDatapoint(
                stat_code=stat_code,
                stat_name=f"[MOCK] {stat_code}",
                date_str=(now - timedelta(days=i)).strftime("%Y%m%d"),
                date=now - timedelta(days=i),
                value=default_val + (i * 0.01),
                unit="%" if "098" in stat_code else "원",
            )
            for i in range(3)
        ]
