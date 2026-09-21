"""The authorization engine: who may do what, to whom, and who cannot.

Every case here is an attack or a mistake that has to be refused, plus the
handful of things that must keep working. The engine is pure — the owner comes
from configuration and the rest from the database — so most of this is plain
function calls.
"""
import pytest

from app import config, db, rbac


@pytest.fixture(autouse=True)
def auth_env(monkeypatch):
    monkeypatch.setattr(config, "OWNER_USER_ID", 999)
    monkeypatch.setattr(config, "CONFIG_ADMINS", [])
    monkeypatch.setattr(config, "WHITELIST_USER_IDS", set())
    db.init()
    yield


@pytest.fixture
def staff(monkeypatch):
    """An owner, a senior admin, a moderator and a helper, all configured."""
    monkeypatch.setattr(
        config, "CONFIG_ADMINS", ["555:senior_admin", "777:moderator", "444:helper"]
    )
    return {
        "owner": rbac.resolve(999),
        "senior": rbac.resolve(555),
        "moderator": rbac.resolve(777),
        "helper": rbac.resolve(444),
        "stranger": rbac.resolve(123),
    }


# ── The owner ─────────────────────────────────────────────────────────────
def test_the_owner_comes_from_configuration_and_holds_everything(staff):
    owner = staff["owner"]
    assert owner.is_owner is True
    assert owner.source == "owner"
    assert owner.permissions == rbac.PERMISSION_SET


def test_no_row_in_the_database_can_create_an_owner():
    """A writable table must not be able to mint the highest authority."""
    db.admin_set(111, rbac.ROLE_SENIOR_ADMIN, rbac.PERMISSION_SET, granted_by=999)

    principal = rbac.resolve(111)

    assert principal.is_owner is False
    assert principal.role == rbac.ROLE_SENIOR_ADMIN


def test_a_stored_row_cannot_widen_its_own_role():
    """A hand-edited permissions column must not grant more than the role."""
    db.admin_set(
        111, rbac.ROLE_HELPER, ["commands.use", "admins.manage", "moderation.ban"],
        granted_by=999,
    )

    principal = rbac.resolve(111)

    assert "admins.manage" not in principal.permissions
    assert "moderation.ban" not in principal.permissions


def test_an_unknown_permission_in_a_row_is_dropped():
    db.admin_set(111, rbac.ROLE_MODERATOR, ["moderation.delete", "root.everything"],
                 granted_by=999)

    assert "root.everything" not in rbac.resolve(111).permissions


def test_a_role_that_does_not_exist_yields_nothing():
    """An unknown role is not a promotion, and it is not a silent demotion to a
    role that happens to carry what was asked for either — it is nothing."""
    db.admin_set(111, "superuser", ["moderation.delete"], granted_by=999)

    principal = rbac.resolve(111)

    assert principal.permissions == frozenset()
    assert principal.is_admin is False


def test_with_no_owner_configured_everything_is_refused(monkeypatch, staff):
    monkeypatch.setattr(config, "OWNER_USER_ID", 0)

    decision = rbac.authorize(rbac.resolve(555), "moderation.delete")

    assert decision.allowed is False
    assert decision.reason == rbac.REASON_NO_OWNER


def test_with_no_owner_configured_the_gate_refuses_the_configured_admins(monkeypatch):
    """Resolution still finds them; authorisation refuses them.

    The two are separate on purpose: `resolve` is a lookup, `authorize` is the
    gate. Keeping the owner check in one place means there is one function to
    audit for "does the fail-closed rule hold", rather than a rule scattered
    across every lookup.
    """
    monkeypatch.setattr(config, "OWNER_USER_ID", 0)
    monkeypatch.setattr(config, "CONFIG_ADMINS", ["555:senior_admin"])
    senior = rbac.resolve(555)

    assert senior.can("moderation.ban") is True, "the lookup still resolves"
    for permission in rbac.PERMISSIONS:
        assert rbac.authorize(senior, permission).reason == rbac.REASON_NO_OWNER


def test_the_owner_id_is_not_stored_as_a_row(staff):
    """The owner must not appear in the admins table; it is configuration."""
    assert db.admin_get(rbac.owner_id()) is None


