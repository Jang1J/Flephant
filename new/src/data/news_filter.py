"""뉴스 헤드라인 키워드 필터.

ticker_keywords / sector_keywords / market_keywords 3단계 매칭.
LLM 호출 전 1차 prefilter. Hot Path 비개입 (Cold Path 진입 전 사용).
"""
from __future__ import annotations

import logging
from typing import Any

from src.data import filter_loader

logger = logging.getLogger(__name__)


class NewsFilter:
    """뉴스 헤드라인 키워드 매칭 3단계 필터.

    Level 1: ticker_keywords (6자리 종목코드 → 키워드)
    Level 2: sector_keywords (섹터명 → 키워드)
    Level 3: market_keywords (카테고리 → 키워드)
    """

    def __init__(self) -> None:
        data = filter_loader.load_news_filter()
        self._ticker_keywords: dict[str, list[str]] = data.get("ticker_keywords", {})
        self._sector_keywords: dict[str, list[str]] = data.get("sector_keywords", {})
        self._market_keywords: dict[str, list[str]] = data.get("market_keywords", {})
        rules = data.get("filter_rules", {})
        self._match_in: list[str] = rules.get("match_in", ["title", "summary"])
        self._match_any: bool = rules.get("match_any", True)
        self._case_sensitive: bool = rules.get("case_sensitive", False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def filter(self, headlines: list[dict]) -> list[dict]:
        """매칭된 headlines 반환. 미매칭은 drop.

        Args:
            headlines: [{"title": str, "summary": str (optional), ...}, ...]

        Returns:
            매칭된 항목 리스트. 각 항목에 matched_level, matched_keywords 필드 추가.
        """
        if not headlines:
            return []

        results: list[dict] = []
        for item in headlines:
            matched = self._match_headline(item)
            if matched is not None:
                enriched = dict(item)
                enriched["matched_level"] = matched["level"]
                enriched["matched_keywords"] = matched["keywords"]
                results.append(enriched)

        logger.debug(
            "[news_filter] 필터 완료: 입력 %d건 → 통과 %d건",
            len(headlines),
            len(results),
        )
        return results

    def classify_level(self, headline: dict) -> str | None:
        """헤드라인의 매칭 레벨 반환. 미매칭이면 None.

        Returns: "ticker" | "sector" | "market" | None
        """
        matched = self._match_headline(headline)
        if matched is None:
            return None
        return matched["level"]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_text(self, item: dict) -> str:
        """match_in 필드에서 검색 대상 텍스트 추출."""
        parts: list[str] = []
        for field in self._match_in:
            val = item.get(field)
            if val and isinstance(val, str):
                parts.append(val)
        combined = " ".join(parts)
        if not self._case_sensitive:
            return combined.lower()
        return combined

    def _normalize_keyword(self, keyword: str) -> str:
        if not self._case_sensitive:
            return keyword.lower()
        return keyword

    def _keywords_matched(self, text: str, keywords: list[str]) -> list[str]:
        """text 안에 포함된 keyword 리스트 반환."""
        return [kw for kw in keywords if self._normalize_keyword(kw) in text]

    def _match_headline(self, item: dict) -> dict[str, Any] | None:
        """매칭 결과 반환. 미매칭이면 None.

        Returns: {"level": str, "keywords": list[str]} or None
        """
        text = self._extract_text(item)
        if not text:
            return None

        # Level 1: ticker (종목코드 기준)
        # headline에 ticker 필드가 있으면 해당 종목만 검사, 없으면 전체 검사
        headline_ticker = item.get("ticker")
        if headline_ticker:
            zfilled = str(headline_ticker).zfill(6)
            kw_list = self._ticker_keywords.get(zfilled, [])
            matched = self._keywords_matched(text, kw_list)
            if matched:
                return {"level": "ticker", "keywords": matched}

        # ticker 필드 없는 경우: 전체 ticker_keywords 검사
        if not headline_ticker:
            for _code, kw_list in self._ticker_keywords.items():
                matched = self._keywords_matched(text, kw_list)
                if matched:
                    return {"level": "ticker", "keywords": matched}

        # Level 2: sector
        for _sector, kw_list in self._sector_keywords.items():
            matched = self._keywords_matched(text, kw_list)
            if matched:
                return {"level": "sector", "keywords": matched}

        # Level 3: market
        for _category, kw_list in self._market_keywords.items():
            matched = self._keywords_matched(text, kw_list)
            if matched:
                return {"level": "market", "keywords": matched}

        return None
