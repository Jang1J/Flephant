"""
Ablation Runner — W10 실험 자동화

Must-have ablation (AI #1 담당):
  #2: UQ on vs No-UQ Risk — UQ tail cap 있음 vs 없음
  #3: PFS(stateful) vs stateless — 진입가 기반 vs return_5d

Must-have ablation (AI #2 담당):
  #1: pure-quant vs full(quant+news) — 뉴스 신호 기여도 측정
  #4: synthesis weight sensitivity — quant_weight 0.5/0.7/0.9 비교

Usage:
    python jobs/run_ablation.py 20260324 --experiment uq          # #2만
    python jobs/run_ablation.py 20260324 --experiment pfs         # #3만
    python jobs/run_ablation.py 20260324 --experiment quant_news  # #1만
    python jobs/run_ablation.py 20260324 --experiment synth_weight # #4만
    python jobs/run_ablation.py 20260324 --experiment all         # 전부
    python jobs/run_ablation.py --replay 5 --experiment all       # 5일 replay 기반
"""

import sys
import json
import copy
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE_DIR))

ABL_DIR = _BASE_DIR / "artifacts" / "ablation"
ABL_DIR.mkdir(parents=True, exist_ok=True)

DMP_DIR = _BASE_DIR / "artifacts" / "daily_market_packet"

KST = timezone(timedelta(hours=9))

_BACKTEST_PLACEHOLDER = {
    "status": "phase1_placeholder",
    "win_rate_hint": None,
    "baseline_delta": None,
    "failure_tags": [],
    "confidence_band": None,
    "recent_sharpe": None,
    "recent_mdd": None,
    "last_updated": None,
    "note": "Phase 1: Backtest Agent 미구현.",
}


# ── 헬퍼: 아티팩트 임시 디렉토리 없이 메모리 내 비교용 결과 추출 ────────────


def _summarize_fdc_pfs(fdc: dict, pfs: dict) -> dict:
    """FDC + PFS에서 비교용 요약을 추출한다. 원본 아티팩트 덮어쓰기 없음."""
    exec_sum = fdc.get("execution_summary", {})
    positions = {
        p["ticker"]: p["weight"]
        for p in pfs.get("positions", [])
    }
    return {
        "approved_count": exec_sum.get("approved_count", 0),
        "vetoed_count": exec_sum.get("vetoed_count", 0),
        "total_exposure": exec_sum.get("total_exposure", pfs.get("total_exposure", 0.0)),
        "cash_ratio": exec_sum.get("cash_ratio", pfs.get("cash_ratio", 100.0)),
        "positions": positions,
        "turnover": pfs.get("daily_turnover"),
        "stop_loss_count": len(pfs.get("realized_pnl", [])),
    }


def _build_comparison(baseline_sum: dict, variant_sum: dict) -> dict:
    """두 결과 요약에서 비교 지표를 생성한다."""
    b_approved = set(
        t for t, w in baseline_sum["positions"].items() if w > 0
    )
    v_approved = set(
        t for t, w in variant_sum["positions"].items() if w > 0
    )
    approval_diff = sorted(b_approved.symmetric_difference(v_approved))

    all_tickers = set(baseline_sum["positions"]) | set(variant_sum["positions"])
    weight_changes = {}
    for t in all_tickers:
        bw = baseline_sum["positions"].get(t, 0.0)
        vw = variant_sum["positions"].get(t, 0.0)
        diff = round(vw - bw, 4)
        if diff != 0:
            weight_changes[t] = diff

    exposure_delta = round(
        variant_sum["total_exposure"] - baseline_sum["total_exposure"], 4
    )

    return {
        "exposure_delta": exposure_delta,
        "approval_diff": approval_diff,
        "weight_changes": weight_changes,
        "summary": (
            f"노출 차이: {exposure_delta:+.2f}%, "
            f"승인 종목 차이: {len(approval_diff)}개, "
            f"비중 변경 종목: {len(weight_changes)}개"
        ),
    }


# ── Ablation #2: UQ on vs No-UQ Risk ─────────────────────────


