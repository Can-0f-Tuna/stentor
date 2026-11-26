"""The official update channel: the site's stentor-release.json names the public tag of the app and this package's wheel.
An update replaces the whole install, so the wheel must go with it. Anything wrong with the file raises, and the
updater then stays on this version; it never falls back to GitHub, which would drop this package."""
import json
import re
import urllib.request

from stentor import __version__, updater

from .account import SITE


# With the offline engine comes the library that runs the English model (english_model.py); small beside it.
ENGLISH_ENGINE = "onnx-asr[cpu,hub]>=0.12,<1"
# Not release.json: that name belongs to the copies installed as hold-dictate, which must stay on their last release.
RELEASE = "stentor-release.json"


class Channel:
    def __init__(self, site: str = SITE):
        self.site = site
        self.wheel = ""

    def latest(self) -> str:
        request = urllib.request.Request(f"{self.site}/{RELEASE}", headers={"User-Agent": f"stentor/{__version__}", "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            release = json.load(response)
        version, tag, wheel = release["version"], release["core_tag"], release["wheel"]
        if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version) or tag != f"v{version}":
            raise ValueError(f"{RELEASE} has a bad version")
        if not isinstance(wheel, str) or not re.fullmatch(rf"{re.escape(self.site)}/dl/[\w.-]+\.whl", wheel):
            raise ValueError(f"{RELEASE} has a bad wheel address")
        self.wheel = wheel
        return tag

    def install_args(self, tag: str, extras=None) -> list[str]:
        """The core's updater puts `--python` (this copy's) in front; `extras` are the ones to keep or add."""
        spec = updater.install_spec(tag) if extras is None else updater.install_spec(tag, extras)  # a core from before extras
        offline = "local" in (updater.installed_extras() if extras is None else extras)
        return ["--with", self.wheel, *(["--with", ENGLISH_ENGINE] if offline else []), spec]
