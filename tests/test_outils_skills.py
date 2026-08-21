# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS,   .


"""Les trois outils skills — jamais sondés en production parce qu'ils écrivent.

Ce que ces tests verrouillent :

  • le schéma annoncé au modèle et la signature réellement appelée disent la
    même chose (le défaut qui rendait `run_script` impossible à appeler) ;
  • un rejet sandbox dit si c'est le code généré ou le banc de test qui est
    fautif — sans quoi on cherche un bug dans une skill qui n'a rien fait ;
  • `skill_improve` ne peut pas écrire hors des skills installés : son
    argument devient un segment de chemin, et un `..` y réécrirait une
    candidate déjà validée en sandbox, juste avant que l'humain l'approuve ;
  • `skill_list` annonce les candidates en attente : l'utilisateur est le seul
    à pouvoir les installer, et rien d'autre ne peut le lui apprendre.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from crush.capabilities.skills import synthesizer as synthesizer_module
from crush.capabilities.skills.lab import SkillLab
from crush.capabilities.skills.lifecycle import SkillLifecycle
from crush.capabilities.skills.synthesizer import SkillSynthesizer
from crush.capabilities.tools import skills as module_skills
from crush.capabilities.tools.skills import (
    SkillCreateTool,
    SkillImproveTool,
    SkillListTool,
)
from crush.kernel.schemas import SkillRecord, SkillStatus
from crush.providers.memory.kernel import MemoryKernel

# ── Doublures ─────────────────────────────────────────────────────────────────


class _LLMFactice:
    """Renvoie toujours le même SKILL.md et compte les appels."""

    def __init__(self, skill_md: str) -> None:
        self._md = skill_md
        self.appels = 0

    async def complete(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
        stream: bool = False,
        context: str = "",
    ) -> str | AsyncIterator[str]:
        self.appels += 1
        return self._md

    async def health_check(self) -> bool:
        return True


class _LabFactice:
    """Rejoue un verdict imposé sans toucher au disque ni au sous-processus."""

    def __init__(self, record: SkillRecord | None) -> None:
        self._record = record
        self.trajectoires: list[dict] = []

    async def propose_from_trajectory(
        self, trajectory: dict, source_event_id: str | None = None
    ) -> SkillRecord | None:
        self.trajectoires.append(trajectory)
        return self._record


class _RegistreFactice:
    """Tient lieu du singleton `skill_registry`, et compte ses rechargements."""

    def __init__(self, skills: list[dict], *, erreur_au_rechargement: bool = False) -> None:
        self._skills = skills
        self._erreur = erreur_au_rechargement
        self.rechargements = 0
        # Nom du skill effectivement recharge : un rechargement GLOBAL
        # executerait tout installed/, sur une action que le modele
        # declenche. On veut donc voir la cible, pas seulement le compte.
        self.recharge: str | None = None

    def list_installed(self) -> list[dict]:
        return self._skills

    def reload_one(self, name: str) -> bool:
        self.rechargements += 1
        self.recharge = name
        if self._erreur:
            raise RuntimeError("skill.py illisible")


def _verdict(status: SkillStatus, notes: str | None = None) -> SkillRecord:
    return SkillRecord(name="skill-test", status=status, sandbox_notes=notes)


def _outil_create(record: SkillRecord | None) -> tuple[SkillCreateTool, _LabFactice]:
    lab = _LabFactice(record)
    return SkillCreateTool(lab=lab), lab


_SKILL_MD_VALIDE = """\
---
name: transcription-audio
description: Transcrit un fichier audio en texte.
license: MIT
metadata:
  author: test
  version: "1.0"
  tags: [audio, transcription]
---

# Transcription audio

Instructions de test.
"""


@pytest.fixture
def installes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirige la racine des skills installés que `improve_skill` relit."""
    racine = tmp_path / "installed"
    racine.mkdir()
    monkeypatch.setattr(synthesizer_module, "SKILLS_INSTALLED_DIR", racine)
    return racine


def _poser_skill(racine: Path, nom: str, corps: str = "corps initial") -> Path:
    dossier = racine / nom
    dossier.mkdir(parents=True)
    (dossier / "SKILL.md").write_text(f"---\nname: {nom}\n---\n\n{corps}\n", encoding="utf-8")
    return dossier


