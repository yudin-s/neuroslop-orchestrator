# Команды, использованные в сессии

Дата: 15 февраля 2026

## Проверки окружения

- Проверка доступности Ollama (хост):
  - `curl -sS http://localhost:11434/api/tags | head`

## Локальные проверки кода оркестратора

- Быстрый синтаксический чек + тесты:
  - `.venv/bin/python -m compileall -q local_agents && .venv/bin/python -m pytest -q`

## Запуски оркестратора (local_agents)

- Локальный прогон в mock-режиме (sanity check требований/ротации/summary):
  - `.venv/bin/python -m local_agents run --goal "Проверка: базовые requirements из цели + ротация artifacts + rolling summary" --repo . --outdir demo_run --llm mock --approval auto --patches propose --ui plain`

- Локальный прогон с Ollama (был прерван):
  - `.venv/bin/python -m local_agents run --goal "Разработать одностраничник для гостевой книги: пользователи оставляют комментарии, они отображаются списком (новые сверху). Нужны состояния loading/error/empty. Валидация: комментарий не пустой, максимум 500 символов. На бэкенде использовать SQLite (локальный файл), API: GET /api/comments, POST /api/comments. Добавь README с командами запуска backend и frontend. Структура решения внутри outdir: backend/ и frontend/." --repo . --outdir guestbook --llm ollama --approval auto --patches write --ui plain --web off`

## Docker

- Сборка образов:
  - `docker compose build`
  - `docker compose build agents`

- Запуск оркестратора строго через Docker (write-mode) с точным goal:
  - `docker compose run --rm agents run --repo /repo --config /repo/local_agents/config.docker.yaml --outdir guestbook --llm ollama --approval auto --patches write --ui plain --web off --goal "Разработай гостевую книгу. Гостевая книга это сервис в виде одностраничного сайта на котором пользователи могут оставлять комментарии длиной до 180 символов."`

## Чистка артефактов генерации проекта

- Удаление остатков от неудачного scaffolding в `guestbook/`:
  - `rm -rf guestbook/backend/src guestbook/frontend/src`
