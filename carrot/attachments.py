"""Files attached to a chat turn: images, PDFs, and plain text.

Two different mechanisms, because they have different requirements:

* **Images** only work if the model can actually see. They are passed to
  the model as image data (Ollama's ``images`` field, or the OpenAI
  ``image_url`` content block), and Carrot refuses rather than silently
  dropping them when the chosen model has no vision capability — a model
  that ignores your screenshot and answers anyway is worse than an error.
* **Documents** (PDF, text, markdown, code) are extracted to text here and
  folded into the prompt, so they work with *every* model, vision or not.

Everything is processed locally. Nothing is uploaded.
"""
import base64
import binascii
import io
import os
from typing import Any, Dict, List, Optional, Tuple

MAX_FILE_BYTES = 20 * 1024 * 1024        # per attachment
MAX_TOTAL_BYTES = 60 * 1024 * 1024       # per turn
MAX_DOC_CHARS = 40_000                   # extracted text folded into a prompt
MAX_PDF_PAGES = 200

IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp"}
TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".log", ".html", ".xml", ".py", ".js", ".ts", ".tsx",
    ".jsx", ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs", ".rb", ".php",
    ".sh", ".sql", ".swift", ".kt", ".scala", ".r", ".m", ".lua", ".pl",
}

# Families that can see, for when the server does not report capabilities.
VISION_HINTS = ("llava", "vision", "-vl", "vl-", "bakllava", "moondream",
                "minicpm-v", "gemma3", "gemma4", "pixtral", "internvl", "qwen2-vl",
                "qwen2.5vl", "granite3.2-vision", "mistral-small3")


class AttachmentError(ValueError):
    """A problem the user can fix — bad file, too big, wrong model."""


def _decode(data_base64: str) -> bytes:
    payload = (data_base64 or "").strip()
    if payload.startswith("data:"):          # strip a data: URL prefix
        _, _, payload = payload.partition(",")
    try:
        return base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise AttachmentError(f"could not decode attachment: {exc}") from exc


def _sniff(name: str, mime: str, blob: bytes) -> str:
    """Return 'image', 'pdf' or 'text' from the content itself where possible.

    The declared MIME comes from the browser and can be wrong or absent, so
    magic bytes win when they are conclusive.
    """
    if blob[:4] == b"%PDF":
        return "pdf"
    if (blob[:8] == b"\x89PNG\r\n\x1a\n" or blob[:3] == b"\xff\xd8\xff"
            or blob[:6] in (b"GIF87a", b"GIF89a")
            or (blob[:4] == b"RIFF" and blob[8:12] == b"WEBP")
            or blob[:2] == b"BM"):
        return "image"
    if (mime or "").lower() in IMAGE_MIMES:
        return "image"
    if (mime or "").lower() == "application/pdf":
        return "pdf"
    if os.path.splitext(name or "")[1].lower() in TEXT_SUFFIXES:
        return "text"
    if (mime or "").lower().startswith("text/"):
        return "text"
    raise AttachmentError(
        f"{name or 'attachment'}: unsupported file type. "
        "Attach an image, a PDF, or a text/code file.")


def _pdf_reader_class():
    """Import pypdf defensively.

    pypdf pulls in ``cryptography`` for encrypted PDFs, and a mismatched
    build of that package raises a Rust panic rather than ImportError —
    which would otherwise take down the whole chat turn. Catch anything.
    """
    try:
        from pypdf import PdfReader
        return PdfReader
    except BaseException as exc:                              # noqa: BLE001
        raise AttachmentError(
            "PDF support is unavailable on this install "
            f"({type(exc).__name__}). Reinstall with `pip install -U pypdf cryptography`, "
            "or paste the text instead."
        ) from None


