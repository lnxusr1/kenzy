# Custom skills (overlay)

Drop your own `@skill` / `@fast_intent` Python files in this directory. They load
**in addition** to the built-in skills bundled with Kenzy
(`kenzy/llm/builtin_skills/`). A file here that defines a skill of the same name
overrides the built-in one.

Disable any skill — built-in or custom — by its function name under
`skills.disabled` in `configs/llm.yaml`.

This directory is the user overlay for a source/dev checkout; an installed copy
uses `~/.config/kenzy/skills/` (created by `kenzy-init`).
