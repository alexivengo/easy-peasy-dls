# Технический справочник

Этот документ нужен для диагностики и разработки DLS. Для обычной работы
достаточно выбрать в Codex навык **Easy Peasy DLS: процесс** или
**Easy Peasy DLS: отладка**.

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
| `remediation-start` | Проверить canonical manifest последнего actionable ReviewIR |
| `remediation-recover` | Восстановить отсутствующий legacy manifest из exact Git objects |
| `candidate-ready` | Выполнить trusted validation, записать dispositions и атомарно создать ReviewPack |
| `candidate-status` | Прочитать компактный implementation progress без запуска команд |
| `review-ready` | Проверить candidate и создать full/delta ReviewPack; base повторного review выводится из ReviewIR |
| `review-run` | Выполнить exact-revision review целиком и импортировать ReviewIR |
| `review-status` | Прочитать состояние review без запуска модели |
| `review-start` | Native-only primitive для совместимости и диагностики |
| `review-import` | Атомарно проверить и импортировать ReviewIR |
| `finding` | Отметить addressed, waived, reopened или note |

Для machine handoff используйте `--json`. Низкоуровневые mutations сохраняют
`--dry-run`, expected state revision и caller-stable operation ID.
`candidate-ready` сам владеет revisions и детерминированным operation ID.

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

Один finding может ссылаться на несколько validation evidence records. CLI принимает
`--evidence A B`, повторённые `--evidence A --evidence B` и comma-separated
`--evidence A,B`, нормализует порядок, удаляет дубли и возвращает
`evidence_count`. Это позволяет отдельно связать с finding, например, Swift suite
и проверку JavaScript bridge, а не оставлять вторую проверку только на уровне
ReviewPack.

Обычный implementation/remediation-flow не вызывает эти primitives вручную.
`candidate-ready` требует явный `policy.review_required_commands`, выполняет их
последовательно и прикрепляет весь обязательный successful evidence set к
каждому `addressed` finding. `--extra-command` принимает только дополнительный
named command из repository config.

Generated evidence содержит command-contract digest, exact HEAD/source digest,
exit status, duration, размер и SHA-256 полного command output. Полный redacted output хранится
только в ignored `.dls/cache/validation`; successful evidence не переносит его в
ReviewPack/model context. Для failure сохраняется ограниченный excerpt.

Последний импортированный ReviewIR является canonical finding snapshot для remediation и gates; более ранние результаты остаются audit history. `note` означает запрос на независимое adjudication, а не закрытие или waiver.

Повторный `review-clear` требует непрерывной native coverage chain и final whole-change semantic pass.

### Инвариант review → remediation

Импорт actionable `not-clear` или `blocked` review атомарно записывает два
immutable artifacts — ReviewIR и remediation manifest — и ссылки на оба в одной
state revision. Успешный runner result поэтому всегда возвращает
`review_result_path`, а actionable результат дополнительно возвращает
`remediation_manifest_path`.

Для интерфейса Codex `review-run` и завершённый `review-status` также возвращают
производный объект `presentation` с контрактом `codex-inline-comments/v1`.
Он содержит безопасные absolute owner-path, строки и готовые `::code-comment`
directives для blocker/should-fix findings. Это только слой отображения:
каноническими остаются ReviewIR, finding ID и ticket verdict. Если текущий HEAD
уже отличается от reviewed HEAD или location нельзя безопасно разрешить внутри
owner checkout, inline-комментарий не создаётся, а finding остаётся в текстовом
отчёте.

Canonical manifest хранится в
`.dls/reviews/<change>/remediations/<review-id>.json`; очистка локального cache
его не удаляет. Manifest содержит только findings последнего canonical ReviewIR,
которые блокируют review или acceptance. Release/production-only gaps не
становятся code-remediation gate.

Исторические ReviewIR не переписываются. Если старый импорт был выполнен до
этого инварианта, `remediation-recover` проверяет ReviewIR, ReviewPack, definition
digest, существование reviewed commit и ancestry текущего HEAD. Authored inputs
читаются из reviewed Git tree; checkout, branch и product source не меняются.
Divergent history, dirty source и tampered artifacts блокируют recovery.

Implementation/remediation-задача после commit вызывает только
`candidate-ready` и заканчивается его `next_action: open-review-task`.
Pipeline использует optional `candidate_runs` в state schema v1, допускает один
активный exact-contract run, переиспользует PASS только при совпадении HEAD,
source и command-contract digests и атомарно записывает dispositions вместе с
ReviewPack. `candidate-status` читает phase, active/completed/remaining commands
и typed next action, но не запускает процесс и не возвращает логи. Только
отдельная read-only review-задача запускает `review-run`.

### End-to-end runner

`review-run` использует фиксированный pipeline:

1. native diff-review — `gpt-5.6-terra/high`;
2. до трёх deterministic specialist lanes для critical review —
   `gpt-5.6-terra/high`;
