# Полная карта возможностей Easy Peasy DLS

Срез: 30 июля 2026 года
Текущая линия: `v0.9.5`

Этот каталог — канонический реестр возможностей и anti-features продукта. Он
сохраняет все исторические KANO-ID и дополняет их возможностями, появившимися
после публичного запуска. Короткий [roadmap](roadmap.md) отвечает на вопрос
«что делать следующим», а этот документ — «что вообще существует, планируется
или намеренно не будет реализовано».

Всего отслеживается 157 пунктов:

- 67 Must-be: `M01–M67`;
- 44 Performance: `P01–P44`;
- 10 Attractive: `A01–A10`;
- 12 Indifferent/premature: `I01–I12`;
- 24 Reverse anti-features: `R01–R24`.

Исходный KANO-снимок содержал 129 пунктов: все они сохранены под прежними ID.
К ним добавлены 28 публичных возможностей `M51–M67` и `P34–P44`. Удалять ID
нельзя: изменившееся решение помечается как заменённое, а не исчезает из карты.

## Статусы

| Маркер | Значение |
|---|---|
| ✅ | Реализовано и должно защищаться regression-тестами или повторяемым gate |
| 🟨 | Контракт существует, но не хватает пилота, полноты или доказательства на нескольких стеках |
| 🧭 | Запланировано в одной из следующих волн |
| 🧊 | Отложено до появления повторяющегося сигнала или данных |
| ↪ | Исходная формулировка заменена новым контрактом; ID сохраняется для трассировки |
| 🚫 | Anti-feature: намеренно не реализовывать |

Приоритет `P0–P3` относится к ценности и срочности, а не к порядку ID. Статус
«реализовано» не означает `accepted`, `released` или `production-verified` для
конкретного пользовательского проекта.

## Must-be — основа доверия

