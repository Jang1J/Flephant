"""Print sanitized environment readiness for KIS virtual paper trading.

This script reads only the current process environment. It never reads `.env`
files and never prints secret values.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.utils.config_loader import load as config_load  # noqa: E402
from src.utils.safe_cast import safe_bool  # noqa: E402


REQUIRED_KIS_ENV = [
    "KIS_MODE",
    "KIS_APP_KEY",
    "KIS_APP_SECRET",
    "KIS_ACCOUNT_NUMBER",
    "KIS_ACCOUNT_PRODUCT_CODE",
]


def _presence(key: str) -> dict[str, Any]:
    value = os.environ.get(key, "")
    return {
        "present": bool(value),
        "length": len(value) if value else 0,
    }


def _mode_specific_candidates(mode: str, suffix: str) -> list[str]:
    if mode == "virtual":
        return [f"KIS_PAPER_{suffix}", f"KIS_{suffix}"]
    if mode == "real":
        return [f"KIS_REAL_{suffix}", f"KIS_{suffix}"]
    return [f"KIS_{suffix}"]


def _effective_presence(mode: str, suffix: str) -> dict[str, Any]:
    candidates = _mode_specific_candidates(mode, suffix)
    for key in candidates:
        value = os.environ.get(key, "").strip()
        if value:
            return {
                "present": True,
                "selected_env": key,
                "length": len(value),
                "candidate_envs": candidates,
            }
    return {
        "present": False,
        "selected_env": None,
        "length": 0,
        "candidate_envs": candidates,
    }


def build_report() -> dict[str, Any]:
    mode = os.environ.get("KIS_MODE", "").strip().lower()
    required = {key: _presence(key) for key in REQUIRED_KIS_ENV}
    effective = {
        "APP_KEY": _effective_presence(mode, "APP_KEY"),
        "APP_SECRET": _effective_presence(mode, "APP_SECRET"),
        "ACCOUNT_NUMBER": _effective_presence(mode, "ACCOUNT_NUMBER"),
        "ACCOUNT_PRODUCT_CODE": _effective_presence(mode, "ACCOUNT_PRODUCT_CODE"),
    }
    account_key = effective["ACCOUNT_NUMBER"]["selected_env"]
    product_key = effective["ACCOUNT_PRODUCT_CODE"]["selected_env"]
    account_number = os.environ.get(str(account_key or ""), "").strip()
    product_code = os.environ.get(str(product_key or ""), "").strip()
    live_env = os.environ.get("ELEPHANT_LIVE_ENABLED", "").strip().lower()
    exec_cfg = config_load("risk_config.yaml", "execution") or {}
    guards = {
        "kis_mode_virtual": mode == "virtual",
        "real_account_not_selected": mode != "real",
        "elephant_live_enabled_false": live_env != "true",
        "risk_config_live_enabled_false": not safe_bool(
            exec_cfg.get("live_enabled", False),
            default=False,
        ),
        "account_number_shape_ok": len(account_number) == 8 and account_number.isdigit(),
        "product_code_shape_ok": len(product_code) == 2 and product_code.isdigit(),
    }
    status = (
        "PASS"
        if mode and all(item["present"] for item in effective.values()) and all(guards.values())
        else "FAIL"
    )
    return {
        "status": status,
        "required_kis_env": required,
        "effective_kis_env": effective,
        "guards": guards,
    }


def main() -> int:
    report = build_report()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
