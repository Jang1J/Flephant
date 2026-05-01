"""new/config/*.yaml 로드 단일 창구. 하드코딩 금지 원칙(불변 원칙 5) 집행."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# module-level 캐시: 프로세스 수명 동안 유지
_CACHE: dict[str, dict] = {}

_CONFIG_ROOT = Path(__file__).resolve().parents[2] / "config"


def _resolve_key(data: dict, dot_key: str) -> Any:
    """dot notation 키를 재귀 탐색. 예: 'dual_source.alpha' → data['dual_source']['alpha']."""
    parts = dot_key.split(".", 1)
    value = data[parts[0]]
    if len(parts) == 1:
        return value
    if not isinstance(value, dict):
        raise KeyError(f"중간 키 '{parts[0]}'이 dict가 아님 (dot key: {dot_key})")
    return _resolve_key(value, parts[1])


def load(config_file: str = "risk_config.yaml", key: str | None = None) -> Any:
    """new/config/{config_file} 에서 yaml safe_load. key 있으면 dot notation 하위만 반환.

    캐시 전략: 파일당 module-level dict. 프로세스 재시작 시 초기화.
    하드코딩 금지 원칙: 모든 수치는 이 함수를 경유할 것.
    """
    if config_file not in _CACHE:
        config_path = _CONFIG_ROOT / config_file
        with config_path.open("r", encoding="utf-8") as fh:
            _CACHE[config_file] = yaml.safe_load(fh) or {}

    data: dict = _CACHE[config_file]

    if key is None:
        return data
    return _resolve_key(data, key)


def reload(config_file: str = "risk_config.yaml") -> None:
    """캐시 무효화 후 재로드 강제. 런타임 설정 변경 시 사용."""
    _CACHE.pop(config_file, None)
