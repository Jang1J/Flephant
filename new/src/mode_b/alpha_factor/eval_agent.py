"""S3-3 Alpha Factor Engine: Eval Agent.

AlphaAgent 논문 3중 정규화. FactorCandidate → EvalResult.
IC 계산(synthetic) + 중복 탐지 + 실패 카테고리 분류.
caller='factor_evaluation' (risk_config.yaml mode_b_allowed_callers).

불변 원칙:
  1. 하드코딩 금지: 모든 수치/가중치/임계값은 risk_config.yaml eval_agent 섹션에서 로드.
  2. Mode B 전용: GPT-4o, caller='factor_evaluation'.
  3. PIT-Safety: IC 계산에 forward_returns = close.pct_change(1).shift(-1) 사용 (t+1, PIT-safe).
  4. bare except 금지.

3중 정규화:
  R_g(f,h) = α₁·SL(f) + α₂·PC(f) + α₃·ER(f,h)

  SL(f) = ast_node_count / max_ast_complexity
  PC(f) = numeric_literal_count / max_pc
  ER(f,h) = β₁·S(f) + β₂·C(h,d,f) + β₃·log(1+feature_count) / log_scale

  R_g < r_g_threshold → 채택 (낮을수록 좋음)
"""
from __future__ import annotations

import ast
import math
import re
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

from src.mode_b.alpha_factor.operators import OPERATOR_NAMESPACE
from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger
from src.utils.mode_guard import mode_b_only

logger = get_logger("eval_agent")


def _llm_content(response: Any) -> str:
    """LLMRouter의 LLMCallResult 또는 테스트용 str mock을 문자열로 정규화."""
    if hasattr(response, "success") and hasattr(response, "content"):
        if not bool(getattr(response, "success")):
            raise RuntimeError(str(getattr(response, "error", "LLM_CALL_FAILED")))
        content = getattr(response, "content", None)
        if content is None:
            raise RuntimeError("LLM_EMPTY_CONTENT")
        return str(content)
    return str(response)

# ------------------------------------------------------------------ #
# EvalResult dataclass
# ------------------------------------------------------------------ #

@dataclass
class EvalResult:
    """3중 정규화 평가 결과.

    Fields:
        candidate_id:      FactorCandidate.candidate_id
        r_g:               3중 정규화 점수 (낮을수록 좋음, 0~1)
        sl:                SL(f) 컴포넌트 (AST 복잡도, 0~1)
        pc:                PC(f) 컴포넌트 (파라미터 수 정규화, 0~1)
        er:                ER(f,h) 컴포넌트 (경제적 관련성, 0~1)
        ic:                Information Coefficient (Spearman, -1~1)
        rank_ic:           RankIC (rank-rank Spearman, -1~1)
        passed:            True = 채택 (R_g < r_g_threshold + |IC| >= ic_min)
        failure_category:  실패 카테고리 5종 중 하나 또는 None
        reason:            판단 근거 한 줄 요약
    """

    candidate_id: str
    r_g: float
    sl: float
    pc: float
    er: float
    ic: float
    rank_ic: float
    passed: bool
    failure_category: str | None
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ------------------------------------------------------------------ #
# EvalAgent
# ------------------------------------------------------------------ #

