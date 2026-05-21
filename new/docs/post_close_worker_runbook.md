# Post-Close Worker Runbook

This worker is the server-side path for non-live nightly evolution. It keeps
`live_trading_allowed=false` and never mutates the production registry.

## What Runs

`new/scripts/post_close_data_update_scheduler.py` stays alive on the server.
After `post_close_data_update.scheduler.run_time_kst` in
`new/config/risk_config.yaml`, it runs `post_close_data_update.py` once for the
latest PIT-safe KOSPI trading day.

The update pipeline is:

1. `live_data_readiness.py --all --require-train`
2. `build_news_dart_archive.py`
3. `materialize_dual_source_history.py`
4. `materialize_exogenous_history.py`
5. `phase2_feature_backfill.py`
6. Optional `post_backfill_prelive.py`

## Server Setup

Create a server-only env file. Do not commit it.

```bash
sudo mkdir -p /etc/elephant-lab
sudo install -m 600 /dev/null /etc/elephant-lab/elephant.env
```

Add only environment variables needed by the real API connectors, for example:

```bash
ELEPHANT_MODE=mode_b
KIS_MODE=virtual
```

Install the systemd unit:

```bash
sudo cp new/deploy/systemd/elephant-post-close-worker.service \
  /etc/systemd/system/elephant-post-close-worker.service
sudo systemctl daemon-reload
sudo systemctl enable --now elephant-post-close-worker.service
```

Check status:

```bash
systemctl status elephant-post-close-worker.service
journalctl -u elephant-post-close-worker.service -f
```

## Job DB

The worker writes job state to:

```text
artifacts/db/post_close_jobs.sqlite3
```

Table:

```text
post_close_job_runs
```

Each row records target date, bundle id, status, blockers, report path,
`registry_mutated`, and `live_trading_allowed`.

## One-Off Server Check

```bash
ELEPHANT_MODE=mode_b \
PYTHONPATH=/opt/elephant/Elephant_Lab/new \
/opt/anaconda3/envs/elephant/bin/python \
new/scripts/post_close_data_update_scheduler.py --once --dry-run
```

Force a dry-run:

```bash
ELEPHANT_MODE=mode_b \
PYTHONPATH=/opt/elephant/Elephant_Lab/new \
/opt/anaconda3/envs/elephant/bin/python \
new/scripts/post_close_data_update_scheduler.py --once --force --dry-run
```

## Safety Contract

- No `.env` reads.
- No live account switching.
- No production registry mutation.
- No PASS if Mode B/PIT guards fail.
- PASS only means non-live update/checks completed. Human approval is still
  required before any production promotion.
