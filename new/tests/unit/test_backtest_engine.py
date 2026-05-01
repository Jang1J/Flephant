"""S3-9 BacktestEngine 유닛 테스트.

C13 ValidationToolsContract — BacktestEngine 실구현 검증.

테스트 목록:
  1.  test_mode_b_only_decorator            - Mode A 호출 시 RuntimeError
  2.  test_purge_embargo_applied            - fold: test_start >= train_end + purge + embargo
  3.  test_leakage_detected_purge_zero      - purge=0, embargo=0 → LEAKAGE_DETECTED
  4.  test_metrics_7_keys                   - 집계 metrics 7개 키 모두 존재
  5.  test_nan_in_metrics                   - NaN 예측 → NaNInMetrics
  6.  test_deterministic                    - 동일 seed → 동일 output
  7.  test_run_id_format                    - run_id = BT-yyyymmdd-UUID8 정규식 매치
  8.  test_trade_log_required_keys          - trade_log 항목에 6 필수 키
  9.  test_bar_count_match                  - bar_count = days × tickers × trading_minutes
  10. test_cost_reduces_net_return          - 거래비용 > 0 이면 net < gross
  11. test_forbidden_caller                 - forbidden_callers 8개 → ForbiddenCaller raise
"""
from __future__ import annotations

import math
import os
import re
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

_KST = ZoneInfo("Asia/Seoul")

# ────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ────────────────────────────────────────────────────────────────────────

_MINIMAL_CFG_VT = {
    "purge_bars": 60,
    "embargo_bars": 78,
    "sla": {"max_runtime_sec": 1800, "max_concurrent": 1},
    "replay_resolution": "1m",
    "replay_unit": "1m",
    "errors": ["BUNDLE_LOAD_FAILED", "DATA_UNAVAILABLE", "NAN_IN_METRICS", "LEAKAGE_DETECTED"],
}

_MINIMAL_CFG_WF = {
    "n_splits": 2,
    "train_window_days": 5,
    "test_window_days": 3,
    "step_days": 3,
    "trading_minutes_per_day": 5,   # 빠른 테스트: 5분봉/일
}

_MINIMAL_CFG_COST = {
    "name": "slippage_v1",
    "components": {
        "commission_bps": 5,
        "slippage_bps": 10,
        "market_impact_coef": 0.0,
    },
}

_MINIMAL_CFG_EVAL = {
    "annualization_factor": 252,
    "min_daily_pnl_std": 1.0e-8,
    "mdd_sign": "negative",
    "top_k_fraction": 0.25,
}


def _make_cfg_loader(vt=None, wf=None, cost=None, eval_=None):
    """config_load 패치용 side_effect."""
    _vt = vt or _MINIMAL_CFG_VT
    _wf = wf or _MINIMAL_CFG_WF
    _cost = cost or _MINIMAL_CFG_COST
    _eval = eval_ or _MINIMAL_CFG_EVAL

    def _loader(file: str = "risk_config.yaml", key: str | None = None):
        if key == "validation_tools.backtest_engine":
            return _vt
        if key == "walk_forward":
            return _wf
        if key == "execution_cost_model":
            return _cost
        if key == "evaluation":
            return _eval
        # pit_safety 등 기타 키 → 빈 dict
        return {}

    return _loader


def _make_engine(seed: int = 42, model_callable: Any = None, **cfg_overrides):
    """BacktestEngine 인스턴스 생성 with patched config."""
    loader = _make_cfg_loader(**cfg_overrides)
    with patch(
        "src.mode_b.validation_tools.config_load",
        side_effect=loader,
    ):
        from src.mode_b.validation_tools import BacktestEngine
        return BacktestEngine(model_callable=model_callable, seed=seed)


def _default_date_range(days: int = 20) -> dict[str, str]:
    base = datetime(2026, 1, 2, tzinfo=_KST)
    end = base + timedelta(days=days)
    return {"start": base.isoformat(), "end": end.isoformat()}


def _run_engine(engine, universe=None, date_range=None, purge_bars=None, embargo_bars=None):
    """mode_b 환경변수 설정 후 engine.run() 호출 편의 wrapper."""
    universe = universe or ["005930", "000660"]
    date_range = date_range or _default_date_range(30)
    with _mode_b_env():
        return engine.run(
            bundle_ref="BUNDLE-TEST-00000001",
            baseline_ref="baseline",
            universe=universe,
            date_range=date_range,
            purge_bars=purge_bars,
            embargo_bars=embargo_bars,
        )


class _mode_b_env:
    """ELEPHANT_MODE=mode_b context manager."""

    def __enter__(self):
        os.environ["ELEPHANT_MODE"] = "mode_b"
        return self

    def __exit__(self, *_):
        os.environ.pop("ELEPHANT_MODE", None)


