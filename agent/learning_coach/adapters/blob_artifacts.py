"""Public blob mirror for interactive visuals (hosted entry only).

The Foundry gateway does not forward the container's /artifacts/{token}
route, so hosted replies would otherwise link to unreachable localhost URLs.
This wrapper keeps the file ArtifactStore authoritative and, when configured
via LEARNING_COACH_ARTIFACT_BLOB_URL, rewrites prepared artifact URLs to a
public blob container and uploads each accepted artifact on commit.

Posture: the container is anonymously readable but not listable; the link is
a bearer capability with a 32-char unguessable token, and a 2-day lifecycle
rule deletes the blobs (see scripts/setup-artifact-blob.sh). Upload failures
never propagate: the turn completes, the link may 404, and the local file
remains for the entries that can serve it. Azure imports stay lazy so the
Space image (which never sets the env var) does not need the SDK.
"""

import logging
from threading import Lock
from urllib.parse import urlsplit

LOG = logging.getLogger(__name__)


class BlobArtifactStore:
    """Artifacts wrapper: local file store + public blob upload on commit."""

    def __init__(self, inner, blob_url: str, *, credential=None):
        parts = urlsplit(blob_url)
        if (parts.scheme not in ("http", "https") or not parts.netloc
                or parts.username or parts.password or parts.query or parts.fragment
                or any(char.isspace() for char in blob_url)):
            raise ValueError("invalid_artifact_blob_url")
        segments = [part for part in parts.path.split("/") if part]
        if len(segments) != 1:
            raise ValueError("invalid_artifact_blob_url")
        self._inner = inner
        self._blob_url = blob_url.rstrip("/")
        self._credential = credential
        self._client = None
        # token -> (session_id, the artifact dict as prepared by inner, whose
        # url still points at the local route and satisfies inner.commit).
        self._prepared = {}
        self._lock = Lock()

    def _ensure_client(self):
        if self._client is None:
            from azure.storage.blob import ContainerClient
            credential = self._credential
            if credential is None:
                from azure.identity import DefaultAzureCredential
                credential = DefaultAzureCredential()
            self._client = ContainerClient.from_container_url(self._blob_url, credential=credential)
        return self._client

    def prepare(self, session_id: str, spec: dict, image=None) -> dict:
        artifact = self._inner.prepare(session_id, spec, image=image)
        with self._lock:
            self._prepared[artifact["token"]] = (session_id, artifact)
        return {**artifact, "url": f"{self._blob_url}/{artifact['token']}.html"}

    def commit(self, session_id: str, artifact: dict) -> None:
        token = artifact.get("token") if isinstance(artifact, dict) else None
        with self._lock:
            prepared = self._prepared.get(token)
        if prepared is None:
            # Never prepared here: let inner raise its usual validation errors.
            self._inner.commit(session_id, artifact)
            return
        # Rebuild the local URL only; inner still validates the token regex,
        # key set, title and sha256(html) against its prepared record.
        self._inner.commit(session_id, {**artifact, "url": prepared[1]["url"]})
        with self._lock:
            self._prepared.pop(token, None)
        try:
            from azure.storage.blob import ContentSettings
            blob = self._ensure_client().get_blob_client(f"{token}.html")
            blob.upload_blob(artifact["html"], overwrite=True,
                             content_settings=ContentSettings(content_type="text/html"))
        except Exception as error:
            LOG.warning("blob_artifact_upload_failed error=%s status=%s",
                        type(error).__name__, getattr(error, "status_code", ""))

    def get(self, token: str):
        return self._inner.get(token)

    def delete_session(self, session_id: str) -> None:
        self._inner.delete_session(session_id)
        with self._lock:
            self._prepared = {key: value for key, value in self._prepared.items()
                              if value[0] != session_id}
