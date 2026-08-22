# Copyright (C) 2026 Maxime Song

"""Ce que l'assistant sait de son utilisateur, rendu pour le prompt.

LE TROU QUE CE MODULE COMBLE

Le Memory Kernel contenait une cinquantaine de faits structurés, scorés,
corrigeables — et AUCUN chemin ne les amenait dans une conversation. Vérifié :

- `MemoryRetrieval` (providers/memory/retrieval.py) n'était instancié nulle part.
- `memory_search` lit l'index vectoriel, alimenté par `topics/*.md` et
  `sessions/*.jsonl` uniquement. Les faits n'y entrent jamais.
- `session_recall` lit ce même index plus le FTS des transcripts.
- Le miroir Markdown n'est indexé nulle part et `memory_load_topic` ne sait pas
  le lire.

Le seul lecteur conversationnel des faits était `memory_journal`, une fenêtre de
DATES. Autrement dit : un fait appris en juin et non reconfirmé était
inatteignable, sauf en nommant son jour. Ce que l'assistant « savait » de son
utilisateur à chaque tour se réduisait à 389 octets de prose plate.

POURQUOI UN BLOC DÉTERMINISTE ET NON UNE RECHERCHE PAR REQUÊTE

À cette échelle, la recherche est une optimisation prématurée, et elle coûte
cher en effets de bord :

- Elle dépend d'une correspondance lexicale. Mesuré sur la base réelle :
  « comment tu me parles » ne trouve RIEN, alors que les faits `prefers
  concision`, `communicates_as approche directe` et `prefers appellation
  monsieur` existent — ils ne partagent simplement aucun mot avec la question.
- Elle rend le préfixe système variable d'un tour à l'autre, ce qui interdit
  toute mise en cache du prompt.
- Elle a une branche de repli qui injecte des faits arbitraires que l'appelant ne
  peut pas distinguer des faits pertinents.

Les cinquante faits filtrés tiennent dans ~500 jetons. On les donne tous, dans un
ordre stable. La sélection par requête commencera à payer vers le millier de
faits ; `retrieval.py` l'attend, réparé, pour ce jour-là.
"""

from __future__ import annotations

from loguru import logger

from crush.kernel.contracts import MemoryStore
from crush.kernel.schemas import Fact, FactStatus

# En dessous, le fait n'est pas su : c'est une hypothèse que l'ingestion n'a vue
# qu'une fois. Le miroir Markdown emploie déjà ce seuil pour mettre en quarantaine
# les croyances incertaines — on le reprend pour ne pas avoir deux vérités sur ce
# qui compte comme « su ».
_CONFIANCE_MINIMALE = 0.5

# Plafond de faits injectés. Choisi pour tenir sous ~600 jetons : le prompt
# système fait déjà ~7 800 jetons et il est repayé à chaque tour, faute de mise
# en cache. Au-delà, on paierait un contexte que le modèle survolerait.
_PLAFOND = 22

# Un objet plus long que ça n'est pas un fait, c'est une phrase que l'extraction
# a mal découpée. On le tronque plutôt que de laisser une ligne manger le bloc.
_LONGUEUR_OBJET = 90

# Nombre de tours du tourniquet de sélection : chaque catégorie obtient une place
# par tour avant qu'aucune n'en obtienne une deuxième.
#
# MESURÉ sur la base réelle (41 faits actifs, Pi, 2026-08-22). Le tri était plat
# — `importance × confiance` puis coupe au plafond — et le PREMIER fait écarté
# était `persona communicates_as monsieur` : la façon dont Max veut qu'on
# s'adresse à lui, qui pèse sur chaque réponse, évincée par des faits d'outil
# mieux notés. La cause n'est pas le score mais la distribution : 15 `preference`
# et 13 `tool` contre 2 `identity` et 3 `persona`. Une catégorie nombreuse rafle
# les places, quelle que soit son utilité.
#
# 4, parce que 3 laissait sortir la quatrième décision et 5 rendait la main aux
# préférences. À 22 places et six catégories peuplées, c'est le point où les
# petites catégories décisives passent toutes.
_QUOTA = 4

