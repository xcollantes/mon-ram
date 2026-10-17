"""Tests for the Cloud Monitoring payload built by main.py."""

from datetime import UTC, datetime
from types import SimpleNamespace

from main import build_series


def test_build_series_reports_percent_and_used_bytes():
    """A reading becomes two generic_node series with the right values."""
    mem = SimpleNamespace(percent=42.5, total=8_000, available=4_600)
    now = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)

    percent, used = build_series(mem, "my-proj", "pi-kitchen", now)

    assert percent["metric"]["type"] == (
        "custom.googleapis.com/mon_ram/memory_used_percent"
    )
    assert percent["points"][0]["value"] == {"doubleValue": 42.5}
    assert used["metric"]["type"] == (
        "custom.googleapis.com/mon_ram/memory_used_bytes"
    )
    assert used["points"][0]["value"] == {"int64Value": "3400"}
    assert used["points"][0]["interval"] == {
        "endTime": "2026-10-15T12:00:00+00:00"
    }
    assert percent["resource"] == {
        "type": "generic_node",
        "labels": {
            "project_id": "my-proj",
            "location": "global",
            "namespace": "mon-ram",
            "node_id": "pi-kitchen",
        },
    }
