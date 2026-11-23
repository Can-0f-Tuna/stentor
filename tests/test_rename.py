"""An install from when the app was called hold-dictate, met by the first Stentor: its settings come along, its
variables still count, and its start-at-login entry goes so two copies never run together."""
import sys
import types
from pathlib import Path

import pytest

from stentor import cli, config as cfgmod, service


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A Linux user's folders with nothing chosen by hand."""
    monkeypatch.delenv("STENTOR_CONFIG_DIR", raising=False)
    monkeypatch.delenv("HOLD_DICTATE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return tmp_path


def old_install(home):
    old = home / "config" / "hold-dictate"
    old.mkdir(parents=True)
    (old / "config.toml").write_text('hotkey = "ctrl+space"\nlearned_terms = "Stentor"\n')
    (old / "history.json").write_text("[]")
    (old / "account.json").write_text('{"token": "t"}')
    (old / "hold-dictate.log").write_text("old lines")
    (old / "settings-server.json").write_text('{"port": 1}')
    return old


def test_the_old_folder_is_copied_once_and_left_in_place(home):
    old = old_install(home)
    assert cfgmod.adopt_old_folder()
    new = home / "config" / "stentor"
    assert sorted(p.name for p in new.iterdir()) == ["account.json", "config.toml", "history.json"]  # no log, no stale server note
    assert cfgmod.load().hotkey == "ctrl+space" and cfgmod.load().extra["learned_terms"] == "Stentor"
    assert (old / "config.toml").exists() and (old / "hold-dictate.log").exists()
    (new / "config.toml").write_text('hotkey = "f9"\n')
    assert not cfgmod.adopt_old_folder() and cfgmod.load().hotkey == "f9"  # the new folder wins from then on


def test_an_existing_new_folder_wins_without_merging(home):
    old_install(home)
    new = home / "config" / "stentor"
    new.mkdir()
    assert not cfgmod.adopt_old_folder() and list(new.iterdir()) == []


def test_nothing_is_copied_without_an_old_folder_or_into_a_chosen_one(home, monkeypatch):
    assert not cfgmod.adopt_old_folder() and not (home / "config" / "stentor").exists()
    old_install(home)
    monkeypatch.setenv("STENTOR_CONFIG_DIR", str(home / "mine"))
    assert not cfgmod.adopt_old_folder() and not (home / "mine").exists() and not (home / "config" / "stentor").exists()


def test_any_command_brings_the_old_settings_along(home, capsys):
    old_install(home)
    assert cli.main(["config"]) == 0
    assert capsys.readouterr().out.strip() == str(home / "config" / "stentor" / "config.toml")
    assert (home / "config" / "stentor" / "account.json").exists()


def test_a_variable_set_under_the_old_name_still_counts(monkeypatch, tmp_path):
    monkeypatch.delenv("STENTOR_CONFIG_DIR", raising=False)
    monkeypatch.delenv("HOLD_DICTATE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOLD_DICTATE_CONFIG_DIR", str(tmp_path / "old"))
    monkeypatch.setenv("HOLD_DICTATE_API_KEY", "old-key")
    assert cfgmod.config_dir() == tmp_path / "old" and cfgmod.Config(provider="custom").effective_key() == "old-key"
    monkeypatch.setenv("STENTOR_CONFIG_DIR", str(tmp_path / "new"))
    monkeypatch.setenv("STENTOR_API_KEY", "new-key")
    assert cfgmod.config_dir() == tmp_path / "new" and cfgmod.Config(provider="custom").effective_key() == "new-key"


@pytest.fixture
def ran(monkeypatch):
    calls = []
    monkeypatch.setattr(service, "_sh", lambda *cmd: calls.append(cmd) or 0)
    monkeypatch.setattr(service.subprocess, "Popen", lambda *a, **k: calls.append(("start",)))
    return calls


def test_linux_install_takes_the_old_unit_away_first(home, ran, monkeypatch):
    monkeypatch.setattr(service.shutil, "which", lambda name: "/usr/bin/" + name)
    old_unit, old_entry = service._linux_unit("hold-dictate"), service._linux_desktop("hold-dictate")
    portal = old_entry.with_name("io.github.hold-dictate.desktop")
    for file in (old_unit, old_entry, portal):
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("old")
    assert "running now" in service.install()
    assert not old_unit.exists() and not old_entry.exists() and not portal.exists()
    assert service._linux_unit().name == "stentor.service" and "-m stentor run" in service._linux_unit().read_text()
    stopped, started = ran.index(("systemctl", "--user", "disable", "--now", "hold-dictate.service")), ran.index(("systemctl", "--user", "restart", "stentor.service"))
    assert stopped < started

    del ran[:]
    assert "running now" in service.install()  # again, with nothing old left: the old name is not touched
    assert not any("hold-dictate.service" in cmd for cmd in ran)


def test_linux_uninstall_removes_both_generations(home, ran):
    files = [service._linux_unit(), service._linux_desktop(), service._linux_unit("hold-dictate"), service._linux_desktop("hold-dictate")]
    for file in files:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("x")
    assert service.uninstall() == "Autostart removed and stopped."
    assert not any(file.exists() for file in files)
    assert ("systemctl", "--user", "disable", "--now", "stentor.service") in ran and ("systemctl", "--user", "disable", "--now", "hold-dictate.service") in ran


def test_mac_install_boots_the_old_agent_out(tmp_path, ran, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    old = service._mac_plist("io.github.hold-dictate")
    old.parent.mkdir(parents=True)
    old.write_text("old")
    assert "running now" in service.install()
    assert not old.exists() and service._mac_plist().name == "io.github.stentor.plist" and service._mac_plist().exists()
    assert ran[0][:2] == ("launchctl", "bootout") and ran[0][-1].endswith("/io.github.hold-dictate")
    service.uninstall()
    assert not service._mac_plist().exists()


def test_windows_install_swaps_the_run_value(ran, monkeypatch):
    values = {"hold-dictate": "old line"}

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def delete(_key, name):
        if name not in values:
            raise FileNotFoundError(name)
        del values[name]

    winreg = types.SimpleNamespace(HKEY_CURRENT_USER=0, KEY_SET_VALUE=0, REG_SZ=1, OpenKey=lambda *a: Key(), CreateKey=lambda *a: Key(),
                                   DeleteValue=delete, SetValueEx=lambda _key, name, _zero, _kind, line: values.update({name: line}))
    monkeypatch.setitem(sys.modules, "winreg", winreg)
    monkeypatch.setattr(sys, "platform", "win32")
    service.install()
    assert list(values) == ["stentor"] and "-m stentor run" in values["stentor"]
    assert ran[0][0] == "powershell" and r"-m\s+hold_dictate\s+run" in ran[0][-1] and ran[1] == ("start",)
    service.uninstall()
    assert values == {}


def test_the_mark_is_drawn_for_the_tray_and_for_a_page():
    from stentor import mark, settings_ui
    assert mark.svg(22).startswith('<svg width="22"') and mark.svg(22) in settings_ui.STALE_PAGE.decode()
    image = pytest.importorskip("PIL.Image") and mark.image(64)
    assert image.size == (64, 64) and image.getpixel((0, 0))[3] == 0 and image.getpixel((30, 34))[3] == 255  # clear corner, solid helmet
