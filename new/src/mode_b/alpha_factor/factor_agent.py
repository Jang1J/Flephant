"""S3-2 Alpha Factor Engine: Factor Agent.

AlphaAgent 논문 §2. 가설(Hypothesis) → Operator Library 기반 Python factor 코드 생성.
AST 파싱 + 실행 검증 + 중복 탐지. 실패 시 max_retries(risk_config.yaml) 재시도.

caller='factor_implementation' (risk_config.yaml mode_b_allowed_callers 화이트리스트).
Mode B 전용. 장중 호출 금지.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from src.mode_b.alpha_factor.idea_agent import Hypothesis
from src.mode_b.alpha_factor.operators import OPERATOR_NAMESPACE
from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger
from src.utils.mode_guard import mode_b_only

logger = get_logger("factor_agent")
_KST = ZoneInfo("Asia/Seoul")

# ------------------------------------------------------------------ #
# FactorCandidate dataclass
# ------------------------------------------------------------------ #

@dataclass
class FactorCandidate:
    """Factor Zoo 저장 단위.

    Fields:
        candidate_id:     "FAC-{YYYYMMDD}-{UUID8}" 형식
        hypothesis_id:    생성 기반 Hypothesis ID
        code:             factor(df) Python 코드 문자열
        ast_hash:         SHA-256[:16] of ast.dump(code)
        ast_node_count:   AST 노드 수 (복잡도 측정)
        description:      Hypothesis.specification 기반 설명
        status:           "active" | "duplicate" | "failed"
        attempt_count:    총 시도 횟수 (재시도 포함)
        created_at:       ISO timestamp (KST)
        error:            실패 메시지 (성공 시 None)
    """

    candidate_id: str
    hypothesis_id: str
    code: str
    ast_hash: str
    ast_node_count: int
    description: str
    status: str  # "active" | "duplicate" | "failed"
    attempt_count: int
    created_at: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ------------------------------------------------------------------ #
# 검증용 test_df 생성 헬퍼
# ------------------------------------------------------------------ #

def _make_test_df(n: int = 50, seed: int = 42) -> pd.DataFrame:
    """factor 실행 검증용 dummy DataFrame (n 행).

    seed: risk_config.yaml alpha_factor.factor_agent_seed에서 로드 (불변 원칙 5).
          FactorAgent.__init__에서 self._test_df_seed로 로드 후 implement() 호출 시 주입.
    """
    rng = np.random.default_rng(seed=seed)
    return pd.DataFrame({
        "close":                  np.abs(rng.standard_normal(n) * 100 + 1000),
        "open":                   np.abs(rng.standard_normal(n) * 100 + 1000),
        "high":                   np.abs(rng.standard_normal(n) * 100 + 1050),
        "low":                    np.abs(rng.standard_normal(n) * 100 + 950),
        "volume":                 np.abs(rng.standard_normal(n) * 1_000_000),
        "vwap":                   np.abs(rng.standard_normal(n) * 100 + 1000),
        "turnover":               np.abs(rng.standard_normal(n) * 1_000_000_000),
        "foreign_net_buy":        rng.standard_normal(n) * 1_000_000_000,
        "institutional_net_buy":  rng.standard_normal(n) * 1_000_000_000,
        "retail_net_buy":         rng.standard_normal(n) * 1_000_000_000,
    })


# ------------------------------------------------------------------ #
# FactorAgent
# ------------------------------------------------------------------ #

class FactorAgent:
    """GPT-4o 기반 Alpha Factor 코드 생성 에이전트.

    Mode B 전용. LLMRouter caller='factor_implementation'.
    AST parse → exec 검증 → 중복 탐지 → Factor Zoo 저장.
    실패 시 max_retries 재시도 (error message를 프롬프트에 포함).

    llm_router: LLMRouter 인스턴스 주입. None이면 fallback factor 사용.
    """

    def __init__(self, llm_router: Any = None) -> None:
        cfg = config_load("risk_config.yaml", "alpha_factor") or {}
        factor_zoo_path = cfg.get(
            "factor_zoo_path", "artifacts/alpha_factor/factor_zoo.jsonl"
        )
        self._factor_zoo_path = Path(factor_zoo_path)
        self._factor_zoo_path.parent.mkdir(parents=True, exist_ok=True)

        self._max_retries: int = int(cfg.get("max_retries", 3))
        self._max_ast_complexity: int = int(cfg.get("max_ast_complexity", 10))
        # W3 yaml화: test_df seed (불변 원칙 5)
        self._test_df_seed: int = int(cfg.get("factor_agent_seed", 42))
        self._llm_router = llm_router
        logger.info(
            "[factor_agent] 초기화. zoo=%s max_retries=%d max_ast=%d test_df_seed=%d",
            self._factor_zoo_path,
            self._max_retries,
            self._max_ast_complexity,
            self._test_df_seed,
        )

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    @mode_b_only
    def implement(
        self,
        hypothesis: Hypothesis,
        max_retries: int | None = None,
    ) -> FactorCandidate:
        """가설 → FactorCandidate 변환.

        Args:
            hypothesis:  IdeaAgent가 생성한 Hypothesis.
            max_retries: 재시도 횟수 (None이면 yaml 값 사용).

        Returns:
            FactorCandidate: status 중 하나 ("active"|"duplicate"|"failed").
        """
        retries = max_retries if max_retries is not None else self._max_retries
        test_df = _make_test_df(seed=self._test_df_seed)
        attempt = 0
        previous_error: str | None = None

        while attempt <= retries:
            attempt += 1
            logger.info(
                "[factor_agent] 구현 시도 %d/%d hypothesis=%s",
                attempt, retries + 1, hypothesis.hypothesis_id,
            )

            # 1. LLM 코드 생성
            raw_response = self._call_llm(hypothesis, previous_error)
            code = self._parse_code(raw_response)

            # 2. AST 파싱 + 복잡도 검사
            ast_hash, node_count, ast_error = self._compute_ast_hash(code)
            if ast_error:
                previous_error = f"AST 파싱 실패: {ast_error}"
                logger.warning("[factor_agent] %s", previous_error)
                continue

            if node_count > self._max_ast_complexity:
                previous_error = (
                    f"AST 노드 수 {node_count}가 상한 {self._max_ast_complexity}을 초과. "
                    f"더 간결하게 구현하라."
                )
                logger.warning("[factor_agent] AST complexity 초과: %d > %d", node_count, self._max_ast_complexity)
                continue

            # 3. 중복 탐지
            if self._is_duplicate(ast_hash):
                logger.info("[factor_agent] 중복 팩터 탐지: ast_hash=%s", ast_hash)
                candidate = FactorCandidate(
                    candidate_id=self._new_candidate_id(),
                    hypothesis_id=hypothesis.hypothesis_id,
                    code=code,
                    ast_hash=ast_hash,
                    ast_node_count=node_count,
                    description=hypothesis.specification,
                    status="duplicate",
                    attempt_count=attempt,
                    created_at=datetime.now(_KST).isoformat(),
                    error="중복 AST hash",
                )
                self._save_candidate(candidate)
                return candidate

            # 4. 실행 검증
            ok, exec_error = self._validate_code(code, test_df)
            if not ok:
                previous_error = f"실행 검증 실패: {exec_error}"
                logger.warning("[factor_agent] %s", previous_error)
                continue

            # 성공
            candidate = FactorCandidate(
                candidate_id=self._new_candidate_id(),
                hypothesis_id=hypothesis.hypothesis_id,
                code=code,
                ast_hash=ast_hash,
                ast_node_count=node_count,
                description=hypothesis.specification,
                status="active",
                attempt_count=attempt,
                created_at=datetime.now(_KST).isoformat(),
                error=None,
            )
            self._save_candidate(candidate)
            logger.info(
                "[factor_agent] 팩터 생성 완료: %s status=active", candidate.candidate_id
            )
            return candidate

        # 최대 재시도 초과 → failed
        logger.error(
            "[factor_agent] 최대 재시도(%d) 초과. hypothesis=%s",
            retries, hypothesis.hypothesis_id,
        )
        candidate = FactorCandidate(
            candidate_id=self._new_candidate_id(),
            hypothesis_id=hypothesis.hypothesis_id,
            code="",
            ast_hash="",
            ast_node_count=0,
            description=hypothesis.specification,
            status="failed",
            attempt_count=attempt,
            created_at=datetime.now(_KST).isoformat(),
            error=previous_error or "max_retries 초과",
        )
        self._save_candidate(candidate)
        return candidate

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _call_llm(
        self,
        hypothesis: Hypothesis,
        previous_error: str | None,
    ) -> str:
        """LLM 호출. llm_router 없으면 fallback 코드 반환."""
        if self._llm_router is None:
            return self._fallback_code(hypothesis)

        prompt = self._build_prompt(hypothesis, previous_error)
        try:
            return self._llm_router.call(
                prompt, mode="mode_b", caller="factor_implementation"
            )
        except Exception as e:
            logger.warning("[factor_agent] LLM 호출 실패: %s. fallback 사용", e)
            return self._fallback_code(hypothesis)

    def _build_prompt(
        self, hypothesis: Hypothesis, previous_error: str | None = None
    ) -> str:
        """GPT-4o factor 코드 생성 프롬프트."""
        operator_list = (
            "rolling_mean(series, window), rolling_std(series, window), "
            "rolling_min(series, window), rolling_max(series, window), "
            "cs_rank(series), ts_rank(series, window), ts_zscore(series, window), "
            "ts_momentum(series, window), correlation(a, b, window), ema(series, span), "
            "volume_ratio(volume, window), price_range(high, low), "
            "vwap_deviation(close, vwap), turnover_rate(turnover, window), "
            "rank(series), ts_argmax(series, window), ts_argmin(series, window), "
            "rsi(series, window)"
        )

        error_section = ""
        if previous_error:
            error_section = f"\n## 이전 구현 오류 (반드시 수정)\n{previous_error}\n"

        return (
            "당신은 KOSPI 퀀트 팩터 엔지니어다.\n"
            "아래 가설을 Python factor 함수로 구현하라.\n\n"
            "## 가설\n"
            f"observation: {hypothesis.observation}\n"
            f"knowledge: {hypothesis.knowledge}\n"
            f"justification: {hypothesis.justification}\n"
            f"specification: {hypothesis.specification}\n"
            "\n## 함수 요구사항\n"
            "- 시그니처: def factor(df: pd.DataFrame) -> pd.Series:\n"
            "- df columns: close, open, high, low, volume, vwap, turnover, "
            "foreign_net_buy, institutional_net_buy, retail_net_buy\n"
            "- 반환: pd.Series (크로스섹션 팩터 스코어, 클수록 매수)\n"
            "- 반드시 pd.Series 반환\n"
            "- NaN 비율 < 80% 유지\n"
            "\n## 사용 가능한 연산자 (모두 df column과 호환)\n"
            f"{operator_list}\n"
            "\n## 코드 길이 제한\n"
            f"AST 노드 수 <= {self._max_ast_complexity} (과적합 방지)\n"
            f"{error_section}"
            "\n```python\n"
            "def factor(df):\n"
            "    ...\n"
            "```"
        )

    def _parse_code(self, response: str) -> str:
        """응답에서 ```python ... ``` 블록 추출. 없으면 전체 텍스트 반환."""
        match = re.search(r"```python\s*(.*?)\s*```", response, re.DOTALL)
        if match:
            return match.group(1).strip()
        # python 없는 일반 코드 블록
        match = re.search(r"```\s*(.*?)\s*```", response, re.DOTALL)
        if match:
            return match.group(1).strip()
        return response.strip()

    def _validate_code(
        self, code: str, test_df: pd.DataFrame
    ) -> tuple[bool, str]:
        """factor(df) exec 실행 + 반환 타입/NaN 비율 검증.

        Returns:
            (True, "") or (False, error_message)
        """
        namespace = dict(OPERATOR_NAMESPACE)
        try:
            exec(compile(code, "<factor>", "exec"), namespace)  # noqa: S102
        except Exception as e:
            return False, f"compile/exec 실패: {e}"

        factor_fn = namespace.get("factor")
        if factor_fn is None:
            return False, "factor 함수 미정의: code에 'def factor(df):' 없음"

        try:
            result = factor_fn(test_df)
        except Exception as e:
            return False, f"factor(df) 실행 오류: {e}"

        if not isinstance(result, pd.Series):
            return False, f"pd.Series 반환 필수. 실제 반환 타입: {type(result).__name__}"

        nan_ratio = result.isna().mean()
        if nan_ratio >= 0.80:
            return False, f"NaN 비율 {nan_ratio:.2%} >= 80%. factor 로직 재검토 필요"

        return True, ""

    def _compute_ast_hash(self, code: str) -> tuple[str, int, str]:
        """AST hash(16자) + 노드 수 계산.

        Returns:
            (ast_hash, node_count, error_message)
            error_message 비어있으면 성공.
        """
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return "", 0, f"SyntaxError: {e}"

        dump = ast.dump(tree, annotate_fields=False)
        ast_hash = hashlib.sha256(dump.encode()).hexdigest()[:16]
        node_count = sum(1 for _ in ast.walk(tree))
        return ast_hash, node_count, ""

    def _is_duplicate(self, ast_hash: str) -> bool:
        """factor_zoo.jsonl에서 동일 ast_hash 존재 여부 확인."""
        if not ast_hash:
            return False
        zoo = self._load_factor_zoo()
        return any(entry.get("ast_hash") == ast_hash for entry in zoo)

    def _save_candidate(self, candidate: FactorCandidate) -> None:
        """Factor Zoo JSONL에 append."""
        with self._factor_zoo_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(candidate.to_dict(), ensure_ascii=False) + "\n")

    def _load_factor_zoo(self) -> list[dict[str, Any]]:
        """factor_zoo.jsonl 전체 로드. 파일 없으면 빈 리스트."""
        if not self._factor_zoo_path.exists():
            return []
        entries: list[dict[str, Any]] = []
        with self._factor_zoo_path.open("r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entries.append(json.loads(stripped))
                except json.JSONDecodeError as e:
                    logger.warning("[factor_agent] factor_zoo JSON 파싱 실패: %s", e)
        return entries

    @staticmethod
    def _new_candidate_id() -> str:
        """FAC-{YYYYMMDD}-{UUID8} 형식 ID 생성."""
        now = datetime.now(ZoneInfo("Asia/Seoul"))
        return f"FAC-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"

    def _fallback_code(self, hypothesis: Hypothesis) -> str:
        """LLM 없을 때 hypothesis.specification 기반 fallback 코드."""
        _ = hypothesis  # spec 힌트 활용 여지 (현재 단순 template)
        return (
            "```python\n"
            "def factor(df):\n"
            "    \"\"\"Fallback: VWAP 편차 기반 모멘텀 팩터.\"\"\"\n"
            "    dev = vwap_deviation(df['close'], df['vwap'])\n"
            "    return rank(dev)\n"
            "```"
        )
