"""과거 1분봉 백필. KIS REST Mock 기반 (Sprint 0 S0-2 완료용).

Sprint 1-0 실구현: 기간 지정 1분봉 수집 + parquet 저장.
실 API 전환은 S1-8 virtual/real 모드에서 수행한다.

PIT-Safety: end_date <= 오늘 18:00 KST (불변 원칙 1).
결과 저장: artifacts/data/{ticker}/bars_1m_{yyyymmdd}.parquet
parquet 미지원 시 JSON Lines fallback (pyarrow 미설치 환경 대응).
"""
from __future__ import annotations

import json
import os
from importlib.util import find_spec
from datetime import date, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from src.connectors.kis_rest import KISRestClient
from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger
from src.utils.pit_guard import PITViolationError
from src.utils.ticker_utils import pad_ticker

logger = get_logger("backfill")

_KST = ZoneInfo("Asia/Seoul")

# artifacts 루트 (new/ 기준)
_ARTIFACTS_ROOT = Path(__file__).resolve().parents[3] / "artifacts" / "data"


def _has_pyarrow() -> bool:
    """parquet writer 사용 가능 여부."""
    return find_spec("pyarrow") is not None


def _temp_artifact_path(target_path: Path) -> Path:
    """같은 디렉토리 temp 경로. 같은 FS에서 replace되어 atomic 보장."""
    token = uuid4().hex
    return target_path.with_name(f".{target_path.stem}.{token}.tmp{target_path.suffix}")


def _atomic_replace(temp_path: Path, target_path: Path) -> None:
    """temp artifact를 final artifact로 atomic replace."""
    temp_path.replace(target_path)


def _alternate_bar_artifact_path(target_path: Path) -> Path | None:
    """동일 ticker/date의 alternate suffix 경로."""
    if target_path.suffix == ".parquet":
        return target_path.with_suffix(".jsonl")
    if target_path.suffix == ".jsonl":
        return target_path.with_suffix(".parquet")
    return None


def _cleanup_alternate_bar_artifact(target_path: Path) -> None:
    """새 artifact 저장 성공 후 stale alternate suffix 제거."""
    alternate_path = _alternate_bar_artifact_path(target_path)
    if alternate_path is None or not alternate_path.exists():
        return
    try:
        alternate_path.unlink()
        logger.info("[backfill] stale alternate artifact 제거: %s", alternate_path)
    except Exception as e:
        logger.warning(
            "[backfill] stale alternate artifact 제거 실패: %s (%s)",
            alternate_path,
            e,
        )


def _load_market_hours() -> tuple[time, time]:
    """risk_config.yaml market_hours (open, close) 로드. HH:MM:SS → time 객체.

    불변 원칙 5: 장 시간은 yaml 경유 (하드코딩 금지).
    """
    cfg = config_load("risk_config.yaml", "market_hours")
    open_str = str(cfg["open"])
    close_str = str(cfg["close"])
    oh, om, os_ = [int(x) for x in open_str.split(":")]
    ch, cm, cs = [int(x) for x in close_str.split(":")]
    return time(oh, om, os_), time(ch, cm, cs)


class BackfillError(Exception):
    """백필 수집 실패."""


# PITViolationError SSOT: src.utils.pit_guard. 직접 import 사용.


def _parse_date(date_str: str) -> date:
    """YYYYMMDD 문자열 → date 객체."""
    return datetime.strptime(date_str, "%Y%m%d").date()


def _pit_check_date(date_str: str, label: str) -> None:
    """날짜가 오늘 18:00 KST snapshot 기준을 초과하면 PITViolationError.

    snapshot_hour는 risk_config.yaml pit_safety.snapshot_hour 로드.
    """
    cfg = config_load("risk_config.yaml", "pit_safety")
    snapshot_hour = int(cfg["snapshot_hour"])
    today = datetime.now(_KST).date()
    snapshot_dt = datetime.combine(today, time(snapshot_hour, 0, 0), tzinfo=_KST)

    target = _parse_date(date_str)
    # target이 오늘보다 미래이면 무조건 위반
    if target > today:
        raise PITViolationError(
            f"PIT-Safety 위반: {label}={date_str}이 snapshot({snapshot_dt.date().isoformat()})보다 미래"
        )
    # target이 오늘이고 현재 시각이 snapshot 기준 전이면 위반
    now_kst = datetime.now(_KST)
    if target == today and now_kst < snapshot_dt:
        raise PITViolationError(
            f"PIT-Safety 위반: {label}={date_str}(오늘)이고 현재 시각({now_kst.isoformat()})이 "
            f"snapshot({snapshot_dt.isoformat()}) 이전"
        )


