"""A small 4-manager league for the scouting tests, loaded into FakeLaLiga.

You (T1) are 3rd, 200 behind Ana (T2). Your lineup has an injured defender in
it and a striker who didn't play last jornada; Ana's best forward has an open
clause you can afford; Bea's best player is shielded; one of Ana's players
unlocks in 5 hours. Jornada 12 kicks off at 21:00, 12h after NOW.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .fakeapi import FakeLaLiga

CET = timezone(timedelta(hours=1))
NOW = datetime(2026, 11, 14, 9, 0, tzinfo=CET)
KICKOFF = "2026-11-14T21:00:00+01:00"
GK, DEF, MID, FWD, COACH = 1, 2, 3, 4, 5


def iso(dt: datetime) -> str:
    return dt.isoformat()


def weeks(points: list[int], minutes: list[int] | None = None) -> list[dict]:
    minutes = minutes or [90] * len(points)
    return [{"weekNumber": 9 + i, "totalPoints": p, "stats": {"mins_played": [m, 2 if m >= 60 else 0]}}
            for i, (p, m) in enumerate(zip(points, minutes, strict=True))]


PLAYERS: dict[str, dict] = {}


def player(pid: str, name: str, pos: int, value: float, avg: float, status: str = "ok",
           recent: list[int] | None = None, minutes: list[int] | None = None, club: str = "3") -> dict:
    record = {"id": pid, "nickname": name, "positionId": pos, "marketValue": int(value * 1_000_000),
              "averagePoints": avg, "points": round(avg * 11), "playerStatus": status, "teamId": club}
    if recent:
        record["playerStats"] = weeks(recent, minutes)
    PLAYERS[pid] = record
    return record


def owned(pt: str, p: dict, *, clause: float | None = None, locked: datetime | None = None,
          shielded: bool = False) -> dict:
    master = {k: v for k, v in p.items() if k != "playerStats"}
    return {"playerTeamId": pt, "playerMaster": master,
            "buyoutClause": int(clause * 1_000_000) if clause else master["marketValue"],
            "buyoutClauseLockedEndTime": iso(locked) if locked else "2026-09-01T13:20:00+02:00",
            "isShielded": shielded}


def schedule() -> dict[str, list[dict]]:
    """Jornadas 1-11 played: club 5 concedes 3 a match, club 7 scores 3. Jornada 12
    starts at KICKOFF; club 11 (newly promoted) has no match in it."""
    weeks: dict[str, list[dict]] = {}
    for w in range(1, 12):
        day = NOW - timedelta(days=7 * (12 - w))
        flip = w % 2 == 0
        weeks[str(w)] = [
            {"id": f"R{w}a", "localId": "5" if flip else "3", "visitorId": "3" if flip else "5",
             "localScore": 2 if flip else 3, "visitorScore": 3 if flip else 2, "matchDate": iso(day)},
            {"id": f"R{w}b", "localId": "9" if flip else "7", "visitorId": "7" if flip else "9",
             "localScore": 1 if flip else 3, "visitorScore": 3 if flip else 1, "matchDate": iso(day)},
        ]
    weeks["12"] = [{"id": "G1", "localId": "3", "visitorId": "5", "matchDate": KICKOFF},
                   {"id": "G2", "localId": "7", "visitorId": "9", "matchDate": "2026-11-15T18:30:00+01:00"}]
    weeks["13"] = [{"id": "G3", "localId": "5", "visitorId": "3", "matchDate": "2026-11-21T21:00:00+01:00"},
                   {"id": "G4", "localId": "9", "visitorId": "11", "matchDate": "2026-11-22T18:30:00+01:00"}]
    weeks["14"] = [{"id": "G5", "localId": "3", "visitorId": "7", "matchDate": "2026-11-28T21:00:00+01:00"},
                   {"id": "G6", "localId": "11", "visitorId": "5", "matchDate": "2026-11-29T18:30:00+01:00"}]
    return weeks


def build(fake: FakeLaLiga, *, balance: int = 4_000_000) -> FakeLaLiga:
    PLAYERS.clear()
    my_squad = [
        owned("PT10", player("P10", "Keeper", GK, 5, 4, recent=[5, 4, 6]), shielded=True),
        owned("PT11", player("P11", "Backup", GK, 1, 1, recent=[0, 0, 0], minutes=[0, 0, 0])),
        owned("PT20", player("P20", "Hurt", DEF, 8, 6, status="injured", recent=[7, 6, 8])),
        owned("PT21", player("P21", "Def A", DEF, 4, 4, recent=[4, 3, 5], club="5")),
        owned("PT22", player("P22", "Def B", DEF, 3, 3, recent=[3, 3, 3], club="7")),
        owned("PT23", player("P23", "Def C", DEF, 2, 2.5, recent=[2, 3, 2], club="9")),
        owned("PT24", player("P24", "Def D", DEF, 6, 5, recent=[6, 5, 7])),
        owned("PT30", player("P30", "Mid A", MID, 10, 6, recent=[6, 7, 6]), locked=NOW + timedelta(days=9)),
        owned("PT31", player("P31", "Mid B", MID, 5, 4, recent=[4, 4, 4], club="5")),
        owned("PT32", player("P32", "Mid C", MID, 3, 3, recent=[3, 2, 4], club="7")),
        owned("PT33", player("P33", "Mid D", MID, 2, 2, recent=[1, 2, 2], club="11")),
        owned("PT40", player("P40", "Fwd A", FWD, 20, 7, recent=[8, 6, 9])),
        owned("PT41", player("P41", "Fwd B", FWD, 6, 4, recent=[5, 4, 4], club="5")),
        owned("PT42", player("P42", "Rested", FWD, 7, 5, recent=[5, 6, 0], minutes=[90, 90, 0],
                              club="7")),
        owned("PT50", player("P50", "Coach", COACH, 1, 3, recent=[3, 3, 3])),
    ]
    ana = [
        owned("PT60", player("P60", "Star", FWD, 2.9, 8), clause=3),
        owned("PT61", player("P61", "Soon", MID, 5, 5), locked=NOW + timedelta(hours=5)),
        owned("PT62", player("P62", "Big", FWD, 30, 9)),
    ]
    bea = [
        owned("PT70", player("P70", "Shielded", MID, 1, 7), shielded=True),
        owned("PT71", player("P71", "Cheap", DEF, 2, 3)),
    ]
    carl = [owned("PT80", player("P80", "Locked", MID, 2, 6), locked=NOW + timedelta(days=10))]
    market = [
        {"id": "M1", "discr": "marketPlayerLeague", "salePrice": 1_200_000, "numberOfBids": 2,
         "expirationDate": "2026-11-14T13:20:00+01:00",
         "playerMaster": player("P90", "Isco", MID, 1.2, 5, recent=[5, 6, 4], club="9")},
        {"id": "M2", "discr": "marketPlayerLeague", "salePrice": 40_000_000,
         "expirationDate": "2026-11-14T13:20:00+01:00", "playerMaster": player("P91", "Pricey", FWD, 40, 7)},
        {"id": "M3", "discr": "marketPlayerLeague", "salePrice": 1_000_000,
         "expirationDate": "2026-11-14T13:20:00+01:00",
         "playerMaster": player("P92", "Crocked", MID, 1, 6, status="injured")},
        {"id": "M4", "discr": "marketPlayerTeam", "salePrice": 6_500_000, "sellerTeam": {"id": "T1"},
         "playerMaster": {k: v for k, v in PLAYERS["P41"].items() if k != "playerStats"},
         "playerTeam": {"playerTeamId": "PT41"}},
        {"id": "M5", "discr": "marketPlayerTeam", "salePrice": 10_000_000, "sellerTeam": {"id": "T3"},
         "playerMaster": player("P72", "Listed", DEF, 10, 4, club="7"),
         "playerTeam": {"playerTeamId": "PT72"}},
    ]

    def row(team: str, manager: int, name: str, points: int, value: float) -> dict:
        return {"team": {"id": team, "teamPoints": points, "teamValue": int(value * 1_000_000),
                         "manager": {"id": manager, "managerName": name}}}

    fake.data.update({
        "current_user": {"id": 101},
        "leagues": [{"id": "L1", "name": "Friends"}],
        "league_ranking": [row("T2", 202, "Ana", 1100, 120), row("T3", 303, "Bea", 1000, 110),
                           row("T1", 101, "Me", 900, 100), row("T4", 404, "Carl", 800, 90)],
        "current_week": {"weekNumber": 12},
        "team_data_by_id": {"T1": {"players": my_squad}, "T2": {"players": ana},
                            "T3": {"players": bea}, "T4": {"players": carl}},
        "team_money": {"teamMoney": balance},
        "current_lineup": {"formation": {
            "goalkeeper": [{"playerTeamId": "PT10"}],
            "defender": [{"playerTeamId": pt} for pt in ("PT20", "PT21", "PT22", "PT23")],
            "midfield": [{"playerTeamId": pt} for pt in ("PT30", "PT31", "PT32", "PT33")],
            "striker": [{"playerTeamId": "PT40"}, {"playerTeamId": "PT42"}],
            "coach": [{"playerTeamId": "PT50"}],
            "tacticalFormation": [4, 4, 2]}},
        "free_formations": [[4, 4, 2], [4, 3, 3], [3, 5, 2]],
        "market": market,
        "players": list(PLAYERS.values()),
        "player_details_by_id": {pid: {"playerMaster": p} for pid, p in PLAYERS.items()},
        "matchday_by_week": schedule(),
        "teams_master": [{"id": c, "name": f"Club {c}", "shortName": f"C{c}"}
                         for c in ("3", "5", "7", "9", "11")],
        "league_activity": {"elements": [
            {"activityTypeId": 32, "createdAt": iso(NOW - timedelta(hours=1)),
             "description": "Ana clausuló a Fwd X por 12.000.000"},
            {"activityTypeId": 6, "createdAt": iso(NOW - timedelta(hours=2)), "amount": 900_000},
            {"activityTypeId": 1, "createdAt": iso(NOW - timedelta(days=3)), "playerName": "Old news"},
        ]},
    })
    return fake
