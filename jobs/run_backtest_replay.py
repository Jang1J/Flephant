"""
KR-Rebound-CNN Replay Backtest
- 실제 종가/시가 기반 portfolio replay
- t일 SC → t+1 시가 체결 → t+1 종가 MTM

Usage:
    python jobs/run_backtest_replay.py --start 20250801 --end 20260325
    python jobs/run_backtest_replay.py --days 100
"""

import sys
import json
import argparse
from pathlib import Path
from datetime import datetime

_BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE_DIR))

import numpy as np
import pandas as pd

DMP_DIR = _BASE_DIR / "artifacts" / "daily_market_packet"
SC_VARIANT_DIR = _BASE_DIR / "artifacts" / "strategy_card_variants" / "rebound"
REPORT_DIR = _BASE_DIR / "reports" / "backtest"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

UNIVERSE_CSV = _BASE_DIR / "config" / "universe_v1.csv"
COST_PER_SIDE = 0.00015  # 편도 0.015%


def get_available_dates(start=None, end=None):
    """DMP 파일에서 가용 거래일 목록 추출"""
    dmp_files = sorted(DMP_DIR.glob("DMP-*.json"))
    dates = [f.stem.replace("DMP-", "") for f in dmp_files]
    if start:
        dates = [d for d in dates if d >= start]
    if end:
        dates = [d for d in dates if d <= end]
    return dates


def load_dmp_prices(date_str):
    """DMP에서 종목별 시가/종가 추출"""
    path = DMP_DIR / f"DMP-{date_str}.json"
    if not path.exists():
        return {}
    with open(path) as f:
        dmp = json.load(f)
    prices = {}
    for ticker, tdata in dmp.get("market_data", {}).items():
        ohlcv = tdata.get("ohlcv", {})
        prices[ticker] = {
            "open": float(ohlcv.get("open") or 0),
            "close": float(ohlcv.get("close") or 0),
        }
    return prices


def load_sc_signals(date_str):
    """SC variant에서 종목별 signal/direction/confidence 추출"""
    path = SC_VARIANT_DIR / f"SC-{date_str}.json"
    if not path.exists():
        return {}
    with open(path) as f:
        cards = json.load(f)
    return {
        c["ticker"]: {
            "signal": c.get("signal", "hold"),
            "direction": c.get("direction", "neutral"),
            "confidence": c.get("confidence", 0),
        }
        for c in cards
    }


def run_backtest(dates, initial_cash=100_000_000):
    """
    실제 가격 기반 replay backtest.
    - t일 SC 생성 (이미 존재하는 SC 사용)
    - t+1 시가 체결, t+1 종가 MTM
    """
    universe = pd.read_csv(UNIVERSE_CSV, dtype={"ticker": str})
    universe["ticker"] = universe["ticker"].apply(lambda x: str(x).zfill(6))
    all_tickers = universe["ticker"].tolist()

    # 벤치마크: equal-weight buy-and-hold
    bm_nav = [1.0]

    # 포트폴리오 상태
    cash = initial_cash
    positions = {}  # {ticker: {"shares": N, "entry_price": P}}
    nav_history = []
    trade_history = []
    daily_metrics = []

    for i in range(len(dates) - 1):
        t_date = dates[i]
        t1_date = dates[i + 1]

        # t일 SC 로드
        signals = load_sc_signals(t_date)

        # t+1 시가/종가
        t1_prices = load_dmp_prices(t1_date)
        t_prices = load_dmp_prices(t_date)

        if not t1_prices:
            continue

        # 벤치마크: EW daily return
        bm_rets = []
        for tk in all_tickers:
            if tk in t_prices and tk in t1_prices:
                p0 = t_prices[tk]["close"]
                p1 = t1_prices[tk]["close"]
                if p0 > 0:
                    bm_rets.append((p1 - p0) / p0)
        if bm_rets:
            bm_nav.append(bm_nav[-1] * (1 + np.mean(bm_rets)))

        # 매수 대상: buy/strong_buy
        buy_tickers = [
            tk for tk, sig in signals.items()
            if sig["signal"] in ("buy", "strong_buy") and sig["direction"] == "long"
        ]

        # 기존 보유 종목 중 hold가 아닌 것 매도
        sell_tickers = [
            tk for tk in list(positions.keys())
            if tk not in [t for t in buy_tickers]
        ]

        # 매도 실행 (t+1 시가)
        for tk in sell_tickers:
            if tk in positions and tk in t1_prices:
                pos = positions[tk]
                sell_price = t1_prices[tk]["open"]
                if sell_price > 0:
                    proceeds = pos["shares"] * sell_price
                    cost = proceeds * COST_PER_SIDE
                    cash += proceeds - cost
                    trade_history.append({
                        "date": t1_date,
                        "ticker": tk,
                        "action": "sell",
                        "price": sell_price,
                        "shares": pos["shares"],
                        "cost": round(cost, 2),
                    })
                    del positions[tk]

        # 매수 실행 (t+1 시가, 균등배분)
        if buy_tickers:
            # 최대 10종목, 현금의 90% 사용
            buy_tickers = buy_tickers[:10]
            investable = cash * 0.9
            per_stock = investable / len(buy_tickers)

            for tk in buy_tickers:
                if tk in positions:
                    continue  # 이미 보유
                if tk not in t1_prices:
                    continue
                buy_price = t1_prices[tk]["open"]
                if buy_price <= 0:
                    continue
                shares = int(per_stock / buy_price)
                if shares <= 0:
                    continue
                cost_amount = shares * buy_price
                trade_cost = cost_amount * COST_PER_SIDE
                cash -= cost_amount + trade_cost
                positions[tk] = {"shares": shares, "entry_price": buy_price}
                trade_history.append({
                    "date": t1_date,
                    "ticker": tk,
                    "action": "buy",
                    "price": buy_price,
                    "shares": shares,
                    "cost": round(trade_cost, 2),
                })

        # Mark-to-market (t+1 종가)
        portfolio_value = cash
        for tk, pos in positions.items():
            if tk in t1_prices:
                portfolio_value += pos["shares"] * t1_prices[tk]["close"]

        nav = portfolio_value / initial_cash
        n_positions = len(positions)
        exposure = 1.0 - (cash / portfolio_value) if portfolio_value > 0 else 0.0

        nav_history.append({
            "date": t1_date,
            "nav": round(nav, 6),
            "cash": round(cash, 2),
            "n_positions": n_positions,
            "exposure": round(exposure, 4),
            "n_buys": len(buy_tickers),
            "n_sells": len(sell_tickers),
        })

        daily_metrics.append(nav)

    # 성과 계산
    nav_arr = np.array(daily_metrics)
    if len(nav_arr) > 1:
        daily_rets = np.diff(nav_arr) / nav_arr[:-1]
        cumulative_return = float(nav_arr[-1] / nav_arr[0] - 1) if nav_arr[0] > 0 else 0
        sharpe = float(daily_rets.mean() / (daily_rets.std() + 1e-10) * np.sqrt(252))
        running_max = np.maximum.accumulate(nav_arr)
        drawdowns = (nav_arr - running_max) / (running_max + 1e-10)
        max_drawdown = float(drawdowns.min())
    else:
        cumulative_return = 0.0
        sharpe = 0.0
        max_drawdown = 0.0

    # 벤치마크 성과
    bm_arr = np.array(bm_nav)
    bm_return = float(bm_arr[-1] / bm_arr[0] - 1) if len(bm_arr) > 1 else 0

    total_trades = len(trade_history)
    total_cost = sum(t["cost"] for t in trade_history)

    summary = {
        "backtest_range": {"start": dates[0], "end": dates[-1]},
        "n_trading_days": len(dates) - 1,
        "initial_cash": initial_cash,
        "final_nav": round(nav_arr[-1], 6) if len(nav_arr) > 0 else 1.0,
        "cumulative_return": round(cumulative_return, 6),
        "sharpe_ratio": round(sharpe, 4),
        "max_drawdown": round(max_drawdown, 6),
        "total_trades": total_trades,
        "total_cost": round(total_cost, 2),
        "avg_exposure": round(np.mean([d["exposure"] for d in nav_history]), 4) if nav_history else 0,
        "benchmark_ew_return": round(bm_return, 6),
        "excess_return": round(cumulative_return - bm_return, 6),
        "execution_rule": "t+1 open fill, t+1 close MTM",
        "cost_per_side": COST_PER_SIDE,
    }

    return summary, nav_history, trade_history


