# GPT Pro Handoff Prompt, Phase 2 Feature Remediation

You are reviewing the attached sanitized Elephant Lab v3 repository snapshot.
Treat the zip as the current canonical code state. Do not assume earlier sector
reports were implemented unless you can verify them in the files.

Important safety constraints:

- Do not recommend reading or packaging `.env`.
- Do not recommend manually editing `artifacts/lgbm/registry.json` active_version.
- Production deployment is allowed only after C12 real backtest verdict=pass,
  regression_risk.flagged=false, and minute_bar_leakage_check.verdict=pass,
  followed by the deployer path.
- `artifacts/lgbm_paper` may be used only for paper rehearsal, not live deploy.
- Live/real account transition must remain blocked.

Current verified state before this handoff:

- Full local test baseline recently passed: 1325 passed, 1 skipped.
- Internal active-model paper-auto rehearsal passed with fake KIS and real HotRunner.
- Production registry active_version remains null.
- Service-policy replay is BLOCKED because turnover/cost-adjusted economics are negative.
- C12 deploy-quality remains BLOCKED.
- Phase 1 backtest repair is present:
  - Top-K long-only replay aligned with trainer top_k_fraction.
  - trade_signal_threshold is deprecated and not loaded by BacktestEngine.
  - BacktestEngine regression_evidence now includes trade_count,
    cost_burn_pct_total, mean_net_return, Dual-Source/default coverage, and
    exogenous/default coverage.
- Phase 2 code now includes:
  - `new/src/data/exogenous_feature_store.py`
  - `new/scripts/phase2_feature_backfill.py`
  - `DatasetBuilder._join_exogenous_features()` daily artifact join path.
  - Feature-quality telemetry in C12 BacktestEngine output.

Current Phase 2 audit result:

- `phase2_feature_backfill.py --end-date 20260508 --business-days 80`
  produced BLOCKED.
- 80/80 common 1m artifact dates exist.
- Dual-Source historical file coverage: 0.0.
- Dual-Source non-neutral date coverage: 0.0.
- Exogenous historical file coverage: 0.0.
- Exogenous non-neutral date coverage: 0.0.

Please audit the attached snapshot and answer with a table:

1. Finding
2. Severity: Critical / High / Medium / Info
3. Confidence 1-10
4. Exact file and line reference
5. Whether it is already implemented, partial, missing, or stale
6. Minimal fix
7. Verification command

Focus specifically on:

- Whether Phase 1 Top-K long-only / threshold deprecation / regression_evidence
  are implemented correctly.
- Whether Phase 2 exogenous and Dual-Source historical feature coverage is now
  honestly represented in code and reports.
- Whether any code still silently treats neutral placeholders as deploy-quality
  evidence.
- What minimal next implementation is needed to produce real non-neutral
  historical Dual-Source / exogenous features without PIT leakage.
- Whether service-policy replay should become a hard C14 deploy gate before any
  production active_version change.
- Whether trade frequency, label horizon, or cost-aware retraining should be the
  next model remediation priority.

Do not give generic advice. Ground every claim in the attached files.
