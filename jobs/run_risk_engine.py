"""
Risk Engine v1
- StrategyCard[] 입력 → RiskCard + CandidateOrderPlan 출력
- Tier 1: Regime Gate (vix_proxy, market_breadth)
- Tier 2: Position-Level Constraints (position cap, sector cap, confidence, volume)
- Core Rules: stop-loss, turnover cap, min cash ratio

Usage:
    python jobs/run_risk_engine.py 20260320
    python jobs/run_risk_engine.py 20260320 --mock   # mock StrategyCard 사용
"""

import sys
import json
import yaml
import pandas as pd
from datetime import datetime
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE_DIR))

from jobs.strategy_loader import load_strategy_cards, generate_mock_strategy_cards  # noqa: F401
from connectors import now_kst, now_kst_iso, make_snapshot_dt
from connectors.llm_router import call_llm

# ── 경로 ──────────────────────────────────────────────────────
POLICY_PATH = _BASE_DIR / "config" / "risk_policy_v0.yaml"
DMP_DIR = _BASE_DIR / "artifacts" / "daily_market_packet"
SC_DIR = _BASE_DIR / "artifacts" / "strategy_card"
OUTPUT_DIR = _BASE_DIR / "artifacts"
UNIVERSE_PATH = _BASE_DIR / "config" / "universe_v1.csv"


def load_policy() -> dict:
    with open(POLICY_PATH) as f:
        return yaml.safe_load(f)


def load_dmp(target_date: str) -> dict | None:
    path = DMP_DIR / f"DMP-{target_date}.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


# ── Tier 1: Regime Gate ──────────────────────────────────────
def determine_regime(macro: dict, policy: dict) -> dict:
    """매크로 데이터에서 regime 판정"""
    rg = policy.get("regime_gate", {})

    vix = macro.get("vix_proxy")
    breadth = macro.get("market_breadth")

    # VIX 판정 (red → yellow → green 우선순위)
    vix_config = rg.get("vix_proxy", {})
    if vix is not None:
        if vix >= vix_config.get("red_gte", 90):
            vix_regime = "red"
        elif vix >= vix_config.get("yellow_gte", 70):
            vix_regime = "yellow"
        else:
            vix_regime = "green"
    else:
        vix_regime = "green"  # 데이터 없으면 보수적으로 green

    # Market Breadth 판정
    mb_config = rg.get("market_breadth", {})
    if breadth is not None:
        if breadth < mb_config.get("red_lt", 0.30):
            mb_regime = "red"
        elif breadth < mb_config.get("yellow_lt", 0.45):
            mb_regime = "yellow"
        else:
            mb_regime = "green"
    else:
        mb_regime = "green"

    # 최종: 더 나쁜 쪽 채택
    regime_order = {"red": 2, "yellow": 1, "green": 0}
    if regime_order[vix_regime] >= regime_order[mb_regime]:
        final = vix_regime
    else:
        final = mb_regime

    reason_parts = []
    if vix is not None:
        reason_parts.append(f"VIX proxy={vix} → {vix_regime}")
    else:
        reason_parts.append("VIX proxy=N/A → green(default)")
    if breadth is not None:
        reason_parts.append(f"Market breadth={breadth} → {mb_regime}")
    else:
        reason_parts.append("Market breadth=N/A → green(default)")

    return {
        "label": final,
        "vix_proxy": vix,
        "market_breadth": breadth,
        "regime_reason": "; ".join(reason_parts),
    }


