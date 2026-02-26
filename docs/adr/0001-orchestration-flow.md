# ADR-0001: Phased orchestration flow

- **Status:** Accepted
- **Date:** 2026-02-26

## Context
Проект управляет несколькими LLM-ролями и должен оставаться воспроизводимым, наблюдаемым и безопасным при работе с локальным репозиторием.

## Decision
Принят фазовый pipeline в `Orchestrator.run()`:
1. reset/rotate `artifacts/`;
2. derive base requirements;
3. run security preflight;
4. optional web research;
5. task planning;
6. role task execution;
7. synthesis and final output.

## Consequences
### Positive
- Предсказуемый порядок действий.
- Простая трассировка по `events.jsonl`.
- Меньше риска использования устаревших артефактов.

### Negative
- Увеличение времени прогона из-за обязательных фаз.
- Необходимость поддерживать строгую совместимость артефактов между фазами.
