# Accounting RAG Corpus Curation Plan

## Objective
Prepare the collected Korean accounting corpus for service RAG by producing a deterministic curated artifact set from the raw Markdown export without deleting or overwriting raw data.

## Current Evidence
- Raw Markdown export: `/data_raid/ruci_workspace/acct_rag_data`
- Corpus root for generated docs only: `/data_raid/ruci_workspace/acct_rag_data/documents/**/*.md`
- Generated Markdown count: `3711`
- `_failures.jsonl`: empty
- Priority counts:
  - `01`: 217, `02`: 49, `03`: 1405, `05`: 230, `06`: 272, `08`: 60, `09`: 522, `10`: 936, `13`: 2, `14`: 6, `15`: 6, `16`: 3, `17`: 3
- Duplicate docs by priority from body hash:
  - `01`: 216, `03`: 4, `08`: 30, `09`: 261, `10`: 470
- Short body documents under 500 chars: `407`
- Strong RAG candidates: FSS priorities `02`, `03`; secondary candidates: priorities `05`, `06`, `09`, `10`, `13`-`17`
- Risky as-is: KASB priority `01`; FSC all-press priority `08`

## Defaults Applied
- Curated outputs must live outside the raw export directory: `/data_raid/ruci_workspace/acct_rag_data_curated/<run_id>/`
- Raw export is read-only input. Do not delete, rewrite, or move files under `/data_raid/ruci_workspace/acct_rag_data`.
- The downstream artifact contract is data-only: curated Markdown, JSONL manifests, quality report. No vector DB ingestion or `/data_raid/ruci_workspace/fino` integration in this plan.
- Duplicates collapse into one canonical document with alias records preserved.
- Priority `01` is quarantined until KASB crawler repair is completed.
- Priority `08` is excluded by default unless a strict accounting relevance rule explicitly keeps a document.

## Artifact Contract
Create a run directory:

```text
/data_raid/ruci_workspace/acct_rag_data_curated/YYYYMMDD-HHMMSS/
  curated_manifest.jsonl
  quarantine_manifest.jsonl
  excluded_manifest.jsonl
  duplicate_aliases.jsonl
  quality_report.md
  documents/
    include/
    secondary/
```

Each manifest row must include:
- `curation_run_id`
- `source_markdown_path`
- `curated_markdown_path`
- `source_priority`
- `agency`
- `source_type`
- `source_subtype`
- `index_name`
- `title`
- `detail_url`
- `attachment_url`
- `body_hash`
- `body_chars`
- `quality_tier`: `include`, `secondary`, `quarantine`, `exclude`
- `quality_flags`: list of strings
- `exclude_reason`: empty unless excluded/quarantined
- `canonical_id`
- `duplicate_group_id`
- `alias_count`

## Quality Rules
- `include`: priority `02` or `03`, non-empty body, not duplicate alias, no severe garbling.
- `secondary`: priority `05`, `06`, `09`, `10`, `13`, `14`, `15`, `16`, or `17`, non-empty body, not duplicate alias, no severe garbling.
- `quarantine`: priority `01`, severe garbling, malformed front matter, missing required metadata, or body under 200 chars.
- `exclude`: priority `08` unless strict accounting relevance is true, duplicate aliases, or documents irrelevant to accounting.
- Strict accounting relevance for priority `08`: title or body must contain at least one of `회계`, `외부감사`, `감리`, `재무제표`, `K-IFRS`, `IFRS`, `감사인`, `회계법인`, `사업보고서`, `증권선물위원회`.
- Severe garbling flag: body contains high-density replacement/control characters or common PDF extraction mojibake patterns; exact regexes must be unit-tested.

## Implementation Tasks

### Task 1: Add Curation Module Skeleton
Ownership: `crawler/fino_acct/curate_markdown.py`, `tests/test_fino_acct_markdown_curation.py`

Write tests first:
- `test_parse_markdown_document_reads_front_matter_and_body`
- `test_build_quality_record_marks_priority_2_as_include`
- `test_build_quality_record_quarantines_priority_1`

Expected RED evidence:
- Running `.venv/bin/python -m pytest tests/test_fino_acct_markdown_curation.py -q` fails because the module/functions do not exist.

Implementation:
- Add typed dataclasses for parsed Markdown, quality records, and curation decisions.
- Parse front matter with a robust YAML/front-matter strategy. If adding a dependency is avoidable, implement a narrow tested parser for the current scalar format.
- Compute SHA-256 over normalized body text.

Acceptance:
- The three tests pass.
- No writes occur outside the test temp directory.

Manual QA:
- CLI/data scenario: run `.venv/bin/python -m crawler.fino_acct.curate_markdown --help`
- PASS if exit code is `0` and stdout lists `--input-dir`, `--output-root`, and `--dry-run`.

### Task 2: Implement Deterministic Deduplication
Ownership: `crawler/fino_acct/curate_markdown.py`, `tests/test_fino_acct_markdown_curation.py`

Write tests first:
- `test_select_canonical_prefers_include_over_secondary_over_quarantine`
- `test_select_canonical_prefers_longer_clean_body_within_same_tier`
- `test_duplicate_aliases_preserve_source_paths`

Implementation:
- Group records by `body_hash`.
- Select canonical by tier rank, then clean body length, then lower source priority, then stable source path.
- Write alias rows for non-canonical duplicates.

