# Copyright (C) 2026 Max Ea
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS,   .


from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, timedelta

from crush.kernel.paths import MEMORY_DATA_DIR

# UsageEntry, PRICING et calculate_cost descendus dans kernel/schemas.py
# en C.1.3 (cassure CYCLE 1). Ré-exportés ici pour ne pas casser les
# call-sites historiques (providers/llm, providers/audio, http_system, etc.)
# qui font encore `from crush.engine.tracking import UsageEntry, ...`.
# Élimination en C9 « zéro ré-export » + bascule app.py → bootstrap.
from crush.kernel.schemas import PRICING, UsageEntry, calculate_cost  # noqa: F401
from crush.kernel.settings import settings


def _tarif(modele: str, champ: str) -> float:
    """Tarif publié appliqué à ce modèle, 0 si le modèle est inconnu de PRICING.

    Un zéro se lit alors directement sur le tableau de bord : un modèle absent
    du barème est compté gratuit, ce qui est la façon silencieuse de
    sous-estimer une facture.
    """
    for tarifs in PRICING.values():
        exact = tarifs.get(modele)
        if exact is not None:
            return float(exact.get(champ, 0.0))
    for tarifs in PRICING.values():
        for cle, valeurs in tarifs.items():
            if modele.startswith(cle):
                return float(valeurs.get(champ, 0.0))
    return 0.0


def _jours_dans_le_mois(jour: date) -> int:
    """Nombre de jours du mois auquel `jour` appartient."""
    debut_mois_suivant = (jour.replace(day=28) + timedelta(days=4)).replace(day=1)
    return (debut_mois_suivant - timedelta(days=1)).day


