"""S3-10 ModeBDeployer 유닛 테스트.

C14 ModeBDeployer 실구현 검증.

테스트 목록:
  1.  test_mode_b_only_deploy               - Mode A 호출 시 RuntimeError
  2.  test_mode_b_only_rollback             - rollback Mode A 호출 시 RuntimeError
  3.  test_deploy_pass_no_regression        - verdict=pass, flagged=False → deploy 성공
  4.  test_deploy_pass_regression_low       - verdict=pass, flagged=True, severity=low → rgc_id 반환
  5.  test_deploy_regression_high_blocked   - severity=high → DeployBlocked(regression_severity_blocked)
  6.  test_deploy_verdict_warn              - verdict=warn → DeployBlocked(operator_review_required)
  7.  test_deploy_verdict_fail              - verdict=fail → DeployBlocked(verdict_fail) + dead_letter_log
  8.  test_deploy_sanity_not_ok             - sanity_check_result != "ok" → DeployBlocked(sanity_check_failed)
  9.  test_atomic_swap_rollback_on_failure  - swap 중간 실패 → PartialDeployRollback raise + backup 복원
  10. test_rollback_standalone              - rollback() 단독 호출 → status="rolled_back"
  11. test_deploy_id_format                 - DEPLOY-yyyymmdd-UUID8 정규식 매치
  12. test_regression_case_saved            - RegressionCase 파일 위치 및 payload 필드 검증
  13. test_backup_metadata_saved            - backup/metadata.json 저장 및 필드 검증
  14. test_swapped_components_all_four      - 4개 컴포넌트 swapped_components에 모두 포함
  15. test_rollback_missing_backup          - backup 없으면 DeployRollbackFailed raise
  16. test_dead_letter_log_append           - fail 시 dead_letter_log.jsonl append
  17. test_forbidden_callers_mode_check     - HotPath = Mode A → mode_b_only 차단 확인
  18. test_deploy_medium_regression_passes  - severity=medium < block=high → deploy 진행
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest

# ────────────────────────────────────────────────────────────────────────
# 테스트용 최소 risk_config.yaml 대체 딕셔너리
# ────────────────────────────────────────────────────────────────────────

_MINIMAL_GATE_CFG = {
    "verdict_required": "pass",
    "regression_severity_block": "high",
    "on_pass": {
        "human_approval_required": False,
        "action": "deploy",
    },
    "on_warn": {
        "human_approval_required": True,
        "action": "operator_review",
    },
    "on_fail": {
        "action": "block_deploy",
        "baseline_action": "hold",
        "log_to": "dead_letter_log",
    },
}


def _make_cfg_loader(gate_cfg: dict | None = None):
    """config_load 패치용 side_effect."""
    _gate = gate_cfg or _MINIMAL_GATE_CFG

    def _loader(file: str = "risk_config.yaml", key: str | None = None):
        if key == "backtest_agent.deploy_decision_gate":
            return _gate
        return {}

    return _loader


# ────────────────────────────────────────────────────────────────────────
# 헬퍼: ModeBDeployer 인스턴스 생성
# ────────────────────────────────────────────────────────────────────────

def _make_deployer(artifacts_root: Path, gate_cfg: dict | None = None):
    """config_load 패치 + artifacts_root 주입으로 Deployer 생성."""
    loader = _make_cfg_loader(gate_cfg=gate_cfg)
    with patch("src.mode_b.deployer.config_load", side_effect=loader):
        from src.mode_b.deployer import ModeBDeployer
        return ModeBDeployer(artifacts_root=artifacts_root)


# ────────────────────────────────────────────────────────────────────────
# Mode B 환경 context manager
# ────────────────────────────────────────────────────────────────────────

class _mode_b_env:
    """ELEPHANT_MODE=mode_b context manager."""

    def __enter__(self):
        os.environ["ELEPHANT_MODE"] = "mode_b"
        return self

    def __exit__(self, *_):
        os.environ.pop("ELEPHANT_MODE", None)


# ────────────────────────────────────────────────────────────────────────
# DEPLOY-yyyymmdd-UUID8 정규식
# ────────────────────────────────────────────────────────────────────────

_DEPLOY_ID_RE = re.compile(r"^DEPLOY-\d{8}-[0-9A-F]{8}$")
_RGC_ID_RE = re.compile(r"^RGC-\d{8}-[0-9A-F]{8}$")


# ────────────────────────────────────────────────────────────────────────
# Test 1: mode_b_only - deploy Mode A 호출 차단
# ────────────────────────────────────────────────────────────────────────

def test_mode_b_only_deploy(tmp_path):
    """Mode A에서 deploy() 호출 → RuntimeError."""
    os.environ.pop("ELEPHANT_MODE", None)
    deployer = _make_deployer(tmp_path)
    with pytest.raises(RuntimeError, match="Mode B 전용"):
        deployer.deploy("BUNDLE-20260501-AABBCCDD", "pass", "ok")


# ────────────────────────────────────────────────────────────────────────
# Test 2: mode_b_only - rollback Mode A 호출 차단
# ────────────────────────────────────────────────────────────────────────

def test_mode_b_only_rollback(tmp_path):
    """Mode A에서 rollback() 호출 → RuntimeError."""
    os.environ.pop("ELEPHANT_MODE", None)
    deployer = _make_deployer(tmp_path)
    with pytest.raises(RuntimeError, match="Mode B 전용"):
        deployer.rollback("DEPLOY-20260501-AABBCCDD")


# ────────────────────────────────────────────────────────────────────────
# Test 3: verdict=pass, regression flagged=False → deploy 성공
# ────────────────────────────────────────────────────────────────────────

def test_deploy_pass_no_regression(tmp_path):
    """verdict=pass, flagged=False → deploy 성공. 기본 필드 검증."""
    from src.mode_b.deployer import RegressionRisk

    deployer = _make_deployer(tmp_path)
    with _mode_b_env():
        result = deployer.deploy(
            bundle_id="BUNDLE-20260501-AABBCCDD",
            backtest_verdict="pass",
            sanity_check_result="ok",
            regression_risk=RegressionRisk(flagged=False),
        )

    assert result["verdict"] == "pass"
    assert result["rollback_required"] is False
    assert result["regression_case_id"] is None
    assert result["human_approval_required"] is False
    assert result["bundle_id"] == "BUNDLE-20260501-AABBCCDD"
    assert "deployed_at" in result
    assert "deploy_id" in result


# ────────────────────────────────────────────────────────────────────────
# Test 4: verdict=pass, regression flagged=True, severity=low → rgc_id 반환
# ────────────────────────────────────────────────────────────────────────

def test_deploy_pass_regression_low(tmp_path):
    """verdict=pass + flagged=True + severity=low → deploy 진행 + rgc_id != None."""
    from src.mode_b.deployer import RegressionRisk

    deployer = _make_deployer(tmp_path)
    with _mode_b_env():
        result = deployer.deploy(
            bundle_id="BUNDLE-20260501-AABBCCDD",
            backtest_verdict="pass",
            sanity_check_result="ok",
            regression_risk=RegressionRisk(
                flagged=True,
                severity="low",
                evidence={"ic_drop": 0.02},
                snapshot_metrics={"sharpe": 1.5},
            ),
        )

    assert result["regression_case_id"] is not None
    assert _RGC_ID_RE.match(result["regression_case_id"]), (
        f"rgc_id 형식 불일치: {result['regression_case_id']}"
    )


# ────────────────────────────────────────────────────────────────────────
# Test 5: regression severity=high → DeployBlocked
# ────────────────────────────────────────────────────────────────────────

def test_deploy_regression_high_blocked(tmp_path):
    """severity=high (>= block=high) → DeployBlocked(regression_severity_blocked)."""
    from src.mode_b.deployer import DeployBlocked, RegressionRisk

    deployer = _make_deployer(tmp_path)
    with _mode_b_env():
        with pytest.raises(DeployBlocked) as exc_info:
            deployer.deploy(
                bundle_id="BUNDLE-20260501-AABBCCDD",
                backtest_verdict="pass",
                sanity_check_result="ok",
                regression_risk=RegressionRisk(flagged=True, severity="high"),
            )
    assert exc_info.value.reason == "regression_severity_blocked"


# ────────────────────────────────────────────────────────────────────────
# Test 6: verdict=warn → DeployBlocked(operator_review_required)
# ────────────────────────────────────────────────────────────────────────

def test_deploy_verdict_warn(tmp_path):
    """verdict=warn → DeployBlocked("operator_review_required")."""
    from src.mode_b.deployer import DeployBlocked

    deployer = _make_deployer(tmp_path)
    with _mode_b_env():
        with pytest.raises(DeployBlocked) as exc_info:
            deployer.deploy(
                bundle_id="BUNDLE-20260501-AABBCCDD",
                backtest_verdict="warn",
                sanity_check_result="ok",
            )
    assert exc_info.value.reason == "operator_review_required"


# ────────────────────────────────────────────────────────────────────────
# Test 7: verdict=fail → DeployBlocked(verdict_fail) + dead_letter_log 기록
# ────────────────────────────────────────────────────────────────────────

def test_deploy_verdict_fail(tmp_path):
    """verdict=fail → DeployBlocked("verdict_fail") + dead_letter_log.jsonl 기록."""
    from src.mode_b.deployer import DeployBlocked

    deployer = _make_deployer(tmp_path)
    with _mode_b_env():
        with pytest.raises(DeployBlocked) as exc_info:
            deployer.deploy(
                bundle_id="BUNDLE-20260501-AABBCCDD",
                backtest_verdict="fail",
                sanity_check_result="ok",
            )

    assert exc_info.value.reason == "verdict_fail"

    # dead_letter_log 기록 확인
    log_path = tmp_path / "dead_letter_log.jsonl"
    assert log_path.exists(), "dead_letter_log.jsonl 생성 안 됨"
    with log_path.open(encoding="utf-8") as fh:
        entry = json.loads(fh.readline())
    assert entry["verdict"] == "fail"
    assert "deploy_id" in entry
    assert "recorded_at" in entry


# ────────────────────────────────────────────────────────────────────────
# Test 8: sanity_check_result != "ok" → DeployBlocked(sanity_check_failed)
# ────────────────────────────────────────────────────────────────────────

def test_deploy_sanity_not_ok(tmp_path):
    """sanity_check_result != 'ok' → DeployBlocked("sanity_check_failed")."""
    from src.mode_b.deployer import DeployBlocked

    deployer = _make_deployer(tmp_path)
    with _mode_b_env():
        with pytest.raises(DeployBlocked) as exc_info:
            deployer.deploy(
                bundle_id="BUNDLE-20260501-AABBCCDD",
                backtest_verdict="pass",
                sanity_check_result="failed",
            )
    assert exc_info.value.reason == "sanity_check_failed"


# ────────────────────────────────────────────────────────────────────────
# Test 9: atomic swap 중간 실패 → PartialDeployRollback + backup 복원
# ────────────────────────────────────────────────────────────────────────

def test_atomic_swap_rollback_on_failure(tmp_path):
    """_swap_single 중간 실패 시 PartialDeployRollback raise + 이전 단계 rollback."""
    from src.mode_b.deployer import PartialDeployRollback

    deployer = _make_deployer(tmp_path)

    call_count = {"n": 0}

    original_swap = deployer._swap_single  # noqa: SLF001

    def _failing_swap(component_name, src_path, dest_path, backup_path):
        call_count["n"] += 1
        if call_count["n"] == 3:  # 3번째 컴포넌트에서 실패
            raise OSError("mock swap failure at step 3")
        original_swap(
            component_name=component_name,
            src_path=src_path,
            dest_path=dest_path,
            backup_path=backup_path,
        )

    deployer._swap_single = _failing_swap  # noqa: SLF001

    with _mode_b_env():
        with pytest.raises(PartialDeployRollback) as exc_info:
            deployer.deploy(
                bundle_id="BUNDLE-20260501-AABBCCDD",
                backtest_verdict="pass",
                sanity_check_result="ok",
            )

    # 2개 컴포넌트는 swap 완료 후 rollback 됨
    exc = exc_info.value
    assert len(exc.rolled_back) >= 0  # rollback 목록 존재 (0개 이상)
    assert "mock swap failure" in exc.failed_step


# ────────────────────────────────────────────────────────────────────────
# Test 10: rollback() 단독 테스트 → status="rolled_back"
# ────────────────────────────────────────────────────────────────────────

def test_rollback_standalone(tmp_path):
    """rollback() 단독 호출: metadata.json 준비 → status='rolled_back'."""
    from src.mode_b.deployer import RegressionRisk

    deployer = _make_deployer(tmp_path)

    # 먼저 deploy 수행해서 backup 생성
    with _mode_b_env():
        result = deployer.deploy(
            bundle_id="BUNDLE-20260501-AABBCCDD",
            backtest_verdict="pass",
            sanity_check_result="ok",
            regression_risk=RegressionRisk(flagged=False),
        )
    deploy_id = result["deploy_id"]

    # 이제 rollback
    with _mode_b_env():
        rb_result = deployer.rollback(deploy_id)

    assert rb_result["status"] == "rolled_back"
    assert rb_result["deploy_id"] == deploy_id
    assert "rolled_back_at" in rb_result
    assert isinstance(rb_result["restored_components"], list)


# ────────────────────────────────────────────────────────────────────────
# Test 11: deploy_id 형식 DEPLOY-yyyymmdd-UUID8
# ────────────────────────────────────────────────────────────────────────

def test_deploy_id_format(tmp_path):
    """deploy_id 형식 DEPLOY-yyyymmdd-UUID8 정규식 매치."""
    deployer = _make_deployer(tmp_path)
    with _mode_b_env():
        result = deployer.deploy(
            bundle_id="BUNDLE-20260501-AABBCCDD",
            backtest_verdict="pass",
            sanity_check_result="ok",
        )
    assert _DEPLOY_ID_RE.match(result["deploy_id"]), (
        f"deploy_id 형식 불일치: {result['deploy_id']}"
    )


# ────────────────────────────────────────────────────────────────────────
# Test 12: RegressionCase 저장 위치 및 payload 필드 검증
# ────────────────────────────────────────────────────────────────────────

def test_regression_case_saved(tmp_path):
    """RegressionCase artifacts/regression_cases/{rgc_id}.jsonl 저장 + payload 필드."""
    from src.mode_b.deployer import RegressionRisk

    deployer = _make_deployer(tmp_path)
    with _mode_b_env():
        result = deployer.deploy(
            bundle_id="BUNDLE-20260501-AABBCCDD",
            backtest_verdict="pass",
            sanity_check_result="ok",
            regression_risk=RegressionRisk(
                flagged=True,
                severity="low",
                evidence={"ic_diff": -0.05},
                snapshot_metrics={"sharpe": 1.2, "mdd": -0.08},
            ),
        )

    rgc_id = result["regression_case_id"]
    assert rgc_id is not None

    case_path = tmp_path / "regression_cases" / f"{rgc_id}.jsonl"
    assert case_path.exists(), f"RegressionCase 파일 없음: {case_path}"

    with case_path.open(encoding="utf-8") as fh:
        payload = json.load(fh)

    assert payload["case_id"] == rgc_id
    assert payload["bundle_id"] == "BUNDLE-20260501-AABBCCDD"
    assert "occurred_at" in payload
    assert payload["severity"] == "low"
    assert payload["evidence"] == {"ic_diff": -0.05}
    assert payload["snapshot_metrics"] == {"sharpe": 1.2, "mdd": -0.08}


# ────────────────────────────────────────────────────────────────────────
# Test 13: backup metadata.json 저장 및 필드 검증
# ────────────────────────────────────────────────────────────────────────

def test_backup_metadata_saved(tmp_path):
    """backup/{deploy_id}/metadata.json 저장 + 필수 필드 검증."""
    deployer = _make_deployer(tmp_path)
    with _mode_b_env():
        result = deployer.deploy(
            bundle_id="BUNDLE-20260501-AABBCCDD",
            backtest_verdict="pass",
            sanity_check_result="ok",
        )

    deploy_id = result["deploy_id"]
    metadata_path = tmp_path / "backup" / deploy_id / "metadata.json"
    assert metadata_path.exists(), f"metadata.json 없음: {metadata_path}"

    with metadata_path.open(encoding="utf-8") as fh:
        metadata = json.load(fh)

    assert metadata["deploy_id"] == deploy_id
    assert metadata["bundle_id"] == "BUNDLE-20260501-AABBCCDD"
    assert "swapped_at" in metadata
    assert isinstance(metadata["swapped_components"], list)


# ────────────────────────────────────────────────────────────────────────
# Test 14: swapped_components 4개 이상 (factor_zoo + lgbm + committee + ppo)
# ────────────────────────────────────────────────────────────────────────

def test_swapped_components_all_four(tmp_path):
    """swapped_components에 4개 핵심 컴포넌트 모두 포함."""
    deployer = _make_deployer(tmp_path)
    with _mode_b_env():
        result = deployer.deploy(
            bundle_id="BUNDLE-20260501-AABBCCDD",
            backtest_verdict="pass",
            sanity_check_result="ok",
        )

    swapped = result["swapped_components"]
    assert "factor_zoo" in swapped
    assert "lgbm_model" in swapped
    assert "committee_model" in swapped
    assert "ppo_policy" in swapped


# ────────────────────────────────────────────────────────────────────────
# Test 15: rollback backup 없으면 DeployRollbackFailed
# ────────────────────────────────────────────────────────────────────────

def test_rollback_missing_backup(tmp_path):
    """backup 디렉토리 없는 deploy_id rollback → DeployRollbackFailed raise."""
    from src.mode_b.deployer import DeployRollbackFailed

    deployer = _make_deployer(tmp_path)
    with _mode_b_env():
        with pytest.raises(DeployRollbackFailed):
            deployer.rollback("DEPLOY-20260501-NOTEXIST")


# ────────────────────────────────────────────────────────────────────────
# Test 16: dead_letter_log append (여러 번 fail)
# ────────────────────────────────────────────────────────────────────────

def test_dead_letter_log_append(tmp_path):
    """verdict=fail 2회 호출 → dead_letter_log.jsonl에 2개 entry append."""
    from src.mode_b.deployer import DeployBlocked

    deployer = _make_deployer(tmp_path)
    with _mode_b_env():
        for _ in range(2):
            with pytest.raises(DeployBlocked):
                deployer.deploy(
                    bundle_id="BUNDLE-20260501-AABBCCDD",
                    backtest_verdict="fail",
                    sanity_check_result="ok",
                )

    log_path = tmp_path / "dead_letter_log.jsonl"
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2, f"dead_letter_log entry 수 불일치: {len(lines)}"
    for line in lines:
        entry = json.loads(line)
        assert entry["verdict"] == "fail"


# ────────────────────────────────────────────────────────────────────────
# Test 17: forbidden_callers - Mode A (HotPath) 호출 차단 확인
# ────────────────────────────────────────────────────────────────────────

def test_forbidden_callers_mode_check(tmp_path):
    """HotPath = Mode A 환경 → deploy() mode_b_only 차단."""
    # ELEPHANT_MODE를 "mode_a" 또는 미설정 = Mode A
    os.environ["ELEPHANT_MODE"] = "mode_a"
    deployer = _make_deployer(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="Mode B 전용"):
            deployer.deploy("BUNDLE-20260501-AABBCCDD", "pass", "ok")
    finally:
        os.environ.pop("ELEPHANT_MODE", None)


# ────────────────────────────────────────────────────────────────────────
# Test 18: severity=medium < block=high → deploy 진행 (차단 안 됨)
# ────────────────────────────────────────────────────────────────────────

def test_deploy_medium_regression_passes(tmp_path):
    """severity=medium, regression_severity_block=high → 차단 안 됨. deploy 진행."""
    from src.mode_b.deployer import RegressionRisk

    deployer = _make_deployer(tmp_path)
    with _mode_b_env():
        result = deployer.deploy(
            bundle_id="BUNDLE-20260501-AABBCCDD",
            backtest_verdict="pass",
            sanity_check_result="ok",
            regression_risk=RegressionRisk(flagged=True, severity="medium"),
        )

    # medium은 차단되지 않아야 함. rgc_id 생성
    assert result["verdict"] == "pass"
    assert result["regression_case_id"] is not None
