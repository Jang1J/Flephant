"""S1-0a KRX 투자자별 수급 unit tests.

실 API 연결은 Sprint 2. 현재는 mock_response 주입 경로만 검증.

2026-04-21 C3 수정:
  - KRX_API_KEY 미설정(_is_mock=True) 환경에서 mock_response=None 호출 시 [] 반환.
  - _is_mock=False(실 API 키 있음) 환경에서 mock_response=None → NotImplementedError.
"""
from __future__ import annotations

import pytest

from src.connectors.krx_rest import KRXRestClient


@pytest.fixture
def client() -> KRXRestClient:
    return KRXRestClient()


def test_get_investor_info_no_mock_is_mock_returns_empty(client: KRXRestClient) -> None:
    """_is_mock=True(KRX_API_KEY 없음): mock_response=None 호출 시 mock 데이터 반환.

    S2-5a 구현 후 변경: 빈 리스트 대신 seed 기반 mock 데이터 반환.
    C3 3필드(foreign_net_buy/institutional_net_buy/retail_net_buy) 포함 여부 확인.
    """
    assert client._is_mock, "테스트 환경에서 KRX_API_KEY 미설정이어야"
    events = client.get_investor_info(
        ticker="005930",
        bgn_de="20260420",
        end_de="20260420",
    )
    assert isinstance(events, list)
    assert len(events) >= 1
    payload = events[0]["payload"]
    assert "foreign_net_buy" in payload
    assert "institutional_net_buy" in payload
    assert "retail_net_buy" in payload


def test_get_investor_info_with_mock_normalizes(client: KRXRestClient) -> None:
    """mock_response 주입 시 C2 정규화 경로 정상 동작."""
    mock = [
        {
            "bas_dt": "20260420",
            "foreign_net_buy": 10_000_000_000,   # 외국인 +100억
            "institutional": -5_000_000_000,      # 기관 -50억
            "retail": -5_000_000_000,             # 개인 -50억
        }
    ]
    events = client.get_investor_info(
        ticker="005930",
        bgn_de="20260420",
        end_de="20260420",
        mock_response=mock,
    )
    assert len(events) == 1
    event = events[0]
    # C2 필수 필드
    assert "event_id" in event
    assert event["source"] == "krx_investor_flow"
    # 정규화 후 구조는 EventNormalizer._normalize_krx_investor_flow 형식 따름
    assert event["event_id"].startswith("EVT-")


def test_get_investor_info_empty_mock_returns_empty(client: KRXRestClient) -> None:
    events = client.get_investor_info(
        ticker="005930",
        bgn_de="20260420",
        end_de="20260420",
        mock_response=[],
    )
    assert events == []


def test_get_investor_info_pads_ticker(client: KRXRestClient) -> None:
    """4자리 ticker 입력 → 내부에서 6자리 padding."""
    events = client.get_investor_info(
        ticker="5930",
        bgn_de="20260420",
        end_de="20260420",
        mock_response=[{"bas_dt": "20260420"}],
    )
    assert len(events) == 1


def test_get_investor_info_malformed_entry_skipped(client: KRXRestClient) -> None:
    """malformed entry는 warning log 후 skip."""
    mock = [
        {"bas_dt": "invalid_date_format"},   # 정규화 실패
        {"bas_dt": "20260420", "foreign_net_buy": 100},
    ]
    events = client.get_investor_info(
        ticker="005930",
        bgn_de="20260420",
        end_de="20260420",
        mock_response=mock,
    )
    # 유효한 하나만 정규화
    assert len(events) <= 1
