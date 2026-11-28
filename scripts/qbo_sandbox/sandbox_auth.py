"""Refresh one QuickBooks Online sandbox connection from an Azure Key Vault."""

import json
import re
import subprocess
from base64 import b64encode
from contextlib import contextmanager
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

APP_SECRET = "openpractice-qbo-sandbox-app"
GRANT_SECRET = "openpractice-qbo-sandbox-grant"
TOKEN_ENDPOINT = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
TOKEN = re.compile(r"[A-Za-z0-9._~+/=-]+")


class SandboxOAuthError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


@contextmanager
def connection_lock(directory):
    """Serialize use of this connection and its rotating refresh token."""
    try:
        import msvcrt
    except ImportError:
        raise SandboxOAuthError("The sandbox runner requires Windows.") from None

    with (directory / "sandbox-connection.lock").open("a+b") as handle:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise SandboxOAuthError("Another sandbox run is using this connection.") from None
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _exchange(client_id, client_secret, refresh_token):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", client_id) or not client_secret or not TOKEN.fullmatch(refresh_token):
        raise SandboxOAuthError("Check the Development app credential and sandbox Refresh Token.")
    basic = b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    request = Request(
        TOKEN_ENDPOINT,
        data=urlencode({"grant_type": "refresh_token", "refresh_token": refresh_token}).encode("ascii"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic}",
        },
        method="POST",
    )
    try:
        with build_opener(_NoRedirect).open(request, timeout=15) as response:
            payload = json.load(response)
    except HTTPError as error:
        with error:
            try:
                fault = json.load(error).get("error")
            except (AttributeError, ValueError):
                fault = None
        if fault == "invalid_grant":
            raise SandboxOAuthError("Intuit rejected the saved grant. Reauthorize the sandbox with the OpenPractice Development app.") from None
        if fault == "invalid_client":
            raise SandboxOAuthError("Intuit rejected the saved OpenPractice Development app credentials.") from None
        raise SandboxOAuthError(f"Intuit token refresh returned HTTP {error.code}.") from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise SandboxOAuthError("Intuit token refresh did not return a readable response.") from None

    if not isinstance(payload, dict) or str(payload.get("token_type", "")).casefold() != "bearer":
        raise SandboxOAuthError("Intuit returned an invalid token response.")
    access, refreshed = payload.get("access_token"), payload.get("refresh_token")
    if not all(isinstance(token, str) and TOKEN.fullmatch(token) for token in (access, refreshed)):
        raise SandboxOAuthError("Intuit did not return usable access and refresh tokens.")
    return access, refreshed


class SandboxVault:
    def __init__(self, vault_name, subscription, cli_python):
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,22}[a-z0-9]", vault_name):
            raise SandboxOAuthError("Provide a valid Azure Key Vault name.")
        self._origin = f"https://{vault_name}.vault.azure.net"
        cli_python = cli_python.resolve()
        try:
            result = subprocess.run(
                [str(cli_python), "-m", "azure.cli", "account", "get-access-token",
                 "--subscription", subscription, "--resource", "https://vault.azure.net",
                 "--query", "accessToken", "--output", "tsv", "--only-show-errors"],
                capture_output=True,
                encoding="utf-8",
                timeout=45,
                check=True,
                shell=False,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            raise SandboxOAuthError(
                "Could not get a Key Vault token. Verify the Azure CLI Python and its sign-in for the QA subscription."
            ) from None
        self._token = result.stdout.strip()
        if not TOKEN.fullmatch(self._token):
            raise SandboxOAuthError("Azure CLI did not return a usable Key Vault token.")
        self._opener = build_opener(_NoRedirect)

    def _request(self, name, method, data=None):
        request = Request(
            f"{self._origin}/secrets/{name}?api-version=2025-07-01",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            data=data,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=20) as response:
                return json.load(response)
        except HTTPError as error:
            error.close()
            if error.code == 404 and method == "GET":
                raise SandboxOAuthError(f"Add {name} in the QA vault before running the live test.") from None
            if error.code == 403:
                raise SandboxOAuthError(f"Key Vault denied {method} for {name}. Check QA vault permissions.") from None
            raise SandboxOAuthError(f"Key Vault returned HTTP {error.code} for {method} {name}.") from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise SandboxOAuthError(f"Key Vault did not return a readable response for {method} {name}.") from None

    def read(self, name, *fields):
        response = self._request(name, "GET")
        try:
            secret = json.loads(response["value"])
            values = tuple(secret[field] for field in fields)
            if all(isinstance(value, str) and value.strip() for value in values):
                return values
        except (KeyError, TypeError, ValueError):
            pass
        raise SandboxOAuthError(f"Check the JSON fields in Key Vault secret {name}.")

    def save_grant(self, realm, refresh_token):
        grant = {"realm_id": realm, "refresh_token": refresh_token}
        self._request(GRANT_SECRET, "PUT", json.dumps({
            "value": json.dumps(grant),
            "contentType": "application/json",
        }).encode("utf-8"))
        if self.read(GRANT_SECRET, "realm_id", "refresh_token") != (realm, refresh_token):
            raise SandboxOAuthError("Key Vault did not return the rotated sandbox grant.")


def get_sandbox_access(vault):
    """Return an access token in memory after persisting any rotated refresh token."""
    client_id, client_secret = vault.read(APP_SECRET, "client_id", "client_secret")
    realm, previous = vault.read(GRANT_SECRET, "realm_id", "refresh_token")
    if not re.fullmatch(r"[0-9]{1,32}", realm):
        raise SandboxOAuthError("Enter a numeric sandbox realm_id as a JSON string in the Key Vault grant.")
    access, refreshed = _exchange(client_id, client_secret, previous)
    if refreshed != previous:
        vault.save_grant(realm, refreshed)
    return realm, access