# ── Tier 2: Position Constraints ─────────────────────────────
def apply_position_constraints(cards: list, regime: dict, policy: dict, universe_df=None,
                                portfolio_state: dict = None, dmp: dict = None) -> tuple:
    """
    StrategyCard에 position-level 제약 적용.
    portfolio_state가 있으면 기존 보유 포지션을 합산해 전체 포트폴리오 기준으로 제약 적용.
    dmp가 있으면 common_filters (min_avg_volume_20d) 적용.
    Returns: (position_risks[], approved_orders[])
    """
    pc = policy.get("position_constraints", {})
    core = policy.get("core_rules", {})
    actions = policy.get("regime_gate", {}).get("actions", {})
    regime_action = actions.get(regime["label"], {})
    cf = policy.get("common_filters", {})

    max_positions = pc.get("max_position_count", 10)
    max_single = pc.get("max_single_weight", 20.0)
    max_sector = pc.get("max_sector_weight", 40.0)
    min_conf = pc.get("min_confidence", 0.3)
    min_cash = core.get("min_cash_ratio", {}).get("ratio", 10.0)
    new_entry_mult = regime_action.get("new_entry_weight_multiplier", 1.0)
    min_avg_volume_20d = cf.get("min_avg_volume_20d")

    # sector 매핑 로드
    uni = pd.read_csv(UNIVERSE_PATH)
    ticker_sector = {}
    for _, row in uni.iterrows():
        ticker_sector[str(row["ticker"]).zfill(6)] = row["wics_sector"]

    # PFS 기존 보유 포지션 정보 추출
    pfs_positions = {}
    if portfolio_state:
        for pos in portfolio_state.get("positions", []):
            pfs_positions[pos["ticker"]] = pos

    existing_tickers = set(pfs_positions.keys())
    existing_count = len(existing_tickers)

    # 기존 보유 섹터 비중 누적 (sector_weights 초기값으로 사용)
    sector_weights = {}
    for ticker, pos in pfs_positions.items():
        sector = ticker_sector.get(ticker, "기타")
        sector_weights[sector] = sector_weights.get(sector, 0) + pos.get("weight", 0)

    # 기존 보유 노출 합산 (신규 buy 허용 여부 계산용)
    existing_exposure = sum(pos.get("weight", 0) for pos in pfs_positions.values())

    # buy/strong_buy만 신규 주문 대상 (기존 보유 종목 제외)
    buy_cards = [
        c for c in cards
        if c["signal"] in ["strong_buy", "buy"]
        and c["confidence"] >= min_conf
        and c["ticker"] not in existing_tickers
    ]
    buy_cards.sort(key=lambda c: c["confidence"], reverse=True)

    # common_filters: min_avg_volume_20d — DMP volume_ratio_20 기반 거래량 필터
    if min_avg_volume_20d is not None and dmp:
        market_data = dmp.get("market_data", {})
        filtered_by_vol = []
        for card in buy_cards:
            ticker = card["ticker"]
            md = market_data.get(ticker, {})
            tech = md.get("tech_features", {})
            volume_ratio_20 = tech.get("volume_ratio_20")
            volume_today = md.get("volume")
            if volume_ratio_20 is not None and volume_today is not None and volume_ratio_20 > 0:
                avg_vol_20d = volume_today / volume_ratio_20
                if avg_vol_20d < min_avg_volume_20d:
                    continue  # 20일 평균 거래량 미달 종목 제외
            filtered_by_vol.append(card)
        buy_cards = filtered_by_vol

    # 최대 포지션 수 제한: 기존 보유 수를 고려한 신규 허용 수
    max_new = max(0, max_positions - existing_count)
    buy_cards = buy_cards[:max_new]

    # 비중 할당 (단순 equal weight)
    if not buy_cards:
        audit_entries = [{
            "rule": "no_buy_candidates",
            "action": "skip",
            "reason": "매수 후보 없음 (buy/strong_buy 신호, confidence 미달, 또는 포지션 한도 도달)",
        }]
        return [], [], audit_entries

    max_exposure = 100.0 - min_cash
    available_exposure = max(0.0, max_exposure - existing_exposure)
    raw_weight = available_exposure / len(buy_cards) if buy_cards else 0

    position_risks = []
    approved_orders = []
    audit_entries = []

    # red regime → 신규 진입 금지
    if regime["label"] == "red":
        audit_entries.append({
            "rule": "regime_gate",
            "action": "block_all",
            "reason": "Regime RED — 신규 진입 전면 금지",
        })
        for card in buy_cards:
            position_risks.append({
                "ticker": card["ticker"],
                "risk_flag": "reject",
                "approved_weight": 0,
                "uncertainty_p85": None,
                "reason": "Regime RED — 신규 진입 금지",
                "reasons": ["Regime RED — 신규 진입 금지"],
            })
        return position_risks, [], audit_entries

    for card in buy_cards:
        ticker = card["ticker"]
        sector = ticker_sector.get(ticker, "기타")
        weight = min(raw_weight, max_single)

        # regime에 따른 신규 비중 조정
        weight = weight * new_entry_mult

        risk_flag = "pass"
        reason = ""
        risk_tags = []

        # confidence 체크
        if card["confidence"] < min_conf:
            risk_flag = "reject"
            reason = f"confidence {card['confidence']} < 최소 {min_conf}"
            risk_tags.append("low_confidence")
            audit_entries.append({"rule": "min_confidence", "ticker": ticker, "action": "reject", "reason": reason})
        else:
            # 섹터 비중 체크 (기존 보유 섹터 비중 포함)
            current_sector_w = sector_weights.get(sector, 0)
            if current_sector_w + weight > max_sector:
                old_weight = weight
                weight = max(0, max_sector - current_sector_w)
                if weight > 0:
                    risk_flag = "cap"
                    reason = f"섹터({sector}) 비중 제한: {old_weight:.1f}% → {weight:.1f}%"
                    risk_tags.append("sector_cap")
                    audit_entries.append({"rule": "sector_cap", "ticker": ticker, "action": "cap", "reason": reason})
                else:
                    risk_flag = "reject"
                    reason = f"섹터({sector}) 비중 한도 초과"
                    risk_tags.append("sector_cap")
                    audit_entries.append({"rule": "sector_cap", "ticker": ticker, "action": "reject", "reason": reason})

        if risk_flag != "reject" and weight > 0:
            sector_weights[sector] = sector_weights.get(sector, 0) + weight
            approved_orders.append({
                "ticker": ticker,
                "name": card.get("name", ticker),
                "action": "buy",
                "weight": round(weight, 2),
                "signal": card["signal"],
                "confidence": card["confidence"],
                "rationale": card["rationale"],
                "risk_flag": risk_flag,
                "risk_tags": risk_tags,
                "evidence_ids": card.get("evidence_ids", []),
                "sizing_rule": "equal_weight",
                "sell_reason": None,
            })

        _reason_str = reason if reason else f"정상 통과: confidence={card['confidence']}"
        position_risks.append({
            "ticker": ticker,
            "risk_flag": risk_flag,
            "approved_weight": round(weight, 2) if risk_flag != "reject" else 0,
            "uncertainty_p85": None,
            "reason": _reason_str,
            "reasons": [_reason_str] + risk_tags,
        })

    return position_risks, approved_orders, audit_entries