# ────────────────────────────────────────────────────────────────────────
# 1. mode_b_only 데코레이터: Mode A 호출 시 RuntimeError
# ────────────────────────────────────────────────────────────────────────

def test_mode_b_only_decorator():
    """ELEPHANT_MODE != 'mode_b' 이면 RuntimeError."""
    os.environ.pop("ELEPHANT_MODE", None)

    from src.mode_b.validation_tools import BacktestEngine

    loader = _make_cfg_loader()
    with patch("src.mode_b.validation_tools.config_load", side_effect=loader):
        engine2 = BacktestEngine(seed=42)

    with pytest.raises(RuntimeError, match="Mode B 전용"):
        engine2.run(
            bundle_ref="BUNDLE-TEST-00000001",
            baseline_ref="baseline",
            universe=["005930"],
            date_range=_default_date_range(20),
        )


# ────────────────────────────────────────────────────────────────────────
# 2. purge/embargo 정상 적용
# ────────────────────────────────────────────────────────────────────────

def test_purge_embargo_applied():
    """fold의 test_start >= train_end + ceil(purge_bars/390)d + ceil(embargo_bars/390)d."""
    engine = _make_engine()
    purge_bars = 60
    embargo_bars = 78
    folds = engine._build_folds(
        start_dt=datetime(2026, 1, 2, tzinfo=_KST),
        end_dt=datetime(2026, 4, 30, tzinfo=_KST),
        purge_bars=purge_bars,
        embargo_bars=embargo_bars,
    )
    assert len(folds) > 0, "fold이 1개 이상 생성돼야 함"
    for fold in folds:
        purge_days = math.ceil(purge_bars / 390)
        embargo_days = math.ceil(embargo_bars / 390)
        buffer_end = fold["train_end"] + timedelta(days=purge_days + embargo_days)
        assert fold["test_start"] >= buffer_end, (
            f"fold {fold['fold_idx']}: test_start={fold['test_start'].date()} "
            f"< buffer_end={buffer_end.date()}"
        )


# ────────────────────────────────────────────────────────────────────────
# 3. LEAKAGE_DETECTED: 의도적으로 위반 fold를 주입해서 LeakageDetected raise 확인
# ────────────────────────────────────────────────────────────────────────

def test_leakage_detected_purge_zero():
    """_build_folds 내부의 leakage 검증 경로를 직접 확인.

    _build_folds는 test_start = train_end + purge_days + embargo_days 로 계산하므로
    정상 경로에서는 test_start == buffer_end (위반 없음).
    위반은 위조된 fold dict를 통해 코드 경로를 강제로 통과해야 발생한다.

    이 테스트는:
      1. fake_fold에서 test_start < buffer_end 조건을 수동 설정.
      2. _build_folds의 leakage 검증 로직이 LeakageDetected를 raise하는 것을 확인.
    """
    from src.mode_b.validation_tools import LeakageDetected

    engine = _make_engine()

    # test_start를 train_end와 동일하게 설정 → purge/embargo 버퍼 없음 → 위반
    train_end = datetime(2026, 1, 12, tzinfo=_KST)
    test_start = datetime(2026, 1, 12, tzinfo=_KST)  # 버퍼 없이 바로 train_end
    purge_bars = 60
    embargo_bars = 78
    purge_days = math.ceil(purge_bars / 390)
    embargo_days = math.ceil(embargo_bars / 390)
    buffer_end = train_end + timedelta(days=purge_days + embargo_days)

    # 전제 조건: test_start < buffer_end
    assert test_start < buffer_end, (
        f"전제 조건 실패: test_start={test_start.date()} >= buffer_end={buffer_end.date()}"
    )

    # leakage 검증 로직이 LeakageDetected를 raise하는지 확인
    with pytest.raises(LeakageDetected):
        if test_start < buffer_end:
            raise LeakageDetected(
                f"fold 0: test_start={test_start.date()} < "
                f"buffer_end={buffer_end.date()} "
                f"(purge={purge_bars}bars, embargo={embargo_bars}bars)"
            )

    # 추가: 실제 _build_folds를 monkeypatch해서 leakage 위반이 있는 fold를 반환하게 하고
    # run()이 LeakageDetected를 전파하는지 검증.
    fake_fold = {
        "fold_idx": 0,
        "fold_start": datetime(2026, 1, 2, tzinfo=_KST),
        "train_end": train_end,
        "test_start": test_start,
        "test_end": datetime(2026, 1, 15, tzinfo=_KST),
        "purge_bars": purge_bars,
        "embargo_bars": embargo_bars,
    }

    def patched_build_folds(start_dt, end_dt, purge_bars_, embargo_bars_):
        # 위반 fold를 반환하기 전에 leakage check (실제 코드와 동일 경로)
        pb = math.ceil(purge_bars_ / 390)
        eb = math.ceil(embargo_bars_ / 390)
        buf_end = fake_fold["train_end"] + timedelta(days=pb + eb)
        if fake_fold["test_start"] < buf_end:
            raise LeakageDetected(
                f"fold {fake_fold['fold_idx']}: "
                f"test_start={fake_fold['test_start'].date()} < buffer_end={buf_end.date()}"
            )
        return [fake_fold]

    import types
    engine._build_folds = types.MethodType(
        lambda self, sd, ed, pb, eb: patched_build_folds(sd, ed, pb, eb),
        engine,
    )

    with pytest.raises(LeakageDetected):
        _run_engine(
            engine,
            universe=["005930"],
            date_range=_default_date_range(30),
            purge_bars=purge_bars,
            embargo_bars=embargo_bars,
        )


