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
| `worktree create/prepare/register/...` | Создать selective linked worktree и атомарно передать approved change его owner checkout |
| `dependency set/list/remove` | Управлять stage-aware same-repository dependencies |
| `delivery-map` | Прочитать bounded карту активных changes, dependencies и overlap |
| `status` | Показать производное состояние изменения |
| `design set/status` | Записать typed UI/UX source или bypass и прочитать bounded status |
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
| `candidate-status [--diagnostic]` | Прочитать компактный implementation progress и при необходимости последний bounded validation failure без запуска команд |
| `review-ready` | Проверить candidate и создать full/delta ReviewPack; base повторного review выводится из ReviewIR |
| `review-run` | Выполнить exact-revision review целиком и импортировать ReviewIR |
| `review-status` | Прочитать состояние review без запуска модели |
| `review-start` | Native-only primitive для совместимости и диагностики |
| `review-import` | Атомарно проверить и импортировать ReviewIR |
| `delivery-receipt` | Вычислить read-only narrative и traceability view одного change без model call |
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

### Platform profile runtime

`default_profile` выбирается только явно. DLS сначала ищет
`.dls/profiles/<name>.toml`, затем bundled profile. `extends` разрешается
рекурсивно до восьми уровней: списки объединяются parent-first с
дедупликацией, scalar-поля ребёнка переопределяют родителя. Cycle, filename/name
mismatch, traversal, symlink, oversized input и неизвестные поля являются
integrity/config failure.

Контракт `dls-platform-profile/v1` поддерживает только discovery hints,
common/platform evidence types, domain capabilities и advisory domain skills.
`process_owner` всегда `dls`; профиль не может задавать argv, shell, approvals,
gates, models или budgets. Resolved projection ограничена по размеру и получает
детерминированный digest.

Digest входит в candidate contract. Profile drift на том же HEAD создаёт новый
candidate run, повторяет trusted validation и делает старый ReviewPack
неподготовленным. Новый ReviewPack v2 хранит только compact
`platform_profile {contract,name,digest}`; context manifest содержит bounded
resolved projection, а `doctor` и `review-metrics` — безопасную provenance без
абсолютных profile paths. Legacy artifacts без marker остаются читаемыми.

Bundled `server-backend` наследует `generic` и покрывает API/compatibility,
persistence/migrations/rollback, background work/retries/idempotency,
concurrency/reliability, containers/deployment, observability, privacy и external
dependencies. Его evidence vocabulary не становится gates автоматически.
Vapor/Linux routing не включает Apple UI, App Store или Apple release gates;
Swift architecture/concurrency/testing остаются применимыми по фактическому коду.

Первый локальный Vapor/Linux preflight использовал E03 HEAD `f3c581f` и
repository-owned `bash scripts/ci.sh`. Trusted validation завершилась за
`69.051 s`, записала `25 586` bytes output metadata без overflow и exact-HEAD
evidence. Bounded implementation context составил `143 365` bytes / `17 144`
words. Draft definition остановил pipeline на `approve-definition`; ReviewPack,
candidate run и model call не создавались. Эти числа не являются backend token
baseline и не обосновывают изменение default budgets или model routing.

### UI/UX source и architecture decisions

Optional state contract `dls-design-source/v1` хранит tier, affected surfaces и
один из двух режимов. `source` использует `precedent`, `artifact` или
`external-version`; `bypass` содержит human rationale и `low | medium | high`
UX risk. Tier 1 допускает exact precedent. Tier 2 требует достаточную
версионированную source. Tier 3 не принимает один precedent: нужен immutable
artifact/external version либо явный bypass.

Repository source должна быть repository-relative, regular, tracked, clean и
привязана к exact Git blob плюс SHA-256 canonical content. Absolute path,
traversal, symlink escape и untracked/dirty source являются integrity failure.
External source использует credential-free HTTPS и обязательную explicit
immutable version; DLS её не загружает и не делает скрытых API calls.

Design approval использует `dls-design-digest/v1`: tier, surfaces, mode и
immutable provenance. Architecture approval использует
`dls-architecture-digest/v1`: один canonical ADR либо bounded SPEC region между
`dls:architecture` markers. Adopted packages могут использовать один
однозначный `## Architecture`/`## Architecture and alternatives`; missing или
несколько кандидатов возвращают `record-architecture-decision`.

Architecture approval обязателен только при impact `architecture` или наличии
canonical ADR. `approve --decision definition --include-design` атомарно
записывает два независимых approvals из одного scoped human ответа. Definition
и accept сохраняют snapshots scoped decisions. Для пакетов, подтверждённых до
v0.10.0 одним whole-definition approval, DLS выводит совместимость только из
точного approved Git revision: если bounded architecture digest на той ревизии
совпадает с текущим, отдельное retroactive approval не требуется. Несвязанная
SPEC-правка сохраняет этот architecture approval; изменение самой architecture
region делает его pending. Поэтому unrelated SPEC edit
инвалидирует полный definition approval, но сохраняет design/architecture;
изменение самого design/architecture делает stale и scoped approval, и полный
definition approval. Legacy approvals без markers продолжают whole-definition
формат хранения и не переписываются; bounded compatibility является только
runtime-проекцией точной approved Git revision.