# ══════════════════════════════════════════════════════════════════════════════
# Schéma déclaré vs signature réelle — la classe d'erreur de run_script
# ══════════════════════════════════════════════════════════════════════════════


def _les_trois_outils() -> list[tuple[str, object]]:
    lab = _LabFactice(None)
    synth = SkillSynthesizer(llm=_LLMFactice(""))
    return [
        ("skill_create", SkillCreateTool(lab=lab)),
        ("skill_improve", SkillImproveTool(synthesizer=synth)),
        ("skill_list", SkillListTool()),
    ]


@pytest.mark.parametrize(("nom", "outil"), _les_trois_outils())
def test_chaque_propriete_du_schema_existe_dans_la_signature(nom: str, outil: object) -> None:
    # `to_claude_schema()` et non l'attribut de classe : un schéma posé en
    # attribut d'instance dans __init__ échapperait à la lecture de classe.
    schema = outil.to_claude_schema()["input_schema"]
    parametres = inspect.signature(outil.execute).parameters
    for propriete in schema["properties"]:
        assert propriete in parametres, f"{nom} : '{propriete}' annoncé, jamais accepté"


@pytest.mark.parametrize(("nom", "outil"), _les_trois_outils())
def test_tout_parametre_sans_defaut_est_declare_requis(nom: str, outil: object) -> None:
    schema = outil.to_claude_schema()["input_schema"]
    requis = set(schema.get("required", []))
    for parametre, valeur in inspect.signature(outil.execute).parameters.items():
        if valeur.kind is inspect.Parameter.VAR_KEYWORD:
            continue
        if valeur.default is inspect.Parameter.empty:
            assert parametre in requis, (
                f"{nom} : '{parametre}' est obligatoire mais absent de required"
            )


@pytest.mark.parametrize(("nom", "outil"), _les_trois_outils())
def test_le_schema_est_annonce_au_modele(nom: str, outil: object) -> None:
    schema = outil.to_claude_schema()
    assert schema["name"] == nom
    assert schema["description"].strip()
    assert schema["input_schema"]["type"] == "object"


def test_aucun_parametre_ne_designe_un_repertoire_de_destination() -> None:
    """La destination n'est pas négociable : elle vient du Lab injecté.

    Un paramètre de chemin dans le schéma rouvrirait exactement la porte que
    le gate ferme — écrire ailleurs que dans la zone tampon.
    """
    schema = SkillCreateTool(lab=_LabFactice(None)).to_claude_schema()["input_schema"]
    for propriete in schema["properties"]:
        assert not any(mot in propriete.lower() for mot in ("dir", "path", "chemin", "target"))


# ══════════════════════════════════════════════════════════════════════════════
# skill_create — entrées limites
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("vide", ["", "   ", "\n\t "])
async def test_create_refuse_une_description_vide_sans_appeler_le_lab(vide: str) -> None:
    outil, lab = _outil_create(_verdict(SkillStatus.SANDBOXED_PASS))

    resultat = await outil.execute(task_description=vide)

    assert resultat.is_error
    assert "task_description" in resultat.content
    # Le refus doit précéder la génération : sinon on paie un appel LLM pour
    # produire une skill à partir de rien.
    assert lab.trajectoires == []


async def test_create_tronque_une_description_demesuree() -> None:
    outil, lab = _outil_create(_verdict(SkillStatus.SANDBOXED_PASS))

    resultat = await outil.execute(task_description="a" * 50_000)

    assert not resultat.is_error
    assert len(lab.trajectoires[0]["task_description"]) == module_skills._TACHE_MAX_CARACTERES
    assert "tronquée" in resultat.content


async def test_create_ignore_un_historique_mal_typé() -> None:
    """Le modèle produit ce JSON : une chaîne au lieu d'une liste arrive."""
    outil, lab = _outil_create(_verdict(SkillStatus.SANDBOXED_PASS))

    resultat = await outil.execute(
        task_description="Tâche répétable.",
        messages="[{'role': 'user'}]",  # type: ignore[arg-type]
        tool_calls=["web_search", {"name": "web_search"}],  # type: ignore[list-item]
    )

    assert not resultat.is_error
    trajectoire = lab.trajectoires[0]
    assert trajectoire["messages"] == []
    assert trajectoire["tool_calls"] == [{"name": "web_search"}]


