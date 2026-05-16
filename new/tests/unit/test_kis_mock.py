"""KIS Mock 모드 unit tests. Sprint 0 S0-2 완료 검증."""
from __future__ import annotations

import pytest


def _set_mock_env(monkeypatch):
    monkeypatch.setenv("KIS_MODE", "mock")
    monkeypatch.setenv("KIS_MOCK_SEED", "42")


# ------------------------------------------------------------------ #
# KISRestClient
# ------------------------------------------------------------------ #


def test_kis_rest_mock_inquire_price(monkeypatch):
    _set_mock_env(monkeypatch)
    from src.connectors.kis_rest import KISRestClient
    client = KISRestClient()
    result = client.inquire_price("005930")
    assert result["ticker"] == "005930"
    assert "current_price" in result
    assert isinstance(result["current_price"], int)
    assert result["_mode"] == "mock"


def test_kis_rest_mock_minute_bar(monkeypatch):
    _set_mock_env(monkeypatch)
    from src.connectors.kis_rest import KISRestClient
    client = KISRestClient()
    bars = client.inquire_minute_bar("005930", n_bars=10)
    assert len(bars) == 10
    for bar in bars:
        assert bar["ticker"] == "005930"
        assert bar["_mode"] == "mock"
        assert bar["open"] > 0
        assert bar["high"] >= bar["low"]


def test_kis_rest_virtual_inquire_price_normalizes_response(monkeypatch):
    """virtual 모드는 KIS 현재가 응답을 C1 호환 dict로 정규화한다."""
    monkeypatch.setenv("KIS_MODE", "virtual")
    from src.connectors.kis_rest import KISRestClient

    client = KISRestClient()

    def fake_call(path, tr_id, params):
        assert path.endswith("/inquire-price")
        assert tr_id == "FHKST01010100"
        assert params["FID_INPUT_ISCD"] == "005930"
        return {
            "rt_cd": "0",
            "output": {
                "stck_prpr": "73500",
                "acml_vol": "123456",
                "acml_tr_pbmn": "987654321",
                "prdy_ctrt": "1.25",
            },
        }

    monkeypatch.setattr(client, "_call_kis_get", fake_call)
    result = client.inquire_price("005930")

    assert result["ticker"] == "005930"
    assert result["current_price"] == 73500
    assert result["volume"] == 123456
    assert result["day_change_pct"] == pytest.approx(0.0125)
    assert result["_mode"] == "virtual"


def test_kis_rest_virtual_minute_bar_normalizes_response(monkeypatch):
    """KIS 분봉 응답 output2를 ts_close 오름차순 C1 bar로 정규화한다."""
    monkeypatch.setenv("KIS_MODE", "virtual")
    from src.connectors.kis_rest import KISRestClient

    client = KISRestClient()

    def fake_call(path, tr_id, params):
        assert path.endswith("/inquire-time-dailychartprice")
        assert tr_id == "FHKST03010230"
        assert params["FID_INPUT_DATE_1"] == "20260508"
        return {
            "rt_cd": "0",
            "output2": [
                {
                    "stck_bsop_date": "20260508",
                    "stck_cntg_hour": "090100",
                    "stck_oprc": "70000",
                    "stck_hgpr": "70200",
                    "stck_lwpr": "69900",
                    "stck_prpr": "70100",
                    "cntg_vol": "1000",
                },
                {
                    "stck_bsop_date": "20260508",
                    "stck_cntg_hour": "090000",
                    "stck_oprc": "69900",
                    "stck_hgpr": "70100",
                    "stck_lwpr": "69800",
                    "stck_prpr": "70000",
                    "cntg_vol": "900",
                },
            ],
        }

    monkeypatch.setattr(client, "_call_kis_get", fake_call)
    bars = client.inquire_minute_bar("005930", n_bars=2, date="20260508")

    assert [bar["close"] for bar in bars] == [70000, 70100]
    assert bars[0]["change"] == 0.0
    assert bars[1]["change"] == 100.0
    assert bars[0]["ts_close"].startswith("2026-05-08T09:00:00")
    assert bars[0]["_mode"] == "virtual"