Единый readiness resolver возвращает `record-design-source`, `approve-design`,
`approve-definition-and-design`, `record-architecture-decision` или
`approve-architecture` до context generation, validation и model calls. Context,
новые ReviewPack v2 и Delivery Receipt получают только tier/surfaces/source
kind/digest/approval status. Metrics получают только UI tier, source kind,
bypass boolean и architecture-required. Source refs, raw design content,
bypass rationale, credentials и absolute paths туда не включаются.

### Dependency-aware parallel delivery

Optional state field `dependencies` использует контракт
`dls-change-dependencies/v1`. Запись хранит target change, первую блокируемую
стадию, требуемый milestone, snapshot target definition digest и rationale.
Dependency действует на названную стадию и все последующие: implementation-only
связь не блокирует definition. Self-reference, cycle, depth больше 16,
неизвестный или cross-repository target и stale target definition завершаются до
validation/model calls.

`accepted-in-base` требует current human acceptance target change и Git ancestry
его принятого reviewed HEAD в dependent HEAD. Squash-equivalent integration
принимается только существующим scoped human `exception`, где exact JSON
conditions содержит оба SHA и digest конкретной dependency. DLS сам не создаёт
такое исключение. Изменение dependency contract входит в definition digest и
инвалидирует approval, context, candidate contract и ReviewPack.

После `v0.9.1` обнаружился portability-дефект: acceptance ошибочно сравнивался
с текущим commit SHA владельца change. Поэтому последующий коммит только с
`.dls/**` делал уже принятый product candidate stale и ложно блокировал
`accepted-in-base`. Исправленный контракт хранит DLS-owned digest принятого
product tree без `.dls` и для legacy approvals вычисляет его из принятого SHA.
Acceptance остаётся current только если принятый SHA является предком текущего
owner HEAD, authored definition не менялась, product tree совпадает и рабочие
product files чисты. Dependency проверяет ancestry именно принятого SHA, а не
более нового metadata HEAD. Product change, divergence и dirty source по-прежнему
инвалидируют acceptance.

Implementation workflow теперь fail-fast до semantic discovery. В standard и
critical задаче, включая Plan Mode, сначала выполняются только plugin provenance
и один `delivery-status`; implementation context строится лишь после разрешающего
typed action. `continue-definition`, dependency/approval/rebase boundary или
другой blocker завершают preflight до чтения product source, тестов и длинной
ручной археологии state. Это удерживает модельный контекст на продуктовой работе,
а механическое объяснение границы оставляет DLS.

`worktree create` сначала разрешает explicit base ref в exact commit, затем
вызывает фиксированный `git worktree add -b ...` argv. Dirty caller checkout не
используется как источник diff и не блокирует создание. Default path и branch —
соседний `<repo>-<CHANGE_ID>-<purpose>` и
`codex/<change-id>-<purpose>`. Matching worktree распознаётся идемпотентно;
collision отклоняется. Инициализация DLS, `new`/`adopt`, регистрация, rebase,
merge и удаление не являются скрытыми side effects этой команды.

`worktree prepare` — обязательный implementation handoff для параллельного
standard/critical change. Approval разрешён только после Git-коммита всех
authored definition artifacts. Команда одним fail-closed шагом создаёт worktree,
переносит state, current approval, dependencies и immutable DLS references,
сверяет definition digest и только затем записывает canonical owner в registry.
Registry имеет приоритет над переносимой копией state в старом checkout. При
ошибке новый worktree откатывается; существующий локальный state никогда не
перезаписывается. Fallback через `init`/`adopt` или продолжение в исходном
checkout запрещены.

Definition digest `dls-definition-digest/v2` включает только authored scope,
behavior, requirements, architecture и acceptance criteria плюс dependency
contract. Changelog, validation evidence, findings и generated regions имеют
роль `execution` и не инвалидируют approval. Legacy approval проецируется на
v2 только когда записанный Git SHA воспроизводит исходный пакет и текущие
authored inputs; потерянное dirty-состояние не восстанавливается догадкой.

RCA v0.9.3: прежний workflow мог записать approval на незакоммиченные документы,
создать новый worktree без переноса approval/dependencies, а затем считать
changelog evidence изменением definition. Неоднозначный `run-candidate-ready`
подталкивал модель продолжать в неверном checkout. Новый контракт возвращает
раздельные `prepare-owner-worktree`, `continue-implementation`,
`run-candidate-ready` и `approve-definition` и применяет те же guards в status,
context и candidate runtime.

