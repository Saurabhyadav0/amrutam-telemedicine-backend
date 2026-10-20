.PHONY: env install up down migrate run test lint fmt typecheck security admin seed-load load openapi tf-check smoke

env:        ## Generate .env with fresh secrets
	uv run python scripts/generate_env.py
install:
	uv sync
up:         ## Full stack: postgres, redis, api, jaeger, prometheus, grafana
	docker compose up -d --build
down:
	docker compose down
migrate:
	uv run alembic upgrade head
run:        ## API on the host (needs `docker compose up -d postgres redis`)
	uv run uvicorn app.main:create_app --factory --reload
test:
	uv run pytest --cov=app --cov-report=term-missing
lint:
	uv run ruff check . && uv run ruff format --check .
fmt:
	uv run ruff check --fix . && uv run ruff format .
typecheck:
	uv run mypy app
security:
	uv run bandit -q -r app && uv run pip-audit
admin:      ## Create an admin user: make admin EMAIL=you@example.com
	uv run python -m app.cli create-admin --email $(EMAIL)
seed-load:  ## 5k doctors / 300k slots into the local compose DB (for search + load tests)
	docker compose exec -T postgres psql -U amrutam -d amrutam -q -f - < scripts/seed_search_load.sql
load:       ## k6 peak-hour mix against the compose stack (READ_RPS, BOOK_RPS, DURATION overridable)
	docker run --rm -i --network amrutam_default -v "$(PWD)/tests/load:/scripts:ro" \
	  -e BASE_URL=http://gateway:8000 -e READ_RPS=$${READ_RPS:-150} -e BOOK_RPS=$${BOOK_RPS:-15} \
	  -e DURATION=$${DURATION:-60s} grafana/k6:0.54.0 run /scripts/booking_mix.js
openapi:    ## Regenerate docs/openapi.yaml from the code
	uv run python scripts/export_openapi.py
tf-check:   ## terraform fmt/validate + checkov (Docker; no AWS credentials needed)
	docker run --rm -v "$(PWD)/infra/terraform:/tf" -w /tf hashicorp/terraform:1.9.8 fmt -check -recursive
	docker run --rm -v "$(PWD)/infra/terraform:/tf" -w /tf hashicorp/terraform:1.9.8 init -backend=false -input=false >/dev/null
	docker run --rm -v "$(PWD)/infra/terraform:/tf" -w /tf hashicorp/terraform:1.9.8 validate
	docker run --rm -v "$(PWD)/infra/terraform:/tf" bridgecrew/checkov:3.2.250 -d /tf --framework terraform --quiet --compact
smoke:      ## End-to-end smoke test (42 checks) against the running compose stack
	uv run python scripts/e2e_smoke.py
