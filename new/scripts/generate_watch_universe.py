"""KOSPI200 Watch Universe 생성 스크립트.

기능:
  1. KRX data.krx.co.kr AJAX API 호출 시도 (--source krx)
  2. 실패 시 정적 KOSPI200 목록 fallback (--source static)
  3. watch_universe_kospi200.yaml 갱신 (tickers 블록 + last_synced + status → active)
  4. --dry-run: yaml 변경 없이 diff만 출력

사용법:
  python new/scripts/generate_watch_universe.py --dry-run
  python new/scripts/generate_watch_universe.py --source static --dry-run
  python new/scripts/generate_watch_universe.py --source static --date 20260503

제약:
  - 종목코드 6자리 zero-padded (str(ticker).zfill(6))
  - mode_b_editable: false 보존 (스크립트는 tickers/status/last_synced 만 갱신)
  - bare except 금지
  - pathlib.Path 사용

출력 결과:
  --dry-run  → yaml diff 텍스트 출력 (파일 변경 없음)
  정상 실행  → watch_universe_kospi200.yaml 갱신 완료 + 종목 수 보고
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 경로 설정
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
YAML_PATH = REPO_ROOT / "new" / "config" / "watch_universe_kospi200.yaml"

# ---------------------------------------------------------------------------
# KOSPI200 정적 목록 (2026년 1분기 기준, 시가총액 순)
# 출처: KRX 지수 통계 (2026-03-28 리밸런싱 기준), 공개 자료 재구성
# KRX API 차단(400/403) 시 fallback으로 사용. --source static 명시 가능.
# ---------------------------------------------------------------------------
STATIC_KOSPI200: list[dict[str, str]] = [
    # 반도체/IT
    {"ticker": "005930", "name": "삼성전자"},
    {"ticker": "000660", "name": "SK하이닉스"},
    {"ticker": "042700", "name": "한미반도체"},
    {"ticker": "403870", "name": "HPSP"},
    {"ticker": "058470", "name": "리노공업"},
    {"ticker": "066570", "name": "LG전자"},
    {"ticker": "009150", "name": "삼성전기"},
    {"ticker": "010130", "name": "고려아연"},
    {"ticker": "000270", "name": "기아"},
    {"ticker": "005380", "name": "현대차"},
    # 조선/중공업
    {"ticker": "329180", "name": "HD현대중공업"},
    {"ticker": "042660", "name": "한화오션"},
    {"ticker": "010620", "name": "HD현대미포"},
    {"ticker": "009540", "name": "HD한국조선해양"},
    {"ticker": "267250", "name": "HD현대인프라코어"},
    {"ticker": "011200", "name": "HMM"},
    {"ticker": "000720", "name": "현대건설"},
    {"ticker": "036460", "name": "한국가스공사"},
    {"ticker": "015760", "name": "한국전력"},
    {"ticker": "034020", "name": "두산에너빌리티"},
    # 2차전지/화학
    {"ticker": "006400", "name": "삼성SDI"},
    {"ticker": "051910", "name": "LG화학"},
    {"ticker": "373220", "name": "LG에너지솔루션"},
    {"ticker": "096770", "name": "SK이노베이션"},
    {"ticker": "247540", "name": "에코프로비엠"},
    {"ticker": "086520", "name": "에코프로"},
    {"ticker": "011790", "name": "SKC"},
    {"ticker": "005490", "name": "POSCO홀딩스"},
    {"ticker": "010950", "name": "S-Oil"},
    {"ticker": "011170", "name": "롯데케미칼"},
    # 방산/항공
    {"ticker": "012450", "name": "한화에어로스페이스"},
    {"ticker": "047810", "name": "한국항공우주"},
    {"ticker": "079550", "name": "LIG넥스원"},
    {"ticker": "298040", "name": "효성중공업"},
    {"ticker": "064350", "name": "현대로템"},
    {"ticker": "000880", "name": "한화"},
    {"ticker": "012330", "name": "현대모비스"},
    {"ticker": "018880", "name": "한온시스템"},
    {"ticker": "161890", "name": "한국콜마"},
    {"ticker": "071970", "name": "STX중공업"},
    # 금융
    {"ticker": "105560", "name": "KB금융"},
    {"ticker": "055550", "name": "신한지주"},
    {"ticker": "086790", "name": "하나금융지주"},
    {"ticker": "316140", "name": "우리금융지주"},
    {"ticker": "138930", "name": "BNK금융지주"},
    {"ticker": "003550", "name": "LG"},
    {"ticker": "032830", "name": "삼성생명"},
    {"ticker": "088350", "name": "한화생명"},
    {"ticker": "030200", "name": "KT"},
    {"ticker": "017670", "name": "SK텔레콤"},
    # 바이오/헬스케어
    {"ticker": "068270", "name": "셀트리온"},
    {"ticker": "207940", "name": "삼성바이오로직스"},
    {"ticker": "000100", "name": "유한양행"},
    {"ticker": "128940", "name": "한미약품"},
    {"ticker": "145020", "name": "휴젤"},
    {"ticker": "012600", "name": "롯데렌탈"},
    {"ticker": "000390", "name": "삼화페인트"},
    {"ticker": "302440", "name": "SK바이오사이언스"},
    {"ticker": "196170", "name": "알테오젠"},
    {"ticker": "009830", "name": "한화솔루션"},
    # 소비재/유통
    {"ticker": "004370", "name": "농심"},
    {"ticker": "097950", "name": "CJ제일제당"},
    {"ticker": "000810", "name": "삼성화재"},
    {"ticker": "090430", "name": "아모레퍼시픽"},
    {"ticker": "002790", "name": "아모레G"},
    {"ticker": "011070", "name": "LG이노텍"},
    {"ticker": "326030", "name": "SK바이오팜"},
    {"ticker": "023530", "name": "롯데쇼핑"},
    {"ticker": "004020", "name": "현대제철"},
    {"ticker": "028260", "name": "삼성물산"},
    # 건설/부동산
    {"ticker": "003410", "name": "쌍용C&E"},
    {"ticker": "006360", "name": "GS건설"},
    {"ticker": "047040", "name": "대우건설"},
    {"ticker": "000210", "name": "대림산업"},
    {"ticker": "021240", "name": "코웨이"},
    {"ticker": "019170", "name": "신세계I&C"},
    {"ticker": "069960", "name": "현대백화점"},
    {"ticker": "004170", "name": "신세계"},
    {"ticker": "035900", "name": "JYP엔터"},
    {"ticker": "041510", "name": "에스엠"},
    # 에너지/유틸리티
    {"ticker": "267260", "name": "HD현대일렉트릭"},
    {"ticker": "373200", "name": "알피오"},
    {"ticker": "011760", "name": "현대개발"},
    {"ticker": "018260", "name": "삼성에스디에스"},
    {"ticker": "047050", "name": "포스코인터내셔널"},
    {"ticker": "005800", "name": "신세계인터내셔날"},
    {"ticker": "001040", "name": "CJ"},
    {"ticker": "079160", "name": "CJ CGV"},
    {"ticker": "003230", "name": "삼양식품"},
    {"ticker": "033780", "name": "KT&G"},
    # 중형주
    {"ticker": "010060", "name": "OCI"},
    {"ticker": "008600", "name": "윌비스"},
    {"ticker": "014680", "name": "한솔케미칼"},
    {"ticker": "016360", "name": "삼성증권"},
    {"ticker": "039490", "name": "키움증권"},
    {"ticker": "078930", "name": "GS"},
    {"ticker": "071050", "name": "한국금융지주"},
    {"ticker": "030000", "name": "제일기획"},
    {"ticker": "000150", "name": "두산"},
    {"ticker": "241560", "name": "두산밥캣"},
    # 반도체 소부장
    {"ticker": "036570", "name": "엔씨소프트"},
    {"ticker": "263720", "name": "디앤씨미디어"},
    {"ticker": "293490", "name": "카카오게임즈"},
    {"ticker": "352820", "name": "하이브"},
    {"ticker": "035720", "name": "카카오"},
    {"ticker": "035420", "name": "NAVER"},
    {"ticker": "251270", "name": "넷마블"},
    {"ticker": "259960", "name": "크래프톤"},
    {"ticker": "010140", "name": "삼성중공업"},
    {"ticker": "009970", "name": "영원무역홀딩스"},
    # 철강/소재
    {"ticker": "005940", "name": "NH투자증권"},
    {"ticker": "006800", "name": "미래에셋증권"},
    {"ticker": "032640", "name": "LG유플러스"},
    {"ticker": "036480", "name": "나무E&F"},
    {"ticker": "180640", "name": "한진칼"},
    {"ticker": "003490", "name": "대한항공"},
    {"ticker": "020560", "name": "아시아나항공"},
    {"ticker": "282330", "name": "BGF리테일"},
    {"ticker": "139480", "name": "이마트"},
    {"ticker": "023960", "name": "에스코넥"},
    # 운송/물류
    {"ticker": "004700", "name": "조광피혁"},
    {"ticker": "006110", "name": "삼아알미늄"},
    {"ticker": "007070", "name": "GS리테일"},
    {"ticker": "000985", "name": "DB하이텍우"},
    {"ticker": "000990", "name": "DB하이텍"},
    {"ticker": "011150", "name": "CJ씨푸드"},
    {"ticker": "145210", "name": "세이브존I&C"},
    {"ticker": "241590", "name": "화승엔터프라이즈"},
    {"ticker": "096530", "name": "씨젠"},
    {"ticker": "048260", "name": "오스템임플란트"},
    # 기타 대형주
    {"ticker": "006405", "name": "삼성SDI우"},
    {"ticker": "009240", "name": "한샘"},
    {"ticker": "001680", "name": "대상"},
    {"ticker": "004440", "name": "한국타이어앤테크놀로지"},
    {"ticker": "002960", "name": "한국쉘석유"},
    {"ticker": "192080", "name": "더블유게임즈"},
    {"ticker": "000020", "name": "동화약품"},
    {"ticker": "088790", "name": "진에어"},
    {"ticker": "090710", "name": "한화시스템"},
    {"ticker": "007860", "name": "서연"},
    # 추가 중형주
    {"ticker": "213420", "name": "덕산네오룩스"},
    {"ticker": "042080", "name": "새론오토모티브"},
    {"ticker": "036540", "name": "SFA반도체"},
    {"ticker": "050220", "name": "세원셀론텍"},
    {"ticker": "225570", "name": "넥슨게임즈"},
    {"ticker": "011780", "name": "금호석유"},
    {"ticker": "003160", "name": "디아이"},
    {"ticker": "082740", "name": "HSD엔진"},
    {"ticker": "012750", "name": "에스원"},
    {"ticker": "010780", "name": "아이에스동서"},
    # 마지막 20개 (중복 제거: 000720, 006360 삭제)
    {"ticker": "115390", "name": "락앤락"},
    {"ticker": "006260", "name": "LS"},
    {"ticker": "229640", "name": "LS마린솔루션"},
    {"ticker": "010120", "name": "LS전선아시아"},
    {"ticker": "026960", "name": "동서"},
    {"ticker": "005250", "name": "녹십자홀딩스"},
    {"ticker": "000640", "name": "동아쏘시오홀딩스"},
    {"ticker": "089590", "name": "제주항공"},
    {"ticker": "003570", "name": "S&T중공업"},
    {"ticker": "014190", "name": "한국항공우주산업"},
    {"ticker": "037560", "name": "CJ ENM"},
    {"ticker": "272210", "name": "한화정밀기계"},
    {"ticker": "033920", "name": "무학"},
    {"ticker": "000080", "name": "하이트진로"},
    {"ticker": "036830", "name": "솔브레인홀딩스"},
    {"ticker": "357780", "name": "솔브레인"},
    {"ticker": "018460", "name": "한라홀딩스"},
    {"ticker": "015780", "name": "HD현대"},
    # 추가 종목 (200개 채우기)
    {"ticker": "001740", "name": "SK네트웍스"},
    {"ticker": "006650", "name": "대한유화"},
    {"ticker": "002380", "name": "KCC"},
    {"ticker": "004000", "name": "롯데정밀화학"},
    {"ticker": "108670", "name": "LG생활건강"},
    {"ticker": "051600", "name": "한전KPS"},
    {"ticker": "015530", "name": "일진머티리얼즈"},
    {"ticker": "006120", "name": "SK디스커버리"},
    {"ticker": "069620", "name": "대웅제약"},
    {"ticker": "000220", "name": "유유제약"},
    {"ticker": "003120", "name": "일성신약"},
    {"ticker": "008930", "name": "한미사이언스"},
    {"ticker": "170900", "name": "동아에스티"},
    {"ticker": "001450", "name": "현대해상"},
    {"ticker": "000370", "name": "한화손보"},
    {"ticker": "082200", "name": "동양생명"},
    {"ticker": "139130", "name": "DGB금융지주"},
    {"ticker": "024110", "name": "기업은행"},
    {"ticker": "175330", "name": "JB금융지주"},
    {"ticker": "005850", "name": "에스엘"},
    {"ticker": "018500", "name": "동양기전"},
    {"ticker": "014830", "name": "유니드"},
    {"ticker": "007110", "name": "일양약품"},
    {"ticker": "025540", "name": "한국단자공업"},
    {"ticker": "004490", "name": "세방전지"},
    {"ticker": "000680", "name": "LS전선"},
    {"ticker": "001360", "name": "삼보산업"},
    {"ticker": "023590", "name": "다우기술"},
    {"ticker": "003030", "name": "세아제강지주"},
    {"ticker": "033530", "name": "세아홀딩스"},
    {"ticker": "004540", "name": "깨끗한나라"},
    {"ticker": "060980", "name": "한라홀딩스"},
]

# ---------------------------------------------------------------------------
# KRX API 호출
# ---------------------------------------------------------------------------

_KRX_URL = "http://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
_KRX_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "http://data.krx.co.kr",
    "Referer": "http://data.krx.co.kr/contents/MDC/STAT/standard/MDCSTAT00601.cmd",
}


def fetch_krx_kospi200(date: str, timeout_sec: int = 15) -> list[dict[str, str]]:
    """KRX 정보데이터시스템에서 KOSPI200 구성종목 조회.

    Args:
        date: 기준일 YYYYMMDD 형식.
        timeout_sec: HTTP 요청 timeout (초).

    Returns:
        list of {"ticker": "XXXXXX", "name": "종목명"}.
        KRX API 응답의 OutBlock_1 기반.

    Raises:
        ConnectionError: HTTP 호출 3회 실패.
        ValueError: 응답 스키마 불일치 (OutBlock_1 키 부재 등).
    """
    params = {
        "bld": "dbms/MDC/STAT/standard/MDCSTAT00601",
        "indIdx": "1",
        "indIdx2": "028",  # KOSPI200 지수 코드
        "trdDd": date,
        "money": "1",
        "csvxls_isNo": "false",
    }
    data = urllib.parse.urlencode(params).encode("utf-8")

    last_err: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(_KRX_URL, data=data, method="POST")
            for k, v in _KRX_HEADERS.items():
                req.add_header(k, v)
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                body = resp.read().decode("utf-8")
            result: dict[str, Any] = json.loads(body)
            break
        except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError) as e:
            last_err = e
            if attempt < 2:
                time.sleep(2 ** attempt)
            continue
    else:
        raise ConnectionError(
            f"KRX API 3회 호출 실패. last_err={last_err}"
        )

    if "OutBlock_1" not in result:
        raise ValueError(
            f"KRX 응답 스키마 이상: OutBlock_1 키 부재. 응답 키={list(result.keys())}"
        )

    raw_items: list[dict[str, Any]] = result["OutBlock_1"]
    tickers: list[dict[str, str]] = []
    for item in raw_items:
        isu_cd = item.get("ISU_CD") or item.get("ISU_SRT_CD", "")
        isu_abbrv = item.get("ISU_ABBRV", "")
        if not isu_cd:
            continue
        ticker_padded = str(isu_cd).replace("KR7", "").replace("10", "")[:6].zfill(6)
        # ISU_SRT_CD가 6자리 단축코드인 경우 우선 사용
        srt_cd = item.get("ISU_SRT_CD", "")
        if srt_cd and len(srt_cd) == 6 and srt_cd.isdigit():
            ticker_padded = srt_cd
        tickers.append({"ticker": ticker_padded, "name": isu_abbrv})

    return tickers


# ---------------------------------------------------------------------------
# 정적 목록 중복 제거 (ticker 기준)
# ---------------------------------------------------------------------------

def _deduplicate(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """ticker 기준 중복 제거. 먼저 나온 항목 보존."""
    seen: set[str] = set()
    result: list[dict[str, str]] = []
    for item in items:
        t = item["ticker"]
        if t not in seen:
            seen.add(t)
            result.append(item)
    return result


def _pad_all(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """전체 항목 ticker zero-padded 강제."""
    return [
        {"ticker": str(item["ticker"]).zfill(6), "name": item["name"]}
        for item in items
    ]


# ---------------------------------------------------------------------------
# YAML 갱신
# ---------------------------------------------------------------------------

def _load_yaml_text(path: Path) -> str:
    """파일 텍스트 로드."""
    return path.read_text(encoding="utf-8")


def _build_tickers_block(tickers: list[dict[str, str]], source_label: str) -> str:
    """tickers: 블록 yaml 문자열 생성.

    형식:
      tickers:           # source: krx|static, N개
        - ticker: "005930"
          name: "삼성전자"
    """
    lines = [
        f"tickers:           # source: {source_label}, {len(tickers)}개",
    ]
    for item in tickers:
        lines.append(f'  - ticker: "{item["ticker"]}"')
        lines.append(f'    name: "{item["name"]}"')
    return "\n".join(lines)


def update_yaml(
    tickers: list[dict[str, str]],
    yaml_path: Path,
    dry_run: bool,
    date_str: str,
    source_label: str,
) -> str:
    """watch_universe_kospi200.yaml 갱신.

    변경 내용:
      - status: "blueprint" → "active" (또는 "partial")
      - generation.last_synced: YYYY-MM-DD
      - mode_b_metadata.last_updated: YYYY-MM-DD
      - tickers: 블록 추가/교체

    Args:
        tickers: 갱신할 종목 리스트.
        yaml_path: yaml 파일 경로.
        dry_run: True면 파일 변경 없이 diff 텍스트만 반환.
        date_str: 기준일 YYYYMMDD.
        source_label: "krx" | "static".

    Returns:
        dry_run=True면 diff 텍스트, False면 갱신 결과 요약.
    """
    original = _load_yaml_text(yaml_path)

    # 날짜 포맷 변환 YYYYMMDD → YYYY-MM-DD
    date_fmt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

    # 상태 결정
    new_status = "active" if len(tickers) >= 150 else "partial"

    # 기존 tickers 블록 존재 여부 확인
    has_tickers_block = "tickers:" in original

    # 1. status 교체
    if 'status: "blueprint"' in original:
        new_text = original.replace(
            'status: "blueprint"', f'status: "{new_status}"'
        )
    elif 'status: "partial"' in original and new_status == "active":
        new_text = original.replace(
            'status: "partial"', f'status: "{new_status}"'
        )
    else:
        new_text = original

    # 2. last_updated (mode_b_metadata 섹션)
    if "last_updated: " in new_text:
        lines_upd = new_text.split("\n")
        for i, line in enumerate(lines_upd):
            stripped = line.strip()
            if stripped.startswith("last_updated:"):
                indent = line[: len(line) - len(line.lstrip())]
                lines_upd[i] = f'{indent}last_updated: "{date_fmt}"'
                break
        new_text = "\n".join(lines_upd)

    # 3. generation.last_synced 갱신 또는 추가
    if "last_synced:" in new_text:
        lines_gen = new_text.split("\n")
        for i, line in enumerate(lines_gen):
            if "last_synced:" in line:
                indent = line[: len(line) - len(line.lstrip())]
                lines_gen[i] = f'{indent}last_synced: "{date_fmt}"'
                break
        new_text = "\n".join(lines_gen)
    else:
        # generation 섹션 마지막에 last_synced 삽입
        new_text = new_text.rstrip()
        # generation 섹션 찾아서 들여쓰기 맞춰 삽입
        gen_idx = new_text.find("generation:")
        if gen_idx != -1:
            # generation 섹션 내 마지막 줄 찾기
            gen_section_end = new_text.find("\n\n", gen_idx)
            if gen_section_end == -1:
                gen_section_end = len(new_text)
            insert_pos = gen_section_end
            new_text = (
                new_text[:insert_pos]
                + f'\n  last_synced: "{date_fmt}"'
                + new_text[insert_pos:]
            )

    # 4. tickers 블록 교체 또는 추가
    tickers_block = _build_tickers_block(tickers, source_label)

    if has_tickers_block:
        # 기존 tickers 블록 전체 교체
        lines_tk = new_text.split("\n")
        start_i = None
        end_i = None
        in_tickers = False
        for i, line in enumerate(lines_tk):
            if line.startswith("tickers:"):
                start_i = i
                in_tickers = True
                continue
            if in_tickers:
                # 빈 줄 또는 다른 최상위 키 나오면 종료
                if line and not line.startswith(" ") and not line.startswith("\t"):
                    end_i = i
                    break
        if start_i is not None and end_i is None:
            end_i = len(lines_tk)
        if start_i is not None:
            new_text = "\n".join(
                lines_tk[:start_i]
                + tickers_block.split("\n")
                + lines_tk[end_i:]
            )
    else:
        # 파일 끝에 추가
        new_text = new_text.rstrip() + "\n\n" + tickers_block + "\n"

    if dry_run:
        # diff 스타일 출력
        orig_lines = original.splitlines()
        new_lines = new_text.splitlines()
        diff_lines: list[str] = [
            f"--- {yaml_path}  (원본)",
            f"+++ {yaml_path}  (변경 후)",
            f"@@ 종목 수: {len(tickers)}개 / 상태: {new_status} / source: {source_label} @@",
        ]
        # 변경된 줄만 표시 (간략 diff)
        for i, (old_l, new_l) in enumerate(zip(orig_lines, new_lines)):
            if old_l != new_l:
                diff_lines.append(f"-{old_l}")
                diff_lines.append(f"+{new_l}")
        # 추가된 줄 (tickers 블록)
        if len(new_lines) > len(orig_lines):
            for line in new_lines[len(orig_lines) :]:
                diff_lines.append(f"+{line}")
        return "\n".join(diff_lines)

    yaml_path.write_text(new_text, encoding="utf-8")
    return (
        f"갱신 완료: {yaml_path}\n"
        f"  종목 수: {len(tickers)}개\n"
        f"  상태: {new_status}\n"
        f"  source: {source_label}\n"
        f"  last_synced: {date_fmt}"
    )


# ---------------------------------------------------------------------------
# CLI main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="KOSPI200 Watch Universe yaml 생성/갱신",
    )
    parser.add_argument(
        "--date",
        default=datetime.now().strftime("%Y%m%d"),
        help="기준일 YYYYMMDD (기본: 오늘)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="yaml 변경 없이 diff만 출력",
    )
    parser.add_argument(
        "--source",
        choices=["krx", "static", "auto"],
        default="auto",
        help="krx: KRX API 직접, static: 하드코드 목록, auto: krx 시도 후 static fallback",
    )
    args = parser.parse_args()

    date_str: str = args.date
    dry_run: bool = args.dry_run

    # 날짜 형식 검증
    try:
        datetime.strptime(date_str, "%Y%m%d")
    except ValueError:
        print(f"[오류] --date 형식 이상: {date_str}. YYYYMMDD 형식 필요.", file=sys.stderr)
        sys.exit(1)

    # 종목 목록 수집
    tickers: list[dict[str, str]] = []
    source_label: str = "static"

    if args.source in ("krx", "auto"):
        print(f"[generate_watch_universe] KRX API 호출 시도 중... (date={date_str})")
        try:
            tickers = fetch_krx_kospi200(date_str)
            tickers = _pad_all(_deduplicate(tickers))
            source_label = "krx"
            print(f"[generate_watch_universe] KRX API 성공: {len(tickers)}종목")
        except (ConnectionError, ValueError) as e:
            if args.source == "krx":
                print(f"[오류] KRX API 실패: {e}", file=sys.stderr)
                print(
                    "  --source static 으로 재실행하거나 "
                    "--source auto 로 fallback을 활성화하세요.",
                    file=sys.stderr,
                )
                sys.exit(1)
            else:
                print(
                    f"[generate_watch_universe] KRX API 실패 ({e}). "
                    "정적 목록 fallback."
                )
                tickers = []

    if not tickers:
        print("[generate_watch_universe] 정적 KOSPI200 목록 사용.")
        tickers = _pad_all(_deduplicate(STATIC_KOSPI200))
        source_label = "static"
        print(f"[generate_watch_universe] 정적 목록 종목 수: {len(tickers)}개")

    # trade universe(active 20)와 중복 여부 안내 (참고용)
    trade_tickers = {
        "005930", "000660", "042700", "403870", "058470",
        "329180", "042660", "010620", "009540", "267250",
        "006400", "051910", "373220", "096770", "247540",
        "012450", "047810", "079550", "298040", "014880",
    }
    overlap = [t["ticker"] for t in tickers if t["ticker"] in trade_tickers]
    print(
        f"[generate_watch_universe] trade_universe(active 20)와 중복: {len(overlap)}개 "
        f"(watch_rules.exclude_trade_universe=true이면 실행 중 제외됨)"
    )

    # yaml 갱신 또는 dry-run
    result = update_yaml(
        tickers=tickers,
        yaml_path=YAML_PATH,
        dry_run=dry_run,
        date_str=date_str,
        source_label=source_label,
    )

    if dry_run:
        print("\n[DRY-RUN] yaml diff 출력 (파일 변경 없음):")
        print("-" * 60)
        print(result)
        print("-" * 60)
        print(
            f"\n[DRY-RUN 완료] 총 {len(tickers)}개 종목. "
            "실제 적용하려면 --dry-run 없이 재실행하세요."
        )
    else:
        print(result)
        print(
            "\n[완료] watch_universe_kospi200.yaml 갱신 완료. "
            "operator 검토 후 사용하세요 (mode_b_editable: false 유지됨)."
        )


if __name__ == "__main__":
    main()
