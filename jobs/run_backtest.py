"""
Full Backtest Agent — Walk-Forward + Purge/Embargo

Walk-forward 방식으로 LightGBM 모델을 훈련/예측하고,
포트폴리오 시뮬레이션 후 성과 지표를 산출한다.

6개 베이스라인과 비교:
  KOSPI200  : 유니버스 전종목 EW buy-and-hold
  EW        : 월말 EW 리밸런싱
  pure-quant: quant_score만 사용 (news 제외)
  pure-news : news_signal만 사용 (quant 제외)
  momentum  : 단순 20일 모멘텀 상위 10종목
  no-UQ     : UQ 필터 없는 메인 전략 (Phase 2 비교용, Phase 1에서는 main과 동일)

산출물:
  reports/backtest/BacktestReport-{start}-{end}.json
  reports/backtest/FailureCaseCard-{start}-{end}.json

Usage:
    python jobs/run_backtest.py --start 20250801 --end 20260325
    python jobs/run_backtest.py --start 20250801 --end 20260325 --baselines
"""

import sys
import json
import argparse
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE_DIR))

import numpy as np
import pandas as pd
import yaml

from connectors import make_snapshot_dt, now_kst_iso

# ── 경로 설정 ──────────────────────────────────────────────────
UNIVERSE_PATH    = _BASE_DIR / "config" / "universe_v1.csv"
RISK_POLICY_PATH = _BASE_DIR / "config" / "risk_policy_v0.yaml"
MODEL_CONFIG_PATH = _BASE_DIR / "models" / "strategy_model" / "config.yaml"
DMP_DIR          = _BASE_DIR / "artifacts" / "daily_market_packet"
TTP_DIR          = _BASE_DIR / "artifacts" / "ticker_text_pack"
REPORT_DIR       = _BASE_DIR / "reports" / "backtest"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

FCC_SCHEMA_PATH  = _BASE_DIR / "schemas" / "failure_case_card.json"

KST = timezone(timedelta(hours=9))

# 거래 비용 (편도) — Gemini Pro 피드백 반영
# 매수: 브로커리지 0.015% = 0.00015
# 매도: 브로커리지 0.015% + 증권거래세 0.18% + 농어촌특별세 0.15% = 0.00345
# 편도 평균: (0.00015 + 0.00345) / 2 ≈ 0.0018
COST_BUY_SIDE = 0.00015    # 매수 편도 (브로커리지만)
COST_SELL_SIDE = 0.00345   # 매도 편도 (브로커리지 + 거래세 + 농특세)
COST_PER_SIDE = 0.0018     # 레거시 호환용 평균값

# 슬리피지 모델: 시가 체결 시 ±슬리피지 반영
# 매수: open * (1 + SLIPPAGE_BPS), 매도: open * (1 - SLIPPAGE_BPS)
SLIPPAGE_BPS = 0.001       # 0.10% (10bps) — KOSPI 대형주 평균 추정


# ── 설정 로드 ─────────────────────────────────────────────────