# ── UQ Tail Cap (P85) ────────────────────────────────────────
def apply_uq_tail_cap(approved_orders: list, policy: dict, audit_entries: list,
                      uq_scores: dict = None) -> list:
    """
    Tier 3: Uncertainty tail cap — P85 threshold 이상이면 비중 축소
    Phase 1에서는 enabled=false이므로 pass-through

    Args:
        approved_orders: 승인된 주문 목록
        policy: risk_policy_v0.yaml 내용
        audit_entries: audit log 누적 리스트 (in-place 추가)
        uq_scores: ticker → uncertainty_score 매핑 (position_risks 연결용)
    """
    uq_config = policy.get("position_constraints", {}).get("uq_tail_cap", {})
    if not uq_config.get("enabled", False):
        # UQ disabled — 종목별 "uq_disabled" 항목 기록
        for order in approved_orders:
            ticker = order["ticker"]
            uq = (uq_scores or {}).get(ticker) if uq_scores else order.get("uncertainty_p85")
            audit_entries.append({
                "rule": "uq_tail_cap",
                "ticker": ticker,
                "uncertainty_score": uq,
                "p85_threshold": uq_config.get("p85_threshold", 0.7),
                "exceeded": False,
                "original_weight": order.get("weight"),
                "capped_weight": None,
                "reduction_factor": None,
                "action": "uq_disabled",
            })
        audit_entries.append({
            "rule": "uq_tail_cap",
            "action": "skip",
            "reason": "UQ tail cap disabled (Phase 1)",
        })
        return approved_orders

    threshold = uq_config.get("p85_threshold", 0.7)
    reduction_factor = uq_config.get("reduction_factor", 0.5)

    capped = []
    for order in approved_orders:
        ticker = order["ticker"]
        uq = (uq_scores or {}).get(ticker) if uq_scores else order.get("uncertainty_p85")
        if uq is None:
            uq = order.get("uncertainty_p85")

        if uq is not None and uq >= threshold:
            original_weight = order["weight"]
            capped_weight = round(original_weight * reduction_factor, 2)
            order["weight"] = capped_weight
            order["risk_tags"] = order.get("risk_tags", []) + ["uq_tail_cap"]
            audit_entries.append({
                "rule": "uq_tail_cap",
                "ticker": ticker,
                "uncertainty_score": round(uq, 4),
                "p85_threshold": threshold,
                "exceeded": True,
                "original_weight": original_weight,
                "capped_weight": capped_weight,
                "reduction_factor": reduction_factor,
                "action": "cap_applied",
            })
        else:
            audit_entries.append({
                "rule": "uq_tail_cap",
                "ticker": ticker,
                "uncertainty_score": round(uq, 4) if uq is not None else None,
                "p85_threshold": threshold,
                "exceeded": False,
                "original_weight": order.get("weight"),
                "capped_weight": None,
                "reduction_factor": None,
                "action": "within_threshold",
            })
        capped.append(order)

    return capped


# ── Stop-Loss Check ──────────────────────────────────────────
def check_stop_loss(approved_orders: list, dmp: dict, policy: dict,
                    audit_entries: list, portfolio_state: dict = None) -> list:
    """
    Stop-loss 체크 (PortfolioState 기반 우선, fallback으로 return_5d)
    - PFS 있으면: 진입가 대비 현재가 손익률로 판정
    - PFS 없으면: DMP tech_features.return_5d로 판정 (legacy)
    """
    sl_config = policy.get("core_rules", {}).get("stop_loss", {})
    if not sl_config:
        return approved_orders

    threshold = sl_config.get("threshold", -5.0)
    market_data = dmp.get("market_data", {})

    # PFS에서 보유 포지션 정보 추출
    pfs_positions = {}
    if portfolio_state:
        for pos in portfolio_state.get("positions", []):
            pfs_positions[pos["ticker"]] = pos

    filtered = []
    for order in approved_orders:
        ticker = order["ticker"]
        pos = pfs_positions.get(ticker)

        if pos:
            # PFS 기반: 진입가 vs 현재가
            entry_price = pos["entry_price"]
            md = market_data.get(ticker, {})
            current_price = md.get("ohlcv", {}).get("close", pos.get("current_price", entry_price))
            pnl_pct = (current_price - entry_price) / entry_price * 100 if entry_price > 0 else 0

            if pnl_pct <= threshold:
                audit_entries.append({
                    "rule": "stop_loss",
                    "ticker": ticker,
                    "action": "reject",
                    "reason": f"[PFS] 진입가 {entry_price:,} → 현재가 {current_price:,} = {pnl_pct:.2f}% <= {threshold}%",
                })
                # order에 stop-loss 정보 기록 (RiskCard trace용)
                order["stop_loss_hit"] = True
                order["unrealized_pnl_pct"] = round(pnl_pct, 2)
                continue
        else:
            # Legacy fallback: return_5d 기반
            md = market_data.get(ticker, {})
            tech = md.get("tech_features", {})
            ret_5d = tech.get("return_5d")

            if ret_5d is not None and ret_5d <= threshold:
                audit_entries.append({
                    "rule": "stop_loss",
                    "ticker": ticker,
                    "action": "reject",
                    "reason": f"[legacy] 5일 수익률 {ret_5d:.2f}% <= stop-loss {threshold}%",
                })
                continue

        filtered.append(order)

    return filtered


