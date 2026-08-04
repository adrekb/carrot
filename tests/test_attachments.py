"""Tests for chat attachments: images, PDFs, text, and vision gating."""
import base64
import io

import pytest

from carrot import attachments as att


def b64(blob: bytes) -> str:
    return base64.b64encode(blob).decode()


PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _pdf_usable() -> bool:
    """pypdf imports cryptography, which is broken in some environments."""
    try:
        from pypdf import PdfReader  # noqa: F401
        return True
    except BaseException:
        return False


def make_pdf(text: str = "Hello from a PDF") -> bytes:
    """A minimal one-page PDF with extractable text."""
    if not _pdf_usable():
        pytest.skip("pypdf unusable in this environment")
    try:
        from reportlab.pdfgen import canvas  # noqa: F401
        have_reportlab = True
    except ImportError:
        have_reportlab = False
    if not have_reportlab:
        pytest.skip("reportlab not installed; cannot synthesise a text PDF")
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, text)
    c.save()
    return buf.getvalue()


# ===== Type sniffing =====

def test_sniffs_image_from_magic_bytes_even_with_wrong_mime():
    images, docs = att.process([{"name": "shot", "mime": "application/octet-stream",
                                 "data": b64(PNG_1PX)}])
    assert len(images) == 1 and not docs


def test_sniffs_pdf_from_magic_bytes():
    assert att._sniff("x", "", b"%PDF-1.7\nrest") == "pdf"


def test_text_recognised_by_extension():
    images, docs = att.process([{"name": "notes.md", "mime": "",
                                 "data": b64(b"# Title\nbody text")}])
    assert not images
    assert docs[0]["name"] == "notes.md" and "body text" in docs[0]["text"]


def test_unknown_type_is_rejected_with_a_useful_message():
    with pytest.raises(att.AttachmentError) as exc:
        att.process([{"name": "thing.bin", "mime": "", "data": b64(b"\x00\x01\x02binary")}])
    assert "unsupported file type" in str(exc.value)


def test_data_url_prefix_is_stripped():
    images, _ = att.process([{"name": "p.png", "mime": "image/png",
                              "data": "data:image/png;base64," + b64(PNG_1PX)}])
    assert len(images) == 1


# ===== Limits =====

def test_single_file_size_limit(monkeypatch):
    monkeypatch.setattr(att, "MAX_FILE_BYTES", 100)
    with pytest.raises(att.AttachmentError) as exc:
        att.process([{"name": "big.txt", "mime": "text/plain", "data": b64(b"x" * 200)}])
    assert "limit" in str(exc.value)


def test_total_size_limit(monkeypatch):
    monkeypatch.setattr(att, "MAX_TOTAL_BYTES", 150)
    with pytest.raises(att.AttachmentError) as exc:
        att.process([
            {"name": "a.txt", "mime": "text/plain", "data": b64(b"x" * 100)},
            {"name": "b.txt", "mime": "text/plain", "data": b64(b"y" * 100)},
        ])
    assert "altogether" in str(exc.value)


def test_empty_file_is_rejected():
    with pytest.raises(att.AttachmentError):
        att.process([{"name": "empty.txt", "mime": "text/plain", "data": ""}])


# ===== PDFs =====

def test_pdf_text_is_extracted():
    pdf = make_pdf("Carrot attachment test")
    images, docs = att.process([{"name": "doc.pdf", "mime": "application/pdf",
                                 "data": b64(pdf)}])
    assert not images
    assert "Carrot attachment test" in docs[0]["text"]


def test_scanned_pdf_says_to_attach_as_image():
    """A PDF with no text layer should explain itself, not fail silently."""
    if not _pdf_usable():
        pytest.skip("pypdf unusable in this environment")
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    with pytest.raises(att.AttachmentError) as exc:
        att.extract_pdf_text(buf.getvalue(), "scan.pdf")
    assert "image" in str(exc.value).lower()


# ===== Prompt folding =====

def test_documents_prompt_truncates_per_document(monkeypatch):
    monkeypatch.setattr(att, "MAX_DOC_CHARS", 4000)
    docs = [{"name": "a.txt", "text": "A" * 5000}, {"name": "b.txt", "text": "B" * 100}]
    prompt = att.documents_prompt(docs)
    # Both are represented; the long one is cut and says so.
    assert "a.txt" in prompt and "b.txt" in prompt
    assert "truncated" in prompt
    assert "B" * 100 in prompt