# ── Calibrage des réserves ────────────────────────────────────────────────────
#
# L'échelle de confiance du projet (§6.5, `providers/memory/ingest.py`) :
#
#     0.55  inférence faible — déduit de ce qu'il a dit
#     0.75  énoncé explicite — il l'a dit
#     0.90  correction humaine — il a corrigé
#     0.99  plafond
#
# Le seuil d'affichage était 0.80, donc AU-DESSUS de « énoncé explicite ».
# Conséquence mesurée sur la base réelle (41 faits actifs, Pi, 2026-08-22) :
# 37 faits sur 41 valent 0.55 ou 0.75, et 18 des 22 lignes du bloc portaient
# « _(à confirmer, 75 %)_ ». L'assistant lisait donc un mur de réserves sur des
# choses que Max avait énoncées lui-même — et rien n'apprend mieux à un modèle à
# tout relativiser.
#
# On met la réserve SOUS l'énoncé explicite : seule une inférence est signalée.
# Valeur dupliquée et non importée : `engine` ne peut importer que `kernel`
# (RÈGLE 3), et l'échelle vit dans `providers`. Le lien est fait par ce
# commentaire, faute de pouvoir l'être par le code.
_CONFIANCE_ENONCE_EXPLICITE = 0.75

# Les catégories, dans l'ordre où elles servent à répondre. L'identité et la
# façon de s'adresser à lui d'abord : elles pèsent sur CHAQUE réponse, alors
# qu'un souvenir d'outil ne pèse que sur les questions qui le concernent.
_ORDRE_CATEGORIES = (
    "identity",
    "persona",
    "preference",
    # Absent de `kernel/vocab.CATEGORIES` mais PRESENT dans les donnees reelles
    # (« max values efficacite operationnelle ») -- vestige d'avant la fermeture
    # du vocabulaire. On le titre quand meme : un fait existant ne doit pas
    # sortir en en-tete brute.
    "values",
    "belief",
    "work_style",
    "constraint",
    "goal",
    "project",
    "decision",
    "habit",
    "relationship",
    "health_fitness",
    "tool",
    "memory_correction",
)

_TITRES = {
    "identity": "Qui il est",
    "persona": "Comment lui parler",
    "preference": "Ce qu'il préfère",
    "values": "Ce à quoi il tient",
    "work_style": "Sa façon de travailler",
    "constraint": "Ses contraintes",
    "goal": "Ses objectifs",
    "project": "Ses projets",
    "decision": "Ses décisions",
    "habit": "Ses habitudes",
    "relationship": "Ses relations",
    "health_fitness": "Santé et forme",
    "tool": "Ses outils",
    "belief": "Ce qu'il croit",
    "memory_correction": "Corrections qu'il a apportées",
}


def _poids(fait: Fact) -> float:
    """Importance × confiance : ce qui compte, et ce dont on est sûr.

    Pas de facteur de récence ici, contrairement au score de `retrieval.py`. Un
    fait d'identité ne devient pas moins vrai parce qu'on n'en a pas reparlé
    depuis deux mois, et l'atténuer ferait sortir du bloc précisément ce qui est
    le plus stable.
    """
    return fait.importance * fait.confidence


def _selectionne(retenus: list[Fact], plafond: int, quota: int) -> list[Fact]:
    """Les `plafond` faits qui obtiennent une place, par tourniquet puis mérite.

    DEUX TOURS, ET C'EST LE POINT

    1. Tourniquet : `quota` tours, et à chaque tour chaque catégorie prend son
       meilleur fait restant. Une catégorie obtient donc sa deuxième place
       seulement quand toutes ont eu leur première.
    2. Mérite : les places restantes vont aux mieux notés, sans égard à la
       catégorie.

    Le quota est un PLANCHER, pas un plafond — sans le second tour, un bloc de 22
    places se contenterait de quatre faits par catégorie et gâcherait le reste.

    Pourquoi un tourniquet et non un premier tour glouton dans l'ordre des
    catégories : glouton, `identity` + `persona` + `preference` consommaient déjà
    neuf places sur dix, et `decision` — qui porte les règles de fonctionnement —
    n'était jamais atteint. Le défaut ne se voyait pas à 22 places ; il apparaît
    dès qu'un appelant resserre le plafond.

    Pourquoi un quota et non un palier par catégorie : essayé, et SIMULÉ sur la
    base réelle avant d'être écrit. Deux paliers, « ce qui pèse sur chaque
    réponse » d'abord, produisaient un bloc PIRE — quinze goûts musicaux
    individuels entraient en évinçant `requires_validation_for déploiement
    fonctionnalité`. Un palier ne corrige pas le déséquilibre, il le déplace.
    """
    par_categorie: dict[str, list[Fact]] = {}
    for f in retenus:  # `retenus` est déjà trié par poids décroissant
        par_categorie.setdefault(f.category, []).append(f)

    # Les catégories connues dans leur ordre d'utilité, les inconnues ensuite.
    # Déterministe : deux bases identiques donnent le même bloc.
    connues = [c for c in _ORDRE_CATEGORIES if c in par_categorie]
    ordre = connues + sorted(c for c in par_categorie if c not in _ORDRE_CATEGORIES)

    pris: list[Fact] = []
    deja = set()
    for tour in range(max(1, quota)):
        for categorie in ordre:
            candidats = par_categorie[categorie]
            if tour < len(candidats) and len(pris) < plafond:
                pris.append(candidats[tour])
                deja.add(candidats[tour].id)

    for f in retenus:
        if len(pris) >= plafond:
            break
        if f.id not in deja:
            pris.append(f)
            deja.add(f.id)

    return pris


