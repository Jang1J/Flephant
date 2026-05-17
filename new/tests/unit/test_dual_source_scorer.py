"""DualSourceScorer 단위 테스트. Sprint 4 S4-1.

coverage (5 sub-task + 통합 + PIT-Safety):

  Sub-task 1: news_score_t (FinBERT + fallback)
    - 뉴스 텍스트 없음 → 0.0 반환
    - 긍정 뉴스 → 양수 점수 (keyword fallback 모드)
    - 부정 뉴스 → 음수 점수 (keyword fallback 모드)
    - FinBERT 실패 시 fallback source_note "finbert_fallback"

  Sub-task 2+3: comm_score_t_1 / comm_score_t_2
    - 긍정 텍스트 t-1 → 양수 (decay 포함)
    - comm_texts_t2=None → comm_score_t_2 = 0.0
    - decay 반영: |comm_score_t_2| < |comm_score_t_1| (t-2에 peak_lag 감쇠 추가)

  Sub-task 4: news_comm_divergence
    - 동일 점수 → divergence = 0.0
    - 반대 방향 점수 → divergence > 0
    - divergence 공식: abs(news - comm_t1)

  Sub-task 5: community_noise_multiplier
    - 정상 게시량 → multiplier = 1.0
    - 이상 급증 → multiplier < 1.0
    - historical 2개 미만 → multiplier = 1.0 (안전 기본값)

  통합 score():
    - C3A 5필드 전체 포함
    - ticker 6자리 zero-padded 자동 적용
    - source_notes 필드 존재

  PIT-Safety:
    - data_ts > snapshot_ts → PITViolationError raise

  배치:
    - score_universe: 2종목 리스트 → 2개 결과
    - PITViolationError 발생 시 전파
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.data.dual_source_scorer import (
    DualSourceScorer,
    _compute_noise_multiplier,
    _keyword_fallback_score,
)
from src.utils.pit_guard import PITViolationError

_KST = ZoneInfo("Asia/Seoul")

# PIT-Safe 기준 시각: 오늘 18:00 KST (충분히 과거)
_SAFE_SNAPSHOT = datetime.now(_KST).replace(hour=18, minute=0, second=0, microsecond=0)
# 안전한 data_ts: 오늘 07:50 KST (배치 전 수집 시각)
_SAFE_DATA_TS = datetime.now(_KST).replace(hour=7, minute=50, second=0, microsecond=0)
# 미래 data_ts (PIT-Safety 위반용)
_FUTURE_TS = _SAFE_SNAPSHOT + timedelta(hours=2)


@pytest.fixture
def scorer() -> DualSourceScorer:
    """DualSourceScorer 인스턴스. FinBERT 로드 없이 fallback 모드 고정."""
    import src.data.dual_source_scorer as mod
    # FinBERT 로드 시도를 건너뛰고 fallback 고정 (네트워크/모델 없는 CI 환경)
    mod._FINBERT_AVAILABLE = False
    return DualSourceScorer()


# =========================================================
# Sub-task 1: news_score_t
# =========================================================

class TestNewsScoreT:
    def test_empty_news_returns_zero(self, scorer: DualSourceScorer) -> None:
        """뉴스 텍스트 없음 → 0.0."""
        score, note = scorer.score_news(
            news_texts=[],
            data_ts=_SAFE_DATA_TS,
            snapshot_ts=_SAFE_SNAPSHOT,
        )
        assert score == 0.0
        assert note == "no_news"

    def test_positive_news_returns_positive_score(self, scorer: DualSourceScorer) -> None:
        """긍정 뉴스 (떡상, 상한가) → 양수 점수 (keyword fallback)."""
        score, _ = scorer.score_news(
            news_texts=["삼성전자 떡상 기대감 상한가 예상"],
            data_ts=_SAFE_DATA_TS,
            snapshot_ts=_SAFE_SNAPSHOT,
        )
        assert score > 0.0, f"긍정 뉴스인데 score={score}"

    def test_negative_news_returns_negative_score(self, scorer: DualSourceScorer) -> None:
        """부정 뉴스 (폭락, 하한가) → 음수 점수 (keyword fallback)."""
        score, _ = scorer.score_news(
            news_texts=["삼성전자 폭락 하한가 우려"],
            data_ts=_SAFE_DATA_TS,
            snapshot_ts=_SAFE_SNAPSHOT,
        )
        assert score < 0.0, f"부정 뉴스인데 score={score}"

    def test_source_note_fallback_when_finbert_unavailable(self, scorer: DualSourceScorer) -> None:
        """FinBERT 불가 시 source_note = 'finbert_fallback'."""
        import src.data.dual_source_scorer as mod
        mod._FINBERT_AVAILABLE = False

        _, note = scorer.score_news(
            news_texts=["테스트 뉴스 텍스트"],
            data_ts=_SAFE_DATA_TS,
            snapshot_ts=_SAFE_SNAPSHOT,
        )
        assert note == "finbert_fallback"

    def test_score_clamped_minus_one_to_plus_one(self) -> None:
        """keyword fallback 점수 -1 ~ +1 범위 clamp 검증."""
        sentiment_dict = {
            "positive": {"strong": ["떡상"] * 100},
            "negative": {"strong": []},
            "weights": {"strong": 1.0, "medium": 0.5, "weak": 0.2},
        }
        score = _keyword_fallback_score("떡상" * 50, sentiment_dict)
        assert -1.0 <= score <= 1.0


# =========================================================
# Sub-task 2+3: comm_score_t_1 / comm_score_t_2
# =========================================================

class TestCommScores:
    def test_positive_t1_texts_positive_score(self, scorer: DualSourceScorer) -> None:
        """긍정 커뮤니티 텍스트 (떡상, 매수) → t-1 양수 점수 (decay 포함).

        텍스트는 spam length_filter(< 10자) 통과하도록 10자 이상 사용.
        """
        s_t1, _ = scorer.score_community(
            comm_texts_t1=["떡상 각이다 매수 지금 바로", "상한가 기대되는 종목입니다"],
            comm_texts_t2=None,
            data_ts_t1=_SAFE_DATA_TS,
            snapshot_ts=_SAFE_SNAPSHOT,
        )
        assert s_t1 > 0.0, f"긍정 커뮤니티인데 comm_score_t1={s_t1}"

    def test_t2_none_returns_zero(self, scorer: DualSourceScorer) -> None:
        """comm_texts_t2=None → comm_score_t_2 = 0.0."""
        _, s_t2 = scorer.score_community(
            comm_texts_t1=["떡상"],
            comm_texts_t2=None,
            data_ts_t1=_SAFE_DATA_TS,
            snapshot_ts=_SAFE_SNAPSHOT,
        )
        assert s_t2 == 0.0

    def test_t2_decay_smaller_than_t1(self, scorer: DualSourceScorer) -> None:
        """동일 텍스트로 t-2 점수는 t-1보다 작아야 함 (peak_lag 감쇠 추가).

        텍스트는 spam length_filter(< 10자) 통과하도록 10자 이상 사용.
        t-1: raw_score × decay_lambda^1
        t-2: raw_score × decay_lambda^2 (peak_lag_days=2)
        decay_lambda=0.4 이므로 |t_2| = |t_1| × 0.4
        """
        texts = ["떡상 각이다 바로 지금", "상한가 기대하는 종목", "매수 추천합니다 지금"]
        s_t1, s_t2 = scorer.score_community(
            comm_texts_t1=texts,
            comm_texts_t2=texts,
            data_ts_t1=_SAFE_DATA_TS,
            data_ts_t2=_SAFE_DATA_TS,
            snapshot_ts=_SAFE_SNAPSHOT,
        )
        # t-1: decay_lambda^1, t-2: decay_lambda^2 → |t_2| < |t_1|
        assert abs(s_t2) < abs(s_t1), (
            f"t-2 점수({s_t2})가 t-1({s_t1})보다 작아야 함 (peak_lag 감쇠)"
        )

    def test_empty_t1_returns_zero(self, scorer: DualSourceScorer) -> None:
        """빈 t-1 텍스트 → 0.0."""
        s_t1, _ = scorer.score_community(
            comm_texts_t1=[],
            snapshot_ts=_SAFE_SNAPSHOT,
        )
        assert s_t1 == 0.0


# =========================================================
# Sub-task 4: news_comm_divergence
# =========================================================

class TestDivergence:
    def test_same_score_zero_divergence(self, scorer: DualSourceScorer) -> None:
        """동일 점수 → divergence = 0.0."""
        assert scorer.compute_divergence(0.5, 0.5) == 0.0

    def test_opposite_scores_max_divergence(self, scorer: DualSourceScorer) -> None:
        """반대 방향 극단 점수 → divergence 최대."""
        div = scorer.compute_divergence(1.0, -1.0)
        assert div == pytest.approx(2.0)

    def test_divergence_formula_abs(self, scorer: DualSourceScorer) -> None:
        """공식: abs(news - comm_t1). 순서 무관 동일 결과."""
        div_ab = scorer.compute_divergence(0.8, 0.3)
        div_ba = scorer.compute_divergence(0.3, 0.8)
        assert div_ab == pytest.approx(div_ba)
        assert div_ab == pytest.approx(0.5)

    def test_divergence_threshold_from_config(self, scorer: DualSourceScorer) -> None:
        """설정값(0.5)과 비교: 0.5 초과 시 uncertainty_penalty 트리거."""
        # divergence_threshold = 0.5 (dual_source.yaml)
        div_below = scorer.compute_divergence(0.3, 0.1)  # 0.2 < threshold
        div_above = scorer.compute_divergence(0.8, 0.1)  # 0.7 > threshold
        assert div_below < scorer._divergence_threshold
        assert div_above > scorer._divergence_threshold


# =========================================================
# Sub-task 5: community_noise_multiplier
# =========================================================

class TestNoiseMultiplier:
    def test_normal_volume_returns_one(self, scorer: DualSourceScorer) -> None:
        """정상 게시량 (z <= threshold) → multiplier = 1.0."""
        hist = [80.0, 90.0, 85.0, 88.0, 92.0]
        mult = scorer.compute_noise_multiplier(current_volume=89.0, historical_volumes=hist)
        assert mult == pytest.approx(1.0)

    def test_spike_volume_returns_less_than_one(self, scorer: DualSourceScorer) -> None:
        """이상 급증 (z >> threshold) → multiplier < 1.0."""
        hist = [80.0, 82.0, 79.0, 81.0, 80.0]
        mult = scorer.compute_noise_multiplier(
            current_volume=500.0,  # 매우 높은 값 → 큰 z-score
            historical_volumes=hist,
        )
        assert mult < 1.0, f"이상 급증인데 multiplier={mult}"
        assert mult > 0.0

    def test_insufficient_history_returns_one(self, scorer: DualSourceScorer) -> None:
        """historical < 2개 → multiplier = 1.0 (안전 기본값)."""
        mult = scorer.compute_noise_multiplier(
            current_volume=200.0,
            historical_volumes=[100.0],  # 1개만
        )
        assert mult == 1.0

    def test_empty_history_returns_one(self, scorer: DualSourceScorer) -> None:
        """historical 빈 리스트 → multiplier = 1.0."""
        mult = scorer.compute_noise_multiplier(current_volume=100.0, historical_volumes=[])
        assert mult == 1.0

    def test_zero_std_returns_one(self) -> None:
        """표준편차=0 (모든 값 동일) → multiplier = 1.0."""
        result = _compute_noise_multiplier(
            current_volume=100.0,
            historical_volumes=[100.0, 100.0, 100.0],
            noise_zscore_threshold=2.5,
        )
        assert result == 1.0

    def test_multiplier_clamped_zero_to_one(self) -> None:
        """multiplier는 항상 0.0 ~ 1.0 범위."""
        result = _compute_noise_multiplier(
            current_volume=10000.0,
            historical_volumes=[10.0, 12.0, 11.0, 9.0, 10.5],
            noise_zscore_threshold=2.5,
        )
        assert 0.0 <= result <= 1.0


# =========================================================
# 통합: score() 전체 C3A 출력
# =========================================================

class TestScoreIntegration:
    def test_score_returns_all_c3a_fields(self, scorer: DualSourceScorer) -> None:
        """score() → C3A 5피처 + ticker + asof + source_notes 전체 포함."""
        result = scorer.score(
            ticker="5930",  # zero-padding 테스트
            news_texts=["삼성전자 실적 호조"],
            comm_texts_t1=["떡상 기대"],
            comm_texts_t2=["매수 좋아"],
            current_volume=100.0,
            historical_volumes=[80.0, 85.0, 90.0],
            data_ts=_SAFE_DATA_TS,
            snapshot_ts=_SAFE_SNAPSHOT,
        )
        required_fields = {
            "ticker",
            "asof",
            "news_score_t",
            "comm_score_t_1",
            "comm_score_t_2",
            "news_comm_divergence",
            "community_noise_multiplier",
            "source_notes",
        }
        missing = required_fields - set(result.keys())
        assert not missing, f"C3A 필수 필드 누락: {missing}"

    def test_score_asof_uses_snapshot_ts(self, scorer: DualSourceScorer) -> None:
        """historical materialize 시 C3A asof는 현재 시각이 아니라 snapshot_ts."""
        snapshot = datetime(2026, 5, 4, 8, 30, 0, tzinfo=_KST)
        result = scorer.score(
            ticker="005930",
            news_texts=["삼성전자 실적 호조"],
            comm_texts_t1=["매수 좋아"],
            data_ts=snapshot,
            snapshot_ts=snapshot,
        )
        assert result["asof"] == snapshot.isoformat()

    def test_ticker_zero_padded(self, scorer: DualSourceScorer) -> None:
        """ticker '5930' → '005930' 자동 변환."""
        result = scorer.score(
            ticker="5930",
            news_texts=[],
            comm_texts_t1=[],
            snapshot_ts=_SAFE_SNAPSHOT,
        )
        assert result["ticker"] == "005930"

    def test_source_notes_present(self, scorer: DualSourceScorer) -> None:
        """source_notes 필드 존재 + 유효한 문자열."""
        result = scorer.score(
            ticker="000660",
            news_texts=["SK하이닉스 반도체 호조"],
            comm_texts_t1=["반도체 주목"],
            snapshot_ts=_SAFE_SNAPSHOT,
        )
        assert result["source_notes"] is not None
        assert isinstance(result["source_notes"], str)

    def test_score_numeric_ranges(self, scorer: DualSourceScorer) -> None:
        """5피처 범위 검증."""
        result = scorer.score(
            ticker="005380",
            news_texts=["현대차 글로벌 수요 증가"],
            comm_texts_t1=["현대차 매수"],
            comm_texts_t2=["현대차 기대"],
            current_volume=120.0,
            historical_volumes=[90.0, 95.0, 100.0, 85.0],
            data_ts=_SAFE_DATA_TS,
            snapshot_ts=_SAFE_SNAPSHOT,
        )
        # news_score_t: -1 ~ +1 (decay 후에도 범위 유지)
        assert -1.0 <= result["news_score_t"] <= 1.0
        assert -1.0 <= result["comm_score_t_1"] <= 1.0
        assert -1.0 <= result["comm_score_t_2"] <= 1.0
        # divergence: 0 ~ 2
        assert 0.0 <= result["news_comm_divergence"] <= 2.0
        # multiplier: 0 ~ 1
        assert 0.0 <= result["community_noise_multiplier"] <= 1.0

    def test_score_universe_two_tickers(self, scorer: DualSourceScorer) -> None:
        """score_universe: 2종목 → 2개 결과."""
        universe = [
            {
                "ticker": "005930",
                "news_texts": ["삼성전자 호조"],
                "comm_texts_t1": ["떡상"],
                "current_volume": 100.0,
                "historical_volumes": [80.0, 85.0, 90.0],
                "data_ts": _SAFE_DATA_TS,
            },
            {
                "ticker": "000660",
                "news_texts": ["SK하이닉스 반도체"],
                "comm_texts_t1": ["매수"],
                "current_volume": 90.0,
                "historical_volumes": [70.0, 75.0, 80.0],
                "data_ts": _SAFE_DATA_TS,
            },
        ]
        results = scorer.score_universe(universe=universe, snapshot_ts=_SAFE_SNAPSHOT)
        assert len(results) == 2
        tickers = {r["ticker"] for r in results}
        assert "005930" in tickers
        assert "000660" in tickers
        assert all(r["asof"] == _SAFE_SNAPSHOT.isoformat() for r in results)


# =========================================================
# PIT-Safety: 미래 데이터 → PITViolationError
# =========================================================

class TestPITSafety:
    def test_future_data_ts_raises_pit_violation(self, scorer: DualSourceScorer) -> None:
        """data_ts > snapshot_ts → PITViolationError raise (불변 원칙 1)."""
        # snapshot_ts 기준을 명시적으로 오늘 08:30 KST로 설정
        snap = datetime.now(_KST).replace(hour=8, minute=30, second=0, microsecond=0)
        # 미래 시각 = 10:00 KST (08:30보다 나중)
        future_ts = datetime.now(_KST).replace(hour=10, minute=0, second=0, microsecond=0)

        with pytest.raises((PITViolationError, ValueError)):
            scorer.score(
                ticker="005930",
                news_texts=["테스트"],
                comm_texts_t1=["테스트"],
                data_ts=future_ts,
                snapshot_ts=snap,
            )

    def test_past_data_ts_passes_pit_check(self, scorer: DualSourceScorer) -> None:
        """data_ts <= snapshot_ts → PITViolationError 미발생 (정상 통과)."""
        snap = datetime.now(_KST).replace(hour=18, minute=0, second=0, microsecond=0)
        past_ts = datetime.now(_KST).replace(hour=7, minute=50, second=0, microsecond=0)

        # 예외 없이 정상 완료해야 함
        result = scorer.score(
            ticker="005930",
            news_texts=[],
            comm_texts_t1=[],
            data_ts=past_ts,
            snapshot_ts=snap,
        )
        assert result["ticker"] == "005930"

    def test_score_universe_pit_violation_propagates(self, scorer: DualSourceScorer) -> None:
        """score_universe: 미래 data_ts → PITViolationError 전파."""
        snap = datetime.now(_KST).replace(hour=8, minute=30, second=0, microsecond=0)
        future_ts = datetime.now(_KST).replace(hour=10, minute=0, second=0, microsecond=0)

        universe = [
            {
                "ticker": "005930",
                "news_texts": [],
                "comm_texts_t1": [],
                "data_ts": future_ts,
            }
        ]
        with pytest.raises((PITViolationError, ValueError)):
            scorer.score_universe(universe=universe, snapshot_ts=snap)


# =========================================================
# R1-W2: comm_texts None 가드
# =========================================================


class TestCommTextsNoneGuard:
    def test_none_in_comm_texts_skipped(self, scorer: DualSourceScorer) -> None:
        """texts 리스트에 None 포함 시 TypeError 없이 skip."""
        # None이 섞여 있어도 예외 없이 나머지 텍스트로 점수 계산해야 함
        s_t1, _ = scorer.score_community(
            comm_texts_t1=[None, "떡상 기대되는 종목 매수", None],  # type: ignore[list-item]
            comm_texts_t2=None,
            snapshot_ts=_SAFE_SNAPSHOT,
        )
        # None은 skip, 유효 텍스트 1개로 점수 반환 (crash 없음이 핵심)
        assert isinstance(s_t1, float)
        assert -1.0 <= s_t1 <= 1.0

    def test_all_none_comm_texts_returns_zero(self, scorer: DualSourceScorer) -> None:
        """모든 항목이 None이면 유효 텍스트 없음 → 0.0."""
        s_t1, _ = scorer.score_community(
            comm_texts_t1=[None, None],  # type: ignore[list-item]
            comm_texts_t2=None,
            snapshot_ts=_SAFE_SNAPSHOT,
        )
        assert s_t1 == 0.0

    def test_int_in_comm_texts_skipped(self, scorer: DualSourceScorer) -> None:
        """str 이외 타입(int) 포함 시 skip. 유효 텍스트만 처리."""
        s_t1, _ = scorer.score_community(
            comm_texts_t1=[42, "상한가 기대 매수 좋아요", True],  # type: ignore[list-item]
            comm_texts_t2=None,
            snapshot_ts=_SAFE_SNAPSHOT,
        )
        assert isinstance(s_t1, float)
