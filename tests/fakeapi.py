"""An in-memory LaLiga Fantasy API with one small, consistent league.

The shapes follow what LaLigaApp's code reads, including the 26/27 changes
(players with teamId only, calendar with localId/visitorId, wrapped lists).
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

import httpx

API = "https://fantasy-api.llt-services.com/api"
STATS = "https://fantasy-api.llt-services.com/stats"

ME = 101
LEAGUE = "L1"
MY_TEAM = "T1"
WEEK = 12

DATA: dict[str, Any] = {
    "current_user": {"id": ME, "managerName": "Me Myself", "email": "me@example.com"},
    "leagues": {"elements": [{"id": LEAGUE, "name": "Friends League"}]},
    "league_ranking": [
        {"position": 1, "team": {"id": MY_TEAM, "teamPoints": 120, "teamValue": 98_000_000,
                                 "manager": {"id": ME, "managerName": "Me Myself"}}},
        {"position": 2, "team": {"id": "T2", "teamPoints": 110, "teamValue": 91_000_000,
                                 "manager": {"id": 202, "managerName": "Pal Friend"}}},
    ],
    "league_ranking_by_week": [
        {"position": 1, "team": {"id": "T2", "teamPoints": 61, "manager": {"id": 202}}},
        {"position": 2, "team": {"id": MY_TEAM, "teamPoints": 48, "manager": {"id": ME}}},
    ],
    "league_activity": {"elements": [
        {"id": 9001, "activityTypeId": 1, "createdAt": "2026-11-01T10:00:00+01:00", "amount": 1_500_000},
    ]},
    "team_data": {"teamValue": 98_000_000, "players": [
        {"playerTeamId": "PT1", "buyoutClause": 45_000_000,
         "playerMaster": {"id": "P1", "nickname": "Pedri", "positionId": 3,
                          "marketValue": 40_000_000, "teamId": "3"}},
        {"playerTeamId": "PT2", "buyoutClause": 9_000_000,
         "playerMaster": {"id": "P3", "nickname": "Unai", "positionId": 1,
                          "marketValue": 7_000_000, "teamId": "5"}},
    ]},
    "team_lineup": {"formation": {"goalkeeper": [{"playerTeamId": "PT2"}], "defender": [],
                                  "midfield": [{"playerTeamId": "PT1"}], "striker": [],
                                  "tacticalFormation": [4, 4, 2]}},
    "current_lineup": {"formation": {"goalkeeper": [{"playerTeamId": "PT2"}], "defender": [],
                                     "midfield": [{"playerTeamId": "PT1"}], "striker": [],
                                     "tacticalFormation": [4, 3, 3]}},
    "free_formations": [[4, 4, 2], [4, 3, 3]],
    "premium_formations": {"elements": [[3, 5, 2]]},
    "premium_configuration": [{"key": "formations", "value": True}],
    "market": [
        {"id": "M1", "discr": "marketPlayerLeague", "salePrice": 1_000_000,
         "expirationDate": "2026-11-08T08:00:00+01:00", "numberOfBids": 2,
         "playerMaster": {"id": "P2", "nickname": "Isco", "positionId": 3, "teamId": "5"}},
        {"id": "M2", "discr": "marketPlayerTeam", "salePrice": 12_000_000,
         "playerMaster": {"id": "P1", "nickname": "Pedri", "teamId": "3"},
         "playerTeam": {"playerTeamId": "PT1", "teamId": MY_TEAM}},
    ],
    "players": [
        {"id": "P1", "nickname": "Pedri", "positionId": 3, "teamId": "3",
         "images": {"transparent": {"256x256": "https://assets.example/p1.png"}}},
        {"id": "P2", "nickname": "Isco", "positionId": 3, "teamId": "5"},
    ],
    "teams_master": [
        {"id": "3", "name": "FC Barcelona", "shortName": "FCB", "slug": "fc-barcelona"},
        {"id": "5", "name": "Real Betis", "shortName": "BET", "slug": "real-betis"},
    ],
    "current_week": {"weekNumber": WEEK},
    "matchday": [{"id": "G1", "localId": "3", "visitorId": "5", "matchState": 1,
                  "matchDate": "2026-11-08T21:00:00+01:00"}],
    "match_stats": [{"id": "G1", "players": []}],
    "team_money": {"teamMoney": 2_500_000},
    "player_offer": [{"id": "O1", "money": 13_000_000, "status": "pending", "bidderTeam": {"id": "T2"}}],
    "player_details": {"playerMaster": {"id": "P1", "nickname": "Pedri", "points": 80,
                                        "averagePoints": 6.7, "playerStats": []},
                       "marketPlayer": None, "seasons": []},
    "check_player_shield": {},
}

CMP = r"/api/v1/competition/(?P<cmp>\d+)"
ROUTES: list[tuple[str, str]] = [
    ("current_user", r"/api/v4/user/me"),
    ("leagues", CMP + r"/leagues"),
    ("league_ranking", CMP + r"/leagues/[^/]+/standing"),
    ("league_ranking_by_week", CMP + r"/leagues/[^/]+/standing/\d+"),
    ("league_activity", CMP + r"/leagues/[^/]+/activity/\d+"),
    ("team_data", CMP + r"/leagues/[^/]+/teams/[^/]+"),
    ("team_lineup", CMP + r"/teams/[^/]+/lineup/week/\d+"),
    ("current_lineup", CMP + r"/teams/[^/]+/lineup"),
    ("formations", r"/api/v4/teams/lineup/formations"),
    ("premium_configuration", r"/api/v4/leagues/premium-configuration"),
    ("market", CMP + r"/league/[^/]+/market"),
    ("players", CMP + r"/players"),
    ("teams_master", r"/api/v3/teams-master"),
    ("current_week", CMP + r"/week/current"),
    ("matchday", CMP + r"/calendar"),
    ("match_stats", r"/stats/v1/competition/\d+/stats/week/\d+"),
    ("team_money", CMP + r"/teams/[^/]+/money"),
    ("player_offer", CMP + r"/league/[^/]+/playerTeam/[^/]+/offer"),
    ("player_details", CMP + r"/player/[^/]+/league/[^/]+"),
    ("check_player_shield", CMP + r"/league/[^/]+/player-team/[^/]+/check-shield"),
]


class FakeLaLiga:
    def __init__(self) -> None:
        self.data = copy.deepcopy(DATA)
        self.overrides: dict[str, httpx.Response] = {}
        self.calls: list[httpx.Request] = []
        self.writes: list[httpx.Request] = []
        self.write_response = httpx.Response(200, json={"id": "new"})

    def endpoint_for(self, request: httpx.Request) -> str | None:
        for name, pattern in ROUTES:
            if re.fullmatch(pattern, request.url.path):
                if name == "formations":
                    return f"{request.url.params.get('option')}_formations"
                return name
        return None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.method != "GET":
            self.writes.append(request)
            return self.write_response
        name = self.endpoint_for(request)
        if name is None:
            return httpx.Response(404, json={"message": "no such route"})
        if name in self.overrides:
            return self.overrides[name]
        return httpx.Response(200, content=json.dumps(self.data[name]),
                              headers={"content-type": "application/json"})

    def get_calls(self, name: str) -> list[httpx.Request]:
        return [c for c in self.calls if c.method == "GET" and self.endpoint_for(c) == name]