def test_documents_prompt_empty_when_nothing_attached():
    assert att.documents_prompt([]) == ""


# ===== Vision capability =====

def test_vision_detected_from_server_capabilities():
    class Client:
        def capabilities(self, model): return ["completion", "vision"]
    assert att.model_supports_vision("mystery-model", Client()) is True

    class NoVision:
        def capabilities(self, model): return ["completion"]
    assert att.model_supports_vision("llava-lookalike", NoVision()) is False


def test_vision_falls_back_to_known_families_when_server_is_silent():
    class Silent:
        def capabilities(self, model): return []
    assert att.model_supports_vision("llava:7b", Silent()) is True
    assert att.model_supports_vision("qwen2.5vl:7b", Silent()) is True
    assert att.model_supports_vision("llama3.2-vision:11b", Silent()) is True
    assert att.model_supports_vision("llama3.2:1b", Silent()) is False


# ===== API =====

def test_chat_rejects_images_for_a_blind_model(client, monkeypatch):
    """Silently dropping the image and answering anyway would be worse."""
    from carrot import app as app_mod
    monkeypatch.setattr(app_mod.ollama_mod.OllamaClient, "supports_vision",
                        lambda self, m: False, raising=False)
    resp = client.post("/api/chat", json={
        "message": "what is this?",
        "attachments": [{"name": "p.png", "mime": "image/png", "data": b64(PNG_1PX)}],
    })
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "cannot read images" in detail and "vision model" in detail


def test_chat_accepts_images_for_a_vision_model(client, monkeypatch):
    from carrot import app as app_mod
    monkeypatch.setattr(app_mod.ollama_mod.OllamaClient, "supports_vision",
                        lambda self, m: True, raising=False)
    seen = {}

    def fake_prepare(conv, message, skill, extra_system=None, mode=None, images=None):
        seen["images"] = images
        seen["extra_system"] = extra_system
        return [{"role": "user", "content": message}], None
    monkeypatch.setattr(app_mod, "_prepare_history", fake_prepare)

    resp = client.post("/api/chat", json={
        "message": "describe it",
        "attachments": [{"name": "p.png", "mime": "image/png", "data": b64(PNG_1PX)}],
    })
    assert resp.status_code == 200
    assert seen["images"] and len(seen["images"]) == 1


def test_chat_accepts_documents_on_any_model(client, monkeypatch):
    """Text extraction works without vision — that is the whole point."""
    from carrot import app as app_mod
    monkeypatch.setattr(app_mod.ollama_mod.OllamaClient, "supports_vision",
                        lambda self, m: False, raising=False)
    seen = {}

    def fake_prepare(conv, message, skill, extra_system=None, mode=None, images=None):
        seen["extra_system"] = extra_system
        seen["images"] = images
        return [{"role": "user", "content": message}], None
    monkeypatch.setattr(app_mod, "_prepare_history", fake_prepare)

    resp = client.post("/api/chat", json={
        "message": "summarise",
        "attachments": [{"name": "readme.md", "mime": "text/markdown",
                         "data": b64(b"the secret word is rutabaga")}],
    })
    assert resp.status_code == 200
    assert seen["images"] is None
    assert "rutabaga" in seen["extra_system"]


def test_chat_without_attachments_is_unchanged(client):
    resp = client.post("/api/chat", json={"message": "hello"})
    assert resp.status_code == 200


def test_broken_pypdf_reports_clearly_instead_of_crashing(monkeypatch):
    """A mismatched cryptography build raises a Rust panic, not ImportError.
    That must surface as a normal attachment error, not kill the chat turn."""
    import sys

    class Exploding:
        def __getattr__(self, item):
            raise BaseException("pyo3_runtime.PanicException: Python API call failed")

    monkeypatch.setitem(sys.modules, "pypdf", Exploding())
    with pytest.raises(att.AttachmentError) as exc:
        att._pdf_reader_class()
    assert "PDF support is unavailable" in str(exc.value)

    # And the whole chat turn degrades to a 400, not a 500.
    monkeypatch.setitem(sys.modules, "pypdf", Exploding())
    with pytest.raises(att.AttachmentError):
        att.process([{"name": "d.pdf", "mime": "application/pdf",
                      "data": b64(b"%PDF-1.7 body")}])
