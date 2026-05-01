"""KRXRestClient unit tests. S0-4 실구현 검증.

coverage:
  - get_stock_price_info happy path (C2 events)
  - get_stock_price_info empty (totalCount=0)
  - get_stock_price_info API error (resultCode="99")
  - normalize integration: EventNormalizer 연동 + C2 필수 필드
  - params exclude None: ticker=None 시 params 제외
  - serviceKey 로그 마스킹
  - _yyyymmdd_to_iso_kst 변환
  - retry on ConnectionError (2회 실패 후 3번째 성공)
  - retry exhausted (3회 전부 실패)
  - ticker padding (srtnCd 4자리 → 6자리)
  - item missing basDt → ValidationError skip (전체 실패 아님)
  - investor flow placeholder None 확인

환경변수 없이 통과하도록 AuthManager.get_krx_key + RateLimiter는 모두 mock.
occurred_at은 과거 시간 사용 (PIT-Safety 통과).
"""
from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.connectors.krx_rest import KRXAPIError, KRXRestClient


# ------------------------------------------------------------------ #
# 공통 픽스처 + 헬퍼
# ------------------------------------------------------------------ #

# PIT-Safety 통과용 과거 날짜
_PAST_BAS_DT = "20260419"
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


def _make_krx_item(
    bas_dt: str = _PAST_BAS_DT,
    srtn_cd: str = "005930",
    isin_cd: str = "KR7005930003",
    itms_nm: str = "삼성전자",
    mrkt_ctg: str = "KOSPI",
    clpr: str = "75000",
    vs: str = "500",
    flt_rt: str = "0.67",
    mkp: str = "74500",
    hipr: str = "75500",
    lopr: str = "74000",
    trqu: str = "15000000",
    tr_prc: str = "1125000000000",
) -> dict[str, Any]:
    return {
        "basDt": bas_dt,
        "srtnCd": srtn_cd,
        "isinCd": isin_cd,
        "itmsNm": itms_nm,
        "mrktCtg": mrkt_ctg,
        "clpr": clpr,
        "vs": vs,
        "fltRt": flt_rt,
        "mkp": mkp,
        "hipr": hipr,
        "lopr": lopr,
        "trqu": trqu,
        "trPrc": tr_prc,
    }


def _make_price_response(items: list[dict]) -> dict[str, Any]:
    return {
        "response": {
            "header": {
                "resultCode": "00",
                "resultMsg": "NORMAL SERVICE",
            },
            "body": {
                "items": {"item": items},
                "numOfRows": len(items),
                "pageNo": 1,
                "totalCount": len(items),
            },
        }
    }


def _make_error_response(result_code: str = "99", result_msg: str = "서비스 오류") -> dict[str, Any]:
    return {
        "response": {
            "header": {
                "resultCode": result_code,
                "resultMsg": result_msg,
            },
            "body": {
                "items": {},
                "numOfRows": 0,
                "pageNo": 1,
                "totalCount": 0,
            },
        }
    }


def _make_client() -> KRXRestClient:
    """auth + rate_limiter mock 주입 클라이언트."""
    mock_auth = MagicMock()
    mock_auth.get_krx_key.return_value = "MOCK_KRX_KEY_1234567890"
    mock_rate = MagicMock()
    mock_rate.wait_and_acquire.return_value = None
    return KRXRestClient(auth=mock_auth, rate_limiter=mock_rate)


# ------------------------------------------------------------------ #
# 1. get_stock_price_info happy path
# ------------------------------------------------------------------ #

def test_get_stock_price_info_happy_path() -> None:
    """정상 응답 → C2 events 리스트 반환."""
    client = _make_client()
    item = _make_krx_item()
    response = _make_price_response([item])

    with patch.object(client, "_http_get_json", return_value=response):
        result = client.get_stock_price_info()

    assert isinstance(result, list)
    assert len(result) == 1
    event = result[0]
    assert event["source"] == "krx_investor_flow"
    assert event["event_type"] == "investor_flow"


# ------------------------------------------------------------------ #
# 2. get_stock_price_info empty (totalCount=0)
# ------------------------------------------------------------------ #

def test_get_stock_price_info_empty() -> None:
    """totalCount=0 → 빈 리스트 반환 (예외 없음)."""
    client = _make_client()
    response = {
        "response": {
            "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE"},
            "body": {
                "items": {},
                "numOfRows": 0,
                "pageNo": 1,
                "totalCount": 0,
            },
        }
    }

    with patch.object(client, "_http_get_json", return_value=response):
        result = client.get_stock_price_info()

    assert result == []


# ------------------------------------------------------------------ #
# 3. get_stock_price_info API error (resultCode="99")
# ------------------------------------------------------------------ #

