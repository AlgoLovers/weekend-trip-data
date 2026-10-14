"""
데이터셋 보강(외부 호출 없음). TourAPI 빌드 끝에서도, 따로도(전 지역 JSON에 다시 적용) 쓴다.

1. 가족 앱에 맞지 않는 곳 빼기(골프장·카지노·경마·주점·모텔 …)
2. 국가유산 연결(pipeline/heritage/heritage.json): 종목·시대·지정일·국가유산포털 링크, 분류로 관심사 더하기
3. 손으로 확인한 정보(pipeline/curated/<지역>.json): 운영시간·요금·소개·아이 팁 덮어쓰기 + verifiedAt

실행: python3 pipeline/enrich/enrich.py            # assets/dataset/*.json 전체에 적용
"""
from __future__ import annotations

import glob
import json
import math
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 이름으로 거른다. "노래비·노래동산"(기념물)은 남기고 노래방·노래연습장만.
EXCLUDE = re.compile(
    r"(?<!미니)(?<!파크)골프(장|클럽|CC|컨트리|아카데미|연습장|존|카운티)|컨트리\s*클럽|(^|\s)\S*CC($|\s|\()|카지노|경마(장|공원)|경륜(장|공원)|경정장|"
    r"노래(방|연습장)|성인(전용|오락)|룸살롱|나이트클럽|당구장|스크린\s*골프|PC\s*방|피씨방|사격장"
)

# 장소 관심사 규칙(지식그래프의 장소–관심사 간선). 앱 검색의 Concepts와 짝을 이룬다.
# 이름에서 찾으면 확실, 소개글에서는 뜻이 분명한 말만 쓴다(오탐을 줄이려고).
INTEREST_RULES = {
    "KING": {"title": ["궁궐", "행궁", "왕릉", "종묘", "감영", "읍성", "관아", "객사", "장릉", "조선왕릉", "대왕"],
             "body": ["조선왕릉", "세종대왕", "단종", "태조 이성계"]},
    "WAR": {"title": ["전적", "전쟁", "대첩", "안보", "땅굴", "통일전망대", "평화전망대", "산성", "노동당사", "격전", "전투", "의병", "현충"],
            "body": ["격전지", "전적지"]},
    # "선사"만 쓰면 '도의선사탑'(禪師)이 걸린다
    "PREHISTORIC": {"title": ["선사유적", "선사시대", "선사박물관", "구석기", "신석기", "고인돌", "지석묘", "공룡", "화석", "암각화", "패총", "고생대", "자연사"],
                    "body": ["구석기", "신석기", "고인돌", "공룡", "화석", "선사시대", "주먹도끼"]},
    # "천문"·"천체"만 쓰면 '춘천문학공원'(춘+천문+학), '남대천체육공원'이 걸린다
    "SPACE": {"title": ["천문대", "천문관", "천문과학", "우주", "운석", "과학관", "천체관", "별마로", "별누리"],
              "body": ["천체망원경", "천문대", "천체 관측"]},
    "FIGURE": {"title": ["생가", "기념관", "문학관", "문학촌", "기념공원", "사당", "서원", "묘역", "유허"],
               "body": ["태어난 곳", "생가", "독립운동"]},
    "NATURE": {"title": ["계곡", "폭포", "동굴", "해변", "해수욕장", "수목원", "휴양림", "생태", "습지", "호수", "숲", "국립공원", "자연휴양"],
               "body": []},
}



# 이름이 산·바다·캠핑장 같은 곳이면 소개글의 역사 단어(근처 인물·사건 언급)로 관심사를 붙이지 않는다.
# 예: '초곡해수욕장'(소개에 근처 생가 언급) → 위인 X, '국지산'(단종 전설 언급) → 왕 X, '별빛캠핑장' → 우주 X
OUTDOOR_TITLE = re.compile(r"산$|봉$|해수욕장|해변|계곡|캠핑|야영|글램핑|수목원|휴양림|마을|공원$|섬$|항$|저수지|호수")
CAMP_TITLE = re.compile(r"캠핑|야영|글램핑|빌리지")


