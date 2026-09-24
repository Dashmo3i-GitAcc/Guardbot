"""The operational data the assistant may read, and the shape it may read it in.

The brief asks for the assistant to be able to answer questions like "why was
this person never made an admin?", "why did Nexus not reply?", "what was the
last agent error?" — which means it needs to read operational history. It also
asks, in the same breath, that no secret ever reach the model. Those two
requirements are in tension exactly once, and this module is where the tension
is resolved: every read is a **typed function with an allowlisted return
shape**, and every string that leaves is passed through a redactor.

Three rules, and each one closes a different door:

**No generic query.** There is no ``execute_sql`` here, and there is no
parameter anywhere that becomes SQL text. Each function knows the one question
it answers, so the set of things the assistant can learn is the set of functions
in this file — which is short enough to review.

**No row is copied through.** The sources are SQLite tables; the answers are
dicts built field by field. A column added to a table later cannot appear in an
answer by default, because nothing here does ``SELECT *`` into a return value.
That is what makes "secret-bearing columns are structurally excluded" a property
rather than a promise.

**No secret leaves.** ``redact`` is applied at the boundary, after the dict is
assembled, so a token that ended up in a detail string — in an error message, in
a task result — is scrubbed even though it was never meant to be there. The
redaction is the second line of defence; the allowlist above is the first.

Everything is bounded: a time window, a count, and (where it applies) a room.
This is context for an answer, not a copy of the database in a prompt.
"""
from __future__ import annotations

import logging
import re
import time

from . import awareness, config, db, nexus, rbac, web_search

log = logging.getLogger("guardbot.agent.data")

# The sources a search may draw on, and the keys they are addressed by.
SOURCE_ADMIN = "admin"
SOURCE_MODEL = "model"
SOURCE_AGENT = "agent"
SOURCE_AWARENESS = "awareness"
SOURCE_MODERATION = "moderation"

SOURCES = (
    SOURCE_ADMIN,
    SOURCE_MODEL,
    SOURCE_AGENT,
    SOURCE_AWARENESS,
    SOURCE_MODERATION,
)

# Bounds. A search is a bounded read; these are the ceilings a caller cannot
# raise past by asking nicely.
MAX_EVENTS = 50
DEFAULT_EVENTS = 20
MAX_WINDOW_SECONDS = 30 * 24 * 3600


def redact(value):
    """Scrub anything credential-shaped out of a string.

    Delegates to the bridge's redactor rather than keeping a second pattern
    list, because two lists of "what a secret looks like" would drift and the
    one that drifted would be the one nobody was reading. The import is lazy so
    that this module — which is imported by the tool layer — does not pull the
    whole agent bridge in just to be able to describe an identity.
    """
    if not isinstance(value, str):
        return value
    from . import agent_bridge

    return agent_bridge.redact(value)


