# E-CIP Research Library — Design System

## 0. Research Log

- Embedded refs: shortlisted `notion.md`, `wired.md`, and `claude.md` → picked `minimalist-skill.md` with `notion.md` for a quiet, warm, reading-first editorial grammar; its product-brand tokens are not copied.
- Lazyweb: skipped — the local-preview boundary forbids network calls and the app may not depend on external research assets.
- Imagen drafts: skipped — the catalogue must use no generated or remote visual assets; the signature is made with typography, paper layers, and CSS only.

## 1. Atmosphere & Identity

An unhurried Korean public-library reading room: warm paper, ink, a restrained vermilion shelf mark, and a vertical book-spine rhythm. The memorable signature is the **catalogue slip**: each record begins with a narrow coloured classification bar and a small archival label, so dense research metadata still feels like a book waiting on a shelf. This is an original local research preview, not an E-CIP visual clone.

## 2. Color

| Role | Token | Value | Usage |
|---|---|---:|---|
| Paper | `--paper` | `#f7f2e8` | Page canvas |
| Paper light | `--paper-light` | `#fffdf8` | Raised reading surfaces |
| Ink | `--ink` | `#26251f` | Main text |
| Muted ink | `--ink-muted` | `#6a665d` | Bibliographic metadata |
| Hairline | `--rule` | `#d5cdbd` | Dividers and card edges |
| Shelf | `--shelf` | `#a94032` | Interactive accent and book-spine marker |
| Shelf dark | `--shelf-dark` | `#772b22` | Hover and pressed states |
| Notice | `--notice` | `#efe1bb` | Local-review warning |
| Focus | `--focus` | `#155e75` | Keyboard focus ring |

The accent appears only on navigation, search focus, and classification markers. Paper surfaces are separated with hairlines and subtle tonal changes, not glass effects or heavy shadows.

## 3. Typography

Use installed local/system stacks only: `"Noto Serif KR", "Batang", Georgia, serif` for display and reading titles; `"Noto Sans KR", "Apple SD Gothic Neo", "Malgun Gothic", Arial, sans-serif` for controls and metadata. No font is downloaded.

| Level | Token | Size | Line-height | Use |
|---|---|---:|---:|---|
| Display | `--type-display` | `clamp(2.1rem, 6vw, 5.25rem)` | 1.02 | Catalogue heading |
| Title | `--type-title` | `clamp(1.75rem, 4vw, 3.25rem)` | 1.16 | Detail title |
| Card title | `--type-card-title` | `1.4rem` | 1.35 | Result title |
| Body | `--type-body` | `1rem` | 1.8 | Reading copy |
| Meta | `--type-meta` | `0.8125rem` | 1.55 | Labels and metadata |

Korean headings use balanced wrapping where supported, and narrow screens lower display size before text can fragment into single syllables.

## 4. Spacing & Layout

Base unit: `4px`. The reading column is `min(100% - 48px, 1160px)` on desktop and `min(100% - 32px, 1160px)` on mobile. Spacing tokens: `--s1: 4px`, `--s2: 8px`, `--s3: 12px`, `--s4: 16px`, `--s5: 24px`, `--s6: 32px`, `--s7: 48px`, `--s8: 64px`.

Catalogue cards are a single editorial list, not a generic tile grid: a spine rail, content column, and date column. At 768px and below, the date joins the metadata row and the list stays single-column.

## 5. Components & States

- **Masthead:** wordmark, a quiet local-preview notice, and an anchored catalogue return link.
- **Search field:** native labelled input with a result count; focus changes the outline to `--focus`.
- **Catalogue slip:** reusable result row with spine rail, title, concise introduction/availability status, metadata, and an internal detail link.
- **Metadata ledger:** definition list that pairs bibliographic labels with values.
- **Source link:** external text link with a small inline SVG arrow; each has `target="_blank"` and `rel="noopener noreferrer"`.
- **Empty state:** a specific message plus a reset-search button; an empty data set has a distinct catalogue-empty message.

Default, hover, keyboard focus, active, and empty states are intentionally visible. Repeated metadata/link patterns are rendered from the same functions rather than styled as one-offs.

## 6. Depth & Motion

Depth is paper-on-paper: a 1px hairline and a small `--lift` (`0 8px 24px rgba(61, 52, 37, 0.07)`) lift only for an actual book-title link hover/focus. That link changes border colour, depth, and `transform` over 160ms; it signals the opening action without pretending the whole row is clickable. Reduced-motion users receive no transition.

## 7. Responsive Behavior

At 768px, the masthead wraps and catalogue rows collapse their date column. At 480px, page gutters become `16px`, the display scale reaches its lower bound, action links stack, and long Korean titles use natural word wrapping without horizontal overflow. Touch targets are at least 44px tall for primary controls.

## 8. Accessibility, Personas & Accepted Debt

Primary users include a researcher scanning source provenance, a Korean-language reader on a narrow phone, and a keyboard-only reviewer checking the local preview. Landmarks, one `h1` per view, persistent visible focus, labelled search, semantic links, contrast-safe ink/paper colors, and `prefers-reduced-motion` support are required.

Accepted debt: the generated local sample contains mainly English source text and only two currently supplied introduction labels; absent-introduction UI is still implemented for future snapshots. External landing pages and PDFs are intentionally outside this local UI’s accessibility control.
