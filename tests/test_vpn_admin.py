"""The VPN operational surface: authority, fail-closed, and the two-step write.

The claim this file exists to make testable is narrow and strong: **a language
model cannot move money in the VPN service.** It can ask for a balance change,
which records a row and produces a question, and it can ask for that row to be
confirmed, which is a reference rather than an approval. Everything the
execution actually needs is re-read from the record written the first time.

Four groups, and each one pins a different way that claim could be false:

* **Authority** — the six writes and the confirmation are owner-only, and an
  administrator who is refused never reaches the VPN bot at all.
* **Fail-closed** — an unconfigured, unreachable or switched-off VPN bot is
  recorded as a refusal in ``admin_audit`` and never reported as done.
* **The two-step** — a money operation is recorded and not executed; confirming
  is separately authorised; the stored payload is what runs.
* **The request boundary** — what a tool call can and cannot express.

Nothing here talks to the VPN bot, to Telegram, or to Google. The VPN client's
nine wrappers are replaced by a recorder, so "nothing reached the VPN bot" is an
assertion about a call list rather than about a message the bot printed — a
refusal printed while still calling the API is not a refusal.
"""
import asyncio
import time

import pytest

from app import admin_service, admin_tools, config, db, rbac, vpn_service, vpnbot

OWNER = 999
SENIOR = 555
MODERATOR = 777
MEMBER = 42
CHAT = -1001234567890
OTHER_CHAT = -1009999999999
BOT_ID = 1

CUSTOMER = 424242
SERVICE_ID = 71
PLAN_ID = 12
TRANSACTION_ID = 88


# ── The environment ───────────────────────────────────────────────────────
# The URL and the secret are set, because several tests are about what happens
# once the integration *is* wired — the pre-flight that refuses an unconfigured
# one runs before authorisation, so leaving the URL empty would make an
# authority test fail for the wrong reason.
#
# Every wrapper is replaced before any test runs, including the ones that never
# mention the double. That is not belt-and-braces, it is the only thing keeping
# this file off the network: a live VPN bot is reachable at this address on the
# deployment host, so an unmocked call would be a real signed request to a real
# service. The signature would not match, so nothing would change — but a test
# suite that talks to production is a test suite that can.
@pytest.fixture(autouse=True)
def vpn_env(monkeypatch):
    monkeypatch.setattr(config, "OWNER_USER_ID", OWNER)
    monkeypatch.setattr(
        config, "CONFIG_ADMINS", [f"{SENIOR}:senior_admin", f"{MODERATOR}:moderator"]
    )
    monkeypatch.setattr(config, "ADMIN_AI_ENABLED", True)
    monkeypatch.setattr(config, "ADMIN_PYTHON_ENABLED", True)
    monkeypatch.setattr(config, "ADMIN_REQUEST_REPLAY_WINDOW", 120)
    monkeypatch.setattr(config, "VPN_CONFIRMATION_TTL_SECONDS", 900)
    monkeypatch.setattr(config, "VPNBOT_API_URL", "http://127.0.0.1:8099")
    monkeypatch.setattr(config, "VPNBOT_SHARED_SECRET", "shared-secret")
    for name in _WRAPPERS:
        monkeypatch.setattr(vpnbot, name, _bind(VpnBotDouble(), name))
    db.init()
    db.admin_reset()
    db.vpn_pending_reset()
    admin_service.prune_reset()
    yield
    db.admin_reset()
    db.vpn_pending_reset()


# ── The VPN bot double ────────────────────────────────────────────────────
# One recorder for all nine wrappers. The names are listed explicitly so that a
# wrapper added to ``app/vpnbot.py`` without a thought for this file shows up as
# a missing attribute rather than as a silently unmocked call to the network.
_WRAPPERS = (
    "set_service_enabled",
    "set_notifications_enabled",
    "set_plan_active",
    "adjust_balance",
    "reject_stale_orders",
    "set_transaction_status",
    "subscription_lookup",
    "service_status",
    "status",
)


