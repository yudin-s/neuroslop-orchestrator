# Local multi-agent (Ollama) — MVP

[![Tests](https://github.com/yudin-s/neuroslop-orchestrator/actions/workflows/tests.yml/badge.svg)](https://github.com/yudin-s/neuroslop-orchestrator/actions/workflows/tests.yml)
[![Lint](https://github.com/yudin-s/neuroslop-orchestrator/actions/workflows/lint.yml/badge.svg)](https://github.com/yudin-s/neuroslop-orchestrator/actions/workflows/lint.yml)

Это минимальный мультиагентный каркас для локального теста: один оркестратор + 5 ролей.

## Как устроен пайплайн
- Оркестратор формирует базовые требования в `artifacts/requirements.md`.
- Роль `security_requirements` выполняет preflight и пишет NFR/threat model.
- Затем оркестратор строит план и запускает роли `backend` → `designer` → `frontend` → `qa`.
- Все этапы и артефакты логируются в `artifacts/events.jsonl`.

Архитектурные артефакты:
- [docs/architecture.md](docs/architecture.md)
- [docs/adr/0001-orchestration-flow.md](docs/adr/0001-orchestration-flow.md)
- [docs/adr/0002-safety-gates.md](docs/adr/0002-safety-gates.md)

Роли:
- `frontend`
- `backend`
- `designer` (UI/UX спецификация под Tailwind)
- `security_requirements` (ИБ + требования)
- `qa`

## Требования
- macOS
- Python 3.11+
- Ollama: https://ollama.com

Опционально:
- Docker Desktop (если хочешь изолировать агентов в контейнере)

Память/железо (очень грубо):
- 3B модели — комфортно почти везде
- 7B модели — заметно лучше для TypeScript/патчей, но требуют больше RAM

## Быстрый старт
1) Установить зависимости:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2) Поднять локальную модель в Ollama (выбери одну маленькую):

```bash
ollama pull qwen2.5:7b
ollama pull qwen2.5-coder:7b

# Фолбэки, если 7b тяжеловато:
ollama pull qwen2.5:3b
ollama pull qwen2.5-coder:3b
```

3) (Опционально) Проверить, что Ollama работает:

```bash
curl http://localhost:11434/api/tags
```

## Изоляция через Docker (модели остаются на хосте / Metal)
Если ты хочешь изолировать “ИИшек” от остальной системы, но при этом **не терять производительность моделей на Apple Silicon**,
то хорошая схема такая:

- Ollama/модели крутятся на macOS хосте (Metal)
- оркестратор/агенты и UI крутятся в Docker
- контейнер видит workspace только read-only, а пишет только в `./artifacts`

### 1) Поднять Ollama на хосте
Убедись, что `ollama serve` запущен и отвечает:

```bash
curl http://localhost:11434/api/tags
```

### 2) Собрать контейнер

```bash
docker compose build
```

### 3) Запустить web UI (наблюдение)

```bash
docker compose up ui
```

Открой: `http://127.0.0.1:8080`

В веб-UI есть простые фильтры по `type`/`role` и кнопка скачивания сырого лога `events.jsonl`.

## Web research (поиск решений в интернете)
Можно разрешить оркестратору ограниченно искать/читать публичные страницы (документация/примеры) и складывать выжимку в артефакт.

Включение:

```bash
python -m local_agents run --goal "..." --repo . --web on
```

Что появится:
- `artifacts/web_research.md` — краткая практичная выжимка + ссылки

Ограничения безопасности:
- доступ только к доменам из `web.allow_domains` в `local_agents/config.yaml`
- запрещены localhost/приватные IP, нестандартные порты, большие ответы

### 4) Запустить агентов

```bash
docker compose run --rm agents run \
  --goal "Проработай логин" \
  --repo /repo \
  --approval auto \
  --patches propose
```

Примечания:
- В `docker-compose.yml` выставлен `OLLAMA_BASE_URL=http://host.docker.internal:11434`, чтобы контейнер ходил в Ollama на хосте.

### Профили compose: safe vs dev
Чтобы не править compose руками, есть два режима:

- **safe (по умолчанию)**: сервисы `ui` и `agents` монтируют workspace как read-only (`.:/repo:ro`).
  Подходит для генерации артефактов и патчей в виде `.diff`.

- **dev (профиль)**: сервисы `ui-dev` и `agents-dev` монтируют workspace как read-write (`.:/repo:rw`).
  Это нужно, если ты хочешь применять патчи (`--patches apply`).

Команды:

```bash
# SAFE (read-only workspace)
docker compose up ui
docker compose run --rm agents run --goal "..." --repo /repo --patches propose

# DEV (read-write workspace)
docker compose --profile dev up ui-dev
docker compose --profile dev run --rm agents-dev run --goal "..." --repo /repo --patches apply --approval interactive
```

## Запуск (основной)
Минимальный запуск:

```bash
python -m local_agents run --goal "Сформируй требования и план работ для фичи логина" \
  --repo . \
  --model qwen2.5:7b
```

Что произойдёт:
- Сначала роль `security_requirements` создаст артефакты требований/ИБ
- Потом оркестратор раздаст задачи `backend → designer → frontend → qa`
- Все события пишутся в `artifacts/events.jsonl`

## Наблюдение в реальном времени (UI)
Есть два способа смотреть прогресс:

- Live-дашборд в терминале: добавь `--ui live`
- Поток событий: всегда пишется `artifacts/events.jsonl` (JSON Lines)

Пример:

```bash
python -m local_agents run --goal "Проработай логин" --repo . --ui live
```

### Веб-интерфейс (одна страница + SSE)
Удобно, если хочешь наблюдать в браузере.

Терминал 1 (поднять UI):

```bash
python -m local_agents serve --repo . --host 127.0.0.1 --port 8080
```

Открой: `http://127.0.0.1:8080`

Терминал 2 (запустить агентов):

```bash
python -m local_agents run --goal "Проработай логин" --repo .
```

## Тестирование без модели (mock)
Для прогонов без Ollama есть режим `--llm mock` — детерминированные ответы, удобно для автотестов.

```bash
python -m local_agents run --goal "mock" --repo . --llm mock --approval auto --ui off
pytest -q
```

Артефакты появятся в `artifacts/`.

## Демо-проект: гостевая книга (SPA)
В этом репозитории есть минимальный пример “guestbook” (одна страница): пользователи оставляют сообщения длиной до 180 символов.

Запуск:

```bash
pip install -r requirements.txt
python backend/app.py
```

Открой: `http://127.0.0.1:8000`

API:
- `GET /api/messages`
- `POST /api/messages` с JSON `{ "text": "..." }`

## Безопасность / tool-gates
По умолчанию включён режим `--approval interactive`: любые изменения файлов/команд требуют подтверждения.
Для автопрогона: `--approval auto`.

При `--patches apply` перед применением патча выполняется `git apply --check`. После успешного apply запускаются доступные проверки (lint/test/build), если соответствующие команды есть в репозитории и разрешены allowlist-ом. Если проверка падает — пайплайн останавливается.

## Патчи (предложения изменений кода)
Роли `frontend/backend/qa` могут возвращать patch proposals в виде unified diff.

- `--patches propose` (по умолчанию): патчи сохраняются как артефакты в `artifacts/patches/*.diff`.
- `--patches apply`: патчи сохраняются и затем применяются через `git apply` (с подтверждением в `--approval interactive`).
- `--patches off`: игнорировать патчи.

Примечание: режим `apply` требует, чтобы папка `--repo` была git-репозиторием (наличие `.git`).

Дополнительно: патчи блокируются, если они затрагивают чувствительные пути (например `.env*`, ключи `*.pem/*.key`, `node_modules/`, `.git/`) или выходят за allowlist (`frontend/`, `backend/` и небольшой набор файлов в корне).

## QA: Playwright валидация
Если в репозитории настроен Playwright, QA шаг можно дополнить автоматической проверкой:

- `--qa-playwright run` — запустит `npx playwright test` и сохранит вывод в `artifacts/playwright_last_run.log`
- `--qa-playwright ui` — запустит `npx playwright test --ui` в фоне и запишет лог в `artifacts/playwright_ui.log`

Пример (две консоли):

```bash
# (опционально) веб-наблюдение
python -m local_agents serve --repo .

# запуск агентов + Playwright UI после QA
python -m local_agents run --goal "Проверь логин" --repo . --qa-playwright ui
```

## Аналитик (ИБ + требования)
Роль `security_requirements` всегда запускается первой и формирует базовые артефакты требований:
- `artifacts/requirements.md`
- `artifacts/nfr_security.md`
- `artifacts/threat_model.md`

## Дизайнер (Tailwind)
Роль `designer` готовит более детальные промпты/спеки для `frontend` в стиле utility-first (Tailwind), чтобы фронтендер мог быстрее и точнее собрать UI.

Ожидаемые артефакты:
- `artifacts/design_spec.md` — экраны, состояния, a11y, UX детали
- `artifacts/tailwind_components.md` — заготовки компонентов/классов (Button/Input/Card и т.п.)

Если включён `--web on`, дизайнер (как и оркестратор) может опираться на выжимку из `artifacts/web_research.md`.

После выполнения задач остальных ролей аналитик автоматически отвечает на их вопросы и создаёт/обновляет артефакты (например `artifacts/answers_from_security.md`).

## Что дальше
Когда MVP заработает, можно подключать MCP-сервера как отдельные инструменты, но здесь всё сделано максимально лёгким и локальным.

## Почему такие модели
Для стека TypeScript (React/Tailwind + NestJS + MongoDB) на небольших размерах обычно лучше всего держатся:
- `qwen2.5-coder:*` для генерации/правок кода (меньше синтаксических ошибок в TS/JS, лучше патчи)
- `qwen2.5:*` (instruct) для оркестрации/требований/синтеза

Если хочешь одним флагом перебить всё: `--model qwen2.5-coder:3b`.

## Ollama vs MLX (производительность на Apple Silicon)
Короткая записка с выводами и ссылками: [docs/ollama-vs-mlx.md](docs/ollama-vs-mlx.md)

## Типовые команды
- Быстро и дёшево: `--model qwen2.5-coder:3b`
- Лучше качество на TS: (дефолт из config) `qwen2.5:7b` + `qwen2.5-coder:7b`
- Ничего не менять в коде, только артефакты: `--patches propose`
- Автоприменение патчей (осторожно): `--patches apply --approval interactive`

## Contribution
- Правила вклада: [CONTRIBUTING.md](CONTRIBUTING.md)
- Список контрибьюторов: [CONTRIBUTORS.md](CONTRIBUTORS.md)
- Владельцы кода: [.github/CODEOWNERS](.github/CODEOWNERS)

Шаблоны GitHub:
- PR template: [.github/pull_request_template.md](.github/pull_request_template.md)
- Issue templates: [.github/ISSUE_TEMPLATE/bug_report.md](.github/ISSUE_TEMPLATE/bug_report.md), [.github/ISSUE_TEMPLATE/feature_request.md](.github/ISSUE_TEMPLATE/feature_request.md)
- Branch protection: [docs/github-branch-protection.md](docs/github-branch-protection.md)
