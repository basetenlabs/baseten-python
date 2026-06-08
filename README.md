# Baseten Python SDK

[![PyPI](https://img.shields.io/pypi/v/baseten.svg)](https://pypi.org/project/baseten/)

Python SDK for Baseten. See the [API documentation](https://basetenlabs.github.io/baseten-python/api/) and [usage](#usage) below.

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

## Upgrading from 0.8.2 and earlier

Version 0.9.0 is a rewrite and shares no API with the earlier `baseten` releases.
Code written against 0.8.2 or earlier will not work. Pin `baseten<0.9` to keep it.