def main():
    parser = argparse.ArgumentParser(description="KR-Rebound Replay Backtest")
    parser.add_argument("--start", type=str, default=None)
    parser.add_argument("--end", type=str, default=None)
    parser.add_argument("--days", type=int, default=None)
    args = parser.parse_args()

    dates = get_available_dates(args.start, args.end)
    if args.days:
        dates = dates[-args.days:]

    if len(dates) < 2:
        print("[Backtest] 가용 거래일 부족. 최소 2일 필요.")
        return

    print(f"[Backtest] 기간: {dates[0]} ~ {dates[-1]} ({len(dates)}거래일)")

    # SC가 없는 날짜는 먼저 생성 필요
    missing_sc = [d for d in dates if not (SC_VARIANT_DIR / f"SC-{d}.json").exists()]
    if missing_sc:
        print(f"[Backtest] SC 미생성 날짜 {len(missing_sc)}개. 먼저 SC를 생성하세요.")
        print(f"  예: python jobs/build_strategy_card_rebound.py {missing_sc[0]}")
        # SC 없는 날짜는 skip
        dates = [d for d in dates if d not in missing_sc]

    summary, nav_history, trade_history = run_backtest(dates)

    # 저장
    summary_path = REPORT_DIR / "backtest_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"[Backtest] 요약 저장: {summary_path}")

    nav_path = REPORT_DIR / "backtest_nav.json"
    with open(nav_path, "w", encoding="utf-8") as f:
        json.dump(nav_history, f, ensure_ascii=False, indent=2)

    trades_path = REPORT_DIR / "backtest_trades.json"
    with open(trades_path, "w", encoding="utf-8") as f:
        json.dump(trade_history, f, ensure_ascii=False, indent=2)

    # 결과 출력
    print(f"\n{'='*50}")
    print(f"KR-Rebound Backtest 결과")
    print(f"{'='*50}")
    print(f"기간: {summary['backtest_range']['start']} ~ {summary['backtest_range']['end']}")
    print(f"거래일: {summary['n_trading_days']}일")
    print(f"누적수익: {summary['cumulative_return']:.2%}")
    print(f"Sharpe: {summary['sharpe_ratio']:.2f}")
    print(f"MDD: {summary['max_drawdown']:.2%}")
    print(f"총 거래: {summary['total_trades']}건 (비용: {summary['total_cost']:,.0f}원)")
    print(f"평균 노출: {summary['avg_exposure']:.1%}")
    print(f"벤치마크(EW): {summary['benchmark_ew_return']:.2%}")
    print(f"초과수익: {summary['excess_return']:.2%}")
    print(f"실행규칙: {summary['execution_rule']}")


if __name__ == "__main__":
    main()
