"""The tool-call wire format: the seam the conversation suite cannot see.

`tests/test_chat.py` replaces `_request`, which is exactly right for testing
policy — what we spend, when we give up, what we do with an answer. The cost is
that everything *below* the seam is invisible to it, and `_wire`'s own docstring
says so. Two live-only faults hid there, and this file exists because of them:

* a function response was sent in a turn with ``role="tool"``, which the API
  rejects outright — "Role 'tool' is not supported" — so every turn in which the
  model called a tool executed the tool and then failed;
* a replayed function call dropped its ``thought_signature``, which the current
  models require, so the call came back refused as well.

Both are the same class of mistake: a payload the API will not accept, which no
test against a stubbed transport can notice. So these tests assert the *shape*
of what goes on the wire — the role, and the signature — rather than the
behaviour above it. Nothing here talks to Google.
"""
from types import SimpleNamespace

import pytest

from app import chat, config, db

# The roles the API documents as valid. A function response belongs in "user";
# the point of the assertion is that it is in this set at all.
_VALID_ROLES = {"user", "model"}


def _part(call, signature=None):
    """One response part: a function call, and the signature beside it."""
    return SimpleNamespace(function_call=call, thought_signature=signature)


def _call(name="delete_message", args=None, call_id=None):
    return SimpleNamespace(name=name, args=args or {}, id=call_id)


def _response(parts=None, text=""):
    content = SimpleNamespace(parts=list(parts or []))
    candidate = SimpleNamespace(content=content)
    return SimpleNamespace(candidates=[candidate], text=text)


@pytest.fixture(autouse=True)
def chat_env(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_CHAT_API_KEY", "test-key")
    monkeypatch.setattr(config, "GEMINI_CHAT_HISTORY_TURNS", 8)
    monkeypatch.setattr(config, "ADMIN_TOOL_MAX_CALLS", 4)
    db.init()
    chat.reset_state()
    yield
    chat.reset_state()


# ── _wire: what actually goes on the wire ─────────────────────────────────
def test_a_replayed_function_call_keeps_its_thought_signature():
    """The signature is required by the current models; dropping it is a 400."""
    from google.genai import types

    wire = chat._wire(
        [
            {
                "role": "model",
                "parts": [
                    {
                        "function_call": types.FunctionCall(
                            name="ban_member", args={"target_user_id": 5}
                        ),
                        "thought_signature": b"signature-bytes",
                    }
                ],
            }
        ]
    )

    assert wire[0].parts[0].thought_signature == b"signature-bytes"
    assert wire[0].parts[0].function_call.name == "ban_member"


def test_a_function_call_without_a_signature_is_still_convertible():
    """Older responses carry none; the conversion must not crash on them."""
    from google.genai import types

    wire = chat._wire(
        [
            {
                "role": "model",
                "parts": [
                    {"function_call": types.FunctionCall(name="list_admins", args={})}
                ],
            }
        ]
    )

    assert wire[0].parts[0].function_call.name == "list_admins"
    assert not wire[0].parts[0].thought_signature


def test_a_function_response_converts():
    from google.genai import types

    wire = chat._wire(
        [
            {
                "role": "user",
                "parts": [
                    {
                        "function_response": types.FunctionResponse(
                            name="ban_member", response={"result": {"ok": True}}
                        )
                    }
                ],
            }
        ]
    )

    assert wire[0].parts[0].function_response.name == "ban_member"


def test_the_converted_role_is_the_one_it_was_given():
    from google.genai import types

    wire = chat._wire(
        [
            {"role": "user", "parts": [{"text": "سلام"}]},
            {"role": "model", "parts": [{"text": "سلام!"}]},
        ]
    )

    assert [turn.role for turn in wire] == ["user", "model"]
    assert wire[0].parts[0].text == "سلام"


# ── _calls_with_signatures: keeping the call and its signature together ───
def test_the_calls_are_read_with_their_signatures():
    from google.genai import types

    response = _response(
        [_part(_call("ban_member", {"target_user_id": 5}), b"sig")]
    )

    found = chat._calls_with_signatures(response, types)

    assert len(found) == 1
    assert found[0]["call"].name == "ban_member"
    assert found[0]["part"]["thought_signature"] == b"sig"


def test_several_calls_in_one_turn_are_all_kept():
    from google.genai import types

    response = _response(
        [
            _part(_call("get_member", {"user_id": 5}), b"a"),
            _part(_call("ban_member", {"target_user_id": 5}), b"b"),
        ]
    )

    found = chat._calls_with_signatures(response, types)

    assert [entry["call"].name for entry in found] == ["get_member", "ban_member"]
    assert [entry["part"]["thought_signature"] for entry in found] == [b"a", b"b"]


def test_a_response_with_no_parts_falls_back_to_function_calls():
    """A future SDK shape must degrade to "no signature", not "no tool call"."""
    from google.genai import types

    response = SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=[]))],
        function_calls=[_call("list_admins", {})],
    )

    found = chat._calls_with_signatures(response, types)

    assert len(found) == 1
    assert found[0]["call"].name == "list_admins"
    assert "thought_signature" not in found[0]["part"]


