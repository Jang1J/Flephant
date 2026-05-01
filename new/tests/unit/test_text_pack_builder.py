"""TextPackBuilder 단위 테스트.

data-engineer 병렬 작업(text_pack_templates 섹션 추가) 완료 전 상태 기준.
templates 없는 상태에서 graceful fallback + 기본 동작 검증.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from src.data import filter_loader
from src.data.text_pack_builder import TextPackBuilder, _warned_missing_templates


@pytest.fixture(autouse=True)
def clear_state():
    """각 테스트 전후 캐시 + 경고 set 초기화."""
    filter_loader.invalidate_cache()
    _warned_missing_templates.clear()
    yield
    filter_loader.invalidate_cache()
    _warned_missing_templates.clear()


def make_builder_with_templates(templates: dict, settings: dict | None = None) -> TextPackBuilder:
    """news_filter.yaml에 text_pack_templates/settings 주입한 builder 생성 헬퍼."""
    base_data = filter_loader.load_news_filter()
    patched = dict(base_data)
    patched["text_pack_templates"] = templates
    if settings:
        patched["text_pack_settings"] = settings

    with patch("src.data.filter_loader.load_news_filter", return_value=patched):
        return TextPackBuilder()


class TestBuildBasic:
    def test_build_basic_samsung(self):
        """005930 + close__mean 피처 → 삼성전자 prefix 포함."""
        builder = make_builder_with_templates(
            {"close__mean": "평균 종가 {value:.0f}원"},
        )
        result = builder.build("005930", {"close__mean": 85000.5})
        assert "삼성전자" in result
        assert "85001" in result or "85000" in result  # 반올림 허용

    def test_build_basic_prefix_format(self):
        """prefix는 '{ticker_name} 30분 요약: ' 기본 형식."""
        builder = make_builder_with_templates({})
        result = builder.build("005930", {})
        assert result.startswith("삼성전자")


class TestEdgeCases:
    def test_build_empty_features_returns_prefix_only(self):
        """피처 없으면 prefix만 반환."""
        builder = make_builder_with_templates({})
        result = builder.build("005930", {})
        assert "삼성전자" in result
        # prefix 뒤에 추가 문장 없음
        assert result == result.rstrip()

    def test_build_missing_template_skipped(self):
        """템플릿 없는 feature는 skip, 다른 feature는 정상 반영."""
        builder = make_builder_with_templates(
            {"close__mean": "평균 종가 {value:.0f}원"},
        )
        result = builder.build("005930", {"close__mean": 85000.0, "unknown_feature": 999})
        assert "평균 종가" in result
        # 알 수 없는 key는 결과에 없음
        assert "999" not in result

    def test_build_respects_max_features_per_pack(self):
        """max_features_per_pack 이상의 feature는 잘림."""
        templates = {f"feat_{i}": f"피처{i} {{value}}" for i in range(12)}
        settings = {"max_features_per_pack": 8, "separator": " | "}
        builder = make_builder_with_templates(templates, settings)
        features = {f"feat_{i}": i for i in range(12)}
        result = builder.build("005930", features)
        # 구분자 개수로 최대 7개 구분자 (8문장이면 7개)
        count = result.count(" | ")
        assert count <= 7


class TestTickerNormalization:
    def test_build_ticker_zfill_applied_str(self):
        """'5930' (4자리 str) → '005930'으로 zfill 후 삼성전자 조회."""
        builder = make_builder_with_templates({})
        result = builder.build("5930", {})
        assert "삼성전자" in result

    def test_build_ticker_zfill_applied_int_like(self):
        """5930 (숫자형 str) → zfill 후 정상 조회."""
        builder = make_builder_with_templates({})
        result = builder.build(5930, {})  # type: ignore[arg-type]
        assert "삼성전자" in result

    def test_build_unknown_ticker_falls_back_to_raw(self):
        """ticker_keywords에 없는 ticker → yaml fallback_ticker_name 포맷 사용."""
        settings = {"fallback_ticker_name": "종목코드 {ticker}"}
        builder = make_builder_with_templates({}, settings)
        result = builder.build("999999", {})
        assert "종목코드 999999" in result


class TestSeparatorAndSettings:
    def test_build_separator_applied(self):
        """separator 설정이 문장 사이에 적용됨."""
        templates = {"a": "A값 {value}", "b": "B값 {value}"}
        settings = {"separator": " | ", "max_features_per_pack": 8}
        builder = make_builder_with_templates(templates, settings)
        result = builder.build("005930", {"a": 1, "b": 2})
        assert " | " in result
        assert "A값 1" in result
        assert "B값 2" in result

    def test_build_custom_prefix_template(self):
        """사용자 정의 prefix_template 적용."""
        templates = {}
        settings = {"prefix_template": "[{ticker_name}] 분석: "}
        builder = make_builder_with_templates(templates, settings)
        result = builder.build("005930", {})
        assert result.startswith("[삼성전자]")


class TestMissingFeaturePolicy:
    def test_build_missing_feature_policy_error_raises_valueerror(self):
        """missing_feature_policy=error 시 미지정 feature → ValueError."""
        settings = {"missing_feature_policy": "error"}
        builder = make_builder_with_templates(
            {"close__mean": "평균 종가 {value:.0f}원"},
            settings,
        )
        with pytest.raises(ValueError, match="policy=error"):
            builder.build("005930", {"close__mean": 85000.0, "unknown_feat": 1})

    def test_build_missing_feature_policy_log_warning_continues(self):
        """missing_feature_policy=log_warning 시 미지정 feature → 예외 없이 다른 feature 반영."""
        settings = {"missing_feature_policy": "log_warning"}
        builder = make_builder_with_templates(
            {"close__mean": "평균 종가 {value:.0f}원"},
            settings,
        )
        result = builder.build("005930", {"close__mean": 85000.0, "unknown_feat": 1})
        assert "평균 종가" in result
        assert "unknown_feat" not in result


class TestNanNoneInfGuard:
    def test_build_skips_nan_none_inf_values(self):
        """NaN / None / inf feature_value는 결과 문자열에 포함되지 않음."""
        templates = {
            "close__mean": "평균 종가 {value:.0f}원",
            "volume__sum_values": "거래량 합계 {value}",
            "volume__maximum": "최대 거래량 {value}",
            "close__minimum": "최저 종가 {value}",
            "close__autocorrelation__lag_1": "1분 자기상관 {value:.2f}",
        }
        builder = make_builder_with_templates(templates)
        features = {
            "close__mean": 85000.5,
            "volume__sum_values": float("nan"),
            "volume__maximum": None,
            "close__minimum": float("inf"),
            "close__autocorrelation__lag_1": 0.85,
        }
        result = builder.build("005930", features)
        assert "nan" not in result
        assert "None" not in result
        assert "inf" not in result
        # valid 2개는 반영
        assert "85001" in result or "85000" in result
        assert "0.85" in result
