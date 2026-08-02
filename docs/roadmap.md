# Roadmap

Текущая линия: `v0.14.0 Platform Proof & Decision UX`

Полный прежний KANO-каталог заморожен как
[planning snapshot](archive/kano-snapshot-2026-07-30.md). Он больше не участвует
в runtime validation.

## Now

- доказать `M38–M39` на реальном Apple UI definition/design lifecycle;
- доказать `M40` на backend architecture decision;
- доказать `P30/P39` полным backend review/acceptance и абсолютными metrics;
- показывать bounded platform profile в status и exact-review provenance;
- показывать одну локализованную human-decision card без copy-paste digest/SHA;
- не менять модели, routing или budgets до сопоставимых pilot observations.
- RUCORE-E03 regression: authored lifecycle `blocked` не считается semantic
  review verdict; legacy failed repair восстанавливается одним compact repair
  без повторного анализа репозитория.

Стабилизированное ядро v0.13:

- `M71`: owner-worktree определяется до первой product-операции;
- `M72`: actionable primary сохраняется при optional lane/budget failure;
- `P47`: основной проект автоматически маршрутизируется в owner;
- `P48`: critical review завершается ранним `not-clear` без лишней lane;
- `P28`: workspace conflicts получают одно безопасное typed действие;
- повторно подтвердить `M53/M65` реальным EPIC-03a recovery без model call.
- промежуточный remediation-коммит не создаёт partial candidate/review loop.
- implementation/remediation продолжается без повторного пользовательского
  prompt до `open-review-task` или доказанного внешнего blocker.
- прерванный dirty owner продолжает работу после одного явного `Да / Нет`,
  сохраняя draft без stash/reset/transfer.
- plugin-bundled `Stop` guard исполняет нетерминальную границу runtime и не даёт
  checkpoint-отчёту подменить завершение implementation/remediation.
- `M73`: захваченный старой задачей hook безопасно переживает удаление
  versioned plugin root и возвращает `dls-hook-upgrade-required`.
- `M74`: два автоматических продолжения являются абсолютным лимитом одной
  пользовательской активации и не сбрасываются Git-активностью.
- `P49`: исчерпание guard завершает turn одной короткой диагностикой без
  дополнительного model call.

## Next

- точечная настройка budgets по сопоставимым данным.
- `P28`: расширенная conflict inventory без destructive automation;
- `P12`: risk routing вне review только после нескольких platform baselines;
- `A01/A02`: design connectors и immutable design provenance;
- `A04/A05`: model/cost recommendations только по надёжным данным.

## Later

- project-wide delivery overview;
- дополнительные flat platform overlays;
- экспорт receipts/changelog для release tooling;
- connectors только при реальной экономии ручного handoff.

## Not doing

- автономное создание Codex-задач или виртуальная команда;
- parallel writers одного change;
- model-authored executable commands;
- automatic human approval;
- mandatory brainstorming, Plan Mode, TDD, ADR, worktree или epic package;
- executable legacy artifacts и восстановление старых runner state machines.
