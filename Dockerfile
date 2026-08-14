# Дежурный: телеграм-бот и слушатель. Мастерская остаётся на ноутбуке.
#
# Код в образе нужен только для запуска. Работает сервер не с ним, а с клоном
# склада в постоянном томе: единственная правда — origin, и код приезжает оттуда
# же вместе с данными. Иначе после каждой правки инструментов пришлось бы
# пересобирать образ, чтобы сервер узнал о ней.
FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates nodejs npm \
    && npm install -g @googleworkspace/cli@0.22.5 \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY start.sh ./
RUN chmod +x start.sh

# Вывод построчно и сразу: иначе логи Amvera показывают тишину до самого падения
ENV PYTHONUNBUFFERED=1

CMD ["/app/start.sh"]
