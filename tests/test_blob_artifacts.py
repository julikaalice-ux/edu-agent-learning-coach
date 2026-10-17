"""Offline tests for the public blob mirror over the file ArtifactStore."""

import logging
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from learning_coach.adapters import visuals
from learning_coach.db import artifacts
from learning_coach.adapters.blob_artifacts import BlobArtifactStore

from test_visuals import EXAMPLES

BASE = "https://eduartifacts5044621b.blob.core.windows.net/artifacts"
SPEC = EXAMPLES[0]


class FakeBlobClient:
    def __init__(self, uploads, error=None):
        self.uploads = uploads
        self.error = error

    def upload_blob(self, data, overwrite=False, content_settings=None):
        if self.error is not None:
            raise self.error
        self.uploads.append({"name": self.name, "data": data,
                             "overwrite": overwrite,
                             "content_type": getattr(content_settings, "content_type", None)})


class FakeContainerClient:
    instances = []

    @classmethod
    def from_container_url(cls, url, credential=None):
        instance = cls(url, credential)
        cls.instances.append(instance)
        return instance

    def __init__(self, url, credential=None):
        self.url = url
        self.credential = credential
        self.blobs = []

    def get_blob_client(self, name):
        blob = FakeBlobClient(self.blobs)
        blob.name = name
        return blob


class BlobArtifactStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inner = artifacts.ArtifactStore(self.root, renderer=visuals.render_visual)
        self.store = BlobArtifactStore(self.inner, BASE, credential=object())
        FakeContainerClient.instances = []
        patcher = mock.patch("azure.storage.blob.ContainerClient", FakeContainerClient)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("azure.storage.blob.ContentSettings",
                             side_effect=lambda **kw: type("S", (), kw)())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_prepare_returns_blob_url_and_keeps_inner_pending(self):
        before = {path.name for path in self.root.iterdir()}
        artifact = self.store.prepare("session-one", SPEC)
        self.assertEqual(set(artifact), {"token", "url", "title", "html"})
        self.assertRegex(artifact["token"], r"^[A-Za-z0-9_-]{32}$")
        self.assertEqual(artifact["url"], f"{BASE}/{artifact['token']}.html")
        self.assertIsNone(self.store.get(artifact["token"]))
        self.assertEqual({path.name for path in self.root.iterdir()}, before)

    def test_commit_writes_inner_file_and_uploads_html_blob(self):
        artifact = self.store.prepare("session-one", SPEC)
        self.store.commit("session-one", artifact)
        self.assertEqual(self.inner.get(artifact["token"]), artifact["html"])
        self.assertEqual(len(FakeContainerClient.instances), 1)
        uploads = FakeContainerClient.instances[0].blobs
        self.assertEqual(len(uploads), 1)
        self.assertEqual(uploads[0]["name"], f"{artifact['token']}.html")
        self.assertEqual(uploads[0]["data"], artifact["html"])
        self.assertTrue(uploads[0]["overwrite"])
        self.assertEqual(uploads[0]["content_type"], "text/html")

    def test_upload_failure_is_logged_and_never_raised(self):
        artifact = self.store.prepare("session-one", SPEC)
        FakeContainerClient.instances = []
        patcher = mock.patch("azure.storage.blob.ContainerClient", FakeContainerClient)
        with patcher, self.assertLogs("learning_coach.adapters.blob_artifacts", level="WARNING") as logs:
            failing = mock.patch("azure.storage.blob.ContentSettings",
                                 side_effect=lambda **kw: type("S", (), kw)())
            with failing:
                blob_patch = mock.patch.object(FakeBlobClient, "upload_blob",
                                               side_effect=ConnectionError("offline"))
                with blob_patch:
                    self.store.commit("session-one", artifact)
        self.assertIn("blob_artifact_upload_failed", "\n".join(logs.output))
        self.assertEqual(self.inner.get(artifact["token"]), artifact["html"])

    def test_commit_with_modified_html_fails_before_upload(self):
        artifact = self.store.prepare("session-one", SPEC)
        with self.assertRaises(ValueError):
            self.store.commit("session-one", {**artifact, "html": "tampered"})
        self.assertEqual(FakeContainerClient.instances, [])

    def test_commit_for_other_session_fails_before_upload(self):
        artifact = self.store.prepare("session-one", SPEC)
        with self.assertRaisesRegex(ValueError, "artifact_not_prepared_for_session"):
            self.store.commit("session-two", artifact)
        self.assertEqual(FakeContainerClient.instances, [])

    def test_commit_for_unknown_token_falls_back_to_inner_validation(self):
        with self.assertRaises(ValueError):
            self.store.commit("session-one", {
                "token": "a" * 32, "url": f"{BASE}/{'a' * 32}.html",
                "title": SPEC["title"], "html": "<html></html>"})
        self.assertEqual(FakeContainerClient.instances, [])

    def test_delete_session_delegates_and_clears_wrapper_state(self):
        artifact = self.store.prepare("session-one", SPEC)
        self.store.commit("session-one", artifact)
        self.store.delete_session("session-one")
        self.assertIsNone(self.store.get(artifact["token"]))
        self.assertEqual(self.store._prepared, {})
        with self.assertRaises(ValueError):
            self.store.commit("session-one", artifact)

    def test_get_delegates_to_inner(self):
        artifact = self.store.prepare("session-one", SPEC)
        self.store.commit("session-one", artifact)
        self.assertEqual(self.store.get(artifact["token"]), artifact["html"])

    def test_commit_accepts_either_blob_or_inner_url(self):
        artifact = self.store.prepare("session-one", SPEC)
        local = {**artifact, "url": f"http://localhost:8088/artifacts/{artifact['token']}"}
        self.store.commit("session-one", local)
        self.assertEqual(self.inner.get(artifact["token"]), artifact["html"])
        self.assertEqual(len(FakeContainerClient.instances[0].blobs), 1)

    def test_invalid_blob_urls_rejected(self):
        for url in ("javascript:alert(1)", "https://user:pw@host/c", "https://host/c?x=1",
                    "https://host/a/b", "https://host/", "", "not a url"):
            with self.subTest(url=url):
                with self.assertRaisesRegex(ValueError, "invalid_artifact_blob_url"):
                    BlobArtifactStore(self.inner, url, credential=object())

    def test_client_and_credential_built_lazily(self):
        store = BlobArtifactStore(self.inner, BASE, credential=None)
        with mock.patch("azure.storage.blob.ContainerClient") as container, \
                mock.patch("azure.identity.DefaultAzureCredential") as credential:
            self.assertEqual(container.call_count, 0)
            self.assertEqual(credential.call_count, 0)
            artifact = store.prepare("session-one", SPEC)
            store.commit("session-one", artifact)
            self.assertEqual(container.from_container_url.call_count, 1)
            self.assertEqual(credential.call_count, 1)

    def test_module_import_does_not_load_azure_storage(self):
        code = ("import sys; import learning_coach.adapters.blob_artifacts; "
                "sys.exit(1 if 'azure.storage.blob' in sys.modules else 0)")
        result = subprocess.run([sys.executable, "-c", code],
                                cwd=Path(__file__).resolve().parents[1] / "agent",
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class ServerWiringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_unset_env_returns_plain_artifact_store(self):
        from learning_coach import server
        with mock.patch.dict("os.environ", {"LEARNING_COACH_ARTIFACT_BLOB_URL": ""}, clear=False):
            store = server._artifact_store(Path(self.temp.name), None, object())
        self.assertIsInstance(store, artifacts.ArtifactStore)
        self.assertNotIsInstance(store, BlobArtifactStore)

    def test_set_env_wraps_with_blob_store_and_passed_credential(self):
        from learning_coach import server
        credential = object()
        with mock.patch.dict("os.environ",
                             {"LEARNING_COACH_ARTIFACT_BLOB_URL": BASE}, clear=False):
            store = server._artifact_store(Path(self.temp.name), None, credential)
        self.assertIsInstance(store, BlobArtifactStore)
        self.assertIs(store._credential, credential)
        self.assertIsInstance(store._inner, artifacts.ArtifactStore)

    def test_invalid_env_degrades_to_plain_store_with_warning(self):
        from learning_coach import server
        with mock.patch.dict("os.environ",
                             {"LEARNING_COACH_ARTIFACT_BLOB_URL": "not a url"}, clear=False), \
                self.assertLogs("learning_coach.server", level="WARNING") as logs:
            store = server._artifact_store(Path(self.temp.name), None, object())
        self.assertIsInstance(store, artifacts.ArtifactStore)
        self.assertIn("artifact_blob_wiring_disabled", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