class VpnBotDouble:
    """Records every call, and answers or fails on demand."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.answers: dict[str, dict] = {}
        self.errors: dict[str, vpnbot.VpnBotError] = {}

    def will_answer(self, name: str, payload: dict) -> None:
        self.answers[name] = dict(payload)

    def will_fail(self, name: str, code: str = vpnbot.ERR_UNREACHABLE) -> None:
        self.errors[name] = vpnbot.VpnBotError(code, "simulated")

    async def _record(self, name: str, *args, **kwargs) -> dict:
        self.calls.append((name, args, kwargs))
        if name in self.errors:
            raise self.errors[name]
        return dict(self.answers.get(name, {"ok": True, "code": "ok"}))

    def named(self, name: str) -> list[tuple]:
        """Every call to one wrapper, as ``(args, kwargs)``."""
        return [(args, kwargs) for call, args, kwargs in self.calls if call == name]

    def touched(self) -> bool:
        return bool(self.calls)


@pytest.fixture
def vpn(monkeypatch):
    double = VpnBotDouble()
    for name in _WRAPPERS:
        monkeypatch.setattr(vpnbot, name, _bind(double, name))
    return double


def _bind(double: VpnBotDouble, name: str):
    """A coroutine function forwarding to the double's recorder."""

    async def wrapper(*args, **kwargs):
        return await double._record(name, *args, **kwargs)

    wrapper.__name__ = name
    return wrapper


# ── Helpers ───────────────────────────────────────────────────────────────
class NoGateway:
    """A gateway the VPN path never calls, so any call to it is a failure."""

    def __getattr__(self, name):
        raise AssertionError(f"the VPN path called gateway.{name}")


def run(coro):
    return asyncio.run(coro)


def request(operation: str, **kwargs) -> admin_service.AdminRequest:
    fields = {
        "chat_id": CHAT,
        "actor_id": OWNER,
        "request_id": admin_service.new_request_id(),
        "interface": admin_service.INTERFACE_AI,
        "at": int(time.time()),
    }
    fields.update(kwargs)
    return admin_service.AdminRequest(operation=operation, **fields)


def execute(operation: str, **kwargs):
    return run(
        admin_service.execute(
            request(operation, **kwargs), NoGateway(), bot_id=BOT_ID
        )
    )


def ai_call(name: str, args: dict, *, actor_id: int = OWNER, chat_id: int = CHAT):
    """Parse one model tool call and run it, the way ``main.py``'s loop does."""
    parsed = admin_tools.parse_write_call(
        name,
        args,
        actor_id=actor_id,
        chat_id=chat_id,
        request_id=admin_service.new_request_id(),
    )
    if parsed is None:
        return None, None
    return run(admin_service.execute(parsed, NoGateway(), bot_id=BOT_ID)), parsed


def balance(**kwargs):
    fields = {
        "target_id": CUSTOMER,
        "amount": 50_000,
        "reason": "تسویه",
    }
    fields.update(kwargs)
    return execute("vpn_balance", **fields)


# ══ AUTHORITY ═════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "operation, fields",
    [
        ("vpn_service_enabled", {"service_id": SERVICE_ID, "enabled": False}),
        ("vpn_notifications", {"target_id": CUSTOMER, "enabled": True}),
        ("vpn_plan_active", {"plan_id": PLAN_ID, "enabled": False}),
        ("vpn_balance", {"target_id": CUSTOMER, "amount": 1_000, "reason": "x"}),
        ("vpn_orders_sweep", {"days": 7, "reason": "x"}),
        (
            "vpn_transaction_status",
            {"transaction_id": TRANSACTION_ID, "status": "refunded", "reason": "x"},
        ),
        ("vpn_confirm", {}),
    ],
)
def test_every_vpn_operation_is_refused_for_an_administrator(
    operation, fields, monkeypatch
):
    """Not "denied by a check somewhere" — inexpressible through the role table.

    ``vpn.manage`` is carried by no role bundle, so a senior administrator is
    refused by the same call that refuses a stranger, and the VPN bot is never
    reached. The second half of the assertion is the one that matters: a refusal
    that still called the service would not be a refusal.
    """
    double = VpnBotDouble()
    for name in _WRAPPERS:
        monkeypatch.setattr(vpnbot, name, _bind(double, name))

    result = execute(operation, actor_id=SENIOR, **fields)

    assert result.outcome == admin_service.OUTCOME_DENIED
    assert result.reason == rbac.REASON_MISSING_PERMISSION
    assert not double.touched(), "a refused request reached the VPN bot"


