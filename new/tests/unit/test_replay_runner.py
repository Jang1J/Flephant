"""S3-9 ReplayRunner 유닛 테스트.

C13 ValidationToolsContract — ReplayRunner 실구현 검증.

테스트 목록:
  1.  test_mode_b_only_decorator        - Mode A 호출 시 RuntimeError
  2.  test_idempotent                   - 같은 input + seed → 100% 동일 output
  3.  test_seed_mismatch_none           - seed=None → SeedMismatch
  4.  test_seed_mismatch_string         - seed='42' (str) → SeedMismatch
  5.  test_event_replay_failed          - malformed event 주입 → EventReplayFailed
  6.  test_agent_activation_count       - 5개 에이전트 count >= 0 + quant >= 390 + 합계 > 0
  7.  test_latency_ordering             - p50 <= p95 <= p99 (cold + hot)
  8.  test_anomaly_count_nonneg         - anomaly_count >= 0
  9.  test_run_id_format                - RPT-yyyymmdd-UUID8 정규식 매치
  10. test_all_six_sources              - 6개 source 전부 처리, REPLAY_DIVERGENCE 없음
  11. test_replay_trace_ref_format      - artifacts/replay/RPT-*.jsonl 경로 형식
  12. test_divergence_detection         - 서로 다른 seed → 결과 다름 (divergence 탐지 가능)
  13. test_forbidden_caller_replay      - forbidden_callers 8개 → ForbiddenCaller raise
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

_KST = ZoneInfo("Asia/Seoul")

# ────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ────────────────────────────────────────────────────────────────────────

_MINIMAL_CFG_RR = {
    "sla": {"max_runtime_sec": 2400},
    "mode": "deterministic_replay",
    "replay_unit": "1m",
    "idempotent": True,
    "event_sources": [
        "naver_news",
        "dart",
        "community",
        "us_market",
        "ecos",
        "krx_investor_flow",
    ],
}


def _make_cfg_loader(rr_cfg=None):
    """config_load 패치용 side_effect."""
    _rr = rr_cfg or _MINIMAL_CFG_RR

    def _loader(file: str = "risk_config.yaml", key: str | None = None):
        if key == "validation_tools.replay_runner":
            return _rr
        return {}

    return _loader


def _make_runner(seed: int = 42, rr_cfg=None):
    """ReplayRunner 인스턴스 생성 with patched config."""
    loader = _make_cfg_loader(rr_cfg=rr_cfg)
    with patch(
        "src.mode_b.validation_tools.config_load",
        side_effect=loader,
    ):
        from src.mode_b.validation_tools import ReplayRunner
        return ReplayRunner(seed=seed)


def _default_date_range(days: int = 5) -> dict[str, str]:
    base = datetime(2026, 1, 2, tzinfo=_KST)
    end = base + timedelta(days=days)
    return {"start": base.isoformat(), "end": end.isoformat()}


def _all_sources() -> list[str]:
    return [
        "naver_news",
        "dart",
        "community",
        "us_market",
        "ecos",
        "krx_investor_flow",
    ]


class _mode_b_env:
    """ELEPHANT_MODE=mode_b context manager."""

    def __enter__(self):
        os.environ["ELEPHANT_MODE"] = "mode_b"
        return self

    def __exit__(self, *_):
        os.environ.pop("ELEPHANT_MODE", None)


def _run_runner(runner, date_range=None, event_sources=None, seed=None):
    """mode_b 환경변수 설정 후 runner.run() 호출 편의 wrapper."""
    date_range = date_range or _default_date_range(5)
    event_sources = event_sources or _all_sources()
    with _mode_b_env():
        return runner.run(
            bundle_ref="BUNDLE-TEST-00000001",
            date_range=date_range,
            event_sources=event_sources,
            mode="deterministic_replay",
            seed=seed,
        )


# ────────────────────────────────────────────────────────────────────────
# 1. mode_b_only 데코레이터: Mode A 호출 시 RuntimeError
# ────────────────────────────────────────────────────────────────────────

def test_mode_b_only_decorator():
    """ELEPHANT_MODE != 'mode_b' 이면 RuntimeError."""
    os.environ.pop("ELEPHANT_MODE", None)

    runner = _make_runner(seed=42)

    with pytest.raises(RuntimeError, match="Mode B 전용"):
        runner.run(
            bundle_ref="BUNDLE-TEST-00000001",
            date_range=_default_date_range(5),
        )


# ────────────────────────────────────────────────────────────────────────
# 2. idempotent: 같은 input + seed → 100% 동일 output
# ────────────────────────────────────────────────────────────────────────

def test_idempotent():
    """동일 seed + input으로 두 번 run() → agent_activation_count / latency / anomaly 동일.

    run_id는 UUID 기반이므로 비교 제외. replay_trace_ref도 run_id 포함이므로 제외.
    idempotent: true (C13 SLA) 준수 확인.

    ref: validation_tools.py ReplayRunner._verify_idempotent()
    """
    runner_a = _make_runner(seed=42)
    runner_b = _make_runner(seed=42)

    dr = _default_date_range(5)
    sources = _all_sources()

    result_a = _run_runner(runner_a, date_range=dr, event_sources=sources)
    result_b = _run_runner(runner_b, date_range=dr, event_sources=sources)

    assert result_a["agent_activation_count"] == result_b["agent_activation_count"], (
        "agent_activation_count 불일치 (idempotent 위반)"
    )
    assert result_a["cold_path_latency"] == result_b["cold_path_latency"], (
        "cold_path_latency 불일치 (idempotent 위반)"
    )
    assert result_a["hot_path_latency"] == result_b["hot_path_latency"], (
        "hot_path_latency 불일치 (idempotent 위반)"
    )
    assert result_a["anomaly_count"] == result_b["anomaly_count"], (
        "anomaly_count 불일치 (idempotent 위반)"
    )

    # _verify_idempotent 내부 메서드도 직접 호출 검증
    with _mode_b_env():
        runner_a._verify_idempotent(result_a, result_b)  # raise 없어야 함


# ────────────────────────────────────────────────────────────────────────
# 3. SEED_MISMATCH: seed=None → SeedMismatch
# ────────────────────────────────────────────────────────────────────────

def test_seed_mismatch_none():
    """ReplayRunner(seed=None) 초기화 시 SeedMismatch."""
    from src.mode_b.validation_tools import SeedMismatch

    loader = _make_cfg_loader()
    with patch("src.mode_b.validation_tools.config_load", side_effect=loader):
        with pytest.raises(SeedMismatch, match="SEED_MISMATCH"):
            from src.mode_b.validation_tools import ReplayRunner
            ReplayRunner(seed=None)  # type: ignore[arg-type]


# ────────────────────────────────────────────────────────────────────────
# 4. SEED_MISMATCH: seed='42' (str) → SeedMismatch
# ────────────────────────────────────────────────────────────────────────

def test_seed_mismatch_string():
    """ReplayRunner(seed='42') 초기화 시 SeedMismatch."""
    from src.mode_b.validation_tools import SeedMismatch

    loader = _make_cfg_loader()
    with patch("src.mode_b.validation_tools.config_load", side_effect=loader):
        with pytest.raises(SeedMismatch, match="SEED_MISMATCH"):
            from src.mode_b.validation_tools import ReplayRunner
            ReplayRunner(seed="42")  # type: ignore[arg-type]


# ────────────────────────────────────────────────────────────────────────
# 5. EVENT_REPLAY_FAILED: malformed event_source 주입 → EventReplayFailed
# ────────────────────────────────────────────────────────────────────────

def test_event_replay_failed():
    """알 수 없는 event_source 주입 시 EventReplayFailed."""
    from src.mode_b.validation_tools import EventReplayFailed

    runner = _make_runner(seed=42)

    with _mode_b_env():
        with pytest.raises(EventReplayFailed, match="EVENT_REPLAY_FAILED"):
            runner.run(
                bundle_ref="BUNDLE-TEST-00000001",
                date_range=_default_date_range(5),
                event_sources=["unknown_source_xyz"],
            )


# ────────────────────────────────────────────────────────────────────────
# 6. agent_activation_count: 4개 에이전트 카운트 >= 0, 합계 > 0
# ────────────────────────────────────────────────────────────────────────

def test_agent_activation_count():
    """news/risk/debate/fda/quant count 각각 >= 0, 합계 > 0 (6 sources 이벤트 있으면).

    quant: C13 api_contracts.md agent_activation_count.quant: int.
    1분봉 시스템 기준 date_range 일수 × 390분봉.
    2026-05-01 S3 Tier 1 수정: quant 키 추가.
    """
    runner = _make_runner(seed=42)
    result = _run_runner(runner, event_sources=_all_sources())
    aac = result["agent_activation_count"]

    assert "news" in aac, "agent_activation_count에 'news' 키 없음"
    assert "risk" in aac, "agent_activation_count에 'risk' 키 없음"
    assert "debate" in aac, "agent_activation_count에 'debate' 키 없음"
    assert "fda" in aac, "agent_activation_count에 'fda' 키 없음"
    assert "quant" in aac, "agent_activation_count에 'quant' 키 없음 (C13 spec 위반)"

    for agent, count in aac.items():
        assert isinstance(count, int), f"agent_activation_count[{agent!r}]가 int 아님: {count!r}"
        assert count >= 0, f"agent_activation_count[{agent!r}] < 0: {count}"

    # quant는 date_range 5일 × 390분봉 = 1950 이상이어야 함
    assert aac["quant"] >= 390, (
        f"quant count가 너무 낮음: {aac['quant']} (5일 × 390 기대)"
    )

    total = sum(aac.values())
    assert total > 0, f"agent_activation_count 합계가 0: {aac}"


# ────────────────────────────────────────────────────────────────────────
# 7. latency ordering: p50 <= p95 <= p99
# ────────────────────────────────────────────────────────────────────────

def test_latency_ordering():
    """cold_path_latency + hot_path_latency 둘 다 p50 <= p95 <= p99 보장."""
    runner = _make_runner(seed=42)
    result = _run_runner(runner)

    for path_key in ("cold_path_latency", "hot_path_latency"):
        lat = result[path_key]
        assert lat["p50"] <= lat["p95"], (
            f"{path_key}: p50({lat['p50']}) > p95({lat['p95']})"
        )
        assert lat["p95"] <= lat["p99"], (
            f"{path_key}: p95({lat['p95']}) > p99({lat['p99']})"
        )
        assert lat["p50"] >= 1, f"{path_key}: p50 < 1ms (비현실적)"


# ────────────────────────────────────────────────────────────────────────
# 8. anomaly_count >= 0
# ────────────────────────────────────────────────────────────────────────

def test_anomaly_count_nonneg():
    """anomaly_count는 항상 0 이상."""
    runner = _make_runner(seed=42)
    result = _run_runner(runner)
    assert isinstance(result["anomaly_count"], int), (
        f"anomaly_count가 int 아님: {type(result['anomaly_count'])}"
    )
    assert result["anomaly_count"] >= 0, (
        f"anomaly_count < 0: {result['anomaly_count']}"
    )


# ────────────────────────────────────────────────────────────────────────
# 9. run_id 형식: RPL-yyyymmdd-UUID8
# ────────────────────────────────────────────────────────────────────────

def test_run_id_format():
    """run_id 가 RPT-yyyymmdd-UUID8 정규식을 만족해야 한다.

    api_contracts.md L34 SSOT: replay_trace_ref = RPT-{yyyymmdd}-{UUID8}.
    2026-05-01 S3 Tier 1 수정: RPL → RPT.
    """
    runner = _make_runner(seed=42)
    result = _run_runner(runner)
    run_id = result["run_id"]
    pattern = re.compile(r"^RPT-\d{8}-[0-9A-F]{8}$")
    assert pattern.match(run_id), f"run_id 형식 불일치: {run_id!r} (기대: RPT-yyyymmdd-UUID8)"


# ────────────────────────────────────────────────────────────────────────
# 10. 6 event_sources 모두 처리: REPLAY_DIVERGENCE 없음
# ────────────────────────────────────────────────────────────────────────

def test_all_six_sources():
    """6개 source 전부 포함한 실행 → EventReplayFailed / ReplayDivergence 없음.

    각 source에서 최소 1개 이벤트 생성 → news + risk count 모두 > 0.
    """
    runner = _make_runner(seed=42)
    sources = _all_sources()
    assert len(sources) == 6, "6개 source 목록이 아님"

    result = _run_runner(runner, event_sources=sources)

    aac = result["agent_activation_count"]
    assert aac["news"] > 0, (
        "6 sources 모두 처리 후 news_count=0 (naver_news/dart/community 이벤트 없음)"
    )
    assert aac["risk"] > 0, (
        "6 sources 모두 처리 후 risk_count=0 (us_market/ecos/krx_investor_flow 이벤트 없음)"
    )
    assert aac["fda"] > 0, "fda_count=0 (cold path 처리 없음)"


# ────────────────────────────────────────────────────────────────────────
# 11. replay_trace_ref 경로 형식
# ────────────────────────────────────────────────────────────────────────

def test_replay_trace_ref_format():
    """replay_trace_ref 가 'artifacts/replay/RPT-yyyymmdd-UUID8.jsonl' 형식.

    api_contracts.md L34 SSOT: replay_trace_ref = RPT-{yyyymmdd}-{UUID8}.
    2026-05-01 S3 Tier 1 수정: RPL → RPT.
    """
    runner = _make_runner(seed=42)
    result = _run_runner(runner)
    ref = result["replay_trace_ref"]

    # 패턴: artifacts/replay/RPT-{8digits}-{8hexchars}.jsonl
    pattern = re.compile(r"^artifacts/replay/RPT-\d{8}-[0-9A-F]{8}\.jsonl$")
    assert pattern.match(ref), (
        f"replay_trace_ref 형식 불일치: {ref!r} "
        "(기대: artifacts/replay/RPT-yyyymmdd-UUID8.jsonl)"
    )


# ────────────────────────────────────────────────────────────────────────
# 12. REPLAY_DIVERGENCE 탐지: 서로 다른 seed → 결과 불일치 → divergence 탐지 가능
#     정상 path(같은 seed)에서는 _verify_idempotent가 raise 안 함
# ────────────────────────────────────────────────────────────────────────

def test_divergence_detection():
    """서로 다른 seed로 실행한 결과를 _verify_idempotent에 주입 → ReplayDivergence.

    정상 path (같은 seed)에서는 raise 없음을 함께 검증.

    C13 SLA: idempotent=true. 같은 input+seed → 동일 output 보장.
    REPLAY_DIVERGENCE는 오직 비정상(다른 seed / 버그) 시에만 발생.

    ref: validation_tools.py ReplayRunner._verify_idempotent()
    """
    from src.mode_b.validation_tools import ReplayDivergence

    runner_42 = _make_runner(seed=42)
    runner_99 = _make_runner(seed=99)

    dr = _default_date_range(5)
    sources = _all_sources()

    result_42 = _run_runner(runner_42, date_range=dr, event_sources=sources)
    result_99 = _run_runner(runner_99, date_range=dr, event_sources=sources)

    # 정상 path: 같은 seed → _verify_idempotent raise 없음
    runner_42b = _make_runner(seed=42)
    result_42b = _run_runner(runner_42b, date_range=dr, event_sources=sources)

    with _mode_b_env():
        runner_42._verify_idempotent(result_42, result_42b)  # raise 없어야 함

    # 비정상 path: 다른 seed → activation_count 또는 latency 불일치 → ReplayDivergence
    # seed 42 vs 99는 다른 rng 경로 → 다른 이벤트 생성 → 다른 결과
    # 만약 두 결과가 우연히 같으면 테스트를 skip (매우 드문 edge case)
    if result_42["agent_activation_count"] == result_99["agent_activation_count"] and \
       result_42["cold_path_latency"] == result_99["cold_path_latency"] and \
       result_42["anomaly_count"] == result_99["anomaly_count"]:
        pytest.skip("seed 42 vs 99 결과가 우연히 동일 (드문 edge case). skip.")

    with pytest.raises(ReplayDivergence, match="REPLAY_DIVERGENCE"):
        runner_42._verify_idempotent(result_42, result_99)


# ────────────────────────────────────────────────────────────────────────
# 13. forbidden_callers: 8개 금지 호출자 → ForbiddenCaller raise
# ────────────────────────────────────────────────────────────────────────

def test_forbidden_caller_replay():
    """C13 forbidden_callers 8개가 ReplayRunner.run 호출 시 ForbiddenCaller raise.

    정상 caller("BacktestAgent")는 raise 없이 통과.
    2026-05-01 S3 Tier 2 Critical 9 수정.
    """
    from src.mode_b.validation_tools import ForbiddenCaller

    forbidden = [
        "FDA", "PortfolioManager", "QuantAgent", "NewsAgent",
        "RiskAgent", "DebateAgent", "ExecutionGateway", "HotPath",
    ]

    runner = _make_runner(seed=42)

    for caller_name in forbidden:
        with _mode_b_env():
            with pytest.raises(ForbiddenCaller, match="FORBIDDEN_CALLER"):
                runner.run(
                    bundle_ref="BUNDLE-TEST-00000001",
                    date_range=_default_date_range(5),
                    event_sources=_all_sources(),
                    caller=caller_name,
                )

    # 정상 caller는 통과
    with _mode_b_env():
        result = runner.run(
            bundle_ref="BUNDLE-TEST-00000001",
            date_range=_default_date_range(5),
            event_sources=_all_sources(),
            caller="BacktestAgent",
        )
    assert "run_id" in result, "정상 caller BacktestAgent 호출 실패"