def _is_market_hours(ts_str: str) -> bool:
    """ts_close ISO 8601이 장중 시간(risk_config.yaml market_hours) 이면 True."""
    try:
        market_open, market_close = _load_market_hours()
    except Exception as e:
        logger.warning(
            "[backfill] market_hours yaml 로드 실패, fallback time(9,0,0)/time(15,30,0) 사용: %s", e
        )
        market_open = time(9, 0, 0)
        market_close = time(15, 30, 0)
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_KST)
        t = dt.astimezone(_KST).time()
        return market_open <= t <= market_close
    except Exception as e:
        logger.debug("[backfill] ts_close 파싱 실패: %s", e)
        return False


def _is_target_trading_bar(bar: dict, date_str: str) -> bool:
    """bar가 요청한 날짜의 장중 1분봉이면 True.

    KIS 과거 분봉 endpoint는 일부 종목/anchor에서 전일 bar를 함께 반환할 수 있다.
    요청일과 다른 bar를 저장하면 DatasetBuilder가 잘못된 날짜 파일을 학습에 섞으므로,
    실 API 응답도 mock 재매핑과 동일하게 요청일 기준으로 강제 필터링한다.
    """
    ts_raw = str(bar.get("ts_close", ""))
    if not _is_market_hours(ts_raw):
        return False
    try:
        dt = datetime.fromisoformat(ts_raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_KST)
        return dt.astimezone(_KST).strftime("%Y%m%d") == date_str
    except Exception as e:
        logger.debug("[backfill] target date 파싱 실패: %s", e)
        return False


