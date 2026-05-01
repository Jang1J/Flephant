"""S1-0 Batch B Evaluation Metrics. C12/C13 metrics 7종 구현.

계산 대상:
  ic       : Pearson IC (cross-sectional 매 ts_close → 시계열 평균)
  icir     : mean(IC) / std(IC)
  rank_ic  : Spearman RankIC
  arr      : Annualized Return (복리 기준)
  ir       : Information Ratio (excess return / tracking error)
  sr       : Sharpe Ratio (risk-free = 0 가정)
  mdd      : Max Drawdown (음수 규약, risk_config.yaml evaluation.mdd_sign)

불변 원칙 준수:
  - annualization_factor 252 (거래일 수) → risk_config.yaml evaluation.annualization_factor
  - IR 분모 0 방지: min_daily_pnl_std → risk_config.yaml evaluation.min_daily_pnl_std
  - MDD 부호 규약: risk_config.yaml evaluation.mdd_sign
  - regime breakdown 라벨 → risk_config.yaml evaluation.regime_labels

C12 BacktestAgentContract.output.metrics 필드명과 1:1 매칭.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger

logger = get_logger("metrics")

# ====================================================================== #
# Config loader
# ====================================================================== #


def _load_eval_cfg() -> dict[str, Any]:
    return config_load("risk_config.yaml", "evaluation")


# 모듈 레벨 cache (lazy init)
# architect + modeler MULTI-AGENT CONFIRMED 2026-04-21 이관
_PERF_VEC_CONFIG_CACHE: dict | None = None


def _get_perf_vec_config() -> dict:
    """performance_vector 관련 config. 첫 호출 시 로드 + 캐싱."""
    global _PERF_VEC_CONFIG_CACHE
    if _PERF_VEC_CONFIG_CACHE is None:
        cfg = _load_eval_cfg()
        _PERF_VEC_CONFIG_CACHE = {
            "clip_low": float(cfg.get("performance_vector_clip_low", 0.01)),
            "clip_high": float(cfg.get("performance_vector_clip_high", 0.99)),
            "neutral_fallback": float(cfg.get("performance_vector_neutral_fallback", 0.5)),
            "rolling_window": int(cfg.get("performance_vector_rolling_window", 30)),
        }
    return _PERF_VEC_CONFIG_CACHE


# ====================================================================== #
# Low-level functions (single group)
# ====================================================================== #


def ic(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Pearson IC. 단일 cross-section (1 ts_close 내 N 종목).

    np.corrcoef 기반. degenerate(분산 0) 시 0.0 반환.
    """
    a = np.asarray(y_true, dtype=float)
    b = np.asarray(y_pred, dtype=float)
    if a.size < 2 or b.size < 2 or a.size != b.size:
        return 0.0
    if float(np.std(a)) < 1e-12 or float(np.std(b)) < 1e-12:
        return 0.0
    corr = float(np.corrcoef(a, b)[0, 1])
    if np.isnan(corr):
        return 0.0
    return corr


