"""
Split Conformal Prediction for Stock Selection
- 분포 가정 없이 coverage guarantee를 제공하는 불확실성 정량화
- UQ baseline(Isotonic Regression)과 비교 가능한 대안 uncertainty layer
- GPT Pro 권고: UQ 재학습 후 비교기로 사용

Usage:
    from models.strategy_model.conformal import ConformalPredictor
    cp = ConformalPredictor(alpha=0.05)
    cp.calibrate(val_predictions, val_labels)
    lower, upper, q_hat = cp.predict_interval(new_predictions)
"""

import json
from pathlib import Path

import numpy as np

_BASE_DIR = Path(__file__).resolve().parent.parent.parent
OUTPUT_DIR = _BASE_DIR / "artifacts" / "strategy_model"


class ConformalPredictor:
    """
    Split Conformal Prediction.

    calibration set의 nonconformity score 분포를 사용하여
    새로운 예측에 대해 분포 가정 없는 prediction interval을 생성한다.

    alpha=0.05 → 95% coverage guarantee (이론적 보장).
    """

    def __init__(self, alpha: float = 0.05):
        self.alpha = alpha
        self.cal_scores = None
        self.q_hat = None

    def calibrate(self, cal_predictions: np.ndarray, cal_labels: np.ndarray) -> dict:
        """
        Calibration set으로 nonconformity score 분포를 계산한다.

        Args:
            cal_predictions: 모델 예측값 (확률 또는 score)
            cal_labels: 실제 레이블 (0/1 또는 연속값)

        Returns:
            메트릭 dict: {n_cal, q_hat, alpha, mean_score}
        """
        scores = np.abs(cal_predictions - cal_labels)
        self.cal_scores = np.sort(scores)

        n = len(self.cal_scores)
        q_level = np.ceil((1 - self.alpha) * (n + 1)) / n
        self.q_hat = float(np.quantile(self.cal_scores, min(q_level, 1.0)))

        print(
            f"[Conformal] calibrate 완료: n={n}, alpha={self.alpha}, "
            f"q_hat={self.q_hat:.4f}"
        )
        return {
            "n_cal": n,
            "q_hat": round(self.q_hat, 6),
            "alpha": self.alpha,
            "mean_score": round(float(scores.mean()), 6),
        }

    def predict_interval(
        self, new_predictions: np.ndarray
    ) -> tuple:
        """
        새 예측에 대해 prediction interval을 반환한다.

        Args:
            new_predictions: 새 예측값 배열

        Returns:
            (lower, upper, q_hat): 하한, 상한, quantile threshold
        """
        if self.q_hat is None:
            raise ValueError("[Conformal] calibrate()를 먼저 실행하세요.")

        lower = new_predictions - self.q_hat
        upper = new_predictions + self.q_hat
        return lower, upper, self.q_hat

    def evaluate_coverage(
        self, test_predictions: np.ndarray, test_labels: np.ndarray
    ) -> dict:
        """
        테스트 셋에서 실제 coverage rate와 interval width를 계산한다.

        Returns:
            {coverage_rate, mean_interval_width, target_coverage, q_hat}
        """
        lower, upper, q_hat = self.predict_interval(test_predictions)
        covered = (test_labels >= lower) & (test_labels <= upper)
        coverage_rate = float(covered.mean())
        interval_width = float((upper - lower).mean())

        print(
            f"[Conformal] 평가: coverage={coverage_rate:.3f} "
            f"(target={1-self.alpha:.3f}), width={interval_width:.4f}"
        )
        return {
            "coverage_rate": round(coverage_rate, 4),
            "mean_interval_width": round(interval_width, 6),
            "target_coverage": round(1 - self.alpha, 4),
            "q_hat": round(q_hat, 6),
        }

    def save(self, path: Path = None) -> Path:
        """Conformal predictor 상태 저장."""
        if path is None:
            path = OUTPUT_DIR / "conformal_predictor.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "alpha": self.alpha,
            "q_hat": self.q_hat,
            "n_cal": len(self.cal_scores) if self.cal_scores is not None else 0,
            "cal_scores_percentiles": {
                "p25": float(np.percentile(self.cal_scores, 25)) if self.cal_scores is not None else None,
                "p50": float(np.percentile(self.cal_scores, 50)) if self.cal_scores is not None else None,
                "p75": float(np.percentile(self.cal_scores, 75)) if self.cal_scores is not None else None,
                "p95": float(np.percentile(self.cal_scores, 95)) if self.cal_scores is not None else None,
            },
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[Conformal] 저장: {path}")
        return path

    @classmethod
    def load(cls, path: Path = None) -> "ConformalPredictor":
        """저장된 Conformal predictor 로드."""
        if path is None:
            path = OUTPUT_DIR / "conformal_predictor.json"
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cp = cls(alpha=data["alpha"])
        cp.q_hat = data.get("q_hat")
        return cp
