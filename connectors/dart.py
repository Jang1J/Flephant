"""
DART 공시 데이터 커넥터
- OpenDART API 기반 공시 리스트 / 기업 개황 조회
- .env에서 DART_API_KEY 로드
"""

import requests
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
DART_API_KEY = os.getenv("DART_API_KEY", "")
BASE_URL = "https://opendart.fss.or.kr/api"


def get_disclosure_list(
    corp_code: str = "",
    bgn_de: str = "",
    end_de: str = "",
    pblntf_ty: str = "",
    page_count: int = 20,
) -> pd.DataFrame:
    """
    공시 리스트 조회

    Args:
        corp_code: 고유번호 (8자리). 비워두면 전체
        bgn_de: 시작일 (YYYYMMDD)
        end_de: 종료일 (YYYYMMDD)
        pblntf_ty: 공시유형 (A=정기, B=주요사항, C=발행, D=지분, E=기타, F=외부감사, G=펀드, H=자산유동화, I=거래소, J=공정위, K=수시)
        page_count: 페이지당 건수

    Returns:
        DataFrame with 공시 목록
    """
    if not DART_API_KEY:
        print("[DART] API 키 미설정, 빈 결과 반환")
        return pd.DataFrame()

    params = {
        "crtfc_key": DART_API_KEY,
        "page_count": page_count,
    }
    if corp_code:
        params["corp_code"] = corp_code
    if bgn_de:
        params["bgn_de"] = bgn_de
    if end_de:
        params["end_de"] = end_de
    if pblntf_ty:
        params["pblntf_ty"] = pblntf_ty

    resp = requests.get(f"{BASE_URL}/list.json", params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "000":
        print(f"[DART] 에러: {data.get('message', 'unknown')}")
        return pd.DataFrame()

    return pd.DataFrame(data.get("list", []))


def get_corp_code_map() -> dict:
    """
    DART 고유번호 ↔ 종목코드 매핑 (XML 다운로드)
    Note: 이 함수는 dart-fss 패키지 활용
    """
    try:
        import dart_fss
        dart_fss.set_api_key(DART_API_KEY)
        corp_list = dart_fss.get_corp_list()
        mapping = {}
        for corp in corp_list.corps:
            if corp.stock_code:
                mapping[corp.stock_code] = {
                    "corp_code": corp.corp_code,
                    "corp_name": corp.corp_name,
                }
        return mapping
    except Exception as e:
        print(f"[DART] corp_code 매핑 실패: {e}")
        return {}


def get_company_overview(corp_code: str) -> dict:
    """
    기업 개황 조회

    Args:
        corp_code: DART 고유번호 (8자리)

    Returns:
        기업 개요 딕셔너리
    """
    if not DART_API_KEY:
        print("[DART] API 키 미설정, 빈 결과 반환")
        return {}

    params = {
        "crtfc_key": DART_API_KEY,
        "corp_code": corp_code,
    }
    resp = requests.get(f"{BASE_URL}/company.json", params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "000":
        print(f"[DART] 에러: {data.get('message', 'unknown')}")
        return {}

    return data


# ── smoke test ──
if __name__ == "__main__":
    print("=== DART Connector Smoke Test ===\n")

    if not DART_API_KEY:
        print("⚠️  DART_API_KEY가 .env에 없습니다!")
        print("   .env 파일에 DART_API_KEY=your_key 를 설정하세요.")
        exit(1)

    # 최근 공시 조회
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")

    print(f"[1] 최근 공시 리스트 ({start}~{end})")
    df = get_disclosure_list(bgn_de=start, end_de=end, page_count=5)
    if not df.empty:
        print(df[["corp_name", "report_nm", "rcept_dt"]].to_string(index=False))
    else:
        print("  (결과 없음)")
    print()

    print("[2] 삼성전자 공시 조회 (corp_code 필요)")
    print("  → corp_code 매핑은 get_corp_code_map()으로 조회")

    print("\n✅ DART smoke test 완료!")
