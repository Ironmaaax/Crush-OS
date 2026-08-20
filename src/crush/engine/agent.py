# Copyright (C) 2026 Barthélemy Houot
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

from loguru import logger

from crush.engine.reflection import needs_reflection
from crush.engine.session import Session
from crush.kernel.contracts import (
    InitiativeStore,
    LLMProvider,
    MemoryIndex,
    SkillRegistry,
    ToolRegistry,
    TopicStore,
)
from crush.kernel.paths import PROMPTS_DIR
from crush.kernel.prompts import render_file
from crush.kernel.schemas import ToolCapture
from crush.kernel.settings import Settings

_STATIC_PROMPT_PATH = PROMPTS_DIR / "system_static.md"
# Variante vocale : meme structure, mais depouillee de tout ce qui ne se dit
# pas a voix haute (livrables fichiers, markdown, routage [BG:PROJECT]).
# Le prompt bureau pese ~7 800 tokens, dont le prefill se paie en attente
# devant l'utilisateur a chaque prise de parole.
_VOICE_PROMPT_PATH = PROMPTS_DIR / "system_voice.md"
# Caractere partage par les deux canaux : l'assistant doit etre le meme
# qu'on lui parle ou qu'on lui ecrive. Injecte via {{persona}}.
_PERSONA_PATH = PROMPTS_DIR / "persona.md"
_MAX_TOOL_RESULT_CHARS = 12_000


def _clip_tool_result(text: str) -> str:
    if len(text) <= _MAX_TOOL_RESULT_CHARS:
        return text
    return (
        text[:_MAX_TOOL_RESULT_CHARS]
        + f"\n...[truncated, {len(text)} characters total]"
    )


