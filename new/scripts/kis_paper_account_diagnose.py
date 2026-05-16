#!/usr/bin/env python
"""KIS paper account read-only diagnostic.

This script never reads `.env` files and never prints secret values. It uses the
current process environment only, then probes read-only account endpoints from
the official KIS domestic stock order/account spec.
"""
from __future__ import annotations

import json
import os
import re
import sys
from base64 import urlsafe_b64decode
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.connectors.kis_rest import KISRestClient  # noqa: E402
from src.utils.auth import AuthManager  # noqa: E402
from src.utils.safe_cast import safe_bool  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")
_REPORT_DIR = ROOT / "artifacts" / "reports" / "kis_account_diagnostic"
_PSBL_ORDER_PATH = "/uapi/domestic-stock/v1/trading/inquire-psbl-order"
_PSBL_ORDER_TR = {"real": "TTTC8908R", "virtual": "VTTC8908R"}


def _mode_candidates(mode: str, suffix: str) -> list[str]:
    if mode == "virtual":
        return [f"KIS_PAPER_{suffix}", f"KIS_{suffix}"]
    if mode == "real":
        return [f"KIS_REAL_{suffix}", f"KIS_{suffix}"]
    return [f"KIS_{suffix}"]


def _selected_env(mode: str, suffix: str) -> tuple[str | None, str]:
    for key in _mode_candidates(mode, suffix):
        value = os.environ.get(key, "").strip()
        if value:
            return key, value
    return None, ""


def _mask_account(account_no: str) -> dict[str, Any]:
    return {
        "present": bool(account_no),
        "length": len(account_no),
        "last4": account_no[-4:] if len(account_no) >= 4 else "",
    }


