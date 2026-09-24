"""GuardBot's Admin Control Center.

A **separate process** from the bot, started as ``python -m app.web`` and run by
its own compose service (``dashboard``). It reads the same SQLite database and
calls the same ``app`` modules the bot does, so the panel can never disagree
with what the bot did — but it never runs inside the bot's event loop, so a
problem in the web layer cannot disturb Telegram polling.

Two rules hold for everything under this package:

* **The dashboard identity is not a Telegram identity.** Being an administrator
  of a Telegram group does not make anybody a dashboard administrator, and the
  panel never trusts a role, a group scope or an owner claim supplied by the
  client. Authentication lives in ``auth.py``; authorization will reuse
  ``app/rbac.py`` from M2 onward.
* **The panel reads through existing modules.** It does not bypass ``rbac``,
  ``admin_service``, ``groups`` or ``key_store``, and it does not reach into
  tables those modules own without them.

See AgentMD §54.24 for the audit and the staged plan this package is built to.
"""
