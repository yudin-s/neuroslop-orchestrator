from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class RunState:
    phase: str = "init"
    active_role: str = ""
    active_task: str = ""
    artifacts_written: int = 0
    patches_seen: int = 0
    files_written: int = 0
    last_type: str = ""


class LiveUI:
    def __init__(self) -> None:
        self.state = RunState()
        self._live = None

    def start(self) -> None:
        from rich.console import Console
        from rich.live import Live

        self._console = Console()
        self._live = Live(self._render(), console=self._console, refresh_per_second=6)
        self._live.__enter__()

    def stop(self) -> None:
        if self._live is not None:
            self._live.__exit__(None, None, None)
            self._live = None

    def on_event(self, event: dict[str, Any]) -> None:
        self.state.last_type = str(event.get("type") or "")
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}

        t = self.state.last_type
        if t == "phase":
            self.state.phase = str(payload.get("phase") or self.state.phase)
        elif t == "task_start":
            self.state.active_role = str(payload.get("role") or "")
            self.state.active_task = str(payload.get("title") or "")
        elif t == "artifact_written":
            self.state.artifacts_written += 1
        elif t == "patch_seen":
            self.state.patches_seen += 1
        elif t == "file_written":
            self.state.files_written += 1

        if self._live is not None:
            self._live.update(self._render())

    def emit(self, event: dict[str, Any]) -> None:
        self.on_event(event)

    def _render(self):
        from rich.panel import Panel
        from rich.table import Table

        table = Table(show_header=False, box=None)
        table.add_row("Phase", self.state.phase)
        table.add_row("Role", self.state.active_role)
        table.add_row("Task", self.state.active_task)
        table.add_row("Artifacts", str(self.state.artifacts_written))
        table.add_row("Patches", str(self.state.patches_seen))
        table.add_row("Files", str(self.state.files_written))
        table.add_row("Last event", self.state.last_type)
        return Panel(table, title="local_agents", border_style="cyan")
