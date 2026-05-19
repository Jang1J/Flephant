"""KIS OAuth + DART/KRX/Naver/ECOS API 키 관리. 환경변수 경유, .env 미접근."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger

logger = get_logger("auth")

# KIS OAuth 엔드포인트
_KIS_BASE_VIRTUAL = "https://openapivts.koreainvestment.com:29443"
_KIS_BASE_REAL = "https://openapi.koreainvestment.com:9443"
_KIS_TOKEN_PATH = "/oauth2/tokenP"


class AuthenticationError(Exception):
    """KIS OAuth 401/403 등 인증 실패."""


def _mask_token(token: str) -> str:
    """토큰 앞 8자리만 노출. 로그 안전."""
    if len(token) <= 8:
        return "****"
    return token[:8] + "..."


class AuthManager:
    """중앙 인증 관리. 환경변수 기반 5종 API 키 + KIS OAuth 자동 refresh.

    불변 원칙: .env 파일 직접 읽기 금지, 토큰은 메모리만 보관, 로그에 token 값 노출 금지.
    """

    _kis_token: str | None
    _kis_token_expires_at: datetime | None
    _shared_kis_token: str | None = None
    _shared_kis_token_expires_at: datetime | None = None
    _shared_kis_token_scope: tuple[str, str] | None = None

    def __init__(self) -> None:
        self._kis_token = None
        self._kis_token_expires_at = None
        # 재시도/refresh 정책은 risk_config.yaml auth_defaults에서 로드 (불변 원칙 5)
        _auth_cfg = config_load("risk_config.yaml", "auth_defaults")
        self.retry_delays: list[int] = list(_auth_cfg["retry_delays"])
        self.refresh_margin_sec: int = int(_auth_cfg["refresh_margin_sec"])
        self.token_rate_limit_retry_sec: int = int(
            _auth_cfg.get("token_rate_limit_retry_sec", 65)
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def needs_refresh(self) -> bool:
        """토큰 갱신 필요 여부 public API (S1-7 SafetyGuards 등이 호출)."""
        return self._need_refresh() and not self._load_shared_kis_token()

    def get_kis_token(self) -> str:
        """유효 KIS access_token. 만료 5분 전이면 auto refresh."""
        if self._need_refresh():
            if self._load_shared_kis_token():
                if self._kis_token is None:
                    raise RuntimeError("[auth] 공유 KIS 토큰 로드 후에도 None. 내부 버그.")
                return self._kis_token

            token, expires_at = self._fetch_kis_token()
            self._kis_token = token
            self._kis_token_expires_at = expires_at
            self._store_shared_kis_token(token, expires_at)
            logger.info(
                "KIS 토큰 발급 완료. 만료: %s, 마스킹: %s",
                expires_at.isoformat(),
                _mask_token(token),
            )

        if self._kis_token is None:
            raise RuntimeError("[auth] KIS 토큰 발급 후에도 None. 내부 버그.")
        return self._kis_token

    def get_dart_key(self) -> str:
        """DART API 키. 환경변수 누락 시 EnvironmentError."""
        return self._require_env("DART_API_KEY")

    def get_krx_key(self) -> str:
        """KRX API 키. 환경변수 누락 시 EnvironmentError."""
        return self._require_env("KRX_API_KEY")

    def get_naver_client(self) -> tuple[str, str]:
        """(client_id, client_secret) 튜플."""
        return (
            self._require_env("NAVER_CLIENT_ID"),
            self._require_env("NAVER_CLIENT_SECRET"),
        )

    def get_ecos_key(self) -> str:
        """ECOS API 키. 환경변수 누락 시 EnvironmentError."""
        return self._require_env("ECOS_API_KEY")

    def get_mode(self) -> str:
        """KIS_MODE 환경변수 일원 read. 기본 'virtual' (mock/virtual/real)."""
        return os.getenv("KIS_MODE", "virtual").strip().lower()

    def get_kis_base_url(self) -> str:
        """KIS_MODE 환경변수에 따라 모의(virtual) / 실계좌(real) base URL 반환."""
        mode = self.get_mode()
        if mode == "real":
            return _KIS_BASE_REAL
        return _KIS_BASE_VIRTUAL

    def get_kis_app_credentials(self) -> tuple[str, str]:
        """KIS REST 요청 헤더용 app key/secret. 값은 호출자 로그에 남기지 않는다."""
        return (
            self._require_first_env(self._mode_specific_kis_keys("APP_KEY")),
            self._require_first_env(self._mode_specific_kis_keys("APP_SECRET")),
        )

    def get_kis_account_parts(self) -> tuple[str, str]:
        """KIS 계좌번호/상품코드. mode-specific env를 generic env보다 우선한다."""
        return (
            self._require_first_env(self._mode_specific_kis_keys("ACCOUNT_NUMBER")),
            self._require_first_env(self._mode_specific_kis_keys("ACCOUNT_PRODUCT_CODE")),
        )

    def validate_env(self) -> dict[str, bool]:
        """각 환경변수 존재 여부 dict. 값은 출력 안 함.

        Returns:
            {"KIS_APP_KEY": True, "DART_API_KEY": False, ...}
        """
        keys = [
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
        ]
        result = {k: bool(os.getenv(k)) for k in keys}
        missing = [k for k, v in result.items() if not v]
        if missing:
            logger.warning("환경변수 미설정: %s", missing)
        else:
            logger.info("환경변수 전체 존재 확인 완료 (%d개)", len(keys))
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _need_refresh(self) -> bool:
        """토큰이 없거나 만료 refresh_margin_sec 이내이면 True."""
        if self._kis_token is None or self._kis_token_expires_at is None:
            return True
        now = datetime.now(tz=timezone.utc)
        return (self._kis_token_expires_at - now).total_seconds() < self.refresh_margin_sec

    def _kis_token_scope(self) -> tuple[str, str]:
        """프로세스 내 KIS 토큰 공유 범위. 값은 로그에 노출하지 않는다."""
        try:
            app_key, _ = self.get_kis_app_credentials()
        except EnvironmentError:
            app_key = ""
        return (self.get_mode(), app_key)

    def _load_shared_kis_token(self) -> bool:
        """동일 프로세스의 다른 AuthManager가 발급한 KIS 토큰 재사용."""
        cls = type(self)
        token = cls._shared_kis_token
        expires_at = cls._shared_kis_token_expires_at
        if token is None or expires_at is None:
            return False
        if cls._shared_kis_token_scope != self._kis_token_scope():
            return False
        now = datetime.now(tz=timezone.utc)
        if (expires_at - now).total_seconds() < self.refresh_margin_sec:
            return False
        self._kis_token = token
        self._kis_token_expires_at = expires_at
        return True

    def _store_shared_kis_token(self, token: str, expires_at: datetime) -> None:
        """KIS OAuth 재발급 제한 회피를 위한 프로세스 단위 메모리 캐시."""
        cls = type(self)
        cls._shared_kis_token = token
        cls._shared_kis_token_expires_at = expires_at
        cls._shared_kis_token_scope = self._kis_token_scope()

    def _fetch_kis_token(self) -> tuple[str, datetime]:
        """KIS OAuth POST /oauth2/tokenP 호출. 재시도 3회 포함.

        Returns:
            (access_token, expires_at_utc)

        Raises:
            AuthenticationError: 401/403 응답
            ConnectionError: 재시도 소진
        """
        app_key, app_secret = self.get_kis_app_credentials()
        base_url = self.get_kis_base_url()
        url = f"{base_url}{_KIS_TOKEN_PATH}"

        payload = {
            "grant_type": "client_credentials",
            "appkey": app_key,
            "appsecret": app_secret,
        }
        headers = {"Content-Type": "application/json; charset=UTF-8"}

        last_exc: Exception | None = None
        for attempt, delay in enumerate(self.retry_delays, start=1):
            try:
                response = self._http_post(url, payload, headers)
                status = response.get("_status_code", 200)

                if status in (401, 403):
                    msg_cd = str(response.get("msg_cd") or response.get("error_code") or "")
                    msg = str(response.get("msg1") or response.get("msg") or response.get("error_description") or "")
                    detail = (
                        f" msg_cd={msg_cd} msg={msg}"
                        if msg_cd or msg else ""
                    )
                    error = AuthenticationError(
                        f"KIS 인증 실패 (HTTP {status}). appkey/appsecret 확인 필요.{detail}"
                    )
                    if msg_cd == "EGW00133" and attempt < len(self.retry_delays):
                        last_exc = error
                        logger.warning(
                            "KIS 토큰 발급 제한(EGW00133). 시도 %d/%d, %d초 후 재시도.",
                            attempt,
                            len(self.retry_delays),
                            self.token_rate_limit_retry_sec,
                        )
                        time.sleep(self.token_rate_limit_retry_sec)
                        continue
                    raise error
                if status != 200:
                    raise ConnectionError(f"KIS 토큰 API 오류 (HTTP {status})")

                access_token: str = response["access_token"]
                expires_in: int = int(response.get("expires_in", 86400))
                expires_at = datetime.now(tz=timezone.utc) + timedelta(seconds=expires_in)
                return access_token, expires_at

            except AuthenticationError:
                raise  # 인증 실패는 재시도 불필요
            except Exception as e:
                last_exc = e
                logger.warning(
                    "KIS 토큰 발급 실패 (시도 %d/%d): %s. %d초 후 재시도.",
                    attempt,
                    len(self.retry_delays),
                    e,
                    delay,
                )
                if attempt < len(self.retry_delays):
                    time.sleep(delay)

        raise ConnectionError(
            f"KIS 토큰 발급 {len(self.retry_delays)}회 재시도 소진: {last_exc}"
        )

    def _mode_specific_kis_keys(self, suffix: str) -> list[str]:
        """KIS_MODE별 env 후보. 명시적 paper/real 값을 generic 값보다 우선한다."""
        mode = self.get_mode()
        if mode == "virtual":
            return [f"KIS_PAPER_{suffix}", f"KIS_{suffix}"]
        if mode == "real":
            return [f"KIS_REAL_{suffix}", f"KIS_{suffix}"]
        return [f"KIS_{suffix}"]

    def _require_first_env(self, keys: list[str]) -> str:
        for key in keys:
            value = os.getenv(key, "").strip()
            if value:
                return value
        joined = "/".join(keys)
        raise EnvironmentError(f"필수 환경변수 누락: {joined}")

    def _http_post(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        """HTTP POST. requests 사용. 없으면 urllib fallback.

        timeout은 risk_config.yaml connector_defaults.timeout_sec 경유 (불변 원칙 5).
        """
        timeout_cfg = config_load("risk_config.yaml", "connector_defaults")
        timeout = int(timeout_cfg["timeout_sec"])

        try:
            import requests  # noqa: PLC0415

            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
            body: dict[str, Any] = resp.json()
            body["_status_code"] = resp.status_code
            return body
        except ImportError:
            # urllib fallback
            import urllib.request  # noqa: PLC0415

            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                url, data=data, headers=headers, method="POST"
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                body["_status_code"] = resp.status
                return body

    @staticmethod
    def _require_env(key: str) -> str:
        """환경변수 조회. 미설정이면 EnvironmentError."""
        value = os.getenv(key)
        if not value:
            raise EnvironmentError(f"필수 환경변수 {key} 누락")
        return value
