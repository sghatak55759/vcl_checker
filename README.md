# Fastly VCL activation watcher

A standard-library Python watcher that polls the Fastly service's active
version and sends a Slack-compatible webhook alert, including a diff of the
generated VCL, when the observed active version changes.

The initial configuration is for a staging trial on macOS. The watcher is
read-only: it does not activate, edit, or roll back Fastly versions. It has not
been run against the supplied service or any live credentials.

## Requirements

- Python 3.8 or later.
- A Fastly API token with read access to the target service. Use a
  service-restricted, read-only token where the Adobe-managed account allows it.
- An HTTPS incoming webhook URL. The alert contains generated VCL differences;
  use a private destination whose audience is authorized to see that
  configuration.
- Outbound HTTPS access to `api.fastly.com` and the webhook provider.

## Configuration and one-time baseline

The watcher reads these environment variables:

| Variable | Required | Purpose |
| --- | --- | --- |
| `FASTLY_SERVICE_ID` | Yes | Fastly service to monitor. |
| `FASTLY_API_TOKEN` | Yes | Fastly token; alternatively read from macOS Keychain. |
| `FASTLY_VCL_WEBHOOK_URL` | Yes | HTTPS webhook URL; alternatively read from macOS Keychain. |
| `FASTLY_VCL_STATE_FILE` | No | State-file path. Defaults to `~/Library/Application Support/fastly-vcl-watch/<service-id>.json`. |

On macOS, secrets can be stored in the login Keychain. These commands prompt
for each value; do not add a password argument or paste a secret into a
command-line option:

```sh
security add-generic-password -U -s fastly-vcl-watch -a FASTLY_API_TOKEN -w
security add-generic-password -U -s fastly-vcl-watch -a FASTLY_VCL_WEBHOOK_URL -w
```

The Keychain item service must be `fastly-vcl-watch`, and the account names
must match the environment variable names above. The watcher first checks the
environment and then retrieves a missing secret from Keychain through macOS's
`security` utility. macOS may ask you to allow that utility to access each
item.

Set the service ID for an interactive trial and run once to establish a
baseline. Replace the example ID with the intended staging service ID:

```sh
export FASTLY_SERVICE_ID="REPLACE_WITH_STAGING_SERVICE_ID"
python3 fastly_vcl_watch.py
```

The first successful run records the active version without sending an alert.
Subsequent runs send an alert when the observed version changes. An activation
back to a lower or previously active version also generates an alert. If the
active version changes but the generated VCL is identical, the alert says so.

For a one-off run without Keychain, set `FASTLY_API_TOKEN` and
`FASTLY_VCL_WEBHOOK_URL` in the process environment. Do not put either value in
the repository, the launchd plist, or shell history.

## Run every five minutes with launchd

Copy `launchd/com.vclchecker.fastly-vcl-watch.plist.example` to
`~/Library/LaunchAgents/com.vclchecker.fastly-vcl-watch.plist`. Edit the copy:

1. Replace both program paths with the absolute paths from `command -v python3`
   and this repository's `fastly_vcl_watch.py`.
2. Set `FASTLY_SERVICE_ID` to the staging service ID.
3. Replace the username in the two log paths. Ensure `~/Library/Logs` exists.
4. Do not add the API token or webhook URL to the plist.

Load and inspect the job:

```sh
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.vclchecker.fastly-vcl-watch.plist"
launchctl print "gui/$(id -u)/com.vclchecker.fastly-vcl-watch"
```

`RunAtLoad` starts an immediate run; `StartInterval` schedules the subsequent
checks every 300 seconds. To stop the job:

```sh
launchctl bootout "gui/$(id -u)/com.vclchecker.fastly-vcl-watch"
```

Check the plist's configured stdout and stderr logs after loading. If your Mac
is asleep or powered off, checks do not run; polling resumes when the job next
runs. This laptop setup is for a staging trial, not a production availability
guarantee.

## State and failure behavior

The state file contains the service ID, last successfully reported active
version, and update time. It is created with restrictive file permissions and
replaced atomically. A per-state-file lock prevents overlapping scheduled runs.

Configuration errors, malformed API responses, Fastly/API failures, VCL
retrieval failures, and webhook failures print an error to stderr and exit
nonzero. The state is not advanced after a failure, so a later run can retry.
If webhook delivery succeeds but writing the state then fails, a duplicate
alert is possible; this favors not losing a change over exactly-once delivery.

Polling cannot report intermediate versions activated and replaced between two
checks. Detection latency is at most one interval when the laptop and job are
available.

## Tests

Run the standard-library unit tests:

```sh
python3 -m unittest discover -s tests -v
```

The tests mock Fastly and webhook interactions; they do not activate VCL or
require credentials.

## Staging validation checklist

1. Confirm the configured Fastly token is read-only and restricted to the
   staging service where possible.
2. Run once and confirm the baseline message; run again and confirm that no
   alert is sent when the active version is unchanged.
3. Activate a small, approved staging VCL change and confirm the alert includes
   the old/new versions and generated-VCL diff.
4. Reactivate the prior staging version and confirm the rollback transition is
   alerted.
5. Test with an invalid token and confirm the command exits nonzero, logs an
   error, and does not advance the state.

The watcher never performs these activation steps itself.

## Fastly API references

- [Service versions API](https://www.fastly.com/documentation/reference/api/services/version/)
- [Fastly Python SDK VCL API reference](https://github.com/fastly/fastly-py/blob/main/docs/VclApi.md)
