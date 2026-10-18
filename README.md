# mon-ram

Reports a machine's RAM usage to Google Cloud Monitoring once a minute, as
the custom metrics `custom.googleapis.com/mon_ram/memory_used_percent` and
`custom.googleapis.com/mon_ram/memory_used_bytes`. It's built for Raspberry
Pis and other Linux machines outside GCP, and it works on macOS too.

```bash
uv sync
cp .env.example .env    # then point it at your key
uv run --env-file .env main.py
uv run pytest
```

- [How it works](docs/how-it-works.md): what gets measured, the metric
  layout, auth, errors, and how to change things.
- [Deploying with GCP](docs/gcp-deployment.md): service account and IAM,
  systemd and launchd setup, the dashboard, the alert policy, and upkeep.