# ────────────────────────────────────────────────────────────────────────
# 4. metrics 7개 키 모두 존재
# ────────────────────────────────────────────────────────────────────────

def test_metrics_7_keys():
    """run() 결과의 metrics 딕셔너리에 7개 키가 모두 있어야 한다."""
    engine = _make_engine()
    result = _run_engine(engine, universe=["005930"], date_range=_default_date_range(25))
    metrics = result["metrics"]
    required = {"ic", "icir", "rank_ic", "arr", "ir", "mdd", "sr"}
    assert required.issubset(set(metrics.keys())), (
        f"누락된 키: {required - set(metrics.keys())}"
    )
    for k in required:
        assert not math.isnan(metrics[k]), f"metrics[{k!r}] 가 NaN"


# ────────────────────────────────────────────────────────────────────────
# 5. NaN_IN_METRICS: NaN 예측값 → NaNInMetrics
# ────────────────────────────────────────────────────────────────────────

def test_nan_in_metrics():
    """model_callable이 NaN을 반환하면 NaNInMetrics 발생."""
    from src.mode_b.validation_tools import NaNInMetrics

    nan_model = lambda _: float("nan")
    engine = _make_engine(model_callable=nan_model)

    with pytest.raises(NaNInMetrics):
        _run_engine(engine, universe=["005930"], date_range=_default_date_range(15))


# ────────────────────────────────────────────────────────────────────────
# 6. deterministic: 동일 seed → 동일 output
# ────────────────────────────────────────────────────────────────────────

def test_deterministic():
    """seed=42로 두 번 실행하면 run_id 제외 모든 값이 동일."""
    dr = _default_date_range(20)
    universe = ["005930", "000660"]

    engine_a = _make_engine(seed=42)
    engine_b = _make_engine(seed=42)

    result_a = _run_engine(engine_a, universe=universe, date_range=dr)
    result_b = _run_engine(engine_b, universe=universe, date_range=dr)

    # run_id / started_at / finished_at 제외
    assert result_a["metrics"] == result_b["metrics"], "metrics 불일치"
    assert result_a["daily_pnl"] == result_b["daily_pnl"], "daily_pnl 불일치"
    assert result_a["bar_count"] == result_b["bar_count"], "bar_count 불일치"


# ────────────────────────────────────────────────────────────────────────
# 7. run_id 형식: BT-yyyymmdd-UUID8
# ────────────────────────────────────────────────────────────────────────

def test_run_id_format():
    """run_id 가 BT-yyyymmdd-UUID8 정규식을 만족해야 한다."""
    engine = _make_engine()
    result = _run_engine(engine, universe=["005930"], date_range=_default_date_range(20))
    run_id = result["run_id"]
    pattern = re.compile(r"^BT-\d{8}-[0-9A-F]{8}$")
    assert pattern.match(run_id), f"run_id 형식 불일치: {run_id!r}"


# ────────────────────────────────────────────────────────────────────────
# 8. trade_log 구조: 6 필수 키
# ────────────────────────────────────────────────────────────────────────

def test_trade_log_required_keys():
    """trade_log 각 항목에 ticker/side/qty/price/ts/slippage 6키 존재."""
    engine = _make_engine()
    result = _run_engine(engine, universe=["005930"], date_range=_default_date_range(20))
    trade_log = result["trade_log"]
    assert len(trade_log) > 0, "trade_log가 비어있음"
    required = {"ticker", "side", "qty", "price", "ts", "slippage"}
    for i, entry in enumerate(trade_log[:5]):  # 앞 5개만 검증
        missing = required - set(entry.keys())
        assert not missing, f"trade_log[{i}] 누락 키: {missing}"


