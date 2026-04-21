# Plan — 56K 크롤러 DB → Heritage DB 확장

**대상 Claude Code 인스턴스**: 56K `products` DB가 있는 서버
**목표**: 크롤러가 수집한 원본 제품 56,064개에서 BJT/MOSFET을 추려 heritage seed JSON으로 export → 이 git 저장소에 커밋 → 반대편 서버(Pro Server 운영 중)에서 import

---

## 0. 컨텍스트 — 이 작업을 왜 하는지

### 두 서버 구조

```
[SERVER-A: 크롤러 서버 — 이 작업의 실행 대상]
├─ 같은 git 저장소 (frwaler)
├─ data/papers.db 또는 설정 경로의 products DB (56,064행)
│  └─ 4개 사이트: infineon(20,264), ti(21,974), nexperia(7,563), vishay(6,263)
├─ html_archive/<site_id>/... (원본 HTML 보관)
└─ crawler/ + api/ (크롤러 + 읽기 API)

[SERVER-B: Pro 서비스 서버 — 결과물을 사용할 곳]
├─ 같은 git 저장소
├─ data/screening.db (heritage_parts 60개, BJT 32 + MOSFET 28)
└─ pro_server/ (스크리닝 서비스, Vercel 연동)
```

### 현재 문제

SERVER-B의 heritage DB(60개)는 스크리너가 "상용 부품을 우주등급 대비 얼마나 위험한지" 판정할 때 참고 샘플이 너무 적음. 반면 SERVER-A에는 56K개 원본 제품 데이터가 있으나 **타입 분류가 안 됨** (BJT인지 MOSFET인지 LDO인지 섞여 있음).

### 이 작업의 산출물

`pro_server/data/heritage_*_commercial.json` 시드 파일 2~4개:
- `heritage_bjt_commercial.json` — BJT 분류된 상용 부품
- `heritage_mosfet_commercial.json` — MOSFET 분류된 상용 부품
- (선택) `heritage_ldo_commercial.json`, `heritage_opamp_commercial.json`

이 JSON을 git push → SERVER-B에서 git pull → import 스크립트로 `heritage_parts` 테이블에 INSERT → 스크리너가 즉시 활용.

---

## 1. Phase 0 — 사전 조사 (30분)

작업 시작 전 DB 실제 구조 확인. 아래를 실행해서 결과를 보고 Phase 1~3 코드를 조정할 것.

### 1-1. DB 경로 확인

```bash
# api/settings.py의 database_url 확인
cat api/settings.py

# 환경변수 확인 (CRAWLER_DATABASE_URL 있을 수 있음)
env | grep -i crawler

# 실제 파일 존재 확인
ls -la <위에서 찾은 경로>
```

### 1-2. products 테이블 스키마 파악

```bash
python -c "
import sqlite3
conn = sqlite3.connect('<DB_PATH>')
c = conn.cursor()
print('=== 스키마 ===')
for row in c.execute('PRAGMA table_info(products)'):
    print(row)
print('=== 사이트별 건수 ===')
for row in c.execute('SELECT site_id, COUNT(*) FROM products GROUP BY site_id'):
    print(row)
print('=== category 분포 (상위 30) ===')
for row in c.execute('SELECT category, COUNT(*) as c FROM products GROUP BY category ORDER BY c DESC LIMIT 30'):
    print(row)
print('=== 샘플 3건 ===')
import json
for row in c.execute('SELECT id, site_id, name, brand, category, specs FROM products LIMIT 3'):
    print(row[0:5])
    if row[5]:
        try:
            specs = json.loads(row[5])
            print('  specs keys:', list(specs.keys())[:20])
        except: pass
"
```

### 1-3. 질문에 답하기 (코드 구현 전)

- [ ] `specs` 컬럼에 **이미 파라미터가 파싱되어 있는지?** (예: `{"VCEO":"50V","IC":"0.8A"}`)
- [ ] `category` 값이 **BJT/MOSFET/LDO 같은 키워드**를 포함하는지? (예: "Bipolar Transistors")
- [ ] `name` 필드가 부품명(MPN)을 담는지, 아니면 설명을 담는지?
- [ ] `html_path`는 실제로 디스크에 존재하는지? (없으면 재파싱 불가능)
- [ ] 사이트별로 `specs` 스키마가 제각각인지? (예: TI는 "VCEO", Infineon은 "V(CEO)")