3. независимый semantic pass — `gpt-5.6-sol`, `high` или `xhigh`;
4. reconciliation на Sol;
5. remediation final-full pass, только когда targeted result не содержит
   review-blocking blocker;
6. DLS-owned сборка и атомарный импорт ReviewIR.

Model-runs выполняются через `codex exec` в read-only ephemeral режиме с
игнорированием пользовательской model-конфигурации. Native lane использует
встроенный prompt официального `codex exec review --base` и сохраняет его
bounded text result; текущий Codex CLI не применяет structured output schema к
этому subcommand. Все DLS-owned semantic decisions используют repository-owned
prompt templates и schemas. Перед модельным вызовом DLS локально проверяет
strict Structured Outputs contract: каждый object запрещает дополнительные
поля, а `required` точно совпадает с `properties`. Механически некорректная
schema поэтому останавливается до API-вызова. Native, semantic и specialist
lanes получают disposable detached worktree exact HEAD, поэтому локальные DLS
metadata не попадают в анализ candidate. Independent lanes не видят native
output или drafts соседних lanes; reconciliation получает их как digest-bound
inputs.

До запуска модели `StateStore` атомарно записывает attempt со статусом `running`.
Для сочетания `review ID + lane + pass` возможна только одна активная попытка.
Повторный `review-run` возвращает `status: running` и `next_action: wait-review`,
не запуская вторую модель. Внутренние operation IDs включают review ID, поэтому
одинаковая пользовательская метка не смешивает attempts разных ревизий.
`review-status` только читает state.

### Наблюдаемость и финализация

`review-status` по умолчанию возвращает компактный `progress`: текущий pipeline
stage, активную lane, количество completed/projected lanes, elapsed time,
последний переход, размер model-facing context и локально извлечённые Codex token
counters. Полные argv, cache paths и provenance доступны только с `--verbose`,
чтобы обычный heartbeat сам не расходовал контекст агента.

Skill не ждёт появления текста в stdout `review-run`: stdout зарезервирован для
единственного финального JSON. Пока исходный shell/session продолжает работать,
skill читает `review-status` отдельной read-only командой раз в 60–90 секунд и
сообщает только переход этапа или один короткий heartbeat. Сырые transcripts и
предварительные findings пользователю не транслируются.

Pipeline отдельно фиксирует `running`, `finalizing`, `failed-finalize` и
`completed`. Ошибка lane переводит pipeline в `failed` и сохраняет извлечённую
причину API/CLI, а `next_action` предлагает retry только когда runner
действительно может его выполнить. Если все model lanes завершились, но
deterministic assembly или atomic import не прошли, следующий `review-run`
проверяет exact HEAD, pack, context и output digests, переиспользует completed
attempts и повторяет только финализацию.

Canonical ticket verdicts не доверяются модели. DLS механически связывает
findings с tickets и вычисляет review state из severity и `blocks`. Поэтому
release/production-only note сохраняется в ReviewIR, но не делает code-review
ticket `not-clear`. Общий review verdict выводится из тех же stage-correct
relations.

Token counters являются локальной диагностической телеметрией. Они включают
cached context и повторные tool turns, поэтому не трактуются как точная стоимость
API. DLS не отправляет эти данные во внешний analytics service.

Подтверждённые `orphan`, `timeout`, `output-cap`, missing или invalid structured
output получают не более одной автоматической повторной попытки. Drift HEAD,
source, definition или pack не повторяется автоматически. Timeout одной попытки
— 30 минут; final output ограничен 256 KiB, JSONL transcript — 1 MiB с явным
признаком truncation. Model, effort, prompt, schema, context, pack и HEAD входят
в digest lane contract: после исправления самого DLS старая failed attempt
остаётся в истории, но не расходует retry budget нового контракта. Completed lane
переиспользуется только при точном совпадении этого digest.

Новые packs помечаются `runner_contract: dls-review-runner/v1`. Для них import
доверяет provenance только completed attempts из DLS state: модель возвращает
semantic decision, но не может сама объявить lane завершённым. Исторические
ReviewPack/ReviewIR v1 и v2 без marker остаются читаемыми как
`legacy-provenance` и не переписываются.

## Проверка исходников

```sh
python3 plugins/dls/scripts/run_tests.py
python3 scripts/validate_public_repo.py
```

Перед release также запускаются системные Codex skill/plugin validators и disposable marketplace install.

## Совместимость

- Python: 3.11 и новее.
- Git обязателен для exact-revision review.
- State schema остаётся v1; ReviewPack и ReviewIR v1, а также v2 без runner
  marker читаются исторически.
- Generic profile предназначен для разных стеков.
- Apple profile — первый углублённо проверенный platform adapter.

## Версионирование

GitHub releases используют обычные теги, например `v0.4.0`. Plugin manifest
добавляет build metadata `+codex.<cachebuster>`, чтобы Codex отличал обновлённые
локальные и marketplace bundles без искусственного изменения feature version.
