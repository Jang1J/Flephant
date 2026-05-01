"""DART Open API REST 커넥터. C2 공시 이벤트 수신 + 정규화.

Sprint 0 S0-3 실구현. 스텁에서 완전 동작 버전으로 교체.

불변 원칙 준수:
  - 인증: AuthManager.get_dart_key() 경유 (.env 직접 접근 금지)
  - Rate: RateLimiter("dart") 경유 (risk_config.yaml rate_limits.dart)
  - 출력: EventNormalizer.normalize(raw, source="dart") 경유
  - 하드코딩 금지: BASE_URL은 공식 고정 endpoint (enum 성격. 허용)
"""
from __future__ import annotations

import time
from datetime import date
from typing import Any

from src.connectors.base import BaseConnector
from src.data.event_normalizer import EventNormalizer
from src.utils.auth import AuthManager
from src.utils.logger import get_logger
from src.utils.pit_guard import PITViolationError, is_pit_safe
from src.utils.rate_limiter import RateLimiter
from src.utils.ticker_utils import pad_ticker
from src.utils.time_utils import now_kst

logger = get_logger("dart_rest")


class DARTAPIError(Exception):
    """DART API status != "000" (013 제외)."""


class DARTRestClient(BaseConnector):
    """DART Open API 공시 검색 + 정규화.

    주요 기능:
      - list_disclosures(): 공시 목록 조회 → C2 정규화 이벤트 리스트
      - get_company(): 기업 개황 조회 → 원본 dict
      - corpCode.xml 다운로드는 S0-8 구현 예정 (TODO)

    불변 원칙: 키는 AuthManager 경유, rate는 RateLimiter 경유, 출력은 EventNormalizer.
    """

    # 공식 고정 endpoint (enum 성격 상수. 하드코딩 예외 허용)
    BASE_URL = "https://opendart.fss.or.kr/api"

    def __init__(
        self,
        auth: AuthManager | None = None,
        rate_limiter: RateLimiter | None = None,
        normalizer: EventNormalizer | None = None,
    ) -> None:
        """의존성 주입. 미지정 시 자동 생성 (테스트에서 mock 주입 가능).

        네트워크 정책 (timeout/retry/backoff)은 risk_config.yaml
        connector_defaults 섹션에서 로드 (불변 원칙 5).

        Mock 분기: DART_API_KEY 미설정 시 mock 모드 자동 전환 (Naver 패턴 통일, 2026-04-21).
        """
        super().__init__()  # BaseConnector: timeout_sec / max_retries / backoff_base 로드
        self.auth = auth or AuthManager()
        self.rate_limiter = rate_limiter or RateLimiter("dart")
        self.normalizer = normalizer or EventNormalizer()

        # Mock 분기 (Naver 와 패턴 통일, 2026-04-21)
        try:
            self._api_key: str | None = self.auth.get_dart_key()
            self._is_mock = self._api_key is None
        except (EnvironmentError, KeyError):
            self._api_key = None
            self._is_mock = True

        if self._is_mock:
            logger.info("[dart_rest] DART_API_KEY 미설정. Mock 모드로 동작.")

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def list_disclosures(
        self,
        corp_code: str | None = None,
        bgn_de: str | None = None,
        end_de: str | None = None,
        pblntf_ty: str | None = None,
        page_no: int = 1,
        page_count: int = 100,
    ) -> list[dict[str, Any]]:
        """공시 목록 조회. C2 정규화된 event dict 리스트 반환.

        Args:
            corp_code: DART 회사코드 8자리. None이면 전체 조회.
            bgn_de: 시작일 YYYYMMDD.
            end_de: 종료일 YYYYMMDD.
            pblntf_ty: 공시 유형 (A=정기공시, B=주요사항보고, C=발행공시, D=지분공시,
                       E=기타, F=외부감사, G=펀드, H=자산유동화, I=거래소공시).
            page_no: 페이지 번호 (1-based).
            page_count: 페이지당 결과 수 (최대 100).

        Returns:
            C2 EventNormalizeContract events 리스트.
            각 항목: event_id, source="dart", event_type="dart", ... 포함.

        Raises:
            DARTAPIError: status != "000" (단 013은 [] 반환).
            ConnectionError: 3회 재시도 후 네트워크 실패.
        """
        if self._is_mock:
            return self._mock_list_disclosures(corp_code, page_count)

        # PIT-Safety guard: 미래 날짜 요청 차단 (불변 원칙 1)
        for date_param, label in ((bgn_de, "bgn_de"), (end_de, "end_de")):
            if date_param and not is_pit_safe(
                f"{date_param[:4]}-{date_param[4:6]}-{date_param[6:8]}T23:59:59+09:00"
            ):
                raise PITViolationError(
                    f"[dart_rest] 미래 날짜 요청 차단: {label}={date_param}"
                )

        # None 값 파라미터 제외 (DART API는 빈 문자열도 오류 유발)
        params: dict[str, str] = {
            k: v
            for k, v in {
                "corp_code": corp_code,
                "bgn_de": bgn_de,
                "end_de": end_de,
                "pblntf_ty": pblntf_ty,
                "page_no": str(page_no),
                "page_count": str(page_count),
            }.items()
            if v is not None
        }

        response = self._call_api("list.json", params)

        status = response.get("status")

        if status == "013":
            # 조회된 데이터 없음: 정상 응답 범주
            safe_params = {k: ("***" if k == "crtfc_key" else v) for k, v in params.items()}
            logger.info("[dart_rest] 공시 조회 결과 없음. params=%s", safe_params)
            return []

        if status != "000":
            raise DARTAPIError(
                f"[dart_rest] DART list.json 오류. "
                f"status={status} message={response.get('message')}"
            )

        # 각 item을 C2 event로 정규화
        normalized: list[dict[str, Any]] = []
        for item in response.get("list", []):
            raw = self._dart_item_to_raw(item)
            try:
                event = self.normalizer.normalize(raw, source="dart")
                normalized.append(event)
            except Exception as e:
                logger.warning(
                    "[dart_rest] 공시 정규화 실패. rcept_no=%s error=%s",
                    item.get("rcept_no"),
                    e,
                )
                continue

        logger.info(
            "[dart_rest] 공시 목록 정규화 완료. 총 %d건 중 %d건 성공.",
            len(response.get("list", [])),
            len(normalized),
        )
        return normalized

    def get_company(self, corp_code: str) -> dict[str, Any]:
        """기업 개황 조회. 원본 dict 반환 (C2 정규화 없음).

        Args:
            corp_code: DART 회사코드 8자리.

        Returns:
            DART company.json 응답 원본 dict (status, corp_name 등 포함).

        Raises:
            DARTAPIError: status != "000".
            ConnectionError: 3회 재시도 후 네트워크 실패.
        """
        params: dict[str, str] = {"corp_code": corp_code}
        response = self._call_api("company.json", params)

        if response.get("status") != "000":
            raise DARTAPIError(
                f"[dart_rest] DART company.json 오류. "
                f"status={response.get('status')} "
                f"message={response.get('message')}"
            )

        logger.info(
            "[dart_rest] 기업 개황 조회 완료. corp_code=%s corp_name=%s",
            corp_code,
            response.get("corp_name", ""),
        )
        return response

    # ------------------------------------------------------------------ #
    # 내부 변환 메서드
    # ------------------------------------------------------------------ #

    def _dart_item_to_raw(self, item: dict[str, Any]) -> dict[str, Any]:
        """DART list 응답 item을 EventNormalizer._normalize_dart 입력 포맷으로 변환.

        EventNormalizer._normalize_dart 필수 필드:
          - title         ← report_nm
          - corp_name     ← corp_name
          - disclosure_time ← rcept_dt (YYYYMMDD → ISO 8601 KST)

        선택 필드:
          - summary       ← rm 또는 report_nm (rm 우선)
          - ticker        ← stock_code (zfill(6))
          - corp_code, rcept_no, flr_nm, corp_cls (원본 보존)
        """
        rcept_dt = item.get("rcept_dt", "")
        if rcept_dt and len(rcept_dt) == 8:
            occurred_at = self._yyyymmdd_to_iso_kst(rcept_dt)
        else:
            # rcept_dt 없거나 형식 이상: 현재 시각으로 fallback
            logger.warning(
                "[dart_rest] rcept_dt 형식 이상. rcept_no=%s rcept_dt=%r. 현재 시각으로 대체.",
                item.get("rcept_no"),
                rcept_dt,
            )
            occurred_at = now_kst().isoformat()

        raw: dict[str, Any] = {
            "title": item.get("report_nm", ""),
            "corp_name": item.get("corp_name", ""),
            "disclosure_time": occurred_at,
            "summary": item.get("rm") or item.get("report_nm", ""),
        }

        # stock_code가 있고 비어 있지 않으면 ticker로 추가
        stock_code = item.get("stock_code", "").strip()
        if stock_code:
            raw["ticker"] = pad_ticker(stock_code)

        # 원본 필드 보존 (EventNormalizer payload에 포함됨)
        for key in ("corp_code", "rcept_no", "flr_nm", "corp_cls"):
            val = item.get(key)
            if val:
                raw[key] = val

        return raw

    @staticmethod
    def _yyyymmdd_to_iso_kst(yyyymmdd: str) -> str:
        """'20260419' → '2026-04-19T00:00:00+09:00'.

        DART rcept_dt는 일자만 제공. 시각은 00:00 KST로 고정.
        """
        parsed = date(
            int(yyyymmdd[0:4]),
            int(yyyymmdd[4:6]),
            int(yyyymmdd[6:8]),
        )
        return f"{parsed.isoformat()}T00:00:00+09:00"

    # ------------------------------------------------------------------ #
    # HTTP 레이어
    # ------------------------------------------------------------------ #

    def _call_api(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        """내부 HTTP 호출. rate_limiter + auth 주입 + 재시도 + timeout.

        흐름:
          1. rate_limiter.wait_and_acquire() (blocking, 최대 10초 대기)
          2. params에 crtfc_key 자동 주입
          3. 최대 3회 재시도, 지수 백오프 (1s / 2s / 4s)
          4. timeout 10초

        로그에서 crtfc_key 값은 "***"로 마스킹.

        Raises:
            ConnectionError: 3회 재시도 후 전부 실패.
        """
        self.rate_limiter.wait_and_acquire()
        key = self._api_key if self._api_key is not None else self.auth.get_dart_key()
        full_params: dict[str, str] = {"crtfc_key": key, **params}
        url = f"{self.BASE_URL}/{path}"

        # 로그용 마스킹 파라미터 (키 값 노출 금지)
        log_params = {k: ("***" if k == "crtfc_key" else v) for k, v in full_params.items()}

        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                logger.info(
                    "[dart_rest] API 호출 시작. path=%s params=%s attempt=%d",
                    path,
                    log_params,
                    attempt + 1,
                )
                return self._http_get_json(url, full_params)
            except (TimeoutError, ConnectionError) as e:
                last_err = e
                if attempt < self.max_retries - 1:
                    wait_sec = self.backoff_base ** attempt  # backoff_base=2 시 1s/2s
                    logger.warning(
                        "[dart_rest] DART API 재시도 %d/%d. wait=%ds path=%s error=%s",
                        attempt + 1,
                        self.max_retries,
                        wait_sec,
                        path,
                        e,
                    )
                    time.sleep(wait_sec)

        raise ConnectionError(
            f"[dart_rest] DART API {path} {self.max_retries}회 재시도 실패. last_err={last_err}"
        )

    # _http_get_json: BaseConnector에서 상속 (S2-11 BaseConnector 리팩토링)

    # ------------------------------------------------------------------ #
    # Mock
    # ------------------------------------------------------------------ #

    def _mock_list_disclosures(
        self,
        corp_code: str | None,
        page_count: int,
    ) -> list[dict[str, Any]]:
        """Mock 응답 (DART_API_KEY 미설정 또는 테스트용). 최대 3개 반환.

        Naver 커넥터 _mock_search 와 동일 패턴 (2026-04-21).
        """
        from src.utils.time_utils import now_kst

        count = min(page_count, 3)
        corp = corp_code or "00000000"
        now_iso = now_kst().isoformat()
        mock_items = []
        for i in range(count):
            raw = {
                "title": f"[MOCK] 공시 {i + 1} (corp_code={corp})",
                "corp_name": f"MOCK기업{i + 1}",
                "disclosure_time": now_iso,
                "summary": f"Mock disclosure {i + 1}",
            }
            try:
                event = self.normalizer.normalize(raw, source="dart")
                mock_items.append(event)
            except Exception as e:
                logger.warning("[dart_rest] Mock 정규화 실패 skip. idx=%d err=%s", i, e)
        return mock_items