def _clean(value):
    """Redact a value, and recurse through the containers a DTO may hold."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


# ── The VPN redactor, and why it is a second one ──────────────────────────
# A VPN service is described by a *connection string*, and that string is the
# credential: ``vless://…`` and its siblings carry the client id in the
# fragment, and a subscription link carries it in the path. So the VPN reads
# need patterns the generic redactor does not have.
#
# They are kept here rather than added to ``agent_bridge._SECRET_PATTERNS`` for
# a specific reason: a bare 32-hex rule is right for a panel client id and wrong
# for this bot's own identity handle, which is a deliberate non-secret that
# ``identity_view`` returns on purpose. A global rule would quietly rewrite it
# and break the thing the identity layer exists to provide. Two redactors, each
# applied where it belongs, cannot interfere.
_VPN_SECRET_PATTERNS: tuple[re.Pattern, ...] = (
    # A configuration URI. The whole thing goes: there is no useful part left
    # once the credential in it is removed, and a half-URI invites the reader to
    # reconstruct the rest.
    re.compile(
        r"(?i)\b(?:vless|vmess|trojan|hysteria2|hysteria|tuic|wireguard|"
        r"v2ray|clash|ssr|ss)://\S+"
    ),
    # A subscription link, by the parameter the panel uses and by the path it
    # serves on. The VPN bot omits the field entirely before serialising, so
    # this is the second line of defence — for a value that arrives inside a
    # string somebody else assembled.
    re.compile(r"(?i)\bsub_url\s*[:=]\s*\S+"),
    re.compile(r"(?i)https?://\S+/(?:sub|subscription|link)/\S*"),
    # A panel client id, hyphenated or bare.
    re.compile(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
    ),
    re.compile(r"\b[0-9a-fA-F]{32}\b"),
)


def redact_vpn(value):
    """Scrub VPN credentials, then anything else credential-shaped.

    Both redactors, in that order, so a token that happens to sit inside a
    connection string is removed by whichever pattern sees it first.
    """
    if not isinstance(value, str):
        return value
    from . import agent_bridge

    out = agent_bridge.redact(value)
    for pattern in _VPN_SECRET_PATTERNS:
        out = pattern.sub(agent_bridge.REDACTED, out)
    return out


def _clean_vpn(value):
    """``_clean`` for VPN payloads: the VPN redactor, recursively."""
    if isinstance(value, str):
        return redact_vpn(value)
    if isinstance(value, dict):
        return {k: _clean_vpn(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean_vpn(v) for v in value]
    return value


def _window(since: int, *, now: int | None = None) -> int:
    """Clamp a requested start time to the maximum window this module allows."""
    now = int(now if now is not None else time.time())
    if since <= 0:
        return now - MAX_WINDOW_SECONDS
    return max(int(since), now - MAX_WINDOW_SECONDS)


def _event(
    *,
    at: int,
    source: str,
    summary: str,
    actor_id: int = 0,
    target_id: int = 0,
    chat_id: int = 0,
    outcome: str = "",
    detail: str = "",
) -> dict:
    """One event in the common shape. Every field is named, none is copied."""
    return {
        "at": int(at or 0),
        "source": source,
        "summary": redact(str(summary or ""))[:240],
        "actor_id": int(actor_id or 0),
        "target_id": int(target_id or 0),
        "chat_id": int(chat_id or 0),
        "outcome": str(outcome or "")[:40],
        "detail": redact(str(detail or ""))[:240],
    }


# ── Per-source readers ────────────────────────────────────────────────────
def _admin_events(*, chat_id=0, actor_id=0, target_id=0, since=0, limit=MAX_EVENTS):
    rows = db.audit_since(chat_id=chat_id or None, since=since, limit=limit)
    out = []
    for row in rows:
        if actor_id and int(row.get("actor_id") or 0) != actor_id:
            continue
        if target_id and int(row.get("target_id") or 0) != target_id:
            continue
        out.append(
            _event(
                at=row.get("at", 0),
                source=SOURCE_ADMIN,
                summary=f"{row.get('action', '')} -> {row.get('outcome', '')}",
                actor_id=row.get("actor_id", 0),
                target_id=row.get("target_id") or 0,
                chat_id=row.get("chat_id") or 0,
                outcome=row.get("outcome", ""),
                detail=row.get("detail", ""),
            )
        )
    return out


def _model_events(*, since=0, limit=MAX_EVENTS):
    """Pool events: rate limits, failures, credential pairing, model changes."""
    out = []
    for row in db.pool_events(limit=limit):
        if int(row.get("at") or 0) < since:
            continue
        out.append(
            _event(
                at=row.get("at", 0),
                source=SOURCE_MODEL,
                summary=(
                    f"{row.get('workload', '')}/{row.get('kind', '')}"
                    + (f" {row.get('reason', '')}" if row.get("reason") else "")
                ),
                detail=row.get("detail", ""),
            )
        )
    return out


def _agent_events(*, actor_id=0, chat_id=0, since=0, limit=MAX_EVENTS):
    out = []
    for row in db.agent_task_recent(limit=limit, chat_id=chat_id):
        if int(row.get("created_at") or 0) < since:
            continue
        if actor_id and int(row.get("actor_id") or 0) != actor_id:
            continue
        out.append(
            _event(
                at=row.get("updated_at") or row.get("created_at") or 0,
                source=SOURCE_AGENT,
                summary=(
                    f"{row.get('request_id', '')} {row.get('repository', '')} "
                    f"{row.get('operation', '')} -> {row.get('status', '')}"
                ),
                actor_id=row.get("actor_id", 0),
                chat_id=row.get("chat_id", 0),
                outcome=row.get("status", ""),
                detail=row.get("error", ""),
            )
        )
    return out


def _awareness_events(*, chat_id=0):
    """The current understanding of a room, as one event. Not a history."""
    if not chat_id:
        return []
    row = db.awareness_get(int(chat_id)) or {}
    if not row:
        return []
    return [
        _event(
            at=row.get("updated_at", 0),
            source=SOURCE_AWARENESS,
            summary=(
                f"passes={row.get('passes', 0)} relevant={row.get('relevant', 0)} "
                f"topic={row.get('topic', '')}"
            ),
            chat_id=chat_id,
            detail=row.get("summary", ""),
        )
    ]


def _moderation_events(*, since=0):
    """Today's moderation counters, as one event. Counters, never content."""
    try:
        usage = db.mod_usage()
    except Exception:  # noqa: BLE001
        log.exception("could not read the moderation counters")
        return []
    if not usage:
        return []
    return [
        _event(
            at=int(time.time()),
            source=SOURCE_MODERATION,
            summary=(
                f"today calls={usage.get('calls', 0)} "
                f"flagged={usage.get('flagged', 0)} allowed={usage.get('allowed', 0)}"
            ),
            detail=(
                f"errors={usage.get('errors', 0)} malformed={usage.get('malformed', 0)} "
                f"skipped={usage.get('skipped', 0)}"
            ),
        )
    ]