def rank_ic(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Spearman RankIC. pct rank 후 Pearson."""
    a = np.asarray(y_true, dtype=float)
    b = np.asarray(y_pred, dtype=float)
    if a.size < 2 or b.size < 2 or a.size != b.size:
        return 0.0
    ra = _pct_rank(a)
    rb = _pct_rank(b)
    return ic(ra, rb)


def rank_transform(x: np.ndarray, method: str = "average") -> np.ndarray:
    """Percentile rank transform. Returns values in (0, 1] (minimum = 1/n > 0, maximum = 1).

    Args:
        x: 1D array.
        method: scipy.stats.rankdata method.

    Returns:
        1D array of same length, dtype float64, values in (0, 1].
    """
    arr = np.asarray(x, dtype=float)
    n = len(arr)
    if n == 0:
        return arr
    from scipy.stats import rankdata  # local import (optional dep)

    ranks = rankdata(arr, method=method)
    return ranks / float(n)


def _pct_rank(arr: np.ndarray) -> np.ndarray:
    """Average-method percentile rank (pandas rank(pct=True) equivalent).

    scipy.stats.rankdata(method='average') 기반으로 tie를 정확히 처리.
    동점값은 모두 동일한 평균 rank를 받음 (production RankIC 정확도 보장).
    """
    return rank_transform(arr, method="average")


# ====================================================================== #
# Panel-level IC/RankIC (매 ts_close 그룹 평균)
# ====================================================================== #


def panel_ic(
    y_true_by_group: dict[Any, np.ndarray],
    y_pred_by_group: dict[Any, np.ndarray],
) -> np.ndarray:
    """각 ts_close 그룹별 IC → array.

    Returns: 그룹별 IC 값 numpy array (NaN 제외).
    """
    out: list[float] = []
    for key, y_true in y_true_by_group.items():
        y_pred = y_pred_by_group.get(key)
        if y_pred is None:
            continue
        v = ic(y_true, y_pred)
        if not np.isnan(v):
            out.append(v)
    return np.asarray(out, dtype=float)


def panel_rank_ic(
    y_true_by_group: dict[Any, np.ndarray],
    y_pred_by_group: dict[Any, np.ndarray],
) -> np.ndarray:
    """각 ts_close 그룹별 RankIC array."""
    out: list[float] = []
    for key, y_true in y_true_by_group.items():
        y_pred = y_pred_by_group.get(key)
        if y_pred is None:
            continue
        v = rank_ic(y_true, y_pred)
        if not np.isnan(v):
            out.append(v)
    return np.asarray(out, dtype=float)


def icir(ic_series: np.ndarray) -> float:
    """mean(IC) / std(IC). 표본 표준편차(`ddof=1`) 사용, 학술/실무 표준.

    ddof=0(모집단 std)은 표본 수가 작을 때 std를 과소추정 → ICIR 과대평가.
    외부 논문 + Backtest 리포트 일관성 위해 `ddof=1` 유지.
    std=0 또는 size<2이면 0.0 반환.
    """
    if ic_series.size < 2:
        return 0.0
    s = float(np.std(ic_series, ddof=1))
    if s < 1e-12:
        return 0.0
    return float(np.mean(ic_series)) / s


# ====================================================================== #
# Return-based metrics
# ====================================================================== #


def _resolve_af(annualization_factor: int | None) -> int:
    """annualization_factor None이면 risk_config.yaml에서 로드 (불변 원칙 5)."""
    if annualization_factor is not None:
        return int(annualization_factor)
    return int(_load_eval_cfg()["annualization_factor"])


def _resolve_min_std(min_std: float | None) -> float:
    """min_std None이면 risk_config.yaml evaluation.min_daily_pnl_std에서 로드."""
    if min_std is not None:
        return float(min_std)
    return float(_load_eval_cfg()["min_daily_pnl_std"])


def annualized_return(
    daily_pnl: np.ndarray,
    annualization_factor: int | None = None,
) -> float:
    """복리 기준 연율화 수익률.

    daily_pnl: 일별 수익률 (비율, 예: 0.01 = +1%).
    annualization_factor: None이면 risk_config.yaml evaluation.annualization_factor 사용.
    Returns: (prod(1+r))^(factor/n) - 1.
    """
    r = np.asarray(daily_pnl, dtype=float)
    n = r.size
    if n == 0:
        return 0.0
    cum = float(np.prod(1.0 + r))
    if cum <= 0.0:
        # 전액 손실 이상 → -1 clip
        return -1.0
    af = _resolve_af(annualization_factor)
    exp = float(af) / float(n)
    return float(cum ** exp - 1.0)


def information_ratio(
    daily_pnl: np.ndarray,
    benchmark: np.ndarray | None = None,
    annualization_factor: int | None = None,
    min_std: float | None = None,
) -> float:
    """IR = mean(excess) / std(excess) * sqrt(factor).

    benchmark=None이면 zero benchmark (== excess Sharpe).
    annualization_factor / min_std None이면 risk_config.yaml에서 로드.
    std < min_std면 0.0 (분모 0 방지).
    """
    r = np.asarray(daily_pnl, dtype=float)
    if r.size < 2:
        return 0.0
    if benchmark is None:
        excess = r
    else:
        b = np.asarray(benchmark, dtype=float)
        if b.size != r.size:
            raise ValueError(
                f"benchmark size({b.size}) != daily_pnl size({r.size})"
            )
        excess = r - b

    ms = _resolve_min_std(min_std)
    af = _resolve_af(annualization_factor)

    s = float(np.std(excess, ddof=1))
    if s < ms:
        return 0.0
    m = float(np.mean(excess))
    return m / s * float(np.sqrt(af))


def sharpe_ratio(
    daily_pnl: np.ndarray,
    annualization_factor: int | None = None,
    min_std: float | None = None,
) -> float:
    """SR = mean(r) / std(r) * sqrt(factor). risk-free=0 가정.

    information_ratio(benchmark=None)과 동일 식이지만, 의미 분리 위해 별도 함수.
    """
    return information_ratio(
        daily_pnl,
        benchmark=None,
        annualization_factor=annualization_factor,
        min_std=min_std,
    )


def max_drawdown(cumulative_or_daily: np.ndarray, daily_input: bool = True) -> float:
    """MDD. 기본: daily return 배열 입력 → cumulative 변환 후 peak-to-trough.

    Returns: 음수 (예: -0.15 = 15% drawdown).
    risk_config.yaml evaluation.mdd_sign == "negative" 규약.
    """
    a = np.asarray(cumulative_or_daily, dtype=float)
    if a.size == 0:
        return 0.0

    if daily_input:
        cum = np.cumprod(1.0 + a)
    else:
        cum = a

    if cum.size == 0:
        return 0.0

    peak = np.maximum.accumulate(cum)
    # (cum - peak) / peak → 현재 drawdown (0 이하)
    dd = (cum - peak) / np.where(peak > 1e-12, peak, 1e-12)
    return float(np.min(dd))


# ====================================================================== #
# MetricsBundle (C12/C13 metrics 7종 dataclass)
# ====================================================================== #


@dataclass
class MetricsBundle:
    """C12 BacktestAgentContract.output.metrics 와 1:1 대응.

    regime_breakdown은 C12 backtest_report.regime_breakdown과 같은 구조.
    """

    ic: float = 0.0
    icir: float = 0.0
    rank_ic: float = 0.0
    arr: float = 0.0
    ir: float = 0.0
    mdd: float = 0.0
    sr: float = 0.0
    regime_breakdown: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """C12 metrics dict 형식으로 직렬화. regime_breakdown은 최상위에 없음."""
        d = asdict(self)
        d.pop("regime_breakdown", None)
        return d

    def to_performance_vector(self, ic_history: np.ndarray | None = None) -> np.ndarray:
        """architecture.md §8.1 SSOT 8-dim 성능 벡터 반환.

        x_t = [IC, ICIR, Rank(IC), Rank(ICIR), ARR, IR, -MDD, SR]
               m1   m2     m3         m4         m5  m6   m7   m8

        Args:
            ic_history: rolling window IC 값 배열 (Rank 계산용).
                - None 또는 빈 배열: Rank(IC) = Rank(ICIR) = 0.0 (fallback)
                - len >= 2: Rank(IC) = rank_transform 기반 cross-sectional 정규화,
                            Rank(ICIR) = _get_perf_vec_config()["neutral_fallback"] (0.5)
                            (ICIR history 별도 전달 인터페이스는 Sprint 4 확장 예정)
                - len == 1: Rank(IC) = ic_history 단일값 대비 self.ic rank, Rank(ICIR) = 0.0

        Returns:
            np.ndarray shape (8,) float64. raw 값 (정규화는 normalize_performance_vector() 경유).

        주의:
            - -MDD: bundle.mdd는 음수 규약이므로 -bundle.mdd = 양수.
            - Rank(IC)/Rank(ICIR): ic_history 기반 cross-sectional rank.
              None이면 0.0 (정규화 미수행 단일 번들).
            - min-max 정규화는 호출자 레벨에서 normalize_performance_vector() 사용.
        """
        if ic_history is not None and len(ic_history) > 0:
            ic_ranks = rank_transform(np.asarray(ic_history, dtype=float))
            # bundle.ic의 rank: ic_history 내에서의 위치 (마지막 값 기준)
            # 배열에 bundle.ic 포함 여부 모름 → 배열 맨 끝에 bundle.ic 추가 후 rank
            ic_with_self = np.append(ic_history, self.ic)
            ic_rank_val = float(rank_transform(ic_with_self)[-1])

            icir_rank_val = 0.0  # 단일값이면 rank=1.0 (최상위), 복수이면 history 경유
            if len(ic_history) >= 2:
                # ICIR은 IC 시계열로부터 rolling으로 도출되는 값.
                # ic_history가 있다면 rolling ICIR의 proxy로 IC std-mean 비 계산.
                # 완전한 ICIR history는 호출자가 직접 전달 권장.
                icir_rank_val = _get_perf_vec_config()["neutral_fallback"]  # 중간값 fallback
        else:
            ic_rank_val = 0.0
            icir_rank_val = 0.0

        vec = np.array([
            self.ic,          # m1: IC
            self.icir,        # m2: ICIR
            ic_rank_val,      # m3: Rank(IC)
            icir_rank_val,    # m4: Rank(ICIR)
            self.arr,         # m5: ARR
            self.ir,          # m6: IR
            -self.mdd,        # m7: -MDD (mdd 음수 규약 → 부호 반전 → 양수)
            self.sr,          # m8: SR
        ], dtype=float)
        return vec

    @classmethod
    def compute(
        cls,
        y_true_by_group: dict[Any, np.ndarray],
        y_pred_by_group: dict[Any, np.ndarray],
        daily_pnl: np.ndarray,
        benchmark_daily: np.ndarray | None = None,
        annualization_factor: int | None = None,
        min_std: float | None = None,
    ) -> "MetricsBundle":
        """C12/C13 metrics 7종 일괄 계산.

        Args:
          y_true_by_group: {ts_close: np.array(label 20 종목)} forward return
          y_pred_by_group: {ts_close: np.array(pred 20 종목)} 모델 score
          daily_pnl: 일별 수익률 array
          benchmark_daily: IR 계산용 benchmark (None이면 zero)
          annualization_factor: None이면 risk_config.yaml에서 로드
          min_std: None이면 risk_config.yaml evaluation.min_daily_pnl_std
        """
        cfg = _load_eval_cfg()
        af = int(annualization_factor) if annualization_factor is not None else int(
            cfg["annualization_factor"]
        )
        ms = float(min_std) if min_std is not None else float(
            cfg["min_daily_pnl_std"]
        )

        ic_arr = panel_ic(y_true_by_group, y_pred_by_group)
        rank_arr = panel_rank_ic(y_true_by_group, y_pred_by_group)

        return cls(
            ic=float(np.mean(ic_arr)) if ic_arr.size > 0 else 0.0,
            icir=icir(ic_arr),
            rank_ic=float(np.mean(rank_arr)) if rank_arr.size > 0 else 0.0,
            arr=annualized_return(daily_pnl, annualization_factor=af),
            ir=information_ratio(
                daily_pnl,
                benchmark=benchmark_daily,
                annualization_factor=af,
                min_std=ms,
            ),
            sr=sharpe_ratio(daily_pnl, annualization_factor=af, min_std=ms),
            mdd=max_drawdown(daily_pnl, daily_input=True),
        )

    # ------------------------------------------------------------------ #

    def regime_breakdown_fill(
        self,
        daily_pnl_by_regime: dict[str, np.ndarray],
        annualization_factor: int | None = None,
        min_std: float | None = None,
    ) -> list[dict[str, Any]]:
        """regime별 sharpe/mdd/n_days 계산해 regime_breakdown에 채움.

        daily_pnl_by_regime: {"bull": np.array, "bear": ..., "sideways": ..., "volatile": ...}
        risk_config.yaml evaluation.regime_labels와 교차 검증.
        """
        cfg = _load_eval_cfg()
        af = int(annualization_factor) if annualization_factor is not None else int(
            cfg["annualization_factor"]
        )
        ms = float(min_std) if min_std is not None else float(
            cfg["min_daily_pnl_std"]
        )
        expected_labels: list[str] = list(cfg["regime_labels"])

        out: list[dict[str, Any]] = []
        for regime in expected_labels:
            r = daily_pnl_by_regime.get(regime)
            if r is None or len(r) == 0:
                continue
            arr = np.asarray(r, dtype=float)
            out.append(
                {
                    "regime": regime,
                    "sharpe": sharpe_ratio(
                        arr, annualization_factor=af, min_std=ms
                    ),
                    "mdd": max_drawdown(arr, daily_input=True),
                    "n_days": int(arr.size),
                }
            )
        self.regime_breakdown = out
        return out


# ====================================================================== #
# normalize_performance_vector (모듈-레벨 헬퍼)
# ====================================================================== #


def normalize_performance_vector(
    vec: np.ndarray,
    history_df: np.ndarray,
) -> np.ndarray:
    """30일 rolling window 기반 min-max 정규화.

    architecture.md §8.1: 정규화는 호출자 레벨에서 수행.
    MetricsBundle.to_performance_vector()는 raw 8-dim만 반환하고,
    이 함수에서 history와 비교해 min-max 스케일링을 적용한다.

    Args:
        vec: shape (8,) raw performance vector (to_performance_vector() 결과).
        history_df: shape (N, 8) 과거 N개 번들의 raw performance vector 행렬.
                    rolling window = risk_config.yaml evaluation.performance_vector_rolling_window.
                    N < window이면 가용한 N개 전체 사용.

    Returns:
        shape (8,) float64. 각 차원 [0, 1] 정규화.
        history가 비어 있거나 min == max이면 해당 차원 0.5 반환 (neutral).

    주의:
        - 정규화 window: risk_config.yaml evaluation.performance_vector_rolling_window (30일).
        - history_df 행 수 < window이면 가용한 것 전부 사용 (no error).
        - min == max (상수 history)이면 0.5 (neutral midpoint).
    """
    cfg = _get_perf_vec_config()
    window = cfg["rolling_window"]

    vec = np.asarray(vec, dtype=float)
    hist = np.asarray(history_df, dtype=float)

    if hist.ndim == 1:
        hist = hist.reshape(1, -1)

    # rolling window 슬라이싱 (가장 최근 window 행)
    if hist.shape[0] > window:
        hist = hist[-window:]

    if hist.shape[0] == 0:
        return np.full_like(vec, cfg["neutral_fallback"], dtype=float)

    # vec를 history에 포함해서 min/max 계산 (현재 값 기준 상대 rank)
    combined = np.vstack([hist, vec.reshape(1, -1)])
    mn = combined.min(axis=0)
    mx = combined.max(axis=0)
    rng = mx - mn

    # boolean mask 분리: np.where 양 브랜치 모두 평가 → divide-by-zero RuntimeWarning 방지
    normalized = np.full_like(vec, cfg["neutral_fallback"], dtype=float)
    valid = rng >= 1e-12
    normalized[valid] = (vec[valid] - mn[valid]) / rng[valid]
    # api_contracts.md C18 normalization 스펙 준수: clip [0.01, 0.99]
    return np.clip(normalized, cfg["clip_low"], cfg["clip_high"]).astype(float)
