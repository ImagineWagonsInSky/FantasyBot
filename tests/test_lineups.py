from __future__ import annotations

import httpx
import pytest

from fantasybot.api.models import Player
from fantasybot.external import ExternalData
from fantasybot.external.html import parse
from fantasybot.external.lineups import LineupEntry, TeamLineup, match_players, parse_lineup
from fantasybot.scout.model import COACH, DEF, FWD, GK, MID, FixtureOutlook, project

from . import scout_league
from .test_external import scout

FF = "https://www.futbolfantasy.com/laliga/equipos/"


# -- the HTML tree -----------------------------------------------------------------


def test_the_tree_survives_sloppy_html():
    doc = parse('<div class="a b"><p>one<br>two</span></p><img src=x><script>alert("x")</script>'
                '<p data-x="1">three</div><p>outside')
    div = doc.find(lambda n: n.has_classes("a", "b"))
    assert div is not None and div.text() == "one two three"  # stray </span> ignored, script skipped
    third = div.find(lambda n: n.get("data-x") == "1")
    assert third is not None and third.previous_element.tag == "script"
    assert doc.text().endswith("outside")


# -- the pitch layout (LaLigaApp's own test pages) ---------------------------------


def shirt(top, name, full="", clase="campo", lesion="-1", alts=(), slug=""):
    href = f"https://www.futbolfantasy.com/jugadores/{slug}" if slug else "#"
    alternatives = "".join(
        f'<a class="juggador pos-{i + 1} flex-column" data-lesion="-1" '
        f'href="{"https://www.futbolfantasy.com/jugadores/" + a[1] if a[1] else "#"}">'
        + (a[0] if a[2] else f'<span class="truncate-name mx-auto">{a[0]}</span>') + "</a>"
        for i, a in enumerate(alts))
    return (f'<div class="jugador_1 {clase} camiseta-wrapper" style="left: 50%; top: {top}%" '
            f'data-onceFF="titular"><a class="camiseta" href="{href}"><div class="fotocontainer laliga">'
            f'<img alt="{full}" class="img" src="x.png" /></div></a><div class="juggadores">'
            f'<a class="juggador pos-0 flex-column" data-lesion="{lesion}" href="{href}">'
            f'<span class="truncate-name mx-auto">{name}</span><div class="view valores is-flex d-none">'
            f'<span class="data-valor" data-valor="1">1,00M</span></div></a>{alternatives}</div></div>')


PRESEASON = ('<html><body><strong class="nombre-entrenador">Manuel Pellegrini</strong>'
             '<div class="tipo_lista_header jugador tipo_lista block-new">'
             '<span class="nombre">Jugador</span></div>'
             + shirt(87, "Valles", "Álvaro Valles", "portero", alts=[("Pau López", "", False),
                                                                     ("Guilherme", "", True)])
             + shirt(70, "D. Llorente", "Diego Javier Llorente", alts=[("Bartra", "", False)])
             + shirt(48, "Fornals", "Pablo Fornals", alts=[("Deossa", "", False)])
             + shirt(27, "Abde", "Abde Ezzalzouli", lesion="1")
             + shirt(27, "Antony", "Antony", alts=[("Ruibal", "", False)])
             + shirt(66, "Bellerín", "Héctor Bellerín", alts=[("Ruibal", "", False)])
             + shirt(66, "Galilea", slug="einar-galilea", alts=[("Recio", "angel-recio", False)])
             + "</body></html>")


def test_the_pitch_layout_matches_laligaapps_parser():
    lineup = parse_lineup(PRESEASON)
    assert lineup is not None and lineup.coach == "Manuel Pellegrini"
    starters = {e.nickname: e for e in lineup.entries if e.starter}
    bench = [e.nickname for e in lineup.entries if not e.starter]
    assert set(starters) == {"Valles", "D. Llorente", "Fornals", "Abde", "Antony", "Bellerín", "Galilea"}
    assert sorted(bench) == ["Bartra", "Deossa", "Guilherme", "Pau López", "Recio", "Ruibal"]  # Ruibal once
    assert starters["Fornals"].name == "Pablo Fornals"  # from the shirt's alt
    assert starters["Galilea"].name == "Einar Galilea"  # from the link's slug
    assert next(e for e in lineup.entries if e.nickname == "Recio").name == "Angel Recio"
    positions = {n: starters[n].position for n in ("Valles", "D. Llorente", "Fornals", "Antony")}
    assert positions == {"Valles": GK, "D. Llorente": DEF, "Fornals": MID, "Antony": FWD}
    assert starters["Abde"].status == "doubt"
    assert starters["Fornals"].probability is None and starters["Fornals"].chance == 0.8


# -- the in-season list layout --------------------------------------------------------


def row(name, prob=None, flag="titular", pos="", slug="", extra="", in_text=True):
    attr = f' data-probabilidad="{prob}"' if prob is not None and not in_text else ""
    text = f" {prob}%" if prob is not None and in_text else ""
    link = (f'<a href="https://www.futbolfantasy.com/jugadores/{slug}"><span class="nombre">{name}</span></a>'
            if slug else f'<span class="nombre">{name}</span>')
    return (f'<div class="cabecera" data-onceff="{flag}" data-posicion="{pos}"></div>'
            f'<div class="jugador tipo_lista"{attr}>{link}{extra}{text}</div>')


