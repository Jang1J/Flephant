"""
US Market Raw Logging Connector
- S&P 500, NASDAQ, SOXX(반도체 ETF) 전일 OHLCV
- feature flag OFF 상태에서는 raw logging만 (파이프라인 미연결)
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = _BASE_DIR / "artifacts" / "us_market"


def fetch_us_market(target_date: str = None) -> dict:
    """미국 시장 전일 데이터 수집. feature_flags.yaml enable_osp=true일 때만 파이프라인 연결."""
    try:
        import yfinance as yf
    except ImportError:
        print("[USMarket] yfinance 미설치. pip install yfinance")
        return {"status": "unavailable", "reason": "yfinance not installed"}

    symbols = {
        "sp500": "^GSPC",
        "nasdaq": "^IXIC",
        "soxx": "SOXX",
        "vix": "^VIX",
    }

    if target_date:
        end = datetime.strptime(target_date, "%Y%m%d")
    else:
        end = datetime.now()
    start = end - timedelta(days=5)

    result = {
        "snapshot_date": target_date or end.strftime("%Y%m%d"),
        "collected_at": datetime.now().isoformat(),
        "us_close_available": False,
        "us_futures_available": False,
        "fx_available": False,
    }

    for name, symbol in symbols.items():
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                timeout=10,
            )
            if not hist.empty:
                last = hist.iloc[-1]
                ret_1d = 0.0
                if len(hist) >= 2:
                    prev_close = float(hist.iloc[-2]["Close"])
                    if prev_close != 0:
                        ret_1d = round(float((last["Close"] - prev_close) / prev_close), 4)
                result[name] = {
                    "close": round(float(last["Close"]), 2),
                    "open": round(float(last["Open"]), 2),
                    "high": round(float(last["High"]), 2),
                    "low": round(float(last["Low"]), 2),
                    "volume": int(last["Volume"]),
                    "date": str(hist.index[-1].date()),
                    "ret_1d": ret_1d,
                }
                result["us_close_available"] = True
            else:
                result[name] = None
        except Exception as e:
            print(f"[USMarket] {name} ({symbol}) 수집 실패: {e}")
            result[name] = None

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"USM-{result['snapshot_date']}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[USMarket] 저장: {out_path}")
    return result


if __name__ == "__main__":
    date = sys.argv[1] if len(sys.argv) > 1 else None
    data = fetch_us_market(date)
    print(json.dumps(data, indent=2, ensure_ascii=False))
