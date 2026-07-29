# Easy Peasy DLS — plugin

Codex-плагин для управляемой, risk-adaptive доставки изменений в Apple, Android, web и backend репозиториях.

В runtime остаются два guarded skills: их можно выбрать явно, а основной workflow
также активируется автоматически только при однозначном DLS-контексте:

- `$dls-workflow` — функции, изменения, спецификации, implementation, review и acceptance;
- `$dls-debug` — evidence-led поиск и исправление багов.

Domain skills могут добавлять экспертизу платформы, но не владеют DLS approvals, state, evidence и gates.

## Локальный CLI

Kernel использует только Python standard library:

```text
python3 dls/scripts/dls.py --help
python3 dls/scripts/dls.py --root <repository> init --dry-run
python3 dls/scripts/dls.py --root <repository> doctor
python3 dls/scripts/dls.py --root <repository> adopt --help
```

Mutations используют expected state revision и caller-stable operation ID. `--json` предназначен для machine handoff, `--dry-run` — для предварительного просмотра.

Repository-owned команды задаются argv arrays в `.dls/config.toml`. DLS не исполняет command text из Markdown или model output.

`dls adopt` регистрирует совместимый существующий change/epic package без переписывания authored files.

`dls worktree create` создаёт selective linked worktree от явно разрешённого
Git SHA; `dls worktree register` связывает change ID с owner checkout в
локальной metadata общего Git common-dir. `dls dependency` хранит stage-aware
same-repository зависимости, а bounded `dls delivery-map` показывает, какие
changes можно вести параллельно. Один writer остаётся scoped к change/worktree,
поэтому независимые validation/review pipelines не получают глобальную
блокировку. DLS не создаёт Codex-задачи, не сканирует соседние ветки и не делает
rebase/merge автоматически.

Actionable `not-clear` import атомарно сохраняет ReviewIR и canonical
digest-bound remediation manifest. `dls remediation-start` проверяет этот
manifest, а `dls remediation-recover` восстанавливает отсутствующий manifest
старого ReviewIR из reviewed Git objects без переключения checkout. Исторические
review остаются читаемыми, но не попадают в рабочий remediation context.

`dls review-ready` проверяет committed candidate, definition approval, готовность tickets, stage-specific current evidence и dispositions findings. Команда создаёт ReviewPack v2 и возвращает `open-review-task` либо одно типизированное блокирующее действие. Для remediation epic base выводится из последнего ReviewIR; первый review требует явный `--base`.

`dls review-run` — публичный end-to-end review orchestrator. Он работает только с
текущим change state, явно зарегистрированным worktree или absolute pack,
выбирает pack текущего HEAD, выполняет state-owned model lanes и атомарно
импортирует ReviewIR. Повторный процесс видит `running` и ждёт вместо запуска
дубликата. `review-start` остаётся native-only diagnostic primitive, а
`review-status` никогда не запускает модель. Его компактный progress показывает
активный этап, завершённые lanes, elapsed time и локальные token counters. Если
модели завершились, а сборка ReviewIR упала, повторный `review-run` использует
сохранённые digest-bound outputs и не вызывает модели заново.

Implementation/remediation-задача не запускает review runner: после
`candidate-ready` она передаёт candidate в отдельную read-only review-задачу.
Если exact-HEAD remediation pack отсутствует, `review-run` может сам выполнить
только доказуемую механику handoff: trusted validation и создание pack из уже
записанных current-HEAD dispositions. Product source остаётся read-only.

ReviewPack/ReviewIR v2 сохраняют читаемость исторических v1 artifacts, считают последний импортированный ReviewIR текущим canonical snapshot, делают закрытие finding ответственностью reviewer и требуют targeted remediation pass плюс финальный whole-change semantic pass перед повторным `review-clear`. Implementer `note` разрешает только независимое adjudication и сам finding не закрывает.

После canonical review import и human `accept` DLS автоматически возвращает
Delivery Receipt: краткий русский Markdown и bounded JSON одного change. Он
детерминированно выводится из state, exact-HEAD evidence, ReviewIR и approvals,
не запускает модель, не записывается в cache или репозиторий и сохраняет
отдельные границы review, acceptance, release и production.

## Структура

- `.codex-plugin/plugin.json` — plugin metadata;
- `skills/` — компактные guarded routers и одноуровневые references;
- `scripts/dls.py` — portable entry point;
- `scripts/dls_core/` — atomic state, gates, context, evidence и review;
- `assets/templates/` — условные authored contracts;
- `assets/schemas/` — versioned JSON contracts;
- `assets/profiles/` — generic profile и Apple adapter;
- `tests/` — unit, fault, CLI, runner и review-integrity tests.

Публичная документация и установка находятся в [корневом README](../../README.md).
