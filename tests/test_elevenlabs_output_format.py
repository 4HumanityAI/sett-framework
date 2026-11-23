"""ElevenLabsTTSAdapter output_format: an explicit MP3 provider format.

The provider is mocked at `requests.post`, as in test_elevenlabs_adapter.py;
no API key or network access is needed.
"""
import pytest

pytest.importorskip(
    "requests",
    reason="ElevenLabsTTSAdapter needs the 'requests' package (extra: [elevenlabs])",
)

from unittest.mock import MagicMock, patch

from sett.core_ruler.adapter import AdapterErrorKind
from sett.exceptions import SETTServiceAdapterError
from sett.services_tts_stt.elevenlabs import ElevenLabsTTSAdapter


def _ok():
    response = MagicMock()
    response.status_code = 200
    response.content = b"fake-mp3-bytes"
    response.text = ""
    return response


@patch("sett.services_tts_stt.elevenlabs.requests.post")
def test_default_sends_no_output_format_and_keeps_the_request(mock_post):
    mock_post.return_value = _ok()
    ElevenLabsTTSAdapter(api_key="k", model_id="test-model").synthesize("hello")
    assert "params" not in mock_post.call_args.kwargs
    assert set(mock_post.call_args.kwargs) == {"headers", "json", "timeout", "allow_redirects"}
    assert mock_post.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize("value", ["mp3_44100_128", "mp3_22050_32", "mp3_44100_192"])
@patch("sett.services_tts_stt.elevenlabs.requests.post")
def test_configured_mp3_format_is_sent_as_query_parameter(mock_post, value):
    mock_post.return_value = _ok()
    adapter = ElevenLabsTTSAdapter(api_key="k", output_format=value, model_id="test-model")
    assert adapter.synthesize("hello") == b"fake-mp3-bytes"
    assert mock_post.call_args.kwargs["params"] == {"output_format": value}
    assert mock_post.call_args.kwargs["headers"]["accept"] == "audio/mpeg"


def test_declared_container_and_capability_stay_mp3():
    adapter = ElevenLabsTTSAdapter(api_key="k", output_format="mp3_44100_128", model_id="test-model")
    assert adapter.audio_format == "mp3"
    assert adapter.capabilities.output_formats == ("audio/mpeg",)


@pytest.mark.parametrize("value", [
    "pcm_44100", "wav_44100", "ulaw_8000", "opus_48000_128", "mp3", "mp3_44100", "MP3_44100_128",
    " mp3_44100_128", "mp3_44100_128 ", "", 128, b"mp3_44100_128",
])
def test_non_mp3_or_malformed_formats_are_rejected_at_construction(value):
    with pytest.raises(SETTServiceAdapterError) as caught:
        ElevenLabsTTSAdapter(api_key="k", output_format=value, model_id="test-model")
    assert caught.value.reason_code == "adapter.output_format_invalid"
    assert caught.value.kind is AdapterErrorKind.CONFIGURATION


@patch("sett.services_tts_stt.elevenlabs.requests.post")
def test_a_per_call_keyword_does_not_change_the_configured_format(mock_post):
    mock_post.return_value = _ok()
    adapter = ElevenLabsTTSAdapter(api_key="k", output_format="mp3_44100_128", model_id="test-model")
    adapter.synthesize("hello", output_format="pcm_44100")
    assert mock_post.call_args.kwargs["params"] == {"output_format": "mp3_44100_128"}
