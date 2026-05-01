"""AuthManager.validate_env() 단위 테스트."""
from __future__ import annotations


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
    # 9개 환경변수 모두 포함
    expected_keys = {
        "KIS_APP_KEY",
        "KIS_APP_SECRET",
        "KIS_ACCOUNT_NUMBER",
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
        "KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NUMBER", "KIS_MODE",
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