class EvalAgent:
    """AlphaAgent 논문 3중 정규화 기반 팩터 평가 에이전트.

    Mode B 전용. LLMRouter caller='factor_evaluation'.
    FactorCandidate → EvalResult 변환.

    Args:
        llm_router: LLMRouter 인스턴스. None이면 alignment fallback=0.5.
    """

    def __init__(self, llm_router: Any = None) -> None:
        cfg = config_load("risk_config.yaml", "eval_agent") or {}
        alpha_cfg = config_load("risk_config.yaml", "alpha_factor") or {}

        # 3중 정규화 가중치
        self._alpha_1: float = float(cfg.get("alpha_1", 0.3))   # SL weight
        self._alpha_2: float = float(cfg.get("alpha_2", 0.2))   # PC weight
        self._alpha_3: float = float(cfg.get("alpha_3", 0.5))   # ER weight

        # ER 내부 가중치
        self._beta_1: float = float(cfg.get("beta_1", 0.4))     # S(f) similarity penalty
        self._beta_2: float = float(cfg.get("beta_2", 0.4))     # C(h,d,f) alignment penalty
        self._beta_3: float = float(cfg.get("beta_3", 0.2))     # feature count penalty

        # 임계값
        self._r_g_threshold: float = float(cfg.get("r_g_threshold", 0.7))
        self._ic_min: float = float(cfg.get("ic_min_threshold", 0.02))
        self._align_thresh: float = float(cfg.get("alignment_threshold", 0.7))
        self._max_pc: int = int(cfg.get("max_pc", 5))
        self._log_scale: float = float(cfg.get("log_scale", 3.0))

        # alpha_factor 섹션 임계값
        self._max_ast_complexity: int = int(alpha_cfg.get("max_ast_complexity", 10))
        self._ic_dup_threshold: float = float(alpha_cfg.get("ic_duplicate_threshold", 0.99))

        # 하드코딩 방지: eval_agent 섹션에서 로드
        self._sl_clip_max: float = float(cfg.get("sl_clip_max", 2.0))
        self._synthetic_bars: int = int(cfg.get("synthetic_bars", 50))
        self._min_valid_samples: int = int(cfg.get("min_valid_samples", 5))
        # C3 yaml화: _compute_alignment 가중치 (불변 원칙 5)
        self._c1_w: float = float(cfg.get("alignment_c1_weight", 0.5))
        self._c2_w: float = float(cfg.get("alignment_c2_weight", 0.5))
        # W3 yaml화: synthetic DataFrame seed (불변 원칙 5)
        self._synthetic_seed: int = int(cfg.get("synthetic_seed", 42))

        self._llm_router = llm_router
        logger.info(
            "[eval_agent] 초기화. r_g_threshold=%.2f ic_min=%.4f align_thresh=%.2f",
            self._r_g_threshold, self._ic_min, self._align_thresh,
        )

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    @mode_b_only
    def evaluate(
        self,
        candidate: Any,  # FactorCandidate
        hypothesis: Any,  # Hypothesis
        factor_zoo: list[dict] | None = None,
    ) -> EvalResult:
        """3중 정규화 + IC 계산 + 중복 탐지. EvalResult 반환.

        Args:
            candidate:   FactorCandidate (factor_agent.FactorCandidate).
            hypothesis:  Hypothesis (idea_agent.Hypothesis).
            factor_zoo:  기존 active 팩터 목록. None이면 빈 리스트로 처리.

        Returns:
            EvalResult: 평가 결과.

        PIT-Safety: forward_returns = close.pct_change(1).shift(-1) 사용.
                    synthetic 데이터로만 IC 계산. 실제 미래 데이터 미사용.
        """
        zoo: list[dict] = factor_zoo or []

        # --- execution_failure 조기 감지 ---
        _test_df = _make_synthetic_df(n=self._synthetic_bars, seed=self._synthetic_seed)
        _exec_ok = _exec_factor_scores(candidate.code, _test_df) is not None
        if not _exec_ok:
            return EvalResult(
                candidate_id=candidate.candidate_id,
                r_g=1.0, sl=0.0, pc=0.0, er=0.0,
                ic=0.0, rank_ic=0.0,
                passed=False,
                failure_category="execution_failure",
                reason="FAIL(execution_failure): factor 실행 불가",
            )

        # --- IC 계산 ---
        ic, rank_ic = self._compute_ic(candidate.code)
        if math.isnan(ic):
            ic = 0.0
        if math.isnan(rank_ic):
            rank_ic = 0.0

        # --- SL 계산 ---
        sl = self._compute_sl(candidate)

        # --- PC 계산 (정규화) ---
        pc_count = self._compute_pc(candidate.code)
        pc_norm = min(pc_count / max(self._max_pc, 1), 1.0)

        # --- 유사도 S(f) ---
        similarity = self._compute_similarity(candidate, zoo)

        # --- 정합성 C(h,d,f) ---
        alignment = self._compute_alignment(hypothesis, candidate)

        # --- feature count (ER beta_3) ---
        feature_count = _count_features(candidate.code)

        # --- ER ---
        # beta3 term 사전 clip: feature_count >= 20이면 log(21)/log_scale > 1.0 초과.
        # 사전 clip으로 R_g 내 각 beta 항 비율 왜곡 방지.
        er_beta3_term = min(1.0, math.log(1 + feature_count) / max(self._log_scale, 1e-8))
        er = (
            self._beta_1 * similarity
            + self._beta_2 * alignment
            + self._beta_3 * er_beta3_term
        )
        er = max(0.0, min(er, 1.0))

        # --- R_g ---
        r_g = (
            self._alpha_1 * sl
            + self._alpha_2 * pc_norm
            + self._alpha_3 * er
        )
        r_g = max(0.0, min(r_g, 1.0))

        # --- 실패 카테고리 분류 ---
        failure_category = self._classify_failure(sl, pc_norm, alignment, similarity, ic)

        passed = failure_category is None and r_g < self._r_g_threshold

        # reason 한 줄 요약
        if passed:
            reason = (
                f"PASS: R_g={r_g:.3f} < {self._r_g_threshold}, "
                f"IC={ic:.4f}, SL={sl:.3f}, PC={pc_norm:.3f}, ER={er:.3f}"
            )
        else:
            reason = (
                f"FAIL({failure_category}): R_g={r_g:.3f}, "
                f"IC={ic:.4f}, SL={sl:.3f}, PC={pc_norm:.3f}, ER={er:.3f}"
            )

        logger.info("[eval_agent] %s candidate_id=%s", reason, candidate.candidate_id)

        return EvalResult(
            candidate_id=candidate.candidate_id,
            r_g=r_g,
            sl=sl,
            pc=pc_norm,
            er=er,
            ic=ic,
            rank_ic=rank_ic,
            passed=passed,
            failure_category=failure_category,
            reason=reason,
        )

    # ------------------------------------------------------------------ #
    # Component calculations
    # ------------------------------------------------------------------ #

    def _compute_sl(self, candidate: Any) -> float:
        """SL(f) = ast_node_count / max_ast_complexity. 0~1 클립."""
        node_count: int = getattr(candidate, "ast_node_count", 0)
        sl = node_count / max(self._max_ast_complexity, 1)
        return max(0.0, min(sl, self._sl_clip_max))  # 초과 허용 (1.0 이상 = complexity_violation)

    def _compute_pc(self, code: str) -> int:
        """PC(f): AST에서 숫자 리터럴(Constant) 카운트.

        window 파라미터 수를 proxy로 사용. int/float Constant 노드 수.
        """
        if not code.strip():
            return 0
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return 0
        count = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                count += 1
        return count

    def _compute_alignment(self, hypothesis: Any, candidate: Any) -> float:
        """C(h,d,f): LLM 평가 가설↔수식 정합 (0~1, 낮을수록 좋음).

        c₁: hypothesis ↔ description 정합 (GPT-4o 평가).
        c₂: description ↔ factor code 정합 (GPT-4o 평가).
        C(h,d,f) = 0.5·c₁ + 0.5·c₂

        LLM 없으면 0.5 fallback.
        """
        if self._llm_router is None:
            logger.debug("[eval_agent] llm_router 없음. alignment fallback=0.5")
            return 0.5

        obs = getattr(hypothesis, "observation", "")
        just = getattr(hypothesis, "justification", "")
        description = getattr(candidate, "description", "")
        code_snippet = (candidate.code or "")[:200]

        c1 = self._llm_score_alignment(
            f"관찰: {obs}\n근거: {just}",
            description,
            "가설과 description",
        )
        c2 = self._llm_score_alignment(
            description,
            code_snippet,
            "description과 factor code",
        )
        return self._c1_w * c1 + self._c2_w * c2

    def _llm_score_alignment(self, text_a: str, text_b: str, label: str) -> float:
        """GPT-4o로 text_a↔text_b 정합 점수 (0~1) 반환. 실패 시 0.5."""
        prompt = (
            "당신은 퀀트 팩터 평가자다.\n"
            f"아래 {label}의 정합성을 0~1로 평가하라.\n"
            "(0=완전 일치, 1=전혀 불일치)\n\n"
            f"[A]\n{text_a}\n\n"
            f"[B]\n{text_b}\n\n"
            "정합성 점수 (0.0~1.0 숫자만 반환):"
        )
        try:
            response = self._llm_router.call(
                prompt, mode="mode_b", caller="factor_evaluation"
            )
            score = float(_llm_content(response).strip().split()[0])
            return max(0.0, min(score, 1.0))
        except Exception as e:
            logger.warning("[eval_agent] alignment LLM 호출 실패: %s. fallback=0.5", e)
            return 0.5

    def _compute_similarity(self, candidate: Any, factor_zoo: list[dict]) -> float:
        """S(f): 기존 zoo와 IC 유사도 최대값 (0~1).

        zoo의 active 팩터와 동일 synthetic 데이터에서 IC를 계산하고
        두 팩터 IC 시리즈의 상관을 구한다.
        즉, 두 팩터의 cross-IC 상관 >= ic_duplicate_threshold → crowding_risk.

        단순화 구현: 동일 synthetic df에서 두 팩터 scores를 구하고 correlation.
        """
        if not factor_zoo:
            return 0.0

        active_zoo = [e for e in factor_zoo if e.get("status") == "active" and e.get("code")]
        if not active_zoo:
            return 0.0

        # candidate code 실행
        cand_scores = _exec_factor_scores(candidate.code)
        if cand_scores is None:
            return 0.0

        max_sim = 0.0
        for entry in active_zoo:
            zoo_scores = _exec_factor_scores(entry["code"])
            if zoo_scores is None:
                continue
            # 두 팩터 스코어 간 Spearman 상관
            valid = cand_scores.notna() & zoo_scores.notna()
            if valid.sum() < self._min_valid_samples:
                continue
            try:
                corr, _ = stats.spearmanr(
                    cand_scores[valid].values,
                    zoo_scores[valid].values,
                )
                sim = abs(float(corr)) if not np.isnan(corr) else 0.0
                if sim > max_sim:
                    max_sim = sim
            except Exception as e:
                logger.warning("[eval_agent] similarity 계산 실패: %s", e)
        return max_sim

    def _compute_ic(self, code: str) -> tuple[float, float]:
        """IC, RankIC 계산 (synthetic OHLCV 50 bars).

        PIT-Safety: forward_returns = close.pct_change(1).shift(-1)
          → t 시점에서 t+1 수익률 예측. 미래 데이터를 학습에 사용하지 않고
            오직 검증 목적의 synthetic 데이터에서만 연산.

        Returns:
            (ic, rank_ic). 계산 불가 시 (0.0, 0.0).
        """
        df = _make_synthetic_df(n=self._synthetic_bars, seed=self._synthetic_seed)
        scores = _exec_factor_scores(code, df)
        if scores is None:
            return 0.0, 0.0

        # PIT-safe: shift(-1) → 1-step ahead forward return
        forward_returns = df["close"].pct_change(1).shift(-1)

        valid = scores.notna() & forward_returns.notna()
        if valid.sum() < self._min_valid_samples:
            logger.warning("[eval_agent] IC 계산 유효 샘플 부족: %d개", valid.sum())
            return 0.0, 0.0

        try:
            ic_result = stats.spearmanr(
                scores[valid].values,
                forward_returns[valid].values,
            )
            ic = float(ic_result.correlation) if not np.isnan(ic_result.correlation) else 0.0
        except Exception as e:
            logger.warning("[eval_agent] IC spearmanr 실패: %s", e)
            return 0.0, 0.0

        try:
            # RankIC: rank-rank spearman
            s_ranked = scores[valid].rank()
            r_ranked = forward_returns[valid].rank()
            rank_ic_result = stats.spearmanr(s_ranked.values, r_ranked.values)
            rank_ic = float(rank_ic_result.correlation) if not np.isnan(rank_ic_result.correlation) else 0.0
        except Exception as e:
            logger.warning("[eval_agent] RankIC 계산 실패: %s", e)
            rank_ic = ic  # fallback to ic

        return ic, rank_ic

    # ------------------------------------------------------------------ #
    # Failure classification
    # ------------------------------------------------------------------ #

    def _classify_failure(
        self,
        sl: float,
        pc_norm: float,
        alignment: float,
        similarity: float,
        ic: float,
    ) -> str | None:
        """실패 카테고리 5종 분류. None=PASS.

        우선순위:
          1. complexity_violation: SL > 1.0
          2. poor_ic: |IC| < ic_min_threshold
          3. crowding_risk: S(f) >= ic_duplicate_threshold
          4. hypothesis_misalignment: C(h,d,f) > alignment_threshold
          None: 전부 통과

        execution_failure는 _compute_ic 내부에서 ic=0.0으로 처리 후
        poor_ic로 귀결. 명시적 실행 예외는 evaluate()에서 처리.
        """
        _ = pc_norm  # PC는 R_g 계산에만 사용 (임계값 기반 단독 실패 없음)

        if sl > 1.0:
            return "complexity_violation"
        if abs(ic) < self._ic_min:
            return "poor_ic"
        if similarity >= self._ic_dup_threshold:
            return "crowding_risk"
        if alignment > self._align_thresh:
            return "hypothesis_misalignment"
        return None


