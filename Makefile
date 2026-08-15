# Every target goes through `uv run`, which uses the project venv and can never
# fall back to a system or conda Python. Use these rather than bare `uvicorn` /
# `pytest` commands.
# Override with e.g. `make dev PORT=8001` if something is stuck on 8000.
PORT ?= 8000

.PHONY: install dev serve test lint check env clean ui up down logs images

install:                ## create .venv and install from uv.lock
	uv sync

dev:                    ## run the API with auto-reload
	uv run uvicorn app.main:app --reload --reload-dir app --port $(PORT)

serve:                  ## run the API without auto-reload
	uv run uvicorn app.main:app --port $(PORT)

test:
	uv run pytest

lint:
	uv run ruff check .

check: lint test

env:                    ## show which interpreter and versions are actually in use
	@uv run python -c "import sys, fastapi, starlette; \
	print('python   ', sys.version.split()[0]); \
	print('exe      ', sys.executable); \
	print('fastapi  ', fastapi.__version__); \
	print('starlette', starlette.__version__)"

ui:                     ## run the Next.js UI in dev mode on :3000
	cd frontend && npm install --silent && npm run dev

up:                     ## whole app in Docker — UI on :3000, API on :8000
	docker compose up --build -d
	@echo "UI  -> http://localhost:3000"
	@echo "API -> http://localhost:8000/docs"

down:
	docker compose down

logs:
	docker compose logs -f

images:                 ## build both deployment images without running them
	docker build -t travel-api:local .
	cd frontend && docker build --build-arg NEXT_PUBLIC_API_BASE_URL=http://localhost:8000 -t travel-ui:local .

clean:
	rm -rf .venv .pytest_cache .ruff_cache
