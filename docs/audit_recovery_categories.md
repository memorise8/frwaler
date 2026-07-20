# 수집 대상 1,994건 — 실행가능 카테고리 분류 & osti.gov 데이터 흐름

**문서 작성**: 2026-05-25
**데이터 출처**: `data/audit/coverage_report.csv`, `data/audit/site_recovery_plan.csv` (각 1,994행)
**관련 산출물**: `client_report.xlsx` (요약 시트의 상위 분류를 actionable 기준으로 세분화한 것)

> 이 문서는 UI 작업과 별개로, 크롤 대상 사이트를 '회원가입/VPN/사이트터짐' 등 실제 조치 단위로 재분류한 분석 기록이다.

---

## 1. 분류 신호 (진단 컬럼)

`coverage_report.csv`의 다음 컬럼 조합으로 각 엔트리를 **상호배타** 카테고리에 1:1 배정한다.

| 컬럼 | 의미 | 주요 값 |
|---|---|---|
| `render_class` | 페이지 형태 판정 | static_list 1177 / auth_blocked 248 / robots_blocked 222 / spa_likely 211 / dead 116 / unknown 20 |
| `block_reason` | 차단 원인 | cloudflare_challenge 194 / cloudflare_403 44 / still_403 7 / other_404 1 |
| `http_status` | 응답 코드 | 200 1398 / 403 248 / 405 25 / 404 22 / 무응답 289 |
| `robots_ok` | robots.txt 허용 | true 1772 / false 222 |
| `analyzer_status` | 셀렉터 추론 결과 | ok 1304 / fail 85 / partial 1 |

**핵심**: `auth_blocked` 248건은 전부 HTTP 403이며, `block_reason`으로 다시 갈린다 — 이것이 '회원가입처럼 보이던' 차단의 실체다 (3절 참조).

---

## 2. 카테고리 분류 결과 (1,994건)

| 코드 | 카테고리 | 건수 | 비율 | 고유도메인 | 필요 조치 |
|---|---|---:|---:|---:|---|
| A | 즉시 수집 가능 | 1,100 | 55.2% | 510 | 없음 (정상) |
| J | SPA/JS 렌더 필요(대부분 자동) | 202 | 10.1% | 110 | 헤드리스 렌더링 — 이미 analyzer ok, 거의 자동 |
| C | Cloudflare JS 챌린지 | 194 | 9.7% | 45 | Playwright로 JS 챌린지 통과 (사람·VPN 불필요) |
| E | robots.txt 정책 차단 | 222 | 11.1% | 85 | 우회 안 함 — 운영자에 데이터셋 요청 |
| F | 사이트 터짐/응답불가 | 116 | 5.8% | 54 | URL 만료·서버다운 → archive.org 또는 제외 |
| H | 셀렉터 보강 필요(LLM 재추론) | 85 | 4.3% | 49 | LLM 셀렉터 재추론 |
| D | IP·지역 차단 (VPN/프록시) | 53 | 2.7% | 11 | Residential Proxy / VPN (다른 IP) |
| G | 미분류/수동점검 | 20 | 1.0% | 2 | 사람 수동 점검 |
| B | 회원가입/로그인 필요(추정) | 1 | 0.1% | 1 | 계정 생성 후 쿠키/토큰 주입 |
| I | 부분 수집 가능 | 1 | 0.1% | 1 | 보조 보강 |
| | **합계** | **1,994** | 100% | | |

**3개 메타그룹**:
- 🟢 **즉시/거의 수집 가능** (A+J): 1,302건
- 🟡 **기술 보강으로 회복** (H+I+G): 106건
- 🔴 **사람·인프라·정책 개입 필요** (C+D+B+E+F): 586건

---

## 3. ⚠️ 핵심 정정 — '회원가입 246'의 실체

이전 `site_recovery_plan.csv` / 대시보드 funnel의 **'🔐 사람개입필요-회원가입 246'**은 로그인 벽이 아니라 전부 **HTTP 403 차단**이었다. 신호로 분해하면:

- **Cloudflare JS 챌린지 194** → Playwright로 해결 (사람 개입 0)
- **IP·지역 차단 53** → VPN/프록시로 해결
- 자동탐지된 진짜 로그인 게이트 → **1건(sciencedirect)**

