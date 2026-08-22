# Copyright (C) 2026 Maxime Song

"""Le graphe de l'assistant — ce dont il est fait, et ce qui est relié à quoi.

CE QUE CETTE VUE APPORTE QUE LES AUTRES N'APPORTENT PAS

Chaque page existante répond à une question isolée : l'Écosystème dit si les
maillons tiennent, le Coffre dit ce qui est retenu, l'Atelier dit ce que
l'assistant sait faire. Aucune ne répond à « comment tout ça se tient-il
ensemble ? » — quel souvenir touche quel outil, quel document concentre
l'essentiel, ce qui est relié à rien.

AUCUNE ARÊTE INVENTÉE

C'est la règle qui décide de tout ici. Un graphe où l'on ajoute des liens pour
faire joli ne dit plus rien : on ne peut plus distinguer une vraie proximité d'un
effet de mise en page, et on cesse de s'y fier. Chaque arête est donc justifiée,
et son `origine` dit d'où elle vient :

- `contenu`  — le fait est dans ce document. Vient de `mirror.grouper()`, la même
               source que l'export Markdown : la vue ne peut pas contredire
               Obsidian.
- `predicat` — le fait porte ce verbe. Regroupe « tout ce qu'il préfère », « tout
               ce qu'il a décidé ». Le SUJET, lui, ne sert à rien comme pivot :
               il vaut « max » sur la quasi-totalité des faits, et ferait un
               moyeu unique aussi gros qu'inutile.
- `memoire`  — relation explicite entre deux faits (`fact_relations`). Aujourd'hui
               ce sont zéro arêtes : la table ne se remplit que quand un fait en
               remplace un autre, et le cas ne s'est pas encore produit. Elles
               apparaîtront d'elles-mêmes.
- `nom`      — le fait NOMME un outil ou une intégration (« max uses notion »).
               Déduite par correspondance de nom, donc marquée comme telle : c'est
               la seule arête de cette vue qui relève d'une supposition.
- `pilote`   — l'outil actionne cette intégration ou cet appareil.
- `partie`   — la racine et ses grandes parties. Volontairement limitée aux
               parties : relier la racine à chaque feuille donnerait un oursin
               où tout est à un pas de tout, ce qui n'apprend rien.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from crush.interfaces.api.ecosysteme import canal_actif
from crush.kernel.remote_agents import registry
from crush.kernel.settings import settings

router = APIRouter()

RACINE = "crush"

# Quel outil actionne quoi. Recopié plutôt que déduit : « list_emails » ne dit pas
# de lui-même qu'il parle à Gmail, et une déduction par mot-clé rattacherait
# « execute_cli » à n'importe quoi. La liste est courte et se relit.
_OUTIL_VERS_INTEGRATION: dict[str, str] = {
    "list_emails": "Gmail",
    "list_calendar_events": "Google Calendar",
    "create_calendar_event": "Google Calendar",
    "notion_tasks": "Notion",
    "spotify_control": "Spotify",
    "get_weather": "Météo",
    "browser": "Web",
    "map_control": "Cartes",
    "printer_3d": "Imprimante 3D",
    "fusion_360": "Fusion 360",
    "vision": "Caméra",
    "remote_pc": "Poste distant",
}

# Ce qui peut être NOMMÉ par un souvenir. Le libellé sert de motif de recherche
# dans l'objet du fait, en minuscules — d'où des entrées courtes et sans accent.
_NOMS_RECONNUS: dict[str, str] = {
    "notion": "Notion",
    "spotify": "Spotify",
    "gmail": "Gmail",
    "calendar": "Google Calendar",
    "agenda": "Google Calendar",
    "obsidian": "Obsidian",
    "telegram": "Telegram",
    "docker": "Docker",
    "fusion": "Fusion 360",
    "python": "Python",
    "claude": "Claude",
    "gemini": "Gemini",
    "ollama": "Ollama",
}

_CANAUX = ("Telegram", "Discord", "Signal", "Slack", "WhatsApp")


@dataclass
class _Graphe:
    noeuds: dict[str, dict] = field(default_factory=dict)
    liens: list[dict] = field(default_factory=list)
    # Les paires déjà posées : un fait peut nommer deux fois la même intégration
    # dans le même objet, et le rendu superposerait deux arêtes identiques.
    _vues: set[tuple[str, str]] = field(default_factory=set)

    def noeud(self, nid: str, type_: str, label: str, detail: str = "") -> str:
        if nid not in self.noeuds:
            self.noeuds[nid] = {
                "id": nid,
                "type": type_,
                "label": label,
                "detail": detail,
                "degre": 0,
            }
        elif detail and not self.noeuds[nid]["detail"]:
            self.noeuds[nid]["detail"] = detail
        return nid

    def lien(self, de: str, vers: str, origine: str) -> None:
        if de == vers or de not in self.noeuds or vers not in self.noeuds:
            return
        cle = (de, vers) if de < vers else (vers, de)
        if cle in self._vues:
            return
        self._vues.add(cle)
        self.liens.append({"de": de, "vers": vers, "origine": origine})
        self.noeuds[de]["degre"] += 1
        self.noeuds[vers]["degre"] += 1


def _mots(texte: str) -> str:
    return texte.lower()


def _construire(request: Request) -> _Graphe:
    g = _Graphe()
    nom_assistant = settings.assistant_name or "Crush"
    g.noeud(RACINE, "racine", nom_assistant, "Le point de départ. Tout le reste en découle.")

    # ── La mémoire : documents, faits, verbes ─────────────────────────────────
    mirror = getattr(request.app.state, "memory_mirror", None)
    kernel = getattr(request.app.state, "memory_kernel", None)
    documents = mirror.grouper() if mirror is not None else []

    for doc in documents:
        did = g.noeud(
            "doc:" + doc.fichier,
            "document",
            doc.titre,
            f"{doc.fichier} — {len(doc.faits)} souvenir(s).",
        )
        g.lien(RACINE, did, "partie")

        for f in doc.faits:
            fid = g.noeud(
                "fait:" + f.id,
                "fait",
                f"{f.predicate} {f.object}",
                f"{f.subject} {f.predicate} {f.object}\n"
                f"confiance {f.confidence:.0%} · importance {f.importance:.0%} · "
                f"vu {f.support_count}× · ^{f.id.replace('_', '-')}",
            )
            g.lien(did, fid, "contenu")

            pid = g.noeud(
                "verbe:" + f.predicate,
                "verbe",
                f.predicate,
                f"Le verbe « {f.predicate} », commun à plusieurs souvenirs.",
            )
            g.lien(fid, pid, "predicat")

    # Relations explicites entre faits. Vides aujourd'hui — voir l'en-tête.
    if kernel is not None:
        for doc in documents:
            for f in doc.faits:
                for rel in kernel.list_relations(f.id):
                    a, b = "fait:" + rel.from_fact_id, "fait:" + rel.to_fact_id
                    if a in g.noeuds and b in g.noeuds:
                        g.lien(a, b, "memoire")

    # ── Ce qu'il sait faire : outils et intégrations ──────────────────────────
    # `schemas()` est l'API réelle du registre — et elle porte la description que
    # le modèle lui-même lit, donc exactement ce qu'il faut afficher dans la fiche.
    registre = getattr(request.app.state, "tool_registry", None)
    schemas: list[dict] = []
    if registre is not None:
        try:
            schemas = registre.schemas()
        except Exception:  # noqa: BLE001 — un registre en vrac ne casse pas la vue
            schemas = []

    for sch in sorted(schemas, key=lambda s: str(s.get("name", ""))):
        nom = str(sch.get("name", "")).strip()
        if not nom:
            continue
        oid = g.noeud("outil:" + nom, "outil", nom, str(sch.get("description", ""))[:400])
        cible = _OUTIL_VERS_INTEGRATION.get(nom)
        if cible:
            iid = g.noeud("integ:" + cible, "integration", cible, f"Service extérieur : {cible}.")
            g.lien(RACINE, iid, "partie")
            g.lien(oid, iid, "pilote")
        else:
            # Un outil qui ne parle à rien d'extérieur reste rattaché à la racine :
            # sinon il flotterait seul, ce qui se lit comme une anomalie alors que
            # c'en est un qui n'a simplement pas de service derrière.
            g.lien(RACINE, oid, "partie")

    # ── Un souvenir qui NOMME un outil ou un service ──────────────────────────
    for doc in documents:
        for f in doc.faits:
            objet = _mots(f.object)
            for motif, libelle in _NOMS_RECONNUS.items():
                if motif not in objet:
                    continue
                cid = next(
                    (
                        c
                        for c in ("integ:" + libelle, "outil:" + libelle.lower())
                        if c in g.noeuds
                    ),
                    None,
                )
                if cid is None:
                    cid = g.noeud(
                        "integ:" + libelle, "integration", libelle, f"Service : {libelle}."
                    )
                    g.lien(RACINE, cid, "partie")
                g.lien("fait:" + f.id, cid, "nom")

    # ── Les canaux ───────────────────────────────────────────────────────────
    for canal in _CANAUX:
        actif = canal_actif(canal)
        cid = g.noeud(
            "canal:" + canal,
            "canal",
            canal,
            ("Actif." if actif else "Configuré mais éteint.") + f" Canal de messagerie {canal}.",
        )
        g.lien(RACINE, cid, "partie")

    # ── Les machines reliées ─────────────────────────────────────────────────
    for agent in registry.list_agents():
        aid = g.noeud(
            "appareil:" + agent.name,
            "appareil",
            agent.name,
            f"{agent.platform} — {len(agent.actions)} action(s) : {', '.join(agent.actions)}",
        )
        g.lien(RACINE, aid, "partie")
        if "outil:remote_pc" in g.noeuds:
            g.lien("outil:remote_pc", aid, "pilote")

    # ── Les skills ───────────────────────────────────────────────────────────
    skills = getattr(request.app.state, "skill_registry", None)
    if skills is not None:
        for nom in sorted(_noms_de_skills(skills)):
            sid = g.noeud("skill:" + nom, "skill", nom, f"Skill « {nom} ».")
            g.lien(RACINE, sid, "partie")

    return g


def _noms_de_skills(registre: object) -> list[str]:
    """Le registre de skills est un singleton historique de forme instable."""
    for attribut in ("names", "list_names"):
        methode = getattr(registre, attribut, None)
        if callable(methode):
            try:
                return [str(n) for n in methode()]
            except Exception:  # noqa: BLE001 — un registre absent ne casse pas la vue
                return []
    brut = getattr(registre, "skills", None) or getattr(registre, "_skills", None) or {}
    if isinstance(brut, dict):
        return [str(k) for k in brut]
    return []


@router.get("/api/graphe")
async def lire_le_graphe(request: Request) -> dict[str, Any]:
    """Les nœuds et les arêtes, avec de quoi remplir les panneaux de la vue.

    `moyeux` est calculé ici et non côté navigateur : c'est le même tri pour
    tout le monde, et la page n'a pas à redécouvrir ce que le serveur sait déjà.
    """
    mirror = getattr(request.app.state, "memory_mirror", None)
    if mirror is None:
        raise HTTPException(503, "Miroir mémoire non disponible.")

    g = _construire(request)
    noeuds = list(g.noeuds.values())

    par_type: dict[str, int] = {}
    for n in noeuds:
        par_type[n["type"]] = par_type.get(n["type"], 0) + 1

    isoles = [n["label"] for n in noeuds if n["degre"] == 0]

    return {
        "noeuds": noeuds,
        "liens": g.liens,
        "par_type": par_type,
        # Les plus reliés d'abord. C'est la réponse à « par quoi tout passe ? ».
        "moyeux": [
            {"id": n["id"], "label": n["label"], "type": n["type"], "degre": n["degre"]}
            for n in sorted(noeuds, key=lambda n: -n["degre"])[:12]
        ],
        # Dit à voix haute ce qui n'est relié à rien, au lieu de le laisser
        # flotter dans un coin où on le prendra pour un défaut d'affichage.
        "isoles": isoles,
        "total": {"noeuds": len(noeuds), "liens": len(g.liens)},
    }


# ── Où il est ─────────────────────────────────────────────────────────────────
#
# Placé ici plutôt que dans un module à part : c'est trois lignes, et la même
# question que le graphe — de quoi cet assistant est-il fait, et qui est là.


@router.get("/api/presence")
async def lire_la_presence(request: Request) -> dict[str, Any]:
    """Ce qu'on sait de sa joignabilité, et ce qu'on ne sait pas.

    `a_la_maison` reste `null` tant que Home Assistant n'est pas branché : le
    tailnet dit qu'un appareil est CONNECTÉ, pas où il se trouve. Un téléphone
    en ligne l'est aussi bien dans le salon que dans un train.
    """
    presence = getattr(request.app.state, "presence", None)
    if presence is None:
        raise HTTPException(503, "Mesure de présence non disponible.")
    etat = await presence.etat()
    return {
        "joignable": etat.joignable,
        "au_poste": etat.au_poste,
        "a_la_maison": etat.a_la_maison,
        "resume": etat.resume(),
        "appareils": etat.appareils,
        "mesure_le": etat.mesure_le.isoformat() if etat.mesure_le else None,
        "erreur": etat.erreur,
    }
