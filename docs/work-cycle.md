# Рабочий цикл: от входа до результата

Практическая инструкция по ежедневной работе. Обоснование решения —
[ADR 0006](adr/0006-live-work-cycle-and-honest-feedback.md), поля позиции — в
[схеме склада](storage-schema.md).

## 1. Открыть живой вход

```sh
make today
```

Команда читает `raw/` и `work/` в момент запуска и показывает в таком порядке:

1. неразобранный вход;
2. уже начатые обязательства;
3. ожидания;
4. кандидатов на свободные места.

Ёмкость по умолчанию берётся из данных о восстановлении. Если она неизвестна или
её нужно задать вручную:

```sh
make today CAP=full
make today CAP=half
make today CAP=low
```

Обычный запуск записывает показанные строки в `raw/log.md` как `представлен`.
Для проверки вывода без телеметрии:

```sh
PYTHONPATH=tools python3 tools/today.py --dry-run
```

Просмотр ничего не начинает автоматически.

## 2. Подтвердить переход

Путь к обязательству передаётся целиком, относительно корня склада.

| Действие | Команда | Что меняется |
|---|---|---|
| Взять | `take` | `open → in-progress`, один раз ставится `started` |
| Приостановить | `wait` | `in-progress → waiting`, нужна причина |
| Продолжить | `resume` | `waiting → in-progress`, исходный `started` сохраняется |
| Завершить | `finish` | `→ resolved`, ставятся `resolved` и `resolution` |
| Отменить | `cancel` | `→ cancelled`, нужны причина и `resolution` |

```sh
make work ACTION=take ITEM=work/programs/example/commitments/delo.md

make work ACTION=wait ITEM=work/programs/example/commitments/delo.md \
  REASON="ждём ответ клиента"

make work ACTION=resume ITEM=work/programs/example/commitments/delo.md

make work ACTION=finish ITEM=work/programs/example/commitments/delo.md \
  RESOLUTION=raw/sources/2026-07-31-result.md

make work ACTION=cancel ITEM=work/programs/example/commitments/delo.md \
  RESOLUTION=raw/sources/2026-07-31-cancellation.md \
  REASON="направление закрыто решением заказчика"
```

Результат должен существовать в `raw/` или `work/`. Начатое ожидание продолжает
занимать место в лимите незавершённого; перевод в `waiting` не освобождает слот.

## 3. Записать реакцию без изменения позиции

Когда строка не берётся в работу, ответ всё равно нужен самоанализу:

```sh
make work ACTION=defer ITEM=work/programs/example/commitments/delo.md \
  REASON="не сегодня"
make work ACTION=dismiss ITEM=work/programs/example/commitments/delo.md \
  REASON="не относится к текущему фокусу"
make work ACTION=correct ITEM=work/programs/example/commitments/delo.md \
  REASON="срок уже согласован на следующую неделю"
```

`defer`, `dismiss` и `correct` добавляют только событие `реакция`; статус и поля
позиции не меняются.

## 4. Пересчитать виды и проверить склад

```sh
make attention   # диагностический обзор сигналов
make reflect     # реакции и показатели потока
make index       # навигация по папкам
make gates       # сначала подставные случаи, затем живой склад
```

`attention`, `self-review` и индексы — производные файлы в `wiki/`; руками их
не правят. Источник рабочего состояния — позиции в `work/`, источник событий —
`raw/`.