| ID | Capability | Status | Priority | Текущее состояние / развитие |
|---|---|---:|---:|---|
| M01 | Один владелец delivery workflow | ✅ | P0 | DLS владеет процессом; domain expertise не создаёт параллельные approvals и gates |
| M02 | Явный запуск без конфликтующего implicit routing | ↪ | P0 | Заменено guarded activation `P35`: DLS включается автоматически только при подтверждённом DLS-контексте |
| M03 | Надёжный launcher установленного плагина | ✅ | P0 | Закрыто plugin-local runtime provenance `M56`; fallback на PATH и архивы запрещён |
| M04 | Risk-adaptive пути вместо epic для любой работы | ✅ | P0 | Micro, routine, standard, roadmap, critical, spike и hotfix выбираются по риску |
| M05 | Минимальный пакет документов | ✅ | P0 | Micro без документа; routine с `CHANGE.md`; SPEC, TICKETS, EPIC и ADR условны |
| M06 | Версионированные шаблоны без model boilerplate | ✅ | P0 | CHANGE, SPEC, TICKETS, EPIC, ADR и config создаются из templates |
| M07 | Единственный canonical owner каждого факта | ✅ | P0 | Authored requirements, state, approvals и Git/evidence разделены по ответственности |
| M08 | Adoption существующего пакета | ✅ | P0 | `dls adopt` регистрирует совместимый пакет без semantic rewrite документов |
| M09 | Детерминированные structural/traceability checks | ✅ | P0 | Gates, links и lifecycle проверяются без model calls |
| M10 | Scoped human approval | ✅ | P0 | Standard/critical approval требует конкретного decision, committed authored package, digest и exact Git SHA |
| M11 | Digest-bound approval staleness | ✅ | P0 | Authored change инвалидирует approval; changelog/evidence/findings/generated regions относятся к execution и не меняют definition digest |
| M12 | Human approval отделён от model verdict | ✅ | P0 | `review-clear`, `accepted`, release и production остаются разными решениями |
| M13 | Точная lifecycle vocabulary | ✅ | P0 | Implemented, validated, review-clear, accepted, release-ready, released и production-verified не смешиваются |
| M14 | Atomic state store | ✅ | P0 | Lock, CAS, operation ID, atomic replace и backup покрыты fault-тестами |
| M15 | Защита путей и файлов | ✅ | P0 | Traversal, symlink escape, malformed artifacts и небезопасные paths отклоняются |
| M16 | Один product-source writer | ✅ | P0 | Implementation меняет source; independent review остаётся read-only |
| M17 | Digest-bound phase context | ✅ | P0 | Definition, implementation, review и remediation используют bounded manifests вместо replay transcript |
| M18 | Запрет silent context truncation | ✅ | P0 | Context caps дают typed failure или large-context fallback, а не скрытую потерю inputs |
| M19 | Trusted validation runner | ✅ | P0 | Исполняются только repository-owned named argv без shell interpolation |
| M20 | Bounded execution и secret redaction | ✅ | P0 | Ограничены env, timeout, output, transcript и command events; чувствительные данные редактируются |
| M21 | Revision-bound evidence | ✅ | P0 | Evidence связано с HEAD, source digest, command contract, duration и safe output digest |
| M22 | Exact-revision acceptance review | ✅ | P0 | ReviewPack фиксирует base/head, merge-base, definition, tickets, evidence и source snapshot |
| M23 | Wrong-checkout fail-fast | ✅ | P0 | DLS не угадывает branch и не сканирует соседние checkout |
| M24 | Review через зарегистрированные epic worktrees | ✅ | P0 | Worktree registry безопасно маршрутизирует change ID к owner checkout |
| M25 | Native diff review | ✅ | P0 | Standard/critical используют Terra/high; routine — один Terra/high pass |
| M26 | Независимый semantic review | ✅ | P0 | Sol получает изолированный контекст и не доверяет native findings автоматически |
| M27 | Reconciliation независимых источников | ✅ | P0 | Запускается только при findings/расхождениях и использует input-only workspace |
| M28 | Final native result отделён от transcript | ✅ | P0 | Bounded transcript не превращает корректный final result в ложный output-cap |
| M29 | Source snapshot после review lanes | ✅ | P0 | Source/HEAD drift блокирует import |
| M30 | Техническая read-only boundary reviewer | ✅ | P0 | Обязательные semantic/specialist lanes запускаются DLS в disposable detached workspaces |
| M31 | Canonical ReviewIR | ✅ | P0 | Хранит verdict, findings, ticket verdicts, provenance и exact-revision digests |
| M32 | Полнота ticket verdicts | ✅ | P0 | Каждый ticket ReviewPack получает независимый verdict |
| M33 | Stage-specific blockers | ✅ | P0 | Review, acceptance, release и production блокируются раздельно |
| M34 | Atomic ReviewIR import | ✅ | P0 | Result, remediation manifest и state фиксируются транзакционно с drift checks |
| M35 | Review не завершается без result path | ✅ | P0 | Успех требует canonical `review_result_path`; actionable result — remediation path |
| M36 | Findings lifecycle | ✅ | P0 | Implementer ставит addressed/note; verified принадлежит independent review; waiver — человеку |
| M37 | Latest-only remediation context | ✅ | P0 | Рабочий manifest строится только из последнего canonical ReviewIR |
| M38 | UI/UX source prerequisite | 🟨 | P0 | Policy и approvals реализованы; реальный UI pilot перенесён в v0.10.0 |
| M39 | Tiered UI policy | 🟨 | P0 | Tier 1 допускает precedent, Tier 2/3 требуют versioned artifact; реальный pilot перенесён в v0.10.0 |
| M40 | Architecture decision до завершения SPEC | 🟨 | P0 | Critical E03 adoption и Draft/approval boundary дают backend preflight evidence; нужен полный approved definition/review lifecycle |
| M41 | ADR только для долговечного решения | ✅ | P0 | ADR остаётся conditional artifact, а не ритуалом для каждого change |
| M42 | Release/production evidence не подменяет review | ✅ | P0 | External gaps не блокируют code review без прямой ссылки на ticket DoD |
| M43 | Doctor и runtime/source drift diagnostics | ✅ | P0 | Проверяются plugin provenance, config, schemas, Git и конфликтующие process plugins |
| M44 | Безопасная install/uninstall/rollback | ✅ | P0 | Cachebuster flow и disposable install/uninstall входят в release gate |
| M45 | Нет скрытых global config mutations | ✅ | P0 | Модели, permissions, agents и plugins меняются только отдельным решением пользователя |
| M46 | Dirty review остаётся advisory | ✅ | P0 | Незакоммиченный diff не создаёт acceptance-grade review-clear |
| M47 | Reviewer-owned closure | ✅ | P0 | Addressed не равно verified; closure создаёт только независимый ReviewIR import |
| M48 | Definition boundary при remediation | ✅ | P0 | Behavior/architecture/acceptance change требует нового approval |
| M49 | Согласованная свежесть candidate/evidence | ✅ | P0 | DLS metadata не инвалидирует product candidate, а новый HEAD инвалидирует evidence |
| M50 | Stage-aware ticket closure | ✅ | P0 | Canonical statuses не расширяются искусственными literals; gates вычисляются |
| M51 | Revision-safe candidate continuation | ✅ | P0 | Descendant HEAD наследует только проверенные dispositions, но не evidence |
| M52 | Восстановимая validation-диагностика | ✅ | P0 | Bounded redacted excerpt доступен после потери shell payload |
| M53 | Bounded review execution | ✅ | P0 | Lane/aggregate tokens, commands, timeout и transcript caps исключают runaway review |
| M54 | Exact-HEAD status coherence | ✅ | P0 | Исторический candidate не выдаётся за текущий handoff |
| M55 | Safe handoff self-healing | ✅ | P0 | Review достраивает только доказуемый remediation handoff и не меняет source |
| M56 | Plugin-local runtime provenance | ✅ | P0 | CLI определяется относительно реально загруженного skill bundle |
| M57 | Logical command budgeting | ✅ | P0 | Paired start/completion events считаются одним command invocation |
| M58 | Native presentation recovery | ✅ | P0 | Строгая plaintext projection сохраняет raw provenance и не повторяет model call |
| M59 | Budget-terminal safety | ✅ | P0 | Budget stop не маскируется parser failure; новый budget образует новый contract |
| M60 | Candidate/review/delivery status из одного pack | ✅ | P0 | Exact HEAD, prior review и typed next action используют общий resolver |
| M61 | Standalone native workspace provenance | ✅ | P0 | Native review запускается в clean exact-HEAD clone; owner-local `.dls` и попытки без workspace marker не входят в canonical provenance |
| M62 | Indeterminate native recovery | ✅ | P0 | Transcript-verified prose не объявляется clean, не повторяет native call и обязательно проходит semantic reconciliation |
| M63 | Stage-aware change dependencies | ✅ | P0 | Dependency блокирует только названную и последующие стадии; `accepted-in-base` проверяет human acceptance и Git ancestry |
| M64 | Change-scoped one-writer | ✅ | P0 | Atomic handoff переносит approved state до registry switch; один owner/single-flight остаётся внутри change без global lock |
| M65 | Completed-output budget recovery | ✅ | P0 | Валидный exact-HEAD output внутри bounded ceiling импортируется без повторного model call; hard limits остаются terminal |
| M66 | Registry-first ReviewPack resolution | ✅ | P0 | Implicit review читает pack и invocation-scoped lease registered owner; stale portable state в caller checkout остаётся только исторической копией |
| M67 | Final-full completion headroom | ✅ | P0 | 16 inspection commands остаются target, а bounded ceiling 24 оставляет место для обязательного structured decision |