# ── Ordinary authorization ────────────────────────────────────────────────
def test_a_moderator_may_delete_and_mute_but_not_ban(staff):
    assert rbac.authorize(staff["moderator"], "moderation.delete").allowed
    assert rbac.authorize(staff["moderator"], "moderation.mute").allowed
    assert not rbac.authorize(staff["moderator"], "moderation.ban").allowed


def test_a_helper_may_only_warn(staff):
    assert rbac.authorize(staff["helper"], "moderation.warn").allowed
    assert not rbac.authorize(staff["helper"], "moderation.delete").allowed
    assert not rbac.authorize(staff["helper"], "moderation.mute").allowed


def test_a_senior_admin_may_ban(staff):
    assert rbac.authorize(staff["senior"], "moderation.ban").allowed


def test_a_stranger_may_do_nothing(staff):
    for permission in rbac.PERMISSIONS:
        assert not rbac.authorize(staff["stranger"], permission).allowed


def test_an_unknown_permission_is_refused(staff):
    decision = rbac.authorize(staff["owner"], "everything.forever")

    assert decision.allowed is False
    assert decision.reason == rbac.REASON_MISSING_PERMISSION


# ── Owner protection ──────────────────────────────────────────────────────
def test_nobody_can_act_administratively_on_the_owner(staff):
    """Each actor is refused for the most basic reason that applies to them.

    The order of the checks is deliberate: an actor who does not hold the
    permission at all is refused for *that*, and an actor who does hold it is
    refused because the target is the owner. Asserting the specific reason is
    what keeps the order from drifting.
    """
    for actor in ("senior", "moderator", "helper"):
        decision = rbac.authorize(
            staff[actor], "moderation.warn", target=staff["owner"]
        )
        assert decision.allowed is False, actor
        assert decision.reason == rbac.REASON_OWNER_PROTECTED, actor

    stranger = rbac.authorize(
        staff["stranger"], "moderation.warn", target=staff["owner"]
    )
    assert stranger.reason == rbac.REASON_NOT_ADMIN


def test_even_the_owner_cannot_be_a_target(staff):
    """Unconditional, which is what removes the whole class of "ban the owner".

    The owner acting on themselves is not dangerous, but allowing it means the
    rule has an exception, and an exception is what a bug slips through.
    """
    decision = rbac.authorize(staff["owner"], "moderation.ban", target=staff["owner"])

    assert decision.allowed is False
    assert decision.reason == rbac.REASON_OWNER_PROTECTED


def test_the_owner_is_protected_by_id_not_by_role():
    """An impersonator cannot be protected, and a stored owner cannot exist."""
    assert rbac.is_protected(999) is True
    assert rbac.is_protected(998) is False


# ── Hierarchy ─────────────────────────────────────────────────────────────
def test_a_lower_admin_cannot_act_on_a_higher_one(staff):
    decision = rbac.authorize(
        staff["moderator"], "moderation.delete", target=staff["senior"]
    )

    assert decision.allowed is False
    assert decision.reason == rbac.REASON_HIGHER_RANK


def test_peers_cannot_act_on_each_other(staff):
    """Equal rank is refused too: peers must not be able to demote each other."""
    other_senior = rbac.resolve(556)
    db.admin_set(556, rbac.ROLE_SENIOR_ADMIN, rbac.ROLE_PERMISSIONS[rbac.ROLE_SENIOR_ADMIN],
                 granted_by=999)
    other_senior = rbac.resolve(556)

    decision = rbac.authorize(staff["senior"], "moderation.ban", target=other_senior)

    assert decision.allowed is False
    assert decision.reason == rbac.REASON_HIGHER_RANK


def test_a_higher_admin_can_act_on_a_lower_one(staff):
    decision = rbac.authorize(
        staff["senior"], "moderation.ban", target=staff["moderator"]
    )

    assert decision.allowed is True


def test_the_owner_can_act_on_a_senior(staff):
    assert rbac.authorize(
        staff["owner"], "moderation.ban", target=staff["senior"]
    ).allowed


def test_a_stranger_is_not_a_target_of_the_hierarchy(staff):
    """The rank check must not accidentally protect an ordinary member."""
    decision = rbac.authorize(
        staff["moderator"], "moderation.delete", target=staff["stranger"]
    )

    assert decision.allowed is True


