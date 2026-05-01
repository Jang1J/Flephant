"""S3-4 FactorZoo + AlphaDecayMonitor unit tests.

done_criteria:
- test >= 8개 PASS
- active/decayed/retired 상태 전이 동작
- pytest 전체 PASS

설계 원칙:
- 각 테스트는 독립 tmp_path 사용. JSONL 상태 공유 없음.
- patch 대상: "src.mode_b.alpha_factor.factor_zoo.config_load" (module-level binding).
- datetime mock: "src.mode_b.alpha_factor.factor_zoo.datetime".
- PIT-Safety 우회: 현재 시각을 20:00 KST로 고정.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from zoneinfo import ZoneInfo

# ------------------------------------------------------------------ #
# 경로 설정
# ------------------------------------------------------------------ #

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "new"))

# ------------------------------------------------------------------ #
# import
# ------------------------------------------------------------------ #

from src.mode_b.alpha_factor.factor_agent import FactorCandidate
from src.mode_b.alpha_factor.factor_zoo import FactorZoo, FactorZooEntry
from src.mode_b.alpha_factor.idea_agent import Hypothesis
from src.utils.pit_guard import PITViolationError

_KST = ZoneInfo("Asia/Seoul")

# 테스트 전용 고정 시각
_AFTER_18 = datetime(2026, 4, 28, 20, 0, 0, tzinfo=_KST)
_BEFORE_18 = datetime(2026, 4, 28, 15, 0, 0, tzinfo=_KST)


# ------------------------------------------------------------------ #
# 공통 헬퍼
# ------------------------------------------------------------------ #

def _make_config_side_effect(zoo_path: str, allow_revival: bool = False):
    """config_load mock side_effect 생성.

    patch 대상: "src.mode_b.alpha_factor.factor_zoo.config_load"
    (factor_zoo.py에서 from src.utils.config_loader import load as config_load 로 바인딩됨)
    """
    def _side(config_file: str = "risk_config.yaml", key: str | None = None):
        if key == "alpha_factor":
            return {
                "alpha_decay_warning_months": 3,
                "alpha_decay_retire_months": 6,
                "factor_zoo_path": zoo_path,
            }
        if key == "pit_safety":
            return {"snapshot_hour": 18}
        if key == "alpha_decay_monitor":
            return {
                "allow_decayed_revival": allow_revival,
                "revival_threshold": 0.02,
            }
        return {}
    return _side


def _make_fake_datetime(fixed: datetime):
    """지정 시각으로 고정된 FakeDatetime 클래스 생성."""
    class _Cls(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed
    return _Cls


def _make_zoo(zoo_path: Path, fixed_dt: datetime = _AFTER_18) -> FactorZoo:
    """config_load + datetime mock이 적용된 FactorZoo 인스턴스 반환."""
    FakeDt = _make_fake_datetime(fixed_dt)
    cfg_mock = _make_config_side_effect(str(zoo_path))
    with patch("src.mode_b.alpha_factor.factor_zoo.config_load", side_effect=cfg_mock):
        with patch("src.mode_b.alpha_factor.factor_zoo.datetime", FakeDt):
            return FactorZoo()


def _make_sample_candidate(suffix: str = "TESTTST1") -> FactorCandidate:
    return FactorCandidate(
        candidate_id=f"FAC-20260428-{suffix}",
        hypothesis_id="HYP-20260428-TESTTEST",
        code="def factor(df):\n    return df['close'].pct_change()",
        ast_hash=f"hash_{suffix}",
        ast_node_count=8,
        description="close 수익률 기반 팩터",
        status="active",
        attempt_count=1,
        created_at="2026-04-28T20:00:00+09:00",
        error=None,
    )


def _make_sample_hypothesis() -> Hypothesis:
    return Hypothesis(
        observation="외국인 순매수 급증 패턴",
        knowledge="외국인 수급은 단기 모멘텀 선행 지표",
        justification="외국인 순매수 증가 시 기관 추종 매수 발생",
        specification="ts_zscore(df['foreign_net_buy'], 20)",
        anchor_id=None,
        created_at="2026-04-28T00:00:00+09:00",
        hypothesis_id="HYP-20260428-TESTTEST",
    )


# ------------------------------------------------------------------ #
# Test 1: FactorZooEntry 스키마 확인
# ------------------------------------------------------------------ #

def test_factor_zoo_entry_schema():
    """FactorZooEntry 필드 목록 확인: hypothesis, ast_text, ic_history, status 포함."""
    entry = FactorZooEntry(
        candidate_id="FAC-20260428-SCHEMA01",
        code="def factor(df): return df['close']",
        ast_hash="hash001",
        ast_node_count=5,
        ast_text="Module(body=[...])",
        description="test",
        hypothesis={"observation": "test", "specification": "ts_zscore"},
        ic_history=[0.05, 0.04, 0.03],
        status="active",
        created_at="2026-04-28T20:00:00+09:00",
        r_g=0.3,
        first_ic=0.05,
        error=None,
    )
    d = entry.to_dict()

    assert "hypothesis" in d
    assert "ast_text" in d
    assert "ic_history" in d
    assert "status" in d
    assert d["status"] == "active"
    assert d["ic_history"] == [0.05, 0.04, 0.03]
    assert d["hypothesis"]["specification"] == "ts_zscore"
    assert d["r_g"] == 0.3
    assert d["first_ic"] == 0.05


# ------------------------------------------------------------------ #
# Test 2: add_candidate → JSONL 기록
# ------------------------------------------------------------------ #

def test_add_candidate_saves_to_jsonl(tmp_path):
    """add_candidate() 호출 후 JSONL 파일에 1줄 기록되는지 확인."""
    zoo_path = tmp_path / "factor_zoo.jsonl"
    candidate = _make_sample_candidate("SAVTEST1")
    hypothesis = _make_sample_hypothesis()

    FakeDt = _make_fake_datetime(_AFTER_18)
    cfg_mock = _make_config_side_effect(str(zoo_path))
    with patch("src.mode_b.alpha_factor.factor_zoo.config_load", side_effect=cfg_mock):
        with patch("src.mode_b.alpha_factor.factor_zoo.datetime", FakeDt):
            zoo = FactorZoo()
            zoo.add_candidate(candidate, hypothesis, eval_result=None)

    assert zoo_path.exists()
    lines = [l for l in zoo_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    saved = json.loads(lines[0])
    assert saved["candidate_id"] == "FAC-20260428-SAVTEST1"
    assert saved["status"] == "active"


# ------------------------------------------------------------------ #
# Test 3: 신규 엔트리 ic_history = []
# ------------------------------------------------------------------ #

def test_add_candidate_has_ic_history(tmp_path):
    """add_candidate() 결과 엔트리의 ic_history가 빈 리스트인지 확인."""
    zoo_path = tmp_path / "factor_zoo.jsonl"
    candidate = _make_sample_candidate("ICHIST01")
    hypothesis = _make_sample_hypothesis()

    FakeDt = _make_fake_datetime(_AFTER_18)
    cfg_mock = _make_config_side_effect(str(zoo_path))
    with patch("src.mode_b.alpha_factor.factor_zoo.config_load", side_effect=cfg_mock):
        with patch("src.mode_b.alpha_factor.factor_zoo.datetime", FakeDt):
            zoo = FactorZoo()
            entry = zoo.add_candidate(candidate, hypothesis, eval_result=None)

    assert entry.ic_history == []


# ------------------------------------------------------------------ #
# Test 4: update_ic → ic_history 값 추가
# ------------------------------------------------------------------ #

def test_update_ic_appends_value(tmp_path):
    """update_ic() 호출 시 ic_history에 값이 append되는지 확인."""
    zoo_path = tmp_path / "factor_zoo.jsonl"
    candidate = _make_sample_candidate("UPDICT01")
    hypothesis = _make_sample_hypothesis()

    FakeDt = _make_fake_datetime(_AFTER_18)
    cfg_mock = _make_config_side_effect(str(zoo_path))
    with patch("src.mode_b.alpha_factor.factor_zoo.config_load", side_effect=cfg_mock):
        with patch("src.mode_b.alpha_factor.factor_zoo.datetime", FakeDt):
            zoo = FactorZoo()
            zoo.add_candidate(candidate, hypothesis, eval_result=None)
            entry = zoo.update_ic("FAC-20260428-UPDICT01", 0.07)

    assert 0.07 in entry.ic_history
    assert len(entry.ic_history) == 1


# ------------------------------------------------------------------ #
# Test 5: IC 3회 하락 → status="decayed"
# ------------------------------------------------------------------ #

def test_decay_warning_after_3_months(tmp_path):
    """ic_history 3회 단조 감소 시 status가 decayed로 전이되는지 확인."""
    zoo_path = tmp_path / "factor_zoo.jsonl"
    candidate = _make_sample_candidate("DECAY3M1")
    hypothesis = _make_sample_hypothesis()

    FakeDt = _make_fake_datetime(_AFTER_18)
    cfg_mock = _make_config_side_effect(str(zoo_path))
    with patch("src.mode_b.alpha_factor.factor_zoo.config_load", side_effect=cfg_mock):
        with patch("src.mode_b.alpha_factor.factor_zoo.datetime", FakeDt):
            zoo = FactorZoo()
            zoo.add_candidate(candidate, hypothesis, eval_result=None)
            zoo.update_ic("FAC-20260428-DECAY3M1", 0.05)
            zoo.update_ic("FAC-20260428-DECAY3M1", 0.04)
            entry = zoo.update_ic("FAC-20260428-DECAY3M1", 0.03)

    assert entry.status == "decayed", f"기대 decayed, 실제: {entry.status}"


# ------------------------------------------------------------------ #
# Test 6: IC 6회 하락 → status="retired"
# ------------------------------------------------------------------ #

def test_retire_after_6_months(tmp_path):
    """ic_history 6회 단조 감소 시 status가 retired로 전이되는지 확인."""
    zoo_path = tmp_path / "factor_zoo.jsonl"
    candidate = _make_sample_candidate("RETIRE61")
    hypothesis = _make_sample_hypothesis()

    FakeDt = _make_fake_datetime(_AFTER_18)
    cfg_mock = _make_config_side_effect(str(zoo_path))
    with patch("src.mode_b.alpha_factor.factor_zoo.config_load", side_effect=cfg_mock):
        with patch("src.mode_b.alpha_factor.factor_zoo.datetime", FakeDt):
            zoo = FactorZoo()
            zoo.add_candidate(candidate, hypothesis, eval_result=None)
            ic_values = [0.10, 0.09, 0.08, 0.07, 0.06, 0.05]
            entry = None
            for ic in ic_values:
                entry = zoo.update_ic("FAC-20260428-RETIRE61", ic)

    assert entry is not None
    assert entry.status == "retired", f"기대 retired, 실제: {entry.status}"


# ------------------------------------------------------------------ #
# Test 7: 비단조 감소 시 상태 변경 없음
# ------------------------------------------------------------------ #

def test_no_decay_when_ic_fluctuates(tmp_path):
    """IC가 등락 반복(비단조) 시 status가 active 유지되는지 확인."""
    zoo_path = tmp_path / "factor_zoo.jsonl"
    candidate = _make_sample_candidate("NODECAY1")
    hypothesis = _make_sample_hypothesis()

    FakeDt = _make_fake_datetime(_AFTER_18)
    cfg_mock = _make_config_side_effect(str(zoo_path))
    with patch("src.mode_b.alpha_factor.factor_zoo.config_load", side_effect=cfg_mock):
        with patch("src.mode_b.alpha_factor.factor_zoo.datetime", FakeDt):
            zoo = FactorZoo()
            zoo.add_candidate(candidate, hypothesis, eval_result=None)
            # 등락 반복: 0.05 → 0.03 → 0.07 (마지막이 반등)
            zoo.update_ic("FAC-20260428-NODECAY1", 0.05)
            zoo.update_ic("FAC-20260428-NODECAY1", 0.03)
            entry = zoo.update_ic("FAC-20260428-NODECAY1", 0.07)

    assert entry.status == "active", f"기대 active, 실제: {entry.status}"


# ------------------------------------------------------------------ #
# Test 8: list_by_status
# ------------------------------------------------------------------ #

def test_list_by_status(tmp_path):
    """list_by_status("active") 호출 시 active 항목만 반환되는지 확인."""
    zoo_path = tmp_path / "factor_zoo.jsonl"
    hypothesis = _make_sample_hypothesis()

    FakeDt = _make_fake_datetime(_AFTER_18)
    cfg_mock = _make_config_side_effect(str(zoo_path))
    with patch("src.mode_b.alpha_factor.factor_zoo.config_load", side_effect=cfg_mock):
        with patch("src.mode_b.alpha_factor.factor_zoo.datetime", FakeDt):
            zoo = FactorZoo()

            cand_a = FactorCandidate(
                candidate_id="FAC-20260428-STATUS01",
                hypothesis_id="HYP-20260428-TESTTEST",
                code="def factor(df): return df['close']",
                ast_hash="aaa",
                ast_node_count=5,
                description="A",
                status="active",
                attempt_count=1,
                created_at="2026-04-28T20:00:00+09:00",
                error=None,
            )
            cand_b = FactorCandidate(
                candidate_id="FAC-20260428-STATUS02",
                hypothesis_id="HYP-20260428-TESTTEST",
                code="def factor(df): return df['volume']",
                ast_hash="bbb",
                ast_node_count=5,
                description="B",
                status="active",
                attempt_count=1,
                created_at="2026-04-28T20:00:00+09:00",
                error=None,
            )
            zoo.add_candidate(cand_a, hypothesis, eval_result=None)
            zoo.add_candidate(cand_b, hypothesis, eval_result=None)

            # cand_b를 decayed로 전이 (3회 단조 감소)
            for ic in [0.05, 0.04, 0.03]:
                zoo.update_ic("FAC-20260428-STATUS02", ic)

            actives = zoo.list_by_status("active")
            decayeds = zoo.list_by_status("decayed")

    assert len(actives) == 1
    assert actives[0].candidate_id == "FAC-20260428-STATUS01"
    assert len(decayeds) == 1
    assert decayeds[0].candidate_id == "FAC-20260428-STATUS02"


# ------------------------------------------------------------------ #
# Test 9: 기존 FactorCandidate 형식 → FactorZooEntry 변환 (backward-compatible)
# ------------------------------------------------------------------ #

def test_backward_compatible_load(tmp_path):
    """ic_history 없는 기존 FactorCandidate dict → FactorZooEntry 변환 확인."""
    zoo_path = tmp_path / "factor_zoo.jsonl"
    # 기존 형식 (FactorCandidate.to_dict() 형태, ic_history/ast_text/hypothesis 없음)
    old_dict = {
        "candidate_id": "FAC-20260101-OLDSTYLE",
        "hypothesis_id": "HYP-20260101-OLDTEST",
        "code": "def factor(df): return df['close'].rolling(5).mean()",
        "ast_hash": "oldhashabcd",
        "ast_node_count": 12,
        "description": "5일 이동평균",
        "status": "active",
        "attempt_count": 1,
        "created_at": "2026-01-01T20:00:00+09:00",
        "error": None,
        "ic": 0.035,  # 기존 EvalResult.ic 필드 (first_ic로 매핑)
    }
    zoo_path.write_text(
        json.dumps(old_dict, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    cfg_mock = _make_config_side_effect(str(zoo_path))
    with patch("src.mode_b.alpha_factor.factor_zoo.config_load", side_effect=cfg_mock):
        zoo = FactorZoo()
        entries = zoo.list_by_status("active")

    assert len(entries) == 1
    e = entries[0]
    assert e.candidate_id == "FAC-20260101-OLDSTYLE"
    assert e.ic_history == []          # 기존 형식에 없음 → 빈 리스트
    assert e.ast_text == ""            # 기존 형식에 없음 → 빈 문자열
    assert e.hypothesis == {}          # 기존 형식에 없음 → 빈 dict
    assert e.first_ic == 0.035         # 기존 "ic" 필드 → first_ic 매핑
    assert e.status == "active"


# ------------------------------------------------------------------ #
# Test 10: check_decay_all → 변경 목록 반환
# ------------------------------------------------------------------ #

def test_check_decay_all(tmp_path):
    """check_decay_all() 호출 시 decay 대상 팩터가 변경 목록에 포함되는지 확인."""
    zoo_path = tmp_path / "factor_zoo.jsonl"
    hypothesis = _make_sample_hypothesis()
    candidate = FactorCandidate(
        candidate_id="FAC-20260428-CHKALL01",
        hypothesis_id="HYP-20260428-TESTTEST",
        code="def factor(df): return df['close']",
        ast_hash="chkall_hash",
        ast_node_count=5,
        description="check_decay_all 테스트용",
        status="active",
        attempt_count=1,
        created_at="2026-04-28T20:00:00+09:00",
        error=None,
    )

    FakeDt = _make_fake_datetime(_AFTER_18)
    cfg_mock = _make_config_side_effect(str(zoo_path))
    with patch("src.mode_b.alpha_factor.factor_zoo.config_load", side_effect=cfg_mock):
        with patch("src.mode_b.alpha_factor.factor_zoo.datetime", FakeDt):
            zoo = FactorZoo()
            zoo.add_candidate(candidate, hypothesis, eval_result=None)

            # ic_history를 직접 주입 (3회 단조 감소)
            entries = zoo._load_all()
            entries[0].ic_history = [0.05, 0.04, 0.03]
            zoo._rewrite_all(entries)

            # check_decay_all 실행
            changed = zoo.check_decay_all()

    assert len(changed) == 1
    assert changed[0]["candidate_id"] == "FAC-20260428-CHKALL01"
    assert changed[0]["old_status"] == "active"
    assert changed[0]["new_status"] == "decayed"


# ------------------------------------------------------------------ #
# Test 11: PIT-Safety - 18:00 이전 update_ic 시 PITViolationError
# ------------------------------------------------------------------ #

def test_pit_safety_blocks_early_update(tmp_path):
    """18:00 KST 이전 update_ic() 호출 시 PITViolationError 발생 확인."""
    zoo_path = tmp_path / "factor_zoo.jsonl"
    candidate = _make_sample_candidate("PITSAFE1")
    hypothesis = _make_sample_hypothesis()

    # 팩터 추가 (20:00 KST → PIT-Safety 통과)
    FakeDtAfter = _make_fake_datetime(_AFTER_18)
    cfg_mock = _make_config_side_effect(str(zoo_path))
    with patch("src.mode_b.alpha_factor.factor_zoo.config_load", side_effect=cfg_mock):
        with patch("src.mode_b.alpha_factor.factor_zoo.datetime", FakeDtAfter):
            zoo = FactorZoo()
            zoo.add_candidate(candidate, hypothesis, eval_result=None)

    # 15:00 KST에서 update_ic 시도 → PITViolationError 기대
    FakeDtBefore = _make_fake_datetime(_BEFORE_18)
    with patch("src.mode_b.alpha_factor.factor_zoo.config_load", side_effect=cfg_mock):
        with patch("src.mode_b.alpha_factor.factor_zoo.datetime", FakeDtBefore):
            zoo2 = FactorZoo()
            with pytest.raises(PITViolationError):
                zoo2.update_ic("FAC-20260428-PITSAFE1", 0.05)


# ------------------------------------------------------------------ #
# Test 12: get() 단독 테스트
# ------------------------------------------------------------------ #

def test_get_candidate_by_id(tmp_path):
    """get(candidate_id)로 특정 팩터 조회."""
    zoo_path = tmp_path / "factor_zoo.jsonl"
    candidate = _make_sample_candidate("GETTEST1")
    hypothesis = _make_sample_hypothesis()

    FakeDt = _make_fake_datetime(_AFTER_18)
    cfg_mock = _make_config_side_effect(str(zoo_path))
    with patch("src.mode_b.alpha_factor.factor_zoo.config_load", side_effect=cfg_mock):
        with patch("src.mode_b.alpha_factor.factor_zoo.datetime", FakeDt):
            zoo = FactorZoo()
            zoo.add_candidate(candidate, hypothesis, eval_result=None)

    with patch("src.mode_b.alpha_factor.factor_zoo.config_load", side_effect=cfg_mock):
        zoo2 = FactorZoo()
        entry = zoo2.get("FAC-20260428-GETTEST1")
        assert entry is not None
        assert entry.candidate_id == "FAC-20260428-GETTEST1"
        assert entry.status == "active"
        # 없는 ID → None
        assert zoo2.get("NONEXISTENT-ID") is None


# ------------------------------------------------------------------ #
# Test 13: add_candidate 후 hypothesis/ast_text 스키마 저장 확인
# ------------------------------------------------------------------ #

def test_add_candidate_full_schema(tmp_path):
    """add_candidate() 후 JSONL에 hypothesis, ast_text, ic_history 필드 저장 확인."""
    zoo_path = tmp_path / "factor_zoo.jsonl"
    candidate = _make_sample_candidate("SCHEMA01")
    hypothesis = _make_sample_hypothesis()

    FakeDt = _make_fake_datetime(_AFTER_18)
    cfg_mock = _make_config_side_effect(str(zoo_path))
    with patch("src.mode_b.alpha_factor.factor_zoo.config_load", side_effect=cfg_mock):
        with patch("src.mode_b.alpha_factor.factor_zoo.datetime", FakeDt):
            zoo = FactorZoo()
            zoo.add_candidate(candidate, hypothesis, eval_result=None)

    lines = [l for l in zoo_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    saved = json.loads(lines[0])
    # 핵심 신규 필드 확인
    assert "hypothesis" in saved, "hypothesis 필드 누락"
    assert "ast_text" in saved, "ast_text 필드 누락"
    assert "ic_history" in saved, "ic_history 필드 누락"
    assert saved["ic_history"] == [], "ic_history 초기값 []이어야 함"
    assert isinstance(saved["hypothesis"], dict), "hypothesis는 dict이어야 함"
    assert len(saved["ast_text"]) > 0, "ast_text는 비어있지 않아야 함"