Acceptance:
- Duplicate aliases are not copied into curated `documents/`.
- Alias rows preserve source path, canonical ID, duplicate group ID, and title.

Manual QA:
- CLI/data scenario: run dry-run on a temp fixture with duplicate Markdown files.
- PASS if stdout reports `canonical=1 aliases=1` and `duplicate_aliases.jsonl` contains the alias source path.

### Task 3: Implement Run Directory and Manifests
Ownership: `crawler/fino_acct/curate_markdown.py`, `tests/test_fino_acct_markdown_curation.py`

Write tests first:
- `test_curate_writes_run_directory_without_touching_raw_input`
- `test_curate_writes_curated_quarantine_excluded_and_alias_manifests`
- `test_curate_uses_atomic_run_directory_publish`

Implementation:
- Write to a temporary run directory under output root, then atomically rename to final run ID.
- Copy canonical Markdown files to `documents/include` or `documents/secondary`.
- Do not mutate source Markdown.
- Support `--run-id` for deterministic tests.

Acceptance:
- Manifest files are valid JSONL.
- Source file mtimes and contents are unchanged in tests.

Manual QA:
- CLI/data scenario: run curation on a small temp fixture with `--run-id qa-fixture`.
- PASS if the output tree contains all four manifest files and raw fixture file hashes are unchanged.

### Task 4: Generate Quality Report
Ownership: `crawler/fino_acct/curate_markdown.py`, `tests/test_fino_acct_markdown_curation.py`

Write tests first:
- `test_quality_report_contains_counts_by_tier_priority_and_reason`
- `test_quality_report_lists_top_duplicate_groups`
- `test_quality_report_lists_manual_review_samples`

Implementation:
- Produce `quality_report.md` with:
  - total raw docs
  - tier counts
  - counts by source priority
  - duplicate group count and alias count
  - exclusion/quarantine reason counts
  - shortest documents
  - top duplicate groups
  - manual review sample paths by tier

Acceptance:
- Report is deterministic for a fixed input set.
- Report distinguishes evidence from recommendations.

Manual QA:
- CLI/data scenario: run curation on full raw corpus with `--dry-run`.
- PASS if stdout includes raw count `3711`, priority `01` quarantine count, and duplicate alias count.

### Task 5: Full Corpus Curation Run
Ownership: runtime output only under `/data_raid/ruci_workspace/acct_rag_data_curated/<run_id>/`

Prerequisites:
- Tasks 1-4 tests are GREEN.
- `.venv/bin/python -m pytest tests/test_fino_acct_markdown_curation.py tests/test_fino_acct_markdown_export.py tests/test_fino_acct_collector.py -q` passes.

Execution:
```bash
.venv/bin/python -m crawler.fino_acct.curate_markdown \
  --input-dir /data_raid/ruci_workspace/acct_rag_data \
  --output-root /data_raid/ruci_workspace/acct_rag_data_curated
```

Acceptance:
- Raw corpus still has `3711` generated Markdown files.
- Curated include + secondary document count equals canonical non-excluded document count.
- Quarantine manifest includes all priority `01` records.
- Excluded manifest includes priority `08` records that fail strict accounting relevance.
- Duplicate aliases include at least the known duplicate families from priorities `01`, `08`, `09`, and `10`.

Manual QA:
- CLI/data scenario: run `wc -l` on every manifest and inspect `quality_report.md`.
- PASS if counts reconcile: `curated + quarantine + excluded + aliases = 3711`.

### Task 6: KASB Repair Handoff
Ownership: `plans/kasb-recrawl-repair.md`

Write a separate repair plan, not code, with:
- Current evidence: 217 priority `01` files, 1 unique body hash.
- Hypothesis: attachment identity for POST download is not preserved in URL uniqueness.
- Required investigation: KASB list/detail HTML form fields, `post_file_no`, `post_file_seq`, attachment button metadata, and DB uniqueness key.
- Acceptance: KASB recrawl produces multiple unique bodies and attachment identities; no duplicate same PDF repeated across all records.

No implementation in this curation wave.

## Full Verification Wave
Run after implementation:

```bash
.venv/bin/python -m pytest tests/test_fino_acct_markdown_curation.py tests/test_fino_acct_markdown_export.py tests/test_fino_acct_collector.py -q
.venv/bin/python -m compileall crawler/fino_acct tests
find /data_raid/ruci_workspace/acct_rag_data/documents -type f -name '*.md' | wc -l
find /data_raid/ruci_workspace/acct_rag_data_curated -maxdepth 2 -type f | sort | tail
```

Pass criteria:
- All tests pass.
- Compileall passes.
- Raw generated Markdown count remains `3711`.
- Latest curated run contains manifests, report, and curated documents.
- `quality_report.md` explicitly lists KASB quarantine, FSC priority `08` exclusions, duplicate alias counts, and recommended RAG tiers.

## Reviewer Gate
Because this plan would touch 3+ files and create production-adjacent data artifacts, run `codex-ultrawork-reviewer` after implementation with:
- this plan
- diff
- RED and GREEN test outputs
- full corpus curation command output
- manifest count reconciliation
- raw corpus preservation evidence

Completion requires unconditional reviewer approval.
