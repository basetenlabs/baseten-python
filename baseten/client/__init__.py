"""Baseten client library.

Use :class:`ManagementClient` or :class:`AsyncManagementClient` for the
management API, and :class:`InferenceClient` or :class:`AsyncInferenceClient`
for the inference API. :class:`SandboxClient` and :class:`AsyncSandboxClient`
reach one sandbox's execution API directly; the higher-level surface lives in
:mod:`baseten.sandbox`.

To authenticate without an API key, pass :class:`WorkloadIdentityCredentials`
or :class:`AsyncWorkloadIdentityCredentials` as a client's ``credentials``.
"""

from baseten.client._inference import (
    AsyncInferenceClient,
    InferenceClient,
    InferenceClientOptions,
)
from baseten.client._management import (
    AsyncManagementClient,
    ManagementClient,
    ManagementClientOptions,
)
from baseten.client._sandbox import (
    AsyncSandboxClient,
    SandboxClient,
    SandboxClientOptions,
)
from baseten.client._token_auth import (
    AccessToken,
    AccessTokenOptions,
    AsyncCredentials,
    Credentials,
)
from baseten.client._workload_identity import (
    AsyncIdentityTokenProvider,
    AsyncWorkloadIdentityCredentials,
    IdentityTokenProvider,
    IdentityTokenProviderContext,
    WorkloadIdentityCredentials,
)

__all__ = [
    "AccessToken",
    "AccessTokenOptions",
    "AsyncCredentials",
    "AsyncIdentityTokenProvider",
    "AsyncInferenceClient",
    "AsyncManagementClient",
    "AsyncSandboxClient",
    "AsyncWorkloadIdentityCredentials",
    "Credentials",
    "IdentityTokenProvider",
    "IdentityTokenProviderContext",
    "InferenceClient",
    "InferenceClientOptions",
    "ManagementClient",
    "ManagementClientOptions",
    "SandboxClient",
    "SandboxClientOptions",
    "WorkloadIdentityCredentials",
]
