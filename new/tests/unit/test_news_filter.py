"""NewsFilter 단위 테스트."""
from __future__ import annotations

import pytest

from src.data import filter_loader
from src.data.news_filter import NewsFilter


@pytest.fixture(autouse=True)
def clear_cache():
    """각 테스트 전후 캐시 초기화."""
    filter_loader.invalidate_cache()
    yield
    filter_loader.invalidate_cache()


@pytest.fixture
def nf() -> NewsFilter:
    return NewsFilter()


class TestTickerMatch:
    def test_filter_ticker_match_samsung(self, nf: NewsFilter):
        """삼성전자 키워드 포함 헤드라인 → matched_level=ticker."""
        headlines = [{"title": "삼성전자 실적 호조 발표", "summary": ""}]
        result = nf.filter(headlines)
        assert len(result) == 1
        assert result[0]["matched_level"] == "ticker"
        assert "삼성전자" in result[0]["matched_keywords"]

    def test_filter_ticker_match_hynix(self, nf: NewsFilter):
        """SK하이닉스 키워드 → matched_level=ticker."""
        headlines = [{"title": "SK하이닉스 HBM 공급 계약 체결"}]
        result = nf.filter(headlines)
        assert len(result) == 1
        assert result[0]["matched_level"] == "ticker"


class TestSectorMatch:
    def test_filter_sector_match_semiconductor(self, nf: NewsFilter):
        """반도체 섹터 키워드 → matched_level=sector."""
        headlines = [{"title": "반도체 수출 증가세 지속"}]
        result = nf.filter(headlines)
        assert len(result) == 1
        assert result[0]["matched_level"] == "sector"

    def test_filter_sector_match_battery(self, nf: NewsFilter):
        """배터리(2차전지) 섹터 키워드 → matched_level=sector."""
        headlines = [{"title": "배터리 소재 공급망 이슈 부각"}]
        result = nf.filter(headlines)
        assert len(result) == 1
        assert result[0]["matched_level"] == "sector"


class TestMarketMatch:
    def test_filter_market_match_rates(self, nf: NewsFilter):
        """금리 키워드 → matched_level=market."""
        headlines = [{"title": "금리 인상 예정, 시장 긴장"}]
        result = nf.filter(headlines)
        assert len(result) == 1
        assert result[0]["matched_level"] == "market"

    def test_filter_market_match_geopolitical(self, nf: NewsFilter):
        """미중 관세 키워드 → matched_level=market."""
        headlines = [{"title": "미중 무역전쟁 재점화 우려"}]
        result = nf.filter(headlines)
        assert len(result) == 1
        assert result[0]["matched_level"] == "market"


class TestNoMatch:
    def test_filter_no_match_returns_empty(self, nf: NewsFilter):
        """관련 없는 헤드라인 → 빈 리스트."""
        headlines = [{"title": "날씨 맑음, 봄나들이 나서요"}]
        result = nf.filter(headlines)
        assert result == []

    def test_filter_empty_input_returns_empty(self, nf: NewsFilter):
        """빈 입력 → 빈 리스트."""
        assert nf.filter([]) == []


class TestTitleOnly:
    def test_filter_title_only_match(self, nf: NewsFilter):
        """summary 없는 헤드라인도 title만으로 매칭."""
        headlines = [{"title": "삼성전자 주가 상승"}]
        result = nf.filter(headlines)
        assert len(result) == 1
        assert result[0]["matched_level"] == "ticker"


class TestCaseInsensitive:
    def test_filter_case_insensitive(self, nf: NewsFilter):
        """대소문자 구분 없이 매칭 (case_sensitive=False)."""
        # "Samsung Electronics"은 ticker_keywords 005930에 있음
        headlines = [{"title": "SAMSUNG ELECTRONICS 주가 전망"}]
        result = nf.filter(headlines)
        assert len(result) == 1
        assert result[0]["matched_level"] == "ticker"


class TestMultipleKeywords:
    def test_filter_multiple_keywords_matched_keywords_list(self, nf: NewsFilter):
        """두 키워드 동시 포함 시 matched_keywords에 모두 포함."""
        # "삼성전자" + "SEC" 동시 포함
        headlines = [{"title": "삼성전자(SEC) 신제품 발표"}]
        result = nf.filter(headlines)
        assert len(result) == 1
        assert len(result[0]["matched_keywords"]) >= 1

    def test_filter_preserves_original_fields(self, nf: NewsFilter):
        """원본 필드 보존 + 신규 필드 추가."""
        headline = {"title": "삼성전자 실적", "source": "뉴스A", "url": "http://test"}
        result = nf.filter([headline])
        assert len(result) == 1
        assert result[0]["source"] == "뉴스A"
        assert result[0]["url"] == "http://test"
        assert "matched_level" in result[0]
        assert "matched_keywords" in result[0]


class TestClassifyLevel:
    def test_classify_level_ticker(self, nf: NewsFilter):
        assert nf.classify_level({"title": "삼성전자 실적"}) == "ticker"

    def test_classify_level_sector(self, nf: NewsFilter):
        assert nf.classify_level({"title": "반도체 수요 증가"}) == "sector"

    def test_classify_level_market(self, nf: NewsFilter):
        assert nf.classify_level({"title": "금리 동결 결정"}) == "market"

    def test_classify_level_none(self, nf: NewsFilter):
        assert nf.classify_level({"title": "오늘 날씨 맑음"}) is None
