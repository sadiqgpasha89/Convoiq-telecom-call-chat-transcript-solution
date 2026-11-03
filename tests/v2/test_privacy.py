"""PII redaction, tokenisation, vault erasure, dual-pass and attestation verification."""
from __future__ import annotations

import pytest

from convoiq.contracts import RawInteraction, Speaker, Turn
from convoiq.privacy import Tokenizer
from convoiq.privacy.redactor import second_pass, verify_attestation
from convoiq.privacy.tokenizer import TOKEN_RE

PII_TEXTS = {
    "phone": ("my number is 408-488-0581 please", "408-488-0581"),
    "email": ("write to jane.doe@example.com thanks", "jane.doe@example.com"),
    "card": ("card is 4111 1111 1111 1111 ok", "4111 1111 1111 1111"),
    "ssn": ("my ssn is 123-45-6789", "123-45-6789"),
}


@pytest.mark.parametrize("name", PII_TEXTS)
def test_pii_removed_and_tokenised(redactor, name):
    text, raw = PII_TEXTS[name]
    r = redactor.redact_text(text)
    assert raw not in r.text
    assert TOKEN_RE.search(r.text), "a token must replace the value"


def test_tokens_are_deterministic_and_keyed():
    a, b = Tokenizer("key-one-0123456789abcdef"), Tokenizer("key-two-0123456789abcdef")
    assert a.token("PHONE", "4084880581") == a.token("PHONE", "4084880581")
    assert a.token("PHONE", "4084880581") != b.token("PHONE", "4084880581"), "tokens must depend on the secret key"
    assert a.token("PHONE", "4084880581") != a.token("PHONE", "4084880582")


def test_no_pii_in_clean_text(redactor):
    t = "My phone has no signal since yesterday morning"
    assert redactor.redact_text(t).text == t


def test_private_ip_and_suffix_reference_are_not_pii(redactor):
    assert redactor.residual_scan("the router is at 192.168.1.1") == []
    assert redactor.residual_scan("card ending in 2243") == []


def test_vault_roundtrip_audit_and_gdpr_erasure(redactor):
    r = redactor.redact_text("call me on 408-488-0581", subject="cust-1")
    tok = r.tokens[0]
    assert redactor.vault.get(tok, "analyst@x", "support") == "408-488-0581"
    assert any(row[1] == "analyst@x" for row in redactor.vault.audit_log()), "every reveal is audited"
    assert redactor.vault.erase_subject("cust-1") == 1
    assert redactor.vault.get(tok, "analyst@x", "support") is None, "erased subject must be unrecoverable"


def _raw(text: str) -> RawInteraction:
    return RawInteraction(interaction_id="x-1", channel="CHAT_APP",
                          turns=[Turn(i=0, role=Speaker.CUSTOMER, text=text), Turn(i=1, role=Speaker.AGENT, text="ok")])


def test_attestation_accepts_clean_and_rejects_tampering(redactor):
    red = redactor.redact_interaction(_raw("my number is 408-488-0581 and email a.b@example.com"))
    assert verify_attestation(red, redactor.tok, redactor, require_signature=True) == []
    forged = red.model_copy(deep=True)
    forged.turns[0].text += " also 4111 1111 1111 1111"
    problems = verify_attestation(forged, redactor.tok, redactor, require_signature=True)
    assert any("hash mismatch" in p for p in problems)


def test_attestation_rejects_bad_signature(redactor):
    red = redactor.redact_interaction(_raw("hello there"))
    other = Tokenizer("a-completely-different-key-0123456")
    assert any("signature" in p for p in verify_attestation(red, other, None, require_signature=True))


def test_cloud_rescan_catches_residual_pii_in_forged_payload(redactor):
    red = redactor.redact_interaction(_raw("hello there"))
    red.turns[0].text = "my ssn is 123-45-6789"                         # edge claims clean, payload is not
    from convoiq.privacy.redactor import hashlib, json
    red.attestation.content_sha256 = hashlib.sha256(json.dumps([t.model_dump(mode="json") for t in red.turns], sort_keys=True).encode()).hexdigest()
    assert any("residual" in p for p in verify_attestation(red, redactor.tok, redactor))


def test_second_pass_is_idempotent_on_clean_and_resigns(redactor):
    red = redactor.redact_interaction(_raw("my number is 408-488-0581"))
    sp = second_pass(redactor, red)
    assert sp.attestation.residual_findings == 0
    assert verify_attestation(sp, redactor.tok, redactor, require_signature=True) == []


@pytest.mark.parametrize("text", [
    "I have your PIN as 4821, is that right?", "The PIN on file is 4821", "can you confirm 4821 is your pin?",
    "your pin has been reset to 123456 ok", "my pin number is 1234", "pin 4821", "Your PIN is 4821."])
def test_pin_variants_are_redacted(redactor, text):
    out = redactor.redact_text(text).text
    assert not any(d in out for d in ("4821", "123456", "1234")), out


@pytest.mark.parametrize("text", ["my pin is not working", "PIN reset takes 24 hours", "I was charged 4821 cents for the pin",
                                  "the plan ends in 2024, pin protection is on"])
def test_pin_detector_does_not_overreach(redactor, text):
    assert redactor.redact_text(text).text == text