class Backfill:
    """과거 1분봉 데이터 수집. S1-0 baseline 학습 전제.

    KIS REST Mock 모드 기본 (실 API는 S1-8 virtual 전환 후).
    결과: artifacts/data/{ticker}/bars_1m_{yyyymmdd}.parquet
    parquet 미지원 환경에서는 .jsonl fallback 자동 전환.
    """

    def __init__(
        self,
        kis_client: KISRestClient | None = None,
        output_dir: Path | None = None,
    ) -> None:
        self._client = kis_client or KISRestClient()
        self._output_dir = output_dir or _ARTIFACTS_ROOT

    def fetch_1m_bars(
        self,
        ticker: str,
        start_date: str,
        end_date: str,
    ) -> list[dict]:
        """기간 1분봉 조회. PIT-Safety 검증. 장중 시간(09:00~15:30)만 필터.

        Mock 모드: inquire_minute_bar로 날짜별 fake OHLCV 생성.
        실 모드는 S1-8에서 구현. 현재는 MockError를 BackfillError로 래핑.

        Returns: list of {ticker, ts_close, open, high, low, close, volume, ...}
        Raises: PITViolationError, BackfillError
        """
        ticker = pad_ticker(ticker)

        # PIT-Safety 검증
        _pit_check_date(end_date, "end_date")

        start = _parse_date(start_date)
        end = _parse_date(end_date)
        if start > end:
            raise BackfillError(f"start_date({start_date}) > end_date({end_date})")

        all_bars: list[dict] = []
        current = start

        while current <= end:
            date_str = current.strftime("%Y%m%d")
            try:
                bars = self._fetch_single_day(ticker, date_str)
                all_bars.extend(bars)
                logger.info(
                    "%s %s: %d 분봉 수집", ticker, date_str, len(bars)
                )
            except Exception as e:
                logger.warning("%s %s 수집 실패: %s", ticker, date_str, e)
            current += timedelta(days=1)

        logger.info(
            "%s 전체 수집 완료: %d 분봉 (%s ~ %s)",
            ticker, len(all_bars), start_date, end_date,
        )
        return all_bars

    def _fetch_single_day(self, ticker: str, date_str: str) -> list[dict]:
        """단일 날짜 1분봉 조회. 실 KIS는 date 지정, mock은 날짜 재매핑."""
        try:
            # 하루 장중 최대 분봉 수. 실 KIS는 date를 받아 과거 일자 분봉을 조회한다.
            bars = self._client.inquire_minute_bar(ticker, n_bars=390, date=date_str)
        except Exception as e:
            raise BackfillError(f"KIS inquire_minute_bar 실패: {e}") from e

        if bars and bars[0].get("_mode") != "mock":
            filtered = [bar for bar in bars if _is_target_trading_bar(bar, date_str)]
            dropped = len(bars) - len(filtered)
            if dropped > 0:
                logger.info(
                    "[backfill] %s %s: 요청일 외/장외 bar %d개 제외",
                    ticker, date_str, dropped,
                )
            return filtered

        # ts_close를 날짜에 맞게 재매핑 (Mock 데이터는 현재 시각 기반이므로 date_str로 교체)
        target_date = _parse_date(date_str)
        adjusted: list[dict] = []
        for bar in bars:
            # 원본 ts에서 time 부분만 추출, date는 target_date로 교체
            try:
                orig_dt = datetime.fromisoformat(bar["ts_close"])
                if orig_dt.tzinfo is None:
                    orig_dt = orig_dt.replace(tzinfo=_KST)
                orig_time = orig_dt.astimezone(_KST).time()
                new_dt = datetime.combine(target_date, orig_time, tzinfo=_KST)
                new_bar = dict(bar)
                new_bar["ts_close"] = new_dt.isoformat()
            except Exception as e:
                logger.debug("[backfill] ts_close 재매핑 실패: %s", e)
                new_bar = dict(bar)

            # 장중 시간 필터
            if _is_market_hours(new_bar["ts_close"]):
                adjusted.append(new_bar)

        return adjusted

    def save_parquet(self, ticker: str, bars: list[dict], date: str) -> Path:
        """parquet 저장. artifacts/data/{ticker}/bars_1m_{date}.parquet.

        pyarrow 미설치 시 JSON Lines(.jsonl)로 fallback.
        Returns: 저장된 파일 경로
        """
        ticker = pad_ticker(ticker)
        save_dir = self._output_dir / ticker
        save_dir.mkdir(parents=True, exist_ok=True)

        try:
            import pandas as pd  # type: ignore[import]
            try:
                if not _has_pyarrow():
                    raise ImportError
                out_path = save_dir / f"bars_1m_{date}.parquet"
                temp_path = _temp_artifact_path(out_path)
                df = pd.DataFrame(bars)
                try:
                    df.to_parquet(temp_path, index=False)
                    _atomic_replace(temp_path, out_path)
                except Exception as e:
                    temp_path.unlink(missing_ok=True)
                    raise BackfillError(f"parquet 저장 실패: {out_path}: {e}") from e
                _cleanup_alternate_bar_artifact(out_path)
                logger.info("[backfill] parquet 저장: %s (%d rows)", out_path, len(bars))
                return out_path
            except ImportError:
                logger.warning("[backfill] pyarrow 미설치. JSON Lines fallback 사용")
                return self._save_jsonl(save_dir, ticker, bars, date)
        except ImportError:
            logger.warning("[backfill] pandas 미설치. JSON Lines fallback 사용")
            return self._save_jsonl(save_dir, ticker, bars, date)

    def _save_jsonl(
        self, save_dir: Path, ticker: str, bars: list[dict], date: str
    ) -> Path:
        """JSON Lines fallback 저장."""
        out_path = save_dir / f"bars_1m_{date}.jsonl"
        temp_path = _temp_artifact_path(out_path)
        try:
            with temp_path.open("w", encoding="utf-8") as fh:
                for bar in bars:
                    fh.write(json.dumps(bar, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            _atomic_replace(temp_path, out_path)
        except Exception as e:
            temp_path.unlink(missing_ok=True)
            raise BackfillError(f"JSONL 저장 실패: {out_path}: {e}") from e
        _cleanup_alternate_bar_artifact(out_path)
        logger.info("[backfill] JSONL 저장: %s (%d rows)", out_path, len(bars))
        return out_path

    def backfill_universe(
        self,
        tickers: list[str],
        start_date: str,
        end_date: str,
    ) -> dict[str, int]:
        """20 KOSPI 종목 일괄 backfill.

        Returns: {ticker: n_bars} (수집된 분봉 수)
        """
        result: dict[str, int] = {}
        for ticker in tickers:
            padded = pad_ticker(ticker)
            try:
                bars = self.fetch_1m_bars(ticker, start_date, end_date)
                result[padded] = len(bars)
                # 날짜별로 분리해서 저장
                bars_by_date: dict[str, list[dict]] = {}
                for bar in bars:
                    try:
                        dt = datetime.fromisoformat(bar["ts_close"])
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=_KST)
                        d_str = dt.astimezone(_KST).strftime("%Y%m%d")
                        bars_by_date.setdefault(d_str, []).append(bar)
                    except Exception as e:
                        logger.debug("[backfill] ts_close 날짜 파싱 실패: %s", e)
                for d_str, day_bars in bars_by_date.items():
                    self.save_parquet(padded, day_bars, d_str)
            except PITViolationError:
                raise
            except Exception as e:
                logger.error("[backfill] %s 일괄 수집 실패: %s", padded, e)
                result[padded] = 0

        logger.info(
            "[backfill] 유니버스 backfill 완료: %d 종목", len(result)
        )
        return result
