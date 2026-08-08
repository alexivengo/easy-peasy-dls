# Project handoff

Разработка Easy Peasy DLS приостановлена на неопределённый срок с 2026-08-08.
GitHub-репозиторий оставлен публичным и активным, но текущие `Now`/`Next` пункты
не являются обещанием продолжения работ.

## Безопасная точка входа

- Последний опубликованный release: `v0.13.6` (`db1f504392b1bb253229547e446b4971c1916e05`).
- Основная ветка до handoff: `2fce4498bccac8093c86be64f41890c1550f6e93`.
- Архивная точка `main`: commit, добавивший этот файл.
- Статус продукта: Public Preview / alpha. Release и production для более
  поздних веток не подтверждены.

## Сохранённые линии разработки

Ветки намеренно не слиты: каждая фиксирует отдельную линию работы и собственное
lifecycle-состояние.

| Ветка | HEAD до handoff | Содержимое и граница |
|---|---|---|
| `main` | `2fce4498bccac8093c86be64f41890c1550f6e93` | Definition/evaluation docs EF-00, routine fix PV-00 и definition KANO-01 поверх `v0.13.6`. Runtime остаётся v0.13.6. |
| `codex/EF-00-implementation` | `fc1edb9d6d8714c6999e0d604ab63fc9233dbca6` | EF-00/EF-01 M0/M1 deterministic evaluation. Не считать release-веткой. |
| `codex/EF-01-implementation` | `2726c2c5b44096e79bc97b8345e08a4c493ee321` | EF-01/EF-02 static M2 corpus и proof contracts. Live release evaluation не выполнена. |
| `codex/v0.14.0-platform-proof` | `cb716e5f690721a12cd5c10d3717f4f33ad70f0e` | Незавершённый v0.14 runtime candidate с platform/profile и lifecycle fixes. Не слит, не reviewed как финальный release и не выпущен. |

## Lifecycle snapshot

Снимок получен локальным DLS `0.13.6+codex.20260802111333` перед его удалением.
Он документирует состояние, но не заменяет повторную проверку после возобновления.

- `KANO-01` на `2fce449`: `definition-reviewed`, следующий шаг
  `approve-definition`, `accepted=false`; release и production —
  `not-evaluated`. Definition не утверждена.
- `EF-02` на `2726c2c`: `review-clear`, `accepted=true`; release и production —
  `not-evaluated`. Это не разрешение на live M2 или release.
- `EF-00` на `fc1edb9`, `EF-01` на `2726c2c` и `PV-00` на `2fce449`
  содержат несогласованную проекцию: верхний `lifecycle=accepted`, но
  `receipt.accepted=false`, а `next_action` остаётся нетерминальным. Не
  нормализовать это в PASS; сначала воспроизвести и решить расхождение.
- `BX-00` сохранён в `docs/changes/BX-00-bx-mechanics-adoption/` только как
  draft definition. Для него нет DLS state, independent review, approval или
  implementation.

## Проверки на 2026-08-08

| Ветка | Test suite | Public validator | Compileall |
|---|---|---|---|
| `main` | PASS — 76 tests, 87.469s | PASS | PASS |
| `codex/EF-00-implementation` | PASS — 76 tests, 78.366s | PASS в clean clone | PASS |
| `codex/EF-01-implementation` | PASS — 86 tests, 158.273s | PASS в clean clone | PASS |
| `codex/v0.14.0-platform-proof` | PASS — 89 tests, 90.154s | PASS | PASS |

В EF owner-worktrees их старый validator видит сохранённый локальный
`.dls/config.toml` как запрещённый artifact. Поэтому validator дополнительно
запущен на тех же exact HEAD в clean clone; там он прошёл. Исходный `.dls`
намеренно не удалялся.

Команды:

```sh
python3 plugins/dls/scripts/run_tests.py
python3 scripts/validate_public_repo.py
python3 -m compileall -q plugins/dls/scripts plugins/dls/hooks plugins/dls/tests
```

## Что не опубликовано

`.dls`, private review/evidence, task guards, Codex runtime data, cache,
транскрипты, логи и абсолютные локальные пути не входят в Git. Public validator
намеренно запрещает tracked `.dls`. Локальные `.dls` сохранены как evidence,
но новый clone их не получит; lifecycle выше является только handoff-снимком.

## Как возобновить работу

1. Начать с `main`, проверить этот handoff и повторить три команды выше на
   актуальном Python 3.11+.
2. Для runtime-продолжения сравнить `codex/v0.14.0-platform-proof` с `main` и
   провести новый exact-HEAD review перед merge или release.
3. Для evaluation продолжать от `codex/EF-01-implementation`; не подменять
   static M2 records результатом live release evaluation.
4. `KANO-01` и `BX-00` вернуть на definition review/approval до implementation.
5. После любого merge заново разделить implemented, validated, review-clear,
   accepted, release и production. Старые receipts не переносятся на новый HEAD.

Ключевые документы: [README](README.md), [technical reference](docs/technical-reference.md),
[roadmap](docs/roadmap.md), [evaluation roadmap](docs/evaluation-framework-roadmap.md).
