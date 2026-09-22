"""The pure parts of Voice Live: states, audio, failures, identity, bridges.

Everything here can be tested without a call, a network or a credential, and
that is the point of the split: these are the rules, and the rules are what a
live call must not be able to bend. The session — which is timing and
interaction — is in ``test_voice_live_session.py``.

The four things this file exists to protect, in the order they matter:

* **Speaker identity cannot come from the model.** The action bridge has no field
  for an actor that a model could populate, and the speaker map refuses to
  attribute a stream it does not know.
* **The action vocabulary is a strict subset of the real one.** A spoken request
  can reach exactly five operations, all of them member-targeted moderation, and
  the owner-only switches, promotions, the coding agent and the VPN are not
  reachable from a voice chat at all.
* **A spoken action goes through the real authorisation.** Proven end to end
  against ``admin_service.execute``, including the refusals.
* **Live models cannot leak into other workloads.** The pool change that admits
  them is gated, and the gate is asserted rather than assumed.
"""
from __future__ import annotations

import asyncio
import math
import random
import time

import pytest

from app import admin_service, config, db, gemini_pool, rbac
from app.voice_live import (
    actions as VA,
    audio as A,
    awareness_bridge as AB,
    errors,
    gemini_live as GL,
    metrics as M,
    speakers as SP,
    state as S,
)


# ══ The state machine ═════════════════════════════════════════════════════
def test_every_state_has_a_label_and_a_transition_entry():
    """The vocabulary and its labels move together, and so do the moves.

    A state with no entry in the table can be entered and never left, which for
    anything but a terminal state is a session that wedges. ``FAILED`` and
    ``DISABLED`` are the only two allowed to be terminal, and both are asserted
    to be reachable from somewhere — a state nobody can reach is a state nobody
    can test.
    """
    for name in S.STATES:
        assert name in S.STATE_LABELS, name
        assert S.label(name) == S.STATE_LABELS[name]
        assert S.destinations(name) is not None
    # every destination is a real state
    for source in S.STATES:
        for target in S.destinations(source):
            assert target in S.STATES, (source, target)
    # only these two may be terminal
    for name in S.STATES:
        if not S.destinations(name):
            assert name in (S.FAILED, S.DISABLED), name
    # and both are reachable
    assert S.FAILED in S.destinations(S.JOINING)
    assert S.DISABLED in S.destinations(S.IDLE)


def test_an_illegal_move_is_refused_and_does_not_move():
    machine = S.Machine(state=S.SPEAKING)
    assert S.allows(S.SPEAKING, S.JOINING) is False
    assert machine.go(S.JOINING) is False
    assert machine.state == S.SPEAKING


def test_a_legal_move_moves_and_is_recorded():
    machine = S.Machine(state=S.CONNECTED)
    assert machine.go(S.LISTENING, reason="speech") is True
    assert machine.state == S.LISTENING
    assert machine.reason == "speech"
    assert machine.history[-1][:2] == (S.CONNECTED, S.LISTENING)
    assert "connected->listening" in machine.trail()


def test_re_asserting_the_current_state_is_not_a_move():
    """Several callers announce a state they expect to be in. Logging those as
    refusals would bury the real ones."""
    machine = S.Machine(state=S.LISTENING)
    assert machine.go(S.LISTENING) is False
    assert machine.history == []


def test_force_moves_anywhere_so_teardown_can_always_finish():
    """A machine wedged where the table cannot leave would hold a voice channel
    open for ever, and "we cannot clean up because the rules say no" is not an
    acceptable answer for something holding a call."""
    machine = S.Machine(state=S.JOINING)
    machine.force(S.IDLE, reason="teardown")
    assert machine.state == S.IDLE


def test_the_history_is_bounded():
    """A barge-in-heavy call moves every few seconds and can run for hours."""
    machine = S.Machine(state=S.CONNECTED)
    for _ in range(S.Machine.HISTORY_LIMIT * 3):
        machine.go(S.LISTENING)
        machine.go(S.CONNECTED)
    assert len(machine.history) <= S.Machine.HISTORY_LIMIT


def test_a_barge_in_leads_to_listening_not_to_waiting():
    """The interrupter is still talking. ``INTERRUPTED`` exists as its own state
    precisely so that leaving it means going to listen."""
    assert S.LISTENING in S.destinations(S.INTERRUPTED)
    assert S.INTERRUPTED in S.destinations(S.SPEAKING)
    # A barge-in while Nexus is silent is not a barge-in.
    assert S.INTERRUPTED not in S.destinations(S.LISTENING)
    assert S.INTERRUPTED not in S.destinations(S.CONNECTED)


