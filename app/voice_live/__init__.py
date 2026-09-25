"""Nexus Voice Live: the same Nexus, reached through a voice interface.

A live call is not a second assistant. It is the assistant this bot already has
— the same awareness, the same authority model, the same audit trail, the same
deterministic switches — with a microphone and a speaker attached. Everything in
this package exists to keep that true:

* ``awareness_bridge`` asks the *existing* ``app/awareness_context`` for the
  room's context. It does not grow a second awareness, and it does not read the
  database itself.
* ``actions`` turns anything the model wants to *do* into a typed
  ``AdminRequest`` and hands it to the *existing* ``app/admin_service``, so the
  authorisation, the audit trail and the guard path are the ones that already
  exist. The model asks; the application decides.
* ``speakers`` is the only source of "who is speaking", and it comes from
  Telegram's participant list. A model's claim about identity is never read.
* ``errors`` and ``state`` are pure: the failure taxonomy and the lifecycle
  table, with no dependencies, so both can be read in full to answer "what can
  go wrong here".

The package is gated by ``GEMINI_LIVE_ENABLED``, which defaults to ``false``.
Nothing in here is reachable until an operator turns it on. The real Telegram
transport needs an MTProto credential — an ``api_id``/``api_hash`` pair and a
logged-in user session, because the Bot API has no method to join a voice chat —
and reports itself as unavailable rather than pretending when it does not have
one. (The library, ``py-tgcalls`` over ``ntgcalls``, installs on this
deployment's Python 3.12, and the credential is provisioned; an earlier note that
said no wheel existed for this interpreter was wrong.) Finding the call that is
already open is done in ``call_discovery`` rather than left to the library, whose
finder reports every discovery failure as one ``NoActiveGroupCall`` — see
``docs/reference/voice-live.md`` §51.18.
"""