> 403은 '로그인하라'가 아니라 '차단됐다'는 신호다. 로그인 벽은 보통 200(로그인 폼) 또는 401로 응답하므로, **현재 데이터만으로는 회원가입 필요 사이트를 자동 식별할 수 없다.** 회원가입 카테고리를 제대로 만들려면 알려진 계정형 도메인을 수동 태깅해야 한다.

---

## 4. 조치 필요 그룹 — 사이트 전체 목록

### D. IP·지역 차단 (VPN/프록시) — 53건 / 11개 도메인
*조치: Residential Proxy / VPN (다른 IP)*

| 도메인 | 건수 |
|---|---:|
| www.gov.il | 37 |
| mentalhealthcommission.ca | 3 |
| www.education.gouv.fr | 3 |
| repo.lib.duth.gr | 3 |
| dpcpsi.nih.gov | 1 |
| datos.gob.mx | 1 |
| www.mch.govt.nz | 1 |
| www.interieur.gouv.fr | 1 |
| www.ameslab.gov | 1 |
| enac.hal.science | 1 |
| culture.gov.gr | 1 |

### B. 회원가입/로그인 필요(추정) — 1건 / 1개 도메인
*조치: 계정 생성 후 쿠키/토큰 주입*

| 도메인 | 건수 |
|---|---:|
| www.sciencedirect.com | 1 |

### F. 사이트 터짐/응답불가 — 116건 / 54개 도메인
*조치: URL 만료·서버다운 → archive.org 또는 제외*

HTTP 상태: `무응답`=67, `405`=25, `404`=22, `521`=1, `500`=1

| 도메인 | 건수 |
|---|---:|
| www.justice.govt.nz | 17 |
| www.ag.gov.au | 8 |
| www.agroscope.admin.ch | 6 |
| infoscience.epfl.ch | 6 |
| nemertes.library.upatras.gr | 6 |
| www.defence.gov.au | 5 |
| openscience.si | 4 |
| www.finance.gov.au | 3 |
| www.acma.gov.au | 3 |
| www.nhmrc.gov.au | 3 |
| www.hud.govt.nz | 3 |
| ir.lib.uth.gr | 3 |
| www.publicservice.govt.nz | 2 |
| www.agriculture.gov.au | 2 |
| www.dfat.gov.au | 2 |
| www.health.gov.au | 2 |
| www.usda.gov | 2 |
| www.bmluk.gv.at | 2 |
| forwit.at | 2 |
| www.nia.nih.gov | 1 |
| order.nia.nih.gov | 1 |
| www.fsc.go.kr | 1 |
| www.nrc.re.kr | 1 |
| www.mss.go.kr | 1 |
| www.sm.ee | 1 |
| www.mps.gov.cn | 1 |
| www.bmftr.bund.de | 1 |
| www.bmfwf.gv.at | 1 |
| www.kcc.go.kr | 1 |
| www.moleg.go.kr | 1 |
| kum.dk | 1 |
| www.industry.gov.au | 1 |
| www.dva.gov.au | 1 |
| www.dss.gov.au | 1 |
| www.mintur.gob.es | 1 |
| zenodo.org | 1 |
| www.kostat.go.kr | 1 |
| www.cpb.nl | 1 |
| www.asser.nl | 1 |
| www.aims.gov.au | 1 |
| www.spo.go.kr | 1 |
| www.police.go.kr | 1 |
| www.mpva.go.kr | 1 |
| www.rda.go.kr | 1 |
| www.motie.go.kr | 1 |
| www.mcee.go.kr | 1 |
| www.moj.go.kr | 1 |
| www.efd.admin.ch | 1 |
| www.fss.or.kr | 1 |
| noc.ac.uk | 1 |
| misportal.jlab.org | 1 |
| www.ehess.fr | 1 |
| www.gwangju.go.kr | 1 |
| bdap-opendata.rgs.mef.gov.it | 1 |

### C. Cloudflare JS 챌린지 — 194건 / 45개 도메인
*조치: Playwright로 JS 챌린지 통과 (사람·VPN 불필요)*

