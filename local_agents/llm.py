from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Any, Literal, Protocol

import requests


Role = Literal[
    "orchestrator",
    "frontend",
    "backend",
    "designer",
    "security_requirements",
    "qa",
]


class LLMClient(Protocol):
    def chat(
        self,
        *,
        system: str,
        user: str,
        json_mode: bool = True,
        timeout_s: int = 300,
        temperature: float | None = None,
        num_predict: int | None = None,
    ) -> str: ...


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    model: str
    temperature: float = 0.2
    # Ограничиваем длину генерации, чтобы модели-агенты не «убегали» в бесконечные ответы.
    # Значение в токенах. None = без лимита.
    num_predict: int | None = 2048


class OllamaClient:
    def __init__(self, config: LLMConfig):
        self.config = config

    def chat(
        self,
        *,
        system: str,
        user: str,
        json_mode: bool = True,
        timeout_s: int = 900,
        temperature: float | None = None,
        num_predict: int | None = None,
    ) -> str:
        url = f"{self.config.base_url.rstrip('/')}/api/chat"

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        temp = float(temperature) if temperature is not None else float(self.config.temperature)
        options: dict[str, Any] = {"temperature": temp}
        effective_num_predict = num_predict if num_predict is not None else self.config.num_predict
        if effective_num_predict is not None:
            options["num_predict"] = int(effective_num_predict)

        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "stream": False,
            "options": options,
        }

        if json_mode:
            payload["format"] = "json"

        # Ретраи на сетевые/временные/серверные ошибки.
        # ВАЖНО: timeout_s трактуем как общий дедлайн на все попытки.
        max_attempts = 3
        backoff_s = 0.6
        last_exc: Exception | None = None
        deadline = time.monotonic() + max(1, int(timeout_s))

        for attempt in range(1, max_attempts + 1):
            remaining_s = max(0.0, deadline - time.monotonic())
            if remaining_s <= 0:
                raise requests.Timeout(f"Ollama request exceeded timeout_s={timeout_s}")

            try:
                connect_timeout = min(10.0, max(1.0, remaining_s))
                resp = requests.post(url, json=payload, timeout=(connect_timeout, remaining_s))
                if resp.status_code in {408, 409, 425, 429, 500, 502, 503, 504}:
                    raise requests.HTTPError(f"HTTP {resp.status_code}: {resp.text[:500]}")
                resp.raise_for_status()
                data = resp.json()
                break
            except (requests.RequestException, ValueError) as e:
                last_exc = e
                if attempt >= max_attempts:
                    raise
                remaining_s = max(0.0, deadline - time.monotonic())
                if remaining_s <= 0:
                    raise
                time.sleep(min(backoff_s, remaining_s))
                backoff_s = min(backoff_s * 1.8, 6.0)
        else:
            raise RuntimeError(f"Ollama request failed: {last_exc}")

        return (data.get("message") or {}).get("content") or ""


