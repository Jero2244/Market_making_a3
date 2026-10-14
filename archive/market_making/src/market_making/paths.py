"""Stable repository paths for the editable, local research installation."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPLAY_POLICY = PROJECT_ROOT / "config" / "replay_acceptance_policy.json"
DEMO_ORDER_LOCK = PROJECT_ROOT / ".demo_order_lock.json"
