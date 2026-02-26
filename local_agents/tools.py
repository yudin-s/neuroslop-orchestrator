from __future__ import annotations

import fnmatch
import os
import shlex
import subprocess
import urllib.parse
from dataclasses import dataclass
from html import unescape
from ipaddress import ip_address
from pathlib import Path
import re
import socket
from typing import Any

import requests


@dataclass(frozen=True)
class ToolPolicy:
    approval: str  # "interactive" | "auto"
    shell_allowlist: list[str]
    max_read_bytes: int
    # Дополнительные шаблоны allowlist (fnmatch). Например: "npm run test:*".
    shell_allowlist_patterns: list[str] | None = None
    # Политика чтения файлов (read-only контекст для LLM):
    # - denylist применяется всегда
    # - если allowlist непустой, читать можно только пути, подпадающие под allowlist
    read_denylist: list[str] | None = None
    read_allowlist: list[str] | None = None

    # Web (опционально)
    web_enabled: bool = False
    web_allow_domains: list[str] | None = None
    web_timeout_s: int = 20
    max_web_bytes: int = 300_000


class ToolError(RuntimeError):
    pass


def _confirm(prompt: str) -> bool:
    ans = input(f"{prompt} [y/N]: ").strip().lower()
    return ans in {"y", "yes"}