## Performance — экономия времени, контекста и ручной работы

| ID | Capability | Status | Priority | Текущее состояние / развитие |
|---|---|---:|---:|---|
| P01 | Короткие команды вместо мегапромтов | ✅ | P0 | Definition, implementation, remediation и review маршрутизируются skill-контрактом; продолжать UX pilots |
| P02 | Routine fast-path в одной задаче | ✅ | P1 | Один Terra/high review без Sol и отдельной review-задачи; нужен cross-platform pilot |
| P03 | Автогенерация минимального package | ✅ | P0 | `dls new` и templates заменяют model-authored boilerplate |
| P04 | Tickets только для coherent slices | 🟨 | P1 | Policy реализована; подтвердить на нескольких standard/roadmap changes |
| P05 | Deterministic scripts вместо model calls | ✅ | P0 | Checks, digests, state, validation, evidence и imports механизированы |
| P06 | Phase-specific context manifest | ✅ | P0 | В model context попадает только phase-relevant projection |
| P07 | Видимость context size | ✅ | P1 | Metrics содержат bytes, words, input count и reason counts |
| P08 | Token/cost/latency telemetry | ✅ | P1 | Child/controller usage и completeness доступны без prompt content; monetary cost зависит от источника |
| P09 | Legacy/DLS baseline | ✅ | P1 | Абсолютная методика и sanitized fixtures есть; проценты ждут сопоставимых pilots |
| P10 | Context warning budgets | ✅ | P1 | Context и review budgets bounded; калибровка продолжается по данным |
| P11 | Hard token/cost limits | ↪ | P2 | Token caps реализованы в `M53`; денежный cap не вводится без надёжного cost source |
| P12 | Risk-based model routing вне review | 🧭 | P1 | A/B pilot только после нескольких platform baselines; global default не менять |
| P13 | Fixed review routing Terra → Sol | ✅ | P0 | Модель и effort сохраняются в provenance |
| P14 | Bounded subagent policy | ↪ | P1 | Обязательные lanes принадлежат runner, не subagents; custom subagent допускается лишь для отдельной boundary |
| P15 | Whole-change review вместо reviewer на ticket | ✅ | P0 | Review выполняется по exact change; ticket verdicts остаются внутри одного ReviewIR |
| P16 | Trigger-based remediation re-review | ✅ | P1 | Delta-first review останавливается рано при actionable finding; final-full только после clean delta |
| P17 | End-to-end remediation launcher | ↪ | P0 | Пользовательский outcome перенесён в `candidate-ready` + self-healing `review-run`; primitive сохранён для диагностики |
| P18 | Compact status и next action | ✅ | P1 | `delivery-status` возвращает один согласованный action; продолжать usability pilots |
| P19 | Generic composable platform core | ✅ | P0 | Core не содержит iOS-only lifecycle assumptions |
| P20 | Первый Apple adapter | ✅ | P1 | Bundled Apple profile существует и проверяется глубже generic |
| P21 | Второй platform adapter | ✅ | P1 | `server-backend` — runtime profile с inheritance, provenance, candidate invalidation и Vapor/Linux preflight |
| P22 | Полная platform profile suite | 🧊 | P2 | Не проектировать абстрактно до нескольких реальных adapters |
| P23 | Semantic repository-discovery cache | 🧊 | P2 | Рассматривать только после измерения повторного discovery overhead |
| P24 | Bulk migration legacy packages | 🧊 | P2 | `adopt` достаточен, пока нет повторяющегося migration volume |
| P25 | Derived status/traceability views | ✅ | P1 | Delivery Receipt даёт stable read-only JSON/Markdown одного change из canonical данных без нового source of truth |
| P26 | Release и production extensions | 🧊 | P2 | Отдельные profiles после стабильного acceptance loop на нескольких стеках |
| P27 | Evidence/cache retention | ✅ | P1 | Canonical artifacts сохраняются; raw cache использует 14-day/two-review policy |
| P28 | Conflict inventory и cleanup verification | 🟨 | P1 | Doctor обнаруживает конфликты; destructive cleanup остаётся user-authorized |
| P29 | Risk-adaptive debugging | ✅ | P0 | `dls-debug` ведёт reproduction → RCA → minimal patch → regression proof |
| P30 | Conditional domain-skill routing | 🟨 | P1 | Backend capabilities и advisory skills передаются без Apple-only routing; нужна telemetry пользы на model reviews нескольких стеков |
| P31 | Выборочный перенос полезных механизмов | ✅ | P0 | Сохранены focused questions, trade-offs, YAGNI, root-cause-first и verification-before-completion |
| P32 | Одна команда review-ready | ↪ | P0 | Пользовательский handoff заменён `candidate-ready`; `review-ready` остаётся low-level primitive |
| P33 | Краткий delivery status | ✅ | P1 | Compact payload показывает current candidate/review, usage completeness и cache size |
| P34 | Нулевой повтор finding bookkeeping | ✅ | P0 | Descendant candidate наследует declaration без повторного списка findings |
| P35 | Guarded automatic activation | ✅ | P0 | DLS активируется по repo state/config или routable artifacts и не перехватывает generic task |
| P36 | Обнаружение reuse длинных задач | ✅ | P0 | Fresh, continued, reused и unavailable определяются без блокировки delivery |
| P37 | Controller/context telemetry | ✅ | P0 | Event counts, usage source и context metadata не читают содержимое сообщений |
| P38 | Короткий handoff между задачами | ✅ | P0 | Передаётся одна пользовательская команда без manifest, SHA и paths |
| P39 | Измерение targeted-review | 🟨 | P1 | Metrics содержат profile provenance; backend preflight намеренно не создаёт model usage, поэтому нужен approved review pilot |
| P40 | Selective worktree preparation | ✅ | P1 | `worktree prepare` создаёт owner от approved exact SHA и переносит governance state без ручного init/adopt, sibling scan или autonomous task |
| P41 | Overlap/conflict preflight | ✅ | P1 | Exact file overlap блокирует поздний candidate handoff, proximity остаётся advisory |
| P42 | Active delivery map | ✅ | P1 | Bounded read-only карта показывает dependencies, parallel groups, integration order и один typed action на change |
| P43 | Input-only final-full | ✅ | P0 | Exact patch/coverage bundle заменяет unrestricted checkout; oversized change получает explicit scope split |
| P44 | Terminal-lane command recovery | ✅ | P0 | Legacy 17/16 повторяет только final-full один раз; native, targeted и reconciliation не оплачиваются повторно |

