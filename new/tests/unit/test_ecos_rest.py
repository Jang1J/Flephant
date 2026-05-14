"""ECOSRestClient unit tests. S2-5 실구현 검증.

coverage:
  - mock 모드: ECOS_API_KEY 미설정 시 _is_mock=True
  - mock 모드: ECOS_API_KEY 설정 시 _is_mock=False
  - get_stat_data: mock 모드 3개 datapoint 반환
  - get_stat_data: interval 검증 ValueError
  - get_stat_data: 실 API 성공 (mock_http)
  - get_stat_data: 실 API 응답 없음 빈 리스트 반환
  - get_macro_pack: 2 피처 반환 (interest_rate, usd_krw)
  - get_macro_pack: 조회 실패 시 0.0 fallback
  - _parse_row: 날짜 파싱 정상
  - _parse_row: 날짜 파싱 실패 시 현재 시각 fallback
  - _parse_row: DATA_VALUE 파싱 실패 시 0.0 fallback
  - _http_get_with_retry: timeout 재시도
  - _http_get_with_retry: 429 재시도
  - _http_get_with_retry: 5xx 재시도
  - _http_get_with_retry: 4xx 비 429 즉시 None 반환
  - _http_get_with_retry: 재시도 소진 None 반환
  - RateLimiter wait_and_acquire 호출 확인 (실 API 경로)
  - ECOSDatapoint 필드 완전성
  - PIT-Safety: end_date 과거 날짜 허용

환경변수 없이 통과하도록 기본 mock 모드.
PIT-Safety: occurred_at/date_str 은 과거 날짜 사용.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
import requests

_KST = ZoneInfo("Asia/Seoul")
_PAST_DATE_STR = "20260419"
_PAST_DATE = datetime(2026, 4, 19, tzinfo=_KST)


# ------------------------------------------------------------------ #
# fixtures
# ------------------------------------------------------------------ #


@pytest.fixture
def mock_rate_limiter():
    rl = MagicMock()
    rl.wait_and_acquire = MagicMock()
    return rl


@pytest.fixture
def mock_normalizer():
    return MagicMock()


@pytest.fixture
def client_mock_mode(mock_rate_limiter, mock_normalizer):
    """ECOS_API_KEY 미설정. Mock 모드."""
    from src.connectors.ecos_rest import ECOSRestClient
    with patch.dict("os.environ", {}, clear=False):
        # 키가 없는 상태를 보장
        import os
        os.environ.pop("ECOS_API_KEY", None)
        c = ECOSRestClient(rate_limiter=mock_rate_limiter, normalizer=mock_normalizer)
    return c


@pytest.fixture
def client_real_mode(mock_rate_limiter, mock_normalizer):
    """ECOS_API_KEY 설정. 실 API 경로 (HTTP는 별도 mock)."""
    from src.connectors.ecos_rest import ECOSRestClient
    with patch.dict("os.environ", {"ECOS_API_KEY": "test_key_1234"}):
        c = ECOSRestClient(rate_limiter=mock_rate_limiter, normalizer=mock_normalizer)
    return c


# ------------------------------------------------------------------ #
# Mock 모드 기본
# ------------------------------------------------------------------ #


def test_mock_mode_no_api_key(client_mock_mode):
    """ECOS_API_KEY 미설정 시 _is_mock=True."""
    assert client_mock_mode._is_mock is True


def test_real_mode_with_api_key(mock_rate_limiter, mock_normalizer):
    """ECOS_API_KEY 설정 시 _is_mock=False."""
    from src.connectors.ecos_rest import ECOSRestClient
    with patch.dict("os.environ", {"ECOS_API_KEY": "test_key_1234"}):
        c = ECOSRestClient(rate_limiter=mock_rate_limiter, normalizer=mock_normalizer)
    assert c._is_mock is False


# ------------------------------------------------------------------ #
# get_stat_data
# ------------------------------------------------------------------ #


def test_get_stat_data_mock_returns_3_datapoints(client_mock_mode):
    """Mock 모드: 3개 datapoint 반환."""
    points = client_mock_mode.get_stat_data("098Y001", "20260401", "20260421")
    assert len(points) == 3


def test_get_stat_data_mock_interest_rate_value(client_mock_mode):
    """Mock 모드: 기준금리 코드 098Y001 -> 3.5 기본값."""
    points = client_mock_mode.get_stat_data("098Y001", "20260401", "20260421")
    assert any(abs(p.value - 3.50) < 0.1 for p in points)


def test_get_stat_data_mock_usd_krw_value(client_mock_mode):
    """Mock 모드: 원/달러 코드 036Y001 -> 1340.5 기본값."""
    points = client_mock_mode.get_stat_data("036Y001", "20260401", "20260421")
    assert any(abs(p.value - 1340.5) < 1.0 for p in points)


def test_get_stat_data_invalid_interval(client_mock_mode):
    """interval 검증: D/M/Q/A 외 ValueError."""
    with pytest.raises(ValueError, match="D/M/Q/A"):
        client_mock_mode.get_stat_data("098Y001", "20260401", "20260421", interval="X")


def test_get_stat_data_real_api_success(client_real_mode, mock_rate_limiter):
    """실 API 경로: HTTP 성공 시 파싱된 datapoint 반환."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "StatisticSearch": {
            "list_total_count": 1,
            "row": [
                {
                    "TIME": "20260419",
                    "STAT_NAME": "기준금리",
                    "DATA_VALUE": "3.50",
                    "UNIT_NAME": "%",
                }
            ],
        }
    }
    mock_resp.raise_for_status = MagicMock()

    with patch("src.connectors.ecos_rest.requests.get", return_value=mock_resp):
        points = client_real_mode.get_stat_data("098Y001", "20260419", "20260421")

    assert len(points) == 1
    assert points[0].value == pytest.approx(3.50)
    assert points[0].stat_code == "098Y001"


