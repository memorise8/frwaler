# capacity_final.csv reliability analysis

[OBJECTIVE] Determine whether the per-site document-capacity estimates in
`scripts/audit/capacity_final.csv` (804 sites) are wrong only for a handful of
rows, or whether entire measurement *methods* systematically underestimate
capacity and need full re-survey.

Read-only analysis. No crawling, no network requests, no file modifications.
Tooling note: the sandboxed `python_repl` in this environment blocks `import`,
`open()`, and `eval()` entirely (GyoshuSecurityError), so it could not load
pandas or read any CSV. All computation below was done with plain-stdlib
Python 3 scripts (`csv`, `statistics`, `math`) run via `python3` from Bash,
per the task's explicit permission to use "python3/pandas or plain python."

---

## 1. Inventory & pipeline understanding

`scripts/audit/capacity_final.csv` (804 rows) is produced by
`consolidate_capacity.py`. Its columns: `site_id, sheet, collected,
max_to_collect, source, exact_or_lowerbound, gap, health, observed_pdf_rate,
projected_pdf_count, projected_storage_gb`. (A *different* script,
`finalize_capacity.py` / `finish_capacity_pipeline.py`, produces a
differently-shaped `capacity_final_report.md/xlsx` from a different priority
chain — it was NOT used to build `capacity_final.csv`; confirmed by exact
column-name match against `consolidate_capacity.py`'s `CSV_FIELDS`.)

`consolidate_capacity.py`'s precedence for `max_to_collect` (first hit wins,
all stamped `exact_or_lowerbound="exact"` except the crawl-count fallback):

1. `big_totals.csv` (`total`, note==`ok`) → `source=big_api`
2. `hal_totals.csv` (`filtered_total`, note==`ok`) → `source=hal_api`
3. `coverage_report.csv` (`source_total`, any `method` except `hal_solr`) → `source=api_<method>`
4. `custom_crawler_totals.SNAPSHOT.csv` (`source_total`>0) → `source=custom_api`
5. `full_survey_totals.csv`: `outcome==ok & counted<500` → `count_crawl` (exact); `counted>=500` → `count_crawl` (lower_bound); `outcome==site_timeout` → `count_timeout` (lower_bound); `zero_parsed/fetch_fail/error/no_crawler` → unmeasured
6. else → unmeasured

Critically, **~20 other audit files with independent per-site counts were
never wired into this precedence chain at all**: `count_only_totals.csv`,
`count_only_capped_1h.csv`, `count_only_uncapped.csv`,
`count_only_totalscan.csv`, `count_only_c_full.csv`, `count_only_datos_mx.csv`,
`count_only_gobmx.csv`, `crawler_health_probe.csv`,
`crawler_health_probe308.csv`, `health_timeout_recheck.csv`,
`lowerbound_deep.csv`, `repaired_remeasure.csv`, `reverify44.csv`,
`truth_sweep_146.csv`, `crawler_health_final.csv`, `exact_probe.csv`,
`html_only_*.csv`, `pdf_rate_sweep.csv`, `big_totals_retry.csv`, `c2_totals.csv`.
Several of these carry a `server_total` field — the actual total the
*server itself* reported during a probe request — which is exactly the
signal that would have caught the DOAJ error (see below). This is the
central finding of the analysis: **capacity_final.csv is stale relative to
data that already exists on disk**, independent of whether any individual
measurement method is flawed.

---

## 2. capacity_final.csv grouped by `source`

