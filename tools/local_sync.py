#!/usr/bin/env python3
"""Автозапуск локальной сверки склада, TickTick и Google Calendar на macOS."""

from __future__ import annotations

import argparse
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

LABEL = "com.gsworkhub.sources"


class LocalSyncError(RuntimeError):
    """Локальный фоновый процесс не удалось настроить или прочитать."""


def paths() -> tuple[Path, Path]:
    agent = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    logs = Path.home() / "Library" / "Logs" / "GSWorkHUB"
    return agent, logs


def domain() -> str:
    return f"gui/{os.getuid()}"


def agent_definition(root: Path, *, python: str | None = None,
                     gws: str | None = None,
                     log_dir: Path | None = None,
                     interval: int = 3600) -> dict:
    """Возвращает полное описание пользовательского процесса launchd."""
    python = python or sys.executable
    gws = gws or shutil.which("gws")
    if not gws:
        raise LocalSyncError("Google-клиент gws не найден в PATH")
    _, default_logs = paths()
    log_dir = log_dir or default_logs
    path_parts = [
        str(Path(python).parent), str(Path(gws).parent),
        "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin",
    ]
    executable_path = ":".join(dict.fromkeys(path_parts))
    return {
        "Label": LABEL,
        "ProgramArguments": [
            python, str(root / "tools" / "source_sync.py"),
            "--watch", "--interval", str(max(30, interval)),
            "--root", str(root),
        ],
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {
            "PATH": executable_path,
            "PYTHONPATH": str(root / "tools"),
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 30,
        "StandardOutPath": str(log_dir / "source-sync.log"),
        "StandardErrorPath": str(log_dir / "source-sync.error.log"),
    }


def launchctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["launchctl", *arguments], capture_output=True, text=True)
    if check and proc.returncode:
        reason = proc.stderr.strip() or proc.stdout.strip() or "неизвестная ошибка"
        raise LocalSyncError(reason)
    return proc


def loaded() -> bool:
    return launchctl("print", f"{domain()}/{LABEL}", check=False).returncode == 0


def wait_until_running(timeout: float = 10) -> None:
    """launchctl возвращает управление чуть раньше фактического старта."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if status()[0]:
            return
        time.sleep(0.25)


def install(root: Path, interval: int) -> None:
    agent, logs = paths()
    agent.parent.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    definition = agent_definition(root, log_dir=logs, interval=interval)
    agent.write_bytes(plistlib.dumps(definition, fmt=plistlib.FMT_XML, sort_keys=False))
    if loaded():
        launchctl("bootout", f"{domain()}/{LABEL}")
    launchctl("bootstrap", domain(), str(agent))
    launchctl("enable", f"{domain()}/{LABEL}")
    launchctl("kickstart", "-k", f"{domain()}/{LABEL}")
    wait_until_running()


def start() -> None:
    agent, _ = paths()
    if not agent.is_file():
        raise LocalSyncError("автозапуск ещё не установлен")
    if not loaded():
        launchctl("bootstrap", domain(), str(agent))
    launchctl("enable", f"{domain()}/{LABEL}")
    launchctl("kickstart", "-k", f"{domain()}/{LABEL}")
    wait_until_running()


def stop() -> None:
    if loaded():
        launchctl("bootout", f"{domain()}/{LABEL}")


def status() -> tuple[bool, str]:
    proc = launchctl("print", f"{domain()}/{LABEL}", check=False)
    if proc.returncode:
        return False, "локальная автосверка не запущена"
    state = re.search(r"\bstate = (\w+)", proc.stdout)
    pid = re.search(r"\bpid = (\d+)", proc.stdout)
    if state and state.group(1) == "running":
        suffix = f", процесс {pid.group(1)}" if pid else ""
        return True, f"локальная автосверка работает{suffix}"
    return False, "автозапуск установлен, но процесс сейчас не работает"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="управлять локальной автосверкой через launchd")
    parser.add_argument("action", choices=("install", "start", "stop", "status"),
                        nargs="?", default="status")
    parser.add_argument("--root", type=Path,
                        default=Path(__file__).resolve().parents[1])
    parser.add_argument("--interval", type=int, default=3600)
    args = parser.parse_args(argv)
    try:
        if args.action == "install":
            install(args.root.resolve(), args.interval)
        elif args.action == "start":
            start()
        elif args.action == "stop":
            stop()
        ok, message = status()
    except (LocalSyncError, OSError) as exc:
        print(f"Локальная автосверка не настроена: {exc}")
        return 2
    print(message.capitalize() + ".")
    return 0 if ok or args.action == "stop" else 1


if __name__ == "__main__":
    raise SystemExit(main())
