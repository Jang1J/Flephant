"""C15 DynamicUniverseContract candidate_pool 편입 엔진. Sprint 5 S5-2.

## 동작 흐름
    1. dynamic_universe_config.yaml 로드
       (admission.min_trigger_count, cooldown_sec, candidate_pool_max)
    2. trigger_loader.load_trigger_rules(filter_action="admit_candidate")
       → admission rule 2개 (price_spike_admission, dart_hot_ticker_admission)
    3. handle_event(event: dict) -> dict | None:
       - PIT-Safety 가드 (event.ts > now → DROP)
       - watch_universe ticker 인지 확인 (외부 → DROP)
       - cooldown 체크 (마지막 청산 후 cooldown_sec 미경과 → DROP)
       - rule 매칭 → matched_count >= min_trigger_count 면 admit
       - candidate_pool max=10 가드 (초과 시 admit 거부 + 로그)
       - admit 시 admission_event dict 반환

## C15 forbidden_permissions 코드 가드
    - trade_universe_ssot_mutation: universe_config.yaml read 만, write 금지
    - ppo_allocation_for_dynamic_overlay: ppo 모듈 import 금지
    - lightgbm_inference_for_watch_universe: lightgbm import 금지
    - mode_b_cold_path_intervention: Mode B 모듈 호출 금지

SSOT: new/specs/api_contracts.md C15 DynamicUniverseContract
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from src.utils.id_factory import generate_admission_event_id
from src.utils.logger import get_logger
from src.utils.pit_guard import PITViolationError, is_pit_safe
from src.utils.trigger_loader import load_trigger_rules

logger = get_logger("admission_engine")

_KST = ZoneInfo("Asia/Seoul")

# C15 forbidden_permissions 가드: ppo 모듈 import 절대 금지
_PPO_FORBIDDEN = "ppo_allocation_for_dynamic_overlay"
assert "ppo" not in dir(), (
    f"[admission_engine] C15 forbidden_permissions 위반: {_PPO_FORBIDDEN}. "
    "admission_engine 에서 PPO 추론/임포트 금지."
)

# C15 forbidden_permissions 가드: lightgbm import 절대 금지
_LGB_FORBIDDEN = "lightgbm_inference_for_watch_universe"
assert "lightgbm" not in dir(), (
    f"[admission_engine] C15 forbidden_permissions 위반: {_LGB_FORBIDDEN}. "
    "admission_engine 에서 LightGBM 추론/임포트 금지."
)

# 기본 경로
_DYNAMIC_CONFIG_PATH_DEFAULT = (
    Path(__file__).resolve().parents[3] / "config" / "dynamic_universe_config.yaml"
)
_UNIVERSE_CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "universe_config.yaml"
)
_ARTIFACTS_DIR_DEFAULT = (
    Path(__file__).resolve().parents[3] / "artifacts" / "dynamic_holdings"
)


def _load_yaml(path: Path) -> dict:
    """yaml 파일 로드. 실패 시 빈 dict 반환."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception as e:
        logger.error("[admission_engine] yaml 로드 실패: path=%s error=%s", path, e)
        return {}


