# Roadmap Easy Peasy DLS

Срез: 27 июля 2026 года

Текущая линия: `v0.4.4`

Этот roadmap использует KANO как практический способ расставить приоритеты для
solo AI-delivery. Must-be защищает доверие к процессу, Performance уменьшает
ручную работу и повтор контекста, Attractive добавляет удобство только после
стабилизации основного цикла.

## Что уже является базой

Must-be ядро реализовано и защищается regression-тестами:

- минимальные risk-adaptive пути вместо обязательного epic-пакета;
- scoped human approval и digest-bound staleness;
- trusted named validation commands и exact-HEAD evidence;
- зарегистрированные worktree без добавления каждого worktree как проекта;
- независимый native и semantic review с canonical ReviewIR;
- latest-only remediation manifest и reviewer-owned verification;
- одна implementation-команда `candidate-ready` и одна review-команда
  `review-run`;
- read-only disposable review workspaces, single-flight и bounded recovery.

Ранее открытые gaps launcher/PATH, semantic write prevention, remediation
launcher и единого review-ready handoff закрыты текущим CLI и skills.

## Сейчас: v0.4.4

| ID | KANO | Возможность | Результат |
|---|---|---|---|
| M51 | Must-be / P0 | Revision-safe candidate continuation | Новый descendant HEAD получает новый run, но наследует неизменившиеся dispositions только из проверенного ближайшего ancestor |
| M52 | Must-be / P0 | Восстановимая validation-диагностика | Потерянный shell payload восстанавливается как bounded redacted diagnostic без автоматической загрузки полного лога |
| P34 | Performance / P0 | Нулевой повтор finding bookkeeping | После validation fix модель передаёт только изменившиеся `addressed`/`note`, а не весь список findings |

Exit criteria v0.4.4:

- exact-HEAD retry сохраняет run и не дублирует успешные команды;
- новый HEAD повторяет весь обязательный validation policy;
- manifest, definition, policy, finding-set или ancestry drift запрещают
  автоматическое наследование;
- пользователь по-прежнему не вводит SHA, operation ID или evidence paths.

## Следующая волна: измерить эффективность

| ID | KANO | Возможность | Условие |
|---|---|---|---|
| P02 | Performance / P1 | Routine fast-path pilot | Реальный bug/chore должен пройти дешевле standard-пути без расширения пакета документов |
| P08 | Performance / P1 | Runtime telemetry | Использовать доступные token, latency и retry metrics без сохранения приватного prompt content |
| P09 | Performance / P1 | Legacy vs DLS baseline | Сравнить handoffs, model calls, документы, elapsed time и escaped findings; не обещать неподтверждённый процент экономии |
| P27 | Performance / P1 | Evidence/cache retention | Ввести измеримые cleanup-правила после анализа размера и повторного использования локальных artifacts |
| P33 | Performance / P1 | Краткий delivery status | Показывать ближайшее действие без чтения state, ReviewIR и transcript |

## Позже, только по данным

- P21 — второй platform adapter из реального web, backend или Android pilot;
- P25 — read-only derived status и traceability views;
- A01 — Figma, Sketch или другой design connector после UI-пилота и решения
  privacy/versioning;
- A06 — cross-project dashboard, только если CLI status перестанет справляться;
- release/production profiles — после стабильного acceptance loop в нескольких
  типах проектов.

## Что не станет частью продукта по умолчанию

Easy Peasy DLS не планирует:

- полностью автономный delivery без пользовательских решений;
- mandatory brainstorming, Plan Mode, TDD, worktree или epic для каждой задачи;
- per-ticket reviewers и subagent-driven routine implementation;
- model-authored shell, arbitrary command execution и скрытые hooks;
- автоматический human approval или изменение глобальной модели;
- release evidence как code-review blocker по умолчанию;
- SQLite, event sourcing и parallel writers без доказанной необходимости.

## Как roadmap пересматривается

Performance и Attractive-возможность повышается в приоритете только после
повторяющегося сигнала минимум в двух релевантных pilots. Наблюдаемые метрики:
ручные действия, model calls, размер контекста, elapsed time, число review
циклов и escaped high-severity defects. Незакрытый Must-be gap всегда важнее
нового connector, dashboard или оптимизации модели.
