"""DARTRestClient unit tests. S0-3 실구현 검증.

coverage:
  - list_disclosures happy path (C2 events)
  - status=013 빈 리스트
  - status=020 DARTAPIError
  - get_company happy path
  - get_company 오류
  - normalize_integration: EventNormalizer 연동 + C2 필수 필드 확인
  - params_exclude_none: None 값 제외
  - crtfc_key 로그 마스킹
  - _yyyymmdd_to_iso_kst 변환
  - retry on ConnectionError (2회 실패 후 3번째 성공)
  - retry exhausted (3회 전부 실패)
  - ticker padding (stock_code 4자리 → 6자리)

환경변수 없이 통과하도록 AuthManager.get_dart_key + RateLimiter는 모두 mock.
occurred_at은 과거 시간 사용 (PIT-Safety 통과).
"""
from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.connectors.dart_rest import DARTAPIError, DARTRestClient


# ------------------------------------------------------------------ #
# 공통 픽스처 + 헬퍼
# ------------------------------------------------------------------ #

# PIT-Safety 통과용 과거 날짜
_PAST_RCEPT_DT = "20260419"
_PAST_ISO_KST = "2026-04-19T00:00:00+09:00"

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


def _make_dart_item(
    corp_code: str = "00126380",
    corp_name: str = "삼성전자",
    stock_code: str = "005930",
    report_nm: str = "분기보고서",
    rcept_no: str = "20260419000001",
    rcept_dt: str = _PAST_RCEPT_DT,
    rm: str = "",
) -> dict[str, Any]:
    return {
        "corp_code": corp_code,
        "corp_name": corp_name,
        "stock_code": stock_code,
        "corp_cls": "Y",
        "report_nm": report_nm,
        "rcept_no": rcept_no,
        "flr_nm": corp_name,
        "rcept_dt": rcept_dt,
        "rm": rm,
    }


def _make_list_response(items: list[dict]) -> dict[str, Any]:
    return {
        "status": "000",
        "message": "정상",
        "total_count": str(len(items)),
        "page_no": "1",
        "page_count": str(len(items)),
        "list": items,
    }


def _make_client() -> DARTRestClient:
    """auth + rate_limiter mock 주입 클라이언트."""
    mock_auth = MagicMock()
    mock_auth.get_dart_key.return_value = "MOCK_KEY_1234567890"
    mock_rate = MagicMock()
    mock_rate.wait_and_acquire.return_value = None
    return DARTRestClient(auth=mock_auth, rate_limiter=mock_rate)


# ------------------------------------------------------------------ #
# 1. list_disclosures happy path
# ------------------------------------------------------------------ #

def test_list_disclosures_happy_path() -> None:
    """정상 응답 → C2 events 리스트 반환."""
    client = _make_client()
    item = _make_dart_item()
    response = _make_list_response([item])

    with patch.object(client, "_http_get_json", return_value=response):
        result = client.list_disclosures()

    assert isinstance(result, list)
    assert len(result) == 1
    event = result[0]
    assert event["source"] == "dart"
    assert event["event_type"] == "dart"


# ------------------------------------------------------------------ #
# 2. list_disclosures empty (status=013)
# ------------------------------------------------------------------ #

def test_list_disclosures_empty() -> None:
    """status=013 → 빈 리스트 반환 (예외 없음)."""
    client = _make_client()
    response = {"status": "013", "message": "조회된 데이터가 없습니다."}

    with patch.object(client, "_http_get_json", return_value=response):
        result = client.list_disclosures()

    assert result == []


# ------------------------------------------------------------------ #
# 3. list_disclosures API error (status=020)
# ------------------------------------------------------------------ #

def test_list_disclosures_api_error() -> None:
    """status=020 (사용한도 초과) → DARTAPIError 발생."""
    client = _make_client()
    response = {"status": "020", "message": "요청 제한을 초과하였습니다."}

    with patch.object(client, "_http_get_json", return_value=response):
        with pytest.raises(DARTAPIError) as exc_info:
            client.list_disclosures()

    assert "020" in str(exc_info.value)


# ------------------------------------------------------------------ #
# 4. get_company happy path
# ------------------------------------------------------------------ #

def test_get_company_happy_path() -> None:
    """기업 개황 정상 응답 → dict 반환."""
    client = _make_client()
    response = {
        "status": "000",
        "message": "정상",
        "corp_name": "삼성전자",
        "corp_code": "00126380",
        "stock_code": "005930",
        "corp_cls": "Y",
    }

    with patch.object(client, "_http_get_json", return_value=response):
        result = client.get_company("00126380")

    assert result["corp_name"] == "삼성전자"
    assert result["status"] == "000"


# ------------------------------------------------------------------ #
# 5. get_company error
# ------------------------------------------------------------------ #

def test_get_company_error() -> None:
    """get_company status != '000' → DARTAPIError."""
    client = _make_client()
    response = {"status": "100", "message": "필수 값을 확인해 주세요."}

    with patch.object(client, "_http_get_json", return_value=response):
        with pytest.raises(DARTAPIError) as exc_info:
            client.get_company("INVALID")

    assert "100" in str(exc_info.value)


# ------------------------------------------------------------------ #
# 6. normalize integration: C2 필수 필드 확인
# ------------------------------------------------------------------ #