def test_get_stock_price_info_api_error() -> None:
    """resultCode='99' → KRXAPIError 발생."""
    client = _make_client()
    response = _make_error_response(result_code="99", result_msg="서비스 오류")

    with patch.object(client, "_http_get_json", return_value=response):
        with pytest.raises(KRXAPIError) as exc_info:
            client.get_stock_price_info()

    assert "99" in str(exc_info.value)


# ------------------------------------------------------------------ #
# 4. normalize integration: C2 필수 필드 확인
# ------------------------------------------------------------------ #

def test_normalize_integration() -> None:
    """실제 EventNormalizer 연동. C2 필수 필드 전부 존재 확인."""
    client = _make_client()
    item = _make_krx_item(srtn_cd="005930", itms_nm="삼성전자")
    response = _make_price_response([item])

    with patch.object(client, "_http_get_json", return_value=response):
        result = client.get_stock_price_info()

    assert len(result) == 1
    event = result[0]
    for field in C2_REQUIRED_FIELDS:
        assert field in event, f"C2 필수 필드 누락: {field}"

    assert event["source"] == "krx_investor_flow"
    assert event["event_type"] == "investor_flow"
    assert event["scope"] == "ticker:005930"


# ------------------------------------------------------------------ #
# 5. params exclude None: ticker=None 시 params 제외
# ------------------------------------------------------------------ #

def test_params_exclude_none() -> None:
    """ticker=None, bgn_de=None → params에서 해당 키 제외."""
    client = _make_client()
    captured_params: dict[str, str] = {}

    def fake_http(url: str, params: dict[str, str]) -> dict[str, Any]:
        captured_params.update(params)
        return _make_price_response([])

    with patch.object(client, "_http_get_json", side_effect=fake_http):
        client.get_stock_price_info(bgn_de=None, end_de=None, ticker=None)

    assert "likeSrtnCd" not in captured_params
    assert "beginBasDt" not in captured_params
    assert "endBasDt" not in captured_params
    # pageNo, numOfRows, resultType는 기본값으로 포함
    assert "pageNo" in captured_params
    assert "numOfRows" in captured_params
    assert "resultType" in captured_params
    # serviceKey는 _call_api가 주입
    assert "serviceKey" in captured_params


# ------------------------------------------------------------------ #
# 6. serviceKey 로그 마스킹
# ------------------------------------------------------------------ #

def test_service_key_masked_in_log(caplog: pytest.LogCaptureFixture) -> None:
    """로그 메시지에 실제 API 키 값이 노출되지 않음."""
    client = _make_client()
    real_key = "MOCK_KRX_KEY_1234567890"
    client.auth.get_krx_key.return_value = real_key

    response = _make_price_response([])

    with caplog.at_level(logging.INFO, logger="krx_rest"):
        with patch.object(client, "_http_get_json", return_value=response):
            client.get_stock_price_info()

    for record in caplog.records:
        assert real_key not in record.getMessage(), (
            f"실제 API 키가 로그에 노출됨: {record.getMessage()}"
        )


# ------------------------------------------------------------------ #
# 7. _yyyymmdd_to_iso_kst 변환
# ------------------------------------------------------------------ #

def test_yyyymmdd_to_iso_kst() -> None:
    """'20260419' → '2026-04-19T00:00:00+09:00'."""
    result = KRXRestClient._yyyymmdd_to_iso_kst("20260419")
    assert result == "2026-04-19T00:00:00+09:00"


def test_yyyymmdd_to_iso_kst_different_date() -> None:
    """다른 날짜도 올바르게 변환."""
    result = KRXRestClient._yyyymmdd_to_iso_kst("20260101")
    assert result == "2026-01-01T00:00:00+09:00"


# ------------------------------------------------------------------ #
# 8. retry on ConnectionError (2회 실패 → 3번째 성공)
# ------------------------------------------------------------------ #

def test_retry_on_connection_error() -> None:
    """_http_get_json이 2번 ConnectionError, 3번째 성공 → 최종 성공 반환."""
    client = _make_client()
    item = _make_krx_item()
    success_response = _make_price_response([item])

    call_count = 0

    def fake_http(url: str, params: dict[str, str]) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ConnectionError(f"mock 연결 실패 {call_count}회")
        return success_response

    with patch("time.sleep"):
        with patch.object(client, "_http_get_json", side_effect=fake_http):
            result = client.get_stock_price_info()

    assert call_count == 3
    assert len(result) == 1


# ------------------------------------------------------------------ #
# 9. retry exhausted (3회 전부 실패 → ConnectionError)
# ------------------------------------------------------------------ #

def test_retry_exhausted() -> None:
    """_http_get_json 3번 전부 ConnectionError → ConnectionError 발생."""
    client = _make_client()

    def always_fail(url: str, params: dict[str, str]) -> dict[str, Any]:
        raise ConnectionError("항상 실패")

    with patch("time.sleep"):
        with patch.object(client, "_http_get_json", side_effect=always_fail):
            with pytest.raises(ConnectionError) as exc_info:
                client.get_stock_price_info()

    assert "재시도 실패" in str(exc_info.value)