## Attractive — полезно после стабилизации core

| ID | Capability | Status | Priority | Условие продвижения |
|---|---|---:|---:|---|
| A01 | Figma/Sketch/другой design connector | 🧊 | P2 | Реальный UI pilot, privacy contract и immutable version reference |
| A02 | Автоматическое design provenance/version | 🧊 | P2 | Надёжный immutable source и явный выбор пользователя |
| A03 | Generated narrative changelog | ✅ | P2 | Delivery Receipt детерминированно строит краткий русский narrative без LLM и не сохраняет его |
| A04 | Model optimization по telemetry | 🧊 | P2 | Сопоставимые A/B данные на нескольких типах задач |
| A05 | Cost-aware recommendation model/effort | 🧊 | P2 | Надёжный cost source; рекомендация вместо скрытого переключения |
| A06 | Cross-project dashboard | 🧊 | P3 | Только если compact CLI status перестанет справляться |
| A07 | Generated ADR index | 🧊 | P3 | Несколько проектов с накопленным набором долговечных ADR |
| A08 | Guided legacy migration report | 🧊 | P2 | Повторяющийся спрос, dry-run, diff и rollback |
| A09 | Release-profile library | 🧊 | P2 | Реальные App Store, Play, web deploy и backend production pilots |
| A10 | Disposable review workspace | ✅ | P1 | Semantic lanes используют detached workspace; native lane — standalone no-hardlinks clone с cleanup в `finally` |