def bloc_memoire(
    store: MemoryStore,
    plafond: int = _PLAFOND,
    confiance_minimale: float = _CONFIANCE_MINIMALE,
    quota: int = _QUOTA,
) -> str:
    """Rend les faits actifs en un bloc Markdown, ou une chaîne vide.

    Déterministe : à base inchangée, la sortie est identique d'un appel à
    l'autre. C'est ce qui la rend relisable — et ce qui permettra une mise en
    cache du préfixe système le jour où l'horodatage à la minute sera réglé.

    Lecture SQLite synchrone : ~50 lignes, quelques microsecondes. L'appelant
    est chargé de la déporter dans un thread s'il est asynchrone.
    """
    try:
        actifs = store.list_facts_by_status(FactStatus.ACTIVE)
    except Exception as exc:  # noqa: BLE001 — un prompt sans mémoire vaut mieux que pas de réponse
        # JOURNALISÉ. Sans cette ligne, une base durablement illisible (verrou,
        # erreur disque, schéma incompatible) privait l'assistant de toute
        # mémoire à chaque tour, indéfiniment, sans laisser la moindre trace :
        # la fonction rend `""` normalement, donc le `try` de l'appelant — qui
        # journalise, lui — ne se déclenchait jamais.
        logger.warning("Bloc mémoire : faits illisibles", error=str(exc))
        return ""

    retenus = [f for f in actifs if f.confidence >= confiance_minimale]
    if not retenus:
        return ""

    # Trié par poids, puis sélectionné par tourniquet. La coupe franche au
    # plafond — `retenus[:plafond]` — était précisément le défaut : elle laissait
    # une catégorie bavarde évincer une catégorie décisive.
    retenus.sort(key=lambda f: (-_poids(f), f.subject, f.predicate))
    retenus = _selectionne(retenus, max(1, plafond), quota)

    par_categorie: dict[str, list[Fact]] = {}
    for f in retenus:
        par_categorie.setdefault(f.category, []).append(f)

    connues = [c for c in _ORDRE_CATEGORIES if c in par_categorie]
    autres = sorted(c for c in par_categorie if c not in _ORDRE_CATEGORIES)

    lignes: list[str] = []
    for categorie in connues + autres:
        lignes.append(f"**{_TITRES.get(categorie, categorie)}**")
        for f in par_categorie[categorie]:
            objet = f.object.strip()
            if len(objet) > _LONGUEUR_OBJET:
                objet = objet[: _LONGUEUR_OBJET - 1] + "…"
            # Un fait déduit ne doit pas être affirmé sur le même ton qu'un fait
            # énoncé — sans marque, le modèle les traite à égalité. Mais la
            # marque ne vaut que si elle est RARE : appliquée aux 18 lignes sur
            # 22 qu'elle couvrait, elle ne distinguait plus rien.
            #
            # « déduit » et non « à confirmer » : la première dit d'où vient le
            # fait, la seconde donne un ordre — et l'assistant s'exécutait, en
            # redemandant ce qu'il savait déjà.
            marque = "" if f.confidence >= _CONFIANCE_ENONCE_EXPLICITE else " _(déduit)_"
            lignes.append(f"- {f.predicate} {objet}{marque}")
        lignes.append("")

    return "\n".join(lignes).strip()
