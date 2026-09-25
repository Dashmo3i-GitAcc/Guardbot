"""Read-only data for the panel's pages, read from the shared database.

The panel is a second *process* over the same SQLite file the bot writes (see
``docker-compose.yml``). That fact decides everything in this module:

* **Persisted state only.** Nothing here builds a pool, reads an in-memory
  registry, or asks ``app/gemini_pool`` for its live view. The bot's pools live
  in the bot's memory; the panel cannot see them, and pretending otherwise —
  by importing ``app/gemini_pool`` and building a *second* registry from the
  panel's own environment — would report the panel's configuration as if it
  were the bot's. What the panel can see is what the bot has written down, and
  that is what it shows, labelled as such.
* **No credentials, ever.** Not masked, not fingerprinted, not read. The pool
  rows carry a masked tail; this module does not even surface that, because the
  Overview is about health and usage, not about identifying a key.
* **Every source is independently guarded.** A read that fails degrades *that*
  number to a zero or an empty list, logs once, and names itself in
  ``failed_sources`` so the page can say a section is unavailable rather than
  showing a confident zero. One broken table must never blank the page.

Nothing here writes. There is no ``INSERT``, no ``UPDATE`` and no ``DELETE`` in
this module, and no code path that reaches one.
"""
from __future__ import annotations

import logging
import time

from app import db
from app.web import copy, labels

log = logging.getLogger("guardbot.web.queries")

# How many recent pool events the Overview shows. Small on purpose: the page is
# a summary, and the events table is the diagnostic record an operator opens
# *after* something looks wrong.
EVENT_LIMIT = 12

# The four workloads that keep a per-day counter table of their own. These are
# fixed by the code (``app/db.py`` has exactly four ``*_usage`` tables), not
# discovered at runtime, so the list is a constant rather than a query. The
# second element is the *name* of the reader — resolved through ``db`` on each
# call rather than captured here — and the third is the outcome column that
# means "the workload did its job": ``relevant`` for the intent classifier,
# ``replies`` for chat, ``flagged`` for moderation, ``transcripts`` for
# transcription. Holding the name rather than the function is what lets a test
# break one reader by patching ``db``, the way it breaks every other source.
USAGE_SOURCES = (
    ("intent", "ai_usage", "relevant"),
    ("chat", "chat_usage", "replies"),
    ("moderation", "mod_usage", "flagged"),
    ("transcribe", "transcript_usage", "transcripts"),
)

def _safe(name: str, reader, default, failures: list[str]):
    """Run one read, and never let it take the page down with it.

    ``reader`` is called with no arguments (bind the arguments in the caller
    with a default-argument lambda). A failure is logged once, recorded in
    ``failures``, and answered with ``default`` — so the caller gets a value of
    the right shape and the page can be honest about which part it could not
    read, instead of rendering a zero that looks like real data.
    """
    try:
        return reader()
    except Exception:  # noqa: BLE001 - one unreadable source is not a page error
        log.exception("panel: could not read %s", name)
        failures.append(name)
        return default


def _state_buckets(rows: list[dict], moment: int) -> dict:
    """Count persisted account rows into the same buckets ``Pool.health`` uses.

    ``health()`` is the bot's own grouping, and the panel mirrors it so the two
    surfaces cannot disagree about the same pool: INVALID is dead,
    QUOTA_EXHAUSTED is out of allowance, RATE_LIMITED/UNAVAILABLE is limited,
    an account still inside a persisted cooldown is limited, and everything else
    — including a state this version does not recognise — is active, exactly as
    ``health``'s final ``else`` treats it.

    One of ``health``'s steps is deliberately *not* reproduced: it also counts
    an account whose daily allowance is spent as limited, and the panel cannot
    know that, because the allowance is a per-account setting the panel does not
    read. So an account that is healthy but has spent its day reads as active
    here and as limited in ``/pool``. That is the one honest difference, and it
    is noted rather than papered over.
    """
    buckets = {"active": 0, "limited": 0, "exhausted": 0, "invalid": 0}
    for row in rows:
        state = str(row.get("state") or "ACTIVE").upper()
        if state == "INVALID":
            buckets["invalid"] += 1
        elif state == "QUOTA_EXHAUSTED":
            buckets["exhausted"] += 1
        elif state in ("RATE_LIMITED", "UNAVAILABLE"):
            buckets["limited"] += 1
        else:
            # The order matters, and it is ``health``'s order: an account whose
            # persisted state is healthy is still limited while its cooldown
            # runs. Checking the state alone would report it as usable.
            try:
                cooling = int(row.get("cooldown_until") or 0) > moment
            except (TypeError, ValueError):
                cooling = False
            buckets["limited" if cooling else "active"] += 1
    return buckets


def _pools(accounts: list[dict], moment: int, failures: list[str]) -> list[dict]:
    """One row per workload the bot has ever persisted an account for.

    The row carries only what the page renders. ``successes`` and the rate-limit
    and quota counters live on the same table, but they are not on this page —
    the pool's *health* is, and its per-account detail is a later screen's job.
    """
    grouped: dict[str, list[dict]] = {}
    for row in accounts:
        grouped.setdefault(str(row.get("workload") or "?"), []).append(row)

    out: list[dict] = []
    for name in sorted(grouped):
        rows = grouped[name]
        totals = _safe(
            f"pool_counts:{name}",
            lambda workload=name: db.pool_counts(workload),
            {},
            failures,
        )
        out.append(
            {
                "workload": name,
                "label": labels.workload(name),
                "accounts": len(rows),
                **_state_buckets(rows, moment),
                "requests": int(totals.get("requests") or 0),
                "failures": int(totals.get("failures") or 0),
            }
        )
    return out


