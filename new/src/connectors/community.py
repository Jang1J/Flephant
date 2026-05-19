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

## Mock/real 모드

네이버 종목토론방 직접 scraping은 공식 API가 아니므로 기본 경로가 아니다.
real 모드는 공식 Naver Search cafearticle/blog API 기반 live community proxy만 사용한다.
Sprint 2 는 **mock 기본**, `COMMUNITY_SCRAPE_ENABLED=1`일 때 real proxy 활성.

SSOT: api_contracts.md v3.5 C2 EventNormalizeContract (source='community')
"""
from __future__ import annotations

import hashlib
import html
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from src.connectors.base import BaseConnector
from src.data.filter_loader import load_spam_rules, load_manipulation_rules, load_sentiment_dict
from src.utils.auth import AuthManager
from src.utils.logger import get_logger
from src.utils.rate_limiter import RateLimiter
from src.data.event_normalizer import EventNormalizer

logger = get_logger("community")

_KST = ZoneInfo("Asia/Seoul")

# new/ 루트 기준 yaml 경로 (Path, 절대경로 독립)
_CONFIG_ROOT = Path(__file__).resolve().parents[2] / "config"
_NAVER_COMMUNITY_URLS = {
    "cafearticle": "https://openapi.naver.com/v1/search/cafearticle.json",
    "blog": "https://openapi.naver.com/v1/search/blog.json",
}
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Naver Search API HTML tag/entity 제거."""
    return html.unescape(_HTML_TAG_PATTERN.sub("", text or "")).strip()


