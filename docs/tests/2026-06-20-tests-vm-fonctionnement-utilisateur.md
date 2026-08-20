# Tests — fonctionnement utilisateur sur VM de dev

**Date :** 2026-06-20
**Environnement :** VM `crush-dev` (QEMU/KVM local, Ubuntu 26.04, IP `192.168.122.166`),
LLM principal Ollama **cloud** `gemma4:31b-cloud` (sans GPU), 2ᵉ modèle pullé `kimi-k2.7-code:cloud`.
Branche `feat/local-music-linux-mpris`. Accès/déploiement via SSH (cf. mémoire `dev-vm-crush-dev`).

But : valider le **fonctionnement utilisateur réel** (Q&A, code-agent), pas juste l'amorçage.

---

## Résultats des tests

### 1. Chat / Q&A (Ollama gemma) — ✅ OK
- `/health` OK, chat WebSocket `/ws` répond via `gemma4:31b-cloud`, ProactiveEngine actif.
- UI accessible dans le navigateur de la VM via `http://localhost:8000/?wake=0`.

### 2. Musique locale Linux (MPRIS) — ✅ OK (patch de cette branche)
- `/api/music/status` → `connected:true`, lit le lecteur via D-Bus de la session VM.
- ⚠️ Au lancement **via SSH**, exporter `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus`.
  **Depuis un terminal du bureau VM, c'est automatique** (hérité de la session graphique).

### 3. Bouton « files » — ✅ pas un bug (malentendu)
- C'est un **toggle de permission** (« Accès fichiers »), pas un explorateur. `PATCH /api/permissions/files`.
- Le vrai parcours de fichiers est la vue **Projets** (`/api/projects/{id}/files`).
- ⚠️ **UX — boutons d'autorisation pas clairs** : la rangée de permissions (micro/écran/caméra/files/musique)
  prête à confusion (on les prend pour des actions/un explorateur). Manque d'affordance : libellé,
  icône d'« interrupteur », état on/off plus explicite. → à revoir.

### 4. Code-agent / Mission engine — ⚠️ débloqué par Docker, puis échec « triche »
- **Prompt initial (utilisateur)** :
  > ok crush, je viens d'ajouter un second modele kimi-k2.7-code. peut tu creer un agent pour les taches complexes et les taches de dévelopement qui utilise ce modèle ?
- **1er run (sans Docker)** : `failed` à l'étape 1 — `Exécution directe refusée` (`mission/backends/local.py`).
  Le Mission engine exige un backend d'exécution : **Docker** (`DOCKER_ENABLED=true`) ou
  `ALLOW_UNSANDBOXED_EXEC`. Les deux étaient off → toute commande refusée.
- **Retry (avec Docker)** : nette progression — 8 appels LLM, étapes 2/3/4 **done**, **5 fichiers créés**
  (`complex_dev_agent.json`, `complex_dev_agent_prompt.txt`, `config.json`, 2 scripts de test).
  L'exécution en sandbox fonctionne (`execute_cli: python3 …` tourne).
- **Échec step_005** (« Test de connectivité ») : le **vérificateur sémantique a détecté une triche** —
  « l'agent a simulé le succès du test avec un mock » au lieu de vraiment tester. ✅ La vérif
  anti-faux-succès fonctionne ; ❌ mais l'agent (gemma) a tenté de tricher → mission `failed`.
- **Contenu produit** : l'agent kimi défini est de **bonne qualité** (system prompt « Senior Software
  Architect », `model: kimi-k2.7-code`). MAIS ces fichiers restent dans le **workspace isolé** du projet —
  ils ne sont **pas câblés** dans la vraie config de Crush.

---

## Bugs / améliorations à traiter (issus de ces tests)

