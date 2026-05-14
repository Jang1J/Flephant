"""AuthManager.validate_env() 단위 테스트."""
from __future__ import annotations


from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src.utils.auth import AuthManager


def test_validate_env_returns_dict(monkeypatch):
    """validate_env()는 환경변수 존재 여부 dict를 반환한다."""
    # 일부 환경변수 설정
    monkeypatch.setenv("KIS_APP_KEY", "test_key")
    monkeypatch.setenv("DART_API_KEY", "test_dart")

    manager = AuthManager()
    result = manager.validate_env()

    assert isinstance(result, dict)
    # KIS 모의투자 주문 계좌까지 포함한 10개 환경변수 모두 포함
    expected_keys = {
        "KIS_APP_KEY",
        "KIS_APP_SECRET",
        "KIS_ACCOUNT_NUMBER",
        "KIS_ACCOUNT_PRODUCT_CODE",
        "KIS_MODE",
        "DART_API_KEY",
        "KRX_API_KEY",
        "NAVER_CLIENT_ID",
        "NAVER_CLIENT_SECRET",
        "ECOS_API_KEY",
    }
    assert set(result.keys()) == expected_keys


def test_validate_env_values_are_bool(monkeypatch):
    """반환 dict 값은 모두 bool이어야 한다. 실제 API 키 값 노출 금지."""
    monkeypatch.setenv("KIS_APP_KEY", "some_actual_key_value")
    monkeypatch.delenv("KIS_APP_SECRET", raising=False)

    manager = AuthManager()
    result = manager.validate_env()

    for key, val in result.items():
        assert isinstance(val, bool), f"{key} 값이 bool이 아님: {val!r}"
    # 실제 키 값이 노출되지 않는지 확인 (값은 bool)
    assert result["KIS_APP_KEY"] is True
    assert result["KIS_APP_SECRET"] is False


def test_validate_env_missing_all(monkeypatch):
    """모든 환경변수가 없으면 전부 False여야 한다."""
    env_keys = [
        "KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NUMBER",
        "KIS_ACCOUNT_PRODUCT_CODE", "KIS_MODE",
        "DART_API_KEY", "KRX_API_KEY", "NAVER_CLIENT_ID",
        "NAVER_CLIENT_SECRET", "ECOS_API_KEY",
    ]
    for k in env_keys:
        monkeypatch.delenv(k, raising=False)

    manager = AuthManager()
    result = manager.validate_env()

    assert all(v is False for v in result.values())


def test_get_kis_base_url_virtual(monkeypatch):
    """KIS_MODE=virtual이면 모의투자 URL 반환."""
    monkeypatch.setenv("KIS_MODE", "virtual")
    manager = AuthManager()
    url = manager.get_kis_base_url()
    assert "openapivts" in url


def test_get_kis_base_url_real(monkeypatch):
    """KIS_MODE=real이면 실계좌 URL 반환."""
    monkeypatch.setenv("KIS_MODE", "real")
    manager = AuthManager()
    url = manager.get_kis_base_url()
    assert "openapivts" not in url
    assert "openapi.koreainvestment.com:9443" in url


def test_get_kis_token_reuses_shared_cache_across_instances(monkeypatch):
    """KIS OAuth 재발급 제한 회피: 같은 프로세스의 AuthManager는 토큰을 공유한다."""
    monkeypatch.setenv("KIS_MODE", "virtual")
    monkeypatch.setenv("KIS_APP_KEY", "shared_key")
    monkeypatch.setenv("KIS_APP_SECRET", "shared_secret")
    monkeypatch.setattr(AuthManager, "_shared_kis_token", None)
    monkeypatch.setattr(AuthManager, "_shared_kis_token_expires_at", None)
    monkeypatch.setattr(AuthManager, "_shared_kis_token_scope", None)

    expires_at = datetime.now(tz=timezone.utc) + timedelta(hours=1)
    first = AuthManager()
    monkeypatch.setattr(first, "_fetch_kis_token", lambda: ("token-a", expires_at))
    assert first.get_kis_token() == "token-a"

    second = AuthManager()
    fetch = MagicMock(side_effect=AssertionError("shared token should be reused"))
    monkeypatch.setattr(second, "_fetch_kis_token", fetch)

    assert second.get_kis_token() == "token-a"
    fetch.assert_not_called()


