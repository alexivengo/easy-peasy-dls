# Easy Peasy DLS и другие AI-development подходы

Срез актуален на **24 июля 2026 года** и основан на публичных описаниях самих проектов. Это не benchmark и не рейтинг: у инструментов разные цели.

## Коротко

| | Easy Peasy DLS | Superpowers | GSD Core | BMAD |
|---|---|---|---|---|
| Главная задача | Управляемая доставка с exact-revision proof | Полная методология разработки через composable skills | Context engineering и spec-driven phase loop | Полный scale-adaptive lifecycle с ролями и workflows |
| Базовый режим | Минимальный путь по риску | Skills включаются автоматически, процесс обязателен | Discuss → Plan → Execute → Verify → Ship | Структурированные analysis, planning, architecture и implementation |
| Агенты | Не по умолчанию; critical получает не более одного дополнительного risk-reviewer | Subagent-driven development и отдельные review-этапы | Heavy work в fresh-context subagents, parallel execution waves | 12+ специализированных ролей и Party Mode |
| Документы | Условные: от отсутствия пакета до EPIC/SPEC/TICKETS | Design и детальный implementation plan | Structured artifacts между фазами и сессиями | Артефакты полного жизненного цикла |
| Контроль человека | Явные definition approval, waiver и accept | Человек подтверждает design, далее возможна длительная автономная работа | Решения фиксируются перед plan, затем фазовый execution | Фасилитируемая работа с экспертными AI-ролями |
| Проверяемость | Candidate SHA, current evidence, ReviewPack/ReviewIR, независимое закрытие findings | TDD, verification и code-review skills | Verify step и fix plans перед завершением фазы | Risk-based testing и workflow-specific проверки |
| Лучше подходит | Разработчику, который уже умеет вести продукт и хочет меньше ceremony | Тем, кто хочет принять цельную методологию агента | Большой работе, где важны свежие контексты и управление фазами | Тем, кому нужен полный процесс и виртуальная экспертная команда |

## Superpowers

[Superpowers](https://github.com/obra/superpowers) называет себя complete software development methodology. Проект автоматически включает skills, формирует design и implementation plan, использует true red/green TDD и subagent-driven development с двумя стадиями review.

Это сильный выбор, если вы хотите, чтобы единая методология управляла процессом сразу в нескольких coding-agent harnesses.

Easy Peasy DLS отличается не отказом от discipline, а областью ответственности:

- skills выбираются явно либо активируются только в подтверждённом DLS-контексте;
- TDD, worktree и subagents не обязательны для каждой задачи;
- механические инварианты принадлежат компактному CLI и одному runtime schema;
- human approvals и exact-revision review являются отдельными проверяемыми объектами.

## GSD Core

Прежний репозиторий [get-shit-done](https://github.com/gsd-build/get-shit-done) архивирован и указывает на актуальный [GSD Core](https://github.com/open-gsd/gsd-core). GSD Core описывает себя как context-engineering и spec-driven framework с циклом Discuss, Plan, Execute, Verify, Ship. Тяжёлая работа выполняется fresh-context subagents, а structured artifacts сохраняют состояние между сессиями.

Это сильный выбор, когда главная проблема — context rot на длинных проектах и нужна дисциплина фазового исполнения.

Easy Peasy DLS тоже сокращает replay контекста, но не делает фазовый orchestration основой каждой задачи. Его основной контракт — человеческое решение, точная ревизия кандидата и доказательства, достаточные для конкретного уровня риска.

## BMAD

[BMAD Method](https://github.com/bmad-code-org/BMAD-METHOD) позиционируется как полный scale-adaptive lifecycle: 34+ workflows, analysis, planning, architecture, implementation, 12+ специализированных ролей и Party Mode.

Это сильный выбор, если нужна готовая структура продуктовой и инженерной команды или фасилитация незнакомых ролей.

Easy Peasy DLS не моделирует организацию. В нём нет обязательных PM, Architect,
UX и Developer personas. Для critical change DLS может добавить ровно одного
независимого risk-reviewer по фиксированному trigger, а владелец продукта
остаётся в диалоге напрямую с Codex.

## Как выбрать

Выбирайте **Superpowers**, если хотите принять цельную автоматическую методологию skills и TDD.

Выбирайте **GSD Core**, если основной риск — потеря качества в длинном контексте и вам подходит фазовый execution.

Выбирайте **BMAD**, если нужен полный lifecycle и набор виртуальных экспертных ролей.

Выбирайте **Easy Peasy DLS**, если вы:

- работаете с Codex;
- хотите управлять definition и acceptance лично;
- не хотите превращать каждое изменение в проект;
- хотите автоматизировать evidence и review integrity;
- предпочитаете меньше обязательных артефактов, но более точные доказательства.

Подходы не обязаны быть взаимоисключающими, но одновременно назначать несколько систем владельцами approvals, state и review нельзя: это снова создаст конфликт правил, ради устранения которого и появился DLS.