class Agent:
    """Construit le prompt (static + dynamic), appelle le LLM, retourne le stream.

    Phase C : `settings` injecté au constructeur (auparavant
    `from config.settings import settings as _s` en local dans
    `_build_system()`). Les autres dépendances (llm, memory_index,
    topic_store, tool_registry, skill_registry, user_prefs_path,
    user_model_path) étaient déjà injectées en Phase pré-C.

    Note CYCLE 1 (CDC §C.1.3) : `from crush.providers.llm.api import
    ToolCapture` au top-level franchit la couche engine → providers.
    Cette dépendance sera résolue dans un commit dédié post-gateway
    en faisant remonter `ToolCapture` (et `UsageEntry`, `calculate_cost`)
    dans `kernel/`. Hors-périmètre du commit présent.
    """

    def __init__(
        self,
        settings: Settings,
        llm: LLMProvider,
        memory_index: MemoryIndex | None = None,
        topic_store: TopicStore | None = None,
        tool_registry: ToolRegistry | None = None,
        user_prefs_path: Path | None = None,
        skill_registry: SkillRegistry | None = None,
        user_model_path: Path | None = None,
        prompt_path: Path | None = None,
        llm_reflexion: LLMProvider | None = None,
        initiative_store: InitiativeStore | None = None,
    ) -> None:
        self._settings = settings
        self._llm = llm
        # Jumeau raisonnant, câblé sur le canal vocal uniquement : `self._llm`
        # y répond sans réfléchir pour tenir la latence, ce qui dessert les
        # questions de fond. `_choisir_llm` arbitre par message.
        self._llm_reflexion = llm_reflexion
        self._initiative_store = initiative_store
        self._memory_index = memory_index
        self._topic_store = topic_store
        self._tool_registry = tool_registry
        self._user_prefs_path = user_prefs_path
        self._skill_registry = skill_registry
        self._user_model_path = user_model_path
        self._prompt_path = prompt_path or _STATIC_PROMPT_PATH

    def _build_system(
        self,
        notifications: list[str] | None = None,
        recall_summary: str | None = None,
        reflexion: bool = False,
    ) -> str:
        """Assemble le prompt système : partie statique + contexte dynamique.

        `reflexion` lève la consigne de brièveté du canal vocal pour ce tour.
        Sans elle, allouer un budget de raisonnement ne sert à rien : mesuré sur
        « vaut-il mieux louer ou acheter », prompt vocal et outils déclarés, le
        modèle produit 71 tokens de sortie et ne réfléchit pas du tout, contre
        483 avec le même budget et le même prompt sans outils. La consigne de
        concision l'emporte sur le budget — il faut donc la desserrer là où on
        veut vraiment qu'il réfléchisse.
        """
        _s = self._settings

        # Le prompt est un GABARIT : `{{user}}` et `{{assistant}}` y sont
        # résolus explicitement. L'ancienne version faisait deux `str.replace`
        # sur les chaînes littérales « Barth » et « Crush » — substitution non
        # ancrée, invisible depuis le fichier de prompt, et limitée à ce seul
        # fichier. Un placeholder inconnu lève désormais une erreur au lieu de
        # produire un prompt à moitié rendu.
        firstname = _s.display_name
        assistant_name = _s.display_assistant_name
        # Le persona est rendu d'abord — il contient lui-même {{user}} et
        # {{assistant}} — puis injecté dans le prompt du canal.
        persona = render_file(_PERSONA_PATH, user=firstname, assistant=assistant_name)
        static_system = render_file(
            self._prompt_path,
            user=firstname,
            assistant=assistant_name,
            persona=persona,
        )
        if _s.quebec_mode:
            static_system += (
                "\n\n## Mode Québécois (ACTIF)\n"
                "Tu parles avec un accent et du dialecte québécois authentique. "
                "Utilise : 'ostie', 'câlice', 'tabarnak' (avec parcimonie),"
                " 'c'est le boutte', 'en masse', 'pantoute', 'tantôt', 'maudit', 'icitte',"
                " 'chu' (je suis), 'ben' (bien), 'toé', 'moé', 'faque', 't'sé',"
                " 'un char' (voiture), 'magasiner' (shopping). "
                f"Garde la personnalité {assistant_name} (direct, efficace, ironie)"
                " avec la couleur québécoise."
            )
        dynamic_parts: list[str] = ["=== CONTEXTE DYNAMIQUE ==="]

        # Identité LLM — indispensable pour les modèles locaux qui ne savent pas ce qu'ils sont
        if _s.llm_provider == "local":
            llm_id = f"Ollama / {_s.ollama_model}"
        else:
            _model_map = {
                "anthropic": _s.anthropic_model,
                "mistral": _s.mistral_model,
                "openai": _s.openai_model,
                # Absent de cette table, le backend actif se voyait annoncer le
                # modèle Anthropic : l'assistant se croyait sur Claude.
                "gemini": _s.gemini_model,
            }
            llm_id = _model_map.get(_s.api_backend, _s.anthropic_model)
        dynamic_parts.append(f"## Moteur LLM actif\n\nTu tournes sur **{llm_id}**.")

        # Date/heure toujours injectée — utile pour le calendrier et les calculs temporels
        now = datetime.now()
        dynamic_parts.append(f"## Date et heure\n\n{now.strftime('%Y-%m-%d %H:%M')}")

        # Repères sur l'utilisateur, tenus dans la configuration. Ils n'étaient
        # injectés nulle part : l'assistant répondait « je ne sais pas encore où
        # tu habites » alors que HOME_CITY était renseignée depuis le premier
        # jour, et allait chercher la météo d'une ville par défaut.
        reperes: list[str] = []
        ville = (_s.home_city or "").strip()
        if ville:
            reperes.append(
                f"- Réside à **{ville}**. C'est la ville sous-entendue quand "
                f"{firstname} dit « ici », « chez moi », ou demande la météo "
                "sans préciser."
            )
        veille = (_s.proactive_city or "").strip()
        if veille and veille.lower() != ville.lower():
            reperes.append(f"- Ville suivie par le briefing proactif : {veille}.")
        if reperes:
            dynamic_parts.append(f"## Repères sur {firstname}\n\n" + "\n".join(reperes))

        if recall_summary:
            dynamic_parts.append(f"## Rappel de sessions précédentes\n\n{recall_summary}")

        if self._user_model_path is not None and self._user_model_path.exists():
            model_text = self._user_model_path.read_text(encoding="utf-8").strip()
            if model_text:
                dynamic_parts.append(f"## Modèle utilisateur\n\n{model_text}")

        if self._user_prefs_path is not None and self._user_prefs_path.exists():
            prefs = self._user_prefs_path.read_text(encoding="utf-8").strip()
            if prefs:
                dynamic_parts.append(f"## Préférences {firstname}\n\n{prefs}")

        if self._memory_index is not None:
            index_content = self._memory_index.read()
            dynamic_parts.append(f"## Mémoire index\n\n{index_content}")

        if self._topic_store is not None:
            topic_names = self._topic_store.list_all()
            if topic_names:
                names_list = "\n".join(f"- `{name}`" for name in topic_names)
                dynamic_parts.append(
                    "## Fichiers thématiques disponibles\n\n"
                    "Ces fichiers ne sont PAS préchargés. Pour les consulter, utilise "
                    "`memory_search` (recherche sémantique) puis `memory_load_topic(filename=...)` "
                    "pour lire un fichier complet si nécessaire (routing [CF]).\n\n"
                    f"{names_list}"
                )

        if self._tool_registry is not None and self._tool_registry.has_tools():
            noms = {s["name"] for s in self._tool_registry.schemas()}
            tool_lines = "\n".join(
                f"- `{s['name']}` : {s['description']}" for s in self._tool_registry.schemas()
            )
            dynamic_parts.append(
                f"## Outils disponibles (router [CF] pour les utiliser)\n\n{tool_lines}"
            )
            # Sorti de la liste et énoncé comme une règle. Noyé parmi vingt
            # autres outils, `report_missing_capability` n'a jamais été appelé
            # une seule fois : le réflexe d'un modèle face à une demande qu'il
            # ne sait pas traiter est de s'excuser, pas de parcourir son
            # inventaire. Le Skill Lab n'a donc jamais rien reçu à étudier.
            if "report_missing_capability" in noms:
                dynamic_parts.append(
                    "## Règle — face à une demande que tu ne sais pas traiter\n\n"
                    "Quand aucun outil ni aucune compétence ne couvre ce que "
                    f"{firstname} demande, appelle `report_missing_capability` "
                    "AVANT de répondre que tu ne sais pas faire. Décris le besoin "
                    "en une ou deux phrases, telles qu'un développeur les lirait.\n\n"
                    "Ce n'est pas une formalité : c'est ainsi que de nouvelles "
                    "capacités te sont ajoutées. Un « je ne peux pas » sans "
                    "signalement laisse le manque invisible, et il se reproduira "
                    "à l'identique.\n\n"
                    "Ne le fais PAS pour : une information que tu peux chercher, "
                    "une tâche qu'un outil existant couvre déjà, une demande "
                    "refusée pour une autre raison que l'absence de moyen."
                )

        if self._skill_registry is not None:
            skills_prompt = self._skill_registry.get_combined_system_prompt()
            if skills_prompt:
                dynamic_parts.append("# SKILLS ACTIFS\n\n" + skills_prompt)

        if notifications:
            notif_content = "\n".join(f"- {n}" for n in notifications)
            dynamic_parts.append(
                f"## Notifications en attente — À GLISSER EN FIN DE RÉPONSE\n\n{notif_content}"
            )

        # Suggestions proactives en attente. UNE LIGNE, pas la liste : le
        # moteur tourne toutes les trois heures sur une machine allumee en
        # permanence, et il y en avait quinze en souffrance. Les enumerer a
        # chaque tour noierait la conversation et couterait leur prefill a
        # chaque appel. Le detail se lit par l'outil, sur demande.
        if self._initiative_store is not None:
            try:
                # Meme filtre que l'outil : seul le mode `validate` attend
                # une decision. Les `notify` ont deja ete delivrees mais
                # gardent le statut « pending », d'ou un compteur qui en
                # annoncait 36 pour 15 reellement en souffrance.
                en_attente = sum(
                    1
                    for i in self._initiative_store.load_pending_all(days=7)
                    if str(getattr(getattr(i, "execution_mode", ""), "value",
                                   getattr(i, "execution_mode", ""))).lower() == "validate"
                )
            except Exception:  # noqa: BLE001 — un magasin illisible n'empeche pas de parler
                en_attente = 0
            if en_attente:
                dynamic_parts.append(
                    "## Suggestions en attente" + chr(10) + chr(10)
                    + f"Tu as préparé {en_attente} suggestion(s) qui attendent la "
                    + f"décision de {firstname}. Ne les récite PAS : mentionne "
                    + "qu'elles existent seulement si le moment s'y prête, ou "
                    + "s'il te les demande. Pour les lire ou trancher, appelle "
                    + "l'outil `initiatives`, et propose-les UNE À LA FOIS."
                )

        if reflexion:
            # Placé en dernier, donc au plus près de la question : c'est la
            # position où une consigne l'emporte sur celles qui précèdent.
            dynamic_parts.append(
                "## Ce tour-ci : prends le temps\n\n"
                "Cette question demande un vrai raisonnement, pas un réflexe. "
                "Pèse les termes du problème avant de parler, puis réponds à "
                "l'oral en trois ou quatre phrases — la consigne de concision "
                "des tours ordinaires ne s'applique pas ici. Reste parlé : "
                "aucune liste, aucun titre, aucune énumération numérotée. "
                "Donne une réponse tranchée, pas un catalogue d'options."
            )

        return static_system + "\n\n" + "\n\n".join(dynamic_parts)

    def has_tools(self) -> bool:
        return (
            self._tool_registry is not None
            and self._tool_registry.has_tools()
            and self._llm.supports_tools
        )

    async def respond(
        self,
        session: Session,
        user_message: str,
        stream: bool = True,
        notifications: list[str] | None = None,
    ) -> str | AsyncIterator[str]:
        """Routing-only pass : ajoute le message, appelle le LLM SANS outils (streaming).

        Le gateway lit le tag [I/CF/BG] depuis le stream et décide ensuite si un
        tool_loop est nécessaire (uniquement pour CF). Pour BG, le worker fait le vrai travail.
        """
        session.add_message("user", user_message)
        llm = self._choisir_llm(user_message)
        system = self._build_system(
            notifications=notifications, reflexion=llm is not self._llm
        )
        logger.debug("Agent responding", session_id=str(session.id), stream=stream)

        result = await llm.complete(
            messages=session.messages,
            system=system,
            stream=True,  # toujours streaming pour la détection du tag
            context="conversation",
        )
        if not stream:
            # collecte le stream pour les appelants non-streaming (tests, consolidation…)
            chunks: list[str] = []
            async for chunk in result:  # type: ignore[union-attr]
                chunks.append(chunk)
            text = "".join(chunks)
            session.add_message("assistant", text)
            return text
        return result

    async def respond_tools(
        self,
        session: Session,
        notifications: list[str] | None = None,
    ) -> str:
        """Tool loop sur les messages existants (user déjà ajouté par respond()).

        Conservé pour rétrocompatibilité (tests). Le gateway utilise désormais
        start_routing_stream() + finalize_tool_capture().
        """
        system = self._build_system(notifications=notifications)
        return await self._llm.tool_loop(
            messages=session.messages,
            system=system,
            tools=self._tool_registry.schemas(),  # type: ignore[union-attr]
            tool_executor=self._tool_registry.call_str,  # type: ignore[union-attr]
            context="conversation",
        )

    def start_routing_stream(
        self,
        session: Session,
        user_message: str,
        notifications: list[str] | None = None,
        recall_summary: str | None = None,
    ) -> tuple[AsyncIterator[str], ToolCapture | None]:
        """Un seul appel LLM streamé, avec outils si disponibles.

        Ajoute user_message à la session, lance le stream et retourne
        (stream, capture). Le ToolCapture est populé dès que le stream
        est entièrement consommé ; None si le provider ne supporte pas les outils.
        """
        session.add_message("user", user_message)
        llm = self._choisir_llm(user_message)
        system = self._build_system(
            notifications=notifications,
            recall_summary=recall_summary,
            reflexion=llm is not self._llm,
        )
        logger.debug("Agent routing stream", session_id=str(session.id))

        if self.has_tools() and hasattr(llm, "stream_with_capture"):
            stream, capture = llm.stream_with_capture(  # type: ignore[union-attr]
                messages=session.messages,
                system=system,
                tools=self._tool_registry.schemas(),  # type: ignore[union-attr]
            )
            return stream, capture

        # Provider sans outil (Ollama, Mistral) — wrapper async pour await complete()
        messages_snap = list(session.messages)

        async def _simple_stream() -> AsyncIterator[str]:
            result = await llm.complete(
                messages=messages_snap, system=system, stream=True, context="conversation"
            )
            async for chunk in result:  # type: ignore[union-attr]
                yield chunk

        return _simple_stream(), None

    def _choisir_llm(self, user_message: str) -> LLMProvider:
        """Voie rapide par défaut, jumeau raisonnant pour les questions de fond.

        Seul le canal vocal reçoit un `llm_reflexion` : le chat écrit garde le
        raisonnement par défaut du modèle, où quelques secondes de plus passent
        inaperçues.
        """
        if self._llm_reflexion is None or not needs_reflection(user_message):
            return self._llm
        logger.info("Réflexion activée — « {} »", user_message[:60])
        return self._llm_reflexion

    async def execute_captured_tools(self, capture: ToolCapture) -> list[str]:
        """Exécute en parallèle les tool_use capturés et retourne les résultats bruts."""
        results = await asyncio.gather(
            *(self._tool_registry.call_str(name, inp) for _, name, inp in capture.calls)  # type: ignore[union-attr]
        )
        logger.debug("Tools executed", names=[n for _, n, _ in capture.calls])
        return list(results)

    async def synthesize(
        self,
        session: Session,
        ack_text: str,
        capture: ToolCapture,
        results: list[str],
    ) -> AsyncIterator[str]:
        """Second appel LLM pour synthétiser les résultats d'outils en réponse naturelle.

        Construit le format Anthropic tool_use/tool_result et streame la synthèse.
        """
        # Bloc assistant avec le texte d'ack + les tool_use calls
        assistant_content: list[dict] = []
        if ack_text.strip():
            assistant_content.append({"type": "text", "text": ack_text})
        for tool_id, tool_name, tool_input in capture.calls:
            assistant_content.append(
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": tool_name,
                    "input": tool_input,
                }
            )

        # Bloc user avec les tool_result
        tool_result_blocks = [
            {"type": "tool_result", "tool_use_id": tid, "content": _clip_tool_result(r)}
            for (tid, _, _), r in zip(capture.calls, results, strict=True)
        ]

        messages = list(session.messages) + [
            {"role": "assistant", "content": assistant_content},
            {"role": "user", "content": tool_result_blocks},
        ]

        system = self._build_system()
        logger.debug("Agent synthesizing tool results", tools=[n for _, n, _ in capture.calls])

        # Pas de tools ici : le LLM se concentre sur la synthèse, pas de chainage.
        # Toujours la voie rapide, même quand la passe 1 a réfléchi : le
        # raisonnement portait sur QUOI faire, et il est déjà fait. Le refaire
        # pour mettre en forme un résultat d'outil ne coûterait que du silence.
        stream = await self._llm.complete(
            messages=messages, system=system, stream=True, context="conversation"
        )
        async for chunk in stream:  # type: ignore[union-attr]
            yield chunk
