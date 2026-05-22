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

## 2026-05-26 Market-Open Plan

Scope: KIS virtual/paper only. Do not enable live trading. Do not mutate the
production registry. `2026-05-25` is a KRX holiday in `risk_config.yaml`, so the
next market-open check is `2026-05-26 09:00 KST`.

Candidate:

```text
BUNDLE-20260521-POSTCLOSE
```

Pre-open, around `08:30 KST`, refresh the current trading day's Dual-Source
artifact through the deploy-quality archive path. This is required because the
deployed candidate expects `news_score_t` during serving. Missing current-day
required features must block before broker reads or orders.

```bash
# In an operator-approved shell, inject API credentials without printing them.
# Do not commit credential-loading commands or secret values.
export PYTHONPATH=/Users/jangjaewon/Desktop/Elephant_Lab/new
/opt/anaconda3/envs/elephant/bin/python new/scripts/build_dart_corp_code_cache.py
/opt/anaconda3/envs/elephant/bin/python new/scripts/build_news_dart_archive.py \
  --end-date 20260526 \
  --business-days 1 \
  --naver-max-pages 10
/opt/anaconda3/envs/elephant/bin/python new/scripts/materialize_dual_source_history.py \
  --end-date 20260526 \
  --business-days 1 \
  --raw-events-dir artifacts/raw/dual_source
```

Pre-open pass criteria:

- `artifacts/cache/dart_corp_code_kospi20.json` may keep the historical filename,
  but it must be freshly rebuilt from the current 30 active universe and show
  `matched=30`, `total=30`, `missing=[]`.
- `artifacts/raw/dual_source/20260526.json` exists and has
  `provenance.deploy_quality=true`.
- The raw archive provenance has `ticker_count=30`.
- The `materialize_dual_source_history` report for `20260526` is `PASS`.
- `artifacts/dual_source/20260526.json` exists.
- It has one `scores[]` row for each active ticker.
- Required model feature `news_score_t` exists for every requested paper ticker.
- `source_stats.input_mode` is either `real` or `archived_raw_events`.
  The deploy-quality archive path writes `archived_raw_events`; this is valid
  when the raw archive provenance is real and `neutral_rehearsal_file=false`.
- Community may be `unavailable_empty` if real scraping is disabled, but mock
  community content must not be mixed in.

At `09:00 KST`, collect KIS virtual evidence first. This command performs the
paper-only balance, probe order, order-history requery, and one-cycle rehearsal
bundle. It must remain virtual/paper only.

```bash
# In an operator-approved shell, inject KIS paper credentials without printing them.
# Do not commit credential-loading commands or secret values.
export PYTHONPATH=/Users/jangjaewon/Desktop/Elephant_Lab/new
/opt/anaconda3/envs/elephant/bin/python new/scripts/collect_kis_paper_evidence.py \
  --bundle-id BUNDLE-20260521-POSTCLOSE \
  --registry-dir artifacts/lgbm_paper_candidate/BUNDLE-20260521-POSTCLOSE \
  --tickers 005930,000660,042700,403870,058470 \
  --ticker 005930 \
  --side buy \
  --qty 1 \
  --auto-price \
  --cycles 1 \
  --interval-sec 0 \
  --assume-empty-system-positions \
  --probe-confirm-phrase PAPER_ORDER_OK \
  --auto-confirm-phrase PAPER_AUTO_OK
```

Use `--assume-empty-system-positions` only when the latest paper balance shows a
flat system account. If the operator manually changes paper holdings before the
open, refresh the system position snapshot first and do not reuse stale `/tmp`
files from prior trading days.

After evidence PASS, rerun read-only status gates:

```bash
export PYTHONPATH=/Users/jangjaewon/Desktop/Elephant_Lab/new
/opt/anaconda3/envs/elephant/bin/python new/scripts/service_readiness_status.py \
  --bundle-id BUNDLE-20260521-POSTCLOSE
/opt/anaconda3/envs/elephant/bin/python new/scripts/prelive_gate.py \
  --bundle-id BUNDLE-20260521-POSTCLOSE \
  --end-date 20260521 \
  --business-days 253 \
  --max-tickers 30
```

If the gates PASS, start a guarded paper-auto run. For the first market-open
verification after the zero-score fix, prefer a 60-cycle run before considering
longer windows.

```bash
# In an operator-approved shell, inject KIS paper credentials without printing them.
# Do not commit credential-loading commands or secret values.
export PYTHONPATH=/Users/jangjaewon/Desktop/Elephant_Lab/new
/opt/anaconda3/envs/elephant/bin/python new/scripts/paper_auto_trade.py \
  --bundle-id BUNDLE-20260521-POSTCLOSE \
  --registry-dir artifacts/lgbm_paper_candidate/BUNDLE-20260521-POSTCLOSE \
  --tickers 005930,000660,042700,403870,058470 \
  --cycles 60 \
  --interval-sec 60 \
  --end-date 20260521 \
  --business-days 253 \
  --confirm-phrase PAPER_AUTO_OK
```

Only add `--cold-risk-report <path>` when a fresh `20260526` cold-risk report
has been generated and inspected. Do not reuse stale reports from prior trading
days for the market-open proof run.

Stop conditions:

- `serving_feature_readiness.status != PASS`
- `quant_output.mode=blocked`
- active model produces zero scores after warmup
- KIS rejects for non-transient broker/account/risk reasons
- consecutive read-only KIS errors exceed the configured skip budget
- any report shows `live_trading_allowed=true` or `registry_mutated=true`

Expected report families:

- `artifacts/reports/paper_trading/*.json`
- `artifacts/reports/paper_auto_trading/*.json`
- `artifacts/reports/service_readiness/*.json`
- `artifacts/reports/prelive_gate/*.json`

## 2026-05-15 Evidence Snapshot

- `paper_trading_balance_reconciliation_20260515_134605.json`: PASS.
- `paper_trading_submit_probe_order_20260515_134857.json`: PASS, order-history
  matched count 1.
- `paper_auto_service_rehearsal_20260515_135618.json`: PASS, external KIS
  virtual, paper auto cycle PASS.
- `service_readiness_BUNDLE-20260512-0AEEE37A_20260515_135651.json`: PASS,
  `deploy_quality=PASS`, `broker_evidence=PASS`, `registry_mutated=false`,
  `live_trading_allowed=false`.