def derive_interests(title: str, overview: str | None, cat1: str | None) -> list[str]:
    t, b = title or "", (overview or "")[:600]
    if OUTDOOR_TITLE.search(t):
        b = ""
    out = [k for k, rule in INTEREST_RULES.items() if any(w in t for w in rule["title"]) or any(w in b for w in rule["body"])]
    if CAMP_TITLE.search(t) and "SPACE" in out:
        out.remove("SPACE")
    if cat1 == "A01" and "NATURE" not in out:  # TourAPI 대분류 A01 = 자연
        out.append("NATURE")
    return out


# 국가유산 분류 → 아이 관심사
CAT_INTERESTS = [
    (re.compile(r"성곽|산성|읍성|전적|봉수|진지|관방|병영|수영"), "WAR"),
    (re.compile(r"선사|고인돌|지석묘|패총|암각화|움집|구석기|신석기|청동기"), "PREHISTORIC"),
    (re.compile(r"사당|생가|고택|가옥|서원|향교|정려|묘소|인물"), "FIGURE"),
    (re.compile(r"자연유산|천연|지질|동굴|식물|노거수|명승|경관|섬|폭포|계곡"), "NATURE"),
]
# 왕과 궁궐: 분류 이름이 정확히 궁궐·왕실무덤일 때나 이름이 왕릉·궁궐일 때만('궁궐·관아' 중분류의 관아는 아님)
KING_CATS = {"궁궐", "왕실무덤", "왕릉", "종묘", "행궁"}
KING_NAME = re.compile(r"왕릉|왕릉원|궁$|궁궐|행궁|종묘|태실")


def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def norm(s: str) -> str:
    s = re.sub(r"\(.*?\)|\[.*?\]", "", s or "")
    return re.sub(r"[\s·.,\-]", "", s)