def test_the_owner_holds_both_vpn_permissions_and_no_role_bundle_does():
    owner = rbac.resolve(OWNER)
    assert owner.can("vpn.read") and owner.can("vpn.manage")
    for role, permissions in rbac.ROLE_PERMISSIONS.items():
        assert "vpn.read" not in permissions, role
        assert "vpn.manage" not in permissions, role


def test_only_the_owner_is_offered_the_vpn_tools():
    offered = set(admin_tools.tool_names_for(rbac.resolve(OWNER)))
    assert {
        "vpn_subscription_lookup",
        "vpn_service_status",
        "get_vpn_status",
        "vpn_admin",
        "confirm_vpn_operation",
    } <= offered

    for actor in (SENIOR, MODERATOR, MEMBER):
        names = set(admin_tools.tool_names_for(rbac.resolve(actor)))
        assert not {n for n in names if "vpn" in n}, actor


# ══ FAIL-CLOSED ═══════════════════════════════════════════════════════════
def test_an_unconfigured_vpn_bot_is_refused_before_anything_is_recorded(monkeypatch):
    """The pre-flight the ``OP_VPN`` kind exists for."""
    monkeypatch.setattr(config, "VPNBOT_API_URL", "")

    result = balance()

    assert result.outcome == admin_service.OUTCOME_VPN_UNAVAILABLE
    assert result.detail == "not_configured"
    assert result.ok is False
    assert db.vpn_pending_waiting() == [], "nothing may be recorded for approval"


def test_an_unreachable_vpn_bot_is_a_refusal_on_the_record(vpn):
    """The fail-closed test. Nothing ran, and the trail says so.

    Run against an operation that executes immediately, so the failure is the
    call itself rather than the approval step. The money operations' equivalent
    is ``test_a_transport_failure_at_confirm_time_puts_the_operation_back``.
    """
    vpn.will_fail("set_service_enabled", vpnbot.ERR_UNREACHABLE)

    result = execute("vpn_service_enabled", service_id=SERVICE_ID, enabled=False)

    assert result.outcome == admin_service.OUTCOME_VPN_UNAVAILABLE
    assert result.ok is False

    row = db.audit_recent(1)[0]
    assert row["action"] == "vpn.service.enabled"
    assert row["outcome"] == admin_service.OUTCOME_VPN_UNAVAILABLE
    assert row["actor_id"] == OWNER


def test_a_recorded_operation_that_cannot_be_reached_is_also_on_the_record(vpn):
    """The same property for the two-step path, which is the one that matters."""
    awaiting = balance()
    vpn.will_fail("adjust_balance", vpnbot.ERR_UNREACHABLE)

    result = confirm(awaiting.extra["pending"]["pending_id"])

    assert result.outcome == admin_service.OUTCOME_VPN_UNAVAILABLE
    assert result.ok is False
    assert db.audit_recent(1)[0]["action"] == "vpn.confirm"
    assert db.audit_recent(1)[0]["outcome"] == admin_service.OUTCOME_VPN_UNAVAILABLE


def test_the_vpn_bots_own_kill_switch_is_unavailable_not_refused(vpn):
    """``admin_disabled`` is a 503 from the other service, not a decision.

    The distinction decides what the owner does next: this is "look at the VPN
    bot's configuration", not "look at your request".
    """
    vpn.errors["set_service_enabled"] = vpnbot.VpnBotError(
        vpnbot.ERR_REFUSED, "admin_disabled", 503
    )

    result = execute("vpn_service_enabled", service_id=SERVICE_ID, enabled=False)

    assert result.outcome == admin_service.OUTCOME_VPN_UNAVAILABLE


def test_a_transport_failure_never_reports_success(vpn):
    vpn.will_fail("set_service_enabled", vpnbot.ERR_BAD_RESPONSE)

    result = execute("vpn_service_enabled", service_id=SERVICE_ID, enabled=False)

    assert result.ok is False
    assert result.outcome == admin_service.OUTCOME_VPN_ERROR


