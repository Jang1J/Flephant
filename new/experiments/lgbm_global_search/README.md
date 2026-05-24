# LGBM Global Search

개인 repo 실험용 bounded global search 하네스입니다. 목적은 2026-05-26 paper 운영 전후로 현재 후보 `BUNDLE-20260521-POSTCLOSE`를 건드리지 않고 더 좋은 research 후보가 있는지 확인하는 것입니다.

> 개인 repo 전용입니다. `new/experiments/lgbm_global_search/`는 팀 `ai-1` 브랜치에 merge/cherry-pick/push하지 않습니다.

## 안전 원칙

- production registry `artifacts/lgbm`를 쓰지 않습니다.
- paper registry/candidate registry를 쓰지 않습니다.
- KIS/API/.env를 호출하거나 읽지 않습니다.
- 결과는 `artifacts/lgbm_global_search/` 아래에 저장합니다.
- 후보가 좋아 보여도 C12 backtest + service-policy replay + prelive 검증 전에는 paper default로 승격하지 않습니다.
- service-policy sweep의 `top_k_fraction`, `decision_stride_bars`, `min_holding_bars`, `rebalance_cooldown_bars`는 replay 정책입니다. 현재 개인 `paper_policy_lab` 런타임이 그대로 강제하는 키가 아니므로, paper 5트랙에는 주문 cap/PPO runtime override만 별도로 비교합니다.

## Dry Run

```bash
/opt/anaconda3/envs/elephant/bin/python new/experiments/lgbm_global_search/run_global_search.py --dry-run
```

## Weekend Proxy Sweep

```bash
/opt/anaconda3/envs/elephant/bin/python new/experiments/lgbm_global_search/run_global_search.py \
  --stage proxy \
  --max-runs 105 \
  --deadline-kst 10:00
```

## 2026-05-24 Guardrail Finding

`GS-20260524-WEEKEND-PROXY-V2`에서 5m research 후보들이 첫 구간에서는 좋아 보였지만, 같은 runtime-actionable 정책을 8개 fold에 다시 걸면 안정성이 부족했습니다.

| 후보 | pass/folds | mean bps | min bps | 판단 |
|---|---:|---:|---:|---|
| `BUNDLE-20260521-POSTCLOSE` | 5/8 | 36.17 | 2.26 | explicit registry 기준 2026-05-26 default 유지 |
| `BUNDLE-20260518-195M0001` | 7/8 | 45.47 | -6.15 | backup/research only |
| `RESEARCH-20260524-5M-EXOG-BASE` | 5/8 | 8.29 | -108.97 | research/shadow only |
| `RESEARCH-20260524-5M-NEWS-BASE` | 4/8 | 11.79 | -30.91 | research/shadow only |
| `RESEARCH-20260524-5M-EXOG-SMALL` | 3/8 | -3.72 | -58.68 | research/shadow only |

`BUNDLE-20260521-POSTCLOSE`는 8개 fold 전부 PASS가 아니라 5/8 PASS입니다. 다만 같은 read-only fold replay에서 min bps가 양수이고 현재 explicit `artifacts/lgbm_paper_candidate/BUNDLE-20260521-POSTCLOSE` registry와 정렬된 후보라 첫날 개인 paper-lab default로 유지합니다. `BUNDLE-20260518-195M0001`는 평균 bps는 높지만 음수/BLOCKED fold가 있어 첫날 default 교체 근거로는 부족합니다. 또한 research 후보 3개는 calibrated trade classifier가 없어 `trade_probability_gate=enable` 시 전부 `classifier_uncalibrated`로 fail-closed 됩니다. 따라서 2026-05-26 paper default는 explicit registry 기준 `BUNDLE-20260521-POSTCLOSE`를 유지하고, backup/research 후보는 C12 + classifier + fold robustness를 모두 통과하기 전까지 default로 승격하지 않습니다.

근거 산출물:

- `artifacts/lgbm_global_search/GS-20260524-WEEKEND-PROXY-V2/service_policy_sweeps/runtime_actionable_fold_robustness.json`
- `artifacts/lgbm_global_search/GS-20260524-WEEKEND-PROXY-V2/service_policy_sweeps/default_bundle_fold_robustness.json`
- `artifacts/lgbm_global_search/GS-20260524-WEEKEND-PROXY-V2/service_policy_sweeps/backup_20260518_fold_robustness.json`
- `artifacts/lgbm_global_search/GS-20260524-WEEKEND-PROXY-V2/service_policy_sweeps/weekend_optimization_guardrail_summary_20260524_0903.json`

### STRICT Gate Calibration

5종목 paper universe에서 `trade_probability_gate`를 낮은 threshold까지 다시 훑었습니다.

| threshold | pass/folds | total orders | mean bps | min bps | reject rate | 판단 |
|---:|---:|---:|---:|---:|---:|---|
| 0.05~0.30 | 2/8 | 262 | 14.35 | -3.70 | 0.00 | baseline과 동일, filter 효과 없음 |
| 0.34 | 2/8 | 266 | 15.82 | -3.70 | 0.02 | 거의 baseline, 약한 filter |
| 0.35 | 1/8 | 256 | 16.41 | -2.80 | 0.22 | shadow calibration 후보 |
| 0.36 | 0/8 | 78 | 4.29 | -1.66 | 0.86 | 너무 강함 |
| tested 0.40~0.45 | 0/8 | 0 | 0.00 | 0.00 | 1.00 | 전 후보 reject |

0.34는 거의 baseline이고 0.36부터는 후보를 지나치게 많이 제거합니다. 따라서 `STRICT_GATE`는 `min_trade_probability: 0.35`로 shadow에서 cliff 주변 분포만 관찰하고, 첫날 paper 주문 트랙에서는 제외합니다.

추가 근거:

- `artifacts/lgbm_global_search/GS-20260524-WEEKEND-PROXY-V2/service_policy_sweeps/default_5ticker_classifier_threshold_low_sweep.json`
- `artifacts/lgbm_global_search/GS-20260524-WEEKEND-PROXY-V2/service_policy_sweeps/default_5ticker_classifier_threshold_fine_sweep.json`

## Full Sweep Candidates

Proxy sweep 결과 상위 후보만 full 253일로 다시 실행합니다.

먼저 proxy summary의 top-N을 full-stage one-candidate 설정으로 변환합니다.

```bash
/opt/anaconda3/envs/elephant/bin/python new/experiments/lgbm_global_search/prepare_full_configs.py \
  --proxy-run-id GS-20260524-WEEKEND-PROXY-V2 \
  --top-n 5 \
  --with-trade-classifier
```

생성 위치는 `artifacts/lgbm_global_search/<proxy-run-id>/full_configs/`입니다.
`full_config_plan.json` 안에 각 full run 명령이 함께 기록됩니다.

```bash
/opt/anaconda3/envs/elephant/bin/python new/experiments/lgbm_global_search/run_global_search.py \
  --stage full \
  --max-runs 20
```

## Validation Bundle Staging

Full 검증에서 통과한 research candidate는 C12/service-policy replay 전에
검증용 bundle layout으로만 stage합니다. Production registry는 변경하지 않습니다.

```bash
/opt/anaconda3/envs/elephant/bin/python new/experiments/lgbm_global_search/stage_research_bundle.py \
  --source-registry-dir artifacts/lgbm_global_search/<full-run-id>/registry \
  --candidate-version <version> \
  --bundle-id RESEARCH-<research-bundle-id> \
  --confirm-phrase STAGE_RESEARCH_BUNDLE_OK
```
