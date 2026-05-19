"""Mode B 22:00 배포 게이트. atomic swap + rollback_on_failure.

C14 ModeBDeployer: 검증 통과 번들만 Hot Path에 반영.

설계 원칙:
  - PIT-Safety: 18:00 KST 이후 Mode B 전용. 장중 호출 차단.
  - FDA can_change_weight=false: Deployer는 모델/팩터 교체만. 비중 결정 없음.
  - LLM 미호출: deterministic 배포 엔진.
  - 하드코딩 금지: 모든 임계값은 risk_config.yaml backtest_agent.deploy_decision_gate 경유.
  - atomic swap: os.replace() (POSIX 원자성 보장). 실패 시 자동 rollback.
  - Mode B 전용: @mode_b_only 데코레이터 강제.

artifacts 경로 구조:
  artifacts/bundles/{bundle_id}/alpha_factor/factor_zoo.jsonl
  artifacts/bundles/{bundle_id}/lgbm/latest_model.pkl
  artifacts/bundles/{bundle_id}/lgbm/committee.pkl
  artifacts/bundles/{bundle_id}/ppo/latest_policy.pkl
  artifacts/backup/{deploy_id}/factor_zoo.jsonl.bak
  artifacts/backup/{deploy_id}/model_registry/lightgbm.pkl.bak
  artifacts/backup/{deploy_id}/model_registry/committee.pkl.bak
  artifacts/backup/{deploy_id}/ppo_policy.pkl.bak
  artifacts/backup/{deploy_id}/agent_constraints.yaml.bak  (있다면)
  artifacts/backup/{deploy_id}/metadata.json
  artifacts/regression_cases/{rgc_id}.jsonl
  artifacts/dead_letter_log.jsonl
"""
from __future__ import annotations

import json
import os
import shutil
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.models.registry import ModelRegistry, _DEPLOY_ACTIVATION_TOKEN
from src.mode_b.service_policy_verifier import verify_service_policy_evidence
from src.utils.config_loader import load as config_load
from src.utils.id_factory import generate_deploy_id, generate_regression_case_id
from src.utils.logger import get_logger
from src.utils.mode_guard import mode_b_only

logger = get_logger("ModeBDeployer")
_KST = ZoneInfo("Asia/Seoul")

# ────────────────────────────────────────────────────────────────────
# 커스텀 예외
# ────────────────────────────────────────────────────────────────────

