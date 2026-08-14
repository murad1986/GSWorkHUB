# Автосбор контекста: три захода в сутки

Склад умеет сам ходить в календарь — у него есть собственный коннектор. Всё
остальное — диктофон Plaud, переписка, документы, задачи, доски — доступно
только агенту через MCP-серверы. Этот документ описывает, как обе половины
запускаются одной командой и как поставить их на расписание.

## Одна команда

```bash
make context
```

`tools/context_sweep.sh` делает три шага подряд:

1. **Календарь** — `make calendar`. Нет доступа — шаг молчит и не роняет
   остальной обход.
2. **Агент** — запускает `codex exec` с промптом
   [prompts/sbor-konteksta.md](prompts/sbor-konteksta.md). Агент перечисляет
   свои MCP-серверы, забирает новое и передаёт его в приём.
3. **Пересборка и проверка** — `make index`, `make attention`, `make lint`.
   Красный линт после обхода пишется в лог отдельной строкой «ВНИМАНИЕ»: обход
   не имеет права молча оставить склад в нарушении схемы.

Лог — `~/Library/Logs/GSWorkHUB/context.log`. Проверка вхолостую, без записи и
без агента: `make context DRY=1`.

Агент запускается так: `codex exec --sandbox workspace-write --cd <корень>
"<текст промпта>"`. Права на запись нужны, чтобы принесённое легло в
`raw/inbox/` через приём; каталог ограничен корнем склада, наружу агент не
пишет по контракту. Другие флаги — переменная `GSWORKHUB_AGENT_ARGS`, другой
агент — `GSWORKHUB_AGENT` (например `GSWORKHUB_AGENT=claude`).

## Почему три раза в сутки, а не постоянно

Разговор, записанный утром, нужен к дневной встрече, а не через неделю. Но
непрерывное слежение за десятком источников стоит внимания и трафика, а
выигрывает минуты. Три захода закрывают рабочий день: перед началом, в
середине, после его окончания.

Повторный заход безопасен. Приём отбрасывает уже записанную версию по паре
«источник + идентификатор» (`source` + `source_ref`), поэтому лишний запуск не
создаёт вторых копий — это проверяется мета-гейтом, случай «повторный запуск
приёма не создаёт второй копии».

## Расписание на macOS

Файл `~/Library/LaunchAgents/com.gsworkhub.context.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.gsworkhub.context</string>
  <key>ProgramArguments</key>
  <array>
    <string>/ПУТЬ/К/GSWorkHUB/tools/context_sweep.sh</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/ПУТЬ/К/GSWorkHUB</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/Users/ИМЯ/.local/bin</string>
  </dict>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>14</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>21</integer><key>Minute</key><integer>0</integer></dict>
  </array>
  <key>StandardOutPath</key>
  <string>/Users/ИМЯ/Library/Logs/GSWorkHUB/context.out.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/ИМЯ/Library/Logs/GSWorkHUB/context.err.log</string>
</dict>
</plist>
```

Заменить `/ПУТЬ/К/GSWorkHUB` и `ИМЯ`, затем:

```bash
mkdir -p ~/Library/Logs/GSWorkHUB
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.gsworkhub.context.plist
launchctl kickstart -p gui/$(id -u)/com.gsworkhub.context   # проверить сразу
launchctl print gui/$(id -u)/com.gsworkhub.context | head -20
```

Снять с расписания: `launchctl bootout gui/$(id -u)/com.gsworkhub.context`.

Ноутбук спал в назначенный час — launchd запустит заход при пробуждении, и это
правильно: пропущенный обход дороже сдвинутого.

## Расписание на сервере Linux

```cron
0 9,14,21 * * * cd /opt/gsworkhub && /opt/gsworkhub/tools/context_sweep.sh >> /var/log/gsworkhub-context.log 2>&1
```

Агент на сервере должен быть авторизован тем же аккаунтом и видеть те же
MCP-серверы, иначе половина источников окажется недоступна — обход это скажет,
но данных не принесёт.

## Что делать с принесённым

Обход только приносит. Дальше — обычный рабочий вход:

```bash
make today
```

Неразобранное показывается первым; разбор идёт по одному источнику, и его
результат — записи в `work/`, а не в `raw/`. Через `intake.pending_days` в
`config/attention.yml` задаётся, сколько дней неразобранное лежит тихо, прежде
чем попроситься в экран внимания.

Накопилось больше, чем разбирается, — это не повод чистить `raw/`. События не
удаляются; разбирается то, что нужно, остальное остаётся историей и находится
поиском.
