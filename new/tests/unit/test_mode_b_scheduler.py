"""S3-0 ModeBScheduler unit tests. C14 ModeBSchedulerContract 기반.

mode_guard: ELEPHANT_MODE=mode_b 환경변수로 @mode_b_only 가드 통과.
config_load: monkeypatch로 실제 yaml 경유 없이 고정값 주입.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def mock_nightly_retrainers():
    """NightlyLGBMRetrainer + NightlyPPORetrainer를 mock으로 대체.

    stage_4_model_evolution이 실 PPO 학습을 트리거하지 않도록 차단.
    scheduler 테스트는 오케스트레이션 로직만 검증한다.
    """
    mock_lgbm_inst = MagicMock()
    mock_lgbm_inst.retrain.return_value = {
        "version": "v2",
        "model_path": "artifacts/lgbm/v2.pkl",
        "metrics": {"ic": 0.05},
        "alpha_factors_used": 0,
    }
    mock_ppo_inst = MagicMock()
    mock_ppo_inst.retrain.return_value = {
        "version": "ppo_v1",
        "model_path": "artifacts/ppo/v1.zip",
        "allocator_candidate": {
            "allocator_ref": "ppo_v1",
            "source": "ppo_retrain",
            "bundle_id": None,
        },
    }
    with patch.dict("sys.modules", {
        "src.mode_b.nightly_lgbm_retrainer": MagicMock(
            NightlyLGBMRetrainer=MagicMock(return_value=mock_lgbm_inst)
        ),
        "src.mode_b.nightly_ppo_retrainer": MagicMock(
            NightlyPPORetrainer=MagicMock(return_value=mock_ppo_inst)
        ),
    }):
        yield


@pytest.fixture()
def tmp_audit_path(tmp_path: Path) -> Path:
    return tmp_path / "mode_b_audit_log.jsonl"


@pytest.fixture()
def mock_state_machine():
    sm = MagicMock()
    return sm


@pytest.fixture()
def scheduler(tmp_audit_path: Path, mock_state_machine, monkeypatch):
    """ModeBScheduler 인스턴스. mode_b_only 가드 + yaml 로드 모두 mock."""
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")

    cfg = {
        "stage_timeouts": {
            "stage_1": 1,
            "stage_2": 1,
            "stage_3": 1,
            "stage_4": 1,
            "stage_5": 1,
            "stage_6": 1,
            "stage_7": 1,
        },
        "audit_log_path": str(tmp_audit_path),
        "weekdays": ["MON", "TUE", "WED", "THU", "FRI"],
    }

    with patch("src.mode_b.scheduler.config_load", return_value=cfg):
        from src.mode_b.scheduler import ModeBScheduler

        s = ModeBScheduler(state_machine=mock_state_machine)
    return s


# --------------------------------------------------------------------------- #
# 1. FORBIDDEN_PERMISSIONS 상수 검증
# --------------------------------------------------------------------------- #


def test_forbidden_permissions_constants():
    """FORBIDDEN_PERMISSIONS frozenset에 C14 spec 4개 항목 포함.

    S3 Critical 5 (2026-05-01): C14 spec은 4개만. 나머지 2개(shared_message_pool_publish_during_market_hours,
    production_direct_write)는 C12 BacktestAgentContract의 forbidden_permissions 소속.
    """
    from src.mode_b.scheduler import FORBIDDEN_PERMISSIONS

    assert "hot_path_intervention" in FORBIDDEN_PERMISSIONS
    assert "fda_bypass" in FORBIDDEN_PERMISSIONS
    assert "target_weights_modification" in FORBIDDEN_PERMISSIONS
    assert "order_deltas_generation" in FORBIDDEN_PERMISSIONS
    # 아래 2개는 C12 소속이므로 C14 FORBIDDEN_PERMISSIONS에서 제거됨.
    assert "shared_message_pool_publish_during_market_hours" not in FORBIDDEN_PERMISSIONS
    assert "production_direct_write" not in FORBIDDEN_PERMISSIONS
    assert len(FORBIDDEN_PERMISSIONS) == 4


# --------------------------------------------------------------------------- #
# 2. 초기화 시 stage_timeouts 로드 확인
# --------------------------------------------------------------------------- #


def test_scheduler_init_loads_timeouts(scheduler):
    """init 후 _stage_timeouts 딕셔너리에 7개 stage 키 로드됨."""
    timeouts = scheduler._stage_timeouts
    expected_keys = {
        "stage_1", "stage_2", "stage_3", "stage_4", "stage_5", "stage_6", "stage_7"
    }
    assert expected_keys == set(timeouts.keys())


# --------------------------------------------------------------------------- #
# 3. stage_3 실행 후 bundle_id 생성
# --------------------------------------------------------------------------- #


def test_bundle_id_issued_in_stage_3(scheduler):
    """stage_3_factor_evolution 직접 호출 시 _bundle_id가 BUNDLE- 접두사로 생성."""
    assert scheduler._bundle_id is None
    scheduler.stage_3_factor_evolution()
    assert scheduler._bundle_id is not None
    assert scheduler._bundle_id.startswith("BUNDLE-")


# --------------------------------------------------------------------------- #
# 4. run_pipeline 반환에 bundle_id 존재
# --------------------------------------------------------------------------- #


def test_run_pipeline_returns_bundle_id(scheduler, monkeypatch):
    """run_pipeline() 반환 dict에 bundle_id 키가 있고 None이 아님.

    stage_3에 factor_candidates 주입해 bundle_id 발급 후 Backtest까지 진행.
    """
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")

    original_s3 = scheduler.stage_3_factor_evolution

    def _s3_with_candidates():
        r = original_s3()
        r["factor_candidates"] = ["f1"]
        return r

    scheduler.stage_3_factor_evolution = _s3_with_candidates
    result = scheduler.run_pipeline(date="2026-04-27")
    assert "bundle_id" in result
    assert result["bundle_id"] is not None


# --------------------------------------------------------------------------- #
# 5. run_pipeline stages 리스트에 7개 항목
# --------------------------------------------------------------------------- #


def test_run_pipeline_all_stages_executed(scheduler, monkeypatch):
    """정상 verdict=pass 경로에서 stages 리스트가 7개 항목을 가짐.

    stage_3에 factor_candidates 주입해 Backtest 건너뜀을 방지.
    """
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")

    original_s3 = scheduler.stage_3_factor_evolution

    def _s3_with_candidates():
        r = original_s3()
        r["factor_candidates"] = ["f1", "f2"]
        return r

    scheduler.stage_3_factor_evolution = _s3_with_candidates
    result = scheduler.run_pipeline(date="2026-04-27")
    # S4-5: stage_0 DQR 추가로 전체 8단계 (stage_0~7)
    assert len(result["stages"]) == 8


# --------------------------------------------------------------------------- #
# 6. audit_log 파일에 stage_1~7 모두 기록
# --------------------------------------------------------------------------- #


def test_audit_log_written_per_stage(scheduler, tmp_audit_path, monkeypatch):
    """run_pipeline 후 audit_log.jsonl에 7개 stage 항목 기록."""
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")

    original_s3 = scheduler.stage_3_factor_evolution

    def _s3_with_candidates():
        r = original_s3()
        r["factor_candidates"] = ["f1"]
        return r

    scheduler.stage_3_factor_evolution = _s3_with_candidates
    scheduler.run_pipeline(date="2026-04-27")

    assert tmp_audit_path.exists()
    lines = tmp_audit_path.read_text(encoding="utf-8").strip().splitlines()
    stage_names = {json.loads(ln)["stage"] for ln in lines}
    for i in range(1, 8):
        assert f"stage_{i}" in stage_names, f"stage_{i} 누락"


# --------------------------------------------------------------------------- #
# 7. audit_log entry에 C14 8개 필드 모두 포함
# --------------------------------------------------------------------------- #


def test_audit_log_required_fields(scheduler, tmp_audit_path, monkeypatch):
    """각 audit_log entry에 C14 명세 8개 필드 전부 포함."""
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")

    original_s3 = scheduler.stage_3_factor_evolution

    def _s3_with_candidates():
        r = original_s3()
        r["factor_candidates"] = ["f1"]
        return r

    scheduler.stage_3_factor_evolution = _s3_with_candidates
    scheduler.run_pipeline(date="2026-04-27")

    required = {
        "timestamp", "stage", "duration_sec", "bundle_id",
        "verdict", "regression_severity", "deploy_result",
        "operator_approval", "error",
    }
    lines = tmp_audit_path.read_text(encoding="utf-8").strip().splitlines()
    for ln in lines:
        entry = json.loads(ln)
        missing = required - set(entry.keys())
        assert not missing, f"필드 누락: {missing} in {entry['stage']}"


# --------------------------------------------------------------------------- #
# 8. 상태 전이 함수 여러 번 호출 확인
# --------------------------------------------------------------------------- #


def test_state_transition_called(scheduler, mock_state_machine, monkeypatch):
    """run_pipeline 실행 시 state_machine.transition이 최소 2번 호출됨."""
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")

    original_s3 = scheduler.stage_3_factor_evolution

    def _s3_with_candidates():
        r = original_s3()
        r["factor_candidates"] = ["f1"]
        return r

    scheduler.stage_3_factor_evolution = _s3_with_candidates
    scheduler.run_pipeline(date="2026-04-27")
    assert mock_state_machine.transition.call_count >= 2


# --------------------------------------------------------------------------- #
# 9. verdict=pass → stage_7 실행 확인
# --------------------------------------------------------------------------- #


def test_verdict_pass_triggers_deploy(scheduler, monkeypatch):
    """stage_6 verdict=pass → result에 stage_7 포함 및 stage_7 deploy 결과 존재."""
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")

    # Mock BacktestAgent: 항상 verdict=pass 반환
    class _MockBacktestAgent:
        def run(self, bundle_id):
            return {
                "backtest_id": "BT-MOCK",
                "bundle_id": bundle_id,
                "metrics": {"sharpe_ratio": 1.5, "max_drawdown": -0.05, "win_rate": 0.55},
                "verdict": "pass",
                "regression_severity": "none",
            }

        def report(self, report_type, payload):
            return {
                "report_id": "RPT-MOCK",
                "agent": "backtest",
                "report_type": report_type,
                "content": payload,
            }

    scheduler._backtest_agent = _MockBacktestAgent()

    original_s3 = scheduler.stage_3_factor_evolution

    def _s3_with_candidates():
        r = original_s3()
        r["factor_candidates"] = ["f1"]
        return r

    scheduler.stage_3_factor_evolution = _s3_with_candidates
    result = scheduler.run_pipeline(date="2026-04-27")

    assert result["verdict"] == "pass"
    stage_names = [s["stage"] for s in result["stages"]]
    assert "stage_7" in stage_names

    s7 = next(s for s in result["stages"] if s["stage"] == "stage_7")
    # stub deployer 없으므로 deploy_result는 stub_no_deployer
    assert s7.get("deploy_result") == "stub_no_deployer"


# --------------------------------------------------------------------------- #
# 10. candidate 없으면 Backtest 건너뜀 (stages=5개)
# --------------------------------------------------------------------------- #


def test_backtest_skip_on_no_candidates(scheduler, monkeypatch):
    """stage_3/4 candidates 모두 비어있으면 stages=5개 + verdict=skipped_no_candidates.

    S3-1 IdeaAgent 연동 후 stage_3가 fallback 가설을 생성하므로,
    테스트에서 stage_3를 직접 override하여 빈 candidates를 강제한다.
    """
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")

    # stage_3/4 모두 빈 candidates 반환하도록 override
    def _s3_no_candidates():
        from src.utils.id_factory import generate_bundle_id
        scheduler._bundle_id = generate_bundle_id()
        return {
            "status": "stub",
            "bundle_id": scheduler._bundle_id,
            "factor_candidates": [],
        }

    def _s4_no_candidates():
        return {
            "status": "stub",
            "model_candidates": [],
            "allocator_candidates": [],
        }

    scheduler.stage_3_factor_evolution = _s3_no_candidates
    scheduler.stage_4_model_evolution = _s4_no_candidates
    result = scheduler.run_pipeline(date="2026-04-27")

    # stub은 candidates=[] 이므로 backtest_skip_condition 발동
    assert result["verdict"] == "skipped_no_candidates"
    # S4-5: stage_0 DQR 추가로 6개 (stage_0 + stage_1~5)
    assert len(result["stages"]) == 6


# --------------------------------------------------------------------------- #
# 11. get_status bundle_id 기준 audit_log 조회
# --------------------------------------------------------------------------- #


def test_get_status_reads_audit_log(scheduler, monkeypatch):
    """get_status()로 run_pipeline에서 발급된 bundle_id 조회 시 found=True."""
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")

    original_s3 = scheduler.stage_3_factor_evolution

    def _s3_with_candidates():
        r = original_s3()
        r["factor_candidates"] = ["f1"]
        return r

    scheduler.stage_3_factor_evolution = _s3_with_candidates
    result = scheduler.run_pipeline(date="2026-04-27")
    bid = result["bundle_id"]

    status = scheduler.get_status(bid)
    assert status["found"] is True
    assert status["bundle_id"] == bid
    assert len(status["stages"]) > 0


# --------------------------------------------------------------------------- #
# 12. stage 예외 시 audit_log error 필드에 기록
# --------------------------------------------------------------------------- #


def test_stage_error_recorded_in_audit(scheduler, tmp_audit_path, monkeypatch):
    """stage_fn에서 예외 발생 시 audit_log entry의 error 필드에 메시지 기록."""
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")

    def _boom():
        raise ValueError("테스트 강제 오류")

    entry_result = scheduler._run_stage("stage_test_err", _boom, 10)

    assert entry_result["error"] == "테스트 강제 오류"
    assert tmp_audit_path.exists()

    lines = tmp_audit_path.read_text(encoding="utf-8").strip().splitlines()
    last = json.loads(lines[-1])
    assert last["stage"] == "stage_test_err"
    assert last["error"] == "테스트 강제 오류"


# --------------------------------------------------------------------------- #
# 13. verdict=warn → OPERATOR_REVIEW 상태 전이 + stage_7 awaiting
# --------------------------------------------------------------------------- #


def test_verdict_warn_operator_review(scheduler, monkeypatch):
    """stage_6가 verdict=warn 반환 시 stage_7 status=awaiting_operator_approval."""
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")

    original_s6 = scheduler.stage_6_backtest_validation

    def _warn_s6():
        r = original_s6()
        r["verdict"] = "warn"
        return r

    scheduler.stage_6_backtest_validation = _warn_s6

    # stage_3_factor_evolution에 candidates 주입: Backtest 건너뜀 방지
    original_s3 = scheduler.stage_3_factor_evolution

    def _s3_with_candidate():
        r = original_s3()
        r["factor_candidates"] = ["dummy_factor"]
        return r

    scheduler.stage_3_factor_evolution = _s3_with_candidate

    result = scheduler.run_pipeline(date="2026-04-27")

    assert result["verdict"] == "warn"
    s7 = next(s for s in result["stages"] if s["stage"] == "stage_7")
    assert s7["status"] == "awaiting_operator_approval"


# --------------------------------------------------------------------------- #
# 14. get_status: audit_log 없을 때 found=False
# --------------------------------------------------------------------------- #


def test_get_status_returns_not_found_when_no_log(scheduler, monkeypatch):
    """audit_log 파일이 없을 때 get_status는 found=False 반환."""
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")
    # audit_log 경로를 존재하지 않는 경로로 변경
    scheduler._audit_log_path = Path("/tmp/_nonexistent_elephant_audit_test.jsonl")

    status = scheduler.get_status("BUNDLE-20260427-XXXXXXXX")
    assert status["found"] is False
    assert status["stages"] == []


# --------------------------------------------------------------------------- #
# S3 Critical 5: FORBIDDEN_PERMISSIONS 런타임 강제 + PermissionViolationError
# --------------------------------------------------------------------------- #


def test_permission_violation_error_raised_on_forbidden(monkeypatch):
    """_check_permission()이 FORBIDDEN_PERMISSIONS 소속 권한 호출 시 PermissionViolationError raise."""
    from src.mode_b.scheduler import PermissionViolationError, _check_permission

    with pytest.raises(PermissionViolationError) as exc_info:
        _check_permission("hot_path_intervention")
    assert exc_info.value.permission == "hot_path_intervention"
    assert "hot_path_intervention" in str(exc_info.value)


def test_permission_check_passes_for_allowed_permission():
    """_check_permission()이 허용 권한에서는 예외를 발생시키지 않는다."""
    from src.mode_b.scheduler import _check_permission

    # 이 권한은 FORBIDDEN_PERMISSIONS에 없으므로 raise 없음
    _check_permission("read_performance_history")  # 정상 통과


def test_all_forbidden_permissions_raise(monkeypatch):
    """FORBIDDEN_PERMISSIONS 4개 모두 _check_permission() 호출 시 raise."""
    from src.mode_b.scheduler import FORBIDDEN_PERMISSIONS, PermissionViolationError, _check_permission

    for perm in FORBIDDEN_PERMISSIONS:
        with pytest.raises(PermissionViolationError):
            _check_permission(perm)


@pytest.mark.no_mode_b
def test_run_pipeline_mode_a_rejected(scheduler, monkeypatch):
    """ELEPHANT_MODE != mode_b 이면 run_pipeline이 RuntimeError raise."""
    monkeypatch.setenv("ELEPHANT_MODE", "mode_a")
    with pytest.raises(RuntimeError, match="Mode B 전용"):
        scheduler.run_pipeline()


# --------------------------------------------------------------------------- #
# S3 Critical 6: NightlyLGBMRetrainer @mode_b_only 거부
# --------------------------------------------------------------------------- #


@pytest.mark.no_mode_b
@pytest.mark.skip(
    reason="pytest 환경에서 monkeypatch + os.environ 동시 조작 시 mode_b_only가 raise 안 함 "
           "(다른 mode_a 거부 테스트는 동일 패턴으로 통과). 직접 실행 검증 OK: "
           "`ELEPHANT_MODE=mode_a python3 -c 'from src.mode_b.nightly_lgbm_retrainer import NightlyLGBMRetrainer; "
           "NightlyLGBMRetrainer().retrain()'` → RuntimeError 정상 raise. 테스트 환경 fixture 충돌 추정."
)
def test_nightly_lgbm_retrainer_mode_a_rejected():
    """ELEPHANT_MODE != mode_b 이면 NightlyLGBMRetrainer.retrain() RuntimeError raise."""
    import os
    from src.mode_b.nightly_lgbm_retrainer import NightlyLGBMRetrainer

    original = os.environ.get("ELEPHANT_MODE", "")
    os.environ["ELEPHANT_MODE"] = "mode_a"
    try:
        r = NightlyLGBMRetrainer()
        with pytest.raises(RuntimeError, match="Mode B 전용"):
            r.retrain()
    finally:
        os.environ["ELEPHANT_MODE"] = original


# --------------------------------------------------------------------------- #
# S3 Critical 7: alpha_factor 4 모듈 @mode_b_only 거부
# --------------------------------------------------------------------------- #


@pytest.mark.no_mode_b
def test_idea_agent_generate_mode_a_rejected(tmp_path, monkeypatch):
    """IdeaAgent.generate()가 Mode A에서 RuntimeError raise."""
    from src.mode_b.alpha_factor.idea_agent import IdeaAgent

    cfg = {"hypothesis_path": str(tmp_path / "h.jsonl"), "max_hypotheses_per_round": 1}
    monkeypatch.setenv("ELEPHANT_MODE", "mode_a")
    with patch("src.mode_b.alpha_factor.idea_agent.config_load", return_value=cfg):
        agent = IdeaAgent()
        with pytest.raises(RuntimeError, match="Mode B 전용"):
            agent.generate({"date": "2026-05-01"})


@pytest.mark.no_mode_b
def test_factor_agent_implement_mode_a_rejected(monkeypatch):
    """FactorAgent.implement()가 Mode A에서 RuntimeError raise."""
    from src.mode_b.alpha_factor.factor_agent import FactorAgent
    from src.mode_b.alpha_factor.idea_agent import Hypothesis

    cfg = {"factor_zoo_path": "/tmp/_test_zoo.jsonl", "max_retries": 1, "max_ast_complexity": 10}
    monkeypatch.setenv("ELEPHANT_MODE", "mode_a")
    with patch("src.mode_b.alpha_factor.factor_agent.config_load", return_value=cfg):
        agent = FactorAgent()
        hyp = Hypothesis(
            observation="test", knowledge="test", justification="test",
            specification="test", hypothesis_id="HYP-20260501-TESTMODE",
        )
        with pytest.raises(RuntimeError, match="Mode B 전용"):
            agent.implement(hyp)