def list_page(starters, subs, coach="Coach"):
    return (f'<html><body><span class="nombre-entrenador">{coach}</span>'
            '<section class="lineup jugadores-titulares-list">'
            '<div class="tipo_lista_header jugador tipo_lista"><span class="nombre">Jugador</span></div>'
            + "".join(starters) + '</section><section class="jugadores-suplentes">' + "".join(subs)
            + "</section></body></html>")


def test_the_list_layout_reads_probabilities_positions_and_injuries():
    html = list_page(
        [row("Courtois", 95, pos="portero"), row("Rüdiger", 70, pos="defensa", in_text=False),
         row("Vini Jr.", 90, pos="delantero", slug="vinicius-junior")],
        [row("Ceballos", 25, flag="suplente", pos="centrocampista", extra='<i class="lesionado"></i>'),
         row("Publicidad del Fantasy", 10, flag="suplente")])
    lineup = parse_lineup(html)
    assert lineup is not None and lineup.coach == "Coach"
    entries = {e.nickname: e for e in lineup.entries}
    assert set(entries) == {"Courtois", "Rüdiger", "Vini Jr.", "Ceballos"}  # header and ad dropped
    assert entries["Courtois"].probability == 95 and entries["Courtois"].position == GK
    assert entries["Rüdiger"].probability == 70  # from data-probabilidad
    assert entries["Vini Jr."].name == "Vinicius Junior" and entries["Vini Jr."].starter
    assert not entries["Ceballos"].starter and entries["Ceballos"].status == "injured"
    assert entries["Ceballos"].chance == 0.25
    assert TeamLineup.from_json(lineup.to_json()) == lineup  # survives the cache


def test_pages_without_a_lineup_give_none():
    assert parse_lineup("") is None
    assert parse_lineup("<html><body><p>Mantenimiento</p></body></html>") is None


# -- matching to LaLiga players ------------------------------------------------------


def lp(pid, nickname, position, name=None, slug=None):
    data = {"id": pid, "nickname": nickname, "name": name, "positionId": position, "teamId": "x"}
    if slug:
        data["slug"] = slug
    return Player.model_validate(data)


def entry(name, nickname=None, position=None, probability=80):
    return LineupEntry(name, nickname or name, position, True, probability)


def test_names_match_across_sources_one_player_each():
    roster = [lp("1", "Einar", DEF, "Einar Galilea"), lp("2", "Dela", DEF, "Adrián de la Fuente"),
              lp("3", "Vini Jr.", FWD, "Vinícius José Paixão de Oliveira Júnior"),
              lp("4", "Morales", MID, "José Luis Morales"), lp("5", "Fornals", MID, "Pablo Fornals"),
              lp("6", "Pau López", GK), lp("7", "Pellegrini", COACH)]
    lineup = TeamLineup("Pellegrini", [
        entry("Einar Galilea", "Galilea", DEF), entry("Adrian De La Fuente", "De la Fuente", DEF),
        entry("Vinicius Junior", "Vini Jr.", FWD), entry("Jose Luis Morales", "Morales", MID),
        entry("Pablo Fornals", "Fornals", MID), entry("Fornals", "Fornals", MID),  # a duplicate row
        entry("Unknown Kid", "Kid", MID), entry("Pellegrini")])
    matched = match_players(lineup, roster)
    assert {pid: e.name for pid, e in matched.items()} == {
        "1": "Einar Galilea", "2": "Adrian De La Fuente", "3": "Vinicius Junior",
        "4": "Jose Luis Morales", "5": "Pablo Fornals"}  # no coach, no keeper, no stranger


def test_position_settles_a_close_call():
    roster = [lp("1", "Raúl García", FWD), lp("2", "Raúl Garcia", DEF)]
    matched = match_players(TeamLineup(None, [entry("Raul Garcia", position=DEF)]), roster)
    assert list(matched) == ["2"]


# -- expected points -------------------------------------------------------------------


def scorer(status="ok", position=MID):
    return Player.model_validate({"id": "s", "nickname": "S", "positionId": position, "averagePoints": 6,
                                  "playerStatus": status})


def test_the_start_chance_replaces_doubt_and_rotation_guesses():
    plain = project(scorer())
    sure = project(scorer(), fixtures=FixtureOutlook(start=1.0))
    benched = project(scorer(), fixtures=FixtureOutlook(start=0.1))
    doubt = project(scorer("doubt"), fixtures=FixtureOutlook(start=0.9))
    assert sure.expected == plain.expected == 6
    assert benched.expected == pytest.approx(6 * (0.3 + 0.7 * 0.1)) and benched.note == "10% to start"
    assert benched.outlook > benched.expected  # one benching is only part of the next three jornadas
    assert doubt.expected == pytest.approx(6 * 0.93)  # not halved again for the doubt
    assert project(scorer("injured"), fixtures=FixtureOutlook(start=0.9)).expected == 0
    coach = project(scorer(position=COACH), fixtures=FixtureOutlook(start=0.0))
    assert coach.expected == 6


