"""S2-11 BaseConnector unit tests."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from src.connectors.base import BaseConnector
from src.connectors.dart_rest import DARTRestClient
from src.connectors.krx_rest import KRXRestClient
from src.connectors.kis_rest import KISRestClient
from src.connectors.naver_rest import NaverNewsClient
from src.connectors.ecos_rest import ECOSRestClient
from src.connectors.us_market import USMarketClient
from src.connectors.community import CommunityCrawler


def test_base_connector_defaults_loaded() -> None:
    """BaseConnector 초기화 시 yaml에서 defaults 로드."""
    bc = BaseConnector()
    assert bc.timeout_sec > 0
    assert bc.max_retries > 0
    assert bc.backoff_base > 0


def test_all_connectors_inherit_base() -> None:
    """7개 커넥터 모두 BaseConnector 상속 확인."""
    from src.connectors.base import BaseConnector  # noqa: PLC0415
    assert issubclass(DARTRestClient, BaseConnector)
    assert issubclass(KRXRestClient, BaseConnector)
    assert issubclass(KISRestClient, BaseConnector)
    assert issubclass(NaverNewsClient, BaseConnector)
    assert issubclass(ECOSRestClient, BaseConnector)
    assert issubclass(USMarketClient, BaseConnector)
    assert issubclass(CommunityCrawler, BaseConnector)


def test_dart_client_inherits_timeout() -> None:
    """DARTRestClient가 BaseConnector에서 timeout_sec 상속."""
    from unittest.mock import MagicMock  # noqa: PLC0415
    mock_auth = MagicMock()
    mock_auth.get_dart_key.return_value = None
    client = DARTRestClient(auth=mock_auth)
    assert hasattr(client, "timeout_sec")
    assert client.timeout_sec > 0


def test_krx_client_inherits_timeout() -> None:
    """KRXRestClient가 BaseConnector에서 timeout_sec 상속."""
    from unittest.mock import MagicMock  # noqa: PLC0415
    mock_auth = MagicMock()
    mock_auth.get_krx_key.return_value = None
    client = KRXRestClient(auth=mock_auth)
    assert hasattr(client, "timeout_sec")
    assert client.timeout_sec > 0


def test_dart_no_duplicate_http_get_json() -> None:
    """DARTRestClient에 _http_get_json이 BaseConnector에서 상속됨 (자체 구현 없음)."""
    # 자체 클래스 dict에는 없고 MRO에서만 발견돼야 함
    assert "_http_get_json" not in DARTRestClient.__dict__
    assert "_http_get_json" not in KRXRestClient.__dict__
    # BaseConnector에는 있어야 함
    assert "_http_get_json" in BaseConnector.__dict__


def test_http_get_json_timeout_error() -> None:
    """BaseConnector._http_get_json TimeoutError 발생 확인."""
    bc = BaseConnector()
    bc.timeout_sec = 1  # 짧은 timeout 설정

    import requests  # noqa: PLC0415
    with patch("requests.get", side_effect=requests.exceptions.Timeout("test")):
        with pytest.raises(TimeoutError):
            bc._http_get_json("http://example.com", {})


def test_http_get_json_headers_passed() -> None:
    """headers 파라미터가 requests.get에 전달됨."""
    bc = BaseConnector()
    from unittest.mock import MagicMock  # noqa: PLC0415
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"ok": True}
    mock_resp.raise_for_status.return_value = None

    with patch("requests.get", return_value=mock_resp) as mock_get:
        bc._http_get_json("http://test.com", {"key": "val"}, headers={"X-Test": "1"})
        call_kwargs = mock_get.call_args.kwargs
        assert call_kwargs.get("headers") == {"X-Test": "1"}