async def test_create_ecarte_les_messages_sans_role() -> None:
    outil, lab = _outil_create(_verdict(SkillStatus.SANDBOXED_PASS))

    await outil.execute(
        task_description="Tâche répétable.",
        messages=[{"content": "orphelin"}, {"role": "user", "content": "utile"}],
    )

    assert lab.trajectoires[0]["messages"] == [{"role": "user", "content": "utile"}]


# ══════════════════════════════════════════════════════════════════════════════
# skill_create — le verdict rendu à l'utilisateur
# ══════════════════════════════════════════════════════════════════════════════


async def test_create_sandbox_vert_nomme_le_geste_de_validation() -> None:
    outil, _ = _outil_create(_verdict(SkillStatus.SANDBOXED_PASS))

    resultat = await outil.execute(task_description="Tâche répétable.")

    assert not resultat.is_error
    assert "validation humaine" in resultat.content.lower()
    assert "/api/skills/lab/skill-test/promote" in resultat.content
    # Sans le refus, l'utilisateur n'a qu'une moitié du choix.
    assert "/api/skills/lab/skill-test/reject" in resultat.content


async def test_create_rejet_de_code_designe_le_code() -> None:
    outil, _ = _outil_create(
        _verdict(
            SkillStatus.SANDBOXED_FAIL,
            "[instantiate] instantiation a échoué : TypeError('argument manquant')",
        )
    )

    resultat = await outil.execute(task_description="Tâche répétable.")

    assert resultat.is_error
    assert "rejetée" in resultat.content.lower()
    assert "code généré est fautif" in resultat.content
    assert "instantiate" in resultat.content
    assert "TypeError" in resultat.content


async def test_create_rejet_d_environnement_disculpe_la_skill() -> None:
    """Le défaut vu en développement : la sandbox ne voit aucune dépendance.

    Rendu comme un rejet ordinaire, il fait chercher un bug dans une skill
    parfaitement saine — et masque la panne réelle.
    """
    outil, _ = _outil_create(
        _verdict(
            SkillStatus.SANDBOXED_FAIL,
            "[import] SkillBase indisponible dans la sandbox : "
            "ModuleNotFoundError(\"No module named 'yaml'\")",
        )
    )

    resultat = await outil.execute(task_description="Tâche répétable.")

    assert resultat.is_error
    assert "banc de test" in resultat.content
    assert "n'est PAS en cause" in resultat.content
    assert "code généré est fautif" not in resultat.content


@pytest.mark.parametrize(
    "notes",
    [
        # Couche posée par le Lab quand il constate lui-même que la machine est
        # en faute ; le drapeau qui l'accompagne n'est pas persisté, seule la
        # note arrive jusqu'ici.
        "[sandbox_env] ENVIRONNEMENT SANDBOX INCOMPLET — la candidate n'est pas "
        "en cause. src=... deps=[...]",
        "[sandbox_error] Erreur infrastructure sandbox : FileNotFoundError('docker')",
        "[parse] sortie sandbox non-JSON (rc=1): stdout='' stderr='Traceback'",
    ],
)
async def test_create_panne_du_banc_de_test_reconnue_par_sa_couche(notes: str) -> None:
    outil, _ = _outil_create(_verdict(SkillStatus.SANDBOXED_FAIL, notes))

    resultat = await outil.execute(task_description="Tâche répétable.")

    assert "banc de test" in resultat.content
    assert "code généré est fautif" not in resultat.content


async def test_create_timeout_ne_condamne_pas_la_skill_d_emblee() -> None:
    outil, _ = _outil_create(_verdict(SkillStatus.SANDBOXED_FAIL, "[timeout] timeout après 30s"))

    resultat = await outil.execute(task_description="Tâche répétable.")

    assert resultat.is_error
    assert "boucle" in resultat.content or "saturée" in resultat.content
    assert "relancer" in resultat.content.lower()


async def test_create_statut_intermediaire_n_est_pas_annonce_comme_un_rejet() -> None:
    """Le Lab peut rendre un record encore CANDIDATE si le gate n'a pas tranché."""
    outil, _ = _outil_create(_verdict(SkillStatus.CANDIDATE))

    resultat = await outil.execute(task_description="Tâche répétable.")

    assert resultat.is_error
    assert "rejetée" not in resultat.content.lower()
    assert "candidate" in resultat.content