위 답에 따라 Phase 2의 구현이 달라짐.

---

## 2. Phase 1 — 부품 타입 분류 (하이브리드)

### 2-1. 1차: 규칙 기반 분류기 (빠름)

`scripts/classify_products.py` 새로 작성:

```python
# -*- coding: utf-8 -*-
"""56K 제품을 부품 타입별로 분류."""
import json
import re
import sqlite3
import sys

# 분류 규칙: category/name 키워드 우선순위대로 매칭
RULES = [
    # (part_type, patterns — 소문자로 매칭)
    ("mosfet", [
        r"\bmosfet\b", r"\bn-channel\b", r"\bp-channel\b",
        r"\benhancement mode\b", r"\bdepletion mode\b",
        r"rds\(on\)", r"\bvgs\b",
    ]),
    ("bjt", [
        r"\bbjt\b", r"\bbipolar\b",
        r"\b(npn|pnp)\b",
        r"\bsmall signal transistor\b",
        r"\bpower transistor\b",  # 주의: power mosfet도 걸리니 mosfet 먼저 매칭
    ]),
    ("ldo", [
        r"\bldo\b", r"\blinear regulator\b",
        r"\blow.?dropout\b",
    ]),
    ("opamp", [
        r"\bop.?amp\b", r"\boperational amplifier\b",
    ]),
    ("diode", [
        r"\bdiode\b", r"\bschottky\b", r"\bzener\b", r"\brectifier\b",
    ]),
    ("logic", [
        r"\blogic gate\b", r"\bnand\b", r"\bnor\b", r"\band gate\b",
    ]),
]

def classify(row) -> str:
    """row: dict with name, category, description, brand"""
    haystack = " ".join(
        str(row.get(k) or "").lower()
        for k in ("category", "name", "description", "brand")
    )
    for part_type, patterns in RULES:
        for p in patterns:
            if re.search(p, haystack):
                return part_type
    return "unknown"


def main(db_path: str, out_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, site_id, name, brand, category, description, url, specs "
        "FROM products"
    ).fetchall()

    counts = {}
    classified = []
    for r in rows:
        pt = classify(dict(r))
        counts[pt] = counts.get(pt, 0) + 1
        if pt != "unknown":
            classified.append({**dict(r), "part_type": pt})

    print("=== 분류 결과 ===")
    for pt, c in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {pt:10s}: {c:,}")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(classified, f, ensure_ascii=False, indent=2)
    print(f"\n저장: {out_path} ({len(classified):,}건)")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
```

**실행 & 검증**:
```bash
python scripts/classify_products.py <DB_PATH> /tmp/classified.json
# 출력 예상: mosfet 8000, bjt 1500, diode 3000, ldo 500, unknown 40000
```

**체크포인트**:
- `unknown`이 전체의 70% 넘으면 → 규칙에 빠진 키워드 있음, 샘플 보고 규칙 추가
- BJT가 너무 적으면(<500) → power transistor 매칭 우선순위 조정
- 분류 결과를 눈으로 검증 (각 타입 20건씩 출력해서 오분류 체크)

### 2-2. 2차 (선택): LLM 검증 — `unknown` 줄이기

규칙으로 놓친 것만 Gemini에게 한 번씩 물어봄. `.env`의 `GEMINI_KEY` 활용.

- 비용 고려: `unknown` 4만개 × 호출당 비용
- 배치 프롬프트: 10~20개 제품명을 한 번에 보내고 JSON array로 분류 받기
- 결과를 캐시 (같은 제품 두 번 안 묻게)

이건 1차 결과 보고 필요하면 추가.

---

## 3. Phase 2 — 파라미터 추출

### 3-1. 입력 데이터 유형 판별 (Phase 0 결과로 결정)

**Case A**: `specs` 컬럼에 이미 key-value가 파싱되어 있음
→ 키 이름 매핑만 하면 됨 (아래 매핑 테이블 사용)

**Case B**: `specs`는 비어있고 `html_path`에 원본 HTML만 있음
→ 사이트별 HTML 파서 필요 (Infineon/TI/Nexperia/Vishay 각각)

**Case C**: 둘 다 부실함
→ 제품명(name)에서 정규식으로라도 추출 (정확도 낮음)

