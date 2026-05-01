"""TextPack 빌더. TSFresh 기반 통계 피처를 자연어 요약으로 변환.

news_filter.yaml의 text_pack_templates / text_pack_settings 섹션 경유.
data-engineer 병렬 작업 완료 전에도 동작: 섹션 없으면 빈 dict로 graceful fallback.
"""
from __future__ import annotations

import logging
import math
from typing import Any

from src.data import filter_loader

logger = logging.getLogger(__name__)

# missing template 경고 중복 억제: key당 1회만
_warned_missing_templates: set[str] = set()


class TextPackBuilder:
    """TSFresh 기반 통계 피처 → 자연어 텍스트 변환기.

    Cold Path NewsAgent/DebateAgent에 전달할 컨텍스트 생성.
    통계 피처(mean, std, autocorr 등)를 LLM이 이해할 수 있는 문장으로 변환.
    """

    def __init__(self) -> None:
        data = filter_loader.load_news_filter()
        # data-engineer 병렬 작업 완료 전: 섹션 없으면 빈 dict
        self._templates: dict[str, str] = data.get("text_pack_templates", {})
        settings: dict[str, Any] = data.get("text_pack_settings", {})
        self._prefix_template: str = settings.get(
            "prefix_template", "{ticker_name} 30분 요약: "
        )
        self._max_features: int = settings.get("max_features_per_pack", 8)
        self._separator: str = settings.get("separator", ", ")
        self._missing_policy: str = settings.get("missing_feature_policy", "skip")
        # yaml에 정의된 fallback_ticker_name 포맷 로드 (Task 1)
        self._fallback_template: str = settings.get(
            "fallback_ticker_name", "종목코드 {ticker}"
        )
        # ticker_keywords 캐시: build() 내 중복 호출 방지
        self._ticker_keywords: dict = data.get("ticker_keywords", {})

    def build(self, ticker: str, features: dict) -> str:
        """피처 딕셔너리 → 텍스트 요약 문자열 반환.

        Args:
            ticker: 종목코드 (int 또는 str, 6자리 zfill 자동 적용)
            features: {feature_name: value, ...}

        Returns:
            "종목명 30분 요약: 문장1, 문장2, ..."
        """
        # 종목코드 6자리 정규화
        zfilled = str(ticker).zfill(6)

        # 종목명 조회: _ticker_keywords 캐시 사용 (중복 yaml 로드 방지)
        kw_list = self._ticker_keywords.get(zfilled, [])
        if kw_list:
            ticker_name = kw_list[0]
        else:
            # yaml fallback_ticker_name 포맷 사용 (Task 1)
            ticker_name = self._fallback_template.format(ticker=zfilled)

        prefix = self._prefix_template.format(ticker_name=ticker_name)

        if not features:
            return prefix.rstrip()

        # 첫 max_features개만 처리
        feature_items = list(features.items())[: self._max_features]

        sentences: list[str] = []
        for feature_name, feature_value in feature_items:
            # NaN / None / inf 가드: LLM에 무의미한 값 전달 방지 (Task 3)
            if feature_value is None or (
                isinstance(feature_value, float) and not math.isfinite(feature_value)
            ):
                continue

            template = self._templates.get(feature_name)
            if template is None:
                if self._missing_policy == "error":
                    # yaml policy=error: 즉시 예외 발생 (Task 2)
                    raise ValueError(
                        f"[text_pack] 템플릿 없음 (policy=error): {feature_name}"
                    )
                elif self._missing_policy == "log_warning":
                    # yaml policy=log_warning: key당 1회만 경고 후 skip (Task 2)
                    if feature_name not in _warned_missing_templates:
                        logger.warning(
                            "[text_pack] 템플릿 없음, log_warning: %s", feature_name
                        )
                        _warned_missing_templates.add(feature_name)
                    continue
                else:
                    # default: skip (yaml policy=skip 또는 미설정)
                    if feature_name not in _warned_missing_templates:
                        logger.warning(
                            "[text_pack] 템플릿 없음, skip: %s", feature_name
                        )
                        _warned_missing_templates.add(feature_name)
                    continue
            else:
                try:
                    sentence = template.format(value=feature_value)
                    sentences.append(sentence)
                except Exception as e:
                    logger.warning(
                        "[text_pack] 템플릿 포맷 실패 (feature=%s): %s",
                        feature_name,
                        e,
                    )

        if not sentences:
            return prefix.rstrip()

        return prefix + self._separator.join(sentences)
