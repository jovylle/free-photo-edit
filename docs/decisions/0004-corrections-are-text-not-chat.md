# 0004 — Corrections are a plain text re-edit, not a chat

**Status** — accepted (2026-11-15)

**Context** — A finished photo is often "close but wrong": the background is busy,
the shirt is off, the light is flat. The owner asked for a voluntary correction path,
sketched as a chat with a vision model that inspects the result ("I can see this
image, the background looks dirty") and then calls the engine again.

**Decision** — Ship the narrow version: a **"Need a correction?"** button on any
finished card opens one textarea. Sending posts the *result PNG* back to the existing
`POST /api/edit` with `preset=custom` and the user's sentence, debits one credit, and
flows through the normal job pipeline. No chat model, no extra endpoint, no new key.

**Why not the chat** — It would add a vision call per turn, a new provider key on the
server (the live `.env` holds only `OPENAI_API_KEY`), a new way for the photo to leave
the box, and a new spend surface to rate-limit — all to turn a sentence into the same
instruction the user could type. The corrections themselves still cost 2.6¢ each, so
the chat's value is UX polish, not capability. Revisit only if corrections get used
and people write unusable instructions.

**Consequences** — Corrections are optional and invisible until clicked, so the base
flow is untouched. Each correction is a new job fed the previous *output*, which is
right for small tweaks but compounds artifacts if a user chains many; there is no
chain cap yet, and history shows repeated "custom" rows. Both are acceptable while the
feature proves itself — the next step, if it sticks, is a per-job correction count and
a "start again from the original" option.
