"""live_data_readiness artifact gate tests."""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo


def _load_script_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "live_data_readiness.py"
    spec = importlib.util.spec_from_file_location("live_data_readiness", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_jsonl_day(base_dir: Path, ticker: str, yyyymmdd: str, rows: int) -> None:
    ticker_dir = base_dir / ticker
    ticker_dir.mkdir(parents=True, exist_ok=True)
    out = ticker_dir / f"bars_1m_{yyyymmdd}.jsonl"
    start = datetime(
        int(yyyymmdd[:4]),
        int(yyyymmdd[4:6]),
        int(yyyymmdd[6:]),
        9,
        0,
        tzinfo=ZoneInfo("Asia/Seoul"),
    )
    with out.open("w", encoding="utf-8") as fh:
        for i in range(rows):
            ts = start + timedelta(minutes=i)
            rec = {
                "ticker": ticker,
                "ts_close": ts.isoformat(),
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1.0,
            }
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _write_jsonl_named_day_with_ts_date(
    base_dir: Path,
    ticker: str,
    filename_yyyymmdd: str,
    ts_yyyymmdd: str,
    rows: int,
) -> None:
    ticker_dir = base_dir / ticker
    ticker_dir.mkdir(parents=True, exist_ok=True)
    out = ticker_dir / f"bars_1m_{filename_yyyymmdd}.jsonl"
    start = datetime(
        int(ts_yyyymmdd[:4]),
        int(ts_yyyymmdd[4:6]),
        int(ts_yyyymmdd[6:]),
        9,
        0,
        tzinfo=ZoneInfo("Asia/Seoul"),
    )
    with out.open("w", encoding="utf-8") as fh:
        for i in range(rows):
            ts = start + timedelta(minutes=i)
            rec = {
                "ticker": ticker,
                "ts_close": ts.isoformat(),
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1.0,
            }
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _write_jsonl_constant_ts(
    base_dir: Path,
    ticker: str,
    yyyymmdd: str,
    rows: int,
) -> None:
    ticker_dir = base_dir / ticker
    ticker_dir.mkdir(parents=True, exist_ok=True)
    out = ticker_dir / f"bars_1m_{yyyymmdd}.jsonl"
    ts = f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}T09:00:00+09:00"
    with out.open("w", encoding="utf-8") as fh:
        for _ in range(rows):
            rec = {
                "ticker": ticker,
                "ts_close": ts,
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1.0,
            }
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _write_parquet_day(base_dir: Path, ticker: str, yyyymmdd: str, rows: int) -> None:
    import pandas as pd

    ticker_dir = base_dir / ticker
    ticker_dir.mkdir(parents=True, exist_ok=True)
    start = datetime(
        int(yyyymmdd[:4]),
        int(yyyymmdd[4:6]),
        int(yyyymmdd[6:]),
        9,
        0,
        tzinfo=ZoneInfo("Asia/Seoul"),
    )
    records = []
    for i in range(rows):
        records.append({
            "ticker": ticker,
            "ts_close": (start + timedelta(minutes=i)).isoformat(),
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume": 1.0,
        })
    pd.DataFrame.from_records(records).to_parquet(
        ticker_dir / f"bars_1m_{yyyymmdd}.parquet",
        index=False,
    )


def test_load_active_tickers_includes_pending_for_final_dataset(monkeypatch, tmp_path):
    """final_dataset_gate가 켜져 있으면 active 20 + pending_data 종목을 함께 로드한다."""
    readiness = _load_script_module()
    universe_path = tmp_path / "universe_config.yaml"
    universe_path.write_text(
        "\n".join([
            "sectors:",
            "  반도체:",
            "    status: confirmed",
            "    stocks:",
            "      - {ticker: '005930', status: active}",
            "  금융:",
            "    status: confirmed_pending_data",
            "    stocks:",
            "      - {ticker: '105560', status: pending_data}",
        ]),
        encoding="utf-8",
    )
    monkeypatch.setattr(readiness, "_UNIVERSE_PATH", universe_path)
    monkeypatch.setattr(
        readiness,
        "config_load",
        lambda file_name, section: {
            "deploy_decision_gate": {
                "final_dataset_gate": {
                    "include_pending_data_tickers": True,
                    "allowed_stock_statuses": ["active", "pending_data"],
                    "allowed_sector_statuses": ["confirmed", "confirmed_pending_data"],
                }
            }
        } if section == "backtest_agent" else {},
    )

    assert readiness._load_active_tickers(None) == ["005930", "105560"]


