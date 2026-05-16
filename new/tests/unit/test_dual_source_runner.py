"""DualSourceRunner 단위 테스트. Sprint 4 S4-1 R1-W1 추가 검증.

coverage:
  batch_window_yaml_loaded: 배치 창이 risk_config.yaml에서 로드되는지 확인
  batch_window_default_fallback: 섹션 없을 때 08:00~08:30 fallback
  is_in_batch_window: 시각 경계 조건
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from src.connectors.community import CommunityPost
from src.data import dual_source_runner
from src.data.dual_source_runner import (
    _is_in_batch_window,
    _load_active_tickers,
    _load_batch_window,
)

_KST = ZoneInfo("Asia/Seoul")


# =========================================================
# batch_window yaml 로드 검증
# =========================================================


def test_batch_window_yaml_loaded() -> None:
    """_load_batch_window()가 risk_config.yaml dual_source.score_build_window 값 반환."""
    sh, sm, eh, em = _load_batch_window()
    # risk_config.yaml 기준: start=08:00, end=08:30
    assert sh == 8
    assert sm == 0
    assert eh == 8
    assert em == 30


def test_batch_window_custom_yaml_override() -> None:
    """config_load가 커스텀 값을 반환할 때 그 값을 사용."""
    fake_cfg = {"score_build_window": {"start": "07:30", "end": "07:45"}}
    with patch("src.data.dual_source_runner.config_load", return_value=fake_cfg):
        sh, sm, eh, em = _load_batch_window()
    assert (sh, sm, eh, em) == (7, 30, 7, 45)


def test_batch_window_default_fallback_when_section_missing() -> None:
    """score_build_window 키 없을 때 기본값 08:00~08:30 fallback."""
    fake_cfg: dict = {}  # score_build_window 없음
    with patch("src.data.dual_source_runner.config_load", return_value=fake_cfg):
        sh, sm, eh, em = _load_batch_window()
    assert (sh, sm, eh, em) == (8, 0, 8, 30)


# =========================================================
# _is_in_batch_window 경계 조건
# =========================================================


def test_is_in_batch_window_at_start() -> None:
    """08:00 KST = 창 시작 경계 → True."""
    now = datetime.now(_KST).replace(hour=8, minute=0, second=0, microsecond=0)
    assert _is_in_batch_window(now) is True


def test_is_in_batch_window_at_end() -> None:
    """08:30 KST = 창 종료 경계 → True."""
    now = datetime.now(_KST).replace(hour=8, minute=30, second=0, microsecond=0)
    assert _is_in_batch_window(now) is True


def test_is_in_batch_window_before_start() -> None:
    """07:59 KST = 창 이전 → False."""
    now = datetime.now(_KST).replace(hour=7, minute=59, second=0, microsecond=0)
    assert _is_in_batch_window(now) is False


def test_is_in_batch_window_after_end() -> None:
    """08:31 KST = 창 이후 → False."""
    now = datetime.now(_KST).replace(hour=8, minute=31, second=0, microsecond=0)
    assert _is_in_batch_window(now) is False


# =========================================================
# active universe + real connector path
# =========================================================


def test_load_active_tickers_from_sectors_shape() -> None:
    """현행 universe_config.yaml sectors 구조에서 active ticker를 로드한다."""
    fake_cfg = {
        "sectors": {
            "반도체": {
                "status": "confirmed",
                "stocks": [
                    {"ticker": "5930", "name": "삼성전자", "status": "active"},
                    {"ticker": "000660", "name": "SK하이닉스", "status": "pending"},
                ],
            },
            "미확정": {"status": "pending", "stocks": []},
        }
    }
    with patch("src.data.dual_source_runner.config_load", return_value=fake_cfg):
        assert _load_active_tickers() == ["005930"]


def test_run_dual_source_batch_real_path_filters_future_data(tmp_path) -> None:
    """use_mock=False 경로가 실 connector 입력을 만들고 snapshot 이후 데이터는 제외한다."""
    snapshot = datetime(2026, 5, 8, 8, 30, tzinfo=_KST)

    class FakeNewsClient:
        _is_mock = False

        def search_news(self, query: str):
            return [
                SimpleNamespace(
                    title=f"{query} 과거 뉴스",
                    description="실적 호조",
                    pub_date=snapshot - timedelta(hours=1),
                ),
                SimpleNamespace(
                    title=f"{query} 미래 뉴스",
                    description="사용되면 PIT 위반",
                    pub_date=snapshot + timedelta(minutes=1),
                ),
            ]

    class FakeCommunity:
        _is_mock = False

        def poll(self, tickers, window_minutes=5):
            return [
                CommunityPost(
                    post_id="C1",
                    ticker=tickers[0],
                    author_id="u1",
                    title="커뮤니티 과거",
                    content="매수 기대",
                    timestamp=snapshot - timedelta(minutes=10),
                    url="",
                ),
                CommunityPost(
                    post_id="C2",
                    ticker=tickers[0],
                    author_id="u2",
                    title="커뮤니티 미래",
                    content="사용되면 PIT 위반",
                    timestamp=snapshot + timedelta(minutes=1),
                    url="",
                ),
            ]

    class FakeScorer:
        def score_universe(self, universe, snapshot_ts=None):
            item = universe[0]
            assert len(item["news_texts"]) == 1
            assert len(item["comm_texts_t1"]) == 1
            assert item["data_ts"] <= snapshot.isoformat()
            return [{
                "ticker": item["ticker"],
                "news_score_t": 0.1,
                "comm_score_t_1": 0.2,
                "comm_score_t_2": 0.0,
                "news_comm_divergence": 0.1,
                "community_noise_multiplier": 1.0,
                "source_notes": "fake",
            }]

    with (
        patch.object(
            dual_source_runner,
            "_load_active_universe",
            return_value=[{"ticker": "005930", "name": "삼성전자", "aliases": []}],
        ),
        patch.object(dual_source_runner, "NaverNewsClient", return_value=FakeNewsClient()),
        patch.object(dual_source_runner, "CommunityCrawler", return_value=FakeCommunity()),
        patch.object(dual_source_runner, "DualSourceScorer", return_value=FakeScorer()),
        patch.object(dual_source_runner, "_ARTIFACT_DIR", tmp_path),
    ):
        result = dual_source_runner.run_dual_source_batch(
            snapshot_ts=snapshot,
            use_mock=False,
        )

    assert len(result) == 1
    out_files = list(tmp_path.glob("*.json"))
    assert len(out_files) == 1
    assert out_files[0].name == "20260508.json"
    payload = json.loads(out_files[0].read_text(encoding="utf-8"))
    assert payload["batch_date"] == "2026-05-08"
    assert payload["source_stats"]["input_mode"] == "real"
    assert payload["source_stats"]["per_ticker"]["005930"]["news_count"] == 1
    assert payload["source_stats"]["per_ticker"]["005930"]["community_count"] == 1


def test_run_dual_source_batch_real_path_does_not_mix_connector_mocks(tmp_path) -> None:
    """use_mock=False에서 credentials 부재 mock connector 결과를 실데이터로 섞지 않는다."""
    snapshot = datetime(2026, 5, 8, 8, 30, tzinfo=_KST)

    class MockNewsClient:
        _is_mock = True

        def search_news(self, query: str):
            raise AssertionError("mock news must not be used in real input mode")

    class MockCommunity:
        _is_mock = True

    class FakeScorer:
        def score_universe(self, universe, snapshot_ts=None):
            item = universe[0]
            assert item["news_texts"] == []
            assert item["comm_texts_t1"] == []
            return [{
                "ticker": item["ticker"],
                "news_score_t": 0.0,
                "comm_score_t_1": 0.0,
                "comm_score_t_2": 0.0,
                "news_comm_divergence": 0.0,
                "community_noise_multiplier": 1.0,
                "source_notes": "empty",
            }]

    with (
        patch.object(
            dual_source_runner,
            "_load_active_universe",
            return_value=[{"ticker": "005930", "name": "삼성전자", "aliases": []}],
        ),
        patch.object(dual_source_runner, "NaverNewsClient", return_value=MockNewsClient()),
        patch.object(dual_source_runner, "CommunityCrawler", return_value=MockCommunity()),
        patch.object(dual_source_runner, "DualSourceScorer", return_value=FakeScorer()),
        patch.object(dual_source_runner, "_ARTIFACT_DIR", tmp_path),
    ):
        result = dual_source_runner.run_dual_source_batch(
            snapshot_ts=snapshot,
            use_mock=False,
        )

    assert len(result) == 1
    out_path = next(tmp_path.glob("*.json"))
    assert out_path.name == "20260508.json"
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["batch_date"] == "2026-05-08"
    assert payload["source_stats"]["news_mode"] == "unavailable_empty"
    assert payload["source_stats"]["community_mode"] == "unavailable_empty"


def test_load_latest_scores_preserves_batch_metadata(tmp_path) -> None:
    """QuantAgent가 PIT guard를 걸 수 있도록 score별 배치 메타를 보존한다."""
    payload = {
        "batch_date": "2026-05-08",
        "snapshot_ts": "2026-05-08T08:30:00+09:00",
        "generated_at": "2026-05-08T08:31:00+09:00",
        "scores": [
            {
                "ticker": "005930",
                "news_score_t": 0.1,
                "comm_score_t_1": 0.2,
            }
        ],
    }
    path = tmp_path / "20260508.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with patch.object(dual_source_runner, "_ARTIFACT_DIR", tmp_path):
        scores = dual_source_runner.load_latest_scores("20260508")

    assert scores == [
        {
            "ticker": "005930",
            "news_score_t": 0.1,
            "comm_score_t_1": 0.2,
            "batch_date": "2026-05-08",
            "snapshot_ts": "2026-05-08T08:30:00+09:00",
            "generated_at": "2026-05-08T08:31:00+09:00",
        }
    ]