| 도메인 | 건수 |
|---|---:|
| www.government.se | 50 |
| www.treasury.govt.nz | 39 |
| www.health.govt.nz | 22 |
| pure.knaw.nl | 12 |
| www.dpmc.govt.nz | 6 |
| www.aihw.gov.au | 5 |
| www.cigionline.org | 5 |
| www.commerce.gov | 4 |
| scc-ccn.ca | 3 |
| www.oeaw.ac.at | 3 |
| www.fraserinstitute.org | 2 |
| intermin.fi | 2 |
| tietokayttoon.fi | 2 |
| www.cda-amc.ca | 2 |
| iris.unitn.it | 2 |
| www.parliament.uk | 2 |
| researchportal.sckcen.be | 2 |
| www.anl.gov | 2 |
| www.publichealth.ie | 2 |
| inl.gov | 2 |
| abzena.com | 1 |
| www.ots.at | 1 |
| www.interior.gob.es | 1 |
| erhvervsstyrelsen.dk | 1 |
| alliancebioversityciat.org | 1 |
| defmin.fi | 1 |
| www.iisd.org | 1 |
| www.mur.gov.it | 1 |
| oikeusministerio.fi | 1 |
| okm.fi | 1 |
| vm.fi | 1 |
| tem.fi | 1 |
| ym.fi | 1 |
| um.fi | 1 |
| stm.fi | 1 |
| data.commerce.gov | 1 |
| data.london.gov.uk | 1 |
| flore.unifi.it | 1 |
| hrbopenresearch.org | 1 |
| www.clingendael.org | 1 |
| pure.prinsesmaximacentrum.nl | 1 |
| committees.parliament.uk | 1 |
| post.parliament.uk | 1 |
| lordslibrary.parliament.uk | 1 |
| cris.iucc.ac.il | 1 |

### E. robots.txt 정책 차단 — 222건 / 85개 도메인
*조치: 우회 안 함 — 운영자에 데이터셋 요청*

| 도메인 | 건수 |
|---|---:|
| www.pbc.gov.cn | 23 |
| www.osti.gov | 16 |
| www.kei.re.kr | 11 |
| www.research-collection.ethz.ch | 8 |
| publica.fraunhofer.de | 5 |
| www.bmleh.de | 5 |
| researchrepository.ucd.ie | 5 |
| hal.inrae.fr | 5 |
| dspace.ut.ee | 4 |
| www.drugsandalcohol.ie | 4 |
| cora.ucc.ie | 4 |
| cnrs.hal.science | 4 |
| www.bundeswirtschaftsministerium.de | 4 |
| www.bok.or.kr | 3 |
| www.kli.re.kr | 3 |
| dspace.emu.ee | 3 |
| opengov.seoul.go.kr | 3 |
| nrc-publications.canada.ca | 3 |
| science-libraries.canada.ca | 3 |
| www.earth-prints.org | 3 |
| researchrepository.ul.ie | 3 |
| ruomoplus.lib.uom.gr | 3 |
| brgm.hal.science | 3 |
| mnhn.hal.science | 3 |
| www.bmi.bund.de | 3 |
| www.comwel.or.kr | 2 |
| www.nhis.or.kr | 2 |
| www.kiep.go.kr | 2 |
| dtic.dimensions.ai | 2 |
| www.etis.ee | 2 |
| www.daejeon.go.kr | 2 |
| ostrnrcan-dostrncan.canada.ca | 2 |
| www.cstb.fr | 2 |
| www.sintef.no | 2 |
| book.ioj.go.kr | 2 |
| ephe.hal.science | 2 |
| pasteur.hal.science | 2 |
| insee.hal.science | 2 |
| ofb.hal.science | 2 |
| enssib.hal.science | 2 |
| asnr.hal.science | 2 |
| inrap.hal.science | 2 |
| bnf.hal.science | 2 |
| anr.hal.science | 2 |
| ifp.hal.science | 2 |
| academie-sciences.hal.science | 2 |
| centralesupelec.hal.science | 2 |
| ehess.hal.science | 2 |
| hal-ciheam.iamm.fr | 2 |
| enva.hal.science | 2 |
| college-de-france.hal.science | 2 |
| cyu.hal.science | 2 |
| grenoble-em.hal.science | 2 |
| www.kice.re.kr | 1 |
| scholar.google.com | 1 |
| repository.cern | 1 |
| repository.graduateinstitute.ch | 1 |
| dataverse.ada.edu.au | 1 |
| apo.ansto.gov.au | 1 |
| openresearch-repository.anu.edu.au | 1 |
| info.daegu.go.kr | 1 |
| www.sejong.go.kr | 1 |
| www.gyeongnam.go.kr | 1 |
| gnews.gg.go.kr | 1 |
| www.bai.go.kr | 1 |
| search.abs.gov.au | 1 |
| nrc-digital-repository.canada.ca | 1 |
| nioz.on.worldcat.org | 1 |
| openaccess.inaf.it | 1 |
| www.rivm.nl | 1 |
| www.nifu.no | 1 |
| rdr.kuleuven.be | 1 |
| repository.wodc.nl | 1 |
| research.thea.ie | 1 |
| sc-data.emsl.pnnl.gov | 1 |
| www.cmi.no | 1 |
| data.gov.au | 1 |
| dair.dias.ie | 1 |
| knowledge.barnardos.ie | 1 |
| dataexplorer.abs.gov.au | 1 |
| pergamos.lib.uoa.gr | 1 |
| dias.library.tuc.gr | 1 |
| dspace.lib.uom.gr | 1 |
| www.lenus.ie | 1 |
| www.datenportal.bmbf.de | 1 |

