"""S3-11 KnowledgeBase 유닛 테스트.

Layer 5 KB 실구현 검증.

테스트 목록:
  1.  test_write_read_roundtrip_micro_notes     - write + read 라운드트립 (micro_notes)
  2.  test_all_six_storage_types_writable       - 6 storage_type 모두 write 가능
  3.  test_invalid_storage_type_raises          - 유효하지 않은 storage_type → KBValidationError
  4.  test_missing_required_field_raises        - 필수 필드 누락 → KBValidationError
  5.  test_pit_safety_future_timestamp_raises   - 미래 timestamp → PITViolationError
  6.  test_message_id_auto_generated            - message_id 없이 write → KB-yyyymmdd-UUID8 형식
  7.  test_search_keyword_matching              - query 키워드 매칭 entry top_k 반환
  8.  test_search_ts_filter_after_before        - after/before 기간 외 entry 제외
  9.  test_read_micro_notes_ticker_filter       - ticker="005930" → 해당 종목만 반환
  10. test_snapshot_data_version                - 현재 상태 snapshot → 파일 생성 + 메타 반환
  11. test_append_only_two_entries              - write 두 번 → 파일에 2 entries
  12. test_jsonl_korean_encoding                - ensure_ascii=False 한국어 정상 read/write
  13. test_storage_type_directory_structure     - 6 storage_type 디렉토리 구조 검증
  14. test_timestamp_auto_injected              - timestamp 없이 write → 자동 주입
  15. test_search_storage_type_filter           - storage_type 지정 시 해당 저장소만 검색
  16. test_search_returns_empty_on_no_match     - 매칭 없으면 빈 list
  17. test_search_top_k_limit                   - top_k=2 → 최대 2개 반환
  18. test_read_nonexistent_file_returns_empty  - 없는 파일 read → 빈 list
  19. test_write_returns_message_id             - write 반환값이 str
  20. test_search_recency_boost_ordering        - 최근 entry가 더 높은 score
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.knowledge.kb import KBValidationError, KnowledgeBase
from src.utils.pit_guard import PITViolationError


# ────────────────────────────────────────────────────────────────────────
# 헬퍼
# ────────────────────────────────────────────────────────────────────────

_PAST_TS = "2026-04-01T09:00:00+00:00"
_PAST_TS2 = "2026-04-10T09:00:00+00:00"


def _now_minus(days: int = 0) -> str:
    """현재 시각에서 days일 뺀 ISO8601 문자열."""
    dt = datetime.now(tz=timezone.utc) - timedelta(days=days)
    return dt.isoformat()


def _future_ts() -> str:
    """현재 시각 + 1일 (PIT-Safety 위반용)."""
    dt = datetime.now(tz=timezone.utc) + timedelta(days=1)
    return dt.isoformat()


def _base_entry(**kwargs) -> dict:
    base = {
        "content": "테스트 내용",
        "sent_from": "TestAgent",
        "timestamp": _PAST_TS,
        "cause_by": "unit_test",
        "situation": "정상",
    }
    base.update(kwargs)
    return base


# ────────────────────────────────────────────────────────────────────────
# Fixture
# ────────────────────────────────────────────────────────────────────────

@pytest.fixture
def kb(tmp_path: Path) -> KnowledgeBase:
    """tmp_path 기반 KnowledgeBase 인스턴스."""
    return KnowledgeBase(storage_root=tmp_path)


# ────────────────────────────────────────────────────────────────────────
# 1. write + read 라운드트립
# ────────────────────────────────────────────────────────────────────────

def test_write_read_roundtrip_micro_notes(kb: KnowledgeBase, tmp_path: Path) -> None:
    entry = _base_entry(ticker="005930", content="삼성전자 매수 관찰")
    mid = kb.write(entry, "micro_notes")

    results = kb.read("micro_notes", ticker="005930", date=_PAST_TS)
    assert len(results) == 1
    assert results[0]["message_id"] == mid
    assert results[0]["content"] == "삼성전자 매수 관찰"
    assert results[0]["storage_type"] == "micro_notes"


# ────────────────────────────────────────────────────────────────────────
# 2. 6 저장소 모두 write 가능
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("stype,extra", [
    ("micro_notes",      {"ticker": "000660"}),
    ("macro_notes",      {}),
    ("debate_history",   {}),
    ("decision_history", {}),
    ("backtest_history", {"run_id": "BT-20260401-AABBCCDD"}),
    ("factor_zoo",       {"factor_id": "FACTOR_MOMENTUM_001"}),
])
def test_all_six_storage_types_writable(
    kb: KnowledgeBase, stype: str, extra: dict
) -> None:
    entry = _base_entry(**extra)
    mid = kb.write(entry, stype)
    assert isinstance(mid, str)
    assert mid.startswith("KB-")


# ────────────────────────────────────────────────────────────────────────
# 3. 유효하지 않은 storage_type → KBValidationError
# ────────────────────────────────────────────────────────────────────────

def test_invalid_storage_type_raises(kb: KnowledgeBase) -> None:
    with pytest.raises(KBValidationError, match="유효하지 않은 storage_type"):
        kb.write(_base_entry(), "unknown_type")


def test_invalid_storage_type_read_raises(kb: KnowledgeBase) -> None:
    with pytest.raises(KBValidationError, match="유효하지 않은 storage_type"):
        kb.read("invalid_store")


# ────────────────────────────────────────────────────────────────────────
# 4. 필수 필드 누락 → KBValidationError
# ────────────────────────────────────────────────────────────────────────

def test_missing_content_raises(kb: KnowledgeBase) -> None:
    entry = {"sent_from": "TestAgent", "timestamp": _PAST_TS}
    with pytest.raises(KBValidationError, match="필수 필드 누락"):
        kb.write(entry, "macro_notes")


def test_missing_sent_from_raises(kb: KnowledgeBase) -> None:
    entry = {"content": "내용", "timestamp": _PAST_TS}
    with pytest.raises(KBValidationError, match="필수 필드 누락"):
        kb.write(entry, "macro_notes")


# ────────────────────────────────────────────────────────────────────────
# 5. 미래 timestamp → PITViolationError
# ────────────────────────────────────────────────────────────────────────

def test_pit_safety_future_timestamp_raises(kb: KnowledgeBase) -> None:
    entry = _base_entry(timestamp=_future_ts())
    with pytest.raises(PITViolationError):
        kb.write(entry, "decision_history")


# ────────────────────────────────────────────────────────────────────────
# 6. message_id 자동 생성 → KB-yyyymmdd-UUID8 형식
# ────────────────────────────────────────────────────────────────────────

def test_message_id_auto_generated(kb: KnowledgeBase) -> None:
    entry = _base_entry()
    # message_id 없이 write
    entry.pop("message_id", None)
    mid = kb.write(entry, "debate_history")
    assert re.match(r"^KB-\d{8}-[0-9A-F]{8}$", mid), f"ID 형식 불일치: {mid}"


# ────────────────────────────────────────────────────────────────────────
# 7. search 키워드 매칭
# ────────────────────────────────────────────────────────────────────────

def test_search_keyword_matching(kb: KnowledgeBase) -> None:
    kb.write(_base_entry(content="삼성전자 급등 관찰"), "macro_notes")
    kb.write(_base_entry(content="하이닉스 하락 리스크"), "macro_notes")
    kb.write(_base_entry(content="삼성전자 조정 구간"), "macro_notes")

    results = kb.search("삼성전자")
    tickers_in_content = [r["content"] for r in results]
    assert all("삼성전자" in c for c in tickers_in_content)
    assert len(results) <= 5  # default top_k


def test_search_returns_top_k_entries(kb: KnowledgeBase) -> None:
    for i in range(10):
        kb.write(_base_entry(content=f"반도체 관찰 {i}"), "macro_notes")

    results = kb.search("반도체", top_k=3)
    assert len(results) <= 3


# ────────────────────────────────────────────────────────────────────────
# 8. search ts_filter after/before
# ────────────────────────────────────────────────────────────────────────

def test_search_ts_filter_after_before(kb: KnowledgeBase) -> None:
    # 오래된 기록
    kb.write(_base_entry(content="오래된 반도체", timestamp="2026-01-01T00:00:00+00:00"), "macro_notes")
    # 최근 기록
    kb.write(_base_entry(content="최근 반도체", timestamp="2026-04-01T00:00:00+00:00"), "macro_notes")

    # after=2026-03-01 → "오래된 반도체"는 제외
    results = kb.search(
        "반도체",
        ts_filter={"after": "2026-03-01T00:00:00+00:00"},
    )
    contents = [r["content"] for r in results]
    assert "최근 반도체" in contents
    assert "오래된 반도체" not in contents


def test_search_ts_filter_before(kb: KnowledgeBase) -> None:
    kb.write(_base_entry(content="초기 기록", timestamp="2026-01-01T00:00:00+00:00"), "macro_notes")
    kb.write(_base_entry(content="후기 기록", timestamp="2026-04-01T00:00:00+00:00"), "macro_notes")

    results = kb.search(
        "기록",
        ts_filter={"before": "2026-02-01T00:00:00+00:00"},
    )
    contents = [r["content"] for r in results]
    assert "초기 기록" in contents
    assert "후기 기록" not in contents


# ────────────────────────────────────────────────────────────────────────
# 9. read 종목별 (micro_notes) ticker 필터
# ────────────────────────────────────────────────────────────────────────

def test_read_micro_notes_ticker_filter(kb: KnowledgeBase) -> None:
    kb.write(_base_entry(ticker="005930", content="삼성전자 관찰"), "micro_notes")
    kb.write(_base_entry(ticker="000660", content="SK하이닉스 관찰"), "micro_notes")

    results_samsung = kb.read("micro_notes", ticker="005930", date=_PAST_TS)
    assert len(results_samsung) == 1
    assert results_samsung[0]["content"] == "삼성전자 관찰"

    results_hynix = kb.read("micro_notes", ticker="000660", date=_PAST_TS)
    assert len(results_hynix) == 1
    assert results_hynix[0]["content"] == "SK하이닉스 관찰"


# ────────────────────────────────────────────────────────────────────────
# 10. snapshot_data_version
# ────────────────────────────────────────────────────────────────────────

def test_snapshot_data_version(kb: KnowledgeBase, tmp_path: Path) -> None:
    # 일부 데이터 기록
    kb.write(_base_entry(content="macro 메모"), "macro_notes")
    kb.write(_base_entry(content="debate 기록"), "debate_history")

    snap = kb.snapshot_data_version(_PAST_TS)

    assert "snapshot_id" in snap
    assert "ts" in snap
    assert "storage_summary" in snap

    summary = snap["storage_summary"]
    # 6 storage_type 모두 summary에 포함
    for stype in ["micro_notes", "macro_notes", "debate_history",
                  "decision_history", "backtest_history", "factor_zoo"]:
        assert stype in summary

    # 파일 생성 확인
    dv_dir = tmp_path / "data_versions"
    assert dv_dir.exists()
    snap_files = list(dv_dir.glob("*.json"))
    assert len(snap_files) == 1

    # 파일 내용 검증
    loaded = json.loads(snap_files[0].read_text(encoding="utf-8"))
    assert loaded["snapshot_id"] == snap["snapshot_id"]
    assert loaded["ts"] == _PAST_TS


def test_snapshot_seq_increments(kb: KnowledgeBase, tmp_path: Path) -> None:
    kb.write(_base_entry(), "macro_notes")
    s1 = kb.snapshot_data_version(_PAST_TS)
    s2 = kb.snapshot_data_version(_PAST_TS)
    # 두 번째 snapshot은 다른 ID
    assert s1["snapshot_id"] != s2["snapshot_id"]
    dv_files = list((tmp_path / "data_versions").glob("*.json"))
    assert len(dv_files) == 2


# ────────────────────────────────────────────────────────────────────────
# 11. append-only 보장
# ────────────────────────────────────────────────────────────────────────

def test_append_only_two_entries(kb: KnowledgeBase, tmp_path: Path) -> None:
    e1 = _base_entry(content="첫 번째")
    e2 = _base_entry(content="두 번째")
    kb.write(e1, "decision_history")
    kb.write(e2, "decision_history")

    results = kb.read("decision_history", date=_PAST_TS)
    assert len(results) == 2
    contents = {r["content"] for r in results}
    assert "첫 번째" in contents
    assert "두 번째" in contents


# ────────────────────────────────────────────────────────────────────────
# 12. JSONL 인코딩 (ensure_ascii=False, 한국어)
# ────────────────────────────────────────────────────────────────────────

def test_jsonl_korean_encoding(kb: KnowledgeBase, tmp_path: Path) -> None:
    entry = _base_entry(
        content="한국어 내용: 시장 급락 경고",
        lesson="손실 회피 전략 필요",
        situation="KOSPI 하락장",
    )
    kb.write(entry, "macro_notes")

    # JSONL 파일 원본 바이트 확인 (non-ASCII 그대로 저장)
    macro_dir = tmp_path / "macro_notes"
    jsonl_files = list(macro_dir.glob("*.jsonl"))
    assert len(jsonl_files) == 1
    raw = jsonl_files[0].read_bytes()
    assert "한국어".encode("utf-8") in raw  # ASCII escape 없이 저장

    # read로 복원 확인
    results = kb.read("macro_notes", date=_PAST_TS)
    assert results[0]["content"] == "한국어 내용: 시장 급락 경고"
    assert results[0]["lesson"] == "손실 회피 전략 필요"


# ────────────────────────────────────────────────────────────────────────
# 13. storage_type별 디렉토리 구조
# ────────────────────────────────────────────────────────────────────────

def test_storage_type_directory_structure(kb: KnowledgeBase, tmp_path: Path) -> None:
    entries = {
        "micro_notes":      _base_entry(ticker="005930"),
        "macro_notes":      _base_entry(),
        "debate_history":   _base_entry(),
        "decision_history": _base_entry(),
        "backtest_history": _base_entry(run_id="BT-20260401-AAAABBBB"),
        "factor_zoo":       _base_entry(factor_id="FACTOR_RSI_001"),
    }
    for stype, entry in entries.items():
        kb.write(entry, stype)

    # 각 storage_type 디렉토리 존재 확인
    assert (tmp_path / "micro_notes").is_dir()
    assert (tmp_path / "macro_notes").is_dir()
    assert (tmp_path / "debate_history").is_dir()
    assert (tmp_path / "decision_history").is_dir()
    assert (tmp_path / "backtest_history").is_dir()
    assert (tmp_path / "factor_zoo").is_dir()

    # micro_notes: {ticker}/{yyyymm}.jsonl 구조
    micro_ticker_dirs = list((tmp_path / "micro_notes").iterdir())
    assert len(micro_ticker_dirs) == 1  # 005930만
    assert micro_ticker_dirs[0].name == "005930"
    assert len(list(micro_ticker_dirs[0].glob("*.jsonl"))) == 1

    # backtest_history: {run_id}.jsonl
    bt_files = list((tmp_path / "backtest_history").glob("*.jsonl"))
    assert any("BT-20260401-AAAABBBB" in f.name for f in bt_files)

    # factor_zoo: {factor_id}.jsonl
    fz_files = list((tmp_path / "factor_zoo").glob("*.jsonl"))
    assert any("FACTOR_RSI_001" in f.name for f in fz_files)


# ────────────────────────────────────────────────────────────────────────
# 14. timestamp 자동 주입
# ────────────────────────────────────────────────────────────────────────

def test_timestamp_auto_injected(kb: KnowledgeBase) -> None:
    entry = {"content": "타임스탬프 없음", "sent_from": "AutoAgent"}
    before_write = datetime.now(tz=timezone.utc)
    kb.write(entry, "decision_history")

    # read로 확인: date=오늘
    today_str = before_write.strftime("%Y-%m-%d")
    results = kb.read("decision_history", date=today_str)
    assert len(results) == 1
    injected_ts = results[0]["timestamp"]
    ts_dt = datetime.fromisoformat(injected_ts)
    if ts_dt.tzinfo is None:
        ts_dt = ts_dt.replace(tzinfo=timezone.utc)
    # 쓴 직후 timestamp 범위 내인지 확인 (±5초 허용)
    delta = abs((ts_dt - before_write).total_seconds())
    assert delta < 5.0, f"자동 주입 timestamp 오차 너무 큼: {delta}s"


# ────────────────────────────────────────────────────────────────────────
# 15. search storage_type 필터
# ────────────────────────────────────────────────────────────────────────

def test_search_storage_type_filter(kb: KnowledgeBase) -> None:
    kb.write(_base_entry(content="반도체 뉴스"), "macro_notes")
    kb.write(_base_entry(content="반도체 토론"), "debate_history")

    # macro_notes만 검색
    results = kb.search("반도체", storage_type="macro_notes")
    assert all(r["storage_type"] == "macro_notes" for r in results)

    # debate_history만 검색
    results_d = kb.search("반도체", storage_type="debate_history")
    assert all(r["storage_type"] == "debate_history" for r in results_d)


# ────────────────────────────────────────────────────────────────────────
# 16. search 매칭 없으면 빈 list
# ────────────────────────────────────────────────────────────────────────

def test_search_returns_empty_on_no_match(kb: KnowledgeBase) -> None:
    kb.write(_base_entry(content="완전히 다른 내용"), "macro_notes")
    results = kb.search("XXXXXXXXXX없는키워드")
    assert results == []


# ────────────────────────────────────────────────────────────────────────
# 17. search top_k 제한
# ────────────────────────────────────────────────────────────────────────

def test_search_top_k_limit(kb: KnowledgeBase) -> None:
    for i in range(10):
        kb.write(_base_entry(content=f"외국인 순매도 관찰 {i}"), "macro_notes")

    results = kb.search("외국인", top_k=2)
    assert len(results) == 2


# ────────────────────────────────────────────────────────────────────────
# 18. 없는 파일 read → 빈 list
# ────────────────────────────────────────────────────────────────────────

def test_read_nonexistent_file_returns_empty(kb: KnowledgeBase) -> None:
    results = kb.read("macro_notes", date="2000-01-01")
    assert results == []


def test_read_nonexistent_ticker_returns_empty(kb: KnowledgeBase) -> None:
    results = kb.read("micro_notes", ticker="999999", date="2000-01-01")
    assert results == []


# ────────────────────────────────────────────────────────────────────────
# 19. write 반환값이 str
# ────────────────────────────────────────────────────────────────────────

def test_write_returns_message_id(kb: KnowledgeBase) -> None:
    mid = kb.write(_base_entry(), "macro_notes")
    assert isinstance(mid, str)
    assert len(mid) > 0


# ────────────────────────────────────────────────────────────────────────
# 20. recency boost: 최근 entry가 더 높은 score
# ────────────────────────────────────────────────────────────────────────

def test_search_recency_boost_ordering(kb: KnowledgeBase) -> None:
    # 오래된 기록 (30일 전)
    old_ts = (datetime.now(tz=timezone.utc) - timedelta(days=30)).isoformat()
    # 최근 기록 (1일 전)
    recent_ts = (datetime.now(tz=timezone.utc) - timedelta(days=1)).isoformat()

    kb.write(_base_entry(content="KOSPI 관찰 오래된", timestamp=old_ts), "macro_notes")
    kb.write(_base_entry(content="KOSPI 관찰 최근", timestamp=recent_ts), "macro_notes")

    results = kb.search("KOSPI 관찰", top_k=2)
    assert len(results) == 2
    # 최근 entry가 먼저 나와야 함
    assert "최근" in results[0]["content"]


# ────────────────────────────────────────────────────────────────────────
# 21. read key 필터 (message_id 매칭)
# ────────────────────────────────────────────────────────────────────────

def test_read_key_filter(kb: KnowledgeBase) -> None:
    mid1 = kb.write(_base_entry(content="첫 항목"), "debate_history")
    kb.write(_base_entry(content="두 번째 항목"), "debate_history")

    result = kb.read("debate_history", key=mid1, date=_PAST_TS)
    assert len(result) == 1
    assert result[0]["message_id"] == mid1
    assert result[0]["content"] == "첫 항목"


# ────────────────────────────────────────────────────────────────────────
# 22. factor_zoo 전용 read (factor_id 기반)
# ────────────────────────────────────────────────────────────────────────

def test_factor_zoo_read_by_factor_id(kb: KnowledgeBase) -> None:
    entry = _base_entry(
        factor_id="FACTOR_MOMENTUM_001",
        content="모멘텀 팩터 v1",
        hypothesis="5일 수익률 양수 지속성",
    )
    mid = kb.write(entry, "factor_zoo")

    results = kb.read("factor_zoo", factor_id="FACTOR_MOMENTUM_001")
    assert len(results) == 1
    assert results[0]["message_id"] == mid
    assert results[0]["content"] == "모멘텀 팩터 v1"


# ────────────────────────────────────────────────────────────────────────
# 23. backtest_history run_id 기반 read
# ────────────────────────────────────────────────────────────────────────

def test_backtest_history_read_by_run_id(kb: KnowledgeBase) -> None:
    entry = _base_entry(
        run_id="BT-20260401-TESTTEST",
        content="백테스트 결과 Sharpe=1.2",
    )
    mid = kb.write(entry, "backtest_history")

    results = kb.read("backtest_history", run_id="BT-20260401-TESTTEST")
    assert len(results) == 1
    assert results[0]["message_id"] == mid


# ────────────────────────────────────────────────────────────────────────
# 24. snapshot_data_version storage_summary file_count 정확성
# ────────────────────────────────────────────────────────────────────────

def test_snapshot_storage_summary_file_count(kb: KnowledgeBase, tmp_path: Path) -> None:
    # macro_notes 2개 파일 (다른 월)
    kb.write(_base_entry(timestamp="2026-01-15T09:00:00+00:00", content="1월 매크로"), "macro_notes")
    kb.write(_base_entry(timestamp="2026-03-15T09:00:00+00:00", content="3월 매크로"), "macro_notes")

    snap = kb.snapshot_data_version(_PAST_TS)
    macro_summary = snap["storage_summary"]["macro_notes"]
    assert macro_summary["file_count"] == 2  # 2개 월별 파일
    assert macro_summary["last_ts"] is not None