def test_an_unexpected_exception_is_reported_rather_than_raised(monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("the adapter broke")

    monkeypatch.setattr(vpnbot, "set_service_enabled", boom)

    result = execute("vpn_service_enabled", service_id=SERVICE_ID, enabled=False)

    assert result.outcome == admin_service.OUTCOME_VPN_ERROR
    assert result.ok is False


# ══ THE TWO-STEP WRITE ════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "operation, fields, wrapper",
    [
        (
            "vpn_balance",
            {"target_id": CUSTOMER, "amount": 50_000, "reason": "x"},
            "adjust_balance",
        ),
        ("vpn_orders_sweep", {"days": 7, "reason": "x"}, "reject_stale_orders"),
        (
            "vpn_transaction_status",
            {"transaction_id": TRANSACTION_ID, "status": "refunded", "reason": "x"},
            "set_transaction_status",
        ),
    ],
)
def test_a_money_operation_is_recorded_and_not_executed(operation, fields, wrapper, vpn):
    result = execute(operation, **fields)

    assert result.outcome == admin_service.OUTCOME_VPN_AWAITING_CONFIRMATION
    assert result.ok is False, "an unapproved operation is not a success"
    assert not vpn.named(wrapper), "the VPN bot was called before the owner approved"

    pending = db.vpn_pending_waiting()
    assert len(pending) == 1
    assert pending[0]["operation"] == operation
    assert pending[0]["actor_id"] == OWNER
    assert result.extra["pending"]["pending_id"] == pending[0]["request_id"]


@pytest.mark.parametrize(
    "operation, fields, wrapper",
    [
        (
            "vpn_service_enabled",
            {"service_id": SERVICE_ID, "enabled": False},
            "set_service_enabled",
        ),
        (
            "vpn_notifications",
            {"target_id": CUSTOMER, "enabled": True},
            "set_notifications_enabled",
        ),
        ("vpn_plan_active", {"plan_id": PLAN_ID, "enabled": False}, "set_plan_active"),
    ],
)
def test_a_reversible_operation_runs_immediately(operation, fields, wrapper, vpn):
    """The other three do not need a second step, and must not be given one.

    The owner already authorised them by asking; a confirmation on a service
    toggle would be ceremony that teaches the owner to approve without reading.
    """
    result = execute(operation, **fields)

    assert result.outcome == admin_service.OUTCOME_OK
    assert result.ok is True
    assert len(vpn.named(wrapper)) == 1
    assert db.vpn_pending_waiting() == []


def test_the_operator_recorded_by_the_vpn_bot_is_the_actor_the_gateway_approved(vpn):
    """The asserted operator is the authorised id, not anything a model sent."""
    execute("vpn_service_enabled", service_id=SERVICE_ID, enabled=False)

    (args, kwargs) = vpn.named("set_service_enabled")[0]
    assert args == (SERVICE_ID, False)
    assert kwargs["operator_id"] == OWNER
    assert kwargs["interface"] == admin_service.INTERFACE_AI


def test_the_vpn_bot_is_audited_under_the_operation_name(vpn):
    execute("vpn_plan_active", plan_id=PLAN_ID, enabled=False)

    row = db.audit_recent(1)[0]
    assert row["action"] == "vpn.plan.active"
    assert row["outcome"] == admin_service.OUTCOME_OK


def test_an_in_band_refusal_is_a_decision_and_keeps_its_code(vpn):
    """A 200 with ``ok: false`` is the VPN bot deciding, not failing."""
    vpn.will_answer("set_service_enabled", {"ok": False, "code": "not_found"})

    result = execute("vpn_service_enabled", service_id=SERVICE_ID, enabled=False)

    assert result.outcome == admin_service.OUTCOME_VPN_REFUSED
    assert result.detail == f"not_found service={SERVICE_ID}"
    assert result.ok is False


def test_the_audit_detail_names_the_object_that_was_changed(vpn):
    """Which service, plan or transaction — not just "ok".

    The ids go in the detail rather than in ``target_id``, because that column
    holds a Telegram id and a service id in it would be read as one.
    """
    execute("vpn_plan_active", plan_id=PLAN_ID, enabled=False)

    assert db.audit_recent(1)[0]["detail"] == f"ok plan={PLAN_ID}"