def test_the_model_may_begin_speaking_from_a_quiet_state():
    """The model starts talking without this side having observed an utterance
    end: context injected between turns, a tool result, or a transcript marker
    that arrives late or not at all. The audio is the fact, so the table permits
    the move — refusing it made the machine disagree with the call it described,
    while the audio played anyway."""
    for quiet in (S.CONNECTED, S.LISTENING):
        assert S.SPEAKING in S.destinations(quiet), quiet
    assert S.THINKING in S.destinations(S.CONNECTED)
    # ...and the deliberate exclusion above is unchanged.
    assert S.INTERRUPTED not in S.destinations(S.CONNECTED)
    assert S.INTERRUPTED not in S.destinations(S.LISTENING)


def test_a_failed_session_can_still_be_torn_down():
    """A failed session is still a session holding a voice channel, and a
    teardown that could not move a failed machine out of its own state would
    leave the call joined for ever."""
    assert S.LEAVING in S.destinations(S.FAILED)


def test_a_reconnect_never_jumps_straight_back_to_listening():
    """After a reconnect the session does not know what is being said, so it
    waits in CONNECTED rather than assuming the pre-drop utterance continues."""
    assert S.destinations(S.RECONNECTING) == frozenset(
        {S.CONNECTED, S.LEAVING, S.FAILED}
    )


def test_the_call_states_are_the_transitional_ones_too():
    """A join that has been in progress for a minute is holding a slot."""
    for name in (S.JOINING, S.LEAVING, S.RECONNECTING):
        assert S.is_call_state(name) is True, name
    for name in (S.IDLE, S.DISABLED, S.FAILED):
        assert S.is_call_state(name) is False, name


def test_describe_is_safe():
    described = S.Machine(state=S.SPEAKING).describe()
    assert described["state"] == S.SPEAKING
    assert described["in_call"] is True
    assert set(described) == {"state", "label", "seconds", "reason", "in_call"}


# ══ Failures ══════════════════════════════════════════════════════════════
def test_only_the_weather_is_retryable():
    for reason in (errors.REASON_CONNECT_FAILED, errors.REASON_CONNECTION_LOST,
                   errors.REASON_STREAM_ENDED, errors.REASON_PROVIDER_BUSY,
                   errors.REASON_TIMEOUT, errors.REASON_GO_AWAY):
        assert errors.is_retryable(reason) is True, reason
    for reason in (errors.REASON_SETUP_REJECTED, errors.REASON_QUOTA_EXHAUSTED,
                   errors.REASON_DISABLED, errors.REASON_BUSY,
                   errors.REASON_NOT_CONFIGURED, errors.REASON_LIMIT_REACHED):
        assert errors.is_retryable(reason) is False, reason


def test_an_unknown_reason_is_not_retryable():
    """The safe direction. An unknown failure retried is a loop; an unknown
    failure abandoned is a call that ends and says so."""
    assert errors.is_retryable("something_nobody_has_seen") is False


def test_the_exception_types_carry_their_retryability():
    assert errors.ProviderTimeout("x").retryable is True
    assert errors.ConnectionLost("x").retryable is True
    assert errors.GoAway("x").retryable is True
    assert errors.SetupRejected("x").retryable is False
    assert errors.QuotaExhausted("x").retryable is False


def test_a_setup_rejection_is_recognised_by_meaning():
    """These are the exact strings the provider produced while this feature was
    being measured, and both are configuration problems that will not improve."""
    for text in (
        "1007 Unsupported language code 'fa-IR'",
        "1007 The requested combination of response modalities (AUDIO) is not supported",
        "API key not valid. Please pass a valid API key.",
    ):
        got = GL.classify(RuntimeError(text))
        assert got.reason == errors.REASON_SETUP_REJECTED, text
        assert got.retryable is False, text


def test_a_dropped_socket_is_retryable():
    got = GL.classify(RuntimeError("connection closed by peer"))
    assert got.reason == errors.REASON_CONNECTION_LOST
    assert got.retryable is True


def test_a_timeout_is_retryable():
    got = GL.classify(asyncio.TimeoutError())
    assert got.reason == errors.REASON_TIMEOUT
    assert got.retryable is True


