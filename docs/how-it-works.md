# Как работает Easy Peasy DLS

DLS добавляет к AI-разработке не ещё одну «методологию на все случаи», а несколько устойчивых границ. Модель занимается тем, где нужно суждение. Скрипты — тем, где нужна повторяемость. Человек сохраняет право определить результат и принять его.

## Основной цикл

```mermaid
flowchart LR
    A["Идея или проблема"] --> B["Выбор минимального пути"]
    B --> C["Definition"]
    C --> D{"Подтверждено человеком?"}
    D -- "нет" --> C
    D -- "да" --> E["Implementation"]
    E --> F["Validation evidence"]
    F --> G["Independent review"]
    G -- "есть findings" --> H["Remediation"]
    H --> F
    G -- "review-clear" --> R["Delivery Receipt"]
    R --> I{"Accept?"}
    I -- "нет" --> C
    I -- "да" --> J["Accepted Receipt"]
    J --> K["Отдельные release gates"]
```

У небольших изменений часть узлов схлопывается в одну задачу. У critical-изменений границы становятся строже, но смысл остаётся тем же.

## 1. Сформулировали

DLS сначала определяет масштаб и риск:

- `micro` — очевидная локальная правка;
- `routine` — компактное изменение с одним `CHANGE.md`;
- `standard` — отдельные definition, implementation и review;
- `roadmap` — несколько связанных delivery slices;
- `critical` — высокий риск для безопасности, данных, concurrency, migration, архитектуры или ключевого пользовательского пути.

Явно выбранный `default_profile` дополняет discovery конкретным словарём платформы.
Bundled `server-backend` направляет внимание на API compatibility,
persistence/migrations, retries/idempotency, deployment, observability и privacy;
`apple` — на затронутые Apple/Swift boundaries. Repository-local профиль может
переопределить bundled профиль. После bounded inheritance DLS фиксирует digest
resolved profile в context, candidate contract, ReviewPack и metrics.

Профиль остаётся подсказкой, а не скрытой методологией: он не содержит argv,
gates, approvals, models или budgets. Domain skills advisory и применяются по
реальному коду; например Vapor/Linux может использовать Swift architecture,
concurrency и testing без Apple UI или App Store gates.

Документы создаются не «потому что так положено». Они появляются только когда разделяют ответственность:

- `CHANGE.md` удерживает небольшое изменение целиком;
- `EPIC.md` объясняет outcome и границы крупного slice;
- `SPEC.md` фиксирует принятое поведение и решения;
- `TICKETS.md` делит реализацию на независимо проверяемые части;
- ADR нужен только для решения, которое действительно важно сохранить отдельно.

Если изменение затрагивает UI, definition должен ссылаться на принятый макет или содержать явное решение работать без него.

## 2. Сделали

После approval implementation получает компактный digest-bound context, а не полный transcript обсуждения. DLS:

- хранит execution state отдельно от authored Markdown;
- запускает только именованные argv-команды из `.dls/config.toml`;
- привязывает evidence к текущей ревизии;
- умеет зарегистрировать отдельный worktree без добавления каждого worktree как проекта Codex;
- возвращает одно типизированное `next_action`, когда следующий шаг пока невозможен.

После committed candidate один `candidate-ready` автоматически завершает
механическую часть implementation: выполняет обязательные review-команды,
создаёт exact-HEAD evidence, записывает заявленные dispositions и атомарно
создаёт ReviewPack. Implementer не переносит state revision, SHA или evidence
paths. Полные validation logs остаются в локальном cache; модель получает только
компактный итог или ограниченный фрагмент ошибки.

Если validation нашёл проблему, Codex исправляет её и коммитит новый candidate.
DLS не требует снова перечислять все findings: при неизменных ReviewIR,
definition, policy и finding set он наследует declaration из ближайшего
предыдущего candidate run, повторяет все обязательные проверки для нового HEAD
и принимает только явные изменения `addressed`/`note`. Потерянную краткую
ошибку можно безопасно восстановить через bounded diagnostic status без чтения
полного лога.

Если review-задача открыта раньше handoff или HEAD изменился после него, DLS не
показывает старый candidate как текущий. Для remediation `review-run` может сам
достроить только механический handoff: убедиться, что current-HEAD dispositions
полны, заново выполнить trusted validation и создать exact-HEAD pack. Он не
редактирует product source и не угадывает semantic status findings. Первый review
без известного base и любой человеческий boundary остаются в implementation.

Implementer может отметить finding как `addressed`, но не как `verified`. Если finding спорный или попал не в тот gate, `note` передаёт его на независимое adjudication и ничего не закрывает. Проверка исправления и reclassification принадлежат reviewer.

### Параллельные changes без глобальной очереди

DLS применяет one-writer к одному change и его owner worktree, а не ко всему
репозиторию. Для нового standard/critical change он может создать linked
worktree от явно выбранного clean SHA и записать stage-aware dependency:

- dependency на implementation не мешает готовить definition;
- `accepted-in-base` требует human acceptance upstream-change и Git ancestry
  его принятого reviewed HEAD в текущем candidate;
- одинаковый изменённый файл разрешает раннюю implementation-работу, но
  блокирует более поздний candidate до интеграции predecessor;
- разные файлы позволяют независимо выполнять validation и review.

