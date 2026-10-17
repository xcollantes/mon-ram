# Deploying mon-ram with Google Cloud

This guide covers:

1. One-time setup in your GCP project: the API, a service account and a key.
2. Installing on a Raspberry Pi or Linux machine (systemd), or on a Mac
   (launchd).
3. Checking that data arrives, then adding a dashboard and an alert.

Run the `gcloud` commands on your workstation. Run the install commands on
the machine you want to monitor.

```bash
export PROJECT=my-gcp-project
export SA=mon-ram@$PROJECT.iam.gserviceaccount.com
gcloud config set project $PROJECT
```

## 1. GCP setup (once per project)

```bash
# Enable the Cloud Monitoring API.
gcloud services enable monitoring.googleapis.com

# Create a service account that can only write metrics.
gcloud iam service-accounts create mon-ram \
  --display-name="mon-ram RAM reporter"
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:$SA" \
  --role="roles/monitoring.metricWriter"
```

`roles/monitoring.metricWriter` can write data points and nothing else. It
can't read your metrics or touch any other resource. So if a key leaks from
a Pi, the most an attacker can do is write junk metrics.

### Create a key (once per machine)

```bash
gcloud iam service-accounts keys create pi-kitchen-key.json \
  --iam-account="$SA"
chmod 600 pi-kitchen-key.json
```

- Use **one key per machine**. Then you can revoke one machine without
  touching the others.