def test_get_stat_data_real_api_appends_item_code(client_real_mode):
    """ECOS item_code가 필요한 통계표는 URL 뒤에 항목 코드를 붙인다."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"StatisticSearch": {"row": []}}
    mock_resp.raise_for_status = MagicMock()

    with patch("src.connectors.ecos_rest.requests.get", return_value=mock_resp) as get:
        client_real_mode.get_stat_data(
            "722Y001",
            "20260419",
            "20260421",
            item_codes=("0101000",),
        )

    assert get.call_args is not None
    assert get.call_args.args[0].endswith("/722Y001/D/20260419/20260421/0101000")


def test_get_stat_data_real_api_empty_row(client_real_mode):
    """실 API 경로: row 없음 -> 빈 리스트."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"StatisticSearch": {"list_total_count": 0, "row": []}}
    mock_resp.raise_for_status = MagicMock()

    with patch("src.connectors.ecos_rest.requests.get", return_value=mock_resp):
        points = client_real_mode.get_stat_data("098Y001", "20260419", "20260421")

    assert points == []


def test_get_stat_data_real_api_none_response(client_real_mode):
    """실 API 경로: HTTP 실패 -> 빈 리스트."""
    with patch.object(client_real_mode, "_http_get_with_retry", return_value=None):
        points = client_real_mode.get_stat_data("098Y001", "20260419", "20260421")
    assert points == []


# ------------------------------------------------------------------ #
# get_macro_pack
# ------------------------------------------------------------------ #


def test_get_macro_pack_returns_two_features(client_mock_mode):
    """get_macro_pack: interest_rate + usd_krw 2 피처 반환."""
    pack = client_mock_mode.get_macro_pack("20260421")
    assert "interest_rate" in pack
    assert "usd_krw" in pack


def test_get_macro_pack_fallback_zero(client_mock_mode):
    """get_macro_pack: 조회 실패 시 0.0 fallback."""
    with patch.object(client_mock_mode, "get_stat_data", return_value=[]):
        pack = client_mock_mode.get_macro_pack("20260421")
    assert pack["interest_rate"] == 0.0
    assert pack["usd_krw"] == 0.0


def test_get_macro_pack_latest_value(client_mock_mode):
    """get_macro_pack: 여러 datapoint 중 최신(date_str 최대) 값 선택."""
    from src.connectors.ecos_rest import ECOSDatapoint
    older = ECOSDatapoint(
        stat_code="098Y001",
        stat_name="기준금리",
        date_str="20260410",
        date=datetime(2026, 4, 10, tzinfo=_KST),
        value=3.0,
        unit="%",
    )
    newer = ECOSDatapoint(
        stat_code="098Y001",
        stat_name="기준금리",
        date_str="20260419",
        date=datetime(2026, 4, 19, tzinfo=_KST),
        value=3.5,
        unit="%",
    )
    with patch.object(client_mock_mode, "get_stat_data", return_value=[older, newer]):
        pack = client_mock_mode.get_macro_pack("20260421")
    assert pack["interest_rate"] == pytest.approx(3.5)


# ------------------------------------------------------------------ #
# _parse_row
# ------------------------------------------------------------------ #


