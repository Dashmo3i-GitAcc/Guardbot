"""Action-layer tests: delete success/failure, strikes, no-punishment rules."""
import asyncio

import pytest

from app.decision import Decision, DecisionResult
from app.moderation import enforce


def explicit_result():
    return DecisionResult(
        Decision.EXPLICIT,
        "ai_confirmed_explicit: explicit_sexual 0.95",
    )


def review_result():
    return DecisionResult(
        Decision.REVIEW,
        "ai_suggestive: 0.55",
    )


def run(coro):
    return asyncio.run(coro)


class DeleteSpy:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    async def __call__(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("Bad Request: message can't be deleted")


class StrikeSpy:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.calls


# 5. delete succeeds -> confirmed action recorded
def test_delete_success_records_confirmed_action():
    delete, strike = DeleteSpy(), StrikeSpy()
    outcome = run(enforce(explicit_result(), delete_media=delete, record_confirmed=strike))
    assert outcome.action == "deleted"
    assert outcome.deleted is True
    assert delete.calls == 1
    assert strike.calls == 1
    assert outcome.strike == 1


# 6. delete fails -> no strike, no ban
def test_delete_failure_applies_no_punishment():
    delete, strike = DeleteSpy(fail=True), StrikeSpy()
    outcome = run(enforce(explicit_result(), delete_media=delete, record_confirmed=strike))
    assert outcome.action == "delete_failed"
    assert outcome.deleted is False
    assert outcome.strike is None
    assert strike.calls == 0  # no strike was added
    assert delete.calls == 1


# 12. explicit adult media -> deleted
def test_explicit_media_is_deleted():
    delete = DeleteSpy()
    outcome = run(enforce(explicit_result(), delete_media=delete, record_confirmed=StrikeSpy()))
    assert outcome.decision is Decision.EXPLICIT
    assert outcome.deleted is True


# 13. ambiguous media -> not deleted, no strike
def test_review_is_allowed_and_never_deleted():
    delete, strike = DeleteSpy(), StrikeSpy()
    outcome = run(enforce(review_result(), delete_media=delete, record_confirmed=strike))
    assert outcome.action == "allowed"
    assert outcome.deleted is False
    assert delete.calls == 0
    assert strike.calls == 0


def test_safe_is_allowed_and_never_deleted():
    delete, strike = DeleteSpy(), StrikeSpy()
    result = DecisionResult(Decision.SAFE, "no explicit evidence")
    outcome = run(enforce(result, delete_media=delete, record_confirmed=strike))
    assert outcome.action == "allowed"
    assert delete.calls == 0
    assert strike.calls == 0


def test_recording_failure_does_not_undo_successful_delete():
    delete = DeleteSpy()

    def broken_record():
        raise RuntimeError("db down")

    outcome = run(enforce(explicit_result(), delete_media=delete, record_confirmed=broken_record))
    assert outcome.deleted is True
    assert outcome.action == "deleted"
    assert outcome.strike is None
