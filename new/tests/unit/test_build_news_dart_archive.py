from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_script(name: str):
    script_path = Path(__file__).resolve().parents[2] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_news_dart_archive_blocks_mock_clients(monkeypatch, tmp_path: Path):
    mod = _load_script("build_news_dart_archive")

    class DummyDART:
        _is_mock = True

    class DummyNaver:
        _is_mock = False

    monkeypatch.setattr(mod, "DARTRestClient", lambda: DummyDART())
    monkeypatch.setattr(mod, "NaverNewsClient", lambda: DummyNaver())
    monkeypatch.setattr(mod, "_load_ticker_meta", lambda path: {"005930": {"corp_code": "001", "name": "삼성전자"}})
    monkeypatch.setattr(mod, "_business_dates", lambda end_date, business_days: ["20260508"])

    report = mod.build_archive(
        end_date="20260508",
        business_days=1,
        corp_cache_path=tmp_path / "corp.json",
        output_dir=tmp_path / "raw",
        report_dir=tmp_path / "reports",
    )

    assert report["status"] == "BLOCKED"
    assert report["files_written"] == []
    assert "required_real_provider_unavailable" in report["blockers"]
    assert not (tmp_path / "raw" / "20260508.json").exists()


def test_build_news_dart_archive_does_not_write_empty_deploy_payload(monkeypatch, tmp_path: Path):
    mod = _load_script("build_news_dart_archive")

    class DummyDART:
        _is_mock = False

    class DummyNaver:
        _is_mock = False

    monkeypatch.setattr(mod, "DARTRestClient", lambda: DummyDART())
    monkeypatch.setattr(mod, "NaverNewsClient", lambda: DummyNaver())
    monkeypatch.setattr(mod, "_load_ticker_meta", lambda path: {"005930": {"corp_code": "001", "name": "삼성전자"}})
    monkeypatch.setattr(mod, "_business_dates", lambda end_date, business_days: ["20260508"])
    monkeypatch.setattr(mod, "_fetch_dart_window", lambda *args, **kwargs: [])
    monkeypatch.setattr(mod, "_fetch_naver_for_ticker", lambda *args, **kwargs: [])
    monkeypatch.setattr(mod, "_fetch_naver_broadcast", lambda *args, **kwargs: [])
    monkeypatch.setattr(mod, "_load_sector_to_tickers", lambda: {})
    monkeypatch.setattr(mod, "_MARKET_QUERIES", ())

    report = mod.build_archive(
        end_date="20260508",
        business_days=1,
        corp_cache_path=tmp_path / "corp.json",
        output_dir=tmp_path / "raw",
        report_dir=tmp_path / "reports",
    )

    assert report["status"] == "BLOCKED"
    assert report["files_written"] == []
    assert report["total_events"] == 0
    assert "no_events_archived" in report["blockers"]
    assert not (tmp_path / "raw" / "20260508.json").exists()