def test_a_text_only_response_yields_no_calls():
    from google.genai import types

    assert chat._calls_with_signatures(_response([], text="سلام"), types) == []


# ── _tool_turn: the loop that was failing in production ───────────────────
def _scripted(monkeypatch, *responses):
    """Replace the network seam and record every payload it is handed."""
    sent: list[list] = []

    async def _request_full(contents, *, tools=None, context=""):
        sent.append(list(contents))
        return responses[min(len(sent) - 1, len(responses) - 1)]

    monkeypatch.setattr(chat, "_request_full", _request_full)
    return sent


def test_the_function_response_is_sent_in_a_role_the_api_accepts(monkeypatch):
    """The regression this file exists for.

    The turn carrying a tool's result was labelled ``tool``, a role the API does
    not have. The result was that the tool ran, the follow-up request was
    refused, and the user was told the assistant was unavailable — for every
    turn in which the model reached for a tool.
    """
    from google.genai import types

    calls = []
    sent = _scripted(
        monkeypatch,
        _response([_part(_call("ban_member", {"target_user_id": 5}), b"sig")]),
        _response([], text="انجام شد"),
    )

    async def on_tool(name, args):
        calls.append((name, args))
        return {"ok": True}

    text = _run(
        chat._tool_turn(
            [{"role": "user", "parts": [{"text": "بنش کن"}]}],
            tools=[],
            context="",
            on_tool=on_tool,
        )
    )

    assert text == "انجام شد"
    assert calls == [("ban_member", {"target_user_id": 5})]
    response_turns = [t for t in sent[1] if t["parts"] and "function_response" in t["parts"][0]]
    assert response_turns, "no function response was sent back"
    for turn in response_turns:
        assert turn["role"] in _VALID_ROLES, turn["role"]
        assert turn["role"] != "tool", "'tool' is not a role the API accepts"


def test_the_replayed_call_carries_the_signature_the_model_gave(monkeypatch):
    from google.genai import types

    sent = _scripted(
        monkeypatch,
        _response([_part(_call("ban_member", {"target_user_id": 5}), b"sig-bytes")]),
        _response([], text="انجام شد"),
    )

    async def on_tool(name, args):
        return {"ok": True}

    _run(
        chat._tool_turn(
            [{"role": "user", "parts": [{"text": "بنش کن"}]}],
            tools=[],
            context="",
            on_tool=on_tool,
        )
    )

    # The second request must contain the model's own call, replayed with the
    # signature attached, or the API refuses it.
    model_turns = [t for t in sent[1] if t["role"] == "model"]
    assert model_turns
    replayed = model_turns[0]["parts"][0]
    assert replayed["function_call"].name == "ban_member"
    assert replayed["thought_signature"] == b"sig-bytes"


def test_a_turn_with_no_tool_call_answers_immediately(monkeypatch):
    sent = _scripted(monkeypatch, _response([], text="سلام"))

    async def on_tool(name, args):  # pragma: no cover - must not be reached
        raise AssertionError("a tool was called when the model asked for none")

    text = _run(
        chat._tool_turn(
            [{"role": "user", "parts": [{"text": "سلام"}]}],
            tools=[],
            context="",
            on_tool=on_tool,
        )
    )

    assert text == "سلام"
    assert len(sent) == 1


def test_a_failing_tool_is_reported_to_the_model_rather_than_raised(monkeypatch):
    """A tool that raises is an answer, not a crash."""
    sent = _scripted(
        monkeypatch,
        _response([_part(_call("ban_member", {"target_user_id": 5}))]),
        _response([], text="نتونستم"),
    )

    async def on_tool(name, args):
        raise RuntimeError("boom")

    text = _run(
        chat._tool_turn(
            [{"role": "user", "parts": [{"text": "بنش کن"}]}],
            tools=[],
            context="",
            on_tool=on_tool,
        )
    )

    assert text == "نتونستم"
    response_turn = [t for t in sent[1] if t["parts"] and "function_response" in t["parts"][0]][0]
    assert response_turn["parts"][0]["function_response"].response["result"] == {
        "error": "the tool failed"
    }


def test_the_loop_is_bounded_and_the_last_request_has_no_tools(monkeypatch):
    """A model that keeps reaching for a tool must still end in words."""
    from app import config as cfg

    monkeypatch.setattr(cfg, "ADMIN_TOOL_MAX_CALLS", 2)
    sent = _scripted(
        monkeypatch,
        _response([_part(_call("get_member", {"user_id": 5}))]),
        _response([_part(_call("get_member", {"user_id": 5}))]),
        _response([], text="تمام"),
    )

    async def on_tool(name, args):
        return {"ok": True}

    text = _run(
        chat._tool_turn(
            [{"role": "user", "parts": [{"text": "کی هست"}]}],
            tools=[],
            context="",
            on_tool=on_tool,
        )
    )

    assert text == "تمام"
    # Three requests: two rounds of calls, then the final one.
    assert len(sent) == 3


def _run(coro):
    import asyncio

    return asyncio.run(coro)