# ------------------------------------------------------------------ #
# 10. ticker padding (srtnCd 4자리 → 6자리)
# ------------------------------------------------------------------ #

def test_ticker_padding() -> None:
    """srtnCd='5930' (4자리) → ticker='005930' (6자리 zero-padded)."""
    client = _make_client()
    item = _make_krx_item(srtn_cd="5930")
    response = _make_price_response([item])

    with patch.object(client, "_http_get_json", return_value=response):
        result = client.get_stock_price_info()

    assert len(result) == 1
    event = result[0]
    assert event["payload"]["ticker"] == "005930"
    assert event["scope"] == "ticker:005930"


# ------------------------------------------------------------------ #
# 11. item missing basDt → skip (전체 실패 아님)
# ------------------------------------------------------------------ #

def test_item_to_raw_missing_fields() -> None:
    """item에 basDt 없음 → 해당 항목 skip, 나머지 정상 처리."""
    client = _make_client()

    # 첫 번째 item: basDt 누락
    bad_item = {"srtnCd": "005930", "clpr": "75000"}
    # 두 번째 item: 정상
    good_item = _make_krx_item(srtn_cd="000660", itms_nm="SK하이닉스")

    # bad_item, good_item 순으로 응답 구성 (totalCount=2이지만 정규화는 1건)
    response = {
        "response": {
            "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE"},
            "body": {
                "items": {"item": [bad_item, good_item]},
                "numOfRows": 2,
                "pageNo": 1,
                "totalCount": 2,
            },
        }
    }

    with patch.object(client, "_http_get_json", return_value=response):
        result = client.get_stock_price_info()

    # bad_item은 skip, good_item만 정규화 성공
    assert len(result) == 1
    assert result[0]["payload"]["ticker"] == "000660"


# ------------------------------------------------------------------ #
# 12. investor flow placeholder None 확인
# ------------------------------------------------------------------ #

def test_investor_flow_placeholder_none() -> None:
    """일별 시세 응답에서 investor_flow 3 필드 = None.
    EventNormalizer payload에 None으로 보존됨.
    SSOT 필드명 (2026-04-20 Phase 1 정렬): institutional_net_buy / retail_net_buy.
    """
    client = _make_client()
    item = _make_krx_item()
    response = _make_price_response([item])

    with patch.object(client, "_http_get_json", return_value=response):
        result = client.get_stock_price_info()

    assert len(result) == 1
    payload = result[0]["payload"]
    assert payload["foreign_net_buy"] is None
    assert payload["institutional_net_buy"] is None
    assert payload["retail_net_buy"] is None


# ------------------------------------------------------------------ #
# S2-5a: 투자자별 수급 실 API 테스트
# ------------------------------------------------------------------ #

def test_get_investor_info_mock_no_response() -> None:
    """mock 모드 (is_mock=True): mock_response=None → mock 데이터 반환, C3 3필드 포함."""
    mock_auth = MagicMock()
    mock_auth.get_krx_key.return_value = None  # 키 없음 → is_mock=True
    mock_rate = MagicMock()
    mock_rate.wait_and_acquire.return_value = None
    client = KRXRestClient(auth=mock_auth, rate_limiter=mock_rate)

    result = client.get_investor_info("005930", "20260101", "20260101")

    assert isinstance(result, list)
    assert len(result) >= 1
    payload = result[0]["payload"]
    assert "foreign_net_buy" in payload
    assert "institutional_net_buy" in payload
    assert "retail_net_buy" in payload


def test_get_investor_info_mock_response_injection() -> None:
    """mock_response 주입 → 정규화 후 C3 필드 반환."""
    client = _make_client()
    client._is_mock = True  # mock 강제

    mock_items = [
        {
            "bas_dt": "20260101",
            "foreign_net_buy": 50000.0,
            "institutional_net_buy": -20000.0,
            "retail_net_buy": -30000.0,
        }
    ]

    result = client.get_investor_info("005930", "20260101", "20260101", mock_response=mock_items)

    assert len(result) == 1
    payload = result[0]["payload"]
    assert payload["foreign_net_buy"] == 50000.0
    assert payload["institutional_net_buy"] == -20000.0
    assert payload["retail_net_buy"] == -30000.0


def test_get_investor_info_pit_safety_future_date() -> None:
    """미래 날짜 bgn_de 입력 → PITViolationError 발생."""
    from src.utils.pit_guard import PITViolationError  # noqa: PLC0415

    mock_auth = MagicMock()
    mock_auth.get_krx_key.return_value = "REAL_KEY"  # is_mock=False
    mock_rate = MagicMock()
    mock_rate.wait_and_acquire.return_value = None
    client = KRXRestClient(auth=mock_auth, rate_limiter=mock_rate)

    with pytest.raises(PITViolationError):
        client.get_investor_info("005930", "20991231", "20991231")
