#!/bin/sh
# Запуск дежурного: склад берётся из origin, а не из образа.
#
# Порядок намеренно осторожный. Сервер никогда не выбрасывает то, что не успел
# отправить: если история разошлась, он останавливается и говорит об этом, а не
# делает reset --hard. Две правды об одном обязательстве — худшее, что может
# случиться со складом; молча выбранная версия и есть вторая правда.
set -eu

DIR=/data/workhub

if [ -z "${WORKHUB_REPO:-}" ]; then
    echo "НЕТ WORKHUB_REPO: адрес склада с доступом на запись не задан" >&2
    exit 2
fi
if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
    echo "НЕТ TELEGRAM_BOT_TOKEN: разговаривать нечем" >&2
    exit 2
fi

# Доступ к складу — ключом развёртывания, привязанным к одному репозиторию.
# Токен учётной записи сюда не кладётся: у него права на всё, а дежурному нужен
# ровно этот склад и ничего больше. Ключ приезжает в base64 одной строкой —
# многострочные значения панели окружения переносят по-разному.
if [ -n "${WORKHUB_DEPLOY_KEY:-}" ]; then
    mkdir -p /root/.ssh && chmod 700 /root/.ssh
    printf '%s' "$WORKHUB_DEPLOY_KEY" | base64 -d > /root/.ssh/id_ed25519
    chmod 600 /root/.ssh/id_ed25519
    GIT_SSH_COMMAND="ssh -i /root/.ssh/id_ed25519 -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
    export GIT_SSH_COMMAND
fi

# Экспорт ``gws auth export --unmasked`` содержит refresh token и сам обновляет
# короткий access token. В панели он хранится одной base64-строкой; на диске —
# только в постоянном томе вне git.
if [ -n "${GOOGLE_WORKSPACE_CREDENTIALS_B64:-}" ]; then
    mkdir -p /data/gws
    printf '%s' "$GOOGLE_WORKSPACE_CREDENTIALS_B64" | base64 -d > /data/gws/credentials.json
    chmod 600 /data/gws/credentials.json
    GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=/data/gws/credentials.json
    GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file
    export GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND
fi

if [ ! -d "$DIR/.git" ]; then
    echo "склада в томе нет — клонирую из origin"
    git clone --quiet "$WORKHUB_REPO" "$DIR"
fi

cd "$DIR"
git remote set-url origin "$WORKHUB_REPO"
git config user.name "workhub server"
git config user.email "server@workhub.local"

# Отстать от origin дежурный не имеет права: он отвечает человеку по складу.
# Не сошлось — работаем на том, что есть, и говорим об этом вслух.
if ! git fetch --quiet origin 2>/dev/null; then
    echo "ВНИМАНИЕ: origin недоступен — работаю на последнем известном складе"
elif ! git rebase --quiet origin/master 2>/dev/null; then
    git rebase --abort 2>/dev/null || true
    echo "ВНИМАНИЕ: история разошлась — не сливаю, нужен человек"
fi

echo "дежурный поднят: $(git log --oneline -1)"
exec env PYTHONPATH="$DIR/tools" python3 "$DIR/tools/duty.py" --root "$DIR"
