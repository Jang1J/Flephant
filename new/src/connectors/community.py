"""커뮤니티 크롤러 v3 Sprint 2 S2-4 (2026-04-21).

Dual-Source 커뮤니티 소스. 뉴스(NaverNewsClient) vs 커뮤니티 divergence 원천.

## 사용 패턴

**표준 Cold Path 경로 (운영)**:
    posts = CommunityCrawler().poll(tickers=["005930", "000660"])
    for post in posts:
        EventGateway.ingest(post, source="community")

**Dual-Source 경로 (Sprint 4 S4-1)**:
    scorer = DualSourceScorer()
    comm_score = scorer.score_community(CommunityCrawler().poll(...))
    # comm_score -> LightGBM 피처 (comm_score_t-1, comm_score_t-2)

## 필터 파이프라인 (3단계)

1. spam_rules.yaml: 길이/URL/키워드/반복 패턴 1차 필터 (rule-based)
2. manipulation_rules.yaml: 주가 조작 패턴 2차 필터 (noise reduction)
3. sentiment_dict.yaml: 긍정/부정 단어 사전 기반 감성 점수

## Mock 모드 (Sprint 2)

공식 API 없음. robots.txt 거부 정책. Sprint 2 는 **mock 기본**.
실 스크래핑은 Sprint 4 S4-6 Paper Trading 정책 결정 후.

SSOT: api_contracts.md v3.5 C2 EventNormalizeContract (source='community')
"""
from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from src.connectors.base import BaseConnector
from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger
from src.utils.rate_limiter import RateLimiter
from src.data.event_normalizer import EventNormalizer

logger = get_logger("community")

_KST = ZoneInfo("Asia/Seoul")

# new/ 루트 기준 yaml 경로 (Path, 절대경로 독립)
_CONFIG_ROOT = Path(__file__).resolve().parents[2] / "config"


@dataclass
class CommunityPost:
    """커뮤니티 raw post (필터 전)."""

    post_id: str           # 커뮤니티 고유 ID
    ticker: str            # 관련 종목 (단일, 6자리 zero-padded)
    author_id: str         # 작성자 해시 (원문 노출 금지)
    title: str
    content: str
    timestamp: datetime    # tz-aware KST
    url: str               # 원문 링크 (옵션)
    view_count: int = 0
    comment_count: int = 0


@dataclass
class CommunityScore:
    """커뮤니티 감성 점수 (필터 후)."""

    ticker: str
    comm_score: float          # -1.0 (강한 부정) ~ +1.0 (강한 긍정)
    post_count: int            # 필터 통과 post 수
    spam_filtered: int         # 스팸 제거 수
    manipulation_flagged: int  # 조작 의심 플래그 수
    window_minutes: int        # 집계 창 (기본 5분)
    timestamp: datetime        # 집계 시각


