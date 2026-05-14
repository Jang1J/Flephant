"""pytest 공통 설정. sys.path에 new/ 추가 + Mode B 격리 fixture + filter_loader cache isolation."""
from __future__ import annotations

import os
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


def pytest_configure(config):
    """custom marker 등록. PytestUnknownMarkWarning 제거."""
    config.addinivalue_line(
        "markers",
        "slow: 시간 오래 걸리는 테스트 (E2E 1주일 시나리오 등). CI: -m 'not slow' 로 제외.",
    )
    config.addinivalue_line(
        "markers",
        "no_mode_b: ELEPHANT_MODE 미설정 강제 (Mode A 거부 테스트용, opt-out 마커).",
    )
    config.addinivalue_line(
        "markers",
        "mode_b: ELEPHANT_MODE=mode_b 자동 설정 (Mode B 함수 호출 테스트용, opt-in 마커).",
    )
    # test 환경 가드 우회 환경변수 자동 설정.
    # 운영 환경에서는 이 파일이 로드되지 않으므로 가드 그대로 유지.
    os.environ["ELEPHANT_TEST_PIT_SKIP"] = "1"
    os.environ["ELEPHANT_TEST_FRESHNESS_SKIP"] = "1"


@pytest.fixture(autouse=True)
def _set_elephant_mode(monkeypatch, request):
    """SHIP-fix W-2 (GPT Pro 2026-05-09): autouse → opt-in 패턴.

    이전: 모든 테스트 default mode_b (Mode A 경계 테스트 회귀 위험).
    현재: mode_b 마커가 있는 테스트만 강제 mode_b. 마커 없으면 ELEPHANT_MODE 미설정 (Mode A 환경).

    backward compat:
        - unit/conftest.py 의 _set_mode_b_env (autouse) 가 unit 디렉토리에서만 mode_b 강제 (현행 유지)
        - integration/non-unit 테스트는 mode_b 마커 명시 시에만 mode_b
        - no_mode_b 마커: ELEPHANT_MODE 명시 제거 (강제 Mode A)
    """
    if request.node.get_closest_marker("no_mode_b"):
        monkeypatch.delenv("ELEPHANT_MODE", raising=False)
        return
    if request.node.get_closest_marker("mode_b"):
        monkeypatch.setenv("ELEPHANT_MODE", "mode_b")
        return
    # 마커 없으면 환경 그대로 유지 (Mode A 가드 정상 동작)