# ── The public reads ──────────────────────────────────────────────────────
def search_events(
    *,
    source: str = "",
    chat_id: int = 0,
    actor_id: int = 0,
    target_id: int = 0,
    since: int = 0,
    limit: int = DEFAULT_EVENTS,
) -> dict:
    """Correlate operational events across the sources this bot keeps.

    One answer shape for every source, so the assistant can read a timeline
    rather than five different documents. The filters are deliberately limited
    to the ids the server already uses — actor, target, room, time — and there
    is no free-text search over message content, because this bot does not keep
    message content for a search to find.

    ``source`` of "" or "all" reads every source. An unknown source is an
    explicit error rather than an empty success: a model told "no events" will
    answer confidently, while a model told "there is no such source" will not.
    """
    wanted = (source or "").strip().lower()
    if wanted and wanted != "all" and wanted not in SOURCES:
        return {"error": f"unknown source {source!r}", "sources": list(SOURCES)}
    chosen = [wanted] if wanted and wanted != "all" else list(SOURCES)
    cap = max(1, min(int(limit or DEFAULT_EVENTS), MAX_EVENTS))
    start = _window(int(since or 0))

    events: list[dict] = []
    for name in chosen:
        try:
            if name == SOURCE_ADMIN:
                events += _admin_events(
                    chat_id=chat_id,
                    actor_id=actor_id,
                    target_id=target_id,
                    since=start,
                    limit=cap,
                )
            elif name == SOURCE_MODEL:
                events += _model_events(since=start, limit=cap)
            elif name == SOURCE_AGENT:
                events += _agent_events(
                    actor_id=actor_id, chat_id=chat_id, since=start, limit=cap
                )
            elif name == SOURCE_AWARENESS:
                events += _awareness_events(chat_id=chat_id)
            elif name == SOURCE_MODERATION:
                events += _moderation_events(since=start)
        except Exception as exc:  # noqa: BLE001 - one source failing is not the read failing
            log.exception("could not read %s events", name)
            events.append(
                _event(
                    at=int(time.time()),
                    source=name,
                    summary="this source could not be read",
                    detail=type(exc).__name__,
                )
            )

    events.sort(key=lambda e: int(e.get("at") or 0), reverse=True)
    return {
        "count": len(events[:cap]),
        "window_seconds": int(time.time()) - start,
        "events": events[:cap],
    }


