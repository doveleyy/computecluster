# Home Platform design system

This is the visual system shared by the Home Platform control plane and its
household apps. Each repository holds a byte-identical copy of this file and
of `tokens.css`. A change to either one lands in every repository that uses
it, in the same release cycle.

The system describes a dark instrument panel. A near-black canvas carries a
faint 32px measuring grid. Hairline borders separate surfaces. Monospace type
sets titles, labels, numbers and navigation. A system sans sets prose and form
input. Each app owns one accent colour, used for its action, its current
selection and its data.

## Enforcement

Every stylesheet takes its values from the tokens below. Each repository's
design-token test fails when a rule outside `tokens.css` does one of these
things:

- sets a padding, margin, gap or inset with a raw length
- sets a font size, border radius or letter spacing with a raw value
- writes a colour literal (`#hex`, `rgb()`, `hsl()`)
- uses a `max-width` or `min-width` media query that is not a breakpoint
- reads a `--token` that `tokens.css` does not declare and the rule does not
  define locally
- declares a token whose value differs from the tables in this file

The test reads these tables, so this file and `tokens.css` cannot drift
apart. Zero is written as `0`. A negative space is written as
`calc(-1 * var(--space-N))`.

## Colour

| Name | Token | Value | Role |
|------|-------|-------|------|
| Canvas | `--bg` | `#050505` | Page background |
| Sunken | `--sunken` | `#080808` | Tab rails, the platform bar, inset wells |
| Surface | `--surface` | `#0a0a0a` | Panels, cards, tiles, inputs |
| Raised | `--raised` | `#101010` | Hovered rows and controls |
| Line | `--line` | `#242424` | Hairline borders and dividers |
| Strong | `--strong` | `#383838` | Hovered borders, chart axes |
| Dim | `--dim` | `#5d5d5d` | Disabled text and decoration only |
| Muted | `--muted` | `#8b8b8b` | Labels and secondary text |
| Text | `--text` | `#f3f3f3` | Body text, titles, the selected tab fill |
| Cyan | `--cyan` | `#55d7ff` | Water, the control plane, the default focus ring |
| Green | `--green` | `#4ce391` | Money, success, a completed state |
| Violet | `--violet` | `#b49cff` | Study and focus time |
| Yellow | `--yellow` | `#f2ca52` | Warning, pending |
| Red | `--red` | `#ff6666` | Errors, overspend, destructive confirmation |
| Red line | `--red-line` | `#642d2d` | The border of an error panel |
| Grid line | `--grid-line` | `rgba(255, 255, 255, .017)` | The canvas grid |
| Tint | `--tint` | `rgba(255, 255, 255, .04)` | Badge and chart fills |
| Tint strong | `--tint-strong` | `rgba(255, 255, 255, .09)` | Badge borders, chart gridlines |
| Scrim | `--scrim` | `rgba(0, 0, 0, .72)` | Behind an open dialog |
| Accent | `--accent` | `var(--cyan)` | The page's own colour; a page sets it to one accent token |

Muted on Surface measures about 5.6:1 and meets WCAG AA for body text. Dim
measures about 3:1, so it never carries information a person needs to read.
A page may set `--accent` and component variables to a colour token. It never
redefines a grey.

## Type

Two families. `--mono` sets titles, labels, numbers, navigation and data.
`--sans` sets prose, form input and body text.

| Name | Token | Value |
|------|-------|-------|
| Mono | `--mono` | `ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace` |
| Sans | `--sans` | `-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif` |

The size scale is fixed. Responsive pages change step at a breakpoint and do
not scale text with the viewport.

| Step | Token | Value | Role |
|------|-------|-------|------|
| 1 | `--text-1` | `10px` | Uppercase labels, tabs, platform bar links, chart ticks |
| 2 | `--text-2` | `11px` | Metadata, badges, inline mono text |
| 3 | `--text-3` | `12px` | Small body, table cells, helper values |
| 4 | `--text-4` | `14px` | Body text and form input |
| 5 | `--text-5` | `16px` | Tile and row names |
| 6 | `--text-6` | `20px` | Card headings |
| 7 | `--text-7` | `24px` | Section headings, the page title on a phone |
| 8 | `--text-8` | `32px` | The page title, panel figures |
| 9 | `--text-9` | `48px` | A page's secondary figure |
| 10 | `--text-10` | `72px` | A page's one hero figure |

