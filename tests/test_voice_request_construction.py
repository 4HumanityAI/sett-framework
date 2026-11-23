"""Identifiers and endpoints cannot redirect a voice request.

A capturing stand-in replaces the HTTP call, so every case reports the URL
and headers the adapter would have sent, and whether it sent anything.
All credentials are synthetic.
"""
from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from sett.core_ruler.adapter import AdapterErrorKind
from sett.exceptions import SETTServiceAdapterError
from sett.services_tts_stt import _http, elevenlabs
from sett.services_tts_stt._http import host_label, path_identifier, validated_endpoint
from sett.services_tts_stt.assemblyai import AssemblyAISTTAdapter
from sett.services_tts_stt.azure import AzureSTTAdapter, AzureTTSAdapter
from sett.services_tts_stt.cartesia import CartesiaTTSAdapter
from sett.services_tts_stt.deepgram import DeepgramSTTAdapter
from sett.services_tts_stt.elevenlabs import ElevenLabsTTSAdapter
from sett.services_tts_stt.gladia import GladiaSTTAdapter
from sett.services_tts_stt.openai_audio import WhisperSTTAdapter

KEY = "SYNTHETIC-KEY-0000"


class _Response:
    status_code = 200
    content = b"ID3synthetic"
    text = ""

    def __init__(self) -> None:
        self.headers: dict = {}

    def json(self):
        return {"DisplayText": "ok"}


@pytest.fixture
def sent(monkeypatch):
    """Every request an adapter makes, as (method, url, headers, params)."""
    requests_made: list = []

    def request(method, url, **kwargs):
        requests_made.append((method, url, dict(kwargs.get("headers") or {}), kwargs.get("params")))
        return _Response()

    monkeypatch.setattr(_http.requests, "request", request)
    monkeypatch.setattr(
        elevenlabs.requests, "post", lambda url, **kwargs: request("POST", url, **kwargs)
    )
    return requests_made


def _refused(caught, reason_code):
    assert caught.value.kind is AdapterErrorKind.CONFIGURATION
    assert caught.value.reason_code == reason_code
    assert caught.value.retryable is False
    assert KEY not in str(caught.value)


# ── ElevenLabs voice identifier ─────────────────────────────────────────

VALID_VOICE_IDS = ["21m00Tcm4TlvDq8ikWAM", "voice_1", "voice-1", "a", "Voice.v2", "x~y"]
INVALID_VOICE_IDS = [
    "",
    ".",
    "..",
    "../../v1/user",
    "a/b",
    "a\\b",
    "v?output_format=pcm_44100",
    "v&x=1",
    "v#fragment",
    "v%2F..%2Fuser",
    "v id",
    "v\nid",
    "v\tid",
    "v\x00id",
    "v\x7fid",
    "v:id",
    "v@host",
    "v;param",
    "v" + chr(0xE9),
    None,
    7,
    b"voice",
]


@pytest.mark.parametrize("voice_id", VALID_VOICE_IDS)
def test_elevenlabs_sends_a_valid_voice_id_as_one_path_segment(sent, voice_id):
    adapter = ElevenLabsTTSAdapter(KEY, model_id="synthetic-model", output_format="mp3_44100_128")

    adapter.synthesize("hello", voice_id=voice_id)
    ElevenLabsTTSAdapter(KEY, voice_id=voice_id, model_id="synthetic-model").synthesize("hello")

    assert len(sent) == 2
    for _method, url, headers, _params in sent:
        parts = urlsplit(url)
        assert parts.hostname == "api.elevenlabs.io"
        assert parts.path == f"/v1/text-to-speech/{voice_id}"
        assert parts.query == "" and parts.fragment == ""
        assert headers["xi-api-key"] == KEY
    assert sent[0][3] == {"output_format": "mp3_44100_128"}


@pytest.mark.parametrize("voice_id", INVALID_VOICE_IDS)
def test_elevenlabs_refuses_a_voice_id_that_is_not_one_path_segment(sent, voice_id):
    adapter = ElevenLabsTTSAdapter(KEY, model_id="synthetic-model", output_format="mp3_44100_128")

    with pytest.raises(SETTServiceAdapterError) as per_call:
        adapter.synthesize("hello", voice_id=voice_id)
    with pytest.raises(SETTServiceAdapterError) as at_construction:
        ElevenLabsTTSAdapter(KEY, voice_id=voice_id, model_id="synthetic-model")

    _refused(per_call, "adapter.configuration_invalid")
    _refused(at_construction, "adapter.configuration_invalid")
    assert sent == []
    assert adapter.capabilities.output_formats == ("audio/mpeg",)


# ── Azure region ────────────────────────────────────────────────────────

VALID_REGIONS = ["westeurope", "eastus2", "usgovvirginia", "north-europe", "a"]
INVALID_REGIONS = [
    "collector.example/x#",
    "collector.example/x?",
    "collector.example",
    "westeurope.evil",
    "user@collector",
    "west europe",
    "west\neurope",
    "-westeurope",
    "westeurope-",
    "west_europe",
    "westeurope:8443",
    "a" * 64,
]


@pytest.mark.parametrize("region", VALID_REGIONS)
def test_azure_region_selects_a_host_inside_the_provider_domain(sent, region):
    AzureTTSAdapter(KEY, region).synthesize("hello")
    AzureSTTAdapter(KEY, region).transcribe(b"RIFF")

    hosts = [urlsplit(url).hostname for _m, url, _h, _p in sent]
    assert hosts == [
        f"{region.lower()}.tts.speech.microsoft.com",
        f"{region.lower()}.stt.speech.microsoft.com",
    ]
    assert all(headers["Ocp-Apim-Subscription-Key"] == KEY for _m, _u, headers, _p in sent)


