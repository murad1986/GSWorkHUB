#!/usr/bin/env python3
"""Один дежурный держит разговорный вход и сверку внешних источников.

Оба процесса одинаково обязательны. Если один молча остановился, дежурный
завершает второй и выходит с ошибкой: платформа перезапустит весь контур, а в
журнале не останется ложного впечатления, что система продолжает работать.
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


class DutyError(RuntimeError):
    """Один из обязательных процессов остановился."""


@dataclass(frozen=True)
class Service:
    name: str
    command: tuple[str, ...]


@dataclass
class Running:
    service: Service
    process: subprocess.Popen


def services(root: Path, interval: int = 300) -> tuple[Service, ...]:
    tools = root / "tools"
    return (
        Service("Telegram", (
            sys.executable, str(tools / "telegram_bot.py"), "poll",
            "--root", str(root),
        )),
        Service("Сверка источников", (
            sys.executable, str(tools / "source_sync.py"), "--watch",
            "--interval", str(interval), "--root", str(root),
        )),
    )


def failed(running: list[Running]) -> tuple[str, int] | None:
    """Возвращает первый остановившийся обязательный процесс."""
    for child in running:
        code = child.process.poll()
        if code is not None:
            return child.service.name, code
    return None


def stop_all(running: list[Running]) -> None:
    for child in running:
        if child.process.poll() is None:
            child.process.terminate()
    for child in running:
        try:
            child.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.process.kill()
            child.process.wait(timeout=5)


def serve(root: Path, interval: int = 300, *,
          popen: Callable[..., subprocess.Popen] = subprocess.Popen) -> None:
    running: list[Running] = []
    try:
        for service in services(root, interval):
            running.append(Running(service, popen(list(service.command))))
            print(f"запущено: {service.name}", flush=True)
        while True:
            stopped = failed(running)
            if stopped is not None:
                name, code = stopped
                raise DutyError(f"{name} остановился, код {code}")
            time.sleep(1)
    finally:
        stop_all(running)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="постоянный дежурный workhub")
    parser.add_argument("--root", type=Path,
                        default=Path(__file__).resolve().parents[1])
    parser.add_argument("--interval", type=int, default=300,
                        help="секунд между сверками внешних источников")
    args = parser.parse_args(argv)

    def stop(_signum, _frame) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        serve(args.root.resolve(), max(30, args.interval))
    except KeyboardInterrupt:
        return 0
    except DutyError as exc:
        print(f"дежурный остановлен: {exc}", file=sys.stderr, flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
