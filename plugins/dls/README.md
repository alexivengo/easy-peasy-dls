# Easy Peasy DLS plugin

Технический ID: `dls`.

Плагин предоставляет:

- **Easy Peasy DLS: процесс** — definition, implementation, review,
  remediation и acceptance;
- **Easy Peasy DLS: отладка** — root-cause-first bug workflow;
- plugin-local `scripts/dls.py`.

v0.11 использует 12 публичных команд, state v2, ReviewPack/ReviewIR v3 и один
strict structured review decision schema. Обычный пользователь работает
короткими запросами в Codex и не переносит CLI, SHA или artifact paths.
