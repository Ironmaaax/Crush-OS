# CRUSH-OS — analyse détaillée du projet

> **Date** : 2026-06-20 · **Analyste** : Claude (Vincent Guilbert)
> **Périmètre** : `/home/vinc1/IdeaProjects/crush-OS` (v0.2.0)
> **Méthode** : analyse 100 % statique — lecture de code, **aucune exécution** du projet.
> Recoupée sur 4 explorations parallèles + lecture directe des fichiers d'exécution
> sensibles + scan CVE des dépendances (via recherche web).

---

## 0. Avertissement licence (à lire avant de « participer »)

Ce n'est **pas** un projet B+D. C'est le projet personnel de **Barthélemy Houot**
(`@Grominet95`), sous **Proprietary Source License** (© 2026) :

- ✅ Autorisé : lire le code (perso / éducatif), l'exécuter en privé.
- ❌ Interdit sans **accord écrit de l'auteur** : modifier, créer une œuvre dérivée,
  contribuer, redistribuer, héberger pour des tiers, usage commercial.

**Conséquence** : toute contribution suppose d'abord un accord explicite de l'auteur
(barth.houot@gmail.com). Le reste de ce document suppose cet accord obtenu.

---

## 1. Carte d'identité

| Champ | Valeur |
|---|---|
| Nom | CRUSH-OS (`crush-os`) — assistant personnel IA, texte + voix temps réel, self-hosted |
| Auteur / licence | Barthélemy Houot — Proprietary Source License |
| Version | `0.2.0` (tag `v0.2.0-architecture`), post-refonte « Opération Squelette » |
| Langage | Python 3.11 (`>=3.11,<3.14`), tout async |
| Volume | ~34 500 lignes / 220 modules dans `src/crush/` (les docs citent ~46k au total) |
| Tests | ~587 unitaires (< 30 s) + ~28 `integration` |
| Deps | `uv` + `uv.lock`, packaging `hatchling` (layout `src/`) |
| Langue projet | Français (code, commits, docs, prompts) |
| Process | API FastAPI (`crush.app`) **+** agent vocal LiveKit (`crush.interfaces.voice.agent`) |

---

## 2. Ce que fait Crush

Assistant local qui : **dialogue** (chat web + voix temps réel LiveKit + Telegram),
**se souvient** (faits atomiques datés), **agit** via des outils (web, Gmail, Calendar,
Spotify, Notion, vision, shell, impression 3D, CAO Fusion 360), **exécute des missions**
planifiées et vérifiées, **prend des initiatives** proactives gouvernées, et **apprend**
(fabrique ses propres « skills », testées en sandbox Docker avant validation humaine).

---

## 3. Architecture en couches strictes (le cœur)

Code packagé sous `src/crush/` mais importé comme `crush.*`. **4 couches dont les
dépendances sont validées mécaniquement par `import-linter`** (gate CI à chaque push).

| Couche | Package(s) | Rôle | Peut importer |
|---|---|---|---|
| **L0** | `kernel/` | Contrats (`Protocols`), `schemas`, bus d'événements, `settings`, `vocab`, `paths`, gouvernance de base | rien d'interne |
| **L1** | `providers/`, `capabilities/`, `analytics/`, `hardware/` | LLM / Mémoire / Audio / Vision ; Outils + Skills ; widgets ; périphériques | `kernel` uniquement (jamais entre eux) |
| **L2** | `engine/` | Orchestration : Gateway, Agent, Router, Sessions, Budget, Mission Engine, Proactif, Background | `kernel` uniquement |
| **L3** | `interfaces/`, `app.py`, `bootstrap.py` | Routers FastAPI, pipeline voix, composition root | tout |

**4 règles `forbidden`** (`pyproject.toml`) : ① `kernel` n'importe rien d'interne ·
② les 4 packages L1 n'importent que `kernel` · ③ `engine` n'importe que `kernel` ·
④ aucun module `crush.*` n'importe le namespace de données racine `config/`.

**Composition root unique** — `bootstrap.build()` instancie le graphe (~30 objets),
ordre `settings → bus → providers → capabilities → engine`, **synchrone et sans réseau**.
Deux entry-points (API + voix) l'appellent tous deux → session, mémoire et outils partagés.

---

## 4. Carte des sous-systèmes

**L2 — `engine/`**

