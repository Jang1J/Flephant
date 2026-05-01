"""LightGBM 퀀트 시그널 추론 전용 모델. artifacts/lgbm/ 로드."""
from __future__ import annotations

from pathlib import Path


class LGBMQuant:
    """Hot Path LightGBM 추론 전용 클래스.

    학습 코드 미포함. artifacts/lgbm/model.pkl 로드.
    Hot Path: <100ms 목표. predict() 동기 호출.
    모델 파라미터/임계값 하드코딩 금지. config_loader.load() 경유.
    Sprint 1 구현 예정.
    """

    _ARTIFACTS_PATH = Path(__file__).resolve().parents[3] / "artifacts" / "lgbm"

    def load(self) -> None:
        """artifacts/lgbm/model.pkl 로드."""
        raise NotImplementedError("Sprint 1 구현 예정")

    def predict(self, features: dict) -> dict:
        """피처 딕셔너리 → 시그널 점수 딕셔너리 반환."""
        raise NotImplementedError("Sprint 1 구현 예정")