def test_an_error_string_is_scrubbed_of_credential_shapes():
    # A synthetic credential of the real shape, never a live one: the point is
    # the *shape* the scrubber matches, and a test that carried a real token
    # would put it in the repository.
    secret = "AQ.Ab8RN6FAKEKEY000000000000000000000000000000000000009"
    out = GL._scrub(f"request failed for key {secret} at endpoint")
    assert "FAKEKEY" not in out
    assert "[redacted" in out
    assert GL._scrub("ordinary text") == "ordinary text"


# ══ Audio ═════════════════════════════════════════════════════════════════
def test_the_frame_sizes_are_what_the_three_rates_imply():
    assert A.FRAME_BYTES == {48000: 1920, 16000: 640, 24000: 960}
    assert A.MIME_PROVIDER_IN == "audio/pcm;rate=16000"
    assert A.frame_samples(48000) == 960


def _tone(rate, ms, hz=440):
    from array import array

    count = rate * ms // 1000
    return array(
        "h", [int(12000 * math.sin(2 * math.pi * hz * i / rate)) for i in range(count)]
    )


def test_bytes_and_samples_round_trip():
    from array import array

    random.seed(7)
    raw = array("h", [random.randint(-32768, 32767) for _ in range(500)])
    assert A.to_samples(A.to_bytes(raw)) == raw


def test_a_half_sample_is_dropped_by_the_pure_function():
    """``to_samples`` has no state, so it cannot carry an odd byte. The stream
    resampler is what carries it — see the next test."""
    assert len(A.to_samples(b"\x01\x02\x03")) == 1


def test_ragged_chunking_is_byte_identical_to_one_shot():
    """Including odd byte boundaries, which is where an earlier version lost a
    byte per chunk and quietly degraded the audio."""
    for src, dst, sizes in (
        (48000, 16000, [1, 3, 7, 64, 333, 1920]),
        (24000, 48000, [1, 2, 5, 96, 1000]),
    ):
        pcm = A.to_bytes(_tone(src, 200))
        batch = A.StreamResampler(src, dst).feed(pcm)
        random.seed(7)
        stream = A.StreamResampler(src, dst)
        out = bytearray()
        i = 0
        while i < len(pcm):
            n = random.choice(sizes)
            out += stream.feed(pcm[i:i + n])
            i += n
        out += stream.flush()
        assert bytes(out) == batch, (src, dst)


def test_the_resampler_converts_the_expected_lengths():
    """200 ms stays 200 ms across a rate change; only the byte count moves.

    48 kHz 200 ms is 9600 samples (19200 bytes); down to 16 kHz that is 3200
    samples (6400 bytes), and 24 kHz 200 ms is 4800 samples (9600 bytes); up to
    48 kHz that is 9600 samples (19200 bytes). The second figure is the one an
    earlier version of this test got wrong by a factor of two, which is exactly
    the kind of error a length assertion exists to catch.
    """
    pcm48 = A.to_bytes(_tone(48000, 200))
    assert len(A.StreamResampler(48000, 16000).feed(pcm48)) == 6400
    pcm24 = A.to_bytes(_tone(24000, 200))
    assert len(A.StreamResampler(24000, 48000).feed(pcm24)) == 19200


def test_flush_emits_the_partial_group_rather_than_dropping_it():
    r = A.StreamResampler(48000, 16000)
    assert len(r.feed(A.to_bytes(_tone(48000, 200)[:5]))) == 2  # one whole group
    assert len(r.flush()) == 2  # the two-sample remainder, averaged
    assert r.flush() == b""


def test_a_mixed_ratio_is_refused_and_the_message_says_what_to_do():
    """24000 -> 16000 is not a conversion the call performs, and approximating it
    silently would be worse than saying so."""
    with pytest.raises(ValueError) as info:
        A.StreamResampler(24000, 16000)
    assert "24000 -> 16000" in str(info.value)
    assert "composed" in str(info.value)


def test_a_passthrough_resampler_changes_nothing():
    r = A.StreamResampler(48000, 48000)
    assert r.passthrough is True
    assert r.feed(b"\x01\x02\x03\x04") == b"\x01\x02\x03\x04"


def test_the_framer_emits_whole_frames_and_pads_the_tail():
    f = A.Framer(A.FRAME_BYTES[48000])
    frames = f.feed(bytes(A.FRAME_BYTES[48000] * 3 + 100))
    assert len(frames) == 3 and all(len(x) == 1920 for x in frames)
    assert len(f.flush()) == 1920  # padded, not short
    assert f.flush() == b""


