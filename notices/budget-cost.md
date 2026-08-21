# Attribution — Budget & Cost Control

Les fichiers `core/budget.py` et les extensions de `agent/project_store.py`
(claim atomique, pause budgétaire) s'inspirent de l'architecture du projet
**Paperclip** (https://github.com/paperclipai/paperclip).



## Patterns réutilisés

| Pattern Paperclip (TypeScript)              | Adaptation Crush (Python)                         |
|---------------------------------------------|----------------------------------------------------|
| `budgetStatusFromObserved(obs, amount, pct)` | `BudgetGuard._scope_status(scope, spent)`          |
| `pauseScopeForBudget(policy)` DB update      | `ProjectStore.pause_for_budget(project, step_id)`  |
| `cancelWorkForScope` hook                    | `_BudgetExceeded` exception → pause propre         |
| `budgetIncidents` table (dédup warn)         | `BudgetGuard._warned: set[str]`                    |
| `resolveWindow(windowKind)` calendar_month   | `BudgetGuard._global_spent()` → JSONL mois courant |
| `approval` atomicity (`RETURNING`)           | `ProjectStore.claim_step()` via `fcntl.LOCK_EX`    |

## Différences notables

- Paperclip utilise PostgreSQL (Drizzle ORM) ; Crush utilise des fichiers JSONL/JSON.
- Le budget global mensuel est lu directement depuis les fichiers `memory_data/conso/`
  déjà écrits par `core/tracking.py` — pas de table `costEvents` séparée.
- Le verrou d'exclusivité mutuelle est `fcntl.flock(LOCK_EX)` sur un fichier
  `.crush/claims.lock` au lieu d'un `UPDATE … RETURNING` SQL.
- Les scopes supportés sont `global`, `project:<id>` et `run:<id>` (extensible).
