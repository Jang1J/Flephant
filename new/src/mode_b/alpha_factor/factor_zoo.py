"""S3-4 Factor Zoo + Alpha Decay Monitor.

architecture.md §3.2 기반.
FactorZooEntry: hypothesis + ast + ic_history + status 풀 스키마.
AlphaDecayMonitor: monthly IC 추적 → 3개월 decayed, 6개월 retired.
JSONL SSOT: artifacts/alpha_factor/factor_zoo.jsonl (기존 호환).

불변 원칙:
  1. 하드코딩 금지: 임계값은 risk_config.yaml alpha_factor 섹션에서 로드.
  2. PIT-Safety: IC 업데이트는 18:00 KST 이후만 허용.
  3. bare except 금지.
  4. JSONL atomic write (.tmp → rename).

Mode B 전용. 장중 호출 금지.
"""
from __future__ import annotations

import ast
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger
from src.utils.mode_guard import mode_b_only
from src.utils.pit_guard import PITViolationError

logger = get_logger("factor_zoo")
_KST = ZoneInfo("Asia/Seoul")


# ------------------------------------------------------------------ #
# FactorZooEntry dataclass
# ------------------------------------------------------------------ #

@dataclass
class FactorZooEntry:
    """Factor Zoo 풀 스키마. ic_history + status 포함.

    Fields:
        candidate_id:    "FAC-{YYYYMMDD}-{UUID8}" 형식
        code:            factor(df) Python 코드 문자열
        ast_hash:        SHA-256[:16] of ast.dump(ast.parse(code))
        ast_node_count:  AST 노드 수 (복잡도)
        ast_text:        ast.dump(ast.parse(code)) 전체 문자열
        description:     Hypothesis.specification 기반 설명
        hypothesis:      Hypothesis.to_dict() 저장
        ic_history:      월별 IC 추이 (append-only)
        status:          "active" | "decayed" | "retired"
        created_at:      ISO timestamp (KST)
        r_g:             EvalResult.r_g (None이면 평가 미완료)
        first_ic:        첫 번째 IC (EvalResult.ic, None이면 미평가)
        error:           실패 메시지 (성공 시 None)
    """

    candidate_id: str
    code: str
    ast_hash: str
    ast_node_count: int
    ast_text: str
    description: str
    hypothesis: dict
    ic_history: list = field(default_factory=list)
    status: str = "active"
    created_at: str = ""
    r_g: float | None = None
    first_ic: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ------------------------------------------------------------------ #
# FactorZoo
# ------------------------------------------------------------------ #