## Indifferent или premature — не включать без новых данных

| ID | Capability | Status | Priority | Решение |
|---|---|---:|---:|---|
| I01 | DLS-specific persona agents | 🧊 | P3 | Built-in roles и runner boundaries достаточны |
| I02 | Большое количество runtime skills | 🧊 | P3 | Добавлять только по повторяющемуся use case |
| I03 | SQLite state store | 🧊 | P3 | JSON + CAS соответствует solo workflow |
| I04 | Full event sourcing | 🧊 | P3 | Maintenance cost выше текущей ценности |
| I05 | Concurrent writers одного change/state | 🧊 | P3 | One-writer запрещает конкурирующие mutations одного change, но не независимые changes в отдельных worktrees |
| I06 | Generic lifecycle transition command | 🧊 | P3 | Typed commands безопаснее |
| I07 | Generic render subsystem | 🧊 | P3 | Templates и derived projections достаточны |
| I08 | Generic migration engine | 🧊 | P3 | Typed adopt/recovery безопаснее |
| I09 | Hooks | 🧊 | P3 | Скрытые side effects и новая security boundary не оправданы |
| I10 | Automatic global cleanup | 🧊 | P3 | Destructive global action требует отдельного решения пользователя |
| I11 | Отдельный architecture registry | 🧊 | P3 | SPEC + conditional ADR пока достаточны |
| I12 | Долгоживущая roadmap-agent task | 🧊 | P3 | Документ и state должны оставаться source of truth |

