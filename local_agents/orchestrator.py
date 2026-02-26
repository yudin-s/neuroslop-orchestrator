"""Core orchestration pipeline for local multi-agent execution.

Главная идея модуля:
- оркестратор управляет строгим фазовым конвейером (requirements → security → planning → tasks → synthesis);
- роли работают через единый событийный bus и tool-gates;
- все промежуточные результаты сохраняются в `artifacts/` для трассировки и воспроизводимости.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .agents import AgentTask, RoleAgent
from .events import EventBus, JsonlFileSink
from .llm import LLMConfig, MockClient, OllamaClient
from .json_repair import parse_json_or_repair
from .prompts import (
    ORCHESTRATOR_PLAN_USER,
    ORCHESTRATOR_REQUIREMENTS_USER,
    ORCHESTRATOR_SYNTHESIS_USER,
    ROLE_SYSTEM,
    SECURITY_PREFLIGHT_USER,
    SECURITY_QA_USER,
)
from .schemas import OrchestratorPlan, OrchestratorSynthesis, ToolRequest, ToolName
from .repo_profile import build_repo_profile
from .tools import ToolPolicy, Tools
from .ui import LiveUI


@dataclass(frozen=True)
class RunConfig:
    repo_root: Path
    config_path: Path
    model_override: str | None
    approval: str  # interactive|auto
    patches: str  # off|propose|apply|write
    llm: str  # ollama|mock
    ui: str  # off|plain|live
    qa_playwright: str  # off|run|ui
    web: str = "off"  # off|on
    outdir: str = "."  # where to write generated app (e.g. goal)


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    return data


def _repo_hint(tools: Tools) -> str:
    try:
        top = tools.list_dir(".")
    except Exception:
        top = []
    # Сильно не раздуваем контекст маленьким моделям
    top_short = ", ".join(top[:30])
    return f"Top-level: {top_short}"


def _llm_model_name(llm: Any) -> str:
    try:
        if hasattr(llm, "config") and getattr(llm, "config") is not None:
            return str(getattr(getattr(llm, "config"), "model", "")) or llm.__class__.__name__
        return str(getattr(llm, "model", "")) or llm.__class__.__name__
    except Exception:
        return llm.__class__.__name__


class _PlainStdoutSink:
    def emit(self, event: dict[str, Any]) -> None:
        try:
            t = (event or {}).get("type")
            p = (event or {}).get("payload") or {}
            if t == "llm_call_start":
                role = p.get("role")
                title = p.get("task_title")
                attempt = p.get("attempt")
                model = p.get("model")
                print(f"[llm_start] role={role} attempt={attempt} model={model} task={title!r}", flush=True)
            elif t == "llm_call_end":
                role = p.get("role")
                title = p.get("task_title")
                attempt = p.get("attempt")
                ms = p.get("elapsed_ms")
                chars = p.get("raw_chars")
                print(
                    f"[llm_end] role={role} attempt={attempt} elapsed_ms={ms} raw_chars={chars} task={title!r}",
                    flush=True,
                )
        except Exception:
            # Никогда не ломаем пайплайн из-за логов.
            return


class Orchestrator:
    def __init__(self, run_cfg: RunConfig):
        self.run_cfg = run_cfg
        cfg = _load_yaml(run_cfg.config_path)
        base_url = os.environ.get("OLLAMA_BASE_URL") or (cfg.get("ollama") or {}).get(
            "base_url", "http://localhost:11434"
        )
        models = cfg.get("models") or {}

        self.repo_root = run_cfg.repo_root

        outdir = (run_cfg.outdir or ".").strip().strip("/")
        if outdir in {"", "."}:
            self.project_root = "."
        else:
            self.project_root = outdir

        self.events = EventBus(
            sinks=[JsonlFileSink(self.repo_root / "artifacts" / "events.jsonl")]
        )
        self.ui = LiveUI() if run_cfg.ui == "live" else None
        if self.ui is not None:
            self.ui.start()
            # подключаем как sink
            self.events.add_sink(self.ui)
        if run_cfg.ui == "plain":
            self.events.add_sink(_PlainStdoutSink())

        self.tools = Tools(
            repo_root=run_cfg.repo_root,
            policy=ToolPolicy(
                approval=run_cfg.approval,
                shell_allowlist=((cfg.get("tools") or {}).get("shell_allowlist") or []),
                shell_allowlist_patterns=((cfg.get("tools") or {}).get("shell_allowlist_patterns") or []),
                max_read_bytes=int(((cfg.get("tools") or {}).get("max_read_bytes") or 400000)),
                read_denylist=((cfg.get("tools") or {}).get("read_denylist") or [
                    ".git/**",
                    ".venv/**",
                    "**/.venv/**",
                    "node_modules/**",
                    "**/node_modules/**",
                    ".env",
                    ".env.*",
                    "**/*.pem",
                    "**/*.key",
                    "**/id_rsa*",
                ]),
                read_allowlist=((cfg.get("tools") or {}).get("read_allowlist") or []),
                web_enabled=(run_cfg.web == "on") and bool((cfg.get("web") or {}).get("enabled", True)),
                web_allow_domains=((cfg.get("web") or {}).get("allow_domains") or []),
                web_timeout_s=int((cfg.get("web") or {}).get("timeout_s") or 20),
                max_web_bytes=int((cfg.get("web") or {}).get("max_bytes") or 300000),
            ),
        )

        self.repo_profile = build_repo_profile(self.repo_root)

        orchestrator_model = run_cfg.model_override or models.get("orchestrator") or "qwen2.5:3b"
        if run_cfg.llm == "mock":
            self.orchestrator_llm = MockClient(model=orchestrator_model)
        else:
            self.orchestrator_llm = OllamaClient(LLMConfig(base_url=base_url, model=orchestrator_model))

        def mk(role: str) -> RoleAgent:
            model = run_cfg.model_override or models.get(role) or orchestrator_model
            if run_cfg.llm == "mock":
                llm = MockClient(model=model)
            else:
                llm = OllamaClient(LLMConfig(base_url=base_url, model=model))
            return RoleAgent(role=role, llm=llm, emit_event=lambda t, p: self.events.emit(type=t, payload=p))

        self.agents = {
            "frontend": mk("frontend"),
            "backend": mk("backend"),
            "designer": mk("designer"),
            "security_requirements": mk("security_requirements"),
            "qa": mk("qa"),
        }

        # Заполняется на время run(); используется для более точных fallback-подсказок.
        self._active_goal: str = ""

        # Rolling context window to keep agents focused across tasks.
        self._rolling_entries: list[str] = []

    def _reset_artifacts_dir(self) -> None:
        """Гарантирует, что запуск пишет в новый/пустой artifacts/.

        Логика:
        - если artifacts/ не существует — создаём;
        - если существует и НЕ пуста — переносим её целиком в artifacts_<YYYYMMDD_HHMMSS> (с суффиксом при коллизии)
          и создаём новую пустую artifacts/.

        Это сохраняет историю прогонов и гарантирует отсутствие «лишнего» контекста для агентов.
        """
        artifacts_dir = self.repo_root / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        try:
            is_empty = not any(artifacts_dir.iterdir())
        except Exception:
            # Если не можем прочитать — будем действовать осторожно: попробуем создать новый каталог всё равно.
            is_empty = False

        if not is_empty:
            # Имя архива: <название>_<дата_время>
            # Название берём из outdir (project_root). Если outdir='.' — используем 'run'.
            raw_name = (self.project_root or ".").strip().strip("/")
            if raw_name in {"", "."}:
                raw_name = "run"
            safe_name = "".join(c if (c.isalnum() or c in {"-", "_"}) else "_" for c in raw_name)
            safe_name = "_".join([p for p in safe_name.split("_") if p])
            safe_name = safe_name[:60] or "run"

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            base = self.repo_root / f"{safe_name}_{ts}"
            archived = base
            # коллизии: добавим _2, _3...
            n = 1
            while archived.exists():
                n += 1
                archived = self.repo_root / f"{safe_name}_{ts}_{n}"

            try:
                shutil.move(str(artifacts_dir), str(archived))
            except Exception:
                # Fallback для bind-mount (Docker): нельзя переименовать сам mountpoint.
                # Поэтому переносим СОДЕРЖИМОЕ artifacts/ в архивную папку.
                try:
                    archived.mkdir(parents=True, exist_ok=True)
                    for child in list(artifacts_dir.iterdir()):
                        try:
                            dest = archived / child.name
                            if dest.exists():
                                # коллизия имён — добавим суффикс
                                k = 2
                                while (archived / f"{child.name}_{k}").exists():
                                    k += 1
                                dest = archived / f"{child.name}_{k}"
                            shutil.move(str(child), str(dest))
                        except Exception:
                            # Последний шанс: если конкретный файл не переносится — оставляем как есть.
                            continue
                except Exception:
                    # Если даже так не получается — тогда уже очищаем, чтобы не дать агентам «лишнее».
                    for child in list(artifacts_dir.iterdir()):
                        try:
                            if child.is_symlink() or child.is_file():
                                child.unlink(missing_ok=True)
                            elif child.is_dir():
                                shutil.rmtree(child)
                            else:
                                child.unlink(missing_ok=True)
                        except Exception:
                            continue

            artifacts_dir.mkdir(parents=True, exist_ok=True)

    def _derive_base_requirements(self, goal: str) -> list[str]:
        """Формирует базовые (стек-нейтральные) requirements из цели."""
        self.events.emit(type="phase", payload={"phase": "requirements"})

        created: list[str] = []
        if self.run_cfg.llm == "mock":
            requirements_md = (
                "# Requirements\n\n"
                f"## Goal\n- {goal.strip()}\n\n"
                "## User stories\n- Пользователь может создать запись\n- Пользователь может увидеть список записей\n\n"
                "## Acceptance criteria\n- Есть создание и просмотр записей\n- Есть базовая валидация\n\n"
                "## Out of scope\n- Авторизация/платежи/сложные роли\n"
            )
            context_window_md = (
                "## Context window\n"
                "- Фокус: минимальный рабочий MVP\n"
                "- Не добавлять лишние страницы/фичи\n"
                "- Сначала контракт API/модель данных, затем реализация\n"
                f"- Писать решение в outdir: {self.project_root}\n"
            )
            self.tools.write_file("artifacts/requirements.md", requirements_md)
            self.tools.write_file("artifacts/context_window.md", context_window_md)
            created += ["artifacts/requirements.md", "artifacts/context_window.md"]
            for p in created:
                self.events.emit(type="artifact_written", payload={"path": p})
            return created

        system = (
            "Ты — RequirementsSynthesizer. Сформируй базовые requirements по цели, "
            "не привязываясь к конкретному языку/фреймворку. Верни ТОЛЬКО JSON."
        )
        user = ORCHESTRATOR_REQUIREMENTS_USER.format(goal=goal, outdir=self.project_root)

        data = self._chat_json_with_retry(
            llm=self.orchestrator_llm,
            system=system,
            user=user,
            role="orchestrator",
            kind="requirements",
            label="orchestrator/requirements",
            timeout_s=240,
            retries=2,
        )

        requirements_md = data.get("requirements_md")
        context_window_md = data.get("context_window_md")

        if isinstance(requirements_md, str) and requirements_md.strip():
            self.tools.write_file("artifacts/requirements.md", requirements_md)
            created.append("artifacts/requirements.md")
            self.events.emit(type="artifact_written", payload={"path": "artifacts/requirements.md"})

        if isinstance(context_window_md, str) and context_window_md.strip():
            self.tools.write_file("artifacts/context_window.md", context_window_md)
            created.append("artifacts/context_window.md")
            self.events.emit(type="artifact_written", payload={"path": "artifacts/context_window.md"})

        return created

    def _append_rolling_summary(
        self,
        *,
        role: str,
        title: str,
        agent_summary: str,
        written_paths: list[str] | None = None,
        artifacts_written: list[str] | None = None,
    ) -> None:
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")
        wp = ", ".join((written_paths or [])[:10])
        aw = ", ".join((artifacts_written or [])[:10])
        entry = (
            f"### {ts} — {role}: {title}\n"
            + (str(agent_summary).strip()[:1400] + "\n" if str(agent_summary).strip() else "")
            + (f"- repo files: {wp}\n" if wp else "")
            + (f"- artifacts: {aw}\n" if aw else "")
        ).strip() + "\n"

        self._rolling_entries.append(entry)
        self._rolling_entries = self._rolling_entries[-12:]

        body = "# Rolling summary (auto)\n\n" + "\n".join(self._rolling_entries)
        self.tools.write_file("artifacts/rolling_summary.md", body)
        self.events.emit(type="artifact_written", payload={"path": "artifacts/rolling_summary.md"})

    def _role_context_dir(self, role: str) -> str:
        """Папка проекта, из которой брать контекст для роли.

        Требование: backend/frontend должны работать с контекстом из соответствующих папок.
        """
        if role not in {"backend", "frontend"}:
            return self.project_root
        if self.project_root == ".":
            return role
        return f"{self.project_root.rstrip('/')}/{role}"

    def _plain_log(self, msg: str) -> None:
        # В Docker обычно запускают с --ui plain; полезно видеть, что происходит.
        if self.run_cfg.ui != "plain":
            return
        try:
            print(msg, flush=True)
        except Exception:
            pass

    def _detect_project_env(self, cwd_rel: str | None = None) -> dict[str, str]:
        """Оркестратор пытается определить PATH для текущего стека (Python/Node)."""
        env = {}
        
        # 1. Если есть .venv, добавляем в PATH
        venv_bin = (self.repo_root / ".venv" / "bin")
        if venv_bin.exists() and venv_bin.is_dir():
            env["PATH"] = str(venv_bin) + os.pathsep + (os.environ.get("PATH") or "")

        # 2. Если есть node_modules/.bin (в корне репо)
        node_bin_root = (self.repo_root / "node_modules" / ".bin")
        if node_bin_root.exists() and node_bin_root.is_dir():
             # Если уже был PATH от venv — объединяем
            current = env.get("PATH") or os.environ.get("PATH") or ""
            env["PATH"] = str(node_bin_root) + os.pathsep + current

        # 3. Если команда запускается из подпапки (cwd_rel), там тоже может быть node_modules
        if cwd_rel:
            cwd_path = (self.repo_root / cwd_rel).resolve()
            if cwd_path.exists() and cwd_path.is_dir():
                sub_node = cwd_path / "node_modules" / ".bin"
                if sub_node.exists() and sub_node.is_dir():
                    current = env.get("PATH") or os.environ.get("PATH") or ""
                    # Локальный node_modules приоритетнее
                    env["PATH"] = str(sub_node) + os.pathsep + current
        
        return env

    def _execute_tool_requests(self, *, role: str, title: str, reqs: list[ToolRequest]) -> list[str]:
        """Выполняет tool_requests от роли. Результаты складывает в artifacts/.

        Возвращает список путей артефактов, которые были созданы.
        """
        if not reqs:
            return []

        created: list[str] = []
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")

        for i, r in enumerate(reqs, start=1):
            payload: dict[str, Any] = {"role": role, "title": title, "tool": str(r.tool), "i": i}
            if r.tool == ToolName.shell:
                cmd0 = str((r.args or {}).get("command") or "").strip()
                cwd0 = (r.args or {}).get("cwd_rel")
                payload.update(
                    {
                        "command": cmd0[:500],
                        "cwd_rel": (str(cwd0)[:200] if isinstance(cwd0, str) else None),
                    }
                )
            self.events.emit(type="tool_start", payload=payload)
            if r.tool == ToolName.shell:
                self._plain_log(
                    f"[tool_start] role={role} task={title!r} i={i} cwd={payload.get('cwd_rel') or '.'} cmd={cmd0!r}"
                )
            try:
                out_txt = ""
                if r.tool == ToolName.shell:
                    cmd = str((r.args or {}).get("command") or "").strip()
                    timeout_s = int((r.args or {}).get("timeout_s") or 1200)
                    cwd_rel = (r.args or {}).get("cwd_rel")
                    cwd_rel_s = str(cwd_rel) if isinstance(cwd_rel, str) and cwd_rel.strip() else None
                    stdin0 = (r.args or {}).get("stdin")
                    stdin_s = str(stdin0) if isinstance(stdin0, str) and stdin0 else None

                    def _classify_interactive(output: str) -> bool:
                        s = (output or "").lower()
                        # Common interactive prompts across npm/npx/pip/etc.
                        prompt_markers = [
                            "ok to proceed?",
                            "(y/n)",
                            "(y/n):",
                            "(y/n)?",
                            "(y/n): ",
                            "continue?",
                            "enter ",
                            "password",
                            "press any key",
                            "select",
                        ]
                        if any(m in s for m in prompt_markers):
                            return True
                        # Heuristic: lines that end with ':' and look like a question
                        lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
                        if lines:
                            last = lines[-1]
                            if last.endswith(":") and ("?" in last or "enter" in last or "choose" in last):
                                return True
                        return False

                    def _maybe_fix_echo_redirection(bad_cmd: str, err: str) -> str | None:
                        # Частая ошибка в /bin/sh (dash): неэкранированные скобки в echo payload.
                        if "Syntax error" not in (err or "") or "(" not in (err or ""):
                            return None
                        m = re.match(r"^echo\s+(.+?)\s*(>>|>)\s*(\S+)\s*$", bad_cmd)
                        if not m:
                            return None
                        payload = (m.group(1) or "").strip()
                        op = m.group(2)
                        target = m.group(3)
                        if "(" not in payload and ")" not in payload:
                            return None
                        # Снимаем внешние кавычки, если они уже есть.
                        if (payload.startswith("'") and payload.endswith("'")) or (
                            payload.startswith('"') and payload.endswith('"')
                        ):
                            payload = payload[1:-1]
                        payload_q = shlex.quote(payload)
                        target_q = shlex.quote(target)
                        return f"printf '%s\\n' {payload_q} {op} {target_q}"

                    def _maybe_fix_pip_requirements_path(bad_cmd: str, err: str) -> str | None:
                        if "Could not open requirements file" not in (err or ""):
                            return None
                        m = re.search(r"(^|\s)-r\s+([^\s]+)", bad_cmd)
                        if not m:
                            return None
                        req_path = (m.group(2) or "").strip().strip('"').strip("'")
                        if not req_path:
                            return None
                        # Если requirements-файл существует в корне репо, но команда выполнялась из подкаталога,
                        # перепишем путь на абсолютный.
                        abs_req = (self.repo_root / req_path.lstrip("/")).resolve()
                        if abs_req.exists() and abs_req.is_file():
                            return re.sub(
                                r"(^|\s)-r\s+[^\s]+",
                                lambda mm: f"{mm.group(1)}-r {shlex.quote(str(abs_req))}",
                                bad_cmd,
                                count=1,
                            )
                        # Фоллбек: если просили просто requirements.txt — попробуем /repo/requirements.txt.
                        if req_path == "requirements.txt":
                            abs_req2 = (self.repo_root / "requirements.txt").resolve()
                            if abs_req2.exists() and abs_req2.is_file():
                                return re.sub(
                                    r"(^|\s)-r\s+requirements\.txt",
                                    lambda mm: f"{mm.group(1)}-r {shlex.quote(str(abs_req2))}",
                                    bad_cmd,
                                    count=1,
                                )
                        return None

                    # Определяем environment для запуска команд (динамический PATH)
                    run_env = self._detect_project_env(cwd_rel=cwd_rel_s)

                    def _fix_command_with_llm(bad_cmd: str, err: str) -> str | None:
                        # Если это shell tool и включен llm (не mock), просим исправить
                        if self.run_cfg.llm == "mock":
                            return None
                        
                        # Эвристика: если ошибка слишком тривиальная (mkdir missing), мы её уже решили.
                        # Если ошибка про 404 package — просим убрать пакет.
                        
                        system = (
                            "Ты — ShellFixer. Твоя задача: исправить команду, которая упала с ошибкой. "
                            "Верни ТОЛЬКО исправленную команду (одну строку) без маркдауна и пояснений. "
                            "Если ошибка 'package not found' (404) — убери проблемный пакет из команды установки. "
                            "Если ошибка синтаксиса — исправь экранирование. "
                            "Если в ошибке есть вывод help/usage — используй только разрешенные флаги. "
                            "Если флаг не поддерживается — убери его или замени на правильный. "
                            "Если ошибка 'merge conflicted' или 'file exists' при инициализации проекта (nest new, create-react-app и т.п.) — добавь очистку: 'rm -rf ./* && <command>'. "
                            "Если команда безнадежно неверна — верни пустоту."
                        )
                        user = f"COMMAND:\n{bad_cmd}\n\nERROR:\n{err[:2000]}"
                        
                        try:
                            repaired = self.orchestrator_llm.chat(
                                system=system, user=user, json_mode=False, timeout_s=60, temperature=0.1
                            )
                            return repaired.strip() if repaired and repaired.strip() else None
                        except Exception:
                            return None

                    try:
                        out_txt = self.tools.shell(
                            cmd,
                            timeout_s=timeout_s,
                            check=True,
                            cwd_rel=cwd_rel_s,
                            env=run_env,
                            stdin=stdin_s,
                        )
                    except Exception as e:
                        err_s = str(e)
                        # Enrich error feedback for the agent: include stdout tail + interactive hint.
                        tail = err_s[-4000:]
                        interactive_hint = ""
                        if _classify_interactive(err_s) or "timed out" in err_s.lower():
                            interactive_hint = (
                                "\n\n[INTERACTIVE DETECTED] Команда, похоже, ждёт ввода. "
                                "Переиздай tool_request так, чтобы она была неинтерактивной "
                                "(флаги -y/--yes/--no-interactive и т.п.) или передай ввод через args.stdin (например 'y\\n')."
                            )
                        err_s = (
                            f"{err_s}\n\n[CONTEXT] command={cmd!r} cwd_rel={cwd_rel_s!r} stdin={'<provided>' if stdin_s else '<none>'}" 
                            f"\n[OUTPUT TAIL]\n{tail}{interactive_hint}"
                        )

                        retried_ok = False


                        # Частая ошибка: агент указывает cwd_rel на папку, которую ещё не создал.
                        # Если cwd находится внутри outdir/project_root — создаём и ретраим один раз.
                        if (
                            r.tool == ToolName.shell
                            and cwd_rel_s
                            and "cwd not found:" in err_s
                            and self.project_root not in {"", "."}
                        ):
                            rel = (cwd_rel_s or ".").replace("\\", "/").strip("/")
                            pr = (self.project_root or ".").replace("\\", "/").strip("/")
                            if rel == pr or rel.startswith(pr + "/"):
                                try:
                                    mkdir_cmd = f"mkdir -p {shlex.quote(rel)}"
                                    self.events.emit(
                                        type="tool_retry",
                                        payload={
                                            **payload,
                                            "reason": "mkdir_missing_cwd",
                                            "fixed_cmd": mkdir_cmd,
                                        },
                                    )
                                    self._plain_log(
                                        f"[tool_retry] role={role} i={i} reason=mkdir_missing_cwd fixed_cmd={mkdir_cmd!r}"
                                    )
                                    self.tools.shell(mkdir_cmd, timeout_s=60, check=True, cwd_rel=None)
                                    out_txt = self.tools.shell(
                                        cmd,
                                        timeout_s=timeout_s,
                                        check=True,
                                        cwd_rel=cwd_rel_s,
                                        env=run_env,
                                        stdin=stdin_s,
                                    )
                                    retried_ok = True
                                except Exception:
                                    # Если mkdir/повтор не помог — продолжаем обычную обработку ниже.
                                    pass

                        if retried_ok:
                            # Команда успешно выполнена после mkdir -p; больше ничего чинить не нужно.
                            pass
                        else:
                            fixed = _maybe_fix_echo_redirection(cmd, err_s)
                            fixed2 = _maybe_fix_pip_requirements_path(cmd, err_s)
                            # fixed3 = _fix_command_with_llm(cmd, err_s)  # Disabled: Orchestrator should not solve problems itself
                            
                            fixed_cmd = fixed or fixed2 # or fixed3
                            if fixed_cmd:
                                self.events.emit(
                                    type="tool_retry",
                                    payload={
                                        **payload,
                                        "reason": ("echo_parentheses_unquoted" if fixed else "pip_requirements_path"),
                                        "fixed_cmd": fixed_cmd[:500],
                                    },
                                )
                                self._plain_log(
                                    f"[tool_retry] role={role} i={i} reason=llm_fix fixed_cmd={fixed_cmd!r}"
                                )
                                # Пробуем выполнить исправленную
                                out_txt = self.tools.shell(
                                    fixed_cmd,
                                    timeout_s=timeout_s,
                                    check=True,
                                    cwd_rel=cwd_rel_s,
                                    env=run_env,
                                    stdin=stdin_s,
                                )
                            else:
                                raise
                elif r.tool == ToolName.list_dir:
                    rel = str((r.args or {}).get("rel_path") or ".")
                    out_txt = "\n".join(self.tools.list_dir(rel))
                elif r.tool == ToolName.read_file:
                    rel = str((r.args or {}).get("rel_path") or "")
                    out_txt = self.tools.read_file(rel)
                elif r.tool == ToolName.grep:
                    pat = str((r.args or {}).get("pattern") or "")
                    glob = str((r.args or {}).get("rel_glob") or "**/*")
                    out_txt = "\n".join(self.tools.grep(pat, rel_glob=glob))
                else:
                    out_txt = f"Unsupported tool: {r.tool}"  # shouldn't happen

                log_path = f"artifacts/tools/{role}/{ts}_{i:02d}.log"
                self.tools.write_file(log_path, (out_txt or "")[:80000])
                created.append(log_path)
                self.events.emit(type="artifact_written", payload={"path": log_path})
                self.events.emit(type="tool_end", payload={**payload, "ok": True, "out_chars": len(out_txt or "")})
                tail = (out_txt or "")[-2000:]
                if tail.strip():
                    self._plain_log(f"[tool_out] role={role} i={i} tail=\n{tail}\n---")
                self._plain_log(f"[tool_end] role={role} i={i} ok=true log={log_path}")
            except Exception as e:
                err_s = str(e)[:80000]
                # Всегда сохраняем ошибку как артефакт (иначе теряется контекст).
                try:
                    err_path = f"artifacts/tools/{role}/{ts}_{i:02d}.error.log"
                    self.tools.write_file(err_path, err_s)
                    created.append(err_path)
                    self.events.emit(type="artifact_written", payload={"path": err_path})
                except Exception:
                    pass

                self.events.emit(type="tool_end", payload={**payload, "ok": False, "error": str(e)[:2000]})
                self._plain_log(f"[tool_end] role={role} i={i} ok=false error={str(e)[:300]!r}")
                if 'err_path' in locals():
                    self._plain_log(f"[tool_error_log] {err_path}")

                # Узкий фоллбек: некоторые агенты пытаются `pip install -r ...` до того как файл создан.
                # Это не критично для генерации файлов; пропускаем, но оставляем лог.
                if r.tool == ToolName.shell and "Could not open requirements file" in str(e):
                    self.events.emit(type="tool_skipped", payload={**payload, "reason": "requirements_file_missing"})
                    self._plain_log(f"[tool_skipped] role={role} i={i} reason=requirements_file_missing")
                    continue
                raise

        return created

    def _run_web_research(self, goal: str) -> list[str]:
        if self.run_cfg.web != "on":
            return []
        if not self.tools.policy.web_enabled:
            return []
        if self.run_cfg.llm == "mock":
            return []

        self.events.emit(type="phase", payload={"phase": "web_research"})

        # 1) Спросим у оркестратора, что искать
        system = (
            "Ты — ResearchPlanner. Верни строго JSON: {queries:[...]} без пояснений. "
            "Сформируй 2-4 коротких поисковых запроса, чтобы найти документацию/примеры для цели."
        )
        user = json.dumps(
            {
                "goal": goal,
                "repo_hint": self.repo_profile.compact_hint(),
                "constraints": {
                    "max_queries": 4,
                    "prefer_official_docs": True,
                },
            },
            ensure_ascii=False,
        )

        plan = self._chat_json_with_retry(
            llm=self.orchestrator_llm,
            system=system,
            user=user,
            role="orchestrator",
            kind="web_research_plan",
            label="orchestrator/web_research_plan",
            timeout_s=120,
            retries=1,
        )
        queries = plan.get("queries")
        if not isinstance(queries, list):
            queries = []
        queries = [q for q in queries if isinstance(q, str) and q.strip()][:4]
        if not queries:
            return []

        # 2) Поиск
        urls: list[str] = []
        for q in queries:
            self.events.emit(type="web_search_start", payload={"query": q})
            try:
                res = self.tools.web_search(q, max_results=5)
                for r in res:
                    u = r.get("url")
                    if isinstance(u, str) and u.startswith("http"):
                        urls.append(u)
                self.events.emit(type="web_search_end", payload={"query": q, "results": len(res)})
            except Exception as e:
                self.events.emit(type="web_search_failed", payload={"query": q, "error": str(e)[:1000]})

        # de-dup and limit
        dedup: list[str] = []
        seen: set[str] = set()
        for u in urls:
            if u in seen:
                continue
            seen.add(u)
            dedup.append(u)
            if len(dedup) >= 8:
                break

        # 3) Fetch
        sources: list[dict[str, Any]] = []
        for u in dedup:
            self.events.emit(type="web_fetch_start", payload={"url": u})
            try:
                txt = self.tools.web_fetch(u)
                sources.append({"url": u, "text": txt[:6000]})
                self.events.emit(type="web_fetch_end", payload={"url": u, "chars": len(txt)})
            except Exception as e:
                self.events.emit(type="web_fetch_failed", payload={"url": u, "error": str(e)[:1000]})

        if not sources:
            return []

        # 4) Summarize -> artifact
        sum_system = (
            "Ты — WebSummarizer. Составь короткую практичную выжимку для разработки. "
            "Верни строго JSON: {artifact_contents:{'artifacts/web_research.md': '...'}}."
        )
        sum_user = json.dumps(
            {
                "goal": goal,
                "sources": sources,
                "format": {
                    "max_lines": 140,
                    "must_include": ["ключевые решения", "ссылки"],
                },
            },
            ensure_ascii=False,
        )
        data = self._chat_json_with_retry(
            llm=self.orchestrator_llm,
            system=sum_system,
            user=sum_user,
            role="orchestrator",
            kind="web_research_summarize",
            label="orchestrator/web_research_summarize",
            timeout_s=180,
            retries=1,
        )
        artifact_contents = data.get("artifact_contents")
        if not isinstance(artifact_contents, dict):
            return []
        content = artifact_contents.get("artifacts/web_research.md")
        if not isinstance(content, str) or not content.strip():
            return []
        path = "artifacts/web_research.md"
        self.tools.write_file(path, content)
        self.events.emit(type="artifact_written", payload={"path": path})
        return [path]

    def _chat_json_with_retry(
        self,
        *,
        llm: Any,
        system: str,
        user: str,
        role: str,
        kind: str,
        label: str,
        timeout_s: int = 300,
        num_predict: int | None = None,
        retries: int = 2,
    ) -> dict[str, Any]:
        """Вызывает LLM в json_mode и делает 1–2 ретрая до JSON-repair.

        Ретраи важны для маленьких моделей: они часто возвращают почти-JSON.
        """
        model = _llm_model_name(llm)
        base_temp = float(getattr(getattr(llm, "config", None), "temperature", 0.2) or 0.2)

        prompt = user
        last_raw = ""
        for attempt in range(1, retries + 2):
            t0 = time.monotonic()
            self.events.emit(
                type="llm_call_start",
                payload={"role": role, "kind": kind, "attempt": attempt, "model": model},
            )
            try:
                raw = llm.chat(
                    system=system,
                    user=prompt,
                    json_mode=True,
                    timeout_s=timeout_s,
                    temperature=max(0.0, base_temp - (attempt - 1) * 0.1),
                    num_predict=num_predict,
                )
                last_raw = raw or ""
            except Exception as e:
                self.events.emit(
                    type="llm_call_error",
                    payload={"role": role, "kind": kind, "attempt": attempt, "model": model, "error": str(e)},
                )
                if attempt >= retries + 1:
                    raise
                time.sleep(min(0.5 * attempt, 2.0))
                continue
            finally:
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                self.events.emit(
                    type="llm_call_end",
                    payload={
                        "role": role,
                        "kind": kind,
                        "attempt": attempt,
                        "model": model,
                        "elapsed_ms": elapsed_ms,
                        "raw_chars": len(last_raw),
                    },
                )

            try:
                data = json.loads(last_raw)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass

            if attempt < retries + 1:
                self.events.emit(
                    type="llm_json_retry",
                    payload={"role": role, "kind": kind, "attempt": attempt, "model": model},
                )
                prompt = (
                    prompt
                    + "\n\nВАЖНО: твой предыдущий ответ был НЕвалидным JSON. "
                    + "Верни СТРОГО валидный JSON-объект без пояснений и без markdown."
                )
                continue

            # Последний шанс: repair (LLM-ремонт)
            self.events.emit(
                type="llm_json_repair",
                payload={"role": role, "kind": kind, "model": model, "label": label},
            )
            return parse_json_or_repair(raw=last_raw, llm=llm, label=label)

        # unreachable
        return {}

    def _normalize_plan(self, plan: OrchestratorPlan) -> OrchestratorPlan:
        # 1) фильтруем пустые/мусорные таски и неизвестные роли
        cleaned = []
        for t in plan.tasks:
            if t.role not in {"backend", "designer", "frontend", "qa", "security_requirements"}:
                continue
            if not (t.title or "").strip() and not (t.description or "").strip():
                continue
            # outputs держим под artifacts/
            t.outputs = [o for o in (t.outputs or []) if isinstance(o, str) and o.startswith("artifacts/")]
            t.inputs = [i for i in (t.inputs or []) if isinstance(i, str)]

            # repo_outputs: только пути файлов репозитория (не artifacts/, не директории).
            repo_out = [p for p in (getattr(t, "repo_outputs", None) or []) if isinstance(p, str)]
            repo_out = [p.strip().lstrip("/") for p in repo_out if p.strip()]

            # Иногда LLM пишет буквальные плейсхолдеры. Приведём к реальному outdir.
            if self.project_root != ".":
                pr = self.project_root.rstrip("/")
                repo_out = [p.replace("<outdir>/", pr + "/", 1) if p.startswith("<outdir>/") else p for p in repo_out]
                repo_out = [p.replace("{outdir}/", pr + "/", 1) if p.startswith("{outdir}/") else p for p in repo_out]

            repo_out = [p for p in repo_out if not p.endswith("/")]
            repo_out = [p for p in repo_out if not p.startswith("artifacts/")]

            # Если outdir != '.', то план часто забывает префикс. Попробуем авто-привести.
            if self.project_root != ".":
                pref = self.project_root.rstrip("/") + "/"
                rewritten: list[str] = []
                for p in repo_out:
                    if p in {"README.md", "run.md", ".gitignore", ".env.example", "docker-compose.yml"}:
                        rewritten.append(p)
                        continue
                    if p.startswith(pref):
                        rewritten.append(p)
                        continue
                    # common project-relative paths
                    if p.startswith("backend/") or p.startswith("frontend/") or p.startswith("packages/"):
                        rewritten.append(pref + p)
                        continue
                    if p in {"package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"}:
                        rewritten.append(pref + p)
                        continue
                    if p.startswith("src/") or p.startswith("apps/") or p.startswith("libs/"):
                        rewritten.append(pref + p)
                        continue
                    rewritten.append(p)
                repo_out = rewritten

            # Дедуп без потери порядка
            t.repo_outputs = list(dict.fromkeys(repo_out))

            # repo_outputs контракт важен только для задач, которые пишут код проекта.
            if t.role not in {"backend", "frontend"}:
                t.repo_outputs = []
            cleaned.append(t)

        # 2) enforce pipeline (базовый порядок). Детализацию шагов (TDD) добавим ниже.
        order = {"backend": 10, "designer": 30, "frontend": 40, "qa": 50, "security_requirements": 60}
        cleaned.sort(key=lambda x: (order.get(x.role, 999), x.title))

        # 3) если забыли добавить designer — вставим минимальную задачу автоматически
        roles = [t.role for t in cleaned]
        has_backend = "backend" in roles
        has_frontend = "frontend" in roles
        has_designer = "designer" in roles
        if has_backend and has_frontend and not has_designer:
            from .schemas import OrchestratorTask

            designer_task = OrchestratorTask(
                role="designer",
                title="Tailwind UI design spec",
                description=(
                    "Подготовь дизайн-спеку и Tailwind utility-first разметку для frontend-разработчика: "
                    "компоненты, состояния, доступность."
                ),
                inputs=[
                    "artifacts/requirements.md",
                    "artifacts/nfr_security.md",
                    "artifacts/api_contract.md",
                    "artifacts/openapi.yaml",
                    "artifacts/web_research.md",
                ],
                outputs=[
                    "artifacts/design_spec.md",
                    "artifacts/tailwind_components.md",
                ],
            )

            # Вставляем после последней backend-задачи
            out = []
            inserted = False
            last_backend_idx = -1
            for idx, t in enumerate(cleaned):
                if t.role == "backend":
                    last_backend_idx = idx
            for idx, t in enumerate(cleaned):
                out.append(t)
                if (not inserted) and last_backend_idx != -1 and idx == last_backend_idx:
                    out.append(designer_task)
                    inserted = True
            cleaned = out

        cleaned.sort(key=lambda x: (order.get(x.role, 999), x.title))

        # 4) Атомарная детализация (TDD-friendly) нужна в основном для write-mode.
        # В propose/apply режимах оставляем план «как есть» (кроме базовой нормализации),
        # чтобы не раздувать число задач и не ломать ожидания.
        if self.run_cfg.patches != "write":
            plan.tasks = cleaned
            return plan

        from .schemas import OrchestratorTask

        out_tasks: list[OrchestratorTask] = []

        def _split_backend(t: OrchestratorTask) -> list[OrchestratorTask]:
            contract = OrchestratorTask(
                role="backend",
                title="Backend 1/2: Contract + skeleton",
                description=(
                    "1) Сформируй artifacts/api_contract.md + artifacts/openapi.yaml (+ artifacts/db_contract.md при необходимости). "
                    "2) Создай минимальный запускаемый скелет backend (может быть очень простым), чтобы следующий шаг мог его развить."
                ),
                inputs=list(t.inputs or []),
                outputs=[
                    "artifacts/api_contract.md",
                    "artifacts/openapi.yaml",
                    "artifacts/db_contract.md",
                ],
                repo_outputs=list(t.repo_outputs or []),
            )
            implement = OrchestratorTask(
                role="backend",
                title="Backend 2/2: Implement API (MVP)",
                description=(
                    "Реализуй рабочий backend по контракту и требованиям: хранилище, эндпоинты, валидация, обработка ошибок. "
                    "Если в цели/требованиях указан стек (например NestJS/FastAPI/Express) — строго следуй ему. "
                    "Добавь минимальные автотесты, соответствующие выбранному стеку, или доведи уже созданные до passing."
                ),
                inputs=list(dict.fromkeys(list(t.inputs or []) + [
                    "artifacts/api_contract.md",
                    "artifacts/openapi.yaml",
                    "artifacts/db_contract.md",
                ])),
                outputs=["artifacts/backend_impl.md"],
                repo_outputs=list(t.repo_outputs or []),
            )
            return [contract, implement]

        def _split_frontend(t: OrchestratorTask) -> list[OrchestratorTask]:
            ui = OrchestratorTask(
                role="frontend",
                title="Frontend 1/2: UI skeleton",
                description=(
                    "Собери UI-скелет (SPA/страницы) по требованиям и дизайн-спеке: разметку, состояния, валидацию формы. "
                    "Пока можно с мок-данными, но без лишней инфраструктуры. "
                    "Если в требованиях указан React + Tailwind — используй их."
                ),
                inputs=list(t.inputs or []),
                outputs=["artifacts/frontend_ui.md"],
                repo_outputs=list(t.repo_outputs or []),
            )
            wiring = OrchestratorTask(
                role="frontend",
                title="Frontend 2/2: API wiring + polish",
                description=(
                    "Подключи UI к backend API (GET/POST), реализуй состояния loading/posting/empty/error, безопасный рендер текста. "
                    "Проверь a11y (label, aria-invalid/aria-describedby, focus-visible)."
                ),
                inputs=list(dict.fromkeys(list(t.inputs or []) + [
                    "artifacts/openapi.yaml",
                    "artifacts/design_spec.md",
                    "artifacts/tailwind_components.md",
                ])),
                outputs=["artifacts/frontend_api.md"],
                repo_outputs=list(t.repo_outputs or []),
            )
            return [ui, wiring]

        def _ensure_tdd_qa_step() -> OrchestratorTask:
            return OrchestratorTask(
                role="qa",
                title="QA (TDD): API tests from contract",
                description=(
                    "Напиши минимальные автотесты для backend API на основе artifacts/openapi.yaml и требований. "
                    "Выбери тест-раннер в зависимости от стека backend (например pytest для Python, jest/supertest для Node). "
                    "Цель: негативные кейсы (валидация), и базовый happy-path."
                ),
                inputs=[
                    "artifacts/requirements.md",
                    "artifacts/openapi.yaml",
                    "artifacts/api_contract.md",
                ],
                outputs=["artifacts/tdd_tests.md"],
            )

        # Группируем и раскладываем: backend(1/2) -> qa(tdd) -> backend(2/2) -> designer -> frontend(1/2,2/2) -> qa финал
        backend_seen = [t for t in cleaned if t.role == "backend"]
        frontend_seen = [t for t in cleaned if t.role == "frontend"]
        designer_seen = [t for t in cleaned if t.role == "designer"]
        qa_seen = [t for t in cleaned if t.role == "qa"]

        if backend_seen:
            if len(backend_seen) == 1:
                out_tasks += _split_backend(backend_seen[0])
            else:
                out_tasks += backend_seen
        if backend_seen:
            out_tasks.append(_ensure_tdd_qa_step())
        if designer_seen:
            out_tasks += designer_seen
        if frontend_seen:
            if len(frontend_seen) == 1:
                out_tasks += _split_frontend(frontend_seen[0])
            else:
                out_tasks += frontend_seen

        # Финальный QA шаг, если в плане его не было.
        if not qa_seen:
            out_tasks.append(
                OrchestratorTask(
                    role="qa",
                    title="QA: Final verification",
                    description="Проверь критерии приёмки, негативные кейсы, кратко опиши результаты.",
                    inputs=["artifacts/requirements.md", "artifacts/openapi.yaml"],
                    outputs=["artifacts/qa_report.md"],
                )
            )
        else:
            out_tasks += qa_seen

        # Удаляем возможные дубликаты/пустые
        plan.tasks = [t for t in out_tasks if (t.title or "").strip() or (t.description or "").strip()]
        return plan

    def _validate_plan_for_write_mode(self, plan: OrchestratorPlan) -> None:
        if self.run_cfg.patches != "write":
            return
        problems: list[str] = []

        has_backend = any(getattr(t, "role", "") == "backend" for t in plan.tasks)
        has_frontend = any(getattr(t, "role", "") == "frontend" for t in plan.tasks)
        backend_has_outputs = any((getattr(t, "role", "") == "backend") and (list(getattr(t, "repo_outputs", None) or [])) for t in plan.tasks)
        frontend_has_outputs = any((getattr(t, "role", "") == "frontend") and (list(getattr(t, "repo_outputs", None) or [])) for t in plan.tasks)
        if has_backend and not backend_has_outputs:
            problems.append("- backend: no tasks with non-empty repo_outputs (required for write-mode)")
        if has_frontend and not frontend_has_outputs:
            problems.append("- frontend: no tasks with non-empty repo_outputs (required for write-mode)")

        for idx, t in enumerate(plan.tasks):
            repo_outputs = list(getattr(t, "repo_outputs", None) or [])
            repo_outputs = [p for p in repo_outputs if isinstance(p, str) and p.strip()]
            if not repo_outputs:
                continue
            role = getattr(t, "role", "")
            if role in {"backend", "frontend"}:
                disallowed = [p for p in repo_outputs if not self._is_allowed_repo_write_path(p)]
                if disallowed:
                    problems.append(
                        f"- task[{idx}] role={role} title={t.title!r}: repo_outputs contain paths not allowed by outdir/policy: {disallowed}"
                    )

        if problems:
            raise RuntimeError(
                "Invalid plan for write-mode. Plan must declare repo_outputs (and respect outdir).\n"
                + "\n".join(problems)
            )

    def _build_task_context(self, *, role: str, inputs: list[str], repo_outputs: list[str] | None = None) -> str:
        blocks: list[str] = []

        # 0) Цель + окно фокуса (контекстное окно)
        if (self._active_goal or "").strip():
            blocks.append(f"--- GOAL ---\n{self._active_goal.strip()}\n--- end GOAL ---")

        for always in ["artifacts/context_window.md", "artifacts/requirements.md", "artifacts/rolling_summary.md"]:
            try:
                if (self.repo_root / always).exists():
                    txt = self.tools.read_file(always)
                    snippet = txt[-12000:] if always.endswith("rolling_summary.md") else txt[:16000]
                    blocks.append(f"--- {always} ---\n{snippet}\n--- end {always} ---")
            except Exception:
                pass

        # 1) Контекст проекта — только из папки роли (backend/frontend), чтобы исключить лишнее из корня репо.
        role_dir = self._role_context_dir(role)
        try:
            if role_dir and role_dir != "." and (self.repo_root / role_dir).exists():
                listing = self.tools.list_dir(role_dir)
                blocks.append(
                    f"--- {role_dir} (dir) ---\n" + "\n".join(listing[:80]) + f"\n--- end {role_dir} (dir) ---"
                )
        except Exception:
            pass

        # 2) Стек-нейтральные манифесты/конфиги (если есть) в папке роли.
        candidate_names = [
            "package.json",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "requirements.txt",
            "pyproject.toml",
            "Cargo.toml",
            "go.mod",
            "pom.xml",
            "build.gradle",
            "gradle.properties",
            "composer.json",
        ]
        for name in candidate_names:
            dep_path = f"{role_dir.rstrip('/')}/{name}" if role_dir not in {"", "."} else name
            try:
                if (self.repo_root / dep_path).exists():
                    txt = self.tools.read_file(dep_path)
                    blocks.append(f"--- {dep_path} ---\n{txt[:12000]}\n--- end {dep_path} ---")
            except Exception:
                pass
        for p in inputs:
            if not isinstance(p, str) or not p.strip():
                continue
            try:
                rel = p.strip()
                abs_path = self.repo_root / rel
                if not abs_path.exists():
                    continue
                # Читаем только разумный объём, чтобы не раздувать контекст.
                txt = self.tools.read_file(rel)
                if not isinstance(txt, str) or not txt.strip():
                    continue
                snippet = txt[:12000]
                blocks.append(f"--- {rel} ---\n{snippet}\n--- end {rel} ---")
            except Exception:
                continue

        # Для write-mode полезно показывать текущее состояние файлов, которые план ожидает изменить.
        for p in (repo_outputs or []):
            try:
                if not isinstance(p, str) or not p.strip():
                    continue
                rel = p.strip().lstrip("/")
                if (self.repo_root / rel).exists():
                    txt = self.tools.read_file(rel)
                    blocks.append(f"--- {rel} (current) ---\n{txt[:12000]}\n--- end {rel} ---")
            except Exception:
                continue

        return "\n\n".join(blocks)

    def _run_intermediate_checks(self, *, role: str, written_paths: list[str]) -> None:
        # 1) Быстрый синтаксический чек для .py
        py_paths = [p for p in written_paths if isinstance(p, str) and p.endswith(".py")]

        def _autofix_python_file(*, path: str, error: str) -> None:
            if self.run_cfg.llm == "mock":
                raise SyntaxError(error)

            try:
                current = self.tools.read_file(path)
            except Exception:
                current = ""

            system = (
                "Ты — PythonFixer. Исправь синтаксическую ошибку в файле. "
                "Верни ТОЛЬКО JSON вида: {file_contents:{'<same path>':'<full corrected file>'}}. "
                "Не меняй пути. Не добавляй лишние файлы."
            )
            user = json.dumps(
                {
                    "path": path,
                    "error": error,
                    "rules": [
                        "Сохрани назначение файла.",
                        "Избегай await вне async def.",
                        "Не подключай внешние БД/сервисы (например MongoDB).",
                        "Сделай минимальные изменения, чтобы код компилировался.",
                    ],
                    "current": current[:24000],
                },
                ensure_ascii=False,
                indent=2,
            )

            llm_obj = getattr(self.agents.get(role), "llm", None) or self.orchestrator_llm

            def _extract_corrected_source(data_obj: Any) -> str | None:
                # 1) canonical: {file_contents:{path: "..."}}
                if isinstance(data_obj, dict):
                    fc0 = data_obj.get("file_contents")
                    if isinstance(fc0, dict):
                        v0 = fc0.get(path)
                        if isinstance(v0, str) and v0.strip():
                            return v0
                        # sometimes key differs by missing leading ./ or similar; match by suffix
                        for k, v in fc0.items():
                            if not isinstance(k, str) or not isinstance(v, str) or not v.strip():
                                continue
                            if k == path or k.endswith("/" + path) or k.endswith(path) or k.endswith(Path(path).name):
                                return v

                    # 2) direct: {"<path>": "..."}
                    direct0 = data_obj.get(path)
                    if isinstance(direct0, str) and direct0.strip():
                        return direct0

                    # 3) best-effort: find a single plausible string value
                    candidates: list[tuple[int, str]] = []
                    for k, v in data_obj.items():
                        if not isinstance(k, str) or not isinstance(v, str) or not v.strip():
                            continue
                        if k.endswith(Path(path).name) or k.endswith(path) or k == "content" or k == "source":
                            candidates.append((len(k), v))
                    if candidates:
                        candidates.sort(key=lambda x: x[0], reverse=True)
                        return candidates[0][1]
                return None
            data = self._chat_json_with_retry(
                llm=llm_obj,
                system=system,
                user=user,
                role=role,
                kind="autofix_syntax",
                label=f"autofix/{role}/{path}",
                timeout_s=180,
                num_predict=4096,
                retries=1,
            )

            # Сохраняем как артефакт для дебага (коротко).
            try:
                self.tools.write_file(
                    "artifacts/autofix_last.json",
                    json.dumps({"path": path, "error": error, "response": data}, ensure_ascii=False, indent=2)[:50000],
                )
                self.events.emit(type="artifact_written", payload={"path": "artifacts/autofix_last.json"})
            except Exception:
                pass

            new_src = _extract_corrected_source(data)
            if not isinstance(new_src, str) or not new_src.strip():
                strict_system = system + "\nОБЯЗАТЕЛЬНО верни JSON с file_contents и ключ ровно равный пути файла."
                data2 = self._chat_json_with_retry(
                    llm=llm_obj,
                    system=strict_system,
                    user=user,
                    role=role,
                    kind="autofix_syntax",
                    label=f"autofix/{role}/{path}/retry",
                    timeout_s=180,
                    num_predict=4096,
                    retries=1,
                )
                new_src = _extract_corrected_source(data2)

            if not isinstance(new_src, str) or not new_src.strip():
                raise RuntimeError(f"autofix did not return corrected content for {path}")

            self._write_repo_files(role=role, file_contents={path: new_src})

        for p in py_paths:
            self.events.emit(type="gate_start", payload={"cmd": f"compile:{p}", "cwd": "."})
            try:
                src = self.tools.read_file(p)
                compile(src, p, "exec")
                self.events.emit(type="gate_end", payload={"cmd": f"compile:{p}", "cwd": ".", "ok": True})
            except Exception as e:
                self.events.emit(
                    type="gate_end",
                    payload={"cmd": f"compile:{p}", "cwd": ".", "ok": False, "error": str(e)[:2000]},
                )

                # Одна автоматическая попытка починки синтаксиса.
                self.events.emit(type="phase", payload={"phase": "autofix_syntax"})
                _autofix_python_file(path=p, error=str(e)[:2000])
                # Перепроверим компиляцию.
                src2 = self.tools.read_file(p)
                compile(src2, p, "exec")
                self.events.emit(type="gate_end", payload={"cmd": f"compile:{p}/after_fix", "cwd": ".", "ok": True})

        # 2) Если затронули backend/тесты — запускаем pytest (allowlisted)
        touched_backend = any(p.startswith("backend/") for p in written_paths)
        touched_tests = any(p.startswith("tests/") or "/test" in p for p in written_paths)
        if (touched_backend or touched_tests) and self.run_cfg.llm != "mock":
            self.events.emit(type="gate_start", payload={"cmd": "python -m pytest", "cwd": "."})
            try:
                out = self.tools.shell("python -m pytest", timeout_s=60 * 10, check=True)
                # Пишем хвост в артефакт для дебага (не раздуваем события)
                log_path = "artifacts/last_pytest.log"
                self.tools.write_file(log_path, (out or "")[-20000:])
                self.events.emit(type="artifact_written", payload={"path": log_path})
                self.events.emit(type="gate_end", payload={"cmd": "python -m pytest", "cwd": ".", "ok": True})
            except Exception as e:
                self.events.emit(
                    type="gate_end",
                    payload={"cmd": "python -m pytest", "cwd": ".", "ok": False, "error": str(e)[:2000]},
                )
                raise

    def _is_allowed_patch_path(self, rel_path: str) -> bool:
        # Базовая allowlist + denylist по чувствительным путям.
        if not self._is_allowed_repo_write_path(rel_path):
            return False
        deny_prefixes = {
            "node_modules/",
            ".git/",
            ".venv/",
        }
        for pref in deny_prefixes:
            if rel_path.startswith(pref):
                return False
        if rel_path == ".env" or rel_path.startswith(".env."):
            return False
        if rel_path.endswith(".pem") or rel_path.endswith(".key") or rel_path.endswith(".p12"):
            return False
        if "/id_rsa" in rel_path or rel_path.startswith("id_rsa"):
            return False
        return True

    def _run_post_apply_gates(self) -> None:
        profile = build_repo_profile(self.repo_root)
        gates = profile.suggested_gate_specs()
        if not gates:
            return

        self.events.emit(type="phase", payload={"phase": "post_apply_gates"})
        for g in gates:
            # Не выходим за пределы allowlist: пропускаем, но логируем.
            normalized = " ".join(g.command.split())
            if normalized not in set(self.tools.policy.shell_allowlist):
                self.events.emit(
                    type="gate_skipped",
                    payload={"cmd": normalized, "cwd": g.cwd, "reason": "not_allowlisted"},
                )
                continue
            self.events.emit(type="gate_start", payload={"cmd": normalized, "cwd": g.cwd})
            try:
                out = self.tools.shell(normalized, timeout_s=60 * 20, check=True, cwd_rel=g.cwd)
                self.events.emit(
                    type="gate_end",
                    payload={"cmd": normalized, "cwd": g.cwd, "ok": True, "out_chars": len(out or "")},
                )
            except Exception as e:
                self.events.emit(
                    type="gate_end",
                    payload={"cmd": normalized, "cwd": g.cwd, "ok": False, "error": str(e)[:2000]},
                )
                raise

    def plan(self, goal: str, created_artifacts: list[str] | None = None) -> OrchestratorPlan:
        self.events.emit(type="phase", payload={"phase": "planning"})
        system = ROLE_SYSTEM["orchestrator"]
        repo_hint = _repo_hint(self.tools)
        repo_hint += "\n" + self.repo_profile.compact_hint()
        repo_hint += f"\nProject output directory (outdir): {self.project_root}"
        if created_artifacts:
            repo_hint += "\nExisting artifacts: " + ", ".join(created_artifacts[:20])
        user = ORCHESTRATOR_PLAN_USER.format(goal=goal, repo_hint=repo_hint)

        data = self._chat_json_with_retry(
            llm=self.orchestrator_llm,
            system=system,
            user=user,
            role="orchestrator",
            kind="plan",
            label="orchestrator/plan",
            timeout_s=300,
            retries=2,
        )

        try:
            plan = OrchestratorPlan.model_validate(data)
            return self._normalize_plan(plan)
        except ValidationError as e:
            raise RuntimeError(f"Invalid OrchestratorPlan: {e}\n{json.dumps(data, ensure_ascii=False)[:4000]}")

    def _run_security_preflight(self, goal: str) -> list[str]:
        self.events.emit(type="phase", payload={"phase": "security_preflight"})
        agent = self.agents["security_requirements"]
        system = ROLE_SYSTEM["security_requirements"]
        user = SECURITY_PREFLIGHT_USER.format(goal=goal)

        data = self._chat_json_with_retry(
            llm=agent.llm,
            system=system,
            user=user,
            role="security_requirements",
            kind="preflight",
            label="security/preflight",
            timeout_s=300,
            retries=2,
        )
        artifact_contents = data.get("artifact_contents", {})
        created: list[str] = []
        if isinstance(artifact_contents, dict):
            for path, content in artifact_contents.items():
                if isinstance(path, str) and path.startswith("artifacts/") and isinstance(content, str):
                    if path in {"artifacts/requirements.md", "artifacts/context_window.md", "artifacts/rolling_summary.md"}:
                        self.events.emit(
                            type="artifact_write_skipped",
                            payload={"path": path, "reason": "reserved_for_orchestrator"},
                        )
                        continue
                    self.tools.write_file(path, content)
                    created.append(path)
                    self.events.emit(type="artifact_written", payload={"path": path})
        return created

    def _save_patch_artifact(self, title: str, diff: str) -> str:
        safe_title = "".join(c for c in title.lower() if c.isalnum() or c in {"-", "_", " "}).strip()
        safe_title = "_".join(safe_title.split())[:60] or "patch"
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        path = f"artifacts/patches/{ts}_{safe_title}.diff"
        self.tools.write_file(path, diff)
        self.events.emit(type="artifact_written", payload={"path": path})
        return path

    def _is_allowed_repo_write_path(self, rel_path: str) -> bool:
        # Запрещаем скрытые/служебные папки и любые попытки обхода.
        if not isinstance(rel_path, str) or not rel_path.strip():
            return False
        if "\\" in rel_path:
            return False
        if ".." in rel_path.split("/"):
            return False
        if rel_path.startswith(".git/") or rel_path == ".git":
            return False

        allowed_root = {"README.md", "run.md", ".gitignore", ".env.example", "docker-compose.yml"}
        if rel_path in allowed_root:
            return True

        # Генерируемое решение — только под project_root (например goal/...).
        if self.project_root != ".":
            pref = self.project_root.rstrip("/") + "/"
            return rel_path.startswith(pref)

        # Back-compat: если outdir='.', разрешаем старые пути
        # if rel_path.startswith("frontend/") or rel_path.startswith("backend/"):
        #     return True
        return True

    def _materialize_write_files_for_task(self, *, role: str, task: AgentTask) -> list[str]:
        """Fallback для write-mode: если роль не вернула file_contents, попросим LLM выдать полный контент файлов."""
        if self.run_cfg.llm == "mock":
            return []

        required_raw: list[str] = [p for p in (task.repo_outputs or []) if isinstance(p, str) and p.strip()]
        # Дедуп без потери порядка
        required: list[str] = list(dict.fromkeys([p.strip() for p in required_raw]))
        if not required:
            # Если план не задал repo_outputs — оркестратор не должен угадывать структуру проекта.
            self.events.emit(
                type="materialize_skipped",
                payload={"role": role, "title": task.title, "reason": "no_repo_outputs_in_plan"},
            )
            return []

        disallowed = [p for p in required if not self._is_allowed_repo_write_path(p)]
        if disallowed:
            self.events.emit(
                type="materialize_skipped",
                payload={
                    "role": role,
                    "title": task.title,
                    "reason": "repo_outputs_path_not_allowed",
                    "paths": disallowed,
                },
            )
            return []

        existing: dict[str, str] = {}
        for p in required + ["README.md", "run.md"]:
            try:
                if self._is_allowed_repo_write_path(p) and (self.repo_root / p).exists():
                    existing[p] = self.tools.read_file(p)[:20000]
            except Exception:
                continue

        artifact_ctx: dict[str, str] = {}
        for inp in (task.inputs or []):
            try:
                if isinstance(inp, str) and inp.startswith("artifacts/") and (self.repo_root / inp).exists():
                    artifact_ctx[inp] = self.tools.read_file(inp)[:20000]
            except Exception:
                continue

        # Планировщик/роль могли забыть положить базовые требования в inputs.
        for always in [
            "artifacts/requirements.md",
            "artifacts/nfr_security.md",
            "artifacts/threat_model.md",
            "artifacts/openapi.yaml",
            "artifacts/api_contract.md",
            "artifacts/db_contract.md",
        ]:
            if always in artifact_ctx:
                continue
            try:
                if (self.repo_root / always).exists():
                    artifact_ctx[always] = self.tools.read_file(always)[:20000]
            except Exception:
                continue

        base_system = (
            "Ты — RepoWriter. Твоя задача: выдать ПОЛНЫЙ контент нужных файлов репозитория.\n"
            "Правила:\n"
            "- Верни ТОЛЬКО JSON.\n"
            "- Верни ключ file_contents, где КЛЮЧИ — реальные пути файлов.\n"
            "  Пример: file_contents={\"path/to/file.txt\": \"...\"}.\n"
            "- Обязательно включи все required_paths как ключи file_contents (один-в-один).\n"
            "- Делай MVP и держи код минимальным (ориентир: до ~250 строк на файл).\n"
            "- НЕЛЬЗЯ подключать внешние сервисы/БД (например MongoDB). Используй только локальные зависимости проекта.\n"
            "- Если в цели/требованиях указан стек — строго следуй ему.\n"
            "- Пиши ТОЛЬКО в required_paths и (если требуется) в корневые файлы: README.md, run.md, .gitignore, .env.example, docker-compose.yml.\n"
            "- Не используй .. и не пиши в .git/.\n"
        )
        user = json.dumps(
            {
                "role": role,
                "task": {"title": task.title, "description": task.description},
                "goal": (self._active_goal or ""),
                "required_paths": required,
                "repo_hint": _repo_hint(self.tools) + "\n" + self.repo_profile.compact_hint(),
                "artifacts": artifact_ctx,
                "existing_files": existing,
            },
            ensure_ascii=False,
            indent=2,
        )

        self.events.emit(
            type="materialize_start",
            payload={"role": role, "title": task.title, "paths": required, "reason": "missing_file_contents"},
        )

        def _call(system: str, *, attempt_label: str, llm_obj: Any) -> dict[str, Any]:
            data_local = self._chat_json_with_retry(
                llm=llm_obj,
                system=system,
                user=user,
                role=role,
                kind="materialize_missing",
                label=f"materialize_missing/{attempt_label}",
                timeout_s=240,
                num_predict=4096,
                retries=1,
            )
            # _chat_json_with_retry сам эмитит llm_call_*; оставим отдельный маркер конца materialize
            self.events.emit(
                type="materialize_llm_end",
                payload={"role": role, "title": task.title, "raw_chars": len(json.dumps(data_local, ensure_ascii=False))},
            )
            return data_local

        # Сначала пробуем модель роли (обычно coder), затем — оркестратор.
        role_llm = getattr(self.agents.get(role), "llm", None)
        data = _call(base_system, attempt_label=f"{role}/role_llm", llm_obj=(role_llm or self.orchestrator_llm))
        file_contents = data.get("file_contents", {})
        if not isinstance(file_contents, dict):
            file_contents = {}

        missing = [p for p in required if p not in file_contents]
        if missing:
            retry_system = (
                base_system
                + "\nОШИБКА: ты не включил required_paths как ключи file_contents. "
                + "Нельзя использовать буквальный ключ 'path'. Повтори ответ корректно."
            )
            data = _call(retry_system, attempt_label=f"{role}/retry_role_llm", llm_obj=(role_llm or self.orchestrator_llm))
            file_contents = data.get("file_contents", {})
            if not isinstance(file_contents, dict):
                file_contents = {}

        missing = [p for p in required if p not in file_contents]
        if missing:
            # Последний шанс: попробуем модель оркестратора (иногда лучше следует формату).
            data = _call(base_system, attempt_label=f"{role}/orchestrator_llm", llm_obj=self.orchestrator_llm)
            file_contents = data.get("file_contents", {})
            if not isinstance(file_contents, dict):
                file_contents = {}

        try:
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            safe_title = "".join(c for c in (task.title or "").lower() if c.isalnum() or c in {"-", "_", " "}).strip()
            safe_title = "_".join(safe_title.split())[:60] or "materialized"
            artifact_path = f"artifacts/materialized/{ts}_{safe_title}.json"
            self.tools.write_file(
                artifact_path,
                json.dumps({"file_contents": file_contents}, ensure_ascii=False, indent=2),
            )
            self.events.emit(type="artifact_written", payload={"path": artifact_path})
        except Exception:
            pass

        written = self._write_repo_files(role=role, file_contents=file_contents)
        self.events.emit(
            type="materialize_end",
            payload={"role": role, "title": task.title, "files_written": len(written)},
        )
        return written

    def _write_repo_files(self, *, role: str, file_contents: dict[str, str]) -> list[str]:
        written: list[str] = []
        for path, content in file_contents.items():
            if not isinstance(path, str) or not isinstance(content, str):
                continue
            if not self._is_allowed_repo_write_path(path):
                self.events.emit(
                    type="file_write_skipped",
                    payload={"role": role, "path": path, "reason": "path_not_allowed"},
                )
                continue
            self.events.emit(type="file_write_start", payload={"role": role, "path": path})
            self.tools.write_file(path, content)
            written.append(path)
            self.events.emit(
                type="file_written",
                payload={"role": role, "path": path, "bytes": len(content.encode("utf-8"))},
            )
        return written

    def _extract_paths_from_diff(self, diff: str) -> list[str]:
        # Пытаемся понять, какие файлы затрагивает diff (для контекста materialize).
        paths: set[str] = set()

        for m in re.finditer(r"^diff --git a/(.+?) b/(.+)$", diff, flags=re.MULTILINE):
            a = (m.group(1) or "").strip()
            b = (m.group(2) or "").strip()
            for p in (a, b):
                if p and p != "/dev/null":
                    paths.add(p)

        for m in re.finditer(r"^\+\+\+\s+b/(.+)$", diff, flags=re.MULTILINE):
            p = (m.group(1) or "").strip()
            if p and p != "/dev/null":
                paths.add(p)
        for m in re.finditer(r"^---\s+a/(.+)$", diff, flags=re.MULTILINE):
            p = (m.group(1) or "").strip()
            if p and p != "/dev/null":
                paths.add(p)
        # Ограничим разумным числом
        out = [p for p in sorted(paths) if isinstance(p, str)]
        return out[:12]

    def _materialize_failed_patch_to_files(
        self,
        *,
        role: str,
        title: str,
        diff: str,
        apply_error: str,
    ) -> list[str]:
        # fallback: попросить LLM выдать полные файлы вместо diff.
        if self.run_cfg.llm == "mock":
            return []

        paths = self._extract_paths_from_diff(diff)
        existing: dict[str, str] = {}
        for p in paths:
            rel = p
            try:
                # читаем только разрешённые (по нашей будущей политике), иначе не тратим контекст
                if self._is_allowed_repo_write_path(rel) and (self.repo_root / rel).exists():
                    existing[rel] = self.tools.read_file(rel)
            except Exception:
                continue

        system = (
            "Ты — RepoMaterializer. Твоя задача: по входному diff-патчу (который не применился) "
            "сгенерировать полный контент файлов для репозитория, чтобы реализовать те же изменения.\n"
            "Правила:\n"
            "- Верни ТОЛЬКО JSON.\n"
            "- Верни ключ file_contents: {\"path\": \"полный контент\"}.\n"
            "- Пиши ТОЛЬКО в файлы из списка paths (извлечены из diff).\n"
            "- Не используй .. и не пиши в .git/.\n"
            "- Если файл уже существует (его контент дан), верни обновлённую полную версию.\n"
        )
        user = json.dumps(
            {
                "title": title,
                "role": role,
                "apply_error": apply_error[:2000],
                "repo_hint": _repo_hint(self.tools),
                "diff": diff[:50000],
                "existing_files": existing,
            },
            ensure_ascii=False,
            indent=2,
        )

        self.events.emit(
            type="materialize_start",
            payload={"role": role, "title": title, "paths": paths},
        )
        raw = self.orchestrator_llm.chat(system=system, user=user, json_mode=True, timeout_s=300)
        self.events.emit(
            type="materialize_llm_end",
            payload={"role": role, "title": title, "raw_chars": len(raw or "")},
        )

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = parse_json_or_repair(raw=raw, llm=self.orchestrator_llm, label=f"materialize/{role}/{title}")

        file_contents = data.get("file_contents", {})
        if not isinstance(file_contents, dict):
            file_contents = {}

        # сохраняем как артефакт для трассировки
        try:
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            safe_title = "".join(c for c in title.lower() if c.isalnum() or c in {"-", "_", " "}).strip()
            safe_title = "_".join(safe_title.split())[:60] or "materialized"
            artifact_path = f"artifacts/materialized/{ts}_{safe_title}.json"
            self.tools.write_file(artifact_path, json.dumps({"file_contents": file_contents}, ensure_ascii=False, indent=2))
            self.events.emit(type="artifact_written", payload={"path": artifact_path})
        except Exception:
            pass

        written = self._write_repo_files(role=role, file_contents=file_contents)
        self.events.emit(
            type="materialize_end",
            payload={"role": role, "title": title, "files_written": len(written)},
        )
        return written

    def _apply_unified_diff(self, diff: str, *, title: str | None = None, role: str | None = None) -> str:
        def _repair_with_llm(bad_diff: str) -> str | None:
            if self.run_cfg.llm == "mock":
                return None
            system = (
                "Ты — DiffFixer. Твоя задача: превратить входной патч в корректный git diff, который понимает `git apply`.\n"
                "Правила:\n"
                "- Верни ТОЛЬКО diff (без объяснений, без markdown).\n"
                "- Каждый файл должен начинаться с `diff --git a/... b/...` и содержать `---`/`+++`.\n"
                "- Если патч выглядит как 'голый хунк' начиная с `@@`, добавь корректные заголовки и выбери разумный путь файла по содержимому.\n"
                "- Если это новый файл, используй `new file mode 100644` и `--- /dev/null`.\n"
            )
            user = "PATCH_BEGIN\n" + bad_diff.strip() + "\nPATCH_END\n"

            self.events.emit(
                type="patch_repair_start",
                payload={"role": role, "title": title, "bad_chars": len(bad_diff or "")},
            )
            try:
                repaired = self.orchestrator_llm.chat(system=system, user=user, json_mode=False, timeout_s=300)
            except Exception as e:
                self.events.emit(
                    type="patch_repair_failed",
                    payload={"role": role, "title": title, "error": str(e)},
                )
                return None

            repaired = (repaired or "").strip()
            self.events.emit(
                type="patch_repair_end",
                payload={"role": role, "title": title, "repaired_chars": len(repaired)},
            )
            if not repaired:
                return None
            if not repaired.endswith("\n"):
                repaired += "\n"
            return repaired

        def _normalize(diff_text: str) -> str:
            # Частая проблема: модель возвращает «псевдо-diff» вида:
            #   a/file
            #   +++ b/file
            #   @@ ...
            # Без заголовка diff --git и строки ---.
            # Здесь пытаемся привести к формату, который понимает git apply.
            if "diff --git " in diff_text:
                return diff_text

            lines = diff_text.splitlines()
            out: list[str] = []
            i = 0
            while i < len(lines):
                line = lines[i]

                # Начало секции файла: "a/path"
                if line.startswith("a/") and (i + 1) < len(lines) and lines[i + 1].startswith("+++ b/"):
                    a_path = line.strip()
                    b_line = lines[i + 1]
                    b_path = b_line[len("+++ ") :].strip()  # "b/path"
                    out.append(f"diff --git {a_path} {b_path}")

                    # Определяем, это новый файл или нет (эвристика по следующему ханку)
                    j = i + 2
                    is_new_file = False
                    while j < len(lines):
                        if lines[j].startswith("@@"):
                            if "-0,0" in lines[j]:
                                is_new_file = True
                            break
                        if lines[j].startswith("a/") and (j + 1) < len(lines) and lines[j + 1].startswith("+++ b/"):
                            break
                        j += 1

                    if is_new_file:
                        out.append("new file mode 100644")
                        out.append("--- /dev/null")
                    else:
                        out.append(f"--- {a_path}")
                    out.append(b_line)
                    i += 2
                    continue

                if line.startswith("+++ b/") and (len(out) == 0 or not out[-1].startswith("--- ")):
                    out.append("--- /dev/null")
                    out.append(line)
                    i += 1
                    continue

                out.append(line)
                i += 1

            return "\n".join(out).strip() + "\n"

        # Перед apply: проверяем, что патч не пытается писать в запрещённые пути.
        touched = self._extract_paths_from_diff(diff)
        blocked = [p for p in touched if not self._is_allowed_patch_path(p)]
        if blocked:
            self.events.emit(
                type="patch_blocked",
                payload={"role": role, "title": title, "paths": blocked[:20]},
            )
            raise RuntimeError(f"Patch touches blocked paths: {blocked[:10]}")

        # Применяем через git apply, если репо — git. Это самый надёжный простой путь.
        git_dir = self.repo_root / ".git"
        if not git_dir.exists():
            raise RuntimeError("Patch apply requires a git repo (.git not found). Use --patches propose.")

        if self.tools.policy.approval == "interactive":
            # локальный интерактивный гейт
            from .tools import _confirm

            if not _confirm("Apply patch via git apply?"):
                raise RuntimeError("User rejected patch apply")

        import subprocess

        def _git_apply(input_diff: str, *, check_only: bool) -> subprocess.CompletedProcess[str]:
            args = ["git", "apply", "--whitespace=nowarn"]
            if check_only:
                args.append("--check")
            args.append("-")
            return subprocess.run(
                args,
                input=input_diff,
                text=True,
                cwd=str(self.repo_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

        # 1) check
        self.events.emit(type="patch_check_start", payload={"role": role, "title": title})
        check_proc = _git_apply(diff, check_only=True)
        candidate = diff
        if check_proc.returncode != 0:
            normalized = _normalize(diff)
            if normalized != diff:
                check_proc = _git_apply(normalized, check_only=True)
                candidate = normalized
            if check_proc.returncode != 0:
                repaired = _repair_with_llm(candidate)
                if repaired:
                    check_proc = _git_apply(repaired, check_only=True)
                    candidate = repaired
        if check_proc.returncode != 0:
            self.events.emit(
                type="patch_check_failed",
                payload={"role": role, "title": title, "output": check_proc.stdout[-4000:]},
            )
            raise RuntimeError(f"git apply --check failed:\n{check_proc.stdout}")
        self.events.emit(type="patch_check_end", payload={"role": role, "title": title, "ok": True})

        # 2) apply (candidate is already check-passing)
        apply_proc = _git_apply(candidate, check_only=False)
        if apply_proc.returncode != 0:
            raise RuntimeError(f"git apply failed (after --check passed?):\n{apply_proc.stdout}")

        # 3) post-apply summary
        try:
            stat = subprocess.run(
                ["git", "diff", "--stat"],
                text=True,
                cwd=str(self.repo_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.events.emit(
                type="git_diff_stat",
                payload={"role": role, "title": title, "output": (stat.stdout or "")[-4000:]},
            )
        except Exception:
            pass

        return apply_proc.stdout.strip()

    def _security_answer_questions(self, goal: str, questions: list[str]) -> list[str]:
        if not questions:
            return []
        self.events.emit(type="phase", payload={"phase": "security_answers"})
        agent = self.agents["security_requirements"]
        system = ROLE_SYSTEM["security_requirements"]
        user = SECURITY_QA_USER.format(goal=goal, questions="\n".join(f"- {q}" for q in questions))

        data = self._chat_json_with_retry(
            llm=agent.llm,
            system=system,
            user=user,
            role="security_requirements",
            kind="qa",
            label="security/qa",
            timeout_s=300,
            retries=2,
        )
        artifact_contents = data.get("artifact_contents", {})
        created: list[str] = []
        if isinstance(artifact_contents, dict):
            for path, content in artifact_contents.items():
                if isinstance(path, str) and path.startswith("artifacts/") and isinstance(content, str):
                    self.tools.write_file(path, content)
                    created.append(path)
                    self.events.emit(type="artifact_written", payload={"path": path})
        return created

    def run(self, goal: str) -> OrchestratorSynthesis:
        """Выполняет полный цикл оркестрации по цели.

        Фазы:
        1) очистка/ротация artifacts;
        2) генерация базовых требований;
        3) security preflight и опциональный web-research;
        4) построение плана и исполнение задач ролей;
        5) synthesis, пост-обработка и итоговый отчёт.
        """
        # Требование: artifacts/ должен быть пустым, чтобы агенты не подхватывали лишнее.
        self._reset_artifacts_dir()

        self.events.emit(type="phase", payload={"phase": "start"})
        # Используется в write-mode materializer/fallback, чтобы модель строго следовала цели.
        self._active_goal = goal
        created_artifacts: list[str] = []

        # 0) Оркестратор строит базовые requirements (нейтрально к стеку).
        created_artifacts += self._derive_base_requirements(goal)

        # 1) Аналитик ИБ делает preflight (NFR/Threat model), не перезаписывая requirements.
        created_artifacts += self._run_security_preflight(goal)

        # (Опционально) Web research артефакт для справки.
        created_artifacts += self._run_web_research(goal)

        # 1) Затем планирование, учитывая что требования уже сформированы в artifacts/.
        plan = self.plan(goal, created_artifacts)
        self._validate_plan_for_write_mode(plan)

        agent_results: list[dict[str, Any]] = []
        collected_questions: list[str] = []

        try:
            for t in plan.tasks:
                role = t.role
                if role not in self.agents:
                    continue

            # security_requirements мы уже прогнали preflight; остальные задачи допускаем,
            # но обычно план и так поставит его первым.

                task = AgentTask(
                    role=role,
                    title=str(t.title or ""),
                    description=str(t.description or ""),
                    inputs=list(t.inputs or []),
                    outputs=list(t.outputs or []),
                    context=self._build_task_context(
                        role=role,
                        inputs=list(t.inputs or []),
                        repo_outputs=list(getattr(t, "repo_outputs", None) or []),
                    ),
                    project_dir=self.project_root,
                    repo_outputs=list(getattr(t, "repo_outputs", None) or []),
                )

                self.events.emit(type="task_start", payload={"role": role, "title": task.title})

                feedback_err = None
                out = None

                # Retry loop (dialogue mode)
                for attempt_i in range(1, 4):
                    if attempt_i > 1:
                        self._plain_log(f"\n[ORCHESTRATOR] Retrying task {task.title!r} (attempt {attempt_i}/3)...")
                    
                    # Call agent (with feedback if any)
                    out = self.agents[role].run(
                        task,
                        change_mode=("write" if self.run_cfg.patches == "write" else "patch"),
                        feedback=feedback_err,
                    )

                    try:
                        # Роль может запросить выполнение инструментов
                        if out.tool_requests:
                            # В write-mode: не выполняем shell-команды (детерминизм/безопасность).
                            # Агент должен вернуть file_contents, а не устанавливать зависимости.
                            if self.run_cfg.patches == "write":
                                non_shell = [r for r in out.tool_requests if getattr(r, "tool", None) != ToolName.shell]
                                shell_reqs = [r for r in out.tool_requests if getattr(r, "tool", None) == ToolName.shell]

                                if non_shell:
                                    created_artifacts += self._execute_tool_requests(
                                        role=role,
                                        title=task.title,
                                        reqs=non_shell,
                                    )
                                if shell_reqs:
                                    feedback_err = (
                                        "В режиме --patches write shell-команды не выполняются. "
                                        "Верни решение через file_contents (полные файлы) и/или используй только read/list/grep tool_requests. "
                                        "Не делай pip/npm install и не запускай команды."
                                    )
                                    # Просим агента переиздать результат без shell tool_requests
                                    continue
                            else:
                                created_artifacts += self._execute_tool_requests(
                                    role=role,
                                    title=task.title,
                                    reqs=out.tool_requests,
                                )
                        # Success
                        break
                    except Exception as e:
                        err_msg = str(e)
                        self._plain_log(f"\n[ORCHESTRATOR] Tool execution error: {err_msg}")
                        feedback_err = f"Ошибка при выполнении команд: {err_msg}"
                        if attempt_i == 3:
                            # Re-raise on last attempt
                            raise

                # After loop, 'out' holds the last result (successful or failed-but-raised above)
                if out:
                    for p in out.patches:
                        self.events.emit(type="patch_seen", payload={"role": role, "title": p.get("title", "patch")})
                    agent_results.append({
                        "role": out.result.role,
                    "summary": out.result.summary,
                    "artifacts": out.result.artifacts,
                    "patches": out.result.patches,
                    "risks": out.result.risks,
                    "open_questions": out.result.open_questions,
                })

                collected_questions += [q for q in out.result.open_questions if isinstance(q, str) and q.strip()]

            # Пишем артефакты (через tool-gate)
                for path, content in out.artifact_contents.items():
                    self.tools.write_file(path, content)
                    created_artifacts.append(path)
                    self.events.emit(type="artifact_written", payload={"path": path})

            # Прямая запись файлов репозитория (write-mode)
                written_paths: list[str] = []
                if self.run_cfg.patches == "write" and out.file_contents:
                    written_paths = self._write_repo_files(role=role, file_contents=out.file_contents)
                    created_artifacts += written_paths
                elif (
                    self.run_cfg.patches == "write"
                    and role in {"backend", "frontend"}
                    and not out.file_contents
                ):
                    # Fallback: роли иногда возвращают валидный JSON, но без file_contents.
                    # В write-mode для backend/frontend это означает, что код не будет сгенерирован.
                    written_paths = self._materialize_write_files_for_task(role=role, task=task)
                    created_artifacts += written_paths

                if written_paths:
                    self._run_intermediate_checks(role=role, written_paths=written_paths)

            # Патчи: либо сохраняем как артефакты, либо применяем.
                if self.repo_root.exists() and out.patches:
                    for p in out.patches:
                        title = p.get("title", "patch")
                        diff = p.get("diff", "")
                        if self.run_cfg.patches == "off":
                            continue
                        if self.run_cfg.patches == "propose":
                            created_artifacts.append(self._save_patch_artifact(title, diff))
                        elif self.run_cfg.patches == "apply":
                            # сначала сохраняем, потом применяем (для трассировки)
                            created_artifacts.append(self._save_patch_artifact(title, diff))
                            try:
                                self._apply_unified_diff(diff, title=str(title), role=str(role))
                                self.events.emit(
                                    type="patch_applied",
                                    payload={"role": role, "title": title},
                                )
                                # P0: гейты после apply; при провале останавливаем пайплайн
                                self._run_post_apply_gates()
                            except Exception as e:
                                self.events.emit(
                                    type="patch_apply_failed",
                                    payload={"role": role, "title": title, "error": str(e)},
                                )
                                # fallback: materialize into full files
                                try:
                                    self._materialize_failed_patch_to_files(
                                        role=str(role),
                                        title=str(title),
                                        diff=str(diff),
                                        apply_error=str(e),
                                    )
                                except Exception as e2:
                                    self.events.emit(
                                        type="materialize_failed",
                                        payload={"role": role, "title": title, "error": str(e2)},
                                    )
                                # если упали гейты/безопасность — прекращаем run
                                raise

                # Контекстное окно: rolling summary для следующего шага
                try:
                    artifacts_written = []
                    if out.result and isinstance(out.result.artifacts, list):
                        artifacts_written += [a for a in out.result.artifacts if isinstance(a, str)]
                    if isinstance(out.artifact_contents, dict):
                        artifacts_written += [a for a in out.artifact_contents.keys() if isinstance(a, str)]
                    self._append_rolling_summary(
                        role=str(role),
                        title=str(task.title),
                        agent_summary=str(getattr(out.result, "summary", "") or ""),
                        written_paths=written_paths,
                        artifacts_written=artifacts_written,
                    )
                    if "artifacts/rolling_summary.md" not in created_artifacts:
                        created_artifacts.append("artifacts/rolling_summary.md")
                except Exception:
                    pass

            # QA validation via Playwright (optional): запускаем после выполнения QA-шага.
                if role == "qa" and self.run_cfg.qa_playwright != "off" and self.run_cfg.llm != "mock":
                    if self.run_cfg.qa_playwright == "run":
                        self.events.emit(type="phase", payload={"phase": "playwright_run"})
                        try:
                            out_txt = self.tools.shell("npx playwright test", timeout_s=60 * 30, check=True)
                            # сохраняем вывод в артефакт
                            log_path = "artifacts/playwright_last_run.log"
                            self.tools.write_file(log_path, out_txt)
                            created_artifacts.append(log_path)
                            self.events.emit(type="artifact_written", payload={"path": log_path})
                        except Exception as e:
                            self.events.emit(type="error", payload={"where": "playwright_run", "error": str(e)})
                    elif self.run_cfg.qa_playwright == "ui":
                        self.events.emit(type="phase", payload={"phase": "playwright_ui"})
                        try:
                            pid = self.tools.shell_background(
                                "npx playwright test --ui",
                                log_rel_path="artifacts/playwright_ui.log",
                            )
                            self.events.emit(
                                type="playwright_started",
                                payload={
                                    "mode": "ui",
                                    "pid": pid,
                                    "log": "artifacts/playwright_ui.log",
                                },
                            )
                        except Exception as e:
                            self.events.emit(type="error", payload={"where": "playwright_ui", "error": str(e)})
        except Exception:
            self.events.emit(type="phase", payload={"phase": "aborted"})
            if self.ui is not None:
                self.ui.stop()
            raise

        # 2) Раунд ответов аналитика на вопросы других ролей
        created_artifacts += self._security_answer_questions(goal, sorted(set(collected_questions)))

        system = ROLE_SYSTEM["orchestrator"]
        user = ORCHESTRATOR_SYNTHESIS_USER.format(
            goal=goal,
            results_json=json.dumps(agent_results, ensure_ascii=False, indent=2),
        )

        data = self._chat_json_with_retry(
            llm=self.orchestrator_llm,
            system=system,
            user=user,
            role="orchestrator",
            kind="synthesis",
            label="orchestrator/synthesis",
            timeout_s=300,
            retries=2,
        )

        try:
            synth = OrchestratorSynthesis.model_validate(data)
        except ValidationError as e:
            raise RuntimeError(
                f"Invalid OrchestratorSynthesis: {e}\n{json.dumps(data, ensure_ascii=False, indent=2)}"
            )

        # Объединяем с тем, что реально записали
        synth.created_artifacts = sorted(set((synth.created_artifacts or []) + created_artifacts))

        self.events.emit(type="phase", payload={"phase": "done"})
        if self.ui is not None:
            self.ui.stop()
        return synth