def test_kis_rest_virtual_investor_daily_normalizes_response(monkeypatch):
    """KIS 일별 수급 응답을 C3 investor_flow raw 필드로 정규화한다."""
    monkeypatch.setenv("KIS_MODE", "virtual")
    from src.connectors.kis_rest import KISRestClient

    client = KISRestClient()

    def fake_call(path, tr_id, params):
        assert path.endswith("/investor-trade-by-stock-daily")
        assert tr_id == "FHPTJ04160001"
        assert params["FID_INPUT_ISCD"] == "005930"
        assert params["FID_INPUT_DATE_1"] == "20260508"
        return {
            "rt_cd": "0",
            "output2": [
                {
                    "stck_bsop_date": "20260508",
                    "frgn_ntby_tr_pbmn": "-1500000000",
                    "orgn_ntby_tr_pbmn": "500000000",
                    "prsn_ntby_tr_pbmn": "1000000000",
                    "frgn_ntby_qty": "-10000",
                    "orgn_ntby_qty": "3000",
                    "prsn_ntby_qty": "7000",
                }
            ],
        }

    monkeypatch.setattr(client, "_call_kis_get", fake_call)
    result = client.investor_trade_by_stock_daily("005930", "20260508")

    assert len(result) == 1
    assert result[0]["ticker"] == "005930"
    assert result[0]["date"] == "2026-05-08T15:30:00+09:00"
    assert result[0]["foreign_net_buy"] == pytest.approx(-1_500_000_000.0)
    assert result[0]["institutional_net_buy"] == pytest.approx(500_000_000.0)
    assert result[0]["retail_net_buy"] == pytest.approx(1_000_000_000.0)


def test_kis_rest_virtual_submit_order_uses_cash_order_endpoint(monkeypatch):
    """KIS 모의투자 현금주문은 공식 order-cash payload/TR로 제출한다."""
    monkeypatch.setenv("KIS_MODE", "virtual")
    monkeypatch.setenv("KIS_ACCOUNT_NUMBER", "99999999")
    monkeypatch.setenv("KIS_ACCOUNT_PRODUCT_CODE", "99")
    monkeypatch.setenv("KIS_PAPER_ACCOUNT_NUMBER", "05018716")
    monkeypatch.setenv("KIS_PAPER_ACCOUNT_PRODUCT_CODE", "01")
    from src.connectors.kis_rest import KISRestClient

    client = KISRestClient()

    def fake_post(path, tr_id, payload):
        assert path.endswith("/order-cash")
        assert tr_id == "VTTC0012U"
        assert payload == {
            "CANO": "05018716",
            "ACNT_PRDT_CD": "01",
            "PDNO": "005930",
            "ORD_DVSN": "00",
            "ORD_QTY": "3",
            "ORD_UNPR": "70000",
            "EXCG_ID_DVSN_CD": "KRX",
            "SLL_TYPE": "",
            "CNDT_PRIC": "",
        }
        return {"rt_cd": "0", "output": {"ODNO": "12345", "ORD_TMD": "101010"}}

    monkeypatch.setattr(client, "_call_kis_post", fake_post)

    result = client.submit_order("5930", "buy", 3, price=70000)

    assert result["status"] == "submitted"
    assert result["order_id"] == "12345"
    assert result["tr_id"] == "VTTC0012U"
    assert result["_mode"] == "virtual"


def test_kis_rest_virtual_submit_order_requires_price_for_limit(monkeypatch):
    """기본 지정가 주문은 가격 누락 시 차단해 실수 시장가를 막는다."""
    monkeypatch.setenv("KIS_MODE", "virtual")
    from src.connectors.kis_rest import KISRestClient

    client = KISRestClient()

    with pytest.raises(ValueError, match="price is required"):
        client.submit_order("005930", "buy", 1)


