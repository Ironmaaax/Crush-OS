# Copyright (C) 2026 Max Ea
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS,   .


from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath

from loguru import logger

from crush.capabilities.tools.base import Tool, ToolResult
from crush.kernel.contracts import FTSIndex, TopicStore, VectorIndex
from crush.kernel.settings import settings

_EXEMPLE_NOM = "projets.md"


def _erreur_nom_topic(filename: str) -> str | None:
    """Retourne un message d'erreur si le nom de topic n'est pas sûr, sinon None.

    Le nom doit désigner un fichier posé directement dans le répertoire des
    topics. On le compare à sa propre feuille au sens Windows ET POSIX : un
    même nom peut être anodin sous un OS et désigner un autre répertoire sous
    l'autre (« C:evil.md » est un chemin relatif au lecteur C: pour Windows,
    un nom de fichier ordinaire pour Linux), et le dépôt est développé sous
    Windows pour tourner sur une Debian.

    Le message est rédigé comme une consigne : le modèle qui appelle l'outil
    doit pouvoir corriger son appel sans deviner la règle.
    """
    nom = filename.strip()
    if not nom:
        return f"Nom de fichier vide. Donne un nom simple, par exemple : {_EXEMPLE_NOM}"
    suspect = (
        PureWindowsPath(nom).name != nom
        or PurePosixPath(nom).name != nom
        or "/" in nom
        or "\\" in nom
        or ".." in nom
        or nom.startswith(".")
        or any(ord(c) < 32 for c in nom)
    )
    if suspect:
        return (
            f"Nom de fichier invalide : '{filename}'. Attendu : un nom de fichier simple, "
            "sans dossier, sans chemin absolu, sans '..' et sans point initial, "
            f"par exemple : {_EXEMPLE_NOM}"
        )
    if not nom.endswith(".md"):
        return (
            f"Nom de fichier invalide : '{filename}'. L'extension doit être '.md' en "
            "minuscules — les topics sont listés par un glob '*.md' sensible à la casse "
            f"sous Linux. Exemple : {_EXEMPLE_NOM}"
        )
    return None