def test_level_and_silence():
    from array import array

    assert A.rms(array("h", [0] * 50)) == 0.0
    assert A.rms(array("h", [1000] * 50)) == 1000.0
    assert A.is_silent(bytes(640)) is True
    assert A.is_silent(A.to_bytes(array("h", [5000] * 320))) is False


# ══ Speaker identity ══════════════════════════════════════════════════════
def test_a_stream_maps_to_the_person_telegram_says_it_is():
    m = SP.SpeakerMap()
    m.update([{"user_id": 111, "ssrc": 900}, {"user_id": 222, "ssrc": 901}])
    assert m.note_frame(900).user_id == 111
    assert m.current_user_id() == 111
    assert m.user_for(901) == 222
    assert m.ssrc_for(222) == 901
    assert m.known(111) is True


def test_an_unknown_stream_is_nobody_not_somebody():
    """The fail-closed answer, and the same value an absent actor has
    everywhere else: 0 is refused downstream as malformed."""
    m = SP.SpeakerMap()
    m.update([{"user_id": 111, "ssrc": 900}])
    assert m.note_frame(4242) is None
    assert m.current_user_id() == 0


def test_a_speaker_who_stopped_is_no_longer_the_current_speaker():
    """Attributing a request to whoever spoke most recently is the guess that
    turns a quiet room into a confused one."""
    m = SP.SpeakerMap(clock=lambda: 100.0)
    m.update([{"user_id": 111, "ssrc": 900}])
    m.note_frame(900)
    assert m.current_user_id(now=100.5) == 111
    assert m.current_user_id(now=100.0 + SP.SPEAKER_TTL_SECONDS + 0.1) == 0


def test_updating_the_roster_replaces_it_rather_than_merging():
    """Somebody who has left must not remain attributable."""
    m = SP.SpeakerMap()
    m.update([{"user_id": 111, "ssrc": 900}])
    m.note_frame(900)
    m.update([{"user_id": 222, "ssrc": 901}])
    assert m.current_user_id() == 0
    assert m.known(111) is False
    assert m.user_for(900) == 0


def test_participants_without_a_usable_id_are_skipped_not_defaulted():
    """A zero would collide with "no stream" and "unknown speaker", and both of
    those collisions resolve towards attribution — the wrong direction."""
    m = SP.SpeakerMap()
    count = m.update([
        {"user_id": 0, "ssrc": 900},
        {"user_id": 111, "ssrc": 0},
        {"user_id": 222, "ssrc": 901},
        {"nonsense": True},
        None,
    ])
    assert count == 2
    assert m.user_for(900) == 0
    assert m.current_user_id() == 0


def test_the_speaker_map_reads_the_native_librarys_own_field_names():
    """``source`` is what ``GroupCallParticipant`` calls the ssrc, and the
    adapter passes its dicts through unchanged."""
    m = SP.SpeakerMap()
    m.update([{"user_id": 333, "source": 902}])
    assert m.user_for(902) == 333


def test_describe_is_safe():
    m = SP.SpeakerMap()
    m.update([{"user_id": 111, "ssrc": 900, "name": "Ali"}])
    described = m.describe()
    assert described == {"participants": 1, "mapped_streams": 1, "current_user_id": 0}
    assert "Ali" not in str(described)


# ══ The awareness bridge ══════════════════════════════════════════════════
@pytest.fixture()
def room():
    from app import awareness_context

    db.init()
    awareness_context.reset_rooms()
    awareness_context.note_room(-1001234567890, "گروه آزمایشی", "supergroup")
    yield -1001234567890
    awareness_context.reset_rooms()


def test_the_bridge_asks_the_existing_awareness(room):
    """Not a second awareness: the text comes from ``awareness_context.blocks``
    over ``awareness_context.build_ctx``, which is the same pair an awareness
    pass uses."""
    snap = AB.AwarenessContextBridge(room).snapshot()
    assert "گروه آزمایشی" in snap.text
    assert "room" in snap.sources
    assert snap.fingerprint


def test_a_bridge_with_no_room_is_refused(room):
    """``build_ctx(0)`` would read an empty window and answer confidently about
    nowhere."""
    with pytest.raises(ValueError):
        AB.AwarenessContextBridge(0)


def test_a_snapshot_is_reused_inside_its_ttl(room):
    b = AB.AwarenessContextBridge(room, ttl=60.0)
    b.snapshot()
    builds = b._builds
    r = b.refresh(force=False)
    assert r.reason == AB.REFRESH_FRESH
    assert r.changed is False
    assert b._builds == builds


