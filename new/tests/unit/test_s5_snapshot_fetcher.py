"""Sprint 5 S5-1 WatchSnapshotFetcher 단위 테스트.

C16 WatchUniverseSnapshotContract 구현 검증.

테스트 케이스:
  1. test_load_watch_tickers_excludes_trade_universe
  2. test_fetch_once_returns_c16_output_schema
  3. test_fetch_once_writes_jsonl_file
  4. test_fetch_once_pit_safety_violation
  5. test_fetch_once_pit_safety_skip_env
  6. test_fetch_once_cache_hit_skips_kis
  7. test_get_price_snapshot_mock_mode_returns_n_tickers (선택)
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
import yaml

from src.dynamic_universe.snapshot_fetcher import WatchSnapshotFetcher
from src.utils.pit_guard import PITViolationError

_KST = ZoneInfo("Asia/Seoul")


# ================================================================
# 헬퍼: 최소 watch_universe yaml (200개 모사 — 실제로는 소수만 사용)
# ================================================================

def _make_watch_yaml(tickers: list[str], exclude_trade: bool = True) -> dict:
    return {
        "version": "0.1.0",
        "watch_rules": {
            "exclude_trade_universe": exclude_trade,
        },
        "tickers": [{"ticker": t, "name": f"종목{t}"} for t in tickers],
    }


def _make_universe_yaml(active_tickers: list[str]) -> dict:
    """universe_config.yaml 모사. active 종목 20개를 sectors에 포함."""
    stocks = [{"ticker": t, "name": f"종목{t}", "status": "active"} for t in active_tickers]
    return {
        "sectors": {
            "반도체": {"status": "confirmed", "stocks": stocks},
        }
    }


def _make_dynamic_cfg(ttl_sec: int = 55) -> dict:
    return {
        "snapshot_cache": {"ttl_sec": ttl_sec, "max_entries": 1000},
        "exit": {"ttl_sec": 1800},
    }


def _make_kis_client(tickers_response: list[dict] | None = None) -> MagicMock:
    """KIS mock 클라이언트. get_price_snapshot 반환값 설정 가능."""
    client = MagicMock()
    if tickers_response is None:
        tickers_response = []
    client.get_price_snapshot.return_value = tickers_response
    return client


# ================================================================
# 1. exclude_trade_universe=true → trade universe 종목 제외
# ================================================================

def test_load_watch_tickers_excludes_trade_universe(tmp_path: Path) -> None:
    """watch_rules.exclude_trade_universe=true 시 trade universe(active) 종목 제외.

    watch 200개 - trade 19개 = 181개.
    (정확한 수치는 yaml fixture 기준으로 검증.)
    """
    # watch: 200개 (000001~000200)
    watch_tickers = [str(i).zfill(6) for i in range(1, 201)]
    # trade universe: 19개 (000001~000019)
    trade_tickers = [str(i).zfill(6) for i in range(1, 20)]

    watch_yaml_path = tmp_path / "watch_universe_kospi200.yaml"
    universe_yaml_path = tmp_path / "universe_config.yaml"
    dynamic_cfg_path = tmp_path / "dynamic_universe_config.yaml"
    snapshot_dir = tmp_path / "snapshots"

    with watch_yaml_path.open("w", encoding="utf-8") as fh:
        yaml.dump(_make_watch_yaml(watch_tickers, exclude_trade=True), fh)
    with universe_yaml_path.open("w", encoding="utf-8") as fh:
        yaml.dump(_make_universe_yaml(trade_tickers), fh)
    with dynamic_cfg_path.open("w", encoding="utf-8") as fh:
        yaml.dump(_make_dynamic_cfg(), fh)

    kis = _make_kis_client([])

    # universe_config.yaml 경로를 monkeypatch 없이 직접 교체하기 위해
    # _UNIVERSE_CONFIG_PATH 모듈 변수를 패치
    with patch(
        "src.dynamic_universe.snapshot_fetcher._UNIVERSE_CONFIG_PATH",
        universe_yaml_path,
    ):
        fetcher = WatchSnapshotFetcher(
            kis_client=kis,
            watch_universe_path=watch_yaml_path,
            dynamic_config_path=dynamic_cfg_path,
            snapshot_dir=snapshot_dir,
        )

    # 200 - 19 = 181
    assert len(fetcher._watch_tickers) == 181
    for t in trade_tickers:
        assert t not in fetcher._watch_tickers


# ================================================================
# 2. fetch_once C16 출력 스키마 검증
# ================================================================

def test_fetch_once_returns_c16_output_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mock KIS → fetch_once 반환 dict가 C16 스키마 준수.

    필수 필드: watch_snapshot_id, ts, snapshots (list[dict])
    """
    monkeypatch.setenv("ELEPHANT_TEST_PIT_SKIP", "true")

    watch_tickers = ["005930", "000660"]
    watch_yaml_path = tmp_path / "watch_universe_kospi200.yaml"
    dynamic_cfg_path = tmp_path / "dynamic_universe_config.yaml"
    snapshot_dir = tmp_path / "snapshots"

    with watch_yaml_path.open("w", encoding="utf-8") as fh:
        yaml.dump(_make_watch_yaml(watch_tickers, exclude_trade=False), fh)
    with dynamic_cfg_path.open("w", encoding="utf-8") as fh:
        yaml.dump(_make_dynamic_cfg(), fh)

    now_str = datetime.now(_KST).isoformat()
    kis_response = [
        {"ticker": "005930", "ts": now_str, "last_price": 70000,
         "day_change_pct": 0.01, "volume": 5000, "turnover": 350000000.0},
        {"ticker": "000660", "ts": now_str, "last_price": 120000,
         "day_change_pct": -0.005, "volume": 3000, "turnover": 360000000.0},
    ]
    kis = _make_kis_client(kis_response)

    fetcher = WatchSnapshotFetcher(
        kis_client=kis,
        watch_universe_path=watch_yaml_path,
        dynamic_config_path=dynamic_cfg_path,
        snapshot_dir=snapshot_dir,
    )
    result = fetcher.fetch_once()

    assert "watch_snapshot_id" in result
    assert result["watch_snapshot_id"].startswith("WS-")
    assert "ts" in result
    assert "snapshots" in result
    assert isinstance(result["snapshots"], list)


