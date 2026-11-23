"""Credentials and speech stay at the configured provider origin."""
import pytest

from sett.exceptions import SETTServiceAdapterError
from sett.services_tts_stt.azure import AzureTTSAdapter
from sett.services_tts_stt.elevenlabs import ElevenLabsTTSAdapter
from sett.services_tts_stt.gladia import GladiaSTTAdapter

requests = pytest.importorskip("requests")


def install_transport(monkeypatch, responses):
    calls = []

    def send(_self, request, **_options):
        calls.append(request.url)
        status, body, headers = responses[len(calls) - 1]
        answer = requests.Response()
        answer.request = request
        answer.url = request.url
        answer.status_code = status
        answer.headers.update(headers)
        answer._content = body
        answer._content_consumed = True
        return answer

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return calls


@pytest.mark.parametrize("status", [302, 307, 308])
@pytest.mark.parametrize("provider", ["elevenlabs", "azure"])
def test_voice_redirect_stops_before_a_second_request(monkeypatch, status, provider):
    calls = install_transport(monkeypatch, [(status, b"", {"Location": "https://other.invalid/path"})])
    adapter = ElevenLabsTTSAdapter("test-key", model_id="test-model") if provider == "elevenlabs" else AzureTTSAdapter("test-key", "eastus")
    with pytest.raises(SETTServiceAdapterError):
        adapter.synthesize("test speech")
    assert len(calls) == 1


@pytest.mark.parametrize("foreign", [
    "https://other.invalid/result", "http://api.gladia.io/result",
    "https://api.gladia.io:8443/result", "https://user@api.gladia.io/result",
    "https://api.gladia.io@other.invalid/result", "//other.invalid/result",
    "https://api.gladia.io.invalid/result", "https://api.gladia.io:invalid/result",
])
def test_gladia_rejects_untrusted_poll_target_before_sending(monkeypatch, foreign):
    import json

    calls = install_transport(monkeypatch, [
        (200, b'{"audio_url":"https://api.gladia.io/audio"}', {}),
        (200, json.dumps({"result_url": foreign}).encode(), {}),
    ])
    with pytest.raises(SETTServiceAdapterError):
        GladiaSTTAdapter("test-key").transcribe(b"sample")
    assert len(calls) == 2
