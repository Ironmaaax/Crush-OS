<!--
SYNC IMPACT REPORT
==================
Version : (gabarit non rempli) → 1.0.0
Motif du numéro : première ratification. Le fichier ne contenait que les jetons
du gabarit ; il n'existait donc aucune version antérieure à incrémenter.

Principes définis (aucun renommé, aucun supprimé — tous nouveaux) :
  I.   Les quatre couches, vérifiées par la machine
  II.  Aucune mesure inventée
  III. Une seule source de vérité, un seul chemin d'écriture
  IV.  Se tromper du côté qui se remarque
  V.   Rien ne sort de chez lui

Sections ajoutées :
  - Contraintes techniques et déploiement (SECTION_2)
  - Portes de qualité et flux de travail (SECTION_3)
  - Gouvernance

Sections supprimées : aucune.

TODO différés :
  - TODO(GUIDANCE_FILE) : aucun fichier de consignes runtime (CLAUDE.md /
    AGENTS.md) n'existe à la racine du dépôt. La gouvernance renvoie donc aux
    sources réelles (`pyproject.toml` pour les portes, `kernel/contracts.py` que
    le projet désigne lui-même comme son document d'architecture). Le créer
    dépasse le périmètre de la commande constitution.
  - TODO(SPEC_KIT_SCOPE) : ce dossier `CrushOS/` est imbriqué dans le dépôt
    `Crush-OS/` sans dépôt git propre et non suivi (`"here": false` dans
    `init-options.json`). À confirmer : cette constitution gouverne-t-elle le
    code existant de `Crush-OS/`, ou un chantier distinct ?
-->

# Constitution de CRUSH-OS

CRUSH-OS est l'assistant personnel auto-hébergé de Maxime Song : une application
FastAPI qui tourne en permanence sur un Raspberry Pi 5, consultée depuis un
téléphone, et qui garde une mémoire durable de son utilisateur. Ce document fixe
ce qui n'est pas négociable. Tout le reste se discute.

## Core Principles

### I. Les quatre couches, vérifiées par la machine

Le code se répartit en quatre niveaux, et les dépendances ne remontent JAMAIS :

- **L0 `kernel/`** — schémas, contrats, réglages, chemins. N'importe rien du
  projet.
- **L1 `providers/` `capabilities/` `analytics/` `hardware/`** — n'importent que
  `kernel`.
- **L2 `engine/`** — n'importe que `kernel`. Ni providers, ni capabilities.
- **L3 `interfaces/` `app.py` `bootstrap.py`** — peuvent tout importer.

Conséquence directe et non contournable : l'engine ne peut pas appeler un
provider. Les dépendances lui sont INJECTÉES via des `Protocol`
`@runtime_checkable` déclarés dans `kernel/contracts.py`. Un composant L2 qui a
besoin d'un service L1 déclare un contrat ; le composition root
(`bootstrap.build()`) le lui passe.

Ces règles sont vérifiées par `import-linter`, quatre contrats nommés RÈGLE 1 à
RÈGLE 4. Une violation fait échouer la CI. Le respect de cette architecture
n'est donc pas une affaire de discipline mais de machine.

**Pourquoi** : c'est ce qui rend chaque couche testable sans monter les autres, et
ce qui a permis d'ajouter la présence, la boîte de réception et le graphe sans
toucher au cœur. Une architecture qu'on ne vérifie pas est une architecture qui a
déjà dérivé.

### II. Aucune mesure inventée

Ce qui ne peut pas être constaté est rendu **inconnu**, jamais approché.

- `None` et `False` ne sont pas interchangeables. « Je ne sais pas s'il est
  joignable » invite à essayer ; « il n'est pas joignable » invite à se taire.
- Une clé absente ou une commande sans réponse rend `null`. JAMAIS `0`, jamais
  une valeur plausible.
- Un voyant ne passe au vert que sur une observation, pas sur une configuration.
  Un canal configuré n'est pas un canal qui répond.
- Tout état dégradé ou absent DOIT porter son remède, en une phrase actionnable.
  Un test le vérifie sur l'ensemble des maillons.

**Pourquoi** : un tableau de bord qui ment est pire qu'une page vide, parce qu'on
cesse de vérifier ailleurs. Ce principe a fait remonter trois défauts réels — un
Telegram affiché « dormant » pendant que le bot répondait, un coût affiché
« 0,00 € » sur une clé jamais lue et dans la mauvaise monnaie, et une sauvegarde
verte vieille de trois mois.

### III. Une seule source de vérité, un seul chemin d'écriture

La mémoire est une base SQLite (`memory_data/crush_memory.db`). Elle est
l'unique source de vérité. Tout le reste en dérive.

- Le miroir Markdown (`memory_data/mirror/`) est **régénéré** depuis SQLite. Il
  est en lecture seule par construction : une édition y est écrasée au rendu
  suivant.
- Une correction humaine passe par `kernel.apply_correction()`, qui trace un
  événement `human_correction`. Ce chemin est unique quelle que soit la porte
  d'entrée : boîte de réception Obsidian, page Coffre, ou voix.
- On ne SUPPRIME jamais un événement ni un fait contredit : on archive, on
  remplace, on marque. L'historique est immuable.
- La même règle vaut hors mémoire : quand une action existe déjà (approuver une
  initiative, corriger un fait), une nouvelle porte d'entrée l'APPELLE, elle ne
  la réimplémente pas.

**Pourquoi** : deux implémentations d'« approuver » qui divergent, c'est un e-mail
parti deux fois ou pas du tout selon le bouton utilisé. Et deux sources de vérité
qui divergent ne se réconcilient jamais, parce que rien ne permet de trancher.

### IV. Se tromper du côté qui se remarque

Face à une configuration illisible ou à une mesure impossible, le comportement de
repli DOIT être choisi selon ce qui se remarque, et non par défaut technique.

- Un seuil de priorité illisible **pousse** : le bruit se remarque et se corrige,
  le silence non.
- Une plage d'heures de silence illisible **ne bâillonne rien** : une faute de
  frappe dans un réglage ne doit jamais faire perdre des messages.
- Une capacité intrusive non explicitement autorisée n'est **pas annoncée** au
  serveur : ce qu'il ignore, il ne peut pas le demander.
- Toute action tournée vers l'extérieur (envoyer un e-mail, lancer une mission)
  DOIT être idempotente sur sa porte d'entrée. Le statut est posé AVANT
  l'exécution, jamais après.
- Fusionner deux éléments à tort fait DISPARAÎTRE ce que l'utilisateur n'a jamais
  vu ; laisser passer un doublon l'agace seulement. Les deux erreurs ne coûtent
  pas la même chose, et le code doit refléter cet écart.

**Pourquoi** : les erreurs ne sont pas symétriques. Un bouton reste tapotable
après coup, et on retape volontiers quand rien ne semble se passer — sans
garde-fou, le deuxième appui renvoie l'e-mail.

### V. Rien ne sort de chez lui

Aucune dépendance de fonctionnement ne quitte les machines de l'utilisateur.

- **Aucun CDN.** Polices, three.js, gsap, mermaid, mediapipe : tout est
  rapatrié dans `static/vendor/` et verrouillé par 25 empreintes SHA-256
  (`scripts/vendor_assets.lock.json`). Le préflight refuse un fichier dont
  l'empreinte a changé.
- **Aucune écoute publique.** L'API écoute en local ; l'accès passe par
  `tailscale serve`, donc chiffré et limité au tailnet. Un service qui donne
  accès à la mémoire n'a rien à faire sur une interface publique.
- Les seules sorties réseau admises sont les services que l'utilisateur a
  explicitement configurés (modèle de langage, Gmail, Notion, Spotify, Telegram).
- Un secret ne passe JAMAIS par la ligne de commande : les arguments d'un
  processus sont lisibles par tous dans `ps`, son environnement ne l'est que par
  son propriétaire.
- Les données de l'utilisateur ne servent pas à alimenter un tiers. Une capacité
  qui observe (lire l'écran, la caméra) exige un drapeau qui lui est PROPRE,
  distinct de celui des actions destructrices.

**Pourquoi** : c'est la raison d'être du projet. Un assistant personnel qui
délègue sa mémoire à un service tiers n'est pas personnel.

## Contraintes techniques et déploiement

**Pile.** Python 3.11, FastAPI, SQLite, `uv` pour les dépendances. Interface web
en HTML/CSS/JS sans cadre applicatif ni étape de compilation : les fichiers
servis sont les fichiers écrits. Pas de bibliothèque ajoutée pour ce qui tient en
cinquante lignes — la simulation de forces du graphe s'appuie sur le three.js
déjà présent plutôt que sur une bibliothèque de graphe de 200 Ko à verrouiller.

**Cible.** Raspberry Pi 5 (8 Go, Debian 13, aarch64), allumée en permanence,
sans écran ni clavier, consultée depuis un téléphone. Cette contrainte décide de
tout : une page doit être lisible sur un écran de téléphone, une mesure doit
tenir en un coup d'œil, et une panne doit se signaler d'elle-même puisque
personne ne regarde la machine.

**Déploiement.** La Pi n'est PAS un dépôt git. Le déploiement se fait par
`tar czf - <fichiers> | ssh … "tar xzf -"` puis redémarrage EXPLICITE du service.
Le rechargement automatique ne fait pas foi : une extraction en cours peut
déclencher un rechargement sur un arbre à moitié écrit.

**Fins de ligne.** `.gitattributes` force LF sur `*.sh`, `crush`, `setup.sh`,
`*.service`, `*.timer`, `*.conf`, et `-text` sur `static/vendor/**`. Développer
sous Windows et déployer sous Linux fait sinon échouer bash sur un `\r`
invisible, et casse les empreintes des fichiers vendorisés.

**Un secret vit dans `.env`** (permissions 600), jamais dans le dépôt. Les
identifiants du partage WebDAV vivent dans `.env.webdav`, également ignoré par
git.

## Portes de qualité et flux de travail

**Aucun déploiement sans les cinq portes.** Elles sont exécutées dans cet ordre
et toutes DOIVENT passer :

1. `ruff check` — style et erreurs, longueur de ligne 100, annotations exigées.
2. `import-linter` — les quatre contrats de couches (principe I).
3. `mypy` — conformité des `Protocol` au boot.
4. `pytest -m "not integration"` — 1557 tests.
5. `snapshot_routes` — les 208 routes HTTP identiques à la baseline, ou la
   baseline régénérée délibérément après preuve de déterminisme.

`scripts/validation/smoke_runtime.py --fake-llm` DOIT imprimer `BOOT OK` : le
graphe d'objets se construit et trois chemins critiques répondent.

**Un test dit POURQUOI il existe.** Le nom décrit le comportement défendu, la
docstring décrit ce qui casserait sans lui — de préférence le défaut réellement
observé. Un test qui dépend de l'heure de son lancement, du contenu du disque ou
de l'ordre d'exécution ne prouve rien et DOIT être rendu déterministe.

**Un commentaire dit POURQUOI, jamais QUOI.** Le code dit déjà ce qu'il fait. Un
commentaire explique la raison d'un choix, et nomme le piège qu'il évite. Les
commentaires du projet citent les défauts observés : c'est ce qui empêche de les
« simplifier » plus tard.

**Une mesure avant une opinion.** Avant de changer un comportement, on regarde
les données réelles. La refonte des initiatives est partie d'un chiffre — 76
propositions, 44 rejetées sur sept jours — et non d'une impression.

**Ce qui est incertain est dit.** Un rendu visuel non ouvert dans un navigateur,
une validation faite par un analyseur au lieu du vrai moteur, une limite de
couverture : cela se déclare, dans le message de commit comme à l'utilisateur.

## Governance

Cette constitution prime sur toute autre pratique du projet. En cas de conflit
entre une habitude et un principe énoncé ici, le principe gagne.

**Procédure d'amendement.** Toute modification exige (1) la version incrémentée
selon la règle ci-dessous, (2) un Sync Impact Report en tête de fichier, et (3)
la raison du changement — pas seulement son contenu. Un principe supprimé ou
redéfini exige de nommer ce qui l'a rendu faux.

**Versionnement.** Sémantique, appliqué à la gouvernance :

- **MAJEUR** — un principe est supprimé, ou redéfini de façon incompatible.
- **MINEUR** — un principe ou une section est ajouté, ou son étendue élargie.
- **CORRECTIF** — clarification, reformulation, correction sans effet sémantique.

**Conformité.** Chaque changement de code est examiné à l'aune de ces principes.
Une complexité ajoutée DOIT être justifiée par un besoin constaté, pas anticipé.
Le respect du principe I n'est pas laissé à l'appréciation : il est vérifié par
`import-linter` à chaque exécution de la CI.

**Consignes runtime.** En l'absence d'un fichier de consignes dédié
(TODO(GUIDANCE_FILE)), les sources qui font foi sont `pyproject.toml` pour les
portes de qualité et `src/crush/kernel/contracts.py`, que le projet désigne
lui-même comme son document d'architecture.

**Version**: 1.0.0 | **Ratified**: 2026-08-22 | **Last Amended**: 2026-08-22
