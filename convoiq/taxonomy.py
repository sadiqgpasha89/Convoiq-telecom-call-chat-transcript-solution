"""Telecom issue / root-cause taxonomy shared by every service, model and eval.

Two layers:

* ``IssueType``  - enterprise-level contact reason (what the customer called about).
* ``ROOT_CAUSES`` - concrete, actionable fault tags.  The first block is taken
  verbatim from the tau2-bench telecom domain (device / line / billing faults that
  were *injected* into each simulated conversation, i.e. real labels).  The second
  block extends the taxonomy to the enterprise use-cases in the ConvoIQ
  specification (billing leakage, churn, fraud) that tau2-bench does not cover.
"""
from __future__ import annotations

from enum import Enum


class IssueType(str, Enum):
    NO_SERVICE = "NO_SERVICE"                # tau2: service_issue
    MOBILE_DATA = "MOBILE_DATA"              # tau2: mobile_data_issue
    MMS = "MMS"                              # tau2: mms_issue
    BILLING_DISPUTE = "BILLING_DISPUTE"
    PAYMENT_SUSPENSION = "PAYMENT_SUSPENSION"  # overdue bill / suspended line
    PLAN_CHANGE = "PLAN_CHANGE"
    SIM_ESIM = "SIM_ESIM"
    CHURN_PORT_OUT = "CHURN_PORT_OUT"
    FRAUD_SOCIAL_ENG = "FRAUD_SOCIAL_ENG"
    ACCOUNT_INQUIRY = "ACCOUNT_INQUIRY"      # routine - Tier-2 fast path
    OTHER = "OTHER"


TAU2_CATEGORY_TO_ISSUE = {
    "service_issue": IssueType.NO_SERVICE,
    "mobile_data_issue": IssueType.MOBILE_DATA,
    "mms_issue": IssueType.MMS,
}

# Issue types that are cheap & routine -> Tier-2 (no agentic swarm).
ROUTINE_ISSUES = {IssueType.ACCOUNT_INQUIRY}

# tag -> (human label, owning domain)
ROOT_CAUSES: dict[str, tuple[str, str]] = {
    # --- tau2-bench telecom (real injected faults) -------------------------
    "airplane_mode_on": ("Airplane mode enabled", "DEVICE"),
    "data_mode_off": ("Mobile data switched off", "DEVICE"),
    "data_saver_mode_on": ("Data saver restricting throughput", "DEVICE"),
    "data_usage_exceeded": ("Plan data allowance exhausted", "BSS_PLAN"),
    "user_abroad_roaming_enabled_off": ("Roaming allowed on line but off on device", "DEVICE"),
    "user_abroad_roaming_disabled_on": ("Roaming not enabled on line (device on)", "BSS_PLAN"),
    "user_abroad_roaming_disabled_off": ("Roaming disabled on both line and device", "BSS_PLAN"),
    "bad_network_preference": ("Network mode preference too restrictive (e.g. 2G/3G only)", "DEVICE"),
    "bad_wifi_calling": ("Wi-Fi calling misconfigured (breaks MMS)", "DEVICE"),
    "break_apn_mms_setting": ("MMS APN/MMSC settings broken", "DEVICE"),
    "break_apn_settings": ("APN settings broken (no service/data)", "DEVICE"),
    "unseat_sim_card": ("SIM card not seated", "DEVICE"),
    "lock_sim_card_pin": ("SIM locked by PIN/PUK", "DEVICE"),
    "bad_vpn": ("VPN degrading connectivity", "DEVICE"),
    "break_app_sms_permission": ("Messaging app lacks SMS permission", "DEVICE"),
    "break_app_storage_permission": ("Messaging app lacks storage permission", "DEVICE"),
    "break_app_both_permissions": ("Messaging app lacks SMS + storage permissions", "DEVICE"),
    "overdue_bill_suspension": ("Line suspended - overdue bill", "BSS_BILLING"),
    "contract_end_suspension": ("Line suspended - contract ended", "BSS_BILLING"),
    # --- enterprise extensions (synthetic scenarios) -----------------------
    "billing_roaming_overcharge": ("Roaming overcharge / bill shock", "BSS_BILLING"),
    "billing_duplicate_charge": ("Duplicate charge", "BSS_BILLING"),
    "network_tower_outage": ("Cell-site / backhaul outage", "OSS_NETWORK"),
    "competitor_offer_churn": ("Competitor offer driving port-out", "COMMERCIAL"),
    "sim_swap_fraud": ("SIM-swap / social-engineering attempt", "SECURITY"),
}

TAU2_ROOT_CAUSES = [k for k in ROOT_CAUSES if k not in {
    "billing_roaming_overcharge", "billing_duplicate_charge", "network_tower_outage",
    "competitor_offer_churn", "sim_swap_fraud"}]

# Mapping IssueType -> default owning team (used by routing + analytics)
ISSUE_OWNER = {
    IssueType.NO_SERVICE: "TIER1_TECH_SUPPORT",
    IssueType.MOBILE_DATA: "TIER1_TECH_SUPPORT",
    IssueType.MMS: "TIER1_TECH_SUPPORT",
    IssueType.BILLING_DISPUTE: "BILLING_OPS",
    IssueType.PAYMENT_SUSPENSION: "COLLECTIONS",
    IssueType.PLAN_CHANGE: "SALES_RETENTION",
    IssueType.SIM_ESIM: "FRAUD_SECURITY",
    IssueType.CHURN_PORT_OUT: "SALES_RETENTION",
    IssueType.FRAUD_SOCIAL_ENG: "FRAUD_SECURITY",
    IssueType.ACCOUNT_INQUIRY: "SELF_SERVICE",
    IssueType.OTHER: "TIER1_TECH_SUPPORT",
}

COMPETITORS = ["verizon", "at&t", "att", "t-mobile", "tmobile", "sprint", "vodafone", "airtel", "jio",
               "o2", "ee", "orange", "three", "telstra", "optus", "boost", "mint mobile", "cricket",
               "metro", "visible", "xfinity mobile", "spectrum mobile"]

# Common English words that are also carrier brands - only treated as competitors in clear brand context.
COMPETITORS_AMBIGUOUS = {"three", "orange", "boost", "metro", "cricket", "visible", "ee", "o2"}