# -- in the briefing ------------------------------------------------------------------


def club_pages():
    """Club 3 = Athletic, 5 = Real Madrid, 7 = FC Barcelona (whose page fails)."""
    athletic = list_page([row("Keeper", 95, pos="portero"), row("Hurt", 70, pos="defensa"),
                          row("Fwd A", 90, pos="delantero")],
                         [row("Mid A", 25, flag="suplente", pos="medio")])
    madrid = list_page([row("Def A", 85, pos="defensa"), row("Fwd B", 60, pos="delantero"),
                        row("Zidane Jr", 55, pos="medio")],
                       [row("Mid B", 15, flag="suplente", pos="medio")])
    return athletic, madrid


@pytest.fixture
def real_clubs(fake):
    scout_league.build(fake)
    names = {"3": "Athletic Club", "5": "Real Madrid", "7": "FC Barcelona"}
    for club in fake.data["teams_master"]:
        club["name"] = names.get(club["id"], club["name"])
    return fake


async def test_the_briefing_uses_probable_lineups(real_clubs, store, settings, tokens, freeze, http,
                                                  clock, alerter, router):
    athletic, madrid = club_pages()
    pages: list[str] = []

    def page(body: str, status: int = 200):
        def handler(request: httpx.Request) -> httpx.Response:
            pages.append(request.url.path)
            assert "FantasyBot" in request.headers["user-agent"]
            return httpx.Response(status, text=body)
        return handler

    router.add("GET", FF + "athletic", page(athletic))
    router.add("GET", FF + "real-madrid", page(madrid))
    router.add("GET", FF + "barcelona", page("", 500))
    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    external = ExternalData(http, settings, clock=lambda: scout_league.NOW, sleep=record)
    text, view = await scout(real_clubs, store, settings, tokens, freeze, http, clock, alerter,
                             external=external)
    assert pages == ["/laliga/equipos/athletic", "/laliga/equipos/real-madrid", "/laliga/equipos/barcelona"]
    assert sleeps == [2.0, 2.0]  # polite between pages
    assert view.lineup_clubs == 2
    squad = {p.name: p for p in view.my_squad}
    assert squad["Mid B"].projection.note == "15% to start"
    assert squad["Mid B"].expected < squad["Mid B"].projection.season_average
    section = text.split("📋")[1].split("\n\n")[0]
    assert "(futbolfantasy.com, 2 clubs)" in section
    assert "In your lineup but may not start: Mid A 25%, Mid B 15%" in section
    assert "Sources disagree: Hurt (LaLiga: injured; futbolfantasy: 70% to start)" in section
    assert "Couldn't match to LaLiga players: Zidane Jr" in section
    assert "Missing today: probable lineups: HTTP 500" in text

    pages.clear()
    view = await scout(real_clubs, store, settings, tokens, freeze, http, clock, alerter,
                       external=external, guard=True)
    assert pages == [] and "P31" in view.lineups  # the guard reads today's copies only


async def test_lineups_can_be_switched_off(real_clubs, store, settings, tokens, freeze, http, clock,
                                           alerter, router):
    pages: list[httpx.Request] = []
    router.add("GET", FF, lambda r: pages.append(r) or httpx.Response(200, text=club_pages()[0]))
    off = settings.model_copy(update={"lineups": False})
    text, view = await scout(real_clubs, store, off, tokens, freeze, http, clock, alerter,
                             external=ExternalData(http, off))
    assert pages == [] and view.lineups == {} and "📋" not in text


async def test_health_check_reads_one_club_page(settings, tokens, client, clock, http, store, router):
    from fantasybot.healthcheck import HealthCheck

    from .conftest import NOW as TOKEN_NOW
    from .conftest import b2c_ok, seed

    seed(store, id_exp=TOKEN_NOW + 3600)
    router.add("POST", "https://login.laliga.es/", b2c_ok(TOKEN_NOW + 3600))
    eleven = list_page([row(f"Titular {i}", 80) for i in range(11)], [row("Sub", 20, flag="suplente")])
    router.add("GET", FF + "real-madrid", httpx.Response(200, text=eleven),
               httpx.Response(200, text="<html><body>nuevo diseño</body></html>"))

    async def no_sleep(_):
        return None

    report = await HealthCheck(settings, tokens, client, clock=clock,
                               external=ExternalData(http, settings, sleep=no_sleep)).run()
    result = {r.name: r for r in report.results}["lineups"]
    assert result.status == "ok" and result.detail == "real-madrid: 11 starters, 1 alternatives"
    other = settings.model_copy(update={"state_dir": settings.state_dir.parent / "fresh"})
    report = await HealthCheck(other, tokens, client, clock=clock,
                               external=ExternalData(http, other, sleep=no_sleep)).run()
    result = {r.name: r for r in report.results}["lineups"]
    assert result.status == "warn" and "layout changed" in result.detail and report.healthy
