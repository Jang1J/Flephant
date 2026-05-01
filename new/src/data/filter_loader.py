"""뉴스/커뮤니티 필터 yaml 로드 단일 창구.

하드코딩 금지 원칙(불변 원칙 5): 모든 필터 규칙은 yaml 경유.
config_loader.py의 캐시 패턴을 직접 구현 (다른 config 파일 4종 전용).
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# module-level 캐시: 프로세스 수명 동안 유지
_CACHE: dict[str, dict] = {}

# new/src/data/filter_loader.py → parents[2] = new/
_CONFIG_ROOT = Path(__file__).resolve().parents[2] / "config"


def _load_yaml(filename: str) -> dict:
    """yaml 파일 로드 + 캐시. 재호출 시 캐시에서 반환."""
    if filename in _CACHE:
        return _CACHE[filename]

    config_path = _CONFIG_ROOT / filename
    try:
        with config_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except FileNotFoundError as e:
        raise FileNotFoundError(
            f"[filter_loader] 설정 파일 없음: {config_path}. 경로 확인 필요."
        ) from e

    _CACHE[filename] = data
    logger.debug("[filter_loader] 로드 완료: %s", filename)
    return data


def load_news_filter() -> dict:
    """new/config/news_filter.yaml 로드.

    ticker_keywords, sector_keywords, market_keywords, filter_rules 포함.
    """
    return _load_yaml("news_filter.yaml")


def load_spam_rules() -> dict:
    """new/config/spam_rules.yaml 로드.

    filters 리스트 포함.
    """
    return _load_yaml("spam_rules.yaml")


def load_manipulation_rules() -> dict:
    """new/config/manipulation_rules.yaml 로드.

    rules 리스트 포함.
    """
    return _load_yaml("manipulation_rules.yaml")


def load_sentiment_dict() -> dict:
    """new/config/sentiment_dict.yaml 로드.

    positive/negative/weights 포함.
    """
    return _load_yaml("sentiment_dict.yaml")


def invalidate_cache(filename: str | None = None) -> None:
    """캐시 무효화. filename=None 이면 전체 초기화."""
    if filename is None:
        _CACHE.clear()
        logger.debug("[filter_loader] 전체 캐시 초기화")
    else:
        _CACHE.pop(filename, None)
        logger.debug("[filter_loader] 캐시 제거: %s", filename)
