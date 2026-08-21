# Copyright (C) 2026 Max Ea
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS,   .


"""Outil notion_tasks — lit les cases à cocher d'une section d'une page Notion.

Notion échoue de trois façons qui se ressemblent vues du serveur : jeton absent,
jeton refusé, page non partagée avec l'intégration. Les trois donnaient le même
« Notion non configuré » ou une trace httpx illisible. Chaque cas nomme
désormais ce qui manque ET le geste exact qui le répare, parce que ce geste se
fait dans l'interface Notion, hors de portée de l'assistant.
"""

from __future__ import annotations

import re

import httpx
from loguru import logger

from crush.capabilities.tools.base import Tool, ToolResult
from crush.kernel.settings import settings

_SECTION_DEFAUT = "Tâches du jour"
_URL_INTEGRATIONS = "https://www.notion.so/my-integrations"

# Un ID de page Notion est un UUID sans tirets en fin d'URL. L'utilisateur colle
# presque toujours l'URL entière dans NOTION_PAGE_ID : plutôt que de renvoyer un
# 404 énigmatique, on en extrait l'ID.
_MOTIF_ID = re.compile(r"([0-9a-fA-F]{32})")


def _normaliser_page_id(valeur: str) -> str:
    """Accepte un ID nu, un ID à tirets, ou l'URL complète de la page."""
    sans_tirets = valeur.strip().replace("-", "")
    trouve = _MOTIF_ID.findall(sans_tirets)
    return trouve[-1] if trouve else valeur.strip()


def _aide_configuration(token_manquant: bool, page_manquante: bool) -> str:
    """Message d'échec qui nomme les variables absentes et où les obtenir."""
    manquantes = []
    if token_manquant:
        manquantes.append("NOTION_TOKEN")
    if page_manquante:
        manquantes.append("NOTION_PAGE_ID")

    lignes = [
        f"Notion non configuré : il manque {' et '.join(manquantes)} "
        "dans le fichier .env du serveur.",
    ]
    if token_manquant:
        lignes.append(
            f"- NOTION_TOKEN : créer une intégration interne sur {_URL_INTEGRATIONS}, "
            "puis copier son « Internal Integration Secret » (il commence par ntn_)."
        )
    if page_manquante:
        lignes.append(
            "- NOTION_PAGE_ID : ouvrir la page des tâches dans Notion et coller son URL "
            "(les 32 caractères hexadécimaux qui la terminent suffisent)."
        )
    lignes.append(
        "Puis, dans Notion, partager la page avec l'intégration : menu « ••• » en haut "
        "à droite → « Connexions » → ajouter l'intégration. Sans ce partage, l'API "
        "répond 404 même avec un jeton valide."
    )
    # Le .env n'est lu qu'au démarrage : sans redémarrage, l'utilisateur croit
    # avoir corrigé et retombe sur le même message.
    lignes.append(
        "Enfin, redémarrer le service de l'assistant : les variables du .env ne sont "
        "lues qu'au démarrage."
    )
    return "\n".join(lignes)


def _diagnostic_http(statut: int) -> str:
    """Traduit un code HTTP Notion en cause probable et en geste réparateur."""
    if statut == 401:
        return (
            "Notion refuse le jeton (401) : NOTION_TOKEN est invalide ou l'intégration "
            f"a été révoquée. Régénérer le secret sur {_URL_INTEGRATIONS}, "
            "le remettre dans le .env du serveur, puis redémarrer le service."
        )
    if statut in (403, 404):
        return (
            f"Notion ne donne pas accès à la page ({statut}) : soit NOTION_PAGE_ID ne "
            "correspond à aucune page, soit — cas le plus fréquent — la page n'est pas "
            "partagée avec l'intégration. Dans Notion : ouvrir la page → menu « ••• » → "
            "« Connexions » → ajouter l'intégration."
        )
    if statut == 429:
        return "Notion limite le débit (429) : réessayer dans une minute."
    return f"Notion a répondu {statut}."


