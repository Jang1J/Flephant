# Local Policy Lab

이 폴더는 2026-05-26 이후 KIS virtual/paper 장중 운영에서 같은 모델을 고정하고 실행 정책만 비교하기 위한 개인 실험용 하네스입니다.

- 개인 repo `Jang1J/Flephant` 실험 브랜치에서만 관리합니다.
- 팀 `ai-1`에는 검증된 default track만 별도로 올립니다.
- `.env`를 읽지 않습니다.
- 실계좌 주문과 production registry mutation을 하지 않습니다.
- 기본 설정은 5개 정책 모두 `mode: paper`이며, 각자 별도 KIS paper 계정/profile을 쓰는 것을 전제로 합니다.
- 계좌가 부족한 경우에만 일부 정책을 `mode: shadow`로 바꿔 주문 후보만 기록할 수 있습니다.
- 실행 결과는 `runs/` 아래에 저장되며 이 폴더는 gitignore됩니다.

## Env Profile

예시로 `MAIN_BASELINE`은 `profile_prefix: KIS_MAIN`을 사용합니다. 실행 전 사용자 터미널에서 아래처럼 profile별 값을 export해야 합니다.

```bash
export KIS_MAIN_MODE=virtual
export KIS_MAIN_APP_KEY=...
export KIS_MAIN_APP_SECRET=...
export KIS_MAIN_ACCOUNT_NUMBER=...
export KIS_MAIN_ACCOUNT_PRODUCT_CODE=01
```

5개 트랙을 실제 paper 주문으로 동시에 돌리려면 각각 `KIS_MAIN_*`, `KIS_SAFE_*`, `KIS_ACTIVE_*`, `KIS_TOPK_*`, `KIS_STRICT_*` paper 계정/key 세트를 준비하세요.

## Dry Run

```bash
/opt/anaconda3/envs/elephant/bin/python new/experiments/paper_policy_lab/run_multi_policy_lab.py --dry-run
```

## 5/26 장중 예시

```bash
/opt/anaconda3/envs/elephant/bin/python new/experiments/paper_policy_lab/run_multi_policy_lab.py \
  --run-id 20260526_open \
  --cycles 60 \
  --interval-sec 60
```

결과는 `new/experiments/paper_policy_lab/runs/<run_id>/` 아래에 policy별 JSON과 로그로 남습니다.