RCA v0.9.4: `status` и `candidate-ready` уже считали worktree registry
authoritative, но implicit ReviewPack resolver повторял routing самостоятельно и
сначала принимал portable state текущего checkout. Review-задача из main поэтому
могла видеть старый HEAD и сообщать об отсутствии pack, хотя зарегистрированный
owner содержал готовый exact-HEAD candidate. Теперь implicit review selection и
single-flight preflight используют тот же registry-first контракт; stale local
pack, pipeline или `running` lease не могут затмить canonical owner.

Pipeline claim дополнительно содержит invocation-scoped instance ID. Два
одновременных вызова с одним operation ID больше не считаются одним исполнителем
только потому, что они работают в одном OS process: один продолжает pipeline,
второй получает `wait-review`. Это защищает semantic context и finalization от
конкурентной перезаписи.

Overlap contract `dls-change-overlap/v1` сравнивает repository-relative product
paths между recorded worktree base и current HEAD. Общий каталог — advisory
proximity, одинаковый файл — integration blocker для более позднего change.
Порядок задаётся dependency graph, затем `registered_at` и change ID. Раннее
implementation не блокируется; `candidate-ready` и `review-run` повторно
проверяют snapshot перед model call. Legacy registry без base остаётся читаемым
со статусом overlap `unavailable`.

`delivery-map` возвращает `dls-delivery-map/v1`, максимум 64 changes и один
typed action на change. Обычный JSON не содержит абсолютных путей; `--verbose`
предназначен для локальной диагностики. State/CAS и single-flight scoped по
change: два разных owner worktree могут одновременно выполнять validation и
review, а два writer одного change по-прежнему схлопываются или получают reuse
warning.

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

Импорт любого результата с actionable review/acceptance findings атомарно
записывает два immutable artifacts — ReviewIR и remediation manifest — и ссылки
на оба в одной state revision. Это относится и к `review-clear`, если finding
блокирует только acceptance. Успешный runner result поэтому всегда возвращает
`review_result_path`, а actionable результат дополнительно возвращает
`remediation_manifest_path`. Только чистый `review-clear` без такого manifest
может перейти к acceptance gate. Исторический actionable result без manifest
получает typed action `recover-remediation-manifest`; recovery не вызывает
модель повторно.

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
и typed next action, но не запускает процесс. По умолчанию он не возвращает
логи; `--diagnostic` добавляет только последний bounded redacted validation
failure. Только отдельная read-only review-задача запускает `review-run`; она
может достроить отсутствующий remediation handoff, но не редактирует product
source и не создаёт semantic dispositions.

### Candidate continuation после нового commit

Наблюдаемый сценарий зафиксирован 27 июля 2026 года на remediation EPIC-01
`R066–R073`. Первый `candidate-ready` выполнился на committed candidate, но
trusted `swift-test` обнаружил несовместимую integration fixture. После
исправления и нового commit HEAD изменился. При возобновлении DLS отклонил
вызов без повторной полной declaration и потребовал снова передать все восемь
finding IDs как `addressed`, хотя canonical remediation manifest и смысл
dispositions не изменились.

Текущий результат не повреждается: validation для нового HEAD выполняется
заново, dispositions и ReviewPack записываются атомарно, а пользователь не
вводит IDs вручную. Но модель повторяет механический список в своём контексте,
может пропустить или продублировать finding и использует прежний operation ID
для уже другой Git-ревизии. Это лишний orchestration/context overhead и
неудачная семантика resume.

В v0.4.4 действует следующий контракт:

- exact-HEAD retry без изменения source продолжает прежний operation ID;
- новый committed descendant HEAD создаёт новый детерминированный candidate run
  и operation ID, а не маскируется под resume старой ревизии;
- DLS выбирает ближайший по Git ancestry eligible blocked, failed или completed
  run и наследует declaration только при совпадении canonical ReviewIR,
  remediation manifest, definition, command policy и полного набора findings;
- skill передаёт только явные изменения declaration, например перевод
  `note → addressed`; неизменные dispositions повторно не перечисляются;
- evidence прежнего HEAD не переиспользуется: весь обязательный command policy
  выполняется заново и связывается только с новым candidate;
- при изменившемся manifest, definition, divergence истории, неизвестном или
  неполном finding set автоматическое наследование запрещено;
- model-facing сообщение ограничивается failed command, короткой причиной и
  фактом нового validation run; operation IDs и полный список findings не
  пересказываются;
- `candidate-status --diagnostic` восстанавливает bounded redacted excerpt,
  evidence path и локальный redacted log path, если исходный shell payload был
  потерян. Raw output остаётся только в ignored cache.

