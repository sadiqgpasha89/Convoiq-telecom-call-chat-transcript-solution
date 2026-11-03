"""Warehouse table definitions - single source of truth for SQLite DDL, BigQuery schemas and Terraform."""
from __future__ import annotations

# (name, type, mode)  types: STRING INTEGER FLOAT BOOLEAN TIMESTAMP
TABLES: dict[str, dict] = {
    "interactions_fact": {
        "partition": "ingested_at", "cluster": ["tenant_id", "issue_type", "region"],
        "columns": [
            ("interaction_id", "STRING", "REQUIRED"), ("tenant_id", "STRING", "REQUIRED"), ("customer_pseudo_id", "STRING", "NULLABLE"),
            ("channel", "STRING", "NULLABLE"), ("started_at", "TIMESTAMP", "NULLABLE"), ("ingested_at", "TIMESTAMP", "REQUIRED"),
            ("issue_type", "STRING", "NULLABLE"), ("issue_confidence", "FLOAT", "NULLABLE"), ("tier", "STRING", "NULLABLE"),
            ("sentiment_mean", "FLOAT", "NULLABLE"), ("sentiment_delta", "FLOAT", "NULLABLE"), ("frustration_index", "FLOAT", "NULLABLE"),
            ("sarcasm_score", "FLOAT", "NULLABLE"), ("resolution_risk", "FLOAT", "NULLABLE"), ("churn_signal_score", "FLOAT", "NULLABLE"),
            ("competitors", "STRING", "NULLABLE"), ("cell_tower_id", "STRING", "NULLABLE"), ("region", "STRING", "NULLABLE"),
            ("plan", "STRING", "NULLABLE"), ("n_turns", "INTEGER", "NULLABLE"), ("injection_score", "FLOAT", "NULLABLE"),
            ("top_root_cause", "STRING", "NULLABLE"), ("top_root_cause_prob", "FLOAT", "NULLABLE"), ("language", "STRING", "NULLABLE"),
            ("agent_id", "STRING", "NULLABLE"), ("model_versions", "STRING", "NULLABLE"), ("enrich_latency_ms", "FLOAT", "NULLABLE"),
        ]},
    "insights_fact": {
        "partition": "created_at", "cluster": ["tenant_id", "issue_type"],
        "columns": [
            ("interaction_id", "STRING", "REQUIRED"), ("tenant_id", "STRING", "REQUIRED"), ("customer_pseudo_id", "STRING", "NULLABLE"),
            ("created_at", "TIMESTAMP", "REQUIRED"), ("mode", "STRING", "NULLABLE"), ("tier", "STRING", "NULLABLE"), ("issue_type", "STRING", "NULLABLE"),
            ("summary", "STRING", "NULLABLE"), ("primary_root_cause", "STRING", "NULLABLE"), ("owner_domain", "STRING", "NULLABLE"),
            ("diagnosis_confidence", "FLOAT", "NULLABLE"), ("compliance_score", "FLOAT", "NULLABLE"), ("n_violations", "INTEGER", "NULLABLE"),
            ("churn_risk", "FLOAT", "NULLABLE"), ("churn_band", "STRING", "NULLABLE"), ("value_at_risk_usd", "FLOAT", "NULLABLE"),
            ("est_value_usd", "FLOAT", "NULLABLE"), ("faithfulness", "FLOAT", "NULLABLE"), ("degraded", "BOOLEAN", "NULLABLE"),
            ("blocked", "BOOLEAN", "NULLABLE"), ("n_actions", "INTEGER", "NULLABLE"), ("latency_ms", "FLOAT", "NULLABLE"),
            ("llm_calls", "INTEGER", "NULLABLE"), ("llm_cost_usd", "FLOAT", "NULLABLE"), ("analysis_json", "STRING", "NULLABLE"),
        ]},
    "compliance_violations_fact": {
        "partition": "created_at", "cluster": ["tenant_id", "rule_id"],
        "columns": [
            ("interaction_id", "STRING", "REQUIRED"), ("tenant_id", "STRING", "REQUIRED"), ("created_at", "TIMESTAMP", "REQUIRED"),
            ("rule_id", "STRING", "REQUIRED"), ("severity", "STRING", "NULLABLE"), ("systemic", "BOOLEAN", "NULLABLE"),
            ("turn_ids", "STRING", "NULLABLE"), ("agent_id", "STRING", "NULLABLE"), ("explanation", "STRING", "NULLABLE"),
        ]},
    "actions_audit": {   # append-only audit trail (spec 12.2)
        "partition": "ts", "cluster": ["tenant_id", "type", "status"],
        "columns": [
            ("action_id", "STRING", "REQUIRED"), ("interaction_id", "STRING", "REQUIRED"), ("tenant_id", "STRING", "REQUIRED"),
            ("ts", "TIMESTAMP", "REQUIRED"), ("type", "STRING", "NULLABLE"), ("status", "STRING", "NULLABLE"), ("params_json", "STRING", "NULLABLE"),
            ("amount_usd", "FLOAT", "NULLABLE"), ("risk", "STRING", "NULLABLE"), ("policy_decisions", "STRING", "NULLABLE"),
            ("approver", "STRING", "NULLABLE"), ("external_ref", "STRING", "NULLABLE"), ("message", "STRING", "NULLABLE"), ("attempts", "INTEGER", "NULLABLE"),
        ]},
    "network_incidents_fact": {
        "partition": "detected_at", "cluster": ["tenant_id", "cell_tower_id"],
        "columns": [
            ("incident_id", "STRING", "REQUIRED"), ("tenant_id", "STRING", "REQUIRED"), ("detected_at", "TIMESTAMP", "REQUIRED"),
            ("cell_tower_id", "STRING", "NULLABLE"), ("region", "STRING", "NULLABLE"), ("issue_type", "STRING", "NULLABLE"),
            ("window_start", "TIMESTAMP", "NULLABLE"), ("window_end", "TIMESTAMP", "NULLABLE"), ("n_interactions", "INTEGER", "NULLABLE"),
            ("expected_rate", "FLOAT", "NULLABLE"), ("observed_rate", "FLOAT", "NULLABLE"), ("zscore", "FLOAT", "NULLABLE"),
            ("status", "STRING", "NULLABLE"), ("ticket_ref", "STRING", "NULLABLE"),
        ]},
    "agent_qa_fact": {
        "partition": "created_at", "cluster": ["tenant_id", "agent_id"],
        "columns": [
            ("interaction_id", "STRING", "REQUIRED"), ("tenant_id", "STRING", "REQUIRED"), ("created_at", "TIMESTAMP", "REQUIRED"),
            ("agent_id", "STRING", "NULLABLE"), ("n_tool_calls", "INTEGER", "NULLABLE"), ("n_errors", "INTEGER", "NULLABLE"),
            ("redundant_calls", "INTEGER", "NULLABLE"), ("efficiency", "FLOAT", "NULLABLE"), ("policy_flags", "STRING", "NULLABLE"),
            ("compliance_score", "FLOAT", "NULLABLE"),
        ]},
    "drift_reports": {
        "partition": "created_at", "cluster": ["tenant_id", "metric"],
        "columns": [
            ("report_id", "STRING", "REQUIRED"), ("tenant_id", "STRING", "REQUIRED"), ("created_at", "TIMESTAMP", "REQUIRED"),
            ("window_start", "TIMESTAMP", "NULLABLE"), ("window_end", "TIMESTAMP", "NULLABLE"), ("metric", "STRING", "REQUIRED"),
            ("value", "FLOAT", "NULLABLE"), ("threshold_warn", "FLOAT", "NULLABLE"), ("threshold_alert", "FLOAT", "NULLABLE"),
            ("status", "STRING", "NULLABLE"), ("detail", "STRING", "NULLABLE"),
        ]},
    "hitl_reviews": {
        "partition": "created_at", "cluster": ["tenant_id", "status"],
        "columns": [
            ("review_id", "STRING", "REQUIRED"), ("interaction_id", "STRING", "REQUIRED"), ("tenant_id", "STRING", "REQUIRED"),
            ("created_at", "TIMESTAMP", "REQUIRED"), ("reason", "STRING", "NULLABLE"), ("status", "STRING", "NULLABLE"),
            ("reviewer", "STRING", "NULLABLE"), ("verdict_json", "STRING", "NULLABLE"), ("reviewed_at", "TIMESTAMP", "NULLABLE"),
        ]},
}

_SQLITE = {"STRING": "TEXT", "INTEGER": "INTEGER", "FLOAT": "REAL", "BOOLEAN": "INTEGER", "TIMESTAMP": "TEXT"}


def sqlite_ddl(table: str) -> str:
    cols = ", ".join(f"{n} {_SQLITE[t]}" for n, t, _ in TABLES[table]["columns"])
    return f"CREATE TABLE IF NOT EXISTS {table} ({cols})"


def bq_schema(table: str) -> list[dict]:
    return [{"name": n, "type": t, "mode": m} for n, t, m in TABLES[table]["columns"]]


def column_names(table: str) -> list[str]:
    return [n for n, _, _ in TABLES[table]["columns"]]
