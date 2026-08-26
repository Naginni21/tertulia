"""The few fixed strings the concierge itself posts in the room."""

from __future__ import annotations

STRINGS: dict[str, dict[str, str]] = {
    "es": {
        "delegate_header": "🤖 {agent} (delegado de {owner})",
        "delegate_label": "{agent} (delegado de {owner})",
        "and": " y ",
        "asleep": "😴 {label} está dormido; seguimos.",
        "turn_timeout": "😴 {label} no respondió a tiempo; seguimos.",
        "passed": "🤖 {label} pasa esta ronda.",
        "share": "📎 comparte «{filename}»",
        "status_title": "Estado de la sala",
        "status_delegate": "{online} {label}",
        "status_ritual": "Ritual en curso: {ritual}",
        "status_no_ritual": "Sin ritual en curso.",
        "status_quota": "cupo usado {used}/{max}",
        "not_admin": "Solo un administrador puede usar ese comando.",
        "no_delegates": "Todavía no hay delegados en la sala.",
        "ritual_busy": "Ya hay un ritual en curso; lo encolé.",
        "online": "🟢",
        "offline": "⚫",
    },
    "en": {
        "delegate_header": "🤖 {agent} ({owner}'s delegate)",
        "delegate_label": "{agent} ({owner}'s delegate)",
        "and": " and ",
        "asleep": "😴 {label} is asleep; moving on.",
        "turn_timeout": "😴 {label} did not answer in time; moving on.",
        "passed": "🤖 {label} passes this round.",
        "share": "📎 shares «{filename}»",
        "status_title": "Room status",
        "status_delegate": "{online} {label}",
        "status_ritual": "Ritual in progress: {ritual}",
        "status_no_ritual": "No ritual in progress.",
        "status_quota": "quota used {used}/{max}",
        "not_admin": "Only an admin can use that command.",
        "no_delegates": "No delegates in the room yet.",
        "ritual_busy": "A ritual is already running; queued.",
        "online": "🟢",
        "offline": "⚫",
    },
}


class Strings:
    def __init__(self, language: str):
        self._s = STRINGS[language]

    def __call__(self, key: str, **fmt: object) -> str:
        return self._s[key].format(**fmt)

    def join_names(self, names: list[str]) -> str:
        if not names:
            return ""
        if len(names) == 1:
            return names[0]
        return ", ".join(names[:-1]) + self._s["and"] + names[-1]