def _extract_kis_error(error: Exception) -> dict[str, str]:
    text = str(error)
    msg_cd_match = re.search(r"msg_cd=([^ ]+)", text)
    msg_match = re.search(r"msg=(.+?)(?:$| last_err=)", text)
    return {
        "error": text,
        "msg_cd": msg_cd_match.group(1) if msg_cd_match else "",
        "msg": msg_match.group(1).strip() if msg_match else "",
    }


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    """Decode JWT payload without verifying signature; never return the raw token."""
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return {}
    payload_b64 = parts[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    decoded = urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8")
    payload = json.loads(decoded)
    return payload if isinstance(payload, dict) else {}


def _token_scope_probe(mode: str, selected_app_key: str) -> dict[str, Any]:
    """Official KIS sample compares JWT jti with paper_app/my_app to detect mode."""
    probe: dict[str, Any] = {
        "status": "FAIL",
        "decoded": False,
        "jti_present": False,
        "jti_matches_selected_app_key": False,
        "jti_matches_paper_app_key": False,
        "jti_matches_real_app_key": False,
        "jti_matches_generic_app_key": False,
    }
    try:
        token = AuthManager().get_kis_token()
        payload = _decode_jwt_payload(token)
        jti = str(payload.get("jti", ""))
        paper_key = os.environ.get("KIS_PAPER_APP_KEY", "").strip()
        real_key = os.environ.get("KIS_REAL_APP_KEY", "").strip()
        generic_key = os.environ.get("KIS_APP_KEY", "").strip()
        probe.update({
            "status": "PASS" if jti else "FAIL",
            "decoded": bool(payload),
            "claim_keys": sorted(str(key) for key in payload.keys()),
            "jti_present": bool(jti),
            "jti_length": len(jti),
            "jti_matches_selected_app_key": bool(selected_app_key and jti == selected_app_key),
            "jti_matches_paper_app_key": bool(paper_key and jti == paper_key),
            "jti_matches_real_app_key": bool(real_key and jti == real_key),
            "jti_matches_generic_app_key": bool(generic_key and jti == generic_key),
            "expected_mode": mode,
            "official_sample_rule": "paper if jwt.jti == paper_app, live if jwt.jti == my_app",
        })
    except Exception as e:
        probe.update({"error": str(e)})
    return probe


def _probe_balance(client: KISRestClient) -> dict[str, Any]:
    try:
        balance = client.get_balance()
        return {
            "status": "PASS",
            "cash_present": "cash" in dict(balance.get("balance") or {}),
            "position_count": len(balance.get("positions", [])),
        }
    except Exception as e:
        return {"status": "FAIL", **_extract_kis_error(e)}


def _probe_buyable(
    client: KISRestClient,
    *,
    account_no: str,
    product_code: str,
    ticker: str,
) -> dict[str, Any]:
    try:
        body = client._call_kis_get(  # noqa: SLF001 - diagnostic script for official spec probe
            path=_PSBL_ORDER_PATH,
            tr_id=_PSBL_ORDER_TR[client.mode],
            params={
                "CANO": account_no,
                "ACNT_PRDT_CD": product_code,
                "PDNO": ticker.zfill(6),
                "ORD_UNPR": "0",
                "ORD_DVSN": "01",
                "CMA_EVLU_AMT_ICLD_YN": "N",
                "OVRS_ICLD_YN": "N",
            },
        )
        output = body.get("output") if isinstance(body.get("output"), dict) else {}
        return {
            "status": "PASS",
            "ord_psbl_cash_present": "ord_psbl_cash" in output,
            "nrcvb_buy_amt_present": "nrcvb_buy_amt" in output,
        }
    except Exception as e:
        return {"status": "FAIL", **_extract_kis_error(e)}


def _diagnose_probes(
    probes: list[dict[str, Any]],
    token_scope: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    ready_product_codes = [
        str(probe.get("product_code", ""))
        for probe in probes
        if probe.get("balance", {}).get("status") == "PASS"
        and probe.get("buyable", {}).get("status") == "PASS"
    ]
    any_endpoint_pass = any(
        stage.get("status") == "PASS"
        for probe in probes
        for stage in (probe.get("balance", {}), probe.get("buyable", {}))
    )
    all_invalid_account = bool(probes) and all(
        (
            probe.get("balance", {}).get("msg_cd") == "OPSQ2000"
            and probe.get("buyable", {}).get("msg_cd") == "OPSQ2000"
        )
        for probe in probes
    )
    token_matches = safe_bool(token_scope.get("jti_matches_selected_app_key", False), default=False)

    if ready_product_codes:
        likely_root_cause = "paper_account_ready"
        next_action = "paper_trading_smoke balance/probe/order_history 진행 가능"
    elif all_invalid_account:
        likely_root_cause = "kis_app_key_account_binding_or_non_openapi_mock_account"
        next_action = (
            "KIS Developers app page에서 selected_app_key_env의 앱키와 "
            "모의투자 계좌 8-2 조합이 OpenAPI에 연결되어 있는지 확인"
        )
    elif token_scope.get("status") != "PASS" or not token_matches:
        likely_root_cause = "kis_auth_or_app_key_invalid"
        next_action = "선택된 KIS paper app key/secret과 token jti 일치 여부 확인"
    else:
        likely_root_cause = "partial_or_endpoint_specific_error"
        next_action = "FAIL stage의 msg_cd/msg를 기준으로 해당 엔드포인트 파라미터 확인"

    status = "PASS" if ready_product_codes else "FAIL"
    return status, {
        "any_read_only_account_endpoint_passed": any_endpoint_pass,
        "all_required_read_only_account_endpoints_passed": bool(ready_product_codes),
        "ready_product_codes": ready_product_codes,
        "all_probes_invalid_check_acno": all_invalid_account,
        "token_jti_matches_selected_app_key": token_matches,
        "likely_root_cause": likely_root_cause,
        "next_action": next_action,
    }


def build_report(ticker: str, product_codes: list[str]) -> dict[str, Any]:
    mode = os.environ.get("KIS_MODE", "virtual").strip().lower()
    app_key_env, app_key = _selected_env(mode, "APP_KEY")
    account_env, account_no = _selected_env(mode, "ACCOUNT_NUMBER")
    product_env, selected_product_code = _selected_env(mode, "ACCOUNT_PRODUCT_CODE")
    candidates: list[str] = []
    for code in [selected_product_code, *product_codes]:
        code_norm = str(code or "").strip()
        if len(code_norm) == 2 and code_norm.isdigit() and code_norm not in candidates:
            candidates.append(code_norm)

    report: dict[str, Any] = {
        "status": "FAIL",
        "generated_at": datetime.now(_KST).isoformat(),
        "runtime": {
            "kis_mode": mode,
            "selected_app_key_env": app_key_env,
            "selected_account_env": account_env,
            "selected_product_env": product_env,
            "app_key_present": bool(app_key),
            "account": _mask_account(account_no),
        },
        "token_scope": _token_scope_probe(mode, app_key),
        "official_spec_probes": [],
        "diagnosis": {},
    }

    for product_code in candidates:
        if mode == "virtual":
            os.environ["KIS_PAPER_ACCOUNT_PRODUCT_CODE"] = product_code
        elif mode == "real":
            os.environ["KIS_REAL_ACCOUNT_PRODUCT_CODE"] = product_code
        else:
            os.environ["KIS_ACCOUNT_PRODUCT_CODE"] = product_code

        client = KISRestClient()
        balance = _probe_balance(client)
        buyable = _probe_buyable(
            client,
            account_no=account_no,
            product_code=product_code,
            ticker=ticker,
        )
        probe = {
            "product_code": product_code,
            "balance": balance,
            "buyable": buyable,
        }
        report["official_spec_probes"].append(probe)

    report["status"], report["diagnosis"] = _diagnose_probes(
        report["official_spec_probes"],
        report.get("token_scope", {}),
    )
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="KIS paper account read-only diagnostic")
    parser.add_argument("--ticker", default="005930")
    parser.add_argument(
        "--product-codes",
        default="01,00,03,22,29",
        help="comma-separated ACNT_PRDT_CD candidates, selected env code is always tried first",
    )
    parser.add_argument("--no-write-report", action="store_true")
    args = parser.parse_args(argv)

    product_codes = [item.strip() for item in str(args.product_codes).split(",")]
    report = build_report(ticker=str(args.ticker), product_codes=product_codes)
    if not args.no_write_report:
        _REPORT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
        path = _REPORT_DIR / f"kis_account_diagnostic_{ts}.json"
        with path.open("w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        report["report_path"] = str(path)
        report["report_path_relative"] = str(path.relative_to(ROOT))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
