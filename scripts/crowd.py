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
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://apis.data.go.kr/B551011"
KST = timezone(timedelta(hours=9))
DAYS = 7
# 작업 제한(30분) 안에 끝내려고, 이만큼 지나면 다 받은 지역만 쓰고 멈춘다(나머지는 어제 파일 유지)
BUDGET_SEC = 20 * 60
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


def get(operation: str, params: dict) -> str:
    key = os.environ.get("DATA_GO_KR_KEY", "").strip()
    if not key:
        sys.exit("DATA_GO_KR_KEY가 없습니다. 저장소 Settings → Secrets and variables → Actions에 등록하세요.")
    q = urllib.parse.urlencode({"serviceKey": key, "MobileOS": "AND", "MobileApp": "WeekendTripData", "_type": "json", **params})
    last = None
    for attempt in range(3):
        try:
            calls[operation.split("/")[0]] += 1
            with urllib.request.urlopen(f"{BASE}/{operation}?{q}", timeout=40) as r:
                text = r.read().decode("utf-8", errors="replace")
            if "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS" in text:
                raise DailyLimit(operation)
            return text
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                if attempt < 2:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise DailyLimit(f"{operation}: 429")
            raise RuntimeError(f"{operation}: HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"{operation}: 네트워크 오류 {last}")


def parse(text: str, operation: str) -> tuple[list[dict], int]:
    """JSON이나 XML 응답 → (항목, totalCount). weekend-trip pipeline/popularity와 같은 처리."""
    t = text.lstrip()
    if t.startswith("{"):
        d = json.loads(t)
        if "response" not in d:
            raise RuntimeError(f"{operation}: {t[:200]}")
        h = d["response"].get("header", {})
        if str(h.get("resultCode")) not in ("0000", "00", "0"):
            raise RuntimeError(f"{operation}: {h.get('resultCode')} {h.get('resultMsg')} {t[:160]}")
        body = d["response"].get("body") or {}
        it = body.get("items")
        rows = []
        if isinstance(it, dict):
            x = it.get("item") or []
            rows = x if isinstance(x, list) else [x]
        return rows, int(body.get("totalCount") or 0)
    root = ET.fromstring(t)
    code = root.findtext(".//resultCode") or root.findtext(".//returnReasonCode")
    if code not in ("0000", "00", "0", None):
        raise RuntimeError(f"{operation}: {code} {root.findtext('.//resultMsg') or root.findtext('.//returnAuthMsg')}")
    rows = [{c.tag: (c.text or "").strip() for c in item} for item in root.iter("item")]
    return rows, int(root.findtext(".//totalCount") or 0)


def paged(operation: str, **params) -> list[dict]:
    out, page = [], 1
    while True:
        rows, total = parse(get(operation, {**params, "numOfRows": 1000, "pageNo": page}), operation)
        out += rows
        if not rows or len(out) >= total:
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
    todo = [s for s in signgu_list() if AREA_REGION.get(s["areaCd"]) and (not want or AREA_REGION[s["areaCd"]] in want)]
    left = Counter(AREA_REGION[s["areaCd"]] for s in todo)  # 지역마다 남은 시군구 수
    failed = []
    began = time.monotonic()
    try:
        for n, s in enumerate(todo, 1):
            if time.monotonic() - began > BUDGET_SEC:
                print(f"시간 제한: {n - 1}/{len(todo)} 시군구에서 멈춤", flush=True)
                break
            region = AREA_REGION[s["areaCd"]]
            a, c = target(s)
            try:
                rows = paged("TatsCnctrRateService/tatsCnctrRatedList", areaCd=a, signguCd=c)
            except RuntimeError as e:
                failed.append(f"{s['signguNm']}: {e}")
                continue
            for name, lv in sorted(levels_for(rows, start).items()):
                by_region[region].append([s["signguNm"], name, lv])
            left[region] -= 1
            if n % 20 == 0:
                print(f"  {n}/{len(todo)} 시군구, {int(time.monotonic() - began)}초", flush=True)
    except DailyLimit as e:
        print(f"하루 한도: {e}. 다 받은 지역만 씁니다.", flush=True)
    out_dir = ROOT / "crowd"
    out_dir.mkdir(exist_ok=True)
    regions = sorted(want or set(AREA_REGION.values()))
    for region in regions:
        sites = by_region.get(region, [])
        path = out_dir / f"{region}.json"
        # 다 못 받은 지역은 어제 파일을 둔다(앱은 날짜가 지난 칸을 쓰지 않는다)
        if left.get(region, 0) > 0 or (not sites and path.exists() and region != "jeonnam"):
            print(f"- {region}: 이번엔 건너뜀(남은 시군구 {left.get(region, 0)})")
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
