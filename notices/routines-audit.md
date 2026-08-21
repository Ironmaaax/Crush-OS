# Attributions — Routines & Audit (Crush V3)

## Inspiration de conception

Le moteur de routines de Crush V3 (`background/routines.py`, `background/scheduler.py`)
s'inspire des patterns architecturaux des projets open-source suivants.
Aucun code source n'a été copié ; seules les **idées de conception** (nommage, politiques,
structure des enregistrements) ont été adaptées en Python.

---

### Paperclip — paperclipai/paperclip

- **URL** : https://github.com/paperclipai/paperclip
- ** ** : MIT
- **Commit de référence** : depth-1 clone, 2026-05-29
- **Concepts repris** :
  - Modèle `Routine` (name, trigger, concurrency_policy, catch_up_policy)
  - Modèle `RoutineRun` (id, status, started_at, finished_at, audit_log)
  - Valeurs de `ConcurrencyPolicy` : `skip_if_active`, `coalesce`, `always_enqueue`
  - Valeurs de `CatchUpPolicy` : `skip_missed`, `enqueue_missed_with_cap`
  - Concept de `RoutineTriggerKind` : `schedule` (→ `cron`), `webhook`, `api` (→ `interval`)
  - Pattern d'activité auditée (`logActivity`) → `AuditStep` + `RoutineRun.audit_log`

**Copyright notice Paperclip** :

```

### Hermes Agent — NousResearch/hermes-agent

- **URL** : https://github.com/NousResearch/hermes-agent
- ** ** : MIT (voir dépôt)
- **Commit de référence** : depth-1 clone (dépôt privé / inaccessible au moment du clone)
- **Concepts repris** :
  - Idée de cron en langage naturel → implémentée ici via `next_cron_datetime(expr, after)`
    sur la base d'expressions 5-champs standard (sans dépendance externe)
  - Livraison de résultats sur n'importe quelle plateforme → `target_channel` dans `Routine`

---

## Fichiers concernés

| Fichier | Rôle |
|---|---|
| `background/routines.py` | Modèles Routine, RoutineRun, AuditStep + RoutineStore + fire_routine + next_cron_datetime |
| `background/scheduler.py` | Intégration des boucles de routines dans le Scheduler existant |
| `proactive/engine.py` | Audit trail ProactiveAuditEvent par décision proactive |
| `tests/test_routines.py` | Tests de couverture |

---

*Crush V3 est un projet personnel — Max Ea, 2026.*