def test_artifact_date_quality_rejects_short_stale_files(monkeypatch, tmp_path):
    """9-row stale artifact는 train 가능 날짜로 세지 않는다."""
    readiness = _load_script_module()
    monkeypatch.setattr(readiness, "_DATA_ROOT", tmp_path)

    for ticker in ("005930", "000660"):
        _write_jsonl_day(tmp_path, ticker, "20260507", 9)
        _write_jsonl_day(tmp_path, ticker, "20260508", 301)

    quality = readiness._artifact_date_quality(
        ["005930", "000660"],
        "20260507",
        "20260508",
        min_rows_per_day=300,
    )

    assert quality["20260507"]["is_valid"] is False
    assert quality["20260508"]["is_valid"] is True
    assert quality["20260507"]["missing_or_short_tickers"][0]["rows"] == 9


def test_artifact_date_quality_rejects_timestamp_date_mismatch(monkeypatch, tmp_path):
    """row 수가 충분해도 파일명 날짜와 ts_close 날짜가 다르면 readiness FAIL."""
    readiness = _load_script_module()
    monkeypatch.setattr(readiness, "_DATA_ROOT", tmp_path)

    for ticker in ("005930", "000660"):
        _write_jsonl_named_day_with_ts_date(tmp_path, ticker, "20260508", "20260507", 301)

    quality = readiness._artifact_date_quality(
        ["005930", "000660"],
        "20260508",
        "20260508",
        min_rows_per_day=300,
    )

    assert quality["20260508"]["is_valid"] is False
    assert quality["20260508"]["valid_ticker_count"] == 0
    first = quality["20260508"]["missing_or_short_tickers"][0]
    assert first["rows"] == 301
    assert first["timestamp_dates_match"] is False
    assert first["valid_date"] is False


def test_artifact_date_quality_rejects_wrong_ticker(monkeypatch, tmp_path):
    """폴더 ticker와 파일 내부 ticker가 다르면 readiness FAIL."""
    readiness = _load_script_module()
    monkeypatch.setattr(readiness, "_DATA_ROOT", tmp_path)

    _write_jsonl_day(tmp_path, "000660", "20260508", 301)
    _write_jsonl_day(tmp_path, "005930", "20260508", 301)
    file_path = tmp_path / "005930" / "bars_1m_20260508.jsonl"
    rows = [
        {**json.loads(line), "ticker": "000660"}
        for line in file_path.read_text(encoding="utf-8").splitlines()
    ]
    file_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )

    quality = readiness._artifact_date_quality(
        ["005930", "000660"],
        "20260508",
        "20260508",
        min_rows_per_day=300,
    )

    assert quality["20260508"]["is_valid"] is False
    first = quality["20260508"]["missing_or_short_tickers"][0]
    assert first["ticker"] == "005930"
    assert first["ticker_matches"] is False


def test_artifact_date_quality_rejects_partial_missing_timestamp(monkeypatch, tmp_path):
    """일부 row의 timestamp가 비어 있으면 row 수가 충분해도 readiness FAIL."""
    readiness = _load_script_module()
    monkeypatch.setattr(readiness, "_DATA_ROOT", tmp_path)

    _write_jsonl_day(tmp_path, "005930", "20260508", 301)
    _write_jsonl_day(tmp_path, "000660", "20260508", 301)
    file_path = tmp_path / "005930" / "bars_1m_20260508.jsonl"
    rows = [json.loads(line) for line in file_path.read_text(encoding="utf-8").splitlines()]
    rows[0].pop("ts_close")
    file_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )

    quality = readiness._artifact_date_quality(
        ["005930", "000660"],
        "20260508",
        "20260508",
        min_rows_per_day=300,
    )

    assert quality["20260508"]["is_valid"] is False
    first = quality["20260508"]["missing_or_short_tickers"][0]
    assert first["ticker"] == "005930"
    assert first["timestamp_dates_match"] is False
    assert first["missing_timestamp_count"] == 1