`delivery-map` показывает активные changes, доступный параллелизм, derived
integration order и один следующий шаг. DLS не сканирует соседние каталоги,
не выбирает ветку скрытно, не создаёт Codex-задачи и не делает rebase/merge.
Пользователь сам открывает отдельные задачи и принимает integration-решения.

## 3. Независимо проверили

ReviewPack фиксирует:

- утверждённый definition digest;
- base и candidate Git SHA;
- актуальное validation evidence;
- тикеты и требования;
- режим полного или remediation-review;
- непрерывную цепочку предыдущих review.

Routine review — один изолированный Terra/high pass в implementation-задаче,
без Sol, specialists и отдельной review-задачи. Первый standard/critical
acceptance-grade review объединяет structured native diff-review и независимый
semantic review. Для critical-изменений DLS может выбрать до трёх specialist
lenses по фактическим risk tags. Эти проходы запускает DLS, а не текущая задача
и не обязательные subagents.

DLS запрашивает structured output и одновременно понимает bounded встроенное
представление `codex exec review`: raw response остаётся неизменным в cache, а
обычный ReviewIR строится из отдельной проверяемой проекции. Второй модельный
вызов для форматирования routine-result не нужен.

Иногда встроенный review завершает turn, но игнорирует structured schema и
оставляет только свободный итоговый текст. На standard/critical пути DLS
проверяет, что этот текст действительно является последним completed
agent-message в digest-bound transcript, и сохраняет его как `indeterminate`.
Он не считается clean и обязательно проходит независимую semantic
reconciliation. Если transcript не подтверждает output, процесс останавливается
с typed integrity/output action вместо слепого retry.

Повторный review сначала проверяет delta и каждый предыдущий finding. Если
подтверждён хотя бы один `blocker` или `should-fix`, compact reconciliation
сразу импортирует `not-clear`: final-full не запускается. Только clean delta
получает один whole-change pass без предварительной reconciliation.

Короткая review-команда вызывает один end-to-end runner. До каждого model-run он
атомарно записывает `running`, поэтому повторный запуск не создаёт вторую
проверку. Timeout, orphan, API failure, missing output и output cap допускают не
более одной инфраструктурной повторной попытки. Логически противоречивый, но
структурно корректный decision не запускает semantic-анализ заново: его получает
один компактный repair-pass без product source и outputs других lanes.

Перед semantic-вызовом runner сам проверяет совместимость output schema со
strict Codex contract. Если обновление DLS меняет prompt, schema или другой
model-facing input, новый digest контракта разрешает чистую попытку, сохраняя
старую как диагностическую историю и не повторяя уже завершённую native lane.

Runner выбирает только pack текущего HEAD. Для повторного review DLS может сам
создать remediation-pack из последнего ReviewIR; старый незавершённый pack не
может перехватить новую проверку. Итогом всегда служит импортированный
`review_result_path`, а не сырой transcript модели.

Между задачами достаточно двух коротких handoff-команд: `Исправь findings
последнего review EPIC-01.` и затем `Проведи code review EPIC-01.` DLS сам
находит canonical manifest и exact-HEAD pack; копировать findings, SHA или пути
не требуется.

Локальная telemetry отмечает `fresh`, `continued` и `reused`. Retry и новый HEAD
того же remediation manifest считаются продолжением одного cycle. Новый ReviewIR
или совмещение implementation и review в одной длинной задаче считаются reuse и
дают одно неблокирующее предупреждение. Без `CODEX_THREAD_ID` процесс продолжает
работать со статусом `unavailable`.

После успешного импорта ReviewIR DLS без model call строит Delivery Receipt.
Это краткий narrative и traceability view одного change: точный HEAD, состояние
definition, candidate и tickets, последние актуальные required checks,
канонический review, human acceptance и ещё не закрытые release/production
границы. Он вычисляется заново при чтении, нигде не сохраняется и не меняет
state revision. Повторный render на неизменном state byte-identical. При
runner/integrity failure без ReviewIR Receipt не создаётся.

## Что остаётся за человеком

Только человек:

- подтверждает definition;
- разрешает scoped waiver;
- принимает результат;
- решает, можно ли выпускать продукт.

Фразы «всё зелёное» недостаточно. DLS отдельно показывает:

- реализовано ли изменение;
- есть ли актуальное validation evidence;
- получен ли `review-clear`;
- записан ли пользовательский `accept`;
- пройдены ли release и production gates.

## Где экономится контекст

- mechanics вынесены в Python CLI и JSON schemas;
- model-facing review context использует компактную проекцию pack и evidence metadata;
- повторный review получает только последний актуальный ReviewIR и remediation manifest;
- evidence дедуплицируется по command ID;
- successful validation output заменяется компактным результатом и digest;
- implementation handoff использует один deterministic `candidate-ready` вместо цикла CLI-команд;
- повторный candidate после validation failure не повторяет неизменившийся список findings;
- generated status не переписывает authored definition;
- routine work не получает пакет документов для critical work;
- новые сессии используют manifest вместо копирования старого диалога.
- reuse длинной Codex-задачи измеряется отдельно от child review-lanes, чтобы
  controller/context overhead не скрывался в общей цифре.

Это не гарантирует фиксированное число токенов: сложность задач различается. Цель — убрать предсказуемые повторы и оставить модели только работу, требующую понимания.