def run_uq_ablation(target_date: str) -> dict:
    """
    UQ ON(기본) vs UQ OFF 결과를 메모리 내에서 비교한다.
    원본 아티팩트(RC, COP, FDC, PFS)는 UQ ON 결과만 디스크에 저장된다.
    UQ OFF 결과는 임시 객체로만 처리한다.
    """
    print(f"\n[Ablation] #2 UQ on/off 실험: {target_date}")
    from jobs.run_risk_engine import run_risk_engine
    from jobs.strategy_loader import has_real_sc
    from jobs.portfolio_manager import PortfolioManager
    from agents.final_decision_agent import FinalDecisionAgent

    use_mock = not has_real_sc(target_date)
    pm = PortfolioManager()
    fda = FinalDecisionAgent(use_llm=False)

    # ── Baseline: UQ ON ──
    print(f"[Ablation] UQ ON 실행...")
    pfs_base = pm.load_or_init(target_date)
    dmp_path = DMP_DIR / f"DMP-{target_date}.json"
    with open(dmp_path, encoding="utf-8") as f:
        dmp = json.load(f)

    rc_base, cop_base = run_risk_engine(
        target_date, use_mock=use_mock,
        portfolio_state=copy.deepcopy(pfs_base),
        backtest_summary=_BACKTEST_PLACEHOLDER,
        disable_uq=False,
    )
    fdc_base = fda.run(
        target_date=target_date,
        cop=cop_base,
        risk_card=rc_base,
        portfolio_state=copy.deepcopy(pfs_base),
        backtest_summary=_BACKTEST_PLACEHOLDER,
    )
    pfs_base_updated = pm.apply_decisions(
        copy.deepcopy(pfs_base), fdc_base, cop_base, dmp
    )
    baseline_sum = _summarize_fdc_pfs(fdc_base, pfs_base_updated)
    baseline_sum["label"] = "UQ ON"
    print(f"[Ablation] UQ ON 완료 — 승인: {baseline_sum['approved_count']}, 노출: {baseline_sum['total_exposure']}%")

    # ── Variant: UQ OFF (dry_run=True로 RC/COP 저장 억제) ──
    print(f"[Ablation] UQ OFF 실행 (dry_run=True — RC/COP 덮어쓰기 없음)...")
    pfs_var = pm.load_or_init(target_date)
    rc_var, cop_var = run_risk_engine(
        target_date, use_mock=use_mock,
        portfolio_state=copy.deepcopy(pfs_var),
        backtest_summary=_BACKTEST_PLACEHOLDER,
        disable_uq=True,
        dry_run=True,
    )
    fdc_var = fda.run(
        target_date=target_date,
        cop=cop_var,
        risk_card=rc_var,
        portfolio_state=copy.deepcopy(pfs_var),
        backtest_summary=_BACKTEST_PLACEHOLDER,
    )
    pfs_var_updated = pm.apply_decisions(
        copy.deepcopy(pfs_var), fdc_var, cop_var, dmp
    )
    variant_sum = _summarize_fdc_pfs(fdc_var, pfs_var_updated)
    variant_sum["label"] = "UQ OFF"
    print(f"[Ablation] UQ OFF 완료 — 승인: {variant_sum['approved_count']}, 노출: {variant_sum['total_exposure']}%")

    comparison = _build_comparison(baseline_sum, variant_sum)

    result = {
        "ablation_id": f"ABL-uq-{target_date}",
        "experiment": "uq_on_off",
        "target_date": target_date,
        "generated_at": datetime.now(KST).isoformat(),
        "baseline": baseline_sum,
        "variant": variant_sum,
        "comparison": comparison,
    }

    out_path = ABL_DIR / f"ABL-uq-{target_date}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[Ablation] 결과 저장: {out_path}")
    print(f"[Ablation] 요약: {comparison['summary']}")

    return result


# ── Ablation #3: PFS stateful vs stateless ────────────────────


