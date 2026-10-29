#!/usr/bin/env python3
"""Poll Fastly for an active VCL version change and alert a webhook."""

import difflib
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, TextIO


FASTLY_API_BASE = "https://api.fastly.com"
KEYCHAIN_SERVICE = "fastly-vcl-watch"
HTTP_TIMEOUT_SECONDS = 20


class WatchError(Exception):
    """An expected configuration, API, state, or notification failure."""


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, new_url):
        return None


def _read_secret(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value

    if sys.platform != "darwin":
        raise WatchError(
            "Missing required setting {} (set it in the environment).".format(name)
        )

    try:
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                name,
                "-w",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WatchError(
            "Could not read {} from the macOS Keychain: {}.".format(name, exc)
        ) from exc

    secret = result.stdout.rstrip("\r\n")
    if result.returncode != 0 or not secret:
        raise WatchError(
            "Missing {}. Set it in the environment or add it to the "
            "macOS Keychain (service '{}', account '{}').".format(
                name, KEYCHAIN_SERVICE, name
            )
        )
    return secret


class Config:
    def __init__(self, service_id: str, token: str, webhook_url: str, state_file: Path):
        self.service_id = service_id
        self.token = token
        self.webhook_url = webhook_url
        self.state_file = state_file


def load_config() -> Config:
    service_id = os.environ.get("FASTLY_SERVICE_ID", "").strip()
    if not service_id or not re.fullmatch(r"[A-Za-z0-9]+", service_id):
        raise WatchError(
            "FASTLY_SERVICE_ID must be set to the alphanumeric Fastly service ID."
        )

    token = _read_secret("FASTLY_API_TOKEN")
    webhook_url = _read_secret("FASTLY_VCL_WEBHOOK_URL")
    try:
        parsed_webhook = urllib.parse.urlsplit(webhook_url)
        valid_webhook = (
            parsed_webhook.scheme == "https"
            and bool(parsed_webhook.hostname)
            and parsed_webhook.username is None
            and parsed_webhook.password is None
            and parsed_webhook.port != 0
        )
    except ValueError as exc:
        raise WatchError("FASTLY_VCL_WEBHOOK_URL is malformed.") from exc
    if not valid_webhook:
        raise WatchError("FASTLY_VCL_WEBHOOK_URL must be an HTTPS URL.")

    default_state = (
        Path.home()
        / "Library"
        / "Application Support"
        / "fastly-vcl-watch"
        / "{}.json".format(service_id)
    )
    state_file = Path(
        os.environ.get("FASTLY_VCL_STATE_FILE", str(default_state))
    ).expanduser()
    return Config(service_id, token, webhook_url, state_file)


class FastlyClient:
    def __init__(self, token: str, timeout: int = HTTP_TIMEOUT_SECONDS):
        self.timeout = timeout
        self.opener = urllib.request.build_opener(NoRedirectHandler())
        self.headers = {
            "Fastly-Key": token,
            "Accept": "application/json",
            "User-Agent": "fastly-vcl-watch/1.0",
        }

    def _get_json(self, path: str) -> Any:
        request = urllib.request.Request(
            FASTLY_API_BASE + path, headers=self.headers, method="GET"
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise WatchError(
                "Fastly API returned HTTP {} for {}.".format(exc.code, path)
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise WatchError(
                "Fastly API request failed for {}: {}.".format(path, exc)
            ) from exc

        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WatchError(
                "Fastly API returned invalid JSON for {}.".format(path)
            ) from exc

    def active_version(self, service_id: str) -> int:
        service_path = urllib.parse.quote(service_id, safe="")
        versions = self._get_json("/service/{}/version".format(service_path))
        if not isinstance(versions, list):
            raise WatchError("Fastly API returned an invalid service-version list.")

        active = [
            item
            for item in versions
            if isinstance(item, dict) and item.get("active") is True
        ]
        if len(active) != 1:
            raise WatchError(
                "Expected exactly one active Fastly version; found {}.".format(
                    len(active)
                )
            )

        version = active[0].get("number")
        if type(version) is not int or version < 1:
            raise WatchError("Fastly API returned an invalid active version number.")
        return version

    def generated_vcl(self, service_id: str, version: int) -> str:
        service_path = urllib.parse.quote(service_id, safe="")
        payload = self._get_json(
            "/service/{}/version/{}/generated_vcl".format(service_path, version)
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
            raise WatchError(
                "Fastly API returned an invalid generated VCL response for "
                "version {}.".format(version)
            )
        return payload["content"]


class WebhookNotifier:
    def __init__(self, webhook_url: str, timeout: int = HTTP_TIMEOUT_SECONDS):
        self.webhook_url = webhook_url
        self.timeout = timeout
        self.opener = urllib.request.build_opener(NoRedirectHandler())

    def send(self, message: str) -> None:
        body = json.dumps({"text": message}).encode("utf-8")
        request = urllib.request.Request(
            self.webhook_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "fastly-vcl-watch/1.0",
            },
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if not 200 <= response.status < 300:
                    raise WatchError(
                        "Webhook returned HTTP {}.".format(response.status)
                    )
                response.read()
        except urllib.error.HTTPError as exc:
            raise WatchError("Webhook returned HTTP {}.".format(exc.code)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise WatchError("Webhook delivery failed: {}.".format(exc)) from exc


def _read_state(state_file: Path, service_id: str) -> Optional[int]:
    try:
        raw = state_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WatchError(
            "Could not read state file '{}': {}.".format(state_file, exc)
        ) from exc

    try:
        state = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WatchError(
            "State file '{}' contains invalid JSON; inspect or move it before "
            "retrying.".format(state_file)
        ) from exc

    if not isinstance(state, dict) or state.get("service_id") != service_id:
        raise WatchError(
            "State file '{}' does not match service '{}'; refusing to overwrite it."
            .format(state_file, service_id)
        )
    version = state.get("active_version")
    if type(version) is not int or version < 1:
        raise WatchError(
            "State file '{}' has an invalid active_version.".format(state_file)
        )
    return version


def _write_state(state_file: Path, service_id: str, version: int) -> None:
    try:
        state_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=".fastly-vcl-watch-", dir=str(state_file.parent)
        )
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as state:
                os.fchmod(state.fileno(), 0o600)
                json.dump(
                    {
                        "service_id": service_id,
                        "active_version": version,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                    state,
                    indent=2,
                    sort_keys=True,
                )
                state.write("\n")
                state.flush()
                os.fsync(state.fileno())
            os.replace(temporary_name, state_file)
            temporary_name = None
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
    except OSError as exc:
        raise WatchError(
            "Could not atomically write state file '{}': {}.".format(
                state_file, exc
            )
        ) from exc


def _build_diff(old_vcl: str, new_vcl: str, old_version: int, new_version: int) -> str:
    return "".join(
        difflib.unified_diff(
            old_vcl.splitlines(keepends=True),
            new_vcl.splitlines(keepends=True),
            fromfile="generated-vcl-version-{}".format(old_version),
            tofile="generated-vcl-version-{}".format(new_version),
        )
    )


def _build_alert(
    service_id: str, old_version: int, new_version: int, diff: str
) -> str:
    observed_at = datetime.now(timezone.utc).isoformat()
    if diff:
        change_details = "VCL diff:\n```diff\n{}```".format(diff)
    else:
        change_details = (
            "Generated VCL is identical between these versions; the active "
            "version changed."
        )
    return (
        "Fastly VCL activation detected\n"
        "Service: {}\n"
        "Version: {} -> {}\n"
        "Observed at: {}\n\n"
        "{}"
    ).format(service_id, old_version, new_version, observed_at, change_details)


def watch_once(
    config: Config,
    fastly: Optional[FastlyClient] = None,
    notifier: Optional[WebhookNotifier] = None,
    output: Optional[TextIO] = None,
) -> str:
    fastly = fastly or FastlyClient(config.token)
    notifier = notifier or WebhookNotifier(config.webhook_url)
    output = output or sys.stdout

    previous_version = _read_state(config.state_file, config.service_id)
    active_version = fastly.active_version(config.service_id)

    if previous_version is None:
        _write_state(config.state_file, config.service_id, active_version)
        print(
            "Baseline recorded for service {} at version {}.".format(
                config.service_id, active_version
            ),
            file=output,
        )
        return "baseline"

    if active_version == previous_version:
        print(
            "No active-version change for service {} (version {}).".format(
                config.service_id, active_version
            ),
            file=output,
        )
        return "unchanged"

    old_vcl = fastly.generated_vcl(config.service_id, previous_version)
    new_vcl = fastly.generated_vcl(config.service_id, active_version)
    diff = _build_diff(old_vcl, new_vcl, previous_version, active_version)
    notifier.send(
        _build_alert(config.service_id, previous_version, active_version, diff)
    )
    _write_state(config.state_file, config.service_id, active_version)
    print(
        "Alert sent for service {}: version {} -> {}.".format(
            config.service_id, previous_version, active_version
        ),
        file=output,
    )
    return "changed"


def _acquire_state_lock(state_file: Path) -> int:
    try:
        state_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            str(state_file) + ".lock", os.O_CREAT | os.O_RDWR, 0o600
        )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return descriptor
    except OSError as exc:
        raise WatchError(
            "Could not lock state file '{}': {}.".format(state_file, exc)
        ) from exc


def main() -> int:
    try:
        config = load_config()
        lock_descriptor = _acquire_state_lock(config.state_file)
        try:
            watch_once(config)
        finally:
            os.close(lock_descriptor)
        return 0
    except WatchError as exc:
        print("fastly-vcl-watch: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