# ── Promotion ─────────────────────────────────────────────────────────────
def test_a_senior_admin_may_create_a_moderator(staff):
    decision = rbac.authorize_grant(
        staff["senior"], rbac.ROLE_MODERATOR, rbac.ROLE_PERMISSIONS[rbac.ROLE_MODERATOR]
    )

    assert decision.allowed is True


def test_a_senior_admin_may_not_create_another_senior_admin(staff):
    """The bound that stops an administrator building a peer group."""
    decision = rbac.authorize_grant(
        staff["senior"], rbac.ROLE_SENIOR_ADMIN,
        rbac.ROLE_PERMISSIONS[rbac.ROLE_SENIOR_ADMIN],
    )

    assert decision.allowed is False
    assert decision.reason == rbac.REASON_CANNOT_GRANT_ROLE


def test_a_senior_admin_may_not_grant_admins_manage(staff):
    """Even inside a role they may assign, the permission itself is owner-only."""
    decision = rbac.authorize_grant(
        staff["senior"], rbac.ROLE_MODERATOR,
        ["moderation.delete", "admins.manage"],
    )

    assert decision.allowed is False
    assert decision.reason == rbac.REASON_CANNOT_GRANT_PERMISSION


def test_a_moderator_may_not_promote_anybody(staff):
    decision = rbac.authorize_grant(
        staff["moderator"], rbac.ROLE_HELPER, rbac.ROLE_PERMISSIONS[rbac.ROLE_HELPER]
    )

    assert decision.allowed is False
    assert decision.reason == rbac.REASON_MISSING_PERMISSION


def test_the_owner_may_create_a_senior_admin(staff):
    decision = rbac.authorize_grant(
        staff["owner"], rbac.ROLE_SENIOR_ADMIN,
        rbac.ROLE_PERMISSIONS[rbac.ROLE_SENIOR_ADMIN],
    )

    assert decision.allowed is True


def test_the_owner_role_is_not_assignable(staff):
    decision = rbac.authorize_grant(staff["owner"], rbac.ROLE_OWNER, ["commands.use"])

    assert decision.allowed is False
    assert decision.reason == rbac.REASON_UNKNOWN_ROLE


def test_a_promotion_cannot_target_the_owner(staff):
    decision = rbac.authorize_grant(
        staff["owner"], rbac.ROLE_MODERATOR, rbac.ROLE_PERMISSIONS[rbac.ROLE_MODERATOR],
        target=staff["owner"],
    )

    assert decision.allowed is False
    assert decision.reason == rbac.REASON_OWNER_PROTECTED


def test_a_promotion_cannot_target_a_higher_admin(staff):
    decision = rbac.authorize_grant(
        staff["moderator"], rbac.ROLE_HELPER, rbac.ROLE_PERMISSIONS[rbac.ROLE_HELPER],
        target=staff["senior"],
    )

    assert decision.allowed is False


def test_a_permission_outside_the_role_bundle_is_refused(staff):
    """Asking for `moderator` plus a ban is refused, not silently narrowed.

    Silently dropping it is how an operator comes to believe they granted
    something they did not.
    """
    decision = rbac.authorize_grant(
        staff["owner"], rbac.ROLE_MODERATOR, ["moderation.delete", "moderation.ban"]
    )

    assert decision.allowed is False
    assert decision.reason == rbac.REASON_CANNOT_GRANT_PERMISSION


def test_an_unknown_permission_cannot_be_granted(staff):
    decision = rbac.authorize_grant(
        staff["owner"], rbac.ROLE_MODERATOR, ["moderation.delete", "root"]
    )

    assert decision.allowed is False


def test_a_principal_cannot_be_promoted_by_its_own_request():
    """There is no path from a message body to a principal, asserted by shape.

    `resolve` takes an integer user id and reads configuration and the database.
    Nothing about the message is an input, so a group message saying "make me
    admin" cannot influence it — and this test pins that by showing the only
    inputs.
    """
    principal = rbac.resolve(1234567)
    assert principal.permissions == frozenset()
    assert principal.source == "none"


