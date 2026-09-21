"""The inbound text filters: links, banned words, phishing shapes.

The property that matters throughout is that a filter is a *rule matcher*, not a
judge. It says "this text matches a rule I was given" or nothing at all, and it
never guesses. Most of these tests are about the ways it must stay quiet.
"""
import pytest

from app import config, text_filters


@pytest.fixture
def armed(monkeypatch):
    """A filter that is switched on with a known rule set."""
    monkeypatch.setattr(config, "FILTER_ENABLED", True)
    monkeypatch.setattr(config, "FILTER_LINK_ACTION", "review")
    monkeypatch.setattr(config, "FILTER_WORD_ACTION", "delete")
    monkeypatch.setattr(config, "FILTER_PHISHING_ACTION", "delete")
    monkeypatch.setattr(config, "FILTER_BANNED_WORDS", ["badword", "کلاهبردار"])
    monkeypatch.setattr(config, "FILTER_ALLOWED_DOMAINS", ["example.com", "good.ir"])
    monkeypatch.setattr(config, "FILTER_EXEMPT_ADMINS", True)
    monkeypatch.setattr(config, "FILTER_MIN_CHARS", 4)
    return text_filters


# ── Off by default ────────────────────────────────────────────────────────
def test_the_filter_is_off_until_it_is_switched_on():
    """The default must be "do nothing", because a delete cannot be undone."""
    assert config.FILTER_ENABLED is False
    assert text_filters.rules() == []
    assert text_filters.inspect("buy cheap stuff at http://1.2.3.4/x") is None


def test_nothing_is_filtered_when_the_filter_is_off(monkeypatch):
    monkeypatch.setattr(config, "FILTER_ENABLED", False)
    monkeypatch.setattr(config, "FILTER_BANNED_WORDS", ["badword"])

    assert text_filters.inspect("badword") is None


# ── The quiet cases ───────────────────────────────────────────────────────
def test_ordinary_text_matches_nothing(armed):
    assert armed.inspect("سلام، حال شما چطوره؟") is None


def test_an_empty_message_matches_nothing(armed):
    assert armed.inspect("") is None
    assert armed.inspect("   ") is None


def test_a_message_below_the_floor_is_not_filtered(armed):
    """A two-character line carries no signal."""
    assert armed.inspect("hi") is None


def test_no_match_is_not_a_statement_that_the_text_is_safe(armed):
    """The module has no opinion on safety, only on rules. Stated as a test."""
    assert armed.inspect("some entirely ordinary sentence") is None


# ── Banned words ──────────────────────────────────────────────────────────
def test_a_banned_word_is_caught(armed):
    hit = armed.inspect("this contains badword in it")
    assert hit is not None
    assert hit.kind == text_filters.KIND_WORD
    assert hit.deletes


def test_a_banned_word_does_not_fire_from_inside_a_longer_word(armed, monkeypatch):
    """The whole reason for word boundaries: 'ass' must not match 'class'."""
    monkeypatch.setattr(config, "FILTER_BANNED_WORDS", ["ass"])
    assert armed.inspect("this class is assorted") is None
    assert armed.inspect("what an ass") is not None


def test_a_banned_word_is_case_folded(armed):
    assert armed.inspect("BADWORD") is not None
    assert armed.inspect("BaDwOrD") is not None


def test_a_persian_banned_word_is_caught(armed):
    """Word boundaries must work for Persian, not only for Latin text."""
    hit = armed.inspect("این پیام از یک کلاهبردار است")
    assert hit is not None
    assert hit.kind == text_filters.KIND_WORD


def test_a_persian_banned_word_does_not_fire_mid_word(armed):
    """The boundary must hold in Persian too, where \\b is useless."""
    assert armed.inspect("کلاهبرداران") is None


def test_a_regex_metacharacter_in_a_word_is_literal(armed, monkeypatch):
    """Operator input is escaped, not compiled as a pattern."""
    monkeypatch.setattr(config, "FILTER_BANNED_WORDS", ["a.c"])
    assert armed.inspect("abc should not match") is None
    assert armed.inspect("the a.c here") is not None


