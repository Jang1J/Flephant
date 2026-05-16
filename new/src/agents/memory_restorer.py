"""S4-8 Agent Memory 복원기. 시스템 재시작 시 KB에서 각 에이전트 메모리 복원.

KB(new/src/knowledge/kb.py) Consumer 역할. KB write/read API만 사용.
에이전트 클래스와 결합 없음 (KB와 에이전트 사이 중간 레이어).

설계 근거: architecture.md §4.2 Agent Memory.
불변 원칙 5: 모든 임계값은 risk_config.yaml agent_memory 섹션에서 로드.
PIT-Safety: KB read는 읽기 전용. KB 자체가 write 시 PIT 검증 수행.

5종 storage_type 지원:
  micro_notes      - NewsAgent 종목별 관찰 노트
  macro_notes      - RiskSlow 거시 노트
  debate_history   - DebateAgent pairwise CoT 기록
  decision_history - FDA 결정 이력
  backtest_history - Mode B Backtest 결과

사용법 (Hot Runner 부트스트랩):
  from src.agents.memory_restorer import AgentMemoryRestorer
  from src.knowledge.kb import KnowledgeBase

  kb = KnowledgeBase()
  config = config_load("risk_config.yaml", "agent_memory")
  restorer = AgentMemoryRestorer(kb, config)
  counts = restorer.restore_all(agents)  # {agent_name: count}
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from src.knowledge.kb import KnowledgeBase
from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger
from src.utils.safe_cast import safe_bool

logger = get_logger("memory_restorer")

# storage_type → 에이전트 이름 매핑 (복원 대상 결정용)
_STORAGE_TO_AGENT: dict[str, str] = {
    "micro_notes": "news_agent",
    "macro_notes": "risk_slow",
    "debate_history": "debate",
    "decision_history": "fda",
    "backtest_history": "backtest",
}


class AgentMemoryRestorer:
    """KB → 에이전트 메모리 복원기.

    restore_all(agents): 각 에이전트에 KB 항목을 inject.
    restore_for_agent(agent_name, storage_type): 단일 에이전트 단일 스토리지 복원.

    에이전트 인스턴스에 `_restored_memory` 속성을 dict로 주입.
    에이전트가 이 속성을 사용할지는 에이전트 자체 책임.
    (결합 없이 공유 가능한 최소 인터페이스)
    """

    def __init__(
        self,
        kb: KnowledgeBase,
        config: dict[str, Any] | None = None,
    ) -> None:
        """AgentMemoryRestorer 생성자.

        Args:
            kb: KnowledgeBase 인스턴스.
            config: agent_memory 섹션 dict. None이면 risk_config.yaml에서 자동 로드.
        """
        self._kb = kb
        if config is None:
            config = config_load("risk_config.yaml", "agent_memory") or {}
        self._enabled: bool = safe_bool(config.get("enabled", True), default=True)
        self._storage_types: list[str] = list(
            config.get("storage_types", list(_STORAGE_TO_AGENT.keys()))
        )
        self._window_days: int = int(config.get("restore_window_days", 7))
        self._max_entries: int = int(config.get("restore_max_entries_per_agent", 100))
        self._ttl_days: dict[str, int] = dict(config.get("ttl_days", {}))

    # ================================================================== #
    # Public API
    # ================================================================== #

    def restore_all(self, agents: dict[str, Any]) -> dict[str, int]:
        """모든 에이전트에 메모리 복원.

        Args:
            agents: {agent_name: agent_instance} dict.
                    예: {"news_agent": news_agent_obj, "debate": debate_obj, ...}

        Returns:
            {agent_name: 복원된 항목 수} dict.
            enabled=false 이면 빈 dict 즉시 반환.
        """
        if not self._enabled:
            logger.info("[memory_restorer] agent_memory.enabled=false. 복원 skip")
            return {}

        counts: dict[str, int] = {}
        for storage_type in self._storage_types:
            agent_name = _STORAGE_TO_AGENT.get(storage_type)
            if not agent_name:
                logger.warning(
                    "[memory_restorer] storage_type=%s 에 대응하는 에이전트 없음. skip",
                    storage_type,
                )
                continue

            agent = agents.get(agent_name)
            if agent is None:
                logger.debug(
                    "[memory_restorer] agents dict에 '%s' 없음. storage_type=%s skip",
                    agent_name, storage_type,
                )
                continue

            entries = self.restore_for_agent(agent_name, storage_type)
            # 에이전트에 _restored_memory 주입
            if not hasattr(agent, "_restored_memory"):
                agent._restored_memory = {}
            existing = agent._restored_memory.get(storage_type, [])
            existing_ids = {
                e.get("message_id")
                for e in existing
                if isinstance(e, dict) and e.get("message_id")
            }
            new_entries = [
                e for e in entries
                if not (isinstance(e, dict) and e.get("message_id") in existing_ids)
            ]
            agent._restored_memory[storage_type] = existing + new_entries
            count = len(new_entries)

            prev = counts.get(agent_name, 0)
            counts[agent_name] = prev + count

            logger.info(
                "[memory_restorer] %s %s %d건 복원",
                agent_name, storage_type, count,
            )

        return counts

    def restore_for_agent(
        self, agent_name: str, storage_type: str
    ) -> list[dict[str, Any]]:
        """단일 에이전트 단일 storage_type 복원.

        window_days 이내 항목만 조회. max_entries 상한 적용.

        Args:
            agent_name: 에이전트 이름 (로그용).
            storage_type: KB storage_type enum.

        Returns:
            최신순 정렬된 항목 list (최대 max_entries).
            에러 시 빈 list 반환 (graceful).
        """
        try:
            entries = self._fetch_window(storage_type)
        except Exception as e:
            logger.warning(
                "[memory_restorer] %s %s 조회 실패: %s",
                agent_name, storage_type, e,
            )
            return []

        # 최신순 정렬 후 상한 적용
        entries.sort(
            key=lambda x: x.get("timestamp", ""),
            reverse=True,
        )
        return entries[: self._max_entries]

    # ================================================================== #
    # Internal
    # ================================================================== #

    def _fetch_window(self, storage_type: str) -> list[dict[str, Any]]:
        """window_days 이내 KB 항목 전체 수집.

        파일 파티션 구조가 storage_type마다 달라 직접 스캔(rglob)으로 통일.
          micro_notes    : {ticker}/{yyyymm}.jsonl — ticker 다수, 파일 스캔 필요
          macro_notes    : {yyyymm}.jsonl — 월 단위 파일, 날짜 루프 시 중복 발생
          debate_history : {yyyymmdd}.jsonl
          decision_history: {yyyymmdd}.jsonl
          backtest_history: {run_id}.jsonl — 날짜 파티션 없음

        모두 _scan_storage_type으로 통일. cutoff/now_utc 기준 timestamp 필터 적용.
        factor_zoo는 agent_memory 대상 아님. 요청 시 빈 list.

        Returns:
            수집된 전체 항목 list (중복 없음).
        """
        now_utc = datetime.now(tz=timezone.utc)
        cutoff = now_utc - timedelta(days=self._window_days)
        return self._scan_storage_type(storage_type, cutoff, now_utc)

    def _scan_storage_type(
        self,
        storage_type: str,
        cutoff: datetime,
        now_utc: datetime,
    ) -> list[dict[str, Any]]:
        """KB storage_type 디렉토리를 직접 스캔해 window 내 항목 수집.

        KB.read가 파티션 키 의존인 경우(micro_notes ticker, backtest_history run_id)
        전체 파일 탐색이 필요할 때 사용.

        Returns:
            window 내 항목 list.
        """
        import json

        storage_dir = self._kb.storage_root / storage_type
        if not storage_dir.exists():
            return []

        results: list[dict[str, Any]] = []
        for jsonl_file in storage_dir.rglob("*.jsonl"):
            try:
                with jsonl_file.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        ts_str = rec.get("timestamp", "")
                        if not ts_str:
                            logger.debug(
                                "[memory_restorer] timestamp 없는 항목 skip: %s",
                                rec.get("message_id", "?"),
                            )
                            continue
                        try:
                            ts_dt = datetime.fromisoformat(ts_str)
                            if ts_dt.tzinfo is None:
                                ts_dt = ts_dt.replace(tzinfo=timezone.utc)
                            if ts_dt < cutoff:
                                continue
                            if ts_dt > now_utc:
                                continue  # PIT-Safety 추가 방어
                        except ValueError:
                            logger.debug(
                                "[memory_restorer] timestamp 파싱 실패 skip: %s",
                                ts_str,
                            )
                            continue
                        results.append(rec)
            except Exception as e:
                logger.debug(
                    "[memory_restorer] 파일 스캔 실패: %s error=%s",
                    jsonl_file, e,
                )
                continue

        return results
