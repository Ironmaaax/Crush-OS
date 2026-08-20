.PHONY: boot run voice livekit start lint test typecheck

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
	@echo "Démarrage Crush (LiveKit + API + Voice)..."
	@trap 'kill $$(jobs -p) 2>/dev/null; exit 0' INT TERM; \
	livekit-server --dev --node-ip 127.0.0.1 --keys "devkey: devsecretdevsecretdevsecretdevsecret" & \
	sleep 2 && uv run python -m crush.app & \
	sleep 4 && uv run python -m crush.interfaces.voice.agent dev; \
	wait

invoque:
	@bash setup.sh

run:
	@uv run python -m crush.app

livekit:
	@echo "Démarrage LiveKit local sur ws://localhost:7880"
	@livekit-server --dev --node-ip 127.0.0.1 --keys "devkey: devsecretdevsecretdevsecretdevsecret"

voice:
	@uv run python -m crush.interfaces.voice.agent dev
