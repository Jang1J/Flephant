"""Historical feature materializer fail-closed tests."""
from __future__ import annotations

import json
import importlib.util
from pathlib import Path
from types import SimpleNamespace


def _load_script(name: str):
    script_path = Path(__file__).resolve().parents[2] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dual_source_history_blocks_without_archived_raw_events(tmp_path):
    mod = _load_script("materialize_dual_source_history")
    report = mod.materialize_dual_source_history(
        end_date="20260508",
        business_days=1,
        raw_events_dir=tmp_path / "missing_raw",
        artifact_dir=tmp_path / "dual_source",
        output_dir=tmp_path / "reports",
    )

    assert report["status"] == "BLOCKED"
    assert report["files_written"] == []
    assert "no_dual_source_artifacts_written" in report["blockers"]
    assert report["per_date"][0]["status"] == "MISSING_RAW_EVENTS"


def test_agent_memory_dual_source_export_is_rehearsal_only(tmp_path):
    export_mod = _load_script("export_agent_memory_dual_source_raw")
    materialize_mod = _load_script("materialize_dual_source_history")

    memory_file = tmp_path / "agent_memory" / "news_agent" / "005930" / "20260508.jsonl"
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text(
        '{"ts":"2026-05-08T08:00:00+09:00","ticker":"005930",'
        '"content":{"event_type":"news","title":"삼성 실적 호조","stance":"buy"}}\n',
        encoding="utf-8",
    )

    export_report = export_mod.export_agent_memory_dual_source_raw(
        end_date="20260508",
        business_days=1,
        agent_memory_dir=tmp_path / "agent_memory",
        output_dir=tmp_path / "raw_agent_memory",
        report_dir=tmp_path / "reports",
    )

    assert export_report["status"] == "PASS"
    assert export_report["deploy_quality"] is False
    raw_path = tmp_path / "raw_agent_memory" / "events_20260508.json"
    assert raw_path.exists()

    materialize_report = materialize_mod.materialize_dual_source_history(
        end_date="20260508",
        business_days=1,
        raw_events_dir=tmp_path / "raw_agent_memory",
        artifact_dir=tmp_path / "dual_source",
        output_dir=tmp_path / "materialize_reports",
    )

    assert materialize_report["status"] == "BLOCKED"
    assert materialize_report["files_written"] == []
    assert "non_deploy_quality_raw_events" in materialize_report["blockers"]
    assert materialize_report["per_date"][0]["status"] == "NON_DEPLOY_QUALITY_RAW_EVENTS"


