# Fastly VCL Activation Alerting Design

## Status

Approved design, pending user review before implementation planning.

## Context

The Fastly service used by Adobe Commerce can receive VCL activations from the
Commerce Admin, deployment pipeline, Magento CLI, or direct API calls. An
activation changes live edge behavior; observing the active Fastly version is
therefore the signal to monitor. The supplied proposal recommends polling the
Fastly API because it covers all activation sources without requiring Fastly
account changes.

The initial trial will monitor the staging service on the user's Mac. The
service ID is supplied by the user and may be configured locally; API tokens
and notification webhook URLs must never be committed or pasted into chat.

## Goals

- Detect a change in the active Fastly service version, regardless of its source.
- Send a webhook alert containing the old and new version identifiers and a
  readable VCL diff.
- Report rollback-like activations (including a transition to a lower or
  previously active version) as changes.
- Keep credentials out of source control and make API, diff, and delivery
  failures visible.
- Provide a safe staging validation procedure without activating VCL as part of
  this implementation.

## Non-goals

- Identify the person or system that activated a version; that requires Fastly
  audit-log access outside the watcher.
- Activate, modify, or roll back VCL automatically.
- Monitor production or additional Fastly services in the initial trial.
- Guarantee observation of every intermediate activation: versions activated
  and replaced between polls may not be individually observed.
- Configure Fastly account-level webhooks or Adobe-managed account settings.

## Design

Implement a small Python watcher using the Fastly read API and Python's standard
library. A macOS `launchd` job runs it every five minutes. The Fastly service ID
and local state-file path are non-secret configuration; the Fastly API token
and webhook URL are supplied at runtime through environment variables, with
local setup instructions recommending macOS Keychain-backed environment
loading. No credentials are stored in the repository.

The watcher reads the service's active version. On its first successful run, it
stores that version as a baseline without sending an alert. On later runs, an
unchanged active version exits successfully without notification. A changed
version causes the watcher to retrieve the VCL representations for the old and
new versions, produce a unified diff, and send a webhook containing the service
identifier, version transition, observation time, and diff. The state file is
advanced only after all required API calls and alert delivery succeed.

The state file is persistent per service and written atomically so an
interrupted write cannot leave a partially updated baseline. A webhook that is
accepted immediately before a local state-write failure can be delivered again
on the next run; at-least-once delivery is preferred over silently losing an
alert. Each separately configured service uses its own state file.

## Errors and operational behavior

- Missing configuration, Fastly API errors, invalid responses, VCL retrieval or
  diff errors, and webhook failures produce a clear diagnostic and nonzero exit.
- On any failure, the stored baseline is not advanced; a later run can retry.
- The launchd configuration must make failures discoverable through macOS job
  logs, and setup documentation must explain how to inspect those logs and
  verify the job is loaded.
- A sleeping or powered-off Mac cannot poll. Detection resumes when scheduled
  execution resumes; this is a laptop trial, not a production availability
  guarantee.
- Polling has up to one interval of detection latency. Multiple activations
  between polls can collapse into a single observed transition.

## Validation

Automated tests will cover baseline creation, no-change behavior, forward
activation, rollback-like transitions, diff generation, webhook payload and
delivery errors, Fastly API errors, and state preservation on failure. Fastly
and webhook interactions will be mocked; tests will not require live
credentials.

The staging smoke-test instructions will ask the operator to configure a
service-scoped read-only token where available, run once to establish a
baseline, confirm a no-change run, activate a minor staging VCL change and
confirm the alert and diff, then reactivate an earlier version and confirm the
rollback alert. An invalid-token check will confirm visible failure and a
nonzero exit. This project will not perform those activations or use the user's
credentials.

## Decisions recorded

- **Detection:** Scheduled active-version polling, rather than a Fastly
  activation webhook or deployment-pipeline-only alert.
- **Initial environment:** Staging service only.
- **Initial host and scheduler:** User's Mac with `launchd`.
- **Polling interval:** Five minutes.
- **Alert transport:** Slack-compatible incoming webhook, selected over email
  because it avoids SMTP relay setup for the trial.
- **Secrets:** Runtime-only configuration; no Fastly token or webhook URL in
  GitHub.
- **Change handling:** Alert on all observed changes, including planned
  deployment changes; no deployment-window suppression.
- **Ownership attribution:** Out of scope.

## Open implementation details

- The webhook provider and URL, and the user's chosen destination/channel, will
  be configured locally and are not needed to implement the watcher.
- Exact Fastly read API calls and the available VCL representation for each
  version must be confirmed during implementation and tested against mocked
  responses before staging use.
- Production promotion is a separate follow-up after the staging trial is
  validated.
