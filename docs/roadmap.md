# Roadmap

Текущая линия: `v0.11.2 Core Reset candidate-base fix`

Полный прежний KANO-каталог заморожен как
[planning snapshot](archive/kano-snapshot-2026-07-30.md). Он больше не участвует
в runtime validation.

## Now

- выпустить и проверить Core Reset;
- провести несколько реальных routine/standard/critical pilots;
- измерять model calls, tokens, blocked runs, recovery frequency и ручные шаги;
- подтвердить migration `swift-of-mcp` до применения к реальному state.

## Next

- Platform & Conflict UX только на доказанных проблемах pilots;
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