Новые candidate runs имеют optional marker `dls-candidate-run/v2`, lineage и
digests ReviewIR, manifest, definition, policy и declaration. State schema
остаётся v1. Legacy runs читаются, но не используются для автоматического
наследования. Явный operation ID нельзя повторно связать с другим candidate
contract.

### Guarded activation, runtime provenance и exact-HEAD status

Workflow активируется без ручного skill-chip только при однозначном сигнале:
repository-local DLS config/state, приложенном ReviewIR/remediation manifest,
review ID или change ID, который разрешается зарегистрированным worktree.
Обычная coding/review-задача без этих сигналов DLS не активирует.

Runtime всегда вычисляется относительно реально загруженного `SKILL.md`.
Plugin-local manifest и `scripts/dls.py --version` обязаны совпасть; иначе typed
action — `reinstall-dls-plugin`. Поиск `dls` в `PATH`, sibling checkout,
исходном plugin-репозитории или архиве запрещён. Каждый JSON payload содержит
`dls_version`, поэтому происхождение ответа диагностируется без публикации
локального пути.

В `v0.7.1` это правило усилено после реального scope-leak: review-задача
запустила CLI из внутреннего R&D-архива. Старый `codex exec review --base`
привязался к owner checkout, включил dirty generated `.dls/state` и выдал
ложный finding о незакоммиченном ReviewPack. Такой native attempt не был
каноническим ReviewIR, но мог быть ошибочно переиспользован по одному
`kind=native`.

Теперь guarded workflow допускает implicit activation только при описанных
выше DLS-сигналах. Native runner создаёт не linked worktree, а standalone
`--no-hardlinks` clone точного HEAD, удаляет remote, запрещает Git alternates и
явно передаёт Codex `--cd` этого clone. Model output сначала пишется во
временный runtime path и только затем переносится в owner-local cache. Новый
ReviewPack marker `native_workspace_contract: dls-native-workspace/v1` и
state-owned workspace provenance не позволяют переиспользовать попытку без
доказанной изоляции. Legacy ReviewPack/ReviewIR остаются читаемыми; небезопасный
незавершённый native attempt получает один bounded повтор, а не считается
успешной lane.

`candidate-status`, `review-status` и `delivery-status` используют один
exact-HEAD resolver. Исторический completed candidate доступен по явному
operation ID для аудита, но получает `exact_head: false`, `prepared: false` и
никогда не предлагает `open-review-task`. Missing, tampered или wrong-HEAD pack
не считается подготовленным.

Для remediation без exact-HEAD pack `review-run` может вызвать внутренний
`candidate-ready`: только если canonical ReviewIR/manifest целы, reviewed HEAD —
предок текущего, definition approval и ticket readiness актуальны, product
source clean, validation policy задана, а каждый actionable finding уже имеет
current-HEAD `addressed` или `note`. Trusted commands выполняются заново, pack
создаётся атомарно, затем тот же runner продолжает review. Первый review без base,
неполная declaration, drift или tampering model calls не запускают.

### End-to-end runner

`review-run` использует фиксированный pipeline:

1. routine: один isolated Terra/high structured review;
2. standard/critical: structured native Terra/high;
3. critical: до трёх deterministic Terra/high specialist lanes;
4. независимый Sol high/xhigh semantic pass;
5. compact input-only Sol/high reconciliation только при findings или расхождении;
6. remediation final-full только после clean native + targeted, без отдельной reconciliation;
7. DLS-owned сборка и атомарный импорт ReviewIR.

Model-runs выполняются через `codex exec` в read-only ephemeral режиме с
игнорированием пользовательской model-конфигурации. Standard/critical native
lane использует официальный `codex exec review --base`. Routine использует
официальный `codex exec review` с одним DLS-owned custom target: текущий Codex
CLI запрещает сочетать positional review instructions и `--base`, поэтому
точные base/head SHA находятся в immutable prompt contract и отдельно
проверяются DLS до и после вызова.

Во всех случаях DLS передаёт `--output-schema`. Некоторые текущие сборки Codex
принимают этот флаг, но встроенный `codex exec review` всё равно записывает
человекочитаемый presentation. DLS сохраняет такой raw output и digest
неизменными и строит отдельную bounded projection: полный P0-P3 review comment
становится native finding, а только явный `review-clear`/`no findings` — чистым
native результатом. Для routine projection также содержит итоговый verdict;
для standard/critical она остаётся входом независимой semantic adjudication.
Нераспознанный текст никогда сам по себе не доказывает clean review.

