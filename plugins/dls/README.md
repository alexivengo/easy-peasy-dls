# Easy Peasy DLS plugin

Технический ID: `dls`.

Плагин предоставляет:

- **Easy Peasy DLS: процесс** — definition, implementation, review,
  remediation и acceptance;
- **Easy Peasy DLS: отладка** — root-cause-first bug workflow;
- plugin-local `scripts/dls.py`;
- bounded `Stop` guard, который после доверия через `/hooks` сохраняет consent
  handoff и даёт максимум два автоматических продолжения независимо от Git
  activity;
- upgrade-safe hook bootstrap, который не ищет другую версию DLS при удалённом
  plugin cache и завершает старую задачу с явной диагностикой.

v0.11 использует 12 публичных команд, state v2, ReviewPack/ReviewIR v3 и один
strict structured review decision schema. Обычный пользователь работает
короткими запросами в Codex и не переносит CLI, SHA или artifact paths.
