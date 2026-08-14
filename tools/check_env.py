#!/usr/bin/env python3
"""Предполётная проверка окружения: что на этой машине есть, чего нет.

Гоняется и на ноутбуке, и на сервере — расхождение в выводе и есть список того,
что не переехало. Ничего не чинит и никуда не ходит с записью: только смотрит.

Молчание тут запрещено: каждая строка говорит либо «есть», либо «нет и почему».
Пустой ответ и отсутствующий доступ — разные вещи, и путать их дороже всего.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRETS = ROOT / "config" / "secrets.env"
OK, NO = "есть", "НЕТ"


def load_secrets(path: Path = SECRETS) -> dict[str, str]:
    """Читает секреты из файла в проекте, не перебивая окружение.

    Файл живёт рядом со складом, но вне git: склад уезжает на сервер целиком, и
    всё, что попало в историю, уедет вместе с ним и останется там навсегда. На
    сервере те же имена задаются переменными окружения — поэтому окружение
    старше файла, а не наоборот.
    """
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip().strip('"').strip("'")
        if name and value and not os.environ.get(name):
            os.environ[name] = value
            out[name] = value
    return out


def line(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {OK if ok else NO:5} · {name}" + (f" — {detail}" if detail else ""))
    return ok


def command(name: str) -> bool:
    return shutil.which(name) is not None


def main() -> int:
    from_file = load_secrets()
    print("СКЛАД")
    if from_file:
        line(f"секреты из config/secrets.env: {', '.join(sorted(from_file))}", True,
             "в git не попадают")
    ready = [
        line("корень склада", (ROOT / "AGENTS.md").is_file(), str(ROOT)),
        line("проверки на месте", (ROOT / "tools" / "verify.py").is_file()),
        line("python 3.11+", sys.version_info >= (3, 11), sys.version.split()[0]),
    ]
    try:
        import yaml  # noqa: F401
        ready.append(line("PyYAML", True))
    except ImportError:
        ready.append(line("PyYAML", False, "pip install -r requirements.txt"))

    print("\nВНЕШНИЕ ДОСТУПЫ")
    granola = bool(os.environ.get("GRANOLA_API_KEY", "").strip())
    if not granola and command("security"):
        got = subprocess.run(
            ["security", "find-generic-password", "-s", "granola-api", "-w"],
            capture_output=True, text=True)
        granola = got.returncode == 0
        line("Granola", granola, "ключ из Keychain — на сервере не будет")
    else:
        line("Granola", granola, "переменная GRANOLA_API_KEY")

    gws = command("gws")
    if gws:
        got = subprocess.run(["gws", "auth", "status"], capture_output=True,
                             text=True, timeout=30)
        try:
            valid = json.loads(got.stdout).get("token_valid") is True
        except (json.JSONDecodeError, ValueError):
            valid = False
        line("Календарь (gws)", valid, "токен живой" if valid
             else "gws есть, но доступ не подтверждён")
    else:
        line("Календарь (gws)", False, "команда gws не установлена")

    ticktick_api = os.environ.get("TICKTICK_API_TOKEN", "").strip()
    ticktick_token = os.environ.get("TICKTICK_ACCESS_TOKEN", "").strip()
    ticktick = Path.home() / ".config/ticktick-mcp/config.json"
    if ticktick_api:
        line("TickTick", True, "постоянный TICKTICK_API_TOKEN")
    elif ticktick_token:
        refresh = all(os.environ.get(name, "").strip() for name in (
            "TICKTICK_REFRESH_TOKEN", "TICKTICK_CLIENT_ID", "TICKTICK_CLIENT_SECRET"))
        detail = ("OAuth с автоматическим обновлением" if refresh
                  else "access token без подтверждённого обновления")
        line("TickTick", True, detail)
    elif ticktick.is_file():
        try:
            data = json.loads(ticktick.read_text(encoding="utf-8"))
            present = bool(data.get("api_token") or data.get("access_token"))
            detail = ("постоянный API token" if data.get("api_token")
                      else "ключ без refresh_token — истекает 13.10.2026")
            line("TickTick", present, detail)
        except (OSError, json.JSONDecodeError) as exc:
            line("TickTick", False, f"файл доступа не прочитан: {exc}")
    else:
        line("TickTick", False,
             f"нет TICKTICK_API_TOKEN, TICKTICK_ACCESS_TOKEN и файла {ticktick}")

    line("Telegram", bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()),
         "переменная TELEGRAM_BOT_TOKEN")
    line("Владелец Telegram", bool(os.environ.get("TELEGRAM_ALLOWED_USER_ID", "").strip()),
         "переменная TELEGRAM_ALLOWED_USER_ID")
    line("Deepgram", bool(os.environ.get("DEEPGRAM_API_KEY", "").strip()),
         "переменная DEEPGRAM_API_KEY")

    health = Path(os.environ.get(
        "HEALTH_STORE_ROOT", str(Path.home() / "Projects/health-store")))
    line("Данные восстановления", health.is_dir(),
         "переменная HEALTH_STORE_ROOT; без неё ёмкость будет «неизвестна» — "
         "это правильный ответ, а не поломка")

    print("\nИТОГ")
    if all(ready):
        print("  Склад готов работать. Внешние доступы, помеченные НЕТ, "
              "не ломают склад — они обязаны называть себя вслух.")
        return 0
    print("  Склад работать не сможет: не хватает основы, смотри строки НЕТ выше.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