# ================================================================
# 3. fetch_once → jsonl 파일 작성 확인
# ================================================================

def test_fetch_once_writes_jsonl_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """artifacts/watch_snapshots/YYYYMMDD.jsonl 작성 확인."""
    monkeypatch.setenv("ELEPHANT_TEST_PIT_SKIP", "true")

    watch_tickers = ["005930"]
    watch_yaml_path = tmp_path / "watch_universe_kospi200.yaml"
    dynamic_cfg_path = tmp_path / "dynamic_universe_config.yaml"
    snapshot_dir = tmp_path / "watch_snapshots"

    with watch_yaml_path.open("w", encoding="utf-8") as fh:
        yaml.dump(_make_watch_yaml(watch_tickers, exclude_trade=False), fh)
    with dynamic_cfg_path.open("w", encoding="utf-8") as fh:
        yaml.dump(_make_dynamic_cfg(), fh)

    now_str = datetime.now(_KST).isoformat()
    kis_response = [
        {"ticker": "005930", "ts": now_str, "last_price": 70000,
         "day_change_pct": 0.01, "volume": 5000, "turnover": 350000000.0},
    ]
    kis = _make_kis_client(kis_response)

    fetcher = WatchSnapshotFetcher(
        kis_client=kis,
        watch_universe_path=watch_yaml_path,
        dynamic_config_path=dynamic_cfg_path,
        snapshot_dir=snapshot_dir,
    )
    fetcher.fetch_once()

    date_str = datetime.now(_KST).strftime("%Y%m%d")
    expected_file = snapshot_dir / f"{date_str}.jsonl"
    assert expected_file.exists(), f"jsonl 파일 없음: {expected_file}"

    with expected_file.open("r", encoding="utf-8") as fh:
        line = fh.readline().strip()
    parsed = json.loads(line)
    assert "watch_snapshot_id" in parsed
    assert "ts" in parsed
    assert "snapshots" in parsed


# ================================================================
# 4. PIT-Safety 위반: 미래 ts 주입 시 PITViolationError
# ================================================================