def test_kis_rest_submit_order_rejects_invalid_ticker_before_broker(monkeypatch):
    """KIS connector direct-call도 broker 호출 전 invalid ticker를 차단한다."""
    monkeypatch.setenv("KIS_MODE", "virtual")
    from src.connectors.kis_rest import KISRestClient

    client = KISRestClient()
    monkeypatch.setattr(
        client,
        "_call_kis_post",
        lambda *_args, **_kwargs: pytest.fail("broker should not be called"),
    )

    with pytest.raises(ValueError, match="valid 6-digit"):
        client.submit_order("ABC", "buy", 1, price=70000)


def test_kis_rest_non_retryable_kis_error_short_circuits_post(monkeypatch):
    """장종료/계좌오류처럼 재시도로 해결 안 되는 KIS 오류는 즉시 반환한다."""
    monkeypatch.setenv("KIS_MODE", "virtual")
    from src.connectors.kis_rest import KISAPIError, KISRestClient

    client = KISRestClient()
    client._max_retries = 3
    calls = {"count": 0}

    monkeypatch.setattr(client, "_kis_headers", lambda tr_id: {})

    def fake_post(url, payload, headers):
        calls["count"] += 1
        return {
            "rt_cd": "1",
            "msg_cd": "40580000",
            "msg1": "모의투자 장종료 입니다.",
        }

    monkeypatch.setattr(client, "_http_post_json", fake_post)

    with pytest.raises(KISAPIError, match="msg_cd=40580000"):
        client._call_kis_post(
            path="/uapi/domestic-stock/v1/trading/order-cash",
            tr_id="VTTC0012U",
            payload={"CANO": "05018716", "ACNT_PRDT_CD": "01"},
        )

    assert calls["count"] == 1


def test_kis_rest_real_submit_order_requires_dual_live_enable(monkeypatch):
    """direct connector 실계좌 주문은 env + risk_config 이중 승인 없이는 차단한다."""
    monkeypatch.setenv("KIS_MODE", "real")
    monkeypatch.delenv("ELEPHANT_LIVE_ENABLED", raising=False)
    from src.connectors.kis_rest import KISAPIError, KISRestClient

    client = KISRestClient()

    with pytest.raises(KISAPIError, match="KIS_MODE=real 주문 차단"):
        client.submit_order("005930", "buy", 1, price=70000)


def test_kis_rest_real_submit_order_treats_string_false_config_as_disabled(monkeypatch):
    """risk_config live_enabled='false' 문자열은 실계좌 주문 허용으로 보지 않는다."""
    monkeypatch.setenv("KIS_MODE", "real")
    monkeypatch.setenv("ELEPHANT_LIVE_ENABLED", "true")
    from src.connectors import kis_rest
    from src.connectors.kis_rest import KISAPIError, KISRestClient

    monkeypatch.setattr(
        kis_rest,
        "config_load",
        lambda file_name, section: {"live_enabled": "false"} if section == "execution" else {},
    )
    client = KISRestClient()

    with pytest.raises(KISAPIError, match="KIS_MODE=real 주문 차단"):
        client.submit_order("005930", "buy", 1, price=70000)


def test_kis_rest_virtual_get_balance_normalizes_response(monkeypatch):
    """KIS 잔고 응답 output1/output2를 positions + balance로 정규화한다."""
    monkeypatch.setenv("KIS_MODE", "virtual")
    monkeypatch.setenv("KIS_ACCOUNT_NUMBER", "05018716")
    monkeypatch.setenv("KIS_ACCOUNT_PRODUCT_CODE", "01")
    from src.connectors.kis_rest import KISRestClient

    client = KISRestClient()

    def fake_get(path, tr_id, params):
        assert path.endswith("/inquire-balance")
        assert tr_id == "VTTC8434R"
        assert params["CANO"] == "05018716"
        assert params["ACNT_PRDT_CD"] == "01"
        return {
            "rt_cd": "0",
            "output1": [
                {
                    "pdno": "005930",
                    "prdt_name": "삼성전자",
                    "hldg_qty": "10",
                    "ord_psbl_qty": "8",
                    "pchs_avg_pric": "70000",
                    "prpr": "71000",
                    "evlu_amt": "710000",
                    "evlu_pfls_amt": "10000",
                    "evlu_pfls_rt": "1.43",
                }
            ],
            "output2": [{
                "dnca_tot_amt": "1000000",
                "tot_evlu_amt": "1710000",
                "nass_amt": "1710000",
            }],
        }

    monkeypatch.setattr(client, "_call_kis_get", fake_get)

    result = client.get_balance()

    assert result["balance"]["cash"] == pytest.approx(1_000_000.0)
    assert result["positions"][0]["ticker"] == "005930"
    assert result["positions"][0]["qty"] == 10
    assert result["positions"][0]["pnl_pct"] == pytest.approx(0.0143)


