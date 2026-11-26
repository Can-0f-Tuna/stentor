import io
import numpy as np
import pytest

from stentor import audio, config
from stentor.providers import TranscribeError, check_api, transcribe_api


def test_config_roundtrip(tmp_path):
    cfg = config.Config(provider="openai", api_key='k"ey', prompt="שלום\nworld", min_hold=0.5, beeps=False)
    path = config.save(cfg, tmp_path / "c.toml")
    assert config.load(path) == cfg


def test_config_defaults_and_unknown_keys(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('provider = "local"\nbogus = 1\n')
    cfg = config.load(p)
    assert cfg.provider == "local" and cfg.hotkey == config.DEFAULT_HOTKEY


def test_env_key_fallback(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    assert config.Config(provider="openai").effective_key() == "from-env"
    assert config.Config(provider="openai", api_key="file").effective_key() == "file"


def test_a_new_install_starts_on_this_computer(tmp_path):
    assert config.Config().provider == "local"
    assert config.load(tmp_path / "none.toml").provider == "local"
    assert "groq" not in config.PROVIDERS


def tone(seconds, freq=220, amp=0.3):
    t = np.arange(int(audio.SAMPLE_RATE * seconds)) / audio.SAMPLE_RATE
    return (np.sin(2 * np.pi * freq * t) * amp).astype(np.float32)


def test_vad_drops_silence_and_keeps_speech():
    quiet = (np.random.default_rng(0).standard_normal(audio.SAMPLE_RATE * 2) * 0.0005).astype(np.float32)
    assert audio.speech_only(quiet) is None
    clip = np.concatenate([quiet, tone(1.0), quiet])
    speech = audio.speech_only(clip)
    assert speech is not None and 0.9 * audio.SAMPLE_RATE < len(speech) < 2.0 * audio.SAMPLE_RATE


def test_vad_keeps_speech_with_no_pause():
    rng = np.random.default_rng(0)
    # Words at a steady level over a loud room: the quietest frames are not far below the speech.
    t = np.arange(audio.SAMPLE_RATE * 8) / audio.SAMPLE_RATE
    clip = (rng.standard_normal(len(t)) * 0.03 * (1 + 0.35 * np.sin(2 * np.pi * 3 * t))).astype(np.float32)
    speech = audio.speech_only(clip)
    assert speech is not None and len(speech) > 0.9 * len(clip)


def test_vad_keeps_all_of_a_clip_with_few_pauses():
    rng = np.random.default_rng(0)
    gap = (rng.standard_normal(audio.SAMPLE_RATE // 3) * 0.01).astype(np.float32)
    clip = np.concatenate([tone(2.0, amp=0.1), gap, tone(2.0, amp=0.1), tone(2.0, amp=0.04), gap, tone(1.0, amp=0.1)])
    speech = audio.speech_only(clip)
    assert speech is not None and len(speech) > 0.9 * len(clip)


def test_vad_hears_a_quiet_microphone():
    quiet = (np.random.default_rng(0).standard_normal(audio.SAMPLE_RATE) * 0.0002).astype(np.float32)
    clip = np.concatenate([quiet, tone(1.0, amp=0.004), quiet])
    assert audio.speech_only(clip) is not None


def test_vad_hears_the_same_words_whatever_the_microphone_level():
    rng = np.random.default_rng(0)
    room = lambda seconds: (rng.standard_normal(int(audio.SAMPLE_RATE * seconds)) * 0.001).astype(np.float32)
    clip = np.concatenate([room(1.0), tone(0.8, amp=0.03), room(0.7), tone(1.2, amp=0.02), room(1.0)])
    normal = audio.speech_only(clip)
    for lower in (20, 30):  # a microphone set that many dB too low
        heard = audio.speech_only(clip * 10 ** (-lower / 20))
        assert heard is not None and len(heard) == len(normal), lower


def test_vad_keeps_a_quiet_room_silent_at_any_microphone_level():
    rng = np.random.default_rng(0)
    room = (rng.standard_normal(audio.SAMPLE_RATE * 3) * 0.001).astype(np.float32)
    for lower in (0, 20, 30, 40):
        assert audio.speech_only(room * 10 ** (-lower / 20)) is None, lower


def test_vad_judges_uneven_noise_the_same_at_any_microphone_level_and_never_hears_digital_silence():
    rng = np.random.default_rng(1)
    hiss = rng.standard_normal(audio.SAMPLE_RATE * 3).astype(np.float32)
    bumps = 1 + 5 * (np.sin(2 * np.pi * 0.7 * np.arange(len(hiss)) / audio.SAMPLE_RATE) > 0.6)  # a fan, a keyboard
    room = hiss * bumps * 0.002  # uneven, so the bar and not the steady-clip rule decides
    frame = audio.SAMPLE_RATE * 30 // 1000
    normal = audio._voiced(room, frame)
    assert 0 < normal.sum() < len(normal)
    for lower in (15, 30):
        assert (audio._voiced(room * 10 ** (-lower / 20), frame) == normal).all(), lower
    assert audio.speech_only(room * 10 ** (-70 / 20)) is None  # its loudest frames are under -80 dBFS


def test_wav_roundtrip(tmp_path):
    p = tmp_path / "a.wav"
    p.write_bytes(audio.wav_bytes(tone(0.5)))
    back = audio.read_audio_file(str(p))
    assert len(back) == int(audio.SAMPLE_RATE * 0.5)


def make_cfg(api, **kw):
    return config.Config(provider="custom", base_url=api.url, api_key="secret", model="m1", **kw)


def test_api_request_shape(api):
    api.text = "  שלום world "
    out = transcribe_api(make_cfg(api, languages="fr", prompt="hint"), tone(0.5))
    assert out == "שלום world"
    method, path, auth, body = api.requests[-1]
    assert path == "/v1/audio/transcriptions" and auth == "Bearer secret"
    for needle in (b'name="model"\r\n\r\nm1', b'name="language"\r\n\r\nfr', b'name="prompt"\r\n\r\nhint', b"RIFF"):
        assert needle in body


@pytest.mark.parametrize("status,word", [(401, "key"), (429, "rate limit")])
def test_api_errors_are_explained(api, status, word):
    api.status = status
    with pytest.raises(TranscribeError, match=word):
        transcribe_api(make_cfg(api), tone(0.5))


def test_missing_key_and_unreachable():
    with pytest.raises(TranscribeError, match="API key"):
        transcribe_api(config.Config(provider="openai"), tone(0.5))
    dead = config.Config(provider="custom", base_url="http://127.0.0.1:9/v1", api_key="k", timeout=2)
    with pytest.raises(TranscribeError, match="could not reach"):
        transcribe_api(dead, tone(0.5))


def test_check_api(api):
    assert check_api(make_cfg(api)) == ""
    api.status = 401
    assert "key" in check_api(make_cfg(api))


def test_language_hints():
    from stentor.config import HEBREW_PROMPT
    from stentor.providers import language_hints
    assert language_hints(config.Config(languages="fr")) == ("fr", "")
    assert language_hints(config.Config(languages="he")) == ("he", HEBREW_PROMPT)  # unforced, short Hebrew came out as Cyrillic
    assert language_hints(config.Config(languages="he", provider="local")) == ("he", HEBREW_PROMPT)
    assert language_hints(config.Config(languages="he,en")) == ("", HEBREW_PROMPT)
    force, prompt = language_hints(config.Config(languages="he,en,ru", prompt="Kubernetes"))
    assert force == "" and prompt.startswith(HEBREW_PROMPT) and "Привет" in prompt and prompt.endswith("Kubernetes")
    assert language_hints(config.Config()) == ("", "")


def test_language_outside_the_list_is_transcribed_again(api, monkeypatch):
    from stentor import providers
    answers = iter([("مرحبا", "arabic", -0.3), ("שלום", "", None)])
    sent = []
    monkeypatch.setattr(providers, "_request", lambda cfg, audio, lang, prompt, fmt: sent.append((lang, fmt)) or next(answers))
    cfg = config.Config(provider="openai", model="whisper-1", api_key="k", languages="he,en")
    assert transcribe_api(cfg, tone(0.5)) == "שלום"
    assert sent == [("", "verbose_json"), ("he", "verbose_json")]


def test_the_language_check_works_for_any_whisper_service():
    """An extension that transcribes somewhere else hands `checked` its own way of asking."""
    from stentor.providers import checked
    cfg = config.Config(languages="he,en")
    asked = []
    replies = {"": ("Бдика", "russian", -0.6), "he": ("בדיקה", "hebrew", -0.2)}
    assert checked(cfg, lambda language: asked.append(language) or replies[language]) == "בדיקה"
    assert asked == ["", "he"]
    assert checked(cfg, lambda language: ("Run it.", "english", -0.1)) == "Run it."


def test_a_language_on_the_list_is_left_alone(monkeypatch):
    from stentor import providers
    sent = []
    monkeypatch.setattr(providers, "_request", lambda cfg, audio, lang, prompt, fmt: sent.append(lang) or ("Run it.", "english", -0.8))
    assert transcribe_api(config.Config(provider="openai", model="whisper-1", api_key="k", languages="he,en"), tone(0.5)) == "Run it."
    assert sent == [""]


def test_settings_an_extension_owns_survive_a_save(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('''provider = "openai"
sounds = true
language = "fr"
some_flag = true
some_text = "a \\"quoted\\" word"
some_table = { "get hub" = "GitHub" }
some_list = [1, "two", { three = 3.5 }]
some_date = 2026-10-31T10:00:00
[some_section]
nested = "yes"
''', encoding="utf-8")
    cfg = config.load(p)
    config.save(cfg, p)
    again = config.load(p)
    assert again == cfg and again.provider == "openai" and again.languages == "fr"
    assert set(again.extra) == {"some_flag", "some_text", "some_table", "some_list", "some_date", "some_section"}
    assert again.extra["some_list"] == [1, "two", {"three": 3.5}] and again.extra["some_section"] == {"nested": "yes"}
    assert "sounds" not in p.read_text(encoding="utf-8")  # read once, then gone


def test_a_setting_in_extra_falls_back_to_its_default():
    cfg = config.Config(extra={"table": "not a table", "flag": True})
    assert cfg.setting("table", {}) == {} and cfg.setting("flag", False) is True and cfg.setting("missing", "x") == "x"


def cpuinfo(tmp_path, cores, threads):
    blocks = [f"processor\t: {i}\nphysical id\t: 0\ncore id\t\t: {i // threads}\n" for i in range(cores * threads)]
    path = tmp_path / "cpuinfo"
    path.write_text("\n".join(blocks))
    return str(path)


@pytest.mark.parametrize("cores, threads, logical, expected", [
    (4, 2, 8, 4),  # hyperthreading: one thread per core
    (8, 1, 8, 8),  # none (Apple silicon, Core Ultra 200): every core, not half of them
    (20, 1, 8, 8),  # limited to fewer processors than the machine has
])
def test_the_local_model_gets_a_thread_per_core(tmp_path, monkeypatch, cores, threads, logical, expected):
    import os
    import sys
    from stentor.providers import physical_cores
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "cpu_count", lambda: logical)
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: set(range(logical)), raising=False)
    assert physical_cores(cpuinfo(tmp_path, cores, threads)) == expected
    assert physical_cores(str(tmp_path / "missing")) == logical // 2
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: {0, 1, 2})  # `taskset`, or a container given three
    assert physical_cores(cpuinfo(tmp_path, cores, threads)) == 3


class FakeWhisper:
    made = []

    def __init__(self, name, **kw):
        FakeWhisper.made.append(name)

    def transcribe(self, sound, **kw):
        info = type("Info", (), {"language": kw.get("language") or "en"})
        return iter([type("Segment", (), {"text": " hi "})]), info


def test_the_model_loads_in_the_background_once(monkeypatch):
    import sys
    import threading
    import time
    import types
    from stentor.providers import LocalTranscriber
    FakeWhisper.made = []
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeWhisper))
    local = LocalTranscriber(config.Config(provider="local"))
    local.preload()
    out = []
    threading.Thread(target=lambda: out.append(local.transcribe(tone(1)))).start()
    deadline = time.monotonic() + 2
    while not out and time.monotonic() < deadline:
        time.sleep(0.01)
    assert out == ["hi"] and FakeWhisper.made == ["small"]


