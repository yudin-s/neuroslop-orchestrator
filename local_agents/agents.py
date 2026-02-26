from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import ValidationError

from .llm import OllamaClient
from .json_repair import parse_json_or_repair
from .prompts import AGENT_TASK_USER, AGENT_TASK_USER_WRITE, ROLE_SYSTEM
from .schemas import AgentResult, ToolRequest


@dataclass
class AgentTask:
    role: str
    title: str
    description: str
    inputs: list[str]
    outputs: list[str]
    context: str = ""
    project_dir: str = "."
    repo_outputs: list[str] = field(default_factory=list)


@dataclass
class AgentOutput:
    result: AgentResult
    artifact_contents: dict[str, str]
    file_contents: dict[str, str]
    patches: list[dict[str, str]]
    tool_requests: list[ToolRequest]
    raw: str


class RoleAgent:
    def __init__(self, *, role: str, llm: OllamaClient, emit_event: Callable[[str, dict[str, Any]], None] | None = None):
        self.role = role
        self.llm = llm
        self.emit_event = emit_event

    def run(self, task: AgentTask, *, change_mode: str = "patch", feedback: str | None = None) -> AgentOutput:
        system = ROLE_SYSTEM[self.role]
        tmpl = AGENT_TASK_USER_WRITE if change_mode == "write" else AGENT_TASK_USER
        user = tmpl.format(
            role=self.role,
            title=task.title,
            description=task.description,
            inputs="\n".join(f"- {i}" for i in task.inputs) or "- (нет)",
            input_contents=(task.context.strip() or "(нет)"),
            project_dir=(task.project_dir or "."),
            repo_outputs=("\n".join(f"- {p}" for p in (task.repo_outputs or [])) or "- (не задано в плане)"),
        )

        if feedback:
            user += f"\n\nВАЖНО: Предыдущая попытка выполнения твоих инструкций завершилась ошибкой:\n{feedback}\n" \
                    f"Пожалуйста, проанализируй ошибку и предложи исправленный вариант действий (другие команды, исправление аргументов и т.д.).\n" \
                    f"Если команда ожидает интерактивный ввод (например '(y/N)', 'Ok to proceed?'), " \
                    f"ты ДОЛЖЕН либо сделать её неинтерактивной флагами (-y/--yes/--no-interactive), " \
                    f"либо (предпочтительно) передать ввод явно в tool_requests: args.stdin='y\\n' (или другой нужный ввод)."

        def _model_name() -> str:
            try:
                if hasattr(self.llm, "config") and getattr(self.llm, "config") is not None:
                    return str(getattr(getattr(self.llm, "config"), "model", "")) or self.llm.__class__.__name__
                return str(getattr(self.llm, "model", "")) or self.llm.__class__.__name__
            except Exception:
                return self.llm.__class__.__name__

        model = _model_name()
        base_temp = float(getattr(getattr(self.llm, "config", None), "temperature", 0.2) or 0.2)

        prompt = user
        raw = ""
        for attempt in range(1, 4):
            t0 = time.monotonic()
            if self.emit_event is not None:
                self.emit_event(
                    "llm_call_start",
                    {
                        "role": self.role,
                        "task_title": task.title,
                        "task_description": task.description[:400],
                        "attempt": attempt,
                        "model": model,
                    },
                )

            raw = self.llm.chat(
                system=system,
                user=prompt,
                json_mode=True,
                temperature=max(0.0, base_temp - (attempt - 1) * 0.1),
            )

            if self.emit_event is not None:
                self.emit_event(
                    "llm_call_end",
                    {
                        "role": self.role,
                        "task_title": task.title,
                        "raw_chars": len(raw or ""),
                        "attempt": attempt,
                        "model": model,
                        "elapsed_ms": int((time.monotonic() - t0) * 1000),
                    },
                )

            try:
                data_try = json.loads(raw)
                if isinstance(data_try, dict):
                    data = data_try
                    break
            except json.JSONDecodeError:
                data = None

            if attempt < 3:
                if self.emit_event is not None:
                    self.emit_event(
                        "llm_json_retry",
                        {"role": self.role, "task_title": task.title, "attempt": attempt, "model": model},
                    )
                prompt = (
                    prompt
                    + "\n\nВАЖНО: твой предыдущий ответ был НЕвалидным JSON. Верни строго валидный JSON-объект без пояснений."
                )
                continue

            data = None

        if data is None:
            data = parse_json_or_repair(raw=raw, llm=self.llm, label=f"role={self.role}/task={task.title}")

        # 'data' уже заполнен выше (с ретраями/repair)

        artifact_contents = data.pop("artifact_contents", {})
        file_contents = data.pop("file_contents", {})
        tool_requests_raw = data.pop("tool_requests", [])
        patches = data.get("patches", [])
        try:
            result = AgentResult.model_validate(data)
        except ValidationError as e:
            raise RuntimeError(f"Invalid AgentResult JSON for role={self.role}: {e}\n{raw}")

        if not isinstance(artifact_contents, dict):
            artifact_contents = {}
        if not isinstance(file_contents, dict):
            file_contents = {}

        tool_requests: list[ToolRequest] = []
        if isinstance(tool_requests_raw, list):
            for tr in tool_requests_raw:
                if not isinstance(tr, dict):
                    continue
                try:
                    tool_requests.append(ToolRequest.model_validate(tr))
                except ValidationError:
                    continue

        # Убеждаемся, что агент не пытается писать вне artifacts/
        safe_artifacts: dict[str, str] = {}
        for path, content in artifact_contents.items():
            if isinstance(path, str) and path.startswith("artifacts/") and isinstance(content, str):
                safe_artifacts[path] = content

        safe_patches: list[dict[str, str]] = []
        if isinstance(patches, list):
            for p in patches:
                if not isinstance(p, dict):
                    continue
                title = p.get("title")
                diff = p.get("diff")
                if isinstance(title, str) and isinstance(diff, str) and diff.strip():
                    safe_patches.append({"title": title.strip(), "diff": diff})

        # Файлы репозитория (только для write-mode): фильтрация путей делает оркестратор.
        safe_files: dict[str, str] = {}
        if change_mode == "write":
            for path, content in file_contents.items():
                if isinstance(path, str) and isinstance(content, str):
                    safe_files[path] = content

        return AgentOutput(
            result=result,
            artifact_contents=safe_artifacts,
            file_contents=safe_files,
            patches=safe_patches,
            tool_requests=tool_requests,
            raw=raw,
        )