# ── The subject of a VPN operation is not an administrative target ────────
@pytest.mark.parametrize("subject", ["owner", "administrator", "stranger"])
def test_a_vpn_operation_whose_subject_is_privileged_is_still_allowed(subject, vpn):
    """A regression found by a live run, not by a test.

    ``AdminRequest.target_id`` means two different things depending on the
    operation: for a ban it is the person being acted *on*, and the hierarchy
    rules are about them; for a balance change it is the customer whose balance
    moves, and who they are has nothing to do with who may move it. Resolving
    the second into a principal made the owner-protection and hierarchy checks
    fire against the customer — so a balance change for the owner was refused
    as "the target is the owner", and one for an administrator as "the target is
    at your own level". Both are wrong, and both are refused here only if the
    fix regresses.
    """
    subject_id = {"owner": OWNER, "administrator": SENIOR, "stranger": MEMBER}[subject]

    result = balance(target_id=subject_id)

    assert result.outcome == admin_service.OUTCOME_VPN_AWAITING_CONFIRMATION
    assert result.ok is False
    assert len(db.vpn_pending_waiting()) == 1


def test_the_hierarchy_rules_still_apply_to_a_user_targeted_operation():
    """The other half of the fix: gating on ``OP_USER`` must not weaken bans.

    The owner is never a valid target of an administrative action, and that is
    still true after the target resolution was narrowed.
    """
    result = execute("ban_member", target_id=OWNER)

    assert result.outcome == admin_service.OUTCOME_DENIED
    assert result.reason == rbac.REASON_OWNER_PROTECTED


def test_a_senior_administrator_cannot_ban_a_peer():
    """And the hierarchy check still refuses a same-level target."""
    result = execute("ban_member", actor_id=SENIOR, target_id=SENIOR)

    assert result.outcome == admin_service.OUTCOME_DENIED
    assert result.reason == rbac.REASON_HIGHER_RANK


def test_a_refusal_carries_no_credential_from_the_service_it_names(vpn):
    vpn.will_answer(
        "set_service_enabled",
        {
            "ok": True,
            "code": "ok",
            "service": {
                "id": SERVICE_ID,
                "display_name": "کاربر",
                "sub_url": "https://panel.example/sub/abc123",
                "client_email": "71-uuid",
            },
        },
    )

    result = execute("vpn_service_enabled", service_id=SERVICE_ID, enabled=True)

    blob = str(result.extra)
    assert "sub_url" not in blob
    assert "client_email" not in blob
    assert "panel.example" not in blob
    assert result.extra["vpn"]["service"]["id"] == SERVICE_ID


# ══ CONFIRMING ════════════════════════════════════════════════════════════
def confirm(pending_id: str = "", **kwargs):
    return execute("vpn_confirm", pending_id=pending_id, **kwargs)


def test_confirming_a_recorded_operation_executes_it(vpn):
    awaiting = balance()
    pending_id = awaiting.extra["pending"]["pending_id"]

    result = confirm(pending_id)

    assert result.outcome == admin_service.OUTCOME_OK
    assert result.ok is True
    assert len(vpn.named("adjust_balance")) == 1
    assert db.vpn_pending_waiting() == []


def test_a_bare_confirmation_resolves_the_single_pending_operation(vpn):
    awaiting = balance()
    pending_id = awaiting.extra["pending"]["pending_id"]

    result = confirm()

    assert result.outcome == admin_service.OUTCOME_OK
    assert result.extra["confirmed"]["pending_id"] == pending_id


def test_confirming_with_two_pending_is_a_question_and_runs_nothing(vpn):
    """«اوکی» with two things waiting is not an approval of either."""
    first = balance()
    second = execute("vpn_orders_sweep", days=7, reason="پاکسازی")

    result = confirm()

    assert result.outcome == admin_service.OUTCOME_VPN_REFUSED
    assert result.detail == "ambiguous"
    assert set(result.extra["candidates"]) == {
        first.extra["pending"]["pending_id"],
        second.extra["pending"]["pending_id"],
    }
    assert not vpn.touched(), "an ambiguous confirmation executed something"


def test_confirming_a_name_that_is_not_waiting_is_refused(vpn):
    balance()

    result = confirm("does-not-exist")

    assert result.outcome == admin_service.OUTCOME_VPN_REFUSED
    assert result.detail.startswith("not_waiting")
    assert not vpn.touched()


