# Image multi-arch : python:3.11-slim publie linux/amd64 ET linux/arm64,
# donc ce Dockerfile se construit tel quel sur un Raspberry Pi 5 (aarch64).
FROM python:3.11-slim

# Deps système du cœur, à l'exécution uniquement — aucune compilation.
#
#   libportaudio2  `sounddevice` appartient a l extra `local-audio`.
#                  C'est un wrapper ctypes : il lui faut la lib PortAudio
#                  présente, même si on n'ouvre jamais de flux audio (sur un
#                  serveur headless, le micro est celui du navigateur client).
#   ca-certificates + curl  TLS sortant vers les APIs LLM/STT/TTS.
#
# Ce qui n'est PLUS nécessaire depuis la sortie des deps lourdes du cœur :
#   portaudio19-dev / gcc / python3-dev  → servaient à compiler pyaudio,
#     transitif de RealtimeSTT, supprimé (aucun usage dans src/).
#   cmake / openblas / libgl1  → requis par dlib et opencv, désormais dans
#     les extras `face` et `vision`. Sur ARM, ces deux-là coûtent très cher :
#     ne les ajouter que si l'extra est réellement installé.
RUN apt-get update -qq && apt-get install -y --no-install-recommends \
    libportaudio2 \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# uv installé en standalone (pas de dépendance à un paquet apt)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Copie du lockfile + pyproject d'abord pour profiter du cache Docker
# sur les layers de dépendances (ne re-sync que si ces fichiers changent)
COPY pyproject.toml uv.lock ./

# Cœur seul. Les extras (vision, face, local-audio, hardware) sont opt-in :
# les sortir du cœur est ce qui rend l'image installable sur un Pi — avec
# `vision`, torch + torchvision ajouteraient ~2,5 Go.
RUN uv sync --frozen --no-install-project --no-group dev

# Copie du reste du code applicatif
COPY . .

# Sync final pour installer le package crush lui-même
RUN uv sync --frozen --no-group dev

# Arborescence attendue par l'app (cf. setup.sh --ci)
RUN mkdir -p memory_data/sessions \
             memory_data/topics \
             memory_data/conso \
             memory_data/initiatives \
             memory_data/curator_reports \
             skills_data/installed \
             skills_data/candidates \
             workspace/projects

EXPOSE 8000

# API seule : le pipeline vocal vit dans l API, sur /ws/voice.
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