### 3-2. 파라미터 키 매핑 테이블

사이트/벤더마다 스펙 키 이름이 다름. 아래 매핑을 기준으로 정규화.

**BJT → `BjtParameters` (pro_server/schemas.py:49)**:
| 표준 키 | 단위 | 매칭 후보 (정규식, 대소문자 무시) |
|--------|------|------|
| `vceo_v` | V | `V\(?CEO\)?`, `Collector.?Emitter.?Voltage`, `VCEO` |
| `vcbo_v` | V | `V\(?CBO\)?`, `Collector.?Base.?Voltage` |
| `vebo_v` | V | `V\(?EBO\)?`, `Emitter.?Base.?Voltage` |
| `ic_max_a` | A | `I\(?C\)?\s*(max|continuous)`, `Collector.?Current` |
| `hfe_min` / `hfe_max` | — | `hFE`, `DC Current Gain`, `Beta` |
| `ft_hz` | Hz | `f\(?T\)?`, `Transition Frequency`, `Gain Bandwidth` |
| `pd_w` | W | `P\(?D\)?`, `Power Dissipation`, `Total Device Dissipation` |
| `tj_max_c` | °C | `T\(?J\)?\s*max`, `Junction Temperature` |
| `polarity` | NPN/PNP | `Polarity`, `Type` — "NPN" or "PNP" 검출 |
| `package` | str | `Package`, `Case`, `Housing` |

**MOSFET → `MosfetParameters` (pro_server/schemas.py:64)**:
| 표준 키 | 단위 | 매칭 후보 |
|--------|------|------|
| `bvdss_v` | V | `V\(?DS\)?`, `BVDSS`, `Drain.?Source.?Voltage`, `V\(BR\)DSS` |
| `vgs_th_v` | V | `V\(?GS\(th\)\)?`, `Gate.?Threshold.?Voltage`, `VGS\(th\)` |
| `rds_on_ohm` | Ω | `R\(?DS\(on\)\)?`, `R_DS\(on\)`, `On.?Resistance` |
| `id_max_a` | A | `I\(?D\)?\s*(max|continuous)`, `Continuous.?Drain.?Current` |
| `idss_a` | A | `I\(?DSS\)?`, `Zero.?Gate.?Voltage.?Drain.?Current` |
| `qg_c` | C | `Q\(?G\)?`, `Gate.?Charge.?Total`, `Total Gate Charge` |
| `pd_w` | W | (BJT와 동일) |
| `tj_max_c` | °C | (BJT와 동일) |
| `polarity` | N-channel/P-channel | `Channel Type`, `Configuration` |
| `package` | str | (BJT와 동일) |

### 3-3. 단위 정규화 함수

벤더 스펙은 `"50 V"`, `"800mA"`, `"1.8W"`, `"1.0k"`, `"6n"` (nC = 6e-9), `"300 MHz"` 등 섞여 있음. 파싱 유틸 필요:

```python
import re
UNIT_MULT = {
    "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3,
    "": 1.0, "k": 1e3, "M": 1e6, "G": 1e9,
}

def parse_numeric(s: str) -> float | None:
    if s is None: return None
    s = str(s).strip().replace(",", "")
    m = re.match(r"^([+-]?\d*\.?\d+)\s*([pnuµmkMG]?)", s)
    if not m: return None
    val = float(m.group(1))
    return val * UNIT_MULT.get(m.group(2), 1.0)
```

**주의**: 
- mA → A 변환은 자동으로 되지만, "0.8A"와 "800mA"는 같은 값 (ic_max_a=0.8)
- hFE는 단위 없음 (raw float)
- `tj_max_c`는 섭씨 그대로
- Hz 는 `MHz/GHz` 접두어 주의 — `300MHz` → `3e8`

### 3-4. 추출 스크립트 스켈레톤

```python
# scripts/extract_params.py
import json
from pathlib import Path

def extract_bjt(row) -> dict:
    specs = json.loads(row["specs"]) if row.get("specs") else {}
    params = {}
    # specs key를 표준 키로 매핑 (regex 매칭)
    for std_key, patterns in BJT_KEY_MAP.items():
        for raw_key, raw_val in specs.items():
            if any(re.search(p, raw_key, re.I) for p in patterns):
                params[std_key] = parse_numeric(raw_val) if std_key.endswith(("_v","_a","_w","_hz","_c")) else raw_val
                break
    # polarity: name/description에서 NPN/PNP 검출
    if "polarity" not in params:
        text = (row.get("name","") + " " + row.get("description","")).upper()
        if "NPN" in text: params["polarity"] = "NPN"
        elif "PNP" in text: params["polarity"] = "PNP"
    return params

def extract_mosfet(row) -> dict:
    # 동일 패턴, MOSFET_KEY_MAP 사용
    ...
```