def test_kis_token_auth_error_includes_sanitized_kis_message(monkeypatch):
    """tokenP 401/403은 KIS msg_cd/msg1를 노출하되 secret은 노출하지 않는다."""
    monkeypatch.setenv("KIS_MODE", "virtual")
    monkeypatch.setenv("KIS_APP_KEY", "app-key")
    monkeypatch.setenv("KIS_APP_SECRET", "super-secret")

    manager = AuthManager()
    monkeypatch.setattr(
        manager,
        "_http_post",
        lambda *_args, **_kwargs: {
            "_status_code": 403,
            "msg_cd": "EGW00123",
            "msg1": "invalid appkey or appsecret",
        },
    )

    with pytest.raises(Exception) as exc:
        manager.get_kis_token()

    message = str(exc.value)
    assert "HTTP 403" in message
    assert "EGW00123" in message
    assert "invalid appkey or appsecret" in message
    assert "super-secret" not in message


def test_kis_credentials_prefer_paper_env_in_virtual_mode(monkeypatch):
    """모의투자 모드에서는 KIS_PAPER_*를 generic KIS_*보다 우선한다."""
    monkeypatch.setenv("KIS_MODE", "virtual")
    monkeypatch.setenv("KIS_APP_KEY", "generic_key")
    monkeypatch.setenv("KIS_APP_SECRET", "generic_secret")
    monkeypatch.setenv("KIS_PAPER_APP_KEY", "paper_key")
    monkeypatch.setenv("KIS_PAPER_APP_SECRET", "paper_secret")
    monkeypatch.setenv("KIS_ACCOUNT_NUMBER", "11111111")
    monkeypatch.setenv("KIS_ACCOUNT_PRODUCT_CODE", "01")
    monkeypatch.setenv("KIS_PAPER_ACCOUNT_NUMBER", "22222222")
    monkeypatch.setenv("KIS_PAPER_ACCOUNT_PRODUCT_CODE", "03")

    manager = AuthManager()

    assert manager.get_kis_app_credentials() == ("paper_key", "paper_secret")
    assert manager.get_kis_account_parts() == ("22222222", "03")


def test_kis_credentials_prefer_real_env_in_real_mode(monkeypatch):
    """실전 모드에서는 KIS_REAL_*를 generic KIS_*보다 우선한다."""
    monkeypatch.setenv("KIS_MODE", "real")
    monkeypatch.setenv("KIS_APP_KEY", "generic_key")
    monkeypatch.setenv("KIS_APP_SECRET", "generic_secret")
    monkeypatch.setenv("KIS_REAL_APP_KEY", "real_key")
    monkeypatch.setenv("KIS_REAL_APP_SECRET", "real_secret")

    manager = AuthManager()

    assert manager.get_kis_app_credentials() == ("real_key", "real_secret")


def test_get_dart_key_missing(monkeypatch):
    """DART_API_KEY 누락 시 EnvironmentError 발생."""
    monkeypatch.delenv("DART_API_KEY", raising=False)
    manager = AuthManager()
    with pytest.raises(EnvironmentError, match="DART_API_KEY"):
        manager.get_dart_key()


def test_get_naver_client_returns_tuple(monkeypatch):
    """get_naver_client()는 (client_id, client_secret) 튜플을 반환한다."""
    monkeypatch.setenv("NAVER_CLIENT_ID", "nid_123")
    monkeypatch.setenv("NAVER_CLIENT_SECRET", "nsecret_456")
    manager = AuthManager()
    result = manager.get_naver_client()
    assert isinstance(result, tuple)
    assert len(result) == 2
    assert result[0] == "nid_123"
    assert result[1] == "nsecret_456"