def run_pfs_ablation(target_date: str) -> dict:
    """
    Stateful(기본, 이월 포지션 기반) vs Stateless(매일 fresh start) 비교.
    Stateless 실행 시 실제 PFS 파일을 수정하지 않는다 (임시 복사본 사용).
    """
    print(f"\n[Ablation] #3 PFS stateful/stateless 실험: {target_date}")
    from jobs.run_risk_engine import run_risk_engine
    from jobs.strategy_loader import has_real_sc
    from jobs.portfolio_manager import PortfolioManager
    from agents.final_decision_agent import FinalDecisionAgent

    use_mock = not has_real_sc(target_date)
    pm = PortfolioManager()
    fda = FinalDecisionAgent(use_llm=False)

    dmp_path = DMP_DIR / f"DMP-{target_date}.json"
    with open(dmp_path, encoding="utf-8") as f:
        dmp = json.load(f)

    # ── Baseline: Stateful (이월 포지션 기반) ──
    print(f"[Ablation] Stateful 실행 (이월 포지션 기반)...")
    pfs_stateful = pm.load_or_init(target_date)
    rc_sf, cop_sf = run_risk_engine(
        target_date, use_mock=use_mock,
        portfolio_state=copy.deepcopy(pfs_stateful),
        backtest_summary=_BACKTEST_PLACEHOLDER,
        disable_uq=False,
    )
    fdc_sf = fda.run(
        target_date=target_date,
        cop=cop_sf,
        risk_card=rc_sf,
        portfolio_state=copy.deepcopy(pfs_stateful),
        backtest_summary=_BACKTEST_PLACEHOLDER,
    )
    pfs_sf_updated = pm.apply_decisions(
        copy.deepcopy(pfs_stateful), fdc_sf, cop_sf, dmp
    )
    baseline_sum = _summarize_fdc_pfs(fdc_sf, pfs_sf_updated)
    baseline_sum["label"] = "Stateful"
    print(
        f"[Ablation] Stateful 완료 — "
        f"승인: {baseline_sum['approved_count']}, "
        f"stop-loss: {baseline_sum['stop_loss_count']}건, "
        f"turnover: {baseline_sum['turnover']}%"
    )

    # ── Variant: Stateless (빈 포트폴리오로 fresh start, dry_run=True로 RC/COP 저장 억제) ──
    print(f"[Ablation] Stateless 실행 (강제 초기 상태, dry_run=True — RC/COP 덮어쓰기 없음)...")
    pfs_stateless = pm.init_empty(target_date)  # 항상 빈 상태 — 실제 파일 저장 없음
    rc_sl, cop_sl = run_risk_engine(
        target_date, use_mock=use_mock,
        portfolio_state=copy.deepcopy(pfs_stateless),
        backtest_summary=_BACKTEST_PLACEHOLDER,
        disable_uq=False,
        dry_run=True,
    )
    fdc_sl = fda.run(
        target_date=target_date,
        cop=cop_sl,
        risk_card=rc_sl,
        portfolio_state=copy.deepcopy(pfs_stateless),
        backtest_summary=_BACKTEST_PLACEHOLDER,
    )
    pfs_sl_updated = pm.apply_decisions(
        copy.deepcopy(pfs_stateless), fdc_sl, cop_sl, dmp
    )
    variant_sum = _summarize_fdc_pfs(fdc_sl, pfs_sl_updated)
    variant_sum["label"] = "Stateless"
    print(
        f"[Ablation] Stateless 완료 — "
        f"승인: {variant_sum['approved_count']}, "
        f"stop-loss: {variant_sum['stop_loss_count']}건, "
        f"turnover: {variant_sum['turnover']}%"
    )

    comparison = _build_comparison(baseline_sum, variant_sum)

    result = {
        "ablation_id": f"ABL-pfs-{target_date}",
        "experiment": "pfs_stateful_stateless",
        "target_date": target_date,
        "generated_at": datetime.now(KST).isoformat(),
        "baseline": baseline_sum,
        "variant": variant_sum,
        "comparison": comparison,
    }

    out_path = ABL_DIR / f"ABL-pfs-{target_date}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[Ablation] 결과 저장: {out_path}")
    print(f"[Ablation] 요약: {comparison['summary']}")

    return result


# ── Ablation #1 (AI #2): pure-quant vs full(quant+news) ──────