def load_risk_policy() -> dict:
    with open(RISK_POLICY_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_model_config() -> dict:
    with open(MODEL_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_universe() -> pd.DataFrame:
    uni = pd.read_csv(UNIVERSE_PATH, dtype={"ticker": str})
    uni["ticker"] = uni["ticker"].apply(lambda x: str(x).zfill(6))
    return uni


# ── DMP 파싱 ─────────────────────────────────────────────────

def get_available_dates(start: str = None, end: str = None) -> list:
    """DMP 파일에서 거래일 목록 추출. PIT-safe 필터 적용."""
    dmp_files = sorted(DMP_DIR.glob("DMP-*.json"))
    dates = [f.stem.replace("DMP-", "") for f in dmp_files]
    if start:
        dates = [d for d in dates if d >= start]
    if end:
        dates = [d for d in dates if d <= end]
    return dates


def load_dmp_data(date_str: str) -> dict:
    """DMP에서 종목별 가격/피처 추출."""
    path = DMP_DIR / f"DMP-{date_str}.json"
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            dmp = json.load(f)
    except Exception as e:
        print(f"[Backtest] DMP 로드 실패 ({date_str}): {e}")
        return {}

    # PIT-Safety: snapshot_dt 확인
    snap = dmp.get("snapshot_dt", "")
    cutoff = make_snapshot_dt(date_str)
    if snap and snap > cutoff:
        print(f"[Backtest] PIT 경고: {date_str} snapshot_dt={snap} > {cutoff}")
        return {}

    result = {}
    for ticker, td in dmp.get("market_data", {}).items():
        ticker = str(ticker).zfill(6)
        ohlcv = td.get("ohlcv", {})
        tech  = td.get("tech_features", {})
        close = float(ohlcv.get("close") or 0)
        sma_5  = float(tech.get("sma_5",  close) or close or 1)
        sma_20 = float(tech.get("sma_20", close) or close or 1)
        sma_60 = float(tech.get("sma_60", close) or close or 1)

        result[ticker] = {
            "open":                 float(ohlcv.get("open") or 0),
            "close":                close,
            "volume":               float(td.get("volume") or 0),
            "return_5d":            float(tech.get("return_5d")  or (close / sma_5  - 1 if sma_5  > 0 else 0)),
            "return_20d":           float(tech.get("return_20d") or (close / sma_20 - 1 if sma_20 > 0 else 0)),
            "return_60d":           float(tech.get("return_60d") or (close / sma_60 - 1 if sma_60 > 0 else 0)),
            "rsi_14":               float(tech.get("rsi_14") or 50.0),
            "volume_ratio_20":      float(tech.get("volume_ratio_20") or 1.0),
            "macd":                 float(tech.get("macd") or 0.0),
            "macd_signal":          float(tech.get("macd_signal") or 0.0),
            "atr_14":               float(tech.get("atr_14") or 0.0),
            "sma5_ratio":           (close / sma_5  - 1.0) if sma_5  > 0 else 0.0,
            "sma20_ratio":          (close / sma_20 - 1.0) if sma_20 > 0 else 0.0,
            "sma60_ratio":          (close / sma_60 - 1.0) if sma_60 > 0 else 0.0,
            "period_agreement_score": float(tech.get("period_agreement_score") or 0.0),
            "stock_sync_score":     float(tech.get("stock_sync_score") or 0.0),
            "market_synchronism":   float(tech.get("market_synchronism") or 0.0),
            "rational_price_gap":   float(tech.get("rational_price_gap") or 0.0),
        }
    return result


def load_news_signals(date_str: str, tickers: list) -> dict:
    """TTP에서 종목별 news_signal 수집."""
    signals = {}
    for ticker in tickers:
        ticker = str(ticker).zfill(6)
        ttp_path = TTP_DIR / f"TTP-{date_str}-{ticker}.json"
        if not ttp_path.exists():
            signals[ticker] = 0.0
            continue
        try:
            with open(ttp_path, encoding="utf-8") as f:
                ttp = json.load(f)
            if "news_signal" in ttp:
                signals[ticker] = float(ttp["news_signal"])
                continue
            scores = [
                float(doc["sentiment_score"])
                for doc in ttp.get("ticker_docs", [])
                if doc.get("sentiment_score") is not None
            ]
            signals[ticker] = round(float(np.mean(scores)), 4) if scores else 0.0
        except Exception as e:
            print(f"[Backtest] TTP 로드 실패 ({ticker}/{date_str}): {e}")
            signals[ticker] = 0.0
    return signals


# ── 라벨 생성 ─────────────────────────────────────────────────

def compute_labels(
    date_idx: int,
    dates: list,
    price_panel: dict,
    tickers: list,
    forward_horizon: int = 5,
    top_quantile: float = 0.25,
) -> dict:
    """
    t+forward_horizon일 후 수익률 기준 상위 top_quantile → 1, 나머지 → 0.
    PIT-Safety: label 계산 시 t+horizon 이후 데이터만 참조.
    """
    t_close_idx = date_idx
    t_horizon_idx = date_idx + forward_horizon
    if t_horizon_idx >= len(dates):
        return {}

    t_date    = dates[t_close_idx]
    th_date   = dates[t_horizon_idx]

    fwd_rets = {}
    for ticker in tickers:
        ticker = str(ticker).zfill(6)
        t_data  = price_panel.get(t_date,  {}).get(ticker)
        th_data = price_panel.get(th_date, {}).get(ticker)
        if not t_data or not th_data:
            continue
        p0 = t_data["close"]
        p1 = th_data["close"]
        if p0 > 0:
            fwd_rets[ticker] = (p1 - p0) / p0

    if not fwd_rets:
        return {}

    threshold = np.quantile(list(fwd_rets.values()), 1.0 - top_quantile)
    return {t: int(r >= threshold) for t, r in fwd_rets.items()}


# ── LightGBM 훈련/예측 ────────────────────────────────────────

FEATURE_COLS = [
    "return_5d", "return_20d", "rsi_14", "volume_ratio_20",
    "macd", "macd_signal", "atr_14", "sma5_ratio", "sma20_ratio",
    "sma60_ratio", "return_60d", "period_agreement_score",
    "stock_sync_score", "market_synchronism", "rational_price_gap",
]


def build_dataset_for_dates(
    dates: list,
    price_panel: dict,
    tickers: list,
    forward_horizon: int = 5,
    top_quantile: float = 0.25,
) -> tuple:
    """날짜 목록에서 (X, y) 반환."""
    rows_X, rows_y = [], []
    for i, date in enumerate(dates):
        day_data = price_panel.get(date, {})
        labels = compute_labels(i, dates, price_panel, tickers,
                                forward_horizon, top_quantile)
        for ticker in tickers:
            ticker = str(ticker).zfill(6)
            td = day_data.get(ticker)
            if not td or ticker not in labels:
                continue
            row = {col: td.get(col, 0.0) for col in FEATURE_COLS}
            rows_X.append(row)
            rows_y.append(labels[ticker])

    if not rows_X:
        return pd.DataFrame(columns=FEATURE_COLS), pd.Series(dtype=int)

    X = pd.DataFrame(rows_X, columns=FEATURE_COLS).fillna(0.0)
    y = pd.Series(rows_y, dtype=int)
    return X, y


def train_lgbm(X: pd.DataFrame, y: pd.Series, cfg: dict):
    """LightGBM 모델 훈련."""
    try:
        import lightgbm as lgb
    except ImportError:
        print("[Backtest] lightgbm 미설치 — 더미 모델 사용")
        return None

    lgbm_cfg = cfg.get("lgbm", {})
    model = lgb.LGBMClassifier(
        objective=lgbm_cfg.get("objective", "binary"),
        metric=lgbm_cfg.get("metric", "auc"),
        num_leaves=lgbm_cfg.get("num_leaves", 31),
        learning_rate=lgbm_cfg.get("learning_rate", 0.05),
        n_estimators=lgbm_cfg.get("n_estimators", 200),
        min_child_samples=lgbm_cfg.get("min_child_samples", 10),
        subsample=lgbm_cfg.get("subsample", 0.8),
        colsample_bytree=lgbm_cfg.get("colsample_bytree", 0.8),
        reg_alpha=lgbm_cfg.get("reg_alpha", 0.1),
        reg_lambda=lgbm_cfg.get("reg_lambda", 0.1),
        verbose=-1,
    )
    if len(y) == 0 or y.nunique() < 2:
        return None
    try:
        model.fit(X, y)
    except Exception as e:
        print(f"[Backtest] LightGBM 훈련 실패: {e}")
        return None
    return model


def predict_proba_scores(model, X: pd.DataFrame) -> np.ndarray:
    """LightGBM 예측 → [-1, 1] 점수. LGBMRanker/Classifier 양쪽 호환."""
    if model is None or X.empty:
        return np.zeros(len(X))
    try:
        # dict wrapper 해제
        estimator = model["model"] if isinstance(model, dict) else model
        feature_cols = model.get("feature_cols") if isinstance(model, dict) else None
        col_means = model.get("col_means", {}) if isinstance(model, dict) else {}
        if feature_cols is not None:
            for c in feature_cols:
                if c not in X.columns:
                    X[c] = col_means.get(c, 0.0)
            X = X[feature_cols]

        if hasattr(estimator, "predict_proba"):
            proba = estimator.predict_proba(X)
            scores = proba[:, 1] if proba.ndim == 2 else proba.ravel()
            return np.clip(scores * 2 - 1, -1.0, 1.0)
        else:
            # LGBMRanker: cross-sectional rank normalize
            raw = estimator.predict(X)
            mn, mx = raw.min(), raw.max()
            rng = mx - mn if mx > mn else 1.0
            return np.clip((raw - mn) / rng * 2 - 1, -1.0, 1.0)
    except Exception as e:
        print(f"[Backtest] 예측 실패: {e}")
        return np.zeros(len(X))


# ── 포트폴리오 시뮬레이터 ─────────────────────────────────────

class PortfolioSimulator:
    """
    단순 포트폴리오 시뮬레이터.
    - 상위 K 종목 균등 배분 (K = max_position_count)
    - t+1 시가 체결, t+1 종가 MTM
    - 거래비용, 손절, 회전율 제한 적용
    """

    def __init__(self, policy: dict, initial_cash: float = 1_000_000_000.0):
        pos_c   = policy["position_constraints"]
        core    = policy["core_rules"]
        self.max_k        = int(pos_c["max_position_count"])
        self.stop_loss    = float(core["stop_loss"]["threshold"]) / 100.0   # -0.05
        self.turnover_cap = float(core["turnover_cap"]["daily_max"]) / 100.0  # 0.30
        self.min_cash_r   = float(core["min_cash_ratio"]["ratio"]) / 100.0   # 0.10
        self.initial_cash = initial_cash
        self.reset()

    def reset(self):
        self.cash = self.initial_cash
        self.positions: dict = {}  # {ticker: {shares, entry_price}}
        self.nav_history: list = []
        self.trade_history: list = []

    def _portfolio_value(self, prices: dict) -> float:
        val = self.cash
        for tk, pos in self.positions.items():
            p = prices.get(tk, {}).get("close", 0)
            val += pos["shares"] * p
        return val if val > 0 else self.cash

    def step(
        self,
        t_date: str,
        t1_date: str,
        selected_tickers: list,
        t1_prices: dict,
    ) -> dict:
        """
        하루 시뮬레이션 스텝.
        selected_tickers: 매수 대상 상위 K 종목 (quant_score 내림차순 정렬 후 전달).
        """
        # t+1 시가 체결 기준
        open_prices  = {tk: t1_prices.get(tk, {}).get("open",  0) for tk in t1_prices}
        close_prices = {tk: t1_prices.get(tk, {}).get("close", 0) for tk in t1_prices}

        pv_before = self._portfolio_value({tk: {"close": open_prices.get(tk, 0)} for tk in t1_prices})

        # ── 손절 처리 ─────────────────────────────────────────
        to_stop = []
        for tk, pos in list(self.positions.items()):
            sell_p = open_prices.get(tk, 0)
            if sell_p > 0 and pos["entry_price"] > 0:
                ret = sell_p / pos["entry_price"] - 1.0
                if ret <= self.stop_loss:
                    to_stop.append(tk)

        stop_turnover = 0.0
        for tk in to_stop:
            pos = self.positions.pop(tk)
            raw_p = open_prices.get(tk, 0)
            sell_p = raw_p * (1 - SLIPPAGE_BPS)  # 매도 슬리피지 반영
            if sell_p > 0:
                proceeds = pos["shares"] * sell_p
                cost = proceeds * COST_SELL_SIDE  # 매도 비용 (거래세 포함)
                self.cash += proceeds - cost
                stop_turnover += proceeds / max(pv_before, 1)
                self.trade_history.append({
                    "date": t1_date, "ticker": tk, "action": "stop_loss",
                    "price": sell_p, "shares": pos["shares"],
                    "cost": round(cost, 2), "ret": round(sell_p / pos["entry_price"] - 1, 4),
                })

        # 손절 자체가 turnover_cap을 초과한 경우 경고 (손절은 리스크 관리상 강제 실행)
        if stop_turnover > self.turnover_cap:
            print(
                f"[Backtest] 경고: {t1_date} 손절 turnover {stop_turnover:.2%} > cap {self.turnover_cap:.2%}. "
                "일반 매도/매수는 전량 차단."
            )

        # ── 매도 (보유 중이나 대상 아닌 종목) ──────────────────
        sell_candidates = [tk for tk in list(self.positions.keys()) if tk not in selected_tickers]
        sell_turnover = 0.0
        for tk in sell_candidates:
            remaining_turn = self.turnover_cap - stop_turnover - sell_turnover
            if remaining_turn <= 0:
                break
            pos = self.positions.pop(tk)
            raw_p = open_prices.get(tk, 0)
            sell_p = raw_p * (1 - SLIPPAGE_BPS)  # 매도 슬리피지
            if sell_p > 0:
                proceeds = pos["shares"] * sell_p
                cost = proceeds * COST_SELL_SIDE  # 매도 비용 (거래세 포함)
                self.cash += proceeds - cost
                sell_turnover += proceeds / max(pv_before, 1)
                self.trade_history.append({
                    "date": t1_date, "ticker": tk, "action": "sell",
                    "price": sell_p, "shares": pos["shares"],
                    "cost": round(cost, 2),
                    "ret": round(sell_p / pos.get("entry_price", sell_p) - 1, 4),
                })

        # ── 매수 (신규 진입만, 이미 보유한 종목 건너뜀) ────────
        new_buys = [tk for tk in selected_tickers if tk not in self.positions][:self.max_k]
        buy_turnover = 0.0
        if new_buys:
            max_invest = self.cash * (1.0 - self.min_cash_r)
            per_stock  = max_invest / len(new_buys) if new_buys else 0.0
            for tk in new_buys:
                remaining_turn = self.turnover_cap - stop_turnover - sell_turnover - buy_turnover
                if remaining_turn <= 0:
                    break
                raw_p = open_prices.get(tk, 0)
                if raw_p <= 0:
                    continue
                buy_p = raw_p * (1 + SLIPPAGE_BPS)  # 매수 슬리피지
                shares = int(per_stock / buy_p)
                if shares <= 0:
                    continue
                cost_amt   = shares * buy_p
                trade_cost = cost_amt * COST_BUY_SIDE  # 매수 비용 (브로커리지만)
                if cost_amt + trade_cost > self.cash:
                    shares = int(self.cash * 0.95 / buy_p / (1 + COST_BUY_SIDE))
                    if shares <= 0:
                        continue
                    cost_amt   = shares * buy_p
                    trade_cost = cost_amt * COST_BUY_SIDE
                self.cash -= cost_amt + trade_cost
                self.positions[tk] = {"shares": shares, "entry_price": buy_p}
                buy_turnover += cost_amt / max(pv_before, 1)
                self.trade_history.append({
                    "date": t1_date, "ticker": tk, "action": "buy",
                    "price": buy_p, "shares": shares, "cost": round(trade_cost, 2),
                })

        # ── NAV 계산 (t+1 종가 MTM) ───────────────────────────
        pv = self.cash
        for tk, pos in self.positions.items():
            close_p = close_prices.get(tk, 0)
            pv += pos["shares"] * close_p

        nav = pv / self.initial_cash
        exposure = 1.0 - (self.cash / pv) if pv > 0 else 0.0
        total_turn = stop_turnover + sell_turnover + buy_turnover

        day_record = {
            "date":       t1_date,
            "nav":        round(nav, 6),
            "cash":       round(self.cash, 2),
            "n_positions": len(self.positions),
            "exposure":   round(exposure, 4),
            "turnover":   round(total_turn, 4),
        }
        self.nav_history.append(day_record)
        return day_record


# ── 성과 계산 ─────────────────────────────────────────────────

def compute_metrics(nav_arr: np.ndarray) -> dict:
    """NAV 시계열에서 성과 지표 계산."""
    if len(nav_arr) < 2:
        return {
            "total_return": 0.0, "sharpe_ratio": 0.0,
            "max_drawdown": 0.0, "win_rate": 0.0, "turnover_avg": 0.0,
        }

    daily_rets = np.diff(nav_arr) / (nav_arr[:-1] + 1e-10)
    total_return  = float(nav_arr[-1] / nav_arr[0] - 1)
    sharpe        = float(daily_rets.mean() / (daily_rets.std() + 1e-10) * np.sqrt(252))
    running_max   = np.maximum.accumulate(nav_arr)
    drawdowns     = (nav_arr - running_max) / (running_max + 1e-10)
    max_drawdown  = float(drawdowns.min())
    win_rate      = float((daily_rets > 0).mean())

    return {
        "total_return": round(total_return, 6),
        "sharpe_ratio": round(sharpe, 4),
        "max_drawdown": round(max_drawdown, 6),
        "win_rate":     round(win_rate, 4),
    }


# ── 신호 생성기 (베이스라인별) ────────────────────────────────

def signals_main(
    date_str: str,
    tickers: list,
    day_data: dict,
    news_signals: dict,
    model,
    quant_weight: float = 0.6,
    top_k: int = 10,
) -> list:
    """메인 전략: LightGBM quant + news 결합 후 상위 K."""
    rows = [{col: day_data.get(tk, {}).get(col, 0.0) for col in FEATURE_COLS} for tk in tickers]
    X = pd.DataFrame(rows, columns=FEATURE_COLS).fillna(0.0)
    q_scores = predict_proba_scores(model, X)
    scored = []
    for i, ticker in enumerate(tickers):
        ticker = str(ticker).zfill(6)
        qs = float(q_scores[i])
        ns = float(news_signals.get(ticker, 0.0))
        combined = qs * quant_weight + ns * (1 - quant_weight)
        scored.append((ticker, combined))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [tk for tk, _ in scored[:top_k]]  # 상위 K 무조건 선택 (cross-sectional rank)


def signals_quant_only(
    tickers: list, day_data: dict, model, top_k: int = 10
) -> list:
    rows = [{col: day_data.get(tk, {}).get(col, 0.0) for col in FEATURE_COLS} for tk in tickers]
    X = pd.DataFrame(rows, columns=FEATURE_COLS).fillna(0.0)
    q_scores = predict_proba_scores(model, X)
    scored = [(str(t).zfill(6), float(s)) for t, s in zip(tickers, q_scores)]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [tk for tk, s in scored[:top_k]]  # 상위 K 무조건 선택


def signals_news_only(
    tickers: list, news_signals: dict, top_k: int = 10
) -> list:
    scored = [(str(t).zfill(6), float(news_signals.get(str(t).zfill(6), 0.0))) for t in tickers]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [tk for tk, s in scored[:top_k]]  # 상위 K 무조건 선택


def signals_momentum_20d(
    tickers: list, day_data: dict, top_k: int = 10
) -> list:
    """단순 20일 모멘텀 상위 K."""
    scored = [
        (str(t).zfill(6), float(day_data.get(str(t).zfill(6), {}).get("return_20d", 0.0)))
        for t in tickers
    ]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [tk for tk, s in scored[:top_k]]  # 상위 K 무조건 선택


def signals_ew_rebalance(tickers: list, date_str: str, top_k: int = 10) -> list:
    """
    월말 리밸런싱 EW: 매월 말일 전종목 균등 배분, 나머지 날은 현재 보유 유지.
    날짜 문자열로만 판단 (월말 = 해당 월 마지막 DMP 기준이 아닌 날짜 기반 근사).
    """
    y, m, d = int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8])
    # 간단하게 매월 15일 이후 첫 리밸런싱으로 근사
    if d >= 25:
        return [str(t).zfill(6) for t in tickers[:top_k]]
    return []  # 리밸런싱 없는 날 — 빈 리스트 반환 (기존 포지션 유지)