class MemoryTopicWriteTool(Tool):
    """Crée ou met à jour un fichier de mémoire thématique."""

    name = "memory_write"
    description = (
        "Écrire un fichier mémoire thématique (topics) : préférences, informations "
        "personnelles, contexte projet, décision à retenir. "
        "Le fichier est CRÉÉ s'il n'existe pas encore — c'est la façon de noter un "
        "sujet nouveau. La liste des fichiers existants figure dans la section "
        "« Fichiers thématiques disponibles » du contexte. "
        "mode='replace' (défaut) remplace tout le contenu : relis d'abord le fichier "
        "avec `memory_load_topic` pour ne rien perdre. mode='append' ajoute à la fin "
        "sans rien écraser — à préférer pour enrichir un fichier existant."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": (
                    "Nom du fichier topic, terminé par '.md', sans dossier "
                    f"(ex: {_EXEMPLE_NOM}). Créé s'il n'existe pas."
                ),
            },
            "content": {
                "type": "string",
                "description": "Contenu Markdown à écrire (ou à ajouter si mode='append').",
            },
            "mode": {
                "type": "string",
                "enum": ["replace", "append"],
                "description": ("'replace' (défaut) écrase le fichier, 'append' ajoute à la fin."),
            },
        },
        "required": ["filename", "content"],
    }

    def __init__(
        self,
        topics_dir: Path | None = None,
        vector_index: VectorIndex | None = None,
    ) -> None:
        self._dir = topics_dir or (Path(settings.memory_dir) / "topics")
        self._vector_index = vector_index

    async def execute(self, filename: str, content: str, mode: str = "replace") -> ToolResult:
        erreur = _erreur_nom_topic(filename)
        if erreur is not None:
            return ToolResult(content=erreur, is_error=True)
        if mode not in ("replace", "append"):
            return ToolResult(
                content=(
                    f"Mode inconnu : '{mode}'. Valeurs acceptées : 'replace' (défaut, "
                    "remplace tout le fichier) ou 'append' (ajoute à la fin)."
                ),
                is_error=True,
            )
        nom = filename.strip()

        # Le répertoire peut ne pas exister : première écriture après un
        # déploiement neuf, ou memory_dir pointant sur un volume fraîchement
        # monté. Le créer ici évite un FileNotFoundError opaque.
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return ToolResult(
                content=(
                    f"Répertoire des topics inaccessible ({self._dir}) : {e}. "
                    "Vérifie le réglage memory_dir et les droits du service crush-api."
                ),
                is_error=True,
            )

        path = self._dir / nom
        # Ceinture et bretelles derrière la validation du nom : on vérifie sur
        # le chemin résolu que l'écriture reste dans le répertoire des topics.
        # Un lien symbolique déjà posé dans ce répertoire ferait sortir
        # l'écriture sans que ni le nom ni le parent ne trahissent quoi que ce soit.
        if path.parent.resolve() != self._dir.resolve() or path.is_symlink():
            return ToolResult(
                content=(f"Écriture refusée : '{filename}' sortirait du répertoire des topics."),
                is_error=True,
            )

        existait = path.exists()
        if existait and not path.is_file():
            return ToolResult(
                content=(
                    f"'{nom}' existe dans les topics mais n'est pas un fichier. "
                    "Choisis un autre nom."
                ),
                is_error=True,
            )

        if mode == "append" and existait:
            try:
                ancien = path.read_text(encoding="utf-8")
            except OSError as e:
                return ToolResult(
                    content=(
                        f"Impossible de relire '{nom}' pour y ajouter du contenu : {e}. "
                        "Réessaie avec mode='replace' si le contenu existant est perdable."
                    ),
                    is_error=True,
                )
            nouveau = f"{ancien.rstrip()}\n\n{content.strip()}\n" if ancien.strip() else content
        else:
            nouveau = content

        try:
            path.write_text(nouveau, encoding="utf-8")
        except OSError as e:
            return ToolResult(
                content=(
                    f"Écriture impossible dans '{path}' : {e}. "
                    "Vérifie les droits du service crush-api sur le répertoire mémoire."
                ),
                is_error=True,
            )

        action = "mise à jour" if existait else "créée"
        avertissement = await self._indexer(nom, nouveau)
        return ToolResult(
            content=f"Mémoire '{nom}' {action} ({len(nouveau)} caractères).{avertissement}"
        )

    async def _indexer(self, nom: str, contenu: str) -> str:
        """Publie le contenu dans l'index sémantique. Retourne un avertissement éventuel.

        L'indexation charge un modèle d'embedding : elle peut échouer là où
        l'écriture a réussi. Laisser remonter l'exception ferait croire au
        modèle que la note est perdue et le pousserait à réécrire en boucle,
        alors que le fichier est bien sur le disque.
        """
        if self._vector_index is None:
            return ""
        try:
            await self._vector_index.add(
                doc_id=f"topic:{nom}",
                text=contenu,
                metadata={"source": "topic", "filename": nom},
            )
            await self._vector_index.persist()
        except Exception as e:
            logger.warning("Indexation du topic échouée", file=nom, error=str(e))
            return (
                f" Attention : l'indexation sémantique a échoué ({e}) — `memory_search` "
                f"ne le trouvera pas, utilise `memory_load_topic(filename='{nom}')`."
            )
        return ""


class MemoryLoadTopicTool(Tool):
    """Charge à la demande le contenu d'un fichier thématique mémoire."""

    name = "memory_load_topic"
    description = (
        "Charger le contenu complet d'un fichier mémoire thématique (topics) à la demande. "
        "Les fichiers thématiques ne sont PLUS préchargés dans le prompt — utilise cet outil "
        "lorsque tu as besoin de consulter le détail d'un sujet précis. "
        "Conseil : utilise d'abord `memory_search` pour identifier le bon fichier."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "Nom exact du fichier topic à lire (ex: user_prefs.md).",
            },
        },
        "required": ["filename"],
    }

    def __init__(self, topic_store: TopicStore) -> None:
        self._store = topic_store

    async def execute(self, filename: str) -> ToolResult:
        erreur = _erreur_nom_topic(filename)
        if erreur is not None:
            return ToolResult(content=erreur, is_error=True)
        nom = filename.strip()
        if not self._store.exists(nom):
            existing = ", ".join(self._store.list_all()) or "(aucun)"
            return ToolResult(
                content=(
                    f"Fichier '{nom}' introuvable. Fichiers disponibles : {existing}. "
                    f"Pour créer ce sujet : memory_write(filename='{nom}', content=...)."
                ),
                is_error=True,
            )
        content = self._store.load(nom)
        return ToolResult(content=f"# {nom}\n\n{content}")