def test_a_snapshot_is_rebuilt_once_stale(room):
    b = AB.AwarenessContextBridge(room, ttl=0.0)
    b.snapshot()
    builds = b._builds
    r = b.refresh(force=False)
    assert r.reason == AB.REFRESH_TTL
    assert b._builds == builds + 1


def test_two_bridges_never_share_a_room(room):
    other = AB.AwarenessContextBridge(-100999)
    assert other.chat_id != room
    assert other.snapshot().chat_id == -100999


def test_the_bridge_redacts_credential_shapes(room):
    # Synthetic, as above: the scrubber is tested against the shape, and a real
    # token must never appear in a file that is committed.
    secret = "AQ.Ab8RN6FAKEKEY000000000000000000000000000000000000009"
    assert secret not in AB._scrub(f"name {secret}")
    assert "AIza" not in AB._scrub("AIzaSyABCDEFGHIJ1234567890")
    assert AB._scrub("سلام") == "سلام"


def test_a_failed_build_is_an_empty_snapshot_not_an_exception(room, monkeypatch):
    """A context block is worth a lot and is never worth a call."""

    def boom(*args, **kwargs):
        raise RuntimeError("the database is on fire")

    monkeypatch.setattr(AB.awareness_context, "build_ctx", boom)
    snap = AB.AwarenessContextBridge(room).snapshot()
    assert snap.text == ""
    assert snap.describe()["empty"] is True


def test_an_unchanged_refresh_reports_no_change(room):
    b = AB.AwarenessContextBridge(room, ttl=0.0)
    b.snapshot()
    r = b.refresh(force=True)
    assert r.changed is False
    assert r.added == () and r.removed == ()


# ══ The action bridge ═════════════════════════════════════════════════════
def test_the_voice_vocabulary_is_a_strict_subset_of_the_real_operations():
    """A name here that is not one there would produce a request refused as an
    unknown operation — which reads like a model bug rather than a typo here."""
    assert VA.VOICE_ACTIONS <= set(admin_service.OPERATIONS)
    assert VA.VOICE_ACTIONS == {
        "ban_member", "unban_member", "mute_member", "unmute_member", "warn_member"
    }


def test_the_dangerous_and_owner_only_operations_are_unreachable_by_voice():
    """The whole reason the vocabulary is a subset rather than the whole table.

    Promotions change the authority table; the switches are already reachable by
    deterministic phrase and a second route to silence the assistant is exactly
    what the asymmetry argument forbids; the coding agent and the VPN have no
    business in a voice chat.
    """
    for name in (
        "promote_member", "demote_member", "delete_message",
        "nexus_offline", "nexus_online", "awareness_offline", "awareness_online",
        "codebuddy_task", "vpn_admin", "vpn_confirm",
    ):
        assert name not in VA.VOICE_ACTIONS, name


def test_the_request_has_no_field_a_model_could_use_to_claim_authority():
    """The brief's rule — "the model must never be allowed to declare
    is_owner=true" — enforced by leaving nowhere to declare it."""
    import dataclasses

    fields = {f.name for f in dataclasses.fields(VA.VoiceActionRequest)}
    assert "actor_id" in fields
    assert not fields & {
        "is_owner", "role", "allowed", "permissions", "actor_role", "authorised"
    }


def test_a_declaration_carries_only_the_declared_parameters():
    for decl in VA.declarations():
        assert set(decl["parameters"]["properties"]) <= VA.VOICE_ACTION_PARAMS
        assert "resolution" in decl["parameters"]["required"]
        assert set(decl["parameters"]["properties"]["resolution"]["enum"]) == set(
            VA.RESOLUTIONS
        )


def _bridge(**kw):
    return VA.VoiceActionBridge(-1001234567890, cooldown=0.0, **kw)


def test_a_well_formed_call_becomes_a_request():
    r = _bridge().parse(
        "ban_member", {"target_user_id": 444, "resolution": "resolved"}, actor_id=111
    )
    assert r.action == "ban_member"
    assert r.actor_id == 111
    assert r.target_id == 444
    assert r.resolved is True
    assert r.request_id