Nothing is smaller than 10px. Weights are 400, 500 and 600. Nothing is bold.

| Name | Token | Value | Role |
|------|-------|-------|------|
| Caps | `--track-caps` | `.13em` | Uppercase labels |
| Nav | `--track-nav` | `.08em` | Tabs, buttons and bar links in capitals |
| Display | `--track-display` | `-.06em` | Mono titles and figures at step 6 and above |

All other text uses the default letter spacing. Numbers in tables, timers,
totals and dates use `font-variant-numeric: tabular-nums`.

## Space

The base unit is 4px. Elements in a group sit 8px apart. Panels sit 12px
apart. A section sits 24px below the one before it.

| Step | Token | Value |
|------|-------|-------|
| Half | `--space-half` | `2px` |
| 1 | `--space-1` | `4px` |
| 2 | `--space-2` | `8px` |
| 3 | `--space-3` | `12px` |
| 4 | `--space-4` | `16px` |
| 5 | `--space-5` | `24px` |
| 6 | `--space-6` | `32px` |
| 7 | `--space-7` | `48px` |

## Shape and size

| Name | Token | Value | Role |
|------|-------|-------|------|
| Small | `--radius-1` | `4px` | Badges, progress bars, chart bars |
| Medium | `--radius-2` | `8px` | Buttons, inputs, tabs, inset frames |
| Large | `--radius-3` | `12px` | Panels, cards, tiles, tab rails, dialogs |
| Round | `--radius-round` | `9999px` | Dots and pills |
| Control | `--control` | `40px` | Buttons, inputs and tabs |
| Small control | `--control-sm` | `32px` | Text buttons and inline links |
| Page width | `--page-width` | `1240px` | The page frame |
| Grid cell | `--grid-cell` | `32px` | The canvas grid |

Surfaces separate with a 1px Line border. Nothing casts a shadow or a glow.

## Layout

Breakpoints are `840px` (narrow: one column) and `600px` (phone). `380px` is
allowed for the smallest phones. Media queries use only these widths.

Every page uses the same frame:

- **Frame.** `.shell` is `min(var(--page-width), 100%)` wide and centred.
  Padding is `--space-5` at the top, `--space-4` at the sides and `--space-7`
  at the bottom, each raised to the safe-area inset when that is larger. The
  sides drop to `--space-3` at 840px. Above 600px the root reserves the
  scrollbar gutter (`scrollbar-gutter: stable` in `tokens.css`), so a page
  that starts or stops scrolling never moves the frame sideways. Phones
  overlay their scrollbars and keep the full width.
- **Header.** A row at the top of the frame, at least `--control` tall, so
  the title sits at the same height whether or not the row holds a button.
  The page title sits on the left with its bottom edge on the row's bottom.
  The right-hand group shares that bottom edge. The gap is `--space-4`. The
  header sits `--space-5` above the page body. At 600px the right-hand group
  may move under the title.
- **Title.** Mono, weight 600, `--text-8`, line height 1, `--track-display`.
  It is `--text-7` at 600px. A punctuation mark inside or after the title is
  Dim.
- **Panel.** Surface fill, 1px Line border, `--radius-3`. A panel head is
  `--space-3` tall with a Line border under it. Its side padding matches the
  body below it, so the title lines up with the content. Its title is a
  Label in Text colour. A count or period on the right is a Label in Muted.

### Rhythm

Space groups the content: tight inside a group, generous between groups, and
more space above a heading than below it.

- **Body.** Tabs sit `--space-5` above the first panel. Panels and cards in
  a column or grid sit `--space-3` apart, including panels with their own
  head. A section heading outside a panel sits `--space-6` below the block
  before it and `--space-3` above its own content.