def _stable_author_hash(*parts: str) -> str:
    """원문 작성자 노출 없이 안정적인 author_id 생성."""
    payload = "|".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _parse_blog_postdate(postdate: str) -> datetime:
    """Naver blog postdate(YYYYMMDD) → KST datetime.

    cafearticle API는 작성 시각을 제공하지 않으므로 이 함수는 blog 전용이다.
    """
    try:
        return datetime.strptime(postdate, "%Y%m%d").replace(
            tzinfo=_KST, hour=12, minute=0, second=0
        )
    except (TypeError, ValueError):
        return datetime.now(_KST)


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
    provider: str = "mock"
    query: str = ""
    timestamp_quality: str = "mock_generated"
    timestamp_confidence: str = "mock"
    collected_at: datetime | None = None


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
        auth: AuthManager | None = None,
        rate_limiter: RateLimiter | None = None,
        normalizer: EventNormalizer | None = None,
    ) -> None:
        super().__init__()  # BaseConnector: timeout_sec / max_retries / backoff_base 로드
        self._timeout_sec = self.timeout_sec
        self._max_retries = self.max_retries
        self._backoff_base = self.backoff_base
        self._auth = auth or AuthManager()

        # rate limiter: risk_config.yaml rate_limits.community 경유
        if rate_limiter is not None:
            self._rate_limiter = rate_limiter
        else:
            self._rate_limiter = RateLimiter("community")

        # spam_rules / manipulation_rules / sentiment_dict yaml 로드 (filter_loader 캐시 경유)
        spam_data = load_spam_rules()
        self._spam_rules = spam_data.get("filters", [])
        manip_data = load_manipulation_rules()
        self._manipulation_rules = manip_data.get("rules", [])
        self._sentiment_dict = load_sentiment_dict()
        # yaml SSOT 엄수. weights 섹션 누락 시 KeyError 전파 (불변 원칙 5).
        if "weights" not in self._sentiment_dict:
            raise KeyError(
                "sentiment_dict.yaml: 'weights' section required (strong/medium/weak)"
            )
        self._sentiment_weights = self._sentiment_dict["weights"]

        # dual_source 설정 (dual_source.yaml 경유 — filter_loader 미지원 파일이므로 직접 로드)
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
        self._client_id: str | None = None
        self._client_secret: str | None = None
        self._ticker_name_map = self._load_ticker_name_map()
        self._ticker_sector_map = self._load_ticker_sector_map()
        if not self._is_mock:
            self._client_id, self._client_secret = self._auth.get_naver_client()

        self._normalizer = normalizer or EventNormalizer()

        if self._is_mock:
            logger.info("[community] Sprint 2 mock 모드 (COMMUNITY_SCRAPE_ENABLED 미설정)")
        else:
            provider = self._community_cfg.get("real_provider", "naver_search")
            logger.info("[community] real 모드 활성: provider=%s", provider)

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
            ConnectionError/TimeoutError: 공식 Naver Search API 호출 실패.
        """
        # PIT-Safety: Sprint 4 실 스크래핑 시 timestamp guard 추가 예정
        # 현재 mock 전용 → 미래 데이터 생성 없음
        normalized_tickers = [str(t).zfill(6) for t in tickers]

        # rate_limiter 획득 (mock 모드에서도 호출해서 테스트 가능하도록)
        self._rate_limiter.wait_and_acquire()

        if self._is_mock:
            return self._mock_poll(normalized_tickers, window_minutes)

        return self._naver_search_poll(normalized_tickers, window_minutes)

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
        self._last_raw_post_count = len(posts)
        self._last_normalize_fail_count = 0
        self._last_normalize_pit_fail_count = 0
        events: list[dict[str, Any]] = []
        for post in posts:
            link_hash = (
                hashlib.sha256(post.url.encode("utf-8")).hexdigest()[:16]
                if post.url
                else ""
            )
            raw = {
                "post_title": f"community_proxy_event:{post.ticker}:{post.provider}",
                "posted_at": post.timestamp.isoformat(),  # _normalize_community 필수 필드
                "body": "",
                "ticker_mentioned": post.ticker,
                "spam_flag": False,
                # 추가 메타데이터 payload에 포함. 원문 title/body/link는 durable event로 저장하지 않는다.
                "post_id": post.post_id,
                "author_id_hash": post.author_id,
                "source_link_hash": link_hash,
                "view_count": post.view_count,
                "comment_count": post.comment_count,
                "provider": post.provider,
                "query": post.query,
                "timestamp_quality": post.timestamp_quality,
                "timestamp_confidence": post.timestamp_confidence,
                "content_retention_policy": "derived_signal_only",
                "stores_raw_content": False,
                "collected_at": (
                    post.collected_at.isoformat()
                    if isinstance(post.collected_at, datetime)
                    else None
                ),
            }
            try:
                event = self._normalizer.normalize(raw, source="community")
                events.append(event)
            except Exception as e:
                self._last_normalize_fail_count += 1
                if "PIT" in str(e) or "snapshot" in str(e):
                    self._last_normalize_pit_fail_count += 1
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

    @staticmethod
    def _load_ticker_name_map() -> dict[str, str]:
        """universe_config.yaml에서 ticker → 종목명 매핑 로드."""
        path = _CONFIG_ROOT / "universe_config.yaml"
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except Exception as e:
            logger.warning("[community] universe_config 로드 실패: %s", e)
            return {}

        mapping: dict[str, str] = {}
        for sector in (data.get("sectors") or {}).values():
            for stock in sector.get("stocks", []) or []:
                ticker = str(stock.get("ticker", "")).zfill(6)
                name = str(stock.get("name", "")).strip()
                if ticker and name:
                    mapping[ticker] = name
        return mapping

    @staticmethod
    def _load_ticker_sector_map() -> dict[str, str]:
        """universe_config.yaml에서 ticker → sector 매핑 로드."""
        path = _CONFIG_ROOT / "universe_config.yaml"
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except Exception as e:
            logger.warning("[community] universe_config sector 로드 실패: %s", e)
            return {}

        mapping: dict[str, str] = {}
        for sector_name, sector in (data.get("sectors") or {}).items():
            for stock in sector.get("stocks", []) or []:
                ticker = str(stock.get("ticker", "")).zfill(6)
                if ticker:
                    mapping[ticker] = str(sector_name)
        return mapping

    # ------------------------------------------------------------------ #
    # 내부 helper: 공식 Naver Search API 기반 community 대체 소스
    # ------------------------------------------------------------------ #

    def _naver_search_poll(
        self,
        tickers: list[str],
        window_minutes: int,
    ) -> list[CommunityPost]:
        """Naver Search cafearticle/blog API로 사용자 생성 글을 수집.

        비공식 종토방 scraping 대신 Naver 공식 Search API를 사용한다.
        cafearticle는 작성 시각을 제공하지 않으므로 ingest 시각을 timestamp로 쓰고,
        blog는 postdate(YYYYMMDD)를 사용한다.
        """
        if not self._client_id or not self._client_secret:
            raise EnvironmentError("NAVER_CLIENT_ID/SECRET required for community real mode")

        provider_names = self._community_cfg.get(
            "naver_search_providers", ["cafearticle", "blog"]
        )
        providers = [
            str(provider)
            for provider in provider_names
            if str(provider) in _NAVER_COMMUNITY_URLS
        ]
        if not providers:
            raise ValueError("dual_source.community.naver_search_providers is empty")

        max_posts = int(self._community_cfg.get("max_posts_per_ticker", 3))
        suffixes_raw = self._community_cfg.get("naver_query_suffixes")
        if suffixes_raw is None:
            suffixes = [str(self._community_cfg.get("naver_query_suffix", "주식")).strip()]
        elif isinstance(suffixes_raw, list):
            suffixes = [str(item).strip() for item in suffixes_raw if str(item).strip()]
        else:
            suffixes = [str(suffixes_raw).strip()]
        if not suffixes:
            suffixes = ["주식"]
        include_ticker_queries = bool(
            self._community_cfg.get("include_ticker_queries", False)
        )
        ticker_suffixes_raw = self._community_cfg.get(
            "naver_ticker_query_suffixes", ["주식"]
        )
        if isinstance(ticker_suffixes_raw, list):
            ticker_suffixes = [
                str(item).strip()
                for item in ticker_suffixes_raw
                if str(item).strip()
            ]
        else:
            ticker_suffixes = [str(ticker_suffixes_raw).strip()]
        if not ticker_suffixes:
            ticker_suffixes = ["주식"]
        include_sector_queries = bool(
            self._community_cfg.get("include_sector_queries", False)
        )
        sector_query_suffix = str(
            self._community_cfg.get("sector_query_suffix", "관련주")
        ).strip() or "관련주"
        max_queries = int(self._community_cfg.get("max_queries_per_ticker", len(suffixes)))
        display = int(self._community_cfg.get("display_per_query", max_posts))
        display = max(1, min(100, display))
        sort = str(self._community_cfg.get("sort", "date"))
        if sort not in {"sim", "date"}:
            sort = "date"

        headers = {
            "X-Naver-Client-Id": self._client_id,
            "X-Naver-Client-Secret": self._client_secret,
        }
        posts: list[CommunityPost] = []

        for ticker in tickers:
            query_name = self._ticker_name_map.get(ticker, ticker)
            queries: list[str] = []
            queries.append(f"{query_name} {suffixes[0]}".strip())
            if include_ticker_queries:
                for suffix in ticker_suffixes:
                    queries.append(f"{ticker} {suffix}".strip())
            if include_sector_queries:
                sector_name = self._ticker_sector_map.get(ticker, "")
                if sector_name:
                    queries.append(f"{sector_name} {sector_query_suffix}".strip())
            for suffix in suffixes[1:]:
                queries.append(f"{query_name} {suffix}".strip())
            queries = list(dict.fromkeys(queries))[:max_queries]
            ticker_posts: list[CommunityPost] = []
            seen_keys: set[str] = set()

            for query in queries:
                if len(ticker_posts) >= max_posts:
                    break
                for provider in providers:
                    if len(ticker_posts) >= max_posts:
                        break
                    payload = self._http_get_json(
                        _NAVER_COMMUNITY_URLS[provider],
                        params={
                            "query": query,
                            "display": str(display),
                            "start": "1",
                            "sort": sort,
                        },
                        headers=headers,
                    )
                    for idx, raw in enumerate(payload.get("items", []) or []):
                        if len(ticker_posts) >= max_posts:
                            break
                        dedupe_key = self._dedupe_key(provider, ticker, raw)
                        if dedupe_key in seen_keys:
                            continue
                        seen_keys.add(dedupe_key)
                        ticker_posts.append(
                            self._naver_item_to_post(provider, ticker, raw, idx, query)
                        )

            posts.extend(ticker_posts)

        return posts

    def _naver_item_to_post(
        self,
        provider: str,
        ticker: str,
        raw: dict[str, Any],
        idx: int,
        query: str = "",
    ) -> CommunityPost:
        """Naver Search item → CommunityPost."""
        title = _strip_html(str(raw.get("title", "")))
        content = _strip_html(str(raw.get("description", "")))
        link = str(raw.get("link", ""))
        collected_at = datetime.now(_KST)

        if provider == "blog":
            postdate = str(raw.get("postdate", ""))
            timestamp = _parse_blog_postdate(postdate)
            author_basis = str(raw.get("bloggername", ""))
            timestamp_quality = (
                "official_postdate_date_only"
                if len(postdate) == 8 and postdate.isdigit()
                else "missing_collected_at"
            )
            timestamp_confidence = "medium_date_only" if timestamp_quality == "official_postdate_date_only" else "low"
        else:
            timestamp = collected_at
            author_basis = str(raw.get("cafename", ""))
            timestamp_quality = "missing_collected_at"
            timestamp_confidence = "low"

        post_id = f"NAVER-{provider}-{ticker}-{_stable_author_hash(link, title)[:10]}-{idx}"
        return CommunityPost(
            post_id=post_id,
            ticker=ticker,
            author_id=_stable_author_hash(provider, author_basis, link),
            title=title,
            content=content,
            timestamp=timestamp,
            url=link,
            view_count=0,
            comment_count=0,
            provider=provider,
            query=query,
            timestamp_quality=timestamp_quality,
            timestamp_confidence=timestamp_confidence,
            collected_at=collected_at,
        )

    @staticmethod
    def _dedupe_key(provider: str, ticker: str, raw: dict[str, Any]) -> str:
        """provider+link+ticker 기준 중복 제거 키."""
        link = str(raw.get("link") or "").strip()
        if link:
            base = f"{provider}|{ticker}|{link}"
        else:
            title = _strip_html(str(raw.get("title", "")))
            desc = _strip_html(str(raw.get("description", "")))
            base = f"{provider}|{ticker}|{title}|{desc}"
        return hashlib.sha256(base.encode("utf-8")).hexdigest()

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