async def test_create_sans_candidate_nomme_les_deux_causes_possibles() -> None:
    outil, _ = _outil_create(None)

    resultat = await outil.execute(task_description="Tâche répétable.")

    assert resultat.is_error
    assert "SKILL.md" in resultat.content
    assert "name" in resultat.content
    assert "installé" in resultat.content


async def test_create_remonte_une_panne_du_lab_sans_planter() -> None:
    class _LabQuiCasse:
        async def propose_from_trajectory(self, trajectory: dict) -> SkillRecord | None:
            raise RuntimeError("base verrouillée")

    resultat = await SkillCreateTool(lab=_LabQuiCasse()).execute(task_description="Tâche.")

    assert resultat.is_error
    assert "base verrouillée" in resultat.content


# ══════════════════════════════════════════════════════════════════════════════
# skill_create — la zone tampon reste la seule destination
# ══════════════════════════════════════════════════════════════════════════════


def _lab_reel(tmp_path: Path, skill_md: str) -> tuple[SkillLab, _LLMFactice, Path, Path]:
    candidates = tmp_path / "candidates"
    installed = tmp_path / "installed"
    llm = _LLMFactice(skill_md)
    lab = SkillLab(
        kernel=MemoryKernel(db_path=tmp_path / "memory.db"),
        lifecycle=SkillLifecycle(db_path=tmp_path / "memory.db"),
        synthesizer=SkillSynthesizer(llm=llm),
        candidates_dir=candidates,
        installed_dir=installed,
    )
    return lab, llm, candidates, installed


async def test_create_n_ecrit_jamais_hors_de_la_zone_tampon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quel que soit le verdict du banc de test, installed/ reste vide."""
    monkeypatch.setattr(module_skills.settings, "docker_enabled", False, raising=False)
    lab, _, candidates, installed = _lab_reel(tmp_path, _SKILL_MD_VALIDE)

    await SkillCreateTool(lab=lab).execute(task_description="Transcrire un audio.")

    assert (candidates / "transcription-audio" / "skill.py").exists()
    assert not installed.exists() or not any(installed.iterdir())


@pytest.mark.parametrize(
    "nom_produit",
    ["../../installed/evil", "../candidates/../../evil", "Evil Skill", "..", "a/b"],
)
async def test_create_refuse_un_nom_de_skill_traversant(
    tmp_path: Path, nom_produit: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Le nom du dossier vient du modèle : il ne doit jamais désigner un chemin."""
    monkeypatch.setattr(module_skills.settings, "docker_enabled", False, raising=False)
    skill_md = _SKILL_MD_VALIDE.replace("name: transcription-audio", f"name: {nom_produit}")
    lab, _, candidates, installed = _lab_reel(tmp_path, skill_md)

    resultat = await SkillCreateTool(lab=lab).execute(task_description="Transcrire un audio.")

    assert resultat.is_error
    assert not installed.exists()
    assert not candidates.exists() or list(candidates.iterdir()) == []


# ══════════════════════════════════════════════════════════════════════════════
# skill_improve
# ══════════════════════════════════════════════════════════════════════════════


async def test_improve_skill_absent_liste_ce_qui_existe(installes: Path) -> None:
    _poser_skill(installes, "web-research")
    _poser_skill(installes, "veille-technique")
    llm = _LLMFactice("")
    outil = SkillImproveTool(synthesizer=SkillSynthesizer(llm=llm))

    resultat = await outil.execute(skill_name="web-recherche", new_experience="cas limite")

    assert resultat.is_error
    assert "introuvable" in resultat.content.lower()
    assert "web-research" in resultat.content
    assert "veille-technique" in resultat.content
    # Un nom à une lettre près : le dire évite un deuxième appel au hasard.
    assert "Vouliez-vous dire" in resultat.content
    assert llm.appels == 0


async def test_improve_sans_aucun_skill_oriente_vers_la_creation(installes: Path) -> None:
    outil = SkillImproveTool(synthesizer=SkillSynthesizer(llm=_LLMFactice("")))

    resultat = await outil.execute(skill_name="inexistant", new_experience="cas limite")

    assert resultat.is_error
    assert "Aucun skill n'est installé" in resultat.content
    assert "skill_create" in resultat.content


