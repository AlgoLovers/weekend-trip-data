"""
관광지 붐빔 예보 파일 만들기. weekend-trip 앱이 하루 한 번 받아 "붐빌 듯 / 보통 / 한적할 듯" 배지를 붙인다.

- 출처: 한국관광공사 관광지 집중률 예측(TatsCnctrRateService/tatsCnctrRatedList). 시군구마다 앞으로 30일,
  관광지별 하루 집중률(0~100) 예측이 온다. 공공데이터라 공개 저장소에 둬도 된다.
- 결과: crowd/<지역>.json. 앞으로 7일만, 관광지마다 하루 한 글자(0 한적할 듯, 1 보통, 2 붐빌 듯, - 예측 없음).
  앱이 장소 이름·시군구로 자기 장소와 잇는다.
- 전남·광주는 이 API에 자료가 없다(2026-11 확인). 빈 파일이 나온다.
- 필요: DATA_GO_KR_KEY. 표준 라이브러리만. 하루 약 350번 부른다(서비스별 하루 1,000번).
- 시군구 코드는 scripts/signgu.json(법정동, 2026 통합 전남광주 반영)을 쓴다. 바뀌면 이 파일만 고친다.

실행: python3 scripts/crowd.py [--regions seoul,busan]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://apis.data.go.kr/B551011"
KST = timezone(timedelta(hours=9))
DAYS = 7
# 집중률 구간. 예측값이라 화면에는 "~할 듯"으로만 쓴다.
QUIET_BELOW = 30.0
BUSY_FROM = 70.0

# 법정동 시도 코드 → 앱 지역 파일 이름(weekend-trip build_dataset의 REGIONS와 같은 이름)
AREA_REGION = {
    "11": "seoul", "26": "busan", "27": "daegu", "28": "incheon", "30": "daejeon", "31": "ulsan",
    "36110": "sejong", "41": "gyeonggi", "43": "chungbuk", "44": "chungnam", "47": "gyeongbuk",
    "48": "gyeongnam", "50": "jeju", "51": "gangwon", "52": "jeonbuk", "12": "jeonnam",
}
# 2026년 통합 전 광주 구 코드. 통합 전남광주(12)는 옛 코드(전남 46, 광주 29)로 묻는다.
GWANGJU_GU = {"동구": "29110", "서구": "29140", "남구": "29155", "북구": "29170", "광산구": "29200"}

calls: Counter = Counter()


class DailyLimit(RuntimeError):
    pass


def api(operation: str, **params) -> dict:
    key = os.environ.get("DATA_GO_KR_KEY", "").strip()
    if not key:
        sys.exit("DATA_GO_KR_KEY가 없습니다. 저장소 Settings → Secrets and variables → Actions에 등록하세요.")
    q = {"serviceKey": key, "MobileOS": "AND", "MobileApp": "WeekendTripData", "_type": "json",
         **{k: v for k, v in params.items() if v is not None}}
    url = f"{BASE}/{operation}?{urllib.parse.urlencode(q)}"
    calls[operation.split("/")[0]] += 1
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                raw = r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise DailyLimit(f"{operation}: HTTP 429")
            last = e
            time.sleep(5 * (attempt + 1))
            continue
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(5 * (attempt + 1))
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            if "LIMITED" in raw:
                raise DailyLimit(f"{operation}: {raw[:120]}")
            raise RuntimeError(f"{operation}: JSON이 아닌 응답(키 오류일 수 있음): {raw[:200]}")
        h = data.get("response", {}).get("header", data)
        code = str(h.get("resultCode", ""))
        if code == "22":
            raise DailyLimit(f"{operation}: {h.get('resultMsg')}")
        if code not in ("0000", "00"):
            raise RuntimeError(f"{operation}: {code} {h.get('resultMsg')}")
        return data["response"]["body"]
    raise RuntimeError(f"{operation}: 네트워크 오류 {last}")


def items(body: dict) -> list[dict]:
    it = body.get("items") or {}
    if not isinstance(it, dict):
        return []
    x = it.get("item") or []
    return x if isinstance(x, list) else [x]


def paged(operation: str, **params) -> list[dict]:
    out, page = [], 1
    while True:
        body = api(operation, numOfRows=1000, pageNo=page, **params)
        got = items(body)
        out += got
        if not got or len(out) >= int(body.get("totalCount") or 0):
            return out
        page += 1


def signgu_list() -> list[dict]:
    """법정동 시도·시군구(scripts/signgu.json, TourAPI ldongCode2에서 한 번 받아 둔 것).
    일반 구가 있는 시는 시 단위만 남긴다(구 단위는 예측이 없다)."""
    out = json.loads((ROOT / "scripts/signgu.json").read_text(encoding="utf-8"))
    codes = {(s["areaCd"], s["signguCd"]) for s in out}
    return [s for s in out if not (len(s["signguCd"]) == 3 and not s["signguCd"].endswith("0")
                                   and (s["areaCd"], s["signguCd"][:-1] + "0") in codes)]


def target(s: dict) -> tuple[str, str]:
    """(areaCd, signguCd) 5자리로 부른다."""
    a, c, name = s["areaCd"], s["signguCd"], s["signguNm"]
    if len(c) == 5:
        return c[:2], c
    if a == "12":
        full = GWANGJU_GU.get(name) or ("46" + c)
        return full[:2], full
    return a, a + c.zfill(3)


def level(rate: float) -> str:
    return "0" if rate < QUIET_BELOW else "2" if rate >= BUSY_FROM else "1"


def levels_for(rows: list[dict], start: datetime) -> dict[str, str]:
    """관광지 이름 → 앞으로 DAYS일 하루 한 글자. 같은 날 값이 여러 개면 큰 값."""
    days = [(start + timedelta(days=i)).strftime("%Y%m%d") for i in range(DAYS)]
    by: dict[str, dict[str, float]] = defaultdict(dict)
    for r in rows:
        try:
            name, ymd, rate = r["tAtsNm"], str(r["baseYmd"]), float(r.get("cnctrRate") or 0)
        except (KeyError, ValueError):
            continue
        if ymd in days:
            by[name][ymd] = max(by[name].get(ymd, 0.0), rate)
    return {n: "".join(level(v[d]) if d in v else "-" for d in days) for n, v in by.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--regions", default="", help="지역(쉼표). 비우면 전체")
    args = ap.parse_args()
    want = set(filter(None, args.regions.split(",")))
    now = datetime.now(KST)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    by_region: dict[str, list[list[str]]] = defaultdict(list)
    failed = []
    try:
        for s in signgu_list():
            region = AREA_REGION.get(s["areaCd"])
            if not region or (want and region not in want):
                continue
            a, c = target(s)
            try:
                rows = paged("TatsCnctrRateService/tatsCnctrRatedList", areaCd=a, signguCd=c)
            except RuntimeError as e:
                failed.append(f"{s['signguNm']}: {e}")
                continue
            for name, lv in sorted(levels_for(rows, start).items()):
                by_region[region].append([s["signguNm"], name, lv])
    except DailyLimit as e:
        print(f"하루 한도: {e}. 받은 지역만 씁니다.")
    out_dir = ROOT / "crowd"
    out_dir.mkdir(exist_ok=True)
    regions = sorted(want or set(AREA_REGION.values()))
    for region in regions:
        sites = by_region.get(region, [])
        path = out_dir / f"{region}.json"
        # 한도·오류로 하나도 못 받았으면 어제 파일을 둔다(앱은 날짜가 지난 칸을 쓰지 않는다)
        if not sites and path.exists() and region != "jeonnam":
            continue
        doc = {"generatedAt": now.isoformat(timespec="seconds"), "start": start.strftime("%Y%m%d"), "days": DAYS,
               "levels": {"0": "한적할 듯", "1": "보통", "2": "붐빌 듯"}, "sites": sites}
        path.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        print(f"- {region}: 관광지 {len(sites)}곳")
    for f in failed[:20]:
        print(f"- 실패 {f}")
    print(f"- 호출: {dict(calls)}")


if __name__ == "__main__":
    main()