class UsageTracker:
    CONSO_DIR = MEMORY_DATA_DIR / "conso"

    def __init__(
        self,
        on_usage_callback: Callable[[UsageEntry], None] | None = None,
    ) -> None:
        """Phase C : `on_usage_callback` injecté pour casser le cycle
        engine.budget ↔ engine.tracking.

        Auparavant : `track()` faisait `from crush.engine.budget import
        get_budget_guard` en local et appelait `guard.record(...)` —
        cycle interne à engine masqué par un import différé.

        Maintenant : tracking ne connaît plus budget. Le câblage
        tracking → budget se fait à l'extérieur (bootstrap.build()).
        """
        self.CONSO_DIR.mkdir(parents=True, exist_ok=True)
        self._on_usage: Callable[[UsageEntry], None] = on_usage_callback or (lambda _: None)

    def set_on_usage_callback(self, callback: Callable[[UsageEntry], None]) -> None:
        """Permet le câblage two-phase utilisé par bootstrap pour casser
        le cycle BudgetGuard ↔ UsageTracker (tracker créé sans callback,
        puis budget créé avec tracker, puis callback ré-attaché ici).
        """
        self._on_usage = callback

    def track(self, entry: UsageEntry) -> None:
        """Enregistre une entrée de consommation dans le fichier JSONL du jour."""
        today = date.today().isoformat()
        log_file = self.CONSO_DIR / f"{today}.jsonl"
        with log_file.open("a") as f:
            f.write(json.dumps(entry.__dict__) + "\n")

        # Notifie l'abonné (typiquement BudgetGuard.record pour les coûts
        # mission). Le câblage vit dans bootstrap.build(), pas ici.
        try:
            self._on_usage(entry)
        except Exception:
            pass  # le tracking ne doit jamais lever d'exception

    def tracked_since(self) -> date | None:
        """Date du plus ancien relevé, None si le registre est vide.

        Le suivi n'a pas toujours existé : tout ce qui a été dépensé avant que
        les providers ne poussent leurs `UsageEntry` est définitivement absent
        d'ici. Sans cette date, le total du mois se lit comme un total complet
        et ne colle pas avec la facturation du fournisseur — l'écart passe pour
        une erreur de tarif alors que ce sont des appels jamais enregistrés.
        """
        dates: list[date] = []
        for f in self.CONSO_DIR.glob("*.jsonl"):
            try:
                dates.append(date.fromisoformat(f.stem))
            except ValueError:
                continue
        return min(dates) if dates else None

    def _read_day(self, d: date) -> list[dict]:
        log_file = self.CONSO_DIR / f"{d.isoformat()}.jsonl"
        if not log_file.exists():
            return []
        entries: list[dict] = []
        with log_file.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return entries

    def get_session_summary(self) -> dict:
        """Résumé de la session courante (depuis minuit)."""
        entries = self._read_day(date.today())
        providers: dict = {}
        total_tokens = 0
        total_calls = 0
        total_cost = 0.0
        total_tts_chars = 0

        for e in entries:
            provider = e.get("provider", "unknown")
            model = e.get("model", "unknown")

            if provider not in providers:
                providers[provider] = {"models": {}, "total_cost": 0.0, "calls": 0}

            prov = providers[provider]
            if model not in prov["models"]:
                prov["models"][model] = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "characters": 0,
                    "audio_minutes": 0.0,
                    "images": 0,
                    "calls": 0,
                    "cost": 0.0,
                }

            m = prov["models"][model]
            m["input_tokens"] += e.get("input_tokens", 0)
            m["output_tokens"] += e.get("output_tokens", 0)
            m["characters"] += e.get("characters", 0)
            m["audio_minutes"] += e.get("audio_minutes", 0.0)
            m["images"] += e.get("images", 0)
            m["calls"] += 1
            m["cost"] += e.get("cost_usd", 0.0)

            prov["calls"] += 1
            prov["total_cost"] += e.get("cost_usd", 0.0)

            total_tokens += e.get("input_tokens", 0) + e.get("output_tokens", 0)
            total_calls += 1
            total_cost += e.get("cost_usd", 0.0)
            total_tts_chars += e.get("characters", 0)

        return {
            "total_tokens": total_tokens,
            "total_api_calls": total_calls,
            "total_cost_usd": round(total_cost, 4),
            "total_tts_chars": total_tts_chars,
            "providers": providers,
        }

    def get_daily_totals(self, days: int = 7) -> list[dict]:
        """Totaux par jour sur les N derniers jours."""
        today = date.today()
        result = []
        for i in range(days - 1, -1, -1):
            d = today - timedelta(days=i)
            entries = self._read_day(d)
            day_cost = sum(e.get("cost_usd", 0.0) for e in entries)
            result.append(
                {
                    "date": d.isoformat(),
                    "day": d.strftime("%a")[:3].upper(),
                    "cost_usd": round(day_cost, 4),
                }
            )
        return result

    def get_recent_calls(self, limit: int = 200) -> list[dict]:
        """Entrées brutes de la session courante, les plus récentes en premier."""
        entries = self._read_day(date.today())
        return list(reversed(entries[-limit:]))

    def get_daily_by_provider(self, days: int = 7) -> list[dict]:
        """Tokens LLM par provider par jour sur les N derniers jours."""
        today = date.today()
        result = []
        for i in range(days - 1, -1, -1):
            d = today - timedelta(days=i)
            entries = self._read_day(d)
            row: dict = {
                "date": d.isoformat(),
                "day": d.strftime("%a")[:3].upper(),
                "anthropic": 0,
                "gemini": 0,
                "openai": 0,
                "elevenlabs": 0,
                "deepgram": 0,
            }
            for e in entries:
                p = e.get("provider", "")
                if p in row:
                    row[p] += e.get("input_tokens", 0) + e.get("output_tokens", 0)
            result.append(row)
        return result

    def get_monthly_totals(self) -> dict:
        """Totaux du mois courant avec ventilation par provider et par type de contexte."""
        today = date.today()
        first = today.replace(day=1)
        total_cost = 0.0
        total_tokens = 0
        prov_acc: dict[str, dict] = {}  # {name: {cost, tokens, chars}}
        type_acc: dict[str, float] = {}  # {type_key: cost}

        d = first
        while d <= today:
            for e in self._read_day(d):
                cost = e.get("cost_usd", 0.0)
                tok = e.get("input_tokens", 0) + e.get("output_tokens", 0)
                prov = e.get("provider", "other")
                ctx = e.get("context", "") or ""
                chars = e.get("characters", 0)

                total_cost += cost
                total_tokens += tok

                if prov not in prov_acc:
                    prov_acc[prov] = {"cost": 0.0, "tokens": 0, "chars": 0}
                prov_acc[prov]["cost"] += cost
                prov_acc[prov]["tokens"] += tok
                prov_acc[prov]["chars"] += chars

                # Classify by usage type
                if prov in ("elevenlabs", "deepgram") or ctx == "voice":
                    # `ctx == "voice"` : la transcription passe par OpenAI, un
                    # provider aussi utilisé pour du texte — seul le contexte
                    # distingue les deux.
                    tkey = "voice"
                elif ctx == "mission" or ctx.startswith("mission:"):
                    # « mission » nu = planification, avant que le projet
                    # n'existe et n'ait donc un identifiant.
                    tkey = "mission"
                elif ctx == "memory":
                    tkey = "memory"
                elif ctx == "proactive":
                    tkey = "proactive"
                elif ctx.startswith("skill"):
                    # skill-synthesis, skill-improvement, skill (exécution).
                    tkey = "skill"
                elif ctx == "conversation":
                    tkey = "conversation"
                else:
                    tkey = "other"
                type_acc[tkey] = type_acc.get(tkey, 0.0) + cost

            d += timedelta(days=1)

        total_cost = round(total_cost, 4)

        # Build providers list (sorted by cost desc)
        prov_list = [
            {
                "name": name,
                "cost_usd": round(v["cost"], 4),
                "tokens": v["tokens"],
                "chars": v["chars"],
                "pct": round(v["cost"] / total_cost, 4) if total_cost else 0,
            }
            for name, v in sorted(prov_acc.items(), key=lambda x: -x[1]["cost"])
        ]

        # Build usage type list
        TYPE_META = {
            "conversation": {
                "label": "Échange direct",
                # Les deux noms venaient d'un « Marc ↔ Crush » codé en dur,
                # hérité du dépôt d'origine et faux pour tout autre utilisateur.
                "sub": (
                    f"chat & voix · {settings.display_name} ↔ "
                    f"{settings.display_assistant_name}"
                ),
                "color": "#4A9EFF",
            },
            "mission": {
                "label": "Agents en mission",
                "sub": "missions autonomes · 24/7",
                "color": "#D97757",
            },
            "memory": {
                "label": "Indexation & mémoire",
                "sub": "lectures & écritures mémoire",
                "color": "#B8963E",
            },
            "proactive": {
                "label": "Proactif",
                "sub": "tâches proactives · auto",
                "color": "#36D399",
            },
            "skill": {
                "label": "Skill Lab",
                "sub": "synthèse & amélioration de compétences",
                "color": "#F472B6",
            },
            "voice": {
                "label": "Voix · STT/TTS",
                "sub": "synthèse & transcription",
                "color": "#A78BFA",
            },
            "other": {"label": "Autre", "sub": "appels non classifiés", "color": "#6B7280"},
        }
        type_list = []
        for key, meta in TYPE_META.items():
            c = type_acc.get(key, 0.0)
            if c == 0.0:
                continue
            type_list.append(
                {
                    "type": key,
                    "label": meta["label"],
                    "sub": meta["sub"],
                    "color": meta["color"],
                    "cost_usd": round(c, 4),
                    "pct": round(c / total_cost, 4) if total_cost else 0,
                }
            )
        type_list.sort(key=lambda x: -x["cost_usd"])

        # Nombre de jours REELLEMENT couverts par le registre ce mois-ci. Une
        # extrapolation basée sur le quantième du mois diviserait par des jours
        # sans relevé et diviserait d'autant la prévision.
        debut = self.tracked_since()
        premier_jour_suivi = max(debut, first) if debut else today
        jours_suivis = max(1, (today - premier_jour_suivi).days + 1)

        return {
            "month": today.strftime("%Y-%m"),
            "cost_usd": total_cost,
            "tokens": total_tokens,
            "providers": prov_list,
            "by_type": type_list,
            "tracked_since": debut.isoformat() if debut else None,
            "days_tracked": jours_suivis,
            "days_in_month": _jours_dans_le_mois(today),
        }

    def get_monthly_by_model(self) -> list[dict]:
        """Tokens et coût par modèle pour le mois courant, triés par coût.

        Entrée et sortie sont rendues SÉPARÉMENT, avec le tarif appliqué. Un
        total « 61 K tokens » ne se vérifie contre aucun barème : les deux sens
        se paient à des prix qui varient d'un facteur six. Google facture
        d'ailleurs par SKU distincts (« text input token count », « text output
        token count »), et c'est à ce niveau que la comparaison se fait.
        """
        today = date.today()
        first = today.replace(day=1)
        model_acc: dict[str, dict] = {}
        total_cost = 0.0

        d = first
        while d <= today:
            for e in self._read_day(d):
                model = e.get("model", "unknown")
                cost = e.get("cost_usd", 0.0)
                entree = e.get("input_tokens", 0)
                sortie = e.get("output_tokens", 0)
                if model not in model_acc:
                    model_acc[model] = {
                        "cost": 0.0,
                        "tokens": 0,
                        "input": 0,
                        "output": 0,
                        "calls": 0,
                    }
                acc = model_acc[model]
                acc["cost"] += cost
                acc["tokens"] += entree + sortie
                acc["input"] += entree
                acc["output"] += sortie
                acc["calls"] += 1
                total_cost += cost
            d += timedelta(days=1)

        total_cost = total_cost or 1e-9
        return [
            {
                "model": name,
                "cost_usd": round(v["cost"], 6),
                "tokens": v["tokens"],
                "input_tokens": v["input"],
                "output_tokens": v["output"],
                "calls": v["calls"],
                # Le barème appliqué, rendu avec le total. Sans lui, un écart
                # avec la facture ne se diagnostique pas : impossible de dire
                # si c'est le tarif qui a vieilli ou des appels qui manquent.
                "rate_input_per_1m": _tarif(name, "input_per_1m"),
                "rate_output_per_1m": _tarif(name, "output_per_1m"),
                "pct": round(v["cost"] / total_cost * 100),
            }
            for name, v in sorted(model_acc.items(), key=lambda x: -x[1]["cost"])
        ]

    def get_today_hourly(self) -> list[float]:
        """Coût par heure (locale) pour aujourd'hui — liste de 24 valeurs."""
        entries = self._read_day(date.today())
        hours = [0.0] * 24
        for e in entries:
            ts = e.get("timestamp", "")
            try:
                hour = int(ts[11:13])
                hours[hour] += e.get("cost_usd", 0.0)
            except (ValueError, IndexError):
                pass
        return [round(h, 6) for h in hours]