В `v0.8.0` проявился ещё один контрактный разрыв: built-in review завершил turn
с exit code 0, но вместо JSON вернул положительно сформулированный свободный
текст. Строгая projection закономерно отклонила его, однако status продолжал
предлагать `resume-review`, хотя тот же parser детерминированно получал ту же
ошибку. `v0.8.1` добавляет безопасный промежуточный статус
`native_decision_status: indeterminate` только для standard/critical review.
DLS проверяет raw output digest, transcript digest, отсутствие failed events,
совпадение `--output-last-message` с последним completed `agent_message` и
следующий за ним `turn.completed`. Затем сохраняет отдельную нормализованную
projection, не изменяя raw output, и обязательно запускает semantic
reconciliation. Такой native result не считается clean, не создаёт verdict сам
по себе и не отменяет независимый semantic pass.

Legacy `invalid-output` может быть восстановлен из уже завершённого raw output
без нового native model call. Неподтверждённый transcript получает
`inspect-review-output`, а digest/path drift — `inspect-review-integrity`;
повторный вызов не образует бесконечный resume-loop. Routine путь сохраняет
строгий итоговый verdict и не использует indeterminate fallback, потому что у
него нет независимой Sol lane.

Release-gate v0.6.1 обнаружил ещё один безопасно восстанавливаемый вариант
clean presentation: Codex завершил анализ фразой `No actionable regressions were
identified in the reviewed diff`, но v0.6.0 принимал только начальные маркеры
`review-clear`/`no findings`. v0.6.1 распознаёт эту точную самостоятельную
clean-фразу, но по-прежнему отклоняет неоднозначные продолжения вроде `..., but
an issue remains`. Existing raw output и digest не переписываются; invalid-output
attempt восстанавливается штатной plaintext projection без повторного native
model call.

Все DLS-owned semantic decisions используют repository-owned
prompt templates и schemas. Перед модельным вызовом DLS локально проверяет
strict Structured Outputs contract: каждый object запрещает дополнительные
поля, а `required` точно совпадает с `properties`. Механически некорректная
schema поэтому останавливается до API-вызова. Native, semantic и specialist
lanes получают disposable detached worktree exact HEAD, поэтому локальные DLS
metadata не попадают в анализ candidate. Independent lanes не видят native
output или drafts соседних lanes; reconciliation получает только digest-bound
inputs в input-only workspace без product checkout.

До запуска модели `StateStore` атомарно записывает attempt со статусом `running`.
Для сочетания `review ID + lane + pass` возможна только одна активная попытка.
Повторный `review-run` возвращает `status: running` и `next_action: wait-review`,
не запуская вторую модель. Внутренние operation IDs включают review ID, поэтому
одинаковая пользовательская метка не смешивает attempts разных ревизий.
`review-status` только читает state.

Если для текущего HEAD нет ReviewPack, `review-status` не показывает прежний
ReviewIR как текущий. `review-run` пытается только guarded remediation recovery,
показывая `candidate-transition: preflight | validating | prepared`. Во время
единственного активного candidate run status — `preparing-candidate` и
`wait-review`. Если доказуемое восстановление невозможно, runner возвращает один
typed action без model calls. Поля `prior_review_id` и
`prior_review_result_path` остаются только исторической ссылкой.

Implicit `review-run` сначала разрешает registered owner и лишь затем читает
state, pack и lease этого owner. Текущий checkout используется только когда для
change нет отдельной registry binding. Relative `--pack` остаётся строгим
current-checkout selector, absolute `--pack` — явным one-off owner selector.

### Наблюдаемость и финализация

`review-run --stream` является обычным каналом прогресса: `started`, переходы
lanes, heartbeat не чаще минуты, budget warning и `completed`. Он не запускает
второй runner. `review-status` по умолчанию возвращает компактный `progress`: текущий pipeline
stage, активную lane, количество completed/projected lanes, elapsed time,
последний переход, размер model-facing context и локально извлечённые Codex token
counters. Полные argv, cache paths и provenance доступны только с `--verbose`,
чтобы обычный heartbeat сам не расходовал контекст агента.

Skill ждёт исходный streamed process и вызывает `review-status` только если
shell/session потерян. Сырые transcripts и предварительные findings не
транслируются.

Pipeline отдельно фиксирует `running`, `finalizing`, `failed-finalize` и
`completed`. Ошибка lane переводит pipeline в `failed` и сохраняет извлечённую
причину API/CLI, а `next_action` предлагает retry только когда runner
действительно может его выполнить. Если все model lanes завершились, но
deterministic assembly или atomic import не прошли, следующий `review-run`
проверяет exact HEAD, pack, context и output digests, переиспользует completed
attempts и повторяет только финализацию.

### Канонические идентификаторы review

Новые ReviewPack помечаются
`identifier_contract: canonical-ticket-ids/v1`. Каждый model prompt получает
точный список допустимых ticket IDs. Ссылки проверяются сразу после каждой
specialist или semantic lane — до запуска следующей дорогой модели.

