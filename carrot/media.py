"""Image and video generation — hosted and on-device, behind one interface.

The same local-first bargain the text side already makes: if you have a GPU and
a Stable Diffusion server, nothing leaves the machine; if you would rather pay
someone else's GPU, paste a key. Neither choice changes anything above this
module, because every backend returns the same thing — bytes, a mime type, and
what it cost you.

Backends fall into three groups:

* **On-device.** An Automatic1111 or ComfyUI server already running on
  localhost. Carrot does not bundle a diffusion model: they are multi-gigabyte,
  licensed individually, and the people who want local image generation
  overwhelmingly already have one of these running. Talking to it is a far
  better deal than shipping a second model runtime.
* **Direct HTTP.** OpenAI and Stability return image bytes from one request.
* **Polled.** Replicate and fal start a job and hand back a URL to watch, which
  is also how essentially all video generation works. One polling loop serves
  both, with a hard ceiling so a stuck job cannot hang a chat turn forever.

Generated media is written to the data directory and returned as an artifact,
so a picture shows up in the conversation rather than as a path the user has to
go find.
"""

from __future__ import annotations

import base64
import os
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

import requests

from .config import CARROT_DIR, get_config, set_config

KIND_IMAGE = "image"
KIND_VIDEO = "video"

# A generation is slow by nature. These are the ceilings past which we stop
# waiting and say so, rather than holding a chat turn open indefinitely.
REQUEST_TIMEOUT = 180
POLL_INTERVAL = 2.0
POLL_CEILING_IMAGE = 180
POLL_CEILING_VIDEO = 900

MAX_BYTES = 32 * 1024 * 1024
MEDIA_DIRNAME = "media"

MIME_BY_SUFFIX = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif",
    ".mp4": "video/mp4", ".webm": "video/webm",
}


class MediaError(RuntimeError):
    """Anything that stopped a generation, phrased for the user."""


# ===== Backend registry =====
#
# `key` names the provider whose stored API key this backend uses, so a key
# pasted once for chat also works for images. An empty `key` means on-device.

BACKENDS: Dict[str, Dict[str, Any]] = {
    "automatic1111": {
        "label": "Stable Diffusion (on-device, Automatic1111)",
        "kinds": [KIND_IMAGE],
        "key": "",
        "local": True,
        "base_url": "http://127.0.0.1:7860",
        "default_model": "",
        "docs": "https://github.com/AUTOMATIC1111/stable-diffusion-webui",
        "note": "Start the WebUI with --api. Nothing leaves your machine.",
    },
    "comfyui": {
        "label": "ComfyUI (on-device)",
        "kinds": [KIND_IMAGE],
        "key": "",
        "local": True,
        "base_url": "http://127.0.0.1:8188",
        "default_model": "",
        "docs": "https://github.com/comfyanonymous/ComfyUI",
        "note": "Needs a saved API-format workflow. Nothing leaves your machine.",
    },
    "openai": {
        "label": "OpenAI (gpt-image-1)",
        "kinds": [KIND_IMAGE],
        "key": "openai",
        "local": False,
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-image-1",
        "docs": "https://platform.openai.com/docs/guides/images",
    },
    "stability": {
        "label": "Stability AI",
        "kinds": [KIND_IMAGE],
        "key": "stability",
        "local": False,
        "base_url": "https://api.stability.ai",
        "default_model": "core",
        "docs": "https://platform.stability.ai",
    },
    "replicate": {
        "label": "Replicate",
        "kinds": [KIND_IMAGE, KIND_VIDEO],
        "key": "replicate",
        "local": False,
        "base_url": "https://api.replicate.com/v1",
        "default_model": "black-forest-labs/flux-schnell",
        "default_video_model": "wan-video/wan-2.5-t2v",
        "docs": "https://replicate.com",
    },
    "fal": {
        "label": "fal.ai",
        "kinds": [KIND_IMAGE, KIND_VIDEO],
        "key": "fal",
        "local": False,
        "base_url": "https://fal.run",
        "default_model": "fal-ai/flux/schnell",
        "default_video_model": "fal-ai/ltx-video",
        "docs": "https://fal.ai",
    },
}