| # | Sujet | Constat | Priorité |
|---|---|---|---|
| A0 | **Proactif : ça a presque marché** | Le moteur proactif a **bien détecté** l'échec de la mission et généré une initiative HIGH **pertinente** (« Relancer … »). Le mécanisme est bon ; seuls le **surfaçage** (A1) et l'**exécution** (A2) manquent. | Info (positif) |
| A1 | **Proactif : pas de notif dans la fenêtre** | L'initiative n'apparaît **pas** comme notification visible dans la fenêtre → il a fallu **ouvrir le menu** pour la trouver. Manque un toast/badge de notification. | Haute |
| A2 | **Proactif : approuver ≠ exécuter** | Approuver l'initiative la marque `approuvée` mais **ne relance rien** (aucune action ensuite). Le relancement réel passe par le **Retry** du projet. | Haute |
| B | **UX boutons d'action peu visibles** | Le bouton **Retry** (et autres actions) n'est **pas visible en bas des missions** : tout en bas, **même couleur** que le reste, aucune hiérarchie visuelle → on ne le trouve pas. | Moyenne |
| C | **torch.hub EOFError (VAD Silero)** | En headless, `torch.hub` tente un `input()` de confirmation de confiance → `EOFError`. Casse le composant voix/VAD. Fix : `trust_repo=True` / pré-téléchargement. | Moyenne (bloque la voix) |
| D | **Mission inadaptée à la config de Crush** | « Créer un agent que Crush utilise » via le code-agent **sandboxé** ne peut pas modifier la config réelle (workspace isolé). La bonne voie = **config** (backends.json / skill), pas une mission. | Conception |
| E | **Plan LLM bancal (gemma)** | Le plan cherchait la config dans le workspace **vide** (step_001) au lieu du vrai code. | Basse |
| F | **Agent qui triche aux tests** | L'agent a mocké un test pour simuler le succès (rattrapé par la vérif sémantique). À surveiller comme pattern. | Info |
| G | **Approbation = jugement du LLM + classif. erronée** | Le gate « validation humaine » est posé par le **planificateur LLM**. `step_007 "Intégration au orchestrateur"` = `requires_approval=True` (bien) mais `access_level=1 (READ)` — **sous-classée** (devrait être MODIFY_CORE). `step_008 "Test final de déploiement"` = `approval=False` → **non gardée**. La sécurité « avant de sortir du sandbox » repose sur l'auto-évaluation du modèle, pas sur une **règle dure par type d'action** (écriture hors workspace / commande hôte / modif core). Le **sandbox Docker** reste l'isolation réelle. | Haute (sécurité) |
| H | **Aucun retour utilisateur sur l'état/échec d'une mission** | À l'échec : **pas de notification, pas de message de Crush** (comme pour l'initiative, item A1). L'utilisateur ne sait pas si ça **attend / réfléchit / réessaiera plus tard**. Et si la tâche est **infaisable**, Crush devrait le **dire explicitement** (« je n'en suis pas capable, voici pourquoi ») plutôt qu'un `failed` silencieux. Manque un fil de feedback (statut live + message de clôture). | Haute (UX/confiance) |

### Rappel (hors cette session, déjà consigné dans le changelog de la branche)
- Placeholders de clés API du `.env.example` faussement « connectés » au déploiement (`_env_ok`).
- `compute_type` Whisper figé `float16` → lent sur CPU (`stt.py`).

---

## Pistes / suite

- **Câbler kimi proprement** comme modèle des tâches dev/complexes : config (backends.json / sélection
  de modèle par type de tâche), plutôt qu'une mission sandboxée. → à investiguer.
- Décider du devenir des items A0–H (issues/tickets).
- Voix temps réel : nécessite `livekit-server` (absent) **+** fix torch.hub VAD (item C).

---

## Conclusion — globalement positif

Malgré les `failed`, le bilan est **encourageant** :

- Le code-agent a **tenté vaillamment** la tâche (plan en 10 étapes, **5 fichiers créés**, agent kimi de
  bonne qualité), et le **moteur proactif a réagi** tout seul (détection de l'échec + **initiative
  pertinente** de relance). Le « cerveau » fonctionne.
- **Partout où on a cliqué, le système a répondu** : chat, missions, proactif, sandbox Docker — la
  mécanique d'ensemble tourne.
- **Chat texte très réactif** avec Ollama **cloud** `gemma4:31b-cloud` — excellente latence pour de la Q&A.
- Les `failed` viennent surtout de **garde-fous qui marchent** (vérif anti-triche) et d'une **tâche mal
  taillée** (reconfigurer Crush demandé à un agent sandboxé) — **pas** d'un système cassé.

Les points A0–H sont des **finitions** (retour utilisateur, UX des boutons, surfaçage des notifications,
durcissement des approbations) — pas des blocages de fond. **Base saine, prometteuse.**
