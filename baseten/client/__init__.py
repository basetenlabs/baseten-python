"""Baseten client library.

Use :class:`ManagementClient` or :class:`AsyncManagementClient` for the
management API, and :class:`InferenceClient` or :class:`AsyncInferenceClient`
for the inference API.
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

__all__ = [
    "AsyncInferenceClient",
    "AsyncManagementClient",
    "InferenceClient",
    "InferenceClientOptions",
    "ManagementClient",
    "ManagementClientOptions",
]
