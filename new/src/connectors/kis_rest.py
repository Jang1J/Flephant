"""KIS REST 커넥터. 주문/잔고/분봉 조회.

모드별 동작:
  mock    : 로컬 fake OHLCV/잔고 생성. 실 API 호출 없음.
  virtual : KIS 모의투자 REST. 주문/잔고/체결/시세 read/write 연결.
  real    : KIS 실계좌 REST. 조회 가능, 주문은 이중 live guard 필요.

불변 원칙 준수:
  - 인증: AuthManager 경유 (.env 직접 접근 금지)
  - Rate: RateLimiter("kis_rest") 경유
  - 하드코딩 금지: magic number 없음
  - 종목코드: pad_ticker() 경유 6자리 zero-padded
"""
from __future__ import annotations

import json
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo

from src.connectors.base import BaseConnector
from src.utils.auth import AuthManager
from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger
from src.utils.pit_guard import PITViolationError, is_pit_safe
from src.utils.rate_limiter import RateLimiter
from src.utils.safe_cast import safe_bool
from src.utils.ticker_utils import is_valid_ticker, pad_ticker

logger = get_logger("kis_rest")

_KST = ZoneInfo("Asia/Seoul")
_KIS_PRICE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-price"
_KIS_INTRADAY_MINUTE_PATH = (
    "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
)
_KIS_DAILY_MINUTE_PATH = (
    "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
)
_KIS_INVESTOR_PATH = "/uapi/domestic-stock/v1/quotations/inquire-investor"
_KIS_INVESTOR_DAILY_PATH = (
    "/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily"
)
_KIS_ORDER_CASH_PATH = "/uapi/domestic-stock/v1/trading/order-cash"
_KIS_BALANCE_PATH = "/uapi/domestic-stock/v1/trading/inquire-balance"
_KIS_DAILY_CCLD_PATH = "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
_KIS_TR_PRICE = "FHKST01010100"
_KIS_TR_INTRADAY_MINUTE = "FHKST03010200"
_KIS_TR_DAILY_MINUTE = "FHKST03010230"
_KIS_TR_INVESTOR = "FHKST01010900"
_KIS_TR_INVESTOR_DAILY = "FHPTJ04160001"
_KIS_TR_ORDER = {
    "real": {"buy": "TTTC0012U", "sell": "TTTC0011U"},
    "virtual": {"buy": "VTTC0012U", "sell": "VTTC0011U"},
}
_KIS_TR_BALANCE = {"real": "TTTC8434R", "virtual": "VTTC8434R"}
_KIS_TR_DAILY_CCLD = {"real": "TTTC0081R", "virtual": "VTTC0081R"}
_KIS_NON_RETRYABLE_MSG_CODES = {
    "OPSQ2000",  # invalid account binding / account check failure
    "40580000",  # paper trading market closed
}


