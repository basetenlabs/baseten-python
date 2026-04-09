# Baseten Python SDK

Python SDK for Baseten.

⚠️ Under active development. Nothing should be considered stable at this time.

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