Raw model output и его digest остаются неизменными. DLS создаёт отдельную
каноническую проекцию для downstream lanes и ReviewIR. Допустимы только точный
ticket ID или однозначные сокращения хвоста `T02` и `T-02`; fuzzy matching,
изменение регистра и удаление ведущих нулей запрещены. Применённые преобразования
сохраняются в DLS-owned `identifier_normalizations`. `requirement_ids` не
нормализуются: до появления отдельного реестра они копируются точно из authored
inputs.

Это устраняет класс сбоев, при котором структурно корректный model JSON проходил
несколько lanes, а неизвестная ссылка обнаруживалась только при ReviewIR import.
Для старого `failed-finalize` DLS сначала выполняет deterministic reassembly из
проверенных completed attempts. Однозначная ссылка исправляется без model call;
логически противоречивый decision передаётся в bounded repair, а не в повторную
terminal или whole-epic lane. Native, specialists и independent semantic при
этом не повторяются.

### Decision repair вместо слепого semantic retry

В `v0.4.2` межполевая ошибка semantic JSON считалась обычным `invalid-output`.
Runner повторял исходную lane с тем же prompt, но не передавал модели причину
отказа. Это могло дважды оплатить один и тот же анализ и получить тот же дефект,
как в случае `still-open` без обязательного replacement finding.

Новый контракт `dls-decision-repair/v1` разделяет два класса отказов:

- timeout, API failure, missing output и output cap могут один раз повторить
  транспортный вызов исходной lane;
- логически противоречивый, но parseable decision не повторяет анализ. DLS
  запускает отдельную compact Sol repair lane.

Repair получает только immutable raw decision, полный список структурированных
ошибок с JSON path, допустимые ticket/prior IDs, полные canonical prior findings
и заранее зарезервированные DLS replacement IDs. Runner собирает все безопасно
классифицируемые межполевые ошибки до model call, чтобы не открывать их по одной
в нескольких repair-циклах. Временный Git workspace не содержит
product source, native output, specialist results, sibling semantic drafts или
пользовательскую конфигурацию. Общий repair bundle ограничен 256 KiB.

DLS не сочиняет finding самостоятельно. Модель возвращает полный decision по той
же strict schema, но может менять только ссылочную структуру, необходимую для
исправления конкретной ошибки. Verdict, summary, prior verdicts, evidence и уже
валидные findings сохраняются; classification нового replacement finding должна
совпасть с canonical prior finding. Повторное логически некорректное repair-
решение не запускается ещё раз. Только инфраструктурный отказ самой repair lane
получает один transport retry.

До model call state атомарно хранит отдельный repair attempt и digest контракта.
ReviewIR v2 может содержать state-owned `semantic.repairs`: original и repair
attempt IDs, raw/error/input/output digests, model/effort, timestamps и transcript
digest. Import сверяет эти поля с DLS state и неизменным raw output. Новый pack
объявляет `decision_repair_contract: dls-decision-repair/v1`; старые ReviewPack и
ReviewIR остаются читаемыми и не переписываются.

`resume-review` означает повторяемую deterministic finalization,
`resume-review-repair` — продолжение одной compact repair lane и ещё не
выполненных downstream lanes, `inspect-review-output` — окончательно
некорректный или небезопасный model output, а `inspect-review-integrity` —
tampering или drift. Предварительные findings во всех случаях остаются скрыты до
успешного канонического импорта.

Canonical ticket verdicts не доверяются модели. DLS механически связывает
findings с tickets и вычисляет review state из severity и `blocks`. Поэтому
release/production-only note сохраняется в ReviewIR, но не делает code-review
ticket `not-clear`. Общий review verdict выводится из тех же stage-correct
relations.

### Task context и короткий handoff

`candidate-ready`, `candidate-status`, `review-run`, `review-status`,
`delivery-status` и `review-metrics` возвращают безопасный объект
`dls-task-context/v1`. Он различает свежую задачу (`fresh`), продолжение того же
canonical cycle (`continued`), reuse задачи для другого cycle или роли
(`reused`) и отсутствие доступного Codex identifier (`unavailable`).

Implementation cycle определяется definition approval + review base для первого
candidate либо canonical ReviewIR + remediation manifest для remediation.
Descendant HEAD одного manifest не создаёт новый cycle. Review cycle определяется
точным ReviewPack ID и digest. Совмещение implementation и независимого review в
одной задаче помечается `reused/cross-role`; routine fast-path является
намеренным исключением.

`reused` не меняет exit code и основной `next_action`: stream один раз сообщает
`context-warning`, а payload рекомендует `open-fresh-task`. Raw thread/turn ID и
локальная ссылка на rollout остаются только в ignored
`.dls/cache/telemetry`. State, stdout, metrics и canonical artifacts содержат
только классификацию, роль и безопасные counts. Отсутствующая или повреждённая
необязательная telemetry даёт `unavailable`; symlink/path traversal остаётся
integrity error.

