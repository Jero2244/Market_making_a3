"""Pure exact-decimal synthetic risk proposals; no production transport."""
from dataclasses import asdict, dataclass, fields
from decimal import Context, localcontext

from market_making.execution.offline_contracts import Identity, decimal, digest, require


@dataclass(frozen=True)
class Policy:
    version: int
    identity: object
    expires_utc: int
    max_quantity: str
    max_notional: str
    max_gross: str
    max_net: str
    max_inventory: str
    max_margin: str
    max_loss: str
    max_drawdown: str
    max_attempts: int
    rate_window_ns: int
    max_age_ns: int
    kill_switch: bool
    production_enabled: bool

    def validate(self, identity, now):
        self.identity.validate()
        now.validate()
        require(type(self.version) is int and self.version == 1 and self.identity == identity,
                "Policy identity/version mismatch")
        require(type(self.expires_utc) is int and now.utc + now.uncertainty < self.expires_utc,
                "Policy expired")
        require(self.production_enabled is False and self.kill_switch is False,
                "Production prohibited / kill switch")
        for name in ("max_quantity", "max_notional", "max_gross", "max_net", "max_inventory",
                     "max_margin", "max_loss", "max_drawdown"):
            require(decimal(getattr(self, name)) > 0, "Invalid risk limit")
        require(type(self.max_attempts) is int and self.max_attempts > 0
                and type(self.rate_window_ns) is int and self.rate_window_ns > 0
                and type(self.max_age_ns) is int and self.max_age_ns > 0, "Invalid rate/freshness policy")


def policy_from_mapping(value, identity, now):
    """Strict versioned config input; unknown/missing fields cannot be ignored."""
    require(isinstance(value, dict) and set(value) == {field.name for field in fields(Policy)},
            "Policy schema missing/altered")
    raw_identity = value["identity"]
    require(isinstance(raw_identity, dict) and set(raw_identity) ==
            {field.name for field in fields(Identity)}, "Policy identity schema invalid")
    result = Policy(**{**value, "identity": Identity(**raw_identity)})
    result.validate(identity, now)
    return result


def validate_approval(approval, policy, now):
    """Fixture authorization only. Independent identity is NOT a verified signature."""
    fields = {"version", "scope", "policy_hash", "expires_utc", "requester", "reviewer",
              "identity", "production_enabled"}
    require(isinstance(approval, dict) and set(approval) == fields, "Approval schema missing/altered")
    require(type(approval["version"]) is int and approval["version"] == 1
            and approval["scope"] == "offline_simulation"
            and approval["production_enabled"] is False
            and approval["identity"] == asdict(policy.identity)
            and approval["policy_hash"] == digest(asdict(policy)), "Approval scope/hash mismatch")
    require(all(isinstance(approval[k], str) and approval[k] for k in ("requester", "reviewer"))
            and approval["requester"] != approval["reviewer"], "Independent reviewer identity required")
    require(type(approval["expires_utc"]) is int
            and now.utc + now.uncertainty < approval["expires_utc"] <= policy.expires_utc,
            "Approval expired")


@dataclass(frozen=True)
class Proposal:
    identity: object
    book_hash: str
    account_hash: str
    metadata_hash: str
    policy_hash: str
    approval_hash: str
    side: str
    quantity: str
    price: str
    expires_utc: int
    schema_version: int = 1


def propose(book, evidence, policy, approval, now, side, quantity, price, expires_utc):
    policy.validate(evidence.identity, now)
    validate_approval(approval, policy, now)
    book.validate(evidence.identity, now, policy.max_age_ns)
    evidence.validate(evidence.identity, now, policy.max_age_ns)
    require(side in {"BUY", "SELL"} and type(expires_utc) is int
            and now.utc + now.uncertainty < expires_utc <= min(policy.expires_utc, approval["expires_utc"]),
            "Invalid proposal expiry/side")
    proposal = Proposal(evidence.identity, digest(asdict(book)), digest(asdict(evidence)),
                        digest(asdict(evidence.metadata)), digest(asdict(policy)), digest(approval),
                        side, quantity, price, expires_utc)
    check(proposal, book, evidence, policy, approval, now, (), 0)
    return proposal


