# Changelog — Crush V3

---

## Phase 1 — Fondation
**Date :** 2026-04-23

### Ce qui marche
- Structure de projet complète (tous les répertoires et stubs des phases 2–9)
- `uv` configuré, venv Python 3.11, toutes les dépendances installées
- `config/settings.py` — pydantic-settings, chargement `.env`, singleton `settings`
- `llm/base.py` — interface `LLMProvider` abstraite
- `llm/api.py` — `AnthropicProvider` + `MistralProvider` (stream + completion)
- `llm/local.py` — `OllamaProvider` via HTTP direct
- `llm/factory.py` — factory auto selon `LLM_PROVIDER` dans `.env`
- `main.py` — FastAPI avec lifespan, routes `/health` et `/ws` (stub echo)
- `scripts/test_llm.py` — script CLI pour tester `LLMProvider.complete()` avec `--stream` et `--provider`
- `prompts/system_static.md` — prompt système V1 avec règles de routing
- `memory_data/` — structure initiale avec `MEMORY.md` et fichiers thématiques de base
- `ruff check .` — 0 warning
- 5 tests pytest passent

### Critères de validation ✅
- [x] `uv run python main.py` démarre le serveur
- [x] `curl localhost:8000/health` → `{"status":"ok","version":"0.1.0"}`
- [x] `scripts/test_llm.py` appelle `LLMProvider.complete()` (avec clé API configurée)
- [x] `ruff check .` passe sans warning

### Dette technique
- `llm/api.py` : le streaming Mistral utilise `stream_async` — à valider contre la vraie API une fois la clé configurée
- `api/websocket.py` : stub echo uniquement, connexion réelle implémentée en Phase 2
- Aucune annotation de type ANN sur les tests (acceptable pour le dev)

### Problèmes rencontrés
- Mistral SDK v2 : import depuis `mistralai.client` et non `mistralai` directement
- `on_event` FastAPI déprécié → migré vers `lifespan` contextmanager
- Règles ruff `ANN101`/`ANN102` supprimées dans ruff 0.15 → retirées de la config

---

## Phase 2 — Conversation core
**Date :** 2026-04-23

### Ce qui marche
- `core/session.py` — `Session` (dataclass) + `SessionManager` (registre in-memory)
- `core/agent.py` — `Agent` : charge le prompt système, appelle le LLM, gère stream/no-stream
- `core/gateway.py` — `Gateway` : normalise l'input, gère la session, fallback vocal sur erreur
- `api/websocket.py` — WebSocket `/ws` complet : protocole JSON, streaming par chunks, reconnexion client
- `main.py` — lifespan initialise gateway + agent + session_manager dans `app.state`
- `ui/static/index.html` — UI chat minimaliste dark-mode, streaming temps réel, auto-resize textarea
- 14 tests pytest passent (4 gateway, 5 session, 3 settings, 2 llm_base)

### Critères de validation ✅
- [x] `localhost:8000` → UI chat accessible
- [x] Message envoyé → réponse streamée en temps réel
- [x] Conversation multi-tour cohérente (session_id réutilisé, historique maintenu)
- [x] `ruff check .` → 0 warning
- [x] 14/14 tests passent

### Dette technique
- Sessions en mémoire uniquement — perdues au redémarrage (Phase 3 : JSONL persist)
- Pas de streaming audio (Phase 6)
- Pas de Speed Router — toutes les requêtes passent en mode direct (Phase 4)
- Pas de gestion de sessions multiples dans l'UI (Phase 8)

### Problèmes rencontrés
- `timezone.utc` → migré vers `datetime.UTC` (Python 3.11+, ruff UP017)
- Import order ruff : `_FALLBACK` avant `Gateway` dans websocket.py

---

## Phase 3 — Mémoire 3 couches
**Date :** 2026-04-23

### Ce qui marche
- `memory/sessions.py` — SessionStore append-only JSONL, restauration par session_id au redémarrage
- `memory/index.py` — MemoryIndex : lecture/écriture MEMORY.md, mise à jour de pointeurs existants
- `memory/topics.py` — TopicStore : lecture, écriture, liste des fichiers thématiques
- `memory/consolidation.py` — ConsolidationAgent fire-and-forget : extrait les faits d'un échange, met à jour topics + MEMORY.md via LLM
- `core/session.py` — callback persist sur add_message, restauration depuis JSONL dans SessionManager
- `core/agent.py` — prompt dynamique : MEMORY.md + tous les fichiers thématiques injectés à chaque requête
- `api/websocket.py` — fire consolidation après chaque échange complet
- `main.py` — câblage complet SessionStore + MemoryIndex + TopicStore + ConsolidationAgent
- `ui/static/index.html` — session_id persisté en localStorage, bouton "Nouvelle session"
- 39/39 tests passent

### Critères de validation ✅
- [x] Sessions JSONL écrites sur disque à chaque message → historique persisté après redémarrage
- [x] Agent charge MEMORY.md + topics à chaque requête (contexte dynamique)
- [x] Consolidation async post-échange → création/mise à jour fichiers thématiques
- [x] MEMORY.md mis à jour avec nouveaux pointeurs
- [x] `ruff check .` → 0 warning | 39/39 tests passent

### Dette technique
- Consolidation charge tous les topics à chaque requête (OK pour Phase 3, optimisation en Phase 7 avec sélection pertinente par LLM)
- Pas de prompt caching Anthropic encore (statique/dynamique identifiés mais non exploités — Phase 7)
- Consolidation tourne après chaque échange ; autoDream (consolidation idle) = Phase 7

### Problèmes rencontrés
- `SessionManager._try_restore` : `UUID(session_id)` lève ValueError si l'ID n'est pas un UUID valide → géré proprement, new session créée
