"""A TokenCredential that asks the Azure CLI with the scope form.

Some Microsoft-account tenants intermittently reject the resource form
(``az account get-access-token --resource X/.default`` → AADSTS500011
"resource principal not found") while the scope form works. AzureCliCredential
in azure-identity hardcodes the resource form, so local development dies with
it. This credential shells out with ``--scope`` instead and is placed first
in the chain; when it fails, the standard DefaultAzureCredential still runs.
"""

from __future__ import annotations

import json
import subprocess

from azure.core.credentials import AccessToken

# azure-identity's abstract interface; importing it keeps this class
# duck-typed against the same contracts.
try:
    from azure.core.credentials import TokenCredential
except ImportError:  # pragma: no cover
    class TokenCredential:  # type: ignore[no-redef]
        pass

DEFAULT_SCOPE = "https://ai.azure.com/.default"


class ScopeCliCredential(TokenCredential):
    """Azure CLI tokens via the scope form; unavailable on any failure."""

    def __init__(self, scope: str = DEFAULT_SCOPE):
        self._scope = scope

    def get_token(self, *scopes: str, **kwargs) -> AccessToken:
        from azure.identity import CredentialUnavailableError

        target = scopes[0] if scopes else self._scope
        if not target.endswith("/.default"):
            target = f"{target.rstrip('/')}/.default"
        try:
            result = subprocess.run(
                ["az", "account", "get-access-token", "--scope", target,
                 "--output", "json"],
                capture_output=True, text=True, check=True, timeout=30)
        except (subprocess.SubprocessError, OSError):
            raise CredentialUnavailableError("scope_cli_failed") from None
        try:
            payload = json.loads(result.stdout)
            return AccessToken(payload["accessToken"], int(payload["expires_on"]))
        except (KeyError, ValueError, json.JSONDecodeError):
            raise CredentialUnavailableError("scope_cli_bad_output") from None


def chained_credential(scope: str = DEFAULT_SCOPE):
    """The scope-form CLI credential first, then the standard chain."""
    from azure.identity import ChainedTokenCredential, DefaultAzureCredential

    return ChainedTokenCredential(ScopeCliCredential(scope), DefaultAzureCredential())
