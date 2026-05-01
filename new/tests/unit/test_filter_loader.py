"""filter_loader.py 단위 테스트."""
from __future__ import annotations

import pytest

from src.data import filter_loader
from src.data.filter_loader import (
    load_manipulation_rules,
    load_news_filter,
    load_sentiment_dict,
    load_spam_rules,
)


@pytest.fixture(autouse=True)
def clear_cache():
    """각 테스트 전후 캐시 초기화."""
    filter_loader.invalidate_cache()
    yield
    filter_loader.invalidate_cache()


class TestLoadNewsFilter:
    def test_load_news_filter_returns_dict_with_ticker_keywords(self):
        data = load_news_filter()
        assert isinstance(data, dict)
        assert "ticker_keywords" in data
        assert isinstance(data["ticker_keywords"], dict)
        # 삼성전자 005930 키 확인
        assert "005930" in data["ticker_keywords"]
        assert "삼성전자" in data["ticker_keywords"]["005930"]

    def test_load_news_filter_has_sector_keywords(self):
        data = load_news_filter()
        assert "sector_keywords" in data
        assert isinstance(data["sector_keywords"], dict)
        assert len(data["sector_keywords"]) > 0

    def test_load_news_filter_has_market_keywords(self):
        data = load_news_filter()
        assert "market_keywords" in data
        assert isinstance(data["market_keywords"], dict)

    def test_load_news_filter_has_filter_rules(self):
        data = load_news_filter()
        assert "filter_rules" in data
        assert "match_in" in data["filter_rules"]
        assert "match_any" in data["filter_rules"]
        assert "case_sensitive" in data["filter_rules"]


class TestLoadSpamRules:
    def test_load_spam_rules_has_filters_list(self):
        data = load_spam_rules()
        assert isinstance(data, dict)
        assert "filters" in data
        assert isinstance(data["filters"], list)
        assert len(data["filters"]) > 0

    def test_load_spam_rules_filters_have_type(self):
        data = load_spam_rules()
        for f in data["filters"]:
            assert "type" in f


class TestLoadManipulationRules:
    def test_load_manipulation_rules_has_rules_list(self):
        data = load_manipulation_rules()
        assert isinstance(data, dict)
        assert "rules" in data
        assert isinstance(data["rules"], list)
        assert len(data["rules"]) > 0

    def test_load_manipulation_rules_entries_have_name(self):
        data = load_manipulation_rules()
        for rule in data["rules"]:
            assert "name" in rule


class TestLoadSentimentDict:
    def test_load_sentiment_dict_has_positive_negative(self):
        data = load_sentiment_dict()
        assert isinstance(data, dict)
        assert "positive" in data
        assert "negative" in data

    def test_load_sentiment_dict_has_weights(self):
        data = load_sentiment_dict()
        assert "weights" in data
        assert "strong" in data["weights"]
        assert "medium" in data["weights"]
        assert "weak" in data["weights"]

    def test_load_sentiment_dict_positive_has_tiers(self):
        data = load_sentiment_dict()
        positive = data["positive"]
        assert "strong" in positive
        assert isinstance(positive["strong"], list)


class TestCaching:
    def test_caching_returns_same_instance(self):
        """동일 호출 시 cache hit: 같은 dict 객체 반환."""
        data1 = load_news_filter()
        data2 = load_news_filter()
        assert data1 is data2

    def test_caching_spam_rules_same_instance(self):
        data1 = load_spam_rules()
        data2 = load_spam_rules()
        assert data1 is data2

    def test_caching_manipulation_rules_same_instance(self):
        data1 = load_manipulation_rules()
        data2 = load_manipulation_rules()
        assert data1 is data2

    def test_caching_sentiment_dict_same_instance(self):
        data1 = load_sentiment_dict()
        data2 = load_sentiment_dict()
        assert data1 is data2

    def test_invalidate_cache_forces_reload(self):
        """invalidate 후 재호출 시 새 객체 반환."""
        data1 = load_news_filter()
        filter_loader.invalidate_cache("news_filter.yaml")
        data2 = load_news_filter()
        # 내용은 같지만 객체는 다름
        assert data1 is not data2
        assert data1 == data2


class TestFileNotFound:
    def test_missing_file_raises_file_not_found(self):
        with pytest.raises(FileNotFoundError, match="설정 파일 없음"):
            filter_loader._load_yaml("nonexistent_file.yaml")