# ── Links ─────────────────────────────────────────────────────────────────
def test_an_unlisted_link_is_reviewed_by_default(armed):
    hit = armed.inspect("have a look at https://random-site.tld/offer")
    assert hit is not None
    assert hit.kind == text_filters.KIND_LINK
    assert hit.reviews
    assert not hit.deletes


def test_an_allow_listed_link_is_left_alone(armed):
    assert armed.inspect("see https://example.com/page") is None


def test_a_subdomain_of_an_allowed_domain_is_allowed(armed):
    assert armed.inspect("see https://cdn.example.com/page") is None


def test_an_allow_list_cannot_be_fooled_by_a_suffix(armed):
    """`notexample.com` must not be treated as `example.com`."""
    hit = armed.inspect("see https://notexample.com/page")
    assert hit is not None
    assert hit.kind == text_filters.KIND_LINK


def test_the_link_action_is_configurable(armed, monkeypatch):
    monkeypatch.setattr(config, "FILTER_LINK_ACTION", "delete")
    hit = armed.inspect("see https://random-site.tld/offer")
    assert hit is not None
    assert hit.deletes


def test_the_link_family_can_be_switched_off_alone(armed, monkeypatch):
    monkeypatch.setattr(config, "FILTER_LINK_ACTION", "off")
    assert armed.inspect("see https://random-site.tld/offer") is None


# ── Phishing ──────────────────────────────────────────────────────────────
def test_an_ip_literal_url_is_phishing(armed):
    hit = armed.inspect("login here http://185.12.4.9/secure")
    assert hit is not None
    assert hit.kind == text_filters.KIND_PHISHING
    assert hit.label == "ip_literal_url"


def test_a_punycode_host_is_phishing(armed):
    hit = armed.inspect("visit https://xn--80ak6aa92e.com/login")
    assert hit is not None
    assert hit.label == "punycode_host"


def test_a_url_shortener_is_phishing(armed):
    hit = armed.inspect("claim at https://bit.ly/3xYzAbC")
    assert hit is not None
    assert hit.label == "url_shortener"


def test_a_seed_phrase_lure_is_phishing(armed):
    hit = armed.inspect("send me your seed phrase to restore the wallet")
    assert hit is not None
    assert hit.label == "seed_phrase_lure"


def test_a_persian_seed_phrase_lure_is_phishing(armed):
    hit = armed.inspect("برای بازیابی کیف پول ۱۲ کلمه را بفرست")
    assert hit is not None
    assert hit.label == "seed_phrase_lure"


def test_an_airdrop_word_without_a_link_is_not_phishing(armed):
    """The words alone are ordinary in a crypto group. Only bait is bait."""
    assert armed.inspect("the airdrop was announced this morning") is None


def test_an_airdrop_lure_with_a_link_is_phishing(armed):
    hit = armed.inspect("free crypto giveaway at https://random-site.tld/x")
    assert hit is not None
    assert hit.kind == text_filters.KIND_PHISHING
    assert hit.label == "airdrop_lure"


def test_a_verification_code_lure_is_phishing(armed):
    hit = armed.inspect("send me the verification code you just received")
    assert hit is not None
    assert hit.label == "code_lure"


def test_a_doubling_scam_is_phishing(armed):
    hit = armed.inspect("send 1 btc and I will double your money")
    assert hit is not None
    assert hit.label == "doubling_scam"


def test_phishing_outranks_a_banned_word(armed):
    """The message that is both is a scam that happens to be rude."""
    hit = armed.inspect("badword — send your seed phrase now")
    assert hit is not None
    assert hit.kind == text_filters.KIND_PHISHING


def test_the_phishing_family_can_be_switched_off_alone(armed, monkeypatch):
    """Switching phishing off downgrades the message, it does not blind the bot.

    The address is still a link, and the link family still sees it — so the
    worst case is that a scam is reviewed instead of deleted. That is the
    correct meaning of "turn this family off": stop judging it as phishing, not
    stop noticing it.
    """
    monkeypatch.setattr(config, "FILTER_PHISHING_ACTION", "off")
    hit = armed.inspect("login here http://185.12.4.9/secure")

    assert hit is not None
    assert hit.kind == text_filters.KIND_LINK
    assert hit.label == "unlisted_link"


