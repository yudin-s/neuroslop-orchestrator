# GitHub Branch Protection (main)

Рекомендуемая конфигурация для ветки `main`:

1. Settings → Branches → Add branch protection rule
2. Branch name pattern: `main`
3. Включить опции:
   - Require a pull request before merging
   - Require approvals: `1`
   - Dismiss stale pull request approvals when new commits are pushed
   - Require status checks to pass before merging
   - Require branches to be up to date before merging
   - Do not allow bypassing the above settings
4. В `Required status checks` добавить:
   - `Tests`
   - `Lint`

## Why
- `Tests` гарантирует, что базовая функциональность не сломана.
- `Lint` гарантирует минимальную чистоту и единообразие кода.