| Module | Responsabilité |
|---|---|
| `agent.py` | Construit le prompt (statique + mémoire + outils), stream LLM, capture les `tool_use` |
| `gateway.py` | Point d'entrée chat unique : session, notifications, routing, boucle d'outils |
| `router.py` | `SpeedRouter` (sans état) : détecte le tag `[I]/[CF]/[BG]/[BG:PROJECT]` |
| `session.py` | `SessionManager` : registre mémoire + restauration JSONL |
| `mission/` | `orchestrator` → `worker_agent` → `verifier` (3 couches) → `reflexion` (leçon) ; `governance` (gate), `capability_engine`, backends (local/Docker/SSH/RPC) |
| `proactive/` | `engine` (boucle ~30 min), `command_center` (vue agrégée), `curator` (entretien nocturne), `collectors/` |
| `background/` | `worker` (file asyncio), `scheduler` (9h/3h…), `notifications`, `routines` (cron) |

**L1 — `providers/`**

| Module | Implémente | Détails |
|---|---|---|
| `llm/` | `LLMProvider` | Anthropic, OpenAI, Mistral (client OpenAI), Gemini, Ollama — streaming + tool-loop |
| `memory/` | `MemoryStore`, `FTSIndex`, `VectorIndex` | Memory Kernel SQLite + miroir Markdown + recherche FTS5/sémantique (`fastembed`) + AutoDream/Consolidation |
| `audio/` | `TTSEngine` | STT (faster-whisper / Deepgram / RealtimeSTT) · TTS (Piper / ElevenLabs) |
| `vision/` | détecteur + recon faciale | Daemon webcam ~2 fps, YOLOv8n, reconnaissance faciale optionnelle (dlib) |

**L3 — `interfaces/`** : ~25 routers FastAPI (`chat`, `memory`, `budget`, `proactive`,
`skills`, `sessions`, `vision`, `system`, `setup_wizard`, `config/*`, `admin`…), pipeline
voix LiveKit, canaux de messagerie, UI admin statique.

---

## 5. Flux d'une mission (bout en bout)

```
Demande → Gateway → [BG:PROJECT] → BackgroundWorker → ProjectOrchestrator
  → LLM planifie N étapes (chacune DOIT avoir un success_criterion)
  → WorkerAgent exécute chaque étape (outils sous gate composite)
     → Verifier 3 couches : structurelle → déterministe (commande) → sémantique (LLM juge)
     → échec ⇒ retry (max 2), sinon FAILED
  → Reflexion : produit une « leçon » → ingérée en mémoire ; si pertinent ⇒ candidat skill
```
Reprise après crash, audit immuable de chaque décision.

---

## 6. Mémoire (Memory Kernel)

Extraction de **faits atomiques** datés, sourcés, renforçables, jamais supprimés.
SQLite = source de vérité ; miroir Markdown **unidirectionnel** (corriger = événement
`human_correction`).