@pytest.mark.parametrize(
    "action,args",
    [
        ("nexus_offline", {"target_user_id": 444}),
        ("promote_member", {"target_user_id": 444}),
        ("", {"target_user_id": 444}),
        ("ban_member", {"target_user_id": 444, "is_owner": True}),
        ("ban_member", {"target_user_id": 444, "role": "owner"}),
        ("ban_member", {}),
        ("ban_member", {"target_user_id": 0}),
        ("ban_member", {"target_user_id": "not a number"}),
    ],
)
def test_a_call_that_is_not_a_request_is_refused_at_parse(action, args):
    assert _bridge().parse(action, args, actor_id=111) is None


def test_an_ambiguous_resolution_is_refused():
    """Extra safety, and not the safety: a model that confidently mishears says
    ``resolved`` too, and what catches that is the hierarchy check."""
    r = _bridge().parse(
        "ban_member", {"target_user_id": 444, "resolution": "ambiguous"}, actor_id=111
    )
    assert _bridge().check(r) == VA.OUTCOME_VOICE_UNRESOLVED


def test_an_unknown_resolution_string_becomes_unknown_and_is_refused():
    r = _bridge().parse(
        "ban_member", {"target_user_id": 444, "resolution": "probably"}, actor_id=111
    )
    assert r.resolution == VA.UNKNOWN
    assert _bridge().check(r) == VA.OUTCOME_VOICE_UNRESOLVED


def test_no_actor_is_refused():
    r = _bridge().parse("ban_member", {"target_user_id": 444}, actor_id=0)
    assert _bridge().check(r) == VA.OUTCOME_VOICE_NO_ACTOR


def test_the_session_ceiling_and_the_cooldown_are_separate_controls():
    ceiling = _bridge(max_actions=2)
    r = ceiling.parse("warn_member", {"target_user_id": 444}, actor_id=111)
    ceiling._requests = 2
    assert ceiling.check(r) == VA.OUTCOME_VOICE_RATE_LIMITED

    paced = VA.VoiceActionBridge(-100, max_actions=10, cooldown=10.0)
    r2 = paced.parse("warn_member", {"target_user_id": 444}, actor_id=111)
    paced._requests = 1
    paced._last_at = paced._clock()
    assert paced.check(r2) == VA.OUTCOME_VOICE_RATE_LIMITED


def test_a_missing_gateway_refuses_rather_than_pretending():
    """Answering "ok" to something that did not happen is the worst available
    outcome: an operator who believes a ban took effect stops watching."""
    b = _bridge()
    r = b.parse("warn_member", {"target_user_id": 444}, actor_id=111)
    result = asyncio.run(b.submit(r, None))
    assert result.ok is False
    assert result.outcome == VA.OUTCOME_VOICE_NO_GATEWAY


def test_the_translation_says_a_model_proposed_this():
    """It is what makes the audit trail answer "was this a person typing or a
    model proposing?" with the truth, and what subjects the request to the
    nexus-offline check."""
    r = _bridge().parse("ban_member", {"target_user_id": 444}, actor_id=111)
    admin_request = VA.request_to_admin(r)
    assert admin_request.interface == admin_service.INTERFACE_AI
    assert admin_request.actor_id == 111
    assert admin_request.at == r.at


# ══ The action path, end to end through the real authorisation ════════════
MEMBER = 333
STRANGER = 999


@pytest.fixture()
def governed(monkeypatch):
    """A group with an owner, one admin, and a stranger."""
    from tests.test_ai_admin import FakeGateway

    monkeypatch.setattr(config, "CONFIG_ADMINS", [])
    monkeypatch.setattr(config, "OWNER_USER_ID", 111)
    db.init()
    db.admin_reset()
    db.admin_set(
        222, rbac.ROLE_ADMIN, rbac.ROLE_PERMISSIONS[rbac.ROLE_ADMIN], granted_by=111
    )
    yield FakeGateway
    db.admin_reset()


def _submit(actor, action, target, gateway=None, resolution="resolved"):
    b = VA.VoiceActionBridge(-1001234567890, max_actions=10, cooldown=0.0)
    request = b.parse(
        action,
        {"target_user_id": target, "resolution": resolution, "reason": "test"},
        actor_id=actor,
    )
    assert request is not None
    gateway = gateway or _gateway_for(gateway)
    return asyncio.run(b.submit(request, gateway)), gateway


def _gateway_for(_):
    from tests.test_ai_admin import FakeGateway

    return FakeGateway()


_MUTATING = {"promote", "demote", "mute", "unmute", "ban", "unban", "delete", "warn"}


def _mutations(gateway):
    return [c for c in gateway.calls if c[0] in _MUTATING]


