"""Voice-model failure falls back to the fast model instead of dropping the reply."""

from tertulia.delegate.adapters.base import AdapterError, Completion
from tertulia.delegate.config import AdapterConfig, BehaviourConfig, DelegateConfig
from tertulia.delegate.daemon import DelegateDaemon


class VoiceDownAdapter:
    """Fails on the default (voice) model, answers on any explicit model."""

    def complete(self, *, system_prompt, prompt, timeout=None, model=None):
        if model is None:
            raise AdapterError("voice model down")
        return Completion(text=f"plan B via {model}", cost_usd=None, raw={})


def _cfg(tmp_path, fast_model):
    return DelegateConfig(
        concierge_url="http://127.0.0.1:1", agent_name="A", owner_name="O", personality="",
        profile_path=tmp_path / "profile.md", memory_dir=tmp_path / "memory",
        state_dir=tmp_path / "state", sandbox_dir=tmp_path / "sandbox",
        token_file=tmp_path / "token", owner_telegram_user_id=None,
        adapter=AdapterConfig(kind="scripted", fast_model=fast_model),
        behaviour=BehaviourConfig(), base_dir=tmp_path,
    )


def test_falls_back_to_fast_model(tmp_path):
    daemon = DelegateDaemon(_cfg(tmp_path, "haiku"), client=None, adapter=VoiceDownAdapter())
    assert daemon._complete_or_fallback("hola") == "plan B via haiku"


def test_no_fast_model_means_no_reply(tmp_path):
    daemon = DelegateDaemon(_cfg(tmp_path, None), client=None, adapter=VoiceDownAdapter())
    assert daemon._complete_or_fallback("hola") is None
