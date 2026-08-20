# Copyright (C) 2026 Barthélemy Houot
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

from __future__ import annotations

from loguru import logger

from crush.capabilities.tools.base import Tool, ToolResult


class ToolRegistry:
    """Registre central de tous les outils disponibles pour Crush."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._skill_tool_names: set[str] = set()

    def register(self, *tools: Tool) -> None:
        for tool in tools:
            self._tools[tool.name] = tool
            logger.debug("Tool registered", name=tool.name)

    def replace_skill_tools(self, *tools: Tool) -> None:
        """Remplace atomiquement les outils venant des skills."""
        for name in list(self._skill_tool_names):
            self._tools.pop(name, None)
        self._skill_tool_names = set()
        for tool in tools:
            self._tools[tool.name] = tool
            self._skill_tool_names.add(tool.name)
            logger.debug("Skill tool registered", name=tool.name)
        logger.info(f"Skill tools sync: {len(tools)} outil(s)")

    def has_tools(self) -> bool:
        return bool(self._tools)

    def schemas(self) -> list[dict]:
        """Retourne les schémas Claude de tous les outils enregistrés."""
        return [t.to_claude_schema() for t in self._tools.values()]

    def core_schemas(self) -> list[dict]:
        """Retourne uniquement les schémas des outils natifs (hors skills)."""
        return [
            t.to_claude_schema()
            for name, t in self._tools.items()
            if name not in self._skill_tool_names
        ]

    async def call(self, name: str, inputs: dict) -> ToolResult:
        """Exécute un outil par nom. Retourne une ToolResult d'erreur si inconnu."""
        tool = self._tools.get(name)
        if tool is None:
            logger.warning("Unknown tool called", name=name)
            return ToolResult(content=f"Outil inconnu: {name}", is_error=True)
        try:
            result = await tool.execute(**inputs)
            # Nom ET arguments : sans eux, diagnostiquer un enchainement
            # d'outils revient a deviner. Les valeurs sont tronquees pour ne
            # pas deverser un resultat entier dans le journal.
            logger.info(
                "Outil exécuté : {}({}) -> {}",
                name,
                ", ".join(f"{k}={str(v)[:40]}" for k, v in inputs.items()),
                "erreur" if result.is_error else "ok",
            )
            return result
        except Exception as e:
            logger.error("Tool execution error", name=name, error=str(e))
            return ToolResult(content=f"Erreur outil {name}: {e}", is_error=True)

    async def call_str(self, name: str, inputs: dict) -> str:
        """Wrapper call() → str pour le tool_loop du LLM provider."""
        result = await self.call(name, inputs)
        if result.is_error:
            return f"[ERREUR] {result.content}"
        return result.content