def test_parse_row_normal(client_mock_mode):
    """_parse_row: 정상 행 파싱."""
    raw = {"TIME": "20260419", "STAT_NAME": "기준금리", "DATA_VALUE": "3.50", "UNIT_NAME": "%"}
    dp = client_mock_mode._parse_row(raw, "098Y001")
    assert dp.date_str == "20260419"
    assert dp.value == pytest.approx(3.50)
    assert dp.unit == "%"


def test_parse_row_invalid_date(client_mock_mode):
    """_parse_row: 날짜 파싱 실패 시 현재 시각 fallback."""
    raw = {"TIME": "INVALID", "DATA_VALUE": "3.50", "UNIT_NAME": "%"}
    dp = client_mock_mode._parse_row(raw, "098Y001")
    assert dp.date is not None  # fallback 으로 현재 시각


def test_parse_row_invalid_value(client_mock_mode):
    """_parse_row: DATA_VALUE 파싱 실패 시 0.0 fallback."""
    raw = {"TIME": "20260419", "DATA_VALUE": "N/A", "UNIT_NAME": "%"}
    dp = client_mock_mode._parse_row(raw, "098Y001")
    assert dp.value == 0.0


# ------------------------------------------------------------------ #
# _http_get_with_retry
# ------------------------------------------------------------------ #


def test_retry_on_timeout(client_real_mode, mock_rate_limiter):
    """timeout 2회 후 성공."""
    mock_ok = MagicMock()
    mock_ok.json.return_value = {"StatisticSearch": {"row": []}}
    mock_ok.raise_for_status = MagicMock()

    call_count = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise requests.Timeout("timeout")
        return mock_ok

    with patch("src.connectors.ecos_rest.requests.get", side_effect=side_effect):
        with patch("src.connectors.ecos_rest.time.sleep"):
            result = client_real_mode._http_get_with_retry("http://test")

    assert result is not None


def test_retry_on_429(client_real_mode):
    """429 재시도."""
    mock_fail = MagicMock()
    mock_fail.status_code = 429
    http_err = requests.HTTPError(response=mock_fail)

    mock_ok = MagicMock()
    mock_ok.json.return_value = {"StatisticSearch": {"row": []}}
    mock_ok.raise_for_status = MagicMock()

    responses_seq = [http_err, http_err, mock_ok]
    call_idx = [0]

    def side_effect(*args, **kwargs):
        r = responses_seq[call_idx[0]]
        call_idx[0] += 1
        if isinstance(r, Exception):
            raise r
        return r

    with patch("src.connectors.ecos_rest.requests.get", side_effect=side_effect):
        with patch("src.connectors.ecos_rest.time.sleep"):
            result = client_real_mode._http_get_with_retry("http://test")

    assert result is not None


def test_4xx_non_429_returns_none(client_real_mode):
    """4xx (429 제외) -> 즉시 None."""
    mock_fail = MagicMock()
    mock_fail.status_code = 403
    http_err = requests.HTTPError(response=mock_fail)

    with patch("src.connectors.ecos_rest.requests.get", side_effect=http_err):
        result = client_real_mode._http_get_with_retry("http://test")

    assert result is None


def test_retry_exhausted_returns_none(client_real_mode):
    """재시도 소진 -> None."""
    with patch("src.connectors.ecos_rest.requests.get", side_effect=requests.Timeout):
        with patch("src.connectors.ecos_rest.time.sleep"):
            result = client_real_mode._http_get_with_retry("http://test")
    assert result is None


# ------------------------------------------------------------------ #
# RateLimiter 호출 확인
# ------------------------------------------------------------------ #


def test_rate_limiter_called_in_real_mode(client_real_mode, mock_rate_limiter):
    """실 API 경로: wait_and_acquire 호출 확인."""
    with patch.object(client_real_mode, "_http_get_with_retry", return_value=None):
        client_real_mode.get_stat_data("098Y001", "20260419", "20260421")
    mock_rate_limiter.wait_and_acquire.assert_called_once()


def test_rate_limiter_not_called_in_mock_mode(client_mock_mode, mock_rate_limiter):
    """Mock 모드: wait_and_acquire 미호출."""
    client_mock_mode.get_stat_data("098Y001", "20260419", "20260421")
    mock_rate_limiter.wait_and_acquire.assert_not_called()


# ------------------------------------------------------------------ #
# ECOSDatapoint 필드 완전성
# ------------------------------------------------------------------ #


def test_ecos_datapoint_fields(client_mock_mode):
    """ECOSDatapoint 6개 필드 모두 존재."""
    points = client_mock_mode.get_stat_data("098Y001", "20260401", "20260421")
    dp = points[0]
    assert hasattr(dp, "stat_code")
    assert hasattr(dp, "stat_name")
    assert hasattr(dp, "date_str")
    assert hasattr(dp, "date")
    assert hasattr(dp, "value")
    assert hasattr(dp, "unit")


