"""PreToolUse-хук: блокирует Bash-команды, обращающиеся к .env (любой вариант, кроме
.env.example). Читает JSON хука со stdin, при попадании печатает deny-решение.

Цель — закрыть лазейку чтения секретов через шелл (cat/type/Get-Content ... .env),
которую permission-паттерны Read/Edit/Write не покрывают.
"""
import json
import re
import sys

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

cmd = (data.get("tool_input") or {}).get("command", "") or ""

# .env.example разрешён — вырезаем его, затем ищем любое оставшееся обращение к .env
stripped = cmd.replace(".env.example", "")
if re.search(r"\.env([^a-zA-Z]|$)", stripped):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                ".env заблокирован политикой проекта (см. .claude/settings.json + "
                ".claude/hooks/block-env.py). Используй .env.example."
            ),
        }
    }))

sys.exit(0)
