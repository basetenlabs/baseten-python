"""Parity between the curated surface and the generated models it wraps.

The surface never exposes generated types: methods take keyword arguments of
primitives and curated dataclasses, and the generated models are constructed
internally. These tests pin that translation, so a spec field added upstream
fails here until the surface either forwards it or lists it under
INTENTIONALLY_NOT_TRANSLATED_*.
"""

from __future__ import annotations

import baseten.client.managementapi
import baseten.client.sandboxapi

# Control-plane fields left to the raw client.
INTENTIONALLY_NOT_TRANSLATED_SANDBOX = {"lifecycle", "network", "ports"}

INTENTIONALLY_NOT_TRANSLATED_PROCESS = {
    "keepAlive",
    "maxRestarts",
    "restartOnFailure",
    "stdin",
    "waitForPorts",
}

# Deliberately matching the JS surface's process record.
INTENTIONALLY_NOT_TRANSLATED_PROCESS_RECORD = {
    "keepAlive",
    "maxRestarts",
    "restartCount",
    "restartOnFailure",
    "stdin",
}

# File-write fields left to the raw client.
INTENTIONALLY_NOT_TRANSLATED_FILE = {"isDirectory", "permissions"}


def test_create_forwards_every_translated_field() -> None:
    forwarded = {
        "name",
        "image",
        "memory",
        "region",
        "envs",
        "labels",
        "display_name",
        "external_id",
    }
    fields = set(baseten.client.managementapi.CreateSandboxRequest.model_fields)
    assert forwarded <= fields
    assert fields - forwarded == INTENTIONALLY_NOT_TRANSLATED_SANDBOX


def test_record_translation_covers_every_translated_field() -> None:
    translated = {
        "name",
        "url",
        "status",
        "state",
        "image",
        "memory",
        "region",
        "enabled",
        "envs",
        "labels",
        "display_name",
        "external_id",
        "created_at",
        "updated_at",
        "created_by",
        "updated_by",
        "last_used_at",
        "expires_in",
    }
    fields = set(baseten.client.managementapi.Sandbox.model_fields)
    assert translated <= fields
    assert fields - translated == INTENTIONALLY_NOT_TRANSLATED_SANDBOX


def test_exec_forwards_every_translated_field() -> None:
    forwarded = {
        "command",
        "workingDir",
        "env",
        "name",
        "timeout",
        "waitForCompletion",
    }
    fields = set(baseten.client.sandboxapi.ProcessRequest.model_fields)
    assert forwarded <= fields
    assert fields - forwarded == INTENTIONALLY_NOT_TRANSLATED_PROCESS


def test_process_record_translation_covers_every_translated_field() -> None:
    translated = {
        "command",
        "completedAt",
        "exitCode",
        "logs",
        "name",
        "pid",
        "startedAt",
        "status",
        "stderr",
        "stdout",
        "workingDir",
    }
    fields = set(baseten.client.sandboxapi.ProcessResponse.model_fields)
    assert translated <= fields
    assert fields - translated == INTENTIONALLY_NOT_TRANSLATED_PROCESS_RECORD


def test_fs_write_forwards_every_translated_field() -> None:
    forwarded = {"content"}
    fields = set(baseten.client.sandboxapi.FileRequest.model_fields)
    assert forwarded <= fields
    assert fields - forwarded == INTENTIONALLY_NOT_TRANSLATED_FILE