def km(a, b) -> float:
    r = math.radians
    h = math.sin(r(b[0] - a[0]) / 2) ** 2 + math.cos(r(a[0])) * math.cos(r(b[0])) * math.sin(r(b[1] - a[1]) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


class Spatial:
    """표준데이터 행을 좌표 격자에 넣고, 이름·거리로 가장 맞는 행을 찾는다."""

    def __init__(self, rows: list[dict], name_key: str, max_km: float = 1.5):
        self.max_km, self.name_key, self.grid = max_km, name_key, {}
        for r in rows:
            try:
                lat, lng = float(r.get("latitude") or 0), float(r.get("longitude") or 0)
            except ValueError:
                continue
            if not (33 <= lat <= 38.7 and 124.5 <= lng <= 132):
                continue
            r["_ll"] = (lat, lng)
            self.grid.setdefault((int(lat * 50), int(lng * 50)), []).append(r)

    def find(self, name: str, lat: float, lng: float) -> dict | None:
        n = norm(name)
        if len(n) < 2:
            return None
        best, best_s = None, 0.0
        gx, gy = int(lat * 50), int(lng * 50)
        k = math.ceil(self.max_km / 1.7)  # 격자 한 칸은 위도 약 2.2km·경도 약 1.8km
        for dx in range(-k, k + 1):
            for dy in range(-k, k + 1):
                for r in self.grid.get((gx + dx, gy + dy), []):
                    d = km((lat, lng), r["_ll"])
                    if d > self.max_km:
                        continue
                    rn = norm(r.get(self.name_key, ""))
                    if not rn:
                        continue
                    if rn == n:
                        sc = 3.0
                    elif (rn in n or n in rn) and min(len(rn), len(n)) >= 3:
                        sc = 2.0
                    else:
                        continue
                    sc -= d / self.max_km
                    if sc > best_s:
                        best, best_s = r, sc
        return best


def _plus(s: str | None) -> str:
    return re.sub(r"\s*\+\s*", ", ", (s or "").strip()).strip(", ")


def museum_hours(r: dict) -> str | None:
    def hm(x):
        x = (x or "").strip()
        return x if re.fullmatch(r"\d{1,2}:\d{2}", x) and x != "00:00" else None
    wo, wc = hm(r.get("weekdayOperOpenHhmm")), hm(r.get("weekdayOperColseHhmm"))
    ho, hc = hm(r.get("holidayOperOpenHhmm")), hm(r.get("holidayCloseOpenHhmm"))
    if not (wo and wc):
        return None
    if not (ho and hc) or (ho, hc) == (wo, wc):
        return f"{wo}~{wc}" if (ho, hc) == (wo, wc) else f"평일 {wo}~{wc}"
    return f"평일 {wo}~{wc}, 주말·공휴일 {ho}~{hc}"


def museum_closed(r: dict) -> str | None:
    t = _plus(r.get("rstdeInfo")).replace(", ", "·")
    t = re.sub(r"(^|·)공($|·)", r"\1공휴일\2", t)
    return t or None


def museum_fee(r: dict) -> str | None:
    def won(x):
        x = re.sub(r"[^0-9]", "", str(x or ""))
        return int(x) if x else None
    a, y, c = won(r.get("adultChrge")), won(r.get("yngbgsChrge")), won(r.get("childChrge"))
    etc = _plus(r.get("etcChrgeInfo"))
    if re.fullmatch(r"(전부\s*)?(무료|0|없음|-)", etc or ""):
        etc = ""
    if a is None and y is None and c is None:
        return etc or None
    if (a or 0) == 0 and (y or 0) == 0 and (c or 0) == 0:
        return "무료" + (f" ({etc})" if etc else "")
    parts = [f"{k} {v:,}원" for k, v in (("어른", a), ("청소년", y), ("어린이", c)) if v is not None]
    return " · ".join(parts) + (f" ({etc})" if etc else "")


# 표준데이터 주소 → 지역 코드와 시군구 이름(파이프라인 build_dataset의 이름 규칙과 같게)
ADDR_REGION = [
    ("서울특별시", "seoul", "서울", "metro"), ("부산광역시", "busan", "부산", "metro"), ("대구광역시", "daegu", "대구", "metro"),
    ("인천광역시", "incheon", "인천", "metro"), ("대전광역시", "daejeon", "대전", "metro"), ("울산광역시", "ulsan", "울산", "metro"),
    ("광주광역시", "jeonnam", "광주", "gu"), ("광주특별시", "jeonnam", "광주", "gu"), ("전남광주통합특별시", "jeonnam", "광주", "gu"),
    ("전라남도", "jeonnam", "광주", "gu"), ("세종특별자치시", "sejong", "세종", "single"), ("경기도", "gyeonggi", "경기", ""),
    ("강원특별자치도", "gangwon", "강원", ""), ("강원도", "gangwon", "강원", ""), ("충청북도", "chungbuk", "충북", ""),
    ("충청남도", "chungnam", "충남", ""), ("전북특별자치도", "jeonbuk", "전북", ""), ("전라북도", "jeonbuk", "전북", ""),
    ("경상북도", "gyeongbuk", "경북", ""), ("경상남도", "gyeongnam", "경남", ""), ("제주특별자치도", "jeju", "제주", ""),
]


def region_of_addr(addr: str | None) -> tuple[str, str] | None:
    """주소 → (지역 코드, 앱 시군구 이름). 모르면 None."""
    a = (addr or "").strip()
    for prefix, code, short, kind in ADDR_REGION:
        if a.startswith(prefix):
            rest = a[len(prefix):].split()
            sg = rest[0] if rest and re.fullmatch(r"\S+[시군구]", rest[0]) else ""
            if kind == "single":
                return code, "세종시"
            if not sg:
                return code, ""
            if kind == "metro":
                return code, f"{short} {sg}"
            if kind == "gu" and sg.endswith("구"):
                return code, f"광주 {sg}"
            if sg == "고성군" and code != "gangwon":
                return code, f"{short} {sg}"
            return code, sg
    return None


def fest_norm(s: str) -> str:
    """축제 이름 비교용: 회차·연도·괄호를 뗀다("제25회 2026 춘천마임축제" → "춘천마임축제")."""
    s = re.sub(r"제?\s*\d+\s*회|20\d\d\s*년?|\(.*?\)", "", s or "")
    return norm(s)


CLOSED_NOW = re.compile(r"^(장기|임시)?\s*휴관\s*(중)?$|리모델링|폐관|휴관\s*중")


def clean_url(u: str | None) -> str | None:
    u = (u or "").strip()
    if not u:
        return None
    u = re.sub(r"^(https?):/+", r"\1://", u)
    return u if u.startswith("http") else "http://" + u


def display_fest_name(name: str) -> str:
    """'2025 보령 국가유산야행', '제13회 춘천영화제' → 앞의 연도·회차를 뗀 이름(회차 날짜는 따로 보여준다)."""
    n = re.sub(r"^\s*(20\d\d\s*년?\s*)?(제?\s*\d+\s*회\s*)?", "", name or "").strip()
    return re.sub(r"\s+", " ", n) or (name or "").strip()


def standard_sets() -> dict:
    base = ROOT / "pipeline/standard"
    return {
        "museum": Spatial(load(base / "museum.json", []), "fcltyNm"),
        "tourist": Spatial(load(base / "tourist.json", []), "trrsrtNm"),
        "festival": Spatial(load(base / "festival.json", []), "fstvlNm", max_km=5.0),
    }


def add_unlisted(ds: dict, std: dict) -> tuple[int, int]:
    """TourAPI에 없는 박물관·미술관과 축제를 표준데이터에서 더한다(이 지역 주소인 것만). 여러 번 해도 같다."""
    import hashlib
    region = ds["region"]
    ds["places"] = [p for p in ds["places"] if not p["id"].startswith("std-")]
    ds["festivals"] = [f for f in ds.get("festivals", []) if not f["id"].startswith("std-")]
    # 이미 있는 장소와 이름·거리로 겹치면 더하지 않는다
    # 같은 곳이 좌표만 조금 다르게 들어온 경우가 있어 10km 안의 같은(포함) 이름은 같은 곳으로 본다
    have = Spatial([{"n": p["name"], "latitude": p["lat"], "longitude": p["lng"]} for p in ds["places"]], "n", 10.0)
    museums = 0
    seen = set()
    for r in std["museum"].grid.values():
        for m in r:
            reg = region_of_addr(m.get("rdnmadr") or m.get("lnmadr"))
            name = (m.get("fcltyNm") or "").strip()
            if not reg or reg[0] != region or not name or EXCLUDE.search(name) or not reg[1]:
                continue
            lat, lng = m["_ll"]
            if CLOSED_NOW.search(museum_closed(m) or "") or have.find(name, lat, lng) or norm(name) in seen:
                continue
            seen.add(norm(name))
            pid = "std-m-" + hashlib.sha1(f"{name}|{lat:.4f}|{lng:.4f}".encode()).hexdigest()[:10]
            ds["places"].append({
                "id": pid, "contentTypeId": 14, "name": name, "sigunguName": reg[1], "lat": lat, "lng": lng,
                "addr": (m.get("rdnmadr") or m.get("lnmadr") or "").strip() or None,
                "overview": _plus(m.get("fcltyIntrcn")) or None,
                "hoursRaw": museum_hours(m), "closedRaw": museum_closed(m), "feeRaw": museum_fee(m),
                "phone": (m.get("operPhoneNumber") or m.get("phoneNumber") or "").strip() or None,
                "images": [], "kidTip": None, "interests": derive_interests(name, m.get("fcltyIntrcn"), None), "verifiedAt": None,
            })
            museums += 1
    # 축제: 이름(회차·연도 뗀 것)과 5km 안이면 같은 축제
    fests = 0
    have_f = {(fest_norm(f["name"])) for f in ds.get("festivals", [])}
    near_f = Spatial([{"n": fest_norm(f["name"]), "latitude": f["lat"], "longitude": f["lng"]} for f in ds.get("festivals", [])], "n", 5.0)
    by_key: dict = {}
    for cell in std["festival"].grid.values():
        for r in cell:
            reg = region_of_addr(r.get("rdnmadr") or r.get("lnmadr"))
            name = (r.get("fstvlNm") or "").strip()
            start, end = (r.get("fstvlStartDate") or "").strip(), (r.get("fstvlEndDate") or "").strip()
            if not reg or reg[0] != region or not name or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end) or end < start:
                continue
            if start < f"{date.today().year - 1}-01-01":  # 작년보다 오래된 회차만 있으면 넣지 않는다
                continue
            key = fest_norm(name)
            lat, lng = r["_ll"]
            if key in have_f or near_f.find(key, lat, lng):
                continue
            e = by_key.setdefault(key, {"r": r, "eds": {}})
            if start >= max(e["eds"].values(), key=lambda x: x["start"], default={"start": ""})["start"]:
                e["r"] = r  # 이름·장소·연락처는 가장 최근 회차 것
            e["eds"][start[:4]] = {"year": int(start[:4]), "start": start, "end": end}
    for key, e in by_key.items():
        r = e["r"]
        reg = region_of_addr(r.get("rdnmadr") or r.get("lnmadr"))
        lat, lng = r["_ll"]
        hp = (r.get("homepageUrl") or "").strip()
        prog = _plus(r.get("fstvlCo"))
        ds["festivals"].append({
            "id": "std-f-" + hashlib.sha1(f"{key}|{reg[1]}".encode()).hexdigest()[:10],
            "name": display_fest_name(r["fstvlNm"]), "sigunguName": reg[1], "lat": lat, "lng": lng,
            "interests": ["FESTIVAL"], "editions": sorted(e["eds"].values(), key=lambda x: x["year"]),
            "addr": (r.get("rdnmadr") or r.get("lnmadr") or "").strip() or None,
            "phone": (r.get("phoneNumber") or "").strip() or None, "images": [],
            "overview": f"주요 프로그램: {prog}" if prog else None,
            "homepage": clean_url(hp),
            "eventPlace": _plus(r.get("opar")) or None,
        })
        fests += 1
    return museums, fests


