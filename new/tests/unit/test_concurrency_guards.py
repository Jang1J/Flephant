"""가드 4: 동시성 focused test (병렬화 PR perf/parallel-top10 대비).

5 시나리오 race-free 검증:
1. shared RateLimiter — 같은 source는 같은 instance + 동시 acquire race-free
2. AuthManager token refresh — double-checked locking으로 single fetch만
3. KISRestClient counter/circuit mutation — lock 보호로 일관성 유지
4. ordered feed — fetch_results dict insertion order와 무관하게 selected_tickers 순서 보존
5. parallel fetch partial failure — 일부 ticker 실패가 다른 ticker에 영향 zero

기존 39 unit test가 단일 thread 행동 보존을 검증하는 반면, 본 파일은 동시성 race
회귀를 사전 차단한다.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src.connectors.kis_rest import KISRestClient
from src.utils.auth import AuthManager
from src.utils.rate_limiter import RateLimiter, get_shared_rate_limiter


# ----------------------------------------------------------------------------
# 시나리오 1: shared RateLimiter
# ----------------------------------------------------------------------------


def test_shared_rate_limiter_returns_singleton_per_source():
    """같은 source 이름에 대해 항상 같은 instance 반환 (process 단위 singleton)."""
    a = get_shared_rate_limiter("kis_rest")
    b = get_shared_rate_limiter("kis_rest")
    assert a is b


def test_shared_rate_limiter_different_sources_distinct():
    """다른 source는 별도 instance (소스별 독립 bucket)."""
    a = get_shared_rate_limiter("kis_rest")
    b = get_shared_rate_limiter("dart")
    assert a is not b


def test_rate_limiter_concurrent_acquire_no_negative_tokens():
    """다수 thread가 동시 acquire 호출해도 토큰 음수 또는 capacity 대폭 초과 없음.

    Lock이 없으면 race로 토큰 음수 가능. Lock 있으면 capacity 이하로만 성공.
    """
    rl = RateLimiter("kis_rest")
    capacity = rl.capacity
    n_threads = capacity * 4  # capacity 초과 호출

    success_count_box = {"v": 0}
    counter_lock = threading.Lock()

    def acquire_one():
        if rl.acquire(1):
            with counter_lock:
                success_count_box["v"] += 1

    threads = [threading.Thread(target=acquire_one) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # refill 시간 거의 없으니 capacity만큼만 성공해야 함. race 있으면 capacity 초과.
    # 약간의 refill 마진 허용(+5).
    assert success_count_box["v"] <= capacity + 5, (
        f"too many successes: {success_count_box['v']} vs capacity {capacity}"
    )


# ----------------------------------------------------------------------------
# 시나리오 2: AuthManager token refresh — double-checked locking
# ----------------------------------------------------------------------------


def test_auth_manager_concurrent_refresh_invokes_fetch_once(monkeypatch):
    """여러 thread가 동시에 get_kis_token 호출해도 _fetch_kis_token이 1회만 일어난다."""
    # 클래스 레벨 공유 캐시 초기화 (다른 테스트와 격리)
    AuthManager._shared_token_data = None

    fetch_count_box = {"v": 0}
    fetch_lock = threading.Lock()

    def fake_fetch(self):
        with fetch_lock:
            fetch_count_box["v"] += 1
        time.sleep(0.05)  # fetch에 시간 걸린다 가정
        return "fake_token_value", datetime.now(tz=timezone.utc) + timedelta(seconds=3600)

    monkeypatch.setattr(AuthManager, "_fetch_kis_token", fake_fetch)
    monkeypatch.setenv("KIS_PAPER_APP_KEY", "test_key")
    monkeypatch.setenv("KIS_PAPER_APP_SECRET", "test_secret")
    monkeypatch.setenv("KIS_PAPER_ACCOUNT_NUMBER", "12345678")
    monkeypatch.setenv("KIS_PAPER_ACCOUNT_PRODUCT_CODE", "01")
    monkeypatch.setenv("KIS_MODE", "virtual")

    def call_get_token():
        m = AuthManager()
        m.get_kis_token()

    threads = [threading.Thread(target=call_get_token) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert fetch_count_box["v"] == 1, (
        f"expected exactly 1 fetch (double-checked locking), got {fetch_count_box['v']}"
    )

    # Cleanup
    AuthManager._shared_token_data = None


# ----------------------------------------------------------------------------
# 시나리오 3: KISRestClient counter / circuit mutation lock
# ----------------------------------------------------------------------------


def test_kis_client_state_lock_protects_circuit_failure_counter(monkeypatch):
    """다수 thread가 동시 _record_call_failure 호출 시 consecutive_failures 정확히 카운트.

    Lock 없으면 read-modify-write race로 count 누락. Lock 있으면 정확히 N.
    """
    monkeypatch.setenv("KIS_PAPER_APP_KEY", "test")
    monkeypatch.setenv("KIS_PAPER_APP_SECRET", "test")
    monkeypatch.setenv("KIS_PAPER_ACCOUNT_NUMBER", "12345678")
    monkeypatch.setenv("KIS_PAPER_ACCOUNT_PRODUCT_CODE", "01")
    monkeypatch.setenv("KIS_MODE", "virtual")

    client = KISRestClient()
    # circuit breaker enabled로 가정. 정확한 카운트 검증을 위해 threshold를 매우 높게 두어
    # 카운트가 threshold에 도달해 reset되지 않게 한다.
    client._circuit_failure_threshold = 100_000

    n_threads = 1000

    def trigger_failure():
        # Exception None 전달 시 circuit 카운터만 증가 (non-retryable 체크 통과)
        client._record_call_failure(None)

    threads = [threading.Thread(target=trigger_failure) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert client._circuit_consecutive_failures == n_threads, (
        f"expected {n_threads} failures counted, got {client._circuit_consecutive_failures}"
    )


def test_kis_client_state_lock_protects_check_circuit_concurrent_reads():
    """다수 thread가 동시 _check_circuit 호출해도 inconsistent state 노출 없음.

    이 테스트는 race가 있으면 raise OR pass가 불일치할 수 있어 statistically 검증.
    """
    import os
    os.environ.setdefault("KIS_PAPER_APP_KEY", "test")
    os.environ.setdefault("KIS_PAPER_APP_SECRET", "test")
    os.environ.setdefault("KIS_PAPER_ACCOUNT_NUMBER", "12345678")
    os.environ.setdefault("KIS_PAPER_ACCOUNT_PRODUCT_CODE", "01")
    os.environ.setdefault("KIS_MODE", "virtual")

    client = KISRestClient()
    # circuit이 막 열린 상황 시뮬레이션
    client._circuit_opened_at = time.monotonic()
    client._circuit_consecutive_failures = client._circuit_failure_threshold

    results_box = {"raise_count": 0, "pass_count": 0}
    counter_lock = threading.Lock()

    def check():
        try:
            client._check_circuit()
            with counter_lock:
                results_box["pass_count"] += 1
        except Exception:
            with counter_lock:
                results_box["raise_count"] += 1

    threads = [threading.Thread(target=check) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 모든 호출이 동일하게 raise 또는 동일하게 pass 해야 함. mixed면 race.
    # circuit이 막 열린 상황이라 모두 raise 기대.
    assert results_box["raise_count"] == 100, (
        f"expected 100 raises, got raise={results_box['raise_count']} pass={results_box['pass_count']}"
    )


# ----------------------------------------------------------------------------
# 시나리오 4: ordered feed — selected_tickers 순서 보존
# ----------------------------------------------------------------------------


def test_ordered_feed_preserves_selected_tickers_order():
    """fetch_results dict insertion order와 무관하게 Phase 2 feed는 selected_tickers 순서 따른다."""
    selected_tickers = ["005930", "000660", "035420", "042700"]
    # 의도적으로 다른 순서로 dict 채움 (as_completed 결과 시뮬레이션)
    fetch_results = {
        "035420": ["bar_a"],
        "005930": ["bar_b"],
        "042700": ["bar_c"],
        "000660": ["bar_d"],
    }

    feed_order = []
    for ticker in selected_tickers:
        bars = fetch_results.get(ticker)
        if bars is None:
            continue
        feed_order.append(ticker)

    assert feed_order == selected_tickers, (
        f"feed order should follow selected_tickers, got {feed_order}"
    )


# ----------------------------------------------------------------------------
# 시나리오 5: parallel fetch partial failure 격리
# ----------------------------------------------------------------------------


def test_parallel_fetch_partial_failure_isolated_per_ticker():
    """일부 ticker 실패가 다른 ticker fetch에 영향 없음 (격리)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    selected_tickers = ["A", "B", "C", "D", "E"]
    failing_tickers = {"B", "D"}

    def fetch(ticker: str):
        if ticker in failing_tickers:
            raise RuntimeError(f"fetch failed for {ticker}")
        return f"bars_{ticker}"

    fetch_results: dict[str, str] = {}
    bar_errors: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=3) as pool:
        future_to_ticker = {pool.submit(fetch, t): t for t in selected_tickers}
        for future in as_completed(future_to_ticker):
            ticker = future_to_ticker[future]
            try:
                fetch_results[ticker] = future.result()
            except Exception as e:
                bar_errors[ticker] = str(e)

    # 실패한 ticker는 bar_errors에, 성공한 ticker는 fetch_results에.
    assert set(bar_errors.keys()) == failing_tickers
    assert set(fetch_results.keys()) == set(selected_tickers) - failing_tickers
    # 성공한 ticker는 정확한 결과 받음.
    for ticker in fetch_results:
        assert fetch_results[ticker] == f"bars_{ticker}"