# ── Administrators ────────────────────────────────────────────────────────
def test_an_administrator_is_exempt_by_default(armed):
    assert armed.inspect("badword", is_admin=True) is None


def test_the_exemption_can_be_turned_off(armed, monkeypatch):
    monkeypatch.setattr(config, "FILTER_EXEMPT_ADMINS", False)
    hit = armed.inspect("badword", is_admin=True)
    assert hit is not None


def test_a_non_administrator_is_never_exempt(armed):
    assert armed.inspect("badword", is_admin=False) is not None


# ── Configuration is input, and is treated as such ────────────────────────
def test_an_unknown_action_falls_back_to_doing_nothing(armed, monkeypatch):
    """A typo must not become a delete."""
    monkeypatch.setattr(config, "FILTER_WORD_ACTION", "delet")
    assert armed.inspect("badword") is None


def test_the_families_are_independent(armed, monkeypatch):
    """Phishing can run while links and words are off."""
    monkeypatch.setattr(config, "FILTER_LINK_ACTION", "off")
    monkeypatch.setattr(config, "FILTER_WORD_ACTION", "off")

    assert armed.inspect("see https://random-site.tld/offer") is None
    assert armed.inspect("badword") is None
    assert armed.inspect("login http://185.12.4.9/x") is not None


def test_a_very_long_word_list_is_capped(armed, monkeypatch):
    monkeypatch.setattr(config, "FILTER_BANNED_WORDS", [f"w{i}" for i in range(2000)])
    assert len(armed.rules()) <= 500 + len(text_filters._PHISHING)


def test_an_absurdly_long_word_is_ignored(armed, monkeypatch):
    monkeypatch.setattr(config, "FILTER_BANNED_WORDS", ["x" * 1000])
    assert not [r for r in armed.rules() if r.kind == text_filters.KIND_WORD]


def test_empty_entries_in_the_word_list_are_skipped(armed, monkeypatch):
    monkeypatch.setattr(config, "FILTER_BANNED_WORDS", ["", "  ", "badword"])
    word_rules = [r for r in armed.rules() if r.kind == text_filters.KIND_WORD]
    assert len(word_rules) == 1


# ── Host parsing ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "token,expected",
    [
        ("https://example.com/a/b", "example.com"),
        ("http://sub.example.com", "sub.example.com"),
        ("www.example.com", "www.example.com"),
        ("https://example.com:8443/x", "example.com"),
        ("https://user:pw@example.com/x", "example.com"),
        ("t.me/somechannel", "t.me"),
        ("https://EXAMPLE.COM/x", "example.com"),
    ],
)
def test_the_host_is_read_correctly(token, expected):
    assert text_filters._host_of(token) == expected


def test_urls_are_extracted_from_text():
    urls = text_filters.extract_urls("go to https://a.tld/x and t.me/channel now")
    assert len(urls) == 2


def test_a_bare_host_is_found_when_there_is_no_scheme():
    urls = text_filters.extract_urls("visit random-site.tld/offer today")
    assert urls


# ── The log must not leak ─────────────────────────────────────────────────
def test_the_log_line_never_contains_the_matched_text(armed):
    hit = armed.inspect("this contains badword in it")
    line = armed.describe(hit)

    assert "badword" not in line
    assert "contains" not in line
    assert "rule=" in line and "action=" in line


def test_the_log_line_says_so_when_nothing_matched(armed):
    assert armed.describe(None) == "filter=none"


def test_the_status_report_does_not_list_the_banned_words(armed):
    report = armed.status()

    assert report["banned_words"] == 2          # the count, not the words
    assert "badword" not in str(report)
    assert report["enabled"] is True


def test_the_status_reports_each_family_separately(armed):
    report = armed.status()

    assert report["link_action"] == "review"
    assert report["word_action"] == "delete"
    assert report["phishing_action"] == "delete"