def test_kis_rest_mock_get_balance_shape(monkeypatch):
    """mock 잔고도 virtual/real과 같은 balance dict shape를 유지한다."""
    _set_mock_env(monkeypatch)
    from src.connectors.kis_rest import KISRestClient

    result = KISRestClient().get_balance()

    assert result["balance"] == {
        "cash": 0.0,
        "total_eval": 0.0,
        "net_asset": 0.0,
    }
    assert result["positions"] == []
    assert result["_mode"] == "mock"


def test_kis_rest_get_preserves_kis_json_error_on_http_500(monkeypatch):
    """KIS가 HTTP 500 JSON body로 주는 msg_cd를 상위 retry policy까지 보존한다."""
    monkeypatch.setenv("KIS_MODE", "virtual")
    import requests

    from src.connectors.kis_rest import KISRestClient

    class FakeResponse:
        status_code = 500

        def json(self):
            return {
                "rt_cd": "1",
                "msg_cd": "OPSQ2000",
                "msg1": "ERROR : INPUT INVALID_CHECK_ACNO",
            }

        def raise_for_status(self):
            raise requests.exceptions.HTTPError("500 Server Error")

    monkeypatch.setattr(requests, "get", lambda *_, **__: FakeResponse())

    body = KISRestClient()._http_get_json("https://example.invalid", {}, {})  # noqa: SLF001

    assert body["msg_cd"] == "OPSQ2000"


def test_kis_rest_get_preserves_retry_after_on_kis_json_error(monkeypatch):
    """HTTP 429 JSON body의 Retry-After를 상위 retry wait까지 보존한다."""
    monkeypatch.setenv("KIS_MODE", "virtual")
    import requests

    from src.connectors.kis_rest import KISRestClient

    class FakeResponse:
        status_code = 429
        headers = {"Retry-After": "2.5"}

        def json(self):
            return {
                "rt_cd": "1",
                "msg_cd": "EGW00201",
                "msg1": "초당 거래건수를 초과하였습니다.",
            }

        def raise_for_status(self):
            raise requests.exceptions.HTTPError("429 Too Many Requests")

    monkeypatch.setattr(requests, "get", lambda *_, **__: FakeResponse())

    body = KISRestClient()._http_get_json("https://example.invalid", {}, {})  # noqa: SLF001

    assert body["msg_cd"] == "EGW00201"
    assert body["_retry_after_sec"] == pytest.approx(2.5)


def test_kis_rest_call_get_uses_retry_after_wait(monkeypatch):
    """KIS retry loop는 Retry-After가 있으면 fixed backoff보다 긴 대기를 사용한다."""
    monkeypatch.setenv("KIS_MODE", "virtual")
    from src.connectors.kis_rest import KISRestClient

    client = KISRestClient()
    client._max_retries = 2  # noqa: SLF001
    monkeypatch.setattr(client.auth, "get_kis_base_url", lambda: "https://example.invalid")
    monkeypatch.setattr(client, "_kis_headers", lambda tr_id: {})
    monkeypatch.setattr(client.rate_limiter, "wait_and_acquire", lambda: None)

    calls = {"n": 0}

    def fake_http_get_json(url, params, headers):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "rt_cd": "1",
                "msg_cd": "EGW00201",
                "msg1": "초당 거래건수를 초과하였습니다.",
                "_retry_after_sec": 2.5,
            }
        return {"rt_cd": "0", "output": {"ok": True}}

    sleeps: list[float] = []
    monkeypatch.setattr(client, "_http_get_json", fake_http_get_json)
    monkeypatch.setattr("src.connectors.kis_rest.time.sleep", lambda sec: sleeps.append(sec))

    body = client._call_kis_get("/uapi/test", "TRTEST", {})  # noqa: SLF001

    assert body["rt_cd"] == "0"
    assert calls["n"] == 2
    assert sleeps == [pytest.approx(2.5)]


