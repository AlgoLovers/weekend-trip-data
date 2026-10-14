"""
전국 축제 파일 만들기. weekend-trip 앱이 하루 한 번 받아 축제 일정을 갱신한다.

- 출처: 한국관광공사 TourAPI 4.0(KorService2) searchFestival2·detailCommon2·detailIntro2
- 결과: festivals/<지역>.json (앱 데이터셋의 Festival 형식과 같다)
- 상태: state/<지역>.json 에 회차(연도별 날짜)와 상세를 누적한다. 같은 contentid를 해마다 다시 쓰는 축제가 있어
  작년 날짜를 남겨 두려는 것("작년 일정 · 올해 미발표"). 공공데이터라 공개 저장소에 둬도 된다.
- 필요: DATA_GO_KR_KEY (공공데이터포털 일반 인증키 Decoding). 표준 라이브러리만.

실행: python3 scripts/festivals.py [--regions seoul,busan] [--max-details 200]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://apis.data.go.kr/B551011/KorService2"
KST = timezone(timedelta(hours=9))
KOREA = ((33.0, 38.7), (124.5, 132.0))

# weekend-trip/pipeline/dataset/build_dataset.py 의 REGIONS와 같은 이름 규칙을 쓴다(시군 이름이 앱의 장소와 맞아야 한다).
REGIONS = {
    "gangwon": {"area": ["강원"], "short": "강원", "ldong": "51"},
    "gyeonggi": {"area": ["경기"], "short": "경기", "ldong": "41"},
    "seoul": {"area": ["서울"], "short": "서울", "ldong": "11", "metro": True},
    "incheon": {"area": ["인천"], "short": "인천", "ldong": "28", "metro": True},
    "chungbuk": {"area": ["충청북도", "충북"], "short": "충북", "ldong": "43"},
    "daejeon": {"area": ["대전"], "short": "대전", "ldong": "30", "metro": True},
    "sejong": {"area": ["세종"], "short": "세종", "ldong": "36", "single": "세종시"},
    "chungnam": {"area": ["충청남도", "충남"], "short": "충남", "ldong": "44"},
    "daegu": {"area": ["대구"], "short": "대구", "ldong": "27", "metro": True},
    "gyeongbuk": {"area": ["경상북도", "경북"], "short": "경북", "ldong": "47"},
    "jeonbuk": {"area": ["전북", "전라북도"], "short": "전북", "ldong": "52"},
    # 2026년 광주·전남 통합(전남광주통합특별시): 옛 지역코드 둘 다, 광주 구는 "광주 동구"
    "jeonnam": {"area": ["전라남도", "전남"], "extra_areas": ["광주"], "short": "전남", "full": "전남광주통합특별시", "ldong": "12", "gu_prefix": "광주"},
    "busan": {"area": ["부산"], "short": "부산", "ldong": "26", "metro": True},
    "ulsan": {"area": ["울산"], "short": "울산", "ldong": "31", "metro": True},
    "gyeongnam": {"area": ["경상남도", "경남"], "short": "경남", "ldong": "48"},
    "jeju": {"area": ["제주"], "short": "제주", "ldong": "50"},
}
SHARED_NAMES = {"고성군": "gangwon"}

calls: Counter = Counter()
LIMIT_HIT = False


def mark_limit() -> None:
    global LIMIT_HIT
    LIMIT_HIT = True


class DailyLimit(RuntimeError):
    pass


def api(operation: str, **params) -> dict:
    key = os.environ.get("DATA_GO_KR_KEY", "").strip()
    if not key:
        sys.exit("DATA_GO_KR_KEY가 없습니다. 저장소 Settings → Secrets and variables → Actions에 등록하세요.")
    q = {"serviceKey": key, "MobileOS": "AND", "MobileApp": "WeekendTripData", "_type": "json",
         **{k: v for k, v in params.items() if v is not None}}
    url = f"{BASE}/{operation}?{urllib.parse.urlencode(q)}"
    calls[operation] += 1
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
        body = api(operation, numOfRows=100, pageNo=page, **params)
        got = items(body)
        out += got
        if not got or len(out) >= int(body.get("totalCount") or 0):
            return out
        page += 1


def clean(text: str | None) -> str | None:
    if not text:
        return None
    t = re.sub(r"<br\s*/?>", "\n", str(text), flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"[ \t]+", " ", t).strip()
    return t or None


def short(text: str | None, limit: int = 300) -> str | None:
    t = clean(text)
    if not t:
        return None
    t = t.replace("\n", " ")
    if len(t) <= limit:
        return t
    cut = t[:limit]
    end = max(cut.rfind(". "), cut.rfind("다."), cut.rfind("요."))
    return (cut[: end + 2] if end > 60 else cut).strip() + ("" if end > 60 else "…")


def https(url: str | None) -> str | None:
    if not url:
        return None
    return "https://" + url[7:] if url.startswith("http://") else url


def homepage(text: str | None) -> str | None:
    if not text:
        return None
    m = re.search(r'href\s*=\s*["\']?(https?://[^"\'\s>]+)', text) or re.search(r"(https?://[^\s\"'<>]+)", text)
    return m.group(1) if m else None


def first_phone(text: str | None) -> str | None:
    t = clean(text)
    m = re.search(r"\(?0\d{1,3}\)?[-.\s]?\d{3,4}[-.\s]?\d{4}|1\d{3}-?\d{4}", t or "")
    return m.group(0) if m else None


def license_of(code: str | None) -> str:
    return {"type1": "TYPE1", "type3": "TYPE3"}.get((code or "").strip().lower(), "UNKNOWN")


def ymd(s: str | None) -> str | None:
    s = (s or "").strip()
    return f"{s[:4]}-{s[4:6]}-{s[6:]}" if re.fullmatch(r"\d{8}", s) else None


def display_sigungu(region: str, name: str) -> str:
    cfg = REGIONS[region]
    name = (name or "").strip()
    if cfg.get("single"):
        return cfg["single"]
    if not name:
        return ""
    if cfg.get("metro"):
        return name if name.startswith(cfg["short"] + " ") else f"{cfg['short']} {name}"
    if cfg.get("gu_prefix") and name.endswith("구") and not name.startswith(cfg["gu_prefix"] + " "):
        return f"{cfg['gu_prefix']} {name}"
    if SHARED_NAMES.get(name, region) != region:
        return f"{cfg['short']} {name}"
    return name


def sigungu_of(region: str, sigungucode, addr1, sigungu: dict[str, str], areacode=None) -> str:
    name = sigungu.get(f"{areacode or ''}-{sigungucode or ''}") or sigungu.get(str(sigungucode or ""))
    if name:
        return name
    addr = re.sub(r"^\s*\S+(특별자치도|특별자치시|광역시|특별시|도)\s+", "", addr1 or "")
    m = re.search(r"(\S+?[시군구])(?=\s|$)", addr)
    return display_sigungu(region, m.group(1) if m else "")


def area_codes(region: str) -> tuple[list[str], dict[str, str]]:
    cfg = REGIONS[region]
    areas = items(api("areaCode2", numOfRows=50))
    codes, sg = [], {}
    for wants in [cfg["area"]] + [[x] for x in cfg.get("extra_areas", [])]:
        g = next((a for w in wants for a in areas if a.get("name") == w), None) or \
            next((a for w in wants for a in areas if w in a.get("name", "")), None)
        if not g:
            raise RuntimeError(f"{wants} 지역 코드를 찾지 못함")
        code = str(g["code"])
        codes.append(code)
        for s in items(api("areaCode2", areaCode=code, numOfRows=50)):
            name = display_sigungu(region, s["name"])
            sg[f"{code}-{s['code']}"] = name
            if len(codes) == 1:
                sg.setdefault(str(s["code"]), name)
    return codes, sg


def ldong_code(region: str) -> str:
    """법정동 시도 코드를 TourAPI(ldongCode2)에서 이름으로 찾는다. 못 찾으면 설정값."""
    cfg = REGIONS[region]
    names = [cfg.get("full", ""), *cfg["area"]]
    try:
        rows = items(api("ldongCode2", numOfRows=50))
    except RuntimeError:
        return cfg["ldong"]
    nm = lambda r: r.get("lDongRegnNm") or r.get("name") or ""
    cd = lambda r: str(r.get("lDongRegnCd") or r.get("code") or "")
    hit = next((r for n in names if n for r in rows if nm(r) == n), None) or \
        next((r for n in names if n for r in rows if n in nm(r)), None)
    return cd(hit) if hit and cd(hit) else cfg["ldong"]


def build_region(region: str, budget: int, log: list[str]) -> int:
    """지역 하나. 상세를 받은 수를 돌려준다(하루 한도에 걸리면 DailyLimit)."""
    state_path = ROOT / f"state/{region}.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    areas, sg = area_codes(region)
    start = date(date.today().year - 1, 1, 1).strftime("%Y%m%d")
    by_id = {str(f["contentid"]): f for area in areas for f in paged("searchFestival2", eventStartDate=start, areaCode=area)}
    for f in paged("searchFestival2", eventStartDate=start, lDongRegnCd=ldong_code(region)):
        by_id.setdefault(str(f["contentid"]), f)
    for cid, f in by_id.items():
        s, e = ymd(f.get("eventstartdate")), ymd(f.get("eventenddate"))
        if not (s and e):
            continue
        h = state.setdefault(cid, {"editions": {}})
        changed = h.get("modifiedtime") != f.get("modifiedtime")
        h.update({k: f.get(k) for k in ("title", "addr1", "mapx", "mapy", "areacode", "sigungucode", "modifiedtime", "tel", "firstimage", "cpyrhtDivCd")})
        h["editions"][s[:4]] = {"start": s, "end": e}
        if changed:
            h.pop("detail", None)  # 수정됐으면 상세를 다시 받는다(작년 요금·시간이 남지 않게)
    # 상세: 끝나지 않은 축제부터
    today = date.today().isoformat()
    todo = sorted((cid for cid, h in state.items() if "detail" not in h),
                  key=lambda c: not any(v["end"] >= today for v in state[c]["editions"].values()))
    done = 0
    try:
        for cid in todo[:budget]:
            if LIMIT_HIT:
                break
            c = (items(api("detailCommon2", contentId=cid)) or [{}])[0]
            i = (items(api("detailIntro2", contentId=cid, contentTypeId="15")) or [{}])[0]
            state[cid]["detail"] = {
                "overview": short(c.get("overview")),
                "homepage": homepage(c.get("homepage")) or homepage(i.get("eventhomepage")),
                "phone": first_phone(c.get("tel")) or first_phone(i.get("sponsor1tel")),
                "eventPlace": clean(i.get("eventplace")), "playTime": clean(i.get("playtime")),
                "feeRaw": clean(i.get("usetimefestival")), "fetchedAt": today,
            }
            done += 1
    except DailyLimit as e:
        # 상세 한도만 찬 것: 목록(사진·날짜)은 계속 갱신되도록 다른 지역은 상세 없이 진행한다.
        mark_limit()
        log.append(f"- 상세 하루 한도 도달({region}): {e}")
    finally:
        state_path.parent.mkdir(exist_ok=True)
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")
        write_output(region, state, sg)
        log.append(f"- {region}: 축제 {len(state)}개, 이번에 받은 회차 {len(by_id)}건, 상세 {done}개 받음, 상세 남음 {len([c for c in state if 'detail' not in state[c]])}개")
    return done


def write_output(region: str, state: dict, sg: dict[str, str]) -> None:
    out = []
    for cid, h in sorted(state.items()):
        try:
            lat, lng = float(h["mapy"]), float(h["mapx"])
        except (TypeError, ValueError, KeyError):
            continue
        if not (KOREA[0][0] <= lat <= KOREA[0][1] and KOREA[1][0] <= lng <= KOREA[1][1]):
            continue
        d = h.get("detail") or {}
        img = [{"url": https(h["firstimage"]), "license": license_of(h.get("cpyrhtDivCd")), "credit": "한국관광공사"}] if h.get("firstimage") else []
        out.append({
            "id": f"festival-{cid}", "name": (h.get("title") or "").strip(),
            "sigunguName": sigungu_of(region, h.get("sigungucode"), h.get("addr1"), sg, h.get("areacode")),
            "lat": lat, "lng": lng, "interests": ["FESTIVAL"],
            "editions": [{"year": int(y), "start": v["start"], "end": v["end"]} for y, v in sorted(h["editions"].items())],
            "addr": clean(h.get("addr1")), "phone": d.get("phone") or first_phone(h.get("tel")), "images": img,
            "overview": d.get("overview"), "homepage": d.get("homepage"),
            "eventPlace": d.get("eventPlace"), "playTime": d.get("playTime"), "feeRaw": d.get("feeRaw"),
        })
    p = ROOT / f"festivals/{region}.json"
    # 축제 내용이 그대로면 파일을 바꾸지 않는다(generatedAt만 바뀌면 ETag가 매일 바뀌어 앱이 매번 다시 받는다).
    try:
        prev = json.loads(p.read_text(encoding="utf-8"))
        if prev.get("festivals") == out:
            return
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    doc = {"schemaVersion": 1, "kind": "festivals", "region": region,
           "generatedAt": datetime.now(KST).isoformat(timespec="seconds"),
           "source": "한국관광공사 TourAPI 4.0 (공공누리)", "festivals": out}
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--regions", default=",".join(REGIONS))
    ap.add_argument("--max-details", type=int, default=150, help="지역마다 새로 받을 축제 상세 수(곳당 2건). 앱 데이터셋 빌드와 하루 한도를 나눠 쓴다")
    args = ap.parse_args()
    log: list[str] = []
    for r in [x.strip() for x in args.regions.split(",") if x.strip()]:
        try:
            build_region(r, args.max_details, log)
        except DailyLimit as e:
            # 목록 기능까지 한도에 걸리면 더 해도 소용없다.
            log.append(f"- 목록 하루 한도 도달, 여기서 멈춤: {e}")
            break
        except Exception as e:  # noqa: BLE001 - 한 지역이 실패해도 나머지는 계속
            log.append(f"- {r} 실패: {type(e).__name__}: {str(e)[:200]}")
    log.append(f"- API 호출: {dict(calls)}")
    print("\n".join(log))
    if os.environ.get("GITHUB_ACTIONS"):
        for line in log:
            print(f"::notice title=festivals::{line[:900]}")


if __name__ == "__main__":
    main()
