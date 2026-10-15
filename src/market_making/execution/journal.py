"""Versioned demo recovery context and process-local filesystem lease.

This does not coordinate other tools, hosts, or human account activity.
Legacy journals are evidence, not automatically authorized recovery intents.
"""
from contextlib import contextmanager
import os
from pathlib import Path
from market_making.http_policy import HOST, PrimaryError, strict_json, MAX_JSON_BYTES

VERSION = 1


def context(account_tag, folder):
    return {"environment": "REMARKETS", "host": HOST, "market": "ROFX",
            "account_tag": account_tag, "evidence_dir": str(Path(folder).resolve())}


def read_journal(path):
    path = Path(path)
    if path.is_symlink():
        raise PrimaryError("Journal symlink rejected.")
    with path.open("rb") as source:
        value = strict_json(source.read(MAX_JSON_BYTES + 1))
    if not isinstance(value, dict):
        raise PrimaryError("Journal must be an object.")
    return value


def validate_recovery(state, expected):
    from market_making.execution.demo_execution import identifier
    from market_making.market_data.instrument_rules import number
    if (type(state.get("schema_version")) is not int or state["schema_version"] != VERSION
            or state.get("context") != expected or state.get("phase") != "acknowledged"):
        raise PrimaryError("Recovery version/context/phase unverified; retain evidence.")
    if (state.get("symbol") != "RFX20/OCT26" or state.get("side") != "BUY"
            or type(state.get("qty")) is not int or state["qty"] != 1
            or state.get("ordType") != "LIMIT" or state.get("timeInForce") != "DAY"
            or number(state.get("price")) <= 0 or number(state["price"]) % 100):
        raise PrimaryError("Recovery intent invalid.")
    ids = state.get("ids")
    if not isinstance(ids, dict) or set(ids) != {"clOrdId", "proprietary"}:
        raise PrimaryError("Unknown submit: manual reconciliation required; never resubmit.")
    return {k: identifier(v) for k, v in ids.items()}


def release_owned_lock(path, expected):
    """Require unchanged ownership evidence; never delete an unknown/stale lock.

    Call only under the cooperating journal lease after proven no-send/terminal.
    Uncooperative external writers remain an account-wide coordination blocker.
    """
    if read_journal(path) != expected:
        raise PrimaryError("Lock ownership changed; retained for manual reconciliation.")
    Path(path).unlink()


@contextmanager
def journal_lease(folder):
    """OS lock released on crash. Never remove/truncate the persistent guard."""
    path = Path(folder) / "journal.guard"
    if path.is_symlink():
        raise PrimaryError("Journal guard symlink rejected.")
    output = path.open("a+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                output.write(b"0")
                output.flush()
            output.seek(0)
            msvcrt.locking(output.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(output.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        yield
    except OSError:
        raise PrimaryError("Journal lease/storage unavailable; no concurrent recovery allowed.") from None
    finally:
        if locked:
            if os.name == "nt":
                output.seek(0)
                msvcrt.locking(output.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(output.fileno(), fcntl.LOCK_UN)
        output.close()
