from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class ApprovalMode(str, Enum):
    interactive = "interactive"
    auto = "auto"


class ToolName(str, Enum):
    read_file = "read_file"
    write_file = "write_file"
    grep = "grep"
    list_dir = "list_dir"
    shell = "shell"


class ToolRequest(BaseModel):
    tool: ToolName
    args: dict[str, Any] = Field(default_factory=dict)


class AgentResult(BaseModel):
    role: Literal["frontend", "backend", "designer", "security_requirements", "qa"]
    summary: str
    artifacts: list[str] = Field(default_factory=list)
    patches: list[dict[str, str]] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class OrchestratorTask(BaseModel):
    role: Literal[
        "orchestrator",
        "frontend",
        "backend",
        "designer",
        "security_requirements",
        "qa",
    ]
    title: str = ""
    description: str = ""
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    # Repo file paths that this task is expected to create/modify (for write-mode).
    # This allows the plan to define arbitrary project structures (NestJS/React/etc)
    # without hardcoding paths inside the orchestrator.
    repo_outputs: list[str] = Field(default_factory=list)


class OrchestratorPlan(BaseModel):
    goal: str
    tasks: list[OrchestratorTask] = Field(default_factory=list)


class OrchestratorSynthesis(BaseModel):
    summary: str
    next_steps: list[str] = Field(default_factory=list)
    created_artifacts: list[str] = Field(default_factory=list)