def test_artifact_date_quality_rejects_partial_missing_ticker(monkeypatch, tmp_path):
    """일부 row의 ticker가 비어 있으면 row 수가 충분해도 readiness FAIL."""
    readiness = _load_script_module()
    monkeypatch.setattr(readiness, "_DATA_ROOT", tmp_path)

    _write_jsonl_day(tmp_path, "005930", "20260508", 301)
    _write_jsonl_day(tmp_path, "000660", "20260508", 301)
    file_path = tmp_path / "005930" / "bars_1m_20260508.jsonl"
    rows = [json.loads(line) for line in file_path.read_text(encoding="utf-8").splitlines()]
    rows[0].pop("ticker")
    file_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )

    quality = readiness._artifact_date_quality(
        ["005930", "000660"],
        "20260508",
        "20260508",
        min_rows_per_day=300,
    )

    assert quality["20260508"]["is_valid"] is False
    first = quality["20260508"]["missing_or_short_tickers"][0]
    assert first["ticker"] == "005930"
    assert first["ticker_matches"] is False
    assert first["ticker_mismatch_count"] == 1


def test_artifact_date_quality_rejects_duplicate_timestamps(monkeypatch, tmp_path):
    """row 수만 맞춘 중복 timestamp artifact는 학습 가능 날짜가 아니다."""
    readiness = _load_script_module()
    monkeypatch.setattr(readiness, "_DATA_ROOT", tmp_path)

    _write_jsonl_constant_ts(tmp_path, "005930", "20260508", 301)
    _write_jsonl_day(tmp_path, "000660", "20260508", 301)

    quality = readiness._artifact_date_quality(
        ["005930", "000660"],
        "20260508",
        "20260508",
        min_rows_per_day=300,
    )

    assert quality["20260508"]["is_valid"] is False
    first = quality["20260508"]["missing_or_short_tickers"][0]
    assert first["ticker"] == "005930"
    assert first["duplicate_ts_count"] == 300


def test_run_backfill_trips_circuit_breaker_after_repeated_empty_fetches(
    monkeypatch,
    tmp_path,
):
    """KIS 반복 실패가 모든 날짜를 끝까지 소모하지 않고 backfill stage를 멈춘다."""
    readiness = _load_script_module()
    monkeypatch.setattr(readiness, "_DATA_ROOT", tmp_path)
    monkeypatch.setattr(
        readiness,
        "_business_dates_between",
        lambda start, end: ["20260511", "20260512", "20260513", "20260514"],
    )

    calls: list[tuple[str, str]] = []

    class FakeBackfill:
        def backfill_universe(self, tickers, start_date, end_date):
            calls.append((start_date, end_date))
            return {ticker: 0 for ticker in tickers}

    def fake_config_load(file_name: str, section: str | None = None):
        if section == "live_data_readiness":
            return {
                "min_rows_per_day": 300,
                "require_all_tickers_for_backfill": True,
                "max_consecutive_backfill_failed_dates": 2,
            }
        if section == "walk_forward":
            return {"trading_minutes_per_day": 390}
        return {}

    monkeypatch.setattr(readiness, "Backfill", lambda: FakeBackfill())
    monkeypatch.setattr(readiness, "config_load", fake_config_load)

    result = readiness.run_backfill(
        ["005930", "000660"],
        "20260511",
        "20260514",
    )

    assert result["status"] == "FAIL"
    assert calls == [("20260511", "20260511"), ("20260512", "20260512")]
    breaker = result["backfill_circuit_breaker"]
    assert breaker["triggered"] is True
    assert breaker["date"] == "20260512"
    assert breaker["consecutive_failed_dates"] == 2