---

## 4. Phase 3 — Heritage seed JSON 생성

### 4-1. MPN 결정 로직

heritage_parts는 MPN이 PK. 어떻게 얻을지:

1. **`external_id` 우선** — 벤더 내부 PN (예: Infineon의 "IRFB4115PBF")
2. **`name`에서 추출** — 제품명 앞부분이 MPN인 경우 많음 (`"IRF540N N-Channel MOSFET"` → `IRF540N`)
3. **URL에서 추출** — 마지막 path segment (`.../product/irf540n` → `IRF540N`)

정규식으로 MPN 후보 추출 (영문+숫자+하이픈, 길이 5~20):
```python
def extract_mpn(row) -> str | None:
    for candidate in [row.get("external_id"), _first_token(row.get("name")), _url_tail(row.get("url"))]:
        if candidate and re.match(r"^[A-Z0-9][A-Z0-9\-]{3,19}$", candidate.upper()):
            return candidate.upper()
    return None
```

**중복 제거**: 같은 MPN이 여러 사이트에 있으면 → spec 채움률이 높은 쪽을 선택.

### 4-2. 출력 스키마 (정확히 맞춰야 함!)

`pro_server/data/heritage_bjt_seed.json`과 동일 구조. 이 저장소에서 이미 사용 중.

```json
[
  {
    "mpn": "2N2222A",
    "manufacturer": "ON Semiconductor",
    "part_type": "bjt",
    "qual_level": null,
    "parameters": {
      "vceo_v": 40.0,
      "vcbo_v": 75.0,
      "vebo_v": 6.0,
      "ic_max_a": 0.8,
      "hfe_min": 100.0,
      "hfe_max": 300.0,
      "ft_hz": 300000000.0,
      "pd_w": 1.8,
      "tj_max_c": 200.0,
      "polarity": "NPN",
      "package": "TO-18"
    },
    "heritage_notes": "Commercial-grade NPN small-signal BJT. Sourced from TI product page, crawled 2026-04-20.",
    "source_url": "https://www.ti.com/product/..."
  }
]
```

**필드별 규칙**:
- `qual_level`: 상용 부품은 **반드시 `null`** (우주등급이 아니니까)
- `part_type`: `"bjt"` | `"mosfet"` | `"ldo"` | `"opamp"` | `"diode"` (소문자)
- `parameters`: 스키마는 `pro_server/schemas.py`의 `BjtParameters` / `MosfetParameters` 클래스를 **그대로 따라야 함**. 없는 필드는 생략 (null로 넣어도 됨)
- `heritage_notes`: 출처 명시 — `"Commercial-grade, crawled from {site_id}, {date}"` 형식
- `source_url`: 제품 상세 페이지 URL (`products.url`)

### 4-3. 파일 분리 & 샘플링 전략

전체 몇만 건을 한 파일에 넣으면 git diff가 힘듦. 제안:

- **샘플링**: 각 타입별 파라미터 추출 성공률이 가장 높은 상위 N개만 seed로 포함 (예: BJT 500개, MOSFET 500개)
- 파일명: `pro_server/data/heritage_{part_type}_commercial.json`
- 나머지는 별도 `heritage_{part_type}_commercial_full.jsonl.gz` (압축, 검토용)

샘플링 기준 (채움률 score):
```python
def fill_score(params: dict) -> float:
    total = len(EXPECTED_KEYS[part_type])
    filled = sum(1 for k in EXPECTED_KEYS[part_type] if params.get(k) is not None)
    return filled / total
# 0.7 이상인 것만 채택
```

---

## 5. Phase 4 — Git 커밋 & SERVER-B에서 import

### 5-1. SERVER-A (이 작업) — 커밋

