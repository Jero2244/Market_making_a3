"""Credential handling: values never appear in status output or reprs."""
import os
from dataclasses import dataclass, field
from pathlib import Path

KEYS = ("PPI_API_KEY", "PPI_API_SECRET", "PPI_AUTHORIZED_CLIENT", "PPI_CLIENT_KEY")

# Production application identifiers bundled by official ppi-client 1.3.0
# (ppi_client/ppi_api_client.py). These are not user account credentials.
SDK_CLIENT_DEFAULTS = {KEYS[2]: "API_CLI_PYTHON", KEYS[3]: "pp19PythonApp12"}


def read_env(path):
    values = {}
    if Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                value = value.strip()
                if value[:1] in ("'", '"') and value[-1:] == value[:1]:
                    value = value[1:-1]
                else:
                    value = value.split(" #", 1)[0].strip()
                values[key.strip()] = value
    return values


def status(value):
    if not value or not value.strip():
        return "missing"
    if value.lower().startswith(("your_", "placeholder", "replace", "changeme", "<")) or value.lower() in ("string", "xxx", "todo"):
        return "placeholder"
    return "present"


@dataclass(repr=False)
class Credentials:
    values: dict = field(repr=False)

    def __repr__(self):
        return "Credentials(<redacted>)"

    def statuses(self):
        return {key: status(self.values.get(key)) for key in KEYS}

    @property
    def ready(self):
        return all(v == "present" for v in self.statuses().values())


def load_credentials(path=".env", environ=None):
    local = read_env(path)
    env = os.environ if environ is None else environ
    values = {k: env.get(k, local.get(k, "")) for k in KEYS}
    for key, default in SDK_CLIENT_DEFAULTS.items():
        if not values[key].strip():
            values[key] = default
    return Credentials(values)


def ensure_env(path=".env", environ=None):
    """Append missing names only; reuse named environment values, never overwrite.

    Caller must ensure the destination is ignored by git. No search through unrelated
    files or credential stores is performed.
    """
    path = Path(path)
    local = read_env(path)
    env = os.environ if environ is None else environ
    additions = []
    for key in KEYS:
        if key not in local:
            value = env.get(key, "")
            # Disallow dotenv injection from environment values.
            if any(c in value for c in "\r\n\x00\"'\\"):
                value = ""
            additions.append(f'{key}="{value}"\n')
    if additions:
        original = path.read_bytes() if path.exists() else b""
        with path.open("ab") as out:
            if original and not original.endswith(b"\n"):
                out.write(b"\n")
            out.write(("\n# PPI production API credentials; fill locally, never commit.\n" + "".join(additions)).encode("utf-8"))
        try:
            path.chmod(0o600)
        except OSError:
            pass  # Windows ACLs must be reviewed locally.
    return load_credentials(path, environ).statuses()