# ── Walk-Forward 엔진 ─────────────────────────────────────────

def run_walk_forward(
    dates: list,
    price_panel: dict,
    tickers: list,
    policy: dict,
    cfg: dict,
) -> tuple:
    """
    Walk-forward 훈련/예측 루프.
    반환: (fold_results, all_trades)
      fold_results: per-fold metrics list
      all_trades  : (date, ticker, signal, quant_score, actual_return) 전체 기록
    """
    wf_cfg = cfg.get("walk_forward", {})
    train_window  = int(wf_cfg.get("train_window", 200))
    val_window    = int(wf_cfg.get("val_window", 20))
    step_size     = int(wf_cfg.get("step_size", 20))
    purge_days    = int(wf_cfg.get("purge_days", 5))
    embargo_days  = int(wf_cfg.get("embargo_days", 5))
    forward_h     = int(cfg.get("label", {}).get("forward_horizon", 5))
    top_q         = float(cfg.get("label", {}).get("top_quantile", 0.25))
    top_k         = int(policy["position_constraints"]["max_position_count"])

    fold_results = []
    all_preds    = []  # (date, ticker, quant_score, actual_return_5d)

    simulator = PortfolioSimulator(policy)
    simulator.reset()

    n = len(dates)
    cursor = train_window

    while cursor + val_window <= n:
        train_end   = cursor
        val_start   = cursor + purge_days + embargo_days
        val_end     = min(cursor + val_window, n - forward_h)

        if val_start >= val_end:
            cursor += step_size
            continue

        train_dates = dates[max(0, train_end - train_window): train_end]
        val_dates   = dates[val_start: val_end]

        if not train_dates or not val_dates:
            cursor += step_size
            continue

        # 훈련
        X_train, y_train = build_dataset_for_dates(
            train_dates, price_panel, tickers, forward_h, top_q
        )
        model = train_lgbm(X_train, y_train, cfg)
        fold_label = f"{train_dates[0]}~{train_dates[-1]} | val {val_dates[0]}~{val_dates[-1]}"
        print(f"[Backtest] 폴드 훈련: {fold_label} (train={len(X_train)}, model={'OK' if model else 'FALLBACK'})")

        # Validation 시뮬레이션
        fold_navs = []
        for i, t_date in enumerate(val_dates[:-1]):
            t1_date = val_dates[i + 1]
            day_data   = price_panel.get(t_date, {})
            t1_data    = price_panel.get(t1_date, {})
            if not day_data or not t1_data:
                continue

            ns = load_news_signals(t_date, tickers)
            selected = signals_main(t_date, tickers, day_data, ns, model, top_k=top_k)
            t1_prices = {tk: {"open": d["open"], "close": d["close"]} for tk, d in t1_data.items()}
            rec = simulator.step(t_date, t1_date, selected, t1_prices)
            fold_navs.append(rec["nav"])

            # 예측 기록
            for tk in selected:
                tk6 = str(tk).zfill(6)
                t1_d = t1_data.get(tk6, {})
                t_d  = day_data.get(tk6, {})
                if t_d.get("close", 0) > 0 and t1_d.get("close", 0) > 0:
                    actual_ret = t1_d["close"] / t_d["close"] - 1
                    all_preds.append({
                        "date": t1_date, "ticker": tk6,
                        "signal": "buy", "quant_score": 1.0,
                        "actual_return": round(actual_ret, 6),
                    })

        fold_metrics = compute_metrics(np.array(fold_navs))
        fold_metrics["fold"] = fold_label
        fold_metrics["n_days"] = len(fold_navs)
        fold_results.append(fold_metrics)

        cursor += step_size

    return fold_results, all_preds, simulator


