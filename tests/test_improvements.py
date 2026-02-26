from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from local_agents.orchestrator import Orchestrator, RunConfig
from local_agents.schemas import OrchestratorPlan, OrchestratorTask
from local_agents.tools import ToolError, ToolPolicy, Tools


def _write_config(dst: Path) -> None:
    src = Path(__file__).resolve().parents[1] / "local_agents" / "config.yaml"
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def test_plan_normalization_enforces_pipeline(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo / "config.yaml")

    orch = Orchestrator(
        RunConfig(
            repo_root=repo,
            config_path=repo / "config.yaml",
            model_override=None,
            approval="auto",
            patches="propose",
            llm="mock",
            ui="off",
            qa_playwright="off",
        )
    )

    plan = OrchestratorPlan(
        goal="x",
        tasks=[
            OrchestratorTask(role="frontend", title="F", description="d"),
            OrchestratorTask(role="backend", title="B", description="d"),
            OrchestratorTask(role="qa", title="Q", description="d"),
            OrchestratorTask(role="security_requirements", title="S", description="d"),
            OrchestratorTask(role="backend", title="", description=""),  # empty -> drop
        ],
    )

    norm = orch._normalize_plan(plan)
    roles = [t.role for t in norm.tasks]
    assert roles == ["backend", "designer", "frontend", "qa", "security_requirements"]


def test_tools_read_policy_blocks_env(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("SECRET=1", encoding="utf-8")

    tools = Tools(
        repo_root=repo,
        policy=ToolPolicy(
            approval="auto",
            shell_allowlist=[],
            max_read_bytes=1000,
            read_denylist=[".env", ".env.*"],
            read_allowlist=[],
        ),
    )

    with pytest.raises(ToolError):
        tools.read_file(".env")


def test_patch_apply_blocks_sensitive_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo / "config.yaml")

    subprocess.run(["git", "init"], cwd=str(repo), check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    orch = Orchestrator(
        RunConfig(
            repo_root=repo,
            config_path=repo / "config.yaml",
            model_override=None,
            approval="auto",
            patches="apply",
            llm="mock",
            ui="off",
            qa_playwright="off",
        )
    )

    bad_patch = (
        "diff --git a/.env b/.env\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        "+++ b/.env\n"
        "@@\n"
        "+SECRET=1\n"
    )

    with pytest.raises(RuntimeError):
        orch._apply_unified_diff(bad_patch, title="bad", role="backend")
