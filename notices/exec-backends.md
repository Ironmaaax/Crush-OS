# Attribution — Execution Backends

Les fichiers `agent/backends/` (LocalBackend, DockerBackend, SSHBackend,
RemoteBackend, ScriptRPCRunner) s'inspirent de l'architecture du projet
**hermes-agent** (https://github.com/NousResearch/hermes-agent).

## Éléments repris

| Fichier Crush                     | Référence hermes-agent                                   |
|------------------------------------|----------------------------------------------------------|
| `agent/backends/base.py`           | `tools/environments/base.py` — ABC + contrat execute()  |
| `agent/backends/ssh.py`            | `tools/environments/ssh.py` — ControlMaster, hash court |
| `agent/backends/remote.py`         | `providers/managed_modal.py`, `providers/daytona.py`    |
| `agent/backends/rpc.py`            | `tools/code_execution_tool.py` — transport fichiers RPC |
| `tools/subagent.py` (SpawnSubagent)| `tools/delegate_tool.py` — contexte isolé, résumé       |
| `tools/subagent.py` (ScriptRPC)    | `tools/code_execution_tool.py` — script-via-RPC         |

## Différences architecturales

- Transport RPC : fichiers JSON dans le workspace partagé au lieu d'Unix Domain Sockets,
  ce qui fonctionne uniformément en local ET en Docker (volume monté).
- Backends simplifiés : le modèle spawn-per-call de hermes est conservé mais les
  snapshots de session shell ne sont pas repris (Crush passe par WorkerCLITool).
- Pas de ControlPersist multi-hôte ni de gestion de clés SSH temporaires.
- La sous-classe RemoteBackend remplace les providers Modal/Daytona complets.
