# Copyright (C) 2026 Max Ea
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS,   .


from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_VALID_WHISPER = frozenset(
    {
        "tiny.en",
        "tiny",
        "base.en",
        "base",
        "small.en",
        "small",
        "medium.en",
        "medium",
        "large-v1",
        "large-v2",
        "large-v3",
        "large",
        "distil-large-v2",
        "distil-medium.en",
        "distil-small.en",
        "distil-large-v3",
        "large-v3-turbo",
        "turbo",
    }
)


class Settings(BaseSettings):
    """Configuration centrale de Crush, chargée depuis .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── LLM ──────────────────────────────────────────────────
    llm_provider: Literal["api", "local"] = Field(
        default="api",
        description="'api' pour Anthropic/Mistral, 'local' pour Ollama.",
    )
    api_backend: Literal["anthropic", "mistral", "openai", "gemini"] = Field(
        default="anthropic",
        description="Backend API principal quand LLM_PROVIDER=api.",
    )

    # Anthropic
    anthropic_api_key: SecretStr = Field(default=SecretStr(""), description="Clé API Anthropic.")
    anthropic_model: str = Field(
        default="claude-sonnet-4-6",
        description="Modèle Anthropic à utiliser.",
    )
    voice_anthropic_model: str = Field(
        default="claude-haiku-4-5-20251001",
        description="Modèle Anthropic pour la voix (plus rapide).",
    )
    openai_model: str = Field(
        default="gpt-4o-mini",
        description="Modèle OpenAI à utiliser pour le LLM principal.",
    )

    # Mistral
    mistral_api_key: SecretStr = Field(default=SecretStr(""), description="Clé API Mistral.")
    mistral_model: str = Field(
        default="mistral-large-latest",
        description="Modèle Mistral à utiliser.",
    )

    # Ollama
    ollama_base_url: str = Field(
        default="http://localhost:11434",
        description="URL du serveur Ollama.",
    )
    ollama_model: str = Field(default="mistral", description="Modèle Ollama à utiliser.")

    # ── Serveur ───────────────────────────────────────────────
    host: str = Field(
        default="127.0.0.1",
        description=(
            "Adresse d'écoute du serveur. '127.0.0.1' (défaut) = localhost uniquement. "
            "Mettre explicitement '0.0.0.0' pour exposer l'API hors de la machine "
            "(Tailscale, VPN, VPS). Ne jamais exposer sans API_AUTH_ENABLED=true."
        ),
    )
    port: int = Field(default=8000)
    environment: Literal["development", "production"] = Field(default="development")

    # ── Sécurité réseau ───────────────────────────────────────
    api_auth_enabled: bool = Field(
        default=False,
        description=(
            "Active l'authentification Bearer sur toutes les routes API. "
            "Désactivé par défaut pour ne pas casser l'usage local. "
            "Obligatoire dès que l'API est exposée hors localhost (Tailscale, VPS)."
        ),
    )
    api_token: SecretStr = Field(
        default=SecretStr(""),
        description=(
            "Token Bearer attendu si api_auth_enabled=True. Générer avec : openssl rand -hex 32. "
            "Sert aussi de clé de signature du cookie de session : le changer déconnecte "
            "toutes les sessions ouvertes."
        ),
    )
    api_allowed_origins: str = Field(
        default="",
        description=(
            "Hôtes supplémentaires acceptés dans l'en-tête Origin, séparés par des virgules. "
            "Les IP locales et les noms Tailscale sont détectés automatiquement "
            "(cf. kernel/network.py) — ce champ ne sert qu'aux cas non détectables "
            "(reverse proxy, nom de domaine personnalisé)."
        ),
    )
    session_max_age_days: int = Field(
        default=30,
        description=(
            "Durée de validité du cookie de session navigateur, en jours. "
            "Au-delà, l'utilisateur repasse par /login."
        ),
    )
    cors_allow_origins: list[str] = Field(
        default_factory=list,
        description=(
            'Origines CORS autorisées (ex: ["http://mon-pc.tailscale:8000"]). '
            "Vide + auth désactivée = localhost par défaut. "
            "Ne jamais laisser vide avec auth activée et exposition réseau."
        ),
    )

    # ── Mémoire ───────────────────────────────────────────────
    memory_dir: str = Field(
        default="memory_data",
        description="Répertoire racine des données mémoire (MEMORY.md, topics/, sessions/).",
    )
    autonomy_auto_execute_enabled: bool = Field(
        default=False,
        description=(
            "PHASE 6 — Active l'auto-exécution des initiatives de niveau "
            "d'autonomie ≥ 3 (SANDBOX, MODIFY_PROJECT) quand le gate composite "
            "(§9) renvoie 'auto'. DÉSACTIVÉ par défaut : toute initiative "
            "qui demanderait une auto-exécution passe par validation humaine "
            "en MVP, peu importe son niveau. À NE FLIPPER QU'APRÈS observation "
            "validée (sous-mouvement séparé). Niveau 5 EXTERNAL_ACTION "
            "(publier/payer/contacter) reste systématiquement en validation "
            "humaine — ce flag NE peut PAS contourner la règle CDC §10.1."
        ),
    )
    auto_install_whitelisted_enabled: bool = Field(
        default=False,
        description=(
            "PHASE 5 — Active l'auto-installation des skills candidates qui "
            "(1) sont issues du CapabilityEngine, (2) passent le sandbox vert, "
            "(3) matchent un domaine listé dans config/permissions.yaml. "
            "DÉSACTIVÉ par défaut : aucune route auto en MVP, toute candidate "
            "passe par promote() humain. À NE FLIPPER QU'APRÈS observation "
            "validée (sous-mouvement séparé, équivalent du flag "
            "ingest_deep_enabled de PHASE 3 MOUVEMENT 2). "
            "Même quand True : INSTALL_PACKAGE et MODIFY_CORE restent "
            "systématiquement en validation humaine, le gate composite §9 ne "
            "peut pas être contourné."
        ),
    )
    ingest_deep_enabled: bool = Field(
        default=False,
        description=(
            "Active l'ingestion BATCH des sessions dans le Memory Kernel lors de "
            "la passe nocturne AutoDream.deep_analyze() (1× par 24h à 3h du mat). "
            "Une seule extraction par session JSONL — pas une boucle par message — "
            "donc le dédoublonnage intra-batch est garanti par le matcher v2. "
            "Les hooks micro (consolidation._run + auto_dream._run_micro à chaque "
            "échange) NE sont JAMAIS branchés au Kernel : c'est une décision "
            "Generative Agents (synthèse périodique sur la conversation complète, "
            "pas extraction à chaud message par message). "
            "Désactivé par défaut tant que la trace 3-5 jours n'a pas été validée."
        ),
    )

    proactive_interval_minutes: int = Field(
        default=180,
        description=(
            "Periode du moteur proactif, en minutes. Chaque cycle consomme UN "
            "appel LLM. A 30 min, cela fait 48 appels par jour — plus du double "
            "du palier gratuit Gemini (20/jour), epuise sans que l'utilisateur "
            "ait rien demande. Mettre 0 desactive le moteur."
        ),
    )

    # ── Outils ────────────────────────────────────────────────
    cli_whitelist_path: str = Field(
        default="config/tools.yaml",
        description="Chemin vers le fichier YAML de scripts CLI whitelistés.",
    )
    allow_unsandboxed_exec: bool = Field(
        default=False,
        description=(
            "Autorise ExecuteCLITool à s'exécuter sans sandbox (tmpdir isolé + env restreint). "
            "Désactivé par défaut. N'activer qu'en dev local en connaissance de cause."
        ),
    )
    skills_dir: str = Field(
        default="skills",
        description="Répertoire racine des skills.",
    )
    skills_catalog_repo: str = Field(
        default="Ironmaaax/Crush-skills",
        description=(
            "Dépôt GitHub owner/nom d'où la boutique lit son catalogue et télécharge "
            "le code des skills. Configurable parce que c'est une URL de "
            "téléchargement, pas un nom : la pointer ailleurs suppose que le dépôt "
            "existe et respecte la même arborescence (index.json + <chemin>/skill.py)."
        ),
    )
    file_search_roots: list[str] = Field(
        default=["~/"],
        description="Répertoires racines autorisés pour la lecture/recherche de fichiers.",
    )
    google_credentials_path: str = Field(
        default="config/google_credentials.json",
        description="Chemin vers le fichier credentials OAuth2 Google.",
    )
    google_client_id: str = Field(
        default="",
        description=(
            "Client ID OAuth2 Google (app type 'Web'). Si renseigné avec le secret, "
            "le fichier google_credentials.json est régénéré automatiquement à partir "
            "de ces deux valeurs — pas besoin de déposer le fichier à la main."
        ),
    )
    google_client_secret: SecretStr = Field(
        default=SecretStr(""), description="Client Secret OAuth2 Google (app type 'Web')."
    )
    google_token_path: str = Field(
        default="config/google_token.json",
        description="Chemin vers le token OAuth2 Google (généré automatiquement).",
    )
    google_gmail_token_path: str = Field(
        default="config/google_gmail_token.json",
        description="Chemin vers le token OAuth2 Gmail (généré automatiquement).",
    )

    # ── Vision ───────────────────────────────────────────────────
    vision_model: str = Field(
        default="gpt-4o",
        description="Modèle OpenAI pour la vision (GPT-4o Vision).",
    )
    vision_webcam_index: int = Field(
        default=0,
        description="Index de la webcam OpenCV (0 = première caméra détectée).",
    )
    vision_screen_max_width: int = Field(
        default=1280,
        description="Largeur max de la capture écran avant envoi à l'API.",
    )
    vision_jpeg_quality: int = Field(
        default=75,
        description="Qualité JPEG des captures (50-85 est suffisant pour l'analyse).",
    )
    vision_object_detection: bool = Field(
        default=False,
        description="Active le daemon de détection d'objets YOLOv8n (webcam en background).",
    )
    vision_yolo_confidence: float = Field(
        default=0.5,
        description="Seuil de confiance YOLOv8n (0.0–1.0).",
    )

    # ── Audio / STT / TTS ─────────────────────────────────────
    openai_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="Clé API OpenAI (LLM principal si api_backend=openai, TTS, Vision).",
    )
    stt_provider: Literal[
        "auto", "openai", "local",
        # Valeurs héritées du pipeline LiveKit. Conservées pour qu'un .env
        # existant ne fasse pas échouer la validation au démarrage : le champ
        # est un Literal, une valeur inconnue empêcherait le boot. Elles sont
        # traduites vers le nouveau pipeline dans `providers/audio/stt.py`.
        "deepgram", "google", "whisper",
    ] = Field(
        default="auto",
        description=(
            "Moteur de transcription. 'auto' essaie OpenAI puis retombe sur le "
            "modèle local à la première défaillance (quota épuisé, réseau coupé). "
            "'local' n'exige aucune clé mais l'extra `local-audio`. "
            "'deepgram'/'google'/'whisper' sont hérités de LiveKit et remappés."
        ),
    )
    deepgram_api_key: SecretStr = Field(
        default=SecretStr(""), description="Clé API Deepgram (STT Nova-2 streaming)."
    )
    whisper_model: str = Field(
        default="tiny",
        description="Taille du modèle faster-whisper : tiny, base, small, medium, large.",
    )

    openai_stt_model: str = Field(
        default="whisper-1",
        description=(
            "Modèle de transcription OpenAI. 'gpt-4o-mini-transcribe' est moins "
            "cher et plus rapide, 'whisper-1' plus éprouvé sur le français."
        ),
    )
    stt_language: str = Field(
        default="fr",
        description=(
            "Langue attendue, en code ISO-639-1. La forcer évite les faux "
            "positifs de détection automatique sur les phrases courtes."
        ),
    )

    @field_validator("whisper_model", mode="before")
    @classmethod
    def _validate_whisper_model(cls, v: str) -> str:
        if v not in _VALID_WHISPER:
            return "tiny"
        return v

    tts_voice: str = Field(
        default="alloy",
        description="Voix OpenAI TTS : alloy, echo, fable, onyx, nova, shimmer.",
    )
    edge_voice: str = Field(
        default="fr-FR-HenriNeural",
        description=(
            "Voix Microsoft Edge TTS (si TTS_PROVIDER=edge). Gratuit, sans clé, "
            "sans quota, et nettement plus naturel que Piper. Autres voix "
            "françaises : fr-FR-DeniseNeural, fr-FR-EloiseNeural, "
            "fr-FR-RemyMultilingualNeural, fr-FR-VivienneMultilingualNeural, "
            "fr-CA-AntoineNeural, fr-CH-FabriceNeural."
        ),
    )
    tts_provider: str = Field(
        default="piper",
        description="Moteur TTS : 'piper' (local), 'elevenlabs' ou 'gemini'.",
    )
    piper_model_path: str = Field(
        default="models/piper/fr_FR-upmc-medium.onnx",
        description="Chemin vers le modèle Piper ONNX.",
    )
    # ── Gemini TTS (Google) ───────────────────────────────────
    # Auth via GOOGLE_API_KEY (clé Gemini API, ai.google.dev) — même clé que le
    # plugin livekit-plugins-google côté pipeline vocal.
    # `GOOGLE_API_KEY` et `GEMINI_API_KEY` désignent la MÊME clé Google AI Studio ;
    # les deux noms circulent dans la doc de Google. Le champ n'acceptait que le
    # premier : une clé posée sous `GEMINI_API_KEY` était silencieusement jetée
    # (`extra="ignore"`), laissant chat, TTS et pipeline vocal sans identifiants
    # alors que la clé était bien présente.
    #
    # On déclare les DEUX champs et on les réconcilie dans `_unify_google_key`
    # plutôt que d'employer `AliasChoices` : celui-ci retient le premier alias
    # PRÉSENT, or `.env.example` pose `GOOGLE_API_KEY=` vide. Une chaîne vide
    # reste une valeur, donc elle gagnait et écrasait la vraie clé.
    google_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="Clé API Google/Gemini — chat Gemini, TTS Gemini et pipeline vocal.",
    )
    gemini_api_key: SecretStr = Field(
        default=SecretStr(""),
        description=(
            "Autre nom de GOOGLE_API_KEY. Si les deux sont renseignés, "
            "GOOGLE_API_KEY l'emporte."
        ),
    )
    gemini_thinking_budget: int | None = Field(
        default=None,
        description=(
            "Jetons alloues au raisonnement prealable de Gemini 2.5+. None laisse "
            "le defaut du modele, 0 le desactive. Ce raisonnement s'ecoule AVANT "
            "le premier token : le pipeline vocal le coupe systematiquement, "
            "quelle que soit cette valeur."
        ),
    )
    reflection_enabled: bool = Field(
        default=True,
        description=(
            "Reflexion selective a l'oral. Le pipeline vocal coupe le raisonnement "
            "prealable pour repondre vite ; quand ce drapeau est actif, il le "
            "rallume pour les seules questions qui portent une marque de "
            "raisonnement (comparaison, cause, planification). Cf. "
            "engine/reflection.py pour le detail du declenchement."
        ),
    )
    reflection_thinking_budget: int = Field(
        default=1024,
        description=(
            "Jetons de raisonnement accordes a une question jugee complexe sur le "
            "canal vocal. Factures au tarif de SORTIE (six fois l'entree chez "
            "Gemini), et payes en silence avant le premier son : monter cette "
            "valeur degrade la latence percue autant que la facture."
        ),
    )
    gemini_model: str = Field(
        default="gemini-2.5-flash",
        description=(
            "Modèle Gemini pour le chat. Deux pièges constatés en production : "
            "(1) les identifiants sont retirés sans préavis — `gemini-2.0-flash` "
            "renvoie 404 NOT_FOUND ; vérifier avec `client.models.list()` ; "
            "(2) le palier gratuit plafonne à 20 requêtes/jour sur les modèles "
            "récents, et les tout derniers renvoient des 503 aux heures de pointe. "
            "`gemini-2.5-flash` est retenu par défaut pour sa stabilité."
        ),
    )
    gemini_tts_model: str = Field(
        default="gemini-2.5-flash-preview-tts",
        description="Modèle Gemini TTS (flash-preview-tts ou pro-preview-tts).",
    )
    gemini_tts_voice: str = Field(
        default="Kore",
        description="Voix Gemini TTS (ex. Kore, Puck, Charon, Aoede…). 30 voix dispo.",
    )
    elevenlabs_api_key: SecretStr = Field(default=SecretStr(""), description="Clé API ElevenLabs.")
    elevenlabs_voice_id: str = Field(default="", description="ID de la voix ElevenLabs.")
    elevenlabs_model: str = Field(
        default="eleven_flash_v2_5",
        description="Modèle ElevenLabs : eleven_flash_v2_5 (~75ms) ou eleven_turbo_v2_5 (~300ms).",
    )

    # ── Notion ────────────────────────────────────────────────
    notion_token: SecretStr = Field(
        default=SecretStr(""), description="Token d'intégration Notion."
    )
    notion_page_id: str = Field(
        default="",
        description="ID de la page Notion des tâches (depuis l'URL).",
    )

    # ── AIS Stream (navires) ─────────────────────────────────
    aisstream_key: SecretStr = Field(
        default=SecretStr(""),
        description="Clé API AISstream.io (navires temps réel).",
    )

    # ── Mapbox (globe natif) ──────────────────────────────────
    mapbox_token: SecretStr = Field(
        default=SecretStr(""), description="Token Mapbox GL JS (projection globe native)."
    )
    mapbox_monthly_limit: int = Field(
        default=40000,
        description=(
            "Plafond mensuel de chargements de carte. Mapbox en offre 50 000 puis "
            "facture ; on s'arrete avant, la marge absorbant l'ecart entre ce que "
            "l'on compte (une remise de jeton) et ce que Mapbox facture (une carte "
            "construite). 0 desactive le garde-fou. Le plafond de depense a "
            "configurer chez Mapbox reste la seule garantie dure ; celui-ci evite "
            "d'y arriver."
        ),
    )

    # ── MapTiler (carte détaillée) ────────────────────────────
    maptiler_key: SecretStr = Field(
        default=SecretStr(""), description="Clé API MapTiler (free tier, carte détaillée globe V2)."
    )

    # ── Musique ───────────────────────────────────────────────
    music_provider: str = Field(
        default="", description="Fournisseur de musique actif: spotify | deezer | local"
    )

    # ── Spotify ───────────────────────────────────────────────
    spotify_client_id: str = Field(default="", description="Spotify app Client ID.")
    spotify_client_secret: SecretStr = Field(
        default=SecretStr(""), description="Spotify app Client Secret."
    )
    spotify_redirect_uri: str = Field(
        default="http://127.0.0.1:8000/api/spotify/callback",
        description="URI de callback OAuth Spotify.",
    )
    spotify_token_path: str = Field(
        default="config/spotify_token.json",
        description="Fichier de token Spotify (généré automatiquement).",
    )

    # ── Deezer ────────────────────────────────────────────────
    deezer_app_id: str = Field(default="", description="Deezer app ID.")
    deezer_app_secret: SecretStr = Field(default=SecretStr(""), description="Deezer app secret.")
    deezer_redirect_uri: str = Field(
        default="http://127.0.0.1:8000/api/deezer/callback",
        description="URI de callback OAuth Deezer.",
    )
    deezer_token_path: str = Field(
        default="config/deezer_token.json",
        description="Fichier de token Deezer (généré automatiquement).",
    )

    # ── Proactivité ───────────────────────────────────────────
    home_city: str = Field(default="Paris", description="Ville pour la météo du briefing.")
    briefing_hour: int = Field(default=9, description="Heure du morning briefing (0-23).")

    # ── Sauvegarde de la memoire ──────────────────────────────
    # La memoire est la seule donnee irremplacable du projet. Ces reglages
    # existent parce qu'elle ne tenait qu'en un exemplaire : le script
    # d'archivage etait la, mais rien ne l'appelait.
    backup_enabled: bool = Field(
        default=True, description="Passe de sauvegarde quotidienne de memory_data/."
    )
    backup_hour: int = Field(
        default=4,
        description="Heure de la sauvegarde (0-23). Apres le Curator nocturne, pas pendant.",
    )
    backup_keep: int = Field(
        default=7, description="Nombre d'archives conservees ; les plus anciennes sont purgees."
    )
    # ── Push des initiatives proactives ───────────────────────
    # Le moteur proactif produisait des decisions a prendre que rien ne poussait :
    # il fallait ouvrir le Command Center pour les decouvrir.
    push_proactive_enabled: bool = Field(
        default=True,
        description=(
            "Pousse les initiatives vers les canaux de messagerie (Telegram...). "
            "Sans ca, une initiative attend que vous ouvriez l'interface."
        ),
    )
    push_notify_priority_min: str = Field(
        default="high",
        description=(
            "Priorite minimale pour pousser une initiative de type NOTIFY : "
            "high, medium ou low. Les VALIDATE sont toujours poussees, elles "
            "demandent une decision. Tout pousser reviendrait a n'etre plus lu."
        ),
    )

    # ── Heures de silence ─────────────────────────────────────
    # Le push fonctionne, et c'est justement le probleme : une suggestion sans
    # urgence n'a pas a sonner a trois heures du matin. Ce qui se remarque, on
    # finit par le couper -- et on perd alors l'alerte utile avec le reste.
    push_heures_silence: str = Field(
        default="23:00-07:00",
        description=(
            "Plage ou l'on ne pousse que ce qui ne peut pas attendre, au format "
            "HH:MM-HH:MM. Vide = pousser a toute heure. La plage peut enjamber "
            "minuit."
        ),
    )
    push_silence_laisse_passer_urgent: bool = Field(
        default=True,
        description=(
            "Pendant les heures de silence, laisser passer les decisions de "
            "priorite haute. Sinon RIEN ne part, y compris ce qui ne peut pas "
            "attendre le matin."
        ),
    )

    # ── Boite de reception Obsidian ───────────────────────────
    # Le miroir Markdown se regenere depuis SQLite : une correction tapee dedans
    # disparaissait au rendu suivant. La boite est le fichier qui, lui, est lu.
    obsidian_inbox_enabled: bool = Field(
        default=True,
        description=(
            "Relit `memory_data/mirror/boite-de-reception.md` et applique les "
            "consignes ecrites a la main (corriger, oublier, retenir)."
        ),
    )
    obsidian_inbox_interval_minutes: int = Field(
        default=10,
        description=(
            "Delai entre deux relectures de la boite. Court, parce qu'une "
            "correction tapee sur telephone doit etre prise en compte pendant "
            "qu'on y pense encore ; en dessous d'une minute, la valeur est ignoree."
        ),
    )

    backup_copy_to: str = Field(
        default="",
        description=(
            "Dossier hors machine ou recopier l'archive (partage reseau, cle USB, "
            "dossier synchronise). Vide = archive sur le meme support que l'original, "
            "ce qui ne protege pas d'une panne de ce support."
        ),
    )
    calendar_reminder_minutes: int = Field(
        default=10,
        description="Délai de rappel avant un event calendar (minutes).",
    )
    proactive_lat: float = Field(default=45.75, description="Latitude pour la météo proactive.")
    proactive_lon: float = Field(default=4.85, description="Longitude pour la météo proactive.")
    proactive_city: str = Field(default="Lyon", description="Nom de ville pour la météo proactive.")

    # ── Home Assistant ────────────────────────────────────────
    home_assistant_url: str = Field(
        default="http://homeassistant.local:8123",
        description="URL de l'instance Home Assistant (collecte proactive).",
    )
    home_assistant_token: SecretStr = Field(
        default=SecretStr(""),
        description="Token d'accès longue durée Home Assistant.",
    )

    # ── Docker V2 ────────────────────────────────────────────
    skill_sandbox_allow_host_exec: bool = Field(
        default=False,
        description=(
            "Autorise le Skill Lab a executer une candidate SUR L'HOTE quand Docker "
            "est indisponible. Defaut False, et il doit le rester : le skill.py d'une "
            "candidate est ecrit par un LLM, et l'executer hors conteneur lui donne le "
            ".env, le reseau et l'ecriture dans skills_data/installed/ — d'ou le code "
            "repart en exec_module dans le processus Crush. La barriere de validation "
            "humaine devient alors contournable par le code qu'elle est censee arbitrer. "
            "Mettre a True revient a accorder au modele les droits du service."
        ),
    )

    docker_enabled: bool = Field(
        default=False,
        description="Active l'exécution des projets dans des containers Docker isolés.",
    )
    docker_base_image: str = Field(
        default="python:3.11-slim",
        description="Image Docker de base pour les containers worker.",
    )
    docker_memory_limit: str = Field(
        default="512m",
        description="Limite mémoire des containers Docker (ex: 512m, 1g).",
    )
    docker_cpu_limit: float = Field(
        default=1.0,
        description="Limite CPU des containers Docker (1.0 = 1 cœur).",
    )
    docker_network: str = Field(
        default="none",
        description="Mode réseau Docker : 'none' (isolé) ou 'bridge' (internet limité).",
    )
    docker_timeout_seconds: int = Field(
        default=300,
        description="Timeout max par step Docker en secondes.",
    )

    # ── Imprimante 3D (BambuLab) ──────────────────────────────
    printer_ip: str = Field(
        default="",
        description="IP locale de la BambuLab.",
    )
    printer_serial: str = Field(
        default="",
        description="Numéro de série BambuLab (ex: 01P00A123456789).",
    )
    printer_access_code: str = Field(
        default="",
        description="Code d'accès BambuLab — 8 chiffres dans Bambu Studio → Settings → Printer.",
    )

    # ── Budget & coût ─────────────────────────────────────────
    budget_enabled: bool = Field(
        default=False,
        description="Active le contrôle de budget (hard-stop + alertes). Désactivé par défaut.",
    )
    budget_monthly_usd: float = Field(
        default=10.0,
        description="Plafond mensuel global en USD (toutes dépenses LLM/API confondues).",
    )
    budget_per_project_usd: float = Field(
        default=2.0,
        description="Plafond par run de projet agent en USD.",
    )
    budget_warn_pct: float = Field(
        default=80.0,
        description="Seuil d'alerte budget (% du plafond). Déclenche une notification.",
    )
    usd_to_eur: float = Field(
        default=0.92,
        description=(
            "Taux USD -> EUR pour l'affichage. Les tarifs des fournisseurs sont "
            "publies en dollars, mais Google facture en euros : sans conversion, "
            "le tableau de bord n'est pas comparable a la facture. Taux indicatif "
            "et fige — a ajuster si l'ecart devient genant."
        ),
    )
    max_concurrent_workers: int = Field(
        default=3,
        description="Nombre maximal de workers agentiques simultanés.",
    )

    # ── Fusion 360 MCP ────────────────────────────────────────
    fusion_enabled: bool = Field(
        default=False,
        description="Active l'intégration Fusion 360 (MCP HTTP).",
    )
    fusion_mcp_url: str = Field(
        default="http://127.0.0.1:27182/mcp",
        description="URL complète du serveur MCP Fusion 360.",
    )

    # ── Face Recognition ──────────────────────────────────────
    face_recognition_enabled: bool = Field(
        default=False,
        description="Active la reconnaissance faciale dans le daemon vision.",
    )
    face_recognition_threshold: float = Field(
        default=0.45,
        description="Distance max pour une correspondance (plus bas = plus strict).",
    )

    # ── Clap Detection ────────────────────────────────────────
    clap_detection_enabled: bool = Field(
        default=False,
        description="Active la détection de double clap pour le wake up.",
    )
    clap_amplitude_threshold: float = Field(
        default=0.35,
        description="Seuil d'amplitude pour détecter un clap (0.0-1.0).",
    )

    # ── Utilisateur ──────────────────────────────────────────
    user_firstname: str = Field(
        default="",
        description="Prénom de l'utilisateur (USER_FIRSTNAME dans .env).",
    )
    user_profile: str = Field(
        default="",
        description=(
            "Bio courte de l'utilisateur (USER_PROFILE dans .env), injectée dans le "
            "moteur proactif. Ex. 'entrepreneur tech, YouTuber hardware, Lyon'. Vide = omis."
        ),
    )

    assistant_name: str = Field(
        default="",
        description="Nom donné à L'assistant (ASSISTANT_NAME dans .env).",
    )

    @model_validator(mode="after")
    def _unify_google_key(self) -> Settings:
        """Fait converger GOOGLE_API_KEY et GEMINI_API_KEY vers un seul champ.

        Les consommateurs ne lisent que `google_api_key` ; l'utilisateur peut
        renseigner l'un ou l'autre nom. On teste le contenu, pas la présence :
        une variable déclarée mais vide ne doit pas masquer l'autre.
        """
        if not self.google_api_key.get_secret_value():
            fallback = self.gemini_api_key.get_secret_value()
            if fallback:
                self.google_api_key = SecretStr(fallback)
        return self

    @property
    def display_name(self) -> str:
        """Prénom à utiliser dans les prompts. Repli sur 'Max' si non configuré."""
        return (self.user_firstname or "").strip() or "Max"

    @property
    def display_assistant_name(self) -> str:
        """Nom de l'assistant dans les prompts. Repli sur 'Crush' si non configuré.

        Miroir de `display_name` : le repli est centralisé ici, jamais dupliqué
        en `settings.assistant_name or "Crush"` sur les sites d'appel. Un .env
        existant n'a pas ASSISTANT_NAME, et lire le champ brut y donnerait la
        chaîne vide — donc « Tu es . » dans tous les prompts système.
        """
        return (self.assistant_name or "").strip() or "Crush"

    # ── Wake Up sequence ─────────────────────────────────────
    wakeup_enabled: bool = Field(
        default=False,
        description="Active la séquence wake up (veille + clap + scan facial). Désactiver en dev.",
    )

    # ── Mode Québécois ────────────────────────────────────────
    quebec_mode: bool = Field(
        default=False,
        description=(
            "Active le mode Québécois : voix québécoise + dialecte québécois dans le prompt."
        ),
    )
    quebec_voice_id: str = Field(
        default="RBhYSNMNu6b2CGZ9Fn1M",
        description="ID de la voix ElevenLabs québécoise.",
    )

    # ── Logging ───────────────────────────────────────────────
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")


# Singleton — importé partout via `from config.settings import settings`
settings = Settings()