# ── 베이스라인 시뮬레이션 ─────────────────────────────────────

def run_baseline_simulation(
    name: str,
    dates: list,
    price_panel: dict,
    tickers: list,
    policy: dict,
    cfg: dict,
    model=None,
) -> dict:
    """단일 베이스라인 시뮬레이션. NAV 시계열 + 성과 지표 반환."""
    sim = PortfolioSimulator(policy)
    top_k = int(policy["position_constraints"]["max_position_count"])
    nav_arr = []

    for i in range(len(dates) - 1):
        t_date  = dates[i]
        t1_date = dates[i + 1]
        day_data = price_panel.get(t_date, {})
        t1_data  = price_panel.get(t1_date, {})
        if not day_data or not t1_data:
            continue

        t1_prices = {tk: {"open": d["open"], "close": d["close"]} for tk, d in t1_data.items()}

        if name == "KOSPI200":
            # 전종목 EW buy-and-hold (매일 리밸런싱 없이 첫날만 매수)
            selected = [str(t).zfill(6) for t in tickers] if i == 0 else []

        elif name == "EW":
            selected = signals_ew_rebalance(tickers, t_date, top_k=len(tickers))
            if not selected:
                selected = []

        elif name == "pure-quant":
            selected = signals_quant_only(tickers, day_data, model, top_k=top_k)

        elif name == "pure-news":
            ns = load_news_signals(t_date, tickers)
            selected = signals_news_only(tickers, ns, top_k=top_k)

        elif name == "momentum":
            selected = signals_momentum_20d(tickers, day_data, top_k=top_k)

        elif name == "no-UQ":
            # Phase 1: UQ 비활성화 상태이므로 main과 동일
            ns = load_news_signals(t_date, tickers)
            selected = signals_main(t_date, tickers, day_data, ns, model, top_k=top_k)

        else:
            selected = []

        rec = sim.step(t_date, t1_date, selected, t1_prices)
        nav_arr.append(rec["nav"])

    metrics = compute_metrics(np.array(nav_arr))
    metrics["name"] = name
    metrics["n_days"] = len(nav_arr)
    return metrics


