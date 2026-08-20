# Session de réflexion — délégation multi-modèle & extensibilité (skills / MCP)

**Date :** 2026-06-20 (soir)
**Environnement :** VM `crush-dev` (`192.168.122.166`), Ollama **cloud** `gemma4:31b-cloud` + `kimi-k2.7-code:cloud`.
**Nature :** session de **conception** (architecture) + **vérifications empiriques ciblées** via SSH.

> Ce document est **complémentaire** des deux autres docs du jour et ne les répète pas :
> - [docs/tests/2026-06-20-tests-vm-fonctionnement-utilisateur.md](../tests/2026-06-20-tests-vm-fonctionnement-utilisateur.md) — tests de fonctionnement (chat, musique, mission engine, bugs A0–H).
> - [docs/changelogs/2026-06-20-music-provider-local-linux.md](../changelogs/2026-06-20-music-provider-local-linux.md) — patch musique MPRIS + annexes (placeholders clés API, `compute_type` Whisper).
>
> Voir **§5 (anti-redite)** pour ce qui est volontairement renvoyé à ces docs.

---

## 1. Objet

Partant de l'échec de la mission « créer un agent Kimi » (déjà constaté dans le doc de tests, item 4),
on a remonté la question de fond : **comment Crush s'étend** (skills, MCP) et **comment faire
cohabiter deux modèles** (Gemma pour la conversation, Kimi pour le code). La session a produit
une cartographie de l'extensibilité + un **diagnostic empirique neuf** sur les capacités réelles du frontal.

---

## 2. Volet extensibilité — skills & MCP

### 2.1 Le système de skills (cartographie)

- Couche L1, [src/crush/capabilities/skills/](../../src/crush/capabilities/skills/), à côté des `tools/`.
  Distinction nette : **`tools/`** = outils natifs livrés dans le package ; **`skills/`** = extensions
  portables installées hors package (`skills_data/installed/`), partageables, stables via l'ABI `skills.*`
  (cf. [skills-abi.md](skills-abi.md)).