async def test_improve_ignore_un_dossier_sans_skill_md(installes: Path) -> None:
    """`improve_skill` lit SKILL.md : annoncer un dossier sans lui est un piège."""
    _poser_skill(installes, "web-research")
    (installes / "preset-nu").mkdir()

    resultat = await SkillImproveTool(
        synthesizer=SkillSynthesizer(llm=_LLMFactice(""))
    ).execute(skill_name="inexistant", new_experience="cas limite")

    assert "web-research" in resultat.content
    assert "preset-nu" not in resultat.content


@pytest.mark.parametrize(
    "nom",
    [
        "../candidates/transcription-audio",
        "..\\installed\\web-research",
        "/etc/passwd",
        "Web-Research",
        "double--tiret",
        "-tiret-en-tete",
        "a" * 65,
        "",
        "   ",
    ],
)
async def test_improve_refuse_un_nom_qui_n_est_pas_un_nom(installes: Path, nom: str) -> None:
    llm = _LLMFactice("")
    outil = SkillImproveTool(synthesizer=SkillSynthesizer(llm=llm))

    resultat = await outil.execute(skill_name=nom, new_experience="cas limite")

    assert resultat.is_error
    assert "invalide" in resultat.content.lower()
    assert llm.appels == 0


async def test_improve_ne_peut_pas_reecrire_une_candidate_en_attente(
    installes: Path, tmp_path: Path
) -> None:
    """Le trou réel : réécrire une candidate déjà verte contourne le gate.

    La candidate a passé la sandbox et attend l'accord humain. Si son
    SKILL.md peut encore changer, l'humain valide un texte qu'il n'a pas lu.
    """
    candidate = _poser_skill(tmp_path / "candidates", "transcription-audio", "corps validé")
    avant = (candidate / "SKILL.md").read_text(encoding="utf-8")
    llm = _LLMFactice("---\nname: transcription-audio\n---\n\ncorps injecté\n")

    resultat = await SkillImproveTool(synthesizer=SkillSynthesizer(llm=llm)).execute(
        skill_name="../candidates/transcription-audio",
        new_experience="tentative",
    )

    assert resultat.is_error
    assert (candidate / "SKILL.md").read_text(encoding="utf-8") == avant
    assert not (candidate / "skill.py").exists()
    assert llm.appels == 0


async def test_improve_refuse_une_experience_vide(installes: Path) -> None:
    _poser_skill(installes, "web-research")
    llm = _LLMFactice("")
    outil = SkillImproveTool(synthesizer=SkillSynthesizer(llm=llm))

    resultat = await outil.execute(skill_name="web-research", new_experience="   ")

    assert resultat.is_error
    assert "new_experience" in resultat.content
    assert llm.appels == 0


