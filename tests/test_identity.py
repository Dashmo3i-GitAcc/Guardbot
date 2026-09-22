"""Identity: the opaque handle, and turning a reference into one person.

Two properties are asserted here and both are security properties, not
conveniences:

* the internal uuid is **stable and opaque** — minted once, never derived from
  the Telegram id, and never a credential; and
* resolution is **exact and refuses to guess** — one match is an answer, two
  matches is a question, and nothing in the module ever picks the most likely
  candidate.

Nothing here talks to Telegram or to Google.
"""
import pytest

from app import config, db, identity, people, rbac

OWNER = 999
CHAT = -1001234567890
OTHER_CHAT = -1009999999999


@pytest.fixture(autouse=True)
def identity_env(monkeypatch):
    monkeypatch.setattr(config, "OWNER_USER_ID", OWNER)
    monkeypatch.setattr(config, "CONFIG_ADMINS", [])
    monkeypatch.setattr(config, "NEXUS_PEOPLE_ENABLED", True)
    monkeypatch.setattr(config, "NEXUS_PEOPLE_MAX_CANDIDATES", 5)
    db.init()
    db.people_reset()
    db.identity_reset()
    people.reset_state()
    yield
    db.people_reset()
    db.identity_reset()


def remember(user_id, first="", last="", username="", chat_id=CHAT):
    return people.remember(
        {"id": user_id, "first_name": first, "last_name": last, "username": username},
        chat_id,
    )


# ── The handle ────────────────────────────────────────────────────────────
def test_a_speaker_is_given_a_handle_when_they_are_recorded():
    remember(500, "میلاد", "رضایی", "milad")

    handle = identity.uuid_for(500)

    assert handle
    assert len(handle) == 32
    assert handle == handle.lower()


def test_the_handle_is_stable_across_calls_and_rooms():
    remember(500, "میلاد", chat_id=CHAT)
    first = identity.uuid_for(500)
    remember(500, "میلاد", chat_id=OTHER_CHAT)

    assert identity.uuid_for(500) == first, "the handle changed for the same person"


def test_two_people_get_different_handles():
    remember(500, "میلاد")
    remember(501, "رضا")

    assert identity.uuid_for(500) != identity.uuid_for(501)


def test_the_handle_is_not_derived_from_the_telegram_id():
    """A derived handle would be reversible, which defeats its purpose."""
    remember(500, "میلاد")
    handle = identity.uuid_for(500)

    assert "500" not in handle
    assert handle != f"{500:032d}"


def test_uuid_for_never_creates_a_row():
    assert identity.uuid_for(123456) == ""
    assert db.identity_count() == 0


def test_ensure_is_idempotent_under_a_second_call():
    first = identity.ensure(500)
    second = identity.ensure(500)
    assert first == second
    assert db.identity_count() == 1


# ── The view ──────────────────────────────────────────────────────────────
def test_the_view_carries_the_facts_the_agent_needs():
    remember(500, "میلاد", "رضایی", "milad")
    db.admin_set(500, rbac.ROLE_MODERATOR, ["commands.use", "moderation.mute"],
                 granted_by=OWNER)

    view = identity.describe(500, chat_id=CHAT)

    assert view["user_id"] == 500
    assert view["uuid"] == identity.uuid_for(500)
    assert view["name"] == "میلاد رضایی"
    assert view["username"] == "milad"
    assert view["role"] == rbac.ROLE_MODERATOR
    assert view["is_admin"] is True
    assert view["is_owner"] is False
    assert "moderation.mute" in view["permissions"]
    assert "میلاد" in view["aliases"]


def test_the_view_never_carries_a_secret():
    """The DTO is built field by field, so there is no column to leak."""
    remember(500, "میلاد")
    view = identity.describe(500)

    for forbidden in ("token", "api_key", "secret", "password", "fingerprint"):
        assert forbidden not in view


def test_the_owner_is_resolved_by_id_not_by_name():
    remember(OWNER, "someone")
    view = identity.describe(OWNER)
    assert view["is_owner"] is True
    assert view["role"] == rbac.ROLE_OWNER


def test_a_claim_in_a_name_does_not_make_somebody_the_owner():
    remember(500, "مالک", "ربات")
    view = identity.describe(500)
    assert view["is_owner"] is False


# ── Resolution ────────────────────────────────────────────────────────────
def test_a_name_resolves_to_one_person():
    remember(500, "میلاد", "رضایی", "milad")

    found = identity.resolve("میلاد", chat_id=CHAT)

    assert found["status"] == "ok"
    assert found["match"] == "name"
    assert found["identity"]["user_id"] == 500


def test_the_persian_letter_variants_are_the_same_name():
    remember(500, "ميلاد")  # Arabic yeh
    found = identity.resolve("میلاد", chat_id=CHAT)  # Persian yeh
    assert found["status"] == "ok"
    assert found["identity"]["user_id"] == 500


def test_a_username_resolves_with_or_without_the_at():
    remember(500, "میلاد", username="milad")

    assert identity.resolve("@milad", chat_id=CHAT)["identity"]["user_id"] == 500
    assert identity.resolve("milad", chat_id=CHAT)["identity"]["user_id"] == 500