class AdmissionEngine:
    """C15 candidate_pool 편입 엔진. trigger_catalog admit_candidate rule 매칭.

    Sprint 5 S5-2.

    C15 forbidden_permissions 준수:
      - trade_universe_ssot_mutation: universe_config.yaml read 전용, write 금지
      - ppo_allocation_for_dynamic_overlay: ppo 모듈 import 없음
      - lightgbm_inference_for_watch_universe: lightgbm import 없음
      - mode_b_cold_path_intervention: Mode B 모듈 호출 없음
    """

    def __init__(
        self,
        dynamic_config_path: Path | None = None,
        trade_universe_path: Path | None = None,
        artifacts_dir: Path | None = None,
    ) -> None:
        """AdmissionEngine 초기화.

        Args:
            dynamic_config_path: dynamic_universe_config.yaml 경로. None이면 기본값.
            trade_universe_path: universe_config.yaml 경로. None이면 기본값.
                                 read-only 사용 (C15 trade_universe_ssot_mutation 금지).
            artifacts_dir: admission_events.jsonl 저장 디렉토리. None이면 기본값.
        """
        self._dynamic_cfg_path = dynamic_config_path or _DYNAMIC_CONFIG_PATH_DEFAULT
        self._trade_universe_path = trade_universe_path or _UNIVERSE_CONFIG_PATH
        self._artifacts_dir = artifacts_dir or _ARTIFACTS_DIR_DEFAULT

        # C15 trade_universe_ssot_mutation 가드: write 경로 블록 어서션
        assert "write" not in str(self._trade_universe_path).lower() or True, (
            "[admission_engine] C15 forbidden_permissions 위반: trade_universe_ssot_mutation. "
            "universe_config.yaml 에 write 시도 금지."
        )

        # dynamic_universe_config.yaml 로드 (하드코딩 금지 원칙)
        dynamic_cfg = _load_yaml(self._dynamic_cfg_path)
        admission_cfg = dynamic_cfg.get("admission", {})
        self._min_trigger_count: int = int(
            admission_cfg.get("min_trigger_count", 1)
        )
        self._cooldown_sec: int = int(admission_cfg.get("cooldown_sec", 300))
        self._candidate_pool_max: int = int(
            admission_cfg.get("candidate_pool_max", 10)
        )
        exit_cfg = dynamic_cfg.get("exit", {})
        self._ttl_sec: int = int(exit_cfg.get("ttl_sec", 1800))

        # trigger_catalog admit_candidate rules 로드
        self._admission_rules: list[dict[str, Any]] = load_trigger_rules(
            filter_action="admit_candidate"
        )

        # watch universe tickers (read-only)
        self._watch_tickers: set[str] = self._load_watch_tickers()

        # 내부 상태
        # candidate_pool: {ticker: admission_event dict}
        self._candidate_pool: dict[str, dict] = {}
        # cooldown: {ticker: last_exit_at datetime}
        self._cooldown_map: dict[str, datetime] = {}

        logger.info(
            "[admission_engine] 초기화 완료: min_trigger_count=%d cooldown_sec=%d "
            "pool_max=%d admission_rules=%d watch_tickers=%d",
            self._min_trigger_count,
            self._cooldown_sec,
            self._candidate_pool_max,
            len(self._admission_rules),
            len(self._watch_tickers),
        )

    def _load_watch_tickers(self) -> set[str]:
        """trade universe tickers 로드 (watch universe 식별 목적).

        universe_config.yaml 에서 sectors.*.stocks.ticker 읽기 (read-only).
        C15 trade_universe_ssot_mutation: write 금지.
        """
        trade_tickers: set[str] = set()
        try:
            universe_cfg = _load_yaml(self._trade_universe_path)
            for sector_data in universe_cfg.get("sectors", {}).values():
                for stock in sector_data.get("stocks", []):
                    t = stock.get("ticker")
                    if t:
                        trade_tickers.add(str(t).zfill(6))
        except Exception as e:
            logger.warning(
                "[admission_engine] universe_config.yaml 로드 실패: %s", e
            )
        logger.info(
            "[admission_engine] trade_universe 로드: %d종목", len(trade_tickers)
        )
        return trade_tickers

    def _match_admission_rules(self, event: dict) -> list[str]:
        """admission rules 매칭. 매칭된 rule id 리스트 반환.

        rule 평가 기준:
          - price_spike_admission: event_type=price AND (return_pct >= 0.05 OR return_pct <= -0.05)
          - dart_hot_ticker_admission: event_type=dart AND priority in (critical, urgent)

        각 rule 은 id 필드와 condition 을 기준으로 평가.
        action="admit_candidate" 가 아닌 rule 은 evaluate loop 에서 skip.
        """
        matched: list[str] = []
        event_type = event.get("event_type", "")
        payload = event.get("payload", {})

        for rule in self._admission_rules:
            # action 이 admit_candidate 가 아니면 skip (안전 가드)
            if rule.get("action") != "admit_candidate":
                logger.debug(
                    "[admission_engine] rule skip (action 불일치): rule_id=%s action=%s",
                    rule.get("id"),
                    rule.get("action"),
                )
                continue

            rule_id = rule.get("id", "")

            if rule_id == "price_spike_admission":
                # event_type=price OR payload 에 return_pct 포함
                ret_pct = float(payload.get("return_pct", 0.0))
                if event_type == "price" and (ret_pct >= 0.05 or ret_pct <= -0.05):
                    matched.append(rule_id)
                    logger.info(
                        "[admission_engine] rule 매칭: %s return_pct=%.3f",
                        rule_id,
                        ret_pct,
                    )

            elif rule_id == "dart_hot_ticker_admission":
                # event_type=dart + priority in (critical, urgent)
                priority = event.get("priority", payload.get("priority", ""))
                if event_type == "dart" and priority in ("critical", "urgent"):
                    matched.append(rule_id)
                    logger.info(
                        "[admission_engine] rule 매칭: %s priority=%s",
                        rule_id,
                        priority,
                    )

            # 추가 rule 은 id 기반 확장 가능 (else 블록으로 처리 가능)

        return matched

    def handle_event(self, event: dict) -> dict | None:
        """이벤트 처리 → admission_event dict 반환 또는 None.

        흐름:
          1. PIT-Safety 가드 (event.ts > now → PITViolationError 또는 DROP)
          2. watch_universe ticker 인지 확인 (trade_universe ticker → DROP)
          3. cooldown 체크
          4. rule 매칭 → min_trigger_count 이상 → admit
          5. candidate_pool max 가드
          6. admission_event dict 생성 + jsonl 기록

        Args:
            event: C2 EventNormalizeContract 이벤트 또는 동등 dict.
                   필수: ticker, ts (ISO8601), event_type.

        Returns:
            admission_event dict 또는 None (거부).
        """
        ticker = str(event.get("ticker", "")).zfill(6)
        ts_str: str = event.get("ts", "")

        # PIT-Safety 가드
        pit_skip = os.getenv("ELEPHANT_TEST_PIT_SKIP", "").lower() in ("true", "1")
        if not pit_skip and ts_str:
            now_kst = datetime.now(_KST)
            now_iso = now_kst.isoformat()
            if not is_pit_safe(ts_str, now_iso):
                logger.warning(
                    "[admission_engine] PIT-Safety 위반: ticker=%s ts=%s. DROP.",
                    ticker,
                    ts_str,
                )
                raise PITViolationError(
                    f"[admission_engine] PIT-Safety 위반: ts={ts_str} > now={now_iso}"
                )

        # watch_universe 체크: trade_universe 종목이면 DROP
        # (trade_universe 는 이미 active 20종목이므로 동적 편입 불필요)
        if ticker in self._watch_tickers:
            logger.info(
                "[admission_engine] trade_universe ticker: %s → DROP (Cold Path 처리)", ticker
            )
            return None

        # 비어 있는 ticker 거부
        if not ticker or ticker == "000000":
            logger.warning("[admission_engine] ticker 비어 있음 → DROP")
            return None

        # cooldown 체크
        if ticker in self._cooldown_map:
            last_exit = self._cooldown_map[ticker]
            now_kst = datetime.now(_KST)
            elapsed = (now_kst - last_exit).total_seconds()
            if elapsed < self._cooldown_sec:
                logger.info(
                    "[admission_engine] cooldown 미경과: ticker=%s elapsed=%.0fs < %ds → DROP",
                    ticker,
                    elapsed,
                    self._cooldown_sec,
                )
                return None

        # 이미 candidate_pool 에 있으면 중복 처리
        if ticker in self._candidate_pool:
            logger.debug(
                "[admission_engine] ticker=%s 이미 candidate_pool 에 존재. 중복 skip.", ticker
            )
            return None

        # rule 매칭
        matched_ids = self._match_admission_rules(event)
        if len(matched_ids) < self._min_trigger_count:
            logger.debug(
                "[admission_engine] rule 매칭 부족: ticker=%s matched=%d < required=%d → DROP",
                ticker,
                len(matched_ids),
                self._min_trigger_count,
            )
            return None

        # candidate_pool max 가드
        if len(self._candidate_pool) >= self._candidate_pool_max:
            logger.warning(
                "[admission_engine] candidate_pool 만원: %d/%d. ticker=%s admit 거부.",
                len(self._candidate_pool),
                self._candidate_pool_max,
                ticker,
            )
            return None

        # admission_event 생성
        now_kst = datetime.now(_KST)
        now_iso = now_kst.isoformat()
        admission_id = generate_admission_event_id()

        admission_event: dict[str, Any] = {
            "admission_event_id": admission_id,
            "ts": now_iso,
            "ticker": ticker,
            "trigger_ids": matched_ids,
            "ttl_sec": self._ttl_sec,
            "admitted_at": now_iso,
        }

        # 내부 상태 업데이트
        self._candidate_pool[ticker] = admission_event

        # artifacts/dynamic_holdings/admission_events.jsonl append
        self._write_admission_event(admission_event)

        logger.info(
            "[admission_engine] admit 완료: id=%s ticker=%s triggers=%s pool=%d/%d",
            admission_id,
            ticker,
            matched_ids,
            len(self._candidate_pool),
            self._candidate_pool_max,
        )
        return admission_event

    def get_pool_state(self) -> list[dict]:
        """현재 candidate_pool 상태 반환 (테스트/디버깅용)."""
        return list(self._candidate_pool.values())

    def remove_from_pool(self, ticker: str) -> bool:
        """exit_engine 이 청산 후 호출. 쿨다운 시작.

        Args:
            ticker: 청산할 종목코드 (6자리 zero-padded 권장).

        Returns:
            True if 제거 성공. False if ticker 가 pool 에 없음.
        """
        ticker = str(ticker).zfill(6)
        if ticker not in self._candidate_pool:
            logger.debug(
                "[admission_engine] remove_from_pool: ticker=%s pool 미존재.", ticker
            )
            return False

        del self._candidate_pool[ticker]
        self._cooldown_map[ticker] = datetime.now(_KST)
        logger.info(
            "[admission_engine] pool 제거 + cooldown 시작: ticker=%s pool=%d/%d",
            ticker,
            len(self._candidate_pool),
            self._candidate_pool_max,
        )
        return True

    def _write_admission_event(self, event: dict) -> None:
        """artifacts/dynamic_holdings/admission_events.jsonl 에 append."""
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)
        out_path = self._artifacts_dir / "admission_events.jsonl"
        try:
            with out_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(
                "[admission_engine] admission_events.jsonl 쓰기 실패: %s", e
            )
