"""The moderation vocabulary: a decision, and the reason for it.

    SAFE      - nothing wrong. Allow.
    REVIEW    - worth a human's attention. Allow + log, never punish.
    EXPLICIT  - content the policy decided to remove.

This module is deliberately tiny and has no dependencies of its own. It used to
hold the local visual policy as well — the ``DecisionEngine`` that turned
NudeNet detections and a scene score into SAFE / REVIEW / EXPLICIT — but that
whole subsystem was removed (see ``AgentMD.md``), and what remains is the pair
of values every surviving moderation path agrees on: the pattern filter, the
text-moderation path, and the executor in ``app/moderation.py``.

Keeping the vocabulary here rather than inside the executor is what lets those
three share one definition of "what a decision is" without any of them owning
it. Nothing in this file can act, and nothing in it can be configured into
acting: an action needs ``app/moderation.py`` and a caller with a Telegram
client.
"""
from dataclasses import dataclass
from enum import Enum


class Decision(str, Enum):
    SAFE = "SAFE"
    REVIEW = "REVIEW"
    EXPLICIT = "EXPLICIT"


@dataclass(frozen=True)
class DecisionResult:
    """One verdict and the sentence that explains it.

    ``reason`` is for the operator's log; it never contains the content itself.
    """

    decision: Decision
    reason: str
