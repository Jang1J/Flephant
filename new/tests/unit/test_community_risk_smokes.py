"""Community/DataLab/source-scope smoke script tests."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_script(name: str):
    script_path = Path(__file__).resolve().parents[2] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def test_community_live_risk_smoke_publishes_and_reaches_fda(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMUNITY_SCRAPE_ENABLED", raising=False)
    script = _load_script("community_live_risk_smoke")

    report = script.run_community_live_risk_smoke(
        tickers=["005930"],
        providers=["cafearticle", "blog"],
        query_suffixes=["주식", "급락", "공시"],
        max_posts_per_ticker=6,
        display=3,
        sort="date",
        include_ticker_queries=False,
        max_queries_per_ticker=3,
        window_minutes=5,
        publish_to_message_pool=True,
        use_post_count_as_smoke_zscore=True,
        news_score_neutral_proxy=True,
        allow_mock=False,
        internal_fake_naver=True,
        output_dir=tmp_path,
        write_report=False,
    )

    assert report["status"] == "PASS"
    assert report["provider_mode"] == "naver_search"
    assert report["is_mock"] is False
    assert report["metrics"]["raw_post_count"] > 0
    assert report["metrics"]["valid_event_count"] > 0
    assert report["metrics"]["message_pool_publish_count"] > 0
    assert report["community_source_health"]["status"] == "PASS"
    assert report["community_source_health"]["provider_coverage"]["status"] == "PASS"
    assert report["community_source_health"]["official_api_proxy"] is True
    assert report["community_source_health"]["compliance"]["stores_raw_content"] is False
    assert report["community_source_health"]["timestamp_confidence_counts"]["low"] > 0
    assert "NEWS_COMMUNITY_DIVERGENCE" in report["community_source_health"]["risk_sidecar"]["reason_codes_emitted"]
    assert report["community_source_health"]["published_at_quality"]["cafearticle"] == "missing_collected_at"
    assert report["community_source_health"]["published_at_quality"]["blog"] == "official_postdate_date_only"
    assert report["fda"]["status"] == "PASS"
    assert report["fda"]["reason_code"] in {"NEWS_COMMUNITY_DIVERGENCE", "RISK_FAST_TRIGGER"}
    assert report["fda"]["can_change_weight"] is False
    for event in report["message_pool"]["risk_warning_messages"]:
        payload = event["payload"]
        assert payload.get("stores_raw_content") is not True
    for result in report["ingest_results"]:
        event = result.get("event", {})
        assert event.get("summary", "") == ""
        assert event.get("payload", {}).get("body", "") == ""


def test_community_live_risk_smoke_warns_when_requested_provider_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMUNITY_SCRAPE_ENABLED", raising=False)
    script = _load_script("community_live_risk_smoke")

    report = script.run_community_live_risk_smoke(
        tickers=["005930"],
        providers=["cafearticle", "blog"],
        query_suffixes=["주식"],
        max_posts_per_ticker=1,
        display=1,
        sort="date",
        include_ticker_queries=False,
        max_queries_per_ticker=1,
        window_minutes=5,
        publish_to_message_pool=True,
        use_post_count_as_smoke_zscore=False,
        news_score_neutral_proxy=False,
        allow_mock=False,
        internal_fake_naver=True,
        output_dir=tmp_path,
        write_report=False,
    )

    assert report["status"] == "PASS"
    assert report["community_source_health"]["status"] == "WARN"
    assert report["community_source_health"]["provider_coverage"]["status"] == "WARN"
    assert report["community_source_health"]["provider_coverage"]["missing_requested_providers"] == ["blog"]


def test_community_live_risk_smoke_skips_fda_without_actionable_risk(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMUNITY_SCRAPE_ENABLED", raising=False)
    script = _load_script("community_live_risk_smoke")

    def neutral_fake_get_json(url, params, headers=None):
        return {
            "items": [
                {
                    "title": "중립 커뮤니티 반응",
                    "description": "특별한 방향성 없이 관찰 중입니다.",
                    "link": "https://example.com/cafe/neutral",
                    "cafename": "주식토론",
                }
            ]
        }

    monkeypatch.setattr(script, "_fake_naver_get_json", neutral_fake_get_json)

    report = script.run_community_live_risk_smoke(
        tickers=["005930"],
        providers=["cafearticle"],
        query_suffixes=["주식"],
        max_posts_per_ticker=1,
        display=1,
        sort="date",
        include_ticker_queries=False,
        max_queries_per_ticker=1,
        window_minutes=5,
        publish_to_message_pool=True,
        use_post_count_as_smoke_zscore=False,
        news_score_neutral_proxy=False,
        allow_mock=False,
        internal_fake_naver=True,
        output_dir=tmp_path,
        write_report=False,
    )

    assert report["status"] == "PASS"
    assert report["fda"]["status"] == "SKIP_NO_ACTIONABLE_RISK"
    assert report["fda"]["reason_code"] is None
    assert report["fda"]["actionable_risk"] is False


def test_community_live_risk_smoke_cli_defaults_to_strict_real_zscore():
    script = _load_script("community_live_risk_smoke")

    args = script.build_parser().parse_args(["--tickers", "005930"])
    assert args.strict_real_zscore is False
    assert args.use_smoke_post_count_zscore is False

    opt_in_args = script.build_parser().parse_args(
        ["--tickers", "005930", "--use-smoke-post-count-zscore"]
    )
    assert opt_in_args.use_smoke_post_count_zscore is True


def test_naver_datalab_attention_smoke_internal_fake(tmp_path):
    script = _load_script("naver_datalab_attention_smoke")

    report = script.run_naver_datalab_attention_smoke(
        tickers=["005930", "000660", "042700"],
        start_date="2026-05-01",
        end_date="2026-05-15",
        time_unit="date",
        internal_fake_naver=True,
        output_dir=tmp_path,
        write_report=False,
    )

    assert report["status"] == "PASS"
    assert report["ratio_is_relative"] is True
    assert report["metrics"]["attention_ratio_rows"] > 0
    assert set(report["metrics"]["latest_ratio_by_ticker"]) >= {"005930", "000660"}


def test_community_source_health_standalone_from_smoke(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMUNITY_SCRAPE_ENABLED", raising=False)
    smoke = _load_script("community_live_risk_smoke")
    health = _load_script("community_source_health")

    smoke_report = smoke.run_community_live_risk_smoke(
        tickers=["005930"],
        providers=["cafearticle", "blog"],
        query_suffixes=["주식", "급락"],
        max_posts_per_ticker=4,
        display=2,
        sort="date",
        include_ticker_queries=False,
        max_queries_per_ticker=2,
        window_minutes=5,
        publish_to_message_pool=True,
        use_post_count_as_smoke_zscore=True,
        news_score_neutral_proxy=True,
        allow_mock=False,
        internal_fake_naver=True,
        output_dir=tmp_path,
        write_report=True,
    )
    report = health.build_community_source_health(
        from_report=Path(smoke_report["report_path"]),
        output_dir=tmp_path,
        write_report=False,
    )

    assert report["status"] == "WARN"
    assert report["raw_count"] > 0
    assert report["valid_event_count"] > 0
    assert report["compliance"]["stores_raw_content"] is False
    assert report["risk_sidecar"]["community_live_risk_ready"] is True
    assert "NEWS_COMMUNITY_DIVERGENCE" in report["risk_sidecar"]["reason_codes_emitted"]


def test_source_scope_summary_reports_current_gap(tmp_path):
    script = _load_script("source_scope_summary")
    script.ROOT = tmp_path
    bundle_id = "BUNDLE-20260518-195M0001"

    _write_json(
        tmp_path / "artifacts/reports/build_news_dart_archive/build_news_dart_archive_fixture.json",
        {
            "status": "PASS",
            "date_count": 2,
            "ticker_count": 30,
            "total_events": 12,
            "zero_event_date_count": 0,
        },
    )
    _write_json(
        tmp_path / "artifacts/reports/dual_source_history/materialize_dual_source_history_fixture.json",
        {
            "status": "PASS",
            "date_count": 2,
            "files_written": ["day-1.json", "day-2.json"],
            "per_date": [
                {
                    "score_count": 30,
                    "non_neutral": True,
                    "source_stats": {
                        "news_event_count": 12,
                        "community_event_count": 0,
                    },
                }
            ],
            "coverage": {
                "dual_source_non_neutral_date_coverage": 1.0,
                "min_dual_source_non_neutral_date_coverage": 0.8,
            },
        },
    )
    _write_json(
        tmp_path / f"artifacts/bundles/{bundle_id}/lgbm/latest_model_metadata.json",
        {
            "version": "test-model",
            "feature_cols": ["news_score_t", "us_sp500_change"],
        },
    )
    implementation = tmp_path / "new/scripts/community_live_risk_smoke.py"
    implementation.parent.mkdir(parents=True, exist_ok=True)
    implementation.write_text("# synthetic fixture\n", encoding="utf-8")

    report = script.build_source_scope_summary(
        bundle_id=bundle_id,
        output_dir=tmp_path,
        write_report=False,
    )

    assert report["status"] == "PASS"
    assert report["news_dart_archive"]["total_events"] > 0
    assert report["dual_source_history"]["source_totals"]["community_event_count"] == 0
    assert "feature_count" in report["selected_model"]
    assert report["cold_path"]["uses_community_risk"] is True