| source | n rows | n valued | sum(max_to_collect) | min | median | max |
|---|--:|--:|--:|--:|--:|--:|
| big_api | 13 | 13 | 3,053,218 | 4,902 | 31,979 | 1,709,118 |
| api_dspace_rest7 | 14 | 14 | 985,676 | 662 | 16,050 | 393,484 |
| api_oai_resumption | 14 | 14 | 605,948 | 211 | 18,046 | 250,523 |
| hal_api | 28 | 28 | 539,919 | 23 | 1,915 | 234,422 |
| custom_api | 37 | 37 | 444,322 | 26 | 949 | 198,333 |
| api_ckan | 18 | 18 | 268,875 | 0 | 111 | 140,429 |
| count_timeout | 221 | 221 | 71,867 | 0 | 310 | 10,521 |
| api_wordpress_posts | 26 | 26 | 55,692 | 1 | 301 | 35,278 |
| count_crawl | 227 | 227 | 41,998 | 1 | 63 | 5,696 |
| api_html_count_regex | 52 | 52 | 33,948 | 1 | 84 | 25,000 |
| api_wordpress_* (7 tiny sub-methods, 1 site each) | 8 | 8 | 8,193 | 3 | — | 3,608 |
| **(unmeasured / blank source)** | **145** | **0** | **0** | — | — | — |

Total 804 rows. As-filed grand total (naive sum of `max_to_collect`):
**6,109,256** documents, from 659 measured sites; 145 sites carry no number
at all.

---

## 3. Cross-check against every other per-site count file

Method: for every `capacity_final.csv` row, computed the maximum value
reported for that `site_id` across **every other audit CSV with a
count-like column** (`server_total`, `counted`, `source_total`, `total`,
`filtered_total`, `exact_total`, `html_list_하한` — 29 candidate files
inventoried; `c2_totals.csv` had no parseable values), **excluding the file
that was literally the origin of that row's number** (so the check is a
genuine independent measurement, not a comparison against itself). Where a
row's counting file records both `counted` and `server_total`, the larger
(`server_total`) was used, since `server_total` is what the server itself
reports as the collection size, while `counted` is only how many items the
crawler pulled before stopping/capping.

Ratio = independent_other_measurement / capacity_final_value.

221 sites had ratio > 2x (independent measurement much larger — candidate
underestimates); 14 sites had ratio < 0.5x (independent measurement smaller
— investigated below and explained as stale/truncated comparison files, not
overestimation in capacity_final).

### Per-method-group agreement (ratio > 2x only)

| source (method group) | n rows | n with 2nd measurement | n ratio>2x | % >2x |
|---|--:|--:|--:|--:|
| hal_api | 28 | 28 | 22 | **78.6%** |
| count_timeout | 221 | 197 | 137 | **69.5%** |
| api_html_count_regex | 52 | 52 | 18 | **34.6%** |
| api_wordpress_posts | 26 | 26 | 6 | 23.1% |
| count_crawl | 227 | 227 | 37 | 16.3% |
| api_oai_resumption | 14 | 14 | 1 | 7.1% |
| custom_api | 37 | 37 | 0 | 0.0% |
| api_ckan | 18 | 17 | 0 | 0.0% |
| api_dspace_rest7 | 14 | 14 | 0 | 0.0% |
| big_api | 13 | 13 | 0 | 0.0% |
| api_wordpress_* (7 tiny groups) | 8 | 8 | 0 | 0.0% |
| (unmeasured) | 145 | 0 (see §5 — recoverable elsewhere) | — | — |

Breaking `count_crawl` and `count_timeout` down by their own
`exact_or_lowerbound` flag confirms the pattern is *systematic within the
truncated/lower_bound sub-populations*, not scattered noise:

| source | exact_or_lowerbound | n | n checked | ratio median | n>2x | %>2x |
|---|---|--:|--:|--:|--:|--:|
| count_timeout | lower_bound | 221 | 197 | 3.03 | 137 | 69.5% |
| count_crawl | exact | 217 | 217 | 1.00 | 34 | 15.7% |
| count_crawl | lower_bound | 10 | 10 | 1.00 | 3 | 30.0% |
| hal_api | exact | 28 | 28 | **5.72** | 22 | 78.6% |
| api_html_count_regex | exact | 52 | 52 | 1.00 | 18 | 34.6% |
| api_wordpress_posts | exact | 26 | 26 | 1.00 | 6 | 23.1% |
| api_oai_resumption | exact | 14 | 14 | 1.00 | 1 | 7.1% |