def test_confirming_with_nothing_pending_is_refused(vpn):
    result = confirm()

    assert result.outcome == admin_service.OUTCOME_VPN_REFUSED
    assert result.detail == "nothing_pending"
    assert not vpn.touched()


def test_an_expired_operation_is_refused_and_never_runs(vpn):
    """Past its deadline, an operation is dropped rather than executed late.

    The row is still on disk, so "the deadline passed" is told apart from "you
    never asked" — the first tells the owner to ask again, the second reads as
    "you are mistaken", and only one of them is true.
    """
    awaiting = balance()
    pending_id = awaiting.extra["pending"]["pending_id"]
    row = db.vpn_pending_get(pending_id)

    # Rewrite the deadline into the past without touching anything else, which
    # is what the passage of time does.
    db.vpn_pending_reset()
    db.vpn_pending_add(
        pending_id,
        actor_id=row["actor_id"],
        chat_id=row["chat_id"],
        operation=row["operation"],
        subject=row["subject"],
        payload=row["payload"],
        expires_at=int(time.time()) - 1,
    )

    result = confirm(pending_id)

    assert result.outcome == admin_service.OUTCOME_VPN_REFUSED
    assert result.detail == "expired"
    assert result.message == config.VPN_CONFIRM_EXPIRED_TEXT
    assert not vpn.named("adjust_balance"), "an expired operation was executed"


def test_a_finished_operation_is_not_reported_as_expired(vpn):
    """A row that ran is not a row that timed out.

    Both are "not waiting", and confusing them would tell the owner their
    operation was dropped when in fact it went through.
    """
    awaiting = balance()
    pending_id = awaiting.extra["pending"]["pending_id"]
    confirm(pending_id)

    result = confirm(pending_id)

    assert result.outcome == admin_service.OUTCOME_VPN_REFUSED
    assert result.detail == "nothing_pending"


def test_an_operation_can_only_be_confirmed_once(vpn):
    awaiting = balance()
    pending_id = awaiting.extra["pending"]["pending_id"]

    first = confirm(pending_id)
    second = confirm(pending_id)

    assert first.outcome == admin_service.OUTCOME_OK
    assert second.outcome == admin_service.OUTCOME_VPN_REFUSED
    assert second.detail == "nothing_pending"
    assert len(vpn.named("adjust_balance")) == 1, "a confirmation ran twice"


def test_a_transport_failure_at_confirm_time_puts_the_operation_back(vpn):
    """Nothing was decided, so the approval is not spent."""
    awaiting = balance()
    pending_id = awaiting.extra["pending"]["pending_id"]
    vpn.will_fail("adjust_balance", vpnbot.ERR_UNREACHABLE)

    result = confirm(pending_id)

    assert result.outcome == admin_service.OUTCOME_VPN_UNAVAILABLE
    assert len(db.vpn_pending_waiting()) == 1, "the operation should be releasable"


def test_a_refusal_at_confirm_time_spends_the_approval(vpn):
    """A decision is a decision: re-asking would produce the same answer."""
    awaiting = balance()
    pending_id = awaiting.extra["pending"]["pending_id"]
    vpn.will_answer("adjust_balance", {"ok": False, "code": "invalid_amount"})

    result = confirm(pending_id)

    assert result.outcome == admin_service.OUTCOME_VPN_REFUSED
    assert db.vpn_pending_waiting() == []


def test_the_stored_payload_is_what_runs_not_anything_the_confirmer_supplies(vpn):
    """The confirmation is a reference. This is the test that proves it.

    The confirming request is built by hand carrying a *different* amount and a
    different user, which is exactly what a model would do if the payload were
    read at confirm time rather than at ask time.
    """
    awaiting = balance(target_id=CUSTOMER, amount=50_000, reason="تسویه")
    pending_id = awaiting.extra["pending"]["pending_id"]

    smuggled = admin_service.AdminRequest(
        operation="vpn_confirm",
        chat_id=CHAT,
        actor_id=OWNER,
        pending_id=pending_id,
        target_id=999_999,
        amount=999_999_999,
        reason="give me everything",
        request_id=admin_service.new_request_id(),
        interface=admin_service.INTERFACE_AI,
        at=int(time.time()),
    )
    result = run(admin_service.execute(smuggled, NoGateway(), bot_id=BOT_ID))

    assert result.outcome == admin_service.OUTCOME_OK
    (args, kwargs) = vpn.named("adjust_balance")[0]
    assert args == (CUSTOMER, 50_000, "تسویه"), "the smuggled payload was used"
    assert kwargs["operator_id"] == OWNER