class MemorySearchTool(Tool):
    """Recherche sémantique dans la mémoire (topics + transcripts) via embeddings."""

    name = "memory_search"
    description = (
        "Recherche sémantique dans toute la mémoire (fichiers thématiques + transcripts). "
        "Renvoie les passages les plus pertinents pour la requête, avec leur source. "
        "Utiliser pour retrouver une information mémorisée avant éventuellement d'appeler "
        "`memory_load_topic` pour le détail complet d'un fichier."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Question ou mots-clés en langage naturel.",
            },
            "k": {
                "type": "integer",
                "description": "Nombre de résultats à renvoyer (défaut : 5).",
            },
        },
        "required": ["query"],
    }

    def __init__(self, vector_index: VectorIndex) -> None:
        self._index = vector_index

    async def execute(self, query: str, k: int = 5) -> ToolResult:
        if not query.strip():
            return ToolResult(content="Requête vide.", is_error=True)
        try:
            k_int = max(1, min(20, int(k)))
        except (TypeError, ValueError):
            k_int = 5
        results = await self._index.search(query=query, k=k_int)
        if not results:
            return ToolResult(content="Aucun résultat pertinent trouvé en mémoire.")
        lines: list[str] = []
        for i, r in enumerate(results, start=1):
            meta = r.get("metadata", {})
            source = meta.get("filename") or meta.get("source") or r.get("doc_id", "?")
            score = r.get("score", 0.0)
            text = r.get("text", "").strip()
            lines.append(f"[{i}] {source} (score={score:.3f})\n{text}")
        return ToolResult(content="\n\n---\n\n".join(lines))


class CrossSessionRecallTool(Tool):
    """Recherche dans les sessions passées par FTS5 + vectoriel.

    Permet à l'agent de rappeler explicitement des échanges antérieurs
    en combinant recherche plein texte exacte et recherche sémantique.
    """

    name = "session_recall"
    description = (
        "Recherche dans les sessions de conversation passées (FTS5 + sémantique). "
        "Retourne les extraits les plus pertinents des échanges précédents. "
        "Utilise pour retrouver ce qui a été dit lors de sessions antérieures, "
        "comme des décisions, préférences ou contexte de projets passés."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Question ou mots-clés à rechercher dans les sessions.",
            },
            "k": {
                "type": "integer",
                "description": "Nombre de résultats (défaut : 6).",
            },
        },
        "required": ["query"],
    }

    def __init__(self, fts_index: FTSIndex, vector_index: VectorIndex) -> None:
        self._fts = fts_index
        self._vector = vector_index

    async def execute(self, query: str, k: int = 6) -> ToolResult:  # type: ignore[override]
        import asyncio

        if not query.strip():
            return ToolResult(content="Requête vide.", is_error=True)
        k_int = max(1, min(20, int(k)))

        fts_results, vec_results = await asyncio.gather(
            self._fts.search(query, k=k_int),
            self._vector.search(query, k=k_int),
        )

        seen: set[str] = set()
        lines: list[str] = []
        for r in fts_results + vec_results:
            doc_id = r["doc_id"]
            if doc_id in seen:
                continue
            seen.add(doc_id)
            text = r["text"][:400].strip()
            score = r.get("score", 0.0)
            lines.append(f"[{doc_id}] (score={score:.3f})\n{text}")
            if len(lines) >= k_int:
                break

        if not lines:
            return ToolResult(content="Aucun résultat trouvé dans les sessions passées.")
        return ToolResult(content="\n\n---\n\n".join(lines))
