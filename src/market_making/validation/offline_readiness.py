"""Authoritative repository readiness inventory; offline, no credentials or state.

This inventory is intentionally NOT a broker attestation or send authorization.
Exit 2 means BLOCKED. Implemented controls are not production readiness claims.
"""
import argparse
import json

BLOCKERS = (
    ("AUTHORITATIVE_BOOK_FRESHNESS", "missing_broker_evidence", "docs/FRESHNESS_SPEC_REQUEST.md"),
    ("BROKER_EXECUTION_SEQUENCE_AND_RECONNECT_CONTRACT", "missing_broker_evidence", "Offline normalized reducer has no verified Primary mapping."),
    ("COMPLETE_ACCOUNT_RECONCILIATION", "missing_broker_evidence", "Active/history/status reads cannot prove positions, balances or margin."),
    ("POSITIONS_BALANCES_MARGIN_ADAPTERS", "missing_code", "Typed synthetic evidence exists; verified broker endpoint adapters remain absent."),
    ("ACCOUNT_WIDE_COORDINATION", "missing_code", "Local demo lock/OS journal lease cannot coordinate other hosts, tools or human activity."),
    ("PRODUCTION_EXECUTION_LEDGER", "missing_code", "Offline fixture ledger is not a production durable broker authority."),
    ("PORTFOLIO_RISK_AND_LOSS_LIMITS", "missing_code", "Bounded single-instrument exact-decimal simulation is not a portfolio valuation/loss engine or approved margin methodology."),
    ("QUOTING_AND_PRODUCTION_ADAPTERS", "missing_code", "Passive synthetic proposals exist; no production routing or quoting adapter implemented."),
    ("RISK_POLICY_AND_ACCOUNT_AUTHORITY", "operational_approval", "External account authority and limits not approved."),
    ("INDEPENDENT_SAFETY_APPROVAL", "operational_approval", "No approval inferred from fixtures or successful tests."),
)

# Six versioned remediation contracts. Local validation is explicitly separate
# from broker evidence and operational approval, with no readiness inference.
CONTRACTS = (
    ("BOOK_FRESHNESS", "offline_contracts.Freshness", "Timestamp meaning, both-side coverage, clock uncertainty and reconnect sequencing", "Market-data authority signoff"),
    ("EXECUTION_LEDGER", "offline_coordinator.SimulationCoordinator", "Identifier mapping, reports, correction semantics and complete historical resolution", "Execution/recovery signoff"),
    ("ACCOUNT_RECONCILIATION", "offline_contracts.AccountEvidence", "Consistent positions/balances/margin/fills/metadata units, valuation and complete history", "Account and reconciliation authority"),
    ("ACCOUNT_COORDINATION", "offline_coordinator.SimulationCoordinator", "Shared deployment authority, external/manual activity and old dispatch isolation", "Deployment/fencing signoff"),
    ("RISK_AND_QUOTES", "offline_risk", "Broker margin and portfolio loss/valuation methodology, production adapters", "Approved independent risk limits"),
    ("CONFIG_AND_APPROVAL", "offline_risk.validate_approval", "Authenticated independent identity/signatures, configuration distribution and expiry", "Independent institutional safety approval"),
)


def inventory():
    return {"schema_version": 2, "status": "BLOCKED", "scope": "repository_offline_inventory",
            "network_used": False, "credentials_loaded": False, "state_created": False,
            "mutations_enabled": False,
            "implemented_controls": ["default_demo_disabled", "unconditional_freshness_send_block",
                "fixed_demo_host_tls_no_redirects_no_retries", "bounded_exact_duplicate_rejecting_rest_json",
                "secret_echo_withholding", "versioned_context_bound_demo_recovery",
                "cooperating_local_journal_writer_lease", "offline_normalized_execution_reducer",
                "versioned_synthetic_freshness_account_risk_contracts",
                "transactional_local_account_simulation_reservations_and_epoch_fencing"],
            "remediation_contracts": [{"code": code, "contract_version": 1,
                "status": "BLOCKED", "local_validation": {"implementation": implementation,
                    "scope": "synthetic_offline_only", "confers_authority": False},
                "external_evidence": {"status": "UNVERIFIED", "required": evidence},
                "operational_approval": {"status": "ABSENT", "required": approval}}
                for code, implementation, evidence, approval in CONTRACTS],
            "blockers": [{"code": code, "category": category, "detail": detail}
                         for code, category, detail in BLOCKERS]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    print(json.dumps(inventory(), sort_keys=True, indent=2))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
