"""Fixed-host, endpoint-allowlisted REST transport with sanitized failures."""
import json
import time
from datetime import datetime, timedelta, timezone
import requests
from .config import KEYS
from .deadline import DeadlineTransport

BASE_URL = "https://clientapi.portfoliopersonal.com/api/1.0/"
ALLOWLIST = {
    "Account/LoginApi": "POST",
    "Account/RefreshToken": "POST",
    "Configuration/InstrumentTypes": "GET",
    "Configuration/Markets": "GET",
    "Configuration/Settlements": "GET",
    "MarketData/SearchInstrument": "GET",
    "MarketData/Book": "GET",
    "MarketData/Current": "GET",
}
PARAMETERS = {
    "MarketData/SearchInstrument": {"Ticker", "Name", "Market", "Type"},
    "MarketData/Book": {"Ticker", "Type", "Settlement"},
    "MarketData/Current": {"Ticker", "Type", "Settlement"},
}


class PPIError(Exception):
    """Only static codes, never server bodies, URLs, headers or exceptions."""


def safe_error_code(exc):
    """Defense in depth for reports: do not trust arbitrary exception text."""
    code = str(exc)
    if code in {"forbidden_endpoint", "forbidden_parameters", "live_opt_in_required",
                "credentials_unavailable", "sensitive_parameters_withheld", "request_budget_exhausted",
                "authentication_missing_or_expired", "response_limit_exceeded", "sensitive_response_withheld",
                "transport_or_json_failure", "invalid_authentication_response", "malformed_configuration",
                "malformed_instrument_search", "invalid_caucion_ticker"}:
        return code
    if code.startswith("http_failure_") and code[len("http_failure_"):] in {str(n) for n in range(100, 600)}:
        return code
    return "ppi_failure_details_withheld"


class Client:
    def __init__(self, credentials, *, live=False, session=None, max_requests=32):
        self.credentials = credentials
        self.live = live
        self.session = session if session is not None else requests.Session()
        self.session.trust_env = False  # No environment proxies or netrc credentials.
        self._transport = DeadlineTransport(self.session)
        self._token = None
        self._expiry = None
        self._refresh_token = None
        self.remaining = min(max_requests, 32)
        self.deadline = time.monotonic() + 120

    def close(self):
        self._token = None
        self._expiry = None
        self._refresh_token = None
        try:
            self.session.close()
        finally:
            self._transport.close()

    def cancel(self):
        self._transport.cancel()

    def begin_watch_cycle(self):
        """Renew the bounded budget, retaining authentication and pooled HTTP."""
        if self._transport.expired:
            raise PPIError("response_limit_exceeded")
        self.remaining = 4  # Initial/renew auth, quote, at most one 401 recovery.
        self.deadline = time.monotonic() + 120

    def ensure_authenticated(self):
        if not self._token or self._expiry <= datetime.now(timezone.utc) + timedelta(seconds=30):
            self.renew_authentication()

    def renew_authentication(self):
        if self._refresh_token:
            data = self.request("Account/RefreshToken", method="POST",
                                body={"refreshToken": self._refresh_token})
            self._accept_authentication(data)
        else:
            self.login()

    def request(self, endpoint, *, method="GET", params=None, body=None):
        if ALLOWLIST.get(endpoint) != method:
            raise PPIError("forbidden_endpoint")
        is_refresh = endpoint == "Account/RefreshToken"
        if ((is_refresh and (not self._refresh_token or body != {"refreshToken": self._refresh_token} or params is not None))
                or (not is_refresh and body is not None)):
            raise PPIError("forbidden_parameters")
        if params is not None and (not isinstance(params, dict) or
                                  not set(params).issubset(PARAMETERS.get(endpoint, set())) or
                                  any(not isinstance(v, str) or len(v) > 200 or any(ord(c) < 32 for c in v) for v in params.values())):
            raise PPIError("forbidden_parameters")
        if not self.live:
            raise PPIError("live_opt_in_required")
        if not self.credentials.ready:
            raise PPIError("credentials_unavailable")
        sensitive = list(self.credentials.values.values()) + [self._token, self._refresh_token]
        if params and any(secret and secret in value for secret in sensitive for value in params.values()):
            raise PPIError("sensitive_parameters_withheld")
        if self._transport.expired:
            raise PPIError("response_limit_exceeded")
        if self.remaining <= 0 or time.monotonic() >= self.deadline:
            raise PPIError("request_budget_exhausted")
        is_login = endpoint == "Account/LoginApi"
        is_auth = is_login or is_refresh
        if not is_auth and (not self._token or datetime.now(timezone.utc) >= self._expiry):
            raise PPIError("authentication_missing_or_expired")
        headers = {"Accept": "application/json", "AuthorizedClient": self.credentials.values[KEYS[2]],
                   "ClientKey": self.credentials.values[KEYS[3]]}
        if is_login:
            headers.update(ApiKey=self.credentials.values[KEYS[0]], ApiSecret=self.credentials.values[KEYS[1]])
        elif not is_refresh:
            headers["Authorization"] = "Bearer " + self._token
        self.remaining -= 1
        response = None
        bound = self._transport.bound(self.deadline, PPIError)
        bound.__enter__()
        try:
            remaining = max(.001, self.deadline - time.monotonic())
            extra = {"json": body} if is_refresh else {}
            response = self.session.request(method, BASE_URL + endpoint, params=params, headers=headers,
                                            timeout=(min(5, remaining), min(10, remaining)),
                                            allow_redirects=False, verify=True, stream=True, **extra)
            if response.status_code != 200:
                if response.status_code == 401:
                    self._token = None
                    self._expiry = None
                raise PPIError("http_failure_" + str(int(response.status_code)))
            chunks = []
            size = 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > 2_000_000 or time.monotonic() >= self.deadline:
                    raise PPIError("response_limit_exceeded")
                chunks.append(chunk)
            data = json.loads(b"".join(chunks))
            if not is_auth:
                # Fail closed on credential/token echoes anywhere in decoded JSON,
                # including escaped/nested strings. Never print the raw response.
                pending = [data]
                while pending:
                    value = pending.pop()
                    if isinstance(value, dict):
                        pending.extend(value.keys())
                        pending.extend(value.values())
                    elif isinstance(value, list):
                        pending.extend(value)
                    elif any(secret and secret in str(value) for secret in sensitive):
                        raise PPIError("sensitive_response_withheld")
            return data
        except PPIError:
            raise
        except Exception:
            raise PPIError("transport_or_json_failure") from None
        finally:
            try:
                if response is not None:
                    response.close()
            finally:
                bound.__exit__(None, None, None)

    def login(self):
        self._token = None
        self._expiry = None
        self._refresh_token = None
        data = self.request("Account/LoginApi", method="POST")
        self._accept_authentication(data)

    def _accept_authentication(self, data):
        if isinstance(data, list) and len(data) == 1:
            data = data[0]
        try:
            token = data["accessToken"]
            refresh = data.get("refreshToken")
            expiry = datetime.fromisoformat(data["expirationDate"].replace("Z", "+00:00"))
            if not isinstance(token, str) or not token or len(token) > 8192 or any(c.isspace() or ord(c) < 32 for c in token):
                raise ValueError
            if refresh is not None and (not isinstance(refresh, str) or not refresh or len(refresh) > 8192
                                        or any(c.isspace() or ord(c) < 32 for c in refresh)):
                raise ValueError
            if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc) + timedelta(seconds=5):
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            raise PPIError("invalid_authentication_response") from None
        self._token, self._expiry = token, expiry
        self._refresh_token = refresh

    def get(self, endpoint, params=None):
        return self.request(endpoint, params=params)