async def test_improve_nominal_reecrit_le_skill(
    installes: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registre = _RegistreFactice([])
    monkeypatch.setattr(module_skills, "skill_registry", registre)
    _poser_skill(installes, "web-research")
    llm = _LLMFactice(_SKILL_MD_VALIDE.replace("transcription-audio", "web-research"))
    outil = SkillImproveTool(synthesizer=SkillSynthesizer(llm=llm))

    resultat = await outil.execute(skill_name="web-research", new_experience="nouveau cas")

    assert not resultat.is_error
    assert "web-research" in resultat.content
    assert "Transcrit un fichier audio" in (installes / "web-research" / "skill.yaml").read_text(
        encoding="utf-8"
    )
    # Sans rechargement, l'amélioration n'existe que sur disque : la
    # conversation suivante continue avec l'ancien prompt.
    assert registre.rechargements == 1


async def test_improve_reste_utilisable_si_le_registre_refuse_de_recharger(
    installes: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registre = _RegistreFactice([], erreur_au_rechargement=True)
    monkeypatch.setattr(module_skills, "skill_registry", registre)
    _poser_skill(installes, "web-research")
    llm = _LLMFactice(_SKILL_MD_VALIDE.replace("transcription-audio", "web-research"))

    resultat = await SkillImproveTool(synthesizer=SkillSynthesizer(llm=llm)).execute(
        skill_name="web-research", new_experience="nouveau cas"
    )

    assert not resultat.is_error
    assert "redémarrage" in resultat.content


# ══════════════════════════════════════════════════════════════════════════════
# skill_list
# ══════════════════════════════════════════════════════════════════════════════


def _outil_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    installes: list[dict] | None = None,
) -> tuple[SkillListTool, SkillLifecycle, Path]:
    monkeypatch.setattr(module_skills, "skill_registry", _RegistreFactice(installes or []))
    lifecycle = SkillLifecycle(db_path=tmp_path / "memory.db")
    candidates = tmp_path / "candidates"
    candidates.mkdir()
    return SkillListTool(lifecycle=lifecycle, candidates_dir=candidates), lifecycle, candidates


def _candidate_verte(
    lifecycle: SkillLifecycle,
    candidates: Path,
    nom: str,
    description: str,
    tags: str = "[audio]",
) -> None:
    lifecycle.create_candidate(name=nom)
    lifecycle.mark_sandbox_result(name=nom, passed=True, notes="[ok] validée")
    (candidates / nom).mkdir(parents=True, exist_ok=True)
    (candidates / nom / "skill.yaml").write_text(
        f"name: {nom}\ndescription: {description}\ntags: {tags}\n", encoding="utf-8"
    )


async def test_list_vide_le_dit_sans_ambiguite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outil, _, _ = _outil_list(tmp_path, monkeypatch)

    resultat = await outil.execute()

    assert not resultat.is_error
    assert "Aucun skill" in resultat.content


async def test_list_annonce_la_candidate_qui_attend_un_accord(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Le défaut de fond : une candidate verte attend, et personne ne le sait.

    L'assistant est le seul canal conversationnel vers l'utilisateur ; s'il ne
    peut pas lire cette liste, l'accord humain n'est jamais demandé.
    """
    outil, lifecycle, candidates = _outil_list(tmp_path, monkeypatch)
    _candidate_verte(lifecycle, candidates, "audio-to-text-transcription", "Transcrit un audio.")

    resultat = await outil.execute()

    assert "audio-to-text-transcription" in resultat.content
    assert "Transcrit un audio." in resultat.content
    assert "/api/skills/lab/audio-to-text-transcription/promote" in resultat.content
    assert "PAS installées" in resultat.content


async def test_list_separe_l_installé_de_ce_qui_attend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Confondre les deux ferait proposer à l'utilisateur un skill inutilisable."""
    outil, lifecycle, candidates = _outil_list(
        tmp_path,
        monkeypatch,
        installes=[
            {
                "name": "web-research",
                "version": "1.0.0",
                "description": "Recherche web.",
                "tags": ["research"],
                "type": "conversational",
            }
        ],
    )
    _candidate_verte(lifecycle, candidates, "audio-to-text-transcription", "Transcrit un audio.")

    resultat = await outil.execute()

    assert "## Skills installés (1)" in resultat.content
    assert "## En attente de validation humaine (1)" in resultat.content
    assert resultat.content.index("web-research") < resultat.content.index(
        "audio-to-text-transcription"
    )


@pytest.mark.parametrize("statut", [SkillStatus.SANDBOXED_FAIL, SkillStatus.REJECTED])
async def test_list_n_annonce_que_les_candidates_reellement_en_attente(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, statut: SkillStatus
) -> None:
    outil, lifecycle, candidates = _outil_list(tmp_path, monkeypatch)
    lifecycle.create_candidate(name="recalee")
    lifecycle._update_status("recalee", statut)

    resultat = await outil.execute()

    assert "recalee" not in resultat.content


async def test_list_sans_description_sur_disque_reste_lisible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La zone tampon peut avoir été nettoyée : la ligne doit tenir quand même."""
    outil, lifecycle, _ = _outil_list(tmp_path, monkeypatch)
    lifecycle.create_candidate(name="orpheline")
    lifecycle.mark_sandbox_result(name="orpheline", passed=True, notes="[ok]")

    resultat = await outil.execute()

    assert "orpheline" in resultat.content
    assert "pas de description" in resultat.content


async def test_list_filtre_par_tag_les_deux_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outil, lifecycle, candidates = _outil_list(
        tmp_path,
        monkeypatch,
        installes=[
            {
                "name": "web-research",
                "version": "1.0.0",
                "description": "Recherche web.",
                "tags": ["research"],
                "type": "conversational",
            }
        ],
    )
    _candidate_verte(lifecycle, candidates, "transcription-audio", "Transcrit.", tags="[audio]")

    resultat = await outil.execute(filter_tag="research")

    assert "web-research" in resultat.content
    assert "transcription-audio" not in resultat.content


async def test_list_survit_a_un_skill_sans_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un skill.yaml incomplet ne doit pas emporter toute la liste."""
    outil, _, _ = _outil_list(
        tmp_path, monkeypatch, installes=[{"name": "bancal", "description": "Sans version."}]
    )

    resultat = await outil.execute()

    assert not resultat.is_error
    assert "bancal" in resultat.content


async def test_list_survit_a_des_tags_mal_typés(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un skill.yaml écrit à la main met souvent une chaîne au lieu d'une liste."""
    outil, _, _ = _outil_list(
        tmp_path,
        monkeypatch,
        installes=[
            {"name": "bancal", "version": "1.0.0", "description": "X.", "tags": "recherche"}
        ],
    )

    resultat = await outil.execute()

    assert not resultat.is_error
    assert "r, e, c, h" not in resultat.content


async def test_list_survit_a_un_yaml_de_candidate_illisible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outil, lifecycle, candidates = _outil_list(tmp_path, monkeypatch)
    _candidate_verte(lifecycle, candidates, "cassee", "peu importe")
    (candidates / "cassee" / "skill.yaml").write_text("tags: [non\nfermé", encoding="utf-8")

    resultat = await outil.execute()

    assert not resultat.is_error
    assert "cassee" in resultat.content


async def test_list_sans_base_de_lifecycle_ne_plante_pas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hors bootstrap complet, l'outil doit rester enregistrable et appelable."""
    monkeypatch.setattr(module_skills, "skill_registry", _RegistreFactice([]))
    monkeypatch.setattr(module_skills.settings, "memory_dir", str(tmp_path / "absent"))

    resultat = await SkillListTool().execute()

    assert not resultat.is_error
    assert "Aucun skill" in resultat.content


def test_description_de_candidate_est_bornee(tmp_path: Path) -> None:
    """Le skill.yaml sort du LLM : sa description arrive dans le contexte du modèle.

    Non bornée, elle noie la réponse de `skill_list` — et se retrouve juste
    au-dessus du lien de promotion, l'endroit du dispositif où l'attention de
    l'utilisateur compte le plus. Le prompt de synthèse demande 200 caractères
    mais rien ne l'imposait.
    """
    from crush.capabilities.tools.skills import _DESCRIPTION_CANDIDATE_MAX, SkillListTool

    cand = tmp_path / "candidates" / "bavarde"
    cand.mkdir(parents=True)
    (cand / "skill.yaml").write_text(
        "name: bavarde\ndescription: " + "x" * 5000 + "\n", encoding="utf-8"
    )

    outil = SkillListTool.__new__(SkillListTool)
    outil._candidates_dir = tmp_path / "candidates"  # type: ignore[attr-defined]
    description, _ = outil._fiche_candidate("bavarde")

    assert len(description) <= _DESCRIPTION_CANDIDATE_MAX + 1, (
        f"{len(description)} caractères renvoyés dans le contexte du modèle"
    )
    assert description.endswith("…"), "la troncature doit être visible"


async def test_improve_ne_recharge_que_le_skill_concerne(
    installes: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rechargement ciblé : c'est la CIBLE qui compte, pas le compte d'appels.

    Un `reload()` global exécuterait le `skill.py` de tout `installed/` via
    `exec_module`, sur une action que le modèle déclenche.
    """
    registre = _RegistreFactice([])
    monkeypatch.setattr(module_skills, "skill_registry", registre)
    _poser_skill(installes, "web-research")
    llm = _LLMFactice(_SKILL_MD_VALIDE.replace("transcription-audio", "web-research"))
    outil = SkillImproveTool(synthesizer=SkillSynthesizer(llm=llm))

    await outil.execute(skill_name="web-research", new_experience="nouveau cas")

    assert registre.recharge == "web-research"

