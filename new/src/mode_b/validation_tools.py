"""C13 ValidationToolsContract. BacktestEngine / ReplayRunner / PerformanceAnalyzer."""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from src.utils.config_loader import load as config_load
from src.utils.id_factory import generate_backtest_id, generate_pa_id, generate_replay_id
from src.utils.logger import get_logger
from src.utils.mode_guard import mode_b_only
from src.utils.ticker_utils import pad_ticker

logger = get_logger("BacktestEngine")
_KST = ZoneInfo("Asia/Seoul")

# ────────────────────────────────────────────────────────────────────
# C13 forbidden_callers 검증 (불변 원칙 3: Backtest Agent Mode B 전용)
# ────────────────────────────────────────────────────────────────────

# C13 forbidden_callers 8개. 이 caller가 BacktestEngine/ReplayRunner/PerformanceAnalyzer를
# 직접 호출하면 즉시 ForbiddenCaller raise.
_FORBIDDEN_CALLERS: frozenset[str] = frozenset({
    "FDA",
    "PortfolioManager",
    "QuantAgent",
    "NewsAgent",
    "RiskAgent",
    "DebateAgent",
    "ExecutionGateway",
    "HotPath",
})


class ForbiddenCaller(RuntimeError):
    """C13 forbidden_caller 위반. Mode A / 장중 경로가 백테스트 도구를 직접 호출."""

    def __init__(self, caller: str) -> None:
        self.caller = caller
        super().__init__(
            f"[FORBIDDEN_CALLER] {caller!r}는 C13 ValidationTools 호출 금지 대상. "
            "Backtest Agent (Mode B) 경유만 허용."
        )


def _check_caller(caller: str) -> None:
    """caller가 forbidden_callers 목록에 속하면 ForbiddenCaller raise."""
    if caller in _FORBIDDEN_CALLERS:
        raise ForbiddenCaller(caller)


# ────────────────────────────────────────────────────────────────────
# 커스텀 예외 (C13 errors 블록)
# ────────────────────────────────────────────────────────────────────

class BacktestError(RuntimeError):
    """BacktestEngine 기반 예외."""
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"[{code}] {detail}")