@pytest.mark.parametrize("region", INVALID_REGIONS)
@pytest.mark.parametrize("adapter_type", [AzureTTSAdapter, AzureSTTAdapter])
def test_azure_refuses_a_region_that_is_not_one_host_label(sent, adapter_type, region):
    with pytest.raises(SETTServiceAdapterError) as caught:
        adapter_type(KEY, region)

    _refused(caught, "adapter.configuration_invalid")
    assert sent == []


def test_azure_region_from_the_environment_is_checked_the_same_way(sent, monkeypatch):
    monkeypatch.setenv("AZURE_SPEECH_REGION", "collector.example/x#")

    with pytest.raises(SETTServiceAdapterError) as caught:
        AzureTTSAdapter(KEY)

    _refused(caught, "adapter.configuration_invalid")
    assert sent == []


def test_azure_refuses_an_output_format_with_control_characters(sent):
    adapter = AzureTTSAdapter(KEY, "westeurope")

    with pytest.raises(SETTServiceAdapterError) as caught:
        adapter.synthesize("hello", output_format="audio-x\r\nX-Injected: 1-mp3")
    with pytest.raises(SETTServiceAdapterError):
        AzureTTSAdapter(KEY, "westeurope", output_format="audio-x\r\nX-Injected: 1-mp3")

    assert caught.value.kind is AdapterErrorKind.CONFIGURATION
    assert caught.value.reason_code == "adapter.output_format_invalid"
    assert sent == []


# ── Deliberately configured endpoints ───────────────────────────────────

VALID_ENDPOINTS = [
    "https://proxy.example/v1",
    "https://proxy.example:8443/v1",
    "http://127.0.0.1:8080/v1",
    "http://localhost/v1",
    "https://[::1]:8443/v1",
    "https://proxy.example/v1?tenant=a",
]
INVALID_ENDPOINTS = [
    "",
    "proxy.example/v1",
    "//proxy.example/v1",
    "ftp://proxy.example/v1",
    "file:///etc/hosts",
    "https://",
    "https:///v1",
    "https://user@proxy.example/v1",
    "https://user:secret@proxy.example/v1",
    "https://proxy.example/v1#fragment",
    "https://proxy.example\\@other.example/v1",
    "https://proxy.example/v 1",
    "https://proxy.example/v1\r\nX-Injected: 1",
    "https://proxy.example:notaport/v1",
    "https://proxy.example:99999/v1",
    None,
    7,
]


@pytest.mark.parametrize("endpoint", VALID_ENDPOINTS)
def test_a_configured_endpoint_is_used_as_given(endpoint):
    assert validated_endpoint(endpoint, provider="p", adapter_name="A") == endpoint


@pytest.mark.parametrize("endpoint", INVALID_ENDPOINTS)
def test_a_malformed_endpoint_is_refused(endpoint):
    with pytest.raises(SETTServiceAdapterError) as caught:
        validated_endpoint(endpoint, provider="p", adapter_name="A")

    _refused(caught, "adapter.invalid_base_url")


ENDPOINT_BUILDERS = [
    lambda url: AzureTTSAdapter(KEY, "westeurope", endpoint=url),
    lambda url: AzureSTTAdapter(KEY, "westeurope", endpoint=url),
    lambda url: CartesiaTTSAdapter("voice", KEY, model="synthetic-model", endpoint=url),
    lambda url: DeepgramSTTAdapter(KEY, model="synthetic-model", endpoint=url),
    lambda url: WhisperSTTAdapter(KEY, model="synthetic-model", endpoint=url),
    lambda url: GladiaSTTAdapter(KEY, base_url=url),
    lambda url: AssemblyAISTTAdapter(KEY, base_url=url),
]


@pytest.mark.parametrize("build", ENDPOINT_BUILDERS)
@pytest.mark.parametrize(
    "url",
    ["https://user:secret@proxy.example/v1", "ftp://proxy.example/v1", "proxy.example/v1",
     "https://proxy.example/v1#fragment", "https://proxy.example/v1\r\nX: 1"],
)
def test_every_adapter_refuses_a_malformed_endpoint_at_construction(sent, build, url):
    with pytest.raises(SETTServiceAdapterError) as caught:
        build(url)

    _refused(caught, "adapter.invalid_base_url")
    assert sent == []


@pytest.mark.parametrize("build", ENDPOINT_BUILDERS)
def test_every_adapter_accepts_a_local_test_endpoint(build):
    build("http://127.0.0.1:8080/v1")


def test_an_azure_endpoint_overrides_the_region_host_and_is_sent_once(sent):
    AzureTTSAdapter(KEY, "westeurope", endpoint="https://proxy.example/v1").synthesize("hello")

    assert len(sent) == 1
    method, url, headers, _params = sent[0]
    assert (method, url) == ("POST", "https://proxy.example/v1")
    assert headers["Ocp-Apim-Subscription-Key"] == KEY


@pytest.mark.parametrize("value", ["westeurope", "a1", "a-b"])
def test_host_label_accepts_single_labels(value):
    assert host_label(value, provider="p", adapter_name="A", parameter="region") == value


@pytest.mark.parametrize("value", ["abc", "A.b_c~d-e", "0"])
def test_path_identifier_accepts_unreserved_characters(value):
    assert path_identifier(value, provider="p", adapter_name="A", parameter="id") == value