def _usage(failures: list[str]) -> tuple[list[dict], int, int]:
    """Today's counters for the four workloads that keep their own table."""
    rows: list[dict] = []
    requests = 0
    errors = 0
    for name, reader_name, positive in USAGE_SOURCES:
        values = _safe(f"usage:{name}", getattr(db, reader_name), {}, failures)
        calls = int(values.get("calls") or 0)
        row_errors = int(values.get("errors") or 0)
        requests += calls
        errors += row_errors
        rows.append(
            {
                "workload": name,
                "label": labels.workload(name),
                "calls": calls,
                "errors": row_errors,
                "skipped": int(values.get("skipped") or 0),
                "positive": int(values.get(positive) or 0),
                "positive_label": labels.positive(positive),
            }
        )
    return rows, requests, errors


def _switches(failures: list[str]) -> list[dict]:
    """The three deployment-wide on/off switches, as the bot has persisted them.

    ``None`` from the database means nobody has ever touched the switch, which
    is not the same fact as "off" — the deploy-time configuration decides in
    that case. The label says so rather than guessing which way it resolved.
    """
    nexus_row = _safe("nexus_state", db.nexus_state_get, None, failures)
    awareness_row = _safe("awareness_control", db.awareness_control_get, None, failures)
    search_row = _safe("search_control", db.search_control_get, None, failures)

    stored = str((nexus_row or {}).get("state") or "")
    # Mirrors ``nexus.load``: an absent or unknown state is the default, online.
    nexus_on = stored != "offline"

    return [
        {
            "key": "nexus",
            "label": copy.OVERVIEW_SWITCH_NEXUS,
            "state": labels.switch(nexus_on),
        },
        {
            "key": "awareness",
            "label": copy.OVERVIEW_SWITCH_AWARENESS,
            "state": labels.switch(
                None if awareness_row is None else bool(awareness_row["enabled"])
            ),
        },
        {
            "key": "search",
            "label": copy.OVERVIEW_SWITCH_SEARCH,
            "state": labels.switch(
                None if search_row is None else bool(search_row["enabled"])
            ),
        },
    ]


def _attention(pools: list[dict]) -> list[dict]:
    """What the operator should look at first, most severe first.

    Only conditions the persisted rows can prove: a workload whose accounts are
    all unusable, one that has shrunk to a single usable account, and any dead
    credential. Nothing here is inferred from a threshold or a trend — a warning
    the data cannot support would be a warning an operator learns to ignore.
    """
    out: list[dict] = []
    for pool in pools:
        if pool["accounts"] == 0:
            continue
        if pool["active"] == 0:
            out.append({"level": "danger", "kind": "pool_empty", "workload": pool["label"]})
        elif pool["active"] == 1 and pool["accounts"] > 1:
            out.append({"level": "warn", "kind": "pool_one", "workload": pool["label"]})
        if pool["invalid"]:
            out.append(
                {
                    "level": "danger",
                    "kind": "pool_invalid",
                    "workload": pool["label"],
                    "count": pool["invalid"],
                }
            )
    # Danger before warning, and stable within a level so the order does not
    # shuffle between two renders of the same state.
    out.sort(key=lambda item: 0 if item["level"] == "danger" else 1)
    return out


def overview(*, now: int | None = None) -> dict:
    """Everything the Overview page shows, from persisted state only.

    Each source is read through :func:`_safe`, so a failure anywhere degrades
    one number and is named in ``failed_sources`` rather than raising. The
    function itself cannot raise: its only failure mode is a payload whose
    ``failed_sources`` is non-empty.
    """
    moment = int(time.time() if now is None else now)
    failures: list[str] = []

    rooms = _safe("rooms", db.authorized_group_list, [], failures)
    people = _safe("people", db.people_count, 0, failures)
    accounts = _safe("accounts", db.pool_accounts, [], failures)
    last_update = _safe("last_update", db.seen_updates_latest, 0, failures)
    events = _safe(
        "events", lambda: db.pool_events(limit=EVENT_LIMIT), [], failures
    )

    pools = _pools(accounts, moment, failures)
    usage, requests_today, errors_today = _usage(failures)

    return {
        "rooms": {
            "enabled": sum(1 for r in rooms if r.get("enabled")),
            "total": len(rooms),
        },
        "people": int(people or 0),
        "accounts": {
            "total": sum(p["accounts"] for p in pools),
            "active": sum(p["active"] for p in pools),
        },
        "today": {"requests": requests_today, "errors": errors_today},
        "last_update": int(last_update or 0),
        "usage": usage,
        "pools": pools,
        "switches": _switches(failures),
        "events": events,
        "attention": _attention(pools),
        "failed_sources": sorted(set(failures)),
        "generated_at": moment,
    }


__all__ = ["EVENT_LIMIT", "USAGE_SOURCES", "overview"]