def test_kis_rest_get_opens_circuit_after_retry_exhaustion(monkeypatch):
    """retry 소진 call이 연속 실패하면 다음 KIS call은 broker 호출 전 차단된다."""
    monkeypatch.setenv("KIS_MODE", "virtual")
    from src.connectors.kis_rest import KISAPIError, KISRestClient

    client = KISRestClient()
    client._max_retries = 1  # noqa: SLF001
    client._circuit_enabled = True  # noqa: SLF001
    client._circuit_failure_threshold = 2  # noqa: SLF001
    client._circuit_open_duration_sec = 60.0  # noqa: SLF001
    monkeypatch.setattr(client.auth, "get_kis_base_url", lambda: "https://example.invalid")
    monkeypatch.setattr(client, "_kis_headers", lambda tr_id: {})
    monkeypatch.setattr(client.rate_limiter, "wait_and_acquire", lambda: None)

    calls = {"n": 0}

    def always_timeout(url, params, headers):
        calls["n"] += 1
        raise TimeoutError("socket timeout")

    monkeypatch.setattr(client, "_http_get_json", always_timeout)

    for _ in range(2):
        with pytest.raises(ConnectionError, match="재시도 실패"):
            client._call_kis_get("/uapi/test", "TRTEST", {})  # noqa: SLF001

    with pytest.raises(KISAPIError, match="circuit breaker OPEN") as exc_info:
        client._call_kis_get("/uapi/test", "TRTEST", {})  # noqa: SLF001

    assert calls["n"] == 2
    assert exc_info.value.retry_after_sec is not None


def test_kis_rest_headers_match_official_sample_shape(monkeypatch):
    """KIS 공식 샘플 kis_auth.py의 공통 계좌조회 헤더 형태를 유지한다."""
    monkeypatch.setenv("KIS_MODE", "virtual")
    from src.connectors.kis_rest import KISRestClient

    client = KISRestClient()
    monkeypatch.setattr(client.auth, "get_kis_token", lambda: "token-a")
    monkeypatch.setattr(
        client.auth,
        "get_kis_app_credentials",
        lambda: ("app-key", "app-secret"),
    )

    headers = client._kis_headers("VTTC8434R")  # noqa: SLF001 - header contract test

    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert headers["Accept"] == "text/plain"
    assert headers["charset"] == "UTF-8"
    assert headers["authorization"] == "Bearer token-a"
    assert headers["appkey"] == "app-key"
    assert headers["appsecret"] == "app-secret"
    assert headers["tr_id"] == "VTTC8434R"
    assert headers["tr_cont"] == ""
    assert headers["custtype"] == "P"