81 rows are marked `exact_or_lowerbound = "exact"` (i.e. the pipeline
believes the number is a finished, complete count) yet still disagree with
an independent measurement by more than 2x — these are the true "mislabeled
exact" bugs: 34 in `count_crawl`, 22 in `hal_api`, 18 in
`api_html_count_regex`, 6 in `api_wordpress_posts`, 1 in `api_oai_resumption`
(doaj).

### The two apparent `big_api` "disagreements" (ratio<0.5x) are false alarms

`ntrs-nasa-gov-search` (cap=646,398 vs. other=71,865 from a
`probe_limit:3`-truncated `crawler_health_probe308.csv` row) and
`search-open-canada-ca-opendata` (cap=5,830 vs. other=238 from an early
`count_only_totals.csv` pass) both show a *smaller* independent number only
because every other file that touched those sites was itself a
truncated/early probe. `big_api` (the fresh platform-API remeasure) remains
the largest, most complete number available for both — confirms `big_api`
is trustworthy, not a counter-example.

### Root cause found for `hal_api` (78.6% underestimated)

`hal_totals.csv`'s Solr query applies narrow `fq` filters per site, e.g.:

```
amu-hal-science-search:  fq=submitType_s:file  fq=docType_s:(REPORT OR OTHER)
anr-hal-science-search:  fq=openAccess_bool:true  fq=submitType_s:file  fq=docType_s:(THESE OR COMM OR ART OR MEM)
cea-hal-science-cnrgh:   fq=collCode_s:CNRGH  fq=docType_s:(THESE OR COMM OR ART)
```

These filters (must have an attached file, must be one of a handful of
document types, sometimes scoped to one sub-collection code) count only a
narrow slice of each HAL portal, not the full site content the crawler
targets. An independent, unfiltered per-site measurement
(`crawler_health_final.csv`, kind=`measured`) shows every one of these
portals is far larger and the numbers are all distinct per institution
(amu=237,733, anr=253,771, cea-cnrgh=151,061, cnam=32,221, espci=12,333,
etc.) — ruling out "it's just a copy-pasted global HAL total" as the
explanation. This is a genuine, systematic scoping bug in the HAL
measurement method, not isolated bad rows.

### Root cause confirmed for `api_oai_resumption` (doaj-org-search specifically)