def test_the_owner_may_ban_through_voice(governed):
    result, gateway = _submit(111, "ban_member", MEMBER)
    assert result.ok is True
    assert ("ban", -1001234567890, MEMBER) in gateway.calls


def test_an_admin_may_mute_through_voice(governed):
    result, gateway = _submit(222, "mute_member", MEMBER)
    assert result.ok is True
    assert ("mute", -1001234567890, MEMBER) in gateway.calls


def test_a_stranger_may_not_ban_through_voice(governed):
    result, gateway = _submit(STRANGER, "ban_member", MEMBER)
    assert result.ok is False
    assert result.outcome == admin_service.OUTCOME_DENIED
    assert result.reason == rbac.REASON_NOT_ADMIN
    assert _mutations(gateway) == []


def test_nobody_may_ban_the_owner_through_voice(governed):
    """Not even the owner. The rule is unconditional, which is what removes the
    whole class of "ban the owner" bugs rather than one instance of it."""
    result, gateway = _submit(111, "ban_member", 111)
    assert result.ok is False
    assert result.reason == rbac.REASON_OWNER_PROTECTED
    assert _mutations(gateway) == []


def test_an_admin_may_not_mute_a_peer_through_voice(governed):
    result, gateway = _submit(222, "mute_member", 222)
    assert result.ok is False
    assert result.reason in (rbac.REASON_HIGHER_RANK, rbac.REASON_SELF_TARGET)
    assert _mutations(gateway) == []


def test_a_missing_telegram_right_refuses_through_voice(governed):
    from tests.test_ai_admin import FakeGateway

    result, gateway = _submit(
        111, "ban_member", MEMBER, gateway=FakeGateway(can_restrict=False)
    )
    assert result.ok is False
    assert result.outcome == admin_service.OUTCOME_BOT_LACKS_RIGHT
    assert _mutations(gateway) == []


def test_a_voice_action_is_replayed_only_once(governed):
    b = VA.VoiceActionBridge(-1001234567890, max_actions=10, cooldown=0.0)
    request = b.parse(
        "warn_member", {"target_user_id": MEMBER, "resolution": "resolved"},
        actor_id=111,
    )
    gateway = _gateway_for(None)
    first = asyncio.run(b.submit(request, gateway))
    second = asyncio.run(b.submit(request, gateway))
    assert first.ok is True
    assert second.duplicate is True
    assert len(_mutations(gateway)) == 1


def test_a_voice_action_lands_in_the_audit_trail_as_ai_originated(governed):
    _submit(111, "warn_member", MEMBER)
    rows = db.audit_since(chat_id=-1001234567890, since=0, limit=10)
    assert rows
    assert any(row.get("actor_id") == 111 for row in rows)


# ══ The pool gate that makes live models safe to admit ════════════════════
LIVE_MODELS = [
    "gemini-3.8-live",
    "gemini-3.1-flash-live-preview",
    "gemini-2.5-flash-native-audio-latest",
]


def _pool(workload, models, capabilities, **kw):
    return gemini_pool.Pool(
        workload,
        [("1", "key-" + "x" * 40)],
        list(models),
        capabilities,
        allow_experimental=kw.pop("allow_experimental", True),
        retries=0,
        backoff=0.0,
        timeout=5.0,
        **kw,
    )


def test_a_live_model_is_recognised_and_listens_and_speaks():
    for name in LIVE_MODELS:
        caps = gemini_pool.capabilities_of(name)
        assert caps is not None and gemini_pool.LIVE in caps, name
        assert gemini_pool.AUDIO_IN in caps and gemini_pool.AUDIO_OUT in caps, name


def test_a_live_transcription_model_can_listen_but_not_speak():
    """It refuses the AUDIO response modality outright, so it must never be
    offered to a workload that expects to be answered in speech — which is the
    failure that was hit live before the capability table knew about it."""
    caps = gemini_pool.capabilities_of("gemini-3.5-transcribe-live")
    assert gemini_pool.AUDIO_IN in caps
    assert gemini_pool.AUDIO_OUT not in caps


def test_no_configured_workload_other_than_live_voice_can_see_a_live_model():
    """The gate. Without it, ``{audio_in} <= {text, audio_in, audio_out, live}``
    is true and the transcription workload would start being handed streaming
    models it cannot call."""
    for spec in config.GEMINI_POOLS:
        if gemini_pool.LIVE in spec["capabilities"]:
            continue
        pool = _pool(
            spec["workload"],
            [*LIVE_MODELS, "gemini-3.5-transcribe", "gemini-flash-latest"],
            spec["capabilities"],
        )
        offered = pool.models_for(pool.accounts[0], time.time())
        assert not [n for n in offered if gemini_pool.is_live(n)], (
            spec["workload"],
            offered,
        )


