"""unit 테스트 전용 conftest. Mode B 환경변수 자동 설정.

S3 Critical 6~7: @mode_b_only 데코레이터가 붙은 공개 API를 테스트할 때
ELEPHANT_MODE=mode_b 가 없으면 RuntimeError가 발생한다.
unit 테스트는 Mode B 컨텍스트에서 실행하므로 autouse fixture로 자동 주입.

Codex 권고 10 (2026-05-09): autouse 는 backward compat 위해 유지하되,
**Mode A 가드 검증 테스트는 @pytest.mark.no_mode_b 명시 필수**.
누락 시 mode_b 환경에서 가드가 가짜 통과할 위험. root conftest.py 는 opt-in
패턴 (mode_b 마커 시에만 강제). unit/conftest.py 는 unit 디렉토리 백워드 호환
유지하지만, Mode A 경계 검증 시 명시 마커 필수.
"""
from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "no_mode_b: ELEPHANT_MODE=mode_b autouse fixture를 비활성화 (Mode A 거부 테스트용 - 필수 명시).",
    )


@pytest.fixture(autouse=True)
def _set_mode_b_env(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """모든 unit 테스트에 ELEPHANT_MODE=mode_b 설정 (자동 적용).

    @mode_b_only 데코레이터 검증 통과용. 실제 장중 환경에서는 반드시 mode_b_only 거부.

    Mode A 거부 테스트 (mode_b_only 데코레이터 가드 검증)는 @pytest.mark.no_mode_b
    마커로 autouse 효과를 명시적으로 비활성화. 누락 시 가드가 mode_b 환경에서
    가짜 통과 → 실제 장중 환경에서 가드 미작동 위험.
    """
    if "no_mode_b" in request.keywords:
        # Codex 권고 10 (2026-05-09): 명시적 unset (이전: return 만, 부모 환경 leak 가능).
        monkeypatch.delenv("ELEPHANT_MODE", raising=False)
        return
    monkeypatch.setenv("ELEPHANT_MODE", "mode_b")
