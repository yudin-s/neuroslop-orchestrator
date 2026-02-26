from __future__ import annotations

import json
from typing import Any

from .llm import LLMClient


_REPAIR_SYSTEM = """Ты — инструмент ремонта JSON.
Твоя задача: получить на вход НЕвалидный JSON (возможно с неэкранированными переводами строк, оборванными строками, лишним текстом) и вернуть ВАЛИДНЫЙ JSON-объект.
Правила:
- Верни ТОЛЬКО JSON (без пояснений, без markdown).
- Если внутри строк есть реальные переводы строк, замени их на \\n.
- Если JSON оборван, постарайся минимально дополнить/закрыть скобки так, чтобы смысл сохранился.
"""


def _extract_json_object(text: str) -> str | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start : end + 1]


def parse_json_or_repair(*, raw: str, llm: LLMClient, label: str) -> dict[str, Any]:
    """Parse JSON; if it fails, try extraction then LLM-based repair."""
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    extracted = _extract_json_object(raw)
    if extracted:
        try:
            data2 = json.loads(extracted)
            if isinstance(data2, dict):
                return data2
        except json.JSONDecodeError:
            pass

    user = (
        f"LABEL: {label}\n"
        "Ниже — невалидный JSON. Преврати его в валидный JSON-объект.\n"
        "INVALID_JSON_BEGIN\n"
        f"{raw}\n"
        "INVALID_JSON_END\n"
    )

    fixed = llm.chat(system=_REPAIR_SYSTEM, user=user, json_mode=True, timeout_s=300)
    data3 = json.loads(fixed)
    if not isinstance(data3, dict):
        raise RuntimeError(f"JSON repair produced non-object for {label}")
    return data3