class KISAPIError(Exception):
    """KIS API 오류."""

    def __init__(self, message: str, retry_after_sec: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_sec = retry_after_sec


class KISRestClient(BaseConnector):
    """KIS 한국투자증권 REST. 모의투자 / 실계좌 / Mock 3 모드.

    Mock 모드:
        KIS_MODE=mock 환경변수로 활성화. 실 API 호출 없이 fake OHLCV 생성.
        재현성을 위해 seed 사용 (KIS_MOCK_SEED 환경변수, 기본 42).

    Virtual/real 모드:
        AuthManager가 모드별 KIS_* env를 선택하고, 국내주식 공식 REST
        TR ID/payload 형식으로 시세/잔고/주문/체결조회 엔드포인트를 호출한다.
    """

    def __init__(
        self,
        auth: AuthManager | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        super().__init__()  # BaseConnector: timeout_sec / max_retries / backoff_base 로드
        # 언더스코어 별칭 유지 (내부 코드 하위 호환)
        self._timeout_sec = self.timeout_sec
        self._max_retries = self.max_retries
        self._backoff_base = self.backoff_base
        circuit_cfg = (config_load("risk_config.yaml", "kis_rest") or {}).get(
            "circuit_breaker",
            {},
        )
        self._circuit_enabled = safe_bool(circuit_cfg.get("enabled", True), default=True)
        self._circuit_failure_threshold = max(
            1,
            int(circuit_cfg.get("failure_threshold", 5)),
        )
        self._circuit_open_duration_sec = float(circuit_cfg.get("open_duration_sec", 60))
        self._circuit_consecutive_failures = 0
        self._circuit_opened_at: float | None = None

        self.auth = auth or AuthManager()
        self.rate_limiter = rate_limiter or RateLimiter("kis_rest")
        self.mode = self.auth.get_mode()

        # connector_mock 파라미터 로드 (불변 원칙 5: 하드코딩 금지)
        mock_cfg = config_load("risk_config.yaml", "connector_mock")
        kis_mock = mock_cfg.get("kis", {})
        self._base_price: int = int(kis_mock.get("base_price", 50000))
        self._price_modulo: int = int(kis_mock.get("price_modulo", 100000))
        self._mock_volume_min: int = int(kis_mock.get("volume_min", 1000))
        self._mock_volume_max: int = int(kis_mock.get("volume_max", 100000))
        self._mock_change_min: int = int(kis_mock.get("change_min", 0))
        self._mock_change_max: int = int(kis_mock.get("change_max", 100))
        self._mock_bid_size_min: int = int(kis_mock.get("bid_size_min", 500))
        self._mock_bid_size_max: int = int(kis_mock.get("bid_size_max", 10000))

        if self.mode == "mock":
            seed_str = os.getenv("KIS_MOCK_SEED", "42")
            self._rng = random.Random(int(seed_str))
            logger.info("Mock 모드 활성. seed=%s", seed_str)

    def inquire_price(self, ticker: str) -> dict[str, Any]:
        """현재가 조회. Mock 모드면 fake 가격, virtual/real은 KIS REST 호출.

        Returns:
            {"ticker": "005930", "current_price": int, "volume": int,
             "ts_close": ISO 8601 KST string}
        """
        ticker = pad_ticker(ticker)

        if self.mode == "mock":
            self.rate_limiter.wait_and_acquire()
            return self._mock_inquire_price(ticker)

        body = self._call_kis_get(
            path=_KIS_PRICE_PATH,
            tr_id=_KIS_TR_PRICE,
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": ticker,
            },
        )
        output = self._require_output_dict(body, "output")
        now_str = datetime.now(_KST).isoformat()
        current_price = self._to_int(output.get("stck_prpr"), "stck_prpr")
        volume = self._to_int(output.get("acml_vol"), "acml_vol", default=0)
        turnover = self._to_float(output.get("acml_tr_pbmn"), default=0.0)
        return {
            "ticker": ticker,
            "current_price": current_price,
            "volume": volume,
            "ts_close": now_str,
            "ingest_ts": now_str,
            "completeness": "full",
            "day_change_pct": self._kis_percent_to_decimal(output.get("prdy_ctrt")),
            "turnover": turnover,
            "_mode": self.mode,
        }

    def get_price_snapshot(self, tickers: list[str]) -> list[dict[str, Any]]:
        """KOSPI200 watch universe N종목 일괄 현재가 조회 (S5-1, C16).

        KIS REST는 단건 inquire_price 만 공식 제공. bulk endpoint 부재 시 N회 sequential 호출.
        rate_limits.kis_rest = 20/sec, burst 50. KOSPI200 200종목 = 평균 3.3 req/s 사용 (안전).

        Args:
            tickers: 종목코드 리스트 (6자리 zero-padded 자동 적용).

        Returns:
            list of dict, each:
                {ticker, ts (ISO8601 KST), last_price, day_change_pct, volume, turnover}
        """
        if self.mode == "mock":
            return self._mock_get_price_snapshot(tickers)

        results: list[dict[str, Any]] = []
        for ticker in tickers:
            price = self.inquire_price(ticker)
            results.append({
                "ticker": price["ticker"],
                "ts": price["ts_close"],
                "last_price": price["current_price"],
                "day_change_pct": price.get("day_change_pct", 0.0),
                "volume": price.get("volume", 0),
                "turnover": price.get("turnover", 0.0),
                "_mode": self.mode,
            })
        return results

    def _mock_get_price_snapshot(self, tickers: list[str]) -> list[dict[str, Any]]:
        """mock 모드: ticker별 fake 현재가 스냅샷 생성.

        200종목 × {last_price=10000~50000 random, day_change_pct=±2% random,
        volume=random, turnover=last_price*volume}.
        """
        results: list[dict[str, Any]] = []
        now_str = datetime.now(_KST).isoformat()
        for ticker in tickers:
            padded = pad_ticker(ticker)
            base_price = self._base_price + (int(padded) % self._price_modulo)
            last_price = base_price + self._rng.randint(-500, 500)
            # ±2% 범위 변동률
            day_change_pct = round(self._rng.uniform(-0.02, 0.02), 6)
            volume = self._rng.randint(self._mock_volume_min, self._mock_volume_max)
            turnover = float(last_price * volume)
            results.append({
                "ticker": padded,
                "ts": now_str,
                "last_price": last_price,
                "day_change_pct": day_change_pct,
                "volume": volume,
                "turnover": turnover,
                "_mode": "mock",
            })
        return results

    def inquire_minute_bar(
        self, ticker: str, n_bars: int = 60, date: str | None = None
    ) -> list[dict[str, Any]]:
        """최근 n분봉 OHLCV 조회.

        Mock 모드: n_bars 개수만큼 fake OHLCV 생성. 가격은 random walk.
        virtual/real 모드: KIS REST 분봉 endpoint 호출.

        Args:
            ticker: 종목 코드 (6자리 zero-padded 자동 적용).
            n_bars: 조회할 분봉 수 (기본 60).
            date: 기준 날짜 YYYYMMDD. None이면 당일. 실 API 경로에서 PIT-Safety guard 적용.

        Returns:
            list of OHLCV dict. ts_close 오름차순.
        """
        ticker = pad_ticker(ticker)

        if self.mode == "mock":
            self.rate_limiter.wait_and_acquire()
            return self._mock_inquire_minute_bar(ticker, n_bars)

        # PIT-Safety guard: date 기반 과거/당일 분봉은 장마감 시각 기준으로
        # 판단한다. 당일 date query는 18:00 KST snapshot 이후에만 허용한다.
        if date:
            self._assert_minute_date_pit_safe(date)

        return self._inquire_kis_minute_bar(ticker, n_bars=n_bars, date=date)

    @staticmethod
    def _minute_date_market_close_ts(date: str) -> datetime:
        close_str = str(config_load("risk_config.yaml", "market_hours")["close"])
        close_time = datetime.strptime(close_str, "%H:%M:%S").time()
        target_day = datetime.strptime(date, "%Y%m%d").date()
        return datetime.combine(target_day, close_time, tzinfo=_KST)

    @classmethod
    def _assert_minute_date_pit_safe(cls, date: str) -> None:
        target_day = datetime.strptime(date, "%Y%m%d").date()
        now = datetime.now(_KST)
        today = now.date()
        if target_day > today:
            raise PITViolationError(f"[kis_rest] 미래 날짜 요청 차단: date={date}")
        if target_day == today:
            pit_cfg = config_load("risk_config.yaml", "pit_safety")
            snapshot_hour = int(pit_cfg["snapshot_hour"])
            snapshot_dt = datetime.combine(
                today,
                datetime.strptime(f"{snapshot_hour:02d}:00:00", "%H:%M:%S").time(),
                tzinfo=_KST,
            )
            if now < snapshot_dt:
                raise PITViolationError(
                    f"[kis_rest] 당일 분봉 date 조회는 snapshot 이후만 허용: "
                    f"date={date} now={now.isoformat()} snapshot={snapshot_dt.isoformat()}"
                )
        market_close_ts = cls._minute_date_market_close_ts(date)
        if not is_pit_safe(market_close_ts):
            raise PITViolationError(f"[kis_rest] 미래 날짜 요청 차단: date={date}")

    def inquire_investor(self, ticker: str) -> dict[str, Any] | None:
        """종목별 현재 투자자 수급 조회.

        KIS 공식 ``inquire-investor`` 엔드포인트를 사용한다. 당일 수급은
        장 종료 후 제공되므로, 운영 배치/백필은 ``investor_trade_by_stock_daily``
        를 우선 사용한다.
        """
        ticker = pad_ticker(ticker)

        if self.mode == "mock":
            self.rate_limiter.wait_and_acquire()
            return self._mock_investor_row(ticker, datetime.now(_KST).strftime("%Y%m%d"))

        body = self._call_kis_get(
            path=_KIS_INVESTOR_PATH,
            tr_id=_KIS_TR_INVESTOR,
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": ticker,
            },
        )
        rows = self._extract_kis_rows(body.get("output"))
        normalized = self._normalize_investor_rows(
            ticker=ticker,
            rows=rows,
            fallback_date=datetime.now(_KST).strftime("%Y%m%d"),
            provider="kis_inquire_investor",
        )
        return normalized[0] if normalized else None

    def investor_trade_by_stock_daily(
        self,
        ticker: str,
        date: str,
    ) -> list[dict[str, Any]]:
        """종목별 투자자매매동향(일별) 조회.

        Returns:
            EventNormalizer ``krx_investor_flow`` 입력과 호환되는 raw dict 리스트.
            ``foreign_net_buy`` / ``institutional_net_buy`` / ``retail_net_buy`` 는
            KIS 거래대금 필드(``*_ntby_tr_pbmn``)를 우선 사용한다.
        """
        ticker = pad_ticker(ticker)

        # PIT-Safety guard: 일별 수급은 "하루 전체" 집계이므로
        # - 미래 날짜 차단
        # - 당일(date==today)은 snapshot(기본 18:00 KST) 이후에만 허용
        # - PIT 비교는 23:59가 아니라 market close timestamp 기준으로 수행
        target_day = datetime.strptime(date, "%Y%m%d").date()
        now = datetime.now(_KST)
        today = now.date()
        if target_day > today:
            raise PITViolationError(f"[kis_rest] 미래 날짜 수급 요청 차단: date={date}")
        if target_day == today:
            pit_cfg = config_load("risk_config.yaml", "pit_safety")
            snapshot_hour = int(pit_cfg["snapshot_hour"])
            snapshot_dt = datetime.combine(
                today,
                datetime.strptime(f"{snapshot_hour:02d}:00:00", "%H:%M:%S").time(),
                tzinfo=_KST,
            )
            if now < snapshot_dt:
                raise PITViolationError(
                    f"[kis_rest] 당일 수급(date) 조회는 snapshot 이후만 허용: "
                    f"date={date} now={now.isoformat()} snapshot={snapshot_dt.isoformat()}"
                )

        market_close_ts = self._minute_date_market_close_ts(date)
        if not is_pit_safe(market_close_ts):
            raise PITViolationError(f"[kis_rest] 미래 날짜 수급 요청 차단: date={date}")

        if self.mode == "mock":
            self.rate_limiter.wait_and_acquire()
            return [self._mock_investor_row(ticker, date)]

        body = self._call_kis_get(
            path=_KIS_INVESTOR_DAILY_PATH,
            tr_id=_KIS_TR_INVESTOR_DAILY,
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": ticker,
                "FID_INPUT_DATE_1": date,
                "FID_ORG_ADJ_PRC": "",
                "FID_ETC_CLS_CODE": "",
            },
        )
        rows = self._extract_kis_rows(body.get("output2"))
        if not rows:
            rows = self._extract_kis_rows(body.get("output1"))

        return self._normalize_investor_rows(
            ticker=ticker,
            rows=rows,
            fallback_date=date,
            provider="kis_investor_trade_by_stock_daily",
        )

    def get_balance(self) -> dict[str, Any]:
        """현재 잔고/포지션 조회. KIS inquire-balance 정규화."""
        if self.mode == "mock":
            return {
                "balance": {"cash": 0.0, "total_eval": 0.0, "net_asset": 0.0},
                "positions": [],
                "summary": {},
                "_mode": "mock",
            }
        account_no, product_code = self._kis_account_parts()
        tr_id = self._tr_id_for_mode(_KIS_TR_BALANCE)
        body = self._call_kis_get(
            path=_KIS_BALANCE_PATH,
            tr_id=tr_id,
            params={
                "CANO": account_no,
                "ACNT_PRDT_CD": product_code,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "01",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        output1 = self._extract_kis_rows(body.get("output1"))
        output2 = self._require_output_dict(body, "output2")
        positions: list[dict[str, Any]] = []
        for row in output1:
            position = self._normalize_balance_row(row)
            if position and position.get("ticker") != "000000":
                positions.append(position)
        balance = {
            "cash": self._to_float(output2.get("dnca_tot_amt"), default=0.0),
            "total_eval": self._to_float(output2.get("tot_evlu_amt"), default=0.0),
            "net_asset": self._to_float(output2.get("nass_amt"), default=0.0),
        }
        return {
            "balance": balance,
            "positions": positions,
            "summary": output2,
            "_mode": self.mode,
        }

    def get_order_history(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        ticker: str = "",
        order_id: str = "",
        side: str = "all",
        execution_filter: str = "all",
    ) -> dict[str, Any]:
        """국내주식 일별 주문체결조회.

        S4-6 paper trading reconciliation에서 주문 제출 이후 broker 체결 상태를
        조회하기 위한 read-only API다.
        """
        if self.mode == "mock":
            return {"orders": [], "summary": {}, "_mode": "mock"}

        today = datetime.now(_KST).strftime("%Y%m%d")
        start = self._normalize_yyyymmdd(start_date or today, "start_date")
        end = self._normalize_yyyymmdd(end_date or start, "end_date")
        ticker_padded = pad_ticker(ticker) if str(ticker).strip() else ""
        side_code = self._order_history_side_code(side)
        ccld_code = self._order_history_execution_code(execution_filter)

        account_no, product_code = self._kis_account_parts()
        tr_id = self._tr_id_for_mode(_KIS_TR_DAILY_CCLD)
        body = self._call_kis_get(
            path=_KIS_DAILY_CCLD_PATH,
            tr_id=tr_id,
            params={
                "CANO": account_no,
                "ACNT_PRDT_CD": product_code,
                "INQR_STRT_DT": start,
                "INQR_END_DT": end,
                "SLL_BUY_DVSN_CD": side_code,
                "INQR_DVSN": "00",
                "PDNO": ticker_padded,
                "CCLD_DVSN": ccld_code,
                "ORD_GNO_BRNO": "",
                "ODNO": str(order_id or ""),
                "INQR_DVSN_3": "00",
                "INQR_DVSN_1": "",
                "EXCG_ID_DVSN_CD": "KRX",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        rows = self._extract_kis_rows(body.get("output1"))
        summary = body.get("output2") if isinstance(body.get("output2"), dict) else {}
        return {
            "orders": [self._normalize_order_history_row(row) for row in rows],
            "summary": summary,
            "date_range": {"start_date": start, "end_date": end},
            "_mode": self.mode,
        }

    def submit_order(
        self,
        ticker: str,
        side: str,
        qty: int,
        price: float | int | None = None,
        order_type: str = "00",
    ) -> dict[str, Any]:
        """KIS 현금주문 제출.

        기본은 지정가(`order_type=00`)만 허용한다. 시장가(`01`)는 호출자가 명시해야 하며
        주문단가는 0으로 전송한다.
        """
        ticker = pad_ticker(ticker)
        if ticker == "000000" or not is_valid_ticker(ticker):
            raise ValueError("ticker must be a valid 6-digit KRX code")
        if self.mode == "mock":
            return {"ticker": ticker, "side": side, "qty": qty,
                    "status": "mock_accepted", "_mode": "mock"}
        self._assert_order_mode_allowed()
        side_norm = str(side).lower()
        if side_norm not in {"buy", "sell"}:
            raise ValueError("side must be 'buy' or 'sell'")
        qty_int = int(qty)
        if qty_int <= 0:
            raise ValueError("qty must be positive")
        order_type_norm = str(order_type or "00")
        if order_type_norm == "01":
            order_price = 0
        else:
            if price is None:
                raise ValueError("price is required for non-market KIS order")
            order_price = int(round(float(price)))
            if order_price <= 0:
                raise ValueError("price must be positive for non-market KIS order")

        account_no, product_code = self._kis_account_parts()
        tr_id = self._tr_id_for_mode(_KIS_TR_ORDER)[side_norm]
        payload = {
            "CANO": account_no,
            "ACNT_PRDT_CD": product_code,
            "PDNO": ticker,
            "ORD_DVSN": order_type_norm,
            "ORD_QTY": str(qty_int),
            "ORD_UNPR": str(order_price),
            "EXCG_ID_DVSN_CD": "KRX",
            "SLL_TYPE": "01" if side_norm == "sell" else "",
            "CNDT_PRIC": "",
        }
        body = self._call_kis_post(
            path=_KIS_ORDER_CASH_PATH,
            tr_id=tr_id,
            payload=payload,
        )
        output = self._require_output_dict(body, "output")
        return {
            "ticker": ticker,
            "side": side_norm,
            "qty": qty_int,
            "price": float(order_price),
            "order_type": order_type_norm,
            "status": "submitted",
            "order_id": output.get("ODNO") or output.get("odno"),
            "order_time": output.get("ORD_TMD") or output.get("ord_tmd"),
            "raw_output": output,
            "tr_id": tr_id,
            "_mode": self.mode,
        }

    # --------------------------------------------------------------------- #
    # Mock 데이터 생성
    # --------------------------------------------------------------------- #

    def _mock_inquire_price(self, ticker: str) -> dict[str, Any]:
        base_price = self._base_price + (int(ticker) % self._price_modulo)
        delta = self._rng.randint(-500, 500)
        now_str = datetime.now(_KST).isoformat()
        return {
            "ticker": ticker,
            "current_price": base_price + delta,
            "volume": self._rng.randint(self._mock_volume_min, self._mock_volume_max),
            "ts_close": now_str,
            "ingest_ts": now_str,
            "completeness": "full",
            "_mode": "mock",
        }

    def _mock_inquire_minute_bar(
        self, ticker: str, n_bars: int
    ) -> list[dict[str, Any]]:
        base = self._base_price + (int(ticker) % self._price_modulo)
        now = datetime.now(_KST).replace(second=0, microsecond=0)
        bars: list[dict[str, Any]] = []
        price = base
        prev_close = base
        for i in range(n_bars):
            ts = now - timedelta(minutes=n_bars - 1 - i)
            open_p = price
            close_p = price + self._rng.randint(-200, 200)
            high_p = max(open_p, close_p) + self._rng.randint(
                self._mock_change_min, self._mock_change_max
            )
            low_p = min(open_p, close_p) - self._rng.randint(
                self._mock_change_min, self._mock_change_max
            )
            volume = self._rng.randint(self._mock_bid_size_min, self._mock_bid_size_max)
            # C1 required_features: vwap / turnover / change / ingest_ts / completeness
            vwap = (open_p + high_p + low_p + close_p) / 4.0
            turnover = float(vwap * volume)
            change = float(close_p - prev_close)
            ingest_ts = now.isoformat()
            bars.append({
                "ticker": ticker,
                "open": open_p,
                "high": high_p,
                "low": low_p,
                "close": close_p,
                "volume": volume,
                "vwap": vwap,
                "turnover": turnover,
                "change": change,
                "ts_close": ts.isoformat(),
                "ingest_ts": ingest_ts,
                "completeness": "full",
                "_mode": "mock",
            })
            price = close_p
            prev_close = close_p
        return bars

    # --------------------------------------------------------------------- #
    # KIS REST helpers
    # --------------------------------------------------------------------- #

    def _call_kis_get(
        self,
        path: str,
        tr_id: str,
        params: dict[str, str],
    ) -> dict[str, Any]:
        """KIS GET 호출. 토큰/키는 AuthManager 경유, 실패 시 지수 백오프 재시도."""
        url = f"{self.auth.get_kis_base_url()}{path}"
        headers = self._kis_headers(tr_id)
        log_params = {
            key: ("***" if key in {"CANO", "ACNT_PRDT_CD"} else value)
            for key, value in params.items()
        }
        self._check_circuit()

        last_err: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                self.rate_limiter.wait_and_acquire()
                self._request_count += 1
                logger.info(
                    "[kis_rest] API 호출 시작. path=%s tr_id=%s params=%s attempt=%d",
                    path,
                    tr_id,
                    log_params,
                    attempt + 1,
                )
                body = self._http_get_json(url, params, headers)
                rt_cd = str(body.get("rt_cd", "0"))
                if rt_cd not in ("0", ""):
                    msg_cd = str(body.get("msg_cd", "UNKNOWN"))
                    msg = str(body.get("msg1", body.get("msg", "")))
                    raise KISAPIError(
                        f"[kis_rest] KIS API 오류 path={path} msg_cd={msg_cd} msg={msg}",
                        retry_after_sec=self._float_or_none(body.get("_retry_after_sec")),
                    )
                self._record_call_success()
                return body
            except Exception as e:
                self._error_count += 1
                last_err = e
                safe_error = self._sanitize_error_text(str(e))
                if self._is_non_retryable_kis_error(e):
                    raise KISAPIError(safe_error) from e
                if attempt < self._max_retries - 1:
                    wait_sec = self._retry_wait_seconds(e, attempt)
                    logger.warning(
                        "[kis_rest] API 재시도 %d/%d. wait=%.2fs path=%s error=%s",
                        attempt + 1,
                        self._max_retries,
                        wait_sec,
                        path,
                        safe_error,
                    )
                    time.sleep(wait_sec)

        self._record_call_failure(last_err)
        raise ConnectionError(
            f"[kis_rest] KIS API {path} {self._max_retries}회 재시도 실패. "
            f"last_err={self._sanitize_error_text(str(last_err))}"
        )

    def _call_kis_post(
        self,
        path: str,
        tr_id: str,
        payload: dict[str, str],
    ) -> dict[str, Any]:
        """KIS POST 호출. 주문 body는 공식 샘플처럼 대문자 key 유지."""
        url = f"{self.auth.get_kis_base_url()}{path}"
        headers = self._kis_headers(tr_id)
        safe_payload = {
            key: ("***" if key in {"CANO", "ACNT_PRDT_CD"} else value)
            for key, value in payload.items()
        }
        self._check_circuit()

        last_err: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                self.rate_limiter.wait_and_acquire()
                self._request_count += 1
                logger.info(
                    "[kis_rest] API POST 시작. path=%s tr_id=%s payload=%s attempt=%d",
                    path,
                    tr_id,
                    safe_payload,
                    attempt + 1,
                )
                body = self._http_post_json(url, payload, headers)
                rt_cd = str(body.get("rt_cd", "0"))
                if rt_cd not in ("0", ""):
                    msg_cd = str(body.get("msg_cd", "UNKNOWN"))
                    msg = str(body.get("msg1", body.get("msg", "")))
                    raise KISAPIError(
                        f"[kis_rest] KIS API 오류 path={path} msg_cd={msg_cd} msg={msg}",
                        retry_after_sec=self._float_or_none(body.get("_retry_after_sec")),
                    )
                self._record_call_success()
                return body
            except Exception as e:
                self._error_count += 1
                last_err = e
                safe_error = self._sanitize_error_text(str(e))
                if self._is_non_retryable_kis_error(e):
                    raise KISAPIError(safe_error) from e
                if attempt < self._max_retries - 1:
                    wait_sec = self._retry_wait_seconds(e, attempt)
                    logger.warning(
                        "[kis_rest] API POST 재시도 %d/%d. wait=%.2fs path=%s error=%s",
                        attempt + 1,
                        self._max_retries,
                        wait_sec,
                        path,
                        safe_error,
                    )
                    time.sleep(wait_sec)

        self._record_call_failure(last_err)
        raise ConnectionError(
            f"[kis_rest] KIS API POST {path} {self._max_retries}회 재시도 실패. "
            f"last_err={self._sanitize_error_text(str(last_err))}"
        )

    def _http_post_json(
        self,
        url: str,
        payload: dict[str, str],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        try:
            import requests  # noqa: PLC0415

            resp = requests.post(
                url,
                headers=headers,
                data=json.dumps(payload),
                timeout=self.timeout_sec,
            )
            if resp.status_code >= 400:
                response_headers = getattr(resp, "headers", {}) or {}
                retry_after_sec = self._parse_retry_after(response_headers.get("Retry-After"))
                try:
                    body = resp.json()
                except ValueError:
                    body = None
                if isinstance(body, dict) and body.get("rt_cd") not in ("0", ""):
                    if retry_after_sec is not None:
                        body["_retry_after_sec"] = retry_after_sec
                    return body
                if resp.status_code == 429:
                    raise KISAPIError(
                        "[kis_rest] HTTP 429 Too Many Requests",
                        retry_after_sec=retry_after_sec,
                    )
            resp.raise_for_status()
            return resp.json()
        except ImportError:
            import urllib.error  # noqa: PLC0415
            import urllib.request  # noqa: PLC0415

            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                    body = resp.read().decode("utf-8")
                    return json.loads(body)
            except TimeoutError:
                raise
            except urllib.error.URLError as e:
                raise ConnectionError(f"HTTP urllib POST 연결 실패: {e}") from e

    def _http_get_json(
        self,
        url: str,
        params: dict[str, str],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """KIS GET JSON.

        일부 KIS 가상투자 엔드포인트는 HTTP 500이어도 JSON body에
        ``rt_cd/msg_cd/msg1``를 담아 반환한다. 이 body를 보존해야 상위
        재시도 정책이 계좌오류/장종료 같은 비재시도 오류를 정확히 분류한다.
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
                if resp.status_code >= 400:
                    response_headers = getattr(resp, "headers", {}) or {}
                    retry_after_sec = self._parse_retry_after(response_headers.get("Retry-After"))
                    try:
                        body = resp.json()
                    except ValueError:
                        body = None
                    if isinstance(body, dict) and body.get("rt_cd") not in ("0", ""):
                        if retry_after_sec is not None:
                            body["_retry_after_sec"] = retry_after_sec
                        return body
                    if resp.status_code == 429:
                        raise KISAPIError(
                            "[kis_rest] HTTP 429 Too Many Requests",
                            retry_after_sec=retry_after_sec,
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
            return super()._http_get_json(url, params, headers)

    def _kis_headers(self, tr_id: str) -> dict[str, str]:
        token = self.auth.get_kis_token()
        app_key, app_secret = self.auth.get_kis_app_credentials()
        return {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "text/plain",
            "charset": "UTF-8",
            "authorization": f"Bearer {token}",
            "appkey": app_key,
            "appsecret": app_secret,
            "tr_id": tr_id,
            "tr_cont": "",
            "custtype": "P",
        }

    def _kis_account_parts(self) -> tuple[str, str]:
        try:
            return self.auth.get_kis_account_parts()
        except EnvironmentError as e:
            raise KISAPIError(str(e)) from e

    @staticmethod
    def _sanitize_error_text(text: str) -> str:
        """requests 예외 URL에 섞인 계좌 파라미터를 로그/리포트에서 제거."""
        out = str(text)
        for key in ("CANO", "ACNT_PRDT_CD"):
            out = re.sub(rf"({key}=)[^&\\s]+", r"\1***", out)
        return out

    def _retry_wait_seconds(self, error: Exception, attempt: int) -> float:
        base_wait = float(self._backoff_base ** attempt)
        retry_after = getattr(error, "retry_after_sec", None)
        if retry_after is None:
            return base_wait
        try:
            return max(base_wait, float(retry_after))
        except (TypeError, ValueError):
            return base_wait

    @staticmethod
    def _parse_retry_after(value: Any) -> float | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return max(0.0, float(text))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(text)
            except (TypeError, ValueError, IndexError):
                return None
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _check_circuit(self) -> None:
        if not self._circuit_enabled or self._circuit_opened_at is None:
            return
        elapsed = time.monotonic() - self._circuit_opened_at
        remaining = self._circuit_open_duration_sec - elapsed
        if remaining > 0:
            raise KISAPIError(
                "[kis_rest] circuit breaker OPEN",
                retry_after_sec=remaining,
            )
        self._circuit_opened_at = None
        self._circuit_consecutive_failures = max(
            0,
            self._circuit_failure_threshold - 1,
        )

    def _record_call_success(self) -> None:
        self._circuit_consecutive_failures = 0
        self._circuit_opened_at = None

    def _record_call_failure(self, error: Exception | None) -> None:
        if not self._circuit_enabled:
            return
        if error is not None and self._is_non_retryable_kis_error(error):
            return
        self._circuit_consecutive_failures += 1
        if self._circuit_consecutive_failures >= self._circuit_failure_threshold:
            self._circuit_opened_at = time.monotonic()
            logger.warning(
                "[kis_rest] circuit breaker OPEN. consecutive_failures=%d threshold=%d",
                self._circuit_consecutive_failures,
                self._circuit_failure_threshold,
            )

    @staticmethod
    def _is_non_retryable_kis_error(error: Exception) -> bool:
        if not isinstance(error, KISAPIError):
            return False
        text = str(error)
        return any(f"msg_cd={code}" in text for code in _KIS_NON_RETRYABLE_MSG_CODES)

    def _assert_order_mode_allowed(self) -> None:
        """실계좌 직접 주문은 env + risk_config 이중 승인 없이는 차단한다."""
        if self.mode != "real":
            return

        env_live = os.getenv("ELEPHANT_LIVE_ENABLED", "").strip().lower() == "true"
        exec_cfg = config_load("risk_config.yaml", "execution") or {}
        config_live = safe_bool(exec_cfg.get("live_enabled", False), default=False)
        if env_live and config_live:
            return
        raise KISAPIError(
            "KIS_MODE=real 주문 차단: ELEPHANT_LIVE_ENABLED=true 및 "
            "risk_config.yaml execution.live_enabled=true가 모두 필요합니다."
        )

    def _tr_id_for_mode(self, table: dict[str, Any]) -> Any:
        if self.mode not in table:
            raise KISAPIError(f"unsupported KIS_MODE for trading API: {self.mode}")
        return table[self.mode]

    @staticmethod
    def _normalize_yyyymmdd(value: str, field_name: str) -> str:
        value_str = str(value or "").strip()
        if len(value_str) != 8 or not value_str.isdigit():
            raise ValueError(f"{field_name} must be YYYYMMDD")
        return value_str

    @staticmethod
    def _order_history_side_code(side: str) -> str:
        side_norm = str(side or "all").lower()
        mapping = {"all": "00", "sell": "01", "buy": "02"}
        if side_norm not in mapping:
            raise ValueError("side must be one of: all, buy, sell")
        return mapping[side_norm]

    @staticmethod
    def _order_history_execution_code(execution_filter: str) -> str:
        filter_norm = str(execution_filter or "all").lower()
        mapping = {"all": "00", "filled": "01", "unfilled": "02"}
        if filter_norm not in mapping:
            raise ValueError("execution_filter must be one of: all, filled, unfilled")
        return mapping[filter_norm]

    def _inquire_kis_minute_bar(
        self,
        ticker: str,
        n_bars: int,
        date: str | None,
    ) -> list[dict[str, Any]]:
        target_date = date or datetime.now(_KST).strftime("%Y%m%d")
        input_hour = self._initial_minute_query_hour(target_date)
        path = _KIS_DAILY_MINUTE_PATH if date else _KIS_INTRADAY_MINUTE_PATH
        tr_id = _KIS_TR_DAILY_MINUTE if date else _KIS_TR_INTRADAY_MINUTE

        seen: set[str] = set()
        collected: list[dict[str, Any]] = []
        max_loops = max(1, min(20, (max(n_bars, 1) + 29) // 30 + 3))

        for _ in range(max_loops):
            params = {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": ticker,
                "FID_INPUT_HOUR_1": input_hour,
                "FID_PW_DATA_INCU_YN": "Y",
            }
            if date:
                params["FID_INPUT_DATE_1"] = target_date
                params["FID_FAKE_TICK_INCU_YN"] = ""
            else:
                params["FID_ETC_CLS_CODE"] = ""

            body = self._call_kis_get(path=path, tr_id=tr_id, params=params)
            output2 = body.get("output2", [])
            if not isinstance(output2, list) or not output2:
                break

            chunk = self._normalize_minute_rows(ticker, output2, target_date)
            new_rows = [row for row in chunk if row["ts_close"] not in seen]
            if not new_rows:
                break

            for row in new_rows:
                seen.add(row["ts_close"])
            collected.extend(new_rows)

            collected.sort(key=lambda row: row["ts_close"])
            if len(collected) >= n_bars:
                break

            earliest = datetime.fromisoformat(
                collected[0]["ts_close"]
            ).astimezone(_KST)
            prev_minute = earliest - timedelta(minutes=1)
            if prev_minute.strftime("%Y%m%d") != target_date:
                break
            input_hour = prev_minute.strftime("%H%M%S")

        collected.sort(key=lambda row: row["ts_close"])
        if len(collected) > n_bars:
            collected = collected[-n_bars:]
        self._recompute_bar_changes(collected)
        return collected

    def _initial_minute_query_hour(self, target_date: str) -> str:
        close_str = str(config_load("risk_config.yaml", "market_hours")["close"])
        close_hhmmss = close_str.replace(":", "")
        today = datetime.now(_KST).strftime("%Y%m%d")
        if target_date == today:
            now = datetime.now(_KST)
            now_hhmmss = now.strftime("%H%M%S")
            return min(now_hhmmss, close_hhmmss)
        return close_hhmmss

    def _normalize_minute_rows(
        self,
        ticker: str,
        rows: list[Any],
        fallback_date: str,
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            ts_close = self._parse_kis_minute_ts(row, fallback_date)
            open_p = self._to_int(row.get("stck_oprc"), "stck_oprc", default=0)
            high_p = self._to_int(row.get("stck_hgpr"), "stck_hgpr", default=0)
            low_p = self._to_int(row.get("stck_lwpr"), "stck_lwpr", default=0)
            close_p = self._to_int(row.get("stck_prpr"), "stck_prpr", default=0)
            volume = self._to_int(row.get("cntg_vol"), "cntg_vol", default=0)
            if close_p <= 0:
                continue
            prices = [p for p in (open_p, high_p, low_p, close_p) if p > 0]
            if not prices:
                continue
            open_p = open_p or close_p
            high_p = high_p or max(prices)
            low_p = low_p or min(prices)
            vwap = float(sum((open_p, high_p, low_p, close_p)) / 4.0)
            normalized.append({
                "ticker": ticker,
                "open": open_p,
                "high": high_p,
                "low": low_p,
                "close": close_p,
                "volume": volume,
                "vwap": vwap,
                "turnover": float(vwap * volume),
                "change": 0.0,
                "ts_close": ts_close.isoformat(),
                "ingest_ts": datetime.now(_KST).isoformat(),
                "completeness": "full",
                "_mode": self.mode,
            })
        normalized.sort(key=lambda row: row["ts_close"])
        return normalized

    @staticmethod
    def _parse_kis_minute_ts(row: dict[str, Any], fallback_date: str) -> datetime:
        raw_date = str(row.get("stck_bsop_date") or fallback_date)
        raw_hour = str(
            row.get("stck_cntg_hour") or row.get("cntg_hour") or "000000"
        )
        raw_hour = raw_hour.zfill(6)[:6]
        return datetime.strptime(raw_date + raw_hour, "%Y%m%d%H%M%S").replace(
            tzinfo=_KST
        )

    @classmethod
    def _normalize_balance_row(cls, row: Any) -> dict[str, Any]:
        if not isinstance(row, dict):
            return {}
        ticker = pad_ticker(str(row.get("pdno") or row.get("PDNO") or ""))
        return {
            "ticker": ticker,
            "name": row.get("prdt_name") or row.get("PRDT_NAME") or "",
            "qty": cls._to_int(row.get("hldg_qty"), "hldg_qty", default=0),
            "available_qty": cls._to_int(
                row.get("ord_psbl_qty"),
                "ord_psbl_qty",
                default=0,
            ),
            "avg_price": cls._to_float(row.get("pchs_avg_pric"), default=0.0),
            "current_price": cls._to_float(row.get("prpr"), default=0.0),
            "eval_amount": cls._to_float(row.get("evlu_amt"), default=0.0),
            "pnl": cls._to_float(row.get("evlu_pfls_amt"), default=0.0),
            "pnl_pct": cls._kis_percent_to_decimal(row.get("evlu_pfls_rt")),
            "raw": row,
        }

    @classmethod
    def _normalize_order_history_row(cls, row: Any) -> dict[str, Any]:
        if not isinstance(row, dict):
            return {}
        ticker_raw = row.get("pdno") or row.get("PDNO") or ""
        ticker = pad_ticker(str(ticker_raw)) if str(ticker_raw).strip() else ""
        side_code = str(
            row.get("sll_buy_dvsn_cd")
            or row.get("SLL_BUY_DVSN_CD")
            or ""
        )
        side_name = str(
            row.get("sll_buy_dvsn_cd_name")
            or row.get("sll_buy_dvsn_name")
            or row.get("SLL_BUY_DVSN_CD_NAME")
            or ""
        )
        order_qty = int(cls._first_number(row, ("ord_qty", "ORD_QTY")) or 0)
        filled_qty = cls._first_number(
            row,
            ("tot_ccld_qty", "ccld_qty", "TOT_CCLD_QTY", "CCLD_QTY"),
        )
        filled_qty_int = int(filled_qty or 0)
        if order_qty > 0 and filled_qty_int >= order_qty:
            status = "filled"
        elif filled_qty_int > 0:
            status = "partial_filled"
        else:
            status = "submitted"

        return {
            "order_id": row.get("odno") or row.get("ODNO") or "",
            "ticker": ticker,
            "name": row.get("prdt_name") or row.get("PRDT_NAME") or "",
            "side": cls._normalize_order_side(side_code, side_name),
            "order_qty": order_qty,
            "order_price": cls._first_number(
                row,
                ("ord_unpr", "ORD_UNPR"),
            ) or 0.0,
            "filled_qty": filled_qty_int,
            "avg_fill_price": cls._first_number(
                row,
                ("avg_prvs", "avg_ccld_pric", "AVG_PRVS", "AVG_CCLD_PRIC"),
            ) or 0.0,
            "filled_amount": cls._first_number(
                row,
                ("tot_ccld_amt", "ccld_amt", "TOT_CCLD_AMT", "CCLD_AMT"),
            ) or 0.0,
            "order_time": row.get("ord_tmd") or row.get("ORD_TMD") or "",
            "status": status,
            "raw": row,
        }

    @staticmethod
    def _normalize_order_side(side_code: str, side_name: str) -> str:
        if side_code == "01" or "매도" in side_name:
            return "sell"
        if side_code == "02" or "매수" in side_name:
            return "buy"
        return ""

    @classmethod
    def _normalize_investor_rows(
        cls,
        ticker: str,
        rows: list[Any],
        fallback_date: str,
        provider: str,
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            date_str = str(row.get("stck_bsop_date") or fallback_date)
            if len(date_str) != 8 or not date_str.isdigit():
                date_str = fallback_date

            foreign_amt = cls._first_number(
                row, ("frgn_ntby_tr_pbmn", "frgn_ntby_pbmn")
            )
            institutional_amt = cls._first_number(
                row, ("orgn_ntby_tr_pbmn", "orgn_ntby_pbmn")
            )
            retail_amt = cls._first_number(
                row, ("prsn_ntby_tr_pbmn", "prsn_ntby_pbmn")
            )

            foreign_qty = cls._first_number(row, ("frgn_ntby_qty",))
            institutional_qty = cls._first_number(row, ("orgn_ntby_qty",))
            retail_qty = cls._first_number(row, ("prsn_ntby_qty",))

            # 일부 응답/계정에서는 거래대금 필드가 비어 있고 수량만 제공된다.
            # C3 raw feature는 net_buy 3종을 반드시 채워야 하므로 수량으로 대체한다.
            if foreign_amt is None:
                foreign_amt = foreign_qty or 0.0
            if institutional_amt is None:
                institutional_amt = institutional_qty or 0.0
            if retail_amt is None:
                retail_amt = retail_qty or 0.0

            normalized.append({
                "ticker": ticker,
                "date": f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}T15:30:00+09:00",
                "foreign_net_buy": float(foreign_amt),
                "institutional_net_buy": float(institutional_amt),
                "retail_net_buy": float(retail_amt),
                "foreign_net_buy_qty": float(foreign_qty or 0.0),
                "institutional_net_buy_qty": float(institutional_qty or 0.0),
                "retail_net_buy_qty": float(retail_qty or 0.0),
                "provider": provider,
            })
        normalized.sort(key=lambda item: item["date"])
        return normalized

    @staticmethod
    def _extract_kis_rows(payload: Any) -> list[Any]:
        if payload is None:
            return []
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            return [payload]
        return []

    @classmethod
    def _first_number(
        cls,
        row: dict[str, Any],
        keys: tuple[str, ...],
    ) -> float | None:
        for key in keys:
            value = row.get(key)
            if value not in (None, ""):
                return cls._to_float(value, default=0.0)
        return None

    def _mock_investor_row(self, ticker: str, date: str) -> dict[str, Any]:
        foreign = float(self._rng.randint(-2_000_000_000, 2_000_000_000))
        institutional = float(self._rng.randint(-2_000_000_000, 2_000_000_000))
        retail = -(foreign + institutional)
        return {
            "ticker": ticker,
            "date": f"{date[:4]}-{date[4:6]}-{date[6:8]}T15:30:00+09:00",
            "foreign_net_buy": foreign,
            "institutional_net_buy": institutional,
            "retail_net_buy": float(retail),
            "foreign_net_buy_qty": 0.0,
            "institutional_net_buy_qty": 0.0,
            "retail_net_buy_qty": 0.0,
            "provider": "mock_kis_investor",
        }

    @staticmethod
    def _recompute_bar_changes(bars: list[dict[str, Any]]) -> None:
        prev_close: int | None = None
        for bar in bars:
            close_p = int(bar["close"])
            bar["change"] = 0.0 if prev_close is None else float(close_p - prev_close)
            prev_close = close_p

    @staticmethod
    def _require_output_dict(body: dict[str, Any], key: str) -> dict[str, Any]:
        output = body.get(key)
        if isinstance(output, dict):
            return output
        if isinstance(output, list):
            for row in output:
                if isinstance(row, dict):
                    return row
        raise KISAPIError(f"[kis_rest] 응답 {key} 누락 또는 dict/list[dict] 아님")

    @staticmethod
    def _to_int(value: Any, field: str, default: int | None = None) -> int:
        if value is None or value == "":
            if default is not None:
                return default
            raise KISAPIError(f"[kis_rest] 정수 필드 누락: {field}")
        try:
            return int(float(str(value).replace(",", "").strip()))
        except ValueError as e:
            raise KISAPIError(f"[kis_rest] 정수 필드 파싱 실패: {field}={value!r}") from e

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        if value is None or value == "":
            return default
        try:
            return float(str(value).replace(",", "").strip())
        except ValueError:
            return default

    @classmethod
    def _kis_percent_to_decimal(cls, value: Any) -> float:
        pct = cls._to_float(value, default=0.0)
        return pct / 100.0
