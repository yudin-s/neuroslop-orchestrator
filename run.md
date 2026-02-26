# Guestbook MVP — как запустить

## Генерация решения агентами (в Docker, с доступом к локальному Ollama)

Агенты запускаются ВНУТРИ контейнера, но обращаются к модели на хосте через API (`host.docker.internal`).
В Docker-режиме мы используем конфиг [local_agents/config.docker.yaml](local_agents/config.docker.yaml), где разрешены любые shell-команды (они остаются внутри контейнера).

Пример запуска (результат будет записан в папку `goal/`):

```bash
docker compose run --rm agents run \
  --repo /repo \
  --config /repo/local_agents/config.docker.yaml \
  --outdir goal \
  --llm ollama \
  --approval auto \
  --patches write \
  --web on \
  --ui plain \
  --goal "..."
```

## 1) Установка зависимостей

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

## 2) Запуск backend (FastAPI + SQLite)

```bash
./.venv/bin/python backend/app.py
```

Backend поднимется на `http://127.0.0.1:8000`.

SQLite база хранится в `backend/guestbook.sqlite3`.

## 3) Запуск frontend (простая статика)

В отдельном терминале:

```bash
./.venv/bin/python -m http.server 5173 --directory frontend
```

Открой в браузере: `http://127.0.0.1:5173`

## 4) Быстрая проверка API

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/api/entries
curl -s -X POST http://127.0.0.1:8000/api/entries \
  -H 'Content-Type: application/json' \
  -d '{"name":"Serge","message":"Hello"}'
```
# Боевой запуск: создать гостевую книгу в test_project

Цель: с помощью нашей мультиагентной системы создать проект `test_project` — сайт “гостевая книга”, где посетители могут оставлять пожелания (React + Tailwind), а пожелания сохраняются в MongoDB.

Ниже — полный порядок запуска (локально и через Docker). Команды дублируют рабочий сценарий.

## 0) Подготовка

Перейти в корень репозитория (где лежит `local_agents/`):

```bash
cd /Users/sergejudin/Develop/neuroslop
```

Создать целевую папку (если ещё нет) и сделать её git-репозиторием (нужно для `--patches apply`).
Если ты пересоздавал папку `test_project`, просто повтори эти команды:

```bash
mkdir -p test_project
cd test_project
git init
cd ..
```

## 1) Запуск моделей на хосте (Metal)

Установить Ollama и поднять локальный сервер (если ещё не запущен):

```bash
ollama serve
```

Подтянуть модели (полноценный режим; обычно быстрее для агентских задач):

```bash
ollama pull qwen2.5:7b
ollama pull qwen2.5-coder:7b
```

Проверить, что Ollama отвечает:

```bash
curl http://localhost:11434/api/tags
```

## 2) Запуск системы локально (без Docker)

Установить зависимости Python (один раз):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

(Опционально) Веб-наблюдение (в отдельном терминале):

```bash
source .venv/bin/activate
python -m local_agents serve --repo test_project --host 127.0.0.1 --port 8080
```

Открой: http://127.0.0.1:8080

Запуск агентов на создание полнофункциональной гостевой книги (React + Tailwind + MongoDB):

```bash
source .venv/bin/activate
python -m local_agents run \
  --goal "Собери в test_project полнофункциональную гостевую книгу: frontend на React + Tailwind (современный UI-kit стиль: карточки/кнопки/инпуты/валидация), backend API на Node.js (TypeScript) и хранение пожеланий в MongoDB.

Функционал:
- Пользователь вводит пожелание (только текст), лимит 180 символов, видит счетчик.
- Отправка сохраняет запись в MongoDB.
- На главной странице отображается список пожеланий (новые сверху) с датой/временем.
- Валидация должна быть и на фронте, и на бэке (не принимать пустые/длинные строки).

Требования к проекту:
- Структура репозитория: frontend/ и backend/ (каждый со своими package.json).
- Frontend: Vite + React + TS, Tailwind подключён, никаких платных Tailwind UI компонентов; только типовые классы Tailwind.
- Backend: минимальный HTTP API (Express/Fastify допустимо) + Mongoose/официальный драйвер.
- Docker Compose: подними MongoDB сервисом, backend должен уметь подключаться через MONGODB_URI из .env.
- Добавь .env.example, README.md с точными командами запуска: 1) docker compose up -d mongodb 2) backend dev 3) frontend dev.
- Добавь простые smoke-check команды (curl для API) и краткий тест-план в artifacts/." \
  --repo test_project \
  --llm ollama \
  --approval interactive \
  --patches apply \
  --ui live
```

Где смотреть результаты:
- Код/файлы проекта: `test_project/`
- Артефакты агентов и логи: `test_project/artifacts/`
- События: `test_project/artifacts/events.jsonl`

Дальше запускай `test_project` по инструкциям, которые агенты положат в `test_project/README.md`.

## 3) Запуск системы в Docker (агенты изолированы, модели на хосте)

Собрать контейнеры:

```bash
docker compose build
```

SAFE режим (workspace read-only, патчи только как .diff):

```bash
docker compose up ui
# в другом терминале
docker compose run --rm agents run \
  --goal "Создай гостевую книгу как одностраничный сайт, лимит 180 символов" \
  --repo /repo/test_project \
  --approval auto \
  --patches propose
```

DEV режим (workspace read-write, можно применять патчи):

```bash
docker compose --profile dev up ui-dev
# в другом терминале
docker compose --profile dev run --rm agents-dev run \
  --goal "Создай гостевую книгу как одностраничный сайт, лимит 180 символов" \
  --repo /repo/test_project \
  --approval interactive \
  --patches apply
```

Примечания:
- Контейнер ходит в Ollama на хосте через `OLLAMA_BASE_URL=http://host.docker.internal:11434`.
- Для применения патчей внутри Docker нужен dev-профиль (rw-mount).

## 4) QA (опционально): Playwright

Если в `test_project` будет настроен Playwright, можно добавить проверку после QA шага:

```bash
python -m local_agents run --goal "..." --repo test_project --qa-playwright run
# или
python -m local_agents run --goal "..." --repo test_project --qa-playwright ui
```
