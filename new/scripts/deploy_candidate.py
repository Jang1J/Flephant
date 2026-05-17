#!/usr/bin/env python
"""C14 candidate bundle deploy CLI.

가장 최신 deployable C12 backtest report를 확인한 뒤 ModeBDeployer를 실행한다.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import prelive_gate  # noqa: E402
from src.mode_b.deployer import ModeBDeployer, RegressionRisk  # noqa: E402
from src.utils.config_loader import load as config_load  # noqa: E402
from src.utils.safe_cast import safe_bool  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")


def _latest_deployable_backtest(bundle_id: str) -> tuple[Path | None, dict[str, Any] | None]:
    return prelive_gate._latest_matching_report(
        ROOT / "artifacts" / "reports" / "backtest",
        f"backtest_{bundle_id}_",
        lambda payload: prelive_gate._is_deployable_backtest_report(payload, bundle_id),
    )


def _latest_any_backtest(bundle_id: str) -> tuple[Path | None, dict[str, Any] | None]:
    return prelive_gate._latest_matching_report(
        ROOT / "artifacts" / "reports" / "backtest",
        f"backtest_{bundle_id}_",
        lambda payload: payload.get("bundle_id") == bundle_id,
    )


def _deployability_payload(
    bundle_id: str,
    backtest_path: Path | None,
    backtest: dict[str, Any] | None,
    *,
    deployable: bool,
    dry_run: bool,
) -> dict[str, Any]:
    regression = (backtest or {}).get("regression_risk") or {}
    leakage = (backtest or {}).get("minute_bar_leakage_check") or {}
    feature_quality = (backtest or {}).get("feature_quality") or {}
    service_policy = (backtest or {}).get("service_policy_replay")
    final_dataset_gate = (
        prelive_gate._final_dataset_gate_result(backtest or {})
        if backtest is not None
        else {"status": "BLOCKED", "blockers": ["backtest_missing"]}
    )
    return {
        "deployable": bool(deployable),
        "dry_run": bool(dry_run),
        "registry_mutated": False,
        "bundle_id": bundle_id,
        "requires_verdict": "pass",
        "requires_regression_risk_flagged": False,
        "requires_minute_bar_leakage_verdict": "pass",
        "requires_feature_quality_gate": True,
        "requires_service_policy_gate": True,
        "requires_final_dataset_gate": True,
        "feature_quality_gate_pass": (
            prelive_gate._feature_quality_gate_pass(backtest or {})
            if backtest is not None
            else False
        ),
        "service_policy_gate_pass": (
            prelive_gate._service_policy_gate_pass(backtest or {}, bundle_id)
            if backtest is not None
            else False
        ),
        "final_dataset_gate_pass": final_dataset_gate.get("status") == "PASS",
        "final_dataset_gate": final_dataset_gate,
        "latest_backtest": {
            "report_path": (
                prelive_gate._repo_relative(backtest_path)
                if backtest_path is not None
                else None
            ),
            "verdict": (backtest or {}).get("verdict"),
            "status": (backtest or {}).get("status"),
            "regression_risk_flagged": regression.get("flagged"),
            "regression_risk_severity": regression.get("severity"),
            "minute_bar_leakage_verdict": leakage.get("verdict"),
            "metrics": (backtest or {}).get("metrics", {}),
            "feature_quality": feature_quality,
            "service_policy_replay": service_policy,
        },
    }


def _write_report(report: dict[str, Any]) -> Path:
    cfg = config_load("risk_config.yaml", "mode_b_deploy") or {}
    report_dir = Path(str(cfg.get("report_dir", "artifacts/reports/deploy")))
    if not report_dir.is_absolute():
        report_dir = ROOT / report_dir
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    bundle_id = str(report.get("bundle_id", "BUNDLE-UNKNOWN"))
    path = report_dir / f"deploy_{bundle_id}_{ts}.json"
    report["report_path"] = str(path)
    try:
        report["report_path_relative"] = str(path.relative_to(ROOT))
    except ValueError:
        report["report_path_relative"] = str(path)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return path


def _required_deploy_confirm_phrase() -> str:
    cfg = config_load("risk_config.yaml", "mode_b_deploy") or {}
    phrase = str(cfg.get("confirm_deploy_phrase", "")).strip()
    if not phrase:
        raise RuntimeError("mode_b_deploy.confirm_deploy_phrase missing")
    return phrase


def _confirmation_blocked_report(bundle_id: str, reason: str) -> dict[str, Any]:
    return {
        "status": "BLOCKED",
        "bundle_id": bundle_id,
        "reason": reason,
        "registry_mutated": False,
        "live_trading_allowed": False,
        "deployability": {
            "deployable": False,
            "dry_run": False,
            "registry_mutated": False,
            "bundle_id": bundle_id,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy C14 candidate bundle")
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="배포 가능성만 평가하고 registry/artifacts를 변경하지 않는다.",
    )
    parser.add_argument("--no-write-report", action="store_true")
    parser.add_argument(
        "--confirm-deploy",
        default="",
        help="실제 registry deploy 승인 문구. dry-run에는 필요 없음.",
    )
    args = parser.parse_args(argv)

    bundle_id = str(args.bundle_id)
    backtest_path, backtest = _latest_deployable_backtest(bundle_id)
    dry_run = bool(args.dry_run)
    if dry_run:
        latest_path, latest_backtest = (
            (backtest_path, backtest)
            if backtest_path and backtest
            else _latest_any_backtest(bundle_id)
        )
        deployability = _deployability_payload(
            bundle_id,
            latest_path,
            latest_backtest,
            deployable=bool(backtest_path and backtest),
            dry_run=True,
        )
        report = {
            "status": "PASS" if backtest_path and backtest else "BLOCKED",
            "dry_run": True,
            "bundle_id": bundle_id,
            "deployable": bool(deployability.get("deployable", False)),
            "service_policy_gate_pass": bool(
                deployability.get("service_policy_gate_pass", False)
            ),
            "registry_mutated": False,
            "live_trading_allowed": False,
            "reason": (
                "deployable_backtest_found"
                if backtest_path and backtest
                else "deployable_backtest_not_found"
            ),
            "deployability": deployability,
        }
        if not args.no_write_report:
            _write_report(report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report.get("status") == "PASS" else 1

    if not backtest_path or not backtest:
        latest_path, latest_backtest = _latest_any_backtest(bundle_id)
        report = {
            "status": "BLOCKED",
            "bundle_id": bundle_id,
            "reason": "deployable_backtest_not_found",
            "deployability": _deployability_payload(
                bundle_id,
                latest_path,
                latest_backtest,
                deployable=False,
                dry_run=False,
            ),
        }
        if not args.no_write_report:
            _write_report(report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    try:
        required_confirm = _required_deploy_confirm_phrase()
    except Exception as e:
        report = _confirmation_blocked_report(
            bundle_id,
            f"deploy_confirm_phrase_unavailable:{type(e).__name__}",
        )
        if not args.no_write_report:
            _write_report(report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    if str(args.confirm_deploy).strip() != required_confirm:
        report = _confirmation_blocked_report(
            bundle_id,
            "production_deploy_confirmation_required",
        )
        if not args.no_write_report:
            _write_report(report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    risk_payload = backtest.get("regression_risk") or {}
    try:
        deploy_result = ModeBDeployer().deploy(
            bundle_id=bundle_id,
            backtest_verdict=str(backtest.get("verdict", "fail")),
            sanity_check_result="ok",
            regression_risk=RegressionRisk(
                flagged=safe_bool(risk_payload.get("flagged", False), default=False),
                severity=str(risk_payload.get("severity", "low")),
                evidence=[str(e) for e in risk_payload.get("evidence", [])],
                snapshot_metrics=dict(risk_payload.get("snapshot_metrics", {})),
            ),
            service_policy_evidence=(
                backtest.get("service_policy_replay")
                if isinstance(backtest.get("service_policy_replay"), dict)
                else {}
            ),
            feature_quality=backtest.get("feature_quality") or {},
        )
        report = {
            "status": "PASS",
            "bundle_id": bundle_id,
            "backtest_report_path": prelive_gate._repo_relative(backtest_path),
            "deploy_result": deploy_result,
        }
    except Exception as e:
        report = {
            "status": "FAIL",
            "bundle_id": bundle_id,
            "backtest_report_path": prelive_gate._repo_relative(backtest_path),
            "error": str(e),
            "error_type": type(e).__name__,
        }

    if not args.no_write_report:
        _write_report(report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
