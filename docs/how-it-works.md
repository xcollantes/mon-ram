# How mon-ram works

`main.py` is a small daemon. Once a minute it reads the machine's RAM usage
and writes it to Google Cloud Monitoring as two custom metrics. It has no
config file and no CLI flags. Everything comes from environment variables
that the Google auth library already understands.

```
psutil.virtual_memory()  ->  build_series()  ->  POST timeSeries.create
        (every 60 s)          (2 series)          (monitoring.googleapis.com)
```

## What gets measured

The reading comes from `psutil.virtual_memory()`:

| Metric | Type | Value |
|---|---|---|
| `custom.googleapis.com/mon_ram/memory_used_percent` | GAUGE, DOUBLE | `percent` |
| `custom.googleapis.com/mon_ram/memory_used_bytes` | GAUGE, INT64 | `total - available` |

"Used" here means **not available**, not "not free". Linux keeps spare RAM
full of page cache, so `free` is almost always low. `available` is what the
kernel could hand to a new program without swapping (`MemAvailable` in
`/proc/meminfo`). psutil computes `percent` from `available`, and the bytes
metric uses the same definition so the two always agree. On macOS, psutil
counts inactive plus free pages as available.

## Where the data lands

Each series is attached to a `generic_node` monitored resource. This is the
resource type Google intends for machines outside GCP, such as a Raspberry Pi
or an on-prem server.

| Label | Value |
|---|---|
| `project_id` | the GCP project the metrics are written to |
| `location` | `global` (a Pi has no GCP region) |
| `namespace` | `mon-ram` |
| `node_id` | the machine's hostname (`socket.gethostname()`) |

Every machine gets its own time series, keyed by `node_id`. **Give every
machine a unique hostname.** Two machines with the same name write into the
same series, and their points collide.

You don't have to create anything up front. Cloud Monitoring creates the
metric descriptors automatically the first time a point arrives.

## Authentication

At startup, `load_credentials()` loads `.env` with python-dotenv (copy
`.env.example` to start). Variables already set in the environment win
over `.env`.

1. `GOOGLE_APPLICATION_CREDENTIALS` points at a service account key file,
   which the script loads directly. Set it in `.env` for local runs; the
   Pi and Linux deployments set it in the service definition.
2. If that isn't set, it falls back to
   [Application Default Credentials](https://cloud.google.com/docs/authentication/application-default-credentials),
   such as your own `gcloud auth application-default login`, which is
   handy for a quick local run.

The project comes from the key file. To send metrics to a different project,
set `GOOGLE_CLOUD_PROJECT`. The script only requests the `monitoring.write`
scope, and the service account only needs `roles/monitoring.metricWriter`.

With a service account key, the script signs its own access token, a
self-signed JWT ([AIP-4111](https://google.aip.dev/auth/4111)), once an hour
using the key. It never calls Google's OAuth token endpoint, so a token
refresh needs no network and can't fail because of it. google-auth also
looks up the account's regional access boundary in a short background thread
about every 6 hours. That lookup never blocks a write.

## The loop and errors

- **At startup**, missing credentials or a missing project raise immediately
  and the process exits. This is fail-fast: systemd or launchd will log it and
  retry.
- **Inside the loop**, any network or auth error (Wi-Fi drop, DNS failure,
  token refresh failure) is logged and the loop keeps going. You lose that
  one point, nothing more. Each request times out after 10 seconds
  (`TIMEOUT_SECONDS`), so a dead network delays the next sample by 10
  seconds at most.
- **A rejected write** (HTTP 4xx/5xx) logs the status code and Google's error
  body. That body explains the problem, for example `Permission
  monitoring.timeSeries.create denied`.
- **Successful writes** are logged only when the state changes: once at
  startup (`write ok (42.1% used)`), and again after recovering from an
  error. A healthy machine writes about one log line a day, not 1,440.

Logs go to stderr, so they end up in `journalctl -u mon-ram` on Linux, or in
the log file set in the launchd plist on macOS.

## Resource use

The script runs as one long-lived, single-threaded process. Between samples
it sits in `time.sleep()`, so it uses no CPU and holds no locks. A sample
does three things:

1. It reads `/proc/meminfo` (on Linux) through psutil.
2. It builds a request of about 1 KB.
3. It sends one HTTPS POST. Google closes idle connections after about 60
   seconds, so at a 60-second interval each POST opens a new TLS connection.
   Keep-alive can't help at this interval.

| Resource | Use (measured on macOS) |
|---|---|
| Memory | about 42 MB peak RSS, almost all of it Python, TLS and google-auth |
| CPU between samples | none |
| Startup | about 0.1 s of CPU with precompiled `.pyc` files, about 0.4 s without |
| Network | about 1 request a minute, plus a boundary lookup every 6 hours |
| Disk | about 1 log line a day while healthy |

**Why a daemon and not a once-a-minute systemd timer?** A timer frees the
memory between runs. But every minute it would pay the full startup cost
(imports, key loading, signing a new token), which uses more CPU in total
than one process that sleeps.

On shutdown, SIGTERM kills the process immediately. It has no cleanup to do,
so it never delays a shutdown or a restart.

## Making changes

- **Sampling interval:** change `INTERVAL_SECONDS` in `main.py`. Cloud
  Monitoring accepts at most one point every 5 seconds per series, so stay at
  or above that. The cost scales linearly with the rate (see
  [gcp-deployment.md](gcp-deployment.md#cost)).
- **Adding a metric:** add another `series("name", {...})` line in
  `build_series()`. For swap, pass `psutil.swap_memory()` into
  `build_series()` as `swap`, then add
  `series("swap_used_percent", {"doubleValue": swap.percent})`. The new
  descriptor appears after the first write. Add an assertion to
  `tests/test_main.py` as well.
- **Changing a metric's type:** you can't change it in place. A descriptor
  keeps the value type from its first write. Delete the descriptor first (see
  [gcp-deployment.md](gcp-deployment.md#deleting-a-metric)), or pick a new
  name.
- **Naming nodes differently:** change `node_id` in `main()`, for example to
  read an environment variable instead of the hostname.

## Running and testing locally

```bash
uv sync
gcloud auth application-default login    # or set GOOGLE_APPLICATION_CREDENTIALS
GOOGLE_CLOUD_PROJECT=my-project uv run main.py
uv run pytest
```

With your own user credentials, you might get a 403 that mentions a quota
project. If so, run `gcloud auth application-default set-quota-project
my-project` once.

The test covers `build_series()`, the pure function that shapes the request
body. To check against GCP itself, follow the verification steps in
[gcp-deployment.md](gcp-deployment.md#3-verify-data-is-arriving).
