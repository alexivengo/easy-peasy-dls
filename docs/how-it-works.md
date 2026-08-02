# Как работает Easy Peasy DLS

## Три задачи вместо процессного лабиринта

### 1. Definition

Codex формулирует outcome, scope, requirements и подход. Standard/critical
definition получает независимый semantic review. Пользователь явно подтверждает
definition и, когда нужно, отдельные architecture/design decisions. DLS
показывает одну связанную с текущим state карточку; пользователь отвечает только
`Да` или `Нет`, не копируя digests.

### 2. Implementation

Задача может быть открыта в основном checkout. До чтения product source DLS
разрешает единственный owner-worktree по Git identity; если его ещё нет —
готовит автоматически от committed HEAD. Dirty caller остаётся нетронутым.
Codex выполняет чтение, правки, тесты и commit только в owner.

Codex реализует подтверждённый contract и запускает focused tests. После commit
один `candidate-ready`:

- проверяет решения и dependencies;
- последовательно выполняет trusted named commands;
- записывает компактное exact-HEAD evidence;
- создаёт текущий ReviewPack.

Implementation-задача останавливается. Пользователь открывает свежую review-задачу.

Если модель преждевременно завершает implementation, Stop guard может дважды
автоматически вернуть её к работе. Это абсолютный лимит одного пользовательского
запроса: активность в Git его не расширяет. Третья остановка заканчивается
явной диагностикой без дополнительного model call. После обновления плагина
нужны перезапуск Codex и свежая задача; захваченный удалённый hook завершается
fail-open с `dls-hook-upgrade-required`.

### 3. Independent review

`review-run --kind code` работает в disposable exact-HEAD read-only worktree.
Routine/standard получают один Terra/high analysis. Critical получает второго
Sol reviewer только для trust, data, reliability или contract risk и только
когда primary не нашёл actionable problem.

Один primary blocker или should-fix немедленно означает canonical `not-clear`:
secondary и reconciliation больше не запускаются. Прямое противоречие reviewers
получает compact reconciliation без product checkout. Некорректный JSON
получает один repair без source.

После `review-clear` DLS показывает reviewed HEAD и definition и спрашивает:
`Принять результат? Да / Нет.` Ответ `Да` атомарно связывается с этой карточкой;
при drift запись отклоняется. Release и production не выводятся автоматически.

## Remediation

Новая implementation-задача читает только current findings из `status
--details findings`, исправляет код, помечает их `addressed` или `note`, коммитит
и снова вызывает `candidate-ready`. Только новый независимый review может
поставить `verified`.

## Параллельная работа

Один change имеет одного writer. Разные changes могут жить в разных Git
worktree, но пользователь продолжает открывать основной проект. DLS хранит лишь
stable Git worktree identity. Единственная dependency
означает: implementation текущего change требует accepted reviewed HEAD другого
change в Git ancestry.

## Что DLS намеренно не делает

- не создаёт Codex-задачи и subagents автоматически;
- не исполняет команды из Markdown или model output;
- не требует ADR, worktree, TDD или epic package для каждой задачи;
- не хранит transcript/recovery history как новый source of truth;
- не переносит, не удаляет и не stash'ит dirty draft автоматически;
- не считает validation равной review, а acceptance равной release.