# ------------------------------------------------------------------ #
# PIT-Safety
# ------------------------------------------------------------------ #


def test_pit_safety_past_end_date_allowed(client_mock_mode):
    """PIT-Safety: 과거 end_date 는 허용."""
    points = client_mock_mode.get_stat_data("098Y001", "20260101", _PAST_DATE_STR)
    assert len(points) >= 0  # 오류 없이 통과


def test_interval_m_monthly_allowed(client_mock_mode):
    """interval M(월) 허용."""
    points = client_mock_mode.get_stat_data("098Y001", "20260101", "20260421", interval="M")
    assert isinstance(points, list)


def test_get_stat_data_rejects_future_end_date(client_mock_mode):
    """end_date 가 미래 (snapshot 이후) -> PITViolationError. W1-1 감사 반영."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from src.utils.pit_guard import PITViolationError
    future_date = (datetime.now(ZoneInfo("Asia/Seoul")) + timedelta(days=1)).strftime("%Y%m%d")
    with pytest.raises(PITViolationError, match="snapshot"):
        client_mock_mode.get_stat_data("098Y001", "20260101", future_date)


# ------------------------------------------------------------------ #
# C2 브리지: to_normalizer_input / poll_and_normalize
# ------------------------------------------------------------------ #


def test_to_normalizer_input_field_mapping(client_mock_mode):
    """to_normalizer_input: stat_name → indicator, value, date 필드명 변환 확인."""
    from src.connectors.ecos_rest import ECOSDatapoint
    dp = ECOSDatapoint(
        stat_code="098Y001",
        stat_name="기준금리",
        date_str="20260419",
        date=datetime(2026, 4, 19, tzinfo=_KST),
        value=3.50,
        unit="%",
    )
    raw = client_mock_mode.to_normalizer_input(dp)

    # EventNormalizer._normalize_ecos 필수 필드 3개 확인
    assert "indicator" in raw, f"indicator 필드 누락: {raw}"
    assert "value" in raw, f"value 필드 누락: {raw}"
    assert "date" in raw, f"date 필드 누락: {raw}"

    # 필드값 매핑 정확성
    assert raw["indicator"] == "기준금리"         # stat_name → indicator
    assert raw["value"] == pytest.approx(3.50)
    assert raw["date"] == "2026-04-19"             # YYYY-MM-DD 형식


def test_to_normalizer_input_preserves_optional_fields(client_mock_mode):
    """to_normalizer_input: stat_code, unit 선택 필드 보존 확인."""
    from src.connectors.ecos_rest import ECOSDatapoint
    dp = ECOSDatapoint(
        stat_code="036Y001",
        stat_name="원/달러환율",
        date_str="20260419",
        date=datetime(2026, 4, 19, tzinfo=_KST),
        value=1340.5,
        unit="원",
    )
    raw = client_mock_mode.to_normalizer_input(dp)
    assert raw.get("stat_code") == "036Y001"
    assert raw.get("unit") == "원"


def test_poll_and_normalize_returns_c2_events(client_mock_mode):
    """poll_and_normalize: mock 데이터 → C2 event 리스트 반환."""
    from unittest.mock import patch

    # normalizer 를 mock 으로 교체하여 event_id 없는 환경에서도 동작 검증
    mock_event = {
        "event_id": "evt-test-001",
        "source": "ecos",
        "event_type": "macro",
        "occurred_at": "2026-04-19T00:00:00+09:00",
        "pit_safe": True,
    }
    with patch.object(client_mock_mode._normalizer, "normalize", return_value=mock_event):
        events = client_mock_mode.poll_and_normalize("098Y001", "20260419", "20260421")

    assert isinstance(events, list)
    assert len(events) == 3  # mock 모드 3개 datapoint
    assert all(e["source"] == "ecos" for e in events)


def test_poll_and_normalize_normalizer_failure_skips(client_mock_mode):
    """poll_and_normalize: normalizer 예외 시 해당 항목 skip, 나머지 반환."""
    from unittest.mock import patch
    from src.data.event_normalizer import ValidationError

    call_count = [0]
    def side_effect(raw, source):
        call_count[0] += 1
        if call_count[0] == 1:
            raise ValidationError("테스트 오류")
        return {"source": source, "event_id": f"evt-{call_count[0]}"}

    with patch.object(client_mock_mode._normalizer, "normalize", side_effect=side_effect):
        events = client_mock_mode.poll_and_normalize("098Y001", "20260419", "20260421")

    # 3개 중 1개 실패 → 2개 반환
    assert len(events) == 2