def test_run_backfill_trips_circuit_breaker_when_most_tickers_short(
    monkeypatch,
    tmp_path,
):
    """대부분 종목이 반복 short fetch면 일부 성공 종목이 있어도 breaker가 열린다."""
    readiness = _load_script_module()
    monkeypatch.setattr(readiness, "_DATA_ROOT", tmp_path)
    monkeypatch.setattr(
        readiness,
        "_business_dates_between",
        lambda start, end: ["20260511", "20260512", "20260513"],
    )

    calls: list[tuple[str, str]] = []

    class MostlyShortBackfill:
        def backfill_universe(self, tickers, start_date, end_date):
            calls.append((start_date, end_date))
            return {"005930": 0, "000660": 0, "105560": 381}

    def fake_config_load(file_name: str, section: str | None = None):
        if section == "live_data_readiness":
            return {
                "min_rows_per_day": 300,
                "require_all_tickers_for_backfill": True,
                "max_consecutive_backfill_failed_dates": 2,
                "backfill_failed_ticker_ratio_threshold": 0.5,
            }
        if section == "walk_forward":
            return {"trading_minutes_per_day": 390}
        return {}

    monkeypatch.setattr(readiness, "Backfill", lambda: MostlyShortBackfill())
    monkeypatch.setattr(readiness, "config_load", fake_config_load)

    result = readiness.run_backfill(
        ["005930", "000660", "105560"],
        "20260511",
        "20260513",
    )

    assert result["status"] == "FAIL"
    assert calls == [("20260511", "20260511"), ("20260512", "20260512")]
    breaker = result["backfill_circuit_breaker"]
    assert breaker["triggered"] is True
    assert breaker["short_fetch_count"] == 2
    assert breaker["expected_ticker_count"] == 3
    assert breaker["short_fetch_ratio"] == 2 / 3
    assert breaker["failed_ticker_ratio_threshold"] == 0.5


def test_artifact_date_quality_rejects_duplicate_date_artifacts(monkeypatch, tmp_path):
    """같은 ticker/date에 jsonl과 parquet가 같이 있으면 중복 artifact로 막는다."""
    readiness = _load_script_module()
    monkeypatch.setattr(readiness, "_DATA_ROOT", tmp_path)

    _write_jsonl_day(tmp_path, "005930", "20260508", 301)
    _write_parquet_day(tmp_path, "005930", "20260508", 301)
    _write_jsonl_day(tmp_path, "000660", "20260508", 301)

    quality = readiness._artifact_date_quality(
        ["005930", "000660"],
        "20260508",
        "20260508",
        min_rows_per_day=300,
    )

    assert quality["20260508"]["is_valid"] is False
    first = quality["20260508"]["missing_or_short_tickers"][0]
    assert first["ticker"] == "005930"
    assert len(first["duplicate_date_artifacts"]) == 2


def test_artifact_date_quality_skips_krx_holidays(monkeypatch, tmp_path):
    """설 연휴처럼 평일 휴장일은 80일 gate 요구 날짜에서 제외한다."""
    readiness = _load_script_module()
    monkeypatch.setattr(readiness, "_DATA_ROOT", tmp_path)

    for ticker in ("005930", "000660"):
        _write_jsonl_day(tmp_path, ticker, "20260213", 301)
        _write_jsonl_day(tmp_path, ticker, "20260219", 301)

    quality = readiness._artifact_date_quality(
        ["005930", "000660"],
        "20260213",
        "20260219",
        min_rows_per_day=300,
    )

    assert list(quality) == ["20260213", "20260219"]
    assert all(day not in quality for day in ("20260216", "20260217", "20260218"))


def test_train_gate_reports_only_valid_artifact_dates(monkeypatch, tmp_path):
    """run_train_if_ready는 partial/stale 날짜를 available_dates에서 제외한다."""
    readiness = _load_script_module()
    monkeypatch.setattr(readiness, "_DATA_ROOT", tmp_path)

    for ticker in ("005930", "000660"):
        _write_jsonl_day(tmp_path, ticker, "20260507", 9)
        _write_jsonl_day(tmp_path, ticker, "20260508", 301)

    result = readiness.run_train_if_ready(
        ["005930", "000660"],
        "20260507",
        "20260508",
        require_train=False,
    )

    assert result["status"] == "SKIP"
    assert result["available_dates"] == 1
    assert result["first_date"] == "20260508"
    assert result["date_quality_sample"]["20260507"]["is_valid"] is False


def test_run_backfill_skips_existing_valid_artifacts(monkeypatch, tmp_path):
    """이미 유효한 parquet/jsonl 날짜는 재호출하지 않고 부족 날짜만 fetch한다."""
    readiness = _load_script_module()
    monkeypatch.setattr(readiness, "_DATA_ROOT", tmp_path)

    for ticker in ("005930", "000660"):
        _write_jsonl_day(tmp_path, ticker, "20260507", 301)

    calls: list[tuple[str, str]] = []

    class FakeBackfill:
        def backfill_universe(self, tickers, start_date, end_date):
            calls.append((start_date, end_date))
            for ticker in tickers:
                _write_jsonl_day(tmp_path, ticker, start_date, 301)
            return {ticker: 301 for ticker in tickers}

    monkeypatch.setattr(readiness, "Backfill", FakeBackfill)

    result = readiness.run_backfill(
        ["005930", "000660"],
        "20260507",
        "20260508",
        skip_existing=True,
    )

    assert result["status"] == "PASS"
    assert calls == [("20260508", "20260508")]
    assert result["skipped_existing_dates"] == ["20260507"]
    assert result["current_fetch_missing_or_short"] == []


