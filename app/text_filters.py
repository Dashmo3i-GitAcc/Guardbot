"""Inbound text filters: links, banned words, and the shapes a scam takes.

These are the rules that do not need a model. A banned word, a link, an
IP-literal address, a message that is a lure plus a link — all of them are
decidable with a pattern, and a rule that can be a pattern should not be a
request against a shared Gemini quota. This module runs *before* any AI is
consulted, and it never consults one.

Three deliberate limits, because a filter that deletes somebody's message is
holding a power that cannot be taken back:

* **Off by default.** ``FILTER_ENABLED`` is false. The capability is here and
  tested; switching it on is an operator's decision, made after reading the
  review log rather than before.
* **It does not guess.** A filter answers "does this text match a rule I was
  given". It never resolves a name to a person, never infers intent, and when
  nothing matches it says so. There is no ambiguity to resolve, which is why
  there is no clarification path here.
* **It cannot act.** This module returns a verdict and nothing else. It does not
  import Telegram, does not delete, does not write to the database. The caller
  hands the verdict to ``app/moderation.py`` — the same executor every other
  violation goes through — so there is one enforcement path and one strike
  ladder, not two.

The phishing patterns are a curated starting set, not a threat feed. They catch
the shapes that are cheap to recognise and expensive to miss; they will not
catch a targeted scam written by a person. Saying so is the point — a filter
that is believed to be complete is worse than one that is known to be partial.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import config

# ── Rule families ─────────────────────────────────────────────────────────
KIND_LINK = "link"
KIND_WORD = "word"
KIND_PHISHING = "phishing"

# The three actions a rule can carry, plus the one that switches it off.
ACTION_OFF = "off"
ACTION_REVIEW = "review"
ACTION_DELETE = "delete"

_ACTIONS = (ACTION_OFF, ACTION_REVIEW, ACTION_DELETE)

# A hard ceiling on how many rules are compiled. Configuration is operator input
# and a thousand-word list would be a denial of service against every message.
_MAX_RULES = 500
_MAX_PATTERN = 400


@dataclass(frozen=True)
class Hit:
    """One rule matched. ``label`` is for the log and is never the matched text."""

    kind: str
    label: str
    action: str

    @property
    def deletes(self) -> bool:
        return self.action == ACTION_DELETE

    @property
    def reviews(self) -> bool:
        return self.action == ACTION_REVIEW


@dataclass(frozen=True)
class Rule:
    kind: str
    label: str
    action: str
    pattern: re.Pattern

    def matches(self, text: str) -> bool:
        return self.pattern.search(text) is not None


# ── URL extraction ────────────────────────────────────────────────────────
# Deliberately simple. A general URL regex is a well-known way to write a
# catastrophic-backtracking bug, and this only has to be good enough to notice
# that a message contains an address and to read its host.
_URL_RE = re.compile(
    r"""(?ix)
    \b
    (?:
        https?:// | www\. | t\.me/ | telegram\.me/
    )
    [^\s<>"'\)\]]+
    """,
)

# A bare host with a plausible TLD, no scheme. Common in scam text.
_BARE_HOST_RE = re.compile(
    r"(?i)\b[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
    r"(?:/[^\s<>\"'\)\]]*)?"
)

_HOST_RE = re.compile(r"(?i)^(?:[a-z][a-z0-9+.-]*://)?(?:[^/@]*@)?([^/:?#]+)")

_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def _host_of(token: str) -> str:
    """The host of a URL-ish token, lowercased, without a port or userinfo."""
    match = _HOST_RE.match(token.strip())
    return (match.group(1) if match else "").strip().lower().rstrip(".")


def extract_urls(text: str) -> list[str]:
    """Every URL-ish token in the text. Scheme-ful first, then bare hosts."""
    found = [m.group(0) for m in _URL_RE.finditer(text or "")]
    if found:
        return found
    # Only look for bare hosts when there is something host-shaped to find.
    return [m.group(0) for m in _BARE_HOST_RE.finditer(text or "")]


def _allowed(host: str, allowed: tuple[str, ...]) -> bool:
    """Whether a host is on the allow-list, including as a subdomain of one."""
    if not host:
        return False
    for domain in allowed:
        d = domain.strip().lower().lstrip(".")
        if not d:
            continue
        if host == d or host.endswith("." + d):
            return True
    return False


# ── The phishing shapes ───────────────────────────────────────────────────
# Each entry is (label, pattern). The label is what the log and the admin report
# say, so it must never contain anything the sender wrote.
#
# The bar for adding one: it must be cheap to recognise and it must be wrong
# almost only when it is a scam. Anything that would fire on ordinary
# conversation belongs in the banned-word list, where the operator chose it
# deliberately, not here.
_PHISHING = (
    # A URL whose host is a bare IP address. No legitimate service asks people
    # to log in at http://185.12.4.9/.
    ("ip_literal_url", re.compile(
        r"(?i)\bhttps?://\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?(?:[/\s]|$)")),
    # A punycode host. Legitimate internationalised domains exist, but in a
    # group this is overwhelmingly a homograph of something people trust.
    ("punycode_host", re.compile(r"(?i)\bxn--[a-z0-9-]{2,}\.")),
    # A shortener hides where a link actually goes, which is the whole point of
    # using one in a scam.
    ("url_shortener", re.compile(
        r"(?i)\b(?:bit\.ly|tinyurl\.com|t\.co|goo\.gl|is\.gd|cutt\.ly|rb\.gy|"
        r"shorturl\.at|rebrand\.ly|tiny\.cc|ow\.ly|buff\.ly)/")),
    # The wallet-recovery shape. "seed phrase" has no innocent use in a group.
    ("seed_phrase_lure", re.compile(
        r"(?i)(?:\bseed\s*phrase\b|\brecovery\s*phrase\b|\bprivate\s*key\b|"
        r"\b۱۲\s*کلمه\b|\b12\s*کلمه\b|\b۲۴\s*کلمه\b|\b24\s*کلمه\b)")),
    # The airdrop/claim shape, but only alongside a link — the words alone are
    # ordinary in a crypto group.
    ("airdrop_lure", re.compile(
        r"(?i)(?:\bairdrop\b|\bclaim\s+(?:your\s+)?(?:reward|token|prize)\b|"
        r"\bfree\s+(?:crypto|gift|token|usdt|btc)\b|\bgiveaway\b)")),
    # Telegram account-takeover bait: they want the code, not the account.
    ("code_lure", re.compile(
        r"(?i)(?:\bverification\s+code\b|\blogin\s+code\b|\bone[- ]?time\s+code\b|"
        r"\bکد\s*(?:ورود|تایید|تأیید|پیامک)\b)")),
    # "Send to this address and get double back" — always a scam.
    ("doubling_scam", re.compile(
        r"(?i)(?:\bdouble\s+your\b|\b2x\s+your\b|\bsend\s+\d+\s*(?:btc|eth|usdt)\b"
        r"|\bبرگرداندن\s*دو\s*برابر\b)")),
)


def _compile_phishing() -> list[Rule]:
    action = _action(config.FILTER_PHISHING_ACTION)
    if action == ACTION_OFF:
        return []
    return [Rule(KIND_PHISHING, label, action, pattern) for label, pattern in _PHISHING]


def _compile_words() -> list[Rule]:
    action = _action(config.FILTER_WORD_ACTION)
    if action == ACTION_OFF:
        return []
    rules: list[Rule] = []
    for index, word in enumerate(config.FILTER_BANNED_WORDS[:_MAX_RULES]):
        word = (word or "").strip()
        if not word or len(word) > _MAX_PATTERN:
            continue
        # A word boundary on both sides, using lookarounds rather than \b
        # because \b is ASCII-oriented and would fire inside Persian words.
        # `\w` is Unicode-aware in Python 3, so this holds for both scripts.
        try:
            pattern = re.compile(
                r"(?<!\w)" + re.escape(word) + r"(?!\w)",
                re.IGNORECASE | re.UNICODE,
            )
        except re.error:  # pragma: no cover - re.escape cannot produce this
            continue
        # The label is the index, not the word. The log must not become a copy
        # of the banned-word list, and the operator already knows what they put
        # at position N.
        rules.append(Rule(KIND_WORD, f"banned_word_{index}", action, pattern))
    return rules


def _action(value: str) -> str:
    """Coerce a configured action into the closed set. Unknown means off.

    Fail closed towards doing nothing: a typo in an environment variable must
    not turn into "delete", which is the one outcome that cannot be undone.
    """
    text = (value or "").strip().lower()
    return text if text in _ACTIONS else ACTION_OFF


def rules() -> list[Rule]:
    """Every configured rule, in a stable order. Phishing, then words."""
    if not config.FILTER_ENABLED:
        return []
    return _compile_phishing() + _compile_words()


def link_action() -> str:
    """The configured action for the link family."""
    return _action(config.FILTER_LINK_ACTION)


# ── The decision ──────────────────────────────────────────────────────────
def inspect(text: str, *, is_admin: bool = False) -> Hit | None:
    """The first rule this text matches, or None.

    Ordered by severity rather than by position: a phishing rule outranks a
    banned word, because the message that is both is a scam that happens to
    contain a rude word, and the report should say the useful thing.

    An empty result is the common case and means "no rule matched" — it is not
    a statement that the text is safe. Only a model can speak to that, and this
    module deliberately has no opinion.
    """
    body = (text or "").strip()
    # The master switch, checked here and not only in `rules()`. It is the whole
    # safety argument for shipping this module: with the feature off, no rule is
    # consulted at all. A gate that lived only in the rule list would still let
    # a direct call filter a message, which is the version of this bug that
    # reaches production.
    if not config.FILTER_ENABLED:
        return None
    if not body or len(body) < max(1, int(config.FILTER_MIN_CHARS)):
        return None
    if is_admin and config.FILTER_EXEMPT_ADMINS:
        return None

    allowed = tuple(config.FILTER_ALLOWED_DOMAINS)
    urls = extract_urls(body)
    hosts = [_host_of(u) for u in urls]
    # Every host is allow-listed, so the link family has nothing to say. A
    # message with no URL at all also lands here, and that is correct: there is
    # no link to judge.
    linkable = [h for h in hosts if h and not _allowed(h, allowed)]
    has_url = bool(urls)

    # 1. Phishing. The lure rules need a link; the structural ones do not.
    for rule in _compile_phishing():
        label = rule.label
        if label in ("ip_literal_url", "punycode_host", "url_shortener"):
            if rule.matches(body):
                return Hit(rule.kind, rule.label, rule.action)
            continue
        if label == "airdrop_lure":
            # The words are ordinary in a crypto group; only a link makes it bait.
            if linkable and rule.matches(body):
                return Hit(rule.kind, rule.label, rule.action)
            continue
        if rule.matches(body):
            return Hit(rule.kind, rule.label, rule.action)

    # 2. Banned words.
    for rule in _compile_words():
        if rule.matches(body):
            return Hit(rule.kind, rule.label, rule.action)

    # 3. A link that is not allow-listed and not phishing-shaped.
    action = link_action()
    if has_url and linkable and action != ACTION_OFF:
        return Hit(KIND_LINK, "unlisted_link", action)

    return None


def describe(hit: Hit | None) -> str:
    """A log-safe one-liner. Never contains the matched text."""
    if hit is None:
        return "filter=none"
    return f"filter={hit.kind} rule={hit.label} action={hit.action}"


def status() -> dict:
    """What is switched on, for the operator. Never contains the word list."""
    return {
        "enabled": bool(config.FILTER_ENABLED),
        "link_action": link_action(),
        "word_action": _action(config.FILTER_WORD_ACTION),
        "phishing_action": _action(config.FILTER_PHISHING_ACTION),
        "banned_words": len(config.FILTER_BANNED_WORDS),
        "allowed_domains": len(config.FILTER_ALLOWED_DOMAINS),
        "exempt_admins": bool(config.FILTER_EXEMPT_ADMINS),
        "counts_as_violation": bool(config.FILTER_COUNTS_AS_VIOLATION),
    }


def reset_state() -> None:
    """Nothing is cached between calls; kept for the test-reset convention."""
    return None