def test_a_model_that_must_be_downloaded_first_says_so(monkeypatch):
    import sys
    import types
    from stentor import providers
    from stentor.providers import LocalTranscriber
    FakeWhisper.made = []
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeWhisper))
    cached = {"small": False}
    monkeypatch.setattr(providers, "model_cached", lambda name: cached[name])
    local, told = LocalTranscriber(config.Config(provider="local")), []
    local.on_download = lambda model, started: told.append((model, started))
    local.load()
    assert told == [("small", True), ("small", False)]
    local.model, cached["small"] = None, True
    local.load()
    assert len(told) == 2  # on disk now: nothing to say


def test_the_language_check_runs_where_verbose_json_is_known_to_work(monkeypatch):
    """Whisper on OpenAI or Groq's address reports the language it heard; gpt-4o-transcribe doesn't, and another
    server may not take verbose_json, so it gets the plain request it always got."""
    from stentor import providers
    formats = []
    monkeypatch.setattr(providers, "_request", lambda cfg, audio, lang, prompt, fmt: formats.append(fmt) or ("Run it.", "english", -0.1))
    for cfg in (config.Config(provider="custom", base_url="https://api.groq.com/openai/v1", api_key="k", languages="he,en"),
                config.Config(provider="openai", model="whisper-1", api_key="k", languages="he,en"),
                config.Config(provider="openai", api_key="k", languages="he,en"),
                config.Config(provider="custom", base_url="http://h/v1", model="Systran/faster-whisper-large-v3", api_key="k", languages="he,en")):
        transcribe_api(cfg, tone(0.5))
    assert formats == ["verbose_json", "verbose_json", "json", "json"]