def check(proposal, book, evidence, policy, approval, now, reservations, recent_attempts):
    """Conservative no-netting envelope includes both pending and uncertain exposure.

    All buys/sells consume cash and margin. This intentionally over-reserves sells;
    fixture limits are not a broker-approved derivatives margin methodology.
    """
    require(type(proposal.schema_version) is int and proposal.schema_version == 1,
            "Proposal contract version")
    policy.validate(evidence.identity, now)
    validate_approval(approval, policy, now)
    book.validate(evidence.identity, now, policy.max_age_ns)
    evidence.validate(evidence.identity, now, policy.max_age_ns)
    require(proposal.identity == evidence.identity and proposal.book_hash == digest(asdict(book))
            and proposal.account_hash == digest(asdict(evidence))
            and proposal.metadata_hash == digest(asdict(evidence.metadata))
            and proposal.policy_hash == digest(asdict(policy))
            and proposal.approval_hash == digest(approval), "Proposal evidence binding changed")
    require(type(proposal.expires_utc) is int and now.utc + now.uncertainty < proposal.expires_utc
            <= min(policy.expires_utc, approval["expires_utc"]), "Proposal expired")
    require(proposal.side in {"BUY", "SELL"} and recent_attempts < policy.max_attempts,
            "Side/rate limit")
    with localcontext(Context(prec=128)):
        metadata = evidence.metadata
        qty, px = decimal(proposal.quantity), decimal(proposal.price)
        require(0 < qty <= decimal(policy.max_quantity) and qty % decimal(metadata.lot) == 0,
                "Quantity/lot violation")
        require(decimal(metadata.low) <= px <= decimal(metadata.high)
                and px % decimal(metadata.tick) == 0, "Price/tick violation")
        # Passive proposals only; synthetic quotes cannot cross the opposing side.
        require(px < decimal(book.ask) if proposal.side == "BUY" else px > decimal(book.bid),
                "Crossing quote prohibited")
        multiplier = decimal(metadata.multiplier)
        require(qty * px * multiplier <= decimal(policy.max_notional), "Order notional limit")
        buys = sells = cash = margin = pending_notional = decimal("0")
        orders = [(o.side, o.quantity, o.price) for o in evidence.working] + list(reservations)
        orders.append((proposal.side, proposal.quantity, proposal.price))
        for side, quantity, price in orders:
            require(side in {"BUY", "SELL"}, "Unknown reservation side")
            q, p = decimal(quantity), decimal(price)
            require(q > 0 and p > 0, "Invalid reservation")
            if side == "BUY":
                buys += q
            else:
                sells += q
            cash += q * p * multiplier
            margin += q * decimal(metadata.margin_per_contract)
            pending_notional += q * max(p, decimal(evidence.valuation)) * multiplier
        inventory = decimal(evidence.position, True)
        worst_inventory = max(abs(inventory + buys), abs(inventory - sells))
        gross = abs(inventory) * decimal(evidence.valuation) * multiplier + pending_notional
        net = worst_inventory * decimal(evidence.valuation) * multiplier
        require(gross <= decimal(policy.max_gross) and net <= decimal(policy.max_net)
                and worst_inventory <= decimal(policy.max_inventory), "Portfolio exposure limit")
        require(margin <= min(decimal(policy.max_margin), decimal(evidence.margin_available))
                and cash <= decimal(evidence.cash), "Margin/cash limit")
        require(decimal(evidence.loss) < decimal(policy.max_loss)
                and decimal(evidence.drawdown) < decimal(policy.max_drawdown), "Loss/drawdown limit")
        return {"gross": str(gross), "net": str(net), "inventory": str(worst_inventory),
                "margin": str(margin), "cash_reserved": str(cash)}