def backends(kind: str = "") -> List[Dict[str, Any]]:
    """Every backend, each carrying whether it is usable right now."""
    listed = []
    for backend_id, spec in BACKENDS.items():
        if kind and kind not in spec["kinds"]:
            continue
        listed.append({
            "id": backend_id,
            "label": spec["label"],
            "kinds": spec["kinds"],
            "local": spec["local"],
            "docs": spec.get("docs", ""),
            "note": spec.get("note", ""),
            "base_url": base_url(backend_id),
            "configured": configured(backend_id),
        })
    return listed


def base_url(backend_id: str) -> str:
    """The endpoint, with the user's override winning.

    Local backends move around — a second GPU box on the LAN, a non-default
    port — so the URL has to be settable without editing code.
    """
    spec = _spec(backend_id)
    override = (get_config().get("media_endpoints", {}) or {}).get(backend_id, "")
    return (override or spec["base_url"]).rstrip("/")


def api_key(backend_id: str) -> str:
    """The key this backend uses, shared with the chat provider of that name."""
    from . import providers as providers_mod

    spec = _spec(backend_id)
    if not spec["key"]:
        return ""
    stored = (get_config().get("media_keys", {}) or {}).get(backend_id, "")
    return stored or providers_mod.api_key(spec["key"])


def set_api_key(backend_id: str, key: str) -> None:
    _spec(backend_id)
    keys = dict(get_config().get("media_keys", {}) or {})
    if key:
        keys[backend_id] = key
    else:
        keys.pop(backend_id, None)
    set_config("media_keys", keys)


def set_endpoint(backend_id: str, url: str) -> None:
    _spec(backend_id)
    endpoints = dict(get_config().get("media_endpoints", {}) or {})
    if url:
        endpoints[backend_id] = url.rstrip("/")
    else:
        endpoints.pop(backend_id, None)
    set_config("media_endpoints", endpoints)


def configured(backend_id: str) -> bool:
    """Local backends are configured by existing; hosted ones need a key.

    Reachability is deliberately not checked here — this is called to draw a
    settings list, and a per-row HTTP probe would make that page crawl.
    """
    spec = _spec(backend_id)
    return True if spec["local"] else bool(api_key(backend_id))


def _spec(backend_id: str) -> Dict[str, Any]:
    spec = BACKENDS.get(backend_id)
    if not spec:
        raise MediaError(f"unknown media backend: {backend_id}")
    return spec


def default_backend(kind: str = KIND_IMAGE) -> str:
    """What to use when the caller did not say.

    A configured choice wins. Otherwise the first *local* backend that is set
    up, then the first configured hosted one — local-first, in the one place
    where "first" is actually a decision rather than a slogan.
    """
    chosen = get_config().get(
        "media_backend_video" if kind == KIND_VIDEO else "media_backend_image", ""
    )
    if chosen and chosen in BACKENDS and kind in BACKENDS[chosen]["kinds"]:
        return chosen
    usable = [b for b in backends(kind) if b["configured"]]
    local = [b for b in usable if b["local"]]
    if local:
        return local[0]["id"]
    if usable:
        return usable[0]["id"]
    raise MediaError(
        f"no {kind} backend is set up. Add an API key in Settings → Media, or "
        f"start a local Stable Diffusion server."
    )


# ===== Storage =====

def media_dir() -> str:
    path = os.path.join(CARROT_DIR, MEDIA_DIRNAME)
    os.makedirs(path, exist_ok=True)
    return path


def save(data: bytes, suffix: str = ".png") -> Dict[str, Any]:
    """Write generated bytes next to the rest of Carrot's data."""
    if not data:
        raise MediaError("the backend returned no data")
    if len(data) > MAX_BYTES:
        raise MediaError(f"generated file is too large ({len(data) // (1024 * 1024)}MB)")
    name = f"{int(time.time())}-{uuid.uuid4().hex[:8]}{suffix}"
    full = os.path.join(media_dir(), name)
    with open(full, "wb") as handle:
        handle.write(data)
    return {
        "path": full,
        "name": name,
        "bytes": len(data),
        "mime": MIME_BY_SUFFIX.get(suffix, "application/octet-stream"),
    }


