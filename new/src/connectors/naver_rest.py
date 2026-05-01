"""Naver 뉴스 REST 커넥터 v3 Sprint 2 S2-3 (2026-04-21).

Cold Path NewsAgent 입력 소스. Naver Open API 뉴스 검색.

아키텍처:
    AuthManager.get_naver_client() -> (client_id, client_secret)
    RateLimiter("naver")   -> Token bucket (req_per_sec=10, burst=20, yaml 경유)
    HTTPClient             -> timeout=10, max_retries=3, backoff=2^attempt (yaml 경유)
    EventNormalizer.normalize(raw, source='naver_news') -> C2 event dict

1분 또는 5분 폴링:
    polling_interval 은 호출자 결정 (S2-6 news_filter.yaml 에서 로드 예정).
    단건 호출 = search_news(query, display=10)
    배치    = search_multi(queries) 반복 호출 + rate_limiter 자동 대기

SSOT: api_contracts.md v3.5 C2 EventNormalizeContract (source='naver_news') + C11 EventAdmissionControl (간접 의존 via EventGateway)
Mock: NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 환경변수 없으면 graceful mock 반환.
불변 원칙 5: timeout/retries/backoff 는 risk_config.yaml connector_defaults 에서 로드.
             _NAVER_API_URL 은 공식 고정 endpoint (enum 성격, 상수 허용).

## 사용 패턴 (2026-04-21 확정)

**표준 Cold Path 경로 (운영)**:
    items = NaverNewsClient().search_news(query, display=10)
    for item in items:
        raw = {"title": item.title, "description": item.description, ...}
        EventGateway.ingest(raw, source="naver_news")
    # EventGateway 가 내부에서 EventNormalizer.normalize() 호출 + EventAdmission + backlog

**편의 경로 (테스트/디버깅)**:
    events = NaverNewsClient().search_and_normalize(query, display=10)
    # C2 event dict 직접 반환. EventAdmission bypass.
    # EventAdmission 의 dead_letter / backlog / priority 관리 기능 미적용.
    # 운영 체인에서는 사용 금지. 단위 테스트 또는 디버깅 전용.

Sprint 2 S2-7 News Agent 는 search_news() + EventGateway.ingest() 패턴 사용.
"""
from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from src.connectors.base import BaseConnector
from src.data.event_normalizer import EventNormalizer
from src.utils.auth import AuthManager
from src.utils.logger import get_logger
from src.utils.rate_limiter import RateLimiter

logger = get_logger("naver_rest")

# 공식 고정 endpoint (enum 성격 상수. 하드코딩 예외 허용)
_NAVER_API_URL = "https://openapi.naver.com/v1/search/news.json"
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")


@dataclass
class NaverNewsItem:
    """Naver 뉴스 단일 item (파싱 후).

    HTML 태그/entity 제거, pubDate RFC 2822 -> tz-aware datetime 변환 완료 상태.
    """

    title: str          # HTML 태그/entity 제거됨
    description: str
    original_link: str
    naver_link: str
    pub_date: datetime  # tz-aware (pubDate RFC 2822 파싱)


def _strip_html(text: str) -> str:
    """HTML 태그 제거 후 HTML entity decode.

    예: "<b>삼성전자</b> Q4 &amp; 실적" -> "삼성전자 Q4 & 실적"
    """
    return html.unescape(_HTML_TAG_PATTERN.sub("", text))


def _parse_rfc2822(pub_date_str: str) -> datetime:
    """RFC 2822 문자열 -> tz-aware datetime.

    email.utils.parsedate_to_datetime 사용 (stdlib, locale 무관).
    빈 문자열/None 입력 시 TypeError, 형식 오류 시 ValueError 발생.
    실패 시 현재 시각(tz-aware) 반환 + debug 로그.
    """
    try:
        pub_date = parsedate_to_datetime(pub_date_str)
        if pub_date.tzinfo is None:
            # 드물게 tz 누락 시 로컬 tz 적용
            pub_date = pub_date.astimezone()
        return pub_date
    except (TypeError, ValueError) as e:
        logger.debug("[naver_rest] pubDate 파싱 실패 fallback: %r err=%s", pub_date_str, e)
        return datetime.now().astimezone()