def test_kis_rest_virtual_get_order_history_normalizes_response(monkeypatch):
    """KIS 일별 주문체결조회 output1을 paper reconciliation용으로 정규화한다."""
    monkeypatch.setenv("KIS_MODE", "virtual")
    monkeypatch.setenv("KIS_ACCOUNT_NUMBER", "05018716")
    monkeypatch.setenv("KIS_ACCOUNT_PRODUCT_CODE", "01")
    from src.connectors.kis_rest import KISRestClient

    client = KISRestClient()

    def fake_get(path, tr_id, params):
        assert path.endswith("/inquire-daily-ccld")
        assert tr_id == "VTTC0081R"
        assert params["INQR_STRT_DT"] == "20260508"
        assert params["INQR_END_DT"] == "20260508"
        assert params["SLL_BUY_DVSN_CD"] == "02"
        assert params["PDNO"] == "005930"
        assert params["CCLD_DVSN"] == "01"
        assert params["EXCG_ID_DVSN_CD"] == "KRX"
        assert "INQR_DVSN_2" not in params
        return {
            "rt_cd": "0",
            "output1": [
                {
                    "odno": "12345",
                    "pdno": "005930",
                    "prdt_name": "삼성전자",
                    "sll_buy_dvsn_cd": "02",
                    "ord_qty": "3",
                    "ord_unpr": "70000",
                    "tot_ccld_qty": "3",
                    "avg_prvs": "70010",
                    "tot_ccld_amt": "210030",
                    "ord_tmd": "101010",
                }
            ],
            "output2": {"tot_ord_qty": "3"},
        }

    monkeypatch.setattr(client, "_call_kis_get", fake_get)

    result = client.get_order_history(
        start_date="20260508",
        end_date="20260508",
        ticker="5930",
        side="buy",
        execution_filter="filled",
    )

    assert result["_mode"] == "virtual"
    assert result["orders"][0]["order_id"] == "12345"
    assert result["orders"][0]["ticker"] == "005930"
    assert result["orders"][0]["side"] == "buy"
    assert result["orders"][0]["status"] == "filled"
    assert result["orders"][0]["avg_fill_price"] == pytest.approx(70010.0)


def test_kis_rest_ticker_padding(monkeypatch):
    _set_mock_env(monkeypatch)
    from src.connectors.kis_rest import KISRestClient
    client = KISRestClient()
    result = client.inquire_price("5930")  # 6자리 미만
    assert result["ticker"] == "005930"  # zfill 적용


def test_kis_rest_seed_reproducibility(monkeypatch):
    _set_mock_env(monkeypatch)
    from src.connectors.kis_rest import KISRestClient
    c1 = KISRestClient()
    c2 = KISRestClient()
    r1 = c1.inquire_price("005930")
    r2 = c2.inquire_price("005930")
    assert r1["current_price"] == r2["current_price"]  # 같은 seed


# ------------------------------------------------------------------ #
# KISWebSocketClient
# ------------------------------------------------------------------ #


def test_kis_ws_mock_subscribe(monkeypatch):
    _set_mock_env(monkeypatch)
    from src.connectors.kis_ws import KISWebSocketClient
    ws = KISWebSocketClient(tickers=["005930", "000660"])
    bars = list(ws.subscribe(n_bars=3))
    assert len(bars) == 6  # 2 tickers x 3 bars
    tickers = {b["ticker"] for b in bars}
    assert tickers == {"005930", "000660"}


def test_kis_ws_virtual_raises(monkeypatch):
    monkeypatch.setenv("KIS_MODE", "virtual")
    from src.connectors.kis_ws import KISWebSocketClient
    ws = KISWebSocketClient()
    with pytest.raises(NotImplementedError):
        list(ws.subscribe(n_bars=1))


def test_kis_ws_mock_bar_fields(monkeypatch):
    """각 bar dict에 C1 MinuteBarContract 호환 필드 포함 여부."""
    _set_mock_env(monkeypatch)
    from src.connectors.kis_ws import KISWebSocketClient
    ws = KISWebSocketClient(tickers=["005930"])
    bars = list(ws.subscribe(n_bars=1))
    assert len(bars) == 1
    bar = bars[0]
    for field in (
        "ticker", "open", "high", "low", "close", "volume", "ts_close", "_mode",
        "vwap", "turnover", "change", "ingest_ts", "completeness",
    ):
        assert field in bar, f"필드 누락: {field}"
    assert bar["high"] >= bar["low"]
    assert bar["_mode"] == "mock"
    assert bar["completeness"] == "full"
    assert isinstance(bar["vwap"], float)
    assert isinstance(bar["turnover"], float)