Нормальный handoff поэтому состоит из одной строки: `Исправь findings последнего
review CHANGE_ID.` или `Проведи code review CHANGE_ID.` Manifest, findings, SHA
и пути повторно в контекст модели не копируются.

`review-metrics` возвращает обратно совместимый `dls-review-metrics/v1`: child
lanes, retries, repairs, elapsed, command events и
input/cached/output/reasoning tokens. Для каждой lane указывается
`usage_source: state | transcript | reported-zero | unavailable`, cached-input
ratio, processed tokens на command event и безопасная сводка model-facing
context: compact/large-context, bytes, words и input count. `--verbose` добавляет
только counts inputs по reason, без путей и содержимого.
`processed_tokens = input + output`; cached tokens уже входят в input и повторно
не суммируются. Нулевой native usage имеет source `reported-zero`, значение
`null` и причину `native-reported-zero`, а не выдуманную оценку или ноль.
Локальный Codex adapter читает только envelope types, lifecycle и usage events
текущей задачи; он считает model messages, tool calls, tool outputs и token
samples, но не читает и не возвращает их содержимое. ID остаётся в ignored
cache, наружу выходит hash.
Активная задача даёт lower bound, завершённая после `--refresh` — exact total,
если все child lanes также измерены. Prompts, сообщения, reasoning и raw outputs
в metrics не попадают. Дополнительно показывается controller share of measured
usage и task reuse status; внешний analytics service не используется.

`delivery-status` возвращает один typed next action и не более 2 KiB.
`cache-prune` по умолчанию dry-run. Canonical state/ReviewPack/ReviewIR,
remediation manifests и evidence не удаляются; raw cache хранит active,
failed/recoverable runs, два последних completed reviews и всё моложе 14 дней.

### Delivery Receipt

`delivery-receipt CHANGE_ID` — чистая derived projection с контрактом
`dls-delivery-receipt/v1`. Обычный CLI выводит русский Markdown до 4 KiB,
`--json` — structured view до 16 KiB. Resolver читает state v1, authored
artifacts, latest exact-HEAD evidence, последний canonical ReviewIR и derived
approval statuses. Он не пишет state, cache или repository files и не запускает
модель. Время генерации отсутствует, поэтому неизменный state даёт byte-identical
Markdown, JSON и digests.

`receipt_digest` считается по structured projection без Markdown и digest
полей; `markdown_digest` — отдельно. Title берётся из первого H1 `CHANGE`,
`EPIC` или `SPEC`, outcome — только из известной authored outcome-секции. При
отсутствии outcome DLS не сочиняет текст. Raw transcripts, prompts, reasoning,
model output, validation stdout, absolute paths и task IDs в Receipt не входят.
Списки имеют bounded `items` и явный `omitted_count`.

Latest canonical ReviewIR — единственный источник текущих findings; история
показывается отдельными counts и не влияет на gates. Review считается current
только для текущего clean HEAD. Acceptance действует только как current human
approval этого HEAD. Release и production имеют лишь `blocked` или
`not-evaluated`: отсутствие finding никогда не превращается в readiness.

После успешного `review-import`/`review-run` Receipt добавляется к результату, а
stream получает ровно одно событие `delivery-receipt` перед `completed`.
`approve --decision accept` возвращает обновлённый accepted Receipt. Failure без
канонического ReviewIR его не создаёт. Receipt остаётся presentation layer, а не
новым approval, review result или canonical artifact.

Подтверждённые `orphan`, `api-failure`, `output-cap` или missing output
получают не более одной автоматической транспортной попытки. `invalid-output`
никогда не повторяет исходный semantic-анализ: безопасная межполевая ошибка идёт
в compact repair, остальные случаи получают `inspect-review-output`. Drift HEAD,
source, definition или pack не запускает модель. Duration timeout является
`budget-exceeded` и не получает дорогой retry. Risk budget ограничивает
processed child tokens, одну lane, command events, duration и transcript.
Token target и hard recovery ceiling различаются: routine 750k/825k,
standard 3m/3.3m, critical 8m/8.8m aggregate; per-lane critical target/ceiling
равны 6m/6.6m. Target overrun внутри ceiling сохраняет результат и явное
предупреждение. Hard ceiling, timeout, command, transcript или output failure не
создаёт ReviewIR и возвращает `inspect-review-budget`.
Model, effort, prompt, schema, context, pack, HEAD и repair input
входят в digest lane contract вместе с effective budget. Completed lane
переиспользуется только при точном совпадении этого digest. Поэтому изменение
bounded budget является явным новым execution contract, а не скрытым retry
прежней попытки.