# ── FailureCaseCard 생성 ──────────────────────────────────────

def build_failure_case_card(
    start: str,
    end: str,
    all_preds: list,
    strategy: str = "momentum",
) -> dict:
    """
    백테스트 예측 기록에서 실패 케이스를 추출하여 FailureCaseCard를 생성한다.

    실패 기준:
      - false_positive: buy 신호인데 actual_return < -0.02
      - false_negative: 미선택인데 상위 성과 (백테스트 후 분석 시 확장 예정)
    """
    failure_cases = []
    failure_type_counts: dict = defaultdict(int)

    for pred in all_preds:
        actual_ret = pred.get("actual_return", 0.0)
        signal     = pred.get("signal", "hold")
        ticker     = str(pred.get("ticker", "")).zfill(6)
        date       = pred.get("date", "")

        if signal in ("buy", "strong_buy") and actual_ret < -0.02:
            ftype = "false_positive"
            if actual_ret < -0.05:
                ftype = "momentum_reversal"
            failure_cases.append({
                "date":          date,
                "ticker":        ticker,
                "signal":        signal,
                "confidence":    round(abs(pred.get("quant_score", 0.5)), 3),
                "actual_return": round(actual_ret, 6),
                "failure_type":  ftype,
                "description":   (
                    f"{ticker} {date}: {signal} 신호 발행 후 "
                    f"실제 수익률 {actual_ret:.2%} ({ftype})"
                ),
            })
            failure_type_counts[ftype] += 1

    total_preds    = len(all_preds)
    total_failures = len(failure_cases)
    failure_rate   = round(total_failures / max(total_preds, 1), 4)

    top_failure_types = sorted(
        failure_type_counts, key=lambda x: failure_type_counts[x], reverse=True
    )[:3]

    # 실패 태그 추출
    failure_tags = list(set(fc["failure_type"] for fc in failure_cases))

    return {
        "card_id":          f"FCC-{end}",
        "generated_at":     now_kst_iso(),
        "artifact_version": "v1.0",
        "strategy":         strategy,
        "period":           {"start": start, "end": end},
        "failure_cases":    failure_cases,
        "summary": {
            "total_failures":    total_failures,
            "failure_rate":      failure_rate,
            "top_failure_types": top_failure_types,
            "failure_tags":      failure_tags,
        },
    }


