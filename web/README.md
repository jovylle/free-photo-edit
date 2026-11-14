# web/

Static UI, deployed as-is (Cloudflare Pages in dev). No build step, no bundler,
no secrets, no absolute backend host baked in.

**Interface** — talks only to `/api/*` on the origin in `meta[name=api-base]`
(empty value = same origin). `app.js` prefixes both fetches and result image URLs
with it.

**Files** — `index.html` (app), `about.html`, `privacy.html`, `terms.html`,
`site.css`, `site.js`, `app.js`, `gcash-qr.png`.

**Checks** — `node --check web/app.js` and `node tests/expired-card.cjs`, both run
by `scripts/check.sh`. The card renderer test slices `cardFor` out of `app.js`;
keep that function name and shape.
