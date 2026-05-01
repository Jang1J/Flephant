"""CommunityCrawler unit tests. S2-4 실구현 검증.

coverage:
  - mock 모드: COMMUNITY_SCRAPE_ENABLED 미설정 시 _is_mock=True
  - mock poll: ticker 당 3개 post 반환
  - ticker zero-padding: "5930" -> "005930"
  - parse_sentiment: CommunityScore 반환
  - spam_filter: length < 10 제거
  - spam_filter: url + 짧은 내용 제거
  - spam_filter: keyword_spam 매칭 제거
  - manipulation_filter: pump_keywords 매칭 제거
  - manipulation_filter: extreme_sentiment 매칭 제거
  - sentiment dict: positive 단어 점수 양수
  - sentiment dict: negative 단어 점수 음수
  - sentiment score range: -1.0 ~ +1.0
  - empty posts: comm_score 0.0
  - rate_limiter: poll 호출마다 wait_and_acquire 1회
  - real 모드: NotImplementedError 발생
  - poll_and_normalize: C2 event dict 반환
  - poll_and_normalize: 정규화 실패 skip (예외 미전파)
  - filter stages chain: spam -> manipulation -> sentiment 순서
  - CommunityScore 필드 완전성

환경변수 없이 통과하도록 COMMUNITY_SCRAPE_ENABLED 미설정 = mock 기본.
occurred_at은 과거 시간 사용 (PIT-Safety 통과).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

if TYPE_CHECKING:
    from src.connectors.community import CommunityPost

_KST = ZoneInfo("Asia/Seoul")

# PIT-Safety 통과: 과거 시각
_PAST_TS = datetime(2025, 4, 21, 9, 0, 0, tzinfo=_KST)

C2_REQUIRED_FIELDS = (
    "event_id",
    "source",
    "event_type",
    "scope",
    "title",
    "summary",
    "occurred_at",
    "ingest_ts",
    "priority",
    "llm_required",
    "ttl",
    "expires_at",
    "supersedes",
    "payload",
    "pit_safe",
)


# ------------------------------------------------------------------ #
# 공통 픽스처
# ------------------------------------------------------------------ #

@pytest.fixture
def mock_rate_limiter():
    rl = MagicMock()
    rl.wait_and_acquire.return_value = None
    return rl


@pytest.fixture
def mock_normalizer():
    n = MagicMock()
    n.normalize.return_value = {
        "event_id": "EVT-test-community-001",
        "source": "community",
        "event_type": "community",
        "scope": "ticker:005930",
        "title": "테스트 게시글",
        "summary": "테스트 내용",
        "occurred_at": _PAST_TS.isoformat(),
        "ingest_ts": _PAST_TS.isoformat(),
        "priority": "low",
        "llm_required": True,
        "ttl": 900,
        "expires_at": (_PAST_TS + timedelta(seconds=900)).isoformat(),
        "supersedes": None,
        "payload": {},
        "pit_safe": True,
    }
    return n


@pytest.fixture
def crawler_mock(mock_rate_limiter, mock_normalizer):
    """mock 모드 CommunityCrawler (COMMUNITY_SCRAPE_ENABLED 미설정)."""
    from src.connectors.community import CommunityCrawler
    return CommunityCrawler(
        rate_limiter=mock_rate_limiter,
        normalizer=mock_normalizer,
    )


def _make_post(
    post_id: str = "POST-001",
    ticker: str = "005930",
    title: str = "테스트 제목",
    content: str = "테스트 내용입니다.",
    timestamp: datetime | None = None,
) -> "CommunityPost":
    from src.connectors.community import CommunityPost
    return CommunityPost(
        post_id=post_id,
        ticker=ticker,
        author_id="user_hash_1",
        title=title,
        content=content,
        timestamp=timestamp or _PAST_TS,
        url="https://mock.example.com/1",
        view_count=100,
        comment_count=5,
    )


# ------------------------------------------------------------------ #
# 1. Mock 모드 기본 동작
# ------------------------------------------------------------------ #

def test_poll_mock_mode_when_no_env(mock_rate_limiter, mock_normalizer):
    """COMMUNITY_SCRAPE_ENABLED 미설정 -> _is_mock=True."""
    with patch.dict("os.environ", {}, clear=False):
        # 혹시 환경변수가 설정돼 있을 경우를 대비해 제거
        import os
        os.environ.pop("COMMUNITY_SCRAPE_ENABLED", None)
        from src.connectors.community import CommunityCrawler
        c = CommunityCrawler(rate_limiter=mock_rate_limiter, normalizer=mock_normalizer)
        assert c._is_mock is True


def test_poll_returns_posts_per_ticker(crawler_mock):
    """mock 모드: ticker 2개 -> 6개 post (ticker 당 3개)."""
    posts = crawler_mock.poll(["005930", "000660"])
    assert len(posts) == 6


def test_poll_returns_3_posts_for_single_ticker(crawler_mock):
    """mock 모드: ticker 1개 -> 3개 post."""
    posts = crawler_mock.poll(["005930"])
    assert len(posts) == 3


def test_poll_zero_padded_tickers(crawler_mock):
    """ticker '5930' -> post.ticker '005930' (zero-padded)."""
    posts = crawler_mock.poll(["5930"])
    assert all(p.ticker == "005930" for p in posts)


def test_poll_zero_padded_short_ticker(crawler_mock):
    """ticker '660' -> post.ticker '000660'."""
    posts = crawler_mock.poll(["660"])
    assert all(p.ticker == "000660" for p in posts)


# ------------------------------------------------------------------ #
# 2. parse_sentiment: CommunityScore 반환
# ------------------------------------------------------------------ #

def test_parse_sentiment_returns_community_score(crawler_mock):
    """parse_sentiment -> {ticker: CommunityScore} dict."""
    posts = crawler_mock.poll(["005930"])
    scores = crawler_mock.parse_sentiment(posts)
    assert "005930" in scores
    from src.connectors.community import CommunityScore
    assert isinstance(scores["005930"], CommunityScore)


def test_community_score_fields_complete(crawler_mock):
    """CommunityScore 필수 7개 필드 완전성."""
    posts = crawler_mock.poll(["005930"])
    scores = crawler_mock.parse_sentiment(posts)
    score = scores["005930"]
    assert hasattr(score, "ticker")
    assert hasattr(score, "comm_score")
    assert hasattr(score, "post_count")
    assert hasattr(score, "spam_filtered")
    assert hasattr(score, "manipulation_flagged")
    assert hasattr(score, "window_minutes")
    assert hasattr(score, "timestamp")


# ------------------------------------------------------------------ #
# 3. spam filter
# ------------------------------------------------------------------ #

def test_spam_filter_length(crawler_mock):
    """제목+내용 10자 미만 -> spam 필터."""
    short_post = _make_post(title="짧", content="글")
    passed, filtered = crawler_mock._filter_spam([short_post])
    assert filtered == 1
    assert len(passed) == 0


def test_spam_filter_normal_post_passes(crawler_mock):
    """길이 충분한 정상 post -> 통과."""
    normal_post = _make_post(title="정상 제목입니다", content="충분한 내용을 가진 게시글입니다.")
    passed, filtered = crawler_mock._filter_spam([normal_post])
    assert filtered == 0
    assert len(passed) == 1


def test_spam_filter_url_spam(crawler_mock):
    """URL 포함 + 짧은 내용 -> spam 필터."""
    url_post = _make_post(title="링크", content="https://spam.com")
    passed, filtered = crawler_mock._filter_spam([url_post])
    # "링크 https://spam.com" = 16자 < 50 + URL 있음 -> spam
    assert filtered == 1


def test_spam_filter_url_with_long_content_passes(crawler_mock):
    """URL 포함이지만 내용 50자 이상 -> 통과 (url_spam 조건: URL AND length < 50)."""
    long_url_post = _make_post(
        title="분석 자료 공유합니다",
        content="https://example.com 이 링크는 공식 분석 자료입니다. 삼성전자 실적 분석 내용 포함.",
    )
    # 길이 충분 -> url_spam 비해당
    passed, filtered = crawler_mock._filter_spam([long_url_post])
    # length_filter(총 길이 >= 10)는 통과. url_spam(length < 50)도 통과.
    # keyword_spam 없으면 통과.
    assert filtered == 0


def test_spam_filter_keyword_match(crawler_mock):
    """spam keyword 포함 -> 필터."""
    kw_post = _make_post(title="리딩방 참여 안내", content="VIP방 입장하세요")
    passed, filtered = crawler_mock._filter_spam([kw_post])
    assert filtered >= 1


# ------------------------------------------------------------------ #
# 4. manipulation filter
# ------------------------------------------------------------------ #

def test_manipulation_filter_pump_keywords(crawler_mock):
    """pump 키워드 (폭등/대박 등) -> manipulation 필터."""
    pump_post = _make_post(title="무조건 폭등", content="오늘 대박 날 것 같습니다")
    passed, flagged = crawler_mock._filter_manipulation([pump_post])
    assert flagged >= 1


def test_manipulation_filter_extreme_words(crawler_mock):
    """extreme_sentiment 키워드 -> manipulation 필터."""
    extreme_post = _make_post(title="분석", content="반드시 오른다 무조건 상승")
    passed, flagged = crawler_mock._filter_manipulation([extreme_post])
    assert flagged >= 1


def test_manipulation_filter_normal_post_passes(crawler_mock):
    """정상 post -> manipulation 통과."""
    normal_post = _make_post(title="실적 분석", content="Q4 실적 발표 예상치 부합. 긍정적 전망.")
    passed, flagged = crawler_mock._filter_manipulation([normal_post])
    assert flagged == 0
    assert len(passed) == 1


# ------------------------------------------------------------------ #
# 5. sentiment dict
# ------------------------------------------------------------------ #

def test_sentiment_dict_positive_word_counts(crawler_mock):
    """긍정 단어 포함 post -> comm_score > 0."""
    pos_post = _make_post(title="매수 추천", content="반등 기대. 기대 실적 호조.")
    score = crawler_mock._compute_sentiment([pos_post])
    assert score > 0.0


def test_sentiment_dict_negative_word_counts(crawler_mock):
    """부정 단어 포함 post -> comm_score < 0."""
    neg_post = _make_post(title="손절 고민", content="하락 지속. 공매도 우려. 실적 쇼크.")
    score = crawler_mock._compute_sentiment([neg_post])
    assert score < 0.0


def test_sentiment_score_range(crawler_mock):
    """comm_score 범위: -1.0 ~ +1.0."""
    posts = crawler_mock.poll(["005930"])
    scores = crawler_mock.parse_sentiment(posts)
    for ticker, score in scores.items():
        assert -1.0 <= score.comm_score <= 1.0, f"{ticker}: comm_score={score.comm_score} 범위 초과"


def test_empty_posts_returns_zero_score(crawler_mock):
    """빈 posts -> _compute_sentiment 0.0."""
    score = crawler_mock._compute_sentiment([])
    assert score == 0.0


def test_parse_sentiment_empty_posts_dict(crawler_mock):
    """빈 posts 리스트 -> parse_sentiment 빈 dict."""
    scores = crawler_mock.parse_sentiment([])
    assert scores == {}


# ------------------------------------------------------------------ #
# 6. rate_limiter 호출 확인
# ------------------------------------------------------------------ #

def test_rate_limiter_invoked_per_poll(crawler_mock, mock_rate_limiter):
    """poll 호출마다 wait_and_acquire 1회 호출."""
    crawler_mock.poll(["005930"])
    crawler_mock.poll(["000660"])
    assert mock_rate_limiter.wait_and_acquire.call_count == 2


# ------------------------------------------------------------------ #
# 7. real 모드: NotImplementedError
# ------------------------------------------------------------------ #

def test_poll_real_mode_raises_not_implemented(mock_rate_limiter, mock_normalizer, monkeypatch):
    """COMMUNITY_SCRAPE_ENABLED=1 -> NotImplementedError."""
    monkeypatch.setenv("COMMUNITY_SCRAPE_ENABLED", "1")
    from src.connectors.community import CommunityCrawler
    c = CommunityCrawler(rate_limiter=mock_rate_limiter, normalizer=mock_normalizer)
    assert c._is_mock is False
    with pytest.raises(NotImplementedError, match="COMMUNITY_SCRAPE_NOT_IMPLEMENTED"):
        c.poll(["005930"])


# ------------------------------------------------------------------ #
# 8. poll_and_normalize: C2 event dict
# ------------------------------------------------------------------ #

def test_poll_and_normalize_c2_events(crawler_mock, mock_normalizer):
    """poll_and_normalize -> C2 event dict 리스트 반환. 필수 필드 확인."""
    events = crawler_mock.poll_and_normalize(["005930"])
    # ticker 당 3개 post
    assert len(events) == 3
    for ev in events:
        for field in C2_REQUIRED_FIELDS:
            assert field in ev, f"C2 필수 필드 누락: {field}"
    assert events[0]["source"] == "community"
    assert events[0]["event_type"] == "community"


def test_poll_and_normalize_handles_normalizer_exception(
    mock_rate_limiter, mock_normalizer
):
    """normalize 예외 -> skip (예외 미전파). 정상 post 만 반환."""
    from src.connectors.community import CommunityCrawler

    # 첫 번째 호출 예외, 이후 정상
    mock_normalizer.normalize.side_effect = [
        Exception("정규화 오류"),
        mock_normalizer.normalize.return_value,
        mock_normalizer.normalize.return_value,
    ]

    c = CommunityCrawler(rate_limiter=mock_rate_limiter, normalizer=mock_normalizer)
    events = c.poll_and_normalize(["005930"])
    # 3개 post 중 첫 번째 실패 -> 2개 반환
    assert len(events) == 2


def test_poll_and_normalize_normalizer_called_with_post_title(
    mock_rate_limiter, mock_normalizer
):
    """poll_and_normalize -> normalizer.normalize() 에 post_title/posted_at 필드 전달 확인."""
    from src.connectors.community import CommunityCrawler
    c = CommunityCrawler(rate_limiter=mock_rate_limiter, normalizer=mock_normalizer)
    c.poll_and_normalize(["005930"])

    # normalize 첫 번째 호출 args/kwargs 확인
    first_call = mock_normalizer.normalize.call_args_list[0]
    # positional: normalize(raw_event, source="community")
    raw_arg = first_call.args[0]
    assert "post_title" in raw_arg      # _normalize_community 필수 필드
    assert "posted_at" in raw_arg       # _normalize_community 필수 필드
    # source 는 positional 또는 keyword
    source_val = (
        first_call.args[1] if len(first_call.args) > 1
        else first_call.kwargs.get("source")
    )
    assert source_val == "community"


# ------------------------------------------------------------------ #
# 9. filter stages chain
# ------------------------------------------------------------------ #

def test_filter_stages_chain(crawler_mock):
    """spam -> manipulation -> sentiment 순서 체인 동작 확인.

    spam 1건 + manipulation 1건 + 정상 1건 -> post_count=1, spam=1, manip=1.
    """
    spam_post = _make_post(post_id="SPAM", title="짧", content="글")           # length 필터
    manip_post = _make_post(post_id="MANIP", title="반드시 오른다", content="무조건 상승 확정")
    normal_post = _make_post(post_id="OK", title="정상 분석 게시글입니다", content="실적 발표 후 긍정적 전망입니다.")

    all_posts = [spam_post, manip_post, normal_post]

    # Stage 1: spam
    after_spam, spam_count = crawler_mock._filter_spam(all_posts)
    assert spam_count >= 1

    # Stage 2: manipulation (spam 제거 후)
    after_manip, manip_count = crawler_mock._filter_manipulation(after_spam)
    assert manip_count >= 1

    # Stage 3: normal_post 가 남아있어야 함
    assert len(after_manip) >= 1
    remaining_ids = [p.post_id for p in after_manip]
    assert "OK" in remaining_ids


# ------------------------------------------------------------------ #
# 10. sentiment dict 독립 픽스처 (Warning #3: 실 yaml 의존 제거)
# ------------------------------------------------------------------ #

def test_sentiment_dict_positive_isolated(monkeypatch, mock_rate_limiter, mock_normalizer):
    """mock sentiment_dict 로 독립 검증 (실 yaml 의존 제거).

    커롤러 내부 _sentiment_dict/_sentiment_weights 를 직접 교체해서
    실 yaml 파일 내용과 무관하게 positive 단어 점수가 양수임을 보장.
    """
    from src.connectors.community import CommunityCrawler, CommunityPost

    crawler = CommunityCrawler(rate_limiter=mock_rate_limiter, normalizer=mock_normalizer)
    # sentiment_dict 를 직접 교체 (실 yaml 의존 제거)
    crawler._sentiment_dict = {
        "positive": {
            "strong": ["급등", "호실적"],
            "medium": ["상승", "기대"],
            "weak": ["약보합"],
        },
        "negative": {
            "strong": [],
            "medium": [],
            "weak": [],
        },
        "weights": {"strong": 1.0, "medium": 0.5, "weak": 0.2},
    }
    crawler._sentiment_weights = crawler._sentiment_dict["weights"]

    post = CommunityPost(
        post_id="t1", ticker="005930", author_id="h1",
        title="급등 기대", content="호실적 전망",
        timestamp=_PAST_TS, url="", view_count=0, comment_count=0,
    )
    score = crawler._compute_sentiment([post])
    assert score > 0.0, f"positive words -> score > 0, got {score}"


def test_sentiment_dict_negative_isolated(monkeypatch, mock_rate_limiter, mock_normalizer):
    """mock sentiment_dict 로 독립 검증 (negative 케이스).

    실 yaml 파일 내용과 무관하게 negative 단어 점수가 음수임을 보장.
    """
    from src.connectors.community import CommunityCrawler, CommunityPost

    crawler = CommunityCrawler(rate_limiter=mock_rate_limiter, normalizer=mock_normalizer)
    crawler._sentiment_dict = {
        "positive": {
            "strong": [],
            "medium": [],
            "weak": [],
        },
        "negative": {
            "strong": ["폭락", "악재"],
            "medium": ["하락", "부진"],
            "weak": ["소폭 하락"],
        },
        "weights": {"strong": 1.0, "medium": 0.5, "weak": 0.2},
    }
    crawler._sentiment_weights = crawler._sentiment_dict["weights"]

    post = CommunityPost(
        post_id="t2", ticker="005930", author_id="h2",
        title="폭락 우려", content="악재 지속 하락 전망",
        timestamp=_PAST_TS, url="", view_count=0, comment_count=0,
    )
    score = crawler._compute_sentiment([post])
    assert score < 0.0, f"negative words -> score < 0, got {score}"


# ------------------------------------------------------------------ #
# 11. filter stages chain Stage 3 sentiment 검증 (Warning #4)
# ------------------------------------------------------------------ #

def test_filter_stages_chain_stage3_sentiment(mock_rate_limiter, mock_normalizer):
    """Stage 1 spam -> Stage 2 manipulation -> Stage 3 sentiment 체인 전체 검증.

    Stage 3: 유효 post 2개 (positive 1 + negative 1) -> comm_score -1.0~+1.0.
    parse_sentiment 로 Stage 3 까지 실제 실행.
    """
    from src.connectors.community import CommunityCrawler

    crawler = CommunityCrawler(rate_limiter=mock_rate_limiter, normalizer=mock_normalizer)
    # 독립 sentiment_dict 주입 (실 yaml 의존 제거)
    crawler._sentiment_dict = {
        "positive": {
            "strong": ["급등"], "medium": ["기대"], "weak": [],
        },
        "negative": {
            "strong": ["폭락"], "medium": ["하락"], "weak": [],
        },
        "weights": {"strong": 1.0, "medium": 0.5, "weak": 0.2},
    }
    crawler._sentiment_weights = crawler._sentiment_dict["weights"]

    ticker = "005930"
    posts = [
        _make_post("stage1", ticker, "", ""),                         # spam (길이 0)
        _make_post("stage2", ticker, "10배 폭등 확정!", ""),          # manipulation
        _make_post("stage3_pos", ticker, "매수 추천", "급등 기대"),    # valid + positive
        _make_post("stage3_neg", ticker, "매도 신호", "폭락 하락"),    # valid + negative
    ]

    scores = crawler.parse_sentiment(posts)

    assert ticker in scores
    score = scores[ticker]

    # Stage 1 spam 필터: 길이 0 post 제거
    assert score.spam_filtered >= 1, "Stage 1 spam 필터 미적용"
    # Stage 2 manipulation 필터: 폭등 확정 post 제거
    assert score.manipulation_flagged >= 1, "Stage 2 manipulation 필터 미적용"
    # Stage 3 유효 post: stage3_pos + stage3_neg 2개
    assert score.post_count == 2, f"Stage 3 유효 post 수 2 기대, got {score.post_count}"
    # sentiment: -1.0 ~ +1.0 범위
    assert -1.0 <= score.comm_score <= 1.0, f"comm_score 범위 초과: {score.comm_score}"


# ------------------------------------------------------------------ #
# 12. coordinated_timing dead code baseline (Warning #5)
# ------------------------------------------------------------------ #

def test_manipulation_coordinated_timing_currently_passes(mock_rate_limiter, mock_normalizer):
    """coordinated_timing rule 은 단일 post 레벨 미구현 (Sprint 4 defer).

    rule 이 yaml 에 존재해도 pass 처리 -> post 가 통과함을 명시적으로 검증.
    Sprint 4 구현 시 regression 방지용 baseline 테스트.
    """
    from src.connectors.community import CommunityCrawler, CommunityPost

    crawler = CommunityCrawler(rate_limiter=mock_rate_limiter, normalizer=mock_normalizer)
    post = CommunityPost(
        post_id="coord1", ticker="005930", author_id="h1",
        title="정상 제목", content="정상 내용 실적 호조",
        timestamp=_PAST_TS, url="", view_count=100, comment_count=5,
    )
    # coordinated_timing rule 존재 여부 무관하게 단일 post 는 통과
    assert crawler._is_manipulation(post) is False, (
        "coordinated_timing 은 Sprint 4 구현 예정. 현재는 단일 post 통과."
    )