# ── Hold/Sell Orders from PFS ─────────────────────────────────
def generate_hold_sell_orders(cards: list, dmp: dict, policy: dict,
                               portfolio_state: dict, audit_entries: list) -> list:
    """
    PFS 기존 보유 종목에 대해 hold/sell order 생성.
    - SC에 sell/strong_sell 신호 → action="sell", sell_reason="signal_sell"
    - stop-loss hit → action="sell", sell_reason="stop_loss"
    - 그 외 → action="hold"
    신규 buy 후보가 없어도 기존 보유 종목은 항상 COP에 포함됨.
    """
    if not portfolio_state:
        return []

    sl_config = policy.get("core_rules", {}).get("stop_loss", {})
    threshold = sl_config.get("threshold", -5.0)
    market_data = dmp.get("market_data", {})

    # SC를 ticker별로 인덱싱
    sc_map = {c["ticker"]: c for c in cards}

    hold_sell_orders = []
    for pos in portfolio_state.get("positions", []):
        ticker = pos["ticker"]
        sc = sc_map.get(ticker, {})
        signal = sc.get("signal", "hold")
        confidence = sc.get("confidence", 0.5)

        # 현재가 & unrealized PnL 계산
        entry_price = pos.get("entry_price", 0)
        md = market_data.get(ticker, {})
        current_price = md.get("ohlcv", {}).get("close", pos.get("current_price", entry_price))
        pnl_pct = (current_price - entry_price) / entry_price * 100 if entry_price > 0 else 0

        # stop-loss 체크 우선
        if pnl_pct <= threshold:
            action = "sell"
            sell_reason = "stop_loss"
            audit_entries.append({
                "rule": "stop_loss_sell",
                "ticker": ticker,
                "action": "sell",
                "reason": f"[PFS] 진입가 {entry_price:,} → 현재가 {current_price:,} = {pnl_pct:.2f}% <= {threshold}%",
            })
        elif signal in ["sell", "strong_sell"]:
            action = "sell"
            sell_reason = "signal_sell"
            audit_entries.append({
                "rule": "signal_sell",
                "ticker": ticker,
                "action": "sell",
                "reason": f"SC signal={signal} (confidence={confidence})",
            })
        else:
            action = "hold"
            sell_reason = None

        hold_sell_orders.append({
            "ticker": ticker,
            "name": pos.get("name", ticker),
            "action": action,
            "weight": pos.get("weight", 0),
            "signal": signal if signal else "hold",
            "confidence": confidence,
            "rationale": sc.get("rationale", "기존 보유 유지" if action == "hold" else f"매도: {sell_reason}"),
            "risk_flag": "pass",
            "risk_tags": [],
            "evidence_ids": sc.get("evidence_ids", []),
            "sizing_rule": None,
            "sell_reason": sell_reason,
        })

    return hold_sell_orders

