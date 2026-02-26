from __future__ import annotations

import argparse
from pathlib import Path

from .orchestrator import Orchestrator, RunConfig


def main() -> None:
    p = argparse.ArgumentParser(prog="local_agents")
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="Run orchestrator + role agents")
    run.add_argument("--goal", required=True)
    run.add_argument("--repo", default=".")
    run.add_argument(
        "--outdir",
        default=".",
        help="Where to write the generated solution inside the repo (e.g. goal)",
    )
    run.add_argument("--config", default=str(Path(__file__).with_name("config.yaml")))
    run.add_argument("--model", default=None, help="Override model for all roles")
    run.add_argument(
        "--approval",
        default="interactive",
        choices=["interactive", "auto"],
        help="Tool gate mode",
    )
    run.add_argument(
        "--patches",
        default="propose",
        choices=["off", "propose", "apply", "write"],
        help=(
            "What to do with code changes: "
            "off|propose (save diffs to artifacts)|apply (git apply with approval)"
            "|write (ask agents for full file contents and write directly)"
        ),
    )
    run.add_argument(
        "--llm",
        default="ollama",
        choices=["ollama", "mock"],
        help="LLM backend: ollama (real local model) or mock (deterministic, for tests)",
    )
    run.add_argument(
        "--ui",
        default="plain",
        choices=["off", "plain", "live"],
        help="Run UI: off|plain|live (terminal dashboard)",
    )
    run.add_argument(
        "--qa-playwright",
        default="off",
        choices=["off", "run", "ui"],
        help="After QA step: run Playwright tests (run) or launch Playwright UI (ui)",
    )

    run.add_argument(
        "--web",
        default="off",
        choices=["off", "on"],
        help="Allow orchestrator to do limited web research (search+fetch) into artifacts/",
    )

    serve = sub.add_parser("serve", help="Serve web UI (SSE) for artifacts/events.jsonl")
    serve.add_argument("--repo", default=".")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)

    args = p.parse_args()

    if args.cmd == "run":
        orch = Orchestrator(
            RunConfig(
                repo_root=Path(args.repo).resolve(),
                config_path=Path(args.config).resolve(),
                model_override=args.model,
                approval=args.approval,
                patches=args.patches,
                llm=args.llm,
                ui=args.ui,
                qa_playwright=args.qa_playwright,
                web=args.web,
                outdir=str(args.outdir or "."),
            )
        )
        synth = orch.run(args.goal)
        print(synth.model_dump_json(indent=2, ensure_ascii=False))

    if args.cmd == "serve":
        from .web import serve as serve_web

        serve_web(repo_root=Path(args.repo).resolve(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