def test_fetch_once_pit_safety_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ELEPHANT_TEST_PIT_SKIP unset 상태 + 미래 ts → PITViolationError.

    is_pit_safe를 patch해서 False 강제.
    """
    monkeypatch.delenv("ELEPHANT_TEST_PIT_SKIP", raising=False)

    watch_tickers = ["005930"]
    watch_yaml_path = tmp_path / "watch_universe_kospi200.yaml"
    dynamic_cfg_path = tmp_path / "dynamic_universe_config.yaml"
    snapshot_dir = tmp_path / "snapshots"

    with watch_yaml_path.open("w", encoding="utf-8") as fh:
        yaml.dump(_make_watch_yaml(watch_tickers, exclude_trade=False), fh)
    with dynamic_cfg_path.open("w", encoding="utf-8") as fh:
        yaml.dump(_make_dynamic_cfg(), fh)

    kis = _make_kis_client([])

    fetcher = WatchSnapshotFetcher(
        kis_client=kis,
        watch_universe_path=watch_yaml_path,
        dynamic_config_path=dynamic_cfg_path,
        snapshot_dir=snapshot_dir,
    )

    # is_pit_safe 를 False 로 패치 → PITViolationError 발생 유도
    with patch("src.dynamic_universe.snapshot_fetcher.is_pit_safe", return_value=False):
        with pytest.raises(PITViolationError):
            fetcher.fetch_once()


# ================================================================
# 5. PIT-Safety 우회: ELEPHANT_TEST_PIT_SKIP=true
# ================================================================

def test_fetch_once_pit_safety_skip_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ELEPHANT_TEST_PIT_SKIP=true 시 PIT 가드 우회 → 정상 실행."""
    monkeypatch.setenv("ELEPHANT_TEST_PIT_SKIP", "true")

    watch_tickers = ["005930"]
    watch_yaml_path = tmp_path / "watch_universe_kospi200.yaml"
    dynamic_cfg_path = tmp_path / "dynamic_universe_config.yaml"
    snapshot_dir = tmp_path / "snapshots"

    with watch_yaml_path.open("w", encoding="utf-8") as fh:
        yaml.dump(_make_watch_yaml(watch_tickers, exclude_trade=False), fh)
    with dynamic_cfg_path.open("w", encoding="utf-8") as fh:
        yaml.dump(_make_dynamic_cfg(), fh)

    now_str = datetime.now(_KST).isoformat()
    kis_response = [
        {"ticker": "005930", "ts": now_str, "last_price": 70000,
         "day_change_pct": 0.01, "volume": 5000, "turnover": 350000000.0},
    ]
    kis = _make_kis_client(kis_response)

    fetcher = WatchSnapshotFetcher(
        kis_client=kis,
        watch_universe_path=watch_yaml_path,
        dynamic_config_path=dynamic_cfg_path,
        snapshot_dir=snapshot_dir,
    )

    # is_pit_safe False 패치 상태에서도 ELEPHANT_TEST_PIT_SKIP=true이면 통과
    with patch("src.dynamic_universe.snapshot_fetcher.is_pit_safe", return_value=False):
        result = fetcher.fetch_once()

    assert "watch_snapshot_id" in result


# ================================================================
# 6. Cache hit → KIS get_price_snapshot 호출 안 됨
# ================================================================

def test_fetch_once_cache_hit_skips_kis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PersistentCache mock에서 hit 발생 시 KIS get_price_snapshot 호출 0회."""
    monkeypatch.setenv("ELEPHANT_TEST_PIT_SKIP", "true")

    watch_tickers = ["005930", "000660"]
    watch_yaml_path = tmp_path / "watch_universe_kospi200.yaml"
    dynamic_cfg_path = tmp_path / "dynamic_universe_config.yaml"
    snapshot_dir = tmp_path / "snapshots"

    with watch_yaml_path.open("w", encoding="utf-8") as fh:
        yaml.dump(_make_watch_yaml(watch_tickers, exclude_trade=False), fh)
    with dynamic_cfg_path.open("w", encoding="utf-8") as fh:
        yaml.dump(_make_dynamic_cfg(), fh)

    now_str = datetime.now(_KST).isoformat()
    # cache hit 반환값 설정
    cached_005930 = {
        "ticker": "005930", "ts": now_str,
        "last_price": 70000, "day_change_pct": 0.01,
        "volume": 5000, "turnover": 350000000.0,
    }
    cached_000660 = {
        "ticker": "000660", "ts": now_str,
        "last_price": 120000, "day_change_pct": -0.005,
        "volume": 3000, "turnover": 360000000.0,
    }

    mock_cache = MagicMock()
    # cache.get(key) → ticker 에 따라 값 반환
    def cache_get_side_effect(key: str):
        if "005930" in key:
            return cached_005930
        if "000660" in key:
            return cached_000660
        return None

    mock_cache.get.side_effect = cache_get_side_effect

    kis = _make_kis_client([])

    fetcher = WatchSnapshotFetcher(
        kis_client=kis,
        watch_universe_path=watch_yaml_path,
        dynamic_config_path=dynamic_cfg_path,
        cache=mock_cache,
        snapshot_dir=snapshot_dir,
    )
    result = fetcher.fetch_once()

    # KIS 호출 0회 (전량 cache hit)
    kis.get_price_snapshot.assert_not_called()

    # 결과에 2개 종목 포함
    assert len(result["snapshots"]) == 2


# ================================================================
# 7. KIS REST mock 모드: N종목 요청 → N개 dict 반환
# ================================================================

def test_get_price_snapshot_mock_mode_returns_n_tickers() -> None:
    """KIS REST mock 모드에서 N종목 요청 시 N개 dict 반환 + 필수 필드 존재."""
    import os
    os.environ["KIS_MODE"] = "mock"

    from src.connectors.kis_rest import KISRestClient

    client = KISRestClient()
    tickers = ["005930", "000660", "042700"]
    result = client.get_price_snapshot(tickers)

    assert len(result) == 3
    for item in result:
        assert "ticker" in item
        assert "ts" in item
        assert "last_price" in item
        assert "day_change_pct" in item
        assert "volume" in item
        assert "turnover" in item
        assert item["_mode"] == "mock"
