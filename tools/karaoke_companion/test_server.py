"""Protocol and durable queue checks; no media tools or model downloads."""
import hashlib
import json
from pathlib import Path
import tempfile
import socket
import ssl
import threading
import unittest
from unittest.mock import patch

from server import Handler, Jobs, Server


class JobsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.jobs = Jobs(Path(self.tmp.name), None, None, None, 30)
        self.asset = hashlib.sha256(b"backing").hexdigest()
        (self.jobs.assets / self.asset).write_bytes(b"backing")

    def tearDown(self):
        self.jobs.pool.shutdown(wait=True, cancel_futures=True)
        self.tmp.cleanup()

    def request(self, **changes):
        return {"id": "request_0001", "kind": "waveform", "assets": [self.asset], **changes}

    def test_separation_requires_the_patched_runtime(self):
        models = Path(self.tmp.name) / "models"
        models.mkdir()
        self.jobs.models, self.jobs.model_revision = models, "fixture"
        versions = {"torch": "2.15.0+cpu", "torchaudio": "2.11.1+cpu", "torchcodec": "0.17.0+cpu", "setuptools": "84.2.1"}
        with patch("server.shutil.which", return_value="/fixture/tool"), \
                patch("server.importlib.util.find_spec", return_value=object()), \
                patch("server.importlib.metadata.version", side_effect=versions.__getitem__):
            self.assertTrue(self.jobs.capabilities()["capabilities"]["separate"])
            versions["torch"] = "2.8.0"
            self.assertFalse(self.jobs.capabilities()["capabilities"]["separate"])
            versions["torch"] = "2.15.0+cpu"
            versions["setuptools"] = "81.0.0"
            self.assertFalse(self.jobs.capabilities()["capabilities"]["separate"])
            versions["setuptools"] = "84.2.1"
            versions["torch"] = "2.15.0rc1"
            self.assertFalse(self.jobs.capabilities()["capabilities"]["separate"])

    def test_retries_return_same_job_and_reject_changed_inputs(self):
        with patch.object(self.jobs, "capabilities", return_value={"capabilities": {"waveform": True}}), \
                patch.object(self.jobs.pool, "submit") as submit:
            first = self.jobs.submit(self.request())
            again = self.jobs.submit(self.request())
            self.assertEqual(first["fingerprint"], again["fingerprint"])
            self.assertEqual(1, submit.call_count)
            with self.assertRaisesRegex(ValueError, "reused"):
                self.jobs.submit(self.request(params={"changed": True}))
            self.assertEqual(b"backing", (self.jobs.jobs / first["id"] / "input-0").read_bytes())

    def test_restart_interrupts_incomplete_jobs(self):
        with patch.object(self.jobs, "capabilities", return_value={"capabilities": {"waveform": True}}), \
                patch.object(self.jobs.pool, "submit"):
            self.jobs.submit(self.request())
        restored = Jobs(Path(self.tmp.name), None, None, None, 30)
        try:
            self.assertEqual("interrupted", restored.states["request_0001"]["status"])
            state = json.loads((restored.jobs / "request_0001" / "state.json").read_text())
            self.assertEqual("interrupted", state["status"])
        finally:
            restored.pool.shutdown(wait=True, cancel_futures=True)

    def test_rejects_missing_assets_and_path_injection(self):
        for changes in [{"id": "../outside"}, {"assets": ["../outside"]}, {"assets": ["a" * 64]}, {"params": []}]:
            with self.assertRaises(ValueError):
                self.jobs.submit(self.request(**changes))

    def test_cancel_before_worker_execution_is_durable_and_removable(self):
        with patch.object(self.jobs, "capabilities", return_value={"capabilities": {"waveform": True}}), \
                patch.object(self.jobs.pool, "submit"):
            self.jobs.submit(self.request())
        self.jobs.cancel("request_0001")
        self.jobs.execute("request_0001")
        self.assertEqual("canceled", self.jobs.states["request_0001"]["status"])
        self.jobs.remove("request_0001")
        self.assertFalse((self.jobs.assets / self.asset).exists())


    def test_removed_canceled_id_can_prepare_again(self):
        with patch.object(self.jobs, "capabilities", return_value={"capabilities": {"waveform": True}}), \
                patch.object(self.jobs.pool, "submit"):
            self.jobs.submit(self.request())
            self.jobs.cancel("request_0001")
            self.jobs.remove("request_0001")
            (self.jobs.assets / self.asset).write_bytes(b"backing")
            self.jobs.submit(self.request())
        with patch("server.subprocess.Popen", side_effect=OSError("fixture worker")) as start:
            self.jobs._dispatch()
        start.assert_called_once()
        self.assertEqual("failed", self.jobs.states["request_0001"]["status"])

    def test_state_write_failure_releases_dispatcher_and_next_job_runs(self):
        from server import atomic_json
        def write(path, state):
            if state.get("id") == "request_0002" and state.get("status") == "running":
                raise OSError("fixture state write failed")
            atomic_json(path, state)
        with patch.object(self.jobs, "capabilities", return_value={"capabilities": {"waveform": True}}), \
                patch.object(self.jobs.pool, "submit"):
            self.jobs.submit(self.request())
            self.jobs.submit(self.request(id="request_0002", priority=90))
        with patch("server.atomic_json", side_effect=write), patch("server.subprocess.Popen", side_effect=OSError("fixture worker")) as start:
            self.jobs._dispatch()
        self.assertEqual(start.call_count, 1)
        self.assertFalse(self.jobs.dispatcher_running)
        self.assertIsNone(self.jobs.current_job_id)
        self.assertEqual("failed", self.jobs.states["request_0002"]["status"])

    def test_cancel_while_starting_keeps_work_directory_until_dispatcher_retires(self):
        with patch.object(self.jobs, "capabilities", return_value={"capabilities": {"waveform": True}}), patch.object(self.jobs.pool, "submit"):
            self.jobs.submit(self.request())
        def starting(*args, **kwargs):
            self.jobs.cancel("request_0001")
            with self.assertRaises(ValueError):
                self.jobs.remove("request_0001")
            raise OSError("fixture worker")
        with patch("server.subprocess.Popen", side_effect=starting):
            self.jobs._dispatch()
        self.assertEqual("removed", self.jobs.remove("request_0001")["status"])

    def test_one_damaged_snapshot_does_not_block_recovery_of_other_jobs(self):
        with patch.object(self.jobs, "capabilities", return_value={"capabilities": {"waveform": True}}), patch.object(self.jobs.pool, "submit"):
            self.jobs.submit(self.request())
        damaged = self.jobs.jobs / "damaged_01"
        damaged.mkdir()
        (damaged / "state.json").write_text("[]")
        restored = Jobs(Path(self.tmp.name), None, None, None, 30)
        self.addCleanup(restored.shutdown)
        self.assertEqual("interrupted", restored.states["request_0001"]["status"])
        self.assertNotIn("damaged_01", restored.states)
        self.assertEqual("[]", (damaged / "state.json").read_text())


