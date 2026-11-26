"""The package as Stentor sees it: found through its entry point, and saved files from 0.8.3 keep working."""
import json
import time
import urllib.error
import urllib.request

import numpy as np
import pytest

from stentor import cli, config, extensions
from stentor.app import Dictation
from stentor.config import Config
from stentor.hotkey import parse_hotkey
from stentor.providers import transcribe_api
from stentor.settings_ui import SettingsServer
from stentor_pro.extension import Extension

from helpers import FakeFeedback, FakeRecorder


def tone(seconds, freq=220, amp=0.3):
    return (np.sin(2 * np.pi * freq * np.arange(int(16000 * seconds)) / 16000) * amp).astype(np.float32)


@pytest.fixture
def server(tmp_path, monkeypatch, signed_in):
    monkeypatch.setenv("STENTOR_CONFIG_DIR", str(tmp_path))
    (tmp_path / "account.json").write_text(signed_in.read_text())
    s = SettingsServer(Config(api_key="gsk_secret1234"), port=0).start()
    yield s
    s.stop()


def call(server, path, body=None):
    headers = {"X-Token": server.token, "Content-Type": "application/json"}
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_the_entry_point_loads():
    assert [type(found) for found in extensions.loaded()] == [Extension]


@pytest.mark.parametrize("settings", [{"provider": "openai", "api_key": "k"}, {"provider": "custom", "base_url": "http://127.0.0.1:9/v1", "api_key": "k"}])
def test_dictation_needs_no_account(signed_in, settings):
    """Signed out, your own key records at once: no sign-in, no settings page opening, no error."""
    signed_in.unlink()
    opened, started, fb = [], [], FakeFeedback()
    d = Dictation(Config(**settings), fb, recorder_factory=lambda level: started.append(1), on_no_key=lambda: opened.append(1))
    d.on_action("start")
    assert started == [1] and not opened and not any(isinstance(e, tuple) and e[0] == "error" for e in fb.log)


def test_nothing_asks_for_a_sign_in_before_dictating(pro, signed_in):
    signed_in.unlink()
    d = Dictation(Config(provider="local"), FakeFeedback(), recorder_factory=FakeRecorder)
    assert pro.before_recording(d) == "" and pro.second_recording(d) is None  # the edit shortcut dictates as before


def test_started_signed_out_opens_nothing(pro, signed_in, capsys):
    signed_in.unlink()
    opened = []
    server = type("Server", (), {"save": lambda self, changes: None, "open": lambda self: opened.append(1)})()
    d = Dictation(Config(provider="local"), FakeFeedback(), recorder_factory=FakeRecorder)
    pro.started(d, server, type("Detector", (), {"wait_released": lambda self, t: True})())
    assert opened == [] and "sign in" not in capsys.readouterr().out


def test_doctor_has_nothing_to_fix_signed_out(pro, signed_in):
    signed_in.unlink()
    assert pro.doctor_lines() == []
    signed_in.write_text(json.dumps({"token": "t", "checked": time.time()}))
    assert pro.doctor_lines() == [(True, "signed in to your Stentor account", "")]
    signed_in.write_text(json.dumps({"token": "t", "plan": "plus", "checked": time.time()}))
    assert pro.doctor_lines() == [(True, "Stentor Cloud on", "")]


def test_settings_save_signed_out(server, tmp_path):
    (tmp_path / "account.json").unlink()
    code, state = call(server, "/api/config", {"beeps": False, "plus_clean": False})
    assert code == 200 and state["account"] is None and config.load(tmp_path / "config.toml").beeps is False


def test_settings_premium_marks_the_notice_and_opens_its_section(pro, signed_in, monkeypatch):
    signed_in.write_text(json.dumps({"token": "t", "plan": "plus", "premium_notice": True, "notice_round": 2, "checked": time.time()}))
    seen = []
    monkeypatch.setattr(cli, "cmd_settings", lambda args, anchor="", alongside=None: seen.append((anchor, alongside is not None)) or 0)
    assert cli.main(["settings", "--premium"]) == 0
    assert seen == [("#premGroup", True)] and not extensions.loaded()[0].account.premium_notice()
    assert cli.main(["settings"]) == 0 and seen[-1] == ("", False)


def test_the_page_has_the_premium_and_account_sections(server):
    with urllib.request.urlopen(server.url, timeout=5) as r:
        page = r.read().decode()
    assert 'id="premGroup"' in page and 'id="acctGroup"' in page and "pageHooks.blockers.push" in page
    assert page.index('id="premGroup"') < page.index('id="updGroup"') and page.index("revealPremium") < page.index("---- start ----")