def nexus_diagnostics(chat_id: int = 0) -> dict:
    """Why Nexus is or is not answering in a room, from the server's own state.

    The brief's "چرا Nexus جواب نداد؟" is not answerable from a log the assistant
    cannot see; it is answerable from the state that decided it. This gathers
    those decisions — the switch, the awareness layer, the pending batch, the
    recent refusals — and, where it can, states the reason in one line rather
    than leaving the model to infer it.
    """
    chat_id = int(chat_id or 0)
    out: dict = {
        "chat_id": chat_id,
        "nexus_state": nexus.state(),
        "nexus_online": nexus.is_online(),
        # The **effective** state — ``configured() and running()`` — because the
        # diagnostic exists to explain why Nexus did or did not act. Reading the
        # configuration here would keep saying "awareness is on" after the owner
        # switched it off with a message, and the model would then be told the
        # room is being read when it is not.
        "awareness_enabled": awareness.enabled(),
        # The same rule as the awareness line above: the effective state, so a
        # diagnostic cannot keep saying "search is on" after the owner switched
        # it off with a message.
        "search_enabled": web_search.enabled(),
        "observe_admins": bool(getattr(config, "NEXUS_OBSERVE_ADMINS", False)),
        # The boundary is the room, not the speaker: every member of a registered
        # group is answered, so there is no actor gate to report.
        "answers_all_members_in_registered_groups": True,
        "reasons": [],
    }

    if not out["nexus_online"]:
        out["reasons"].append(
            "Nexus is switched off, so it is not answering anybody. Only the "
            "owner can switch it back on."
        )

    if chat_id:
        try:
            state = db.awareness_get(chat_id) or {}
            pending = db.group_pending(chat_id)
            out["awareness"] = {
                "passes": int(state.get("passes") or 0),
                "relevant": int(state.get("relevant") or 0),
                "topic": str(state.get("topic") or "")[:200],
                "summary": redact(str(state.get("summary") or ""))[:600],
                "updated_at": int(state.get("updated_at") or 0),
                "seen_message_id": int(state.get("seen_message_id") or 0),
                "pending_messages": int(pending[0].get("pending") or 0) if pending else 0,
            }
            if not out["awareness_enabled"]:
                out["reasons"].append("The awareness layer is switched off.")
            elif out["awareness"]["pending_messages"] == 0:
                out["reasons"].append(
                    "There is nothing unread in this room, so no pass was due."
                )
        except Exception:  # noqa: BLE001
            log.exception("could not read the awareness state for diagnostics")
            out["awareness"] = {}

        try:
            refusals = [
                row
                for row in db.audit_since(chat_id=chat_id, since=0, limit=MAX_EVENTS)
                if row.get("outcome") != db.AUDIT_OK
            ][:5]
            out["recent_refusals"] = [
                {
                    "at": int(row.get("at") or 0),
                    "action": row.get("action", ""),
                    "outcome": row.get("outcome", ""),
                    "actor_id": int(row.get("actor_id") or 0),
                    "detail": redact(str(row.get("detail") or ""))[:200],
                }
                for row in refusals
            ]
        except Exception:  # noqa: BLE001
            log.exception("could not read the refusals for diagnostics")
            out["recent_refusals"] = []

    try:
        out["recent_model_events"] = [
            {
                "at": int(row.get("at") or 0),
                "workload": row.get("workload", ""),
                "kind": row.get("kind", ""),
                "reason": row.get("reason", ""),
            }
            for row in db.pool_events(limit=8)
        ]
    except Exception:  # noqa: BLE001
        out["recent_model_events"] = []

    if not out["reasons"]:
        out["reasons"].append(
            "Nothing in the server's state is preventing an answer; if Nexus was "
            "silent, the room's conversation simply did not call for a reply."
        )
    return out


def agent_task_view(request_id: str, *, chat_id: int = 0) -> dict:
    """One coding-agent task, as the owner may see it. Redacted, bounded.

    ``chat_id`` scopes the read to one room when given: a task raised in another
    group is not readable here, so a group's assistant cannot be used to pull
    another group's task text or result.
    """
    request_id = (request_id or "").strip()[:64]
    if not request_id:
        return {"error": "no task id supplied"}
    try:
        row = db.agent_task_get(request_id)
    except Exception:  # noqa: BLE001
        log.exception("could not read an agent task")
        return {"error": "the task could not be read"}
    if not row:
        return {"error": "no such task", "request_id": request_id}
    if chat_id and int(row.get("chat_id") or 0) != int(chat_id):
        return {"error": "no such task", "request_id": request_id}
    from . import agent_bridge

    from . import identity

    actor_id = int(row.get("actor_id") or 0)
    return {
        "request_id": row.get("request_id", ""),
        "status": row.get("status", ""),
        "status_label": agent_bridge.status_label(row.get("status", "")),
        "repository": row.get("repository", ""),
        "operation": row.get("operation", ""),
        "danger": row.get("danger", ""),
        "actor_id": actor_id,
        "actor_uuid": identity.uuid_for(actor_id),
        "chat_id": int(row.get("chat_id") or 0),
        "created_at": int(row.get("created_at") or 0),
        "updated_at": int(row.get("updated_at") or 0),
        "started_at": int(row.get("started_at") or 0),
        "finished_at": int(row.get("finished_at") or 0),
        # The task text and the result are the two attacker-adjacent strings in
        # the row; both are redacted and bounded on the way out.
        "task": redact(str(row.get("task") or ""))[:1200],
        "result": redact(str(row.get("result") or ""))[:2000],
        "error": redact(str(row.get("error") or ""))[:600],
        "confirmed_by": int(row.get("confirmed_by") or 0),
    }


def identity_view(user_id: int, *, chat_id: int = 0) -> dict:
    """One person, assembled by :mod:`app.identity` and redacted here."""
    from . import identity

    return _clean(identity.describe(int(user_id), chat_id=int(chat_id) or 0))