class TransportTest(unittest.TestCase):
    def test_drip_fed_headers_have_an_absolute_deadline(self):
        server = Server(('127.0.0.1', 0), Handler)
        server.header_timeout = 0.2
        server.token = "fixture-only-token-with-at-least-32-bytes"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        stop = threading.Event()
        try:
            with socket.create_connection(server.server_address, timeout=1) as peer:
                peer.sendall(b'GET /v1/libraries HTTP/1.1\r\nX-Slow: ')
                def drip():
                    while not stop.wait(0.02):
                        try:
                            peer.sendall(b'x')
                        except OSError:
                            return
                sender = threading.Thread(target=drip, daemon=True)
                sender.start()
                try:
                    self.assertEqual(peer.recv(1), b'')
                finally:
                    stop.set()
                    sender.join(timeout=1)
        finally:
            stop.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_idle_tls_peer_does_not_block_accept_and_times_out(self):
        fixtures = Path(__file__).resolve().parents[2] / "test/fixtures/steam_relay"
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(fixtures / "cert.pem", fixtures / "key.pem")
        server = Server(("127.0.0.1", 0), Handler, tls_context=context, handshake_timeout=0.5)
        server.token = "fixture-only-token-with-at-least-32-bytes"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        idle = socket.create_connection(server.server_address, timeout=2)
        try:
            client = ssl.create_default_context(cafile=str(fixtures / "cert.pem"))
            client.minimum_version = ssl.TLSVersion.TLSv1_2
            with client.wrap_socket(socket.create_connection(server.server_address, timeout=2), server_hostname="localhost") as active:
                active.sendall(b"GET /v1/capabilities HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                self.assertIn(b"401", active.recv(1024))
            self.assertEqual(b"", idle.recv(1))
        finally:
            idle.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
