"""Compliance rules R1-R10: every rule has a positive (mutation) test and the clean baseline must trigger none.

R4/R5/R7/R8/R9/R10 have no labelled data (reported as *unmeasured* in the eval report) - these mutation tests are
their only functional evidence, so they are intentionally explicit."""
from __future__ import annotations

import pytest

from convoiq.agents.compliance import RULES, check_compliance

from .conftest import make_conv, needs_models

pytestmark = needs_models

OPEN = ("agent", "Hello, you've reached TelcoCare. This call may be recorded for quality and training purposes. How can I help?")
VERIFY = [("agent", "For your security, can you confirm the PIN on the account?"),
          ("customer", "Sure, the PIN is 4821."),
          ("agent", "Thank you, you're verified.")]


def baseline(extra=None, channel="VOICE") -> dict:
    turns = [OPEN, ("customer", "My phone shows no service since this morning.")] + VERIFY + [
        ("agent", "I'm sorry about that. Please toggle airplane mode on and off and tell me what happens."),
        ("customer", "That fixed it, thank you."),
        ("agent", "Great, anything else I can help with today?")]
    if extra:
        turns = extra(turns)
    return make_conv(turns, channel=channel)


def ids(enrich, conv, settings) -> set[str]:
    return {v.rule_id for v in check_compliance(enrich(conv), settings).violations}


def test_rule_registry_is_complete_and_marks_measurement_status():
    assert [r.id.split("_")[0] for r in RULES] == [f"R{i}" for i in range(1, 11)]
    assert {r.id.split("_")[0] for r in RULES if r.measured} == {"R1", "R2", "R3", "R6"}


def test_clean_baseline_has_no_violations(enrich, settings):
    res = check_compliance(enrich(baseline()), settings)
    assert res.violations == [] and res.score == 1.0


def test_r1_account_work_before_verification(enrich, settings):
    conv = make_conv([OPEN, ("customer", "Why is my bill so high?"),
                      ("agent", "Let me pull up your account right away. I can see your bill is $140."),
                      ("customer", "That seems wrong."), ("agent", "Can you confirm the PIN on the account?")])
    assert "R1_NO_ID_VERIFICATION" in ids(enrich, conv, settings)


def test_r1_not_triggered_when_verified_first(enrich, settings):
    conv = baseline(lambda t: t[:-3] + [("agent", "Let me pull up your account. I can see the plan is active.")] + t[-3:])
    assert "R1_NO_ID_VERIFICATION" not in ids(enrich, conv, settings)


DISPUTE = [("customer", "There is a $95.70 fee on my bill that I don't recognise.")]


def test_r2_billing_dispute_without_disclosure(enrich, settings):
    conv = make_conv([OPEN] + DISPUTE + VERIFY + [("agent", "I'll credit the fee back to your account."), ("customer", "Thanks.")])
    assert "R2_NO_DISPUTE_DISCLOSURE" in ids(enrich, conv, settings)


def test_r2_cleared_by_disclosure(enrich, settings):
    conv = make_conv([OPEN] + DISPUTE + VERIFY + [("agent", "You have the right to dispute this charge and we will open an investigation."),
                                                  ("customer", "Thanks.")])
    assert "R2_NO_DISPUTE_DISCLOSURE" not in ids(enrich, conv, settings)


def test_r3_credit_above_authority_without_approval(enrich, settings):
    conv = make_conv([OPEN] + DISPUTE + VERIFY + [("agent", "No problem, I've applied a credit of $120.00 to your account right now."),
                                                  ("customer", "Great.")])
    assert "R3_UNAUTHORIZED_CREDIT" in ids(enrich, conv, settings)


def test_r3_ok_when_small_or_approved(enrich, settings):
    small = make_conv([OPEN] + DISPUTE + VERIFY + [("agent", "I've applied a credit of $20.00 to your account.")])
    approved = make_conv([OPEN] + DISPUTE + VERIFY + [("agent", "I've submitted a $120.00 credit request for supervisor approval.")])
    assert "R3_UNAUTHORIZED_CREDIT" not in ids(enrich, small, settings) | ids(enrich, approved, settings)