def test_dual_source_history_blocks_missing_deploy_quality_provenance(tmp_path):
    mod = _load_script("materialize_dual_source_history")
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "20260508.json").write_text(
        json.dumps({
            "events": [{
                "ticker": "005930",
                "event_ts": "2026-05-08T08:00:00+09:00",
                "source": "news",
                "title": "실적 호조",
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    report = mod.materialize_dual_source_history(
        end_date="20260508",
        business_days=1,
        raw_events_dir=raw_dir,
        artifact_dir=tmp_path / "dual_source",
        output_dir=tmp_path / "reports",
    )

    assert report["status"] == "BLOCKED"
    assert report["files_written"] == []
    assert "non_deploy_quality_raw_events" in report["blockers"]
    assert report["per_date"][0]["status"] == "NON_DEPLOY_QUALITY_RAW_EVENTS"


def test_dual_source_history_blocks_missing_event_timestamp(monkeypatch, tmp_path):
    mod = _load_script("materialize_dual_source_history")
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "20260508.json").write_text(
        json.dumps({
            "provenance": {"deploy_quality": True},
            "events": [{
                "ticker": "005930",
                "source": "news",
                "title": "실적 호조",
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "_load_active_universe", lambda: [{"ticker": "005930"}])
    monkeypatch.setattr(mod, "_business_dates", lambda end_date, business_days: ["20260508"])

    report = mod.materialize_dual_source_history(
        end_date="20260508",
        business_days=1,
        raw_events_dir=raw_dir,
        artifact_dir=tmp_path / "dual_source",
        output_dir=tmp_path / "reports",
    )

    assert report["status"] == "BLOCKED"
    assert report["files_written"] == []
    assert report["per_date"][0]["status"] == "ERROR"
    assert "missing required timestamp" in report["per_date"][0]["error"]


def test_dual_source_history_uses_sector_and_market_fallback(monkeypatch, tmp_path):
    mod = _load_script("materialize_dual_source_history")
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "20260508.json").write_text(
        (
            "{"
            '"provenance":{"deploy_quality":true},'
            '"events":['
            '{'
            '"ticker":"005930",'
            '"event_ts":"2026-05-08T08:00:00+09:00",'
            '"source":"news",'
            '"title":"삼성전자 실적 호조"'
            '}'
            "]}"
        ),
        encoding="utf-8",
    )

    captured_rows = []

    class DummyScorer:
        def score_universe(self, rows, snapshot_ts=None):
            captured_rows.extend(rows)
            return [
                {
                    "ticker": row["ticker"],
                    "asof": "2026-05-08T08:30:00+09:00",
                    "news_score_t": 0.5 if row["ticker"] == "005930" else 0.0,
                    "comm_score_t_1": 0.0,
                    "comm_score_t_2": 0.0,
                    "news_comm_divergence": 0.5 if row["ticker"] == "005930" else 0.0,
                    "community_noise_multiplier": 1.0,
                    "source_notes": "fake",
                }
                for row in rows
            ]

        def score(self, **kwargs):
            return {
                "ticker": kwargs["ticker"],
                "asof": "2026-05-08T08:30:00+09:00",
                "news_score_t": 0.25,
                "comm_score_t_1": 0.0,
                "comm_score_t_2": 0.0,
                "news_comm_divergence": 0.25,
                "community_noise_multiplier": 1.0,
                "source_notes": "market",
            }

    monkeypatch.setattr(mod, "_load_active_universe", lambda: [
        {"ticker": "005930"},
        {"ticker": "000660"},
        {"ticker": "051910"},
    ])
    monkeypatch.setattr(mod, "_business_dates", lambda end_date, business_days: ["20260508"])
    monkeypatch.setattr(mod, "DualSourceScorer", lambda: DummyScorer())

    report = mod.materialize_dual_source_history(
        end_date="20260508",
        business_days=1,
        raw_events_dir=raw_dir,
        artifact_dir=tmp_path / "dual_source",
        output_dir=tmp_path / "reports",
    )

    assert report["status"] == "PASS", report
    rows_by_ticker = {row["ticker"]: row for row in captured_rows}
    assert rows_by_ticker["005930"]["source_scope"]["news"] == "ticker"
    assert rows_by_ticker["000660"]["source_scope"]["news"] == "sector_fallback"
    assert rows_by_ticker["051910"]["source_scope"]["news"] == "market_fallback"
    source_stats = report["per_date"][0]["source_stats"]
    assert source_stats["fallback_scope_counts"]["news"]["ticker"] == 1
    assert source_stats["fallback_scope_counts"]["news"]["sector_fallback"] == 1
    assert source_stats["fallback_scope_counts"]["news"]["market_fallback"] == 1
    assert source_stats["market_backstop_rows"] == 2
    artifact = tmp_path / "dual_source" / "20260508.json"
    assert artifact.exists()
    assert "news_scope=sector_fallback" in artifact.read_text(encoding="utf-8")
    assert "market_backstop" in artifact.read_text(encoding="utf-8")


def test_dual_source_history_coverage_threshold_uses_risk_config(monkeypatch, tmp_path):
    mod = _load_script("materialize_dual_source_history")
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "20260508.json").write_text(
        json.dumps({
            "provenance": {"deploy_quality": True},
            "rows": [{
                "ticker": "005930",
                "news_texts": ["실적 호조"],
                "comm_texts_t1": [],
                "comm_texts_t2": [],
                "data_ts": "2026-05-08T08:00:00+09:00",
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    class DummyScorer:
        def score_universe(self, rows, snapshot_ts=None):
            return [
                {
                    "ticker": row["ticker"],
                    "asof": "2026-05-08T08:30:00+09:00",
                    "news_score_t": 0.5,
                    "comm_score_t_1": 0.0,
                    "comm_score_t_2": 0.0,
                    "news_comm_divergence": 0.5,
                    "community_noise_multiplier": 1.0,
                    "source_notes": "test",
                }
                for row in rows
            ]

        def score(self, **kwargs):
            return {
                "ticker": kwargs["ticker"],
                "news_score_t": 0.0,
                "comm_score_t_1": 0.0,
                "comm_score_t_2": 0.0,
                "news_comm_divergence": 0.0,
                "community_noise_multiplier": 1.0,
                "source_notes": "test",
            }

    def fake_config_load(file_name: str, section: str | None = None):
        if file_name == "risk_config.yaml" and section == "phase2_feature_backfill":
            return {"min_dual_source_non_neutral_date_coverage": 0.4}
        return {}

    monkeypatch.setattr(mod, "config_load", fake_config_load)
    monkeypatch.setattr(mod, "_load_active_universe", lambda: [{"ticker": "005930"}])
    monkeypatch.setattr(mod, "_business_dates", lambda end_date, business_days: ["20260508", "20260511"])
    monkeypatch.setattr(mod, "DualSourceScorer", lambda: DummyScorer())

    report = mod.materialize_dual_source_history(
        end_date="20260511",
        business_days=2,
        raw_events_dir=raw_dir,
        artifact_dir=tmp_path / "dual_source",
        output_dir=tmp_path / "reports",
    )

    assert report["status"] == "PASS", report
    assert report["coverage"]["dual_source_non_neutral_date_coverage"] == 0.5
    assert report["coverage"]["min_dual_source_non_neutral_date_coverage"] == 0.4


def test_exogenous_history_blocks_without_required_real_providers(monkeypatch, tmp_path):
    mod = _load_script("materialize_exogenous_history")

    class DummyUS:
        _is_mock = False

    class DummyECOS:
        _is_mock = True

    class DummyKRX:
        def _has_kis_investor_provider(self):
            return False

    monkeypatch.setattr(mod, "USMarketClient", lambda: DummyUS())
    monkeypatch.setattr(mod, "ECOSRestClient", lambda: DummyECOS())
    monkeypatch.setattr(mod, "KRXRestClient", lambda: DummyKRX())
    monkeypatch.setattr(mod, "_active_tickers", lambda: ["005930"])
    monkeypatch.setattr(mod, "_business_dates", lambda end_date, business_days: ["20260508"])

    report = mod.materialize_exogenous_history(
        end_date="20260508",
        business_days=1,
        artifact_dir=tmp_path / "exogenous",
        output_dir=tmp_path / "reports",
    )

    assert report["status"] == "BLOCKED"
    assert report["files_written"] == []
    assert "required_real_provider_unavailable" in report["blockers"]
    assert report["provider_availability"]["ecos_real"] is False
    assert report["provider_availability"]["kis_investor_real"] is False


def test_exogenous_active_tickers_include_pending_for_final_dataset(monkeypatch):
    """exogenous history도 final_dataset_gate 30종목 universe를 사용한다."""
    mod = _load_script("materialize_exogenous_history")
    universe_cfg = {
        "sectors": {
            "반도체": {
                "status": "confirmed",
                "stocks": [{"ticker": "5930", "status": "active"}],
            },
            "금융": {
                "status": "confirmed_pending_data",
                "stocks": [{"ticker": "105560", "status": "pending_data"}],
            },
        },
        "backtest_universe_mode": {"fallback_tickers": []},
    }
    risk_cfg = {
        "deploy_decision_gate": {
            "final_dataset_gate": {
                "include_pending_data_tickers": True,
                "allowed_stock_statuses": ["active", "pending_data"],
                "allowed_sector_statuses": ["confirmed", "confirmed_pending_data"],
            }
        }
    }

    def fake_config_load(file_name: str, section: str | None = None):
        if file_name == "universe_config.yaml":
            return universe_cfg
        if file_name == "risk_config.yaml" and section == "backtest_agent":
            return risk_cfg
        return {}

    monkeypatch.setattr(mod, "config_load", fake_config_load)

    assert mod._active_tickers() == ["005930", "105560"]


def test_exogenous_history_blocks_us_market_mock_result_after_real_client_init(
    monkeypatch,
    tmp_path,
):
    mod = _load_script("materialize_exogenous_history")

    class DummyUS:
        _is_mock = False

        def get_indices(self, as_of):
            return SimpleNamespace(
                us_sp500_change=0.01,
                us_nasdaq_change=0.02,
                us_vix=18.5,
                us_soxx_change=0.03,
                source="mock",
                as_of_date=as_of,
            )

    class DummyECOS:
        _is_mock = False

        def get_macro_pack(self, date_key):
            return {"interest_rate": 3.5, "usd_krw": 1350.0}

    class DummyKRX:
        def _has_kis_investor_provider(self):
            return True

        def get_investor_info(self, ticker, bgn_de, end_de):
            raise AssertionError("mock US market source should block before KRX fetch")

    monkeypatch.setattr(mod, "USMarketClient", lambda: DummyUS())
    monkeypatch.setattr(mod, "ECOSRestClient", lambda: DummyECOS())
    monkeypatch.setattr(mod, "KRXRestClient", lambda: DummyKRX())
    monkeypatch.setattr(mod, "_active_tickers", lambda: ["005930"])
    monkeypatch.setattr(mod, "_business_dates", lambda end_date, business_days: ["20260508"])

    report = mod.materialize_exogenous_history(
        end_date="20260508",
        business_days=1,
        artifact_dir=tmp_path / "exogenous",
        output_dir=tmp_path / "reports",
    )

    assert report["status"] == "BLOCKED"
    assert report["files_written"] == []
    assert "us_market_source_not_yfinance" in report["blockers"]
    assert report["per_date"][0]["source_stats"]["us_market_source"] == "mock"
    assert not (tmp_path / "exogenous" / "20260508.json").exists()


def test_exogenous_history_accepts_normalized_investor_events(monkeypatch, tmp_path):
    mod = _load_script("materialize_exogenous_history")

    class DummyUS:
        _is_mock = False

        def get_indices(self, as_of):
            assert as_of == "2026-05-08"
            return SimpleNamespace(
                us_sp500_change=0.01,
                us_nasdaq_change=0.02,
                us_vix=18.5,
                us_soxx_change=0.03,
                source="yfinance",
                as_of_date="2026-05-07",
            )

    class DummyECOS:
        _is_mock = False

        def get_macro_pack(self, date_key):
            return {"interest_rate": 3.5, "usd_krw": 1350.0}

    class DummyKRX:
        def _has_kis_investor_provider(self):
            return True

        def get_investor_info(self, ticker, bgn_de, end_de):
            assert bgn_de == "20260507"
            assert end_de == "20260507"
            return [
                {
                    "event_type": "investor_flow",
                    "occurred_at": "2026-05-07T15:30:00+09:00",
                    "payload": {
                        "ticker": ticker,
                        "date": "2026-05-07T15:30:00+09:00",
                        "foreign_net_buy": "1000",
                        "institutional_net_buy": -250,
                        "retail_net_buy": "-750",
                    },
                }
            ]

    monkeypatch.setattr(mod, "USMarketClient", lambda: DummyUS())
    monkeypatch.setattr(mod, "ECOSRestClient", lambda: DummyECOS())
    monkeypatch.setattr(mod, "KRXRestClient", lambda: DummyKRX())
    monkeypatch.setattr(mod, "_active_tickers", lambda: ["005930", "000660"])
    monkeypatch.setattr(mod, "_business_dates", lambda end_date, business_days: ["20260508"])

    report = mod.materialize_exogenous_history(
        end_date="20260508",
        business_days=1,
        artifact_dir=tmp_path / "exogenous",
        output_dir=tmp_path / "reports",
    )

    assert report["status"] == "PASS", report
    assert report["coverage"]["written_date_count"] == 1
    assert report["per_date"][0]["source_stats"]["investor_ticker_count"] == 2
    assert report["per_date"][0]["source_stats"]["investor_failures"] == {}
    assert (tmp_path / "exogenous" / "20260508.json").exists()


def test_exogenous_history_uses_latest_us_close_on_korea_only_holiday(
    monkeypatch,
    tmp_path,
):
    mod = _load_script("materialize_exogenous_history")

    class DummyUS:
        _is_mock = False

        def get_indices(self, as_of):
            assert as_of == "2026-05-06"
            return SimpleNamespace(
                us_sp500_change=0.01,
                us_nasdaq_change=0.02,
                us_vix=18.5,
                us_soxx_change=0.03,
                source="yfinance",
                as_of_date="2026-05-05",
            )

    class DummyECOS:
        _is_mock = False

        def get_macro_pack(self, date_key):
            return {"interest_rate": 3.5, "usd_krw": 1350.0}

    class DummyKRX:
        def _has_kis_investor_provider(self):
            return True

        def get_investor_info(self, ticker, bgn_de, end_de):
            assert bgn_de == "20260504"
            assert end_de == "20260504"
            return [{
                "payload": {
                    "ticker": ticker,
                    "date": bgn_de,
                    "foreign_net_buy": 1000,
                    "institutional_net_buy": 0,
                    "retail_net_buy": -1000,
                }
            }]

    monkeypatch.setattr(mod, "USMarketClient", lambda: DummyUS())
    monkeypatch.setattr(mod, "ECOSRestClient", lambda: DummyECOS())
    monkeypatch.setattr(mod, "KRXRestClient", lambda: DummyKRX())
    monkeypatch.setattr(mod, "_active_tickers", lambda: ["005930"])
    monkeypatch.setattr(mod, "_business_dates", lambda end_date, business_days: ["20260506"])

    report = mod.materialize_exogenous_history(
        end_date="20260506",
        business_days=1,
        artifact_dir=tmp_path / "exogenous",
        output_dir=tmp_path / "reports",
    )

    assert report["status"] == "PASS", report
    stats = report["per_date"][0]["source_stats"]
    assert stats["us_market_expected_as_of_date"] == "2026-05-05"
    assert stats["us_market_request_as_of_date"] == "2026-05-06"
    assert (tmp_path / "exogenous" / "20260506.json").exists()


def test_exogenous_history_coverage_threshold_uses_risk_config(monkeypatch, tmp_path):
    mod = _load_script("materialize_exogenous_history")

    class DummyUS:
        _is_mock = False

        def get_indices(self, as_of):
            as_of_dates = {
                "2026-05-08": "2026-05-07",
                "2026-05-09": "2026-05-08",
            }
            non_neutral = as_of == "2026-05-08"
            return SimpleNamespace(
                us_sp500_change=0.01 if non_neutral else 0.0,
                us_nasdaq_change=0.0,
                us_vix=0.0,
                us_soxx_change=0.0,
                source="yfinance",
                as_of_date=as_of_dates[as_of],
            )

    class DummyECOS:
        _is_mock = False

        def get_macro_pack(self, date_key):
            return {"interest_rate": 0.0, "usd_krw": 0.0}

    class DummyKRX:
        def _has_kis_investor_provider(self):
            return True

        def get_investor_info(self, ticker, bgn_de, end_de):
            return [{
                "payload": {
                    "ticker": ticker,
                    "date": bgn_de,
                    "foreign_net_buy": 0.0,
                    "institutional_net_buy": 0.0,
                    "retail_net_buy": 0.0,
                }
            }]

    def fake_config_load(file_name: str, section: str | None = None):
        if file_name == "risk_config.yaml" and section == "phase2_feature_backfill":
            return {"min_exogenous_non_neutral_date_coverage": 0.4}
        return {}

    monkeypatch.setattr(mod, "config_load", fake_config_load)
    monkeypatch.setattr(mod, "USMarketClient", lambda: DummyUS())
    monkeypatch.setattr(mod, "ECOSRestClient", lambda: DummyECOS())
    monkeypatch.setattr(mod, "KRXRestClient", lambda: DummyKRX())
    monkeypatch.setattr(mod, "_active_tickers", lambda: ["005930"])
    monkeypatch.setattr(mod, "_business_dates", lambda end_date, business_days: ["20260508", "20260511"])

    report = mod.materialize_exogenous_history(
        end_date="20260511",
        business_days=2,
        artifact_dir=tmp_path / "exogenous",
        output_dir=tmp_path / "reports",
    )

    assert report["status"] == "PASS", report
    assert report["coverage"]["exogenous_non_neutral_date_coverage"] == 0.5
    assert report["coverage"]["min_exogenous_non_neutral_date_coverage"] == 0.4
    assert report["coverage"]["written_date_count"] == 1


def test_exogenous_history_blocks_future_us_market_asof(monkeypatch, tmp_path):
    mod = _load_script("materialize_exogenous_history")

    class DummyUS:
        _is_mock = False

        def get_indices(self, as_of):
            return SimpleNamespace(
                us_sp500_change=0.01,
                us_nasdaq_change=0.0,
                us_vix=18.5,
                us_soxx_change=0.0,
                source="yfinance",
                as_of_date="2026-05-08",
            )

    class DummyECOS:
        _is_mock = False

        def get_macro_pack(self, date_key):
            return {"interest_rate": 3.5, "usd_krw": 1350.0}

    class DummyKRX:
        def _has_kis_investor_provider(self):
            return True

    monkeypatch.setattr(mod, "USMarketClient", lambda: DummyUS())
    monkeypatch.setattr(mod, "ECOSRestClient", lambda: DummyECOS())
    monkeypatch.setattr(mod, "KRXRestClient", lambda: DummyKRX())
    monkeypatch.setattr(mod, "_active_tickers", lambda: ["005930"])
    monkeypatch.setattr(mod, "_business_dates", lambda end_date, business_days: ["20260508"])

    report = mod.materialize_exogenous_history(
        end_date="20260508",
        business_days=1,
        artifact_dir=tmp_path / "exogenous",
        output_dir=tmp_path / "reports",
    )

    assert report["status"] == "BLOCKED"
    assert "us_market_as_of_after_expected_close" in report["blockers"]
    assert not (tmp_path / "exogenous" / "20260508.json").exists()


def test_exogenous_history_blocks_stale_us_market_asof(monkeypatch, tmp_path):
    mod = _load_script("materialize_exogenous_history")

    class DummyUS:
        _is_mock = False

        def get_indices(self, as_of):
            assert as_of == "2026-05-08"
            return SimpleNamespace(
                us_sp500_change=0.01,
                us_nasdaq_change=0.0,
                us_vix=18.5,
                us_soxx_change=0.0,
                source="yfinance",
                as_of_date="2026-05-06",
            )

    class DummyECOS:
        _is_mock = False

        def get_macro_pack(self, date_key):
            return {"interest_rate": 3.5, "usd_krw": 1350.0}

    class DummyKRX:
        def _has_kis_investor_provider(self):
            return True

        def get_investor_info(self, ticker, bgn_de, end_de):
            raise AssertionError("stale US market date should block before KRX fetch")

    monkeypatch.setattr(mod, "USMarketClient", lambda: DummyUS())
    monkeypatch.setattr(mod, "ECOSRestClient", lambda: DummyECOS())
    monkeypatch.setattr(mod, "KRXRestClient", lambda: DummyKRX())
    monkeypatch.setattr(mod, "_active_tickers", lambda: ["005930"])
    monkeypatch.setattr(mod, "_business_dates", lambda end_date, business_days: ["20260508"])

    report = mod.materialize_exogenous_history(
        end_date="20260508",
        business_days=1,
        artifact_dir=tmp_path / "exogenous",
        output_dir=tmp_path / "reports",
    )

    assert report["status"] == "BLOCKED"
    assert "us_market_as_of_mismatch" in report["blockers"]
    assert not (tmp_path / "exogenous" / "20260508.json").exists()