# ------------------------------------------------------------------ #
# Module-level helpers (not bound to EvalAgent)
# ------------------------------------------------------------------ #

def _make_synthetic_df(n: int = 50, seed: int = 42) -> pd.DataFrame:
    """IC 계산용 synthetic OHLCV DataFrame (50 bars).

    numpy seed 고정 → 동일 데이터로 반복 평가 가능.
    seed: risk_config.yaml eval_agent.synthetic_seed에서 로드 (불변 원칙 5).
          EvalAgent.__init__에서 self._synthetic_seed로 로드 후 호출 시 주입.
    """
    rng = np.random.default_rng(seed=seed)
    base_price = np.abs(rng.standard_normal(n) * 100 + 1000)
    return pd.DataFrame({
        "close":                  base_price,
        "open":                   np.abs(rng.standard_normal(n) * 100 + 1000),
        "high":                   base_price * (1 + rng.uniform(0, 0.02, n)),
        "low":                    base_price * (1 - rng.uniform(0, 0.02, n)),
        "volume":                 np.abs(rng.standard_normal(n) * 1_000_000 + 5_000_000),
        "vwap":                   base_price * (1 + rng.standard_normal(n) * 0.001),
        "turnover":               np.abs(rng.standard_normal(n) * 1_000_000_000 + 5_000_000_000),
        "foreign_net_buy":        rng.standard_normal(n) * 1_000_000_000,
        "institutional_net_buy":  rng.standard_normal(n) * 1_000_000_000,
        "retail_net_buy":         rng.standard_normal(n) * 1_000_000_000,
    })


