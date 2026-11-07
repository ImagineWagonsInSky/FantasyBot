"""Response shape adapters, ported from LaLigaApp's responseAdapters.js
(GPL-3.0-or-later, (c) 2025–present Externoak).

LaLiga changes response shapes between seasons (26/27 moved players to
`teamId` without an embedded team, wrapped lists in `elements`, and replaced
the calendar's `local`/`visitor` objects with IDs). These functions turn the
current shapes back into one stable shape before validation.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

LIST_KEYS = ("elements", "data", "leagues", "items")


class ResponseShapeError(Exception):
    """The response is not shaped the way the client expects."""

    def __init__(self, endpoint: str, problem: str) -> None:
        super().__init__(f"{endpoint}: {problem}")
        self.endpoint = endpoint
        self.problem = problem


def extract_list(endpoint: str, body: Any) -> list[Any]:
    """A list, or a list wrapped in one of the known envelope keys.

    Unlike LaLigaApp's extractArray, an unrecognised shape is an error rather
    than an empty list, so a silent API change shows up in the health check.
    """
    if isinstance(body, list):
        return body
    if isinstance(body, Mapping):
        for key in LIST_KEYS:
            if isinstance(body.get(key), list):
                return body[key]
        keys = ", ".join(sorted(map(str, body))[:8]) or "none"
        raise ResponseShapeError(endpoint, f"expected a list, got an object with keys: {keys}")
    raise ResponseShapeError(endpoint, f"expected a list, got {type(body).__name__}")


def extract_teams_master(body: Any) -> list[Any]:
    """26/27 returns a flat list; older seasons wrapped it in {teams} or {data: {teams}}."""
    if isinstance(body, Mapping):
        if isinstance(body.get("teams"), list):
            return body["teams"]
        inner = body.get("data")
        if isinstance(inner, Mapping) and isinstance(inner.get("teams"), list):
            return inner["teams"]
    return extract_list("teams_master", body)


def teams_by_id(teams: list[Any]) -> dict[str, Mapping[str, Any]]:
    return {str(t["id"]): t for t in teams if isinstance(t, Mapping) and "id" in t}


def normalize_player(player: Any, teams: Mapping[str, Mapping[str, Any]]) -> Any:
    if not isinstance(player, Mapping):
        return player
    out = dict(player)
    team_id = player.get("teamId")
    if team_id is None and isinstance(player.get("team"), Mapping):
        team_id = player["team"].get("id")
    team_id = str(team_id) if team_id is not None else None
    if not out.get("name"):
        out["name"] = player.get("nickname")
    if not out.get("team") and team_id is not None:
        info = teams.get(team_id, {})
        out["team"] = {"id": team_id, **{k: info.get(k) for k in
                                          ("name", "shortName", "slug", "badgeColor", "badgeWhite")}}
    if not out.get("images"):
        images = player.get("images") if isinstance(player.get("images"), Mapping) else {}
        url = player.get("image") or (images.get("transparent") or {}).get("256x256")
        if url:
            out["images"] = {"transparent": {"256x256": url}, "player": url}
    return out


def normalize_standing_entry(entry: Any) -> Any:
    if not isinstance(entry, Mapping):
        return entry
    team = entry.get("team") if isinstance(entry.get("team"), Mapping) else {}
    manager = team.get("manager") if isinstance(team.get("manager"), Mapping) else {}

    def first(*values: Any) -> Any:
        return next((v for v in values if v is not None), None)

    return {
        **entry,
        "id": first(entry.get("id"), team.get("id")),
        "name": first(entry.get("name"), team.get("name")),
        "points": first(entry.get("points"), team.get("teamPoints"), team.get("points")),
        "teamValue": first(entry.get("teamValue"), team.get("teamValue")),
        "weekPoints": first(entry.get("weekPoints"), team.get("weekPoints")),
        "userId": first(entry.get("userId"), manager.get("id")),
        "manager": entry["manager"] if isinstance(entry.get("manager"), str) else manager.get("managerName"),
        "team": {
            **team,
            "points": first(team.get("points"), team.get("teamPoints")),
            "manager": {**manager, "managerName": manager.get("managerName")},
        },
    }


def resolve_match_teams(match: Any, teams: Mapping[str, Mapping[str, Any]]) -> Any:
    if not isinstance(match, Mapping) or match.get("local") or match.get("localId") is None:
        return match

    def team_for(team_id: Any) -> Any:
        if team_id is None:
            return None
        return teams.get(str(team_id)) or {"id": str(team_id)}

    return {**match, "local": team_for(match.get("localId")),
            "visitor": team_for(match.get("visitorId"))}


def calendar_needs_teams(matches: list[Any]) -> bool:
    return any(isinstance(m, Mapping) and not m.get("local") and m.get("localId") is not None
               for m in matches)
