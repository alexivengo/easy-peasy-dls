# Технический справочник

Этот документ нужен для диагностики и разработки DLS. Для обычной работы достаточно `$dls-workflow` и `$dls-debug`.

## Состав плагина

```text
plugins/dls/
├── .codex-plugin/plugin.json
├── skills/
│   ├── dls-workflow/
│   └── dls-debug/
├── scripts/
│   ├── dls.py
│   └── dls_core/
├── assets/
│   ├── profiles/
│   ├── schemas/
│   └── templates/
└── tests/
```

CLI использует только Python standard library. Он не исполняет shell-текст из Markdown или ответа модели: validation commands задаются в `.dls/config.toml` как argv arrays.

## CLI

Из checkout этого репозитория:

```sh
python3 plugins/dls/scripts/dls.py --help
python3 plugins/dls/scripts/dls.py --root /path/to/project doctor
```

Основные команды:

| Команда | Назначение |
|---|---|
| `init` | Создать repository-local DLS state и конфигурацию |
| `doctor` | Проверить готовность плагина и репозитория |
| `new` | Создать минимальный change package |
| `adopt` | Зарегистрировать совместимый существующий пакет без переписывания |
| `worktree` | Явно связать change ID и linked worktree |
| `status` | Показать производное состояние изменения |
| `check` | Выполнить детерминированные gates |
| `context` | Создать digest-bound context manifest |
| `approve` | Записать scoped human decision |
| `ticket` | Изменить canonical ticket state |
| `validate` | Запустить доверенную repository command |
| `evidence` | Импортировать immutable validation evidence |
| `review-pack` | Создать exact-revision review handoff |
| `remediation-start` | Выбрать последний актуальный ReviewIR и создать manifest |
| `review-ready` | Проверить candidate и создать full/delta ReviewPack |
| `review-start` | Запустить native lane и подготовить semantic review |
| `review-import` | Атомарно проверить и импортировать ReviewIR |
| `finding` | Отметить addressed, waived, reopened или note |

Для machine handoff используйте `--json`. Для поддерживаемых mutations доступны `--dry-run`, expected state revision и caller-stable operation ID.

## Repository contract

После `init` проект хранит:

- `.dls/config.toml` — profile и доверенные команды;
- authored change documents — definition и tickets;
- state — approvals, execution status, evidence и findings;
- immutable context/review/remediation artifacts;
- локальный cache с сырыми review transcripts.

Cache, locks, temporary files и сырые model transcripts не должны попадать в Git. Canonical artifacts и политика их хранения зависят от профиля проекта.

## Review integrity

ReviewPack v2 связывает:

- `epic_base_sha` и `comparison_base_sha`;
- candidate HEAD;
- definition digest;
- prior ReviewIR и remediation manifest;
- current evidence;
- deterministic risk lenses.

ReviewIR v2 обязан содержать ticket verdicts, provenance review lanes и verdict для каждого прежнего actionable finding. Implementer записывает только `addressed`; `verified` появляется исключительно при независимом импорте нового ReviewIR.

Повторный `review-clear` требует непрерывной native coverage chain и final whole-change semantic pass.

## Проверка исходников

```sh
python3 plugins/dls/scripts/run_tests.py
python3 scripts/validate_public_repo.py
```

Перед release также запускаются системные Codex skill/plugin validators и disposable marketplace install.

## Совместимость

- Python: 3.11 и новее.
- Git обязателен для exact-revision review.
- State schema остаётся v1; ReviewPack и ReviewIR v1 читаются исторически.
- Generic profile предназначен для разных стеков.
- Apple profile — первый углублённо проверенный platform adapter.

## Версионирование

GitHub releases используют обычные теги, например `v0.2.0`. Plugin manifest добавляет build metadata `+codex.<cachebuster>`, чтобы Codex отличал обновлённые локальные и marketplace bundles без искусственного изменения feature version.
