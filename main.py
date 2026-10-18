"""Report this machine's RAM usage to Google Cloud Monitoring."""

import json
import logging
import os
import socket
import time
from datetime import UTC, datetime
from typing import Any

import google.auth
import psutil
from dotenv import load_dotenv
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.urllib3 import AuthorizedHttp
from google.oauth2 import service_account
from urllib3.exceptions import HTTPError

INTERVAL_SECONDS = 60
TIMEOUT_SECONDS = 10
METRIC_PREFIX = "custom.googleapis.com/mon_ram/"
API_URL = "https://monitoring.googleapis.com/v3/projects/{}/timeSeries"
SCOPES = ["https://www.googleapis.com/auth/monitoring.write"]

log = logging.getLogger("mon-ram")


def build_series(
    mem: Any, project_id: str, node_id: str, now: datetime
) -> list[dict]:
    """
    Build the Cloud Monitoring time series for one RAM reading.

    Args:
        mem: A psutil.virtual_memory() result (percent, total, available)
        project_id: GCP project that receives the metrics
        node_id: Name of this machine, used as the generic_node node_id
        now: Timestamp of the reading

    Returns:
        TimeSeries dicts ready for the timeSeries.create request body
    """
    resource = {
        "type": "generic_node",
        "labels": {
            "project_id": project_id,
            "location": "global",
            "namespace": "mon-ram",
            "node_id": node_id,
        },
    }
    interval = {"endTime": now.isoformat()}

    def series(name: str, value: dict) -> dict:
        return {
            "metric": {"type": METRIC_PREFIX + name},
            "resource": resource,
            "points": [{"interval": interval, "value": value}],
        }

    return [
        series("memory_used_percent", {"doubleValue": mem.percent}),
        # Self-signed JWT is minted locally, so there is no
        # hourly round trip to Google's OAuth token endpoint.
        series(
            "memory_used_bytes",
            {"int64Value": str(mem.total - mem.available)},
        ),
    ]


def load_credentials() -> tuple[Any, str | None]:
    """
    Load GCP credentials, reading GOOGLE_APPLICATION_CREDENTIALS from .env.

    Variables already set in the environment win over .env, so systemd
    and launchd settings keep working.

    Returns:
        The scoped credentials and the GCP project ID, if one is known
    """
    load_dotenv()
    key_file = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if key_file:
        credentials, project_id = google.auth.load_credentials_from_file(
            key_file, scopes=SCOPES
        )
    else:
        # No key file, so fall back to gcloud user credentials.
        credentials, project_id = google.auth.default(scopes=SCOPES)
    return credentials, os.getenv("GOOGLE_CLOUD_PROJECT") or project_id


def main() -> None:
    """Send a RAM reading every INTERVAL_SECONDS until stopped."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s: %(name)s: %(levelname)s: %(message)s",
    )
    credentials, project_id = load_credentials()
    if not project_id:
        raise SystemExit(
            "No GCP project found: set GOOGLE_CLOUD_PROJECT or use a "
            "service account key via GOOGLE_APPLICATION_CREDENTIALS."
        )
    if isinstance(credentials, service_account.Credentials):
        # Self-signed JWT is minted locally, so there is no
        # hourly round trip to Google's OAuth token endpoint.
        credentials = credentials.with_always_use_jwt_access(True)
    http = AuthorizedHttp(credentials)
    url = API_URL.format(project_id)
    headers = {"Content-Type": "application/json"}
    node_id = socket.gethostname()
    log.info("reporting %s to project %s", node_id, project_id)

    healthy = False  # Log state changes only, not every write.
    while True:
        mem = psutil.virtual_memory()
        series = build_series(mem, project_id, node_id, datetime.now(UTC))
        body = json.dumps({"timeSeries": series}).encode()
        try:
            resp = http.urlopen(
                "POST", url, body=body, headers=headers,
                timeout=TIMEOUT_SECONDS,
            )
            if resp.status >= 300:
                healthy = False
                log.error(
                    "write failed %s: %s",
                    resp.status, resp.data.decode(errors="replace"),
                )
            elif not healthy:
                healthy = True
                log.info("write ok (%.1f%% used)", mem.percent)
        except (HTTPError, GoogleAuthError) as e:
            healthy = False
            log.error("write failed: %s", e)
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
