from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PackageInfo:
    path: str  # relative
    name: str | None
    scripts: dict[str, str]
    deps: set[str]
    dev_deps: set[str]


@dataclass(frozen=True)
class RepoProfile:
    repo_root: Path
    packages: list[PackageInfo]
    has_pyproject: bool
    has_pytest: bool

    def compact_hint(self) -> str:
        parts: list[str] = []
        if self.packages:
            pk = ", ".join(p.path for p in self.packages[:6])
            parts.append(f"packages: {pk}")
        if self.has_pyproject:
            parts.append("python: pyproject.toml")
        if self.has_pytest:
            parts.append("tests: pytest")

        frameworks: list[str] = []
        deps = set().union(*(p.deps | p.dev_deps for p in self.packages)) if self.packages else set()
        if "react" in deps:
            frameworks.append("react")
        if "vite" in deps:
            frameworks.append("vite")
        if "tailwindcss" in deps:
            frameworks.append("tailwind")
        if "@nestjs/core" in deps:
            frameworks.append("nestjs")
        if "express" in deps:
            frameworks.append("express")
        if "fastify" in deps:
            frameworks.append("fastify")
        if "mongoose" in deps or "mongodb" in deps:
            frameworks.append("mongodb")
        if frameworks:
            parts.append("stack: " + ", ".join(sorted(set(frameworks))))

        return "\n".join(parts) if parts else "(empty)"

    def suggested_gates(self) -> list[str]:
        """Backward-compatible wrapper for just commands (cwd='.')"""
        return [g.command for g in self.suggested_gate_specs() if g.cwd == "."]

    def suggested_gate_specs(self) -> list[GateSpec]:
        """Return conservative verification commands to run after patch apply.

        Каждый элемент включает cwd (relative) + команду.
        Команды *потом* будут отфильтрованы через allowlist Tools.shell.
        """
        specs: list[GateSpec] = []

        # Python gates
        if self.has_pytest:
            specs.append(GateSpec(cwd=".", command="pytest"))

        # JS/TS gates per package
        for p in self.packages:
            scripts = p.scripts
            # order matters: lint/typecheck -> tests -> build
            if "lint" in scripts:
                specs.append(GateSpec(cwd=p.path, command="npm run lint"))
            if "typecheck" in scripts:
                specs.append(GateSpec(cwd=p.path, command="npm run typecheck"))
            if "test" in scripts:
                specs.append(GateSpec(cwd=p.path, command="npm test"))
            elif "test:unit" in scripts:
                specs.append(GateSpec(cwd=p.path, command="npm run test:unit"))
            if "build" in scripts:
                specs.append(GateSpec(cwd=p.path, command="npm run build"))

        # de-dup preserve order
        seen: set[tuple[str, str]] = set()
        out: list[GateSpec] = []
        for g in specs:
            key = (g.cwd, g.command)
            if key in seen:
                continue
            seen.add(key)
            out.append(g)
        return out


@dataclass(frozen=True)
class GateSpec:
    cwd: str  # relative
    command: str


def _load_package_json(path: Path) -> dict[str, Any] | None:
    try:
        txt = path.read_text(encoding="utf-8")
        data = json.loads(txt)
        if isinstance(data, dict):
            return data
    except Exception:
        return None
    return None


def _package_info(repo_root: Path, package_json: Path) -> PackageInfo | None:
    data = _load_package_json(package_json)
    if not data:
        return None
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        scripts = {}

    def _deps(key: str) -> set[str]:
        obj = data.get(key)
        if not isinstance(obj, dict):
            return set()
        return {str(k) for k in obj.keys() if isinstance(k, str)}

    rel_dir = str(package_json.parent.relative_to(repo_root)).replace("\\", "/")
    rel_dir = rel_dir if rel_dir != "." else "."

    return PackageInfo(
        path=rel_dir,
        name=data.get("name") if isinstance(data.get("name"), str) else None,
        scripts={str(k): str(v) for k, v in scripts.items() if isinstance(k, str) and isinstance(v, str)},
        deps=_deps("dependencies"),
        dev_deps=_deps("devDependencies"),
    )


def build_repo_profile(repo_root: Path) -> RepoProfile:
    repo_root = repo_root.resolve()
    packages: list[PackageInfo] = []

    # Сканируем только несколько ожидаемых мест, без рекурсии по всему дереву.
    candidates = [
        repo_root / "package.json",
        repo_root / "frontend" / "package.json",
        repo_root / "backend" / "package.json",
    ]

    for pj in candidates:
        if pj.exists() and pj.is_file():
            info = _package_info(repo_root, pj)
            if info is not None:
                packages.append(info)

    has_pyproject = (repo_root / "pyproject.toml").exists()

    # Эвристика pytest: наличие tests/ или pytest.ini/pyproject с pytest.
    has_pytest = False
    if (repo_root / "tests").exists():
        has_pytest = True
    if (repo_root / "pytest.ini").exists():
        has_pytest = True
    if has_pyproject:
        try:
            txt = (repo_root / "pyproject.toml").read_text(encoding="utf-8", errors="ignore")
            if "pytest" in txt:
                has_pytest = True
        except Exception:
            pass

    return RepoProfile(
        repo_root=repo_root,
        packages=packages,
        has_pyproject=has_pyproject,
        has_pytest=has_pytest,
    )
