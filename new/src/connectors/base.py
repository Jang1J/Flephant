"""BaseConnector: 7개 커넥터 공통 추상 기반 클래스.

S2-11 실구현. 중복 패턴 추출:
  - connector_defaults (timeout_sec / max_retries / backoff_base) 로드
  - _http_get_json(url, params, headers=None): requests + urllib fallback

각 커넥터는 이 클래스를 상속하여 중복 초기화 코드를 제거한다.
커넥터별 auth / rate_limiter 관리는 각 클래스에서 유지 (auth 방식이 다름).

불변 원칙 5: 모든 수치는 risk_config.yaml connector_defaults에서 로드.
"""
from __future__ import annotations

import json
from typing import Any

from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger

logger = get_logger("base_connector")


class BaseConnector:
    """커넥터 공통 기반 클래스.

    상속 시 __init__에서 super().__init__()을 호출하여
    timeout_sec / max_retries / backoff_base를 자동 로드한다.
    """

    def __init__(self) -> None:
        self._load_defaults()

    def _load_defaults(self) -> None:
        """risk_config.yaml connector_defaults 로드.

        설정: timeout_sec / max_retries / backoff_base
        불변 원칙 5: 하드코딩 금지. yaml SSOT.
        """
        try:
            defaults = config_load("risk_config.yaml", "connector_defaults")
            self.timeout_sec: int = int(defaults.get("timeout_sec", 10))
            self.max_retries: int = int(defaults.get("max_retries", 3))
            self.backoff_base: int = int(defaults.get("backoff_base", 2))
        except Exception as e:
            logger.error(
                "[base_connector] connector_defaults 로드 실패, 비상 기본값 사용: %s", e
            )
            self.timeout_sec = 10
            self.max_retries = 3
            self.backoff_base = 2

    def _http_get_json(
        self,
        url: str,
        params: dict[str, str],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """HTTP GET + JSON 파싱. requests 우선, 없으면 urllib fallback.

        Args:
            url: 요청 URL
            params: 쿼리 파라미터 dict
            headers: 추가 HTTP 헤더 (Naver API 등 헤더 인증 커넥터용)

        Raises:
            TimeoutError: 요청 timeout 초과.
            ConnectionError: 네트워크 레벨 에러.
        """
        try:
            import requests  # noqa: PLC0415

            try:
                resp = requests.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=self.timeout_sec,
                )
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.Timeout as e:
                raise TimeoutError(
                    f"HTTP timeout ({self.timeout_sec}s): {e}"
                ) from e
            except requests.exceptions.ConnectionError as e:
                raise ConnectionError(f"HTTP 연결 실패: {e}") from e

        except ImportError:
            # urllib fallback (requests 미설치 환경)
            import urllib.error  # noqa: PLC0415
            import urllib.parse  # noqa: PLC0415
            import urllib.request  # noqa: PLC0415

            query = urllib.parse.urlencode(params)
            full_url = f"{url}?{query}"
            req = urllib.request.Request(full_url, headers=headers or {})
            try:
                with urllib.request.urlopen(
                    req, timeout=self.timeout_sec
                ) as resp:
                    body = resp.read().decode("utf-8")
                    return json.loads(body)
            except TimeoutError:
                raise
            except urllib.error.URLError as e:
                raise ConnectionError(f"HTTP urllib 연결 실패: {e}") from e