- **Catalogue communautaire** = repo GitHub **`Ironmaaax/Crush-skills`** ; l'installeur
  ([installer.py:17](../../src/crush/capabilities/skills/installer.py#L17)) télécharge depuis
  `raw.githubusercontent.com/Ironmaaax/Crush-skills/main`, [catalog.json](../../src/crush/capabilities/skills/catalog.json)
  en est le miroir.
- **3 types** : `conversational` (`SkillBase` = `SYSTEM_PROMPT` injecté + `get_tools()`), `preset`
  (séquence `skill.yaml` déclenchée par triggers → `execute_preset`), `view` (UI JS/CSS).
- **2 sources d'install** : `Crush-skills` (officiel) **et** **ClawHub** (`clawhub.ai`,
  [_clawhub.py](../../src/crush/capabilities/skills/_clawhub.py)). Un skill = code Python arbitraire →
  `vet-skill` recommandé avant install d'un skill tiers.
- Gouvernance du repo (CONTRIBUTING.md / AGENTS.md) : validation **statique** uniquement
  (`validate_catalog.py`, `build_index.py`, 1 extension/PR, secrets via `requires_env`).
  Le **comportement runtime relève de crush-OS** (sandbox Docker).

### 2.2 MCP — état réel

- **Aucun pont MCP générique.** Le seul MCP de la codebase est **Fusion 360**, codé **en dur** comme
  *tool natif* : [capabilities/tools/fusion.py](../../src/crush/capabilities/tools/fusion.py) (`_FusionClient`,
  HTTP JSON-RPC 2.0, `Mcp-Session-Id`, SSE, port 27182). Mono-serveur, 3 outils écrits à la main,
  **pas** de `tools/list` (découverte dynamique).
- **« Intégrations »** (onglet de [capabilities.js](../../src/crush/interfaces/ui/static/capabilities.js))
  = **panneau de saisie de credentials** (clés/OAuth/URL → `.env`), **pas** un système de plugins MCP.
- **Gmail / Calendar** = *tools natifs* + **OAuth2 Google officiel** (`google-auth-oauthlib`,
  [google_oauth.py](../../src/crush/interfaces/api/google_oauth.py)), pas du MCP. Même pattern que Notion/Spotify.

### 2.3 Brancher un MCP (FreeCAD, Context7…) — faisabilité

Possible **via un skill/tool** dont le `get_tools()` réutilise le client de `fusion.py`. Conditions :

| Critère | Impact |
|---|---|
| Transport **HTTP/SSE** (ex. Context7) | `_FusionClient` réutilisable quasi tel quel ✅ |
| Transport **stdio** (beaucoup de MCP FreeCAD) | couche stdio à écrire (spawn + pipes) |
| **Sandbox Docker** | service distant = accès réseau ; app locale = joindre l'hôte |
| **Auth** | clé API → `requires_env` |

**Context7** est le meilleur banc d'essai (remote HTTP, auth par clé, 2 outils). **Réserve d'usage** :
Context7 sert un agent qui code → pertinent seulement si Crush fait de l'aide au dev.
Conclusion : le pattern MCP→skill est viable, mais **rien n'est prêt** — c'est à écrire.

---

## 3. Volet délégation multi-modèle (Gemma frontal + Kimi worker)

### 3.1 La vision

Crush **multi-modèle** : le frontal conversationnel (`gemma4:31b-cloud`) **délègue** les tâches de
code/complexes à un sous-agent sur modèle spécialisé (`kimi-k2.7-code:cloud`). Crush = chef d'orchestre,
pas l'agent de code. Reco : livrables bornés (Word/Excel/scripts) → natif ; gros projets → déléguer à un
vrai agent de code ; **ne pas réimplémenter** une boucle agentique de code dans Crush.

### 3.2 Vérifications empiriques **nouvelles** (non présentes dans le doc de tests)

1. **gemma4:31b-cloud sait faire du function-calling natif.** Test direct sur le daemon
   (`POST /api/chat` + champ `tools`) → `tool_calls` impeccable (`get_weather{city:"Paris"}`) en **~300 ms**.
   → **Le frontal n'est PAS le maillon faible.** L'hypothèse « petit modèle = ne tool-call pas »
   (avertissement [local.py:55-62](../../src/crush/providers/llm/local.py#L55-L62)) **ne s'applique pas** à gemma4.

2. **Deux mécanismes de déclenchement distincts** :
   - **marqueur texte** `[BG:PROJECT]` (parsé par Crush) → marche **sans** function-calling →
     c'est ce qui a déclenché la mission Kimi ;
   - **tool_calls natifs** (browser, weather…) → nécessitent le function-calling du modèle.
   ([subagent.py](../../src/crush/capabilities/tools/subagent.py) : `spawn_subagent` interne vs `[BG:PROJECT]` livrable.)

3. **Échec « trouve une pizzeria » = parité d'outils incomplète côté Ollama (PAS un skill cassé, PAS le modèle).**
   L'outil de recherche **existe** : tool `browser`, action `search` = **DuckDuckGo**
   ([browser.py:207](../../src/crush/capabilities/tools/browser.py#L207)), **enregistré au boot**
   ([bootstrap.py:292](../../src/crush/bootstrap.py#L292)) et autorisé sans confirmation
   (`web_search: ALWAYS` = exécute sans demander, [approvals.py](../../src/crush/kernel/approvals.py)).
   **Mais en conversation, les outils ne sont passés au LLM que si le provider a `stream_with_capture`**
   ([agent.py:228](../../src/crush/engine/agent.py#L228)) — défini **4× dans `api.py`** (Anthropic/OpenAI/Mistral/Gemini),
   **0× dans `local.py`** (Ollama). Avec Ollama on tombe donc dans `_simple_stream()`
   ([agent.py:236-244](../../src/crush/engine/agent.py#L236-L244)) → `complete()` **sans outils** → gemma ne voit
   jamais le `browser` → **complaisance** (« je regarde ça »). Installer `web-researcher` n'aide pas (et **aggrave**
   le « répond puis s'arrête » : son prompt pousse à chercher un outil indisponible). **Fix réel : implémenter
   `stream_with_capture` sur `OllamaProvider`** — son `tool_loop` fait déjà 90 % (passe les outils, parse les
   `tool_calls` natifs). Insight projet : **parité d'outils-en-conversation incomplète entre backends** (Crush
   développé surtout sur Anthropic ; Ollama de second rang sur ce point).

4. **Sélecteur de modèle (Settings) = global, pas par rôle.** L'onglet *Modèles* **détecte
   dynamiquement** Kimi via `GET /api/ollama/models` → daemon `/api/tags`
   ([config/llm.py:62](../../src/crush/interfaces/api/config/llm.py#L62)) — re-query live, sans redémarrage —
   et permet de switcher avec **hot-swap** (`_LLM_HOT_SWAP_KEYS`,
   [config/_env.py:52](../../src/crush/interfaces/api/config/_env.py#L52)). **MAIS** ça change `OLLAMA_MODEL`,
   le modèle **global** lu par toutes les instances ([factory.py:10-13](../../src/crush/providers/llm/factory.py#L10-L13)).
   Switcher = basculer **TOUT** l'assistant sur Kimi (« un **OU** l'autre »), **pas** une délégation par rôle
   (« Gemma **ET** Kimi »).

5. **C'est gemma (pas Kimi) qui a exécuté la mission.** Le `WorkerAgent` reçoit `voice_llm` à l'injection
   ([worker_agent.py:618](../../src/crush/engine/mission/worker_agent.py#L618)) = backend global = gemma.
   Kimi n'est câblé dans aucune instance de provider — seulement servi par le daemon. La mission
   « fais bosser Kimi » a donc été faite **par gemma**, qui a écrit un JSON mentionnant `"model": "kimi-k2.7-code"`
   sans jamais appeler Kimi.

### 3.3 Diagnostic d'architecture

- Le frontal est **capable** (tool-call + marqueurs). Ce qui manque : **(a)** des capacités installées
  (0 skill, pas d'outil de recherche) ; **(b)** la **délégation par rôle** (un modèle ≠ par agent).
- Le choix du modèle est aujourd'hui **global** (un seul `OLLAMA_MODEL`). La vision exige **deux providers
  simultanés**, pas une bascule manuelle.

### 3.4 Le câblage requis (minimal)

Le `WorkerAgent` **n'a quasiment pas à changer** : il accepte déjà son LLM par injection
([worker_agent.py:220-231](../../src/crush/engine/mission/worker_agent.py#L220-L231)). Deux points seulement :

1. **`OllamaProvider` paramétrable** — aujourd'hui fige `settings.ollama_model`
   ([local.py:52](../../src/crush/providers/llm/local.py#L52)) → ajouter un `model` optionnel.
2. **Bootstrap** — construire **2 instances** sur le même daemon (gemma frontal + kimi worker) et injecter
   la bonne au worker, au lieu de `voice_llm`.

Respecte les couches : provider en **L1**, injection en **L3** (`bootstrap`), worker (L2) agnostique du modèle.
**Auth Ollama cloud** = au niveau **daemon** (`ollama signin`), transparente pour le provider → rien à faire côté auth.
Le **hot-swap** existant prouve que recréer un `OllamaProvider` à la volée fonctionne → brique réutilisable
pour la 2ᵉ instance.

**Statique vs dynamique :**

| Approche | Sens | Effort |
|---|---|---|
| **Statique** (recommandé pour démarrer) | worker **toujours** sur Kimi ; le frontal ne décide rien | les 2 points ci-dessus |
| **Dynamique** | modèle **choisi par mission** | + mécanisme de décision (critère, qui choisit, transport du choix) |

Le statique réalise déjà la vision (le mission engine ne sert qu'aux tâches lourdes = celles à mettre sur Kimi).

---

## 4. Lien avec MCP

Dans cette architecture, MCP devient **accessoire** : le cœur, c'est l'**exécution de code vérifiée**
(le mission engine + sandbox Docker, déjà en place). MCP (ex. Context7) = source de doc fraîche, branchée
*sur l'agent de code*, en option — utile mais pas structurant.

---

## 5. Volontairement **non répété** ici (voir doc de tests / changelog)

Déjà documenté ailleurs, pas redétaillé dans ce compte rendu :

- **Run mission Kimi** (Docker, plan 10 étapes, 5 fichiers, triche step_005, vérif anti-faux-succès) → doc tests §4.
- **Proactif** (détection d'échec + initiative HIGH + boucle de relance) → doc tests A0/A1/A2.
- **Mission inadaptée** (reconfigurer Crush via agent sandboxé = impossible, voie = config) → doc tests D.
- **Approbations / classification d'accès** (gate posé par le LLM) → doc tests G.
- **Pas de retour utilisateur sur l'échec** → doc tests H.
- **UX boutons / permissions / Retry** → doc tests 3, B.
- **torch.hub VAD, placeholders clés API, `compute_type` Whisper** → doc tests C + annexes du changelog.

---

## 6. Suite

1. 🍕 **Recherche web (cause réelle)** : implémenter `stream_with_capture` sur `OllamaProvider` (base = son `tool_loop` existant) pour que les outils — dont `browser`/DuckDuckGo — soient exposés à gemma **en conversation**. (Installer `web-researcher` ne suffit pas : l'outil existe déjà, il n'est juste pas passé au modèle côté Ollama.)
2. 🔌 **Câbler Kimi (statique)** : `OllamaProvider(model=…)` paramétrable + 2 instances au bootstrap + injection worker.
   (Le « choix dynamique du modèle » = itération ultérieure.)
3. 🧹 Désamorcer la boucle proactive sur la mission impossible (cf. doc tests A2 / item D).
4. 🧩 **MCP** (optionnel) : prototyper un skill « pont MCP » sur Context7 (transport HTTP), base = `_FusionClient`.

---

## 7. Note — papier de recherche

Cette expérimentation (assistant perso self-hosted **multi-modèle** : délégation par rôle, proactif
gouverné, **limites de la self-modification** d'un agent, garde-fous sandbox, découverte dynamique de
modèles) constitue le **matériau d'un papier de recherche** à venir. Findings bruts conservés en mémoire
(`crush-vm-delegation-findings`, `crush-multimodel-delegation`).