def resolve_identity(query: str, *, chat_id: int = 0) -> dict:
    """Resolve any spoken or written reference to one person."""
    from . import identity

    return _clean(identity.resolve(query, chat_id=int(chat_id) or 0))


def service_status() -> dict:
    """Which integrations exist, and what each can actually do."""
    from . import service_adapters

    return _clean(
        {
            "integrations": service_adapters.capabilities(),
            "summary": service_adapters.summary(),
        }
    )


# ── The VPN reads ─────────────────────────────────────────────────────────
# The fields a VPN service may be described by, mirroring the allowlist the VPN
# bot applies before it serialises (``user_services.PUBLIC_SERVICE_FIELDS``).
#
# Two independent narrowings of the same object, and that is deliberate rather
# than redundant: the VPN bot's copy protects every caller of its API, and this
# one holds even if a future version of that API starts sending more. The
# connection string and the panel client id are absent from both lists — the
# plan's rule is that omitting beats redacting, because a field that is never
# copied cannot be leaked by a redactor that misses.
VPN_SERVICE_FIELDS = (
    "id",
    "display_name",
    "plan_name",
    "status",
    "enable",
    "is_trial",
    "days_left",
    "days_left_short",
    "expires_at",
    "total_bytes",
    "used_bytes",
    "limit_ip",
)

# Ceilings. A list of services is context for an answer, not a dump.
MAX_VPN_SERVICES = 20


def vpn_service_view(raw) -> dict:
    """One VPN service, field by field. Never ``**raw``.

    Only scalars are copied, and only the ones on the allowlist. A nested
    object — which is where a credential would hide — is dropped rather than
    walked, because there is no field in this shape that is meant to be one.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for key in VPN_SERVICE_FIELDS:
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(value, bool) or isinstance(value, (int, float)):
            out[key] = value
        elif isinstance(value, str):
            out[key] = redact_vpn(value)[:120]
    return out


def vpn_subscription_view(answer) -> dict:
    """Every VPN service belonging to one Telegram account.

    The count is computed from the narrowed rows rather than taken from the
    answer, so a row that was dropped for being unreadable cannot inflate it.
    """
    if not isinstance(answer, dict):
        return {"error": "the VPN service answered something that was not readable"}
    raw = answer.get("services")
    rows = [vpn_service_view(item) for item in raw] if isinstance(raw, list) else []
    rows = [row for row in rows if row][:MAX_VPN_SERVICES]
    return {
        "found": bool(answer.get("found")),
        "telegram_id": int(answer.get("telegram_id") or 0),
        "has_service": bool(answer.get("has_service")),
        "count": len(rows),
        "services": rows,
    }


def vpn_one_service_view(answer) -> dict:
    """One VPN service by its own id, or an explicit "there is no such service"."""
    if not isinstance(answer, dict):
        return {"error": "the VPN service answered something that was not readable"}
    return {
        "found": bool(answer.get("found")),
        "service_id": int(answer.get("service_id") or 0),
        "service": vpn_service_view(answer.get("service")),
    }


def vpn_status_view(answer) -> dict:
    """What the VPN integration currently has switched on, from its own report."""
    if not isinstance(answer, dict):
        return {"error": "the VPN service answered something that was not readable"}
    return {
        "service": str(answer.get("service") or "")[:40],
        "acquisition_enabled": bool(answer.get("acquisition")),
        "admin_writes_enabled": bool(answer.get("admin_writes")),
        "panel_configured": bool(answer.get("panel_configured")),
        "bot_username_configured": bool(answer.get("bot_username_configured")),
    }


def vpn_unreachable(code: str = "") -> dict:
    """The answer when the VPN bot could not be asked at all.

    An explicit error rather than an empty success, for the same reason the rest
    of this module does it: a model told "no services" will say so, while a
    model told ``{}`` will fill the gap in itself. The code is one of
    ``app/vpnbot.py``'s four, and it carries no URL and no detail string.
    """
    return {
        "error": "the VPN service could not be reached",
        "code": str(code or "")[:40],
    }


def principal_view(user_id: int) -> dict:
    """The narrow role view, for callers that only need authority facts."""
    principal = rbac.resolve(int(user_id))
    return {
        "user_id": principal.user_id,
        "role": principal.role,
        "role_label": principal.label,
        "level": principal.level,
        "is_owner": principal.is_owner,
        "is_admin": principal.is_admin,
        "permissions": sorted(principal.permissions),
    }
