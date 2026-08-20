# Copyright (C) 2026 Max Ea
# This file is part of CRUSH-OS,   .
 

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ToolResult:
    content: str
    is_error: bool = False


class Tool(ABC):
    """Base class pour tous les outils Crush.

    Chaque sous-classe définit name, description, input_schema comme attributs de classe
    (ou d'instance pour les outils à description dynamique comme CLIRunnerTool).
    """

    name: str = ""
    description: str = ""
    input_schema: dict = {}  # noqa: RUF012

    def to_claude_schema(self) -> dict:
        """Retourne le schéma au format attendu par l'API Anthropic."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    @abstractmethod
    async def execute(self, **kwargs: object) -> ToolResult: ...
