import threading
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from claudegram.claude import ClaudeClient, ClaudeError
from claudegram.config import Config


def text_delta(text):
    return SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=text))


def final_message(text="hola", *, stop_reason="end_turn", stop_details=None, model="claude-opus-5"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking="…"), SimpleNamespace(type="text", text=text)],
        model=model,
        stop_reason=stop_reason,
        stop_details=stop_details,
        usage=SimpleNamespace(
            input_tokens=11, output_tokens=22, cache_read_input_tokens=33, cache_creation_input_tokens=44
        ),
    )


class FakeStream:
    def __init__(self, events, final):
        self._events = events
        self._final = final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self._events)

    def get_final_message(self):
        return self._final


class FakeMessages:
    def __init__(self, outcomes):
        self.calls: list[dict] = []
        self._outcomes = outcomes

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return FakeStream(*outcome)


class FakeAnthropic:
    def __init__(self, outcomes):
        self.messages = FakeMessages(list(outcomes))
        self.beta = SimpleNamespace(messages=FakeMessages(list(outcomes)))


@pytest.fixture
def config():
    def build(**overrides):
        env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ALLOWED_USER_IDS": "1"}
        env.update(overrides)
        return Config.from_env(env)

    return build


def test_opus_request_carries_thinking_effort_cache_and_fallbacks(config):
    fake = FakeAnthropic([([text_delta("ho"), text_delta("la")], final_message())])
    client = ClaudeClient(config(), client=fake)

    reply = client.ask(model="claude-opus-5", effort="high", brief=False, system="S",
                       messages=[{"role": "user", "content": "hola"}])

    sent = fake.beta.messages.calls[0]
    assert sent["model"] == "claude-opus-5"
    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["output_config"] == {"effort": "high"}
    assert sent["cache_control"] == {"type": "ephemeral"}
    assert sent["fallbacks"] == "default"
    assert sent["betas"] == ["server-side-fallback-2026-07-01"]
    assert reply.text == "hola"
    assert reply.usage == {"input": 11, "output": 22, "cache_read": 33, "cache_write": 44}
    assert not reply.truncated


def test_haiku_gets_no_thinking_and_no_effort(config):
    fake = FakeAnthropic([([], final_message(model="claude-haiku-4-5"))])
    client = ClaudeClient(config(), client=fake)

    client.ask(model="claude-haiku-4-5", effort="high", brief=False, system="S",
               messages=[{"role": "user", "content": "hola"}])

    sent = fake.messages.calls[0]  # no betas -> stable endpoint
    assert "thinking" not in sent
    assert "output_config" not in sent
    assert "fallbacks" not in sent
    assert fake.beta.messages.calls == []


def test_brief_uses_a_mid_conversation_system_message_on_opus(config):
    fake = FakeAnthropic([([], final_message())])
    client = ClaudeClient(config(), client=fake)

    client.ask(model="claude-opus-5", effort="medium", brief=True, system="S",
               messages=[{"role": "user", "content": "hola"}])

    sent = fake.beta.messages.calls[0]
    assert sent["messages"][-1]["role"] == "system"
    assert sent["system"] == "S"


def test_brief_falls_back_to_the_system_prompt_on_sonnet(config):
    fake = FakeAnthropic([([], final_message(model="claude-sonnet-5"))])
    client = ClaudeClient(config(), client=fake)

    client.ask(model="claude-sonnet-5", effort="medium", brief=True, system="S",
               messages=[{"role": "user", "content": "hola"}])

    sent = fake.messages.calls[0]
    assert all(m["role"] != "system" for m in sent["messages"])
    assert sent["system"].startswith("S\n\nModo breve")


def test_history_is_not_mutated_by_the_brief_flag(config):
    fake = FakeAnthropic([([], final_message())])
    client = ClaudeClient(config(), client=fake)
    history = [{"role": "user", "content": "hola"}]

    client.ask(model="claude-opus-5", effort="medium", brief=True, system="S", messages=history)

    assert history == [{"role": "user", "content": "hola"}]


def test_cancel_stops_the_stream_and_returns_the_partial_text(config):
    cancel = threading.Event()
    cancel.set()
    fake = FakeAnthropic([([text_delta("empiezo")], final_message())])
    client = ClaudeClient(config(), client=fake)

    reply = client.ask(model="claude-opus-5", effort="medium", brief=False, system="S",
                       messages=[{"role": "user", "content": "hola"}], cancel=cancel)

    assert reply.cancelled is True
    assert reply.text == ""  # cancelled before the first delta was consumed


