"""NaverNewsClient unit tests. S2-3 실구현 검증.

coverage:
  - mock 모드: env 없으면 _is_mock=True, 최대 3개 반환
  - real 모드: env 있으면 _is_mock=False
  - search_news 파라미터 검증 (display/start/sort 범위)
  - _parse_item: HTML 태그 제거, HTML entity 디코드, pubDate 파싱
  - _parse_item: pubDate 형식 이상 시 현재 시각 fallback
  - _http_get_with_retry: timeout 후 성공
  - _http_get_with_retry: 429 백오프 재시도
  - _http_get_with_retry: 5xx 백오프 재시도
  - _http_get_with_retry: max_retries 소진 None 반환
  - _http_get_with_retry: 4xx 즉시 None 반환 (재시도 없음)
  - rate_limiter 호출 확인 (real 모드)
  - search_and_normalize: C2 event dict 반환
  - search_and_normalize: 정규화 실패 skip
  - search_multi: 복수 쿼리 결과 합산

환경변수 없이 통과하도록 AuthManager.get_naver_client 는 모두 mock.
occurred_at은 과거 시간 사용 (PIT-Safety 통과).
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests as req_lib

# ------------------------------------------------------------------ #
# 공통 상수
# ------------------------------------------------------------------ #

_PAST_PUB_DATE = "Mon, 21 Apr 2025 09:00:00 +0900"  # 과거 날짜 (PIT-Safety 통과)
_PAST_PUB_ISO = "2025-04-21T09:00:00+09:00"

C2_REQUIRED_FIELDS = (
    "event_id",
    "source",
    "event_type",
    "scope",
    "title",
    "summary",
    "occurred_at",
    "ingest_ts",
    "priority",
    "llm_required",
    "ttl",
    "expires_at",
    "supersedes",
    "payload",
    "pit_safe",
)


def _make_naver_item_raw(
    title: str = "삼성전자 <b>영업이익</b> 사상 최고",
    description: str = "삼성전자가 &amp; Q4 영업이익...",
    pub_date: str = _PAST_PUB_DATE,
    original_link: str = "https://news.example.com/1",
    link: str = "https://news.naver.com/1",
) -> dict[str, Any]:
    return {
        "title": title,
        "description": description,
        "pubDate": pub_date,
        "originallink": original_link,
        "link": link,
    }


def _make_naver_response(items: list[dict[str, Any]], total: int = 1) -> dict[str, Any]:
    return {
        "lastBuildDate": "Mon, 21 Apr 2025 09:05:00 +0900",
        "total": total,
        "start": 1,
        "display": len(items),
        "items": items,
    }


# ------------------------------------------------------------------ #
# 픽스처
# ------------------------------------------------------------------ #

@pytest.fixture
def mock_auth():
    """AuthManager mock: get_naver_client() EnvironmentError 발생 -> mock 모드."""
    auth = MagicMock()
    auth.get_naver_client.side_effect = EnvironmentError("NAVER_CLIENT_ID 누락")
    return auth


@pytest.fixture
def real_auth():
    """AuthManager mock: 인증 성공 -> real 모드."""
    auth = MagicMock()
    auth.get_naver_client.return_value = ("test_client_id", "test_client_secret")
    return auth


@pytest.fixture
def mock_rate_limiter():
    return MagicMock()


@pytest.fixture
def mock_normalizer():
    normalizer = MagicMock()
    normalizer.normalize.return_value = {
        "event_id": "EVT-test-001",
        "source": "naver_news",
        "event_type": "news",
        "scope": "market",
        "title": "테스트 뉴스",
        "summary": "요약",
        "occurred_at": _PAST_PUB_ISO,
        "ingest_ts": _PAST_PUB_ISO,
        "priority": "normal",
        "llm_required": True,
        "ttl": 1800,
        "expires_at": _PAST_PUB_ISO,
        "supersedes": None,
        "payload": {},
        "pit_safe": True,
    }
    return normalizer


@pytest.fixture
def client_mock_mode(mock_auth, mock_rate_limiter):
    """NAVER 키 없는 mock 모드 클라이언트."""
    from src.connectors.naver_rest import NaverNewsClient
    return NaverNewsClient(auth=mock_auth, rate_limiter=mock_rate_limiter)


@pytest.fixture
def client_real_mode(real_auth, mock_rate_limiter, mock_normalizer):
    """NAVER 키 있는 real 모드 클라이언트 (requests 는 별도 mock)."""
    from src.connectors.naver_rest import NaverNewsClient
    return NaverNewsClient(
        auth=real_auth,
        rate_limiter=mock_rate_limiter,
        normalizer=mock_normalizer,
    )


# ------------------------------------------------------------------ #
# 1. Mock 모드 동작
# ------------------------------------------------------------------ #

def test_mock_mode_when_no_env(mock_auth, mock_rate_limiter):
    """env 없으면 _is_mock=True."""
    from src.connectors.naver_rest import NaverNewsClient
    client = NaverNewsClient(auth=mock_auth, rate_limiter=mock_rate_limiter)
    assert client._is_mock is True


def test_mock_returns_up_to_3_items(client_mock_mode):
    """mock 모드는 display 에 관계없이 최대 3개 반환."""
    items = client_mock_mode.search_news("삼성전자", display=10)
    assert len(items) == 3


def test_mock_returns_display_items_when_lt3(client_mock_mode):
    """display=2 이면 2개 반환."""
    items = client_mock_mode.search_news("SK하이닉스", display=2)
    assert len(items) == 2


def test_mock_title_contains_query(client_mock_mode):
    """mock 제목에 쿼리 포함."""
    items = client_mock_mode.search_news("현대차", display=1)
    assert "현대차" in items[0].title


def test_mock_rate_limiter_not_called(client_mock_mode, mock_rate_limiter):
    """mock 모드에서는 rate_limiter 호출 없음."""
    client_mock_mode.search_news("테스트")
    mock_rate_limiter.wait_and_acquire.assert_not_called()


# ------------------------------------------------------------------ #
# 2. Real 모드 초기화
# ------------------------------------------------------------------ #

def test_real_mode_when_env_set(real_auth, mock_rate_limiter):
    """env 있으면 _is_mock=False."""
    from src.connectors.naver_rest import NaverNewsClient
    client = NaverNewsClient(auth=real_auth, rate_limiter=mock_rate_limiter)
    assert client._is_mock is False


# ------------------------------------------------------------------ #
# 3. 파라미터 검증 (ValueError)
# ------------------------------------------------------------------ #

def test_search_news_invalid_display_zero(client_mock_mode):
    with pytest.raises(ValueError, match="display"):
        client_mock_mode.search_news("테스트", display=0)


def test_search_news_invalid_display_over_100(client_mock_mode):
    with pytest.raises(ValueError, match="display"):
        client_mock_mode.search_news("테스트", display=101)


def test_search_news_invalid_start_zero(client_mock_mode):
    with pytest.raises(ValueError, match="start"):
        client_mock_mode.search_news("테스트", start=0)


def test_search_news_invalid_start_over_1000(client_mock_mode):
    with pytest.raises(ValueError, match="start"):
        client_mock_mode.search_news("테스트", start=1001)


def test_search_news_invalid_sort(client_mock_mode):
    with pytest.raises(ValueError, match="sort"):
        client_mock_mode.search_news("테스트", sort="invalid")


# ------------------------------------------------------------------ #
# 4. _parse_item: HTML 처리 + pubDate 파싱
# ------------------------------------------------------------------ #

def test_parse_item_strips_html_tags(client_real_mode):
    """HTML 태그 제거."""
    raw = _make_naver_item_raw(title="<b>삼성전자</b> 실적 발표")
    item = client_real_mode._parse_item(raw)
    assert "<b>" not in item.title
    assert "삼성전자" in item.title


def test_parse_item_decodes_html_entities(client_real_mode):
    """HTML entity 디코드."""
    raw = _make_naver_item_raw(description="삼성전자 &amp; SK하이닉스 동반 상승")
    item = client_real_mode._parse_item(raw)
    assert "&" in item.description
    assert "&amp;" not in item.description


def test_parse_item_strips_nested_html(client_real_mode):
    """중첩 HTML 태그 처리."""
    raw = _make_naver_item_raw(title="<span class='x'><b>뉴스</b></span> 제목")
    item = client_real_mode._parse_item(raw)
    assert "뉴스" in item.title
    assert "<" not in item.title


def test_parse_item_valid_pubdate(client_real_mode):
    """RFC 2822 pubDate 올바르게 파싱."""
    raw = _make_naver_item_raw(pub_date=_PAST_PUB_DATE)
    item = client_real_mode._parse_item(raw)
    assert item.pub_date.year == 2025
    assert item.pub_date.month == 4
    assert item.pub_date.day == 21
    assert item.pub_date.tzinfo is not None


def test_parse_item_handles_invalid_pubdate(client_real_mode):
    """pubDate 형식 이상 시 현재 시각 fallback (예외 없음)."""
    raw = _make_naver_item_raw(pub_date="not-a-date")
    item = client_real_mode._parse_item(raw)
    assert item.pub_date is not None
    assert item.pub_date.tzinfo is not None


def test_parse_item_empty_pubdate(client_real_mode):
    """pubDate 빈 문자열 -> fallback."""
    raw = _make_naver_item_raw(pub_date="")
    item = client_real_mode._parse_item(raw)
    assert item.pub_date is not None


def test_parse_item_links(client_real_mode):
    """original_link, naver_link 올바르게 매핑."""
    raw = _make_naver_item_raw(
        original_link="https://orig.com/1",
        link="https://naver.com/1",
    )
    item = client_real_mode._parse_item(raw)
    assert item.original_link == "https://orig.com/1"
    assert item.naver_link == "https://naver.com/1"


# ------------------------------------------------------------------ #
# 5. HTTP 재시도 / 백오프 (real 모드)
# ------------------------------------------------------------------ #

def test_http_get_with_retry_timeout_then_success(client_real_mode):
    """첫 호출 timeout, 두 번째 성공."""
    good_resp = MagicMock()
    good_resp.json.return_value = _make_naver_response([_make_naver_item_raw()])
    good_resp.raise_for_status.return_value = None

    timeout_exc = req_lib.Timeout("timeout")

    with patch("src.connectors.naver_rest.requests.get") as mock_get, \
         patch("src.connectors.naver_rest.time.sleep") as mock_sleep:
        mock_get.side_effect = [timeout_exc, good_resp]
        items = client_real_mode.search_news("테스트")

    assert len(items) == 1
    mock_sleep.assert_called_once()


def test_http_get_with_retry_429_backoff(client_real_mode):
    """429 응답 시 재시도."""
    http_429 = req_lib.HTTPError(response=MagicMock(status_code=429))
    good_resp = MagicMock()
    good_resp.json.return_value = _make_naver_response([_make_naver_item_raw()])
    good_resp.raise_for_status.return_value = None

    with patch("src.connectors.naver_rest.requests.get") as mock_get, \
         patch("src.connectors.naver_rest.time.sleep"):
        mock_get.side_effect = [http_429, good_resp]
        items = client_real_mode.search_news("테스트")

    assert len(items) == 1


def test_http_get_with_retry_5xx_backoff(client_real_mode):
    """5xx 서버 오류 시 재시도."""
    http_503 = req_lib.HTTPError(response=MagicMock(status_code=503))
    good_resp = MagicMock()
    good_resp.json.return_value = _make_naver_response([_make_naver_item_raw()])
    good_resp.raise_for_status.return_value = None

    with patch("src.connectors.naver_rest.requests.get") as mock_get, \
         patch("src.connectors.naver_rest.time.sleep"):
        mock_get.side_effect = [http_503, good_resp]
        items = client_real_mode.search_news("테스트")

    assert len(items) == 1


def test_http_get_with_retry_max_retries_returns_none(client_real_mode):
    """max_retries 회 전부 timeout -> None 반환 -> search_news 빈 리스트."""
    timeout_exc = req_lib.Timeout("timeout")

    with patch("src.connectors.naver_rest.requests.get") as mock_get, \
         patch("src.connectors.naver_rest.time.sleep"):
        # max_retries=3 이면 총 3회 시도
        mock_get.side_effect = [timeout_exc] * 3
        items = client_real_mode.search_news("테스트")

    assert items == []


def test_http_get_4xx_returns_none_immediately(client_real_mode):
    """4xx (401) 시 즉시 None -> 재시도 없음."""
    http_401 = req_lib.HTTPError(response=MagicMock(status_code=401))

    with patch("src.connectors.naver_rest.requests.get") as mock_get, \
         patch("src.connectors.naver_rest.time.sleep") as mock_sleep:
        mock_get.side_effect = http_401
        items = client_real_mode.search_news("테스트")

    assert items == []
    mock_sleep.assert_not_called()  # 재시도 없으므로 sleep 없음


# ------------------------------------------------------------------ #
# 6. rate_limiter 호출 확인 (real 모드)
# ------------------------------------------------------------------ #

def test_rate_limiter_invoked_per_call(client_real_mode, mock_rate_limiter):
    """real 모드에서 search_news 호출마다 wait_and_acquire 1회 호출."""
    good_resp = MagicMock()
    good_resp.json.return_value = _make_naver_response([_make_naver_item_raw()])
    good_resp.raise_for_status.return_value = None

    with patch("src.connectors.naver_rest.requests.get", return_value=good_resp):
        client_real_mode.search_news("테스트1")
        client_real_mode.search_news("테스트2")

    assert mock_rate_limiter.wait_and_acquire.call_count == 2


# ------------------------------------------------------------------ #
# 7. search_and_normalize: C2 이벤트 반환
# ------------------------------------------------------------------ #

def test_search_and_normalize_returns_c2_events(client_real_mode, mock_normalizer):
    """search_and_normalize -> C2 event dict 반환. 필수 필드 확인."""
    good_resp = MagicMock()
    good_resp.json.return_value = _make_naver_response([_make_naver_item_raw()])
    good_resp.raise_for_status.return_value = None

    with patch("src.connectors.naver_rest.requests.get", return_value=good_resp):
        events = client_real_mode.search_and_normalize("삼성전자")

    assert len(events) == 1
    for field in C2_REQUIRED_FIELDS:
        assert field in events[0], f"C2 필수 필드 누락: {field}"
    assert events[0]["source"] == "naver_news"
    assert events[0]["event_type"] == "news"


def test_search_and_normalize_skips_malformed(client_real_mode, mock_normalizer):
    """normalize 실패 시 skip (예외 전파 없음)."""
    mock_normalizer.normalize.side_effect = Exception("정규화 오류")

    good_resp = MagicMock()
    good_resp.json.return_value = _make_naver_response(
        [_make_naver_item_raw(), _make_naver_item_raw(title="뉴스2")]
    )
    good_resp.raise_for_status.return_value = None

    with patch("src.connectors.naver_rest.requests.get", return_value=good_resp):
        events = client_real_mode.search_and_normalize("테스트")

    # 모두 실패 시 빈 리스트 반환 (예외 없음)
    assert events == []


def test_search_and_normalize_normalizer_input_fields(client_real_mode, mock_normalizer):
    """normalizer.normalize() 에 전달되는 raw 가 published_at 필드를 포함하는지 확인."""
    good_resp = MagicMock()
    good_resp.json.return_value = _make_naver_response([_make_naver_item_raw()])
    good_resp.raise_for_status.return_value = None

    with patch("src.connectors.naver_rest.requests.get", return_value=good_resp):
        client_real_mode.search_and_normalize("SK하이닉스")

    # normalizer.normalize 호출 확인
    call_args = mock_normalizer.normalize.call_args
    # normalize(raw_event, source) -> positional args
    args = call_args.args if hasattr(call_args, "args") else call_args[0]
    raw_arg = args[0]
    source_arg = args[1] if len(args) > 1 else call_args.kwargs.get("source")
    assert "title" in raw_arg
    assert "published_at" in raw_arg  # EventNormalizer._normalize_naver_news 필수 필드
    assert source_arg == "naver_news"


# ------------------------------------------------------------------ #
# 8. search_multi
# ------------------------------------------------------------------ #

def test_search_multi_aggregates_results(client_mock_mode):
    """search_multi 2개 쿼리 -> 결과 합산."""
    items = client_mock_mode.search_multi(["삼성전자", "SK하이닉스"], display=2)
    # 각 쿼리당 min(2, 3)=2 -> 총 4개
    assert len(items) == 4


def test_search_multi_empty_queries(client_mock_mode):
    """빈 쿼리 리스트 -> 빈 결과."""
    items = client_mock_mode.search_multi([])
    assert items == []


# ------------------------------------------------------------------ #
# 9. PIT-Safety: PITViolationError drop 경로
# ------------------------------------------------------------------ #

def test_search_and_normalize_skips_pit_violation(mock_auth, mock_rate_limiter):
    """EventNormalizer 가 PITViolationError raise 시 search_and_normalize 는 drop + 빈 리스트 반환."""
    from unittest.mock import MagicMock
    from src.data.event_normalizer import PITViolationError
    from src.connectors.naver_rest import NaverNewsClient

    mock_normalizer = MagicMock()
    mock_normalizer.normalize.side_effect = PITViolationError("test violation")

    client = NaverNewsClient(
        auth=mock_auth,
        rate_limiter=mock_rate_limiter,
        normalizer=mock_normalizer,
    )
    # mock 모드: search_news 는 min(display, 3) 개 NaverNewsItem 반환
    events = client.search_and_normalize("삼성전자", display=3)

    # PIT 위반이므로 전부 drop -> 빈 리스트
    assert events == []
    # normalize 가 mock items 수(3개)만큼 호출됨
    assert mock_normalizer.normalize.call_count == 3
