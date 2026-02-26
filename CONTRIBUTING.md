# Contributing

## Workflow
1. Create a feature branch from `main`.
2. Keep changes focused and small.
3. Run local checks before opening PR:
   - `pytest -q`
4. Open a pull request with:
   - problem statement;
   - implementation summary;
   - validation notes.

## Coding rules
- Respect existing project structure and naming.
- Do not commit secrets, private keys, `.env` files.
- Prefer minimal safe changes over broad refactors.

## Review expectations
- CI/tests must pass.
- Required GitHub checks for merge: `Tests`, `Lint`.
- Security-impacting changes must include rationale in PR description.
- Architecture-impacting changes should include or update an ADR in `docs/adr/`.
