# Collection report MVP design contract

## Route and product boundary

This contract applies only to the read-only `/admin/collection-report` MVP. It presents the route's existing server-side collection-report values and existing navigation destinations; it does not introduce search, filters, client-side fetching, mutations, API/auth changes, or root-layout changes.

The visual direction is an original Libertree catalogue report: information-led, compact, and operationally calm. E-CIP is inspiration for the list-first hierarchy only. Do not reproduce E-CIP logos, assets, wording, layouts, or exact colour/spacing/font tokens.

## Original visual system

- **Type:** use the application's existing Korean-capable sans-serif stack. Report title is 24–28px/700; section titles are 15–17px/700; body/table labels are 13–14px; supporting metadata is 12px.
- **Spacing:** use a 4px rhythm with 8, 12, 16, 24, and 32px gaps. Keep the main content readable without decorative empty space.
- **Tokens:** warm paper background, white content surfaces, charcoal text, muted slate metadata, and one original Libertree blue for interactive emphasis. Use a restrained border and shadow treatment; do not use gradients as branding.
- **Status semantics:** pair colour with a text label and, where useful, an icon or shape. Never make colour the only signal.

## Information hierarchy and primitives

1. A compact report header states the report identity, current snapshot/status, and provenance-safe explanatory copy. It may retain only existing route navigation controls.
2. A compact status band presents the existing collection funnel and coverage values with visible numeric text beside any bar/indicator.
3. A dense exception table presents PDF-gap/site diagnostics with semantic `table`, `caption` or accessible label, `thead`, and `th` scope. It is the primary working surface, not a decorative card wall.
4. Diagnosis and recovery sections retain the existing report groups, using neutral explanatory copy rather than invented operational instructions or raw source data.

Route-local presentation primitives are: `ReportHeader`, `StatusBand`, `MetricRow`, `ExceptionTable`, and `EmptyState`. They may remain local markup when extraction would add indirection. Each primitive supports its normal, zero-data, and unavailable/missing-diagnosis state.

## Responsive and accessible behaviour

- **Desktop (1280px+):** align header metadata and summary information on one compact line where space permits; show the dense table columns normally.
- **Mobile (375px):** stack report metadata and summaries; retain readable 13px-or-larger table text. The table sits in its own horizontally scrollable wrapper with a visible affordance, while the page itself must not gain horizontal overflow.
- **Empty data:** render a named, readable empty state that explains that there is no report value to display; never leave a blank section.
- **Missing diagnosis:** keep the diagnosis section visible and name the unavailable diagnosis separately from a true zero count.
- **Keyboard:** all retained links/controls expose a clearly visible focus ring with sufficient contrast; focus order follows the page hierarchy.
- **Status and charts:** every status/bar has adjacent text and numbers; do not rely on hover-only or colour-only meaning.

## Data, evidence, and handoff boundaries

The route remains presentation-only and read-only. Do not change database queries, schemas, PDFs, Excel files, blobs, crawlers, services, URLs, authentication, or APIs. Do not place raw collection records, credentials, tokens, or sensitive source values in screenshots, documents, or validation evidence.

Accepted MVP debt: the shared shell, global navigation, other admin screens, delivery packaging, and richer filtering/search stay unchanged. A later frontend phase may revisit these only after this one-route report is accepted.
