# Notices — Messaging Gateway

## Hermes (NousResearch) — MIT License

Le design du `MessagingGateway` (session mapping cross-plateforme, `BasePlatformAdapter` ABC,
`dispatch()` avec persistance JSON) s'inspire des patterns publiés dans le projet Hermes
par NousResearch (https://github.com/NousResearch/hermes).

```
MIT License

Copyright (c) 2024 NousResearch

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Fichiers Crush utilisant ces patterns

- `channels/base.py` — `ChannelAdapter` ABC, `IncomingMessage`, `MessageTarget`, `Platform`
- `channels/gateway.py` — `MessagingGateway` avec session map JSON
- `channels/telegram_bot.py` — refactorisé pour implémenter `ChannelAdapter`
- `channels/discord_bot.py` — adaptateur Discord avec import guard
- `channels/whatsapp.py`, `channels/signal_bot.py`, `channels/slack_bot.py` — stubs
- `api/channels.py` — router FastAPI webhook `/api/channels/{platform}/webhook`