def _extraire_taches(blocs: list[dict], section: str) -> tuple[bool, list[str], list[str]]:
    """Retourne (section trouvée, tâches non cochées, titres rencontrés).

    Les titres sont remontés même en cas d'échec : c'est ce qui permet de dire à
    l'utilisateur quelles sections existent réellement plutôt que de lui laisser
    croire que sa liste est vide.
    """
    cible = section.strip().lower()
    dans_section = False
    taches: list[str] = []
    titres: list[str] = []
    trouvee = False

    for bloc in blocs:
        btype = bloc.get("type", "")

        if btype.startswith("heading_"):
            rich_text = bloc.get(btype, {}).get("rich_text", [])
            texte = "".join(rt.get("plain_text", "") for rt in rich_text).strip()
            if texte:
                titres.append(texte)
            if cible and cible in texte.lower():
                dans_section = True
                trouvee = True
            elif dans_section:
                # Le titre suivant clôt la section : on a tout ramassé.
                dans_section = False
            continue

        if dans_section and btype == "to_do":
            donnees = bloc.get("to_do", {})
            if not donnees.get("checked", False):
                rich_text = donnees.get("rich_text", [])
                texte = "".join(rt.get("plain_text", "") for rt in rich_text).strip()
                if texte:
                    taches.append(texte)

    return trouvee, taches, titres


class NotionTasksTool(Tool):
    """Récupère les tâches non cochées d'une section d'une page Notion."""

    name = "notion_tasks"
    description = (
        "Récupère les tâches non cochées d'une section de la page Notion de "
        f"l'utilisateur. Sans argument, lit la section « {_SECTION_DEFAUT} » ; "
        "passer `section` pour en lire une autre (le nom des sections réelles est "
        "renvoyé si celle demandée n'existe pas)."
    )
    input_schema: dict = {  # noqa: RUF012
        "type": "object",
        "properties": {
            "section": {
                "type": "string",
                "description": (
                    "Titre de la section à lire dans la page Notion, "
                    f"insensible à la casse. Défaut : « {_SECTION_DEFAUT} »."
                ),
                "default": _SECTION_DEFAUT,
            },
        },
        "required": [],
    }

    _BASE_URL = "https://api.notion.com/v1"
    _NOTION_VERSION = "2022-06-28"

    async def execute(self, **kwargs: object) -> ToolResult:
        section = str(kwargs.get("section") or _SECTION_DEFAUT)

        token = settings.notion_token.get_secret_value().strip()
        page_id_brut = settings.notion_page_id.strip()
        if not token or not page_id_brut:
            return ToolResult(
                content=_aide_configuration(not token, not page_id_brut),
                is_error=True,
            )

        headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": self._NOTION_VERSION,
        }
        page_id = _normaliser_page_id(page_id_brut)

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                blocs = await self._lire_blocs(client, headers, page_id)
        except httpx.HTTPStatusError as e:
            logger.error("notion_tasks : HTTP {}", e.response.status_code)
            return ToolResult(content=_diagnostic_http(e.response.status_code), is_error=True)
        except httpx.HTTPError as e:
            logger.error("notion_tasks : réseau {}", e)
            return ToolResult(
                content=(
                    f"Notion injoignable ({type(e).__name__}) : vérifier l'accès réseau "
                    "sortant du serveur vers api.notion.com."
                ),
                is_error=True,
            )

        trouvee, taches, titres = _extraire_taches(blocs, section)

        if not trouvee:
            connus = ", ".join(f"« {t} »" for t in titres) if titres else "aucun"
            return ToolResult(
                content=(
                    f"Section « {section} » absente de la page Notion. "
                    f"Titres présents : {connus}. "
                    "Rappeler l'outil avec `section` = l'un de ces titres, ou créer la "
                    "section dans Notion."
                ),
                is_error=True,
            )

        if not taches:
            return ToolResult(content=f"Aucune tâche non cochée dans « {section} ».")
        return ToolResult(content="\n".join(f"- {t}" for t in taches))

    async def _lire_blocs(
        self,
        client: httpx.AsyncClient,
        headers: dict,
        page_id: str,
    ) -> list[dict]:
        url = f"{self._BASE_URL}/blocks/{page_id}/children"
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json().get("results", [])
