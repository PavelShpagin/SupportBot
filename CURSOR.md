# SupportBot – Global AI Instructions

These instructions apply to every AI-assisted session in this repository.

## API Cost Discipline

- **Be cautious with Gemini API usage.** Do NOT run large-scale evaluations, batch OCR, or multi-model experiments without an explicit request.
- Only call Gemini/OpenAI APIs for minimal, targeted tests or the actual product logic.
- **ALERT: if the remaining API quota or budget drops below $100, add a prominent `⚠ BUDGET LOW: <$100 REMAINING` warning here and stop any non-essential API calls immediately.**

## Deployment

- **Deploy ONLY to the Oracle VM at `161.33.64.115`.** Never deploy locally, never spin up local Docker stacks or databases for production use.
- Scripts: `./scripts/deploy-oci.sh full` (full redeploy), `./scripts/deploy-oci.sh push` (push code + restart).
- SSH key: `~/.ssh/supportbot_ed25519`

## Commit Discipline

- **After every small self-contained change, commit and push immediately.** Do not accumulate large multi-feature commits.
- Commit format: `type(scope): short description` (e.g. `fix(ingest): handle missing json column`).

## Architecture Constraints

- Backend: signal-bot (FastAPI), signal-ingest, signal-desktop (headless), MySQL, Chroma.
- No Oracle DB (legacy only).
- Images served from `/var/lib/signal/bot/` via `/static/` mount.
- Tests live in `tests/` and run with `pytest`.
