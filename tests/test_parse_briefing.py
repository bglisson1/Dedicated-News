"""Parser checks for model talking points. No network."""

import io
import json
import sys
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build

CONTEXT = "S&P 500 closed at 7743.41. Oil is 93.47. The mortgage rate is 7.03."
SECRET = "sk-live-key-DO-NOT-LOG"

HEARING = "You may hear that tension overseas is pushing fuel prices up."
WHY = (
    "Trucks move almost everything we buy, so when fuel costs more that can nudge "
    "the price of groceries and the other errands in a normal week."
)
BUT = "But one noisy week is not a reason to redo a plan that was built for years."
STORY = (
    "Picture filling the car and deciding the whole year has to change because the tank costs more. "
    "The pump is one errand. The plan is the long trip, and we do not turn around over one fill-up."
)
SAY = ["We see the headline, and we stay with your plan."]


def card(**overrides):
    payload = {
        "hearing": HEARING,
        "why": WHY,
        "but": BUT,
        "story": STORY,
        "say": SAY,
    }
    payload.update(overrides)
    return payload


def parse(content, context=CONTEXT):
    log = io.StringIO()
    with redirect_stdout(log):
        briefing = build.parse_briefing(content, context, "openrouter", secret=SECRET)
    return briefing, log.getvalue()


def test_fenced_json():
    body = json.dumps({"cards": [card(), card(hearing="You may hear that the Fed, which sets short-term interest rates, raised them.")]})
    briefing, log = parse("Here are the cards:\n```json\n" + body + "\n```\n")
    assert briefing is not None, log
    assert len(briefing.cards) == 2
    assert "fuel prices" in briefing.cards[0].hearing
    assert "FAILED" not in log


def test_preamble_then_object():
    body = json.dumps({"cards": [card()]})
    briefing, log = parse(
        "Sure — three notes would be a lot, so here is one solid card.\n\n"
        + body
        + "\n\nLet me know if you want a change."
    )
    assert briefing is not None, log
    assert len(briefing.cards) == 1
    assert briefing.cards[0].hearing.startswith("You may hear")


def test_variant_keys_and_casing():
    payload = {
        "TalkingPoints": [
            {
                "WhatClientsAreHearing": HEARING,
                "WhyItMatters": WHY,
                "RealityCheck": "One noisy week is not a reason to redo a plan that was built for years.",
                "Analogy": STORY,
                "AdvisorLines": SAY,
            }
        ]
    }
    briefing, log = parse(json.dumps(payload))
    assert briefing is not None, log
    assert len(briefing.cards) == 1
    assert briefing.cards[0].reality.lower().startswith("but")
    assert briefing.cards[0].story.startswith("Picture filling")
    assert briefing.cards[0].say == SAY


def test_top_level_array():
    briefing, log = parse(json.dumps([card()]))
    assert briefing is not None, log
    assert len(briefing.cards) == 1


def test_missing_optional_story_still_passes():
    raw = card(
        why=WHY + " The household still buys the same groceries, and the week does not get to rewrite the plan.",
    )
    raw.pop("story")
    raw.pop("say")
    briefing, log = parse(json.dumps({"cards": [raw]}))
    assert briefing is not None, log
    assert briefing.cards[0].story == ""
    assert briefing.cards[0].say == []
    html = build.render_talk_card(briefing.cards[0])
    assert "<h3>" in html
    assert "Picture filling" not in html


def test_jargon_and_rounded_fact_are_kept():
    raw = card(
        why=WHY + " The Dow was loud, and stocks sat near 7743.",
    )
    briefing, log = parse(json.dumps({"cards": [raw]}))
    assert briefing is not None, log
    assert "7743" in briefing.cards[0].why


def test_drop_bad_card_keep_the_good_one():
    bad = card(say=["You should buy stocks now and ignore the plan."])
    briefing, log = parse(json.dumps({"cards": [bad, card()]}))
    assert briefing is not None, log
    assert len(briefing.cards) == 1
    assert "fuel prices" in briefing.cards[0].hearing
    assert "dropped card 1: forbidden language" in log
    assert "FAILED" not in log


def test_reject_invented_number_and_log_the_reply():
    invented = card(why=WHY + " The index fell 4321 points before lunch.")
    raw = "```json\n" + json.dumps({"cards": [invented]}) + "\n```"
    raw = raw.replace("fuel", f"fuel {SECRET}", 1)
    briefing, log = parse(raw)
    assert briefing is None
    assert "FAILED rejected card 1 number not in the facts" in log
    assert "talking points: model reply:" in log
    assert SECRET not in log
    reply = log.split("model reply:", 1)[1].strip()
    assert len(reply) <= 400


def test_reject_empty_and_missing_headline():
    briefing, log = parse(json.dumps({"cards": [{}]}))
    assert briefing is None
    assert "FAILED rejected card 1 empty" in log
    briefing, log = parse(json.dumps({"cards": [{"why": WHY, "but": BUT, "story": STORY}]}))
    assert briefing is None
    assert "no headline" in log


def test_not_json_logs_a_slice_and_redacts_a_bearer_token():
    text = "I could not format this. Bearer abcdefghijklmnop " + ("word " * 80)
    briefing, log = parse(text)
    assert briefing is None
    assert "FAILED rejected not json" in log
    assert "Bearer [redacted]" in log
    assert "abcdefghijklmnop" not in log
    reply = log.split("model reply:", 1)[1].strip()
    assert len(reply) <= 400


def test_openrouter_requests_json_object():
    seen = {}

    class Response:
        def read(self, _n=None):
            return b'{"choices":[{"message":{"content":"{}"}}]}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_open(request, timeout=40):
        seen["body"] = json.loads(request.data.decode())
        seen["headers"] = {key.lower(): value for key, value in request.header_items()}
        return Response()

    original = urllib.request.urlopen
    urllib.request.urlopen = fake_open
    try:
        build.post_provider(
            "openrouter",
            SECRET,
            "https://openrouter.ai/api/v1/chat/completions",
            "anthropic/claude-sonnet-5",
            "system",
            "user",
            temperature=0.7,
            max_tokens=1200,
        )
    finally:
        urllib.request.urlopen = original
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert SECRET not in json.dumps(seen["body"])
    assert seen["headers"]["authorization"] == f"Bearer {SECRET}"


def test_json_mode_falls_back_when_the_model_rejects_it():
    calls = []

    class Response:
        def read(self, _n=None):
            return b'{"choices":[{"message":{"content":"{}"}}]}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_open(request, timeout=40):
        body = json.loads(request.data.decode())
        calls.append(body)
        if "response_format" in body:
            raise urllib.error.HTTPError(
                request.full_url,
                400,
                "Bad Request",
                hdrs=None,
                fp=io.BytesIO(b'{"error":{"message":"response_format is not supported"}}'),
            )
        return Response()

    original = urllib.request.urlopen
    urllib.request.urlopen = fake_open
    log = io.StringIO()
    try:
        with redirect_stdout(log):
            text = build.post_provider(
                "openrouter",
                SECRET,
                "https://openrouter.ai/api/v1/chat/completions",
                "anthropic/claude-sonnet-5",
                "system",
                "user",
            )
    finally:
        urllib.request.urlopen = original
    assert text == "{}"
    assert "response_format" in calls[0]
    assert "response_format" not in calls[1]
    assert "does not accept json mode" in log.getvalue()
    assert SECRET not in log.getvalue()


def main():
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
        print(f"ok {test.__name__}")
    print(f"{len(tests)} tests passed")


if __name__ == "__main__":
    main()