class BundleLoadFailed(BacktestError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("BUNDLE_LOAD_FAILED", detail)


class DataUnavailable(BacktestError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("DATA_UNAVAILABLE", detail)


class NaNInMetrics(BacktestError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("NAN_IN_METRICS", detail)


class LeakageDetected(BacktestError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("LEAKAGE_DETECTED", detail)


# ────────────────────────────────────────────────────────────────────
# BacktestEngine
# ────────────────────────────────────────────────────────────────────

class BacktestEngine:
    """C13 ValidationToolsContract 백테스트 엔진.

    walk-forward CV + Purge/Embargo (Prado 1% rule) + 거래비용 모델.

    설계 원칙:
      - PIT-Safety: 학습 fold의 label horizon + lookback 만큼 purge,
                    test window 1% 만큼 embargo 버퍼 삽입. 위반 시 LEAKAGE_DETECTED.
      - 거래비용: risk_config.yaml execution_cost_model 섹션 로드.
      - 하드코딩 금지: 모든 임계값은 risk_config.yaml 경유.
      - LLM 미호출: deterministic 계산 엔진.
      - Mode B 전용: @mode_b_only 데코레이터 강제.

    Args:
        model_callable: 예측 함수. (features: list[float]) -> float.
                        None 이면 랜덤 시그널(테스트용 stub).
        seed: 재현성용 랜덤 시드.
    """

    def __init__(
        self,
        model_callable: Any | None = None,
        seed: int = 42,
    ) -> None:
        self._model_callable = model_callable
        self._seed = seed
        self._cfg_vt = config_load("risk_config.yaml", "validation_tools.backtest_engine")
        self._cfg_wf = config_load("risk_config.yaml", "walk_forward")
        self._cfg_cost = config_load("risk_config.yaml", "execution_cost_model")
        self._cfg_eval = config_load("risk_config.yaml", "evaluation")
        # S3 Critical 8: 하드코딩 제거. mock_data 파라미터를 yaml에서 로드.
        _mock_cfg: dict[str, Any] = (self._cfg_vt or {}).get("mock_data", {})
        self._mock_base_price: float = float(_mock_cfg.get("base_price", 50000.0))
        self._mock_price_noise_std: float = float(_mock_cfg.get("price_noise_std", 1000.0))
        self._mock_signal_return_corr: float = float(
            _mock_cfg.get("signal_return_correlation", 0.05)
        )
        self._mock_return_noise_std: float = float(_mock_cfg.get("return_noise_std", 0.02))
        # S3 ARR Fix: initial_capital으로 daily_pnl(dollar) → 수익률 비율 변환.
        # 단위 혼용 방지. risk_config.yaml backtest 섹션에서 로드.
        _backtest_cfg: dict[str, Any] = config_load("risk_config.yaml", "backtest") or {}
        self._initial_capital: float = float(
            _backtest_cfg.get("initial_capital", 100_000_000.0)
        )

    # ────────────────────────────────────────────────────
    # public API
    # ────────────────────────────────────────────────────

    @mode_b_only
    def run(
        self,
        bundle_ref: str,
        baseline_ref: str,
        universe: list[str],
        date_range: dict[str, str],
        execution_cost_model: str = "slippage_v1",
        replay_resolution: str = "1m",
        purge_bars: int | None = None,
        embargo_bars: int | None = None,
        caller: str = "BacktestAgent",
    ) -> dict[str, Any]:
        """C13 BacktestEngine.run — walk-forward 백테스트 실행.

        Args:
            bundle_ref: 후보 bundle 식별자.
            baseline_ref: 비교 baseline 식별자.
            universe: 6자리 종목코드 리스트.
            date_range: {"start": ISO8601, "end": ISO8601}.
            execution_cost_model: "slippage_v1". risk_config.yaml 경유.
            replay_resolution: "1m" (고정).
            purge_bars: None 이면 risk_config.yaml 값 사용.
            embargo_bars: None 이면 risk_config.yaml 값 사용.
            caller: 호출자 식별자. forbidden_callers 8개에 속하면 ForbiddenCaller raise.
                    정상 호출자: "BacktestAgent", "ModeBScheduler" 등.

        Returns:
            C13 BacktestResult 딕셔너리.

        Raises:
            ForbiddenCaller: caller가 C13 forbidden_callers 목록에 포함된 경우.
            BundleLoadFailed: bundle_ref가 빈 문자열이거나 잘못된 경우.
            DataUnavailable: date_range 파싱 실패.
            LeakageDetected: purge/embargo 조건 위반.
            NaNInMetrics: metrics 계산 결과에 NaN 포함.
        """
        _check_caller(caller)
        started_at = datetime.now(_KST).isoformat()
        run_id = generate_backtest_id()

        logger.info(
            "[BacktestEngine] run 시작. run_id=%s bundle=%s baseline=%s",
            run_id, bundle_ref, baseline_ref,
        )

        # 입력 검증
        if not bundle_ref or not bundle_ref.strip():
            raise BundleLoadFailed(f"bundle_ref가 비어있음: {bundle_ref!r}")

        try:
            start_dt = datetime.fromisoformat(date_range["start"])
            end_dt = datetime.fromisoformat(date_range["end"])
        except Exception as e:
            raise DataUnavailable(f"date_range 파싱 실패: {e}") from e

        if start_dt >= end_dt:
            raise DataUnavailable(
                f"start >= end: {date_range['start']} >= {date_range['end']}"
            )

        # purge_bars / embargo_bars 로드 (None 이면 yaml SSOT)
        _purge = purge_bars if purge_bars is not None else int(self._cfg_vt["purge_bars"])
        _embargo = embargo_bars if embargo_bars is not None else int(self._cfg_vt["embargo_bars"])

        # 종목코드 정규화
        padded_universe = [pad_ticker(t) for t in universe]

        # walk-forward fold 생성
        folds = self._build_folds(start_dt, end_dt, _purge, _embargo)

        if not folds:
            raise DataUnavailable(
                "walk-forward fold 생성 실패: date_range가 너무 짧거나 파라미터 불일치"
            )

        # 각 fold 실행 + 결과 수집
        fold_results = self._run_folds(folds, padded_universe, _purge, _embargo)

        # 집계 metrics 계산
        metrics = self._aggregate_metrics(fold_results)

        # NaN 검증
        self._check_nan(metrics)

        # trade_log, daily_pnl, bar_count 집계
        daily_pnl: list[float] = []
        trade_log: list[dict[str, Any]] = []
        bar_count = 0

        for fr in fold_results:
            daily_pnl.extend(fr["daily_pnl"])
            trade_log.extend(fr["trade_log"])
            bar_count += fr["bar_count"]

        finished_at = datetime.now(_KST).isoformat()

        result: dict[str, Any] = {
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "metrics": metrics,
            "daily_pnl": daily_pnl,
            "trade_log": trade_log,
            "bar_count": bar_count,
            # C13 rules.result_persistence: "KB with TTL=30days"
            # Sprint 4 KB 통합 시 KnowledgeBase.write(result, "backtest_history") + 30일 후 자동 삭제.
            # 현재는 메타 필드만 명시 (실 KB write 없음).
            "_persistence_target": "kb_30d",  # TODO Sprint 4: KB integration
        }

        logger.info(
            "[BacktestEngine] 완료. run_id=%s bar_count=%d ic=%.4f sr=%.4f",
            run_id, bar_count, metrics["ic"], metrics["sr"],
        )
        return result

    # ────────────────────────────────────────────────────
    # walk-forward fold 생성
    # ────────────────────────────────────────────────────

    def _build_folds(
        self,
        start_dt: datetime,
        end_dt: datetime,
        purge_bars: int,
        embargo_bars: int,
    ) -> list[dict[str, Any]]:
        """walk-forward fold 목록 생성.

        각 fold:
          train: [fold_start, train_end)
          purge 버퍼: train_end ~ train_end + purge_bars 분
          embargo 버퍼: train_end + purge_bars ~ train_end + purge_bars + embargo_bars 분
          test: [test_start, test_end)

        PIT-Safety 강제: train_end + purge_bars + embargo_bars <= test_start.
        """
        n_splits = int(self._cfg_wf["n_splits"])
        train_days = int(self._cfg_wf["train_window_days"])
        test_days = int(self._cfg_wf["test_window_days"])
        step_days = int(self._cfg_wf.get("step_days", test_days))

        purge_days = math.ceil(purge_bars / 390)   # 390분봉 = 1거래일
        embargo_days = math.ceil(embargo_bars / 390)

        folds = []
        for i in range(n_splits):
            fold_start = start_dt + timedelta(days=i * step_days)
            train_end = fold_start + timedelta(days=train_days)
            test_start = train_end + timedelta(days=purge_days + embargo_days)
            test_end = test_start + timedelta(days=test_days)

            if test_end > end_dt:
                break

            # Leakage 검증: test_start >= train_end + purge + embargo
            buffer_end = train_end + timedelta(days=purge_days + embargo_days)
            if test_start < buffer_end:
                raise LeakageDetected(
                    f"fold {i}: test_start={test_start.date()} < "
                    f"buffer_end={buffer_end.date()} "
                    f"(purge={purge_bars}bars, embargo={embargo_bars}bars)"
                )

            folds.append({
                "fold_idx": i,
                "fold_start": fold_start,
                "train_end": train_end,
                "test_start": test_start,
                "test_end": test_end,
                "purge_bars": purge_bars,
                "embargo_bars": embargo_bars,
            })

        return folds

    # ────────────────────────────────────────────────────
    # 각 fold 실행
    # ────────────────────────────────────────────────────

    def _run_folds(
        self,
        folds: list[dict[str, Any]],
        universe: list[str],
        purge_bars: int,
        embargo_bars: int,
    ) -> list[dict[str, Any]]:
        """각 fold에 대해 시뮬레이션 실행. fold_result 목록 반환."""
        import numpy as np
        rng = np.random.default_rng(self._seed)

        fold_results = []
        for fold in folds:
            fr = self._run_single_fold(fold, universe, rng)
            fold_results.append(fr)

        return fold_results

    def _run_single_fold(
        self,
        fold: dict[str, Any],
        universe: list[str],
        rng: Any,
    ) -> dict[str, Any]:
        """단일 fold 시뮬레이션.

        모델이 없으면 rng 기반 예측 stub 사용 (테스트용).
        NaN 입력 탐지 시 NaNInMetrics 즉시 raise.
        """
        test_start = fold["test_start"]
        test_end = fold["test_end"]
        test_days = max(1, (test_end - test_start).days)
        trading_minutes = int(self._cfg_wf["trading_minutes_per_day"])

        # 거래비용 파라미터 (risk_config.yaml SSOT)
        commission_bps = float(self._cfg_cost["components"]["commission_bps"])
        slippage_bps = float(self._cfg_cost["components"]["slippage_bps"])
        total_cost_bps = (commission_bps + slippage_bps) / 10000.0  # bps → 비율

        daily_pnl: list[float] = []
        trade_log: list[dict[str, Any]] = []
        bar_count_fold = 0

        predicted_signals: list[float] = []
        actual_returns: list[float] = []

        for day_offset in range(test_days):
            day = test_start + timedelta(days=day_offset)
            day_pnl = 0.0

            for bar_idx in range(trading_minutes):
                for ticker in universe:
                    # 예측 시그널 생성
                    if self._model_callable is not None:
                        pred = float(self._model_callable(
                            rng.normal(0, 1, size=4).tolist()
                        ))
                    else:
                        pred = float(rng.normal(0, 1))

                    # NaN 탐지
                    if math.isnan(pred):
                        raise NaNInMetrics(
                            f"예측값 NaN: ticker={ticker} "
                            f"fold={fold['fold_idx']} day_offset={day_offset}"
                        )

                    # 실제 수익률 (synthetic: 예측과 약한 상관. 파라미터는 risk_config.yaml SSOT)
                    actual_ret = (
                        pred * self._mock_signal_return_corr
                        + float(rng.normal(0, self._mock_return_noise_std))
                    )

                    predicted_signals.append(pred)
                    actual_returns.append(actual_ret)

                    # 시그널이 양수면 매수, 음수면 매도
                    side = "buy" if pred > 0 else "sell"
                    qty = 1
                    price = self._mock_base_price + float(rng.normal(0, self._mock_price_noise_std))
                    slippage_amt = price * (slippage_bps / 10000.0)

                    # 거래비용 반영 수익률
                    gross_ret = actual_ret
                    net_ret = (1 + gross_ret) * (1 - total_cost_bps) - 1
                    day_pnl += net_ret * qty * price

                    trade_log.append({
                        "ticker": ticker,
                        "side": side,
                        "qty": qty,
                        "price": price,
                        "ts": (
                            day.replace(
                                hour=9, minute=bar_idx % 60,
                                second=0, microsecond=0,
                            ).isoformat()
                        ),
                        "slippage": slippage_amt,
                    })
                    bar_count_fold += 1

            daily_pnl.append(day_pnl)

        return {
            "fold_idx": fold["fold_idx"],
            "predicted_signals": predicted_signals,
            "actual_returns": actual_returns,
            "daily_pnl": daily_pnl,
            "trade_log": trade_log,
            "bar_count": bar_count_fold,
        }

    # ────────────────────────────────────────────────────
    # 집계 metrics 계산
    # ────────────────────────────────────────────────────

    def _aggregate_metrics(self, fold_results: list[dict[str, Any]]) -> dict[str, float]:
        """C13 metrics 7종 계산.

        ic, icir, rank_ic, arr, ir, mdd, sr
        """
        all_pred: list[float] = []
        all_actual: list[float] = []
        all_daily_pnl: list[float] = []

        for fr in fold_results:
            all_pred.extend(fr["predicted_signals"])
            all_actual.extend(fr["actual_returns"])
            all_daily_pnl.extend(fr["daily_pnl"])

        n = len(all_pred)
        if n == 0:
            raise DataUnavailable("집계할 시그널 없음 (fold_results 전부 비어있음)")

        # IC = Pearson(predicted, actual)
        ic = self._pearsonr(all_pred, all_actual)

        # ICIR: fold별 IC 분산 기반
        fold_ics = [
            self._pearsonr(fr["predicted_signals"], fr["actual_returns"])
            for fr in fold_results
            if len(fr["predicted_signals"]) > 1
        ]
        if len(fold_ics) > 1:
            mean_ic = sum(fold_ics) / len(fold_ics)
            std_ic = self._std(fold_ics)
            min_std = float(self._cfg_eval.get("min_daily_pnl_std", 1e-8))
            icir = mean_ic / max(std_ic, min_std)
        elif fold_ics:
            icir = fold_ics[0]
        else:
            icir = 0.0

        # Rank IC = Spearman(rank_pred, rank_actual)
        rank_ic = self._spearmanr(all_pred, all_actual)

        # 연율화 파라미터 (risk_config.yaml SSOT)
        ann_factor = int(self._cfg_eval.get("annualization_factor", 252))
        min_pnl_std = float(self._cfg_eval.get("min_daily_pnl_std", 1e-8))

        # ARR: daily_pnl은 dollar PnL → initial_capital으로 나눠 수익률 비율로 변환.
        # (1 + dollar_pnl)^x 는 단위 혼용 오류. 수익률 비율 기반으로 연율화.
        total_days = len(all_daily_pnl)
        if total_days == 0:
            raise DataUnavailable("daily_pnl이 비어있어 ARR 계산 불가")

        total_return_ratio = sum(all_daily_pnl) / max(self._initial_capital, 1.0)
        years = total_days / max(ann_factor, 1)
        arr = (1 + total_return_ratio) ** (1.0 / max(years, 1.0 / ann_factor)) - 1

        # IR = mean(daily_pnl) / std(daily_pnl) * sqrt(252)
        # 이전 구현 버그: arr(이미 연율화된 값) / std * sqrt(252) → ~252배 과대평가.
        # 올바른 계산: mean_daily / std_daily * sqrt(ann_factor).
        # SR과 동일한 형태이나 SR은 무위험수익률 차감 없는 원시 Sharpe이고,
        # IR은 정보 비율로 같은 공식 사용. 2026-05-01 S3 Tier 1 수정.
        std_pnl = self._std(all_daily_pnl)
        mean_pnl = sum(all_daily_pnl) / max(len(all_daily_pnl), 1)
        ir = mean_pnl / max(std_pnl, min_pnl_std) * math.sqrt(ann_factor)

        # MDD = min(cumulative / running_max - 1)
        mdd = self._max_drawdown(all_daily_pnl)

        # SR = mean(daily_pnl) / std(daily_pnl) * sqrt(252)
        sr = mean_pnl / max(std_pnl, min_pnl_std) * math.sqrt(ann_factor)

        return {
            "ic": ic,
            "icir": icir,
            "rank_ic": rank_ic,
            "arr": arr,
            "ir": ir,
            "mdd": mdd,
            "sr": sr,
        }

    # ────────────────────────────────────────────────────
    # NaN 검증
    # ────────────────────────────────────────────────────

    def _check_nan(self, metrics: dict[str, float]) -> None:
        """metrics 딕셔너리에 NaN 포함 시 NaNInMetrics raise."""
        nan_keys = [k for k, v in metrics.items() if math.isnan(v)]
        if nan_keys:
            raise NaNInMetrics(f"metrics NaN 발견: {nan_keys}")

    # ────────────────────────────────────────────────────
    # 통계 헬퍼 (외부 의존성 최소화)
    # ────────────────────────────────────────────────────

    @staticmethod
    def _mean(xs: list[float]) -> float:
        if not xs:
            return 0.0
        return sum(xs) / len(xs)

    @staticmethod
    def _std(xs: list[float]) -> float:
        if len(xs) < 2:
            return 0.0
        m = sum(xs) / len(xs)
        return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))

    @classmethod
    def _pearsonr(cls, xs: list[float], ys: list[float]) -> float:
        """Pearson 상관계수. 데이터 부족 시 0.0 반환."""
        n = len(xs)
        if n < 2 or len(ys) != n:
            return 0.0
        mx, my = cls._mean(xs), cls._mean(ys)
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        sy = math.sqrt(sum((y - my) ** 2 for y in ys))
        denom = sx * sy
        if denom < 1e-12:
            return 0.0
        return cov / denom

    @staticmethod
    def _rank(xs: list[float]) -> list[float]:
        """순위 배열 반환 (1-based, ties: average)."""
        sorted_idx = sorted(range(len(xs)), key=lambda i: xs[i])
        ranks = [0.0] * len(xs)
        i = 0
        while i < len(sorted_idx):
            j = i
            while j + 1 < len(sorted_idx) and xs[sorted_idx[j + 1]] == xs[sorted_idx[i]]:
                j += 1
            avg_rank = (i + j) / 2 + 1
            for k in range(i, j + 1):
                ranks[sorted_idx[k]] = avg_rank
            i = j + 1
        return ranks

    @classmethod
    def _spearmanr(cls, xs: list[float], ys: list[float]) -> float:
        """Spearman 순위 상관계수."""
        if len(xs) < 2 or len(xs) != len(ys):
            return 0.0
        return cls._pearsonr(cls._rank(xs), cls._rank(ys))

    @staticmethod
    def _max_drawdown(daily_pnl: list[float]) -> float:
        """MDD 계산. 음수 반환 (risk_config.yaml evaluation.mdd_sign='negative').

        peak=0 초기화: 누적 PnL이 아직 양수에 도달하지 않은 경우 high water mark가
        없으므로 drawdown 계산을 skip한다. peak > 0인 시점부터 비율 기반 MDD 측정.
        이전 구현의 `max(abs(peak), 1e-10)` 분모는 peak=0 시 -1e10 생산 버그.
        2026-05-01 S3 Tier 1 수정.
        """
        if not daily_pnl:
            return 0.0
        cum = 0.0
        peak = 0.0
        mdd = 0.0
        for pnl in daily_pnl:
            cum += pnl
            if cum > peak:
                peak = cum
            # high water mark가 양수일 때만 비율 기반 drawdown 계산
            if peak > 0:
                dd = (cum - peak) / peak
                if dd < mdd:
                    mdd = dd
        return mdd


class ReplayRunnerError(RuntimeError):
    """ReplayRunner 기반 예외."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"[{code}] {detail}")


class ReplayDivergence(ReplayRunnerError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("REPLAY_DIVERGENCE", detail)


class SeedMismatch(ReplayRunnerError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("SEED_MISMATCH", detail)


class EventReplayFailed(ReplayRunnerError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("EVENT_REPLAY_FAILED", detail)


# 6개 event_sources (C13 SSOT)
_VALID_SOURCES = frozenset({
    "naver_news",
    "dart",
    "community",
    "us_market",
    "ecos",
    "krx_investor_flow",
})

# agent_activation_count 분류 규칙
_NEWS_SOURCES = frozenset({"naver_news", "dart", "community"})
_RISK_SOURCES = frozenset({"us_market", "ecos", "krx_investor_flow"})


class ReplayRunner:
    """C13 ValidationToolsContract 리플레이 실행기.

    과거 이벤트 스트림을 순서대로 결정론적으로 재생.
    에이전트 activation count + latency 분포 + anomaly 집계.

    설계 원칙:
      - PIT-Safety: replay_now(ts 기준) 이후 데이터 절대 미접근.
      - deterministic: seed 기반 random state 고정, 같은 input → 100% 동일 output.
      - LLM 미호출: agent 호출 횟수만 카운트, 실 LLM 호출 없음.
      - Mode B 전용: @mode_b_only 데코레이터 강제.
      - idempotent: side effect 없음, 메모리 상태 reset, 파일 미생성.
      - 하드코딩 금지: risk_config.yaml validation_tools.replay_runner 섹션 로드.

    Args:
        seed: 재현성용 랜덤 시드 (int, 필수).
    """

    def __init__(self, seed: int = 42) -> None:
        if not isinstance(seed, int):
            raise SeedMismatch(f"seed는 int여야 함: {seed!r} ({type(seed).__name__})")
        self._seed = seed
        self._cfg = config_load("risk_config.yaml", "validation_tools.replay_runner")

    # ────────────────────────────────────────────────────
    # public API
    # ────────────────────────────────────────────────────

    @mode_b_only
    def run(
        self,
        bundle_ref: str,
        date_range: dict[str, str],
        event_sources: list[str] | None = None,
        mode: str = "deterministic_replay",
        seed: int | None = None,
        caller: str = "BacktestAgent",
    ) -> dict[str, Any]:
        """C13 ReplayRunner.run — 결정론적 이벤트 재생 실행.

        Args:
            bundle_ref: 후보 bundle 식별자.
            date_range: {"start": ISO8601, "end": ISO8601}.
            event_sources: 6개 source 목록. None 이면 기본 6개 전부 사용.
            mode: "deterministic_replay" 고정.
            seed: None 이면 self._seed 사용. int 아니면 SeedMismatch.
            caller: 호출자 식별자. forbidden_callers 8개에 속하면 ForbiddenCaller raise.

        Returns:
            C13 ReplayRunner output 딕셔너리.

        Raises:
            ForbiddenCaller: caller가 C13 forbidden_callers 목록에 포함된 경우.
            SeedMismatch: seed가 None or non-int.
            EventReplayFailed: 이벤트 처리 중 예외.
            ReplayDivergence: 같은 seed로 두 번 실행 시 결과 불일치.
        """
        _check_caller(caller)
        # seed 검증 (불변 원칙: 하드코딩 금지 아닌 입력 검증)
        effective_seed = seed if seed is not None else self._seed
        if not isinstance(effective_seed, int):
            raise SeedMismatch(
                f"seed는 int여야 함: {effective_seed!r} ({type(effective_seed).__name__})"
            )

        # event_sources 기본값 처리
        if event_sources is None:
            event_sources = list(_VALID_SOURCES)

        logger.info(
            "[ReplayRunner] run 시작. bundle=%s seed=%d sources=%s",
            bundle_ref, effective_seed, event_sources,
        )

        # date_range 파싱
        try:
            start_dt = datetime.fromisoformat(date_range["start"])
            end_dt = datetime.fromisoformat(date_range["end"])
        except Exception as e:
            raise EventReplayFailed(f"date_range 파싱 실패: {e}") from e

        if start_dt >= end_dt:
            raise EventReplayFailed(
                f"start >= end: {date_range['start']} >= {date_range['end']}"
            )

        # deterministic rng 초기화 (seed 고정)
        import numpy as np
        rng = np.random.default_rng(effective_seed)

        # 이벤트 스트림 생성 (결정론적)
        events = self._build_event_stream(start_dt, end_dt, event_sources, rng)

        # 이벤트 처리 + agent activation count 집계
        try:
            activation, anomaly_count = self._process_events(events, rng)
        except ReplayRunnerError:
            raise
        except Exception as e:
            raise EventReplayFailed(f"이벤트 처리 중 예외: {e}") from e

        # quant: Hot Path QuantAgent는 1분봉마다 실행. 이벤트 기반이 아닌 bar 수 기반.
        # C13 api_contracts.md agent_activation_count.quant: int.
        # 1거래일 = 390분봉 (09:00~15:30 KST). 주말 제외 정확한 영업일 대신 단순 일수 기준.
        # (start_dt, end_dt) 날짜 차이 × 390. Sprint 4에서 영업일 카운트로 교체 가능.
        replay_days = max(1, (end_dt - start_dt).days)
        trading_minutes_per_day = int(
            self._cfg.get("trading_minutes_per_day", 390)
        )
        activation["quant"] = replay_days * trading_minutes_per_day

        # latency 시뮬레이션 (결정론적 분포, seed 기반)
        cold_latency = self._simulate_latency(
            rng=rng,
            n_samples=max(len(events), 100),
            p50_center=15000,
            spread=5000,
        )
        hot_latency = self._simulate_latency(
            rng=rng,
            n_samples=max(len(events) * 10, 1000),
            p50_center=50,
            spread=30,
        )

        # run_id: RPT-{yyyymmdd}-{UUID8} (api_contracts.md L34 SSOT)
        run_id = generate_replay_id()

        # replay_trace_ref: KB artifact 경로 (파일 미생성, 경로만 반환)
        replay_trace_ref = f"artifacts/replay/{run_id}.jsonl"

        result: dict[str, Any] = {
            "run_id": run_id,
            "replay_trace_ref": replay_trace_ref,
            "agent_activation_count": activation,
            "cold_path_latency": cold_latency,
            "hot_path_latency": hot_latency,
            "anomaly_count": anomaly_count,
        }

        logger.info(
            "[ReplayRunner] 완료. run_id=%s events=%d anomaly=%d",
            run_id, len(events), anomaly_count,
        )
        return result

    # ────────────────────────────────────────────────────
    # 이벤트 스트림 생성 (결정론적)
    # ────────────────────────────────────────────────────

    def _build_event_stream(
        self,
        start_dt: datetime,
        end_dt: datetime,
        event_sources: list[str],
        rng: Any,
    ) -> list[dict[str, Any]]:
        """date_range + sources 기반 결정론적 이벤트 목록 생성.

        PIT-Safety: 생성된 이벤트의 ts <= replay_now(=end_dt). 미래 참조 없음.
        안정 정렬(stable sort): ts 기준 오름차순.
        """
        total_seconds = max(int((end_dt - start_dt).total_seconds()), 1)
        events: list[dict[str, Any]] = []

        for src in event_sources:
            if src not in _VALID_SOURCES:
                raise EventReplayFailed(f"알 수 없는 event_source: {src!r}")

            # source당 이벤트 수: 결정론적 (rng 사용)
            n_events = int(rng.integers(3, 8))  # 3~7개

            for _ in range(n_events):
                offset_sec = int(rng.integers(0, total_seconds))
                ts = (start_dt + timedelta(seconds=offset_sec)).isoformat()
                events.append({
                    "source": src,
                    "ts": ts,
                    "payload": {
                        "score": float(rng.uniform(-1.0, 1.0)),
                        "volume_z": float(rng.standard_normal()),
                    },
                })

        # 안정 정렬: ts 기준 오름차순
        events.sort(key=lambda e: e["ts"])
        return events

    # ────────────────────────────────────────────────────
    # 이벤트 처리 + agent activation count
    # ────────────────────────────────────────────────────

    def _process_events(
        self,
        events: list[dict[str, Any]],
        rng: Any,
    ) -> tuple[dict[str, int], int]:
        """이벤트 스트림 처리 → activation count + anomaly count.

        agent_activation_count 분류 규칙 (C13 SSOT):
          - news:   naver_news + dart + community 이벤트 수
          - risk:   us_market + ecos + krx_investor_flow + community anomaly 트리거
          - debate: news_signal + risk_warning이 같은 ticker에 동시 발생 시뮬
          - fda:    cold path에서 처리된 전체 이벤트 수 (approve/veto 시뮬)

        quant: _process_events는 이벤트 기반 4개만 반환. quant는 run() 호출 지점에서
               date_range 일수 × 390분봉으로 별도 계산 후 activation dict에 추가.
               C13 api_contracts.md agent_activation_count.quant: int.

        PIT-Safety: 이벤트 ts > replay_now 참조하지 않음 (이미 build_event_stream에서 보장).
        """
        news_count = 0
        risk_count = 0
        anomaly_count = 0
        fda_count = 0

        # ticker별 동시 발생 추적 (debate 트리거 시뮬)
        ticker_news: set[str] = set()
        ticker_risk: set[str] = set()

        for event in events:
            src = event.get("source", "")
            payload = event.get("payload", {})

            # source별 분류
            if src in _NEWS_SOURCES:
                news_count += 1
                ticker_news.add(src)  # src를 proxy ticker로 사용 (실 ticker 없음)
                fda_count += 1  # Cold Path FDA 처리 대상

            elif src in _RISK_SOURCES:
                risk_count += 1
                ticker_risk.add(src)
                fda_count += 1

            # anomaly 탐지: community 이상 또는 risk volume spike
            volume_z = payload.get("volume_z", 0.0)
            score = payload.get("score", 0.0)
            if src == "community" and abs(volume_z) > 2.0:
                anomaly_count += 1
                risk_count += 1  # community anomaly → risk 트리거 추가

            elif src in _RISK_SOURCES and abs(score) > 0.8:
                anomaly_count += 1

        # debate: news + risk source 겹침 시뮬 (ticker_news ∩ ticker_risk 유사 개념)
        # 실제 ticker가 없으므로 비율 기반 시뮬
        debate_count = int(len(ticker_news & ticker_risk))
        if debate_count == 0 and news_count > 0 and risk_count > 0:
            # 이벤트가 있으면 최소 1번 debate 발생으로 시뮬
            debate_count = 1

        activation: dict[str, int] = {
            "news": news_count,
            "risk": risk_count,
            "debate": debate_count,
            "fda": fda_count,
        }

        return activation, anomaly_count

    # ────────────────────────────────────────────────────
    # latency 시뮬레이션 (결정론적)
    # ────────────────────────────────────────────────────

    def _simulate_latency(
        self,
        rng: Any,
        n_samples: int,
        p50_center: int,
        spread: int,
    ) -> dict[str, int]:
        """seed 기반 결정론적 latency 분포 → p50/p95/p99.

        반드시 p50 <= p95 <= p99 보장 (numpy percentile 속성).
        단위: ms (hot_path) 또는 ms (cold_path, 스펙은 ms 단위).
        """
        import numpy as np

        samples = rng.normal(loc=p50_center, scale=spread, size=n_samples)
        samples = np.abs(samples)  # 음수 방지

        p50 = int(np.percentile(samples, 50))
        p95 = int(np.percentile(samples, 95))
        p99 = int(np.percentile(samples, 99))

        # 정렬 보장 (floating point rounding 등 edge case 방어)
        p50 = max(p50, 1)
        p95 = max(p95, p50)
        p99 = max(p99, p95)

        return {"p50": p50, "p95": p95, "p99": p99}

    # ────────────────────────────────────────────────────
    # REPLAY_DIVERGENCE 검증 (idempotent 보장용 내부 유틸)
    # ────────────────────────────────────────────────────

    def _verify_idempotent(
        self,
        result_a: dict[str, Any],
        result_b: dict[str, Any],
    ) -> None:
        """두 실행 결과의 run_id 제외 필드 비교. 불일치 시 ReplayDivergence.

        사용 패턴: 외부 호출자가 같은 input으로 두 번 run()해서 이 메서드로 검증.
        정상 path에서는 절대 raise되지 않아야 함 (idempotent: true).
        """
        # run_id는 UUID 기반이므로 매 실행마다 다름 → 비교 제외
        check_keys = ["agent_activation_count", "cold_path_latency", "hot_path_latency", "anomaly_count"]
        for key in check_keys:
            if result_a.get(key) != result_b.get(key):
                raise ReplayDivergence(
                    f"key={key!r}: run1={result_a.get(key)} != run2={result_b.get(key)}"
                )


class PerformanceAnalyzerError(RuntimeError):
    """PerformanceAnalyzer 기반 예외."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"[{code}] {detail}")


class BaselineMissing(PerformanceAnalyzerError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("BASELINE_MISSING", detail)


class RegimeLabelMissing(PerformanceAnalyzerError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("REGIME_LABEL_MISSING", detail)


class AblationInfeasible(PerformanceAnalyzerError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("ABLATION_INFEASIBLE", detail)


# 허용 ablation component 목록 (C13 SSOT, v3.5: dual_source 추가. risk_config.yaml 동기화 완료)
_VALID_ABLATION_COMPONENTS = frozenset({"factor", "model", "allocator", "dual_source"})


class PerformanceAnalyzer:
    """C13 ValidationToolsContract 성과 분석기.

    regime breakdown + ablation + baseline 대비 비교 + regression_risk 판정.

    설계 원칙:
      - PIT-Safety: baseline_run_id 시점 < 분석기 호출 시점 (mock 구현에서 입력 검증).
      - FDA can_change_weight=false: 분석만 수행. 비중 수정 없음.
      - Mode B 전용: @mode_b_only 데코레이터 강제.
      - LLM 미호출: 순수 deterministic 계산.
      - 하드코딩 금지: 모든 임계값은 risk_config.yaml에서 로드.
      - BT 결과 로딩: Sprint 4 실구현 전 mock. optional metrics_override로 직접 주입 가능.

    Args:
        seed: ablation 유의성 계산용 결정론적 시드.
    """

    def __init__(self, seed: int = 42) -> None:
        self._seed = seed
        self._cfg_pa = config_load("risk_config.yaml", "validation_tools.performance_analyzer")
        self._cfg_eval = config_load("risk_config.yaml", "evaluation")
        self._cfg_ba = config_load("risk_config.yaml", "backtest_agent")
        # verdict 임계값: risk_config.yaml에서 로드 (하드코딩 금지)
        self._verdict_improve_sharpe = float(
            self._cfg_pa.get("verdict_improve_sharpe_threshold", 0.1)
        )
        self._verdict_degrade_sharpe = float(
            self._cfg_pa.get("verdict_degrade_sharpe_threshold", -0.1)
        )
        self._verdict_degrade_mdd = float(
            self._cfg_pa.get("verdict_degrade_mdd_threshold", -0.05)
        )
        # regression_risk 임계값
        self._reg_sharpe_drop = float(
            self._cfg_pa.get("regression_sharpe_drop_threshold", -0.2)
        )
        self._reg_mdd_drop = float(
            self._cfg_pa.get("regression_mdd_drop_threshold", -0.10)
        )
        self._reg_bear_sharpe_floor = float(
            self._cfg_pa.get("regression_bear_sharpe_floor", -1.0)
        )
        self._reg_dual_source_counterproductive = float(
            self._cfg_pa.get("regression_dual_source_counterproductive", 0.3)
        )
        # 허용 ablation components (yaml 우선, fallback = C13 고정 목록)
        yaml_components = self._cfg_pa.get("ablation_components", [])
        self._cfg_ablation_components: frozenset[str] = (
            frozenset(yaml_components) | frozenset({"dual_source"})
            if yaml_components
            else _VALID_ABLATION_COMPONENTS
        )
        # valid regime 목록
        self._valid_regimes: list[str] = list(
            self._cfg_pa.get(
                "regime_labels",
                self._cfg_eval.get("regime_labels", ["bull", "bear", "sideways", "volatile"]),
            )
        )

    # ────────────────────────────────────────────────────
    # public API
    # ────────────────────────────────────────────────────

    @mode_b_only
    def analyze(
        self,
        backtest_run_id: str,
        baseline_run_id: str | None,
        regime_labels: list[dict[str, str]],
        ablation_components: list[str] | None = None,
        *,
        caller: str = "BacktestAgent",
        metrics_override: dict[str, Any] | None = None,
        baseline_metrics_override: dict[str, Any] | None = None,
        daily_pnl_override: list[float] | None = None,
    ) -> dict[str, Any]:
        """C13 PerformanceAnalyzer.analyze — 성과 분석 실행.

        Args:
            backtest_run_id: BT-yyyymmdd-uuid8 형식의 backtest 실행 ID.
            baseline_run_id: 비교 기준 baseline 실행 ID. None 이면 BASELINE_MISSING.
            regime_labels: [{"date": ISO8601, "regime": "bull|bear|sideways|volatile"}, ...].
            ablation_components: 끄고 비교할 component 목록.
                                  None 이면 risk_config.yaml 목록 + dual_source.
            caller: 호출자 식별자. forbidden_callers 8개에 속하면 ForbiddenCaller raise.
            metrics_override: Sprint 4 전 테스트/mock용 직접 주입 metrics.
                               None 이면 backtest_run_id 기반 mock 생성.
            baseline_metrics_override: baseline metrics 직접 주입.
            daily_pnl_override: regime breakdown용 일별 PnL 직접 주입.

        Returns:
            C13 PerformanceAnalyzer output 딕셔너리.

        Raises:
            ForbiddenCaller: caller가 C13 forbidden_callers 목록에 포함된 경우.
            BaselineMissing: baseline_run_id가 None.
            RegimeLabelMissing: backtest 기간 날짜 중 regime_label 없는 날 존재.
            AblationInfeasible: ablation_components에 유효하지 않은 값 포함.
        """
        _check_caller(caller)
        _pa_logger = get_logger("PerformanceAnalyzer")
        _pa_logger.info(
            "[PerformanceAnalyzer] analyze 시작. bt_run=%s baseline=%s",
            backtest_run_id,
            baseline_run_id,
        )

        # 1. baseline_run_id 검증 (BASELINE_MISSING)
        if not baseline_run_id:
            raise BaselineMissing(
                f"baseline_run_id가 None 또는 빈 문자열: {baseline_run_id!r}"
            )

        # 2. ablation_components 검증 (ABLATION_INFEASIBLE)
        _components = ablation_components if ablation_components is not None else list(
            self._cfg_ablation_components
        )
        invalid_comps = [c for c in _components if c not in _VALID_ABLATION_COMPONENTS]
        if invalid_comps:
            raise AblationInfeasible(
                f"유효하지 않은 ablation component: {invalid_comps}. "
                f"허용 목록: {sorted(_VALID_ABLATION_COMPONENTS)}"
            )

        # 3. regime_labels 파싱 → date → regime 매핑
        regime_map = self._build_regime_map(regime_labels)

        # 4. backtest metrics 로드 (mock or override)
        bt_metrics = metrics_override if metrics_override is not None else (
            self._mock_metrics_from_run_id(backtest_run_id)
        )
        baseline_metrics = baseline_metrics_override if baseline_metrics_override is not None else (
            self._mock_metrics_from_run_id(baseline_run_id, is_baseline=True)
        )

        # 5. daily_pnl 로드 (mock or override)
        daily_pnl_with_dates = daily_pnl_override if daily_pnl_override is not None else (
            self._mock_daily_pnl(backtest_run_id, regime_map)
        )

        # 6. regime_labels 완전성 검증 (REGIME_LABEL_MISSING)
        # daily_pnl_with_dates 의 날짜들이 regime_map 에 있어야 함
        if isinstance(daily_pnl_with_dates, list) and daily_pnl_with_dates:
            missing_dates = self._check_regime_coverage(
                daily_pnl_with_dates,
                regime_map,
                backtest_run_id,
            )
            if missing_dates:
                raise RegimeLabelMissing(
                    f"backtest 기간 날짜에 regime_label 없음: {missing_dates[:5]}"
                )

        # 7. regime_breakdown 계산
        regime_breakdown = self._compute_regime_breakdown(
            daily_pnl_with_dates, regime_map
        )

        # 8. ablation 계산
        ablation = self._compute_ablation(
            _components, bt_metrics, backtest_run_id
        )

        # 9. baseline_comparison 계산
        baseline_comparison = self._compute_baseline_comparison(
            bt_metrics, baseline_metrics
        )

        # 10. regression_risk 판정
        regression_risk = self._compute_regression_risk(
            baseline_comparison, regime_breakdown, ablation
        )

        # 11. run_id 생성
        run_id = generate_pa_id()

        result: dict[str, Any] = {
            "run_id": run_id,
            "regime_breakdown": regime_breakdown,
            "ablation": ablation,
            "baseline_comparison": baseline_comparison,
            "regression_risk": regression_risk,
        }

        _pa_logger.info(
            "[PerformanceAnalyzer] 완료. run_id=%s verdict=%s flagged=%s",
            run_id,
            baseline_comparison.get("verdict"),
            regression_risk.get("flagged"),
        )
        return result

    # ────────────────────────────────────────────────────
    # regime_breakdown
    # ────────────────────────────────────────────────────

    def _build_regime_map(
        self, regime_labels: list[dict[str, str]]
    ) -> dict[str, str]:
        """regime_labels → {date_str: regime} 매핑 생성.

        date_str 형식: "YYYY-MM-DD".
        """
        regime_map: dict[str, str] = {}
        for entry in regime_labels:
            raw_date = entry.get("date", "")
            regime = entry.get("regime", "")
            if not raw_date or not regime:
                continue
            # ISO8601 → date-only 부분 추출
            date_str = raw_date[:10]  # "YYYY-MM-DD"
            regime_map[date_str] = regime
        return regime_map

    def _mock_daily_pnl(
        self,
        run_id: str,
        regime_map: dict[str, str],
    ) -> list[dict[str, Any]]:
        """backtest_run_id seed 기반 결정론적 daily_pnl 생성.

        Returns: [{"date": "YYYY-MM-DD", "pnl": float}, ...]
        """
        import hashlib
        import random

        seed = int(hashlib.md5(run_id.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)

        # regime_map에 등록된 날짜 기준으로 pnl 생성
        if regime_map:
            dates = sorted(regime_map.keys())
            result = []
            for d in dates:
                pnl = rng.gauss(0.001, 0.02)
                result.append({"date": d, "pnl": pnl})
            return result

        # regime_map 없으면 30일 mock
        base_date = datetime(2026, 1, 2)
        result = []
        for i in range(30):
            d = (base_date + timedelta(days=i)).strftime("%Y-%m-%d")
            pnl = rng.gauss(0.001, 0.02)
            result.append({"date": d, "pnl": pnl})
        return result

    def _check_regime_coverage(
        self,
        daily_pnl: list[dict[str, Any]] | list[float],
        regime_map: dict[str, str],
        run_id: str,
    ) -> list[str]:
        """daily_pnl 날짜 목록 중 regime_map 미포함 날짜 반환.

        daily_pnl이 float list (override) 면 날짜를 추적할 수 없어 skip.
        """
        if not daily_pnl:
            return []
        if isinstance(daily_pnl[0], (int, float)):
            # float list override → 날짜 매핑 없음, 검증 skip
            return []
        missing: list[str] = []
        for entry in daily_pnl:
            d = entry.get("date", "")[:10]
            if d and d not in regime_map:
                missing.append(d)
        return missing

    def _compute_regime_breakdown(
        self,
        daily_pnl: list[dict[str, Any]] | list[float],
        regime_map: dict[str, str],
    ) -> list[dict[str, Any]]:
        """regime 별 sharpe, mdd, n_days 계산.

        4개 regime 모두 출력 (n_days=0 이면 sharpe=0, mdd=0).
        """
        min_pnl_std = float(self._cfg_eval.get("min_daily_pnl_std", 1e-8))
        ann_factor = int(self._cfg_eval.get("annualization_factor", 252))

        # regime 별 pnl 분류
        regime_pnl: dict[str, list[float]] = {r: [] for r in self._valid_regimes}

        if daily_pnl and isinstance(daily_pnl[0], dict):
            for entry in daily_pnl:
                d = entry.get("date", "")[:10]
                pnl = float(entry.get("pnl", 0.0))
                regime = regime_map.get(d)
                if regime and regime in regime_pnl:
                    regime_pnl[regime].append(pnl)
        # float list override: 날짜 미포함 → regime 배분 불가, 모두 첫 regime에 넣거나 skip
        # regime_map 기반으로 결정하므로 float list일 때는 모두 n_days=0 처리

        result = []
        for regime in self._valid_regimes:
            pnls = regime_pnl.get(regime, [])
            n_days = len(pnls)
            if n_days == 0:
                result.append({
                    "regime": regime,
                    "sharpe": 0.0,
                    "mdd": 0.0,
                    "n_days": 0,
                })
                continue

            mean_pnl = sum(pnls) / n_days
            std_pnl = BacktestEngine._std(pnls)
            sharpe = mean_pnl / max(std_pnl, min_pnl_std) * math.sqrt(ann_factor)
            mdd = BacktestEngine._max_drawdown(pnls)

            result.append({
                "regime": regime,
                "sharpe": sharpe,
                "mdd": mdd,
                "n_days": n_days,
            })

        return result

    # ────────────────────────────────────────────────────
    # ablation
    # ────────────────────────────────────────────────────

    def _mock_metrics_from_run_id(
        self,
        run_id: str,
        is_baseline: bool = False,
    ) -> dict[str, float]:
        """run_id hash seed 기반 결정론적 mock metrics 생성.

        PIT-Safety: baseline은 backtest보다 과거 → is_baseline=True 시 sr 약간 낮게.
        """
        import hashlib
        import random

        seed = int(hashlib.md5(run_id.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)

        base_sr = rng.uniform(0.5, 1.5)
        if is_baseline:
            base_sr *= 0.9  # baseline은 약간 낮게 (PIT-Safety 의미: 더 오래된 모델)

        return {
            "ic": rng.uniform(0.02, 0.08),
            "icir": rng.uniform(0.1, 0.5),
            "rank_ic": rng.uniform(0.02, 0.08),
            "arr": rng.uniform(0.05, 0.20),
            "ir": rng.uniform(0.3, 1.0),
            "mdd": -rng.uniform(0.05, 0.20),
            "sr": base_sr,
        }

    def _mock_ablation_metrics(
        self,
        component: str,
        full_run_id: str,
    ) -> dict[str, float]:
        """component 제거 후 mock 성과 (결정론적).

        seed = hash(full_run_id + component) → 재현 가능.
        """
        import hashlib
        import random

        seed_str = f"{full_run_id}:{component}"
        seed = int(hashlib.md5(seed_str.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)

        # ablation 시 약간 성과 하락 시뮬 (component 제거 = 성능 저하)
        sr_delta = rng.uniform(-0.3, 0.1)
        mdd_delta = rng.uniform(-0.05, 0.05)

        return {
            "sr": sr_delta,
            "mdd": mdd_delta,
        }

    def _compute_ablation(
        self,
        components: list[str],
        full_metrics: dict[str, float],
        full_run_id: str,
    ) -> list[dict[str, Any]]:
        """각 component 제거 시 delta_sharpe, delta_mdd, significance 계산.

        significance: bootstrap CI 대신 결정론적 mock p-value (seed 기반).
        delta_sharpe = ablation_sr - full_sr (음수 = component가 sharpe 향상에 기여).
        """
        import hashlib
        import random

        full_sr = full_metrics.get("sr", 0.0)
        full_mdd = full_metrics.get("mdd", 0.0)

        result = []
        for comp in components:
            ablation_delta = self._mock_ablation_metrics(comp, full_run_id)
            ablation_sr = full_sr + ablation_delta["sr"]
            ablation_mdd = full_mdd + ablation_delta["mdd"]

            delta_sharpe = ablation_sr - full_sr
            delta_mdd = ablation_mdd - full_mdd

            # significance: deterministic p-value (seed 기반 [0.01, 0.10])
            seed_str = f"sig:{full_run_id}:{comp}"
            seed = int(hashlib.md5(seed_str.encode()).hexdigest()[:8], 16)
            rng = random.Random(seed)
            significance = rng.uniform(0.01, 0.10)

            result.append({
                "component": comp,
                "delta_sharpe": delta_sharpe,
                "delta_mdd": delta_mdd,
                "significance": significance,
            })

        return result

    # ────────────────────────────────────────────────────
    # baseline_comparison
    # ────────────────────────────────────────────────────

    def _compute_baseline_comparison(
        self,
        bt_metrics: dict[str, float],
        baseline_metrics: dict[str, float],
    ) -> dict[str, Any]:
        """backtest vs baseline delta 계산 + verdict 판정.

        verdict 기준 (risk_config.yaml에서 로드):
          improved: delta_sharpe >= +threshold AND delta_mdd >= 0
          degraded: delta_sharpe <= -threshold OR delta_mdd <= -mdd_threshold
          neutral: 그 외
        """
        bt_sr = bt_metrics.get("sr", 0.0)
        bt_mdd = bt_metrics.get("mdd", 0.0)
        bt_arr = bt_metrics.get("arr", 0.0)

        base_sr = baseline_metrics.get("sr", 0.0)
        base_mdd = baseline_metrics.get("mdd", 0.0)
        base_arr = baseline_metrics.get("arr", 0.0)

        delta_sharpe = bt_sr - base_sr
        delta_mdd = bt_mdd - base_mdd
        delta_arr = bt_arr - base_arr

        # verdict 판정
        improve_thr = self._verdict_improve_sharpe
        degrade_thr = self._verdict_degrade_sharpe
        degrade_mdd_thr = self._verdict_degrade_mdd

        if delta_sharpe >= improve_thr and delta_mdd >= 0.0:
            verdict = "improved"
        elif delta_sharpe <= degrade_thr or delta_mdd <= degrade_mdd_thr:
            verdict = "degraded"
        else:
            verdict = "neutral"

        return {
            "delta_sharpe": delta_sharpe,
            "delta_mdd": delta_mdd,
            "delta_arr": delta_arr,
            "verdict": verdict,
        }

    # ────────────────────────────────────────────────────
    # regression_risk
    # ────────────────────────────────────────────────────

    def _compute_regression_risk(
        self,
        baseline_comparison: dict[str, Any],
        regime_breakdown: list[dict[str, Any]],
        ablation: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """regression_risk 판정.

        flagged 조건 (any):
          1. delta_sharpe < regression_sharpe_drop_threshold (-0.2)
          2. delta_mdd < regression_mdd_drop_threshold (-0.10)
          3. bear 또는 volatile regime에서 sharpe < bear_sharpe_floor (-1.0)
          4. dual_source ablation에서 delta_sharpe > +0.3 (dual_source 제거 오히려 좋음)

        severity: 1 trigger → low, 2 → medium, 3+ → high
        """
        evidence: list[str] = []

        delta_sharpe = baseline_comparison.get("delta_sharpe", 0.0)
        delta_mdd = baseline_comparison.get("delta_mdd", 0.0)

        # 조건 1: 큰 sharpe 하락
        if delta_sharpe < self._reg_sharpe_drop:
            evidence.append(
                f"delta_sharpe {delta_sharpe:.4f} < threshold {self._reg_sharpe_drop:.4f} "
                "(baseline 대비 sharpe 급락)"
            )

        # 조건 2: MDD 악화
        if delta_mdd < self._reg_mdd_drop:
            evidence.append(
                f"delta_mdd {delta_mdd:.4f} < threshold {self._reg_mdd_drop:.4f} "
                "(MDD 10% 이상 악화)"
            )

        # 조건 3: bear/volatile regime sharpe 급락
        for entry in regime_breakdown:
            if entry["regime"] in ("bear", "volatile"):
                if entry["n_days"] > 0 and entry["sharpe"] < self._reg_bear_sharpe_floor:
                    evidence.append(
                        f"{entry['regime']} regime sharpe {entry['sharpe']:.4f} "
                        f"< floor {self._reg_bear_sharpe_floor:.4f}"
                    )

        # 조건 4: dual_source 제거 시 오히려 sharpe 상승 (dual_source counterproductive)
        for abl in ablation:
            if abl["component"] == "dual_source":
                if abl["delta_sharpe"] > self._reg_dual_source_counterproductive:
                    evidence.append(
                        f"dual_source ablation delta_sharpe {abl['delta_sharpe']:.4f} "
                        f"> {self._reg_dual_source_counterproductive:.4f}: "
                        "dual_source counterproductive (제거 시 오히려 성과 향상)"
                    )

        flagged = len(evidence) > 0

        if not flagged:
            severity = "low"
        elif len(evidence) == 1:
            severity = "low"
        elif len(evidence) == 2:
            severity = "medium"
        else:
            severity = "high"

        return {
            "flagged": flagged,
            "evidence": evidence,
            "severity": severity,
        }
