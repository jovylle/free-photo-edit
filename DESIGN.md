# DESIGN.md — free-photo-edit

## Concept
A small, fast photo editor for strangers on the web: upload, edit, download. Quiet lilac paper, low contrast, dashed dividers (no solid borders), sharp 90-degree corners, one muted violet accent. Non-techy friendly.

## Palette
| token | value | use |
|---|---|---|
| `--paper` / `--bg` | oklch(0.95 0.010 290) / oklch(0.22 0.012 290) | page surface |
| `--paper-alt` / `--card` / `--input` | oklch(0.91 0.012 290) / oklch(0.27 0.012 290) | panels, insets, fields |
| `--ink` | oklch(0.36 0.012 290) / oklch(0.82 0.012 290) | primary text |
| `--ink-muted` / `--muted` | oklch(0.50 0.012 290) / oklch(0.68 0.012 290) | captions, secondary text |
| `--onaccent` | oklch(0.96 0.005 290) / oklch(0.96 0.005 290) | text on accent surfaces |
| `--err` | oklch(0.55 0.18 25) / oklch(0.70 0.18 25) | error text |
| `--accent` | oklch(0.48 0.10 280) / oklch(0.62 0.10 280) | links, one emphasis |
| `--accent-alt` | oklch(0.38 0.05 290) / oklch(0.72 0.05 290) | hover, secondary emphasis |

## Type roles
- Display: Manrope (400/500/700), used for headings and labels.
- Text: Karla (400/500), 16px base, 1.6 line height.
- Scale: 1rem base, 1.25 ratio. Max measure 68ch.

## Spacing & edges
- Space unit 0.25rem (4px). Radius 0px. No strokes, no shadows. Tonal blocks only.

## Motion budget
150ms ease, gentle hover only. Reduced motion respected.

## Layout DNA
Single centered column (max 1280px), generous vertical rhythm, tonal blocks instead of borders, flush-left text, one muted accent used sparingly.

## Allowed devices
- Desktop and mobile (390–1440px). Touch targets >= 44px.

## Banned for this project
- Solid borders, serif type, high contrast, drop shadows, gradients, rounded cards, ALL-CAPS eyebrows, decorative 01/02/03, arrows on every CTA.

## Reference links
- Screenshot or live reference: free-photo-edit-dev.uft1.com
- Fixed visual reference (preset section of preview.html): adopted from existing site.css (no preset; documented current look).