| Table | Contenu |
|---|---|
| `events` | Log immuable de tout ce qui arrive |
| `facts` | Claims atomiques (prédicat/catégorie d'un vocabulaire fermé), statut, confiance, decay |
| `fact_observations` | Renforcement sans duplication |
| `fact_relations` | `supersedes` / `contradicts` / `supports` / `related_to` |

SQLite WAL + busy_timeout (concurrence API ↔ voix). Consolidation nocturne (AutoDream +
ConsolidationAgent).

---

## 7. Outils disponibles

| Outil | Sensibilité | Rôle |
|---|---|---|
| `browser` | 🌐 Réseau | Fetch + scraping + recherche DuckDuckGo (bloque localhost/IP privées — cf. §10) |
| `filesystem` | 📁 FS (lecture) | Lecture (≤100 Ko), recherche par pattern, confinement par `allowed_roots` |
| `cli` / `execute_cli` | ⚠️ Exécution | Binaires whitelistés + blocklist + sandbox tmpdir + approbation |
| `gmail`, `calendar` | 🔑 OAuth Google | Lire emails / lister-créer événements |
| `notion`, `spotify`, `weather` | 🌐 API | Tâches Notion / lecture musique / météo |
| `vision` | 📷 Caméra | Capture + détection objets + rappel visuel |
| `memory` | 💾 | Écrit/recherche dans le topic store |
| `printer` (3D), `fusion` (CAO) | ⚙️ Matériel | Impression Bambulabs / scripts Fusion 360 |
| `subagent`, `preset`, `skills`, `capability`, `show_view`, `map_control` | 🧩 | Sous-agents, séquences, skills, vues dashboard |

Interface commune `Tool` (`base.py`), enregistrement via `ToolRegistry` (`registry.py`).

---

## 8. Skills auto-générées (cycle de vie)

```
signal (capability gap) → Synthesizer (SKILL.md) → candidates/
   → SkillLab : sandbox Docker (test vert obligatoire) → SANDBOXED_PASS
   → promotion HUMAINE → installed/ → ACTIVE
```
Sandbox Docker : `--network=none`, `cap-drop ALL`, `no-new-privileges`, RO + tmpfs,
timeout 30 s. **Aucun chemin n'auto-installe une skill** ; l'humain valide. Alias ABI
(`from skills.base import …`) garanti stable.

---

## 9. Modèle de sécurité & gouvernance (vue d'ensemble)

| Brique | Mécanisme |
|---|---|
| Auth API | Token Bearer, comparaison constant-time (`hmac.compare_digest`), `SecretStr`, exemptions explicites (UI/OAuth/WS) |
| Gate composite | 3 axes — **risque** (`AccessLevel` 0-5) × **catégorie** (`approvals.json`) × **budget** — décision la plus restrictive : `AUTO`/`DRY_RUN`/`APPROVAL`/`REFUSED` |
| Approbation humaine | `asyncio.Future`, timeout 120 s ; `INSTALL_PACKAGE`/`MODIFY_CORE` ⇒ toujours approbation |
| CLI (`execute_cli`) | Blocklist inconditionnelle → `shlex` → whitelist binaire → approbation interpréteurs/système → sandbox tmpdir ; `create_subprocess_exec` (pas de `shell=True`) |
| Budget | Multi-scope (global/projet/run), hard-stop déterministe, alerte à 80 % |
| Secrets | 12 champs `SecretStr` ; `.env` + tokens OAuth/Telegram gitignorés |
| Telegram | Accès réservé à `TELEGRAM_OWNER_ID`, vérifié sur chaque handler |
| Curator | Chemins « personnalité » protégés : le noyau ne se réécrit jamais seul |

**Bons réflexes confirmés** : `yaml.safe_load` partout, pas d'`eval`/`exec`/`pickle`,
sous-process en liste d'arguments dans les chemins principaux.

---

## 10. Revue sécurité approfondie (constats fondés sur lecture du code)

> Ces constats corrigent un premier survol automatique : la sandbox des presets et
> l'injection macOS n'étaient **pas** maîtrisées contrairement à ce qui avait été dit.

### 10.1 `execute_cli` (`tools/cli.py`) — référence solide, **un angle mort**

Le tool `ExecuteCLITool` est bien conçu : 5 couches (blocklist → shlex → whitelist sur
binaire résolu `Path(parts[0]).name` → approbation → sandbox), bon modèle de menace
documenté (injection de prompt via contenu externe), `create_subprocess_exec` sans shell.

**🟠 Angle mort réel** : `python`, `python3`, `pip`, `uv`, `git` sont whitelistés **et
traités comme « safe »** (pas dans `_INTERPRETERS_REQUIRE_APPROVAL`, qui ne contient que
`osascript`). Donc `execute_cli("python -c '<code arbitraire>'")` passe **sans approbation
humaine** — idem `pip install <pkg>` (= exécution de code). Seule mitigation : la « sandbox »
= `cwd` tmpdir + `env` restreint. Ce **n'est pas une frontière de sécurité** : pas
d'isolation namespace, accès filesystem par chemins absolus et **réseau** intacts.
→ Une injection de prompt atteignant `execute_cli` peut exécuter du code arbitraire sur
l'hôte sans confirmation. **Correctif** : ajouter `python*`, `pip`, `uv`, `git` (hooks)
à l'ensemble « approbation requise ».

`CLIRunnerTool` (`run_script`) est sûr : alias prédéfinis dans `config/tools.yaml` (vide
par défaut), `create_subprocess_exec`.

### 10.2 Presets (`skills/executor.py`) — **contourne toute la gouvernance CLI**

| # | Constat | Sévérité |
|---|---|---|
| 1 | `_exec_cli` exécute `asyncio.create_subprocess_shell(step.get_command())` — **shell arbitraire, sans blocklist/whitelist/approbation/sandbox**. Contourne entièrement `cli.py`. Le test SkillLab ne lance pas les steps CLI → un preset promu peut exécuter n'importe quoi sur l'hôte au runtime. | 🟠 Élevé |
| 2 | `_exec_notify` macOS : `osascript -e '...'` n'échappe que `"` (pas `'`) → un `'` dans `step.body`/`title` casse le quoting shell ⇒ **injection**. | 🟠 Élevé |
| 3 | `_exec_notify` Windows : PowerShell `MessageBox::Show('{body}','{title}')`, **aucun échappement** ⇒ injection triviale via `'`. | 🟠 Élevé |

**Correctif** : router `_exec_cli` par la même blocklist/whitelist que `execute_cli` (ou
au minimum la blocklist) ; pour `_exec_notify`, ne pas construire de shell par f-string —
passer par `create_subprocess_exec` avec arguments en liste, ou `shlex.quote`.

### 10.3 `browser.py` — SSRF : protection présente mais **contournable**

`_validate_url` bloque `localhost`, `127.x`, `10.x`, `172.16-31.x`, `192.168.x`, `::1`,
`*.local` — mais :

- `follow_redirects=True` et validation **seulement sur l'URL initiale** ⇒ une URL
  publique qui redirige en 30x vers `http://127.0.0.1/` ou `http://169.254.169.254/`
  **contourne** le contrôle.
- `169.254.0.0/16` (link-local / métadonnées cloud) **non bloqué**.
- **DNS rebinding** : un hôte `evil.com` résolvant vers une IP privée passe (la regex ne
  teste que des littéraux).
- Encodages IP alternatifs (décimal `2130706433`, octal, IPv6-mapped) non couverts.

Impact modéré pour un assistant local mono-utilisateur, mais le contrôle est **présenté
comme une garantie** dans la docstring. **Correctif** : résoudre l'hôte en IP et valider
l'IP (pas le nom), revalider après chaque redirection (redirections manuelles), ajouter
`169.254.x`.

### 10.4 `filesystem.py` — **correct** (à garder en exemple)

`ReadFileTool`/`FindFilesTool` font `path.resolve()` **puis** `is_relative_to(root)` :
les symlinks sont résolus **avant** la vérification → confinement correct. Lecture seule,
cap 100 Ko, gate de permissions. Bon pattern.

### 10.5 `subagent.py` — gating correct

`execute_script` (RPC) passe par `approval_checker.check("code_write", …)` puis un backend
sandboxé (Docker si activé, sinon refus sauf `ALLOW_UNSANDBOXED_EXEC`). `spawn_subagent`
ouvre une session fraîche mais réutilise les mêmes outils → même gouvernance.

### 10.6 Récapitulatif des risques résiduels

| Sévérité | Zone | Constat | Correctif |
|---|---|---|---|
| 🟠 Élevé | `tools/cli.py` | `python`/`pip`/`uv`/`git` exécutables sans approbation ; sandbox ≠ frontière | Approbation requise pour ces binaires |
| 🟠 Élevé | `skills/executor.py` `_exec_cli` | Shell arbitraire hors gouvernance | Router par la blocklist/whitelist CLI |
| 🟠 Élevé | `skills/executor.py` `_exec_notify` | Injection shell macOS **et** Windows | `create_subprocess_exec` / `shlex.quote` |
| 🟡 Moyen | `tools/browser.py` | SSRF contournable (redirect, 169.254, DNS rebinding) | Valider l'IP résolue + revalider après redirect |
| 🟡 Faible | `interfaces/api/config/settings.py` | `POST /api/settings/update` mute `os.environ` (3 sources de vérité) — déjà au backlog | N'écrire que `.env` + singleton |
| ℹ️ Historique | `kernel/settings.py` | `__repr__` a fui des clés en clair → corrigé via `SecretStr` | Lint rule `*_key/_token/_secret` → `SecretStr` |

Note : les canaux Discord/Slack/Signal/WhatsApp existent comme **adaptateurs**
(`channels/*.py`) mais **seul Telegram a sa lib en dépendance** → les autres sont des
squelettes non câblés.

---

## 11. Scan CVE des dépendances (versions résolues du `uv.lock`)

Sources : GitHub Security Advisories, osv.dev, NVD, PyPI. Aucun n° de CVE fabriqué.

| Paquet | Version | CVE | Sévérité | Affecté ? | Corrigé en |
|---|---|---|---|---|---|
| **yt-dlp** | 2026.3.17 | CVE-2026-50574 | **HIGH (8.3)** | **OUI** | 2026.6.9 (écriture arbitraire/RCE via `--downloader aria2c`) |
| **starlette** | 1.0.0 | CVE-2026-54283 | **HIGH** | **OUI** | 1.3.1 (DoS `request.form()` urlencoded) |
| **starlette** | 1.0.0 | CVE-2026-48710 | MEDIUM (6.5) | **OUI** | 1.0.1 (bypass autorisation via Host malformé) |
| **yt-dlp** | 2026.3.17 | CVE-2026-50019 | MEDIUM | **OUI** | 2026.6.9 (fuite cookies via `--downloader curl`) |
| pillow | 12.2.0 | (FITS/font/PSD) | — | NON | déjà à la version de correction |
| lxml | 6.1.0 | CVE-2026-41066 (XXE) | — | NON | déjà corrigé |
| ultralytics | 8.4.41 | (supply-chain 8.3.41-46) | — | NON | versions trojanisées antérieures |
| aiohttp, cryptography, jinja2, requests, opencv-python, anthropic | (récentes) | divers | — | NON | versions résolues ≥ correctifs |

**yt-dlp est à la fois dépendance ET whitelisté dans `execute_cli` + utilisé par les
presets** → sa CVE pèse plus lourd ici. fastapi 0.136.0 n'a pas de CVE propre : l'exposition
vient de **starlette**.

**Top actions** :
1. `yt-dlp` → ≥ 2026.6.9 (HIGH, RCE). Mitigation immédiate : ne pas utiliser `--downloader aria2c`/`curl`.
2. `starlette` → ≥ 1.3.1 (corrige HIGH + MEDIUM) ; revalider la compat `fastapi`/`uvicorn`.
3. Re-`uv lock` + `pip-audit`/`uv pip audit` sur l'intégralité du lock (ce scan a ciblé une liste prioritaire, pas les ~400 transitives).

---

## 12. Qualité, CI & conventions (pour contribuer)

| Élément | Détail |
|---|---|
| Lane CI rapide (chaque push) | `ruff check` + `lint-imports` (4 contrats) + `mypy` (scopé `kernel`) + `pytest -m "not integration"` + diff baseline routes |
| Lane CI lourde (`main`/hebdo) | deps système + suite complète + **B9 install à froid** |
| mypy | scopé `kernel/` seulement |
| ruff | line-length 100, `E W F I B UP ANN ASYNC TID` ; imports absolus |
| Routes | gelées vs `routes.baseline.txt` (régénérer si modif) |
| Commits | français, conventionnels (`feat(...)`, `fix(...)`, `chore(...)`) |
| Commandes | `make test` / `make lint` / `make typecheck` ; `./crush run\|api\|voice\|eclosion` |

---

## 13. Dette technique notable (BACKLOG → Phase G)

- Fermer les 5 `ignore_imports` import-linter (résidus RÈGLE 2/3 → injection).
- Conformité `Protocols` stricte : 2 couples `Tool`/`Skill` (variance ABC vs Protocol).
- Unifier le fix `os.environ` / `.env` (source de vérité unique).
- Réorg `tests/` en miroir de `src/crush/<couche>/` (52 fichiers à plat).
- Migration CI hors Node 20 (actions dépréciées) avant sept. 2026.
- Smoke test du process voix non couvert (dépend de LiveKit runtime).

---

## 14. Plan de contribution proposé (sous réserve §0)

**Étape 0 (bloquante)** : obtenir l'accord écrit de l'auteur.

**Premier PR idéal — bumps de sécurité** (diff minime, valeur haute, vérifiable par la CI) :
- `yt-dlp` → ≥ 2026.6.9, `starlette` → ≥ 1.3.1 ; re-`uv lock` ; CI verte.

**Deuxième PR — durcissement injections** (bien scopé, testable) :
- `executor.py` `_exec_notify` : passer en `create_subprocess_exec` / `shlex.quote`.
- `executor.py` `_exec_cli` : router par la blocklist/whitelist CLI.
- `cli.py` : approbation requise pour `python`/`pip`/`uv`/`git`.

**Tâches d'onboarding (faible risque)** : migration CI Node 20 ; lint rule
`*_key/_token/_secret` → `SecretStr` ; clôture mutation `os.environ`.

**Workflow** : brancher depuis `main` ; commits français conventionnels ;
`make lint && make test && make typecheck` verts ; respecter les couches (import-linter) ;
régénérer la baseline des routes si une route change ; marquer `@pytest.mark.integration`
les tests lourds.

---

*Document généré en mode global B+D — analyse statique, sans exécution du code analysé.*
