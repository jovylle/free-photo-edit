#!/bin/sh
# Deploy the static dev frontend to Cloudflare Pages, pointed at the dev API.
# Usage: scripts/deploy-dev-frontend.sh [api-base]
# Default: the PRODUCTION api. Dev is a frontend-only staging surface (CORS
# allows freephoto-uft1.pages.dev); there is no separate dev backend.
set -eu
API="${1:-https://freephoto.uft1.com}"
OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT
cp -R web/. "$OUT/"
# The repo keeps api-base empty so the container can serve same-origin.
sed -i '' "s|name=\"api-base\" content=\"\"|name=\"api-base\" content=\"$API\"|" "$OUT"/*.html
npx wrangler pages deploy "$OUT" --project-name freephoto-uft1 \
  --branch main --commit-dirty=true
