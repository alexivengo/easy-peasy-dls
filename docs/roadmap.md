# Roadmap Easy Peasy DLS

Срез: 29 июля 2026 года

Текущая линия: `v0.9.1`

Этот roadmap использует KANO как практический способ расставить приоритеты для
solo AI-delivery. Must-be защищает доверие к процессу, Performance уменьшает
ручную работу и повтор контекста, Attractive добавляет удобство только после
стабилизации основного цикла.

Полный реестр всех 154 реализованных, запланированных, отложенных и намеренно
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
- read-only disposable review workspaces, single-flight и bounded recovery;
- bounded review execution: risk budgets, compact context и ранняя остановка.
- deterministic Delivery Receipt: read-only narrative и traceability одного
  change без model call и нового source of truth.
- stage-aware parallel delivery: selective worktrees, explicit dependencies,
  overlap preflight и one-writer на change вместо глобальной очереди.

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

Локальный backend pilot на E03 HEAD `f3c581f` дал следующие абсолютные данные:
`canonical-ci` завершился за `69.051 s`, сохранил `25 586` bytes bounded output
без overflow; implementation context составил `143 365` bytes и `17 144` words
(`18 856–30 856` грубо оценённых tokens). Создан один exact-HEAD evidence record,
но `0` ReviewPack, `0` candidate runs и `0` model calls: preflight корректно
вернул `approve-definition`. Поэтому это profile/runtime доказательство, а не
backend review-usage baseline для настройки budgets или моделей.

## v0.7.1 — native review scope integrity

| ID | KANO | Возможность | Результат |
|---|---|---|---|
| M61 | Must-be / P0 | Standalone native workspace provenance | Native review получает clean standalone clone exact HEAD, не видит owner-local `.dls`, а попытка без workspace marker не переиспользуется |
| P35 | Performance / P0 | Реальная guarded activation | Workflow skill разрешён для implicit invocation, но description по-прежнему требует явный DLS-сигнал |

Exit criteria v0.7.1: dirty generated DLS sidecar владельца отсутствует в
native workspace, recorded argv не содержит owner/temp paths, Git common-dir и
remote не ведут обратно в owner checkout, а legacy owner-scoped attempt не
может стать canonical native provenance.

## v0.8.0 — Delivery Receipt

| ID | KANO | Возможность | Результат |
|---|---|---|---|
| A03 | Attractive / P2 | Generated narrative changelog | Краткий русский narrative одного change вычисляется из canonical данных без LLM и не сохраняется |
| P25 | Performance / P1 | Derived traceability view | Stable JSON связывает definition, tickets, current evidence, latest ReviewIR, acceptance и внешние границы |

Exit criteria v0.8.0: повторный render byte-identical, Receipt не меняет state
или файловый inventory, stale review/acceptance не доказывают текущий HEAD,
release/production не выводятся из отсутствия findings, а successful import и
accept автоматически возвращают один Receipt.

## v0.8.1 — безопасное восстановление native output

| ID | KANO | Возможность | Результат |
|---|---|---|---|
| M62 | Must-be / P0 | Indeterminate native recovery | Completed prose сверяется с immutable transcript, не считается clean, не повторяет native model call и обязательно проходит semantic reconciliation |

Exit criteria v0.8.1: legacy `invalid-output` восстанавливается без второго
native call, output/transcript digests остаются неизменными, неоднозначный текст
не создаёт verdict самостоятельно, а unsafe/integrity случаи получают terminal
typed action без resume-loop.

## v0.9.0 — dependency-aware parallel delivery

| ID | KANO | Возможность | Результат |
|---|---|---|---|
| M63 | Must-be / P0 | Stage-aware dependencies | Definition, implementation, review и acceptance блокируются только с нужной стадии; `accepted-in-base` проверяет human acceptance и Git ancestry |
| M64 | Must-be / P0 | Change-scoped one-writer | Single-flight и CAS остаются на change/worktree, а независимые changes не получают глобальную блокировку |
| P40 | Performance / P1 | Selective worktree preparation | Standard/critical parallel change создаётся от explicit clean SHA, без sibling scan, rebase или autonomous task creation |
| P41 | Performance / P1 | Overlap/conflict preflight | Exact file overlap сериализует поздний candidate handoff; proximity остаётся advisory |
| P42 | Performance / P1 | Delivery map | Bounded read-only карта показывает active changes, dependencies, parallel groups, integration order и typed actions |

Exit criteria v0.9.0: dirty/not-clear predecessor не блокирует независимую
definition, implementation dependency возвращает `wait-dependency`, accepted
upstream требует `rebase-after-dependency` до появления его HEAD в base, а два
непересекающихся changes одновременно проходят candidate/review pipelines без
глобального lock.

## v0.9.1 — bounded final review и zero-call recovery

| ID | KANO | Возможность | Результат |
|---|---|---|---|
| M65 | Must-be / P0 | Completed-output budget recovery | Валидный exact-HEAD output внутри bounded recovery ceiling не выбрасывается и импортируется без повторного model call |
| P43 | Performance / P0 | Input-only final-full | Whole-change финал получает exact patch/coverage bundle, 16 command events и typed scope split вместо unrestricted checkout |

Exit criteria v0.9.1: реальный EPIC-01 review восстанавливается с тем же
operation ID, без нового model attempt и без изменения product source; будущий
final-full не получает checkout и не обрезает coverage молча.

## Следующая волна v0.10.0: UI/UX Attractive pilot

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
- A01 — Figma, Sketch или другой design connector после UI-пилота и решения
  privacy/versioning;
- A06 — cross-project dashboard, только если CLI status перестанет справляться;
- release/production profiles — после стабильного acceptance loop в нескольких
  типах проектов.

Полный P2/P3 backlog, включая migration, repository cache и model/cost
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
- SQLite, event sourcing и concurrent writers одного change/state.

## Как roadmap пересматривается

Performance и Attractive-возможность повышается в приоритете только после
повторяющегося сигнала минимум в двух релевантных pilots. Наблюдаемые метрики:
ручные действия, model calls, размер контекста, elapsed time, число review
циклов и escaped high-severity defects. Незакрытый Must-be gap всегда важнее
нового connector, dashboard или оптимизации модели.
