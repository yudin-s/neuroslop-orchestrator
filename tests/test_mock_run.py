from __future__ import annotations

from pathlib import Path

from local_agents.orchestrator import Orchestrator, RunConfig


def test_mock_run_creates_artifacts(tmp_path: Path) -> None:
    # Create a minimal fake repo root
    repo = tmp_path / "repo"
    repo.mkdir()

    # Copy config.yaml from package into the temp repo
    # (we only need it to exist; mock LLM doesn't call Ollama)
    config_src = Path(__file__).resolve().parents[1] / "local_agents" / "config.yaml"
    config_dst = repo / "config.yaml"
    config_dst.write_text(config_src.read_text(encoding="utf-8"), encoding="utf-8")

    orch = Orchestrator(
        RunConfig(
            repo_root=repo,
            config_path=config_dst,
            model_override=None,
            approval="auto",
            patches="propose",
            llm="mock",
            ui="off",
            qa_playwright="off",
        )
    )

    synth = orch.run("mock goal")

    # Must create the analyst preflight artifacts
    assert (repo / "artifacts" / "requirements.md").exists()
    assert (repo / "artifacts" / "nfr_security.md").exists()
    assert (repo / "artifacts" / "threat_model.md").exists()

    # Must create designer artifacts
    assert (repo / "artifacts" / "design_spec.md").exists()
    assert (repo / "artifacts" / "tailwind_components.md").exists()

    # Must create event log
    assert (repo / "artifacts" / "events.jsonl").exists()

    # Should propose patches as diff artifacts
    patches_dir = repo / "artifacts" / "patches"
    assert patches_dir.exists()
    assert any(p.suffix == ".diff" for p in patches_dir.iterdir())

    # Synthesis should include created artifacts list (at least something)
    assert isinstance(synth.created_artifacts, list)
