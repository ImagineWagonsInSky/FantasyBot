"""Expected points for the next jornada.

Deliberately simple and explainable: a blend of recent form (the last three
completed jornadas, counting jornadas a player didn't play as 0) and the
season average, scaled by availability. No hidden magic, so the briefing can
say why it ranks one player above another.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..api.models import Player, PlayerWeek

GK, DEF, MID, FWD, COACH = 1, 2, 3, 4, 5
POSITION_LABELS = {GK: "GK", DEF: "DEF", MID: "MID", FWD: "FWD", COACH: "COACH"}
_POSITION_WORDS = {
    "portero": GK, "defensa": DEF, "centrocampista": MID, "mediocampista": MID,
    "delantero": FWD, "entrenador": COACH,
}

# playerStatus values LaLigaApp knows: ok, doubt, injured, suspended.
STATUS_AVAILABILITY = {"ok": 1.0, "doubt": 0.5, "injured": 0.0, "suspended": 0.0}
UNKNOWN_STATUS_AVAILABILITY = 0.7
RECENT_JORNADAS = 3
FORM_WEIGHT = 0.6  # the rest is the season average
DID_NOT_PLAY_FACTOR = 0.7  # fit, but didn't play last jornada: rotation risk


def position_of(player: Player) -> int | None:
    if player.positionId in POSITION_LABELS:
        return player.positionId
    if player.position:
        return _POSITION_WORDS.get(player.position.strip().lower())
    return None


def position_label(player: Player) -> str:
    return POSITION_LABELS.get(position_of(player) or 0, "?")


@dataclass(frozen=True)
class Projection:
    expected: float
    season_average: float
    form: float | None  # average of the last RECENT_JORNADAS completed jornadas
    availability: float
    note: str  # why it's discounted, e.g. "injured", "didn't play last jornada"


def completed_weeks(player: Player, current_week: int | None) -> list[PlayerWeek]:
    weeks = [w for w in (player.playerStats or []) if w.weekNumber is not None
             and (current_week is None or w.weekNumber < current_week)]
    return sorted(weeks, key=lambda w: w.weekNumber or 0)


def project(player: Player, current_week: int | None = None) -> Projection:
    status = (player.playerStatus or "ok").lower()
    availability = STATUS_AVAILABILITY.get(status, UNKNOWN_STATUS_AVAILABILITY)
    note = "" if status == "ok" else status
    season = float(player.averagePoints or 0)

    weeks = completed_weeks(player, current_week)
    recent = weeks[-RECENT_JORNADAS:]
    form = sum(float(w.totalPoints or 0) for w in recent) / len(recent) if recent else None
    base = FORM_WEIGHT * form + (1 - FORM_WEIGHT) * season if form is not None else season

    factor = availability
    if status == "ok" and weeks and position_of(player) != COACH and weeks[-1].minutes == 0:
        factor *= DID_NOT_PLAY_FACTOR
        note = "didn't play last jornada"
    return Projection(round(factor * base, 2), round(season, 2),
                      round(form, 2) if form is not None else None, availability, note)