- `.gitignore` excludes `*-key.json` and `key.json`. Never commit a key.
- If this fails with `constraints/iam.disableServiceAccountKeyCreation`,
  your organization blocks key creation. This is the default for newer
  organizations. An Org Policy Admin can exempt the project. The other
  option is
  [Workload Identity Federation](https://cloud.google.com/iam/docs/workload-identity-federation),
  which avoids keys but is much more setup for a home Pi.

## 2a. Install on a Raspberry Pi or Linux machine

The Pi needs Python 3.11 or newer. Raspberry Pi OS Bookworm ships 3.11, and
Trixie ships 3.13.

**On the Pi**, install uv and make a home for the code:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh    # then open a new shell
sudo mkdir -p /opt/mon-ram && sudo chown "$USER" /opt/mon-ram
sudo useradd --system --no-create-home --shell /usr/sbin/nologin mon-ram
```

**From your workstation**, copy the code and the key over (repo root shown):

```bash
rsync -a --exclude .venv --exclude .git ./ pi@pi-kitchen.local:/opt/mon-ram/
scp pi-kitchen-key.json pi@pi-kitchen.local:/tmp/
```

**On the Pi**, install the dependencies, the key and the service:

```bash
cd /opt/mon-ram
# --python /usr/bin/python3: the venv uses the system Python, so the
#   mon-ram user can run it (a uv-managed Python would live in your $HOME).
# --compile-bytecode: precompiles .pyc files. mon-ram can't write them into
#   the venv, so without this every start recompiles the libraries (about
#   4x the startup CPU).
uv sync --no-dev --compile-bytecode --python /usr/bin/python3

sudo install -D -o root -g mon-ram -m 640 /tmp/pi-kitchen-key.json \
  /etc/mon-ram/key.json
rm /tmp/pi-kitchen-key.json

sudo tee /etc/systemd/system/mon-ram.service >/dev/null <<'EOF'
[Unit]
Description=Report RAM usage to Google Cloud Monitoring
# Ordering only, no Wants=: never holds up boot waiting for the network.
After=network-online.target

[Service]
User=mon-ram
Environment=GOOGLE_APPLICATION_CREDENTIALS=/etc/mon-ram/key.json
ExecStart=/opt/mon-ram/.venv/bin/python /opt/mon-ram/main.py
Restart=always
RestartSec=30
# Yield CPU and disk to everything else, and cap memory in case of a leak.
Nice=19
IOSchedulingClass=idle
MemoryMax=100M

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now mon-ram
journalctl -u mon-ram -f     # expect one "write ok", then only errors
```

How the unit stays out of the way:

- **No `Wants=network-online.target`.** That line would pull in the
  wait-online service, which can delay boot. Without it, if the network is
  down at startup the first write fails, the failure is logged, and the next
  minute retries.
- **`Nice=19` and `IOSchedulingClass=idle`.** These give it the lowest CPU
  and disk priority. I didn't use `CPUSchedulingPolicy=idle`, because that
  would stop it completely under heavy load, which is exactly when you want
  the RAM metric.
- **`MemoryMax=100M`.** This guards against a leak; the script normally uses
  about 40 MB. If systemd warns that the memory controller is unavailable,
  your Pi kernel has the memory cgroup disabled and the limit is ignored.
  Nothing else changes.

**32-bit Raspberry Pi OS:** PyPI has no armv7 wheels for `psutil` or `cffi`,
so `uv sync` compiles them. That takes about a minute and needs
`sudo apt install gcc python3-dev libffi-dev` first. 64-bit Pi OS doesn't
need this step.

**Updating the code:** rsync again, run `uv sync --no-dev --compile-bytecode
--python /usr/bin/python3` if the dependencies changed, then run
`sudo systemctl restart mon-ram`.

## 2b. Install on macOS (launchd)

From the repo root on the Mac:

```bash
uv sync --no-dev
mkdir -p ~/.config/mon-ram
mv mac-key.json ~/.config/mon-ram/key.json && chmod 600 ~/.config/mon-ram/key.json

REPO=$(pwd)
cat > ~/Library/LaunchAgents/com.mon-ram.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.mon-ram</string>
  <key>ProgramArguments</key>
  <array>
    <string>$REPO/.venv/bin/python</string>
    <string>$REPO/main.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>GOOGLE_APPLICATION_CREDENTIALS</key>
    <string>$HOME/.config/mon-ram/key.json</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/mon-ram.log</string>
</dict>
</plist>
EOF

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mon-ram.plist
tail -f ~/Library/Logs/mon-ram.log
```

This runs as a per-user agent. It starts when you log in and restarts if it
crashes. `ProcessType=Background` tells macOS to throttle its CPU and disk
use so it never competes with your apps. The log gets one `write ok` line,
then only errors. To stop it, run
`launchctl bootout gui/$(id -u)/com.mon-ram`. After you edit the code or the
plist, run `launchctl kickstart -k gui/$(id -u)/com.mon-ram`.

## 3. Verify data is arriving

**Console:** go to **Monitoring → Metrics Explorer → Select a metric**. Turn
off "Active" if the metric doesn't show up yet. Then pick **Generic Node →
Custom → `custom/mon_ram/memory_used_percent`**. You'll see one line per
`node_id`. New points can take a minute or two to appear.

**CLI:** this lists the last 10 minutes of points. On Linux, replace
`date -u -v-10M` with `date -u -d '-10 min'`.

```bash
curl -s --get \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://monitoring.googleapis.com/v3/projects/$PROJECT/timeSeries" \
  --data-urlencode 'filter=metric.type="custom.googleapis.com/mon_ram/memory_used_percent"' \
  --data-urlencode "interval.startTime=$(date -u -v-10M +%Y-%m-%dT%H:%M:%SZ)" \
  --data-urlencode "interval.endTime=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

## 4. Dashboard

**Quick way:** in Metrics Explorer, chart the metric as in step 3, then click
**Save chart** and pick a new or existing dashboard.

**As code:** this creates a dashboard with both metrics. Each machine is its
own line.

```bash
cat > dashboard.json <<'EOF'
{
  "displayName": "mon-ram",
  "mosaicLayout": {
    "columns": 12,
    "tiles": [
      {
        "width": 6, "height": 4,
        "widget": {
          "title": "RAM used (%)",
          "xyChart": {
            "dataSets": [{
              "plotType": "LINE",
              "timeSeriesQuery": {"timeSeriesFilter": {
                "filter": "metric.type=\"custom.googleapis.com/mon_ram/memory_used_percent\" resource.type=\"generic_node\"",
                "aggregation": {"alignmentPeriod": "60s", "perSeriesAligner": "ALIGN_MEAN"}
              }}
            }],
            "yAxis": {"label": "%", "scale": "LINEAR"}
          }
        }
      },
      {
        "xPos": 6, "width": 6, "height": 4,
        "widget": {
          "title": "RAM used (bytes)",
          "xyChart": {
            "dataSets": [{
              "plotType": "LINE",
              "timeSeriesQuery": {"timeSeriesFilter": {
                "filter": "metric.type=\"custom.googleapis.com/mon_ram/memory_used_bytes\" resource.type=\"generic_node\"",
                "aggregation": {"alignmentPeriod": "60s", "perSeriesAligner": "ALIGN_MEAN"}
              }}
            }],
            "yAxis": {"label": "bytes", "scale": "LINEAR"}
          }
        }
      }
    ]
  }
}
EOF
gcloud monitoring dashboards create --config-from-file=dashboard.json
```

To change it later, either edit it in the console or run
`gcloud monitoring dashboards update`. The update needs the dashboard's
`etag`, which you get from `gcloud monitoring dashboards list`.

## 5. Alert when RAM stays above 90%

Create the policy **after** the first point has been written. Until then the
metric doesn't exist, and policy creation fails with "Cannot find metric(s)".

First, create somewhere for alerts to go. This example uses email. You can
also create channels in **Monitoring → Alerting → Edit notification
channels**.

```bash
gcloud beta monitoring channels create \
  --display-name="mon-ram email" --type=email \
  --channel-labels=email_address=you@example.com
CHANNEL=$(gcloud beta monitoring channels list \
  --filter='displayName="mon-ram email"' --format='value(name)')
```

Next, create the policy. It fires when any machine averages above 90% for 5
minutes, and it reports each machine separately.

```bash
cat > policy.json <<'EOF'
{
  "displayName": "mon-ram: RAM above 90%",
  "combiner": "OR",
  "conditions": [{
    "displayName": "memory_used_percent > 90 for 5 min",
    "conditionThreshold": {
      "filter": "metric.type=\"custom.googleapis.com/mon_ram/memory_used_percent\" resource.type=\"generic_node\"",
      "aggregations": [{"alignmentPeriod": "60s", "perSeriesAligner": "ALIGN_MEAN"}],
      "comparison": "COMPARISON_GT",
      "thresholdValue": 90,
      "duration": "300s",
      "trigger": {"count": 1}
    }
  }],
  "documentation": {
    "content": "RAM on ${resource.label.node_id} has been above 90% for 5 minutes. Check `top` or `journalctl` on that machine.",
    "mimeType": "text/markdown"
  }
}
EOF
gcloud monitoring policies create --policy-from-file=policy.json \
  --notification-channels="$CHANNEL"
```

**Tuning:** to change the threshold, edit `thresholdValue`. To change how long
the condition must hold before it fires, edit `duration`. Then update the
policy in **Monitoring → Alerting** or with
`gcloud monitoring policies update POLICY_ID --policy-from-file=policy.json`.
To find the ID, run `gcloud monitoring policies list --format='value(name)'`.

## 6. Ongoing changes

### Adding a machine

Create a new key (step 1), then install it (step 2a or 2b). Make sure the
machine's hostname is unique, because that's its `node_id`. The new machine
shows up on the dashboard and in the alert automatically, with no changes in
GCP.

### Removing a machine

```bash
# On the machine:
sudo systemctl disable --now mon-ram
# From your workstation, find and delete that machine's key:
gcloud iam service-accounts keys list --iam-account="$SA"
gcloud iam service-accounts keys delete KEY_ID --iam-account="$SA"
```

Its series stops getting new points, and the old data ages out on its own.

### Rotating a key

1. Create a new key (step 1).
2. Copy it to `/etc/mon-ram/key.json` on the machine.
3. Run `sudo systemctl restart mon-ram`.
4. Delete the old key ID with `keys delete`.

### Deleting a metric

You need this if you change a metric's value type, or you want to wipe its
history. Deleting a descriptor also **deletes all of its data**. If any
machine still writes to the metric, the descriptor gets created again.

```bash
curl -s -X DELETE \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://monitoring.googleapis.com/v3/projects/$PROJECT/metricDescriptors/custom.googleapis.com/mon_ram/memory_used_bytes"
```

### Cost

Custom metrics are billed by the volume of data ingested, at 8 bytes per
point, and the first 150 MiB per month are free. Each machine sends 2 series
at 1 point a minute. That's about 86,400 points, or roughly 0.7 MB, a month,
so a few hundred machines still fit in the free tier. If you lower
`INTERVAL_SECONDS`, the cost goes up in proportion. Check the current
[Cloud Monitoring pricing](https://cloud.google.com/stackdriver/pricing)
before you scale up.
