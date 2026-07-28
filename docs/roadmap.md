# Roadmap Easy Peasy DLS

Срез: 28 июля 2026 года

Текущая линия: `v0.7.0`

Этот roadmap использует KANO как практический способ расставить приоритеты для
solo AI-delivery. Must-be защищает доверие к процессу, Performance уменьшает
ручную работу и повтор контекста, Attractive добавляет удобство только после
стабилизации основного цикла.

Полный реестр всех 145 реализованных, запланированных, отложенных и намеренно
исключённых возможностей находится в [карте возможностей](capability-catalog.md).
Этот файл остаётся короткой выборкой ближайших волн, а не вторым каталогом.

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
- bounded review execution: risk budgets, compact context и ранняя остановка.

Ранее открытые gaps launcher/PATH, semantic write prevention, remediation
launcher и единого review-ready handoff закрыты текущим CLI и skills.

## Реализовано в v0.4.4

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

## Реализовано в v0.5.0 — экономичный review

| ID | KANO | Возможность | Результат |
|---|---|---|---|
| M53 | Must-be / P0 | Bounded review execution | Token/lane/event/time/transcript budgets останавливают runaway review без ложного verdict |
| P02 | Performance / P1 | Routine fast-path | Validation и ровно один isolated Terra/high review без Sol и отдельной review-задачи |
| P08 | Performance / P1 | Runtime telemetry | Child и controller usage с privacy filtering и явной полнотой измерения |
| P09 | Performance / P1 | Legacy vs DLS baseline | Локальная абсолютная baseline-методика; публичные проценты не заявляются по одному проекту |
| P27 | Performance / P1 | Cache retention | Canonical artifacts сохраняются, raw cache очищается по безопасному 14-day/two-review правилу |
| P33 | Performance / P1 | Краткий delivery status | Один compact typed next action без чтения state и transcripts |

## Реализовано в v0.6.0 — надёжная активация и handoff

| ID | KANO | Возможность | Результат |
|---|---|---|---|
| M54 | Must-be / P0 | Exact-HEAD status coherence | Candidate, review и delivery status не выдают исторический handoff за текущий |
| M55 | Must-be / P0 | Safe handoff self-healing | Review может достроить только доказуемый remediation handoff без изменения product source |
| P35 | Performance / P0 | Guarded automatic activation | Явный skill-chip необязателен при однозначном DLS-контексте, generic задачи не перехватываются |
| M56 | Must-be / P0 | Plugin-local runtime provenance | CLI и version берутся только из реально загруженного plugin bundle |
| M57 | Must-be / P0 | Logical command budgeting | Paired start/completion telemetry считается одним вызовом и не создаёт ложный budget failure |
| M58 | Must-be / P0 | Native presentation recovery | Официальный plaintext review строго проецируется без потери raw provenance и повторного model call |
| M59 | Must-be / P0 | Budget-terminal safety | Budget stop не маскируется parser crash, а новый budget образует новый lane contract |

Exit criteria v0.6.0: exact-HEAD statuses согласованы, self-healing не запускает
model calls при validation/integrity failure, generic задачи не активируют DLS,
а реальный EPIC-01 review завершается каноническим ReviewIR до публикации.

## v0.6.1 — context hygiene и согласованная telemetry

| ID | KANO | Возможность | Результат |
|---|---|---|---|
| M60 | Must-be / P0 | Согласованные exact-HEAD status | Candidate, review и delivery используют один проверенный pack, HEAD и prior-review link |
| P36 | Performance / P0 | Обнаружение reuse длинных задач | Fresh, continued и reused cycles различаются без блокировки delivery |
| P37 | Performance / P0 | Controller/context telemetry | Event counts, context bytes/words, usage sources и controller share без чтения содержимого |
| P38 | Performance / P0 | Короткий handoff | Между свежими задачами передаётся одна команда, а не manifest, findings, SHA и пути |
| P39 | Performance / P1 | Targeted-review pilot | Текущие context/usage значения фиксируются как baseline без преждевременной смены prompt, модели или budgets |

Exit criteria v0.6.1: reuse даёт одно advisory-предупреждение, raw task IDs не
покидают ignored cache, native zero остаётся unavailable/reported-zero, а status
и metrics сохраняют прежние payload limits. Review-алгоритм не меняется.

## v0.7.0 — рабочие platform profiles и backend pilot

| ID | KANO | Возможность | Результат |
|---|---|---|---|
| P21 | Performance / P1 | Второй platform adapter | `server-backend` стал runtime profile с bounded inheritance, digest-bound provenance и Vapor/Linux preflight |
| M40 | Must-be / P0 | Architecture decision lifecycle | Critical E03 adoption и Draft/approval boundary проверены; полный backend definition/review lifecycle ещё нужен |
| P30 | Performance / P1 | Conditional domain routing | Backend capabilities и advisory skills попадают в context без Apple UI/App Store routing; польза на model review ещё не измерена |
| P39 | Performance / P1 | Targeted-review baseline | Profile provenance добавлена в metrics; backend model usage намеренно отсутствует до approval |

Exit criteria v0.7.0: repository profile безопасно перекрывает bundled,
profile drift инвалидирует candidate/pack, `canonical-ci` создаёт exact-HEAD
evidence, а E03 preflight честно останавливается на `approve-definition` без
ReviewPack и model calls. Models и default budgets не меняются.

## Следующая волна v0.7.1: UI/UX platform pilot

- `M38–M39` — проверить Tier 1 precedent и Tier 2/3 versioned design artifact на
  реальном UI change;
- `M40` — завершить early architecture decision lifecycle на approved critical
  change;
- `P30`, `P39` — сравнить routing/context/usage на разрешённых Swift и backend
  reviews;
- `P10`, `P12` — менять budgets или model routing только после сопоставимых
  абсолютных baseline, без публичного процента экономии по одному pilot.

## Позже, только по данным

- P22 — следующий adapter только из реального web или Android pilot;
- P25 — read-only derived status и traceability views;
- A01 — Figma, Sketch или другой design connector после UI-пилота и решения
  privacy/versioning;
- A06 — cross-project dashboard, только если CLI status перестанет справляться;
- release/production profiles — после стабильного acceptance loop в нескольких
  типах проектов.

Полный P2/P3 backlog, включая migration, repository cache, changelog, model/cost
recommendations и сохранённые anti-features, ведётся только в
[карте возможностей](capability-catalog.md).

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
