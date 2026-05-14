from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import kis_paper_account_diagnose  # noqa: E402


def test_kis_paper_account_diagnosis_requires_balance_and_buyable_pass() -> None:
    status, diagnosis = kis_paper_account_diagnose._diagnose_probes(  # noqa: SLF001
        [
            {
                "product_code": "01",
                "balance": {"status": "FAIL", "msg_cd": ""},
                "buyable": {"status": "PASS"},
            },
        ],
        {"status": "PASS", "jti_matches_selected_app_key": True},
    )

    assert status == "FAIL"
    assert diagnosis["any_read_only_account_endpoint_passed"] is True
    assert diagnosis["all_required_read_only_account_endpoints_passed"] is False
    assert diagnosis["likely_root_cause"] == "partial_or_endpoint_specific_error"


def test_kis_paper_account_diagnosis_ready_product_code() -> None:
    status, diagnosis = kis_paper_account_diagnose._diagnose_probes(  # noqa: SLF001
        [
            {
                "product_code": "01",
                "balance": {"status": "PASS"},
                "buyable": {"status": "PASS"},
            },
        ],
        {"status": "PASS", "jti_matches_selected_app_key": True},
    )

    assert status == "PASS"
    assert diagnosis["ready_product_codes"] == ["01"]
    assert diagnosis["likely_root_cause"] == "paper_account_ready"


def test_kis_paper_account_diagnosis_invalid_account_binding() -> None:
    status, diagnosis = kis_paper_account_diagnose._diagnose_probes(  # noqa: SLF001
        [
            {
                "product_code": "01",
                "balance": {"status": "FAIL", "msg_cd": "OPSQ2000"},
                "buyable": {"status": "FAIL", "msg_cd": "OPSQ2000"},
            },
        ],
        {"status": "PASS", "jti_matches_selected_app_key": True},
    )

    assert status == "FAIL"
    assert diagnosis["all_probes_invalid_check_acno"] is True
    assert (
        diagnosis["likely_root_cause"]
        == "kis_app_key_account_binding_or_non_openapi_mock_account"
    )