# ── Turnover Cap ─────────────────────────────────────────────
def check_turnover(approved_orders: list, prev_orders: list, policy: dict,
                   audit_entries: list, portfolio_state: dict = None) -> list:
    """
    Turnover cap 체크 (PortfolioState 기반 우선, fallback으로 prev_orders)
    - PFS 있으면: 실제 포지션 weight delta로 계산
    - PFS 없으면: 전일 COP orders 비교 (legacy)
    """
    tc_config = policy.get("core_rules", {}).get("turnover_cap", {})
    if not tc_config:
        return approved_orders

    cap = tc_config.get("daily_max", 30.0)

    # PortfolioState 기반 turnover 계산
    # curr_weights: buy/hold orders의 비중 (sell은 0으로 처리)
    if portfolio_state and portfolio_state.get("positions"):
        prev_weights = {p["ticker"]: p["weight"] for p in portfolio_state["positions"]}
        curr_weights = {
            o["ticker"]: (o["weight"] if o.get("action") != "sell" else 0)
            for o in approved_orders
        }
        all_tickers = set(prev_weights) | set(curr_weights)
        total_change = sum(
            abs(curr_weights.get(t, 0) - prev_weights.get(t, 0))
            for t in all_tickers
        )
        turnover = round(total_change / 2, 2)  # 편도 기준
        new_entries = {o["ticker"] for o in approved_orders if o.get("action") == "buy"} - set(prev_weights)

        audit_entries.append({
            "rule": "turnover_calc",
            "action": "info",
            "reason": f"[PFS] 실제 포지션 delta 기반 turnover={turnover:.1f}% (cap={cap}%)",
        })
    elif prev_orders:
        # Legacy: 전일 COP 비교
        prev_tickers = set(o.get("ticker") for o in prev_orders)
        curr_tickers = set(o["ticker"] for o in approved_orders)
        new_entries = curr_tickers - prev_tickers
        total_weight = sum(o["weight"] for o in approved_orders)
        change_weight = sum(o["weight"] for o in approved_orders if o["ticker"] in new_entries)
        turnover = (change_weight / total_weight * 100) if total_weight > 0 else 0
    else:
        # 첫 거래일
        audit_entries.append({
            "rule": "turnover_cap",
            "action": "skip",
            "reason": "전일 포지션 없음 (첫 거래일) — turnover check 생략",
        })
        return approved_orders

    if turnover <= cap:
        return approved_orders

    # Hard cap: 신규 진입 종목을 confidence 낮은 순으로 제거
    kept = [o for o in approved_orders if o["ticker"] not in new_entries]
    new_orders = sorted(
        [o for o in approved_orders if o["ticker"] in new_entries],
        key=lambda x: x.get("confidence", 0), reverse=True,
    )

    total_all = sum(o["weight"] for o in kept) + sum(o["weight"] for o in new_orders)
    allowed_change = total_all * cap / 100 if total_all > 0 else 0
    added_weight = 0

    for order in new_orders:
        if added_weight + order["weight"] <= allowed_change:
            kept.append(order)
            added_weight += order["weight"]
        else:
            audit_entries.append({
                "rule": "turnover_cap",
                "ticker": order["ticker"],
                "action": "reject",
                "reason": f"turnover hard cap {cap}% 초과로 신규 진입 제거 (confidence={order.get('confidence', 'N/A')})",
            })

    removed = len(new_entries) - len([o for o in kept if o["ticker"] in new_entries])
    audit_entries.append({
        "rule": "turnover_cap",
        "action": "enforce",
        "reason": f"turnover {turnover:.1f}% > cap {cap}% → 신규 {removed}종목 제거",
    })

    return kept


# ── RiskAuditLog ─────────────────────────────────────────────
def save_audit_log(target_date: str, audit_entries: list, regime: dict,
                   backtest_summary: dict = None, snapshot_suffix: str = "180000"):
    """RiskAuditLog 저장"""
    log_dir = _BASE_DIR / "logs" / "risk_audit"
    log_dir.mkdir(parents=True, exist_ok=True)

    _bs = backtest_summary or {}
    log = {
        "audit_id": f"RAL-{target_date}-{snapshot_suffix}",
        "target_date": target_date,
        "generated_at": now_kst_iso(),
        "regime": regime["label"],
        "backtest_status": _bs.get("status"),
        "backtest_win_rate": _bs.get("win_rate_hint"),
        "backtest_failure_tags": _bs.get("failure_tags", []),
        "entries": audit_entries,
        "summary": {
            "total_rules_checked": len(audit_entries),
            "actions": {},
        }
    }

    # action별 카운트
    for entry in audit_entries:
        action = entry.get("action", "unknown")
        log["summary"]["actions"][action] = log["summary"]["actions"].get(action, 0) + 1

    path = log_dir / f"RAL-{target_date}-{snapshot_suffix}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)

    return path


