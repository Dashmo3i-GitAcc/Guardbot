"""Make the app package importable in tests without a real .env."""
import os
import sys

import pytest

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("GROUP_IDS", "-1001234567890")
os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("TMP_DIR", "/tmp/guardbot-test")
# The runtime credential store defaults to the container's data volume, which
# does not exist on a test host — but a test that exercises the add/remove flow
# must never be able to reach a real one either. Pointing it at the test temp
# directory keeps the suite hermetic; the tests that actually add a credential
# override it again with a per-test path.
os.environ.setdefault(
    "GEMINI_KEY_STORE_PATH", "/tmp/guardbot-test/gemini_keys.json"
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def fresh_nexus_state():
    """Start every test from a known Nexus state.

    ``app/nexus.py`` caches the runtime state in a module-level variable, and
    that is deliberate — it is read on every group message and a database hit
    per message would be wasteful. The cost is that a test which switches Nexus
    off would leave every later test in a silent bot, which is the kind of
    cross-test contamination that turns a real failure into an unrelated one.

    Clearing the cache is enough: the next read comes from the database, which
    is ``:memory:`` per process and empty unless a test wrote to it.

    ``app/awareness.py`` keeps two clocks the same way — when each room's newest
    unread message arrived, and when the age purge last ran. Both are in-process
    and both are read as "how long has it been", so a value left behind by one
    test would make the next one's wait or purge depend on test order.

    It also caches the owner's awareness switch, and that one is worse to leak
    than either clock: a test that switches the layer off would leave every
    later test capturing nothing and passing no rooms, and the failures would
    read as "awareness is broken" rather than "a test forgot to reset".
    """
    from app import (
        awareness,
        awareness_schedule,
        chat_queue,
        db,
        gemini_keys,
        groups,
        main,
        memory,
        nexus,
        state,
        vpn_service,
        web_search,
    )
    from app.web import audit as dashboard_audit

    # The schema, for every test rather than for whichever test happened to need
    # it first. ``db._conn`` is a module global, so a test that reaches
    # ``rbac.resolve`` — which reads the ``admins`` table — passed only because
    # an earlier test in the same process had initialised the database. Running
    # one file on its own therefore failed with "'NoneType' object has no
    # attribute 'execute'", which reads as a broken authorization path rather
    # than as a test that was never self-sufficient. ``init`` is idempotent.
    db.init()
    nexus.reset_state()
    # The room allowlist, both the table and the process cache. A room a test
    # registered (or revoked) must not be visible to the next test, and the
    # one-time seed from ``GROUP_IDS`` must be free to run again — otherwise a
    # test would inherit whichever rooms an earlier one happened to leave behind
    # and the failure would read as an authorization bug rather than as leaked
    # state.
    db.authorized_groups_reset()
    groups.reset_state()
    # The Admin Control Center's audit retention counter. It decides *when* the
    # next whole-table prune runs, so a value carried over from one test would
    # make the next test's first panel write prune for a reason that is not in
    # that test. The trail itself lives in the per-test in-memory database and
    # needs no clearing.
    dashboard_audit.reset_state()
    awareness.reset_timers()
    awareness.reset_switch()
    # The search workload's rate window, breaker and cached client. Left behind,
    # a test that opened the search circuit would leave every later test's
    # informational question silently unsearched, and the failure would read as
    # "search is broken" rather than as a leaked breaker.
    web_search.reset_state()
    # An armed "send me the key now" prompt is process state with a five-minute
    # life, which is longer than a test run. Left behind, it would make the next
    # test's private message be consumed as a credential.
    gemini_keys.reset_pending()
    # The duplicate-reply marker: a room id left behind by one test would make
    # the next test's ambient pass refuse to answer, which is exactly the kind of
    # order-dependent failure that hides a real defect.
    main._nexus_addressed.clear()
    # The bot's own Telegram rights, which are cached per chat with a TTL. A
    # test that gives its fake bot a permission would otherwise hand that
    # permission to the next test's fake bot, and the failure would read as a
    # security bug — "it muted without the right" — rather than as a cache.
    main._bot_rights_cache.clear()
    # The VPN service's retention counter. It is a plain integer that decides
    # *when* the next prune runs, so a value carried over from one test would
    # make the next test's first recorded operation prune (or not prune) for a
    # reason that is not in that test.
    vpn_service.prune_reset()
    # The memory retention counter, which decides *when* the next whole-table
    # prune runs. Left behind, a test that recorded enough clauses to trigger it
    # would make the next test's first write prune (or not) for a reason that is
    # not in that test.
    memory.reset_state()
    # The state retention counter, for the same reason: it decides *when* the
    # next whole-table prune runs, so a value left behind would make the next
    # test's first transition prune (or not) for a reason that is not in it.
    state.reset_state()
    # The awareness scheduler's per-room hints. A hint is process state keyed by
    # chat id with a one-hour life, so a hint noted by one test would defer the
    # next test's room and the failure would read as "awareness stopped
    # reading" rather than as a leaked hint. This is the same class of state as
    # ``awareness.reset_timers`` above and is reset for the same reason.
    awareness_schedule.reset()
    # The turn queue's per-conversation locks and its concurrency gate. The gate
    # is built once from config and then cached, so a test that set a concurrency
    # of one would otherwise hand that bound to every later test; the locks are
    # process state keyed by (chat, user) that must not carry a held lock across
    # a test boundary.
    chat_queue.reset_state()
    yield
    nexus.reset_state()
    # Only the cache is cleared here, never the table: a module fixture's own
    # teardown runs *before* this one and some of them close ``db._conn`` (see
    # ``test_filter_pipeline``), so a table reset here would raise on a
    # connection that is legitimately gone. The next test's setup resets the
    # table anyway, which is what actually guarantees isolation.
    groups.reset_state()
    awareness.reset_timers()
    awareness.reset_switch()
    gemini_keys.reset_pending()
    main._nexus_addressed.clear()
    main._bot_rights_cache.clear()
    web_search.reset_state()
    vpn_service.prune_reset()
    memory.reset_state()
    state.reset_state()
    awareness_schedule.reset()
    chat_queue.reset_state()