---

## 5. osti.gov 데이터 흐름 (별도 조사)

**결론: osti.gov는 직접 크롤하지 않는다(robots 차단). 전부 NETL(DOE) 크롤러를 거쳐 간접 수집된다.**

| 경로 | 대상 | 결과 |
|---|---|---|
| ① 직접 osti.gov 검색 (입력 16건) | `osti.gov/pages/search/...`, `osti.gov/dataexplorer/search/...` (연구소별: LBNL·ORNL·ANL·LANL·LLNL·BNL·SLAC·PNNL·Sandia·ARM·DOE Geothermal 등) | **전부 robots_blocked → 다운로드 0** |
| ② NETL 크롤러 경유 | `netl.doe.gov` → OSTI API → `osti.gov/servlets/purl/{id}` | **255 문서, PDF 226개 저장** |

**경로 ② 메커니즘** (`crawler/sites/custom/netl-doe-gov-fecm-external-r-and-.py`, site_id `netl-doe-gov-fecm-external-r-and-`):
1. **목록**: `netl.doe.gov/FECM-External-R-and-D-Final-Technical-Reports` (301→HGEO) 내 iframe `netl.doe.gov/projects/project-Final-Report-List.aspx` — ~492건이 DataTables 한 페이지에 통째로, 각 행에 OSTI ID 포함.
2. **메타 보강**: OSTI 공개 API `https://www.osti.gov/api/v1/records/{osti_id}` → 저자·키워드·DOI·초록. (meta_url = `osti.gov/biblio/{id}`)
3. **PDF 다운로드**: `https://www.osti.gov/servlets/purl/{osti_id}` (HEAD로 content-type 확인 후 취득).

즉 robots가 막는 `/pages/search`·`/dataexplorer/search` 대신, **robots에 안 걸리는 공개 API(`/api/v1/records`)와 PDF 엔드포인트(`/servlets/purl`)**를 NETL 크롤러가 사용한다.

---

## 6. client_report.xlsx 요약과의 일치 검증

본 분류는 xlsx '요약' 시트를 actionable 기준으로 **쪼갠** 것이라 합이 정확히 맞는다:

- A.영구불가 276 = E.robots 222 + D.VPN 53 + B.로그인 1
- B.응답불가 116 = F.터짐 116
- C.JS-only 20 = G.미분류 20
- D.Cloudflare 챌린지 194 = C.챌린지 194
- 보강필요 85 = H 85 · partial 1 = I 1
- 수집가능(ok) 1,302 = A.즉시 1,100 + J.SPA 202

---

## 7. 다음 단계 후보

1. **회원가입 카테고리 정밀화** — 알려진 계정형 도메인 수동 태깅, 또는 D(VPN 53)·E(robots 222) 사이트 직접 확인
2. **엑셀/CSV 내보내기** — 위 10개 카테고리 + 사이트 전체 목록을 `client_report` 새 시트로
3. **대시보드 반영** — funnel을 이 10개 actionable 기준으로 교체/보강

*VPN 1순위 후보: `www.gov.il` (37건, 이스라엘 정부 — 지역 IP 차단 거의 확실).*
