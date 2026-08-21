# Notices — Messaging Gateway


## Fichiers Crush utilisant ces patterns

- `channels/base.py` — `ChannelAdapter` ABC, `IncomingMessage`, `MessageTarget`, `Platform`
- `channels/gateway.py` — `MessagingGateway` avec session map JSON
- `channels/telegram_bot.py` — refactorisé pour implémenter `ChannelAdapter`
- `channels/discord_bot.py` — adaptateur Discord avec import guard
- `channels/whatsapp.py`, `channels/signal_bot.py`, `channels/slack_bot.py` — stubs
- `api/channels.py` — router FastAPI webhook `/api/channels/{platform}/webhook`
