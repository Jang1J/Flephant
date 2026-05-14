# KIS Virtual Paper Trading Runbook

Purpose: prove the paper-trading path before any real-account switch. Real/live
trading stays blocked unless a separate operator gate changes the policy.

## Safety Rules

- Do not read or commit `.env`.
- Keep `KIS_MODE=virtual`.
- Keep `execution.live_enabled=false` in `new/config/risk_config.yaml`.
- Use limit orders only.
- Probe order quantity must stay within `paper_trading.max_probe_order_qty`.
- Do not edit `artifacts/lgbm/registry.json` manually. Active model promotion
  must go through C12 real backtest and C14 deploy.

## Environment

Set these in the user terminal that owns the KIS paper credentials:

```bash
export PYTHONPATH=new
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export KIS_MODE=virtual
export KIS_APP_KEY="$KIS_PAPER_APP_KEY"
export KIS_APP_SECRET="$KIS_PAPER_APP_SECRET"
export KIS_ACCOUNT_NUMBER="$KIS_PAPER_ACCOUNT_NUMBER"
export KIS_ACCOUNT_PRODUCT_CODE="$KIS_PAPER_ACCOUNT_PRODUCT_CODE"
```

Check sanitized readiness:

```bash
python new/scripts/print_env_readiness.py
```

The output must show `status=PASS`; it prints presence and length only, never
secret values.

## Balance And Reconciliation

```bash
python new/scripts/paper_trading_smoke.py --action balance
```

Expected report:

```text
artifacts/reports/paper_trading/paper_trading_balance_reconciliation_*.json
```

Pass criteria:

- `status=PASS`
- `mode_guard.status=PASS`
- `balance.status=PASS`
- `reconciliation.status=PASS`

## Probe Order

Run this only after balance/reconciliation PASS. Choose a conservative limit
price from the current KIS virtual quote path.

```bash
python new/scripts/paper_trading_smoke.py \
  --action submit-probe \
  --ticker 005930 \
  --side buy \
  --qty 1 \
  --price <LIMIT_PRICE> \
  --order-type 00 \
  --confirm-phrase PAPER_ORDER_OK
```

Pass criteria:

- `mode_guard.status=PASS`
- `order_guard.status=PASS`
- broker response contains an accepted/submitted order identifier
- order history/fill lookup is recorded

## One-Cycle Paper Auto

Run this only after:

- C12 real backtest PASS for the candidate bundle
- C14 deploy promotes the candidate to active
- prelive gate PASS
- KIS virtual balance/reconciliation/probe PASS

```bash
python new/scripts/paper_auto_trade.py \
  --tickers 005930 \
  --cycles 1 \
  --interval-sec 0 \
  --confirm-phrase PAPER_AUTO_OK \
  --end-date 20260508 \
  --business-days 80
```

The final report must show hot decision, FDA approve/veto reason, paper
execution response, and reconciliation evidence while `live_enabled=false`.
