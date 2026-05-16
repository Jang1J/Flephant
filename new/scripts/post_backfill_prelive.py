"""Post-backfill pre-live orchestrator.

Run this after the final dataset ``live_data_readiness --all --require-train``
job finishes. The script does not read .env files. It uses the already sourced
process environment, keeps live trading disabled, and advances only through
safe pre-live gates.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
NEW_ROOT = REPO_ROOT / "new"
if str(NEW_ROOT) not in sys.path:
    sys.path.insert(0, str(NEW_ROOT))

from scripts import prelive_gate  # noqa: E402
from scripts.service_policy_replay import run_service_policy_replay  # noqa: E402
from src.execution.paper_trading import PaperTradingRunner  # noqa: E402
from src.jobs.run_backtest import run_backtest  # noqa: E402
from src.mode_b.nightly_lgbm_retrainer import NightlyLGBMRetrainer  # noqa: E402
from src.utils.id_factory import generate_bundle_id  # noqa: E402
from src.utils.trading_calendar import (  # noqa: E402
    kospi_trading_start_date,
    previous_kospi_trading_day,
)

_KST = ZoneInfo("Asia/Seoul")
_REPORT_ROOT = REPO_ROOT / "artifacts" / "reports" / "prelive_pipeline"


def _previous_business_day(today: date | None = None) -> date:
    return previous_kospi_trading_day(today)


def _business_start_date(end: date, business_days: int) -> date:
    return kospi_trading_start_date(end, business_days)


def _final_gate_default_end_date() -> str:
    gate_cfg = prelive_gate._final_dataset_gate_cfg()
    expected = prelive_gate._parse_dataset_date(gate_cfg.get("expected_end_date"))
    if expected is not None:
        return expected.strftime("%Y%m%d")
    return _previous_business_day().strftime("%Y%m%d")


def _stage(status: str, message: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {"status": status, "message": message}
    if detail:
        payload.update(detail)
    return payload


def _repo_relative(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _write_report(report: dict[str, Any]) -> Path:
    _REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    path = _REPORT_ROOT / f"post_backfill_prelive_{ts}.json"
    report["report_path"] = str(path)
    report["report_path_relative"] = _repo_relative(path)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return path


def _load_system_positions(path: str | None) -> list[dict[str, Any]] | None:
    if not path:
        return None
    pos_path = Path(path)
    with pos_path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict) and isinstance(data.get("positions"), list):
        return list(data["positions"])
    if isinstance(data, list):
        return list(data)
    raise ValueError("system positions JSON must be a list or {'positions': [...]}")


def _latest_readiness_status(end_date: str, business_days: int, max_tickers: int) -> dict[str, Any]:
    gate = prelive_gate.build_report(
        end_date=end_date,
        business_days=business_days,
        max_tickers=max_tickers,
    )
    wanted = {
        "01_code_ssot",
        "02_real_data_readiness",
        "03_80_business_day_data",
        "09_ops_risk",
    }
    blockers = [
        key
        for key in wanted
        if gate.get("stages", {}).get(key, {}).get("status") != "PASS"
    ]
    return {
        "status": "PASS" if not blockers else "BLOCKED",
        "gate": gate,
        "blockers": blockers,
    }


def _final_training_tickers(max_tickers: int) -> list[str]:
    """Resolve the final deploy training universe, including pending_data names."""
    return prelive_gate._active_tickers(
        max_tickers=max_tickers,
        include_pending_data=True,
    )


def run_pipeline(
    *,
    end_date: str,
    business_days: int,
    max_tickers: int,
    bundle_id: str | None,
    run_paper_balance: bool,
    system_positions_json: str | None,
    submit_probe: bool,
    ticker: str,
    side: str,
    qty: int,
    price: float | None,
    confirm_phrase: str | None,
    order_type: str,
    target_col_override: str | None = None,
    registry_dir: str | None = None,
    allow_production_candidate_write: bool = False,
) -> dict[str, Any]:
    end_dt = datetime.strptime(end_date, "%Y%m%d").date()
    start_date = _business_start_date(end_dt, business_days).strftime("%Y%m%d")
    resolved_bundle_id = bundle_id or generate_bundle_id()
    stages: dict[str, Any] = {}

    report: dict[str, Any] = {
        "status": "RUNNING",
        "generated_at": datetime.now(_KST).isoformat(),
        "start_date": start_date,
        "end_date": end_date,
        "business_days": business_days,
        "bundle_id": resolved_bundle_id,
        "target_col_override": target_col_override,
        "registry_dir": registry_dir,
        "runtime": {
            "elephant_mode": os.environ.get("ELEPHANT_MODE"),
            "kis_mode": os.environ.get("KIS_MODE"),
            "live_enabled_expected": False,
        },
        "stages": stages,
    }

    if os.environ.get("ELEPHANT_MODE") != "mode_b":
        stages["00_mode_b_guard"] = _stage(
            "BLOCKED",
            "post-backfill prelive pipeline requires ELEPHANT_MODE=mode_b.",
            {"current_mode": os.environ.get("ELEPHANT_MODE")},
        )
        report["status"] = "BLOCKED"
        report["blockers"] = ["00_mode_b_guard"]
        return report

    readiness = _latest_readiness_status(end_date, business_days, max_tickers)
    stages["01_prelive_gate_before"] = readiness
    if readiness["status"] != "PASS":
        stages["02_lgbm_bundle"] = _stage(
            "SKIP",
            "80-day readiness gate is not PASS yet.",
            {"upstream_blockers": readiness["blockers"]},
        )
        report["status"] = "BLOCKED"
        report["blockers"] = ["01_prelive_gate_before"]
        return report

    training_tickers = _final_training_tickers(max_tickers)
    report["training_tickers"] = training_tickers
    report["training_ticker_count"] = len(training_tickers)
    if not training_tickers:
        stages["02_lgbm_bundle"] = _stage(
            "BLOCKED",
            "Final training universe resolved to zero tickers.",
            {"max_tickers": max_tickers},
        )
        report["status"] = "BLOCKED"
        report["blockers"] = ["02_lgbm_bundle"]
        return report

    try:
        lgbm_result = NightlyLGBMRetrainer(
            registry_dir=registry_dir,
            allow_production_candidate_write=allow_production_candidate_write,
        ).retrain(
            bundle_id=resolved_bundle_id,
            tickers=training_tickers,
            start_date=start_date,
            end_date=end_date,
            target_col_override=target_col_override,
        )
        stages["02_lgbm_bundle"] = _stage(
            "PASS" if lgbm_result.get("candidate_bundle_staged") else "BLOCKED",
            "LightGBM candidate bundle retrain/staging finished.",
            {"result": lgbm_result},
        )
    except Exception as e:
        stages["02_lgbm_bundle"] = _stage(
            "FAIL",
            "LightGBM candidate bundle retrain failed.",
            {"error": str(e), "error_type": type(e).__name__},
        )

    if stages["02_lgbm_bundle"]["status"] != "PASS":
        report["status"] = "BLOCKED"
        report["blockers"] = ["02_lgbm_bundle"]
        return report

    try:
        service_policy = run_service_policy_replay(
            resolved_bundle_id,
            write_report=True,
        )
        stages["03_service_policy_replay"] = _stage(
            "PASS" if service_policy.get("status") == "PASS" else "BLOCKED",
            "Service-policy replay completed for the C12 fold window.",
            {
                "result": {
                    "status": service_policy.get("status"),
                    "gate": service_policy.get("gate", {}),
                    "metrics": service_policy.get("metrics", {}),
                    "date_range": service_policy.get("date_range", {}),
                    "report_path": service_policy.get("report_path"),
                    "report_path_relative": service_policy.get("report_path_relative"),
                },
            },
        )
    except Exception as e:
        stages["03_service_policy_replay"] = _stage(
            "FAIL",
            "Service-policy replay failed before C12.",
            {"error": str(e), "error_type": type(e).__name__},
        )

    if stages["03_service_policy_replay"]["status"] != "PASS":
        report["status"] = "BLOCKED"
        report["blockers"] = ["03_service_policy_replay"]
        return report

    backtest = run_backtest(resolved_bundle_id, write_report=True)
    backtest_deployable = prelive_gate._is_deployable_backtest_report(
        backtest,
        resolved_bundle_id,
    )
    stages["04_backtest"] = _stage(
        "PASS" if backtest_deployable else "BLOCKED",
        "BacktestAgent run completed.",
        {
            "result": backtest,
            "deployable": backtest_deployable,
            "required": {
                "bundle_id": resolved_bundle_id,
                "verdict": "pass",
                "regression_risk.flagged": False,
                "minute_bar_leakage_check.verdict": "pass",
                "feature_quality_gate": True,
                "service_policy_gate": True,
                "feature_quality_gate_pass": prelive_gate._feature_quality_gate_pass(backtest),
                "service_policy_gate_pass": prelive_gate._service_policy_gate_pass(
                    backtest,
                    resolved_bundle_id,
                ),
                "service_policy_report_path": (
                    (backtest.get("service_policy_replay") or {}).get("service_policy_report_path")
                    or (backtest.get("service_policy_replay") or {}).get("report_path")
                ),
                "service_policy_report_sha256": (
                    (backtest.get("service_policy_replay") or {}).get("service_policy_report_sha256")
                ),
            },
        },
    )

    if stages["04_backtest"]["status"] != "PASS":
        report["status"] = "BLOCKED"
        report["blockers"] = ["04_backtest"]
        return report

    if run_paper_balance:
        try:
            system_positions = _load_system_positions(system_positions_json)
            paper_report = PaperTradingRunner().run_balance_reconciliation(
                system_positions=system_positions,
                write_report=True,
            )
            stages["05_paper_balance_reconciliation"] = _stage(
                "PASS" if paper_report.get("status") == "PASS" else "BLOCKED",
                "Paper balance/reconciliation completed.",
                {"result": paper_report},
            )
        except Exception as e:
            stages["05_paper_balance_reconciliation"] = _stage(
                "FAIL",
                "Paper balance/reconciliation failed.",
                {"error": str(e), "error_type": type(e).__name__},
            )
    else:
        stages["05_paper_balance_reconciliation"] = _stage(
            "SKIP",
            "Pass --run-paper-balance to execute read-only paper balance/reconciliation.",
        )

    if stages["05_paper_balance_reconciliation"]["status"] != "PASS":
        report["status"] = "BLOCKED"
        report["blockers"] = ["05_paper_balance_reconciliation"]
        return report

    if submit_probe:
        try:
            probe_report = PaperTradingRunner().submit_probe_order(
                ticker=ticker,
                side=side,
                qty=qty,
                price=price,
                order_type=order_type,
                confirm_phrase=confirm_phrase,
                write_report=True,
            )
            stages["06_paper_probe_order"] = _stage(
                "PASS" if probe_report.get("status") == "PASS" else "BLOCKED",
                "Paper probe order completed.",
                {"result": probe_report},
            )
        except Exception as e:
            stages["06_paper_probe_order"] = _stage(
                "FAIL",
                "Paper probe order failed.",
                {"error": str(e), "error_type": type(e).__name__},
            )
    else:
        stages["06_paper_probe_order"] = _stage(
            "SKIP",
            "Pass --submit-probe and the configured confirm phrase to execute a paper probe order.",
        )

    final_gate = prelive_gate.build_report(
        end_date=end_date,
        business_days=business_days,
        max_tickers=max_tickers,
        bundle_id=resolved_bundle_id,
    )
    stages["07_prelive_gate_after"] = final_gate
    blockers = [
        name
        for name, stage in stages.items()
        if stage.get("status") in {"BLOCKED", "FAIL"}
    ]
    report["status"] = "PASS" if not blockers and final_gate.get("status") == "PASS" else "BLOCKED"
    report["blockers"] = blockers if blockers else list(final_gate.get("blockers", []))
    return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    default_end = _final_gate_default_end_date()
    parser = argparse.ArgumentParser(
        description="Run post-backfill gates up to the pre-live boundary",
    )
    parser.add_argument("--end-date", default=default_end, help="YYYYMMDD")
    parser.add_argument("--business-days", type=int, default=prelive_gate._final_gate_min_business_days())
    parser.add_argument("--max-tickers", type=int, default=prelive_gate._final_gate_min_tickers())
    parser.add_argument("--bundle-id", default="")
    parser.add_argument("--run-paper-balance", action="store_true")
    parser.add_argument("--system-positions-json", default="")
    parser.add_argument("--submit-probe", action="store_true")
    parser.add_argument("--ticker", default="005930")
    parser.add_argument("--side", choices=["buy", "sell"], default="buy")
    parser.add_argument("--qty", type=int, default=1)
    parser.add_argument("--price", type=float, default=None)
    parser.add_argument("--order-type", default="00")
    parser.add_argument("--confirm-phrase", default="")
    parser.add_argument("--target-col-override", default="")
    parser.add_argument("--registry-dir", default="")
    parser.add_argument("--allow-production-candidate-write", action="store_true")
    parser.add_argument("--no-write-report", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report = run_pipeline(
        end_date=str(args.end_date),
        business_days=int(args.business_days),
        max_tickers=int(args.max_tickers),
        bundle_id=str(args.bundle_id).strip() or None,
        run_paper_balance=bool(args.run_paper_balance),
        system_positions_json=str(args.system_positions_json).strip() or None,
        submit_probe=bool(args.submit_probe),
        ticker=str(args.ticker),
        side=str(args.side),
        qty=int(args.qty),
        price=args.price,
        confirm_phrase=str(args.confirm_phrase).strip() or None,
        order_type=str(args.order_type or "00"),
        target_col_override=str(args.target_col_override).strip() or None,
        registry_dir=str(args.registry_dir).strip() or None,
        allow_production_candidate_write=bool(args.allow_production_candidate_write),
    )
    if not bool(args.no_write_report):
        _write_report(report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
