"""Prompt builder for free-photo-edit (1-photo v1).

Contract: preset = prompt template; user instruction quoted verbatim (never
paraphrased); face/identity invariants repeated so the engine preserves
everything the user did not ask to change. Tagalog/English/Bisaya pass
through untouched.
"""

from __future__ import annotations

PRESETS = {
    "background": (
        "Replace ONLY the background with: {detail} "
        "Keep the subject — face, pose, clothing, lighting on them — "
        "exactly the same, with clean natural edges."
    ),
    "clothes": (
        "Change ONLY the clothing as described below. Keep the person's face, "
        "pose, body, lighting, and background exactly the same — new clothing "
        "fitted naturally with realistic folds and shadows."
    ),
    "restore": (
        "Restore and enhance this photo: remove scratches, noise, blur, and "
        "blemishes, fix faded colors and lighting. Keep the exact same "
        "subject, face, pose, composition — change NOTHING else."
    ),
    "custom": "{detail}",
}

INVARIANTS = (
    "Change ONLY what the instruction asks for. "
    "Keep the exact same subject identity, composition, lighting, "
    "camera angle, and background unless the instruction says otherwise."
)


def resolve_preset(preset: str, user_prompt: str) -> str:
    """Expand a preset name into a full instruction.

    - background: requires user detail (the new background).
    - clothes / restore: preset text + optional user detail appended.
    - custom / empty / unknown: returns the user prompt unchanged.
    """
    instruction = (user_prompt or "").strip()
    key = (preset or "").strip().lower()
    if not key or key == "custom" or key not in PRESETS:
        return instruction
    template = PRESETS[key]
    if key == "background":
        if not instruction:
            raise ValueError("background preset needs a description of the new background")
        return template.format(detail=instruction)
    if key == "custom":
        return instruction
    if instruction:
        return f"{template} Additional detail: {instruction}"
    return template


def build_edit_prompt(user_prompt: str, preset: str = "custom",
                      model_id: str = "flux-2-dev") -> str:
    """Build the full engine prompt for one edit job."""
    instruction = (user_prompt or "").strip()
    if not instruction:
        raise ValueError("prompt must not be empty")
    resolved = resolve_preset(preset, instruction)
    key = (preset or "custom").strip().lower() or "custom"

    return (
        f"[free-photo-edit | preset={key} | model={model_id}]\n"
        f"SCENE: Preserve the scene, backdrop, lighting, and photographic style "
        f"of the reference photo.\n"
        f"SUBJECT: Preserve the subject's identity exactly — same face/body/object, "
        f"same pose and framing, unless the instruction explicitly requests a change.\n"
        f'DETAILS: Apply this instruction verbatim: "{instruction}" '
        f"Resolved edit: {resolved} "
        f"Render any text, logos, or small features exactly as specified, "
        f"with correct spelling.\n"
        f"CONSTRAINTS: Photorealistic output. No extra watermark, caption, or border. "
        f"{INVARIANTS} "
        f"Final invariant check before saving: {INVARIANTS}"
    )
