.PHONY: boot run start lint test typecheck

# ── Garde-fous architecture (Phase F) ────────────────────────────────
# `make lint` : ruff + import-linter (contrat de couches CDC §2.2).
# `make typecheck` : mypy scopé kernel + conformité Protocols (F.1.3bis).
# `make test` : suite pytest (unit + integration).
lint:
	@uv run ruff check
	@uv run lint-imports

typecheck:
	@uv run mypy

test:
	@uv run pytest -q

start:
	@echo "Démarrage Crush (API, vocal inclus sur /ws/voice)..."
	@uv run python -m crush.app

invoque:
	@bash setup.sh

run:
	@uv run python -m crush.app