## Reverse — anti-features

| ID | Не реализовывать | Status | Почему |
|---|---|---:|---|
| R01 | Полностью автономный delivery без решений человека | 🚫 | Scope, UX, architecture, risk и acceptance принадлежат пользователю |
| R02 | Mandatory brainstorming для любой задачи | 🚫 | Добавляет ритуал, когда intent уже ясен |
| R03 | Mandatory Plan Mode на каждом шаге | 🚫 | Дублирует transient planning и context |
| R04 | Mandatory TDD независимо от риска | 🚫 | Test strategy следует failure risk |
| R05 | Mandatory worktree для каждой задачи | 🚫 | Worktree — инструмент isolation, а не универсальный gate |
| R06 | Полный epic package для routine | 🚫 | Возвращает бюрократию |
| R07 | Обязательный отдельный brief | 🚫 | Brief должен становиться входом в canonical definition |
| R08 | Отдельный change-request документ | 🚫 | Достаточны contract delta и reapproval |
| R09 | Отдельный architecture gate для каждого change | 🚫 | Architecture review условен и встроен в definition review |
| R10 | Design artifact для любого UI diff | 🚫 | Tier 1 precedent не требует искусственного макета |
| R11 | Reviewer на каждый ticket | 🚫 | Повышает handoff и correlated noise |
| R12 | Subagent-driven routine implementation | 🚫 | Нарушает one-writer simplicity |
| R13 | Spawn ради параллелизма | 🚫 | Допустим только независимый результат с отдельной boundary |
| R14 | Неподтверждённый implicit process routing | 🚫 | Guarded activation `P35` разрешает только доказуемый DLS-контекст |
| R15 | Model-authored shell как executable source | 🚫 | Неприемлемая security boundary |
| R16 | Arbitrary commands через DLS | 🚫 | Разрешены только named repository-owned argv |
| R17 | Автоматический human approval | 🚫 | Model verdict и generic «ок» не являются authority |
| R18 | Автоматическое признание authored change несущественным | 🚫 | Разрушает approval integrity |
| R19 | Сканирование соседних worktrees/branches | 🚫 | Может выбрать неправильный checkout |
| R20 | Автоматическая смена global model/effort | 🚫 | Эксперименты должны быть scoped и подтверждёнными |
| R21 | Автоматическое удаление plugins/agents/skills/архивов | 🚫 | Destructive action требует отдельного разрешения |
| R22 | Release evidence как default code-review blocker | 🚫 | Review, acceptance, release и production имеют разные границы |
| R23 | Второй writable status в Markdown | 🚫 | Создаёт рассинхронизацию с state |
| R24 | Полный перенос внешней методологии | 🚫 | Переносятся только отдельные механизмы с доказанной ценностью |

