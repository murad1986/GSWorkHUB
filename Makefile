# Единственное «готово» для склада.
PY ?= python3

gates: verify lint

sync: ## обмен с сервером (make sync ACTION=pull|push|status|presence)
	@PYTHONPATH=tools $(PY) tools/sync.py $(or $(ACTION),status) $(if $(MESSAGE),--message "$(MESSAGE)")

check-env: ## что на этой машине есть, чего нет — гонять до переноса
	@$(PY) tools/check_env.py

welcome: ## состояние склада одним экраном — с него начинается сессия
	@PYTHONPATH=tools $(PY) tools/welcome.py

today: ## живой короткий вход из raw/work (make today CAP=full|half|low)
	@PYTHONPATH=tools $(PY) tools/today.py --capacity $(or $(CAP),auto)

work: ## переход обязательства (make work ACTION=take ITEM=work/.../x.md [UNTIL=ГГГГ-ММ-ДД])
	@PYTHONPATH=tools $(PY) tools/workflow.py $(ACTION) $(ITEM) $(if $(RESOLUTION),--resolution "$(RESOLUTION)") $(if $(REASON),--reason "$(REASON)") $(if $(UNTIL),--until $(UNTIL))

telegram: ## постоянный разговорный вход (make telegram ACTION=check|poll)
	@PYTHONPATH=tools $(PY) tools/telegram_bot.py $(or $(ACTION),check) $(if $(ONCE),--once)

attention: ## пересобрать экран внимания
	$(PY) tools/attention.py

index: ## пересобрать навигацию по складу
	PYTHONPATH=tools $(PY) tools/index.py

capture: ## принять внешние записи из JSON (make capture FILE=vhod.json [DRY=1])
	@PYTHONPATH=tools $(PY) tools/capture.py $(if $(FILE),--file $(FILE)) $(if $(DRY),--dry-run)

context: ## полный обход внешних источников агентом (make context [DRY=1])
	@tools/context_sweep.sh $(if $(DRY),--dry-run)

reflect: ## самоанализ: работает ли система (make reflect TOUCHES=1 — одной строкой о заходах)
	$(PY) tools/reflect.py $(if $(TOUCHES),--touches)

touch: ## отметить состоявшийся заход (make touch KIND=morning|evening)
	$(PY) tools/activity.py $(or $(KIND),morning)

harvest: ## что ещё есть в закрытой работе (make harvest ITEM=… | DAYS=7)
	@PYTHONPATH=tools $(PY) tools/harvest.py $(if $(ITEM),--item $(ITEM)) $(if $(DAYS),--days $(DAYS))

calendar: ## принять новые и изменённые события календаря в raw/inbox
	@PYTHONPATH=tools $(PY) tools/agenda.py $(if $(DAYS),--days $(DAYS))

local-sync: ## фоновый приём календаря на macOS (ACTION=install|start|stop|status)
	@PYTHONPATH=tools $(PY) tools/local_sync.py $(or $(ACTION),status) $(if $(INTERVAL),--interval $(INTERVAL))

advice: ## история советов роли (make advice KIND=coach LIST=1 | KIND=coach TYPE=… ITEM=… CONTEXT=… BASIS=… TEXT=…)
	@PYTHONPATH=tools $(PY) tools/advice.py $(KIND) $(if $(LIST),--list) $(if $(ITEM),--item $(ITEM)) $(if $(TYPE),--type "$(TYPE)") $(if $(CONTEXT),--context "$(CONTEXT)") $(if $(BASIS),--basis "$(BASIS)") $(if $(TEXT),--text "$(TEXT)")

brief: ## досье к интервью (make brief C=work/clients/alfa)
	PYTHONPATH=tools $(PY) tools/interview.py brief --container $(C)

track: ## что взять сейчас (make track CAP=auto|full|half|low)
	PYTHONPATH=tools $(PY) tools/tracker.py all --capacity $(or $(CAP),auto)

capacity: ## ёмкость дня по данным о восстановлении
	PYTHONPATH=tools $(PY) tools/capacity.py ## порядок важен: сначала проверяем проверку, потом склад

policy: ## что уместно предлагать сегодня (make policy CLASS=research CAP=auto|full|half|low)
	@PYTHONPATH=tools $(PY) tools/policy.py $(CLASS) --capacity $(or $(CAP),auto)

intervene: ## что требует вмешательства сейчас (make intervene CAP=auto|full|half|low)
	@PYTHONPATH=tools $(PY) tools/interventions.py --capacity $(or $(CAP),auto)

lint: ## склад соответствует схеме
	$(PY) tools/lint.py

verify: ## проверки ловят то, ради чего написаны
	PYTHONPATH=tools $(PY) tools/verify.py

.PHONY: gates capture context sync check-env lint verify attention index reflect touch advice calendar local-sync harvest track brief capacity policy intervene welcome today work telegram
