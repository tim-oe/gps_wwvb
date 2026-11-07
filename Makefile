# Host details. Override on the command line: make remote PI_HOST=other ARGS='status'
PI_HOST ?= tec-pi-wwv
PI_DEV_DIR ?= ~/src/gps_wwvb-dev
PI_REL_DIR ?= ~/src/gps_wwvb
DURATION ?= 60
ARGS ?= run

.PHONY: help test coverage lint format requirements run-sim dev-setup dev-deps gnss-setup sync remote watch \
	fetch-recordings debug build deploy remote-status logs svc-start svc-stop

help:
	@echo "Local:   make test | coverage | lint | format | requirements | run-sim"
	@echo "Pi dev:  make dev-setup | dev-deps | gnss-setup | sync | remote ARGS='status' | watch | fetch-recordings | debug ARGS='status'"
	@echo "Release: make build | deploy | remote-status | logs | svc-start | svc-stop"

test:
	poetry run pytest

coverage: test
	poetry run coverage report
	@echo "HTML report: reports/coverage/index.html"

lint:
	poetry run ruff check src tests
	poetry run ruff format --check src tests
	poetry run mypy src
	poetry run pylint src tests

format:
	poetry run ruff check --fix src tests
	poetry run ruff format src tests

requirements:
	poetry run python scripts/export_requirements.py

run-sim:
	poetry run gps-wwvb --config gps_wwvb.toml run --simulate

dev-setup:
	ssh -o BatchMode=yes $(PI_HOST) 'bash -s -- --deps-only' < deploy/install_on_pi.sh
	bash scripts/sync_dev.sh $(PI_HOST) $(PI_DEV_DIR) --full
	ssh -o BatchMode=yes $(PI_HOST) 'cd $(PI_DEV_DIR) && python3 -m venv --system-site-packages .venv && .venv/bin/pip install --disable-pip-version-check --require-hashes -r requirements.txt && .venv/bin/pip install --disable-pip-version-check --no-deps es100-wwvb==0.4.4 && .venv/bin/pip install --disable-pip-version-check --no-deps -e . && .venv/bin/pip install --disable-pip-version-check debugpy'

dev-deps: sync
	ssh -o BatchMode=yes $(PI_HOST) 'cd $(PI_DEV_DIR) && if [[ ! -x .venv/bin/pip ]]; then echo "No dev venv. Run make dev-setup first." >&2; exit 1; fi && .venv/bin/pip install --disable-pip-version-check --require-hashes -r requirements.txt && .venv/bin/pip install --disable-pip-version-check --no-deps -e .'

gnss-setup:
	ssh -o BatchMode=yes $(PI_HOST) 'set -e; trap "sudo systemctl start gpsd.socket gpsd.service" EXIT; sudo systemctl stop gpsd.socket gpsd.service; sudo ubxtool -f /dev/ttyS0 -s 9600 -e GALILEO; sudo ubxtool -f /dev/ttyS0 -s 9600 -p CFG-GNSS; sudo ubxtool -f /dev/ttyS0 -s 9600 -p SAVE'

sync:
	bash scripts/sync_dev.sh $(PI_HOST) $(PI_DEV_DIR)

remote: sync
	ssh -o BatchMode=yes $(PI_HOST) 'sudo systemctl stop gps-wwvb.service || true; echo "Stopped gps-wwvb.service. Restart it with: make svc-start"'
	ssh -t $(PI_HOST) 'cd $(PI_DEV_DIR) && exec .venv/bin/gps-wwvb --config gps_wwvb.toml $(ARGS)'

watch:
	@if command -v watchexec >/dev/null 2>&1; then \
		watchexec -w src -w gps_wwvb.toml -- $(MAKE) sync; \
	else \
		echo "watchexec is not installed; syncing every 2s (Ctrl-C to stop)"; \
		while true; do $(MAKE) sync; sleep 2; done; \
	fi

fetch-recordings: sync
	ssh -o BatchMode=yes $(PI_HOST) 'sudo systemctl stop gps-wwvb.service || true; echo "Stopped gps-wwvb.service. Restart it with: make svc-start"'
	@stamp=$$(date -u +%Y%m%dT%H%M%SZ); \
	remote=/tmp/gps_wwvb-rec-$$stamp; \
	ssh -o BatchMode=yes $(PI_HOST) "mkdir -p $$remote && cd $(PI_DEV_DIR) && .venv/bin/gps-wwvb --config gps_wwvb.toml record --out $$remote --duration $(DURATION)"; \
	mkdir -p tests/data/$$stamp; \
	if command -v rsync >/dev/null 2>&1; then \
		rsync -az $(PI_HOST):$$remote/ tests/data/$$stamp/; \
	else \
		ssh -o BatchMode=yes $(PI_HOST) "tar -C $$remote -cf - ." | tar -C tests/data/$$stamp -xf -; \
	fi; \
	echo "saved tests/data/$$stamp"

debug: sync
	ssh -o BatchMode=yes $(PI_HOST) 'sudo systemctl stop gps-wwvb.service || true; echo "Stopped gps-wwvb.service. Restart it with: make svc-start"'
	ssh -L 5678:127.0.0.1:5678 -t $(PI_HOST) 'cd $(PI_DEV_DIR) && exec .venv/bin/python -m debugpy --listen 127.0.0.1:5678 --wait-for-client -m gps_wwvb.cli --config gps_wwvb.toml $(ARGS)'

build:
	poetry build

deploy:
	PI_HOST="$(PI_HOST)" PI_REL_DIR="$(PI_REL_DIR)" bash scripts/deploy.sh

remote-status:
	ssh $(PI_HOST) 'systemctl status gps-wwvb --no-pager'

logs:
	ssh -t $(PI_HOST) 'journalctl -u gps-wwvb -f'

svc-start:
	ssh $(PI_HOST) 'sudo systemctl start gps-wwvb'

svc-stop:
	ssh $(PI_HOST) 'sudo systemctl stop gps-wwvb'