def extract_pdf_text(blob: bytes, name: str = "document.pdf") -> str:
    PdfReader = _pdf_reader_class()
    try:
        reader = PdfReader(io.BytesIO(blob))
    except Exception as exc:
        raise AttachmentError(f"{name}: could not read this PDF ({exc})") from exc
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception:
            raise AttachmentError(f"{name}: this PDF is password-protected")
    pages = []
    for index, page in enumerate(reader.pages[:MAX_PDF_PAGES]):
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
    text = "\n\n".join(p.strip() for p in pages if p.strip())
    if not text.strip():
        raise AttachmentError(
            f"{name}: no selectable text — this looks like a scanned PDF. "
            "Attach it as an image instead so a vision model can read it.")
    return text


def model_supports_vision(model: str, ollama_client=None) -> bool:
    """Whether ``model`` can accept images.

    Asks the server first (Ollama reports capabilities), then falls back to
    recognising known vision families by name.
    """
    name = (model or "").lower()
    if ollama_client is not None:
        try:
            caps = ollama_client.capabilities(model)
            if caps:
                return "vision" in caps
        except Exception:
            pass
    return any(hint in name for hint in VISION_HINTS)


def process(attachments: List[Dict[str, Any]]) -> Tuple[List[str], List[Dict[str, str]]]:
    """Split raw attachments into (image_base64_list, document_records).

    Documents come back as ``{"name": ..., "text": ...}`` ready to fold into
    the prompt. Raises AttachmentError with a message meant for the user.
    """
    images: List[str] = []
    documents: List[Dict[str, str]] = []
    total = 0

    for item in attachments or []:
        name = (item.get("name") or "attachment").strip()
        blob = _decode(item.get("data") or item.get("data_base64") or "")
        if not blob:
            raise AttachmentError(f"{name}: the file is empty")
        if len(blob) > MAX_FILE_BYTES:
            raise AttachmentError(
                f"{name} is {len(blob) // (1024 * 1024)} MB — the limit is "
                f"{MAX_FILE_BYTES // (1024 * 1024)} MB per file")
        total += len(blob)
        if total > MAX_TOTAL_BYTES:
            raise AttachmentError("those attachments are too large altogether — "
                                  f"the limit is {MAX_TOTAL_BYTES // (1024 * 1024)} MB per message")

        kind = _sniff(name, item.get("mime") or item.get("type") or "", blob)
        if kind == "image":
            images.append(base64.b64encode(blob).decode("ascii"))
        elif kind == "pdf":
            documents.append({"name": name, "text": extract_pdf_text(blob, name)})
        else:
            try:
                text = blob.decode("utf-8", errors="replace")
            except Exception as exc:
                raise AttachmentError(f"{name}: could not read as text ({exc})") from exc
            if not text.strip():
                raise AttachmentError(f"{name}: the file has no text in it")
            documents.append({"name": name, "text": text})
    return images, documents


def documents_prompt(documents: List[Dict[str, str]]) -> str:
    """Fold extracted document text into one system block.

    Each document is truncated on its own so one huge PDF cannot crowd the
    others out, and the truncation is stated rather than hidden.
    """
    if not documents:
        return ""
    from . import policy

    budget = max(MAX_DOC_CHARS // max(len(documents), 1), 2000)
    parts = ["The user attached the following file(s). Use them to answer."]
    for doc in documents:
        text = doc["text"]
        if len(text) > budget:
            text = text[:budget] + f"\n[… truncated, {len(doc['text']) - budget} more characters]"
        # The user chose to attach it; they did not write what is inside it.
        # A PDF is a document somebody else composed, which makes it exactly
        # the same kind of input as a web page — screened, and enveloped so
        # the model reads it as material rather than as orders.
        parts.append(f"\n--- {doc['name']} ---\n"
                     + policy.ingest(text, origin=f"attached file: {doc['name']}"))
    return "\n".join(parts)


def describe(images: List[str], documents: List[Dict[str, str]]) -> str:
    """A short human summary to store alongside the user's message."""
    bits = []
    if images:
        bits.append(f"{len(images)} image{'s' if len(images) != 1 else ''}")
    if documents:
        names = ", ".join(d["name"] for d in documents[:3])
        more = f" +{len(documents) - 3} more" if len(documents) > 3 else ""
        bits.append(f"{names}{more}")
    return "attached: " + "; ".join(bits) if bits else ""