```bash
cd <repo>
git checkout -b hackerton  # 또는 현재 브랜치 확인 (hackerton이 맞음)
git add pro_server/data/heritage_bjt_commercial.json
git add pro_server/data/heritage_mosfet_commercial.json
git add scripts/classify_products.py scripts/extract_params.py scripts/export_heritage_seed.py
# docs/ 아래 작업 노트도 함께
git add docs/HERITAGE_EXPANSION_REPORT_<DATE>.md  # 아래 §6 참고
git commit -m "Add commercial heritage seeds from 56K crawler DB

- Classified N products into BJT/MOSFET/LDO from 4 vendor sites
- Extracted parameters with fill rate ≥0.7
- BJT: X entries, MOSFET: Y entries
"
git push
```

**주의**: 56K 원본 DB (`papers.db` / `products.db`)는 **절대 커밋하지 말 것**. `.gitignore`에 이미 `*.db`가 있는지 확인하고 없으면 추가.

### 5-2. SERVER-B에서 import (이미 작동 중인 서비스 서버)

SERVER-B에서 실행할 import 스크립트도 함께 만들어 주면 좋음. `scripts/import_commercial_heritage.py`:

```python
# -*- coding: utf-8 -*-
"""Commercial heritage seed JSON을 screening.db에 INSERT OR IGNORE."""
import json, sqlite3, sys
from pathlib import Path

SEED_DIR = Path(__file__).parent.parent / "pro_server" / "data"
DB_PATH = Path(__file__).parent.parent / "data" / "screening.db"

def main():
    conn = sqlite3.connect(DB_PATH)
    inserted = 0
    for fn in ["heritage_bjt_commercial.json", "heritage_mosfet_commercial.json"]:
        path = SEED_DIR / fn
        if not path.exists():
            print(f"skip: {fn} not found"); continue
        data = json.loads(path.read_text())
        for p in data:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO heritage_parts "
                    "(mpn, manufacturer, part_type, qual_level, parameters, heritage_notes, source_url) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (p["mpn"], p["manufacturer"], p["part_type"], p.get("qual_level"),
                     json.dumps(p["parameters"], ensure_ascii=False),
                     p.get("heritage_notes",""), p.get("source_url","")),
                )
                if conn.total_changes > 0:
                    inserted += 1
            except Exception as e:
                print(f"  error on {p.get('mpn')}: {e}")
    conn.commit()
    print(f"inserted: {inserted}")

if __name__ == "__main__":
    main()
```

SERVER-B 실행:
```bash
git pull
.venv/bin/python scripts/import_commercial_heritage.py
sudo systemctl restart frwaler-pro  # Pro Server 재시작 (캐시된 heritage_parts 새로고침)
```

---

## 6. 작업 완료 후 보고서

`docs/HERITAGE_EXPANSION_REPORT_<YYYY-MM-DD>.md` 작성. 포함할 내용:

- [ ] 실행 환경 (DB 경로, 총 건수)
- [ ] 분류 결과 표 (타입별 건수)
- [ ] 파라미터 채움률 분포 히스토그램 (텍스트로)
- [ ] 샘플링 후 최종 seed에 들어간 건수
- [ ] **오분류 사례 10건** (손으로 검토한 것 — 규칙 개선용)
- [ ] 알려진 한계 (예: 벤더 X는 spec이 없어서 name에서만 추출함)
- [ ] SERVER-B에서 import 시 예상 영향 (heritage_parts 총 건수 변화)

---

## 7. 체크리스트 (작업 진행 순서)

### Phase 0 — 조사
- [ ] products DB 경로 확인 & 접근 가능
- [ ] 스키마 조회, 사이트별 건수 확인
- [ ] specs 컬럼 상태 확인 (파싱 완료 / 비어있음)
- [ ] category/name 샘플 10개씩 4개 사이트 → 분류 패턴 감 잡기

### Phase 1 — 분류
- [ ] `scripts/classify_products.py` 작성
- [ ] 실행 → 분포 확인
- [ ] `unknown` 비율이 50% 넘으면 규칙 추가
- [ ] BJT/MOSFET 각 100건 눈으로 검증 (오분류 기록)

### Phase 2 — 파라미터
- [ ] BJT 10건, MOSFET 10건 먼저 수동으로 추출 테스트 (스크립트 없이)
- [ ] `scripts/extract_params.py` 작성
- [ ] 채움률 분포 확인 — 0.7 이상이 각 타입별 300건 넘어야 유용

