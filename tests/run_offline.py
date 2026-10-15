"""Run entire fixture suite with actual sockets/HTTP and dotenv access forbidden."""
from pathlib import Path
import os
import socket
import sys
import unittest
from unittest.mock import patch
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
# Subprocess lease tests must import this checkout, not an unrelated editable
# installation. This does not read dotenv or grant network permission.
os.environ["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src") + os.pathsep + os.environ.get("PYTHONPATH", "")
original_read_text = Path.read_text


def no_dotenv(path, *args, **kwargs):
    if path.name == ".env":
        raise AssertionError("Credential file access forbidden in offline suite")
    return original_read_text(path, *args, **kwargs)


with patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")), \
     patch.object(socket.socket, "connect_ex", side_effect=AssertionError("Network forbidden")), \
     patch.object(socket, "create_connection", side_effect=AssertionError("Network forbidden")), \
     patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden")), \
     patch.object(requests.sessions.Session, "request", side_effect=AssertionError("HTTP forbidden")), \
     patch.object(Path, "read_text", no_dotenv):
    suite = unittest.defaultTestLoader.discover(str(Path(__file__).resolve().parent))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