class MockClient:
    """Детерминированный локальный LLM для тестов/демо без Ollama."""

    def __init__(self, model: str = "mock"):
        self.model = model

    def chat(
        self,
        *,
        system: str,
        user: str,
        json_mode: bool = True,
        timeout_s: int = 300,
        temperature: float | None = None,
        num_predict: int | None = None,
    ) -> str:
        if "Orchestrator" in system and "Сформируй JSON-план" in user:
            plan = {
                "goal": "mock",
                "tasks": [
                    {
                        "role": "backend",
                        "title": "API contract",
                        "description": "Define backend API contract",
                        "inputs": ["artifacts/requirements.md", "artifacts/nfr_security.md"],
                        "outputs": ["artifacts/api_contract.md"],
                    },
                    {
                        "role": "designer",
                        "title": "Tailwind UI design spec",
                        "description": "Prepare Tailwind utility-first spec for frontend",
                        "inputs": ["artifacts/requirements.md", "artifacts/api_contract.md"],
                        "outputs": ["artifacts/design_spec.md", "artifacts/tailwind_components.md"],
                    },
                    {
                        "role": "frontend",
                        "title": "UI plan",
                        "description": "Define UI screens and API integration",
                        "inputs": ["artifacts/api_contract.md", "artifacts/requirements.md"],
                        "outputs": ["artifacts/ui_plan.md"],
                    },
                    {
                        "role": "qa",
                        "title": "Test plan",
                        "description": "Define test matrix",
                        "inputs": ["artifacts/requirements.md"],
                        "outputs": ["artifacts/test_plan.md"],
                    },
                ],
            }
            return json.dumps(plan, ensure_ascii=False)

        if "ИБ + требования" in system and "первичное изучение требований" in user:
            preflight = {
                "role": "security_requirements",
                "summary": "mock preflight",
                "artifacts": [
                    "artifacts/requirements.md",
                    "artifacts/nfr_security.md",
                    "artifacts/threat_model.md",
                ],
                "patches": [],
                "risks": ["mock risk"],
                "open_questions": ["Какие провайдеры аутентификации поддерживаем?"],
                "artifact_contents": {
                    "artifacts/requirements.md": "# Requirements (mock)\n\n- User can sign up/sign in\n",
                    "artifacts/nfr_security.md": "# NFR+Security (mock)\n\n- JWT, rate limits\n",
                    "artifacts/threat_model.md": "# Threat model (mock)\n\n- STRIDE notes\n",
                },
            }
            return json.dumps(preflight, ensure_ascii=False)

        if "Вопросы от других ролей" in user:
            answers = {
                "role": "security_requirements",
                "summary": "mock answers",
                "artifacts": ["artifacts/answers_from_security.md"],
                "patches": [],
                "risks": [],
                "open_questions": [],
                "artifact_contents": {
                    "artifacts/answers_from_security.md": "# Answers (mock)\n\n- Use JWT + refresh tokens\n"
                },
            }
            return json.dumps(answers, ensure_ascii=False)

        if "агент BACKEND" in system:
            backend = {
                "role": "backend",
                "summary": "mock backend",
                "artifacts": ["artifacts/api_contract.md"],
                "patches": [
                    {
                        "title": "Add auth module",
                        "diff": (
                            "diff --git a/backend/README.md b/backend/README.md\n"
                            "new file mode 100644\n"
                            "index 0000000..1111111\n"
                            "--- /dev/null\n"
                            "+++ b/backend/README.md\n"
                            "@@\n"
                            "+mock\n"
                        ),
                    }
                ],
                "risks": [],
                "open_questions": [],
                "artifact_contents": {"artifacts/api_contract.md": "# API (mock)\n\n- POST /auth/login\n"},
            }
            return json.dumps(backend, ensure_ascii=False)

        if "агент DESIGNER" in system:
            designer = {
                "role": "designer",
                "summary": "mock design spec",
                "artifacts": ["artifacts/design_spec.md", "artifacts/tailwind_components.md"],
                "patches": [],
                "risks": [],
                "open_questions": [],
                "artifact_contents": {
                    "artifacts/design_spec.md": (
                        """# Design system spec (mock)

## Foundations
- Color roles: surface/text/border/primary/danger
- Typography: base text-sm, headings font-semibold
- Spacing: Tailwind spacing scale (4px step)
- Focus: focus-visible ring

## Screens
- Login
""".strip()
                    ),
                    "artifacts/tailwind_components.md": (
                        """# Tailwind components (mock)

## Button
<button class=\"rounded-md px-4 py-2 text-sm font-medium bg-slate-900 text-white\">Action</button>
""".strip()
                    ),
                },
            }
            return json.dumps(designer, ensure_ascii=False)

        if "агент FRONTEND" in system:
            frontend = {
                "role": "frontend",
                "summary": "mock frontend",
                "artifacts": ["artifacts/ui_plan.md"],
                "patches": [],
                "risks": [],
                "open_questions": ["Нужна ли 2FA?"],
                "artifact_contents": {"artifacts/ui_plan.md": "# UI (mock)\n\n- Login page\n"},
            }
            return json.dumps(frontend, ensure_ascii=False)

        if "агент QA" in system:
            qa = {
                "role": "qa",
                "summary": "mock qa",
                "artifacts": ["artifacts/test_plan.md"],
                "patches": [],
                "risks": [],
                "open_questions": [],
                "artifact_contents": {"artifacts/test_plan.md": "# Test plan (mock)\n\n- Unit\n- Integration\n"},
            }
            return json.dumps(qa, ensure_ascii=False)

        if "Синтезируй итоговый JSON" in user:
            synth = {"summary": "mock synthesis", "next_steps": ["Implement"], "created_artifacts": []}
            return json.dumps(synth, ensure_ascii=False)

        return "{}"
