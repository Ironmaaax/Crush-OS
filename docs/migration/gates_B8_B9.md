# Libellés des GATES B8 et B9 (verrouillés en fin de Phase A)

Le CDC v1.3 référence B8 (continuité des données) et B9 (install à froid)
dans §0.5, F.1.2 et F.3 mais ne donne pas leurs commandes — elles ont été
rédigées en fin de Phase A et validées par Max (cf. rapport Phase A et
les trois resserrages reçus avec le « GO PHASE B »).

Ce fichier est la source de vérité de ces deux gates jusqu'à leur
intégration dans le CDC v1.4.

---

## GATE B8 — Continuité des données — **[LOCAL]**

**Intention** : après les déplacements physiques de Phase B (`vision/faces/`
→ `vision_data/faces/`, `skills/installed/` → `skills_data/installed/`,
conservation de `memory_data/` et `config/*.json`), prouver qu'aucune
donnée utilisateur ne s'est perdue **et** que les données préservées sont
fonctionnellement intactes — pas seulement présentes.

**Commande** : nouveau script `scripts/migration/check_data_state.py`
exécuté en fin de B, qui vérifie quatre points, dans cet ordre, et fail
rapidement à la première anomalie :

1. **Comptes vs baseline A.8** — règles d'égalité par catégorie :
   - `skills.installed.dirs == baseline` : **égalité stricte**. Un
     déplacement de fichiers ne doit JAMAIS changer ce nombre. Un compte
     qui monte → réimport / duplication. Un compte qui descend → perte.
   - `vision.faces.files == baseline` : **égalité stricte**, même raison.
   - `facts.total ≥ baseline` et `events.total ≥ baseline` : l'app peut
     avoir tourné entre la baseline A et la fin de B (faits / events
     ajoutés légitimement). Détecte la perte, pas la légitime croissance.
   - Pour chaque token présent dans la baseline (`size > 0`) : doit être
     présent post-migration (`size > 0`). Tokens absents dans la baseline
     restent absents (pas de fabrication).

   *Détecte : suppression accidentelle (vision/faces/, skills/), DB
   réinitialisée puis re-remplie en double, réimport accidentel d'un
   loader, ou disparition d'un token.*

2. **Skills installés CHARGENT ET S'INSTANCIENT via le `SkillRegistry`
   réel** post-migration : `from crush.capabilities.skills.registry
   import SkillRegistry; reg.load_all()`. La liste des noms chargés
   est **strictement identique** (set-equality, pas count-equality) à
   `skills.installed.loaded_by_registry` de la baseline.

   *Détecte : un skill qui ne charge plus à cause d'un alias namespace
   cassé, d'un chemin de skill.yaml mal résolu, ou de l'ABI skills.base
   devenue inaccessible.*

3. **Un fait connu de la baseline est relu correctement** depuis le
   `MemoryStore` migré : `count_facts(status=ACTIVE) ≥ baseline`, et
   un échantillon (le plus ancien fait par `created_at`) est récupérable
   par `find_active_match(subject, predicate, category)`.

   *Détecte : un schéma de DB cassé ou un chemin de DB qui n'a pas suivi
   la disposition `kernel/paths.py`.*

4. **Au moins un token OAuth présent dans la baseline résout via le
   nouveau chemin** : le fichier est lisible, parsable en JSON, et
   `Settings.google_token_path` (ou équivalent) pointe vers un chemin
   qui existe et est non-vide.

   *Détecte : un déménagement de `config/` qui n'aurait pas été
   répercuté dans `kernel/paths.py`.*

**Résultat attendu** : sortie `GATE B8 ✅ (4/4 checks passed)` ou ligne
`FAIL #N: <détail>` à la première anomalie.

---

## GATE B9 — Install à froid — **[CI]**

**Intention** : prouver qu'un clone neuf du repo, sur une machine neuve,
peut être installé et **booter effectivement** — pas seulement que le
script d'install rend la main sans erreur.

**Décision « deps lourdes » (verrouillée avant Phase B)** : B9 installe
les **vraies** deps lourdes (cmake/openblas pour dlib, portaudio19-dev
pour RealtimeSTT, libgl1 pour opencv) — donc **B9 vit dans la lane CI
COMPLÈTE** (push sur `main` + scheduled hebdo, F.1.2), pas dans la lane
rapide. Pas de stub : un install à froid qui stube ces deps ne teste
plus le même environnement que la prod et ne prouve rien.

Conséquence opérationnelle : on ne déclenche pas B9 à chaque push de la
branche de refonte (trop lent). On le déclenche manuellement (workflow
dispatch) en fin de Phase B, puis automatiquement en F sur la lane
complète.

**Commande** : un job CI dédié sur un runner Ubuntu propre qui :

1. `git clone` + `apt-get install` des deps système lourdes (mêmes paquets
   que ceux installés par le `ci.yml` existant).
2. `uv sync` (toutes deps Python, dont dlib/face-recognition compilées).
3. **Lance `setup.sh`** (post-Q4 fusion) jusqu'au bout, exit code 0 exigé.
4. **Vérifie la disposition** : les dossiers créés correspondent à la
   cible `kernel/paths.py` (`memory_data/`, `vision_data/faces/`,
   `skills_data/installed/` selon Q2, `config/`). Aucun dossier de
   l'ancienne disposition (`vision/faces/`, `skills/installed/` dans
   `src/crush/capabilities/skills/`) n'apparaît au niveau du package
   installé.
5. **Boot effectif** : `uv run python scripts/validation/smoke_runtime.py
   --fake-llm` rend la main avec exit code 0 et imprime `BOOT OK`.
   (`--fake-llm` = sans clé Anthropic réelle, conformément à C.1.7.)
6. **Auto-test du chemin chaud des paths** : le smoke charge un prompt,
   écrit/relit un fichier dans le data dir, sert un asset statique via
   `TestClient`, charge un skill installé — exactement le test runtime
   de la GATE B7b. Si B7b passe ailleurs mais pas dans l'install à froid,
   B9 fail (= la disposition à froid diverge de la disposition courante).

**Résultat attendu** : `GATE B9 ✅ — install OK, boot OK` ou échec
explicite à l'étape qui rate.