def test_r4_voice_without_recording_notice(enrich, settings):
    conv = baseline(lambda t: [("agent", "Hello, TelcoCare, how can I help?")] + t[1:])
    assert "R4_NO_RECORDING_NOTICE" in ids(enrich, conv, settings)


def test_r4_not_applicable_to_chat(enrich, settings):
    conv = baseline(lambda t: [("agent", "Hello, TelcoCare, how can I help?")] + t[1:], channel="CHAT_APP")
    assert "R4_NO_RECORDING_NOTICE" not in ids(enrich, conv, settings)


def test_r5_agent_reads_back_sensitive_identifier(enrich, settings):
    conv = baseline(lambda t: t[:4] + [("agent", "I have your PIN as 4821, is that right?")] + t[4:])
    assert "R5_SENSITIVE_READBACK" in ids(enrich, conv, settings)


def test_r6_misleading_guarantee(enrich, settings):
    conv = baseline(lambda t: t[:-1] + [("agent", "I guarantee this will never happen again.")])
    assert "R6_MISLEADING_GUARANTEE" in ids(enrich, conv, settings)


def test_r7_escalation_ignored_and_honoured(enrich, settings):
    ask = [OPEN, ("customer", "My phone has no service. This is the third time, I want to speak to a manager right now!"),
           ("agent", "Let me try restarting your device.")]
    assert "R7_ESCALATION_IGNORED" in ids(enrich, make_conv(ask), settings)
    ok = ask + [("agent", "I understand, I'll transfer you to a supervisor now.")]
    assert "R7_ESCALATION_IGNORED" not in ids(enrich, make_conv(ok), settings)


def test_r8_unprofessional_language(enrich, settings):
    conv = baseline(lambda t: t[:-1] + [("agent", "Calm down, that's not my problem.")])
    assert "R8_UNPROFESSIONAL_LANGUAGE" in ids(enrich, conv, settings)


def test_r9_port_out_obstruction(enrich, settings):
    conv = make_conv([OPEN, ("customer", "I want to cancel and port my number out to T-Mobile."),
                      ("agent", "I'm sorry, I can't port your number out today.")])
    assert "R9_PORT_OUT_OBSTRUCTION" in ids(enrich, conv, settings)


def test_r9_security_reason_is_not_obstruction(enrich, settings):
    conv = make_conv([OPEN, ("customer", "I want to cancel and port my number out to T-Mobile."),
                      ("agent", "I can't port your number until I verify your identity - what's the PIN?")])
    assert "R9_PORT_OUT_OBSTRUCTION" not in ids(enrich, conv, settings)


def test_r10_discount_above_authority(enrich, settings):
    conv = baseline(lambda t: t[:-1] + [("agent", "I can give you a 40% discount for the next year.")])
    v = [x for x in check_compliance(enrich(conv), settings).violations if x.rule_id.startswith("R10")]
    assert v and "40" in v[0].title


def test_r10_within_authority_ok(enrich, settings):
    conv = baseline(lambda t: t[:-1] + [("agent", "I can offer a 10% discount for six months.")])
    assert "R10_DISCOUNT_ABOVE_AUTHORITY" not in ids(enrich, conv, settings)


def test_violation_evidence_points_at_real_turns(enrich, settings):
    conv = baseline(lambda t: t[:-1] + [("agent", "I guarantee this will never happen again.")])
    enr = enrich(conv)
    n = len(enr.turns)
    for v in check_compliance(enr, settings).violations:
        assert all(0 <= i < n for i in v.turn_ids)
        for e in v.evidence:
            assert e.ref.startswith("turn:")


def test_score_penalises_severity():
    from convoiq.agents.compliance import SEV_WEIGHT
    from convoiq.contracts import Severity
    assert SEV_WEIGHT[Severity.HIGH] > SEV_WEIGHT[Severity.MEDIUM] > SEV_WEIGHT[Severity.LOW]
