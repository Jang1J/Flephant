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


def test_kis_rest_virtual_raises(monkeypatch):
    """virtual 모드는 아직 NotImplementedError (Sprint 1)."""
    monkeypatch.setenv("KIS_MODE", "virtual")
    from src.connectors.kis_rest import KISRestClient
    client = KISRestClient()
    with pytest.raises(NotImplementedError):
        client.inquire_price("005930")


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
