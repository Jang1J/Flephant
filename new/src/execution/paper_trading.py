"""S4-6 paper trading readiness runner.

목표는 실계좌 전환 직전 단계까지 안전하게 닫는 것이다.
기본 동작은 read-only 잔고/정합성 확인이며, 모의 주문은 명시 확인 문구가
있을 때만 ExecutionGateway(paper) 경로로 제출한다.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.connectors.kis_rest import KISRestClient
from src.execution.execution_gateway import ExecutionGateway
from src.execution.kill_switch import KillSwitch
from src.ops.audit_logger import AuditLogger
from src.ops.safety_guards import SafetyGuards
from src.utils.config_loader import load as config_load
from src.utils.id_factory import generate_decision_id
from src.utils.logger import get_logger
from src.utils.ticker_utils import pad_ticker

logger = get_logger("paper_trading")

_KST = ZoneInfo("Asia/Seoul")
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


class PaperTradingError(RuntimeError):
    """paper trading readiness 실패."""


class PaperTradingRunner:
    """잔고조회, 주문 smoke, 체결조회, reconciliation을 하나의 리포트로 묶는다."""

    def __init__(
        self,
        kis_client: Any | None = None,
        report_dir: Path | str | None = None,
        audit_logger: AuditLogger | None = None,
        kill_switch: KillSwitch | None = None,
    ) -> None:
        cfg = config_load("risk_config.yaml", "paper_trading")
        default_report_dir = Path(str(cfg["report_dir"]))
        if not default_report_dir.is_absolute():
            default_report_dir = _PROJECT_ROOT / default_report_dir

        self._kis_client = kis_client or KISRestClient()
        self._report_dir = Path(report_dir) if report_dir else default_report_dir
        self._report_dir.mkdir(parents=True, exist_ok=True)
        self._audit_logger = audit_logger
        self._kill_switch = kill_switch or KillSwitch()
        self._confirm_phrase = str(cfg["confirm_order_phrase"])
        self._require_virtual_mode = bool(cfg["require_virtual_mode"])
        self._max_probe_order_qty = int(cfg["max_probe_order_qty"])
        self._allow_market_order = bool(cfg["allow_market_order"])

    @property
    def confirm_phrase(self) -> str:
        return self._confirm_phrase

    def run_balance_reconciliation(
        self,
        system_positions: list[dict[str, Any]] | None = None,
        write_report: bool = True,
    ) -> dict[str, Any]:
        """read-only balance + optional system/KIS reconciliation."""
        report = self._base_report("balance_reconciliation")
        mode_check = self._paper_mode_check()
        report["stages"]["mode_guard"] = mode_check
        if mode_check["status"] != "PASS":
            report["status"] = "FAIL"
            return self._finish_report(report, write_report)

        try:
            balance = self._kis_client.get_balance()
            positions = list(balance.get("positions", []))
            report["stages"]["balance"] = {
                "status": "PASS",
                "balance": balance.get("balance"),
                "position_count": len(positions),
                "positions": positions,
                "_mode": balance.get("_mode", self._client_mode()),
            }

            if system_positions is None:
                report["stages"]["reconciliation"] = {
                    "status": "SKIP",
                    "reason": "system_positions_not_provided",
                }
            else:
                recon = SafetyGuards().reconcile_positions(system_positions, positions)
                report["stages"]["reconciliation"] = {
                    "status": "PASS" if recon["ok"] else "FAIL",
                    **recon,
                }

            report["status"] = self._overall_status(report)
        except Exception as e:
            report["status"] = "FAIL"
            report["stages"]["balance"] = {
                "status": "FAIL",
                "error": str(e),
            }

        return self._finish_report(report, write_report)

    def submit_probe_order(
        self,
        ticker: str,
        side: str,
        qty: int,
        price: float | int | None,
        order_type: str = "00",
        confirm_phrase: str | None = None,
        write_report: bool = True,
    ) -> dict[str, Any]:
        """명시 확인 후 KIS 모의투자 서버에 1건 주문을 제출한다.

        확인 문구가 없으면 주문은 나가지 않고 SKIP 리포트를 남긴다.
        """
        report = self._base_report("submit_probe_order")
        mode_check = self._paper_mode_check()
        report["stages"]["mode_guard"] = mode_check
        if mode_check["status"] != "PASS":
            report["status"] = "FAIL"
            return self._finish_report(report, write_report)

        guard = self._order_guard(
            ticker=ticker,
            side=side,
            qty=qty,
            price=price,
            order_type=order_type,
            confirm_phrase=confirm_phrase,
        )
        report["stages"]["order_guard"] = guard
        if guard["status"] != "PASS":
            report["status"] = "SKIP" if guard.get("safe_skip") else "FAIL"
            return self._finish_report(report, write_report)

        try:
            report["stages"]["balance_before"] = self._read_balance_stage()
            fd = self._probe_final_decision(
                ticker=ticker,
                side=side,
                qty=qty,
                price=price,
                order_type=order_type,
            )
            gateway = ExecutionGateway(
                kill_switch=self._kill_switch,
                audit_logger=self._audit_logger,
                kis_client=self._kis_client,
                mode_override="paper",
                live_enabled_override=False,
            )
            result = gateway.execute(fd)
            execution_report = result["execution_report"]
            broker_order_ids = self._broker_order_ids(execution_report)
            report["stages"]["execution"] = {
                "status": (
                    "PASS"
                    if execution_report["status"] in {"submitted", "filled", "partial_filled"}
                    else "FAIL"
                ),
                "result": result,
            }
            report["stages"]["order_history"] = self._read_order_history_stage(
                ticker=ticker,
                side=side,
                order_id=broker_order_ids[0] if broker_order_ids else None,
                execution_filter="all",
            )
            report["stages"]["balance_after"] = self._read_balance_stage()
            report["status"] = self._overall_status(report)
        except Exception as e:
            report["status"] = "FAIL"
            report["stages"]["execution"] = {
                "status": "FAIL",
                "error": str(e),
            }

        return self._finish_report(report, write_report)

    def run_order_history(
        self,
        ticker: str = "",
        side: str = "all",
        order_id: str | None = None,
        execution_filter: str = "all",
        write_report: bool = True,
    ) -> dict[str, Any]:
        """기존 broker order id를 read-only로 재조회한다."""
        report = self._base_report("order_history")
        mode_check = self._paper_mode_check()
        report["stages"]["mode_guard"] = mode_check
        if mode_check["status"] != "PASS":
            report["status"] = "FAIL"
            return self._finish_report(report, write_report)

        report["stages"]["order_history"] = self._read_order_history_stage(
            ticker=ticker,
            side=side,
            order_id=order_id,
            execution_filter=execution_filter,
        )
        report["status"] = self._overall_status(report)
        return self._finish_report(report, write_report)

    def _paper_mode_check(self) -> dict[str, Any]:
        mode = self._client_mode()
        if self._require_virtual_mode and mode != "virtual":
            return {
                "status": "FAIL",
                "error_code": "PAPER_MODE_REQUIRED",
                "message": "S4-6 paper trading requires KIS_MODE=virtual.",
                "current_mode": mode,
            }
        return {"status": "PASS", "current_mode": mode}

    def _order_guard(
        self,
        ticker: str,
        side: str,
        qty: int,
        price: float | int | None,
        order_type: str,
        confirm_phrase: str | None,
    ) -> dict[str, Any]:
        if confirm_phrase != self._confirm_phrase:
            return {
                "status": "SKIP",
                "safe_skip": True,
                "reason": "confirm_phrase_missing_or_mismatch",
                "required_phrase": self._confirm_phrase,
            }
        side_norm = str(side).lower()
        if side_norm not in {"buy", "sell"}:
            return {"status": "FAIL", "reason": "side_must_be_buy_or_sell"}
        qty_int = int(qty)
        if qty_int <= 0 or qty_int > self._max_probe_order_qty:
            return {
                "status": "FAIL",
                "reason": "qty_out_of_probe_limit",
                "qty": qty_int,
                "max_probe_order_qty": self._max_probe_order_qty,
            }
        order_type_norm = str(order_type or "00")
        if order_type_norm == "01" and not self._allow_market_order:
            return {"status": "FAIL", "reason": "market_order_not_allowed"}
        if order_type_norm != "01" and (price is None or float(price) <= 0):
            return {"status": "FAIL", "reason": "positive_price_required"}
        return {
            "status": "PASS",
            "ticker": pad_ticker(str(ticker)),
            "side": side_norm,
            "qty": qty_int,
            "price": float(price or 0.0),
            "order_type": order_type_norm,
        }

    def _probe_final_decision(
        self,
        ticker: str,
        side: str,
        qty: int,
        price: float | int | None,
        order_type: str,
    ) -> dict[str, Any]:
        return {
            "decision_id": generate_decision_id(),
            "approved": True,
            "target_weights": {},
            "order_deltas": [{
                "ticker": pad_ticker(str(ticker)),
                "side": str(side).lower(),
                "qty": int(qty),
                "price": float(price or 0.0),
                "order_type": str(order_type or "00"),
                "reason": "paper_trading_probe",
            }],
            "reason_code": "PAPER_TRADING_PROBE",
        }

    def _read_balance_stage(self) -> dict[str, Any]:
        balance = self._kis_client.get_balance()
        return {
            "status": "PASS",
            "balance": balance.get("balance"),
            "position_count": len(balance.get("positions", [])),
            "positions": balance.get("positions", []),
            "_mode": balance.get("_mode", self._client_mode()),
        }

    def _read_order_history_stage(
        self,
        ticker: str,
        side: str,
        order_id: str | None = None,
        execution_filter: str = "all",
    ) -> dict[str, Any]:
        if not hasattr(self._kis_client, "get_order_history"):
            return {"status": "SKIP", "reason": "kis_client_no_get_order_history"}
        query = {
            "ticker": pad_ticker(str(ticker)) if str(ticker).strip() else "",
            "order_id": str(order_id or ""),
            "side": str(side or "all").lower(),
            "execution_filter": str(execution_filter or "all").lower(),
        }
        try:
            try:
                history = self._kis_client.get_order_history(**query)
            except TypeError:
                history = self._kis_client.get_order_history(
                    ticker=query["ticker"],
                    side=query["side"],
                    execution_filter=query["execution_filter"],
                )
            orders = list(history.get("orders", []))
            matched_orders = self._filter_history_matches(orders, query)
            if query["order_id"] and not matched_orders:
                return {
                    "status": "FAIL",
                    "error_code": "BROKER_ORDER_ID_NOT_FOUND_IN_HISTORY",
                    "reason": "broker_order_id_not_found_in_history",
                    "query": query,
                    "orders": orders,
                    "summary": history.get("summary", {}),
                    "matched_order_count": 0,
                    "matched_orders": [],
                    "_mode": history.get("_mode", self._client_mode()),
                }
            return {
                "status": "PASS",
                "query": query,
                "orders": orders,
                "summary": history.get("summary", {}),
                "matched_order_count": len(matched_orders),
                "matched_orders": matched_orders,
                "_mode": history.get("_mode", self._client_mode()),
            }
        except Exception as e:
            return {"status": "FAIL", "query": query, "error": str(e)}

    @staticmethod
    def _broker_order_ids(execution_report: dict[str, Any]) -> list[str]:
        ids: list[str] = []
        for fill in execution_report.get("fills", []):
            if not isinstance(fill, dict):
                continue
            raw = (
                fill.get("broker_order_id")
                or fill.get("order_id")
                or (fill.get("broker_response") or {}).get("order_id")
                or (fill.get("broker_response") or {}).get("ODNO")
                or (fill.get("broker_response") or {}).get("odno")
            )
            if raw:
                ids.append(str(raw))
        return ids

    @staticmethod
    def _filter_history_matches(
        orders: list[dict[str, Any]],
        query: dict[str, str],
    ) -> list[dict[str, Any]]:
        order_id = query.get("order_id", "")
        ticker = query.get("ticker", "")
        side = query.get("side", "all")
        matched: list[dict[str, Any]] = []
        for order in orders:
            if not isinstance(order, dict):
                continue
            ids = {
                str(order.get("order_id") or ""),
                str(order.get("broker_order_id") or ""),
                str(order.get("ODNO") or ""),
                str(order.get("odno") or ""),
            }
            if order_id and order_id not in ids:
                continue
            if ticker and pad_ticker(str(order.get("ticker", ""))) != ticker:
                continue
            if side != "all" and str(order.get("side", "")).lower() != side:
                continue
            matched.append(order)
        return matched

    def _base_report(self, action: str) -> dict[str, Any]:
        return {
            "status": "PENDING",
            "action": action,
            "generated_at": datetime.now(_KST).isoformat(),
            "evidence": self._evidence_metadata(),
            "runtime": {
                "kis_mode": self._client_mode(),
                "live_enabled": False,
                "confirm_order_required": True,
            },
            "stages": {},
            "failures": [],
        }

    def _finish_report(self, report: dict[str, Any], write_report: bool) -> dict[str, Any]:
        report["failures"] = self._collect_failures(report)
        if report["status"] == "PENDING":
            report["status"] = self._overall_status(report)
        if write_report:
            report_path = self._write_report(report)
            report["report_path"] = str(report_path)
            try:
                report["report_path_relative"] = str(report_path.relative_to(_PROJECT_ROOT))
            except ValueError:
                report["report_path_relative"] = str(report_path)
        return report

    def _write_report(self, report: dict[str, Any]) -> Path:
        ts = datetime.now(timezone.utc).astimezone(_KST).strftime("%Y%m%d_%H%M%S")
        path = self._report_dir / f"paper_trading_{report['action']}_{ts}.json"
        with path.open("w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        logger.info("[paper_trading] report 저장: %s", path)
        return path

    def _client_mode(self) -> str:
        return str(getattr(self._kis_client, "mode", "unknown")).lower()

    def _evidence_metadata(self) -> dict[str, Any]:
        mode = self._client_mode()
        account_no = ""
        product_code = ""
        app_key = ""
        auth = getattr(self._kis_client, "auth", None)
        try:
            if auth is not None and hasattr(auth, "get_kis_account_parts"):
                account_no, product_code = auth.get_kis_account_parts()
        except Exception as e:
            _ = e
        try:
            if auth is not None and hasattr(auth, "get_kis_app_credentials"):
                app_key, _app_secret = auth.get_kis_app_credentials()
        except Exception as e:
            _ = e

        account_hash = self._short_sha256(f"{account_no}|{product_code}") if account_no else ""
        env_fingerprint = (
            self._short_sha256(f"{mode}|{app_key}|{account_no}|{product_code}")
            if app_key or account_no
            else ""
        )
        generated = datetime.now(_KST)
        return {
            "schema_version": "1.0.0",
            "evidence_run_id": (
                f"PTR-{generated.strftime('%Y%m%d%H%M%S%f')}-"
                f"{env_fingerprint[:8] or 'unknown'}"
            ),
            "broker_env_fingerprint": env_fingerprint,
            "account_hash": account_hash,
            "account_last4": account_no[-4:] if len(account_no) >= 4 else "",
            "account_product_code": product_code,
            "code_version": self._code_version(),
        }

    @staticmethod
    def _short_sha256(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _code_version() -> str:
        explicit = os.getenv("ELEPHANT_CODE_VERSION", "").strip()
        if explicit:
            return explicit
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=_PROJECT_ROOT,
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            )
            return result.stdout.strip() or "unknown"
        except Exception as e:
            _ = e
            return "unknown"

    @staticmethod
    def _collect_failures(report: dict[str, Any]) -> list[str]:
        failures: list[str] = []
        for name, stage in report.get("stages", {}).items():
            if isinstance(stage, dict) and stage.get("status") == "FAIL":
                failures.append(str(name))
        return failures

    @staticmethod
    def _overall_status(report: dict[str, Any]) -> str:
        statuses = [
            stage.get("status")
            for stage in report.get("stages", {}).values()
            if isinstance(stage, dict)
        ]
        if any(status == "FAIL" for status in statuses):
            return "FAIL"
        if statuses and all(status == "SKIP" for status in statuses):
            return "SKIP"
        return "PASS"
