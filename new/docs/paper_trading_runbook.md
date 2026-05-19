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
```

Inject the KIS paper app key, app secret, account number, and product code from
the operator terminal only. Do not write those values or env assignments into
repo files or release notes.

Check sanitized readiness:

```bash
PYTHONPATH=new /opt/anaconda3/envs/elephant/bin/python new/scripts/print_env_readiness.py
```

The output must show `status=PASS`; it prints presence and length only, never
secret values.

## Balance And Reconciliation

```bash
PYTHONPATH=new /opt/anaconda3/envs/elephant/bin/python new/scripts/paper_trading_smoke.py \
  --action balance \
  --assume-empty-system-positions
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
PYTHONPATH=new /opt/anaconda3/envs/elephant/bin/python new/scripts/paper_trading_smoke.py \
  --action submit-probe \
  --ticker 005930 \
  --side buy \
  --qty 1 \
  --auto-price \
  --order-type 00 \
  --confirm-phrase PAPER_ORDER_OK
```

Pass criteria:

- `mode_guard.status=PASS`
- `order_guard.status=PASS`
- broker response contains an accepted/submitted order identifier
- order history/fill lookup is recorded

KIS OAuth token issuance is rate-limited. If `EGW00133` appears, wait at least
75 seconds and rerun the same command. Do not change credentials.

## One-Cycle Paper Auto

Run this only after:

- C12 real backtest PASS for the candidate bundle
- C14 service-policy replay PASS
- KIS virtual balance/reconciliation/probe PASS

```bash
PYTHONPATH=new /opt/anaconda3/envs/elephant/bin/python new/scripts/paper_auto_service_rehearsal.py \
  --registry-dir artifacts/lgbm_paper \
  --tickers 005930 \
  --cycles 1 \
  --interval-sec 0 \
  --confirm-phrase PAPER_AUTO_OK
```

The final report must show hot decision, FDA approve/veto reason, paper
execution response, and reconciliation evidence while `live_enabled=false`.

## 2026-05-15 Evidence Snapshot

- `paper_trading_balance_reconciliation_20260515_134605.json`: PASS.
- `paper_trading_submit_probe_order_20260515_134857.json`: PASS, order-history
  matched count 1.
- `paper_auto_service_rehearsal_20260515_135618.json`: PASS, external KIS
  virtual, paper auto cycle PASS.
- `service_readiness_BUNDLE-20260512-0AEEE37A_20260515_135651.json`: PASS,
  `deploy_quality=PASS`, `broker_evidence=PASS`, `registry_mutated=false`,
  `live_trading_allowed=false`.