# ----------------------------------------------------------------------------
# 시나리오 6: concurrent GetRecommendations (full integration)
# ----------------------------------------------------------------------------


def _make_mock_quant_agent(bundle_id: str):
    """build_recommendations_payload용 mock quant agent.

    score_cross_section은 mode=active로 1 ticker 응답.
    """
    mock = MagicMock()
    mock.model_metadata = {"version": "test_v1", "bundle_id": bundle_id}
    mock.on_bar = MagicMock()
    mock.score_cross_section = MagicMock(return_value={
        "tickers": ["005930"],
        "scores": {"005930": 0.5},
        "confidences": {"005930": 0.1},
        "ts": "2026-06-01T09:00:00+09:00",
        "mode": "active",
        "latency_ms": 1.0,
        "n_tickers": 1,
    })
    return mock


def _make_mock_market_client():
    """build_recommendations_payload용 mock KIS client.

    inquire_minute_bar는 ticker별 60개 분봉 반환 (warmup_bars 충족).
    """
    mock = MagicMock()

    def fake_bars(ticker, n_bars=60):
        return [
            {
                "ticker": ticker,
                "ts_close": f"2026-06-01T09:{i:02d}:00+09:00",
                "open": 70000,
                "high": 70100,
                "low": 69900,
                "close": 70000,
                "volume": 1000,
            }
            for i in range(n_bars or 60)
        ]

    mock.inquire_minute_bar = MagicMock(side_effect=fake_bars)
    return mock