В `v0.5.0` command-event budget ошибочно считал `item.started` и
`item.completed` одного Codex command как два вызова. На реальном EPIC-01 это
превратило 17 логических команд в 34 события и ложно превысило critical cap 32.
Контракт `logical-invocations/v1` дедуплицирует пары по immutable item ID;
анонимные legacy events по-прежнему считаются отдельно. Старый budget failure
может получить ровно один corrected retry только если сохранённый transcript и
его digest доказывают этот точный double-count, а логическое число вызовов
укладывается в исходный budget. Остальные budget failures не становятся
retryable.

Первый real critical pilot после исправления double-count дошёл до 36
логических команд и 5.51 млн processed tokens. Он показал, что прежние caps 32
commands, 2.5 млн на lane и 5 млн aggregate обрывали уже завершённый валидный
targeted-pass. В `v0.6.0` critical budget откалиброван до 48 commands, 1.5 MiB
transcript, 6 млн на lane и 8 млн aggregate; 20-минутный timeout не расширялся.
В `v0.9.1` подтверждён второй класс сбоя: final-full EPIC-01 завершился с
валидным structured output, но usage стал известен лишь в `turn.completed` и
оказался 6 364 076 tokens при target 6m; общий child total достиг 8 718 502 при
target 8m. Старый контракт выбросил уже оплаченный результат. Контракт
`dls-review-budget/v2` разрешает zero-call recovery только внутри ограниченного
10%/absolute ceiling и после проверки exact HEAD, source, definition, pack,
output/transcript digests и исходных non-token limits. Status возвращает
`resume-review-budget`; raw artifacts и usage не меняются. Command, time,
transcript, transport, integrity и over-ceiling failures так не
восстанавливаются.

Remediation final-full выполняется не в product checkout, а в input-only
workspace. DLS формирует exact `epic.patch`, coverage manifest со всеми changed
paths/blob IDs, compact context и budget plan. В `v0.9.5` 16 inspection commands
остаются model-facing target, но runtime hard ceiling равен 24: prompt требует
batch reads и резерв для финального JSON. 15-минутный timeout, transcript 1 MiB
и общий input bundle 2 MiB не расширялись. Превышение текущего ceiling остаётся
terminal `inspect-review-budget`.

В `v0.9.5` проявился отдельный integrity gap: compact context перечислял
ReviewPack projection и filtered requirements по owner-local путям
`.dls/cache/context/...`, а prompt одновременно запрещал input-only модели
читать что-либо вне `.dls-review-input`. Runner проверял digests, но копировал
эти два файла за объявленную sandbox-границу. Поэтому final-full мог честно
сообщить, что заявленный ReviewPack недоступен; тот же дефект существовал в
reconciliation.

`v0.9.6` вводит `dls-bound-context-inputs/v1`. Перед обеими input-only lanes DLS
проверяет исходный context manifest и digests, затем создаёт immutable bundle:

- `.dls-review-input/bound-inputs.json`;
- `.dls-review-input/bound/review-pack.json`;
- `.dls-review-input/bound/requirements.json`, когда projection существует.

Manifest содержит только reason, stable relative path, bytes и SHA-256; исходные
локальные paths в model input contract не переносятся. Bundle digest, count и
bytes записываются DLS-owned lane provenance. Workspace повторно сверяет context,
manifest и физические copies до model call. Полный ReviewPack больше не
учитывается как «виртуальный» input в final-full budget: учитываются реально
переданные compact files. Исторические ReviewPack/ReviewIR остаются читаемыми и
не переписываются; прежний finding закрывается только следующим независимым
review.

Hotfix появился после EPIC-02a: final-full успел сделать 17 команд при лимите
16, был убит до structured output, хотя duration, transcript и aggregate token
budgets не были исчерпаны. Старый общий текст ошибки также не различал command
и transcript overflow. Теперь state сохраняет `budget_failure_kind`, фактическое
и допустимое значение. Legacy exact-HEAD attempt 17/16 получает typed action
`resume-review-command-budget`: тот же `review-run` повторяет только final-full
ровно один раз под новым digest-bound contract, переиспользуя native, targeted
и reconciliation. Current ceiling 24, timeout, transcript, integrity и drift
автоматически не повторяются. Если полное покрытие не помещается, DLS не
обрезает его молча и возвращает `split-review-scope` до model call.

Новые packs помечаются `runner_contract: dls-review-runner/v2`,
`context_contract: dls-review-context/v2`, `economy_contract:
dls-review-economy/v1`, `budget_contract: dls-review-budget/v2`,
`native_output_contract: dls-native-review/v2` и
`native_workspace_contract: dls-native-workspace/v1`. Для них import
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
- Server-backend profile — второй runtime adapter, проверенный локальным
  Vapor/Linux preflight без live model review.

## Версионирование

GitHub releases используют обычные теги, например `v0.10.1`. Plugin manifest
добавляет build metadata `+codex.<cachebuster>`, чтобы Codex отличал обновлённые
локальные и marketplace bundles без искусственного изменения feature version.