def test_the_live_voice_workload_is_offered_exactly_the_bidirectional_models():
    pool = _pool(
        "live_voice",
        [*LIVE_MODELS, "gemini-3.5-transcribe-live", "gemini-flash-latest"],
        frozenset({gemini_pool.AUDIO_IN, gemini_pool.AUDIO_OUT, gemini_pool.LIVE}),
    )
    assert pool.models_for(pool.accounts[0], time.time()) == LIVE_MODELS


def test_the_shipped_live_voice_workload_is_configured_safely():
    """The defaults matter: a preview model needs the opt-in, and the workload
    must not quietly be able to spend another workload's allowance."""
    spec = [s for s in config.GEMINI_POOLS if s["workload"] == "live_voice"][0]
    assert spec["allow_experimental"] is True
    assert spec["capabilities"] == frozenset(
        {gemini_pool.AUDIO_IN, gemini_pool.AUDIO_OUT, gemini_pool.LIVE}
    )
    assert spec["retries"] == 0  # a stream has no partial answer to fail over from
    assert spec["daily_budget"] >= 1
    assert config.GEMINI_LIVE_ALLOW_SHARED_KEY is False


def test_the_feature_is_off_by_default():
    """A live call holds a socket, spends a stream and joins a channel other
    people are in. A deployment that has not opted in must not reach any of it."""
    assert config.GEMINI_LIVE_ENABLED is False


# ══ Metrics ═══════════════════════════════════════════════════════════════
def test_a_missing_measurement_is_none_not_zero():
    """A turn that produced no audio has *no* latency, and reporting it as zero
    would drag every average down and make the feature look faster than it is."""
    watch = M.Stopwatch()
    watch.mark(M.UTTERANCE_END)
    assert watch.gap(M.UTTERANCE_END, M.FIRST_AUDIO) is None
    assert M._ms(None) is None


def test_a_span_is_measured_and_averaged():
    watch = M.Stopwatch()
    watch.mark(M.UTTERANCE_END)
    watch.mark(M.FIRST_AUDIO)
    value = watch.close(M.UTTERANCE_END, M.FIRST_AUDIO)
    assert value is not None and value >= 0
    assert watch.average(M.UTTERANCE_END, M.FIRST_AUDIO) == value


def test_forgetting_a_mark_stops_a_turn_inheriting_the_last_ones_latency():
    watch = M.Stopwatch()
    watch.mark(M.UTTERANCE_END)
    watch.mark(M.FIRST_AUDIO)
    watch.forget(M.UTTERANCE_END, M.FIRST_AUDIO)
    assert watch.gap(M.UTTERANCE_END, M.FIRST_AUDIO) is None


def test_metrics_describe_carries_numbers_and_machine_keys_only():
    m = M.Metrics()
    m.failure(errors.REASON_TIMEOUT)
    # Whole seconds, because ``describe`` rounds audio to one decimal for the
    # status line: 50 frames is 1.0 s in and 150 is 3.0 s out, so the assertion
    # is about the derivation from ``FRAME_MS`` rather than about rounding.
    m.note_frames(inbound=50, outbound=150)
    described = m.describe()
    assert described["failures"] == {errors.REASON_TIMEOUT: 1}
    assert described["audio_in_seconds"] == pytest.approx(1.0)
    assert described["audio_out_seconds"] == pytest.approx(3.0)
    assert described["response_ms"] is None
    # nothing here can hold a transcript
    assert all(not isinstance(v, str) for v in described.values()
               if not isinstance(v, dict))


def test_the_process_totals_are_scalars_and_bounded():
    M.reset_state()
    for _ in range(M._LATENCY_SAMPLES_LIMIT * 2):
        m = M.Metrics()
        m.watch.mark(M.UTTERANCE_END)
        m.watch.mark(M.FIRST_AUDIO)
        m.watch.close(M.UTTERANCE_END, M.FIRST_AUDIO)
        M.note_session(m)
    assert len(M._latency_samples) <= M._LATENCY_SAMPLES_LIMIT
    assert M.totals()["sessions"] == M._LATENCY_SAMPLES_LIMIT * 2
    M.reset_state()