### Phase 3 — Export
- [ ] `scripts/export_heritage_seed.py` 작성 (classify + extract + format + filter)
- [ ] `heritage_bjt_commercial.json` 생성, JSON valid 체크
- [ ] `heritage_mosfet_commercial.json` 생성
- [ ] 10건 무작위 추출 → 필드 검증 (단위 올바른지, polarity 정확한지)

### Phase 4 — 배포
- [ ] `.gitignore`에 `*.db` 있는지 확인
- [ ] `scripts/import_commercial_heritage.py` 작성
- [ ] 커밋, push (hackerton 브랜치)
- [ ] 보고서 작성

---

## 8. 자주 실수하는 포인트 (주의)

1. **단위 변환 실수**: `300MHz` → `300` 그대로 쓰면 안 됨. `300e6` = `3e8`. 테스트 꼭 할 것.
2. **폴라리티 혼동**: MOSFET에서 "N-channel"을 NPN으로, BJT의 NPN을 N-channel로 잘못 쓰는 실수 빈번. 타입별 분기 필요.
3. **qual_level 혼동**: JAN/JANS/JANSR 같은 접두어가 MPN에 있다고 자동으로 qual_level 넣지 말 것. 상용 크롤링 데이터는 모두 `null`이 기본.
4. **중복 MPN**: `2N2222A` 같은 표준 MPN은 여러 벤더가 만듦. `(mpn, manufacturer)`가 실질 키. INSERT OR IGNORE 쓰기 전에 기존 SERVER-B의 `heritage_parts`와 중복 체크 로직 필요.
5. **SERVER-A에서 test 금지**: SERVER-A엔 `screening.db`가 없음. import 스크립트는 작성만 하고, 실제 실행은 SERVER-B에서 한다.
6. **HTML 재파싱 비용**: 56K HTML을 전부 재파싱하면 오래 걸림. `specs` 컬럼 먼저 활용하고, 필요한 경우만 HTML 재방문.
7. **폴라리티 정보 누락**: `polarity`가 없으면 스크리너 정확도 떨어짐. name/description에서도 못 뽑으면 그 항목은 seed에서 제외 (품질 우선).

---

## 9. 참고 — SERVER-B 스키마 소스 파일

(같은 git 저장소이므로 SERVER-A에서도 이 파일들 참조 가능)

- `pro_server/schemas.py:49-75` — `BjtParameters`, `MosfetParameters` (정답지)
- `pro_server/data/heritage_bjt_seed.json` — 현재 seed (포맷 레퍼런스)
- `pro_server/data/heritage_mosfet_seed.json` — 현재 seed
- `pro_server/routers/screening.py` — 스크리너 로직 (heritage_parts 어떻게 사용하는지)
- `pro_server/services/scorer.py` — 점수 계산 (heritage match 기여)

---

## 10. 범위 밖 (이번 작업에서는 안 함)

- Linear Regulator (LDO), Op-Amp 지원 → 이는 SERVER-B 쪽에서 **스키마 추가 + scorer 확장**이 선행되어야 함. 이번엔 BJT/MOSFET만 우선.
- Gemini 기반 자동 분류 (Phase 1-2) → 규칙만으로 부족하다고 판단될 때만 추가.
- 전체 FTS5 검색 인덱스, 추천 엔진 → 이후 Phase.
- Named Tunnel 설정 → 인프라 작업, 별도 트랙.

---

## 11. 질문 / 블로커 생기면

작성자(이 plan)에게 확인 필요한 것:
- DB 스키마가 위 가정과 완전히 다를 때 (예: `products` 테이블이 아니라 `items` 테이블)
- `specs` 컬럼이 JSON이 아닌 다른 포맷일 때
- 4개 사이트 중 일부가 완전히 다른 구조일 때

SERVER-B의 담당자(사용자)에게 확인:
- seed 파일을 한 번에 push할지, 사이트별로 분할 PR할지
- `heritage_parts`에 새 컬럼(`source_site_id`, `crawled_at`) 추가할지 — 추적용으로 유용함
- 분류 오분류 허용 기준 (정확도 95% vs 90%)

---

**이 plan 하나만 보고 전체 작업이 가능하도록 설계됨. Phase 0 결과에 따라 2~4일 소요 예상.**
