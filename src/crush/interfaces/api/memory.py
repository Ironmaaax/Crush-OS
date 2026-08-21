# Copyright (C) 2026 Max Ea
# This file is part of CRUSH-OS,   .


from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from crush.kernel.settings import settings
from crush.providers.memory.boite_reception import NOM_FICHIER
from crush.providers.memory.mirror import ancre_de_fait
from crush.providers.memory.schemas import FactStatus

router = APIRouter()


class _ContentBody(BaseModel):
    content: str


class _CorrectionBody(BaseModel):
    target_fact_id: str
    new_status: str | None = None
    new_object: str | None = None
    new_confidence: float | None = None
    correction_text: str = ""


def _mem_dir(request: Request) -> Path:  # noqa: ARG001

    return Path(settings.memory_dir)


@router.get("/api/memory/index")
async def get_memory_index(request: Request) -> dict:
    p = _mem_dir(request) / "MEMORY.md"
    return {"content": p.read_text(encoding="utf-8") if p.exists() else ""}


@router.put("/api/memory/index")
async def put_memory_index(body: _ContentBody, request: Request) -> dict:
    p = _mem_dir(request) / "MEMORY.md"
    p.write_text(body.content, encoding="utf-8")
    return {"ok": True}


@router.get("/api/memory/topics")
async def list_memory_topics(request: Request) -> list[dict]:
    topics_dir = _mem_dir(request) / "topics"
    if not topics_dir.exists():
        return []
    result = []
    for p in sorted(topics_dir.glob("*.md")):
        stat = p.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
        result.append(
            {
                "name": p.name,
                "size": stat.st_size,
                "mtime": mtime.isoformat(),
            }
        )
    return result


@router.get("/api/memory/topics/{name}")
async def get_memory_topic(name: str, request: Request) -> dict:
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "Nom invalide")
    p = _mem_dir(request) / "topics" / name
    if not p.exists():
        raise HTTPException(404, "Fichier introuvable")
    return {"name": name, "content": p.read_text(encoding="utf-8")}


@router.put("/api/memory/topics/{name}")
async def put_memory_topic(name: str, body: _ContentBody, request: Request) -> dict:
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "Nom invalide")
    p = _mem_dir(request) / "topics" / name
    if not p.exists():
        raise HTTPException(404, "Fichier introuvable")
    p.write_text(body.content, encoding="utf-8")

    # Synchronise le VectorIndex si disponible
    vector_index = getattr(request.app.state, "vector_index", None)
    if vector_index is not None:

        async def _update_vector() -> None:
            await vector_index.add(
                doc_id=f"topic:{name}",
                text=body.content,
                metadata={"source": "topic", "filename": name},
            )
            await vector_index.persist()

        asyncio.create_task(_update_vector(), name=f"vector-update-{name}")

    return {"ok": True}


@router.delete("/api/memory/topics/{name}")
async def delete_memory_topic(name: str, request: Request) -> dict:
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "Nom invalide")
    p = _mem_dir(request) / "topics" / name
    if not p.exists():
        raise HTTPException(404, "Fichier introuvable")
    p.unlink()

    # Retire le document du VectorIndex si disponible
    vector_index = getattr(request.app.state, "vector_index", None)
    if vector_index is not None:

        async def _remove_vector() -> None:
            async with vector_index._lock:
                vector_index._remove_doc_locked(f"topic:{name}")
            await vector_index.persist()

        asyncio.create_task(_remove_vector(), name=f"vector-remove-{name}")

    return {"ok": True}


