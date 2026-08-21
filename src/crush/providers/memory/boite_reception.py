# Copyright (C) 2026 Maxime Song

"""Boîte de réception Obsidian — le seul chemin d'écriture de l'humain vers la mémoire.

POURQUOI PAS LE MIROIR LUI-MÊME

`mirror.py` est unidirectionnel par construction (CDC §6.7) : chaque passe
nocturne réécrit intégralement ses fichiers depuis SQLite. Les rendre éditables
aurait deux conséquences, toutes deux mauvaises — une correction tapée dans le
train disparaîtrait au rendu suivant, et il n'existerait plus de source de vérité
unique : le Markdown et la base pourraient diverger indéfiniment, sans que rien
ne permette de trancher.

Ce module prend l'autre chemin : un fichier que le miroir n'écrit JAMAIS, relu
périodiquement, dont chaque ligne est traduite en une opération qui existait déjà
(`kernel.apply_correction`, `ingest.ingest`). Le sens d'écriture reste unique ;
ceci n'est qu'une file d'attente, pas une seconde vérité.

CE QUI EST GARANTI

- **Aucune ligne n'est perdue.** Une ligne incomprise n'est pas supprimée : elle
  descend sous « Pas comprises » avec la raison, où l'on peut la réparer. Elle
  n'est pas retentée en boucle non plus — sinon le même échec reviendrait à
  chaque passe, et le fichier grossirait tout seul.
- **Le fichier n'est réécrit que si quelque chose a été traité.** En régime
  normal — boîte vide — la Pi n'y touche pas du tout. Une synchronisation
  bidirectionnelle n'a donc aucune occasion de fabriquer un fichier de conflit
  pendant les heures où personne n'écrit rien.
- **Tout reste tracé.** Chaque opération passe par `apply_correction`, donc par
  un event `human_correction` : l'historique ne distingue pas une correction
  tapée dans Obsidian d'une correction dictée à voix haute.

LA SYNTAXE, ET POURQUOI ELLE EST AUSSI PERMISSIVE

Elle est tapée au pouce, dans un train, dans une application qui ajoute
d'elle-même des puces et des cases à cocher. Exiger une forme exacte, c'est
garantir que la moitié des lignes finiront en « pas comprises ». On accepte donc
les puces, les cases, les majuscules, les accents absents et les deux-points
facultatifs.

    faux ^fact-00c2b7c5e5 : je bois du thé, pas du café
    oublie ^fact-00c2b7c5e5
    retiens : je passe au thé vert le matin
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from loguru import logger

from crush.kernel.schemas import FactStatus, ResultatBoiteReception
from crush.providers.memory.kernel import MemoryKernel
from crush.providers.memory.mirror import MemoryMirror, id_depuis_ancre

# Le nom est en français et sans jargon : il apparaît dans la liste des fichiers
# d'Obsidian, sur un téléphone, et doit se comprendre sans avoir lu ce module.
#
# À la RACINE du miroir, jamais dans `user/` ni `crush/` : `mirror.export()`
# n'écrit que des chemins de ces deux dossiers et ne balaie pas le répertoire. La
# boîte est donc hors d'atteinte du rendu par construction, pas par convention —
# un test le vérifie (cf. tests/test_boite_reception.py).
NOM_FICHIER = "boite-de-reception.md"

_VERBES = {
    "corrige": ("faux", "non", "corrige", "corriger", "errone", "erroné"),
    "oublie": ("oublie", "oublié", "supprime", "efface", "archive"),
    "retiens": ("retiens", "note", "souviens", "rappelle"),
}

# Puce, astérisque, case à cocher : ce qu'Obsidian ajoute tout seul quand on
# continue une liste, et que l'utilisateur n'a donc pas choisi de taper.
_PREFIXE = re.compile(r"^\s*(?:[-*+]\s*)?(?:\[[ xX]?\]\s*)?")

# `^fact-00c2b7c5e5` tel qu'affiché dans le miroir, `fact_00c2b7c5e5` si
# l'identifiant vient de l'API : les deux désignent le même fait, on prend les deux.
_ANCRE = re.compile(r"\^?(fact[-_][0-9a-fA-F]{4,})")

_SEP_TRAITE = "## Traité"
_SEP_INCOMPRISES = "## Pas comprises"


@dataclass
class _Instruction:
    """Une ligne comprise, prête à être exécutée."""

    action: str
    ligne: str
    ancre: str = ""
    texte: str = ""


@dataclass
class _Lecture:
    """Ce qu'on a tiré du fichier : ce qui s'exécute, et ce qui ne se comprend pas."""

    instructions: list[_Instruction] = field(default_factory=list)
    incomprises: list[tuple[str, str]] = field(default_factory=list)
    deja_incomprises: list[str] = field(default_factory=list)


class BoiteReception:
    """Lit la boîte, exécute chaque ligne, et réécrit ce qui a été fait."""

    def __init__(
        self,
        kernel: MemoryKernel,
        mirror: MemoryMirror,
        ingest: object | None = None,
    ) -> None:
        self._kernel = kernel
        self._mirror = mirror
        # `retiens :` a besoin de la chaîne d'extraction complète — la même que
        # celle qui lit les conversations, garde-fou persona inclus. Sans elle,
        # les deux autres verbes fonctionnent quand même.
        self._ingest = ingest

    @property
    def chemin(self) -> Path:
        return self._mirror.root / NOM_FICHIER

    # ── Cycle de vie ──────────────────────────────────────────────────────────

    def creer_si_absente(self) -> bool:
        """Écrit le fichier vide et son mode d'emploi. Vrai s'il a été créé.

        Le mode d'emploi vit DANS le fichier : c'est le seul endroit où on le
        lira, puisqu'on l'ouvre sur un téléphone, loin de ce dépôt.
        """
        if self.chemin.exists():
            return False
        self.chemin.parent.mkdir(parents=True, exist_ok=True)
        self.chemin.write_text(_gabarit(), encoding="utf-8")
        logger.info("Boîte de réception créée", chemin=str(self.chemin))
        return True

    async def traiter(self) -> ResultatBoiteReception:
        """Exécute les lignes en attente. Ne touche au fichier que s'il y en avait."""
        self.creer_si_absente()

        try:
            contenu = self.chemin.read_text(encoding="utf-8")
        except OSError as exc:
            return ResultatBoiteReception(erreur=f"lecture impossible : {exc}")

        lecture = _lire(contenu)
        if not lecture.instructions and not lecture.incomprises:
            # Le cas de tous les jours. On sort SANS écrire : c'est ce qui rend
            # une synchronisation bidirectionnelle sûre le reste du temps.
            return ResultatBoiteReception()

        resultat = ResultatBoiteReception()
        comptes_rendus: list[str] = []

        for instr in lecture.instructions:
            comptes_rendus.append(await self._executer(instr, resultat))

        resultat.incomprises = len(lecture.incomprises)

        try:
            self.chemin.write_text(
                _gabarit(
                    traite=comptes_rendus,
                    incomprises=lecture.incomprises,
                    incomprises_conservees=lecture.deja_incomprises,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            # Les corrections SONT appliquées : seule la trace lisible manque.
            resultat.erreur = f"réécriture impossible : {exc}"
            logger.warning("Boîte de réception non réécrite", error=str(exc))

        # Le miroir doit refléter ce qui vient de changer. Sans ça, on corrige à
        # 10 h et le fichier continue d'afficher l'ancienne valeur jusqu'à 3 h du
        # matin — on croit alors que la correction n'a pas été prise en compte.
        if resultat.appliquees:
            try:
                self._mirror.export()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Miroir non régénéré après correction", error=str(exc))

        logger.info(
            "Boîte de réception traitée",
            appliquees=resultat.appliquees,
            ignorees=resultat.ignorees,
            incomprises=resultat.incomprises,
        )
        return resultat

    # ── Exécution d'une ligne ─────────────────────────────────────────────────

    async def _executer(self, instr: _Instruction, resultat: ResultatBoiteReception) -> str:
        if instr.action == "corrige":
            return self._corriger(instr, resultat)
        if instr.action == "oublie":
            return self._oublier(instr, resultat)
        return await self._retenir(instr, resultat)

    def _corriger(self, instr: _Instruction, resultat: ResultatBoiteReception) -> str:
        fact_id = id_depuis_ancre(instr.ancre)
        _evt, fait = self._kernel.apply_correction(
            target_fact_id=fact_id,
            new_object=instr.texte,
            correction_text=instr.ligne,
            source="obsidian",
        )
        if fait is None:
            resultat.ignorees += 1
            return f"`{instr.ligne}` → aucun fait `{fact_id}` (déjà supprimé ?)"
        resultat.appliquees += 1
        return f"`{instr.ligne}` → corrigé : **{fait.subject} {fait.predicate} {fait.object}**"

    def _oublier(self, instr: _Instruction, resultat: ResultatBoiteReception) -> str:
        fact_id = id_depuis_ancre(instr.ancre)
        # ARCHIVED, pas de suppression : le fait sort du miroir et des rappels,
        # l'historique reste intact. Un souvenir qu'on demande d'oublier est
        # justement celui dont on veut pouvoir constater plus tard qu'il a existé.
        _evt, fait = self._kernel.apply_correction(
            target_fact_id=fact_id,
            new_status=FactStatus.ARCHIVED,
            correction_text=instr.ligne,
            source="obsidian",
        )
        if fait is None:
            resultat.ignorees += 1
            return f"`{instr.ligne}` → aucun fait `{fact_id}` (déjà supprimé ?)"
        resultat.appliquees += 1
        return f"`{instr.ligne}` → oublié : **{fait.subject} {fait.predicate} {fait.object}**"

    async def _retenir(self, instr: _Instruction, resultat: ResultatBoiteReception) -> str:
        if self._ingest is None:
            resultat.ignorees += 1
            return f"`{instr.ligne}` → impossible : l'extraction de faits n'est pas branchée"
        try:
            rendu = await self._ingest.ingest(  # type: ignore[attr-defined]
                content=instr.texte,
                source="obsidian",
                event_type="user_statement",
            )
        except Exception as exc:  # noqa: BLE001
            resultat.ignorees += 1
            logger.warning("Boîte : ingestion échouée", error=str(exc))
            return f"`{instr.ligne}` → échec de l'extraction ({type(exc).__name__})"

        nouveaux = len(getattr(rendu, "new_facts", None) or [])
        confirmes = len(getattr(rendu, "confirmed", None) or [])
        resultat.appliquees += 1
        resultat.retenus += nouveaux
        if nouveaux:
            return f"`{instr.ligne}` → {nouveaux} fait(s) retenu(s)"
        if confirmes:
            return f"`{instr.ligne}` → déjà su ({confirmes} fait(s) confirmé(s))"
        return f"`{instr.ligne}` → noté, aucun fait durable à en tirer"


# ── Lecture du fichier ────────────────────────────────────────────────────────


def _lire(contenu: str) -> _Lecture:
    """Sépare les lignes à exécuter de celles qu'on ne comprend pas.

    Tout ce qui suit `## Traité` ou `## Pas comprises` est de l'historique, pas
    une consigne : le relire reviendrait à rejouer indéfiniment ce qui est fait.
    """
    lecture = _Lecture()
    zone = "consignes"
    dans_bloc = False

    for brute in contenu.splitlines():
        ligne = brute.strip()

        # Le mode d'emploi en tête de fichier montre les trois consignes dans un
        # bloc de code. Sans suivre l'ouverture du bloc, ces EXEMPLES seraient
        # exécutés à chaque passe — dont `retiens : je passe au thé vert`, qui
        # inventerait une préférence toutes les dix minutes.
        if ligne.startswith("```"):
            dans_bloc = not dans_bloc
            continue
        if dans_bloc:
            continue

        if ligne.startswith(_SEP_TRAITE):
            zone = "traite"
            continue
        if ligne.startswith(_SEP_INCOMPRISES):
            zone = "incomprises"
            continue

        if zone == "incomprises":
            # Conservées telles quelles : on ne les rejoue pas, on ne les perd pas.
            if ligne and not ligne.startswith(("<!--", "#", "_")):
                lecture.deja_incomprises.append(ligne)
            continue
        if zone == "traite":
            continue

        if not ligne or ligne.startswith(("<!--", "#", ">", "_", "|", "---")):
            continue

        instr, raison = _analyser(ligne)
        if instr is not None:
            lecture.instructions.append(instr)
        elif raison:
            lecture.incomprises.append((ligne, raison))

    return lecture


def _analyser(ligne: str) -> tuple[_Instruction | None, str]:
    """Traduit une ligne en instruction, ou dit pourquoi elle n'en est pas une."""
    corps = _PREFIXE.sub("", ligne).strip()
    if not corps:
        return None, ""

    tete = corps.split(maxsplit=1)[0]
    premier = tete.lower().rstrip(":,.")
    action = next((a for a, mots in _VERBES.items() if premier in mots), None)
    if action is None:
        return None, "ne commence pas par `faux`, `oublie` ou `retiens`"

    reste = corps[len(tete) :].strip().lstrip(":").strip()

    if action == "retiens":
        if not reste:
            return None, "`retiens` sans rien à retenir"
        return _Instruction(action="retiens", ligne=ligne, texte=reste), ""

    trouve = _ANCRE.search(reste)
    if trouve is None:
        return None, "aucune référence `^fact-…` — laquelle des mémoires ?"
    ancre = trouve.group(1)

    if action == "oublie":
        return _Instruction(action="oublie", ligne=ligne, ancre=ancre), ""

    # `faux ^fact-xxx : le bon texte` — ce qui suit l'ancre est la correction.
    apres = reste[trouve.end() :].strip().lstrip(":-").strip()
    if not apres:
        return None, "`faux` sans la version correcte — utiliser `oublie` pour supprimer"
    return _Instruction(action="corrige", ligne=ligne, ancre=ancre, texte=apres), ""


# ── Écriture du fichier ───────────────────────────────────────────────────────


def _gabarit(
    traite: list[str] | None = None,
    incomprises: list[tuple[str, str]] | None = None,
    incomprises_conservees: list[str] | None = None,
) -> str:
    # TOUT le mode d'emploi est en citation (`>`). Ce n'est pas un choix
    # d'esthétique : `_lire` ignore les lignes de citation, donc cette prose ne
    # peut pas être relue comme une consigne. Écrite en texte normal, chaque
    # phrase du mode d'emploi finissait signalée « pas comprise » à la première
    # passe — le fichier se plaignait de lui-même.
    #
    # Le reste du fichier, lui, est lu INTÉGRALEMENT : il n'y a pas de zone
    # réservée où il faudrait penser à écrire. Sur un téléphone, on ouvre le
    # fichier et on tape là où le curseur tombe ; une consigne écrite au mauvais
    # endroit doit fonctionner quand même.
    lignes = [
        "# Boîte de réception",
        "",
        "> Le seul fichier du miroir qui accepte tes modifications. Écris une",
        "> consigne par ligne, n'importe où en dehors de cette citation.",
        ">",
        "> `faux ^fact-00c2b7c5e5 : je bois du thé, pas du café`",
        "> `oublie ^fact-00c2b7c5e5`",
        "> `retiens : je passe au thé vert le matin`",
        ">",
        "> Les `^fact-…` se copient en fin de ligne depuis n'importe quel fichier",
        "> du miroir. Puces, majuscules et accents n'ont pas d'importance.",
        ">",
        "> Ce qui est traité descend plus bas, avec son résultat. Rien n'est",
        "> jamais supprimé d'ici : une ligne mal comprise attend sous « Pas",
        "> comprises », où tu peux la réparer et la remonter.",
        "",
        # Le trait et le blanc en dessous ne sont pas décoratifs : ils donnent un
        # endroit évident où poser le curseur. Sans eux, le fichier s'ouvre sur un
        # pavé de citation immédiatement suivi de l'historique, et il faut deviner
        # où l'on a le droit d'écrire. `_lire` ignore les lignes `---`.
        "---",
        "",
        "",
    ]

    if traite:
        lignes.append(f"{_SEP_TRAITE} le {datetime.now().strftime('%d/%m/%Y à %H:%M')}")
        lignes.append("")
        lignes += [f"- {c}" for c in traite]
        lignes.append("")

    restantes = list(incomprises_conservees or [])
    if incomprises:
        restantes += [f"{ligne}  <!-- {raison} -->" for ligne, raison in incomprises]

    if restantes:
        lignes.append(_SEP_INCOMPRISES)
        lignes.append("")
        lignes.append("_Rien n'a été fait de ces lignes. Corrige-les et remonte-les en haut._")
        lignes.append("")
        lignes += restantes
        lignes.append("")

    return "\n".join(lignes)
