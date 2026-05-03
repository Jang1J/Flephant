"""Layer 5 Knowledge Base. 에이전트 공유 지식 저장소.

6 저장소:
  micro_notes      - 종목별 관찰 (NewsAgent micro memory)
  macro_notes      - 거시 메모 (RiskSlow macro memory)
  debate_history   - DebateAgent CoT 기록
  decision_history - FDA 결정 기록
  backtest_history - BacktestEngine 결과
  factor_zoo       - alpha factor 카탈로그 + 메타

파일 레이아웃:
  artifacts/knowledge_base/
    micro_notes/{ticker}/{yyyymm}.jsonl
    macro_notes/{yyyymm}.jsonl
    debate_history/{yyyymmdd}.jsonl
    decision_history/{yyyymmdd}.jsonl
    backtest_history/{run_id}.jsonl
    factor_zoo/{factor_id}.jsonl
    data_versions/{yyyymmdd}_{seq}.json

PIT-Safety: timestamp <= now() 강제. 미래 timestamp write 시 PITViolationError.
불변 원칙 5: 모든 임계값은 risk_config.yaml knowledge_base 섹션에서 로드.
Hot Path write 금지: Mode B + Cold Path만 write. Hot Path는 read-only.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.utils.config_loader import load as config_load
from src.utils.id_factory import generate_kb_id
from src.utils.logger import get_logger
from src.utils.pit_guard import PITViolationError

logger = get_logger("kb")

# ─────────────────────────────────────────────────────────────
# 내부 예외
# ─────────────────────────────────────────────────────────────


class KBValidationError(ValueError):
    """필수 필드 누락 또는 유효하지 않은 storage_type."""


# ─────────────────────────────────────────────────────────────
# 설정 로드 헬퍼 (불변 원칙 5)
# ─────────────────────────────────────────────────────────────


def _kb_cfg() -> dict[str, Any]:
    """knowledge_base 섹션 로드. 캐시는 config_loader 내부 처리."""
    return config_load("risk_config.yaml", "knowledge_base")


# ─────────────────────────────────────────────────────────────
# KnowledgeBase
# ─────────────────────────────────────────────────────────────


class KnowledgeBase:
    """에이전트 공유 지식 저장소 (Layer 5).

    write: Mode B / Cold Path 에이전트가 학습 결과/결정 기록.
    read:  storage_type + key/ticker/date 기반 조회.
    search: 키워드 매칭 + recency boost (Sprint 4: Vector DB 교체 예정).
    snapshot_data_version: PIT replay용 KB 상태 스냅샷.

    Hot Path는 read-only. write는 Mode B + Cold Path만 허용.
    """

    def __init__(self, storage_root: str | Path | None = None) -> None:
        cfg = _kb_cfg()
        if storage_root is not None:
            self._root = Path(storage_root)
        else:
            self._root = Path(cfg["storage_root"])
        self._valid_types: list[str] = list(cfg["storage_types"])
        self._required_fields: list[str] = list(cfg["required_fields"])
        self._pit_check: bool = bool(cfg.get("pit_safety_check", True))
        self._top_k_default: int = int(cfg.get("search_default_top_k", 5))
        self._recency_lambda: float = float(cfg.get("search_recency_boost_lambda", 0.1))

    # ─────────────────────────────────────────────────────────
    # Public properties
    # ─────────────────────────────────────────────────────────

    @property
    def storage_root(self) -> Path:
        """KB 루트 디렉토리 경로 (외부 소비자용 public API)."""
        return self._root

    # ─────────────────────────────────────────────────────────
    # 내부 유틸
    # ─────────────────────────────────────────────────────────

    def _validate_storage_type(self, storage_type: str) -> None:
        if storage_type not in self._valid_types:
            raise KBValidationError(
                f"[KB] 유효하지 않은 storage_type: '{storage_type}'. "
                f"허용: {self._valid_types}"
            )

    def _validate_required_fields(self, entry: dict[str, Any]) -> None:
        missing = [f for f in self._required_fields if f not in entry or entry[f] is None]
        if missing:
            raise KBValidationError(
                f"[KB] 필수 필드 누락: {missing}"
            )

    def _validate_pit(self, timestamp: str) -> None:
        """timestamp가 현재 시각보다 미래이면 PITViolationError."""
        if not self._pit_check:
            return
        now_utc = datetime.now(tz=timezone.utc)
        try:
            ts_dt = datetime.fromisoformat(timestamp)
        except ValueError as exc:
            raise KBValidationError(f"[KB] timestamp 파싱 실패: {timestamp!r}") from exc
        if ts_dt.tzinfo is None:
            # naive → UTC 가정
            ts_dt = ts_dt.replace(tzinfo=timezone.utc)
        if ts_dt > now_utc:
            raise PITViolationError(
                f"[KB] PIT-Safety 위반: timestamp={timestamp!r} > now={now_utc.isoformat()}"
            )

    def _resolve_path(
        self,
        storage_type: str,
        entry: dict[str, Any] | None = None,
        ticker: str | None = None,
        date_str: str | None = None,
        run_id: str | None = None,
        factor_id: str | None = None,
    ) -> Path:
        """storage_type + 메타데이터 → JSONL 파일 경로 결정.

        반환 경로의 부모 디렉토리는 호출자가 mkdir 처리.
        """
        # timestamp 기반 파티션 키 추출
        ts: str | None = None
        if entry is not None:
            ts = entry.get("timestamp")
        if ts is None and date_str is not None:
            ts = date_str

        def _yyyymm(t: str) -> str:
            return t[:7].replace("-", "")  # "2026-05" → "202605"

        def _yyyymmdd(t: str) -> str:
            return t[:10].replace("-", "")  # "2026-05-01" → "20260501"

        base = self._root
        if storage_type == "micro_notes":
            # micro_notes/{ticker}/{yyyymm}.jsonl
            tkr = ticker or (entry or {}).get("ticker", "UNKNOWN")
            tkr = str(tkr).zfill(6)
            ym = _yyyymm(ts) if ts else "000000"
            return base / "micro_notes" / tkr / f"{ym}.jsonl"

        if storage_type == "macro_notes":
            # macro_notes/{yyyymm}.jsonl
            ym = _yyyymm(ts) if ts else "000000"
            return base / "macro_notes" / f"{ym}.jsonl"

        if storage_type == "debate_history":
            # debate_history/{yyyymmdd}.jsonl
            ymd = _yyyymmdd(ts) if ts else "00000000"
            return base / "debate_history" / f"{ymd}.jsonl"

        if storage_type == "decision_history":
            # decision_history/{yyyymmdd}.jsonl
            ymd = _yyyymmdd(ts) if ts else "00000000"
            return base / "decision_history" / f"{ymd}.jsonl"

        if storage_type == "backtest_history":
            # backtest_history/{run_id}.jsonl
            rid = run_id or (entry or {}).get("run_id", "unknown")
            return base / "backtest_history" / f"{rid}.jsonl"

        if storage_type == "factor_zoo":
            # factor_zoo/{factor_id}.jsonl
            fid = factor_id or (entry or {}).get("factor_id", "unknown")
            return base / "factor_zoo" / f"{fid}.jsonl"

        # 이 라인에 도달하면 validate_storage_type이 먼저 막아야 함
        raise KBValidationError(f"[KB] _resolve_path: 알 수 없는 storage_type={storage_type!r}")

    # ─────────────────────────────────────────────────────────
    # 공개 메서드
    # ─────────────────────────────────────────────────────────

    def write(self, entry: dict[str, Any], storage_type: str) -> str:
        """KB에 항목 기록. JSONL append-only.

        Args:
            entry: KB 메시지 dict. 필수 필드: content, sent_from, timestamp.
            storage_type: 6종 enum 중 하나.

        Returns:
            저장된 message_id (KB-yyyymmdd-UUID8).

        Raises:
            KBValidationError: storage_type 또는 필수 필드 오류.
            PITViolationError: timestamp가 미래인 경우 (불변 원칙 1).
        """
        self._validate_storage_type(storage_type)

        # entry 복사본 작업 (원본 훼손 방지)
        rec: dict[str, Any] = dict(entry)

        # timestamp 자동 주입 (없으면 now)
        if "timestamp" not in rec or rec["timestamp"] is None:
            rec["timestamp"] = datetime.now(tz=timezone.utc).isoformat()

        # message_id 자동 생성
        if "message_id" not in rec or not rec["message_id"]:
            rec["message_id"] = generate_kb_id()

        # storage_type 주입
        rec["storage_type"] = storage_type

        # 필수 필드 검증
        self._validate_required_fields(rec)

        # PIT-Safety 검증 (불변 원칙 1)
        self._validate_pit(rec["timestamp"])

        # 파일 경로 결정 + 디렉토리 생성
        filepath = self._resolve_path(storage_type, entry=rec)
        filepath.parent.mkdir(parents=True, exist_ok=True)

        # JSONL append
        with filepath.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

        logger.info(
            "[KB] write storage=%s msg_id=%s file=%s",
            storage_type, rec["message_id"], filepath.name,
        )
        return rec["message_id"]

    def read(
        self,
        storage_type: str,
        key: str | None = None,
        ticker: str | None = None,
        date: str | None = None,
        run_id: str | None = None,
        factor_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """JSONL에서 항목 조회.

        Args:
            storage_type: 6종 enum.
            key: message_id 정확 매칭 (지정 시 해당 항목만).
            ticker: micro_notes 전용 종목 코드 필터.
            date: "YYYY-MM-DD" 형식. debate_history/decision_history 날짜 지정.
                  micro_notes/macro_notes는 "YYYY-MM" 월 지정도 허용.
            run_id: backtest_history 전용.
            factor_id: factor_zoo 전용.

        Returns:
            매칭 항목 list. 파일 없으면 빈 list.
        """
        self._validate_storage_type(storage_type)

        # 파일 경로 결정
        filepath = self._resolve_path(
            storage_type,
            entry=None,
            ticker=ticker,
            date_str=date,
            run_id=run_id,
            factor_id=factor_id,
        )

        if not filepath.exists():
            return []

        results: list[dict[str, Any]] = []
        with filepath.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if key is not None and rec.get("message_id") != key:
                    continue
                results.append(rec)

        return results

    def search(
        self,
        query: str,
        storage_type: str | None = None,
        top_k: int | None = None,
        ts_filter: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """키워드 기반 전문 검색 + recency boost.

        검색 대상 필드: content, lesson, situation.
        점수 = hit_count + recency_boost (exp(-lambda * days_old)).
        ts_filter: {"after": ISO8601, "before": ISO8601} PIT replay용.

        storage_type 지정 시 해당 저장소만, 없으면 전체 6개 순회.

        Args:
            query: 검색 키워드 (공백 구분 OR 매칭).
            storage_type: 특정 저장소만 검색. None이면 전체.
            top_k: 상위 N개 반환. None이면 risk_config 기본값 사용.
            ts_filter: {"after": ..., "before": ...} ISO8601 필터.

        Returns:
            score 내림차순 정렬된 top_k 항목 list.

        Note:
            # TODO Sprint 4: Vector DB (faiss/pgvector) 통합으로 교체.
            현재는 키워드 substring match. 한국어 대응 위해 lower() 적용.
        """
        k = top_k if top_k is not None else self._top_k_default
        query_lower = query.lower()
        keywords = query_lower.split()

        # ts_filter 파싱
        after_dt: datetime | None = None
        before_dt: datetime | None = None
        if ts_filter:
            if "after" in ts_filter:
                after_dt = datetime.fromisoformat(ts_filter["after"])
                if after_dt.tzinfo is None:
                    after_dt = after_dt.replace(tzinfo=timezone.utc)
            if "before" in ts_filter:
                before_dt = datetime.fromisoformat(ts_filter["before"])
                if before_dt.tzinfo is None:
                    before_dt = before_dt.replace(tzinfo=timezone.utc)

        search_types = [storage_type] if storage_type else self._valid_types
        now_utc = datetime.now(tz=timezone.utc)

        candidates: list[tuple[float, dict[str, Any]]] = []

        for stype in search_types:
            # 해당 storage_type의 모든 JSONL 파일 순회
            type_dir = self._root / stype
            if not type_dir.exists():
                continue
            for jsonl_file in type_dir.rglob("*.jsonl"):
                with jsonl_file.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        # ts_filter 적용
                        if after_dt or before_dt:
                            raw_ts = rec.get("timestamp", "")
                            if raw_ts:
                                try:
                                    rec_dt = datetime.fromisoformat(raw_ts)
                                    if rec_dt.tzinfo is None:
                                        rec_dt = rec_dt.replace(tzinfo=timezone.utc)
                                    if after_dt and rec_dt < after_dt:
                                        continue
                                    if before_dt and rec_dt > before_dt:
                                        continue
                                except ValueError:
                                    pass

                        # 키워드 hit count
                        search_text = " ".join(
                            str(rec.get(f, "")).lower()
                            for f in ("content", "lesson", "situation")
                        )
                        hit_count = sum(
                            1 for kw in keywords if kw and kw in search_text
                        )
                        if hit_count == 0:
                            continue

                        # recency boost
                        recency_score = self._vector_search_mock(rec, now_utc)
                        score = hit_count + recency_score
                        candidates.append((score, rec))

        # 점수 내림차순 정렬 후 top_k 반환
        candidates.sort(key=lambda x: x[0], reverse=True)
        return [rec for _, rec in candidates[:k]]

    def _vector_search_mock(
        self, rec: dict[str, Any], now_utc: datetime
    ) -> float:
        """recency boost 계산.

        score = exp(-lambda * days_old). 오늘 기록 = 1.0, 오래될수록 감쇠.

        # TODO Sprint 4: Vector DB integration (faiss / pgvector).
        현재는 recency 점수만 반환 (키워드 점수 보조).
        """
        raw_ts = rec.get("timestamp", "")
        if not raw_ts:
            return 0.0
        try:
            rec_dt = datetime.fromisoformat(raw_ts)
            if rec_dt.tzinfo is None:
                rec_dt = rec_dt.replace(tzinfo=timezone.utc)
            days_old = max(0.0, (now_utc - rec_dt).total_seconds() / 86400.0)
            return math.exp(-self._recency_lambda * days_old)
        except ValueError:
            return 0.0

    def snapshot_data_version(self, ts: str) -> dict[str, Any]:
        """현재 KB 상태 스냅샷. PIT replay용.

        각 storage_type별 JSONL 파일 목록 + 마지막 entry timestamp를 기록.
        스냅샷 파일: artifacts/knowledge_base/data_versions/{yyyymmdd}_{seq}.json

        Args:
            ts: 스냅샷 기준 시각 (ISO8601). PIT replay 기준점.

        Returns:
            {snapshot_id, ts, storage_summary: {storage_type: {file_count, last_ts}}}
        """
        summary: dict[str, dict[str, Any]] = {}

        for stype in self._valid_types:
            type_dir = self._root / stype
            files: list[Path] = []
            if type_dir.exists():
                files = list(type_dir.rglob("*.jsonl"))
            last_ts: str | None = None
            for f in sorted(files):
                try:
                    with f.open("r", encoding="utf-8") as fh:
                        lines = [ln.strip() for ln in fh if ln.strip()]
                    if lines:
                        last_line = lines[-1]
                        rec = json.loads(last_line)
                        candidate = rec.get("timestamp")
                        if candidate:
                            if last_ts is None or candidate > last_ts:
                                last_ts = candidate
                except (json.JSONDecodeError, OSError):
                    pass
            summary[stype] = {
                "file_count": len(files),
                "last_ts": last_ts,
            }

        # 스냅샷 ID + 파일 결정
        # seq: data_versions/ 디렉토리 내 오늘 날짜 파일 수 + 1
        dv_dir = self._root / "data_versions"
        dv_dir.mkdir(parents=True, exist_ok=True)
        date_prefix = ts[:10].replace("-", "")  # "20260501"
        existing = list(dv_dir.glob(f"{date_prefix}_*.json"))
        seq = len(existing) + 1
        snapshot_id = f"SNAP-{date_prefix}-{seq:04d}"
        snap_path = dv_dir / f"{date_prefix}_{seq:04d}.json"

        snapshot = {
            "snapshot_id": snapshot_id,
            "ts": ts,
            "storage_summary": summary,
        }

        with snap_path.open("w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, ensure_ascii=False, indent=2)

        return snapshot