def data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


# ===== HTTP helpers =====

def _post(url: str, *, headers: Dict[str, str], json_body: Optional[Dict[str, Any]] = None,
          data: Optional[Dict[str, Any]] = None, files: Optional[Dict[str, Any]] = None,
          timeout: int = REQUEST_TIMEOUT):
    try:
        response = requests.post(url, headers=headers, json=json_body, data=data,
                                 files=files, timeout=timeout)
    except requests.RequestException as exc:
        raise MediaError(_connection_message(url, exc))
    if response.status_code >= 400:
        raise MediaError(_http_message(response))
    return response


def _get(url: str, *, headers: Dict[str, str], timeout: int = REQUEST_TIMEOUT):
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise MediaError(_connection_message(url, exc))
    if response.status_code >= 400:
        raise MediaError(_http_message(response))
    return response


def _connection_message(url: str, exc: Exception) -> str:
    """A refused connection to localhost means "it isn't running", so say that."""
    if "127.0.0.1" in url or "localhost" in url:
        return (
            f"could not reach the local server at {url}. Is it running, and was "
            f"it started with its API enabled?"
        )
    return f"could not reach {url}: {exc}"


def _http_message(response) -> str:
    body = ""
    try:
        payload = response.json()
        body = (
            payload.get("error", {}).get("message")
            if isinstance(payload.get("error"), dict) else payload.get("error")
        ) or payload.get("detail") or payload.get("message") or ""
    except Exception:
        body = (response.text or "")[:300]
    if response.status_code in (401, 403):
        return f"the API key was rejected ({response.status_code}). {body}".strip()
    if response.status_code == 429:
        return f"rate limited by the provider. {body}".strip()
    return f"generation failed ({response.status_code}). {body}".strip()


def _download(url: str) -> bytes:
    response = _get(url, headers={})
    return response.content


def _suffix_for(url: str, default: str) -> str:
    for suffix in MIME_BY_SUFFIX:
        if url.lower().split("?", 1)[0].endswith(suffix):
            return suffix
    return default


# ===== Backends =====

def _generate_automatic1111(prompt: str, *, backend_id: str, negative: str = "",
                            width: int = 1024, height: int = 1024, steps: int = 25,
                            **_) -> List[bytes]:
    response = _post(
        f"{base_url(backend_id)}/sdapi/v1/txt2img",
        headers={"Content-Type": "application/json"},
        json_body={
            "prompt": prompt, "negative_prompt": negative,
            "width": width, "height": height, "steps": steps,
        },
    )
    images = response.json().get("images") or []
    if not images:
        raise MediaError("the local server returned no images")
    return [base64.b64decode(_strip_data_uri(image)) for image in images]


def _generate_comfyui(prompt: str, *, backend_id: str, workflow: Optional[Dict] = None, **_) -> List[bytes]:
    """ComfyUI runs a saved workflow, so it needs one before it can be asked.

    Failing with the reason is much better than sending an empty graph and
    reporting whatever ComfyUI says about it.
    """
    graph = workflow or get_config().get("media_comfy_workflow") or None
    if not graph:
        raise MediaError(
            "ComfyUI needs a workflow. Export one from ComfyUI with "
            "'Save (API Format)' and paste it in Settings → Media."
        )
    queued = _post(f"{base_url(backend_id)}/prompt",
                   headers={"Content-Type": "application/json"},
                   json_body={"prompt": _fill_comfy_prompt(graph, prompt)}).json()
    prompt_id = queued.get("prompt_id")
    if not prompt_id:
        raise MediaError("ComfyUI did not queue the job")

    deadline = time.time() + POLL_CEILING_IMAGE
    while time.time() < deadline:
        history = _get(f"{base_url(backend_id)}/history/{prompt_id}", headers={}).json()
        entry = history.get(prompt_id)
        if entry:
            images = [
                image
                for output in entry.get("outputs", {}).values()
                for image in output.get("images", [])
            ]
            if not images:
                raise MediaError("the workflow finished but produced no images")
            return [
                _get(
                    f"{base_url(backend_id)}/view?filename={image['filename']}"
                    f"&subfolder={image.get('subfolder', '')}&type={image.get('type', 'output')}",
                    headers={},
                ).content
                for image in images
            ]
        time.sleep(POLL_INTERVAL)
    raise MediaError(f"ComfyUI did not finish within {POLL_CEILING_IMAGE}s")