def run_quant_news_ablation(target_date: str) -> dict:
    """
    AI #2 Ablation #1: pure-quant SC vs full(quant+news) SC 비교.
    뉴스 신호의 실질적 기여도를 측정한다.
    """
    print(f"\n[Ablation] #1 pure-quant vs full(quant+news): {target_date}")

    quant_only_path = _BASE_DIR / "artifacts" / "strategy_card_variants" / "quant_only" / f"SC-{target_date}.json"
    momentum_path = _BASE_DIR / "artifacts" / "strategy_card_variants" / "momentum" / f"SC-{target_date}.json"

    # SC가 없으면 먼저 생성 시도
    if not quant_only_path.exists() or not momentum_path.exists():
        print(f"[Ablation] SC 미생성 — build_strategy_card_momentum.py 실행...")
        import subprocess
        subprocess.run(
            [sys.executable, str(_BASE_DIR / "jobs" / "build_strategy_card_momentum.py"), target_date],
            cwd=str(_BASE_DIR),
        )

    if not quant_only_path.exists() or not momentum_path.exists():
        print(f"[Ablation] SC 생성 실패 — 건너뜀")
        return {"error": f"SC 생성 실패: {target_date}"}

    with open(quant_only_path, encoding="utf-8") as f:
        quant_cards = json.load(f)
    with open(momentum_path, encoding="utf-8") as f:
        full_cards = json.load(f)

    # signal 분포 비교
    def _signal_dist(cards):
        dist = {}
        for c in cards:
            sig = c.get("signal", "unknown")
            dist[sig] = dist.get(sig, 0) + 1
        return dist

    def _buy_tickers(cards):
        return set(c["ticker"] for c in cards if c.get("signal") in ("strong_buy", "buy"))

    quant_dist = _signal_dist(quant_cards)
    full_dist = _signal_dist(full_cards)
    quant_buys = _buy_tickers(quant_cards)
    full_buys = _buy_tickers(full_cards)

    # confidence 차이 분석
    quant_conf_map = {c["ticker"]: c.get("confidence", 0) for c in quant_cards}
    full_conf_map = {c["ticker"]: c.get("confidence", 0) for c in full_cards}
    conf_diffs = {}
    for tk in quant_conf_map:
        diff = round(full_conf_map.get(tk, 0) - quant_conf_map.get(tk, 0), 4)
        if abs(diff) > 0.001:
            conf_diffs[tk] = diff

    result = {
        "ablation_id": f"ABL-quant_news-{target_date}",
        "experiment": "pure_quant_vs_full",
        "target_date": target_date,
        "generated_at": datetime.now(KST).isoformat(),
        "baseline": {"label": "pure-quant", "signal_dist": quant_dist, "buy_count": len(quant_buys)},
        "variant": {"label": "full(quant+news)", "signal_dist": full_dist, "buy_count": len(full_buys)},
        "comparison": {
            "buy_overlap": sorted(quant_buys & full_buys),
            "quant_only_buys": sorted(quant_buys - full_buys),
            "news_added_buys": sorted(full_buys - quant_buys),
            "confidence_diffs": conf_diffs,
            "summary": (
                f"quant BUY {len(quant_buys)}종목, full BUY {len(full_buys)}종목, "
                f"겹침 {len(quant_buys & full_buys)}종목, "
                f"뉴스로 추가 {len(full_buys - quant_buys)}종목, "
                f"뉴스로 제거 {len(quant_buys - full_buys)}종목"
            ),
        },
    }

    out_path = ABL_DIR / f"ABL-quant_news-{target_date}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[Ablation] 결과 저장: {out_path}")
    print(f"[Ablation] 요약: {result['comparison']['summary']}")

    return result


# ── Ablation #4 (AI #2): synthesis weight sensitivity ────────


def run_synth_weight_ablation(target_date: str) -> dict:
    """
    AI #2 Ablation #4: quant_weight 0.5/0.7/0.9에 따른 SC 변화 측정.
    같은 quant_scores + news_signals에서 가중치만 변경하여 비교한다.
    """
    print(f"\n[Ablation] #4 synthesis weight sensitivity: {target_date}")

    import numpy as np
    import pandas as pd

    dmp_path = DMP_DIR / f"DMP-{target_date}.json"
    if not dmp_path.exists():
        return {"error": f"DMP 없음: {target_date}"}

    with open(dmp_path, encoding="utf-8") as f:
        dmp = json.load(f)

    universe_csv = _BASE_DIR / "config" / "universe_v1.csv"
    universe = pd.read_csv(universe_csv, dtype={"ticker": str})
    tickers = [str(t).zfill(6) for t in universe["ticker"]]

    # quant scores: DMP return_5d 기반 rank → 0~1 정규화 (모델 미학습 시 fallback)
    scores = {}
    for tk in tickers:
        tf = dmp.get("market_data", {}).get(tk, {}).get("tech_features", {})
        scores[tk] = float(tf.get("return_5d", 0.0))
    vals = list(scores.values())
    mn, mx = min(vals), max(vals)
    rng = mx - mn if mx > mn else 1.0
    quant_scores = {tk: (v - mn) / rng for tk, v in scores.items()}

    # news signals: 간단히 0.0 (뉴스 미사용 baseline)
    news_signals = {tk: 0.0 for tk in tickers}
    try:
        from models.strategy_model.news_strategy import compute_news_signals
        ns = compute_news_signals(target_date, tickers)
        if ns:
            news_signals = ns
    except Exception as e:
        print(f"[Ablation] 뉴스 로드 실패 (0.0 사용): {e}")

    # 가중치별 SC 생성 비교
    weights = [0.5, 0.7, 0.9]
    results_by_weight = {}

    for qw in weights:
        nw = round(1.0 - qw, 2)
        buy_tickers = []
        confs = {}
        for tk in tickers:
            qs = quant_scores.get(tk, 0.5)
            ns_val = news_signals.get(tk, 0.0)
            combined = qs * qw + ((ns_val + 1) / 2) * nw
            confs[tk] = round(combined, 4)
            if combined >= 0.55:
                buy_tickers.append(tk)
        results_by_weight[f"qw_{qw}"] = {
            "quant_weight": qw,
            "news_weight": nw,
            "buy_count": len(buy_tickers),
            "buy_tickers": sorted(buy_tickers),
            "avg_confidence": round(sum(confs.values()) / len(confs), 4) if confs else 0,
        }
        print(f"[Ablation] qw={qw}: BUY {len(buy_tickers)}종목, avg_conf={results_by_weight[f'qw_{qw}']['avg_confidence']}")

    result = {
        "ablation_id": f"ABL-synth_weight-{target_date}",
        "experiment": "synthesis_weight_sensitivity",
        "target_date": target_date,
        "generated_at": datetime.now(KST).isoformat(),
        "variants": results_by_weight,
        "comparison": {
            "weight_range": [0.5, 0.7, 0.9],
            "buy_counts": {k: v["buy_count"] for k, v in results_by_weight.items()},
            "summary": " / ".join(
                f"qw={v['quant_weight']}: BUY {v['buy_count']}종목"
                for v in results_by_weight.values()
            ),
        },
    }

    out_path = ABL_DIR / f"ABL-synth_weight-{target_date}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[Ablation] 결과 저장: {out_path}")
    print(f"[Ablation] 요약: {result['comparison']['summary']}")

    return result