class CommunityCrawler(BaseConnector):
    """커뮤니티 크롤러.

    - poll(tickers): raw post 목록 수집 (mock/real 자동 분기)
    - parse_sentiment(posts): 3-stage 필터 + sentiment 점수 계산

    Sprint 2: Mock 기본. 실 스크래핑은 Sprint 4 S4-6 에서 정책 결정.
    """

    def __init__(
        self,
        rate_limiter: RateLimiter | None = None,
        normalizer: EventNormalizer | None = None,
    ) -> None:
        super().__init__()  # BaseConnector: timeout_sec / max_retries / backoff_base 로드
        self._timeout_sec = self.timeout_sec
        self._max_retries = self.max_retries
        self._backoff_base = self.backoff_base

        # rate limiter: risk_config.yaml rate_limits.community 경유
        if rate_limiter is not None:
            self._rate_limiter = rate_limiter
        else:
            self._rate_limiter = RateLimiter("community")

        # spam_rules / manipulation_rules / sentiment_dict yaml 로드
        self._spam_rules = self._load_yaml_section(
            _CONFIG_ROOT / "spam_rules.yaml", "filters"
        )
        self._manipulation_rules = self._load_yaml_section(
            _CONFIG_ROOT / "manipulation_rules.yaml", "rules"
        )
        self._sentiment_dict = self._load_yaml_section(
            _CONFIG_ROOT / "sentiment_dict.yaml", root=True
        )
        # yaml SSOT 엄수. weights 섹션 누락 시 KeyError 전파 (불변 원칙 5).
        if "weights" not in self._sentiment_dict:
            raise KeyError(
                "sentiment_dict.yaml: 'weights' section required (strong/medium/weak)"
            )
        self._sentiment_weights = self._sentiment_dict["weights"]

        # dual_source 설정 (dual_source.yaml 경유)
        self._dual_cfg = self._load_yaml_section(
            _CONFIG_ROOT / "dual_source.yaml", root=True
        )
        # TODO(Sprint4-S4-1): DualSourceScorer 에서 activate.
        # 현재는 로드만 해둠. decay_lambda / peak_lag_days / noise_zscore 는
        # Sprint 4 S4-1 dual_source_scorer.py 에서 comm_score_t-1 / comm_score_t-2
        # 계산 시 사용 예정.
        self._community_cfg = self._dual_cfg.get("community", {})

        # Mock 분기: 공식 API 없음. Sprint 2 는 mock 기본.
        # 환경변수 COMMUNITY_SCRAPE_ENABLED=1 로 실 스크래핑 활성화 가능 (Sprint 4 정책 결정 후)
        self._scrape_enabled = os.environ.get("COMMUNITY_SCRAPE_ENABLED") == "1"
        self._is_mock = not self._scrape_enabled

        self._normalizer = normalizer or EventNormalizer()

        if self._is_mock:
            logger.info("[community] Sprint 2 mock 모드 (COMMUNITY_SCRAPE_ENABLED 미설정)")

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def poll(self, tickers: list[str], window_minutes: int = 5) -> list[CommunityPost]:
        """ticker 목록 커뮤니티 글 window_minutes 치 수집.

        Args:
            tickers: 종목 코드 리스트 (6자리 zero-padded 자동 적용)
            window_minutes: 수집 창 (기본 5분, 5분 폴링)

        Returns:
            raw CommunityPost 리스트. 필터 적용 전.

        Raises:
            NotImplementedError: COMMUNITY_SCRAPE_ENABLED=1 인데 실 스크래핑 미구현.
        """
        # PIT-Safety: Sprint 4 실 스크래핑 시 timestamp guard 추가 예정
        # 현재 mock 전용 → 미래 데이터 생성 없음
        normalized_tickers = [str(t).zfill(6) for t in tickers]

        # rate_limiter 획득 (mock 모드에서도 호출해서 테스트 가능하도록)
        self._rate_limiter.wait_and_acquire()

        if self._is_mock:
            return self._mock_poll(normalized_tickers, window_minutes)

        # 실 스크래핑 경로 (Sprint 4 S4-6 에서 구현)
        raise NotImplementedError(
            "COMMUNITY_SCRAPE_NOT_IMPLEMENTED: 실 스크래핑은 Sprint 4 S4-6 Paper Trading 정책 결정 후 구현. "
            "현재는 Mock 모드 (COMMUNITY_SCRAPE_ENABLED=0) 권장."
        )

    def parse_sentiment(
        self,
        posts: list[CommunityPost],
        window_minutes: int = 5,
    ) -> dict[str, CommunityScore]:
        """3-stage 필터 + sentiment 점수 계산. ticker 별 CommunityScore dict 반환.

        Stages:
            1. spam filter: length/url/keyword
            2. manipulation filter: 주가 조작 패턴 (rule-based)
            3. sentiment dict: 긍정/부정 단어 점수 합산 (strong/medium/weak 가중치)

        Returns:
            {ticker: CommunityScore(comm_score, post_count, spam_filtered, ...)}
        """
        by_ticker: dict[str, list[CommunityPost]] = defaultdict(list)
        for post in posts:
            by_ticker[post.ticker].append(post)

        scores: dict[str, CommunityScore] = {}
        now = datetime.now(_KST)

        for ticker, ticker_posts in by_ticker.items():
            # Stage 1: spam filter
            after_spam, spam_count = self._filter_spam(ticker_posts)
            # Stage 2: manipulation filter
            after_manip, manip_count = self._filter_manipulation(after_spam)
            # Stage 3: sentiment 점수
            raw_score = self._compute_sentiment(after_manip)

            scores[ticker] = CommunityScore(
                ticker=ticker,
                comm_score=raw_score,
                post_count=len(after_manip),
                spam_filtered=spam_count,
                manipulation_flagged=manip_count,
                window_minutes=window_minutes,
                timestamp=now,
            )

        return scores

    def poll_and_normalize(
        self, tickers: list[str], window_minutes: int = 5
    ) -> list[dict[str, Any]]:
        """poll + C2 EventNormalize. 편의 메서드.

        **경고**: EventGateway bypass. 운영은 poll() + EventGateway.ingest() 사용 권장.

        C2 event_normalizer._normalize_community 필수 필드:
            post_title (= post.title), posted_at (= post.timestamp.isoformat())
        """
        posts = self.poll(tickers, window_minutes)
        events: list[dict[str, Any]] = []
        for post in posts:
            raw = {
                "post_title": post.title,       # _normalize_community 필수 필드
                "posted_at": post.timestamp.isoformat(),  # _normalize_community 필수 필드
                "body": post.content[:200] if post.content else "",
                "ticker_mentioned": post.ticker,
                "spam_flag": False,
                # 추가 메타데이터 payload에 포함
                "post_id": post.post_id,
                "author_id_hash": post.author_id,
                "url": post.url,
                "view_count": post.view_count,
                "comment_count": post.comment_count,
            }
            try:
                event = self._normalizer.normalize(raw, source="community")
                events.append(event)
            except Exception as e:
                logger.warning(
                    "[community] 정규화 실패 skip: post_id=%s err=%s",
                    post.post_id,
                    e,
                )
        return events

    # ------------------------------------------------------------------ #
    # 내부 helper: yaml 로드
    # ------------------------------------------------------------------ #

    @staticmethod
    def _load_yaml_section(
        path: Path,
        section: str | None = None,
        root: bool = False,
    ) -> list | dict:
        """yaml 파일에서 섹션 로드.

        Args:
            path: 절대 또는 상대 경로
            section: 최상위 키. None 또는 root=True 이면 전체 dict 반환.
            root: True 이면 전체 dict 반환.

        Raises:
            FileNotFoundError: 파일 없음
            yaml.YAMLError: 파싱 실패
        """
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if root or section is None:
            return data
        return data.get(section, [])

    # ------------------------------------------------------------------ #
    # 내부 helper: 필터 파이프라인
    # ------------------------------------------------------------------ #

    def _filter_spam(
        self, posts: list[CommunityPost]
    ) -> tuple[list[CommunityPost], int]:
        """Stage 1: spam_rules.yaml filters 기반. (passed, filtered_count) 반환."""
        passed = []
        filtered = 0
        for post in posts:
            if self._is_spam(post):
                filtered += 1
            else:
                passed.append(post)
        return passed, filtered

    def _is_spam(self, post: CommunityPost) -> bool:
        """spam_rules.yaml 각 rule 체크. 하나라도 매치 시 True."""
        combined_text = f"{post.title} {post.content}"
        text_len = len(combined_text)

        for rule in self._spam_rules:
            rule_type = rule.get("type", "")

            if rule_type == "length_filter":
                # condition: "length < 10"
                cond = rule.get("condition", "")
                match = re.search(r"length\s*<\s*(\d+)", cond)
                if match and text_len < int(match.group(1)):
                    return True

            elif rule_type == "url_spam":
                # "contains_url AND length < 50"
                has_url = bool(re.search(r"https?://", combined_text))
                cond = rule.get("condition", "")
                match = re.search(r"length\s*<\s*(\d+)", cond)
                if has_url and match and text_len < int(match.group(1)):
                    return True

            elif rule_type == "keyword_spam":
                keywords = rule.get("keywords", [])
                for kw in keywords:
                    if kw in combined_text:
                        return True

            # repeat_detector: 단일 post 레벨로 적용 불가 (user-history 상태 필요)
            # Sprint 4 에서 user-history-based repeat detection 구현 예정

        return False

    def _filter_manipulation(
        self, posts: list[CommunityPost]
    ) -> tuple[list[CommunityPost], int]:
        """Stage 2: manipulation_rules.yaml rules 기반. (passed, flagged_count) 반환."""
        passed = []
        flagged = 0
        for post in posts:
            if self._is_manipulation(post):
                flagged += 1
            else:
                passed.append(post)
        return passed, flagged

    def _is_manipulation(self, post: CommunityPost) -> bool:
        """manipulation_rules.yaml 각 rule 체크.

        yaml 구조: rules 리스트. name / condition / action 필드.
        """
        combined_text = f"{post.title} {post.content}"

        for rule in self._manipulation_rules:
            name = rule.get("name", "")

            if name == "repeat_pump":
                # "같은 종목 + 극단 긍정 + 5분 내 10건+"
                # 단일 post 레벨에서는 극단 긍정 키워드로 1차 탐지
                extreme_positive = ["폭등", "대박", "상한가", "10배", "5배"]
                if any(kw in combined_text for kw in extreme_positive):
                    return True

            elif name == "coordinated_timing":
                # "3개+ 계정이 1분 내 동시 게시"
                # 단일 post 레벨로 적용 불가. Sprint 4 에서 구현.
                pass

            elif name == "extreme_sentiment":
                # "sentiment > 0.95 AND spike_ratio > 5.0"
                # 단일 post 레벨: 복합 긍정 표현 감지
                extreme_words = ["반드시 오른다", "무조건 상승", "100% 오른다", "확정 급등"]
                if any(kw in combined_text for kw in extreme_words):
                    return True

        return False

    # ------------------------------------------------------------------ #
    # 내부 helper: 감성 점수
    # ------------------------------------------------------------------ #

    def _compute_sentiment(self, posts: list[CommunityPost]) -> float:
        """Stage 3: sentiment_dict.yaml 단어 기반 감성 점수.

        yaml 구조:
            positive:
              strong: [...], medium: [...], weak: [...]
            negative:
              strong: [...], medium: [...], weak: [...]
            weights:
              strong: 1.0, medium: 0.5, weak: 0.2

        Returns:
            -1.0 (강한 부정) ~ +1.0 (강한 긍정). post 수 0이면 0.0.
        """
        if not posts:
            return 0.0

        positive = self._sentiment_dict.get("positive", {})
        negative = self._sentiment_dict.get("negative", {})
        weights = self._sentiment_weights

        # strong/medium/weak 단어 목록 추출
        pos_strong = positive.get("strong", [])
        pos_medium = positive.get("medium", [])
        pos_weak = positive.get("weak", [])
        neg_strong = negative.get("strong", [])
        neg_medium = negative.get("medium", [])
        neg_weak = negative.get("weak", [])

        # self._sentiment_weights 에서 직접 참조 (__init__ 에서 검증됨)
        w_strong = float(weights["strong"])
        w_medium = float(weights["medium"])
        w_weak = float(weights["weak"])

        total_score = 0.0
        for post in posts:
            combined_text = f"{post.title} {post.content}"

            pos_score = (
                sum(w_strong for w in pos_strong if w in combined_text)
                + sum(w_medium for w in pos_medium if w in combined_text)
                + sum(w_weak for w in pos_weak if w in combined_text)
            )
            neg_score = (
                sum(w_strong for w in neg_strong if w in combined_text)
                + sum(w_medium for w in neg_medium if w in combined_text)
                + sum(w_weak for w in neg_weak if w in combined_text)
            )

            total = pos_score + neg_score
            if total > 0:
                post_score = (pos_score - neg_score) / total  # -1.0 ~ +1.0
            else:
                post_score = 0.0
            total_score += post_score

        return total_score / len(posts)

    # ------------------------------------------------------------------ #
    # Mock
    # ------------------------------------------------------------------ #

    def _mock_poll(
        self, tickers: list[str], window_minutes: int
    ) -> list[CommunityPost]:
        """Mock 응답: ticker 당 3개 가상 post. Sprint 2 테스트/개발용."""
        now = datetime.now(_KST)
        posts: list[CommunityPost] = []

        sample_titles = [
            "실적 기대주 분석",
            "오늘 장 하락 원인",
            "매수 타이밍 공유",
        ]
        sample_contents = [
            "실적 발표 후 긍정적 반응. 상승 모멘텀 지속 전망. 기대 매수.",
            "외국인 매도 영향. 단기 조정 가능성. 하락 우려.",
            "지지선 확인 후 매수 진입 검토. 반등 기대.",
        ]

        for ticker in tickers:
            for i in range(3):
                posts.append(
                    CommunityPost(
                        post_id=f"MOCK-{ticker}-{i + 1}",
                        ticker=ticker,
                        author_id=f"user_hash_{i + 1}",
                        title=sample_titles[i],
                        content=sample_contents[i],
                        timestamp=now - timedelta(minutes=i),
                        url=f"https://mock.community.example.com/{ticker}/{i + 1}",
                        view_count=100 + i * 50,
                        comment_count=5 + i * 2,
                    )
                )

        return posts
