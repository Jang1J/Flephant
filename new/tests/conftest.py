"""pytest 공통 설정. sys.path에 new/ 추가 + Mode B 격리 fixture + filter_loader cache isolation."""
from __future__ import annotations

import pathlib
import sys

import pytest

# new/ 루트를 sys.path 최상단에 추가: from src.xxx import yyy 가능
_NEW_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_NEW_ROOT) not in sys.path:
    sys.path.insert(0, str(_NEW_ROOT))


@pytest.fixture(autouse=True)
def _add_src_to_path() -> None:  # type: ignore[return]
    """모든 테스트에 자동 적용. sys.path 보장."""
    # 실제 경로 추가는 모듈 레벨에서 이미 처리. fixture는 명시적 선언용.
    pass


@pytest.fixture(autouse=True, scope="module")
def _reset_filter_loader_cache():
    """filter_loader module-level _CACHE를 test module 시작 시 초기화.

    이유: yaml 값 변경 mock이나 invalidate_cache 호출 누락으로 인한 test 간 state leak 방지.
    Mode B Scheduler (S3-0)가 yaml 갱신 후 invalidate_cache() 호출하는 것과 동일한 의도.
    2026-04-23 추가 (reviewer Phase D+1 Info 해소).
    """
    try:
        from src.data.filter_loader import invalidate_cache
        invalidate_cache()
    except Exception:
        # filter_loader 없는 레거시 test module 보호
        pass
    yield
