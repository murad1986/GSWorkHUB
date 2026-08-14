#!/usr/bin/env bash
# Полный обход внешних источников: то, что умеет склад сам, плюс то, что умеет
# только агент через свои MCP-серверы (диктофон, переписка, документы, доски).
#
# Запускается руками — `make context` — и по расписанию три раза в сутки.
# Повторный запуск безопасен: приём отбрасывает уже записанные версии по паре
# «источник + идентификатор», поэтому лишний заход не создаёт вторых копий.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

LOG_DIR="${GSWORKHUB_LOG_DIR:-$HOME/Library/Logs/GSWorkHUB}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/context.log"
say() { printf '%s · %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" | tee -a "$LOG"; }

say "обход начат"

# 1. Источники, у которых есть собственный коннектор. Каждый отвечает за себя и
#    молчит, если доступа нет: отсутствие ключа не должно ронять весь обход.
if [ "$DRY" = "0" ]; then
    make -s sources >>"$LOG" 2>&1 || say "сверка задач и календаря не прошла — см. лог"
    make -s granola >>"$LOG" 2>&1 || true
fi

# 2. Источники, доступные только агенту через MCP. Промпт лежит в репозитории и
#    правится как обычный файл — расписание его не хранит.
PROMPT="$ROOT/docs/prompts/sbor-konteksta.md"
AGENT="${GSWORKHUB_AGENT:-codex}"
if [ ! -f "$PROMPT" ]; then
    say "нет промпта $PROMPT — шаг агента пропущен"
elif ! command -v "$AGENT" >/dev/null 2>&1; then
    say "агент $AGENT не найден в PATH — шаг агента пропущен"
elif [ "$DRY" = "1" ]; then
    say "dry-run: агент $AGENT не запускался"
else
    say "агент $AGENT собирает контекст по промпту"
    "$AGENT" exec --cd "$ROOT" "$(cat "$PROMPT")" >>"$LOG" 2>&1 \
        || say "агент завершился с ошибкой — см. лог"
fi

# 3. Пересборка видов и проверка схемы. Красный линт после обхода — сигнал
#    человеку, а не повод молча продолжать.
if [ "$DRY" = "0" ]; then
    make -s index >>"$LOG" 2>&1 || true
    make -s attention >>"$LOG" 2>&1 || true
    if make -s lint >>"$LOG" 2>&1; then
        say "обход закончен, склад соответствует схеме"
    else
        say "ВНИМАНИЕ: линт красный после обхода — разобрать: make lint"
    fi
else
    say "dry-run закончен"
fi
