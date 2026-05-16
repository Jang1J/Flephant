"""S4-7 PersistentCache unit tests.

테스트 목록:
  1. set + get 기본 동작
  2. TTL 만료 후 get → None
  3. delete 동작
  4. clear_expired 정확성
  5. 동시성 (멀티 스레드 read)
  6. stats() 반환 형식
  7. enabled=False noop
  8. News Agent cache hit/miss integration
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.cache.persistent_cache import PersistentCache


# ============================================================
# 공통 fixture
# ============================================================


@pytest.fixture()
def cache(tmp_path: Path) -> PersistentCache:
    """tmp 경로 SQLite DB 사용. 테스트 독립성 보장."""
    db_path = tmp_path / "test_cache.db"
    c = PersistentCache(db_path=db_path)
    yield c
    c.close()


# ============================================================
# 1. set + get 기본 동작
# ============================================================


def test_set_get_basic(cache: PersistentCache) -> None:
    """set 후 get이 동일 값 반환."""
    value = {"stance": "buy", "narrative": "테스트 뉴스", "tickers": ["005930"]}
    cache.set("news:005930:EVT-001", value, ttl_seconds=3600)

    result = cache.get("news:005930:EVT-001")

    assert result is not None
    assert result["stance"] == "buy"
    assert result["narrative"] == "테스트 뉴스"
    assert result["tickers"] == ["005930"]


def test_get_miss_returns_none(cache: PersistentCache) -> None:
    """존재하지 않는 key → None."""
    assert cache.get("news:999999:NOT_EXIST") is None


def test_set_overwrites_existing(cache: PersistentCache) -> None:
    """동일 key 재set 시 값 갱신."""
    cache.set("agent_report:risk_slow:EVT-002", {"score": 0.3}, ttl_seconds=1800)
    cache.set("agent_report:risk_slow:EVT-002", {"score": 0.9}, ttl_seconds=1800)

    result = cache.get("agent_report:risk_slow:EVT-002")
    assert result is not None
    assert result["score"] == 0.9


# ============================================================
# 2. TTL 만료 후 get → None
# ============================================================


def test_ttl_expiry(cache: PersistentCache) -> None:
    """ttl_seconds=1 설정 후 2초 후 get → None."""
    cache.set("news:000660:EVT-003", {"x": 1}, ttl_seconds=1)

    # 만료 전 조회
    assert cache.get("news:000660:EVT-003") is not None

    # 만료 대기 (1초 TTL + 여유 0.5초)
    time.sleep(1.5)

    # 만료 후 조회
    assert cache.get("news:000660:EVT-003") is None


def test_ttl_zero_no_expiry(cache: PersistentCache) -> None:
    """ttl_seconds=0 → 만료 없음."""
    cache.set("agent_report:debate:EVT-004", {"result": "ok"}, ttl_seconds=0)
    time.sleep(0.1)
    assert cache.get("agent_report:debate:EVT-004") is not None


# ============================================================
# 3. delete 동작
# ============================================================


def test_delete_existing_key(cache: PersistentCache) -> None:
    """존재하는 key delete → True 반환, get → None."""
    cache.set("news:035420:EVT-005", {"data": 1}, ttl_seconds=3600)
    deleted = cache.delete("news:035420:EVT-005")
    assert deleted is True
    assert cache.get("news:035420:EVT-005") is None


def test_delete_nonexistent_key(cache: PersistentCache) -> None:
    """존재하지 않는 key delete → False."""
    deleted = cache.delete("news:NOT_EXIST:EVT-999")
    assert deleted is False


# ============================================================
# 4. clear_expired 정확성
# ============================================================


def test_clear_expired(cache: PersistentCache) -> None:
    """만료 항목만 삭제, 유효 항목은 보존."""
    # 만료될 항목 2개 (ttl=1)
    cache.set("news:005380:EVT-A", {"v": 1}, ttl_seconds=1)
    cache.set("news:005380:EVT-B", {"v": 2}, ttl_seconds=1)
    # 유효 항목 1개 (ttl=3600)
    cache.set("news:005380:EVT-C", {"v": 3}, ttl_seconds=3600)

    # 만료 대기
    time.sleep(1.5)

    deleted_count = cache.clear_expired()
    assert deleted_count == 2

    # 유효 항목 살아있음
    assert cache.get("news:005380:EVT-C") is not None
    # 만료 항목 없음
    assert cache.get("news:005380:EVT-A") is None
    assert cache.get("news:005380:EVT-B") is None


# ============================================================
# 5. 동시성 (멀티 스레드 read)
# ============================================================


def test_concurrent_reads(cache: PersistentCache) -> None:
    """10 스레드가 동시에 같은 key를 read해도 결과 동일."""
    cache.set("news:051910:EVT-CONC", {"score": 42}, ttl_seconds=3600)

    results: list = []
    errors: list = []

    def reader() -> None:
        try:
            v = cache.get("news:051910:EVT-CONC")
            results.append(v)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=reader) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"동시 read 중 에러: {errors}"
    assert len(results) == 10
    for r in results:
        assert r is not None
        assert r["score"] == 42


# ============================================================
# 6. stats() 반환 형식
# ============================================================


def test_stats_keys(cache: PersistentCache) -> None:
    """stats()가 필수 키를 포함하여 반환."""
    cache.set("news:000270:EVT-S1", {"x": 1}, ttl_seconds=3600)
    cache.set("news:000270:EVT-S2", {"x": 2}, ttl_seconds=1)

    time.sleep(1.5)  # EVT-S2 만료

    s = cache.stats()
    assert "total_keys" in s
    assert "expired_keys" in s
    assert "active_keys" in s
    assert "db_path" in s
    assert "enabled" in s
    assert s["total_keys"] >= 2
    assert s["expired_keys"] >= 1
    assert s["active_keys"] >= 1


# ============================================================
# 7. enabled=False noop
# ============================================================


def test_disabled_cache_noop(tmp_path: Path) -> None:
    """cache.enabled=False 시 set/get 모두 noop."""
    db_path = tmp_path / "disabled.db"
    c = PersistentCache(db_path=db_path)
    # enabled 강제 비활성화
    c._enabled = False
    try:
        c.set("news:005935:EVT-DIS", {"value": 99}, ttl_seconds=3600)
        result = c.get("news:005935:EVT-DIS")
        assert result is None
    finally:
        c._enabled = True  # cleanup
        c.close()


def test_string_false_cache_config_is_disabled(monkeypatch, tmp_path: Path) -> None:
    """cache.enabled='false' 문자열이 캐시를 켜지 않도록 방어."""
    monkeypatch.setattr(
        "src.cache.persistent_cache._load_cache_config",
        lambda: {
            "enabled": "false",
            "storage_path": str(tmp_path / "ignored.db"),
            "news_ttl_seconds": 3600,
            "agent_report_ttl_seconds": 1800,
            "cleanup_interval_seconds": 600,
            "max_entries": 100,
        },
    )
    c = PersistentCache(db_path=tmp_path / "string_false.db")
    try:
        assert c.stats()["enabled"] is False
        c.set("news:005930:EVT-DIS", {"value": 1}, ttl_seconds=3600)
        assert c.get("news:005930:EVT-DIS") is None
    finally:
        c.close()


# ============================================================
# 8. News Agent integration: cache hit/miss
# ============================================================


def test_news_agent_cache_hit(tmp_path: Path) -> None:
    """같은 event_id 재분석 시 LLM 호출 skip (cache hit)."""
    from src.agents.cold.news import NewsAgent

    # LLM router mock
    mock_router = MagicMock()
    mock_result = MagicMock()
    mock_result.success = True
    mock_result.content = (
        '{"stance":"buy","impacted_tickers":["005930"],'
        '"impacted_sectors":["반도체"],"narrative":"테스트 캐시 결과"}'
    )
    mock_router.call.return_value = mock_result

    db_path = tmp_path / "news_integration.db"
    cache = PersistentCache(db_path=db_path)

    try:
        agent = NewsAgent(llm_router=mock_router, cache=cache)

        event = {
            "event_type": "news",
            "ticker": "005930",
            "title": "삼성전자 실적 호조",
            "summary": "영업이익 급증",
            "event_id": "EVT-20260502-NEWS-001",
        }

        # 1차 호출: LLM 호출
        result1 = agent.analyze(event)
        assert result1 is not None
        assert mock_router.call.call_count == 1

        # 2차 호출: 동일 event_id → cache hit → LLM 호출 없음
        result2 = agent.analyze(event)
        assert result2 is not None
        assert mock_router.call.call_count == 1  # 추가 호출 없음

        # 결과 일치
        assert result1["payload"]["stance"] == result2["payload"]["stance"]
    finally:
        cache.close()


def test_news_agent_no_cache_no_event_id(tmp_path: Path) -> None:
    """event_id 없으면 캐시 key 생성 안 함. LLM 항상 호출."""
    from src.agents.cold.news import NewsAgent

    mock_router = MagicMock()
    mock_result = MagicMock()
    mock_result.success = True
    mock_result.content = (
        '{"stance":"neutral","impacted_tickers":[],'
        '"impacted_sectors":[],"narrative":"이벤트 ID 없음"}'
    )
    mock_router.call.return_value = mock_result

    db_path = tmp_path / "no_event_id.db"
    cache = PersistentCache(db_path=db_path)

    try:
        agent = NewsAgent(llm_router=mock_router, cache=cache)
        event = {
            "event_type": "news",
            "ticker": "000270",
            "title": "기아 뉴스",
            "summary": "내용 없음",
            # event_id 미포함
        }

        agent.analyze(event)
        agent.analyze(event)

        # event_id 없으므로 캐시 미사용. LLM 2번 호출.
        assert mock_router.call.call_count == 2
    finally:
        cache.close()


# ============================================================
# C-R4 신규 테스트: stats() lock + max_entries eviction
# ============================================================


def test_stats_lock_no_negative_active_keys(tmp_path: Path) -> None:
    """stats()가 lock 보호 하에서 active_keys 음수를 반환하지 않음 (C-R4-1).

    10 스레드 동시 stats() 호출 중 active_keys >= 0 항상 유지.
    """
    db_path = tmp_path / "stats_lock.db"
    c = PersistentCache(db_path=db_path)

    # 유효 항목 5개 + 곧 만료될 항목 5개 삽입
    for i in range(5):
        c.set(f"key:valid:{i}", {"v": i}, ttl_seconds=3600)
    for i in range(5):
        c.set(f"key:expiring:{i}", {"v": i}, ttl_seconds=1)

    results: list[dict] = []
    errors: list[Exception] = []

    def call_stats() -> None:
        try:
            s = c.stats()
            results.append(s)
        except Exception as e:
            errors.append(e)

    # 만료 직후 타이밍에서 동시 stats() 호출
    time.sleep(1.1)
    threads = [threading.Thread(target=call_stats) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"stats() 동시 호출 중 에러: {errors}"
    for s in results:
        assert s["active_keys"] >= 0, f"active_keys 음수 발생: {s}"

    c.close()


def test_max_entries_eviction(tmp_path: Path) -> None:
    """max_entries=5 설정 시 6번째 set에서 LRU eviction 발생 (C-R4-2).

    총 항목 수가 max_entries를 초과하지 않아야 함.
    """
    db_path = tmp_path / "max_entries.db"
    c = PersistentCache(db_path=db_path)
    # 강제로 max_entries=5 설정 (yaml 로드값 override)
    c._max_entries = 5

    try:
        for i in range(5):
            c.set(f"key:{i}", {"v": i}, ttl_seconds=3600)

        s_before = c.stats()
        assert s_before["total_keys"] == 5

        # 6번째 삽입: eviction 발생해야 함
        c.set("key:new", {"v": 99}, ttl_seconds=3600)

        s_after = c.stats()
        # eviction 후 total은 5 이하 (evict 1개 + 신규 1개 = 동일 5개)
        assert s_after["total_keys"] <= 5, (
            f"eviction 미발생: total_keys={s_after['total_keys']} > max_entries=5"
        )
        # 새 항목은 존재해야 함
        assert c.get("key:new") is not None
    finally:
        c.close()