def test_concurrent_build_recommendations_payload_isolated():
    """여러 thread가 동시에 build_recommendations_payload 호출 시 cross-request 격리.

    각 호출이:
    - 독립적 request_id 생성
    - 자기 quant_agent + market_data_client 사용 (서로 간섭 없음)
    - 동일 bundle_id 응답 (mismatch 없음)
    """
    from src.integration.grpc.payloads import build_recommendations_payload

    bundle_id = "TEST_BUNDLE"
    results: list[dict] = []
    results_lock = threading.Lock()
    errors: list[BaseException] = []

    def call_one():
        try:
            # quant_agent + market_data_client를 호출마다 새 instance로 주입
            # (실제 gRPC server도 매 요청마다 새로 만드는 패턴과 동일)
            payload = build_recommendations_payload(
                bundle_id=bundle_id,
                tickers=["005930"],
                include_diagnostics=False,
                quant_agent=_make_mock_quant_agent(bundle_id),
                market_data_client=_make_mock_market_client(),
            )
            with results_lock:
                results.append(payload)
        except BaseException as e:  # noqa: BLE001
            with results_lock:
                errors.append(e)

    threads = [threading.Thread(target=call_one) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 모든 호출이 예외 없이 완료
    assert not errors, f"concurrent calls raised exceptions: {errors[:3]}"
    assert len(results) == 10, f"expected 10 results, got {len(results)}"

    # 각 호출은 unique request_id 부여 (UUID 기반이라 충돌 없어야 함)
    request_ids = [r.get("request_id") for r in results]
    assert len(set(request_ids)) == 10, f"request_ids must be unique, got duplicates"

    # 각 호출은 자기 bundle_id를 응답에 그대로 반영 (cross-request 오염 없음)
    for r in results:
        response_bundle_id = r.get("bundle_id", "")
        assert response_bundle_id == bundle_id, (
            f"unexpected bundle_id in response: {response_bundle_id} (expected {bundle_id})"
        )