- **Cards and dialogs.** A card inside a panel, and a dialog body, has
  `--space-5` padding, or `--space-4` at 600px. A panel body that holds a
  list or table has no padding of its own, so rows reach its edges.
- **Rows.** A row in a list or table is at least 48px tall: `--space-4`
  above and below one line of text, `--space-4` at each side.
- **Facts.** A cell in a fact grid has `--space-3` padding. Its label sits
  `--space-1` above the value.
- **Forms.** A label sits `--space-2` above its control. A hint sits
  `--space-2` below it. Fields sit `--space-5` apart. A group heading inside
  a form sits at least `--space-6` below the field before it and `--space-3`
  above its first field.
- **Messages.** A status, empty or error line sits `--space-3` from the
  block it describes. It never sits between the tabs and a heading.
- **Balance.** A box is as tall as its content plus its padding. Cards and
  tiles set no minimum height. Cards in one row share a height only when they
  show the same kind of content. Then only a footer row sits at the bottom,
  so footers line up across the row; the rest of the content stays together
  at the top. Unlike cards align to the top of the row. Dense lists and roomy
  cards follow the same rhythm, so no box on a page looks empty beside a
  crowded one.

## Components

- **Button.** `--control` tall, `--space-3` side padding, `--radius-2`, Surface
  fill and a Line border. Hover raises the fill to Raised and the border to
  Strong. Disabled drops to 50% opacity.
- **Primary button.** Text fill with Canvas text. Hover mixes the fill to
  `color-mix(in srgb, var(--text) 88%, var(--bg))`. One per view, on the
  action that finishes the task.
- **Input.** `--control` tall, `--space-3` side padding, `--radius-2`, Sunken
  fill, Line border. Focus shows the focus ring.
- **Tabs.** A rail with Sunken fill, Line border, `--radius-3` and `--space-1`
  padding. The rail is as wide as its tabs. At 600px it spans the frame and
  the tabs share its width in one row. Each tab is `--control` tall,
  `--radius-2`, a `--text-1` uppercase label at `--track-nav`. The current
  tab fills with Text and Canvas text.
- **Label.** Mono, weight 600, `--text-1`, uppercase, `--track-caps`, Muted.
- **Badge.** `--text-2` mono, Tint fill, 1px Tint strong border, `--radius-1`,
  padding `--space-1` by `--space-2`.
- **Focus ring.** `2px solid var(--accent)` with a 2px offset on every link,
  button, input and summary. A row that fills its container draws the ring
  2px inside its own edge, so the container cannot clip it and it never
  paints onto the next row.
- **Charts.** Series take the page accent. Axes and ticks use Strong and
  Muted at `--text-1`. Gridlines use Tint strong. Chart scripts read colours
  from these tokens and never hard-code one.

## Rules

These rules adapt Impeccable's craft floor and operate mode, WCAG 2.2, and
the owner's own copy rule.

### Copy

- Every piece of text carries information the interface cannot show without
  it: names, dates, amounts with units, axis labels, section titles.
- No captions that narrate what a chart or control already shows. No
  instructions such as "Tap a day to see more". The affordance and the state
  explain the interaction.
- No label above a heading unless it holds a fact the heading lacks, such as
  a date or a count.
- A heading states a fact. It never sets a mood.
- A control names its action. An error names the problem and the next step.
- An empty state says what is missing in one line and offers the control
  that fills it.

### Visual

- One accent per page. It marks the primary action, the current selection and
  the data series. Inactive states stay grey.
- Show state with a word or a shape as well as a colour.
- Body text meets 4.5:1 contrast and large text 3:1.
- Every interactive element has default, hover, focus and disabled states. A
  form also shows its error and loading states.
- Theme the parts the browser draws: text selection, caret, scrollbars and
  the focus ring.
- Transitions last 150 to 250ms and show a change of state. Honour
  `prefers-reduced-motion`.
- No gradient text, glass, glow, side-stripe borders or nested cards.
- The same control looks the same on every page and in every app.
