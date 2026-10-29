import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fastly_vcl_watch as watcher


class ConfigTests(unittest.TestCase):
    def test_load_config_accepts_https_webhook_and_keychain_secrets(self):
        environment = {
            "FASTLY_SERVICE_ID": "service123",
            "FASTLY_VCL_STATE_FILE": "/tmp/vcl-watch-state.json",
        }
        with patch.dict("os.environ", environment, clear=True):
            with patch.object(
                watcher,
                "_read_secret",
                side_effect=["read-token", "https://hooks.example.test/secret"],
            ):
                config = watcher.load_config()

        self.assertEqual("service123", config.service_id)
        self.assertEqual("read-token", config.token)
        self.assertEqual("https://hooks.example.test/secret", config.webhook_url)
        self.assertEqual(Path("/tmp/vcl-watch-state.json"), config.state_file)

    def test_load_config_rejects_non_https_webhook(self):
        environment = {"FASTLY_SERVICE_ID": "service123"}
        with patch.dict("os.environ", environment, clear=True):
            with patch.object(
                watcher,
                "_read_secret",
                side_effect=["read-token", "http://hooks.example.test/secret"],
            ):
                with self.assertRaisesRegex(watcher.WatchError, "HTTPS URL"):
                    watcher.load_config()

    def test_load_config_rejects_malformed_webhook(self):
        environment = {"FASTLY_SERVICE_ID": "service123"}
        with patch.dict("os.environ", environment, clear=True):
            with patch.object(
                watcher,
                "_read_secret",
                side_effect=["read-token", "https://[invalid"],
            ):
                with self.assertRaisesRegex(watcher.WatchError, "malformed"):
                    watcher.load_config()


class FakeFastly:
    def __init__(self, active, vcl=None, error=None):
        self.active = active
        self.vcl = vcl or {}
        self.error = error
        self.vcl_requests = []

    def active_version(self, service_id):
        if self.error:
            raise self.error
        return self.active

    def generated_vcl(self, service_id, version):
        self.vcl_requests.append((service_id, version))
        return self.vcl[version]


class FakeNotifier:
    def __init__(self, error=None):
        self.messages = []
        self.error = error

    def send(self, message):
        if self.error:
            raise self.error
        self.messages.append(message)


class WatchOnceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.state_file = Path(self.temporary_directory.name) / "state.json"
        self.config = watcher.Config(
            service_id="service123",
            token="test-token",
            webhook_url="https://example.test/hook",
            state_file=self.state_file,
        )
        self.output = io.StringIO()

    def write_state(self, version):
        watcher._write_state(self.state_file, self.config.service_id, version)

    def read_version(self):
        return json.loads(self.state_file.read_text(encoding="utf-8"))[
            "active_version"
        ]

    def test_first_run_records_baseline_without_alert(self):
        fastly = FakeFastly(active=12)
        notifier = FakeNotifier()

        result = watcher.watch_once(self.config, fastly, notifier, self.output)

        self.assertEqual("baseline", result)
        self.assertEqual(12, self.read_version())
        self.assertEqual([], notifier.messages)

    def test_unchanged_active_version_does_not_alert_or_fetch_vcl(self):
        self.write_state(12)
        fastly = FakeFastly(active=12)
        notifier = FakeNotifier()

        result = watcher.watch_once(self.config, fastly, notifier, self.output)

        self.assertEqual("unchanged", result)
        self.assertEqual([], notifier.messages)
        self.assertEqual([], fastly.vcl_requests)

    def test_activation_sends_diff_then_updates_state(self):
        self.write_state(12)
        fastly = FakeFastly(
            active=13,
            vcl={
                12: "sub vcl_recv {\n  set req.http.old = \"yes\";\n}\n",
                13: "sub vcl_recv {\n  set req.http.new = \"yes\";\n}\n",
            },
        )
        notifier = FakeNotifier()

        result = watcher.watch_once(self.config, fastly, notifier, self.output)

        self.assertEqual("changed", result)
        self.assertEqual(13, self.read_version())
        self.assertEqual([("service123", 12), ("service123", 13)], fastly.vcl_requests)
        self.assertEqual(1, len(notifier.messages))
        self.assertIn("Version: 12 -> 13", notifier.messages[0])
        self.assertIn('set req.http.old = "yes";', notifier.messages[0])
        self.assertIn('set req.http.new = "yes";', notifier.messages[0])

    def test_rollback_to_lower_version_is_reported(self):
        self.write_state(13)
        fastly = FakeFastly(active=12, vcl={12: "same\n", 13: "changed\n"})
        notifier = FakeNotifier()

        result = watcher.watch_once(self.config, fastly, notifier, self.output)

        self.assertEqual("changed", result)
        self.assertEqual(12, self.read_version())
        self.assertIn("Version: 13 -> 12", notifier.messages[0])

    def test_identical_generated_vcl_still_alerts_for_version_change(self):
        self.write_state(12)
        fastly = FakeFastly(active=13, vcl={12: "same\n", 13: "same\n"})
        notifier = FakeNotifier()

        watcher.watch_once(self.config, fastly, notifier, self.output)

        self.assertIn("active version changed", notifier.messages[0])

    def test_webhook_failure_does_not_advance_state(self):
        self.write_state(12)
        fastly = FakeFastly(active=13, vcl={12: "old\n", 13: "new\n"})
        notifier = FakeNotifier(watcher.WatchError("delivery failed"))

        with self.assertRaisesRegex(watcher.WatchError, "delivery failed"):
            watcher.watch_once(self.config, fastly, notifier, self.output)

        self.assertEqual(12, self.read_version())

    def test_fastly_failure_does_not_advance_state(self):
        self.write_state(12)
        fastly = FakeFastly(
            active=13, error=watcher.WatchError("Fastly unavailable")
        )

        with self.assertRaisesRegex(watcher.WatchError, "Fastly unavailable"):
            watcher.watch_once(self.config, fastly, FakeNotifier(), self.output)

        self.assertEqual(12, self.read_version())

    def test_vcl_retrieval_failure_does_not_advance_state(self):
        self.write_state(12)

        class VclFailureFastly(FakeFastly):
            def generated_vcl(self, service_id, version):
                raise watcher.WatchError("VCL unavailable")

        with self.assertRaisesRegex(watcher.WatchError, "VCL unavailable"):
            watcher.watch_once(
                self.config,
                VclFailureFastly(active=13),
                FakeNotifier(),
                self.output,
            )

        self.assertEqual(12, self.read_version())

    def test_invalid_state_is_not_silently_rebaselined(self):
        self.state_file.write_text("{", encoding="utf-8")
        fastly = FakeFastly(active=12)

        with self.assertRaisesRegex(watcher.WatchError, "invalid JSON"):
            watcher.watch_once(self.config, fastly, FakeNotifier(), self.output)

        self.assertEqual("{", self.state_file.read_text(encoding="utf-8"))


class FastlyClientTests(unittest.TestCase):
    def test_active_version_requires_exactly_one_active_entry(self):
        client = watcher.FastlyClient("token")
        client._get_json = lambda path: [
            {"active": False, "number": 2},
            {"active": True, "number": 3},
        ]

        self.assertEqual(3, client.active_version("service123"))

    def test_active_version_rejects_multiple_active_entries(self):
        client = watcher.FastlyClient("token")
        client._get_json = lambda path: [
            {"active": True, "number": 2},
            {"active": True, "number": 3},
        ]

        with self.assertRaisesRegex(watcher.WatchError, "exactly one active"):
            client.active_version("service123")

    def test_generated_vcl_requires_content_string(self):
        client = watcher.FastlyClient("token")
        client._get_json = lambda path: {"content": "sub vcl_recv {}"}

        self.assertEqual("sub vcl_recv {}", client.generated_vcl("service123", 2))

        client._get_json = lambda path: {"content": None}
        with self.assertRaisesRegex(watcher.WatchError, "invalid generated VCL"):
            client.generated_vcl("service123", 2)

    def test_requests_use_read_only_fastly_endpoints(self):
        client = watcher.FastlyClient("token")
        requested_paths = []

        def fake_get_json(path):
            requested_paths.append(path)
            if path.endswith("/version"):
                return [{"active": True, "number": 2}]
            return {"content": "sub vcl_recv {}"}

        client._get_json = fake_get_json

        self.assertEqual(2, client.active_version("service123"))
        self.assertEqual(
            "sub vcl_recv {}", client.generated_vcl("service123", 2)
        )
        self.assertEqual(
            [
                "/service/service123/version",
                "/service/service123/version/2/generated_vcl",
            ],
            requested_paths,
        )


if __name__ == "__main__":
    unittest.main()
