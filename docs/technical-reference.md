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
| `candidate-status [--diagnostic]` | Прочитать компактный implementation progress и при необходимости последний bounded validation failure без запуска команд |
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
Нераспознанный, заблокированный или неоднозначный текст не импортируется.
Повторный вызов после обновления DLS может построить projection из уже
завершённого raw output без нового native model call.

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

Подтверждённые `orphan`, `api-failure`, `output-cap` или missing output
получают не более одной автоматической транспортной попытки. `invalid-output`
никогда не повторяет исходный semantic-анализ: безопасная межполевая ошибка идёт
в compact repair, остальные случаи получают `inspect-review-output`. Drift HEAD,
source, definition или pack не запускает модель. Duration timeout является
`budget-exceeded` и не получает дорогой retry. Risk budget ограничивает
processed child tokens, одну lane, command events, duration и transcript:
routine 750k/10m, standard 3m/15m, critical 5m/20m с меньшими per-lane caps.
Budget failure не создаёт ReviewIR и возвращает `inspect-review-budget`.
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
Завершённый digest-bound structured output можно принять без нового model call
только после явного нового budget contract, в который укладывается измеренный
usage. Command, time, transcript, transport и integrity failures так не
восстанавливаются. Остальные terminal budget attempts возвращают
типизированный `inspect-review-budget`, в том числе без output artifact.

Новые packs помечаются `runner_contract: dls-review-runner/v2`,
`context_contract: dls-review-context/v2`, `economy_contract:
dls-review-economy/v1` и `native_output_contract: dls-native-review/v2`. Для них import
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

GitHub releases используют обычные теги, например `v0.7.0`. Plugin manifest
добавляет build metadata `+codex.<cachebuster>`, чтобы Codex отличал обновлённые
локальные и marketplace bundles без искусственного изменения feature version.