def test_an_internal_uuid_resolves_back_to_the_person():
    remember(500, "میلاد")
    handle = identity.uuid_for(500)

    found = identity.resolve(handle, chat_id=CHAT)

    assert found["status"] == "ok"
    assert found["match"] == "uuid"
    assert found["identity"]["user_id"] == 500


def test_a_numeric_id_resolves_and_is_marked_unseen_when_it_is():
    # A realistic Telegram id — at least four digits, so it is unambiguously an
    # id rather than a three-character word.
    remember(5001234567, "میلاد")

    seen = identity.resolve("5001234567", chat_id=CHAT)
    unseen = identity.resolve("123456789", chat_id=CHAT)

    assert seen["status"] == "ok" and seen["seen_before"] is True
    assert unseen["status"] == "ok" and unseen["seen_before"] is False
    assert unseen["identity"]["user_id"] == 123456789


def test_an_unknown_name_is_unknown_not_a_guess():
    found = identity.resolve("هیچکس", chat_id=CHAT)
    assert found["status"] == "unknown"


def test_two_people_with_one_name_is_ambiguous_with_candidates():
    """The single most dangerous thing this module could do is pick one."""
    remember(500, "میلاد", "رضایی")
    remember(501, "میلاد", "احمدی")

    found = identity.resolve("میلاد", chat_id=CHAT)

    assert found["status"] == "ambiguous"
    assert found["count"] == 2
    assert {c["user_id"] for c in found["candidates"]} == {500, 501}
    assert "identity" not in found, "an ambiguous answer must not name a person"


def test_a_full_name_disambiguates_two_people_sharing_a_first_name():
    remember(500, "میلاد", "رضایی")
    remember(501, "میلاد", "احمدی")

    found = identity.resolve("میلاد رضایی", chat_id=CHAT)

    assert found["status"] == "ok"
    assert found["identity"]["user_id"] == 500


def test_an_empty_query_is_invalid():
    assert identity.resolve("", chat_id=CHAT)["status"] == "invalid"
    assert identity.resolve("   ", chat_id=CHAT)["status"] == "invalid"


def test_a_two_character_query_is_not_a_name():
    remember(500, "علی")
    # «علی» is three characters and resolves; a two-character query does not.
    assert identity.resolve("می", chat_id=CHAT)["status"] == "unknown"


def test_an_unknown_uuid_is_unknown():
    found = identity.resolve("0" * 32, chat_id=CHAT)
    assert found["status"] == "unknown"
    assert found["match"] == "uuid"


def test_resolution_grants_nothing():
    """A resolved identity is a target, never an authorisation."""
    remember(500, "میلاد")
    found = identity.resolve("میلاد", chat_id=CHAT)

    assert "permissions" in found["identity"]
    # The permission list describes the *target*; it is not a grant to the
    # caller, and the caller's own authority is resolved separately.
    assert found["identity"]["role"] == rbac.ROLE_GUEST


# ── The counter ───────────────────────────────────────────────────────────
def test_every_resolution_is_counted_by_its_outcome():
    """The tally must be complete by construction, not by remembering to add
    a bump to each return."""
    # Realistic ten-digit ids: ``_ID_RE`` deliberately needs four digits or more
    # so that a short Persian word is never mistaken for one.
    remember(5001234567, "میلاد")
    remember(5001234568, "میلاد")  # a colliding name, so one query is ambiguous

    identity.resolve("میلاد", chat_id=CHAT)                    # ambiguous
    identity.resolve("5001234567", chat_id=CHAT)               # ok
    identity.resolve("", chat_id=CHAT)                         # invalid
    identity.resolve("0" * 32, chat_id=CHAT)                   # unknown uuid

    counts = db.identity_resolution_counts()
    assert counts.get("ambiguous") == 1
    assert counts.get("ok") == 1
    assert counts.get("invalid") == 1
    assert counts.get("unknown") == 1


def test_a_failing_counter_does_not_fail_the_lookup(monkeypatch):
    """A metric is never worth a wrong answer."""
    remember(500, "میلاد")

    def boom(outcome):
        raise RuntimeError("the counter is unavailable")

    monkeypatch.setattr(db, "identity_resolution_bump", boom)
    # The public wrapper must swallow it; the lookup still answers.
    found = identity.resolve("میلاد", chat_id=CHAT)
    assert found["status"] == "ok"


def test_the_counter_line_reports_counts_and_no_names():
    remember(500, "میلاد")
    identity.resolve("میلاد", chat_id=CHAT)

    line = identity.resolution_line()

    assert "identity lookups:" in line
    assert "ok=1" in line
    assert "میلاد" not in line, "a count line must not carry a name"


def test_the_counter_line_says_so_when_nothing_has_been_looked_up():
    assert "none yet" in identity.resolution_line()


def test_reset_clears_the_counter():
    remember(500, "میلاد")
    identity.resolve("میلاد", chat_id=CHAT)
    assert db.identity_resolution_counts()

    db.identity_reset()
    assert db.identity_resolution_counts() == {}
