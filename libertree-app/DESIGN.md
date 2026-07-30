# Libertree Design System

## 0. Research Log

- Embedded refs: shortlisted Notion, Linear, and GitHub; picked `minimalist-skill` and Notion for document-first hierarchy, whisper borders, and compact catalogue controls.
- Lazyweb: skipped because this deliberately empty catalogue shell does not need an external product screen to define its retained navigation surface.
- Imagen drafts: skipped because the shell uses a semantic inline tree-ring mark rather than a hero illustration; an image draft would add an unused asset.
- React dev tooling: skipped because the migration permits only dependencies required by the retained shell; inspection tooling is deferred to the live-catalogue implementation.

## 1. Atmosphere & Identity

Libertree is a quiet reading room for globally collected documents: warm paper, charcoal ink, and one restrained leaf-green signal. Its signature is a concentric tree-ring mark that pairs a living catalogue with an archival shelf without suggesting any collection-control capability.

## 2. Color

| Role | Token | Value | Usage |
| --- | --- | --- | --- |
| Canvas | `--canvas` | `#f7f6f1` | Page field |
| Surface | `--surface` | `#fffefb` | Cards and header |
| Ink | `--ink` | `#202622` | Headings and controls |
| Muted | `--muted` | `#687069` | Supporting copy |
| Rule | `--rule` | `#dedfd7` | Structural dividers |
| Moss | `--moss` | `#365b42` | Catalogue action and focus |
| Moss wash | `--moss-wash` | `#e6eee5` | Status and empty state |
| Sand wash | `--sand-wash` | `#efe9da` | Secondary label |

`--moss` is reserved for interactive emphasis; all other colors establish reading hierarchy.

## 3. Typography

| Level | Size | Weight | Line height | Usage |
| --- | --- | --- | --- | --- |
| Display | `clamp(2.25rem, 6vw, 4.5rem)` | 600 | 1.02 | Catalogue identity |
| H1 | `clamp(1.75rem, 4vw, 3rem)` | 600 | 1.12 | Page title |
| H2 | `1.25rem` | 600 | 1.3 | Card heading |
| Body | `1rem` | 400 | 1.6 | Reading copy |
| Caption | `0.75rem` | 600 | 1.4 | Labels and metadata |

- Serif: `"Noto Serif KR", "Iowan Old Style", "AppleMyungjo", serif` for editorial headings.
- Sans: `"Noto Sans KR", "Malgun Gothic", sans-serif` for navigation and body.
- Mono: `"SFMono-Regular", Consolas, monospace` for compact metadata.

## 4. Spacing & Layout

The base unit is 4px: `--space-2` 8px, `--space-3` 12px, `--space-4` 16px, `--space-5` 20px, `--space-6` 24px, `--space-8` 32px, `--space-10` 40px, `--space-12` 48px, and `--space-16` 64px. The content limiter is 1180px with a responsive inline gutter. The document owns vertical scroll; header and footer stay in normal document flow, so the empty catalogue cannot create competing scroll regions.

## 5. Components

### Masthead

- **Structure**: landmark header, brand link, single catalogue link, status label.
- **States**: link hover, keyboard focus, current route.
- **Accessibility**: visible focus ring; the tree-ring mark is decorative beside a text name.
- **Layout**: cluster that wraps before horizontal overflow.

### Catalogue Frame

- **Structure**: page label, display title, reading lead, search affordance, catalogue-status panel.
- **States**: default, focus, disabled empty state, later loading state.
- **Accessibility**: search input has an explicit label; disabled action explains why it is unavailable.
- **Layout**: content-limiter with an intrinsic two-column panel that becomes one column safely.

### Quiet Card

- **Structure**: bordered surface with an overline, heading, and body copy.
- **States**: default and later populated state; no decorative hover because it is not interactive.
- **Accessibility**: semantic section headings and readable Korean line length.
- **Layout**: stack.

### Browse Card and Shelf

- **Structure**: a linked, whisper-bordered card presents one real catalogue aggregation: its Korean-facing label, the number of matching documents, and a quiet action label. A shelf groups the cards under one editorial heading for continent, country, or institution type.
- **States**: default, hover (moss wash and moss rule), keyboard focus, and an empty shelf that is omitted rather than padded with placeholder categories.
- **Accessibility**: each card is one link with a descriptive accessible name; the count is text, not a chart or color-only cue. The landing search form retains an explicit label and submits to the full exploration route.
- **Layout**: continent and institution shelves use responsive intrinsic grids; country cards use a denser intrinsic grid. Every grid collapses to one column before a card can force horizontal scrolling.

## 6. Motion & Interaction

Interactive controls use a 150ms color and transform transition; active controls scale to 0.98. `prefers-reduced-motion` removes these transitions. There is no decorative entrance motion in this intentionally sparse shell.

## 7. Depth & Surface

The strategy is borders-only: warm tonal separation and `1px solid var(--rule)` define every surface. There are no drop shadows.

## 8. Accessibility Constraints & Accepted Debt

- Target WCAG 2.2 AA: body contrast exceeds 4.5:1, all interactive elements have a 3px moss focus outline, Korean copy uses natural line height, and reduced motion is respected.
- Browse counts are server-side, read-only SQLite aggregations. They are intentionally not cached as static values, so a category card never presents a fabricated total.
- The category system describes collecting source and institution context, not a content-topic taxonomy. Content-topic classification remains future product work.
