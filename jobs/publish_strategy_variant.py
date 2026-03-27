"""
Strategy Variant Publisher
- strategy_profiles.yaml에서 variant path 읽기
- 선택된 variant SC를 artifacts/strategy_card/SC-{date}.json으로 복사
- 기존 파이프라인(Risk→FDA→PFS)이 바로 소비 가능

Usage:
    python jobs/publish_strategy_variant.py --profile rebound --date 20260325
    python jobs/publish_strategy_variant.py --profile momentum --date 20260325
"""

import sys
import json
import shutil
import argparse
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE_DIR))

import yaml

PROFILES_PATH = _BASE_DIR / "config" / "strategy_profiles.yaml"
CANONICAL_DIR = _BASE_DIR / "artifacts" / "strategy_card"
SC_SCHEMA_PATH = _BASE_DIR / "schemas" / "strategy_card.json"
CANONICAL_DIR.mkdir(parents=True, exist_ok=True)


def load_profiles() -> dict:
    """strategy_profiles.yaml 로드"""
    if not PROFILES_PATH.exists():
        raise FileNotFoundError(f"[Publisher] strategy_profiles.yaml 없음: {PROFILES_PATH}")
    with open(PROFILES_PATH) as f:
        return yaml.safe_load(f)


def validate_schema(cards: list) -> list:
    """스키마 검증"""
    required = [
        "card_id", "snapshot_dt", "artifact_version", "ticker",
        "direction", "signal", "confidence", "pre_risk_score",
        "rationale", "source_strategy", "evidence_ids",
    ]
    issues = []
    try:
        import jsonschema
        with open(SC_SCHEMA_PATH) as f:
            schema = json.load(f)
        for card in cards:
            try:
                jsonschema.validate(instance=card, schema=schema)
            except jsonschema.ValidationError as e:
                issues.append(f"[Publisher] 스키마 오류 ({card.get('ticker')}): {e.message}")
    except ImportError:
        for card in cards:
            for field in required:
                if field not in card:
                    issues.append(f"[Publisher] 필수 필드 누락 ({card.get('ticker')}): {field}")
    return issues


def main():
    parser = argparse.ArgumentParser(description="Strategy Variant → Canonical SC Publish")
    parser.add_argument("--profile", required=True, help="사용할 profile 이름 (e.g., rebound, momentum)")
    parser.add_argument("--date", required=True, help="대상 날짜 (YYYYMMDD)")
    args = parser.parse_args()

    profile_name = args.profile
    target_date = args.date
    print(f"[Publisher] 시작: profile={profile_name}, date={target_date}")

    # profiles.yaml 로드
    try:
        profiles_cfg = load_profiles()
    except FileNotFoundError as e:
        print(f"[Publisher] {e}")
        sys.exit(1)

    profiles = profiles_cfg.get("profiles", {})
    if profile_name not in profiles:
        available = list(profiles.keys())
        print(f"[Publisher] 알 수 없는 profile: '{profile_name}'. 사용 가능: {available}")
        sys.exit(1)

    profile = profiles[profile_name]
    variant_dir = _BASE_DIR / profile["variant_dir"]
    variant_path = variant_dir / f"SC-{target_date}.json"

    # variant 파일 존재 확인
    if not variant_path.exists():
        generator = profile.get("generator", "알 수 없음")
        print(
            f"[Publisher] variant 파일 없음: {variant_path}\n"
            f"  → 먼저 생성하세요: python {generator} {target_date}"
        )
        sys.exit(1)

    # 파일 로드 + 스키마 검증
    with open(variant_path, encoding="utf-8") as f:
        cards = json.load(f)

    if not isinstance(cards, list):
        cards = [cards]

    issues = validate_schema(cards)
    if issues:
        for iss in issues:
            print(f"[Publisher] 경고: {iss}")
        print(f"[Publisher] 스키마 검증 실패 ({len(issues)}개 이슈). publish 중단.")
        sys.exit(1)

    print(f"[Publisher] 스키마 검증 통과 ({len(cards)}개 카드)")

    # canonical path로 복사
    canonical_path = CANONICAL_DIR / f"SC-{target_date}.json"
    shutil.copy2(variant_path, canonical_path)

    print(f"[Publisher] Publish 완료: {canonical_path}")
    print(f"[Publisher]   profile={profile_name} ({profile.get('description', '')})")
    print(f"[Publisher]   alpha_family={profile.get('alpha_family', 'unknown')}")
    print(f"[Publisher]   카드 수={len(cards)}개")
    print(f"[Publisher] strategy_loader.has_real_sc('{target_date}') = True 상태")


if __name__ == "__main__":
    main()
