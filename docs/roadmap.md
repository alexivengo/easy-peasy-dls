# Roadmap

Текущая линия: `v0.13.4 Runtime Completion Guard`

Полный прежний KANO-каталог заморожен как
[planning snapshot](archive/kano-snapshot-2026-07-30.md). Он больше не участвует
в runtime validation.

## Now

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

## Next

- UI/design pilot на настоящем change;
- backend review pilot после current definition approval;
- точечная настройка budgets по сопоставимым данным.

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