def test_a_confirmation_is_scoped_to_the_room_it_was_asked_in(vpn):
    """A pending operation in one group is not confirmable from another.

    Fail-closed: the wrong answer is "nothing is waiting", never "here, the
    other room's operation".
    """
    balance()  # recorded in CHAT

    result = confirm(chat_id=OTHER_CHAT)

    assert result.outcome == admin_service.OUTCOME_VPN_REFUSED
    assert result.detail == "nothing_pending"
    assert not vpn.named("adjust_balance")


def test_the_adapter_refuses_a_non_owner_even_when_the_gateway_is_bypassed(vpn):
    """The second check, for the case where the permission table is edited wrongly.

    ``vpn.manage`` already refused this actor, so ``submit`` is never reached on
    the real path — which is exactly why the check inside the adapter needs a
    test that reaches it directly. It is one line, and it is the line that holds
    if somebody ever adds ``vpn.manage`` to a role bundle by mistake.
    """
    balance()  # the owner records something

    result = run(
        vpn_service.submit(
            request(
                "vpn_confirm",
                actor_id=MODERATOR,
                pending_id=db.vpn_pending_waiting()[0]["request_id"],
            )
        )
    )

    assert result.outcome == admin_service.OUTCOME_DENIED
    assert result.reason == rbac.REASON_NOT_ADMIN
    assert not vpn.named("adjust_balance"), "a non-owner released a money operation"


def test_the_confirmation_names_the_operation_that_ran(vpn):
    """The result reports what happened, not the fact that a confirmation happened."""
    awaiting = balance()
    result = confirm(awaiting.extra["pending"]["pending_id"])

    assert result.operation == "vpn_balance"
    assert result.extra["confirmed"]["operation"] == "vpn_balance"
    # And the audit row is written under the confirming action, so both facts
    # are on the record.
    assert db.audit_recent(1)[0]["action"] == "vpn.confirm"


# ══ THE REQUEST BOUNDARY ══════════════════════════════════════════════════
@pytest.mark.parametrize(
    "operation, fields, missing",
    [
        ("vpn_service_enabled", {"service_id": 0, "enabled": True}, "service_id"),
        ("vpn_service_enabled", {"service_id": 5}, "enabled"),
        ("vpn_notifications", {"enabled": True}, "telegram_id"),
        ("vpn_plan_active", {"plan_id": 0, "enabled": True}, "plan_id"),
        ("vpn_balance", {"target_id": CUSTOMER, "reason": "x"}, "amount"),
        ("vpn_balance", {"target_id": CUSTOMER, "amount": 10}, "reason"),
        ("vpn_orders_sweep", {"reason": "x"}, "days"),
        ("vpn_orders_sweep", {"days": 7}, "reason"),
        ("vpn_transaction_status", {"status": "refunded", "reason": "x"}, "transaction_id"),
        ("vpn_transaction_status", {"transaction_id": 8, "reason": "x"}, "status"),
    ],
)
def test_an_incomplete_operation_is_refused_by_naming_the_missing_field(
    operation, fields, missing, vpn
):
    """Refused rather than repaired. The name of the field is the detail."""
    result = execute(operation, **fields)

    assert result.outcome == admin_service.OUTCOME_MALFORMED
    assert result.detail == missing
    assert not vpn.touched(), "an incomplete request reached the VPN bot"


def test_a_string_is_not_read_as_a_boolean(vpn):
    """``bool("false")`` is ``True``, so a string is not coerced.

    A model that sends the word rather than the value has not expressed a state,
    and guessing one is the failure the argument check exists to prevent.
    """
    outcome, _parsed = ai_call(
        "vpn_admin",
        {"operation": "vpn_service_enabled", "service_id": SERVICE_ID, "enabled": "false"},
    )

    assert outcome.outcome == admin_service.OUTCOME_MALFORMED
    assert outcome.detail == "enabled"
    assert not vpn.touched()


