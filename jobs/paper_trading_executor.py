"""
Paper Trading Executor — 모의 투자 주문 실행기

- FinalDecisionCard의 approve된 주문을 모의 실행
- dry_run_only (기본 True): 실제 주문 없이 로그만 생성
- max_order_notional: 1회 최대 주문 금액 제한
- order journal: 중복 주문 방지 (idempotency)
- KIS 모의투자 API 연동 준비 (Phase 2+)

Usage:
    python jobs/paper_trading_executor.py 20260324
    python jobs/paper_trading_executor.py 20260324 --live  # dry_run 해제 (주의)
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE_DIR))

FDC_DIR = _BASE_DIR / "artifacts" / "final_decision_card"
DMP_DIR = _BASE_DIR / "artifacts" / "daily_market_packet"
PFS_DIR = _BASE_DIR / "artifacts" / "portfolio_state"
PTL_DIR = _BASE_DIR / "artifacts" / "paper_trading_log"
PTL_DIR.mkdir(parents=True, exist_ok=True)

RECONCILE_DIR = _BASE_DIR / "artifacts" / "broker_reconcile_report"
RECONCILE_DIR.mkdir(parents=True, exist_ok=True)

KST = timezone(timedelta(hours=9))


class KISClient:
    """KIS 모의투자 API 클라이언트 (Phase 2 연동 대비 stub)"""

    def __init__(self, app_key: str = "", app_secret: str = "", is_paper: bool = True):
        self.app_key = app_key or os.getenv("KIS_APP_KEY", "")
        self.app_secret = app_secret or os.getenv("KIS_APP_SECRET", "")
        self.account_no = os.getenv("KIS_ACCOUNT_NO", "")
        self.is_paper = is_paper if is_paper else (os.getenv("KIS_IS_PAPER", "true").lower() == "true")
        self._connected = False

    def connect(self):
        """API 연결 (현재는 stub)"""
        print("[PaperTrading] KIS API 미연결, stub 모드로 동작")
        self._connected = False

    def place_order(self, ticker: str, action: str, quantity: int, price: float) -> dict:
        """주문 실행 (현재는 stub — 항상 성공 반환)"""
        return {"status": "success", "order_id": f"KIS-{ticker}", "message": "stub"}

    def cancel_order(self, order_id: str) -> dict:
        """주문 취소 (현재는 stub — 항상 성공 반환)"""
        return {"status": "success", "order_id": order_id, "message": "cancel stub"}

    def get_balance(self) -> dict:
        """잔고 조회 (stub)"""
        return {"cash": 0, "positions": []}

    def get_executions(self, date: str) -> list:
        """체결 조회 (stub)"""
        return []


def _load_fdc(date: str) -> dict | None:
    """FDC-{date}.json 로드"""
    path = FDC_DIR / f"FDC-{date}.json"
    if not path.exists():
        # -HHMMSS 형태로도 시도
        candidates = sorted(FDC_DIR.glob(f"FDC-{date}-*.json"))
        if not candidates:
            print(f"[PaperTrading] FDC 파일 없음: {path}")
            return None
        path = candidates[-1]
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_dmp(date: str) -> dict | None:
    """DMP-{date}.json 로드"""
    path = DMP_DIR / f"DMP-{date}.json"
    if not path.exists():
        candidates = sorted(DMP_DIR.glob(f"DMP-{date}-*.json"))
        if not candidates:
            print(f"[PaperTrading] DMP 파일 없음: {path}")
            return None
        path = candidates[-1]
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_pfs(date: str) -> dict | None:
    """PFS-{date}-*.json 로드 (가장 최신)"""
    candidates = sorted(PFS_DIR.glob(f"PFS-{date}-*.json"))
    if not candidates:
        return None
    with open(candidates[-1], encoding="utf-8") as f:
        return json.load(f)


def _load_existing_ptl(date: str) -> dict | None:
    """기존 PTL 로그 로드 (idempotency 확인용)"""
    path = PTL_DIR / f"PTL-{date}.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _already_executed(existing_ptl: dict | None, ticker: str) -> bool:
    """같은 날짜+종목 조합이 이미 실행됐는지 확인"""
    if existing_ptl is None:
        return False
    for order in existing_ptl.get("orders", []):
        if order.get("ticker") == ticker and order.get("status") in ("executed", "dry_run"):
            return True
    return False


def execute_paper_trades(
    date: str,
    dry_run: bool = True,
    max_order_notional: float = 50_000_000,
    total_portfolio_value: float = 100_000_000,
    cancel_tickers: list | None = None,
) -> dict:
    """
    FDC의 approve된 주문을 모의 실행하고 PTL 저장.

    Args:
        date: YYYYMMDD 형식 날짜
        dry_run: True면 실제 주문 없이 로그만 생성
        max_order_notional: 1회 최대 주문 금액 (원)
        total_portfolio_value: 가상 총자산 (원)
        cancel_tickers: 취소할 종목코드 리스트 (None이면 취소 없음)

    Returns:
        생성된 PaperTradingLog dict
    """
    print(f"[PaperTrading] 실행 시작: {date}, dry_run={dry_run}")

    fdc = _load_fdc(date)
    if fdc is None:
        raise FileNotFoundError(f"[PaperTrading] FDC 없음: {date}")

    dmp = _load_dmp(date)
    if dmp is None:
        raise FileNotFoundError(f"[PaperTrading] DMP 없음: {date}")

    existing_ptl = _load_existing_ptl(date)
    market_data = dmp.get("market_data", {})
    fdc_id = fdc.get("decision_id", f"FDC-{date}-180000")

    kis = KISClient(is_paper=True)
    orders = []

    # cancel_tickers 처리
    if cancel_tickers:
        for raw_ticker in cancel_tickers:
            ticker = str(raw_ticker).zfill(6)
            order_id = f"ORD-{date}-{ticker}-CANCEL"
            print(f"[PaperTrading] 주문 취소 요청: {ticker}")
            result = kis.cancel_order(order_id)
            status = "cancelled" if result.get("status") == "success" else "cancel_failed"
            orders.append({
                "order_id": order_id,
                "ticker": ticker,
                "name": ticker,
                "action": "cancel",
                "weight": 0.0,
                "quantity": 0,
                "price": 0.0,
                "notional": 0.0,
                "status": status,
                "reject_reason": None,
                "timestamp": datetime.now(KST).isoformat(),
            })

    for decision in fdc.get("decisions", []):
        if decision.get("decision") != "approve":
            continue

        ticker = str(decision.get("ticker", "")).zfill(6)
        name = decision.get("name", ticker)
        action = decision.get("action", "buy")
        weight = float(decision.get("weight", 0.0))

        # 이미 실행된 주문 skip (idempotency)
        if _already_executed(existing_ptl, ticker):
            print(f"[PaperTrading] 중복 주문 skip: {ticker} ({name})")
            orders.append({
                "order_id": f"ORD-{date}-{ticker}",
                "ticker": ticker,
                "name": name,
                "action": action,
                "weight": weight,
                "quantity": 0,
                "price": 0.0,
                "notional": 0.0,
                "status": "rejected",
                "reject_reason": "already_executed",
                "timestamp": datetime.now(KST).isoformat(),
            })
            continue

        # 현재가 조회
        md = market_data.get(ticker, {})
        close_price = float(md.get("ohlcv", {}).get("close", 0))

        if close_price <= 0:
            print(f"[PaperTrading] 현재가 없음, skip: {ticker}")
            orders.append({
                "order_id": f"ORD-{date}-{ticker}",
                "ticker": ticker,
                "name": name,
                "action": action,
                "weight": weight,
                "quantity": 0,
                "price": 0.0,
                "notional": 0.0,
                "status": "rejected",
                "reject_reason": "price_unavailable",
                "timestamp": datetime.now(KST).isoformat(),
            })
            continue

        # 주문 수량 계산 (총자산 × weight% / 현재가)
        target_value = total_portfolio_value * (weight / 100.0)
        quantity = int(target_value // close_price)
        notional = round(quantity * close_price, 2)

        # max_order_notional 초과 시 reject
        if notional > max_order_notional:
            print(f"[PaperTrading] 최대 주문금액 초과, reject: {ticker} notional={notional:,.0f}원")
            orders.append({
                "order_id": f"ORD-{date}-{ticker}",
                "ticker": ticker,
                "name": name,
                "action": action,
                "weight": weight,
                "quantity": quantity,
                "price": close_price,
                "notional": notional,
                "status": "rejected",
                "reject_reason": "max_notional_exceeded",
                "timestamp": datetime.now(KST).isoformat(),
            })
            continue

        # 주문 실행
        if dry_run:
            status = "dry_run"
            print(f"[PaperTrading] DRY_RUN: {action.upper()} {ticker}({name}) "
                  f"{quantity}주 @ {close_price:,.0f}원 = {notional:,.0f}원")
        else:
            result = kis.place_order(ticker, action, quantity, close_price)
            status = "executed" if result.get("status") == "success" else "rejected"
            print(f"[PaperTrading] 주문 {status}: {action.upper()} {ticker}({name}) "
                  f"{quantity}주 @ {close_price:,.0f}원 = {notional:,.0f}원")

        orders.append({
            "order_id": f"ORD-{date}-{ticker}",
            "ticker": ticker,
            "name": name,
            "action": action,
            "weight": weight,
            "quantity": quantity,
            "price": close_price,
            "notional": notional,
            "status": status,
            "reject_reason": None,
            "timestamp": datetime.now(KST).isoformat(),
        })

    # 요약 통계
    executed_count = sum(1 for o in orders if o["status"] in ("executed", "dry_run"))
    rejected_count = sum(1 for o in orders if o["status"] == "rejected")
    cancelled_count = sum(1 for o in orders if o["status"] == "cancelled")
    total_notional = sum(o["notional"] for o in orders if o["status"] in ("executed", "dry_run"))
    total_orders = len(orders)
    execution_rate = round(executed_count / total_orders, 4) if total_orders > 0 else 0.0

    ptl = {
        "log_id": f"PTL-{date}",
        "target_date": date,
        "generated_at": datetime.now(KST).isoformat(),
        "dry_run": dry_run,
        "portfolio_value": total_portfolio_value,
        "max_order_notional": max_order_notional,
        "fdc_id": fdc_id,
        "orders": orders,
        "summary": {
            "total_orders": total_orders,
            "executed_count": executed_count,
            "rejected_count": rejected_count,
            "cancelled_count": cancelled_count,
            "total_notional": total_notional,
            "execution_rate": execution_rate,
        },
    }

    # PTL 저장
    ptl_path = PTL_DIR / f"PTL-{date}.json"
    with open(ptl_path, "w", encoding="utf-8") as f:
        json.dump(ptl, f, ensure_ascii=False, indent=2)
    print(f"[PaperTrading] PTL 저장 완료: {ptl_path}")

    # Reconciliation 실행
    _run_reconciliation(date, ptl)

    return ptl


def _run_reconciliation(date: str, ptl: dict) -> dict:
    """
    PFS ↔ PTL 대조 리포트 생성.

    Args:
        date: YYYYMMDD 형식 날짜
        ptl: 방금 생성한 PaperTradingLog dict

    Returns:
        생성된 BrokerReconcileReport dict
    """
    pfs = _load_pfs(date)
    mismatches = []

    if pfs is None:
        print(f"[PaperTrading] PFS 없음, reconciliation 건너뜀: {date}")
        status = "error"
        pfs_positions = 0
        summary = "PFS 파일 없음"
    else:
        pfs_positions = len(pfs.get("positions", []))
        ptl_orders = ptl.get("summary", {}).get("executed_count", 0)

        # PFS 포지션 ticker 집합
        pfs_tickers = {str(p["ticker"]).zfill(6): p for p in pfs.get("positions", [])}
        # PTL 실행된 주문 ticker 집합
        ptl_executed = {
            str(o["ticker"]).zfill(6): o
            for o in ptl.get("orders", [])
            if o["status"] in ("executed", "dry_run")
        }

        # PTL에 있지만 PFS에 없는 종목
        for ticker, order in ptl_executed.items():
            if ticker not in pfs_tickers:
                mismatches.append({
                    "ticker": ticker,
                    "field": "position_existence",
                    "pfs_value": None,
                    "ptl_value": order["status"],
                    "description": f"{ticker}({order.get('name', '')}) PTL 실행됐으나 PFS에 포지션 없음",
                })

        # PFS에 있지만 PTL에 없는 종목 (단순 정보 — 기존 보유 가능)
        for ticker, pos in pfs_tickers.items():
            if ticker not in ptl_executed:
                mismatches.append({
                    "ticker": ticker,
                    "field": "order_existence",
                    "pfs_value": pos.get("weight"),
                    "ptl_value": None,
                    "description": f"{ticker}({pos.get('name', '')}) PFS 포지션이나 PTL 주문 없음 (기존 보유 가능)",
                })

        # 공통 종목 비중 비교
        for ticker in set(pfs_tickers) & set(ptl_executed):
            pfs_weight = pfs_tickers[ticker].get("weight", 0)
            ptl_weight = ptl_executed[ticker].get("weight", 0)
            if abs(pfs_weight - ptl_weight) > 0.01:
                mismatches.append({
                    "ticker": ticker,
                    "field": "weight",
                    "pfs_value": pfs_weight,
                    "ptl_value": ptl_weight,
                    "description": f"{ticker} 비중 불일치: PFS={pfs_weight}%, PTL={ptl_weight}%",
                })

        status = "mismatch" if mismatches else "pass"
        summary = (
            f"총 {len(mismatches)}건 불일치" if mismatches
            else "PFS ↔ PTL 정합성 이상 없음"
        )

    report = {
        "report_id": f"BRR-{date}",
        "target_date": date,
        "generated_at": datetime.now(KST).isoformat(),
        "status": status,
        "pfs_positions": pfs_positions if pfs else 0,
        "ptl_orders": ptl.get("summary", {}).get("total_orders", 0),
        "mismatches": mismatches,
        "summary": summary,
    }

    report_path = RECONCILE_DIR / f"BRR-{date}.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[PaperTrading] Reconciliation 완료: {report_path} — {status} ({summary})")

    return report


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Paper Trading Executor")
    parser.add_argument(
        "date",
        nargs="?",
        default=datetime.now(KST).strftime("%Y%m%d"),
        help="실행 날짜 (YYYYMMDD)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="dry_run 해제 (실제 주문 시도)",
    )
    parser.add_argument(
        "--max-notional",
        type=float,
        default=50_000_000,
        help="1회 최대 주문금액 (기본: 5천만원)",
    )
    parser.add_argument(
        "--portfolio-value",
        type=float,
        default=100_000_000,
        help="가상 총자산 (기본: 1억원)",
    )
    parser.add_argument(
        "--cancel",
        nargs="*",
        default=None,
        help="취소할 종목코드 (e.g., --cancel 005930 000660)",
    )
    args = parser.parse_args()

    execute_paper_trades(
        date=args.date,
        dry_run=not args.live,
        max_order_notional=args.max_notional,
        total_portfolio_value=args.portfolio_value,
        cancel_tickers=args.cancel,
    )
