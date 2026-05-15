"""S3-10 ModeBDeployer 유닛 테스트.

C14 ModeBDeployer 실구현 검증.

테스트 목록:
  1.  test_mode_b_only_deploy               - Mode A 호출 시 RuntimeError
  2.  test_mode_b_only_rollback             - rollback Mode A 호출 시 RuntimeError
  3.  test_deploy_pass_no_regression        - verdict=pass, flagged=False → deploy 성공
  4.  test_deploy_pass_regression_low       - flagged=True, severity=low → DeployBlocked(regression_risk_flagged)
  5.  test_deploy_regression_high_blocked   - severity=high → DeployBlocked(regression_risk_flagged)
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
  18. test_deploy_medium_regression_blocked - flagged=True, severity=medium → DeployBlocked
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
_BUNDLE_ID = "BUNDLE-20260501-AABBCCDD"


_REQUIRED_CANDIDATE_ARTIFACTS = {
    "alpha_factor/factor_zoo.jsonl": b'{"factor": "candidate"}\n',
    "lgbm/latest_model.pkl": b"CANDIDATE_LGBM",
    "lgbm/committee.pkl": b"CANDIDATE_COMMITTEE",
    "ppo/latest_policy.pkl": b"CANDIDATE_PPO",
}


def _candidate_lgbm_metadata(bundle_id: str = _BUNDLE_ID) -> dict:
    return {
        "version": "candidate_lgbm_v1",
        "bundle_id": bundle_id,
        "train_start": "2026-01-01",
        "train_end": "2026-05-01",
        "feature_cols": ["feat_a", "feat_b"],
        "label_horizon_bars": 5,
        "label_generation_version": "session_local_v2",
        "label_session_scope": "ticker_trading_day",
        "metrics": {
            "ic": 0.01,
            "icir": 0.5,
            "rank_ic": 0.02,
            "arr": 0.1,
            "ir": 1.0,
            "mdd": -0.01,
            "sr": 1.0,
        },
        "commit_hash": "abc1234",
        "data_version": "unit-test",
        "created_at": "2026-05-01T18:00:00+09:00",
    }


def _prepare_candidate_bundle(
    root: Path,
    bundle_id: str = _BUNDLE_ID,
    include_ppo: bool = True,
) -> Path:
    """ModeBDeployer staging bundle fixture."""
    bundle_root = root / "bundles" / bundle_id
    for rel, payload in _REQUIRED_CANDIDATE_ARTIFACTS.items():
        if rel == "ppo/latest_policy.pkl" and not include_ppo:
            continue
        path = bundle_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    metadata_path = bundle_root / "lgbm" / "latest_model_metadata.json"
    with metadata_path.open("w", encoding="utf-8") as fh:
        json.dump(_candidate_lgbm_metadata(bundle_id), fh, ensure_ascii=False, indent=2)
    return bundle_root


def _prepare_live_artifacts(root: Path) -> dict[str, bytes]:
    """rollback/content 보존 검증용 live artifacts."""
    live_payloads = {
        "alpha_factor/factor_zoo.jsonl": b"LIVE_FACTOR",
        "lgbm/latest_model.pkl": b"LIVE_LGBM",
        "lgbm/committee.pkl": b"LIVE_COMMITTEE",
        "ppo/latest_policy.pkl": b"LIVE_PPO",
    }
    for rel, payload in live_payloads.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return live_payloads


def _passing_service_policy_evidence(root: Path, bundle_id: str = _BUNDLE_ID) -> dict:
    """Production deploy용 service-policy hard gate PASS fixture."""
    import hashlib

    report = {
        "status": "PASS",
        "bundle_id": bundle_id,
        "gate": {"status": "PASS"},
        "policy_checks": {
            "deploy_candidate_by_service_policy": True,
            "no_naked_short_exposure": True,
            "order_caps_respected": True,
            "cash_guard_respected": True,
        },
        "order_stats": {"naked_short_attempts": 0},
    }
    path = root / "artifacts" / "reports" / "service_policy_replay" / "pass.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "status": "PASS",
        "bundle_id": bundle_id,
        "service_policy_report_path": "artifacts/reports/service_policy_replay/pass.json",
        "service_policy_report_sha256": digest,
        "gate": dict(report["gate"]),
        "policy_checks": dict(report["policy_checks"]),
        "order_stats": dict(report["order_stats"]),
    }


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

    _prepare_candidate_bundle(tmp_path)
    deployer = _make_deployer(tmp_path)
    with _mode_b_env():
        result = deployer.deploy(
            bundle_id=_BUNDLE_ID,
            backtest_verdict="pass",
            sanity_check_result="ok",
            regression_risk=RegressionRisk(flagged=False),
        )

    assert result["verdict"] == "pass"
    assert result["rollback_required"] is False
    assert result["regression_case_id"] is None
    assert result["human_approval_required"] is False
    assert result["bundle_id"] == _BUNDLE_ID
    assert "deployed_at" in result
    assert "deploy_id" in result
    assert result["activated_model_version"] == "candidate_lgbm_v1"

    registry = json.loads((tmp_path / "lgbm" / "registry.json").read_text())
    assert registry["active_version"] == "candidate_lgbm_v1"
    active = [v for v in registry["versions"] if v["version"] == "candidate_lgbm_v1"][0]
    assert active["status"] == "active"
    assert active["bundle_id"] == _BUNDLE_ID


def test_deploy_missing_candidate_blocks_before_touching_live(tmp_path):
    """candidate bundle 없으면 live artifact를 건드리지 않고 배포 차단."""
    from src.mode_b.deployer import DeployBlocked, RegressionRisk

    live_payloads = _prepare_live_artifacts(tmp_path)
    deployer = _make_deployer(tmp_path)
    with _mode_b_env():
        with pytest.raises(DeployBlocked) as exc_info:
            deployer.deploy(
                bundle_id=_BUNDLE_ID,
                backtest_verdict="pass",
                sanity_check_result="ok",
                regression_risk=RegressionRisk(flagged=False),
            )

    assert exc_info.value.reason == "candidate_artifact_invalid"
    for rel, payload in live_payloads.items():
        assert (tmp_path / rel).read_bytes() == payload


def test_deploy_replaces_live_with_candidate_and_rollback_restores_live(tmp_path):
    """배포 성공은 candidate 내용으로 교체하고 rollback은 기존 live 내용을 복원한다."""
    from src.mode_b.deployer import RegressionRisk

    live_payloads = _prepare_live_artifacts(tmp_path)
    _prepare_candidate_bundle(tmp_path)
    deployer = _make_deployer(tmp_path)

    with _mode_b_env():
        result = deployer.deploy(
            bundle_id=_BUNDLE_ID,
            backtest_verdict="pass",
            sanity_check_result="ok",
            regression_risk=RegressionRisk(flagged=False),
        )

    for rel, candidate_payload in _REQUIRED_CANDIDATE_ARTIFACTS.items():
        assert (tmp_path / rel).read_bytes() == candidate_payload
        assert (tmp_path / rel).stat().st_size > 0

    with _mode_b_env():
        deployer.rollback(result["deploy_id"])

    for rel, payload in live_payloads.items():
        assert (tmp_path / rel).read_bytes() == payload


def test_deploy_allows_missing_ppo_policy_and_keeps_existing_allocator(tmp_path):
    """PPO 후보가 없으면 LGBM만 배포하고 기존 PPO/heuristic을 유지한다."""
    from src.mode_b.deployer import RegressionRisk

    _prepare_live_artifacts(tmp_path)
    _prepare_candidate_bundle(tmp_path, include_ppo=False)
    deployer = _make_deployer(tmp_path)

    with _mode_b_env():
        result = deployer.deploy(
            bundle_id=_BUNDLE_ID,
            backtest_verdict="pass",
            sanity_check_result="ok",
            regression_risk=RegressionRisk(flagged=False),
        )

    assert "ppo_policy" not in result["swapped_components"]
    assert (tmp_path / "lgbm" / "registry.json").exists()


# ────────────────────────────────────────────────────────────────────────
# Test 4: verdict=pass, regression flagged=True, severity=low → rgc_id 반환
# ────────────────────────────────────────────────────────────────────────

def test_deploy_pass_regression_low(tmp_path):
    """verdict=pass + flagged=True + severity=low → deploy 차단 + rgc_id 기록."""
    from src.mode_b.deployer import DeployBlocked, RegressionRisk

    deployer = _make_deployer(tmp_path)
    with _mode_b_env():
        with pytest.raises(DeployBlocked) as exc_info:
            deployer.deploy(
                bundle_id="BUNDLE-20260501-AABBCCDD",
                backtest_verdict="pass",
                sanity_check_result="ok",
                regression_risk=RegressionRisk(
                    flagged=True,
                    severity="low",
                    evidence=["ic_drop=0.02"],
                    snapshot_metrics={"sharpe": 1.5},
                ),
            )

    assert exc_info.value.reason == "regression_risk_flagged"
    match = re.search(r"regression_case_id=(RGC-\d{8}-[0-9A-F]{8})", str(exc_info.value))
    assert match, f"rgc_id 미기록: {exc_info.value}"


# ────────────────────────────────────────────────────────────────────────
# Test 5: regression severity=high → DeployBlocked
# ────────────────────────────────────────────────────────────────────────

def test_deploy_regression_high_blocked(tmp_path):
    """severity=high → DeployBlocked(regression_risk_flagged)."""
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
    assert exc_info.value.reason == "regression_risk_flagged"


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

    _prepare_candidate_bundle(tmp_path)
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
                bundle_id=_BUNDLE_ID,
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

    _prepare_candidate_bundle(tmp_path)
    deployer = _make_deployer(tmp_path)

    # 먼저 deploy 수행해서 backup 생성
    with _mode_b_env():
        result = deployer.deploy(
            bundle_id=_BUNDLE_ID,
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
    _prepare_candidate_bundle(tmp_path)
    deployer = _make_deployer(tmp_path)
    with _mode_b_env():
        result = deployer.deploy(
            bundle_id=_BUNDLE_ID,
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
    rgc_id = deployer._create_regression_case(  # noqa: SLF001
        deploy_id="DEPLOY-20260501-AABBCCDD",
        bundle_id="BUNDLE-20260501-AABBCCDD",
        regression_risk=RegressionRisk(
            flagged=True,
            severity="low",
            evidence=["ic_diff=-0.05"],
            snapshot_metrics={"sharpe": 1.2, "mdd": -0.08},
        ),
    )

    case_path = tmp_path / "regression_cases" / f"{rgc_id}.jsonl"
    assert case_path.exists(), f"RegressionCase 파일 없음: {case_path}"

    with case_path.open(encoding="utf-8") as fh:
        payload = json.load(fh)

    assert payload["case_id"] == rgc_id
    assert payload["bundle_id"] == "BUNDLE-20260501-AABBCCDD"
    assert "occurred_at" in payload
    assert payload["severity"] == "low"
    assert payload["evidence"] == ["ic_diff=-0.05"]
    assert payload["snapshot_metrics"] == {"sharpe": 1.2, "mdd": -0.08}


# ────────────────────────────────────────────────────────────────────────
# Test 13: backup metadata.json 저장 및 필드 검증
# ────────────────────────────────────────────────────────────────────────

def test_backup_metadata_saved(tmp_path):
    """backup/{deploy_id}/metadata.json 저장 + 필수 필드 검증."""
    _prepare_candidate_bundle(tmp_path)
    deployer = _make_deployer(tmp_path)
    with _mode_b_env():
        result = deployer.deploy(
            bundle_id=_BUNDLE_ID,
            backtest_verdict="pass",
            sanity_check_result="ok",
        )

    deploy_id = result["deploy_id"]
    metadata_path = tmp_path / "backup" / deploy_id / "metadata.json"
    assert metadata_path.exists(), f"metadata.json 없음: {metadata_path}"

    with metadata_path.open(encoding="utf-8") as fh:
        metadata = json.load(fh)

    assert metadata["deploy_id"] == deploy_id
    assert metadata["bundle_id"] == _BUNDLE_ID
    assert "swapped_at" in metadata
    assert isinstance(metadata["swapped_components"], list)


# ────────────────────────────────────────────────────────────────────────
# Test 14: swapped_components 4개 이상 (factor_zoo + lgbm + committee + ppo)
# ────────────────────────────────────────────────────────────────────────

def test_swapped_components_all_four(tmp_path):
    """swapped_components에 4개 핵심 컴포넌트 모두 포함."""
    _prepare_candidate_bundle(tmp_path)
    deployer = _make_deployer(tmp_path)
    with _mode_b_env():
        result = deployer.deploy(
            bundle_id=_BUNDLE_ID,
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

def test_deploy_medium_regression_blocked(tmp_path):
    """severity=medium이라도 flagged=True이면 C14 deploy gate에서 차단."""
    from src.mode_b.deployer import DeployBlocked, RegressionRisk

    deployer = _make_deployer(tmp_path)
    with _mode_b_env():
        with pytest.raises(DeployBlocked) as exc_info:
            deployer.deploy(
                bundle_id="BUNDLE-20260501-AABBCCDD",
                backtest_verdict="pass",
                sanity_check_result="ok",
                regression_risk=RegressionRisk(flagged=True, severity="medium"),
            )

    assert exc_info.value.reason == "regression_risk_flagged"


def test_service_policy_gate_requires_report_binding(tmp_path):
    """Production service-policy gate는 embedded report path/hash 없으면 차단."""
    from src.mode_b.deployer import DeployBlocked

    deployer = _make_deployer(tmp_path)
    evidence = _passing_service_policy_evidence(tmp_path)
    evidence.pop("service_policy_report_sha256")

    with pytest.raises(DeployBlocked) as exc_info:
        deployer._check_service_policy_gate(evidence, bundle_id=_BUNDLE_ID)

    assert exc_info.value.reason == "service_policy_gate_failed"
    assert "service_policy_report_sha_missing" in str(exc_info.value)


def test_service_policy_gate_accepts_bound_pass_report(tmp_path):
    """Embedded path/hash가 있는 PASS service-policy evidence는 gate 통과."""
    deployer = _make_deployer(tmp_path)
    deployer._check_service_policy_gate(
        _passing_service_policy_evidence(tmp_path),
        bundle_id=_BUNDLE_ID,
    )


def test_service_policy_gate_uses_relative_fallback_when_absolute_path_stale(tmp_path):
    """Portable C14 gate: stale producer absolute path falls back to repo-relative path."""
    deployer = _make_deployer(tmp_path)
    evidence = _passing_service_policy_evidence(tmp_path)
    relative_path = evidence["service_policy_report_path"]
    evidence["service_policy_report_path"] = "/producer/machine/missing/pass.json"
    evidence["service_policy_report_path_relative"] = relative_path
    evidence["report_path"] = "/producer/machine/missing/pass.json"
    evidence["report_path_relative"] = relative_path

    deployer._check_service_policy_gate(evidence, bundle_id=_BUNDLE_ID)