class FactorZoo:
    """Factor Zoo: FactorZooEntry JSONL SSOT 관리 + Alpha Decay Monitor.

    - add_candidate(): EvalResult 통과 후 신규 팩터 추가.
    - update_ic(): 월별 IC 업데이트 + decay 체크. PIT-Safety 강제.
    - check_decay_all(): 전체 active 팩터 decay 일괄 체크.
    - get() / list_by_status(): 조회.

    JSONL atomic write: .tmp → rename. 부분 쓰기 방지.
    기존 FactorCandidate 형식 (ic_history 없음) 자동 변환 (backward-compatible).
    """

    def __init__(self) -> None:
        cfg = config_load("risk_config.yaml", "alpha_factor") or {}
        self._warn_months: int = int(cfg.get("alpha_decay_warning_months", 3))
        self._retire_months: int = int(cfg.get("alpha_decay_retire_months", 6))
        factor_zoo_path: str = cfg.get(
            "factor_zoo_path", "artifacts/alpha_factor/factor_zoo.jsonl"
        )
        self._zoo_path = Path(factor_zoo_path)
        self._zoo_path.parent.mkdir(parents=True, exist_ok=True)

        # PIT-Safety snapshot 기준: 18:00 KST
        pit_cfg = config_load("risk_config.yaml", "pit_safety") or {}
        self._snapshot_hour: int = int(pit_cfg.get("snapshot_hour", 18))

        # decay 비가역 방지: opt-in revival 경로 (default False).
        # risk_config.yaml alpha_factor.allow_decayed_revival: true 로 활성화.
        # IC 회복 시 decayed → active 복귀. retired는 회복 불가.
        alpha_decay_cfg = config_load("risk_config.yaml", "alpha_decay_monitor") or cfg
        self._allow_revival: bool = bool(
            alpha_decay_cfg.get("allow_decayed_revival", False)
        )
        self._revival_threshold: float = float(
            alpha_decay_cfg.get("revival_threshold", 0.02)
        )

        logger.info(
            "[factor_zoo] 초기화. path=%s warn=%d retire=%d revival=%s",
            self._zoo_path,
            self._warn_months,
            self._retire_months,
            self._allow_revival,
        )

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    @mode_b_only
    def add_candidate(
        self,
        candidate: Any,
        hypothesis: Any,
        eval_result: Any | None,
    ) -> FactorZooEntry:
        """EvalResult 통과 후 Factor Zoo에 신규 팩터 추가.

        Args:
            candidate:   FactorCandidate 인스턴스.
            hypothesis:  Hypothesis 인스턴스.
            eval_result: EvalResult 인스턴스 또는 None.

        Returns:
            FactorZooEntry: 저장된 엔트리.
        """
        # AST text 생성
        ast_text = ""
        if candidate.code:
            try:
                ast_text = ast.dump(ast.parse(candidate.code))
            except SyntaxError as e:
                logger.warning("[factor_zoo] AST 파싱 실패: %s", e)

        hypothesis_dict: dict = {}
        if hypothesis is not None:
            try:
                hypothesis_dict = hypothesis.to_dict()
            except Exception as e:
                logger.warning("[factor_zoo] hypothesis.to_dict() 실패: %s", e)

        r_g_val: float | None = None
        first_ic_val: float | None = None
        if eval_result is not None:
            r_g_val = getattr(eval_result, "r_g", None)
            first_ic_val = getattr(eval_result, "ic", None)

        entry = FactorZooEntry(
            candidate_id=candidate.candidate_id,
            code=candidate.code,
            ast_hash=candidate.ast_hash,
            ast_node_count=candidate.ast_node_count,
            ast_text=ast_text,
            description=candidate.description,
            hypothesis=hypothesis_dict,
            ic_history=[],
            status="active",
            created_at=datetime.now(_KST).isoformat(),
            r_g=r_g_val,
            first_ic=first_ic_val,
            error=candidate.error,
        )

        # 기존 엔트리 확인 후 중복이면 덮어쓰지 않고 기존 반환
        entries = self._load_all()
        existing_ids = {e.candidate_id for e in entries}
        if entry.candidate_id in existing_ids:
            logger.info(
                "[factor_zoo] 이미 존재: %s. 기존 엔트리 반환", entry.candidate_id
            )
            for e in entries:
                if e.candidate_id == entry.candidate_id:
                    return e

        entries.append(entry)
        self._rewrite_all(entries)
        logger.info("[factor_zoo] 팩터 추가 완료: %s", entry.candidate_id)
        return entry

    @mode_b_only
    def update_ic(
        self,
        candidate_id: str,
        ic_value: float,
        recorded_month: str | None = None,
    ) -> FactorZooEntry:
        """월별 IC 업데이트 + Decay 체크.

        Args:
            candidate_id:    업데이트 대상 팩터 ID.
            ic_value:        이번 달 IC 값.
            recorded_month:  "YYYY-MM" 형식. None이면 현재 월 사용.

        Returns:
            FactorZooEntry: 업데이트된 엔트리.

        Raises:
            PITViolationError: 18:00 KST 이전 호출 시.
            KeyError: candidate_id 미존재 시.
        """
        # PIT-Safety: 18:00 KST 이후만 허용
        now_kst = datetime.now(_KST)
        snapshot_time = time(self._snapshot_hour, 0, 0)
        if now_kst.time() < snapshot_time:
            raise PITViolationError(
                f"[factor_zoo] PIT-Safety 위반: IC 업데이트는 {self._snapshot_hour}:00 KST 이후만 허용. "
                f"현재 시각: {now_kst.strftime('%H:%M:%S')}"
            )

        if recorded_month is None:
            recorded_month = now_kst.strftime("%Y-%m")

        entries = self._load_all()
        target_idx: int | None = None
        for i, e in enumerate(entries):
            if e.candidate_id == candidate_id:
                target_idx = i
                break

        if target_idx is None:
            raise KeyError(f"[factor_zoo] candidate_id 미존재: {candidate_id}")

        entry = entries[target_idx]
        entry.ic_history.append(ic_value)

        # Decay 체크 (active → decayed → retired 단방향)
        new_status = self._check_decay(entry)
        if new_status is not None and new_status != entry.status:
            logger.info(
                "[factor_zoo] 상태 전이: %s %s → %s (ic_history=%s)",
                candidate_id,
                entry.status,
                new_status,
                entry.ic_history,
            )
            entry.status = new_status
        elif self._allow_revival and entry.status == "decayed":
            # opt-in revival: decayed → active. IC 회복 시 단방향 규칙 완화.
            # retired는 회복 불가. warn_months 이상 IC 평균이 revival_threshold 초과 시만.
            recent = entry.ic_history[-self._warn_months:]
            if len(recent) >= self._warn_months:
                recent_mean = sum(abs(v) for v in recent) / len(recent)
                if recent_mean > self._revival_threshold:
                    logger.info(
                        "[factor_zoo] revival: %s decayed → active (recent_ic_mean=%.4f > %.4f)",
                        candidate_id, recent_mean, self._revival_threshold,
                    )
                    entry.status = "active"

        entries[target_idx] = entry
        self._rewrite_all(entries)
        logger.info(
            "[factor_zoo] IC 업데이트: %s ic=%.4f month=%s status=%s",
            candidate_id,
            ic_value,
            recorded_month,
            entry.status,
        )
        return entry

    def check_decay_all(self) -> list[dict]:
        """전체 active 팩터의 Decay 상태 일괄 체크.

        Returns:
            변경된 엔트리 목록. 각 항목은 {"candidate_id", "old_status", "new_status"}.
        """
        entries = self._load_all()
        changed: list[dict] = []
        updated = False

        for i, entry in enumerate(entries):
            if entry.status == "retired":
                continue
            new_status = self._check_decay(entry)
            if new_status is not None and new_status != entry.status:
                old_status = entry.status
                changed.append({
                    "candidate_id": entry.candidate_id,
                    "old_status": old_status,
                    "new_status": new_status,
                })
                entries[i].status = new_status
                updated = True
                logger.info(
                    "[factor_zoo] check_decay_all 상태 전이: %s %s → %s",
                    entry.candidate_id,
                    old_status,
                    new_status,
                )

        if updated:
            self._rewrite_all(entries)

        logger.info("[factor_zoo] check_decay_all 완료. 변경=%d건", len(changed))
        return changed

    def get(self, candidate_id: str) -> FactorZooEntry | None:
        """ID로 조회. 없으면 None."""
        for entry in self._load_all():
            if entry.candidate_id == candidate_id:
                return entry
        return None

    def list_by_status(self, status: str) -> list[FactorZooEntry]:
        """상태별 조회."""
        return [e for e in self._load_all() if e.status == status]

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _check_decay(self, entry: FactorZooEntry) -> str | None:
        """IC 추이 기반 상태 전이 판단.

        history[-retire_months:] 단조 감소 → "retired"
        history[-warn_months:] 단조 감소 → "decayed"
        변경 없으면 None.

        단조 감소 정의: 각 연속 쌍에서 앞 값 >= 뒤 값 (비증가 수열).
        """
        history = entry.ic_history
        retire_months = self._retire_months
        warn_months = self._warn_months

        def is_monotone_decreasing(values: list) -> bool:
            return all(values[i] >= values[i + 1] for i in range(len(values) - 1))

        if len(history) >= retire_months:
            if is_monotone_decreasing(history[-retire_months:]):
                return "retired"

        if len(history) >= warn_months:
            if is_monotone_decreasing(history[-warn_months:]):
                return "decayed"

        return None

    def _load_all(self) -> list[FactorZooEntry]:
        """JSONL 전체 로드. 기존 FactorCandidate 형식도 FactorZooEntry로 변환."""
        if not self._zoo_path.exists():
            return []

        entries: list[FactorZooEntry] = []
        with self._zoo_path.open("r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    d = json.loads(stripped)
                    entries.append(self._from_dict(d))
                except json.JSONDecodeError as e:
                    logger.warning(
                        "[factor_zoo] JSONL %d번째 줄 파싱 실패: %s", line_num, e
                    )
        return entries

    def _from_dict(self, d: dict) -> FactorZooEntry:
        """dict → FactorZooEntry 변환. 기존 FactorCandidate 형식 호환.

        기존 형식에 없는 필드는 기본값으로 채움:
        - ic_history: [] (없으면 빈 리스트)
        - status: "active" (없으면 active)
        - ast_text: "" (없으면 빈 문자열)
        - hypothesis: {} (없으면 빈 dict)
        - first_ic: ic 필드 fallback (기존 EvalResult.ic 저장 필드)
        """
        return FactorZooEntry(
            candidate_id=d.get("candidate_id", ""),
            code=d.get("code", ""),
            ast_hash=d.get("ast_hash", ""),
            ast_node_count=d.get("ast_node_count", 0),
            ast_text=d.get("ast_text", ""),
            description=d.get("description", ""),
            hypothesis=d.get("hypothesis", {}),
            ic_history=d.get("ic_history", []),
            status=d.get("status", "active"),
            created_at=d.get("created_at", ""),
            r_g=d.get("r_g"),
            first_ic=d.get("first_ic") or d.get("ic"),
            error=d.get("error"),
        )

    def _rewrite_all(self, entries: list[FactorZooEntry]) -> None:
        """atomic rewrite: .tmp 파일에 쓴 후 rename. 부분 쓰기 방지.

        JSONL atomic write 원칙 준수.
        """
        tmp_path = self._zoo_path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        os.replace(tmp_path, self._zoo_path)
