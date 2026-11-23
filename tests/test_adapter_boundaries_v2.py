"""HTTP redirection, response budgets and configured audio containers."""
import io
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from sett.exceptions import SETTLLMAdapterError, SETTServiceAdapterError
from sett.services_llm import ollama
from sett.services_tts_stt import azure


@contextmanager
def local_server(response):
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            hits.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            status, headers, body = response()
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_ollama_redirect_does_not_reach_another_origin(status):
    with (
        local_server(lambda: (200, {}, b'{"response":"unexpected"}')) as (other, hits),
        local_server(lambda: (status, {"Location": other + "/other"}, b"")) as (url, first),
        pytest.raises(SETTLLMAdapterError),
    ):
        ollama.OllamaAdapter(base_url=url, model="test-model").complete("synthetic prompt")
    assert first == ["/api/generate"]
    assert hits == []


def test_ollama_response_budget_exact_boundary(monkeypatch):
    body = b'{"response":"ok"}'
    sizes = []

    class Response(io.BytesIO):
        def read(self, size=-1):
            sizes.append(size)
            return super().read(size)

    monkeypatch.setattr(ollama, "_open_request", lambda *a, **k: Response(body))
    assert ollama.OllamaAdapter(max_response_bytes=len(body), model="test-model").complete("x") == "ok"
    with pytest.raises(SETTLLMAdapterError) as caught:
        ollama.OllamaAdapter(max_response_bytes=len(body) - 1, model="test-model").complete("x")
    assert caught.value.reason_code == "adapter.invalid_response"
    assert sizes == [len(body) + 1, len(body)]


@pytest.mark.parametrize("size", [0, -1, True, 1.5, "1024"])
def test_ollama_rejects_invalid_response_budget(size):
    with pytest.raises(SETTLLMAdapterError):
        ollama.OllamaAdapter(max_response_bytes=size, model="test-model")


@pytest.mark.parametrize("format_name,container,mime", [
    ("audio-24khz-48kbitrate-mono-mp3", "mp3", "audio/mpeg"),
    ("riff-24khz-16bit-mono-pcm", "wav", "audio/wav"),
    ("ogg-24khz-16bit-mono-opus", "ogg", "audio/ogg"),
    ("webm-24khz-16bit-mono-opus", "webm", "audio/webm"),
    ("raw-24khz-16bit-mono-pcm", "pcm", "audio/pcm"),
])
def test_azure_configured_container_matches_request(monkeypatch, format_name, container, mime):
    calls = []

    def request(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(content=b"synthetic audio")

    monkeypatch.setattr(azure, "checked_request", request)
    adapter = azure.AzureTTSAdapter("test-key", "test-region", output_format=format_name)
    assert adapter.audio_format == container
    assert adapter.capabilities.output_formats == (mime,)
    assert adapter.synthesize("hello") == b"synthetic audio"
    assert calls[0]["headers"]["X-Microsoft-OutputFormat"] == format_name


def test_azure_rejects_container_change_without_network_or_state_mutation(monkeypatch):
    calls = []
    monkeypatch.setattr(azure, "checked_request", lambda *a, **k: calls.append(k))
    adapter = azure.AzureTTSAdapter("test-key", "test-region")
    with pytest.raises(SETTServiceAdapterError) as caught:
        adapter.synthesize("hello", output_format="ogg-24khz-16bit-mono-opus")
    assert caught.value.reason_code == "adapter.output_format_invalid"
    assert adapter.audio_format == "mp3"
    assert calls == []