def test_deltas_are_streamed_to_the_callback(config):
    seen = []
    fake = FakeAnthropic([([text_delta("ho"), text_delta("la")], final_message())])
    client = ClaudeClient(config(), client=fake)

    client.ask(model="claude-opus-5", effort="medium", brief=False, system="S",
               messages=[{"role": "user", "content": "x"}], on_delta=seen.append)

    assert seen == ["ho", "hola"]


def test_refusal_is_surfaced(config):
    details = SimpleNamespace(category="cyber", explanation="no puedo con eso")
    fake = FakeAnthropic([([], final_message("", stop_reason="refusal", stop_details=details))])
    client = ClaudeClient(config(), client=fake)

    reply = client.ask(model="claude-opus-5", effort="medium", brief=False, system="S",
                       messages=[{"role": "user", "content": "x"}])

    assert reply.refusal == "no puedo con eso"


def test_max_tokens_marks_the_reply_as_truncated(config):
    fake = FakeAnthropic([([], final_message(stop_reason="max_tokens"))])
    client = ClaudeClient(config(), client=fake)
    reply = client.ask(model="claude-opus-5", effort="medium", brief=False, system="S",
                       messages=[{"role": "user", "content": "x"}])
    assert reply.truncated is True


def _bad_request(message):
    response = httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    return anthropic.BadRequestError(message, response=response, body=None)


def test_unsupported_fallback_beta_is_dropped_and_retried(config):
    fake = FakeAnthropic([_bad_request("unsupported beta: server-side-fallback")])
    fake.beta.messages._outcomes = [_bad_request("unsupported beta: server-side-fallback")]
    fake.messages._outcomes = [([], final_message())]
    client = ClaudeClient(config(), client=fake)

    reply = client.ask(model="claude-opus-5", effort="medium", brief=False, system="S",
                       messages=[{"role": "user", "content": "x"}])

    assert reply.text == "hola"
    assert len(fake.beta.messages.calls) == 1  # first attempt, with the beta
    assert len(fake.messages.calls) == 1       # retry, without it


def test_other_bad_requests_are_reported_verbatim(config):
    fake = FakeAnthropic([_bad_request("messages: too long")])
    client = ClaudeClient(config(), client=fake)

    with pytest.raises(ClaudeError) as excinfo:
        client.ask(model="claude-opus-5", effort="medium", brief=False, system="S",
                   messages=[{"role": "user", "content": "x"}])
    assert "too long" in str(excinfo.value)


def test_connection_errors_get_a_friendly_message(config):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    fake = FakeAnthropic([anthropic.APIConnectionError(request=request)])
    client = ClaudeClient(config(), client=fake)

    with pytest.raises(ClaudeError) as excinfo:
        client.ask(model="claude-opus-5", effort="medium", brief=False, system="S",
                   messages=[{"role": "user", "content": "x"}])
    assert "conectar" in str(excinfo.value)


def test_refusal_fallbacks_can_be_disabled_by_config(config):
    fake = FakeAnthropic([([], final_message())])
    client = ClaudeClient(config(CLAUDEGRAM_REFUSAL_FALLBACKS="false"), client=fake)
    client.ask(model="claude-opus-5", effort="medium", brief=False, system="S",
               messages=[{"role": "user", "content": "x"}])
    assert "fallbacks" not in fake.messages.calls[0]


def test_missing_api_key_is_explained_not_traced(config):
    class NoCredentials:
        def __init__(self):
            self.messages = self
            self.beta = SimpleNamespace(messages=self)
            self.models = self

        def stream(self, **kwargs):
            raise TypeError("Could not resolve authentication method. Expected one of api_key…")

        def retrieve(self, model):
            raise TypeError("Could not resolve authentication method. Expected one of api_key…")

    client = ClaudeClient(config(), client=NoCredentials())

    with pytest.raises(ClaudeError) as excinfo:
        client.ask(model="claude-opus-5", effort="medium", brief=False, system="S",
                   messages=[{"role": "user", "content": "x"}])
    assert "ANTHROPIC_API_KEY" in str(excinfo.value)

    with pytest.raises(ClaudeError):
        client.check("claude-opus-5")


def test_unrelated_type_errors_still_propagate(config):
    class Broken:
        def __init__(self):
            self.messages = self
            self.beta = SimpleNamespace(messages=self)

        def stream(self, **kwargs):
            raise TypeError("stream() got an unexpected keyword argument 'fallbacks'")

    client = ClaudeClient(config(), client=Broken())
    with pytest.raises(TypeError):
        client.ask(model="claude-opus-5", effort="medium", brief=False, system="S",
                   messages=[{"role": "user", "content": "x"}])