def test_an_undeclared_argument_refuses_the_whole_call(vpn):
    """A model that invents ``actor_id`` has not described the call it thinks it did."""
    outcome, parsed = ai_call(
        "vpn_admin",
        {
            "operation": "vpn_balance",
            "telegram_id": CUSTOMER,
            "amount": 10,
            "reason": "x",
            "actor_id": OWNER,
        },
    )

    assert parsed is None
    assert outcome is None
    assert not vpn.touched()


def test_the_actor_and_the_room_come_from_the_caller_not_the_arguments(vpn):
    outcome, parsed = ai_call(
        "vpn_admin",
        {
            "operation": "vpn_balance",
            "telegram_id": CUSTOMER,
            "amount": 10,
            "reason": "x",
        },
        actor_id=OWNER,
        chat_id=CHAT,
    )

    assert parsed.actor_id == OWNER
    assert parsed.chat_id == CHAT
    assert parsed.target_id == CUSTOMER
    assert outcome.outcome == admin_service.OUTCOME_VPN_AWAITING_CONFIRMATION


def test_vpn_admin_cannot_be_used_to_confirm_its_own_request(vpn):
    """The two halves stay two calls, structurally.

    ``vpn_confirm`` is a separate tool, and naming it as the operation of
    ``vpn_admin`` is refused at the request boundary — so a single tool call can
    never both ask for a money operation and approve it.
    """
    outcome, parsed = ai_call("vpn_admin", {"operation": "vpn_confirm"})

    assert parsed is None
    assert outcome is None
    assert not vpn.touched()


def test_vpn_admin_refuses_an_unknown_operation(vpn):
    outcome, parsed = ai_call("vpn_admin", {"operation": "vpn_delete_everything"})

    assert parsed is None
    assert outcome is None
    assert not vpn.touched()


def test_the_two_operation_tables_agree():
    """``admin_service`` keeps its own copy to avoid an import cycle.

    A copy that can drift is worse than no copy, so this asserts they are the
    same set — the duplication is deliberate and this is what makes it safe.
    """
    assert admin_service.VPN_OPERATIONS == vpn_service.VPN_OPERATION_NAMES
    assert set(admin_service.VPN_OPERATIONS) <= set(admin_service.OPERATIONS)
    for name in admin_service.VPN_OPERATIONS:
        assert admin_service.OPERATIONS[name].permission == "vpn.manage"
        assert admin_service.OPERATIONS[name].kind == admin_service.OP_VPN
        assert admin_service.OPERATIONS[name].right is None


def test_the_three_operations_that_need_approval_are_the_three_that_move_money():
    needs = {name for name, op in vpn_service.VPN_OPS.items() if op.needs_confirmation}
    assert needs == {"vpn_balance", "vpn_orders_sweep", "vpn_transaction_status"}


def test_a_replayed_request_is_a_duplicate_and_not_a_second_execution(vpn):
    parsed = admin_tools.parse_write_call(
        "vpn_admin",
        {"operation": "vpn_plan_active", "plan_id": PLAN_ID, "enabled": False},
        actor_id=OWNER,
        chat_id=CHAT,
        request_id="fixed-id",
    )

    first = run(admin_service.execute(parsed, NoGateway(), bot_id=BOT_ID))
    second = run(admin_service.execute(parsed, NoGateway(), bot_id=BOT_ID))

    assert first.outcome == admin_service.OUTCOME_OK
    assert second.outcome == admin_service.OUTCOME_DUPLICATE
    assert len(vpn.named("set_plan_active")) == 1


def test_a_pending_operation_is_not_a_failure_in_the_refusal_report(vpn):
    """It is a state, not a refusal — counting it would send the owner hunting."""
    balance()

    refusals = admin_service.recent_refusals(limit=10)

    assert all(r["outcome"] != admin_service.OUTCOME_VPN_AWAITING_CONFIRMATION for r in refusals)
    assert admin_service.OUTCOME_VPN_AWAITING_CONFIRMATION not in admin_service._REFUSAL_OUTCOMES