class DeployBlocked(RuntimeError):
    """배포 차단 예외. verdict/regression severity/sanity 검증 실패 시."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(f"[DEPLOY_BLOCKED:{reason}] {detail}")


class DeployRollbackFailed(RuntimeError):
    """rollback 중 일부 복원 실패 시 raise."""

    def __init__(self, failed_components: list[str]) -> None:
        self.failed_components = failed_components
        super().__init__(
            f"[DEPLOY_ROLLBACK_FAILED] 복원 실패 컴포넌트: {failed_components}"
        )


class PartialDeployRollback(RuntimeError):
    """atomic swap 중간 실패 후 rollback 완료. 일부 컴포넌트만 swap된 채로 rollback됨."""

    def __init__(self, failed_step: str, rolled_back: list[str]) -> None:
        self.failed_step = failed_step
        self.rolled_back = rolled_back
        super().__init__(
            f"[PARTIAL_DEPLOY_ROLLBACK] step='{failed_step}' 실패 후 rollback 완료. "
            f"복원 완료 컴포넌트: {rolled_back}"
        )


# ────────────────────────────────────────────────────────────────────
# regression_risk 입력 데이터클래스
# ────────────────────────────────────────────────────────────────────

@dataclass
class RegressionRisk:
    """BacktestEngine 또는 Validation이 생성하는 회귀 리스크 정보."""

    flagged: bool = False
    severity: str = "low"          # low | medium | high
    # P1 fix (2026-05-09): C12 schema (api_contracts.md L885) evidence: [string] 정합.
    # 이전 dict 타입 → list[str] 로 변경. ic_drop / ic_diff 등 수치는 문자열 직렬화.
    evidence: list[str] = field(default_factory=list)
    snapshot_metrics: dict[str, Any] = field(default_factory=dict)


# ────────────────────────────────────────────────────────────────────
# ModeBDeployer
# ────────────────────────────────────────────────────────────────────

_ARTIFACTS_ROOT = Path(__file__).resolve().parents[3] / "artifacts"

# 심각도 순위 (비교용)
_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}

# 배포 대상 컴포넌트 정의 (순서 = swap 순서)
_COMPONENT_PATHS = {
    "factor_zoo": (
        "alpha_factor/factor_zoo.jsonl",       # source (bundle 내 신규)
        "alpha_factor/factor_zoo.jsonl",       # dest  (live)
        "factor_zoo.jsonl.bak",                # backup 파일명
    ),
    "lgbm_model": (
        "lgbm/latest_model.pkl",
        "lgbm/latest_model.pkl",
        "model_registry/lightgbm.pkl.bak",
    ),
    "committee_model": (
        "lgbm/committee.pkl",
        "lgbm/committee.pkl",
        "model_registry/committee.pkl.bak",
    ),
    "ppo_policy": (
        "ppo/latest_policy.pkl",
        "ppo/latest_policy.pkl",
        "ppo_policy.pkl.bak",
    ),
}

_OPTIONAL_COMPONENT_PATH = {
    "agent_constraints": (
        "co_steer/agent_constraints.yaml",
        "co_steer/agent_constraints.yaml",
        "agent_constraints.yaml.bak",
    ),
}

def _active_service_policy_universe() -> list[str]:
    cfg = config_load("risk_config.yaml", "backtest_agent") or {}
    gate_cfg = cfg.get("deploy_decision_gate", {}).get("final_dataset_gate", {}) or {}
    universe_cfg = config_load("universe_config.yaml") or {}
    include_pending = bool(gate_cfg.get("include_pending_data_tickers", False))
    allowed_stock = {"active"}
    allowed_sector = {"confirmed"}
    if include_pending:
        allowed_stock = {
            str(s)
            for s in gate_cfg.get("allowed_stock_statuses", ["active", "pending_data"])
        }
        allowed_sector = {
            str(s)
            for s in gate_cfg.get(
                "allowed_sector_statuses",
                ["confirmed", "confirmed_pending_data"],
            )
        }
    tickers: list[str] = []
    for sector in (universe_cfg.get("sectors") or {}).values():
        if not isinstance(sector, dict):
            continue
        if str(sector.get("status")) not in allowed_sector:
            continue
        for stock in sector.get("stocks", []) or []:
            if not isinstance(stock, dict):
                continue
            if str(stock.get("status")) in allowed_stock:
                ticker = str(stock.get("ticker", "")).zfill(6)
                if ticker != "000000":
                    tickers.append(ticker)
    return sorted(dict.fromkeys(tickers))

class ModeBDeployer:
    """C14 ModeBDeployer: 22:00 배포 게이트 실구현.

    역할:
      - verdict=pass 번들만 Hot Path 아티팩트에 반영 (atomic swap).
      - verdict=warn 시 DeployBlocked("operator_review_required") raise.
      - verdict=fail 시 DeployBlocked("verdict_fail") raise + dead_letter_log.
      - regression severity >= regression_severity_block 시 DeployBlocked raise.
      - sanity_check_result != "ok" 시 DeployBlocked raise.
      - 각 swap 단계 실패 시 이전 단계 rollback 자동 수행.
      - regression_risk.flagged=True 이면 RegressionCase 생성 + JSONL 저장.

    하드코딩 금지:
      모든 임계값은 risk_config.yaml backtest_agent.deploy_decision_gate 경유.

    Args:
        artifacts_root: artifacts 디렉토리 경로. None 이면 프로젝트 기본값.
    """

    def __init__(self, artifacts_root: Path | None = None) -> None:
        self._root = artifacts_root or _ARTIFACTS_ROOT
        self._gate_cfg: dict[str, Any] = config_load(
            "risk_config.yaml",
            "backtest_agent.deploy_decision_gate",
        )
        logger.info(
            "[ModeBDeployer] 배포 게이트 설정 로드 완료: verdict_required=%s regression_severity_block=%s",
            self._gate_cfg.get("verdict_required"),
            self._gate_cfg.get("regression_severity_block"),
        )

    # ────────────────────────────────────────────────────
    # public API
    # ────────────────────────────────────────────────────

    @mode_b_only
    def deploy(
        self,
        bundle_id: str,
        backtest_verdict: str,
        sanity_check_result: str,
        regression_risk: RegressionRisk | None = None,
        service_policy_evidence: dict[str, Any] | None = None,
        feature_quality: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """검증된 번들을 Hot Path 아티팩트에 atomic swap 배포.

        Args:
            bundle_id: BUNDLE-{yyyymmdd}-{uuid8} (S3-7 생성).
            backtest_verdict: "pass" | "warn" | "fail" (C12 verdict).
            sanity_check_result: "ok" (leakage_check + smoke_test 통합 결과).
            regression_risk: RegressionRisk 객체. None 이면 RegressionRisk() 기본값.
            service_policy_evidence: service-policy replay evidence. Production
                root deploy requires PASS.
            feature_quality: C12 feature coverage telemetry. Production root
                deploy requires configured non-neutral coverage.

        Returns:
            배포 결과 딕셔너리 (deploy_id, bundle_id, deployed_at, swapped_components,
            rollback_required, regression_case_id, verdict, human_approval_required).

        Raises:
            DeployBlocked: verdict != "pass" / severity >= block / sanity != "ok".
            PartialDeployRollback: swap 중간 실패 후 rollback 완료.
            DeployRollbackFailed: rollback 도중 일부 복원 실패.
        """
        if regression_risk is None:
            regression_risk = RegressionRisk()

        deploy_id = generate_deploy_id()
        logger.info(
            "[ModeBDeployer] 배포 시작: deploy_id=%s bundle_id=%s verdict=%s",
            deploy_id, bundle_id, backtest_verdict,
        )

        # ── 1. Pre-condition 검증 ──────────────────────────────────────
        self._check_preconditions(backtest_verdict, sanity_check_result, regression_risk)

        # ── 2. Verdict 분기 ───────────────────────────────────────────
        self._check_verdict(backtest_verdict, deploy_id)

        # ── 3. Regression risk gate ───────────────────────────────────
        if regression_risk.flagged:
            rgc_id = self._create_regression_case(
                deploy_id=deploy_id,
                bundle_id=bundle_id,
                regression_risk=regression_risk,
            )
            raise DeployBlocked(
                "regression_risk_flagged",
                (
                    f"regression_risk.flagged=True severity={regression_risk.severity!r} "
                    f"regression_case_id={rgc_id}"
                ),
            )

        # ── 3b. C14 extended deploy gates ──────────────────────────────
        # Unit tests and isolated temp roots may call deploy() directly. The
        # production artifact root must still fail closed unless the caller
        # supplies C12 feature-quality and service-policy evidence.
        if self._uses_production_root():
            self._check_feature_quality_gate(feature_quality or {})
            self._check_service_policy_gate(service_policy_evidence or {}, bundle_id=bundle_id)

        # ── 4. Atomic swap ────────────────────────────────────────────
        self._validate_candidate_bundle(bundle_id)
        self._check_lgbm_version_collision(bundle_id)
        previous_active_version = self._current_lgbm_active_version()
        backup_dir = self._root / "backup" / deploy_id
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "model_registry").mkdir(parents=True, exist_ok=True)

        swapped: list[str] = []
        activated_model_version: str | None = None
        try:
            swapped = self._atomic_swap_all(bundle_id, backup_dir)
            activated_model_version = self._activate_lgbm_registry(bundle_id)
        except Exception as swap_err:
            logger.warning("[ModeBDeployer] swap 실패: %s. rollback 시작. swapped=%s", swap_err, swapped)
            rolled_back = self._rollback_components(swapped, backup_dir)
            try:
                self._restore_lgbm_registry(previous_active_version)
            except Exception as registry_err:
                logger.warning("[ModeBDeployer] registry rollback 실패: %s", registry_err)
            raise PartialDeployRollback(
                failed_step=str(swap_err),
                rolled_back=rolled_back,
            ) from swap_err

        # ── 5. metadata.json 저장 ─────────────────────────────────────
        self._save_backup_metadata(
            backup_dir,
            deploy_id,
            bundle_id,
            swapped,
            previous_active_version=previous_active_version,
            activated_model_version=activated_model_version,
        )

        deployed_at = datetime.now(_KST).isoformat()
        logger.info(
            "[ModeBDeployer] 배포 완료: deploy_id=%s swapped=%s rgc_id=%s deployed_at=%s",
            deploy_id, swapped, None, deployed_at,
        )

        on_pass = self._gate_cfg.get("on_pass", {})
        return {
            "deploy_id": deploy_id,
            "bundle_id": bundle_id,
            "deployed_at": deployed_at,
            "swapped_components": swapped,
            "rollback_required": False,
            "regression_case_id": None,
            "verdict": backtest_verdict,
            "human_approval_required": on_pass.get("human_approval_required", False),
            "activated_model_version": activated_model_version,
        }

    @mode_b_only
    def rollback(self, deploy_id: str) -> dict[str, Any]:
        """이전 배포를 롤백. backup 디렉토리의 .bak 파일로 복원.

        Args:
            deploy_id: DEPLOY-{yyyymmdd}-{uuid8} (deploy() 반환값).

        Returns:
            rollback 결과 딕셔너리 (deploy_id, status, restored_components, rolled_back_at).

        Raises:
            DeployRollbackFailed: 일부 컴포넌트 복원 실패 시.
        """
        logger.info("[ModeBDeployer] rollback 시작: deploy_id=%s", deploy_id)
        backup_dir = self._root / "backup" / deploy_id

        if not backup_dir.exists():
            raise DeployRollbackFailed([f"backup 디렉토리 없음: {backup_dir}"])

        # metadata.json에서 swap 목록 로드
        metadata_path = backup_dir / "metadata.json"
        if not metadata_path.exists():
            raise DeployRollbackFailed([f"backup metadata 없음: {metadata_path}"])

        with metadata_path.open("r", encoding="utf-8") as fh:
            metadata = json.load(fh)

        swapped_components = metadata.get("swapped_components", [])
        restored = self._rollback_components(swapped_components, backup_dir)
        if "previous_active_version" in metadata:
            self._restore_lgbm_registry(metadata.get("previous_active_version"))

        rolled_back_at = datetime.now(_KST).isoformat()
        logger.info("[ModeBDeployer] rollback 완료: deploy_id=%s restored=%s", deploy_id, restored)
        return {
            "deploy_id": deploy_id,
            "status": "rolled_back",
            "restored_components": restored,
            "rolled_back_at": rolled_back_at,
        }

    # ────────────────────────────────────────────────────
    # private: pre-condition 검증
    # ────────────────────────────────────────────────────

    def _check_preconditions(
        self,
        backtest_verdict: str,
        sanity_check_result: str,
        regression_risk: RegressionRisk,
    ) -> None:
        """sanity_check_result 및 regression_risk 배포 조건 검증."""
        if sanity_check_result != "ok":
            raise DeployBlocked(
                "sanity_check_failed",
                f"sanity_check_result={sanity_check_result!r} (필요값: 'ok')",
            )

    def _check_feature_quality_gate(self, feature_quality: dict[str, Any]) -> None:
        """Block production deploy when Phase 2 feature coverage is missing/neutral."""
        gate_cfg = self._gate_cfg.get("feature_quality_gate", {}) or {}
        min_dual = float(gate_cfg.get("min_dual_source_non_neutral_row_coverage", 0.8))
        min_exog = float(gate_cfg.get("min_exogenous_non_neutral_row_coverage", 0.8))
        dual_rows = int(feature_quality.get("dual_source_rows", 0) or 0)
        dual_non_neutral = int(feature_quality.get("dual_source_non_neutral_rows", 0) or 0)
        exog_rows = int(feature_quality.get("exogenous_rows", 0) or 0)
        exog_non_neutral = int(feature_quality.get("exogenous_non_neutral_rows", 0) or 0)
        dual_rate = dual_non_neutral / max(dual_rows, 1)
        exog_rate = exog_non_neutral / max(exog_rows, 1)
        if dual_rows <= 0 or exog_rows <= 0 or dual_rate < min_dual or exog_rate < min_exog:
            raise DeployBlocked(
                "feature_quality_gate_failed",
                (
                    f"dual_source_non_neutral={dual_non_neutral}/{dual_rows} "
                    f"exogenous_non_neutral={exog_non_neutral}/{exog_rows}"
                ),
            )


    def _check_service_policy_gate(self, evidence: dict[str, Any], *, bundle_id: str) -> None:
        """Block production deploy unless service-policy replay is PASS."""
        verification = verify_service_policy_evidence(
            evidence,
            bundle_id=bundle_id,
            repo_root=self._repo_root_for_evidence(),
            expected_date_range=(
                evidence.get("service_policy_expected_date_range")
                or evidence.get("date_range")
            ),
            expected_universe=_active_service_policy_universe(),
        )
        if not verification.passed:
            raise DeployBlocked(
                "service_policy_gate_failed",
                (
                    f"service_policy_status={evidence.get('status')!r} "
                    f"blockers={verification.blockers}"
                ),
            )

    def _repo_root_for_evidence(self) -> Path:
        if self._root.name == "artifacts":
            return self._root.parent
        return self._root

    def _uses_production_root(self) -> bool:
        """Return True when this deployer points at the live project artifacts root."""
        try:
            return self._root.resolve(strict=False) == _ARTIFACTS_ROOT.resolve(strict=False)
        except OSError:
            return self._root == _ARTIFACTS_ROOT

    def _check_verdict(self, backtest_verdict: str, deploy_id: str) -> None:
        """verdict 분기. warn/fail 시 DeployBlocked raise."""
        if backtest_verdict == "pass":
            return

        if backtest_verdict == "warn":
            on_warn = self._gate_cfg.get("on_warn", {})
            action = on_warn.get("action", "operator_review")
            raise DeployBlocked(
                "operator_review_required",
                f"verdict=warn → action={action!r}. operator 수동 승인 필요.",
            )

        if backtest_verdict == "fail":
            on_fail = self._gate_cfg.get("on_fail", {})
            self._write_dead_letter_log(deploy_id, backtest_verdict, on_fail)
            raise DeployBlocked(
                "verdict_fail",
                f"verdict=fail → block_deploy. baseline_action={on_fail.get('baseline_action', 'hold')!r}",
            )

        # 알 수 없는 verdict
        raise DeployBlocked(
            "unknown_verdict",
            f"verdict={backtest_verdict!r}. 허용값: pass | warn | fail",
        )

    # ────────────────────────────────────────────────────
    # private: atomic swap
    # ────────────────────────────────────────────────────

    def _atomic_swap_all(self, bundle_id: str, backup_dir: Path) -> list[str]:
        """4단계 순차 swap. 각 단계 실패 시 즉시 Exception raise."""
        swapped: list[str] = []
        bundle_root = self._bundle_root(bundle_id)

        # Step A-D: 주요 컴포넌트
        for component_name, (src_rel, dest_rel, bak_name) in _COMPONENT_PATHS.items():
            if component_name == "ppo_policy" and not (bundle_root / src_rel).exists():
                logger.info("[ModeBDeployer] ppo_policy 후보 없음. 기존 PPO/heuristic 유지.")
                continue
            self._swap_single(
                component_name=component_name,
                src_path=bundle_root / src_rel,
                dest_path=self._root / dest_rel,
                backup_path=backup_dir / bak_name,
            )
            swapped.append(component_name)

        # Step E: agent_constraints (있다면)
        src_rel_opt, dest_rel_opt, bak_name_opt = _OPTIONAL_COMPONENT_PATH["agent_constraints"]
        src_path_opt = bundle_root / src_rel_opt
        if src_path_opt.exists():
            self._swap_single(
                component_name="agent_constraints",
                src_path=src_path_opt,
                dest_path=self._root / dest_rel_opt,
                backup_path=backup_dir / bak_name_opt,
            )
            swapped.append("agent_constraints")

        return swapped

    def _bundle_root(self, bundle_id: str) -> Path:
        """검증 완료 candidate bundle staging root."""
        return self._root / "bundles" / bundle_id

    def _validate_candidate_bundle(self, bundle_id: str) -> None:
        """live 파일을 건드리기 전에 candidate bundle 존재/무결성을 검증."""
        bundle_root = self._bundle_root(bundle_id)
        problems: list[str] = []

        for component_name, (src_rel, dest_rel, _bak_name) in _COMPONENT_PATHS.items():
            src_path = bundle_root / src_rel
            dest_path = self._root / dest_rel
            if component_name == "ppo_policy" and not src_path.exists():
                continue
            if src_path.resolve() == dest_path.resolve():
                problems.append(f"{component_name}: source_equals_dest:{src_path}")
                continue
            if not src_path.is_file():
                problems.append(f"{component_name}: missing:{src_path}")
                continue
            if src_path.stat().st_size <= 0:
                problems.append(f"{component_name}: empty:{src_path}")

        lgbm_metadata_path = bundle_root / "lgbm" / "latest_model_metadata.json"
        if not lgbm_metadata_path.is_file():
            problems.append(f"lgbm_model_metadata: missing:{lgbm_metadata_path}")
        elif lgbm_metadata_path.stat().st_size <= 0:
            problems.append(f"lgbm_model_metadata: empty:{lgbm_metadata_path}")
        else:
            try:
                with lgbm_metadata_path.open("r", encoding="utf-8") as fh:
                    lgbm_metadata = json.load(fh)
                if not str(lgbm_metadata.get("version", "")).strip():
                    problems.append("lgbm_model_metadata: missing_version")
                if lgbm_metadata.get("bundle_id") != bundle_id:
                    problems.append(
                        "lgbm_model_metadata: bundle_id_mismatch:"
                        f"{lgbm_metadata.get('bundle_id')!r}"
                    )
            except json.JSONDecodeError as e:
                problems.append(f"lgbm_model_metadata: invalid_json:{e}")

        src_rel_opt, dest_rel_opt, _bak_name_opt = _OPTIONAL_COMPONENT_PATH["agent_constraints"]
        src_path_opt = bundle_root / src_rel_opt
        if src_path_opt.exists():
            dest_path_opt = self._root / dest_rel_opt
            if src_path_opt.resolve() == dest_path_opt.resolve():
                problems.append(f"agent_constraints: source_equals_dest:{src_path_opt}")
            elif not src_path_opt.is_file():
                problems.append(f"agent_constraints: not_file:{src_path_opt}")
            elif src_path_opt.stat().st_size <= 0:
                problems.append(f"agent_constraints: empty:{src_path_opt}")

        if problems:
            raise DeployBlocked(
                "candidate_artifact_invalid",
                (
                    f"bundle_id={bundle_id} candidate artifact 검증 실패. "
                    + "; ".join(problems)
                ),
            )

    def _current_lgbm_active_version(self) -> str | None:
        registry_path = self._root / "lgbm" / "registry.json"
        if not registry_path.exists():
            return None
        try:
            with registry_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            active = data.get("active_version")
            return str(active) if active else None
        except Exception as e:
            logger.warning("[ModeBDeployer] registry active_version 읽기 실패: %s", e)
            return None

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _check_lgbm_version_collision(self, bundle_id: str) -> None:
        """기존 production version이 다른 bundle/model이면 fail-closed."""
        bundle_root = self._bundle_root(bundle_id)
        metadata_path = bundle_root / "lgbm" / "latest_model_metadata.json"
        with metadata_path.open("r", encoding="utf-8") as fh:
            metadata = json.load(fh)
        version = str(metadata.get("version", "")).strip()
        if not version:
            return

        live_lgbm_dir = self._root / "lgbm"
        version_pkl = live_lgbm_dir / f"{version}.pkl"
        version_meta = live_lgbm_dir / f"{version}_metadata.json"
        if not version_pkl.exists():
            return

        candidate_pkl = bundle_root / "lgbm" / "latest_model.pkl"
        if not candidate_pkl.is_file():
            return

        if self._sha256_file(version_pkl) != self._sha256_file(candidate_pkl):
            raise DeployBlocked(
                "lgbm_version_collision",
                (
                    f"bundle_id={bundle_id} version={version} already exists "
                    "with different model bytes"
                ),
            )

        if not version_meta.is_file():
            raise DeployBlocked(
                "lgbm_version_collision",
                (
                    f"bundle_id={bundle_id} version={version} already exists "
                    "without metadata"
                ),
            )

        try:
            with version_meta.open("r", encoding="utf-8") as fh:
                live_metadata = json.load(fh)
        except json.JSONDecodeError as e:
            raise DeployBlocked(
                "lgbm_version_collision",
                f"bundle_id={bundle_id} version={version} metadata invalid_json:{e}",
            ) from e

        live_bundle_id = live_metadata.get("bundle_id")
        if live_bundle_id and live_bundle_id != bundle_id:
            raise DeployBlocked(
                "lgbm_version_collision",
                (
                    f"bundle_id={bundle_id} version={version} already belongs "
                    f"to bundle_id={live_bundle_id!r}"
                ),
            )

    def _activate_lgbm_registry(self, bundle_id: str) -> str:
        metadata_path = self._bundle_root(bundle_id) / "lgbm" / "latest_model_metadata.json"
        with metadata_path.open("r", encoding="utf-8") as fh:
            metadata = json.load(fh)
        version = str(metadata.get("version", "")).strip()
        if not version:
            raise DeployBlocked(
                "candidate_artifact_invalid",
                f"bundle_id={bundle_id} lgbm metadata version 없음",
            )
        live_lgbm_dir = self._root / "lgbm"
        live_lgbm_dir.mkdir(parents=True, exist_ok=True)
        version_pkl = live_lgbm_dir / f"{version}.pkl"
        version_meta = live_lgbm_dir / f"{version}_metadata.json"
        if not version_pkl.exists():
            shutil.copy2(live_lgbm_dir / "latest_model.pkl", version_pkl)
        metadata["model_path"] = str(version_pkl)
        metadata["metadata_path"] = str(version_meta)
        with version_meta.open("w", encoding="utf-8") as fh:
            json.dump(metadata, fh, ensure_ascii=False, indent=2)
        ModelRegistry(artifacts_dir=live_lgbm_dir).activate_deployed_candidate(
            version,
            deploy_token=_DEPLOY_ACTIVATION_TOKEN,
        )
        return version

    def _restore_lgbm_registry(self, previous_active_version: str | None) -> None:
        ModelRegistry(artifacts_dir=self._root / "lgbm").restore_active_version(
            previous_active_version,
            clear_latest_pointer=False,
        )

    def _swap_single(
        self,
        component_name: str,
        src_path: Path,
        dest_path: Path,
        backup_path: Path,
    ) -> None:
        """단일 컴포넌트 atomic swap.

        1. dest_path 존재 시 backup_path로 백업 (os.replace).
        2. src_path 존재 시 dest_path로 atomic swap.
        3. src_path를 dest_path로 atomic replace.
        """
        if src_path.resolve() == dest_path.resolve():
            raise ValueError(
                f"{component_name}: candidate source and live dest are identical: {src_path}"
            )
        if not src_path.is_file():
            raise FileNotFoundError(f"{component_name}: candidate artifact 없음: {src_path}")
        if src_path.stat().st_size <= 0:
            raise ValueError(f"{component_name}: candidate artifact 비어 있음: {src_path}")

        backup_path.parent.mkdir(parents=True, exist_ok=True)

        # backup 단계
        if dest_path.exists():
            os.replace(str(dest_path), str(backup_path))
            logger.info("[ModeBDeployer] 백업 완료: %s → %s", component_name, backup_path)
        else:
            # 원래 live 파일이 없던 상태를 rollback에서 복원하기 위한 marker.
            backup_path.write_bytes(b"")
            logger.info("[ModeBDeployer] 백업 스킵(파일 없음): %s → 빈 .bak 생성", component_name)

        # atomic swap 단계
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(src_path), str(dest_path))
        logger.info("[ModeBDeployer] swap 완료: %s → %s", component_name, dest_path)

    def _rollback_components(self, components: list[str], backup_dir: Path) -> list[str]:
        """지정된 컴포넌트 역순 rollback. 실패 시 DeployRollbackFailed raise."""
        failed: list[str] = []
        restored: list[str] = []

        all_paths = {**_COMPONENT_PATHS, **_OPTIONAL_COMPONENT_PATH}
        # 역순 복원
        for component_name in reversed(components):
            if component_name not in all_paths:
                continue
            _, dest_rel, bak_name = all_paths[component_name]
            dest_path = self._root / dest_rel
            backup_path = backup_dir / bak_name

            try:
                if backup_path.exists() and backup_path.stat().st_size > 0:
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(str(backup_path), str(dest_path))
                    restored.append(component_name)
                    logger.info("[ModeBDeployer] 복원 완료: %s", component_name)
                else:
                    # 빈 .bak = 원래 없던 파일. dest도 삭제하여 원상 복귀
                    if dest_path.exists():
                        dest_path.unlink()
                    if backup_path.exists():
                        backup_path.unlink()
                    restored.append(component_name)
                    logger.info("[ModeBDeployer] 복원(삭제): %s (원래 없던 파일)", component_name)
            except Exception as e:
                failed.append(component_name)
                logger.warning("[ModeBDeployer] 복원 실패: %s: %s", component_name, e)

        if failed:
            raise DeployRollbackFailed(failed)

        return restored

    # ────────────────────────────────────────────────────
    # private: metadata / regression_case / dead_letter
    # ────────────────────────────────────────────────────

    def _save_backup_metadata(
        self,
        backup_dir: Path,
        deploy_id: str,
        bundle_id: str,
        swapped_components: list[str],
        previous_active_version: str | None = None,
        activated_model_version: str | None = None,
    ) -> None:
        """backup 디렉토리에 metadata.json 저장."""
        metadata = {
            "deploy_id": deploy_id,
            "bundle_id": bundle_id,
            "swapped_at": datetime.now(_KST).isoformat(),
            "swapped_components": swapped_components,
            "previous_active_version": previous_active_version,
            "activated_model_version": activated_model_version,
        }
        metadata_path = backup_dir / "metadata.json"
        with metadata_path.open("w", encoding="utf-8") as fh:
            json.dump(metadata, fh, ensure_ascii=False, indent=2)
        logger.info("[ModeBDeployer] backup metadata 저장: %s", metadata_path)

    def _create_regression_case(
        self,
        deploy_id: str,
        bundle_id: str,
        regression_risk: RegressionRisk,
    ) -> str:
        """RegressionCase 생성 + artifacts/regression_cases/{rgc_id}.jsonl 저장.

        Returns:
            rgc_id (RGC-{yyyymmdd}-{uuid8}).
        """
        rgc_id = generate_regression_case_id()
        case_dir = self._root / "regression_cases"
        case_dir.mkdir(parents=True, exist_ok=True)
        case_path = case_dir / f"{rgc_id}.jsonl"

        payload = {
            "case_id": rgc_id,
            "occurred_at": datetime.now(_KST).isoformat(),
            "bundle_id": bundle_id,
            "deploy_id": deploy_id,
            "evidence": regression_risk.evidence,
            "severity": regression_risk.severity,
            "snapshot_metrics": regression_risk.snapshot_metrics,
        }
        with case_path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

        logger.info("[ModeBDeployer] RegressionCase 저장: %s", case_path)
        return rgc_id

    def _write_dead_letter_log(
        self,
        deploy_id: str,
        verdict: str,
        on_fail_cfg: dict[str, Any],
    ) -> None:
        """verdict=fail 시 dead_letter_log.jsonl append 기록."""
        log_to = on_fail_cfg.get("log_to", "dead_letter_log")
        log_path = self._root / f"{log_to}.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        entry = {
            "deploy_id": deploy_id,
            "verdict": verdict,
            "recorded_at": datetime.now(_KST).isoformat(),
            "baseline_action": on_fail_cfg.get("baseline_action", "hold"),
        }
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

        logger.info("[ModeBDeployer] dead_letter_log 기록: %s", log_path)