## Карта следующих волн

Это порядок проверки гипотез, а не обещание конкретных дат.

### v0.7.0 — platform profile runtime и backend preflight

- `P21` закрыт bundled `server-backend` adapter и Vapor/Linux E03 preflight;
- `M40`, `P30`, `P39` получили дополнительные доказательства, но остаются partial;
- `P12` остаётся planned: models и default budgets не меняются без
  сопоставимого backend model-review baseline.

### v0.7.1 — native review scope integrity

- `M61`: standalone clone, explicit Codex `--cd` и state-owned workspace provenance;
- `P35`: guarded workflow действительно доступен для implicit invocation по DLS-сигналу;
- owner-scoped legacy native attempt получает bounded replacement вместо reuse.

### v0.8.0 — Delivery Receipt

- `A03`: deterministic generated narrative одного change без LLM и persistence;
- `P25`: stable derived traceability по definition, tickets, evidence, ReviewIR,
  acceptance и отдельным release/production boundaries;
- receipt автоматически возвращается после canonical review import и `accept`.

### v0.8.1 — безопасное восстановление native output

- `M62`: completed unstructured native output получает digest/transcript
  validation и статус `indeterminate`, а не ложный clean verdict;
- semantic reconciliation обязательна, native model call не повторяется;
- unsafe output и integrity drift получают terminal typed actions вместо
  бесконечного `resume-review`.

### v0.9.0 — dependency-aware parallel delivery

- `M63`, `M64`: stage-aware dependencies и one-writer на change/worktree;
- `P40`: selective worktree от explicit clean SHA;
- `P41`: exact-file overlap сериализует candidate/integration, не раннюю работу;
- `P42`: bounded delivery map без model calls и локальных paths.

### v0.9.5 — bounded final-full command recovery

- `M67`: model-facing target 16 отделён от bounded runtime ceiling 24;
- `P44`: legacy command stop ниже нового ceiling повторяет только terminal
  final-full и сохраняет provenance завершённых upstream lanes;
- текущие command/time/transcript/integrity ceilings остаются terminal.

### v0.10.0 — UI/UX и полный architecture lifecycle

- `M38–M39`: UI tiers и immutable design source на реальном change;
- `M40`: approved critical definition/review lifecycle;
- `P30`, `P39`: сравнимые Swift/backend/UI routing и usage pilots.

### Следующая P1-волна — platform и conflict UX

- `P28`: повторяемый conflict inventory без автоматического destructive cleanup;
- завершить cross-platform доказательство `P02`, `P08`, `P09` и `P39`.

### P2 — ecosystem только по подтверждённому спросу

- `P26`, `A09`: release/production profiles;
- `A01`, `A02`: versioned design connectors/provenance;
- `P23`: semantic discovery cache;
- `P24`, `A08`: guided migration;
- `A04–A05`: model/cost recommendations на основе данных.

### P3 — не планировать заранее

`A06`, `A07` и `I01–I12` остаются вне активного roadmap, пока повторяющаяся
проблема не докажет, что текущего CLI, state и conditional artifacts недостаточно.

## Правило пересмотра

Новый Attractive/Indifferent пункт не вытесняет незакрытый Must-be. Performance
повышается в приоритете только после повторяющегося сигнала минимум в двух
релевантных pilots. Для изменения статуса фиксируются абсолютные данные: ручные
действия, model calls, context/processed tokens, elapsed time, review cycles и
escaped high-severity defects.