def test_run_backfill_does_not_skip_stale_timestamp_artifact(monkeypatch, tmp_path):
    """파일명 날짜와 내부 ts_close 날짜가 다르면 stale artifact로 보고 재호출한다."""
    readiness = _load_script_module()
    monkeypatch.setattr(readiness, "_DATA_ROOT", tmp_path)

    for ticker in ("005930", "000660"):
        _write_jsonl_named_day_with_ts_date(tmp_path, ticker, "20260507", "20260506", 301)

    calls: list[tuple[str, str]] = []

    class FakeBackfill:
        def backfill_universe(self, tickers, start_date, end_date):
            calls.append((start_date, end_date))
            for ticker in tickers:
                _write_jsonl_day(tmp_path, ticker, start_date, 301)
            return {ticker: 301 for ticker in tickers}

    monkeypatch.setattr(readiness, "Backfill", FakeBackfill)

    result = readiness.run_backfill(
        ["005930", "000660"],
        "20260507",
        "20260507",
        skip_existing=True,
    )

    assert result["status"] == "PASS"
    assert calls == [("20260507", "20260507")]
    assert result["skipped_existing_dates"] == []
    for ticker in ("005930", "000660"):
        assert result["files"][ticker]["timestamp_dates_match"]["20260507"] is True


def test_smoke_blocks_mock_sources_without_allow_mock(monkeypatch):
    """live readiness 기본값은 mock connector를 PASS로 인정하지 않는다."""
    readiness = _load_script_module()
    monkeypatch.setenv("KIS_MODE", "mock")
    monkeypatch.delenv("DART_API_KEY", raising=False)
    monkeypatch.delenv("NAVER_CLIENT_ID", raising=False)
    monkeypatch.delenv("NAVER_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("ECOS_API_KEY", raising=False)
    monkeypatch.delenv("COMMUNITY_SCRAPE_ENABLED", raising=False)

    class MockUSMarketClient:
        _is_mock = True

        def get_indices(self, as_of=None):
            return SimpleNamespace(
                us_sp500_change=-0.003,
                us_nasdaq_change=-0.005,
                us_vix=18.5,
                us_soxx_change=-0.011,
                as_of_date="2026-05-07",
                source="mock",
            )

    monkeypatch.setattr(readiness, "USMarketClient", MockUSMarketClient)

    result = readiness.run_smoke(["005930"], "20260508", allow_mock=False)

    assert result["kis_investor_daily"]["status"] == "FAIL"
    assert result["kis_investor_daily"]["error_code"] == "MOCK_SOURCE_NOT_ALLOWED"
    assert result["community"]["status"] == "FAIL"
    assert result["ecos_macro"]["status"] == "FAIL"
    assert result["us_overnight"]["status"] == "FAIL"
    assert result["us_overnight"]["error_code"] == "MOCK_SOURCE_NOT_ALLOWED"


def test_smoke_passes_us_overnight_with_real_source(monkeypatch):
    """US overnight도 live readiness smoke의 필수 real-data 게이트다."""
    readiness = _load_script_module()

    class FakeKIS:
        mode = "virtual"
        auth = object()

        def investor_trade_by_stock_daily(self, ticker, as_of_date):
            return [{
                "ticker": ticker,
                "date": "2026-05-08T15:30:00+09:00",
                "foreign_net_buy": 1.0,
                "institutional_net_buy": 2.0,
                "retail_net_buy": -3.0,
            }]

        def get_price_snapshot(self, tickers):
            return [{"ticker": ticker, "last_price": 1.0} for ticker in tickers]

    class FakeKRX:
        _is_mock = False

        def __init__(self, auth=None):
            self.auth = auth

        def get_investor_info(self, ticker, bgn_de, end_de):
            return [{
                "payload": {
                    "ticker": ticker,
                    "foreign_net_buy": 1.0,
                    "institutional_net_buy": 2.0,
                    "retail_net_buy": -3.0,
                }
            }]

    class FakeDART:
        _is_mock = False

        def list_disclosures(self, bgn_de, end_de, page_count):
            return [{"event_id": "EVT"}]

    class FakeNaver:
        _is_mock = False

        def search_news(self, query, display):
            return [{"title": query}]

    class FakeCommunity:
        _is_mock = False

        def poll(self, tickers):
            return [
                SimpleNamespace(
                    ticker=tickers[0],
                    timestamp=datetime(2026, 5, 8, 12, 0, tzinfo=ZoneInfo("Asia/Seoul")),
                )
            ]

    class FakeECOS:
        _is_mock = False

        def get_macro_pack(self, as_of_date):
            return {"interest_rate": 0.0, "usd_krw": 1450.8}

    class FakeUSMarketClient:
        _is_mock = False

        def get_indices(self, as_of=None):
            assert as_of == "2026-05-08"
            return SimpleNamespace(
                us_sp500_change=-0.003,
                us_nasdaq_change=-0.001,
                us_vix=17.1,
                us_soxx_change=-0.02,
                as_of_date="2026-05-07",
                source="yfinance",
            )

    monkeypatch.setattr(readiness, "KISRestClient", FakeKIS)
    monkeypatch.setattr(readiness, "KRXRestClient", FakeKRX)
    monkeypatch.setattr(readiness, "DARTRestClient", FakeDART)
    monkeypatch.setattr(readiness, "NaverNewsClient", FakeNaver)
    monkeypatch.setattr(readiness, "CommunityCrawler", FakeCommunity)
    monkeypatch.setattr(readiness, "ECOSRestClient", FakeECOS)
    monkeypatch.setattr(readiness, "USMarketClient", FakeUSMarketClient)

    result = readiness.run_smoke(["005930"], "20260508", allow_mock=False)

    assert all(item["status"] == "PASS" for item in result.values())
    assert result["us_overnight"]["indices"]["source"] == "yfinance"
    assert result["us_overnight"]["indices"]["as_of_date"] == "2026-05-07"


def test_smoke_keeps_community_raw_posts_out_of_historical_c2_evidence(monkeypatch):
    """과거 as_of smoke는 현재 community raw post를 C2 event evidence로 세지 않는다."""
    readiness = _load_script_module()

    class FakeKIS:
        mode = "virtual"
        auth = object()

        def investor_trade_by_stock_daily(self, ticker, as_of_date):
            return [{
                "ticker": ticker,
                "date": "2026-05-08T15:30:00+09:00",
                "foreign_net_buy": 1.0,
                "institutional_net_buy": 2.0,
                "retail_net_buy": -3.0,
            }]

        def get_price_snapshot(self, tickers):
            return [{"ticker": ticker, "last_price": 1.0} for ticker in tickers]

    class FakeKRX:
        _is_mock = False

        def __init__(self, auth=None):
            self.auth = auth

        def get_investor_info(self, ticker, bgn_de, end_de):
            return [{
                "payload": {
                    "ticker": ticker,
                    "foreign_net_buy": 1.0,
                    "institutional_net_buy": 2.0,
                    "retail_net_buy": -3.0,
                }
            }]

    class FakeDART:
        _is_mock = False

        def list_disclosures(self, bgn_de, end_de, page_count):
            return [{"event_id": "EVT"}]

    class FakeNaver:
        _is_mock = False

        def search_news(self, query, display):
            return [{"title": query}]

    class FakeCommunity:
        _is_mock = False

        def poll(self, tickers):
            return [
                SimpleNamespace(
                    ticker=tickers[0],
                    timestamp=datetime(2026, 5, 16, 2, i, tzinfo=ZoneInfo("Asia/Seoul")),
                )
                for i in range(3)
            ]

    class FakeECOS:
        _is_mock = False

        def get_macro_pack(self, as_of_date):
            return {"interest_rate": 2.5, "usd_krw": 1450.8}

    class FakeUSMarketClient:
        _is_mock = False

        def get_indices(self, as_of=None):
            return SimpleNamespace(
                us_sp500_change=-0.003,
                us_nasdaq_change=-0.001,
                us_vix=17.1,
                us_soxx_change=-0.02,
                as_of_date="2026-05-07",
                source="yfinance",
            )

    monkeypatch.setattr(readiness, "KISRestClient", FakeKIS)
    monkeypatch.setattr(readiness, "KRXRestClient", FakeKRX)
    monkeypatch.setattr(readiness, "DARTRestClient", FakeDART)
    monkeypatch.setattr(readiness, "NaverNewsClient", FakeNaver)
    monkeypatch.setattr(readiness, "CommunityCrawler", FakeCommunity)
    monkeypatch.setattr(readiness, "ECOSRestClient", FakeECOS)
    monkeypatch.setattr(readiness, "USMarketClient", FakeUSMarketClient)

    result = readiness.run_smoke(["005930"], "20260508", allow_mock=False)

    assert result["community"]["status"] == "PASS"
    assert result["community"]["event_count"] == 0
    assert result["community"]["raw_post_count"] == 3
    assert result["community"]["as_of_date"] == "20260508"
    assert result["community"]["as_of_aligned_post_count"] == 0
    assert result["community"]["as_of_mismatch_count"] == 3
    assert result["community"]["normalized_in_smoke"] is False


def test_write_report_persists_report_path(monkeypatch, tmp_path):
    """stdout JSON과 저장 JSON의 report_path가 어긋나지 않는다."""
    readiness = _load_script_module()
    monkeypatch.setattr(readiness, "_REPORT_ROOT", tmp_path)
    report = {"status": "PASS", "stages": {}}

    out = readiness._write_report(report)
    saved = json.loads(out.read_text(encoding="utf-8"))

    assert saved["report_path"] == str(out)
    assert "report_path_relative" in saved
    assert report["report_path"] == str(out)


def test_backfill_fails_when_live_fetch_zero_even_if_artifact_exists(monkeypatch, tmp_path):
    """기존 artifact가 유효해도 이번 live fetch가 0 rows면 backfill은 FAIL이다."""
    readiness = _load_script_module()
    monkeypatch.setattr(readiness, "_DATA_ROOT", tmp_path)
    monkeypatch.setattr(readiness, "_readiness_min_rows", lambda key: 300)
    monkeypatch.setattr(
        readiness,
        "_readiness_cfg",
        lambda: {"require_all_tickers_for_backfill": True},
    )

    for ticker in ("005930", "000660"):
        _write_jsonl_day(tmp_path, ticker, "20260508", 381)

    class ZeroFetchBackfill:
        def backfill_universe(self, tickers, start_date, end_date):
            return {ticker: 0 for ticker in tickers}

    monkeypatch.setattr(readiness, "Backfill", ZeroFetchBackfill)

    result = readiness.run_backfill(["005930", "000660"], "20260508", "20260508")

    assert result["status"] == "FAIL"
    assert result["missing_or_empty_tickers"] == []
    assert result["files"]["005930"]["valid_dates"]["20260508"] is True
    assert result["current_fetch_missing_or_short"] == [
        {"ticker": "005930", "date": "20260508", "fetched_rows": 0},
        {"ticker": "000660", "date": "20260508", "fetched_rows": 0},
    ]


def test_backfill_passes_when_live_fetch_and_artifact_are_valid(monkeypatch, tmp_path):
    """이번 live fetch와 저장 artifact가 모두 유효할 때만 backfill PASS."""
    readiness = _load_script_module()
    monkeypatch.setattr(readiness, "_DATA_ROOT", tmp_path)
    monkeypatch.setattr(readiness, "_readiness_min_rows", lambda key: 300)
    monkeypatch.setattr(
        readiness,
        "_readiness_cfg",
        lambda: {"require_all_tickers_for_backfill": True},
    )

    for ticker in ("005930", "000660"):
        _write_jsonl_day(tmp_path, ticker, "20260508", 381)

    class ValidFetchBackfill:
        def backfill_universe(self, tickers, start_date, end_date):
            return {ticker: 381 for ticker in tickers}

    monkeypatch.setattr(readiness, "Backfill", ValidFetchBackfill)

    result = readiness.run_backfill(["005930", "000660"], "20260508", "20260508")

    assert result["status"] == "PASS"
    assert result["current_fetch_missing_or_short"] == []
    assert result["fetch_counts_by_date"]["20260508"] == {
        "005930": 381,
        "000660": 381,
    }
