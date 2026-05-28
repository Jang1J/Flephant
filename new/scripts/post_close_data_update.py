#!/usr/bin/env python
"""Post-close data update orchestrator.

This script closes the gap between market close and Mode B by selecting the
latest PIT-safe KOSPI trading day, refreshing real minute data, rebuilding
news/DART + dual-source/exogenous features, and optionally handing off to the
existing post-backfill pre-live pipeline.

It never reads .env files, never submits orders, and never mutates production
registry state.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time as time_module
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
NEW_ROOT = ROOT / "new"
if str(NEW_ROOT) not in sys.path:
    sys.path.insert(0, str(NEW_ROOT))

from src.utils.config_loader import load as config_load, reload as config_reload  # noqa: E402
from src.utils.safe_cast import safe_bool, safe_int  # noqa: E402
from src.utils.trading_calendar import (  # noqa: E402
    is_kospi_trading_day,
    kospi_trading_dates_between,
    kospi_trading_start_date,
    previous_kospi_trading_day,
)

_KST = ZoneInfo("Asia/Seoul")
_REPORT_DIR = ROOT / "artifacts" / "reports" / "post_close_data_update"
_RISK_CONFIG_PATH = NEW_ROOT / "config" / "risk_config.yaml"


@dataclass(frozen=True)
class StageSpec:
    name: str
    command: list[str]
    enabled: bool = True


def _repo_relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _parse_yyyymmdd(value: str) -> date:
    return datetime.strptime(str(value), "%Y%m%d").date()


def _format_yyyymmdd(value: date) -> str:
    return value.strftime("%Y%m%d")


def _risk_cfg() -> dict[str, Any]:
    return config_load("risk_config.yaml") or {}


def _final_dataset_gate_cfg() -> dict[str, Any]:
    cfg = _risk_cfg()
    gate_cfg = (
        cfg.get("backtest_agent", {})
        .get("deploy_decision_gate", {})
        .get("final_dataset_gate", {})
    )
    return gate_cfg if isinstance(gate_cfg, dict) else {}


def _post_close_cfg() -> dict[str, Any]:
    cfg = _risk_cfg().get("post_close_data_update", {})
    return cfg if isinstance(cfg, dict) else {}


def _retry_cfg() -> dict[str, Any]:
    cfg = _post_close_cfg().get("retry", {})
    return cfg if isinstance(cfg, dict) else {}


def _promotion_cfg() -> dict[str, Any]:
    cfg = _post_close_cfg().get("final_dataset_promotion", {})
    return cfg if isinstance(cfg, dict) else {}


def _stage_enabled(cfg: dict[str, Any], key: str, default: bool = True) -> bool:
    stages = cfg.get("stages", {}) or {}
    return safe_bool(stages.get(key), default=default)


def _format_registry_template(raw: Any, *, target_end_date: str, stage: str) -> str:
    template = str(raw or "").strip()
    if not template:
        template = "artifacts/lgbm_research/post_close/{target_end_date}/{stage}"
    return template.format(target_end_date=target_end_date, stage=stage)


def _pit_snapshot_time() -> time:
    pit_cfg = _risk_cfg().get("pit_safety", {}) or {}
    hour = safe_int(pit_cfg.get("snapshot_hour"), default=18, min_value=0, max_value=23)
    minute = safe_int(pit_cfg.get("snapshot_minute"), default=0, min_value=0, max_value=59)
    return time(hour, minute)


def _mode_b_window_state(now: datetime | None = None) -> dict[str, Any]:
    current = (now or datetime.now(_KST)).astimezone(_KST)
    cfg = (_risk_cfg().get("mode_b_scheduler", {}) or {}).get("execution_window", {}) or {}
    start_hour = safe_int(cfg.get("start_hour"), default=18, min_value=0, max_value=23)
    end_hour = safe_int(cfg.get("end_hour"), default=22, min_value=0, max_value=23)
    return {
        "now": current.isoformat(),
        "timezone": "Asia/Seoul",
        "elephant_mode": os.environ.get("ELEPHANT_MODE"),
        "required_elephant_mode": "mode_b",
        "window": f"{start_hour:02d}:00-{end_hour:02d}:59 KST",
        "mode_ok": os.environ.get("ELEPHANT_MODE") == "mode_b",
        "window_ok": start_hour <= current.hour <= end_hour,
    }


def latest_pit_safe_trading_day(now: datetime | None = None) -> date:
    """Return the newest trading day whose post-close snapshot is PIT-safe."""
    current = (now or datetime.now(_KST)).astimezone(_KST)
    today = current.date()
    if is_kospi_trading_day(today) and current.time() >= _pit_snapshot_time():
        return today
    return previous_kospi_trading_day(today)


def _current_final_dataset_end() -> date | None:
    raw = str(_final_dataset_gate_cfg().get("expected_end_date") or "").strip()
    if not raw:
        return None
    try:
        return _parse_yyyymmdd("".join(ch for ch in raw if ch.isdigit())[:8])
    except ValueError:
        return None


def _final_dataset_start() -> date | None:
    raw = str(_final_dataset_gate_cfg().get("expected_start_date") or "").strip()
    if not raw:
        return None
    try:
        return _parse_yyyymmdd("".join(ch for ch in raw if ch.isdigit())[:8])
    except ValueError:
        return None


def _default_business_days() -> int:
    return safe_int(
        _final_dataset_gate_cfg().get("min_business_days"),
        default=249,
        min_value=1,
    )


def _training_window_policy() -> str:
    return str(
        _post_close_cfg().get("training_window_policy")
        or "fixed_min_business_days"
    ).strip()


def _resolve_business_days(target_day: date, requested_business_days: int) -> int:
    """Resolve post-close training length.

    ``expanding_from_final_start`` keeps the first deploy-quality dataset start
    date as an anchor and expands by one trading day whenever a newer
    post-close target is available. This preserves the original 249-day
    evidence while allowing nightly accumulation.
    """
    requested = max(1, int(requested_business_days))
    if _training_window_policy() != "expanding_from_final_start":
        return requested
    start = _final_dataset_start()
    if start is None or start > target_day:
        return requested
    expanded = len(kospi_trading_dates_between(start, target_day))
    return max(requested, expanded)


def _default_max_tickers() -> int:
    return safe_int(
        _final_dataset_gate_cfg().get("min_tickers"),
        default=30,
        min_value=1,
    )


def _python_command(script_name: str, *args: str) -> list[str]:
    return [sys.executable, str(NEW_ROOT / "scripts" / script_name), *args]


def build_stage_plan(
    *,
    end_date: str,
    business_days: int,
    max_tickers: int,
    run_prelive: bool,
    bundle_id: str | None,
) -> list[StageSpec]:
    cfg = _post_close_cfg()
    raw_events_dir = str(cfg.get("raw_events_dir") or "artifacts/raw/dual_source")
    skip_existing = safe_bool(cfg.get("skip_existing_backfill"), default=True)
    target_col = str(cfg.get("target_col_override") or "label_195m_net_ret")
    registry_templates = cfg.get("registry_dirs", {})
    registry_templates = registry_templates if isinstance(registry_templates, dict) else {}
    train_registry_dir = _format_registry_template(
        registry_templates.get("live_data_readiness_train"),
        target_end_date=end_date,
        stage="live_data_readiness",
    )
    prelive_registry_dir = _format_registry_template(
        registry_templates.get("post_backfill_prelive"),
        target_end_date=end_date,
        stage="post_backfill_prelive",
    )
    post_backfill_raw = cfg.get("post_backfill_prelive", {})
    post_backfill_cfg = post_backfill_raw if isinstance(post_backfill_raw, dict) else {}

    live_args = [
        "--all",
        "--end-date",
        end_date,
        "--business-days",
        str(business_days),
        "--max-tickers",
        str(max_tickers),
        "--require-train",
        "--train-registry-dir",
        train_registry_dir,
        "--dns-preflight",
    ]
    if skip_existing:
        live_args.append("--skip-existing-backfill")

    prelive_args = [
        "--end-date",
        end_date,
        "--business-days",
        str(business_days),
        "--max-tickers",
        str(max_tickers),
        "--target-col-override",
        target_col,
        "--registry-dir",
        prelive_registry_dir,
    ]
    if bundle_id:
        prelive_args.extend(["--bundle-id", bundle_id])
    if safe_bool(post_backfill_cfg.get("run_paper_balance"), default=False):
        prelive_args.append("--run-paper-balance")
    if safe_bool(post_backfill_cfg.get("submit_probe"), default=False):
        prelive_args.append("--submit-probe")
        if post_backfill_cfg.get("confirm_phrase"):
            prelive_args.extend(["--confirm-phrase", str(post_backfill_cfg["confirm_phrase"])])
    dual_source_cols = post_backfill_cfg.get("dual_source_feature_cols")
    if isinstance(dual_source_cols, list) and dual_source_cols:
        prelive_args.extend([
            "--dual-source-feature-cols",
            ",".join(str(col) for col in dual_source_cols),
        ])

    return [
        StageSpec(
            "live_data_readiness",
            _python_command("live_data_readiness.py", *live_args),
            _stage_enabled(cfg, "live_data_readiness", True),
        ),
        StageSpec(
            "news_dart_archive",
            _python_command(
                "build_news_dart_archive.py",
                "--end-date",
                end_date,
                "--business-days",
                str(business_days),
            ),
            _stage_enabled(cfg, "news_dart_archive", True),
        ),
        StageSpec(
            "dual_source_materialize",
            _python_command(
                "materialize_dual_source_history.py",
                "--end-date",
                end_date,
                "--business-days",
                str(business_days),
                "--raw-events-dir",
                raw_events_dir,
                *(["--skip-existing"] if skip_existing else []),
            ),
            _stage_enabled(cfg, "dual_source_materialize", True),
        ),
        StageSpec(
            "exogenous_materialize",
            _python_command(
                "materialize_exogenous_history.py",
                "--end-date",
                end_date,
                "--business-days",
                str(business_days),
                *(["--skip-existing"] if skip_existing else []),
            ),
            _stage_enabled(cfg, "exogenous_materialize", True),
        ),
        StageSpec(
            "phase2_feature_backfill",
            _python_command(
                "phase2_feature_backfill.py",
                "--end-date",
                end_date,
                "--business-days",
                str(business_days),
            ),
            _stage_enabled(cfg, "phase2_feature_backfill", True),
        ),
        StageSpec(
            "post_backfill_prelive",
            _python_command("post_backfill_prelive.py", *prelive_args),
            bool(run_prelive) and _stage_enabled(cfg, "post_backfill_prelive", True),
        ),
    ]


def _parse_stdout_json(stdout: str) -> dict[str, Any] | None:
    raw = str(stdout or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(raw[start : end + 1])
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None


def _run_stage(stage: StageSpec, timeout_sec: int) -> dict[str, Any]:
    if not stage.enabled:
        return {
            "status": "SKIP",
            "command": stage.command,
            "reason": "stage_disabled",
        }

    env = os.environ.copy()
    pythonpath = str(NEW_ROOT)
    if env.get("PYTHONPATH"):
        pythonpath = f"{pythonpath}{os.pathsep}{env['PYTHONPATH']}"
    env["PYTHONPATH"] = pythonpath

    started_at = datetime.now(_KST)
    try:
        proc = subprocess.run(
            stage.command,
            cwd=str(ROOT),
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        return {
            "status": "BLOCKED",
            "command": stage.command,
            "returncode": None,
            "error": f"timeout_after_{timeout_sec}s",
            "failure_class": "transient_stage_timeout",
            "retryable": True,
            "retry_after_sec": 90,
            "stdout_tail": (e.stdout or "")[-4000:] if isinstance(e.stdout, str) else "",
            "stderr_tail": (e.stderr or "")[-4000:] if isinstance(e.stderr, str) else "",
            "latency_sec": (datetime.now(_KST) - started_at).total_seconds(),
        }

    stdout_json = _parse_stdout_json(proc.stdout)
    reported_status = stdout_json.get("status") if stdout_json else None
    stage_status = (
        "PASS"
        if proc.returncode == 0 and (reported_status in {None, "PASS"})
        else "BLOCKED"
    )
    result = {
        "status": stage_status,
        "command": stage.command,
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-4000:],
        "stderr_tail": proc.stderr[-4000:],
        "latency_sec": (datetime.now(_KST) - started_at).total_seconds(),
    }
    if stdout_json:
        result["reported_status"] = reported_status
        for key in (
            "report_path",
            "report_path_relative",
            "target_end_date",
            "end_date",
            "business_days",
            "bundle_id",
            "registry_mutated",
            "live_trading_allowed",
            "failures",
            "failure_details",
            "failure_classes",
            "failure_class",
            "retryable",
            "retry_after_sec",
        ):
            if key in stdout_json:
                result[key] = stdout_json.get(key)
    return result


def _stage_retryable(stage_result: dict[str, Any]) -> bool:
    if safe_bool(stage_result.get("retryable"), default=False):
        return True
    failure_class = str(stage_result.get("failure_class") or "")
    if failure_class.startswith("transient_"):
        return True
    classes = stage_result.get("failure_classes")
    if isinstance(classes, list) and classes:
        return all(str(item).startswith("transient_") for item in classes)
    return False


def _stage_retry_after(stage_result: dict[str, Any], default_sec: int) -> int:
    return safe_int(
        stage_result.get("retry_after_sec"),
        default=default_sec,
        min_value=0,
        max_value=1800,
    )


def _write_report(report: dict[str, Any]) -> Path:
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    path = _REPORT_DIR / f"post_close_data_update_{ts}.json"
    report["report_path"] = str(path)
    report["report_path_relative"] = _repo_relative(path)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return path


def _rewrite_final_dataset_gate_text(
    text: str,
    *,
    target_end_date: str,
    business_days: int,
) -> tuple[str, dict[str, bool]]:
    """Update only final_dataset_gate date/window fields while preserving YAML comments."""
    lines = text.splitlines(keepends=True)
    inside = False
    block_indent = 4
    replaced = {"expected_end_date": False, "min_business_days": False}
    rewritten: list[str] = []
    for line in lines:
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        if line.startswith("    final_dataset_gate:"):
            inside = True
            block_indent = 4
        elif inside and stripped and not stripped.startswith("#") and indent <= block_indent:
            inside = False

        if inside and stripped.startswith("expected_end_date:"):
            newline = "\n" if line.endswith("\n") else ""
            rewritten.append(f'      expected_end_date: "{target_end_date}"{newline}')
            replaced["expected_end_date"] = True
            continue
        if inside and stripped.startswith("min_business_days:"):
            newline = "\n" if line.endswith("\n") else ""
            rewritten.append(f"      min_business_days: {int(business_days)}{newline}")
            replaced["min_business_days"] = True
            continue
        rewritten.append(line)

    return "".join(rewritten), replaced


def _promote_final_dataset_gate(
    *,
    report: dict[str, Any],
    target_day: date,
    business_days: int,
    current_end: date | None,
    run_prelive: bool,
) -> dict[str, Any]:
    cfg = _promotion_cfg()
    if not safe_bool(cfg.get("enabled"), default=False):
        return {"status": "SKIP", "reason": "final_dataset_promotion_disabled"}
    if not safe_bool(cfg.get("update_risk_config"), default=True):
        return {"status": "SKIP", "reason": "risk_config_update_disabled"}
    if current_end is not None and current_end >= target_day:
        return {
            "status": "SKIP",
            "reason": "final_dataset_already_at_or_after_target",
            "current_final_dataset_end_date": _format_yyyymmdd(current_end),
            "target_end_date": _format_yyyymmdd(target_day),
        }
    if safe_bool(cfg.get("require_mode_b"), default=True) and os.environ.get("ELEPHANT_MODE") != "mode_b":
        return {
            "status": "BLOCKED",
            "reason": "elephant_mode_not_mode_b",
            "required_env": {"ELEPHANT_MODE": "mode_b"},
        }
    if safe_bool(cfg.get("require_post_backfill_prelive_pass"), default=True):
        if not run_prelive:
            return {
                "status": "BLOCKED",
                "reason": "post_backfill_prelive_not_requested",
            }
        stage = (report.get("stages") or {}).get("post_backfill_prelive") or {}
        if stage.get("status") != "PASS":
            return {
                "status": "BLOCKED",
                "reason": "post_backfill_prelive_not_pass",
                "stage_status": stage.get("status"),
            }
        evidence = _validate_post_backfill_evidence(
            stage,
            target_end_date=_format_yyyymmdd(target_day),
            business_days=business_days,
            bundle_id=str(report.get("bundle_id") or ""),
            max_tickers=safe_int(report.get("max_tickers"), default=30, min_value=1),
        )
        if evidence.get("status") != "PASS":
            return evidence

    target_end = _format_yyyymmdd(target_day)
    path = _RISK_CONFIG_PATH
    try:
        original = path.read_text(encoding="utf-8")
        updated, replaced = _rewrite_final_dataset_gate_text(
            original,
            target_end_date=target_end,
            business_days=business_days,
        )
        missing = [key for key, ok in replaced.items() if not ok]
        if missing:
            return {
                "status": "BLOCKED",
                "reason": "final_dataset_gate_fields_not_found",
                "missing_fields": missing,
                "risk_config_path": str(path),
            }
        backup_dir = ROOT / str(
            cfg.get("backup_dir")
            or "artifacts/reports/post_close_data_update/config_backups"
        )
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"risk_config_before_final_dataset_{target_end}_{ts}.yaml"
        shutil.copy2(path, backup_path)
        path.write_text(updated, encoding="utf-8")
        config_reload("risk_config.yaml")
    except Exception as e:
        return {
            "status": "FAIL",
            "reason": "risk_config_write_failed",
            "error": str(e),
            "error_type": type(e).__name__,
            "risk_config_path": str(path),
        }

    return {
        "status": "PASS",
        "target_end_date": target_end,
        "previous_final_dataset_end_date": (
            _format_yyyymmdd(current_end) if current_end is not None else None
        ),
        "promoted_min_business_days": int(business_days),
        "risk_config_path": str(path),
        "backup_path": str(backup_path),
        "backup_path_relative": _repo_relative(backup_path),
        "registry_mutated": False,
        "live_trading_allowed": False,
    }


def _stage_report_path(stage: dict[str, Any]) -> Path | None:
    for key in ("report_path", "report_path_relative"):
        raw = str(stage.get(key) or "").strip()
        if not raw:
            continue
        path = Path(raw)
        return path if path.is_absolute() else ROOT / path
    return None


def _read_stage_report(stage: dict[str, Any]) -> dict[str, Any] | None:
    path = _stage_report_path(stage)
    if path is None or not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _normalize_yyyymmdd(value: Any) -> str | None:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else None


def _normalize_date_range(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    start = _normalize_yyyymmdd(value.get("start") or value.get("start_date"))
    end = _normalize_yyyymmdd(value.get("end") or value.get("end_date"))
    if not start or not end:
        return None
    return {"start": start, "end": end}


def _model_metadata_from_report(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("candidate_model_metadata", "model_metadata", "candidate_metadata"):
        raw = payload.get(key)
        if isinstance(raw, dict):
            return raw
    artifact = payload.get("candidate_artifact")
    if isinstance(artifact, dict):
        nested = artifact.get("metadata")
        if isinstance(nested, dict):
            return nested
        if any(key in artifact for key in ("train_start", "train_end", "requested_tickers")):
            return artifact
    return {}


def _validate_post_backfill_evidence(
    stage: dict[str, Any],
    *,
    target_end_date: str,
    business_days: int,
    bundle_id: str,
    max_tickers: int,
) -> dict[str, Any]:
    payload = _read_stage_report(stage)
    if payload is None:
        return {
            "status": "BLOCKED",
            "reason": "post_backfill_report_missing_or_unreadable",
            "stage_report_path": str(_stage_report_path(stage) or ""),
        }
    blockers: list[str] = []
    if payload.get("status") != "PASS":
        blockers.append("post_backfill_status_not_pass")
    if str(payload.get("end_date") or "") != target_end_date:
        blockers.append("post_backfill_end_date_mismatch")
    if safe_int(payload.get("business_days"), default=-1) != int(business_days):
        blockers.append("post_backfill_business_days_mismatch")
    if bundle_id and str(payload.get("bundle_id") or "") != bundle_id:
        blockers.append("post_backfill_bundle_id_mismatch")
    if safe_int(payload.get("training_ticker_count"), default=0) < int(max_tickers):
        blockers.append("training_ticker_count_below_expected")

    stages = payload.get("stages") if isinstance(payload.get("stages"), dict) else {}
    required_stages = [
        "02_feature_coverage",
        "02_lgbm_bundle",
        "03_service_policy_replay",
        "04_backtest",
        "05_paper_balance_reconciliation",
        "07_prelive_gate_after",
    ]
    for name in required_stages:
        if (stages.get(name) or {}).get("status") != "PASS":
            blockers.append(f"{name}_not_pass")

    lgbm_result = (stages.get("02_lgbm_bundle") or {}).get("result") or {}
    if safe_bool(lgbm_result.get("synthetic_fallback"), default=False):
        blockers.append("lgbm_synthetic_fallback")
    if lgbm_result and not safe_bool(lgbm_result.get("candidate_bundle_staged"), default=False):
        blockers.append("candidate_bundle_not_staged")

    backtest = (stages.get("04_backtest") or {}).get("result") or {}
    regression = backtest.get("regression_risk") or {}
    leakage = backtest.get("minute_bar_leakage_check") or {}
    service_policy = backtest.get("service_policy_replay") or {}
    expected_policy_range = _normalize_date_range(
        backtest.get("service_policy_expected_date_range")
        or backtest.get("date_range")
    )
    service_policy_stage = (stages.get("03_service_policy_replay") or {}).get("result") or {}
    service_policy_stage_range = _normalize_date_range(
        service_policy_stage.get("date_range")
    )
    embedded_service_policy_range = _normalize_date_range(
        service_policy.get("date_range")
    )
    model_metadata = _model_metadata_from_report(backtest)
    model_train_end = _normalize_yyyymmdd(model_metadata.get("train_end"))
    lgbm_end = _normalize_yyyymmdd(
        lgbm_result.get("end_date")
        or lgbm_result.get("train_end")
        or lgbm_result.get("training_end")
    )
    if backtest and backtest.get("verdict") != "pass":
        blockers.append("backtest_verdict_not_pass")
    if backtest and safe_bool(regression.get("flagged"), default=True):
        blockers.append("backtest_regression_flagged")
    if backtest and leakage.get("verdict") != "pass":
        blockers.append("backtest_leakage_not_pass")
    if backtest and service_policy.get("status") != "PASS":
        blockers.append("service_policy_not_pass")
    if backtest:
        if model_train_end != target_end_date:
            blockers.append("backtest_model_train_end_mismatch")
        if not expected_policy_range:
            blockers.append("backtest_service_policy_expected_date_range_missing")
        if not service_policy_stage_range:
            blockers.append("service_policy_replay_date_range_missing")
        elif expected_policy_range and service_policy_stage_range != expected_policy_range:
            blockers.append("service_policy_replay_date_range_mismatch")
        if not embedded_service_policy_range:
            blockers.append("backtest_embedded_service_policy_date_range_missing")
        elif expected_policy_range and embedded_service_policy_range != expected_policy_range:
            blockers.append("backtest_embedded_service_policy_date_range_mismatch")
    if lgbm_end is not None and lgbm_end != target_end_date:
        blockers.append("lgbm_training_end_mismatch")

    prelive_after = stages.get("07_prelive_gate_after") or {}
    if str(prelive_after.get("end_date") or "") != target_end_date:
        blockers.append("prelive_gate_after_end_date_mismatch")
    if (
        "business_days" in prelive_after
        and safe_int(prelive_after.get("business_days"), default=-1) != int(business_days)
    ):
        blockers.append("prelive_gate_after_business_days_mismatch")

    if blockers:
        return {
            "status": "BLOCKED",
            "reason": "post_backfill_evidence_contract_failed",
            "blockers": blockers,
            "stage_report_path": str(_stage_report_path(stage) or ""),
        }
    return {
        "status": "PASS",
        "stage_report_path": str(_stage_report_path(stage) or ""),
        "required_stages": required_stages,
    }


def run_update(
    *,
    end_date: str | None,
    business_days: int,
    max_tickers: int,
    dry_run: bool,
    run_prelive: bool,
    bundle_id: str | None,
    timeout_sec: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    cfg = _post_close_cfg()
    target_day = _parse_yyyymmdd(end_date) if end_date else latest_pit_safe_trading_day(now)
    target_end = _format_yyyymmdd(target_day)
    requested_business_days = int(business_days)
    effective_business_days = _resolve_business_days(target_day, requested_business_days)
    start_day = kospi_trading_start_date(target_day, effective_business_days)
    current_end = _current_final_dataset_end()
    plan = build_stage_plan(
        end_date=target_end,
        business_days=effective_business_days,
        max_tickers=max_tickers,
        run_prelive=run_prelive,
        bundle_id=bundle_id,
    )
    enabled_plan = [stage for stage in plan if stage.enabled]
    report: dict[str, Any] = {
        "status": "DRY_RUN" if dry_run else "PENDING",
        "action": "post_close_data_update",
        "generated_at": datetime.now(_KST).isoformat(),
        "target_end_date": target_end,
        "target_start_date": _format_yyyymmdd(start_day),
        "business_days": effective_business_days,
        "requested_business_days": requested_business_days,
        "training_window_policy": _training_window_policy(),
        "final_dataset_start_anchor": (
            _format_yyyymmdd(_final_dataset_start())
            if _final_dataset_start() is not None
            else None
        ),
        "max_tickers": max_tickers,
        "current_final_dataset_end_date": (
            _format_yyyymmdd(current_end) if current_end is not None else None
        ),
        "pit_snapshot_time": _pit_snapshot_time().strftime("%H:%M"),
        "dry_run": dry_run,
        "run_prelive": run_prelive,
        "bundle_id": bundle_id,
        "registry_mutated": False,
        "live_trading_allowed": False,
        "stages": {},
        "commands": {stage.name: stage.command for stage in plan},
        "blockers": [],
        "caveats": [
            "This orchestrator does not read .env; API keys must already be present in the process environment for real API stages.",
            "Final dataset SSOT promotion is fail-closed and updates only risk_config final_dataset_gate date/window fields after configured Mode B evidence passes.",
        ],
    }

    if current_end is not None and current_end >= target_day:
        report["status"] = "SKIP"
        report["reason"] = "final_dataset_already_at_or_after_target"
        return report

    if dry_run:
        report["blockers"] = ["dry_run_only"]
        return report

    window = _mode_b_window_state(now)
    if not window["mode_ok"] or not window["window_ok"]:
        report["status"] = "BLOCKED"
        report["blockers"] = ["mode_b_window_not_open"]
        report["mode_b_window"] = window
        report["required_env"] = {"ELEPHANT_MODE": "mode_b"}
        return report

    timeout = safe_int(
        timeout_sec if timeout_sec is not None else cfg.get("stage_timeout_sec"),
        default=3600,
        min_value=1,
    )
    stop_on_failure = safe_bool(cfg.get("stop_on_stage_failure"), default=True)
    retry_cfg = _retry_cfg()
    retry_enabled = safe_bool(retry_cfg.get("enabled"), default=True)
    max_attempts = safe_int(retry_cfg.get("max_attempts"), default=3, min_value=1, max_value=10)
    retry_backoff_sec = safe_int(
        retry_cfg.get("backoff_sec"),
        default=90,
        min_value=0,
        max_value=1800,
    )
    blockers: list[str] = []
    stage_attempts: dict[str, Any] = {}
    for stage in enabled_plan:
        attempts: list[dict[str, Any]] = []
        stage_result: dict[str, Any] = {}
        for attempt_index in range(1, max_attempts + 1):
            if attempt_index > 1:
                retry_window = _mode_b_window_state(datetime.now(_KST))
                if not retry_window["mode_ok"] or not retry_window["window_ok"]:
                    stage_result = {
                        "status": "BLOCKED",
                        "command": stage.command,
                        "returncode": None,
                        "attempt": attempt_index,
                        "reason": "mode_b_window_closed_before_retry",
                        "retry_window": retry_window,
                    }
                    attempts.append(stage_result)
                    break
            stage_result = _run_stage(stage, timeout)
            stage_result["attempt"] = attempt_index
            attempts.append(stage_result)
            if stage_result.get("status") == "PASS":
                break
            if not retry_enabled or attempt_index >= max_attempts:
                break
            if not _stage_retryable(stage_result):
                break
            retry_window = _mode_b_window_state(datetime.now(_KST))
            if not retry_window["mode_ok"] or not retry_window["window_ok"]:
                stage_result["retry_stopped_reason"] = "mode_b_window_closed"
                stage_result["retry_window"] = retry_window
                break
            sleep_sec = min(_stage_retry_after(stage_result, retry_backoff_sec), retry_backoff_sec)
            stage_result["next_retry_after_sec"] = sleep_sec
            if sleep_sec > 0:
                time_module.sleep(sleep_sec)
        if len(attempts) > 1:
            stage_attempts[stage.name] = attempts
        report["stages"][stage.name] = stage_result
        if stage_result.get("status") != "PASS":
            blockers.append(stage.name)
            if stop_on_failure:
                break

    if stage_attempts:
        report["stage_attempts"] = stage_attempts
    report["blockers"] = blockers
    report["status"] = "PASS" if not blockers else "BLOCKED"
    if report["status"] == "PASS":
        promotion = _promote_final_dataset_gate(
            report=report,
            target_day=target_day,
            business_days=effective_business_days,
            current_end=current_end,
            run_prelive=run_prelive,
        )
        report["ssot_promotion"] = promotion
        if promotion.get("status") in {"BLOCKED", "FAIL"}:
            report["status"] = "BLOCKED"
            report["blockers"] = ["ssot_final_dataset_promotion"]
    return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    default_cfg = _post_close_cfg()
    parser = argparse.ArgumentParser(
        description="Run post-close data/backfill/materialization update for the latest PIT-safe KOSPI date.",
    )
    parser.add_argument("--end-date", default="", help="YYYYMMDD. Defaults to latest PIT-safe KOSPI trading day.")
    parser.add_argument("--business-days", type=int, default=_default_business_days())
    parser.add_argument("--max-tickers", type=int, default=_default_max_tickers())
    parser.add_argument("--bundle-id", default="")
    parser.add_argument(
        "--run-prelive",
        action="store_true",
        default=safe_bool(default_cfg.get("run_post_backfill_prelive_by_default"), default=False),
        help="Also run post_backfill_prelive.py after data/features are refreshed.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout-sec", type=int, default=None)
    parser.add_argument("--no-write-report", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report = run_update(
        end_date=str(args.end_date).strip() or None,
        business_days=int(args.business_days),
        max_tickers=int(args.max_tickers),
        dry_run=bool(args.dry_run),
        run_prelive=bool(args.run_prelive),
        bundle_id=str(args.bundle_id).strip() or None,
        timeout_sec=args.timeout_sec,
    )
    if not bool(args.no_write_report):
        _write_report(report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") in {"PASS", "DRY_RUN"} else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