class Tools:
    def __init__(self, repo_root: Path, policy: ToolPolicy):
        self.repo_root = repo_root
        self.policy = policy

    def _is_shell_allowed(self, normalized: str) -> bool:
        if normalized in set(self.policy.shell_allowlist):
            return True
        pats = self.policy.shell_allowlist_patterns or []
        return any(fnmatch.fnmatch(normalized, p) for p in pats)

    def list_dir(self, rel_path: str = ".") -> list[str]:
        path = (self.repo_root / rel_path).resolve()
        if not str(path).startswith(str(self.repo_root.resolve())):
            raise ToolError("Path escapes repo root")
        if not path.exists():
            raise ToolError(f"Not found: {rel_path}")
        if not path.is_dir():
            raise ToolError(f"Not a directory: {rel_path}")
        return sorted([p.name + ("/" if p.is_dir() else "") for p in path.iterdir()])

    def _is_read_allowed(self, rel_path: str) -> bool:
        rel_path = (rel_path or "").lstrip("/")
        rel_path = rel_path.replace("\\", "/")
        deny = self.policy.read_denylist or []
        allow = self.policy.read_allowlist or []

        # denylist (включая каталоги)
        for pat in deny:
            if fnmatch.fnmatch(rel_path, pat):
                return False
            # удобство: если паттерн выглядит как директория, блокируем и её содержимое
            if pat.endswith("/") and rel_path.startswith(pat):
                return False

        # allowlist (если задан)
        if allow:
            for pat in allow:
                if fnmatch.fnmatch(rel_path, pat):
                    return True
                if pat.endswith("/") and rel_path.startswith(pat):
                    return True
            return False

        return True

    def read_file(self, rel_path: str, max_bytes: int | None = None) -> str:
        path = (self.repo_root / rel_path).resolve()
        if not str(path).startswith(str(self.repo_root.resolve())):
            raise ToolError("Path escapes repo root")
        if not path.exists() or not path.is_file():
            raise ToolError(f"Not a file: {rel_path}")

        if not self._is_read_allowed(rel_path):
            raise ToolError(f"Read blocked by policy: {rel_path}")

        limit = max_bytes or self.policy.max_read_bytes
        data = path.read_bytes()
        if len(data) > limit:
            raise ToolError(f"File too large ({len(data)} bytes), limit {limit}: {rel_path}")
        return data.decode("utf-8", errors="replace")

    def write_file(self, rel_path: str, content: str) -> str:
        path = (self.repo_root / rel_path).resolve()
        if not str(path).startswith(str(self.repo_root.resolve())):
            raise ToolError("Path escapes repo root")

        if self.policy.approval == "interactive":
            if not _confirm(f"Write file {rel_path}?"):
                raise ToolError("User rejected write")

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return rel_path

    def grep(self, pattern: str, rel_glob: str = "**/*") -> list[str]:
        # простой grep без зависимости от ripgrep
        results: list[str] = []
        for p in self.repo_root.glob(rel_glob):
            if not p.is_file():
                continue
            rel = str(p.relative_to(self.repo_root)).replace("\\", "/")
            if not self._is_read_allowed(rel):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            if pattern in text:
                results.append(rel)
        return sorted(results)

    def shell(
        self,
        command: str,
        timeout_s: int = 120,
        *,
        check: bool = True,
        cwd_rel: str | None = None,
        env: dict[str, str] | None = None,
        stdin: str | None = None,
    ) -> str:
        command_s = (command or "").strip()
        normalized = " ".join(shlex.split(command_s))
        if not self._is_shell_allowed(normalized):
            raise ToolError(f"Command not allowlisted: {normalized}")

        if self.policy.approval == "interactive":
            if not _confirm(f"Run shell: {normalized}?"):
                raise ToolError("User rejected shell")

        cwd = self.repo_root
        if cwd_rel is not None:
            rel = (cwd_rel or ".").replace("\\", "/")
            path = (self.repo_root / rel).resolve()
            if not str(path).startswith(str(self.repo_root.resolve())):
                raise ToolError("cwd escapes repo root")
            if not path.exists() or not path.is_dir():
                raise ToolError(f"cwd not found: {cwd_rel}")
            cwd = path

        if env is not None:
            # Сливаем базовый env с переданным
            base = os.environ.copy()
            base.update(env)
            env = base
        else:
            env = os.environ.copy()

        stdin_s: str | None = None
        if stdin is not None:
            stdin_s = str(stdin)
            # Ограничим размер stdin во избежание случайного "залипания"/переполнения.
            if len(stdin_s) > 20_000:
                raise ToolError(f"stdin too large ({len(stdin_s)} chars), limit 20000")

        try:
            proc = subprocess.run(
                command_s,
                shell=True,
                cwd=str(cwd),
                env=env,
                input=stdin_s,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout_s,
                text=True,
            )
        except subprocess.TimeoutExpired as e:
            out = ""
            try:
                if hasattr(e, "stdout") and e.stdout:
                    out = str(e.stdout)
            except Exception:
                out = ""
            raise ToolError(
                f"Command timed out after {timeout_s}s: {normalized}\n{out}"
            )
        if check and proc.returncode != 0:
            raise ToolError(f"Command failed ({proc.returncode}): {normalized}\n{proc.stdout}")
        return proc.stdout

    def shell_background(self, command: str, *, log_rel_path: str, env: dict[str, str] | None = None) -> int:
        command_s = (command or "").strip()
        normalized = " ".join(shlex.split(command_s))
        if not self._is_shell_allowed(normalized):
            raise ToolError(f"Command not allowlisted: {normalized}")

        if self.policy.approval == "interactive":
            if not _confirm(f"Run shell in background: {normalized}?"):
                raise ToolError("User rejected shell")

        log_path = (self.repo_root / log_rel_path).resolve()
        if not str(log_path).startswith(str(self.repo_root.resolve())):
            raise ToolError("Log path escapes repo root")
        log_path.parent.mkdir(parents=True, exist_ok=True)

        merged_env = os.environ.copy()
        venv_bin = (self.repo_root / ".venv" / "bin")
        if venv_bin.exists() and venv_bin.is_dir():
            merged_env["PATH"] = str(venv_bin) + os.pathsep + (merged_env.get("PATH") or "")
        if env:
            merged_env.update(env)

        with log_path.open("w", encoding="utf-8") as log_file:
            try:
                proc = subprocess.Popen(
                    command_s,
                    shell=True,
                    cwd=str(self.repo_root),
                    env=merged_env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            except Exception as e:
                raise ToolError(f"Failed to start background command: {normalized}\n{e}")
        return int(proc.pid)

    def _is_public_internet_host(self, hostname: str) -> bool:
        host = (hostname or "").strip().lower().strip("[]")
        if not host:
            return False
        if host in {"localhost", "127.0.0.1", "0.0.0.0"}:
            return False

        # Разрешаем только домены из allowlist (суффиксное совпадение)
        allow = [d.lower().lstrip(".") for d in (self.policy.web_allow_domains or []) if isinstance(d, str) and d]
        if allow:
            ok = False
            for d in allow:
                if host == d or host.endswith("." + d):
                    ok = True
                    break
            if not ok:
                return False

        # Блокируем приватные/локальные IP после резолва
        try:
            infos = socket.getaddrinfo(host, None)
            for fam, _, _, _, sockaddr in infos:
                if fam not in {socket.AF_INET, socket.AF_INET6}:
                    continue
                ip_str = sockaddr[0]
                ip = ip_address(ip_str)
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
                    return False
        except Exception:
            # если не можем резолвить — лучше отказать, чем выдать SSRF
            return False

        return True

    def web_fetch(self, url: str) -> str:
        if not self.policy.web_enabled:
            raise ToolError("Web access disabled by policy")
        if not isinstance(url, str) or not url.strip():
            raise ToolError("Invalid URL")

        u = urllib.parse.urlparse(url.strip())
        if u.scheme not in {"http", "https"}:
            raise ToolError("Only http/https URLs are allowed")
        if u.port not in {None, 80, 443}:
            raise ToolError("Only default ports 80/443 are allowed")
        if not self._is_public_internet_host(u.hostname or ""):
            raise ToolError("URL blocked by domain/IP policy")

        headers = {"User-Agent": "local_agents/0.1 (+https://localhost)"}
        resp = requests.get(url, headers=headers, timeout=int(self.policy.web_timeout_s))
        resp.raise_for_status()
        data = resp.content
        if len(data) > int(self.policy.max_web_bytes):
            raise ToolError(f"Web response too large ({len(data)} bytes)")

        # Очень простой html->text (без внешних зависимостей)
        text = data.decode(resp.encoding or "utf-8", errors="replace")
        text = re.sub(r"(?is)<script.*?>.*?</script>", " ", text)
        text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
        text = re.sub(r"(?is)<[^>]+>", " ", text)
        text = unescape(text)
        text = re.sub(r"[\t\r ]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        return text.strip()

    def web_search(self, query: str, *, max_results: int = 5) -> list[dict[str, Any]]:
        """Минимальный поиск через DuckDuckGo HTML.

        Без API-ключей; может быть нестабильным, поэтому это best-effort.
        Возвращает список: {title, url, snippet}.
        """
        if not self.policy.web_enabled:
            raise ToolError("Web access disabled by policy")
        q = (query or "").strip()
        if not q:
            return []

        # DDG HTML endpoint
        url = "https://duckduckgo.com/html/?q=" + urllib.parse.quote_plus(q)
        if not self._is_public_internet_host("duckduckgo.com"):
            raise ToolError("Search blocked by domain policy")

        headers = {"User-Agent": "local_agents/0.1"}
        resp = requests.get(url, headers=headers, timeout=int(self.policy.web_timeout_s))
        resp.raise_for_status()
        html = resp.text
        if len(html.encode("utf-8")) > int(self.policy.max_web_bytes):
            html = html[: int(self.policy.max_web_bytes)]

        results: list[dict[str, Any]] = []
        # Простейший парсинг ссылок результатов
        for m in re.finditer(r"(?is)<a[^>]+class=\"result__a\"[^>]+href=\"(.*?)\"[^>]*>(.*?)</a>", html):
            href = unescape(m.group(1) or "")
            title_html = m.group(2) or ""
            title = re.sub(r"(?is)<[^>]+>", " ", title_html).strip()
            href = href.strip()
            if not href:
                continue
            results.append({"title": title, "url": href, "snippet": ""})
            if len(results) >= int(max_results):
                break
        return results
