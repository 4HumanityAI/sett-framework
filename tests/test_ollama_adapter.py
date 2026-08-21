"""
SETT Framework: Tests: OllamaAdapter
======================================================
Since Ollama itself is an external local service (not something we can
assume is installed/running in a test environment), these tests mock
the HTTP layer rather than requiring a live Ollama server. Real
end-to-end verification against an actual running Ollama instance
should be done manually: see templates/README.md-style guidance in
services_llm/ollama.py's docstring for setup steps.
"""
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from sett import PhrasingExpert
from sett.exceptions import (
    SETTCancelledError,
    SETTLLMAdapterError,
    SETTTimeoutError,
)
from sett.services_llm.ollama import OllamaAdapter


def _fake_response(payload: dict) -> MagicMock:
    """Build a mock object that behaves like the result of urlopen()."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(payload).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False
    return mock_resp


class TestOllamaAdapterBasics:

    def test_default_model_name(self):
        adapter = OllamaAdapter()
        assert adapter.model_name == "qwen3:1.7b"

    def test_custom_model_name(self):
        adapter = OllamaAdapter(model="phi4-mini")
        assert adapter.model_name == "phi4-mini"

    def test_repr_contains_model_and_url(self):
        adapter = OllamaAdapter(model="phi4-mini", base_url="http://localhost:11434")
        r = repr(adapter)
        assert "phi4-mini" in r
        assert "11434" in r

    def test_base_url_trailing_slash_is_stripped(self):
        adapter = OllamaAdapter(base_url="http://localhost:11434/")
        assert adapter._base_url == "http://localhost:11434"

    def test_temperature_clamped_to_valid_range(self):
        assert OllamaAdapter(temperature=5.0)._temperature == 1.0
        assert OllamaAdapter(temperature=-1.0)._temperature == 0.0


class TestOllamaAdapterComplete:

    @patch("urllib.request.urlopen")
    def test_complete_returns_response_text(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"response": "Hello, I am your assistant."})
        adapter = OllamaAdapter()
        result = adapter.complete("Say good morning")
        assert result == "Hello, I am your assistant."

    @patch("urllib.request.urlopen")
    def test_complete_sends_correct_endpoint_and_model(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"response": "ok"})
        adapter = OllamaAdapter(model="qwen3:1.7b")
        adapter.complete("hello")

        sent_request = mock_urlopen.call_args[0][0]
        assert sent_request.full_url == "http://localhost:11434/api/generate"

        sent_body = json.loads(sent_request.data.decode("utf-8"))
        assert sent_body["model"] == "qwen3:1.7b"
        assert sent_body["prompt"] == "hello"
        assert sent_body["stream"] is False

    @patch("urllib.request.urlopen")
    def test_complete_includes_system_prompt_when_given(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"response": "ok"})
        adapter = OllamaAdapter()
        adapter.complete("hello", system="You are a warm assistant.")

        sent_body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        assert sent_body["system"] == "You are a warm assistant."

    @patch("urllib.request.urlopen")
    def test_complete_omits_system_key_when_not_given(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"response": "ok"})
        adapter = OllamaAdapter()
        adapter.complete("hello")

        sent_body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        assert "system" not in sent_body

    @patch("urllib.request.urlopen")
    def test_complete_missing_response_key_returns_empty_string(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({})
        adapter = OllamaAdapter()
        assert adapter.complete("hello") == ""


class TestOllamaAdapterChat:

    @patch("urllib.request.urlopen")
    def test_chat_returns_message_content(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response(
            {"message": {"role": "assistant", "content": "Good morning, Dan."}}
        )
        adapter = OllamaAdapter()
        result = adapter.chat([{"role": "user", "content": "good morning"}])
        assert result == "Good morning, Dan."

    @patch("urllib.request.urlopen")
    def test_chat_sends_correct_endpoint(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"message": {"content": "ok"}})
        adapter = OllamaAdapter()
        adapter.chat([{"role": "user", "content": "hello"}])

        sent_request = mock_urlopen.call_args[0][0]
        assert sent_request.full_url == "http://localhost:11434/api/chat"

    @patch("urllib.request.urlopen")
    def test_chat_prepends_system_message(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"message": {"content": "ok"}})
        adapter = OllamaAdapter()
        adapter.chat(
            [{"role": "user", "content": "hello"}],
            system="Sos tu asistente.",
        )

        sent_body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        assert sent_body["messages"][0] == {"role": "system", "content": "Sos tu asistente."}
        assert sent_body["messages"][1] == {"role": "user", "content": "hello"}

    @patch("urllib.request.urlopen")
    def test_chat_without_system_does_not_prepend(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"message": {"content": "ok"}})
        adapter = OllamaAdapter()
        adapter.chat([{"role": "user", "content": "hello"}])

        sent_body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        assert len(sent_body["messages"]) == 1
        assert sent_body["messages"][0]["role"] == "user"


class TestOllamaAdapterErrorHandling:

    @patch("urllib.request.urlopen")
    def test_connection_error_raises_sett_llm_adapter_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        adapter = OllamaAdapter()
        with pytest.raises(SETTLLMAdapterError) as exc_info:
            adapter.complete("hello")
        assert "Ollama" in str(exc_info.value)

    @patch("urllib.request.urlopen")
    def test_malformed_json_raises_sett_llm_adapter_error(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not valid json{{{"
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = False
        mock_urlopen.return_value = mock_resp

        adapter = OllamaAdapter()
        with pytest.raises(SETTLLMAdapterError):
            adapter.complete("hello")

    def test_error_does_not_leak_raw_urllib_exception_type(self):
        """
        Callers should only ever need to catch SETTLLMAdapterError,
        consistent with the other adapters: not urllib-specific
        exceptions, which would leak an implementation detail.
        """
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("down")
            adapter = OllamaAdapter()
            try:
                adapter.complete("hello")
                assert False, "should have raised"
            except SETTLLMAdapterError:
                pass
            except Exception as e:  # noqa: BLE001
                pytest.fail(f"Leaked non-SETT exception type: {type(e)}")


class TestOllamaAdapterBaseUrlValidation:
    """
    AUD4-ROB-02: a wrong base_url used to surface as a bare ValueError
    from urllib on the first completion, walking straight through the
    SETTLLMAdapterError protection every caller relies on. It is a
    configuration mistake, so it is refused at construction.
    """

    @pytest.mark.parametrize(
        "base_url",
        [
            "not-a-url",           # urllib: "unknown url type"
            "http://[::1",         # urllib: "Invalid IPv6 URL"
            "http://localhost:abc",  # urllib: port not an integer
            "//localhost:11434",   # no scheme
            "ftp://localhost",     # scheme this adapter does not speak
            "http://",             # scheme but no host
            "",
            "   ",
        ],
    )
    def test_invalid_base_url_is_refused_at_construction(self, base_url):
        with pytest.raises(SETTLLMAdapterError) as exc_info:
            OllamaAdapter(base_url=base_url)
        assert "base_url" in str(exc_info.value)

    @pytest.mark.parametrize(
        "base_url, expected",
        [
            ("http://localhost:11434", "http://localhost:11434"),
            ("http://localhost:11434/", "http://localhost:11434"),
            ("https://ollama.internal", "https://ollama.internal"),
            ("http://[::1]:11434", "http://[::1]:11434"),
            ("http://192.168.1.20:11434/", "http://192.168.1.20:11434"),
            ("  http://localhost:11434  ", "http://localhost:11434"),
        ],
    )
    def test_valid_base_url_is_accepted(self, base_url, expected):
        assert OllamaAdapter(base_url=base_url)._base_url == expected

    def test_construction_failure_happens_before_any_request(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            with pytest.raises(SETTLLMAdapterError):
                OllamaAdapter(base_url="not-a-url")
            mock_urlopen.assert_not_called()


class TestOllamaAdapterTranslatesEveryFailure:
    """
    The named except clauses cover the frequent failures. This covers the
    contract: whatever else the request raises still arrives as
    SETTLLMAdapterError, because enumerating urllib's failure modes has
    already proven incomplete once.
    """

    def test_host_with_a_space_does_not_leak_http_client_invalid_url(self):
        """
        Not a URLError and not a ValueError: http.client.InvalidURL,
        raised before any socket is opened. Passes base_url validation
        (it has a scheme and a host) and would have escaped the three
        named clauses.
        """
        adapter = OllamaAdapter(base_url="http://local host:11434")
        with pytest.raises(SETTLLMAdapterError):
            adapter.complete("hello")

    @pytest.mark.parametrize(
        "raised",
        [
            ValueError("unknown url type"),
            OSError("socket exploded"),
            RuntimeError("something nobody enumerated"),
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        ],
    )
    def test_unenumerated_failures_become_adapter_errors(self, raised):
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = raised
            adapter = OllamaAdapter()
            with pytest.raises(SETTLLMAdapterError) as exc_info:
                adapter.complete("hello")
        assert exc_info.value.__cause__ is raised

    @pytest.mark.parametrize("method", ["complete", "chat"])
    def test_both_entry_points_translate(self, method):
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = RuntimeError("nope")
            adapter = OllamaAdapter()
            with pytest.raises(SETTLLMAdapterError):
                if method == "complete":
                    adapter.complete("hello")
                else:
                    adapter.chat([{"role": "user", "content": "hello"}])

    @pytest.mark.parametrize(
        "lifecycle_error",
        [SETTCancelledError("stopped"), SETTTimeoutError("out of time")],
    )
    def test_lifecycle_exceptions_keep_their_type(self, lifecycle_error):
        """
        The catch-all must not turn "somebody asked for this to stop" into
        "the provider failed": a caller distinguishing cancellation from
        failure would silently lose that distinction.
        """
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = lifecycle_error
            adapter = OllamaAdapter()
            with pytest.raises(type(lifecycle_error)):
                adapter.complete("hello")


class TestPhrasingExpertProtectionHolds:
    """
    The effect AUD4-ROB-02 actually reported: the fallback that keeps a
    component answering with no model reachable was being bypassed.
    """

    def test_fallback_text_is_used_when_the_adapter_fails_unusually(self):
        class Demo(PhrasingExpert):
            OUTPUT_KEY = "greeting"

            def determine_facts(self, context):
                return {"time_of_day": "morning"}

            def build_prompt(self, facts, context):
                return "Greet the user."

            def fallback_text(self, facts, context):
                return "Good morning."

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = ValueError("unknown url type")
            expert = Demo(name="demo", llm=OllamaAdapter())
            result = expert.resolve({})

        assert result["greeting"] == "Good morning."
