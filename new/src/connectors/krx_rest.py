"""KRX 공공데이터포털 REST 커넥터. 일별 시세 조회 + C2 정규화.

Sprint 0 S0-4 실구현. 스텁에서 완전 동작 버전으로 교체.

불변 원칙 준수:
  - 인증: AuthManager.get_krx_key() 경유 (.env 직접 접근 금지)
  - Rate: RateLimiter("krx_investor_flow") 경유 (risk_config.yaml rate_limits.krx_investor_flow)
  - 출력: EventNormalizer.normalize(raw, source="krx_investor_flow") 경유
  - 하드코딩 금지: BASE_URL은 공식 고정 endpoint (enum 성격. 허용)

구현 범위 (Sprint 0):
  - 일별 시세 조회 (OHLCV)
  - 투자자별 수급: placeholder None + Sprint 1 TODO
    (공공데이터포털 KRX 일별 시세 엔드포인트에 투자자별 수급 없음)

Mock 분기: KRX_API_KEY 미설정 시 mock 모드 자동 전환 (DART/Naver 패턴 통일, 2026-04-21).
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

logger = get_logger("krx_rest")


class KRXAPIError(Exception):
    """KRX 공공데이터포털 API resultCode != "00"."""


class KRXRestClient(BaseConnector):
    """KRX 공공데이터포털 일별 시세 조회 + 정규화.

    주요 기능:
      - get_stock_price_info(): 일별 시세 OHLCV 조회 → C2 정규화 이벤트 리스트

    투자자별 수급(외국인/기관/개인)은 Sprint 1에서 별도 엔드포인트로 추가 예정.
    현재 일별 시세 응답에 수급 데이터 없음.

    불변 원칙: 키는 AuthManager 경유, rate는 RateLimiter 경유, 출력은 EventNormalizer.
    """

    # 공식 고정 endpoint (enum 성격 상수. 하드코딩 예외 허용)
    BASE_URL = "https://apis.data.go.kr/1160100/service/GetStockSecuritiesInfoService"
    # S2-5a: 투자자별 수급 전용 엔드포인트 (별도 서비스)
    INVESTOR_URL = (
        "https://apis.data.go.kr/1160100/service"
        "/GetStockInvestorInfoService/getStockInvestorInfoService"
    )

    def __init__(
        self,
        auth: AuthManager | None = None,
        rate_limiter: RateLimiter | None = None,
        normalizer: EventNormalizer | None = None,
    ) -> None:
        """의존성 주입. 미지정 시 자동 생성 (테스트에서 mock 주입 가능).

        네트워크 정책 (timeout/retry/backoff)은 risk_config.yaml
        connector_defaults 섹션에서 로드 (불변 원칙 5).

        Mock 분기: KRX_API_KEY 미설정 시 mock 모드 자동 전환 (DART/Naver 패턴 통일, 2026-04-21).
        """
        super().__init__()  # BaseConnector: timeout_sec / max_retries / backoff_base 로드
        self.auth = auth or AuthManager()
        self.rate_limiter = rate_limiter or RateLimiter("krx_investor_flow")
        self.normalizer = normalizer or EventNormalizer()

        # Mock 자동 분기 (DART/Naver 패턴 통일, 2026-04-21)
        try:
            self._api_key: str | None = self.auth.get_krx_key()
            self._is_mock = self._api_key is None
        except (EnvironmentError, KeyError):
            self._api_key = None
            self._is_mock = True

        if self._is_mock:
            logger.info("[krx_rest] KRX_API_KEY 미설정. Mock 모드로 동작.")

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def get_stock_price_info(
        self,
        bgn_de: str | None = None,
        end_de: str | None = None,
        ticker: str | None = None,
        page_no: int = 1,
        num_of_rows: int = 100,
    ) -> list[dict[str, Any]]:
        """일별 시세 조회. C2 정규화된 event(source="krx") 리스트 반환.

        Args:
            bgn_de: 시작일 YYYYMMDD. None이면 전체 조회.
            end_de: 종료일 YYYYMMDD. None이면 전체 조회.
            ticker: 종목코드 (6자리 또는 단축코드). None이면 전체 조회.
            page_no: 페이지 번호 (1-based).
            num_of_rows: 페이지당 결과 수.

        Returns:
            C2 EventNormalizeContract events 리스트.
            각 항목: event_id, source="krx_investor_flow", event_type="investor_flow", ... 포함.

        Raises:
            KRXAPIError: resultCode != "00".
            ConnectionError: 3회 재시도 후 네트워크 실패.
        """
        if self._is_mock:
            return self._mock_stock_price_info(
                ticker=ticker,
                bgn_de=bgn_de,
                end_de=end_de,
                num_of_rows=num_of_rows,
            )

        # PIT-Safety guard: 미래 날짜 요청 차단 (불변 원칙 1)
        for date_param, label in ((bgn_de, "bgn_de"), (end_de, "end_de")):
            if date_param and not is_pit_safe(
                f"{date_param[:4]}-{date_param[4:6]}-{date_param[6:8]}T23:59:59+09:00"
            ):
                raise PITViolationError(
                    f"[krx_rest] 미래 날짜 요청 차단: {label}={date_param}"
                )

        # None 값 파라미터 제외 (빈 문자열도 오류 유발 가능)
        params: dict[str, str] = {
            k: v
            for k, v in {
                "beginBasDt": bgn_de,
                "endBasDt": end_de,
                "likeSrtnCd": str(ticker).zfill(6) if ticker is not None else None,
                "pageNo": str(page_no),
                "numOfRows": str(num_of_rows),
                "resultType": "json",
            }.items()
            if v is not None
        }

        response = self._call_api("getStockPriceInfo", params)

        body = response.get("response", {}).get("body", {})
        header = response.get("response", {}).get("header", {})
        result_code = header.get("resultCode", "")

        if result_code != "00":
            raise KRXAPIError(
                f"[krx_rest] KRX API 오류. "
                f"resultCode={result_code} resultMsg={header.get('resultMsg')}"
            )

        total_count = int(body.get("totalCount", 0))
        if total_count == 0:
            logger.info("[krx_rest] 일별 시세 조회 결과 없음. params=%s", {
                k: ("***" if k == "serviceKey" else v) for k, v in params.items()
            })
            return []

        items_raw = body.get("items", {})
        if isinstance(items_raw, dict):
            items = items_raw.get("item", [])
        else:
            items = []

        # 단건 응답도 dict로 올 수 있음 (목록은 list, 단건은 dict)
        if isinstance(items, dict):
            items = [items]

        normalized: list[dict[str, Any]] = []
        for item in items:
            try:
                raw = self._item_to_raw(item)
                event = self.normalizer.normalize(raw, source="krx_investor_flow")
                normalized.append(event)
            except Exception as e:
                logger.warning(
                    "[krx_rest] 시세 정규화 실패. srtnCd=%s basDt=%s error=%s",
                    item.get("srtnCd"),
                    item.get("basDt"),
                    e,
                )
                continue

        logger.info(
            "[krx_rest] 일별 시세 정규화 완료. totalCount=%d, 정규화 성공=%d건.",
            total_count,
            len(normalized),
        )
        return normalized

    # ------------------------------------------------------------------ #
    # Mock 응답
    # ------------------------------------------------------------------ #

    def _mock_stock_price_info(
        self,
        ticker: str | None,
        bgn_de: str | None,
        end_de: str | None,
        num_of_rows: int,
    ) -> list[dict[str, Any]]:
        """Mock: OHLCV 3개 일별 데이터 반환. DART/Naver 패턴 통일 (2026-04-21).

        KRX_API_KEY 미설정 환경에서 instantiation 실패 방지.
        실 API 연결은 KRX_API_KEY 설정 후 자동 전환.
        """
        ticker_str = str(ticker).zfill(6) if ticker is not None else "005930"
        count = min(num_of_rows, 3)

        mock_items = []
        base_dates = ["20260418", "20260419", "20260420"]
        for i in range(count):
            bas_dt = base_dates[i % len(base_dates)]
            raw = self._item_to_raw({
                "srtnCd": ticker_str,
                "basDt": bas_dt,
                "clpr": "70000",
                "mkp": "69500",
                "hipr": "70500",
                "lopr": "69000",
                "trqu": "1000000",
                "trPrc": "70000000000",
                "vs": "500",
                "fltRt": "0.72",
                "isinCd": f"KR{ticker_str}",
                "itmsNm": f"MOCK_{ticker_str}",
                "mrktCtg": "KOSPI",
            })
            try:
                event = self.normalizer.normalize(raw, source="krx_investor_flow")
                mock_items.append(event)
            except Exception as e:
                logger.warning(
                    "[krx_rest] Mock 정규화 실패. ticker=%s error=%s",
                    ticker_str, e,
                )

        logger.info(
            "[krx_rest] Mock 일별 시세 반환. ticker=%s, %d건.",
            ticker_str, len(mock_items),
        )
        return mock_items

    # ------------------------------------------------------------------ #
    # S1-0a 투자자별 수급 (2026-04-20 추가, 실 API 연결은 Sprint 2 defer)
    # ------------------------------------------------------------------ #

    def get_investor_info(
        self,
        ticker: str,
        bgn_de: str,
        end_de: str,
        mock_response: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """S1-0a: 투자자별 수급 (외국인/기관/개인 net buy). C3 investor_flow 3 피처.

        Sprint 1 MVP: mock_response 주입 시 정규화만 수행 (테스트/dev용).
        실 API 연결은 Sprint 2 S2-5에서:
          - 공공데이터포털 "KRX 투자자별 거래 실적" 엔드포인트 연결 or
          - KRX 공식 웹 크롤링 (data.krx.co.kr)
          - rate_limiter("krx_investor_flow") 재사용, timeout 10s

        Args:
            ticker: 종목코드 (6자리)
            bgn_de, end_de: YYYYMMDD 기간
            mock_response: 테스트용 raw 응답 (실 API 호출 대체)

        Returns: C2 EventNormalizeContract events 리스트 (source=krx_investor_flow).
                 각 event payload: {foreign_net_buy, institutional, retail}
        """
        ticker_padded = pad_ticker(str(ticker))

        if mock_response is None:
            if self._is_mock:
                # KRX_API_KEY 미설정: mock 데이터 반환
                logger.info(
                    "[krx_rest] get_investor_info Mock 모드. ticker=%s → mock 반환.",
                    ticker_padded,
                )
                return self._mock_investor_info(ticker_padded, bgn_de, end_de)
            # S2-5a 실 API 경로 (PIT-Safety guard)
            for date_param, label in [(bgn_de, "bgn_de"), (end_de, "end_de")]:
                if date_param and not is_pit_safe(date_param):
                    raise PITViolationError(
                        f"[krx_rest] 미래 날짜 요청 차단: {label}={date_param}"
                    )
            return self._fetch_investor_info(ticker_padded, bgn_de, end_de)

        # rate limiter는 실 API 호출할 때만 의미. mock 경로도 기본 acquire.
        self.rate_limiter.wait_and_acquire()

        normalized: list[dict[str, Any]] = []
        for item in mock_response:
            try:
                # mock response는 이미 C3 필드 포함 가정.
                # C3 SSOT (api_contracts.md feature_manifest investor_flow):
                #   foreign_net_buy / institutional_net_buy / retail_net_buy
                raw = {
                    "ticker": ticker_padded,
                    "date": self._yyyymmdd_to_iso_kst(
                        str(item.get("bas_dt", bgn_de))
                    ),
                    "foreign_net_buy": item.get("foreign_net_buy", 0.0),
                    "institutional_net_buy": item.get(
                        "institutional_net_buy",
                        item.get("institutional", 0.0),   # 레거시 키 호환
                    ),
                    "retail_net_buy": item.get(
                        "retail_net_buy",
                        item.get("retail", 0.0),          # 레거시 키 호환
                    ),
                }
                event = self.normalizer.normalize(raw, source="krx_investor_flow")
                normalized.append(event)
            except Exception as e:
                logger.warning(
                    "[krx_rest] investor_info 정규화 실패. ticker=%s error=%s",
                    ticker_padded, e,
                )
                continue

        logger.info(
            "[krx_rest] investor_info 정규화 완료. ticker=%s, 성공=%d건",
            ticker_padded, len(normalized),
        )
        return normalized

    # ------------------------------------------------------------------ #
    # 내부 변환 메서드
    # ------------------------------------------------------------------ #

    def _item_to_raw(self, item: dict[str, Any]) -> dict[str, Any]:
        """KRX 일별 시세 item → EventNormalizer._normalize_krx 입력 포맷.

        EventNormalizer._normalize_krx 필수 필드:
          - ticker: srtnCd (zfill 6)
          - date: basDt (YYYYMMDD → ISO 8601 KST)

        선택 필드 (현재 일별 시세 엔드포인트에 없음. Sprint 1에서 수급 엔드포인트 추가 예정):
          - foreign_net_buy: None (TODO Sprint 1)
          - institutional: None (TODO Sprint 1)
          - retail: None (TODO Sprint 1)

        추가 원본 필드 (payload 보존):
          - clpr, mkp, hipr, lopr, trqu, trPrc, vs, fltRt, isinCd, itmsNm, mrktCtg
        """
        srtn_cd = item.get("srtnCd", "").strip()
        bas_dt = item.get("basDt", "").strip()

        if not srtn_cd:
            raise ValueError("[krx_rest] srtnCd 누락")
        if not bas_dt or len(bas_dt) != 8:
            raise ValueError(f"[krx_rest] basDt 형식 이상: {bas_dt!r}")

        ticker_str = pad_ticker(srtn_cd)
        date_iso = self._yyyymmdd_to_iso_kst(bas_dt)

        # 투자자별 수급은 이 엔드포인트에 없음 (Sprint 1 TODO)
        logger.debug(
            "[krx_rest] 투자자별 수급 데이터 없음 (일별 시세 엔드포인트). "
            "foreign_net_buy/institutional/retail = None. Sprint 1에서 수급 엔드포인트 추가 예정."
        )

        raw: dict[str, Any] = {
            "ticker": ticker_str,
            "date": date_iso,
            # 수급 필드는 선택이므로 None 그대로 (EventNormalizer 통과)
            "foreign_net_buy": None,
            "institutional": None,
            "retail": None,
        }

        # OHLCV 원본 필드 보존 (payload에 포함됨)
        for key in ("clpr", "mkp", "hipr", "lopr", "trqu", "trPrc", "vs", "fltRt",
                    "isinCd", "itmsNm", "mrktCtg"):
            val = item.get(key)
            if val is not None:
                raw[key] = val

        return raw

    @staticmethod
    def _yyyymmdd_to_iso_kst(yyyymmdd: str) -> str:
        """'20260419' → '2026-04-19T00:00:00+09:00'.

        KRX basDt는 일자만 제공. 수급 발표 기준 15:30 KST가 자연스럽지만
        EventNormalizer._normalize_krx가 15:30 KST로 보정하므로 여기서는 ISO 8601 형식만 맞춤.
        10자리 날짜 문자열(YYYY-MM-DD)을 그대로 전달해도 동일 결과.
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
          2. params에 serviceKey 자동 주입
          3. 최대 3회 재시도, 지수 백오프 (1s / 2s / 4s)
          4. timeout 10초

        로그에서 serviceKey 값은 "***"로 마스킹.

        Raises:
            RuntimeError: mock 모드에서 직접 호출 시 (auth.get_krx_key() 실 호출 방지).
            ConnectionError: 3회 재시도 후 전부 실패.
        """
        if self._is_mock:
            raise RuntimeError(
                "[krx_rest] _call_api는 mock 모드에서 호출 불가. "
                "get_stock_price_info() / get_investor_info()의 mock 분기를 사용하라."
            )
        self.rate_limiter.wait_and_acquire()
        key = self.auth.get_krx_key()
        full_params: dict[str, str] = {"serviceKey": key, **params}
        url = f"{self.BASE_URL}/{path}"

        # 로그용 마스킹 파라미터 (키 값 노출 금지)
        log_params = {k: ("***" if k == "serviceKey" else v) for k, v in full_params.items()}

        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                logger.info(
                    "[krx_rest] API 호출 시작. path=%s params=%s attempt=%d",
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
                        "[krx_rest] KRX API 재시도 %d/%d. wait=%ds path=%s error=%s",
                        attempt + 1,
                        self.max_retries,
                        wait_sec,
                        path,
                        e,
                    )
                    time.sleep(wait_sec)

        raise ConnectionError(
            f"[krx_rest] KRX API {path} {self.max_retries}회 재시도 실패. last_err={last_err}"
        )

    # _http_get_json: BaseConnector에서 상속 (S2-11 BaseConnector 리팩토링)

    # ------------------------------------------------------------------ #
    # S2-5a: 투자자별 수급 helper 메서드
    # ------------------------------------------------------------------ #

    def _fetch_investor_info(
        self, ticker: str, bgn_de: str, end_de: str
    ) -> list[dict[str, Any]]:
        """공공데이터포털 getStockInvestorInfoService 실 API 호출.

        bgn_de ~ end_de 각 날짜에 대해 단건 요청 (API 특성상 날짜 범위 미지원).
        """
        from datetime import datetime as _dt, timedelta as _td  # noqa: PLC0415

        start = _dt.strptime(bgn_de, "%Y%m%d").date()
        end = _dt.strptime(end_de, "%Y%m%d").date()
        results: list[dict[str, Any]] = []

        cur = start
        while cur <= end:
            if cur.weekday() < 5:  # 월~금 영업일
                date_str = cur.strftime("%Y%m%d")
                self.rate_limiter.wait_and_acquire()
                params = {
                    "serviceKey": self._api_key or "",
                    "resultType": "json",
                    "basDt": date_str,
                    "likeSrtnCd": ticker,
                    "numOfRows": "100",
                    "pageNo": "1",
                }
                try:
                    body = self._http_get_json(self.INVESTOR_URL, params)
                    items = self._extract_investor_items(body)
                    for item in items:
                        raw = self._parse_investor_item(item, ticker, date_str)
                        if raw:
                            event = self.normalizer.normalize(
                                raw, source="krx_investor_flow"
                            )
                            results.append(event)
                except Exception as e:
                    logger.warning(
                        "[krx_rest] investor_info 실 API 실패. date=%s error=%s",
                        date_str, e,
                    )
            cur += _td(days=1)

        logger.info(
            "[krx_rest] investor_info 실 API 완료. ticker=%s 건수=%d",
            ticker, len(results),
        )
        return results

    @staticmethod
    def _extract_investor_items(body: dict[str, Any]) -> list[dict[str, Any]]:
        """공공데이터포털 응답에서 item 리스트 추출."""
        try:
            items = body["response"]["body"]["items"]["item"]
            return items if isinstance(items, list) else [items]
        except (KeyError, TypeError):
            return []

    @staticmethod
    def _parse_investor_item(
        item: dict[str, Any], ticker: str, date_str: str
    ) -> dict[str, Any] | None:
        """API 응답 item → C3 investor_flow 포맷 변환.

        외국인/기관/개인 net buy = 매수량 - 매도량.
        """
        try:
            frg_buy = int(item.get("frgStkbuyVolm", 0) or 0)
            frg_sel = int(item.get("frgStkselVolm", 0) or 0)
            org_buy = int(item.get("orgStkbuyVolm", 0) or 0)
            org_sel = int(item.get("orgStkselVolm", 0) or 0)
            ind_buy = int(item.get("indvStkbuyVolm", 0) or 0)
            ind_sel = int(item.get("indvStkselVolm", 0) or 0)
            return {
                "ticker": ticker,
                "date": f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}T15:30:00+09:00",
                "foreign_net_buy": frg_buy - frg_sel,
                "institutional_net_buy": org_buy - org_sel,
                "retail_net_buy": ind_buy - ind_sel,
            }
        except (ValueError, TypeError) as e:
            logger.warning("[krx_rest] investor_item 파싱 실패: %s", e)
            return None

    def _mock_investor_info(
        self, ticker: str, bgn_de: str, end_de: str
    ) -> list[dict[str, Any]]:
        """Mock 투자자 수급 데이터 생성 (seed 기반 재현 가능)."""
        import hashlib  # noqa: PLC0415

        seed = int(hashlib.md5(f"{ticker}{bgn_de}".encode()).hexdigest()[:8], 16)
        rng_state = seed

        def _next_int(lo: int = -100000, hi: int = 100000) -> int:
            nonlocal rng_state
            rng_state = (rng_state * 1664525 + 1013904223) & 0xFFFFFFFF
            return lo + (rng_state % (hi - lo))

        raw = {
            "ticker": ticker,
            "date": f"{bgn_de[:4]}-{bgn_de[4:6]}-{bgn_de[6:8]}T15:30:00+09:00",
            "foreign_net_buy": float(_next_int()),
            "institutional_net_buy": float(_next_int()),
            "retail_net_buy": float(_next_int()),
        }
        event = self.normalizer.normalize(raw, source="krx_investor_flow")
        return [event]
