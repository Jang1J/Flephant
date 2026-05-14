"""S4-4 HotPathProfiler. Hot Path 단계별 레이턴시 측정 + SLA 경보.

책임:
  - 매 tick Hot Path의 각 단계(quant/ppo/pm/risk_fast/fda/hot_loop) ms 기록
  - p50/p95/p99/max 산출 (OpsMonitor.latency_percentiles와 동일 인터페이스)
  - SLA 위반 감지 → artifacts/ops_alerts.jsonl 기록
  - JSON 리포트 저장: artifacts/profiling/hotpath_YYYYMMDD.json

SLA 임계값: risk_config.yaml hot_path.sla (p50/p95/p99_ms).
SSOT 정책: hot_path.sla.p95_ms 는 quant_agent.latency_p95_target_ms 와 같은 값.
          system_os_metrics 주석(L821-L823) 에 중복 선언 금지 명시.
          hot_path.sla 블록은 단계별 세분화 임계값 용도이며 전체 SLA SSOT는 quant_agent 섹션.

불변 원칙:
  - Hot Path 동기 LLM 호출 금지 (불변 원칙 4). profiler 내 LLM 미호출.
  - 하드코딩 금지 (불변 원칙 5). SLA 임계값 risk_config.yaml 경유.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger

logger = get_logger("profiler")

_ARTIFACTS_BASE = Path(__file__).resolve().parents[3] / "artifacts"
_DEFAULT_ALERTS_PATH = _ARTIFACTS_BASE / "ops_alerts.jsonl"
_DEFAULT_PROFILING_DIR = _ARTIFACTS_BASE / "profiling"

# Hot Path 측정 대상 단계 (고정 목록)
HOT_STAGES: tuple[str, ...] = ("quant", "ppo", "pm", "risk_fast", "fda", "hot_loop")


def _load_sla_config() -> dict[str, float]:
    """risk_config.yaml hot_path.sla 로드.

    hot_path.sla 키가 없으면 quant_agent.latency_p95_target_ms fallback.
    하드코딩 금지 원칙 준수: yaml 없이 작동하더라도 값은 yaml 경유.
    """
    try:
        sla_cfg = config_load("risk_config.yaml", "hot_path") or {}
        sla = sla_cfg.get("sla", {})
        p95_ms = float(sla.get("p95_ms", 100.0))
        p50_ms = float(sla.get("p50_ms", 50.0))
        p99_ms = float(sla.get("p99_ms", 150.0))
        return {"p50_ms": p50_ms, "p95_ms": p95_ms, "p99_ms": p99_ms}
    except (KeyError, TypeError):
        pass

    # fallback: quant_agent.latency_p95_target_ms
    try:
        qa_cfg = config_load("risk_config.yaml", "quant_agent") or {}
        p95_ms = float(qa_cfg.get("latency_p95_target_ms", 100.0))
        return {"p50_ms": 50.0, "p95_ms": p95_ms, "p99_ms": 150.0}
    except (KeyError, TypeError):
        logger.warning("[profiler] SLA config 로드 실패. default 100ms 사용")
        return {"p50_ms": 50.0, "p95_ms": 100.0, "p99_ms": 150.0}


class HotPathProfiler:
    """Hot Path 단계별 레이턴시 프로파일러.

    사용 패턴 (HotRunner.run_once 내부):
        profiler = HotPathProfiler()
        t0 = profiler.start_stage("quant")
        ... quant 연산 ...
        profiler.end_stage("quant", t0)
        ...
        profiler.end_stage("hot_loop", t_loop_start)
        violations = profiler.check_sla()

    percentiles():
        각 stage 별 p50/p95/p99/max/n 반환.

    write_report():
        JSON 리포트를 artifacts/profiling/hotpath_YYYYMMDD.json 에 저장.
    """

    def __init__(
        self,
        window_size: int | None = None,
        alerts_path: Path | None = None,
        profiling_dir: Path | None = None,
    ) -> None:
        # 윈도우 크기는 quant_agent.latency_window 경유 (하드코딩 금지)
        if window_size is None:
            try:
                qa_cfg = config_load("risk_config.yaml", "quant_agent") or {}
                window_size = int(qa_cfg.get("latency_window", 1000))
            except (KeyError, TypeError):
                window_size = 1000

        self._window_size = int(window_size)
        self._sla = _load_sla_config()
        self._alerts_path = alerts_path or _DEFAULT_ALERTS_PATH
        self._profiling_dir = profiling_dir or _DEFAULT_PROFILING_DIR

        # stage → deque[float] (ms)
        self._records: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=self._window_size)
        )

        # tick별 메타: ts, ticker 수 기록 (리포트용)
        self._tick_meta: deque[dict[str, Any]] = deque(maxlen=self._window_size)

        logger.info(
            "[profiler] 초기화: window=%d, sla p50/p95/p99=%.0f/%.0f/%.0f ms",
            self._window_size,
            self._sla["p50_ms"],
            self._sla["p95_ms"],
            self._sla["p99_ms"],
        )

    # ================================================================== #
    # 타이머 API
    # ================================================================== #

    def start_stage(self, stage: str) -> float:
        """단계 시작. perf_counter() 반환. end_stage()에 전달."""
        return time.perf_counter()

    def end_stage(
        self,
        stage: str,
        t0: float,
        ticker: str | None = None,
        ts: str | None = None,
    ) -> float:
        """단계 종료. 경과 ms 기록 후 반환."""
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self._records[stage].append(elapsed_ms)
        return elapsed_ms

    def record(
        self,
        stage: str,
        duration_ms: float,
        ticker: str | None = None,
        ts: str | None = None,
    ) -> None:
        """직접 ms 값 기록 (외부에서 측정 완료된 값 주입용)."""
        self._records[stage].append(float(duration_ms))

    def record_tick(
        self,
        n_tickers: int,
        ts: str | None = None,
        stage_ms: dict[str, float] | None = None,
    ) -> None:
        """tick 단위 메타 기록 (리포트 트레이스용)."""
        self._tick_meta.append({
            "ts": ts or datetime.now(tz=timezone.utc).isoformat(),
            "n_tickers": n_tickers,
            "stage_ms": stage_ms or {},
        })

    # ================================================================== #
    # Percentile 산출
    # ================================================================== #

    def percentiles(self, stage: str | None = None) -> dict[str, Any]:
        """단계별 p50/p95/p99/max/n 반환.

        stage=None → 모든 측정 단계 dict 반환.
        stage 지정 → 해당 단계만.
        """
        if stage is not None:
            return self._stage_percentiles(stage)

        result: dict[str, Any] = {}
        stages = set(self._records.keys())
        for s in stages:
            result[s] = self._stage_percentiles(s)
        return result

    def _stage_percentiles(self, stage: str) -> dict[str, float]:
        records = list(self._records.get(stage, []))
        if not records:
            return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "n": 0}
        arr = np.asarray(records, dtype=float)
        return {
            "p50": float(np.percentile(arr, 50)),
            "p95": float(np.percentile(arr, 95)),
            "p99": float(np.percentile(arr, 99)),
            "max": float(arr.max()),
            "n": int(arr.size),
        }

    # ================================================================== #
    # SLA 검증
    # ================================================================== #

    def check_sla(self, min_samples: int = 5) -> list[dict[str, Any]]:
        """SLA 위반 목록 반환. 위반 시 ops_alerts.jsonl 에 기록.

        min_samples 미만이면 검증 skip (통계 불안정).
        """
        violations: list[dict[str, Any]] = []

        for stage in HOT_STAGES:
            stats = self._stage_percentiles(stage)
            n = stats["n"]
            if n < min_samples:
                continue

            stage_violations: list[dict[str, Any]] = []
            for pct_key, sla_key in [
                ("p50", "p50_ms"),
                ("p95", "p95_ms"),
                ("p99", "p99_ms"),
            ]:
                actual = stats[pct_key]
                threshold = self._sla[sla_key]
                if actual > threshold:
                    stage_violations.append({
                        "percentile": pct_key,
                        "actual_ms": round(actual, 3),
                        "sla_ms": threshold,
                        "excess_ms": round(actual - threshold, 3),
                    })

            if stage_violations:
                entry = {
                    "stage": stage,
                    "n": n,
                    "violations": stage_violations,
                }
                violations.append(entry)

        if violations:
            self._write_alert(violations)

        return violations

    def _write_alert(self, violations: list[dict[str, Any]]) -> None:
        """ops_alerts.jsonl 에 SLA 위반 append."""
        try:
            self._alerts_path.parent.mkdir(parents=True, exist_ok=True)
            alert = {
                "ts": datetime.now(tz=timezone.utc).isoformat(),
                "type": "hot_path_sla_violation",
                "violations": violations,
            }
            with self._alerts_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(alert, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("[profiler] ops_alerts.jsonl 기록 실패: %s", e)

    # ================================================================== #
    # JSON 리포트
    # ================================================================== #

    def write_report(self, output: Path | None = None) -> Path:
        """artifacts/profiling/hotpath_YYYYMMDD.json 저장.

        output 지정 시 해당 경로 사용.
        반환: 실제 저장된 Path.
        """
        if output is None:
            date_str = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
            output = self._profiling_dir / f"hotpath_{date_str}.json"

        output.parent.mkdir(parents=True, exist_ok=True)

        report = {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "sla_thresholds": self._sla,
            "window_size": self._window_size,
            "stages": self.percentiles(),
            "sla_violations": self.check_sla(),
            "tick_count": len(self._tick_meta),
        }

        with output.open("w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        logger.info("[profiler] 리포트 저장: %s", output)
        return output

    # ================================================================== #
    # 편의 메서드
    # ================================================================== #

    def reset(self) -> None:
        """측정 기록 초기화. 장 시작 시 호출."""
        self._records.clear()
        self._tick_meta.clear()
        logger.info("[profiler] 측정 기록 초기화")

    def summary_text(self) -> str:
        """로그용 요약 문자열."""
        lines = ["[profiler] Hot Path 성능 요약:"]
        stages_data = self.percentiles()
        for stage in HOT_STAGES:
            if stage not in stages_data or stages_data[stage]["n"] == 0:
                continue
            s = stages_data[stage]
            lines.append(
                f"  {stage}: p50={s['p50']:.2f}ms / p95={s['p95']:.2f}ms / "
                f"p99={s['p99']:.2f}ms (n={s['n']})"
            )
        return "\n".join(lines)