def run_risk_engine(target_date: str, use_mock: bool = False, prev_cop_path: str = None,
                    portfolio_state: dict = None, dmp_override: dict = None,
                    backtest_summary: dict = None, disable_uq: bool = False,
                    dry_run: bool = False, snapshot_hour: int = 18):
    """메인: Risk Engine 실행 (dmp_override로 장중 merged DMP 전달 가능)
    disable_uq=True: YAML enabled 값과 무관하게 UQ tail cap 건너뜀 (ablation용)
    dry_run=True: RC/COP/AuditLog를 디스크에 저장하지 않고 메모리 객체만 반환 (ablation variant용)
    snapshot_hour: snapshot 기준 시각 (기본 18, intraday 호출 시 해당 시각 전달)
    """
    print(f"\n{'='*60}")
    print(f"  Risk Engine v3: {target_date}")
    print(f"{'='*60}\n")

    policy = load_policy()
    now_str = now_kst_iso()
    _hhmm = f"{snapshot_hour:02d}0000"
    snapshot_dt = make_snapshot_dt(target_date, snapshot_hour)

    # 1. DMP 로드 (dmp_override가 있으면 사용 — intraday merged DMP)
    dmp = dmp_override if (dmp_override and dmp_override.get("tickers")) else load_dmp(target_date)
    if not dmp:
        print("❌ DailyMarketPacket 없음. 먼저 build_daily_market_packet.py 실행")
        return None, None

    macro = dmp.get("macro_snapshot", {})
    print(f"[1/7] DMP 로드 완료 ({len(dmp['tickers'])}종목)")

    # 2. StrategyCard 로드
    if use_mock:
        print("[2/7] Mock StrategyCard 생성...")
        cards = generate_mock_strategy_cards(target_date, dmp)
    else:
        cards = load_strategy_cards(target_date)
        if not cards:
            print("[2/7] StrategyCard 없음 → Mock 생성으로 전환")
            cards = generate_mock_strategy_cards(target_date, dmp)

    print(f"  → {len(cards)}개 StrategyCard")

    # 3. Tier 1: Regime Gate
    print("[3/7] Tier 1: Regime Gate...")
    regime = determine_regime(macro, policy)
    tier1_pass = regime["label"] != "red"  # red이면 신규 진입 금지
    print(f"  → Regime: {regime['label']} ({regime['regime_reason']})")

    # 4. Tier 2: Position Constraints
    print("[4/7] Tier 2: Position Constraints...")
    position_risks, approved_orders, audit_entries = apply_position_constraints(cards, regime, policy, portfolio_state=portfolio_state, dmp=dmp)

    # 4.5. hold/sell orders 생성 (PFS 기반)
    hold_sell_orders = generate_hold_sell_orders(cards, dmp, policy, portfolio_state, audit_entries)
    buy_tickers_in_cop = {o["ticker"] for o in approved_orders}
    # hold/sell orders 중 신규 buy와 겹치지 않는 것만 추가
    for order in hold_sell_orders:
        if order["ticker"] not in buy_tickers_in_cop:
            approved_orders.append(order)
    print(f"  → hold/sell orders: {sum(1 for o in approved_orders if o.get('action') in ('hold','sell'))}건")

    # 5. Stop-Loss Check (buy orders에만 적용 — hold/sell은 generate_hold_sell_orders에서 처리)
    print("[5/7] Stop-Loss Check...")
    buy_orders = [o for o in approved_orders if o.get("action") == "buy"]
    non_buy_orders = [o for o in approved_orders if o.get("action") != "buy"]
    buy_orders = check_stop_loss(buy_orders, dmp, policy, audit_entries, portfolio_state)
    approved_orders = buy_orders + non_buy_orders

    # 5.5. UQ score 주입 (모델이 있으면)
    uq_scores = {}  # ticker → uncertainty_score (position_risks 연결용)
    try:
        from jobs.uq_calibration import predict_uncertainty
        for order in approved_orders:
            tech = dmp.get("market_data", {}).get(order["ticker"], {}).get("tech_features", {})
            feat = {
                "confidence": order.get("confidence", 0.0),
                "pre_risk_score": order.get("pre_risk_score", 0.0),
                "rsi_14": tech.get("rsi_14", 50),
                "volume_ratio_20": tech.get("volume_ratio_20", 1.0),
                "return_5d": tech.get("return_5d", 0),
            }
            pred = predict_uncertainty(feat, ticker=order["ticker"])
            uq_score = pred.get("uncertainty_score")
            if uq_score is not None:
                order["uncertainty_p85"] = uq_score
                uq_scores[order["ticker"]] = uq_score
    except Exception as e:
        print(f"[RiskEngine] UQ 모델 로드 실패 (skip): {e}")

    # 6. UQ Tail Cap (P85)
    print("[6/7] UQ Tail Cap...")
    uq_yaml_enabled = policy.get("position_constraints", {}).get("uq_tail_cap", {}).get("enabled", False)
    if disable_uq:
        uq_active = False
        for order in approved_orders:
            audit_entries.append({
                "rule": "uq_tail_cap",
                "ticker": order["ticker"],
                "action": "uq_disabled",
                "uncertainty_score": uq_scores.get(order["ticker"]),
                "note": "ablation: disable_uq=True",
            })
        audit_entries.append({
            "rule": "uq_tail_cap",
            "action": "skip",
            "reason": "UQ tail cap disabled (ablation override: disable_uq=True)",
            "uq_override": "disabled_by_param",
        })
        print(f"[RiskEngine] UQ tail cap: 비활성화 (ablation)")
    else:
        uq_active = uq_yaml_enabled
        print(f"[RiskEngine] UQ tail cap: {'활성화' if uq_active else '비활성화'}")
        approved_orders = apply_uq_tail_cap(approved_orders, policy, audit_entries, uq_scores)

    # 7. Turnover Check
    print("[7/7] Turnover Check...")
    prev_orders = []
    if prev_cop_path:
        try:
            with open(prev_cop_path) as f:
                prev_cop = json.load(f)
                prev_orders = prev_cop.get("orders", [])
        except Exception as e:
            print(f"[RiskEngine] prev_cop 로드 실패 (skip): {e}")
    approved_orders = check_turnover(approved_orders, prev_orders, policy, audit_entries, portfolio_state)

    # Regime action 가져오기
    actions = policy.get("regime_gate", {}).get("actions", {})
    regime_action = actions.get(regime["label"], {})

    # ── position_risks에 PFS 기반 정보 보강 ──
    pfs_positions = {}
    if portfolio_state:
        pfs_positions = {p["ticker"]: p for p in portfolio_state.get("positions", [])}

    # approved_orders 최종 비중 인덱싱 (UQ cap 적용 후)
    approved_weight_map = {o["ticker"]: o["weight"] for o in approved_orders}

    for pr in position_risks:
        ticker = pr["ticker"]
        pos = pfs_positions.get(ticker)
        if pos:
            pr["holding_days"] = pos.get("holding_days", 0)
            entry_price = pos.get("entry_price")
            pr["entry_price"] = entry_price
            # DMP 기준 현재가로 unrealized_pnl_pct 직접 계산 (PFS 캐시 값 사용 금지)
            current_price = dmp.get("market_data", {}).get(ticker, {}).get("ohlcv", {}).get("close")
            if entry_price and entry_price > 0 and current_price:
                pr["unrealized_pnl_pct"] = round((current_price - entry_price) / entry_price * 100, 2)
            else:
                pr["unrealized_pnl_pct"] = pos.get("unrealized_pnl_pct")
        else:
            pr["holding_days"] = 0
            pr["entry_price"] = None
            pr["unrealized_pnl_pct"] = None

        # ── uncertainty trace 보강 ──
        uq_score = uq_scores.get(ticker)
        pr["uncertainty_score"] = uq_score

        # UQ cap 적용 여부 판단: audit_entries에서 해당 ticker의 cap_applied 항목 확인
        uq_cap_entry = next(
            (e for e in audit_entries
             if e.get("rule") == "uq_tail_cap"
             and e.get("ticker") == ticker
             and e.get("action") == "cap_applied"),
            None,
        )
        if uq_cap_entry:
            pr["uq_cap_applied"] = True
            pr["weight_before_cap"] = uq_cap_entry.get("original_weight")
            pr["weight_after_cap"] = uq_cap_entry.get("capped_weight")
        else:
            pr["uq_cap_applied"] = False
            pr["weight_before_cap"] = None
            pr["weight_after_cap"] = None

    # ── RiskCard 생성 ──
    risk_card = {
        "risk_id": f"RC-{target_date}-{_hhmm}",
        "snapshot_dt": snapshot_dt,
        "generated_at": now_str,
        "artifact_version": "v1.0",
        "regime": regime,
        "tier1_pass": tier1_pass,
        "uq_enabled": uq_active,
        "portfolio_constraints": {
            "max_position_count": policy["position_constraints"]["max_position_count"],
            "max_single_weight": policy["position_constraints"]["max_single_weight"],
            "max_sector_weight": policy["position_constraints"]["max_sector_weight"],
            "max_total_exposure": regime_action.get("max_total_exposure", 100),
        },
        "position_risks": position_risks,
    }

    # ── CandidateOrderPlan 생성 ──
    # total_exposure: buy+hold 비중 합산 (sell은 청산 예정이므로 제외)
    active_orders = [o for o in approved_orders if o.get("action") in ("buy", "hold")]
    total_exposure = sum(o["weight"] for o in active_orders)
    cash_ratio = 100.0 - total_exposure

    # 섹터 breakdown (buy+hold 기준)
    uni = pd.read_csv(UNIVERSE_PATH)
    ticker_sector = {str(r["ticker"]).zfill(6): r["wics_sector"] for _, r in uni.iterrows()}
    sector_breakdown = {}
    for o in active_orders:
        sec = ticker_sector.get(o["ticker"], "기타")
        sector_breakdown[sec] = sector_breakdown.get(sec, 0) + o["weight"]

    cop = {
        "plan_id": f"COP-{target_date}-{_hhmm}",
        "snapshot_dt": snapshot_dt,
        "generated_at": now_str,
        "artifact_version": "v1.0",
        "regime": regime["label"],
        "execution_time": "t+1 open",
        "orders": approved_orders,
        "portfolio_summary": {
            "total_exposure": round(total_exposure, 2),
            "cash_ratio": round(cash_ratio, 2),
            "position_count": len(active_orders),
            "sector_breakdown": {k: round(v, 2) for k, v in sector_breakdown.items()},
        }
    }

    # LLM 리스크 내러티브 생성 (선택적 — 실패해도 RC 생성 영향 없음)
    llm_risk_analysis = None
    try:
        print("[LLM] 리스크 내러티브 생성 중...")
        rejected_tickers = [
            pr["ticker"] for pr in position_risks if pr.get("risk_flag") == "reject"
        ]
        capped_tickers = [
            pr["ticker"] for pr in position_risks if pr.get("risk_flag") == "cap"
        ]
        stop_loss_tickers = [
            e.get("ticker", "") for e in audit_entries
            if e.get("rule") == "stop_loss" and e.get("action") == "reject"
        ]
        rejected_str = ", ".join(rejected_tickers) if rejected_tickers else "없음"
        capped_str = ", ".join(capped_tickers) if capped_tickers else "없음"
        stop_loss_str = ", ".join(stop_loss_tickers) if stop_loss_tickers else "없음"

        prompt_messages = [
            {
                "role": "system",
                "content": "너는 한국 주식 포트폴리오 리스크 관리자야. 현재 리스크 상황을 분석해줘.",
            },
            {
                "role": "user",
                "content": (
                    "현재 시장 리스크 상태:\n"
                    f"- Regime: {regime['label']} "
                    f"(VIX proxy: {regime.get('vix_proxy', 'N/A')}, "
                    f"Market breadth: {regime.get('market_breadth', 'N/A')})\n"
                    f"- 포지션: {len(approved_orders)}종목, "
                    f"총 노출 {total_exposure:.1f}%\n"
                    f"- Stop-loss 해당: {stop_loss_str}\n"
                    f"- reject 종목: {rejected_str}\n"
                    f"- 비중 제한(cap) 종목: {capped_str}\n\n"
                    "리스크 관리자 관점에서 현재 상황을 분석하라.\n"
                    "반드시 아래 JSON 형식으로만 답하라:\n"
                    '{"risk_narrative": "리스크 분석 3~4문장 (한국어)", '
                    '"risk_level": "low" 또는 "moderate" 또는 "elevated" 또는 "high", '
                    '"attention_items": ["주의항목1", "주의항목2"]}'
                ),
            },
        ]

        llm_result = call_llm(prompt_messages, temperature=0.3, max_tokens=512)
        raw_content = llm_result.get("content", "")
        model_used = llm_result.get("model", "unknown")

        import re
        json_match = re.search(r'\{.*\}', raw_content, re.DOTALL)
        if json_match:
            parsed = json.loads(json_match.group())
            llm_risk_analysis = {
                "risk_narrative": parsed.get("risk_narrative", ""),
                "risk_level": parsed.get("risk_level", "moderate"),
                "attention_items": parsed.get("attention_items", []),
                "model_used": model_used,
            }
            valid_levels = ["low", "moderate", "elevated", "high"]
            if llm_risk_analysis["risk_level"] not in valid_levels:
                llm_risk_analysis["risk_level"] = "moderate"
            print(f"  → 리스크 내러티브 생성 완료 (level={llm_risk_analysis['risk_level']}, model={model_used})")
        else:
            print("  [WARN] LLM 응답 JSON 파싱 실패 → llm_risk_analysis=null")
    except Exception as e:
        print(f"  [RiskEngine] 리스크 내러티브 LLM 호출 실패 (skip): {e}")

    risk_card["llm_risk_analysis"] = llm_risk_analysis

    print(f"\n{'─'*40}")
    print(f"  Regime: {regime['label']}")
    _buy_cnt = sum(1 for o in approved_orders if o.get("action") == "buy")
    _hold_cnt = sum(1 for o in approved_orders if o.get("action") == "hold")
    _sell_cnt = sum(1 for o in approved_orders if o.get("action") == "sell")
    print(f"  buy: {_buy_cnt}종목, hold: {_hold_cnt}종목, sell: {_sell_cnt}종목")
    print(f"  거부 신규: {len(position_risks) - _buy_cnt}종목")
    print(f"  총 노출: {total_exposure:.1f}%")
    print(f"  현금: {cash_ratio:.1f}%")
    print(f"  섹터: {sector_breakdown}")
    print(f"{'─'*40}")

    if dry_run:
        # ablation variant 실행 — 원본 아티팩트 덮어쓰기 없이 메모리 객체만 반환
        print(f"[RiskEngine] dry_run=True → 저장 생략 (ablation variant)")
        return risk_card, cop

    # 저장
    rc_dir = _BASE_DIR / "artifacts" / "risk_card"
    cop_dir = _BASE_DIR / "artifacts" / "candidate_order_plan"
    rc_dir.mkdir(parents=True, exist_ok=True)
    cop_dir.mkdir(parents=True, exist_ok=True)

    rc_path = rc_dir / f"RC-{target_date}-{_hhmm}.json"
    cop_path = cop_dir / f"COP-{target_date}-{_hhmm}.json"

    with open(rc_path, "w", encoding="utf-8") as f:
        json.dump(risk_card, f, ensure_ascii=False, indent=2)
    with open(cop_path, "w", encoding="utf-8") as f:
        json.dump(cop, f, ensure_ascii=False, indent=2)

    # ── RiskAuditLog 저장 ──
    ral_path = save_audit_log(target_date, audit_entries, regime, backtest_summary, snapshot_suffix=_hhmm)

    print(f"\n✅ RiskCard: {rc_path}")
    print(f"✅ CandidateOrderPlan: {cop_path}")
    print(f"✅ RiskAuditLog: {ral_path} ({len(audit_entries)}건)")

    # Schema 검증
    from jsonschema import validate as jvalidate

    with open(_BASE_DIR / "schemas" / "risk_card.json") as f:
        rc_schema = json.load(f)
    with open(_BASE_DIR / "schemas" / "candidate_order_plan.json") as f:
        cop_schema = json.load(f)

    try:
        jvalidate(instance=risk_card, schema=rc_schema)
        print("✅ RiskCard schema PASS")
    except Exception as e:
        print(f"❌ RiskCard schema FAIL: {e.message}")

    try:
        jvalidate(instance=cop, schema=cop_schema)
        print("✅ CandidateOrderPlan schema PASS")
    except Exception as e:
        print(f"❌ COP schema FAIL: {e.message}")

    return risk_card, cop


if __name__ == "__main__":
    import argparse
    _parser = argparse.ArgumentParser()
    _parser.add_argument("date", nargs="?", default=now_kst().strftime("%Y%m%d"))
    _parser.add_argument("--mock", action="store_true", help="mock StrategyCard 강제 사용")
    _parser.add_argument("--no-uq", action="store_true", help="UQ tail cap 비활성화 (ablation용)")
    _args = _parser.parse_args()
    run_risk_engine(_args.date, use_mock=_args.mock, disable_uq=_args.no_uq)
