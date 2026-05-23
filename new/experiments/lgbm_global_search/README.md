# LGBM Global Search

개인 repo 실험용 bounded global search 하네스입니다. 목적은 2026-05-26 paper 운영 전후로 현재 후보 `BUNDLE-20260521-POSTCLOSE`를 건드리지 않고 더 좋은 research 후보가 있는지 확인하는 것입니다.

## 안전 원칙

- production registry `artifacts/lgbm`를 쓰지 않습니다.
- paper registry/candidate registry를 쓰지 않습니다.
- KIS/API/.env를 호출하거나 읽지 않습니다.
- 결과는 `artifacts/lgbm_global_search/` 아래에 저장합니다.
- 후보가 좋아 보여도 C12 backtest + service-policy replay + prelive 검증 전에는 paper default로 승격하지 않습니다.

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