def _exec_factor_scores(
    code: str,
    df: pd.DataFrame | None = None,
) -> pd.Series | None:
    """factor(df) 코드 실행 후 pd.Series 반환. 실패 시 None.

    OPERATOR_NAMESPACE 전체를 exec namespace에 주입.
    """
    if not code or not code.strip():
        return None
    if df is None:
        df = _make_synthetic_df()

    exec_ns: dict[str, Any] = {**OPERATOR_NAMESPACE, "df": df}
    try:
        exec(compile(code, "<eval_factor>", "exec"), exec_ns)  # noqa: S102
    except Exception as e:
        logger.debug("[eval_agent] factor exec 실패: %s", e)
        return None

    factor_fn = exec_ns.get("factor")
    if factor_fn is None:
        return None

    try:
        result = factor_fn(df)
        if isinstance(result, pd.Series):
            return result
        return None
    except Exception as e:
        logger.debug("[eval_agent] factor(df) 실행 오류: %s", e)
        return None


def _count_features(code: str) -> int:
    """factor code에서 df 컬럼 참조 수 추정.

    'df["col"]' 또는 "df['col']" 패턴 카운트. 중복 포함.
    """
    if not code:
        return 0
    pattern = r"df\[[\'\"](\w+)[\'\"]\]"
    matches = re.findall(pattern, code)
    return len(set(matches))