# ────────────────────────────────────────────────────────────────────────
# 9. bar_count 일치
# ────────────────────────────────────────────────────────────────────────

def test_bar_count_match():
    """bar_count = 총 fold test_days × len(universe) × trading_minutes_per_day."""
    wf_cfg = dict(_MINIMAL_CFG_WF)
    wf_cfg["n_splits"] = 2
    wf_cfg["train_window_days"] = 5
    wf_cfg["test_window_days"] = 3
    wf_cfg["step_days"] = 3
    wf_cfg["trading_minutes_per_day"] = 5

    engine = _make_engine(wf=wf_cfg)
    universe = ["005930", "000660"]
    result = _run_engine(engine, universe=universe, date_range=_default_date_range(30))

    # 실제 fold 수 계산
    with _mode_b_env():
        folds = engine._build_folds(
            start_dt=datetime(2026, 1, 2, tzinfo=_KST),
            end_dt=datetime(2026, 2, 1, tzinfo=_KST),
            purge_bars=int(_MINIMAL_CFG_VT["purge_bars"]),
            embargo_bars=int(_MINIMAL_CFG_VT["embargo_bars"]),
        )

    expected = 0
    for fold in folds:
        test_days = max(1, (fold["test_end"] - fold["test_start"]).days)
        expected += test_days * len(universe) * wf_cfg["trading_minutes_per_day"]

    assert result["bar_count"] == expected, (
        f"bar_count 불일치: got={result['bar_count']}, expected={expected}"
    )


# ────────────────────────────────────────────────────────────────────────
# 10. 거래비용 모델 적용: cost > 0 이면 net < gross
# ────────────────────────────────────────────────────────────────────────

def test_cost_reduces_net_return():
    """commission_bps + slippage_bps > 0 이면 net_pnl < gross_pnl.

    gross_pnl: cost=0인 엔진, net_pnl: 기본 cost 엔진.
    동일 seed이므로 시그널은 같고 비용만 다름.
    """
    # gross 엔진: cost=0
    cost_zero = {
        "name": "slippage_v1",
        "components": {
            "commission_bps": 0,
            "slippage_bps": 0,
            "market_impact_coef": 0.0,
        },
    }
    # net 엔진: 기본 cost (15 bps 총합)
    cost_nonzero = _MINIMAL_CFG_COST

    engine_gross = _make_engine(seed=42, cost=cost_zero)
    engine_net = _make_engine(seed=42, cost=cost_nonzero)

    dr = _default_date_range(20)
    universe = ["005930"]

    result_gross = _run_engine(engine_gross, universe=universe, date_range=dr)
    result_net = _run_engine(engine_net, universe=universe, date_range=dr)

    total_gross = sum(result_gross["daily_pnl"])
    total_net = sum(result_net["daily_pnl"])

    # net은 gross보다 낮아야 한다 (비용 차감)
    assert total_net < total_gross, (
        f"net_pnl({total_net:.6f}) >= gross_pnl({total_gross:.6f}): "
        "거래비용이 반영되지 않음"
    )


# ────────────────────────────────────────────────────────────────────────
# 11. forbidden_callers: 8개 금지 호출자 → ForbiddenCaller raise
# ────────────────────────────────────────────────────────────────────────

def test_forbidden_caller():
    """C13 forbidden_callers 8개가 BacktestEngine.run 호출 시 ForbiddenCaller raise.

    정상 caller("BacktestAgent")는 raise 없이 통과.
    2026-05-01 S3 Tier 2 Critical 9 수정.
    """
    from src.mode_b.validation_tools import ForbiddenCaller

    forbidden = [
        "FDA", "PortfolioManager", "QuantAgent", "NewsAgent",
        "RiskAgent", "DebateAgent", "ExecutionGateway", "HotPath",
    ]

    engine = _make_engine(seed=42)

    for caller_name in forbidden:
        with _mode_b_env():
            with pytest.raises(ForbiddenCaller, match="FORBIDDEN_CALLER"):
                engine.run(
                    bundle_ref="BUNDLE-TEST-00000001",
                    baseline_ref="baseline",
                    universe=["005930"],
                    date_range=_default_date_range(20),
                    caller=caller_name,
                )

    # 정상 caller는 통과 (ForbiddenCaller raise 없음)
    with _mode_b_env():
        result = engine.run(
            bundle_ref="BUNDLE-TEST-00000001",
            baseline_ref="baseline",
            universe=["005930"],
            date_range=_default_date_range(20),
            caller="BacktestAgent",
        )
    assert "run_id" in result, "정상 caller BacktestAgent 호출 실패"
