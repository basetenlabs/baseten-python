# Baseten Python SDK

[![PyPI](https://img.shields.io/pypi/v/baseten.svg)](https://pypi.org/project/baseten/)

Python SDK for Baseten. See the [API documentation](https://basetenlabs.github.io/baseten-python/) and [usage](#usage) below.

⚠️ SDK may change in incompatible ways between releases until the SDK reaches 1.0.

## Install

```bash
pip install baseten
```

## Usage

Current SDK only has barebones client. Here is usage example of the barebones underlying client:

```python
from baseten.client import ManagementClient

with ManagementClient(api_key="my-api-key") as client:
    for model in client.api.get_models().models:
        print(model.name)
```

Or for async:

```python
from baseten.client import AsyncManagementClient

async with AsyncManagementClient(api_key="my-api-key") as client:
    for model in (await client.api.get_models()).models:
        print(model.name)
```

### Authenticating with workload identity federation

Instead of an API key, a workload can exchange its identity provider's token (for example a projected Kubernetes service account token) for a short-lived Baseten access token under a trust rule. The credentials exchange on first use, cache the access token, and replace it before it expires:

```python
from baseten.client import ManagementClient, WorkloadIdentityCredentials

credentials = WorkloadIdentityCredentials(
    trust_rule_id="my-trust-rule-id",
    identity_token_file="/var/run/secrets/tokens/baseten",
)
with ManagementClient(credentials=credentials) as client:
    ...
```

`AsyncWorkloadIdentityCredentials` is the same for async clients.

## Sandboxes

`baseten.sandbox` creates and works with sandboxes. HTTP/2 is used when the `h2` package is installed, which is strongly recommended:

```bash
pip install "baseten" "httpx[http2]"
```

```python
from baseten.sandbox import SandboxClient

with SandboxClient(api_key="my-api-key") as client:
    sandbox = client.create()
    try:
        sandbox.fs.write(path="/tmp/hello.txt", content="hello")
        result = sandbox.process.exec(
            command="cat /tmp/hello.txt", wait_for_completion=True
        )
        print(result.stdout)
    finally:
        client.delete(name=sandbox.name)
```

`AsyncSandboxClient` is the same for async code. Operations the SDK does not wrap are available on the generated clients, through `client.raw_api` for the control plane and `sandbox.raw_api` for a sandbox. See the [API documentation](https://basetenlabs.github.io/baseten-python/) for the rest.

## Upgrading from 0.8.2 and earlier

Version 0.9.0 is a rewrite and shares no API with the earlier `baseten` releases.
Code written against 0.8.2 or earlier will not work. Pin `baseten<0.9` to keep it.