# ── 메인 ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Full Backtest Agent — Walk-Forward + Purge/Embargo"
    )
    parser.add_argument("--start", required=True, help="백테스트 시작일 YYYYMMDD")
    parser.add_argument("--end",   required=True, help="백테스트 종료일 YYYYMMDD")
    parser.add_argument(
        "--baselines", action="store_true",
        help="6개 베이스라인 병렬 시뮬레이션 포함"
    )
    args = parser.parse_args()

    start_date = args.start
    end_date   = args.end

    # 날짜 형식 검증
    for d in (start_date, end_date):
        try:
            datetime.strptime(d, "%Y%m%d")
        except ValueError:
            print(f"[Backtest] 날짜 형식 오류: {d} (YYYYMMDD 필요)")
            sys.exit(1)

    # 설정 로드
    policy = load_risk_policy()
    cfg    = load_model_config()
    uni    = load_universe()
    tickers = uni["ticker"].tolist()

    print(f"[Backtest] 기간: {start_date} ~ {end_date}")
    print(f"[Backtest] 유니버스: {len(tickers)}종목")

    # 가용 거래일
    dates = get_available_dates(start_date, end_date)
    if len(dates) < 10:
        print(f"[Backtest] 가용 거래일 부족 ({len(dates)}일). 최소 10일 필요.")
        sys.exit(1)
    print(f"[Backtest] 가용 거래일: {len(dates)}일 ({dates[0]} ~ {dates[-1]})")

    # 가격 패널 로드
    print(f"[Backtest] DMP 패널 로드 중...")
    price_panel: dict = {}
    for d in dates:
        price_panel[d] = load_dmp_data(d)
    loaded = sum(1 for v in price_panel.values() if v)
    print(f"[Backtest] 가격 패널 로드 완료: {loaded}/{len(dates)}일")

    # ── Walk-Forward 실행 ──────────────────────────────────────
    print(f"\n[Backtest] Walk-Forward 시작...")
    fold_results, all_preds, simulator = run_walk_forward(
        dates, price_panel, tickers, policy, cfg
    )

    # 전체 집계 성과
    main_navs = np.array([r["nav"] for r in simulator.nav_history])
    agg_metrics = compute_metrics(main_navs)
    total_trades = len(simulator.trade_history)
    avg_turnover = round(
        float(np.mean([r.get("turnover", 0) for r in simulator.nav_history])), 4
    ) if simulator.nav_history else 0.0

    print(f"\n[Backtest] Walk-Forward 완료:")
    print(f"  누적수익: {agg_metrics['total_return']:.2%}")
    print(f"  Sharpe:   {agg_metrics['sharpe_ratio']:.2f}")
    print(f"  MDD:      {agg_metrics['max_drawdown']:.2%}")
    print(f"  승률:     {agg_metrics['win_rate']:.2%}")
    print(f"  총 거래:  {total_trades}건")
    print(f"  평균회전율: {avg_turnover:.2%}")

    # ── 베이스라인 시뮬레이션 ─────────────────────────────────
    baseline_results = {}
    if args.baselines:
        print(f"\n[Backtest] 베이스라인 시뮬레이션 시작...")
        # 베이스라인용 간단 모델 훈련 (전체 기간 단일 훈련)
        wf_cfg = cfg.get("walk_forward", {})
        train_window = int(wf_cfg.get("train_window", 200))
        train_dates  = dates[:train_window]
        X_bl, y_bl   = build_dataset_for_dates(
            train_dates, price_panel, tickers,
            int(cfg.get("label", {}).get("forward_horizon", 5)),
            float(cfg.get("label", {}).get("top_quantile", 0.25)),
        )
        bl_model = train_lgbm(X_bl, y_bl, cfg)

        for bl_name in ["KOSPI200", "EW", "pure-quant", "pure-news", "momentum", "no-UQ"]:
            print(f"[Backtest] 베이스라인 [{bl_name}] 실행 중...")
            try:
                bl_metrics = run_baseline_simulation(
                    bl_name, dates, price_panel, tickers, policy, cfg, bl_model
                )
                baseline_results[bl_name] = bl_metrics
                print(
                    f"[Backtest] [{bl_name}] 완료: "
                    f"return={bl_metrics['total_return']:.2%}, "
                    f"sharpe={bl_metrics['sharpe_ratio']:.2f}"
                )
            except Exception as e:
                print(f"[Backtest] 베이스라인 [{bl_name}] 실패: {e}")
                baseline_results[bl_name] = {"error": str(e)}

    # ── BacktestReport 생성 ────────────────────────────────────
    report = {
        "report_id":        f"BTR-{start_date}-{end_date}",
        "generated_at":     now_kst_iso(),
        "artifact_version": "v1.0",
        "strategy":         "momentum",
        "period":           {"start": start_date, "end": end_date},
        "n_trading_days":   len(dates),
        "walk_forward_config": {
            "mode":          cfg.get("walk_forward", {}).get("mode", "expanding"),
            "train_window":  cfg.get("walk_forward", {}).get("train_window", 200),
            "val_window":    cfg.get("walk_forward", {}).get("val_window", 20),
            "step_size":     cfg.get("walk_forward", {}).get("step_size", 20),
            "purge_days":    cfg.get("walk_forward", {}).get("purge_days", 5),
            "embargo_days":  cfg.get("walk_forward", {}).get("embargo_days", 5),
        },
        "portfolio_config": {
            "max_position_count": policy["position_constraints"]["max_position_count"],
            "stop_loss_pct":      policy["core_rules"]["stop_loss"]["threshold"],
            "turnover_cap_pct":   policy["core_rules"]["turnover_cap"]["daily_max"],
            "min_cash_ratio_pct": policy["core_rules"]["min_cash_ratio"]["ratio"],
            "cost_buy_side":      COST_BUY_SIDE,
            "cost_sell_side":     COST_SELL_SIDE,
            "slippage_bps":       SLIPPAGE_BPS,
            "cost_per_side_legacy": COST_PER_SIDE,
        },
        "aggregate_metrics": {
            **agg_metrics,
            "total_trades":  total_trades,
            "avg_turnover":  avg_turnover,
            "n_folds":       len(fold_results),
        },
        "fold_results":    fold_results,
        "baselines":       baseline_results,
        "nav_history":     simulator.nav_history,
        "trade_history":   simulator.trade_history,
    }

    report_path = REPORT_DIR / f"BacktestReport-{start_date}-{end_date}.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n[Backtest] 리포트 저장: {report_path}")

    # ── FailureCaseCard 생성 ───────────────────────────────────
    fcc = build_failure_case_card(start_date, end_date, all_preds)
    fcc_path = REPORT_DIR / f"FailureCaseCard-{start_date}-{end_date}.json"
    with open(fcc_path, "w", encoding="utf-8") as f:
        json.dump(fcc, f, ensure_ascii=False, indent=2)
    print(f"[Backtest] FailureCaseCard 저장: {fcc_path}")
    print(
        f"[Backtest] 실패 케이스: {fcc['summary']['total_failures']}건 "
        f"(실패율 {fcc['summary']['failure_rate']:.2%})"
    )

    print(f"\n[Backtest] 완료.")


if __name__ == "__main__":
    main()