class NaverNewsClient(BaseConnector):
    """Naver Open API 뉴스 검색 클라이언트.

    Cold Path NewsAgent / DART cross-reference 용.
    환경변수 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 없으면 mock 모드 자동 분기.

    통합:
        - AuthManager.get_naver_client(): (client_id, client_secret)
        - RateLimiter("naver"): Token bucket
        - EventNormalizer.normalize(..., source='naver_news'): C2 변환
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
        self._rate_limiter = rate_limiter or RateLimiter("naver")
        self._normalizer = normalizer or EventNormalizer()

        # Mock 분기: env 키 없으면 mock 모드
        try:
            client_id, client_secret = self._auth.get_naver_client()
            self._client_id: str | None = client_id
            self._client_secret: str | None = client_secret
            self._is_mock = False
        except EnvironmentError:
            self._client_id = None
            self._client_secret = None
            self._is_mock = True
            logger.info("[naver_rest] NAVER_CLIENT_ID/SECRET 미설정. Mock 모드로 동작.")

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def search_news(
        self,
        query: str,
        display: int = 10,
        start: int = 1,
        sort: str = "date",
    ) -> list[NaverNewsItem]:
        """뉴스 검색 (파싱된 NaverNewsItem 리스트 반환).

        Args:
            query:   검색어 (한글 가능)
            display: 결과 수 1~100
            start:   시작 위치 1~1000
            sort:    'sim' (정확도순) | 'date' (날짜순, 기본)

        Returns:
            NaverNewsItem 리스트. 실패/mock 시 [] 또는 mock items.

        Raises:
            ValueError: display/start/sort 범위 위반 시.
        """
        if not 1 <= display <= 100:
            raise ValueError(f"display 1~100 범위 초과: {display}")
        if not 1 <= start <= 1000:
            raise ValueError(f"start 1~1000 범위 초과: {start}")
        if sort not in {"sim", "date"}:
            raise ValueError(f"sort 'sim' 또는 'date'만 허용: {sort!r}")

        if self._is_mock:
            return self._mock_search(query, display)

        # rate_limiter 토큰 확보 (blocking, max 10s)
        self._rate_limiter.wait_and_acquire()

        headers = {
            "X-Naver-Client-Id": self._client_id,
            "X-Naver-Client-Secret": self._client_secret,
        }
        params: dict[str, Any] = {
            "query": query,
            "display": display,
            "start": start,
            "sort": sort,
        }

        resp_json = self._http_get_with_retry(_NAVER_API_URL, headers, params)
        if resp_json is None:
            logger.warning("[naver_rest] HTTP 최종 실패. query=%r 빈 결과 반환.", query)
            return []

        items_raw = resp_json.get("items", [])
        return [self._parse_item(it) for it in items_raw]

    def search_and_normalize(
        self,
        query: str,
        display: int = 10,
    ) -> list[dict[str, Any]]:
        """뉴스 검색 후 C2 EventNormalize 경유 event dict 리스트 반환.

        **경고 (2026-04-21 Option B 확정)**:
            이 메서드는 EventGateway 를 bypass 하여 직접 EventNormalizer 호출.
            EventAdmission 의 dead_letter / backlog / priority 기능 미적용.
            운영 Cold Path 에서는 사용 금지. 테스트/디버깅 전용.
            운영에서는 `search_news()` + `EventGateway.ingest(raw, 'naver_news')` 사용.

        Returns:
            C2 event dict 리스트 (event_id / source='naver_news' / event_type='news' / ...).
            정규화 실패 item은 skip (warning 로그).
        """
        items = self.search_news(query, display=display)
        events: list[dict[str, Any]] = []
        for item in items:
            raw = self._item_to_normalizer_input(item, query)
            try:
                event = self._normalizer.normalize(raw, source="naver_news")
                events.append(event)
            except Exception as e:
                logger.warning(
                    "[naver_rest] C2 정규화 실패 skip. title=%r err=%s",
                    item.title[:40],
                    e,
                )
        return events

    def search_multi(
        self,
        queries: list[str],
        display: int = 10,
    ) -> list[NaverNewsItem]:
        """복수 쿼리 순차 검색. rate_limiter 는 search_news() 내부에서 처리."""
        results: list[NaverNewsItem] = []
        for q in queries:
            results.extend(self.search_news(q, display=display))
        return results

    # ------------------------------------------------------------------ #
    # 내부 변환 헬퍼
    # ------------------------------------------------------------------ #

    def _parse_item(self, raw: dict[str, Any]) -> NaverNewsItem:
        """raw JSON item dict -> NaverNewsItem (HTML strip, pubDate 파싱)."""
        title = _strip_html(raw.get("title", "")).strip()
        description = _strip_html(raw.get("description", "")).strip()
        pub_date = _parse_rfc2822(raw.get("pubDate", ""))

        return NaverNewsItem(
            title=title,
            description=description,
            original_link=raw.get("originallink", ""),
            naver_link=raw.get("link", ""),
            pub_date=pub_date,
        )

    @staticmethod
    def _item_to_normalizer_input(item: NaverNewsItem, query: str) -> dict[str, Any]:
        """NaverNewsItem -> EventNormalizer._normalize_naver_news 입력 포맷 변환.

        _normalize_naver_news 필수 필드: title, published_at
        선택 필드: summary, link, _query
        """
        return {
            "title": item.title,
            "published_at": item.pub_date.isoformat(),
            "summary": item.description,
            "link": item.naver_link,
            "_query": query,
            "original_link": item.original_link,
        }

    # ------------------------------------------------------------------ #
    # HTTP 레이어
    # ------------------------------------------------------------------ #

    def _http_get_with_retry(
        self,
        url: str,
        headers: dict[str, Any],
        params: dict[str, Any],
    ) -> dict[str, Any] | None:
        """HTTP GET + 지수 백오프 재시도. 최종 실패 시 None 반환.

        재시도 횟수: max_retries (risk_config.yaml connector_defaults)
        백오프: backoff_base ** attempt (attempt=0 -> 1s, attempt=1 -> 2s, ...)
        """
        last_err: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                resp = requests.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=self._timeout_sec,
                )
                resp.raise_for_status()
                return resp.json()

            except requests.Timeout as e:
                last_err = e
                logger.warning(
                    "[naver_rest] timeout (시도 %d/%d): %s",
                    attempt + 1,
                    self._max_retries,
                    e,
                )

            except requests.HTTPError as e:
                last_err = e
                status = getattr(e.response, "status_code", None)
                if status == 429:
                    logger.warning(
                        "[naver_rest] 429 rate limit 응답 (시도 %d/%d)",
                        attempt + 1,
                        self._max_retries,
                    )
                elif status is not None and 500 <= status < 600:
                    logger.warning(
                        "[naver_rest] 5xx %s 서버 오류 (시도 %d/%d)",
                        status,
                        attempt + 1,
                        self._max_retries,
                    )
                else:
                    # 4xx 등 비재시도 오류 (401, 403, 400 등)
                    logger.warning(
                        "[naver_rest] HTTP %s 오류 (시도 %d/%d): %s",
                        status,
                        attempt + 1,
                        self._max_retries,
                        e,
                    )
                    # 클라이언트 오류는 재시도해도 무의미하므로 즉시 중단
                    return None

            except requests.RequestException as e:
                last_err = e
                logger.warning(
                    "[naver_rest] 요청 오류 (시도 %d/%d): %s",
                    attempt + 1,
                    self._max_retries,
                    e,
                )

            if attempt < self._max_retries - 1:
                sleep_sec = self._backoff_base ** attempt
                logger.info("[naver_rest] %ds 후 재시도.", sleep_sec)
                time.sleep(sleep_sec)

        logger.error(
            "[naver_rest] %d회 재시도 후 최종 실패. url=%s last_err=%s",
            self._max_retries,
            url,
            last_err,
        )
        return None

    # ------------------------------------------------------------------ #
    # Mock
    # ------------------------------------------------------------------ #

    @staticmethod
    def _mock_search(query: str, display: int) -> list[NaverNewsItem]:
        """Mock 응답 (환경변수 미설정 또는 테스트용). 최대 3개 반환."""
        now = datetime.now().astimezone()
        count = min(display, 3)
        return [
            NaverNewsItem(
                title=f"[MOCK] {query} 관련 뉴스 {i + 1}",
                description=f"Mock description for query='{query}' item {i + 1}",
                original_link=f"https://mock.example.com/article/{i + 1}",
                naver_link=f"https://news.naver.com/mock/{i + 1}",
                pub_date=now,
            )
            for i in range(count)
        ]