@router.get("/api/memory/facts")
async def list_memory_facts(
    request: Request,
    status: str = "active",
    category: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """Liste les facts du Kernel (vraie fenêtre sur ce que Crush sait).

    Le miroir Markdown (memory_data/mirror/) reste la version *lisible*
    régénérée par AutoDream deep ; cet endpoint expose les facts BRUTS
    pour l'UI Atelier/Mémoire. Lecture seule — la correction passe par
    POST /api/memory/correct.
    """

    kernel = getattr(request.app.state, "memory_kernel", None)
    if kernel is None:
        raise HTTPException(503, "Memory Kernel non disponible.")
    try:
        st = FactStatus(status)
    except ValueError:
        raise HTTPException(
            400, f"Status invalide '{status}'. Valeurs : {[s.value for s in FactStatus]}"
        ) from None
    facts = kernel.list_facts_by_status(st, limit=limit)
    if category:
        facts = [f for f in facts if f.category == category]
    return [
        {
            "id": f.id,
            "subject": f.subject,
            "predicate": f.predicate,
            "object": f.object,
            "category": f.category,
            "status": f.status.value,
            "confidence": round(f.confidence, 3),
            "support_count": f.support_count,
            "decay_policy": f.decay_policy.value,
            "importance": round(f.importance, 3),
            "last_seen_at": f.last_seen_at.isoformat(),
            "created_at": f.created_at.isoformat(),
        }
        for f in facts
    ]


@router.post("/api/memory/correct")
async def correct_memory_fact(body: _CorrectionBody, request: Request) -> dict:
    """Applique une correction humaine sur un fact (§6.7).

    Délègue à kernel.apply_correction() qui trace un event human_correction
    en audit immuable et met à jour le fact. C'est l'UNIQUE chemin d'écriture
    légitime sur la mémoire depuis l'UI — le miroir Markdown reste lecture seule.
    """

    kernel = getattr(request.app.state, "memory_kernel", None)
    if kernel is None:
        raise HTTPException(503, "Memory Kernel non disponible.")

    new_status_enum: FactStatus | None = None
    if body.new_status:
        try:
            new_status_enum = FactStatus(body.new_status)
        except ValueError:
            raise HTTPException(
                400,
                f"Status invalide '{body.new_status}'. Valeurs : {[s.value for s in FactStatus]}",
            ) from None

    event, fact = kernel.apply_correction(
        target_fact_id=body.target_fact_id,
        new_object=body.new_object,
        new_status=new_status_enum,
        new_confidence=body.new_confidence,
        correction_text=body.correction_text,
        source="ui_atelier",
    )
    return {
        "event_id": event.id,
        "fact_found": fact is not None,
        "fact": (
            {
                "id": fact.id,
                "status": fact.status.value,
                "object": fact.object,
                "confidence": round(fact.confidence, 3),
            }
            if fact
            else None
        ),
    }


@router.post("/api/memory/autodream")
async def trigger_autodream(request: Request) -> dict:
    import asyncio

    auto_dream = getattr(request.app.state, "auto_dream", None)
    if not auto_dream:
        raise HTTPException(503, "AutoDream non disponible")
    asyncio.create_task(
        auto_dream._run_micro_safe(user_message="[trigger manuel]", assistant_message=""),
        name="autodream-manual",
    )
    return {"triggered": True}


@router.post("/api/memory/trigger-deep")
async def trigger_deep(request: Request) -> dict:
    """Force une passe AutoDream.deep_analyze() sans attendre 3h du matin.

    Outil d'observation pour la phase d'activation (MOUVEMENT 2 option D) :
    permet de déclencher une ingestion batch à la demande, regarder ce qu'elle
    produit via scripts/observe_kernel_ingest.py, ajuster le prompt si bruit,
    re-déclencher — itération en minutes au lieu d'une nuit.

    Garde-fou : refuse si settings.ingest_deep_enabled=False. L'endpoint NE
    DOIT PAS contourner le flag de bascule, sinon on crée une porte
    d'ingestion qui shunte l'interrupteur principal.
    """

    if not settings.ingest_deep_enabled:
        raise HTTPException(
            503,
            "Ingestion deep désactivée (settings.ingest_deep_enabled=False). "
            "Flip le flag avant d'utiliser cet endpoint.",
        )

    auto_dream = getattr(request.app.state, "auto_dream", None)
    if not auto_dream:
        raise HTTPException(503, "AutoDream non disponible")

    asyncio.create_task(auto_dream.deep_analyze(), name="autodream-deep-manual")
    return {"triggered": True, "scope": "deep"}


# ── Le coffre : le miroir tel qu'Obsidian le montre, mais modifiable ───────────
#
# La page Atelier/Mémoire existait déjà, sous forme de tableau de faits. Elle
# répondait à « qu'est-ce que Crush sait ? » mais pas à « où ce souvenir vit-il,
# et à côté de quoi ? » — or c'est cette question qu'on se pose en relisant sa
# propre mémoire, et c'est précisément ce qu'Obsidian rend lisible.
#
# Ces deux routes montrent donc la MÊME répartition en documents que le miroir
# Markdown (via `mirror.grouper()`, sa source unique) et acceptent les deux
# gestes qui manquaient depuis un navigateur : corriger une ligne là où on la
# lit, et dicter un nouveau souvenir.


class _RetenirBody(BaseModel):
    texte: str


@router.get("/api/memory/coffre")
async def lire_le_coffre(request: Request) -> dict:
    """Le miroir en documents, avec l'ancre Obsidian de chaque fait.

    Lecture du KERNEL, pas des fichiers .md. Les deux disent la même chose, mais
    les fichiers ne sont réécrits qu'à la passe nocturne : les lire ici
    afficherait l'état d'hier matin et donnerait l'impression qu'une correction
    n'a pas été prise en compte.
    """
    mirror = getattr(request.app.state, "memory_mirror", None)
    if mirror is None:
        raise HTTPException(503, "Miroir mémoire non disponible.")

    documents = [
        {
            "fichier": doc.fichier,
            "titre": doc.titre,
            "dossier": doc.fichier.split("/")[0] if "/" in doc.fichier else "",
            "faits": [
                {
                    "id": f.id,
                    "ancre": ancre_de_fait(f.id),
                    "subject": f.subject,
                    "predicate": f.predicate,
                    "object": f.object,
                    "category": f.category,
                    "confidence": round(f.confidence, 3),
                    "importance": round(f.importance, 3),
                    "support_count": f.support_count,
                    "decay_policy": f.decay_policy.value,
                    "valid_to": f.valid_to.date().isoformat() if f.valid_to else None,
                    "last_seen_at": f.last_seen_at.isoformat(),
                }
                # Même tri que le rendu Markdown : la page web et Obsidian
                # doivent présenter les faits dans le même ordre, sinon on
                # cherche dans l'une ce qu'on a vu dans l'autre.
                for f in sorted(
                    doc.faits,
                    key=lambda f: (-(f.importance * f.confidence), -f.support_count),
                )
            ],
        }
        for doc in sorted(mirror.grouper(), key=lambda d: d.fichier)
    ]

    boite = mirror.root / NOM_FICHIER
    return {
        "documents": documents,
        "total_faits": sum(len(d["faits"]) for d in documents),
        # De quoi afficher la boîte de réception à côté : c'est le même geste,
        # depuis un autre appareil.
        "boite": {
            "existe": boite.exists(),
            "contenu": boite.read_text(encoding="utf-8") if boite.exists() else "",
        },
    }


@router.post("/api/memory/retenir")
async def retenir_un_souvenir(body: _RetenirBody, request: Request) -> dict:
    """Confie un souvenir en texte libre à la mémoire.

    Passe par la chaîne d'extraction complète — la même que celle qui lit les
    conversations, garde-fou persona inclus — et non par une écriture directe :
    un fait inséré à la main échapperait à la réconciliation et créerait un
    doublon du souvenir qu'il était censé préciser.
    """
    texte = body.texte.strip()
    if not texte:
        raise HTTPException(400, "Rien à retenir.")

    ingest = getattr(request.app.state, "memory_ingest", None)
    if ingest is None:
        raise HTTPException(503, "Extraction de faits non disponible.")

    rendu = await ingest.ingest(content=texte, source="ui_coffre", event_type="user_statement")
    nouveaux = list(getattr(rendu, "new_facts", None) or [])
    confirmes = list(getattr(rendu, "confirmed", None) or [])
    return {
        "retenus": len(nouveaux),
        "deja_sus": len(confirmes),
        "faits": [
            {"id": f.id, "subject": f.subject, "predicate": f.predicate, "object": f.object}
            for f in nouveaux
        ],
    }


@router.post("/api/memory/coffre/traiter")
async def traiter_la_boite(request: Request) -> dict:
    """Applique tout de suite ce qui attend dans la boîte de réception.

    Sans ça, une correction écrite depuis Obsidian attend la passe suivante — au
    plus dix minutes, mais dix minutes pendant lesquelles on doute que ça ait
    marché, et on la réécrit.
    """
    boite = getattr(request.app.state, "boite_memoire", None)
    if boite is None:
        raise HTTPException(503, "Boîte de réception non disponible.")
    resultat = await boite.traiter()
    return {
        "appliquees": resultat.appliquees,
        "ignorees": resultat.ignorees,
        "incomprises": resultat.incomprises,
        "retenus": resultat.retenus,
        "erreur": resultat.erreur,
    }