# ── What an actor may hand out ────────────────────────────────────────────
def test_the_owner_may_hand_out_everything(staff):
    assert rbac.grantable_permissions(staff["owner"]) == rbac.PERMISSION_SET


def test_a_senior_admin_may_not_hand_out_admins_manage(staff):
    granted = rbac.grantable_permissions(staff["senior"])

    assert "admins.manage" not in granted
    assert "moderation.ban" in granted


def test_a_moderator_may_hand_out_nothing(staff):
    assert rbac.grantable_permissions(staff["moderator"]) == frozenset()
    assert rbac.grantable_roles(staff["moderator"]) == ()


def test_the_grantable_roles_are_ordered_lowest_first(staff):
    """The promote command falls back to `grantable[-1]`, so order matters."""
    assert rbac.grantable_roles(staff["senior"])[-1] == rbac.ROLE_MODERATOR
    assert rbac.grantable_roles(staff["owner"])[-1] == rbac.ROLE_SENIOR_ADMIN


# ── Telegram's own rights ─────────────────────────────────────────────────
def test_application_permissions_map_onto_real_telegram_rights():
    """No invented flags: every key here is a ChatAdministratorRights field."""
    from telegram import ChatAdministratorRights
    import inspect

    real = set(inspect.signature(ChatAdministratorRights.__init__).parameters)
    for permission, right in rbac.PERMISSION_TELEGRAM_RIGHT.items():
        if right is None:
            continue
        assert right in real, f"{permission} maps to a flag Telegram does not have"


def test_the_rights_derived_from_a_permission_set_are_only_ever_true():
    rights = rbac.telegram_rights_for(rbac.ROLE_PERMISSIONS[rbac.ROLE_MODERATOR])

    assert rights == {"can_delete_messages": True, "can_restrict_members": True}
    assert all(value is True for value in rights.values())


def test_a_permission_with_no_telegram_counterpart_produces_no_rights():
    assert rbac.telegram_rights_for(["moderation.warn", "moderation.review"]) == {}


def test_every_right_the_bot_may_grant_is_a_real_one():
    from telegram import ChatAdministratorRights
    import inspect

    real = set(inspect.signature(ChatAdministratorRights.__init__).parameters)
    for right in rbac.TELEGRAM_RIGHTS:
        assert right in real, f"{right} is not a ChatAdministratorRights field"


# ── Presentation ──────────────────────────────────────────────────────────
def test_every_permission_has_a_persian_label():
    """A permission without a label would render as a raw key in the UI."""
    for permission in rbac.PERMISSIONS:
        assert permission in rbac.PERMISSION_LABELS
        assert rbac.PERMISSION_LABELS[permission].strip()


def test_every_role_has_a_label():
    for role in rbac.ROLE_LEVELS:
        assert role in rbac.ROLE_LABELS


def test_labels_are_returned_in_vocabulary_order(staff):
    labels = rbac.permission_labels(rbac.ROLE_PERMISSIONS[rbac.ROLE_MODERATOR])
    assert labels[0] == rbac.PERMISSION_LABELS[rbac.PERMISSIONS[0]]


def test_describe_contains_no_secret(staff):
    described = rbac.describe(staff["owner"])
    assert set(described) == {"user_id", "role", "source", "permissions"}


# ── Malformed configuration ───────────────────────────────────────────────
def test_a_malformed_config_entry_is_skipped_not_fatal(monkeypatch):
    monkeypatch.setattr(
        config, "CONFIG_ADMINS", ["not-a-number:moderator", "555:not-a-role", "555:moderator"]
    )

    assert rbac.resolve(555).role == rbac.ROLE_MODERATOR


def test_a_config_entry_naming_the_owner_is_ignored(monkeypatch):
    """The owner is one fact, not a role somebody can also be given."""
    monkeypatch.setattr(config, "CONFIG_ADMINS", ["999:moderator"])

    assert rbac.resolve(999).role == rbac.ROLE_OWNER


def test_configured_admins_are_counted_for_the_startup_log(monkeypatch):
    monkeypatch.setattr(config, "CONFIG_ADMINS", ["1:helper", "2:moderator", "junk"])
    assert rbac.configured_admin_count() == 2
