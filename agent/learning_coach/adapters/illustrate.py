"""Text-to-image backends for the ``illustration`` visual kind.

Two backends, selected by ``LEARNING_COACH_IMAGE_BACKEND``:

- ``flux`` (default): Foundry Black Forest Labs provider route, deployment
  ``LEARNING_COACH_FLUX_DEPLOYMENT`` (default ``flux-2-pro``). Uses the
  signed-in Azure identity; the resource has key authentication disabled.
- ``pollinations``: keyless public endpoint, kept only as an offline-demo
  fallback. Lower quality and watermarked.

The caller (pipeline) only sees :func:`generate_image` returning PNG/JPEG
bytes; failures raise and are handled by the existing visual fallback.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
import time
import urllib.parse
import urllib.request

from azure.identity import DefaultAzureCredential

FLUX_SCOPE = "https://ai.azure.com/.default"
TIMEOUT_SECONDS = 90
DEFAULT_FLUX_DEPLOYMENT = "flux-2-pro"
# Labels are drawn by the application, never by the image model: generated
# lettering is often wrong, especially for Chinese.
NO_TEXT_SUFFIX = " No text, letters, captions, labels, logos or watermarks anywhere in the image."


def with_no_text(prompt: str) -> str:
    return prompt.rstrip() + NO_TEXT_SUFFIX


def _default_account_endpoint() -> str | None:
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "")
    if "/api/projects/" in endpoint:
        return endpoint.split("/api/projects/", 1)[0].rstrip("/")
    return None


def _flux_url() -> str:
    account = os.environ.get("LEARNING_COACH_FLUX_ACCOUNT_ENDPOINT") or _default_account_endpoint()
    deployment = os.environ.get("LEARNING_COACH_FLUX_DEPLOYMENT", DEFAULT_FLUX_DEPLOYMENT)
    if not account:
        raise RuntimeError("flux_endpoint_unavailable")
    return (f"{account}/providers/blackforestlabs/v1/{deployment}"
            f"?api-version=preview")


_TOKEN_LOCK = threading.Lock()
_CREDENTIAL = None
_TOKEN = None


def use_credential(credential) -> None:
    """Share the app's credential, so pictures do not sign in a second time."""
    global _CREDENTIAL, _TOKEN
    with _TOKEN_LOCK:
        _CREDENTIAL, _TOKEN = credential, None


def _flux_token() -> str:
    """One credential and one cached token for every picture.

    Building a new credential per picture walks the whole sign-in chain each
    time; on a laptop signed in with the Azure CLI that alone took about
    30 seconds. The token is reused until five minutes before it expires.
    """
    global _CREDENTIAL, _TOKEN
    with _TOKEN_LOCK:
        if _TOKEN is None or _TOKEN.expires_on - time.time() < 300:
            if _CREDENTIAL is None:
                _CREDENTIAL = DefaultAzureCredential()
            _TOKEN = _CREDENTIAL.get_token(FLUX_SCOPE)
        return _TOKEN.token


def _flux_request(prompt: str, width: int, height: int) -> bytes:
    token = _flux_token()
    body = json.dumps({
        "model": os.environ.get("LEARNING_COACH_FLUX_DEPLOYMENT", DEFAULT_FLUX_DEPLOYMENT),
        "prompt": prompt,
        "width": width,
        "height": height,
        "output_format": "png",
    }).encode()
    request = urllib.request.Request(_flux_url(), data=body, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read())
    item = payload["data"][0]
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"])
    if item.get("url"):
        with urllib.request.urlopen(item["url"], timeout=TIMEOUT_SECONDS) as image:
            return image.read()
    raise RuntimeError("flux_empty_response")


def _pollinations_request(prompt: str, width: int, height: int) -> bytes:
    query = urllib.parse.urlencode({
        "width": width, "height": height, "nologo": "true", "model": "flux",
        "seed": str(abs(hash(prompt)) % 10_000_000),
    })
    url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(prompt)}?{query}"
    request = urllib.request.Request(url, headers={"User-Agent": "edu-agent-demo/1.0"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        content_type = response.headers.get("Content-Type", "")
        data = response.read()
    if not data or ("image" not in content_type and not data.startswith(b"\xff\xd8") and not data.startswith(b"\x89PNG")):
        raise RuntimeError("pollinations_invalid_image")
    return data


def _is_png(data: bytes) -> bool:
    return data.startswith(b"\x89PNG")


def image_page_html(spec: dict, image: bytes) -> str:
    """Self-contained viewer page for a generated illustration."""
    import html as html_module

    mime = "image/png" if _is_png(image) else "image/jpeg"
    data_url = f"data:{mime};base64," + base64.b64encode(image).decode("ascii")
    title = html_module.escape(spec["title"], quote=True)
    alt = html_module.escape(spec.get("alt") or spec["title"], quote=True)
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{title}</title>"
        "<style>body{margin:0;font-family:system-ui,sans-serif;background:#edf3f6;"
        "display:flex;flex-direction:column;align-items:center;padding:16px}"
        "img{max-width:100%;border-radius:12px;background:white}"
        "p{color:#425d70;font-size:.9rem}</style></head>"
        f'<body><img src="{data_url}" alt="{alt}"><p>{title}</p></body></html>'
    )


async def generate_image(prompt: str, *, width: int = 1024, height: int = 1024) -> bytes:
    """Generate one image and return its bytes; raise on any failure."""
    backend = os.environ.get("LEARNING_COACH_IMAGE_BACKEND", "flux")
    prompt = with_no_text(prompt)
    if backend == "pollinations":
        return await asyncio.to_thread(_pollinations_request, prompt, width, height)
    return await asyncio.to_thread(_flux_request, prompt, width, height)
