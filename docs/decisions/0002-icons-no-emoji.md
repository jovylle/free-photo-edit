# 0002 — No emoji; icons come from one SVG sprite

**Status** — accepted (2026-11-14)

**Context** — Every emoji in the UI (brand, theme toggle, presets, buttons,
history status) was a text character, so it rendered differently per OS,
could not inherit the palette, and read as decorative rather than designed.

**Decision** — The site uses no emoji. All icons are outline glyphs in
`web/icons.svg` (15 `<symbol>`s, 24×24, `stroke="currentColor"`, 1.8px),
referenced as `<svg class="ico"><use href="icons.svg#i-name"></use></svg>`.
`.ico` (in `site.css`) sizes them to `1em` so they follow surrounding text.
The favicon is a data-URI SVG of the sparkle mark, not an emoji.
`api/server.py` preset labels carry no emoji; the client picks the icon by
preset id, so labels stay plain text.
Page shell (header, main, footer, `.page`) is one 1024px container.

**Consequences** — Icons inherit `currentColor` and size with the text; a new
icon is one `<symbol>` plus one `<use>`. Decorative prose emoji are simply
dropped rather than replaced. Any new icon must be added to the sprite, not
inlined per page.