def test_the_page_part_names_stentor_cloud_and_never_groq(pro):
    """What this package adds to the settings page: Stentor Cloud by name, never the old Plus or Premium, never Groq."""
    import re
    for part in pro.settings_page():
        assert not re.search(r"groq", part, re.I)
        assert not re.search(r"\bPremium\b|\bPlus\b", part)  # renderPremium, premGroup and the like are ids, not words
        assert "Stentor Cloud" in part


def test_the_page_script_parses(tmp_path):
    """A syntax error would blank the whole settings page while every Python test stays green."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    from stentor_pro.extension import HERE
    script = (HERE / "settings_part.js").read_text(encoding="utf-8")
    body = script.strip().removeprefix("<script>").removesuffix("</script>")
    path = tmp_path / "settings_part.js"
    path.write_text(body, encoding="utf-8")
    result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_a_bad_edit_shortcut_in_the_file_is_turned_off(pro, capsys):
    hotkey = parse_hotkey("alt+space")
    cfg = Config(extra={"edit_hotkey": "alt+pageup"})
    assert pro.second_hotkey(cfg, hotkey) is None and cfg.extra["edit_hotkey"] == ""
    assert "doesn't work" in capsys.readouterr().err
    assert pro.second_hotkey(Config(extra={"edit_hotkey": "alt+space"}), hotkey) is None
    assert str(pro.second_hotkey(Config(), hotkey)) == "alt+shift+space"  # the default


def test_the_notice_comes_with_the_next_dictation_until_it_is_clicked(pro, signed_in):
    class Fb:
        events, sent = object(), []

        def send(self, line):
            self.sent.append(line)
    pro.feedback = Fb()
    assert pro.pill_lines() == []
    pro.tick()
    assert Fb.sent == []
    signed_in.write_text(json.dumps({"token": "t", "premium": True, "premium_notice": True, "notice_round": 2, "checked": time.time()}))
    assert pro.pill_lines() == []  # the old Premium flag alone turns nothing on, so no notice says it did
    signed_in.write_text(json.dumps({"token": "t", "plan": "plus", "premium_notice": True, "notice_round": 2, "checked": time.time()}))
    assert pro.pill_lines() == ["P on"]  # before S, so the ball comes with the pill
    pro.tick()
    assert Fb.sent == []
    pro.account.premium_notice_seen()
    pro.tick()
    pro.tick()
    assert Fb.sent == ["P -"]


def test_without_an_overlay_the_notice_is_a_notification(pro, signed_in):
    notified = []
    pro.feedback = type("Fb", (), {"events": None, "notify": lambda self, m: notified.append(m)})()
    signed_in.write_text(json.dumps({"token": "t", "plan": "plus", "premium_notice": True, "notice_round": 2, "checked": time.time()}))
    pro.tick()
    assert "Stentor Cloud is on" in notified[0] and not pro.account.premium_notice()


OLD_CONFIG = '''# Stentor settings. Edit by hand, or run `stentor settings`.

provider = "groq"
api_key = "gsk_old1234567"
api_key_service = "groq"
languages = "he,en"
hotkey = "alt+space"
edit_hotkey = "alt+ctrl+e"
live_text = false
word_overrides = { "versal" = "Vercel" }
premium_style = true
premium_learn = true
learned_terms = "Kubernetes, pnpm"
learned_fixes = { "get hub" = "GitHub" }
'''
OLD_ACCOUNT = {"token": "tok", "email": "you@example.com", "name": "You", "premium": True, "checked": 0, "notice_round": 2}


def test_a_saved_0_8_3_config_and_account_keep_working(tmp_path, pro, monkeypatch):
    monkeypatch.setenv("STENTOR_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text(OLD_CONFIG, encoding="utf-8")
    (tmp_path / "account.json").write_text(json.dumps(OLD_ACCOUNT))
    from stentor_pro.account import Account
    pro.account = Account(site="http://127.0.0.1:9")
    cfg = config.load()
    assert str(pro.second_hotkey(cfg, parse_hotkey(cfg.hotkey))) == "alt+ctrl+e"
    assert pro.account.premium() and not pro.account.premium_notice()
    d = Dictation(cfg, FakeFeedback(), recorder_factory=FakeRecorder, paster=lambda *a, **k: "")
    assert extensions.rewriter(d) is None and not d._types_live()  # Premium on your own key is gone: as heard
    assert extensions.adjust_words({}, cfg) == {} and extensions.adjust_prompt("Hint", cfg) == "Hint"  # so is the dictionary
    (tmp_path / "account.json").write_text(json.dumps(dict(OLD_ACCOUNT, plan="plus")))
    assert extensions.rewriter(d) is not None  # with Stentor Cloud, all of it is on as it was saved
    assert extensions.adjust_words({}, cfg) == {"get hub": "GitHub"}
    assert extensions.adjust_prompt("Hint", cfg) == "Hint Kubernetes, pnpm."
    server = SettingsServer(cfg, port=0).start()
    try:
        code, state = call(server, "/api/config", {"beeps": True})
        assert code == 200 and state["account"]["email"] == "you@example.com"
        assert state["config"]["premium_learn"] is True and state["premium"]["terms"] == ["Kubernetes", "pnpm"]
    finally:
        server.stop()
    after = config.load()
    assert after.extra == {"edit_hotkey": "alt+ctrl+e", "premium_learn": True, "learned_terms": "Kubernetes, pnpm",
                           "learned_fixes": {"get hub": "GitHub"}, "plus_cloud": True, "plus_clean": True,
                           "clean_model": "qwen/qwen3.8-27b"}


def retried(monkeypatch, *answers):
    from stentor import providers
    answers, sent = iter(answers), []
    monkeypatch.setattr(providers, "_request", lambda cfg, audio, lang, prompt, fmt: sent.append(lang) or next(answers))
    # the address of a saved Groq setting, where the core asks for the language heard (_request never leaves here)
    cfg = config.Config(provider="custom", base_url=config.OLD_GROQ["base_url"], model="whisper-large-v3", api_key="k", languages="he,en")
    return transcribe_api(cfg, tone(0.5)), sent


def test_unsure_english_is_checked_in_the_first_language(monkeypatch):
    text, sent = retried(monkeypatch, ("Time shall also be coming.", "english", -0.8), ("בדיקה", "hebrew", -0.2))
    assert text == "בדיקה" and sent == ["", "he"]


def test_the_surer_text_wins_the_second_look(monkeypatch):
    text, _ = retried(monkeypatch, ("Ship it!", "english", -0.5), ("שיפיט", "hebrew", -0.9))
    assert text == "Ship it!"


def test_confident_second_language_is_left_alone(monkeypatch):
    text, sent = retried(monkeypatch, ("Run the build again.", "english", -0.1))
    assert text == "Run the build again." and sent == [""]


def test_edit_shortcut_is_checked_and_must_differ(server):
    code, state = call(server, "/api/config", {"edit_hotkey": "Ctrl+Shift+E"})
    assert code == 200 and state["config"]["edit_hotkey"] == "ctrl+shift+e"  # written the usual way
    code, body = call(server, "/api/config", {"edit_hotkey": config.DEFAULT_HOTKEY})  # the dictation shortcut
    assert code == 400 and "different shortcut" in body["error"]
    code, body = call(server, "/api/config", {"hotkey": "ctrl+shift+e"})
    assert code == 400 and "Edit by voice shortcut" in body["error"]
    for bad in ("space", "alt+pageup", "shift+"):
        code, body = call(server, "/api/config", {"edit_hotkey": bad})
        assert code == 400 and body["error"].startswith("edit shortcut")
    code, state = call(server, "/api/config", {"edit_hotkey": ""})
    assert code == 200 and state["config"]["edit_hotkey"] == ""  # off
    assert server.cfg.extra["edit_hotkey"] == ""


def test_typing_as_you_speak_and_the_clean_up_turn_each_other_off(server, tmp_path):
    call(server, "/api/config", {"plus_clean": False})
    code, state = call(server, "/api/config", {"plus_clean": True})
    assert state["config"]["plus_clean"] and not state["config"]["live_text"]
    code, state = call(server, "/api/config", {"live_text": True})
    assert state["config"]["live_text"] and not state["config"]["plus_clean"]
    code, state = call(server, "/api/config", {"plus_clean": True})
    assert state["config"]["plus_clean"] and not state["config"]["live_text"]
    saved = config.load(tmp_path / "config.toml")
    assert saved.extra["plus_clean"] and not saved.live_text
    code, state = call(server, "/api/config", {"live_text": False})  # going back to the default turns nothing on
    assert state["config"]["plus_clean"]
    code, state = call(server, "/api/config", {"plus_clean": False})
    assert not state["config"]["plus_clean"] and not state["config"]["live_text"]


def test_premium_switches_and_the_learned_lists(server, tmp_path):
    server.cfg.extra.update(learned_terms="Kubernetes, Vercel", learned_fixes={"get hub": "GitHub", "versal": "Vercel"})
    code, state = call(server, "/api/state")
    assert state["premium"] == {"terms": ["Kubernetes", "Vercel"]}  # nothing about a key of your own
    code, state = call(server, "/api/config", {"plus_clean": True, "premium_learn": True, "learned_terms": "Kubernetes",
                                                "learned_fixes": {"get hub": "GitHub"}})
    assert code == 200 and state["config"]["plus_clean"] and state["config"]["premium_learn"]
    assert state["premium"]["terms"] == ["Kubernetes"] and state["config"]["learned_fixes"] == {"get hub": "GitHub"}
    saved = config.load(tmp_path / "config.toml")
    assert saved.extra["learned_terms"] == "Kubernetes" and saved.extra["learned_fixes"] == {"get hub": "GitHub"} and saved.extra["plus_clean"]
    assert call(server, "/api/config", {"plus_clean": "yes"})[0] == 400
    assert call(server, "/api/config", {"learned_fixes": ["nope"]})[0] == 400
    assert call(server, "/api/config", {"clean_model": "llama-3.3-70b"})[0] == 400
    assert call(server, "/api/config", {"clean_model": "openai/gpt-oss-120b"})[1]["config"]["clean_model"] == "openai/gpt-oss-120b"
    code, state = call(server, "/api/config", {"learned_terms": " a, A, b ,, c", "learned_fixes": {"x" * 61: "y", "ok": "fine"}})
    assert state["config"]["learned_terms"] == "a, b, c" and state["config"]["learned_fixes"] == {"ok": "fine"}
    code, state = call(server, "/api/config", {"learned_terms": "", "learned_fixes": {}})  # Forget everything
    assert state["premium"]["terms"] == [] and state["config"]["learned_fixes"] == {}


def sharing_on(account_file, **more):
    data = json.loads(account_file.read_text())
    account_file.write_text(json.dumps(dict(data, collection=True, consent_version=1, **more)))


def test_the_card_is_in_the_page_and_stays_out_of_the_state_while_the_switch_is_off(server):
    with urllib.request.urlopen(server.url, timeout=5) as r:
        page = r.read().decode()
    for text in ("Help improve accuracy?", "Save your recordings and their text on our servers to train our models. Change it anytime in Settings.",
                 "What we collect", "Yes, help", "No thanks", "Delete my data", "pageHooks.renders.push(renderSharing)", "/api/sharing/delete"):
        assert text in page, text
    assert call(server, "/api/state")[1]["sharing"]["ask"] is False
    assert call(server, "/api/state")[1]["sharing"]["on"] is False


def test_an_answer_does_nothing_while_the_switch_is_off(server, tmp_path):
    status, reply = call(server, "/api/sharing/yes", {})
    assert status == 400 and "consent" not in json.loads((tmp_path / "account.json").read_text())


def test_the_page_answers_and_the_answer_is_kept_with_the_account(server, tmp_path):
    sharing_on(tmp_path / "account.json")
    assert call(server, "/api/state")[1]["sharing"]["ask"] is True
    status, state = call(server, "/api/sharing/no", {})
    assert status == 200 and state["sharing"] ["answer"] == "no" and state["sharing"]["ask"] is False
    assert json.loads((tmp_path / "account.json").read_text())["consent"]["choice"] == "no"


def test_the_buttons_of_the_stork_run_a_command(tmp_path, monkeypatch, signed_in):
    sharing_on(signed_in)
    assert cli.main(["consent", "yes"]) == 0
    assert json.loads(signed_in.read_text())["consent"]["choice"] == "yes"
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    assert cli.main(["consent", "info"]) == 0
    assert opened[0].endswith("/data")


def test_the_buttons_do_nothing_with_the_switch_off(signed_in):
    assert cli.main(["consent", "yes"]) == 0
    assert "consent" not in json.loads(signed_in.read_text())


def test_delete_answers_with_sharing_already_off(server, tmp_path, monkeypatch):
    sharing_on(tmp_path / "account.json", consent={"version": 1, "choice": "yes", "sent": True})
    pro = extensions.loaded()[0]
    monkeypatch.setattr(pro.account, "_request", lambda *a, **k: (200, {"deleted": 2, "files_pending": False}))
    status, state = call(server, "/api/sharing/delete", {})
    assert status == 200 and state["deleted"]["deleted"] == 2 and state["sharing"]["answer"] == "no"