# ---- configs from when Groq was a preset (and the default) ----

GROQ = "https://api.groq.com/openai/v1"


def sent_request(monkeypatch, cfg) -> tuple[str, str, bytes]:
    """(URL, Authorization, body) of the transcription request `cfg` makes, caught before it leaves."""
    from stentor import providers
    caught = []

    class Reply(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(request, timeout):
        caught.append((request.full_url, request.get_header("Authorization"), request.data))
        return Reply(b'{"text": "hello"}')
    monkeypatch.setattr(providers.urllib.request, "urlopen", urlopen)
    assert transcribe_api(cfg, tone(0.5)) == "hello"
    return caught[-1]


def test_an_old_groq_config_transcribes_as_before_and_keeps_its_key_after_a_save(tmp_path, monkeypatch):
    from stentor.settings_ui import SettingsServer
    monkeypatch.setenv("STENTOR_CONFIG_DIR", str(tmp_path))
    path = tmp_path / "config.toml"
    path.write_text('provider = "groq"\napi_key = "gsk_old1234"\napi_key_service = "groq"\nmodel = ""\nlanguages = "en"\n')
    cfg = config.load()
    assert (cfg.provider, cfg.base_url, cfg.model, cfg.api_key_service) == ("custom", GROQ, "whisper-large-v3", "custom")
    url, auth, body = sent_request(monkeypatch, cfg)
    assert url == GROQ + "/audio/transcriptions" and auth == "Bearer gsk_old1234"
    assert b'name="model"\r\n\r\nwhisper-large-v3\r\n' in body
    server = SettingsServer(cfg, port=0)
    server.save({"hotkey": "ctrl+alt+d"})  # any later save from the settings page
    again = config.load()
    url_again, auth_again, body_again = sent_request(monkeypatch, again)
    assert (url_again, auth_again) == (url, auth) and b'name="model"\r\n\r\nwhisper-large-v3\r\n' in body_again
    assert (again.provider, again.base_url, again.model, again.api_key, again.effective_key()) == ("custom", GROQ, "whisper-large-v3", "gsk_old1234", "gsk_old1234")
    assert 'provider = "custom"' in path.read_text() and "groq\"" not in path.read_text().replace("api.groq.com", "")


def test_a_groq_model_picked_by_hand_is_kept(tmp_path, monkeypatch):
    p = tmp_path / "c.toml"
    p.write_text('provider = "groq"\napi_key = "k"\nmodel = "whisper-large-v3-turbo"\n')
    cfg = config.load(p)
    assert (cfg.effective_base_url(), cfg.effective_model(), cfg.effective_key()) == (GROQ, "whisper-large-v3-turbo", "k")


def test_groq_api_key_still_works_for_a_migrated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_env5678")
    p = tmp_path / "c.toml"
    p.write_text('provider = "groq"\n')
    cfg = config.load(p)
    assert sent_request(monkeypatch, cfg)[:2] == (GROQ + "/audio/transcriptions", "Bearer gsk_env5678")
    config.save(cfg, p)
    assert config.load(p).effective_key() == "gsk_env5678"  # also once saved as a custom server
    assert config.Config(provider="custom", base_url="https://other.example/v1").effective_key() == ""  # only for Groq's address


def test_a_file_without_a_provider_and_with_a_key_stays_on_groq(tmp_path, monkeypatch):
    """Before, the default provider was Groq, so a key in a file with no provider was a Groq key."""
    p = tmp_path / "c.toml"
    p.write_text('api_key = "gsk_nameless"\n')
    cfg = config.load(p)
    assert (cfg.provider, cfg.effective_base_url(), cfg.effective_model(), cfg.effective_key()) == ("custom", GROQ, "whisper-large-v3", "gsk_nameless")
    p.write_text('hotkey = "ctrl+alt+d"\n')
    assert config.load(p).provider == "local"  # nothing to keep using: the new default
    monkeypatch.setenv("GROQ_API_KEY", "gsk_env")
    assert config.load(p).effective_key() == "gsk_env"  # the key in the environment counts too


def test_a_groq_key_saved_while_elsewhere_is_found_on_the_way_back(tmp_path):
    """Saved for Groq, then This computer picked: going back to Your own key finds Groq and the key again."""
    from stentor.settings_ui import SettingsServer
    p = tmp_path / "c.toml"
    p.write_text('provider = "local"\napi_key = "gsk_kept"\napi_key_service = "groq"\n')
    cfg = config.load(p)
    assert cfg.provider == "local" and cfg.api_key_service == "custom"
    server = SettingsServer(cfg, port=0)
    assert server.state()["key_service"] == "custom"
    server.save({"provider": "custom"})
    assert (server.cfg.effective_base_url(), server.cfg.effective_model(), server.cfg.effective_key()) == (GROQ, "whisper-large-v3", "gsk_kept")


def test_a_groq_key_is_never_sent_to_another_custom_server(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('provider = "custom"\nbase_url = "https://mine.example/v1"\napi_key = "gsk_x"\napi_key_service = "groq"\n')
    assert config.load(p).effective_key() == ""


def test_a_key_only_in_the_environment_keeps_the_old_default(tmp_path, monkeypatch):
    """Before, no config file and a key in the environment meant Groq with that key: it still does."""
    for name in ("GROQ_API_KEY", "STENTOR_API_KEY"):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.delenv("STENTOR_API_KEY", raising=False)
        monkeypatch.setenv(name, "gsk_env")
        cfg = config.load(tmp_path / "none.toml")
        assert (cfg.provider, cfg.effective_base_url(), cfg.effective_key()) == ("custom", GROQ, "gsk_env")
    monkeypatch.delenv("STENTOR_API_KEY")
    assert config.load(tmp_path / "none.toml").provider == "local"


def test_setup_still_takes_the_old_groq_choice(tmp_path, monkeypatch):
    """Scripts that ran `stentor setup --provider groq` get Groq's address as another server."""
    from stentor import cli, providers
    path = tmp_path / "config.toml"
    monkeypatch.setattr(config, "config_path", lambda: path)
    monkeypatch.setattr(cli, "ask", lambda question, default="": default)
    monkeypatch.setattr(providers, "check_api", lambda cfg: "")
    assert cli.main(["setup", "--provider", "groq", "--api-key", "gsk_k", "--languages", "he,en", "--no-service"]) == 0
    cfg = config.load(path)
    assert (cfg.provider, cfg.effective_base_url(), cfg.effective_model(), cfg.effective_key()) == ("custom", GROQ, "whisper-large-v3", "gsk_k")
