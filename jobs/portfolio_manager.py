"""
PortfolioState 관리 모듈
- 포지션 진입/청산 추적
- 진입가 기반 stop-loss 판정
- 실제 turnover 계산
- 미실현/실현 손익 추적

Usage:
    from jobs.portfolio_manager import PortfolioManager
    pm = PortfolioManager()
    state = pm.load_or_init(target_date)
    state = pm.apply_orders(state, cop, dmp)
    pm.save(state, target_date)
"""

import json
import sys
import yaml
from datetime import datetime
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE_DIR))

from connectors import make_snapshot_dt

PFS_DIR = _BASE_DIR / "artifacts" / "portfolio_state"
PFS_DIR.mkdir(parents=True, exist_ok=True)

_POLICY_PATH = _BASE_DIR / "config" / "risk_policy_v0.yaml"


class PortfolioManager:
    """포트폴리오 상태 관리자"""

    def __init__(self):
        """risk_policy_v0.yaml에서 stop_loss_threshold 로드"""
        try:
            with open(_POLICY_PATH, encoding="utf-8") as f:
                policy = yaml.safe_load(f)
            self.stop_loss_threshold = float(
                policy["core_rules"]["stop_loss"]["threshold"]
            )
        except Exception as e:
            print(f"[PortfolioManager] 정책 로드 실패, 기본값 -5.0 사용: {e}")
            self.stop_loss_threshold = -5.0

    def load_latest(self, before_date: str) -> dict | None:
        """before_date 이전의 가장 최근 PortfolioState 로드"""
        states = sorted(PFS_DIR.glob("PFS-*.json"))
        for p in reversed(states):
            date_str = p.stem.split("-")[1]  # PFS-YYYYMMDD-HHMMSS → YYYYMMDD
            if date_str < before_date:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
        return None

    def load(self, target_date: str) -> dict | None:
        """특정 날짜의 가장 최신 PortfolioState 로드"""
        files = sorted(PFS_DIR.glob(f"PFS-{target_date}-*.json"))
        if not files:
            return None
        # 가장 최신 파일 (HHMMSS가 큰 것)
        with open(files[-1], encoding="utf-8") as f:
            return json.load(f)

    def init_empty(self, target_date: str) -> dict:
        """빈 포트폴리오 상태 초기화"""
        from connectors import now_kst_iso
        snapshot_dt = make_snapshot_dt(target_date)
        return {
            "state_id": f"PFS-{target_date}-180000",
            "snapshot_dt": snapshot_dt,
            "generated_at": now_kst_iso(),
            "artifact_version": "v1.0",
            "positions": [],
            "cash_ratio": 100.0,
            "total_exposure": 0.0,
            "daily_turnover": 0.0,
            "realized_pnl": [],
        }

    def load_or_init(self, target_date: str) -> dict:
        """전일 상태 로드 → 없으면 빈 상태 초기화"""
        prev = self.load_latest(target_date)
        if prev:
            # 전일 포지션을 오늘로 이월
            return self._carry_forward(prev, target_date)
        return self.init_empty(target_date)

    def _carry_forward(self, prev_state: dict, target_date: str) -> dict:
        """전일 포지션을 오늘로 이월 (holding_days +1, turnover 리셋)"""
        from connectors import now_kst_iso
        snapshot_dt = make_snapshot_dt(target_date)
        positions = []
        for pos in prev_state.get("positions", []):
            carried = {**pos}
            carried["holding_days"] = pos.get("holding_days", 0) + 1
            carried["stop_loss_hit"] = False  # 오늘 기준 재판정
            positions.append(carried)

        return {
            "state_id": f"PFS-{target_date}-180000",
            "snapshot_dt": snapshot_dt,
            "generated_at": now_kst_iso(),
            "artifact_version": "v1.0",
            "positions": positions,
            "cash_ratio": prev_state.get("cash_ratio", 100.0),
            "total_exposure": prev_state.get("total_exposure", 0.0),
            "daily_turnover": 0.0,
            "realized_pnl": [],
        }

    def apply_decisions(self, state: dict, fdc: dict, cop: dict, dmp: dict,
                        stop_loss_threshold: float = None) -> dict:
        """
        FinalDecisionCard 기반으로 포지션 업데이트.
        stop-loss는 RiskEngine → COP sell order → FDC에서 명시적으로 처리됨.
        PortfolioManager는 FDC에서 approve된 sell order만 실행한다.

        1. 기존 포지션 현재가 업데이트
        2. FDC approve된 sell order → 청산
        3. FDC approve된 buy order → 신규 진입
        4. turnover 계산
        """
        market_data = dmp.get("market_data", {})
        decisions = {d["ticker"]: d for d in fdc.get("decisions", [])}
        orders = {o["ticker"]: o for o in cop.get("orders", [])}

        new_positions = []
        realized = []
        entry_weight_sum = 0
        exit_weight_sum = 0

        # ── 기존 포지션 처리 ──
        for pos in state.get("positions", []):
            ticker = pos["ticker"]
            md = market_data.get(ticker, {})
            ohlcv = md.get("ohlcv", {})
            current_price = ohlcv.get("close", pos["current_price"])

            # 미실현 손익 업데이트
            entry_price = pos["entry_price"]
            pnl_pct = round((current_price - entry_price) / entry_price * 100, 2) if entry_price > 0 else 0

            # FDC approve된 sell order → 청산
            # sell은 COP에서 명시적으로 생성된 것만 처리 (stop_loss, signal_sell 모두 포함)
            decision = decisions.get(ticker, {})
            cop_order = orders.get(ticker, {})
            is_approved_sell = (
                decision.get("decision") == "approve"
                and decision.get("action") == "sell"
            )
            is_vetoed = decision.get("decision") == "veto"

            if is_approved_sell:
                sell_reason = cop_order.get("sell_reason") or decision.get("veto_reason") or "sell signal"
                # 청산 사유를 분류하여 audit trail 강화
                is_stop_loss = (sell_reason == "stop_loss") or (
                    "stop_loss" in str(cop_order.get("sell_reason", "")).lower()
                )
                exit_date = state.get("snapshot_dt", "")[:10]
                if is_stop_loss:
                    print(
                        f"[PortfolioManager] {ticker} 스톱로스 청산: "
                        f"pnl={pnl_pct:.2f}% (임계값={self.stop_loss_threshold}%), "
                        f"진입가={entry_price}, 현재가={current_price}, 날짜={exit_date}"
                    )
                else:
                    print(
                        f"[PortfolioManager] {ticker} 청산 (사유={sell_reason}): "
                        f"pnl={pnl_pct:.2f}%, 진입가={entry_price}, 현재가={current_price}, 날짜={exit_date}"
                    )
                realized.append({
                    "ticker": ticker,
                    "entry_price": entry_price,
                    "exit_price": current_price,
                    "pnl_pct": pnl_pct,
                    "reason": sell_reason,
                    "exit_reason": "stop_loss" if is_stop_loss else sell_reason,
                    "exit_date": exit_date,
                })
                exit_weight_sum += pos["weight"]
                continue

            if is_vetoed:
                # veto된 buy order가 있는 경우에만 청산 (기존 보유 종목 veto는 hold 유지)
                if decision.get("action") == "buy":
                    # buy veto → 신규 진입 안 함 (이미 보유 중이면 유지)
                    pass
                # veto된 sell order → 청산 안 함 (hold 유지)

            # 유지
            pos_updated = {**pos}
            pos_updated["current_price"] = current_price
            pos_updated["unrealized_pnl_pct"] = pnl_pct
            pos_updated["stop_loss_hit"] = False
            new_positions.append(pos_updated)

        # ── 신규 매수 반영 (approve된 buy만) ──
        existing_tickers = {p["ticker"] for p in new_positions}
        for d in fdc.get("decisions", []):
            if d["decision"] != "approve" or d["action"] != "buy":
                continue
            ticker = d["ticker"]

            if ticker in existing_tickers:
                # 이미 보유 중인 종목: FDC 승인 weight를 목표 비중으로 설정 (리밸런싱)
                for pos in new_positions:
                    if pos["ticker"] == ticker:
                        old_weight = pos["weight"]
                        pos["weight"] = d["weight"]
                        entry_weight_sum += abs(d["weight"] - old_weight)
                        break
                continue

            order = orders.get(ticker, {})
            md = market_data.get(ticker, {})
            ohlcv = md.get("ohlcv", {})
            entry_price = ohlcv.get("close", 0)

            new_positions.append({
                "ticker": ticker,
                "name": d.get("name", ticker),
                "entry_date": dmp.get("snapshot_dt", "")[:10].replace("-", ""),
                "entry_price": entry_price,
                "current_price": entry_price,
                "weight": d["weight"],
                "unrealized_pnl_pct": 0.0,
                "holding_days": 0,
                "stop_loss_hit": False,
            })
            entry_weight_sum += d["weight"]

        # ── 포트폴리오 요약 ──
        total_exposure = round(sum(p["weight"] for p in new_positions), 2)
        cash_ratio = round(100.0 - total_exposure, 2)
        daily_turnover = round((entry_weight_sum + exit_weight_sum) / 2, 2)  # 편도 기준

        state["positions"] = new_positions
        state["cash_ratio"] = cash_ratio
        state["total_exposure"] = total_exposure
        state["daily_turnover"] = daily_turnover
        state["realized_pnl"] = realized

        return state

    def save(self, state: dict, target_date: str) -> Path:
        """PortfolioState 저장"""
        from connectors import now_kst_iso
        state["generated_at"] = now_kst_iso()
        path = PFS_DIR / f"PFS-{target_date}-180000.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        return path

    def check_stop_loss_from_state(self, state: dict, threshold: float = None) -> list:
        """PortfolioState 기반 stop-loss 대상 종목 반환"""
        if threshold is None:
            threshold = self.stop_loss_threshold

        hits = []
        for pos in state.get("positions", []):
            if pos.get("unrealized_pnl_pct", 0) <= threshold:
                hits.append({
                    "ticker": pos["ticker"],
                    "pnl_pct": pos["unrealized_pnl_pct"],
                    "entry_price": pos["entry_price"],
                    "current_price": pos["current_price"],
                })
        return hits

    def calc_real_turnover(self, prev_state: dict, curr_state: dict) -> float:
        """전일 대비 실제 포지션 변화량 기반 turnover 계산"""
        prev_tickers = {p["ticker"]: p["weight"] for p in prev_state.get("positions", [])}
        curr_tickers = {p["ticker"]: p["weight"] for p in curr_state.get("positions", [])}

        all_tickers = set(prev_tickers) | set(curr_tickers)
        total_change = sum(
            abs(curr_tickers.get(t, 0) - prev_tickers.get(t, 0))
            for t in all_tickers
        )
        return round(total_change / 2, 2)  # 편도 기준
