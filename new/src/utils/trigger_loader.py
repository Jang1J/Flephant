"""trigger_catalog / risk_config 공통 로더. DRY 추출 (S5-2).

risk_fast.py 와 AdmissionEngine 양쪽이 같은 trigger_catalog 로딩 코드를
사용하도록 단일 창구로 분리.

하드코딩 금지 원칙(불변 원칙 5): 모든 임계값은 risk_config.yaml 경유.
"""
from __future__ import annotations

from typing import Any

from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger

logger = get_logger("trigger_loader")


def load_trigger_rules(filter_action: str | None = None) -> list[dict[str, Any]]:
    """risk_config.yaml trigger_catalog.rules 로드.

    Args:
        filter_action: None 이면 전체 반환.
                       "review_risk", "admit_candidate" 등 지정 시
                       해당 action 만 필터링해서 반환.

    Returns:
        rule dict 리스트. 로드 실패 시 빈 리스트.

    Examples:
        # 전체 rules 로드
        rules = load_trigger_rules()

        # admit_candidate action 만 로드
        admit_rules = load_trigger_rules(filter_action="admit_candidate")
    """
    try:
        catalog = config_load("risk_config.yaml", "trigger_catalog")
        rules: list[dict[str, Any]] = catalog.get("rules", [])
    except Exception as e:
        logger.warning("[trigger_loader] trigger_catalog 로드 실패: %s", e)
        return []

    if filter_action is None:
        logger.debug(
            "[trigger_loader] trigger_catalog 전체 로드: rules=%d",
            len(rules),
        )
        return rules

    filtered = [r for r in rules if r.get("action") == filter_action]
    logger.debug(
        "[trigger_loader] trigger_catalog 필터: action=%s matched=%d / total=%d",
        filter_action,
        len(filtered),
        len(rules),
    )
    return filtered


def load_thresholds(section: str, keys: list[str]) -> dict[str, float]:
    """risk_config.yaml {section} 에서 numeric threshold 일괄 로드.

    Args:
        section: dot notation 섹션 경로.
                 예: 'risk_fast.cold_path', 'dynamic_universe'
        keys: 로드할 키 목록. 하나라도 누락 시 KeyError raise.

    Returns:
        {key: float} 딕셔너리.

    Raises:
        KeyError: section 또는 keys 가 yaml 에 없을 때.
                  시스템 설정 오류 즉시 노출 (불변 원칙 5).
    """
    try:
        section_data: Any = config_load("risk_config.yaml", section)
    except Exception as e:
        raise KeyError(
            f"[trigger_loader] risk_config.yaml '{section}' 섹션 로드 실패: {e}"
        ) from e

    if not isinstance(section_data, dict):
        raise KeyError(
            f"[trigger_loader] risk_config.yaml '{section}' 이 dict 가 아님 "
            f"(type={type(section_data).__name__}). 설정 파일 점검 필요."
        )

    result: dict[str, float] = {}
    for key in keys:
        if key not in section_data:
            raise KeyError(
                f"[trigger_loader] risk_config.yaml {section}.{key} 누락. "
                "설정 파일 점검 필요."
            )
        result[key] = float(section_data[key])

    logger.debug(
        "[trigger_loader] threshold 로드 완료: section=%s keys=%s",
        section,
        list(result.keys()),
    )
    return result
