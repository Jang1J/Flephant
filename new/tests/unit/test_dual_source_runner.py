"""DualSourceRunner 단위 테스트. Sprint 4 S4-1 R1-W1 추가 검증.

coverage:
  batch_window_yaml_loaded: 배치 창이 risk_config.yaml에서 로드되는지 확인
  batch_window_default_fallback: 섹션 없을 때 08:00~08:30 fallback
  is_in_batch_window: 시각 경계 조건
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from src.data.dual_source_runner import _is_in_batch_window, _load_batch_window

_KST = ZoneInfo("Asia/Seoul")


# =========================================================
# batch_window yaml 로드 검증
# =========================================================


def test_batch_window_yaml_loaded() -> None:
    """_load_batch_window()가 risk_config.yaml dual_source.score_build_window 값 반환."""
    sh, sm, eh, em = _load_batch_window()
    # risk_config.yaml 기준: start=08:00, end=08:30
    assert sh == 8
    assert sm == 0
    assert eh == 8
    assert em == 30


def test_batch_window_custom_yaml_override() -> None:
    """config_load가 커스텀 값을 반환할 때 그 값을 사용."""
    fake_cfg = {"score_build_window": {"start": "07:30", "end": "07:45"}}
    with patch("src.data.dual_source_runner.config_load", return_value=fake_cfg):
        sh, sm, eh, em = _load_batch_window()
    assert (sh, sm, eh, em) == (7, 30, 7, 45)


def test_batch_window_default_fallback_when_section_missing() -> None:
    """score_build_window 키 없을 때 기본값 08:00~08:30 fallback."""
    fake_cfg: dict = {}  # score_build_window 없음
    with patch("src.data.dual_source_runner.config_load", return_value=fake_cfg):
        sh, sm, eh, em = _load_batch_window()
    assert (sh, sm, eh, em) == (8, 0, 8, 30)


# =========================================================
# _is_in_batch_window 경계 조건
# =========================================================


def test_is_in_batch_window_at_start() -> None:
    """08:00 KST = 창 시작 경계 → True."""
    now = datetime.now(_KST).replace(hour=8, minute=0, second=0, microsecond=0)
    assert _is_in_batch_window(now) is True


def test_is_in_batch_window_at_end() -> None:
    """08:30 KST = 창 종료 경계 → True."""
    now = datetime.now(_KST).replace(hour=8, minute=30, second=0, microsecond=0)
    assert _is_in_batch_window(now) is True


def test_is_in_batch_window_before_start() -> None:
    """07:59 KST = 창 이전 → False."""
    now = datetime.now(_KST).replace(hour=7, minute=59, second=0, microsecond=0)
    assert _is_in_batch_window(now) is False


def test_is_in_batch_window_after_end() -> None:
    """08:31 KST = 창 이후 → False."""
    now = datetime.now(_KST).replace(hour=8, minute=31, second=0, microsecond=0)
    assert _is_in_batch_window(now) is False