`coverage_report.csv`'s doaj-org-search row: `source_total=35582,
coverage_pct=4.0, method=oai_resumption, status=partial,
note=https://doaj.org/oai`. Three independent probes agree DOAJ's real size
is ~13.3-13.4M: `crawler_health_probe.csv` server_total=13,362,368;
`crawler_health_probe308.csv` server_total=13,373,055;
`count_only_totalscan.csv` server_total=13,362,110. `count_only_harness.py`
even has a comment referencing exactly this case ("DOAJ가 OAI로 35k 잘못측정 →
server_total로 진짜 13.3M 확인"). However, checking **all 14**
`api_oai_resumption` sites individually shows the other 13 agree with
`crawler_health_final.csv` at ratio = 1.000 exactly — DOAJ is a genuine,
isolated single-site failure of the OAI resumption-token approach (its
`completeListSize` reflects a scoped/limited OAI set, not the full ~13M
article index), **not** evidence the whole `oai_resumption` method is
broken.

---

## 4. Worst 30 disagreements (both values, both methods)

| site_id | capacity_final value (method) | independent value (file) | ratio |
|---|--:|--:|--:|
| ethniccommunities-govt-nz-resources | 86 (count_crawl) | 0 (count_only_totals.csv) | 0.000 * |
| mpi-govt-nz-resources-and-forms | 35 (count_crawl) | 0 (count_only_totals.csv) | 0.000 * |
| nkvts-no-english | 381 (count_timeout) | 0 (count_only_totals.csv) | 0.000 * |
| transparency-gov-au-publications | 2,693 (count_crawl) | 0 (count_only_totals.csv) | 0.000 * |
| transport-govt-nz-documents-and-public | 3 (count_timeout) | 0 (count_only_totals.csv) | 0.000 * |
| etis-ee-portal | 1 (count_timeout) | 327,834 (count_only_c_full.csv) | 327834.0 |
| hud-govt-nz-stats-and-insights | 1 (count_timeout) | 2,089 (count_only_c_full.csv) | 2089.0 |
| afp-gov-au-search | 1 (count_crawl) | 2,025 (exact_probe.csv) | 2025.0 |
| fmi-ch-research | 1 (count_timeout) | 2,018 (count_only_c_full.csv) | 2018.0 |
| cds-cern-ch-collection | 121 (count_crawl) | 110,861 (crawler_health_probe308.csv) | 916.2 |
| hcsp-fr-explorecgi | 32 (count_crawl) | 21,497 (exact_probe.csv) | 671.8 |
| mofe-go-kr-id | 3 (count_timeout) | 1,983 (count_only_c_full.csv) | 661.0 |
| irb-usi-ch-publications | 1 (count_timeout) | 583 (count_only_c_full.csv) | 583.0 |
| espci-hal-science-search | 23 (hal_api) | 12,333 (crawler_health_final.csv) | 536.2 |
| kliimaministeerium-ee-otsing | 10 (count_timeout) | 4,291 (count_only_totalscan.csv) | 429.1 |
| cnam-hal-science-ceet | 84 (hal_api) | 32,221 (crawler_health_final.csv) | 383.6 |
| **doaj-org-search** | **35,582 (api_oai_resumption)** | **13,373,055 (crawler_health_probe308.csv)** | **375.8** |
| cedelft-eu-reports | 1 (api_html_count_regex) | 357 (full_survey_totals.csv) | 357.0 |
| nistdigitalarchives-contentdm-oclc-org-digital | 24 (count_crawl) | 8,257 (count_only_totalscan.csv) | 344.0 |
| mfds-go-kr-brd | 14 (count_timeout) | 4,798 (exact_probe.csv) | 342.7 |
| mof-go-kr-doc | 365 (count_timeout) | 104,162 (exact_probe.csv) | 285.4 |
| samr-gov-cn-zw | 1 (count_timeout) | 283 (lowerbound_deep.csv) | 283.0 |
| kice-re-kr-boardcnts | 279 (count_timeout) | 78,174 (exact_probe.csv) | 280.2 |
| siseministeerium-ee-otsing | 10 (count_timeout) | 2,503 (crawler_health_probe308.csv) | 250.3 |
| moe-go-kr-boardcnts | 300 (count_crawl) | 72,852 (exact_probe.csv) | 242.8 |
| cwf-ca-publications | 1 (api_wordpress_posts) | 229 (full_survey_totals.csv) | 229.0 |
| home-cern-news | 10 (count_crawl) | 2,200 (count_only_capped_1h.csv) | 220.0 |
| state-gwd-go-kr-portal | 46 (count_timeout) | 8,333 (exact_probe.csv) | 181.2 |
| cea-hal-science-cnrgh | 866 (hal_api) | 151,061 (crawler_health_final.csv) | 174.4 |
| iheid-swisscovery-ch-discovery | 24 (count_crawl) | 3,749 (count_only_c_full.csv) | 156.2 |

\* The 5 ratio=0.000 rows are the flip side: capacity_final has a small
positive number and one comparison file recorded 0 for that site (a
blocked/failed probe attempt on a different day) — these are noise from a
failed probe run, not evidence of overestimation; capacity_final's own
number for these 5 is corroborated elsewhere and unchanged in the corrected
totals below.

---

## 5. Classification of method groups

**TRUSTWORTHY** (independent measurements agree within 2x for nearly all sites):
- `big_api` (13 sites, 0% disagreement) — fresh platform-API remeasure, most authoritative source.
- `custom_api` (37 sites, 0% disagreement).
- `api_ckan` (18 sites, 0% disagreement).
- `api_dspace_rest7` (14 sites, 0% disagreement).
- `api_oai_resumption` (14 sites, 13/14 agree exactly; 1 isolated site-specific failure — see doaj below).
- The 8 single-site `api_wordpress_*` sub-methods (0% disagreement).
- `count_crawl` (227 sites, 83.7% agree within 2x) — mostly trustworthy; treat as trustworthy at the method level but re-check the 37 individual outlier sites.

**UNTRUSTWORTHY** (systematic underestimation — method itself, not just a few rows, is broken):
- `hal_api` (28 sites, 78.6% underestimated up to 536x) — narrow Solr `fq` filters (file-attachment + doc-type + sub-collection scoping) count a slice of each portal, not the full site.
- `count_timeout` (221 sites, 69.5% underestimated up to 327,834x) — single-pass wall-clock-timeout counts; already self-labeled `lower_bound`, but the true scale of undercount is far worse than the label implies. Needs full re-survey with longer/uncapped runs.
- `api_html_count_regex` (52 sites, 34.6% underestimated up to 357x) — regex-scraped "showing X of Y" pagination text is fragile and both over- and under-counts depending on page markup (`coverage_report.csv` shows this method's own status is split `over`/`partial` for every single row, never a clean `ok`).
- `api_wordpress_posts` (26 sites, 23.1% underestimated up to 229x) — WP REST API pagination/post-type scoping undercounts for roughly a quarter of sites.

**UNVERIFIABLE-BY-CAPACITY_FINAL-ALONE, but recoverable without new crawling**:
- 145 `(unmeasured)` sites — 145/145 (100%) actually have a usable measurement in one of the ~20 audit files that `consolidate_capacity.py` never reads. This is a pipeline gap, not a true data gap: no re-crawl is required, only a fix to the merge script's input list.
- Isolated site: `doaj-org-search` — the one genuine failure inside the otherwise-trustworthy `api_oai_resumption` group (OAI `completeListSize` reflects a scoped subset, not DOAJ's full ~13.3M index). A method-wide re-survey of `api_oai_resumption` is not warranted; a single-site fix/reverify is.

---

## 6. Corrected best-estimate totals

Per site: `corrected = max(capacity_final.max_to_collect, best independent
measurement found in any other audit file)`.

- **Sites with a usable value: 804 / 804** (the 145 previously-"unmeasured" sites all resolved to a real number from another file; 0 sites remain with zero information anywhere in the audit corpus).
- **CORRECTED GRAND TOTAL: 24,260,462** documents (vs. 6,109,256 as currently filed in capacity_final.csv — a **+18,151,206 (+297%)** increase, driven overwhelmingly by DOAJ alone (+13.34M) plus the HAL and timeout corrections).
- 387 sites (48%) have a corrected value more than 2x their capacity_final.csv value (or had no value at all) — these are the sites that need the merge-script fix / re-survey attention. 417 sites (52%) are already within 2x of the best available evidence and can be treated as fine as-is.

### Top 20 sites by corrected capacity

| site_id | corrected | capacity_final.csv value (method) | changed? |
|---|--:|--:|---|
| doaj-org-search | 13,373,055 | 35,582 (api_oai_resumption) | YES |
| e-stat-go-jp-stat-search | 1,709,118 | 1,709,118 (big_api) | no |
| ots-at-pressemappe | 1,561,267 | *(unmeasured)* | YES |
| ntrs-nasa-gov-search | 646,398 | 646,398 (big_api) | no |
| openresearch-repository-anu-edu-au-search | 393,484 | 393,484 (api_dspace_rest7) | no |
| prism-go-kr-homepage | 385,715 | 10,521 (count_timeout) | YES |
| etis-ee-portal | 327,834 | 1 (count_timeout) | YES |
| sonar-ch-global | 309,727 | 309,727 (big_api) | no |
| research-collection-ethz-ch-search | 303,530 | 303,530 (api_dspace_rest7) | no |
| inserm-hal-science-search | 254,477 | 234,422 (hal_api) | YES |
| anr-hal-science-search | 253,771 | 140,008 (hal_api) | YES |
| pergamos-lib-uoa-gr-search | 250,523 | 250,523 (api_oai_resumption) | no |
| amu-hal-science-search | 237,733 | 2,938 (hal_api) | YES |
| datacatalogue-adruk-org-browser | 219,675 | *(unmeasured)* | YES |
| etera-ee-browse | 210,234 | 210,234 (big_api) | no |
| ostrnrcan-dostrncan-canada-ca-search | 198,333 | 198,333 (custom_api) | no |
| cea-hal-science-cnrgh | 151,061 | 866 (hal_api) | YES |
| data-gov-au-data | 140,429 | 140,429 (api_ckan) | no |
| data-gv-at-datasets | 116,818 | 116,818 (custom_api) | no |
| dspace-ut-ee-search | 114,527 | 114,527 (api_dspace_rest7) | no |

`ots-at-pressemappe` (1,561,267) and `datacatalogue-adruk-org-browser`
(219,675), `etis-ee-portal` (327,834) and `prism-go-kr-homepage` (385,715)
were spot-checked for corroboration: the latter three are each confirmed by
2-3 independent probe files landing within a few percent of each other
(e.g. etis-ee-portal's `server_total` reads 327,834 identically in both
`crawler_health_probe.csv` and `count_only_c_full.csv`); `ots-at-pressemappe`
rests on a single `exact_probe.csv` `total_text_only` reading and should be
spot-verified before being treated as fully confirmed.

### Bucket distribution (corrected capacity)

| bucket | sites |
|---|--:|
| < 100,000 | 779 |
| 100,000 – 1,000,000 | 22 |
| > 1,000,000 | 3 (doaj-org-search, e-stat-go-jp-stat-search, ots-at-pressemappe) |

---

## 7. Answer to the core question

**Not just a handful of rows.** Four measurement methods covering 327 of the
804 sites (40.7%) — `hal_api`, `count_timeout`, `api_html_count_regex`, and
`api_wordpress_posts` — show systematic underestimation on 20-79% of their
sites, each with an identifiable structural cause (over-narrow HAL query
filters, wall-clock timeout truncation, fragile regex text-scraping, and WP
REST pagination scoping respectively). On top of that, another 145 sites
(18%) are simply missing from capacity_final.csv even though ~20 other audit
files already contain usable measurements for every one of them — a wiring
gap in `consolidate_capacity.py`'s input list, not a measurement problem.
The DOAJ case the task opened with (35,582 vs ~13.3M) is real but is the
single clear casualty inside an otherwise-reliable method
(`api_oai_resumption`, 13/14 sites agree exactly) — it needs a one-site fix,
not a re-survey of every OAI-resumption site. `big_api`, `custom_api`,
`api_ckan`, and `api_dspace_rest7` (82 sites, 10.2%) are fully corroborated
and need no action.

## Limitations

- [LIMITATION] The cross-check relies entirely on other locally-stored audit
  CSVs, not on fresh network measurement (per task constraints — no
  crawling was performed). A handful of "corrected" values rest on a single
  corroborating file (e.g. `ots-at-pressemappe`, `exact_probe.csv`
  `total_text_only`) and should be spot-verified with a real re-crawl before
  being treated as final.
- [LIMITATION] "Independent" measurement selection excludes only the single
  literal origin file per capacity_final row (per `consolidate_capacity.py`'s
  documented precedence); it does not account for possible shared upstream
  bugs between different audit scripts hitting the same buggy endpoint
  (e.g. if two different scripts both queried a paginated API that itself
  caps at 500 results, agreement between them would look like corroboration
  but still be wrong).
- [LIMITATION] Ratios use the *maximum* of all available other-file values,
  which biases toward finding larger "corrected" numbers; if a site's true
  capacity fluctuates (documents added/removed over time between probe
  dates), this could overstate the correction for a few volatile sites.
- [LIMITATION] `python_repl` (Gyoshu bridge) in this environment rejects all
  `import` statements and file I/O builtins, so no confidence intervals or
  formal significance tests were computed with scientific libraries;
  reported statistics are plain counts, ratios, and medians via Python's
  stdlib `statistics` module, run through `python3` via Bash (not
  `python_repl`) because the sandboxed REPL could not load pandas or open
  any file.