# ── Replay 모드: N거래일 연속 ablation ────────────────────────


def _get_replay_dates(n: int) -> list:
    """backfill된 DMP에서 평일 날짜 최근 N개 반환"""
    dates = []
    for p in sorted(DMP_DIR.glob("DMP-*.json")):
        date_str = p.stem.replace("DMP-", "")
        if len(date_str) == 8:
            from datetime import datetime as _dt
            if _dt.strptime(date_str, "%Y%m%d").weekday() < 5:
                dates.append(date_str)
    return dates[-n:]


def run_replay_ablation(days: int, experiment: str):
    """N거래일에 걸쳐 ablation을 반복 실행한다."""
    dates = _get_replay_dates(days)
    print(f"\n[Ablation] Replay {days}거래일: {dates}")
    print(f"[Ablation] 실험: {experiment}")

    all_results = {}
    for td in dates:
        dmp_path = DMP_DIR / f"DMP-{td}.json"
        if not dmp_path.exists():
            print(f"[Ablation] DMP 없음 — 건너뜀: {td}")
            continue
        try:
            if experiment in ("uq", "all"):
                all_results[f"uq-{td}"] = run_uq_ablation(td)
            if experiment in ("pfs", "all"):
                all_results[f"pfs-{td}"] = run_pfs_ablation(td)
            if experiment in ("quant_news", "all"):
                all_results[f"quant_news-{td}"] = run_quant_news_ablation(td)
            if experiment in ("synth_weight", "all"):
                all_results[f"synth_weight-{td}"] = run_synth_weight_ablation(td)
        except Exception as e:
            print(f"[Ablation] {td} 실패: {e}")
            all_results[f"error-{td}"] = str(e)

    print(f"\n[Ablation] Replay 완료: {len(all_results)}건")
    return all_results


# ── 진입점 ────────────────────────────────────────────────────


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ablation Runner — W10 실험 자동화")
    parser.add_argument(
        "date", nargs="?", default=None,
        help="대상 날짜 YYYYMMDD (--replay 사용 시 불필요)"
    )
    parser.add_argument(
        "--experiment", choices=["uq", "pfs", "quant_news", "synth_weight", "all"], default="all",
        help="실험 종류 (uq=#2, pfs=#3, quant_news=#1, synth_weight=#4, all=전부)"
    )
    parser.add_argument(
        "--replay", type=int, default=None, metavar="N",
        help="N거래일 replay 기반 실험"
    )
    args = parser.parse_args()

    if args.replay:
        run_replay_ablation(args.replay, args.experiment)
    else:
        if not args.date:
            parser.error("단일 날짜 실행 시 date 인수가 필요합니다.")
        target = args.date

        dmp_path = DMP_DIR / f"DMP-{target}.json"
        if not dmp_path.exists():
            print(f"[Ablation] DMP 파일 없음: {dmp_path}")
            print(f"[Ablation] 먼저 python jobs/build_daily_market_packet.py {target} 를 실행하세요.")
            sys.exit(1)

        if args.experiment in ("uq", "all"):
            run_uq_ablation(target)
        if args.experiment in ("pfs", "all"):
            run_pfs_ablation(target)
        if args.experiment in ("quant_news", "all"):
            run_quant_news_ablation(target)
        if args.experiment in ("synth_weight", "all"):
            run_synth_weight_ablation(target)
