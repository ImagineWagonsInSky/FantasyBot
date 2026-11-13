from __future__ import annotations

import json
import re
import stat

import anthropic
import httpx2
import pytest
from pydantic import SecretStr

from fantasybot.advisor import Aliases, Candidate, cost_line, read_answer, system_prompt
from fantasybot.daily import deliver, prepare

from .test_actions import CHAT, NOW, act, tg  # noqa: F401  (fixtures)

KEY = "sk-ant-api03-" + "k" * 40
FRIENDS = ("Ana", "Bea", "Carl")


class FakeClaude:
    """The Messages and Models API, answering from what the request contains."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.answer = self.default_answer
        self.status = 200
        self.stop_reason = "end_turn"

    @staticmethod
    def find(data: dict, start: str, contains: str = "") -> str:
        return next(c["id"] for c in data["candidates"]
                    if c["action"].startswith(start) and contains in c["action"])

    def default_answer(self, data: dict) -> dict:
        isco = self.find(data, "Bid", "should beat")
        steal = self.find(data, "Pay", "Star")
        protect = self.find(data, "Raise Fwd A's clause")
        same_player = self.find(data, "Bid 1.20M on Isco")
        return {"headline": "Rival A is 200 ahead: take Star from Rival A and win Isco.",
                "picks": [{"id": steal, "why": "Swing of 6 xP a jornada, and it hurts Rival A."},
                          {"id": "c999", "why": "not a candidate"},
                          {"id": isco, "why": "Rival A and Rival B need a MID; this beats them."},
                          {"id": same_player, "why": "a second Isco bid"},
                          {"id": protect, "why": "Rival C can afford Fwd A's clause."}],
                "notes": ["Rival B is selling Listed.", "x" * 500]}

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if request.url.path.startswith("/v1/models/"):
            if self.status != 200:
                return httpx2.Response(self.status, json={"type": "error", "error": {
                    "type": "authentication_error", "message": "invalid x-api-key"}})
            return httpx2.Response(200, json={"id": "claude-opus-5-5", "type": "model",
                                              "display_name": "Claude Opus 5.5",
                                              "created_at": "2026-10-01T00:00:00Z"})
        if self.status != 200:
            return httpx2.Response(self.status, json={"type": "error", "error": {
                "type": "authentication_error", "message": "invalid x-api-key"}})
        body = json.loads(request.content)
        data = json.loads(body["messages"][0]["content"])
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "thinking", "thinking": "", "signature": "sig"},
                        {"type": "text", "text": json.dumps(self.answer(data))}],
            "stop_reason": self.stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 6000, "output_tokens": 400}})

    @property
    def sent(self) -> dict:
        return json.loads(self.requests[-1].content)

    @property
    def data(self) -> dict:
        return json.loads(self.sent["messages"][0]["content"])


@pytest.fixture
def claude():
    return FakeClaude()


@pytest.fixture
def planned(act, claude):  # noqa: F811
    act.settings.anthropic_api_key = SecretStr(KEY)
    http = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(claude))

    async def no_sleep(_):
        return None

    async def run(**kw):
        return await prepare(act.client, act.settings, alerter_for(act), now=NOW, sleep=no_sleep,
                             http_client=http, **kw)

    act.prepare = run
    return act


def alerter_for(kit):
    from .conftest import RecordingAlerter

    if not hasattr(kit, "alerter"):
        kit.alerter = RecordingAlerter()
    return kit.alerter


def mentions_a_friend(text: str) -> bool:
    return any(re.search(rf"(?<!\w){name}(?!\w)", text) for name in FRIENDS)


# -- the request ---------------------------------------------------------------------------


async def test_the_plan_request_is_anonymous_low_effort_and_constrained(planned, claude):
    daily = await planned.prepare()
    request = claude.requests[-1]
    assert request.headers["x-api-key"] == KEY
    assert "server-side-fallback-2026-07-01" in request.headers["anthropic-beta"]
    body = claude.sent
    assert body["model"] == "claude-opus-5-5" and body["fallbacks"] == "default"
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["system"] == system_prompt(planned.settings)
    assert "no shields in this league" in body["system"] and "{protection}" not in body["system"]
    sent = body["messages"][0]["content"]
    assert not mentions_a_friend(sent)  # managers, their teams, the feed's free text: none of it
    assert "Rival A" in sent and "Rival C" in sent
    data = claude.data
    assert {r["alias"] for r in data["rivals"]} == {"Rival A", "Rival B", "Rival C"}
    assert data["you"]["cash"] == "4.00M" and data["you"]["behind_leader"] == 200
    assert not any("Shield" in c["action"] for c in data["candidates"])  # no shields in this league
    assert daily.problem is None and daily.plan is not None


async def test_only_listed_candidates_survive_one_per_player_with_names_restored(planned, claude):
    daily = await planned.prepare()
    picks = daily.plan.picks
    assert [p.candidate.label for p in picks] == [
        "Steal Star from Ana (3.00M)", "Bid 1.28M on Isco", "Raise Fwd A's clause +2.00M"]
    assert picks[0].why == "Swing of 6 xP a jornada, and it hurts Ana."
    assert daily.plan.headline.startswith("Ana is 200 ahead")
    assert daily.plan.notes[0] == "Bea is selling Listed." and len(daily.plan.notes[1]) <= 220
    assert daily.plan.usage == "claude-opus-5-5: 6.0k tokens in, 0.4k out ≈ $0.032"
    text = daily.plan_text
    assert text.startswith("🧠 Today's plan · jornada 12 starts Sat 14 Nov 21:00")
    assert "1. Steal Star from Ana (3.00M): Swing of 6 xP" in text
    assert "3. Raise Fwd A's clause +2.00M: Carl can afford Fwd A's clause." in text


# -- what gets sent --------------------------------------------------------------------------


async def test_only_the_plan_is_sent_with_its_buttons_and_details_on_request(planned, claude):
    daily = await planned.prepare()
    await deliver(daily, planned.settings, planned.alerter, planned.center)
    assert planned.alerter.messages == []  # the full briefing isn't sent
    plan = planned.tg.sent[-1]
    assert plan["text"].startswith("🧠 Today's plan") and "/details for the full briefing" in plan["text"]
    buttons = planned.tg.buttons()
    assert list(buttons) == ["1. Steal Star from Ana (3.00M)", "2. Bid 1.28M on Isco",
                             "3. Raise Fwd A's clause +2.00M"]
    await planned.tap("2. Bid 1.28M on Isco")
    assert "Bid 1.28M for Isco" in planned.tg.text and planned.fake.writes == []  # asks first, as always

    await planned.center.handle({"update_id": 2, "message": {
        "text": "/details", "chat": {"id": CHAT, "type": "private"}, "from": {"id": CHAT}}})
    texts = [m["text"] for m in planned.tg.sent[-4:]]
    assert any(t.startswith("📄 The full briefing from") for t in texts)
    assert any("🛒 Market" in t for t in texts)


async def test_both_style_and_no_listener_send_the_full_briefing_too(planned, claude):
    planned.settings.briefing_style = "both"
    daily = await planned.prepare()
    await deliver(daily, planned.settings, planned.alerter, planned.center)
    assert planned.tg.sent[-1]["text"].startswith("🧠") and "/details" not in planned.tg.sent[-1]["text"]
    assert any("🛒 Market" in m for m in planned.alerter.messages)
    planned.alerter.messages.clear()
    planned.settings.briefing_style = "plan"
    await deliver(daily, planned.settings, planned.alerter, None)  # no listener to answer /details
    sent = planned.alerter.messages
    assert sent[0].startswith("🧠") and any("🛒 Market" in m for m in sent)


@pytest.mark.parametrize("trouble, reason", [
    ("status", "the API key was refused; store a new one with `./fb secret set anthropic_api_key`"),
    ("refusal", "the model declined to answer"),
    ("garbage", "the answer wasn't the expected JSON"),
])
async def test_without_a_plan_the_full_briefing_goes_out_as_before(planned, claude, trouble, reason):
    if trouble == "status":
        claude.status = 401
    elif trouble == "refusal":
        claude.stop_reason = "refusal"
    else:
        claude.answer = lambda data: "not an object"
    daily = await planned.prepare()
    assert daily.plan is None and daily.problem == reason
    await deliver(daily, planned.settings, planned.alerter, planned.center)
    assert planned.alerter.messages[-1] == f"🧠 No plan today: {reason}"
    assert any("🛒 Market" in m for m in planned.alerter.messages)
    assert "Want to act on any of this?" in planned.tg.text  # the usual buttons
    assert KEY not in "".join(planned.alerter.messages)


async def test_no_key_means_no_call_and_the_usual_briefing(planned, claude):
    planned.settings.anthropic_api_key = None
    daily = await planned.prepare()
    assert claude.requests == [] and daily.plan is None and daily.problem is None
    planned.settings.anthropic_api_key = SecretStr(KEY)
    planned.settings.advisor = False
    assert (await planned.prepare()).plan is None and claude.requests == []


# -- pieces --------------------------------------------------------------------------------------


def test_answers_are_checked_against_the_candidates():
    class View:
        standing: list = []

    aliases = Aliases.__new__(Aliases)
    aliases._names, aliases._real, aliases._teams = {"Ana": "Rival A"}, {"Rival A": "Ana"}, {}
    cands = [Candidate("c1", "Bid 1M on X", "Bid 1M on X", group="s0"),
             Candidate("c2", "Bid 2M on X", "Bid 2M on X", group="s0"),
             Candidate("c3", "Shield Y", "Shield Y")]
    plan = read_answer({"headline": "Rival A leads", "picks": [
        {"id": "c2", "why": "beats Rival A"}, {"id": "c1", "why": "same player"}, {"id": " c3 ", "why": "ok"},
        {"id": "c4", "why": "made up"}, "junk"], "notes": [1, "Rival D?"]}, cands, aliases)
    assert [p.candidate.id for p in plan.picks] == ["c2", "c3"]
    assert plan.picks[0].why == "beats Ana" and plan.headline == "Ana leads"
    assert plan.notes == ["Rival D?"]  # an alias that doesn't exist stays as it is
    assert aliases.scrub("Ana's team and Banana") == "Rival A's team and Banana"


def test_cost_line_for_unknown_models_has_tokens_only():
    usage = type("U", (), {"input_tokens": 1500, "output_tokens": 100})()
    assert cost_line("some-other-model", usage) == "some-other-model: 1.5k tokens in, 0.1k out"


async def test_health_check_confirms_the_key_without_spending(settings, tokens, client, clock, http, store,
                                                               router, claude):
    from fantasybot.external import ExternalData
    from fantasybot.healthcheck import HealthCheck

    from .conftest import NOW as TOKEN_NOW
    from .conftest import b2c_ok, seed

    seed(store, id_exp=TOKEN_NOW + 3600)
    router.add("POST", "https://login.laliga.es/", b2c_ok(TOKEN_NOW + 3600))
    transport = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(claude))

    async def run(s):
        report = await HealthCheck(s, tokens, client, clock=clock, external=ExternalData(http, s),
                                   anthropic_http=transport).run()
        return report, {r.name: r for r in report.results}["daily_plan"]

    _, result = await run(settings)
    assert result.status == "skip"
    keyed = settings.model_copy(update={"anthropic_api_key": SecretStr(KEY)})
    _, result = await run(keyed)
    assert (result.status, result.detail) == ("ok", "Claude Opus 5.5 available, effort low")
    assert all(r.url.path.startswith("/v1/models/") for r in claude.requests)  # no message, no tokens
    claude.status = 401
    report, result = await run(keyed)
    assert result.status == "warn" and "refused" in result.detail and report.healthy


def test_secret_set_takes_an_anthropic_key(tmp_path, monkeypatch, capsys):
    import io

    from fantasybot.cli import main

    target = tmp_path / "secrets" / "anthropic_api_key"
    monkeypatch.setenv("FANTASYBOT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FANTASYBOT_ANTHROPIC_API_KEY_FILE", str(target))
    monkeypatch.setattr("sys.stdin", io.StringIO("0123456789abcdef0123456789abcdef\n"))
    assert main(["secret", "set", "anthropic_api_key"]) == 1 and not target.exists()
    monkeypatch.setattr("sys.stdin", io.StringIO(KEY + "\n"))
    assert main(["secret", "set", "anthropic_api_key"]) == 0
    assert target.read_text().strip() == KEY and stat.S_IMODE(target.stat().st_mode) == 0o600
    out = capsys.readouterr()
    assert KEY not in out.out + out.err


async def test_shields_are_only_suggested_when_the_league_has_them(planned, claude):
    planned.settings.shields = True
    daily = await planned.prepare()
    assert any(c["action"].startswith("Shield Fwd A and Def D") for c in claude.data["candidates"])
    assert "Shielding (only in the app)" in claude.sent["system"]
    assert "Shield Fwd A, Def D in the app." in "\n".join(daily.messages)


async def test_the_plan_never_sees_a_managers_listing_as_biddable(planned, claude):
    await planned.prepare()
    data = claude.data
    assert "Listed" not in [m["name"] for m in data["market"]]
    assert not any("on Listed" in c["action"] for c in data["candidates"])