def _fill_comfy_prompt(graph: Dict[str, Any], prompt: str) -> Dict[str, Any]:
    """Put the user's text into the workflow's positive prompt node.

    Substituting into the first CLIPTextEncode is a heuristic, but the
    alternative — making people hand-edit JSON for every generation — is not a
    feature anyone would use.
    """
    filled = {k: dict(v) for k, v in graph.items()}
    for node in filled.values():
        if node.get("class_type") == "CLIPTextEncode":
            inputs = dict(node.get("inputs", {}))
            if "text" in inputs:
                inputs["text"] = prompt
                node["inputs"] = inputs
                break
    return filled


def _generate_openai(prompt: str, *, backend_id: str, model: str = "", count: int = 1,
                     size: str = "1024x1024", **_) -> List[bytes]:
    response = _post(
        f"{base_url(backend_id)}/images/generations",
        headers={"Authorization": f"Bearer {api_key(backend_id)}",
                 "Content-Type": "application/json"},
        json_body={
            "model": model or _spec(backend_id)["default_model"],
            "prompt": prompt, "n": max(1, min(int(count), 4)), "size": size,
        },
    )
    out = []
    for item in response.json().get("data", []):
        if item.get("b64_json"):
            out.append(base64.b64decode(item["b64_json"]))
        elif item.get("url"):
            out.append(_download(item["url"]))
    if not out:
        raise MediaError("OpenAI returned no image data")
    return out


def _generate_stability(prompt: str, *, backend_id: str, model: str = "",
                        negative: str = "", **_) -> List[bytes]:
    endpoint = f"{base_url(backend_id)}/v2beta/stable-image/generate/{model or 'core'}"
    body = {"prompt": prompt, "output_format": "png"}
    if negative:
        body["negative_prompt"] = negative
    response = _post(
        endpoint,
        headers={"Authorization": f"Bearer {api_key(backend_id)}", "Accept": "image/*"},
        # Stability's v2 endpoints take multipart, not JSON.
        files={"none": ""},
        data=body,
    )
    return [response.content]


def _generate_replicate(prompt: str, *, backend_id: str, model: str = "",
                        kind: str = KIND_IMAGE, **extra) -> List[bytes]:
    spec = _spec(backend_id)
    chosen = model or (
        spec["default_video_model"] if kind == KIND_VIDEO else spec["default_model"]
    )
    started = _post(
        f"{base_url(backend_id)}/models/{chosen}/predictions",
        headers={"Authorization": f"Bearer {api_key(backend_id)}",
                 "Content-Type": "application/json", "Prefer": "respond-async"},
        json_body={"input": {"prompt": prompt, **_clean(extra)}},
    ).json()
    return _poll_replicate(started, backend_id, kind)


def _poll_replicate(started: Dict[str, Any], backend_id: str, kind: str) -> List[bytes]:
    ceiling = POLL_CEILING_VIDEO if kind == KIND_VIDEO else POLL_CEILING_IMAGE
    url = (started.get("urls") or {}).get("get")
    state = started
    deadline = time.time() + ceiling
    headers = {"Authorization": f"Bearer {api_key(backend_id)}"}
    while state.get("status") in ("starting", "processing") and url:
        if time.time() > deadline:
            raise MediaError(f"the job did not finish within {ceiling}s")
        time.sleep(POLL_INTERVAL)
        state = _get(url, headers=headers).json()
    if state.get("status") == "failed":
        raise MediaError(state.get("error") or "the provider reported the job failed")
    if state.get("status") == "canceled":
        raise MediaError("the job was cancelled")
    output = state.get("output")
    urls = [output] if isinstance(output, str) else [u for u in (output or []) if isinstance(u, str)]
    if not urls:
        raise MediaError("the job finished but returned nothing")
    return [_download(u) for u in urls]