def heritage_interests(h: dict) -> set[str]:
    cats = h.get("cats") or []
    text = " ".join(cats) + " " + h.get("name", "")
    out = {code for rx, code in CAT_INTERESTS if rx.search(text)}
    if KING_CATS & set(cats) or KING_NAME.search(h.get("name", "")):
        out.add("KING")
    if h.get("kind") in ("천연기념물", "명승"):
        out.add("NATURE")
    return out


def enrich(ds: dict, heritage: dict | None = None, curated: dict | None = None, log: list[str] | None = None,
           standard: dict | None = None) -> dict:
    """데이터셋 dict를 고쳐서 돌려준다(같은 객체). 여러 번 적용해도 결과가 같다."""
    log = log if log is not None else []
    heritage = heritage if heritage is not None else load(ROOT / "pipeline/heritage/heritage.json", {})
    cur_file = curated if curated is not None else load(ROOT / f"pipeline/curated/{ds['region']}.json", {})
    # 파일 형식: {"places": {placeId: {...}}, "askCards": [...]} (예전 형식: placeId → {...})
    curated = cur_file.get("places", cur_file if "askCards" not in cur_file else {})
    curated = {k: v for k, v in curated.items() if not k.startswith("_")}

    before = len(ds["places"])
    dropped = [p["name"] for p in ds["places"] if EXCLUDE.search(p["name"]) and p["id"] not in curated]
    keep_ids = {p["id"] for p in ds["places"] if p["name"] not in dropped or p["id"] in curated}
    ds["places"] = [p for p in ds["places"] if p["id"] in keep_ids]
    used = {x["placeId"] for x in ds.get("themePlaces", [])}
    lost = used - keep_ids
    if lost:  # 테마에 쓰인 장소는 빼지 않는다
        raise SystemExit(f"테마 장소가 제외 규칙에 걸림: {lost}")

    std = standard if standard is not None else standard_sets()
    filled = Counter()
    for p in ds["places"]:
        # 표준데이터: 비어 있는 칸만 채운다(TourAPI 상세가 먼저)
        m = std["museum"].find(p["name"], p["lat"], p["lng"]) if "museum" in std else None
        if m:
            for k, v in (("hoursRaw", museum_hours(m)), ("closedRaw", museum_closed(m)), ("feeRaw", museum_fee(m)),
                         ("overview", _plus(m.get("fcltyIntrcn")) or None), ("phone", m.get("operPhoneNumber") or m.get("phoneNumber"))):
                if v and not p.get(k):
                    p[k] = v
                    filled[k] += 1
        t = std["tourist"].find(p["name"], p["lat"], p["lng"]) if "tourist" in std else None
        if t and not p.get("overview") and (t.get("trrsrtIntrcn") or "").strip():
            p["overview"] = t["trrsrtIntrcn"].strip()
            filled["overview"] += 1
    for f in ds.get("festivals", []):
        r = std["festival"].find(f["name"], f["lat"], f["lng"]) if "festival" in std else None
        if not r:
            continue
        prog = _plus(r.get("fstvlCo"))
        if prog and not f.get("overview"):
            f["overview"] = f"주요 프로그램: {prog}"
            filled["festival-overview"] += 1
        if not f.get("eventPlace") and r.get("opar"):
            f["eventPlace"] = _plus(r["opar"])
        if not f.get("phone") and r.get("phoneNumber"):
            f["phone"] = r["phoneNumber"].strip()
        if not f.get("homepage") and clean_url(r.get("homepageUrl")):
            f["homepage"] = clean_url(r.get("homepageUrl"))
    added = add_unlisted(ds, std) if std else (0, 0)
    log.append(f"- 표준데이터({ds['region']}): {dict(filled)}, 새로 더함 박물관 {added[0]}곳·축제 {added[1]}개")

    linked = 0
    for p in ds["places"]:
        h = heritage.get(p["id"])
        if h:
            p["heritage"] = {"kind": h["kind"], "name": h["name"], "period": h.get("period"),
                             "designated": h.get("designated"), "url": h["url"]}
            p["interests"] = sorted(set(p.get("interests") or []) | heritage_interests(h))
            linked += 1
        else:
            p.pop("heritage", None)
        c = curated.get(p["id"])
        if c:
            for k in ("overview", "hoursRaw", "closedRaw", "feeRaw", "phone", "kidTip"):
                if c.get(k):
                    p[k] = c[k]
            if c.get("interests"):
                p["interests"] = sorted(set(p.get("interests") or []) | set(c["interests"]))
            p["verifiedAt"] = c.get("verifiedAt") or p.get("verifiedAt")
    ids = {p["id"] for p in ds["places"]}
    cards = [c for c in cur_file.get("askCards", []) if c.get("targetId") in ids or c.get("scope") == "THEME"]
    existing = {c["id"] for c in ds.get("askCards", [])}
    ds["askCards"] = [c for c in ds.get("askCards", []) if not c["id"].startswith("cur-")] + [c for c in cards if c["id"] not in existing or c["id"].startswith("cur-")]
    log.append(f"- 보강({ds['region']}): 제외 {before - len(ds['places'])}곳"
               + (f"(예: {', '.join(dropped[:5])})" if dropped else "")
               + f", 국가유산 연결 {linked}곳, 직접 확인 {sum(1 for p in ds['places'] if p['id'] in curated)}곳")
    return ds


def main() -> None:
    log: list[str] = []
    heritage = load(ROOT / "pipeline/heritage/heritage.json", {})
    standard = standard_sets()
    for f in sorted(glob.glob(str(ROOT / "app/src/main/assets/dataset/*.json"))):
        path = Path(f)
        text = path.read_text(encoding="utf-8")
        ds = json.loads(text)
        enrich(ds, heritage, None, log, standard)
        out = json.dumps(ds, ensure_ascii=False, indent=1) + "\n"
        if out != text:
            # 내용이 바뀌면 버전을 올려야 앱이 다시 가져온다(지금 시각 yyyyMMddHH, 같은 시간대면 +1)
            from datetime import datetime, timedelta, timezone
            now = int(datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d%H"))
            ds["datasetVersion"] = max(now, ds["datasetVersion"] + 1)
            out = json.dumps(ds, ensure_ascii=False, indent=1) + "\n"
            path.write_text(out, encoding="utf-8")
    print("\n".join(log))


if __name__ == "__main__":
    sys.exit(main())