def test_normalize_integration() -> None:
    """실제 EventNormalizer 연동. C2 필수 필드 전부 존재 확인."""
    client = _make_client()
    item = _make_dart_item(
        corp_name="LG에너지솔루션",
        stock_code="373220",
        report_nm="주요사항보고서",
        rcept_dt=_PAST_RCEPT_DT,
    )
    response = _make_list_response([item])

    with patch.object(client, "_http_get_json", return_value=response):
        result = client.list_disclosures()

    assert len(result) == 1
    event = result[0]
    for field in C2_REQUIRED_FIELDS:
        assert field in event, f"C2 필수 필드 누락: {field}"

    # source, event_type 확인
    assert event["source"] == "dart"
    assert event["event_type"] == "dart"
    # scope: ticker 있으면 "ticker:XXXXXX"
    assert event["scope"].startswith("ticker:")


# ------------------------------------------------------------------ #
# 7. params exclude None
# ------------------------------------------------------------------ #

def test_params_exclude_none() -> None:
    """corp_code=None → params에서 corp_code 키 제외."""
    client = _make_client()
    captured_params: dict[str, str] = {}

    def fake_http(url: str, params: dict[str, str]) -> dict[str, Any]:
        captured_params.update(params)
        return _make_list_response([])

    with patch.object(client, "_http_get_json", side_effect=fake_http):
        client.list_disclosures(corp_code=None, bgn_de=None, end_de=None)

    assert "corp_code" not in captured_params
    assert "bgn_de" not in captured_params
    # page_no, page_count는 기본값 포함
    assert "page_no" in captured_params
    assert "page_count" in captured_params
    # crtfc_key는 _call_api가 주입
    assert "crtfc_key" in captured_params


# ------------------------------------------------------------------ #
# 8. crtfc_key 로그 마스킹
# ------------------------------------------------------------------ #

def test_crtfc_key_masked_in_log(caplog: pytest.LogCaptureFixture) -> None:
    """로그 메시지에 실제 API 키 값이 노출되지 않음."""
    client = _make_client()
    real_key = "MOCK_KEY_1234567890"
    client.auth.get_dart_key.return_value = real_key

    response = _make_list_response([])

    with caplog.at_level(logging.INFO, logger="dart_rest"):
        with patch.object(client, "_http_get_json", return_value=response):
            client.list_disclosures()

    # 로그 전체에서 실제 키 값이 노출되지 않아야 함
    for record in caplog.records:
        assert real_key not in record.getMessage(), (
            f"실제 API 키가 로그에 노출됨: {record.getMessage()}"
        )


# ------------------------------------------------------------------ #
# 9. _yyyymmdd_to_iso_kst
# ------------------------------------------------------------------ #

def test_yyyymmdd_to_iso_kst() -> None:
    """'20260419' → '2026-04-19T00:00:00+09:00'."""
    result = DARTRestClient._yyyymmdd_to_iso_kst("20260419")
    assert result == "2026-04-19T00:00:00+09:00"


def test_yyyymmdd_to_iso_kst_different_date() -> None:
    """다른 날짜도 올바르게 변환."""
    result = DARTRestClient._yyyymmdd_to_iso_kst("20260101")
    assert result == "2026-01-01T00:00:00+09:00"


# ------------------------------------------------------------------ #
# 10. retry on ConnectionError (2회 실패 → 3번째 성공)
# ------------------------------------------------------------------ #

def test_retry_on_connection_error() -> None:
    """_http_get_json이 2번 ConnectionError, 3번째 성공 → 최종 성공 반환."""
    client = _make_client()
    item = _make_dart_item()
    success_response = _make_list_response([item])

    call_count = 0

    def fake_http(url: str, params: dict[str, str]) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ConnectionError(f"mock 연결 실패 {call_count}회")
        return success_response

    with patch("time.sleep"):  # sleep 생략으로 테스트 속도 유지
        with patch.object(client, "_http_get_json", side_effect=fake_http):
            result = client.list_disclosures()

    assert call_count == 3
    assert len(result) == 1


# ------------------------------------------------------------------ #
# 11. retry exhausted (3회 전부 실패 → ConnectionError)
# ------------------------------------------------------------------ #

def test_retry_exhausted() -> None:
    """_http_get_json 3번 전부 ConnectionError → ConnectionError 발생."""
    client = _make_client()

    def always_fail(url: str, params: dict[str, str]) -> dict[str, Any]:
        raise ConnectionError("항상 실패")

    with patch("time.sleep"):
        with patch.object(client, "_http_get_json", side_effect=always_fail):
            with pytest.raises(ConnectionError) as exc_info:
                client.list_disclosures()

    assert "재시도 실패" in str(exc_info.value)


# ------------------------------------------------------------------ #
# 12. ticker padding
# ------------------------------------------------------------------ #

def test_ticker_padding() -> None:
    """stock_code='5930' (4자리) → ticker='005930' (6자리 zero-padded)."""
    client = _make_client()
    item = _make_dart_item(stock_code="5930")
    response = _make_list_response([item])

    with patch.object(client, "_http_get_json", return_value=response):
        result = client.list_disclosures()

    assert len(result) == 1
    event = result[0]
    assert event["payload"]["ticker"] == "005930"
    assert event["scope"] == "ticker:005930"


# ------------------------------------------------------------------ #
# 보조 테스트: 빈 stock_code → scope="market"
# ------------------------------------------------------------------ #

def test_empty_stock_code_scope_market() -> None:
    """stock_code 빈 문자열 → scope='market' (비상장 회사 처리)."""
    client = _make_client()
    item = _make_dart_item(stock_code="")
    response = _make_list_response([item])

    with patch.object(client, "_http_get_json", return_value=response):
        result = client.list_disclosures()

    assert len(result) == 1
    event = result[0]
    assert event["scope"] == "market"