def _generate_fal(prompt: str, *, backend_id: str, model: str = "",
                  kind: str = KIND_IMAGE, **extra) -> List[bytes]:
    spec = _spec(backend_id)
    chosen = model or (
        spec["default_video_model"] if kind == KIND_VIDEO else spec["default_model"]
    )
    payload = _post(
        f"{base_url(backend_id)}/{chosen}",
        headers={"Authorization": f"Key {api_key(backend_id)}",
                 "Content-Type": "application/json"},
        json_body={"prompt": prompt, **_clean(extra)},
        timeout=POLL_CEILING_VIDEO if kind == KIND_VIDEO else REQUEST_TIMEOUT,
    ).json()
    items = payload.get("images") or payload.get("video") or payload.get("videos") or []
    if isinstance(items, dict):
        items = [items]
    urls = [item.get("url") for item in items if isinstance(item, dict) and item.get("url")]
    if not urls:
        raise MediaError("fal returned no media")
    return [_download(u) for u in urls]


def _clean(extra: Dict[str, Any]) -> Dict[str, Any]:
    """Only pass through parameters the caller actually set."""
    drop = {"backend_id", "kind", "model", "count", "conversation_id", "title"}
    return {k: v for k, v in extra.items() if k not in drop and v not in (None, "", 0)}


def _strip_data_uri(value: str) -> str:
    return value.split(",", 1)[1] if value.startswith("data:") else value


HANDLERS: Dict[str, Callable[..., List[bytes]]] = {
    "automatic1111": _generate_automatic1111,
    "comfyui": _generate_comfyui,
    "openai": _generate_openai,
    "stability": _generate_stability,
    "replicate": _generate_replicate,
    "fal": _generate_fal,
}


# ===== The one entry point =====

def generate(prompt: str, *, kind: str = KIND_IMAGE, backend: str = "",
             conversation_id: str = "", title: str = "", **options) -> Dict[str, Any]:
    """Generate media and return it as saved files plus an artifact.

    Every backend funnels through here so the caller never has to know which
    one ran — the artifact it gets back renders the same either way.
    """
    text = (prompt or "").strip()
    if not text:
        raise MediaError("a generation needs a prompt")
    if kind not in (KIND_IMAGE, KIND_VIDEO):
        raise MediaError(f"unknown media kind: {kind}")

    backend_id = backend or default_backend(kind)
    spec = _spec(backend_id)
    if kind not in spec["kinds"]:
        raise MediaError(f"{spec['label']} cannot generate {kind}")
    if not configured(backend_id):
        raise MediaError(
            f"{spec['label']} has no API key yet — add one in Settings → Media."
        )

    started = time.time()
    blobs = HANDLERS[backend_id](text, backend_id=backend_id, kind=kind, **options)
    suffix = ".mp4" if kind == KIND_VIDEO else ".png"
    saved = [save(blob, suffix) for blob in blobs]

    result = {
        "kind": kind,
        "backend": backend_id,
        "backend_label": spec["label"],
        "local": spec["local"],
        "prompt": text,
        "seconds": round(time.time() - started, 1),
        "files": saved,
    }
    # An image belongs in the conversation, not in a folder the user has to go
    # find. Video is linked rather than inlined: the artifact store holds text,
    # and a base64 mp4 would blow straight past its size ceiling.
    if kind == KIND_IMAGE and saved:
        result["artifact"] = _as_artifact(saved[0], text, title, conversation_id)
    return result


def _as_artifact(saved: Dict[str, Any], prompt: str, title: str, conversation_id: str):
    from . import artifacts as artifacts_mod

    try:
        with open(saved["path"], "rb") as handle:
            blob = handle.read()
        return artifacts_mod.create(
            artifacts_mod.KIND_IMAGE,
            data_uri(blob, saved["mime"]),
            title=title or prompt[:60],
            conversation_id=conversation_id,
            meta={"generated": True, "prompt": prompt},
        )
    except Exception:
        # A picture that saved but could not be filed is still a success.
        return None
