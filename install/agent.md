Install and set up Stentor on this computer for me, check that it works, and report any problem you can't fix on GitHub.

Stentor is voice typing for the whole computer: hold Alt+Space, speak, let go, and the words are typed at the cursor. Site: https://stentor-app.vercel.app. Code and issues: https://github.com/Can-0f-Tuna/stentor

1. Install
   - macOS or Linux: curl -fsSL https://stentor-app.vercel.app/install.sh | sh
   - Windows (PowerShell): irm https://stentor-app.vercel.app/install.ps1 | iex
   The installer installs uv if it is missing, installs the latest release, starts Stentor now and at every login, and opens the settings page in my browser. If the stentor command is not found afterwards, open a new shell or add ~/.local/bin to PATH. Run stentor --version to confirm.

2. Where my speech is transcribed. I do this part myself.
   The settings page opens under Transcription > Where. Tell me the choices and let me pick:
   - Stentor Cloud: the easiest, a paid subscription. I sign in and subscribe on the settings page; no key.
   - This computer: free and offline. I press Install the offline engine on the settings page (or you run: stentor offline). It is a large download, and Stentor restarts when it is done.
   - Your own key (advanced): OpenAI, or another OpenAI-compatible server. I paste the key into the settings page and click Check and save.
   Don't ask me to paste a key into this chat, and never write it to a file, a command line or an issue. If the settings page didn't open, run: stentor settings
   Wait until I say it's done.

3. Check the setup
   Run: stentor doctor
   Fix every line marked FIX by following the hint printed under it. Things only I can do, tell me exactly what to click:
   - macOS: allow Microphone, and Accessibility / Input Monitoring for Stentor (or the terminal) in System Settings > Privacy & Security.
   - Linux: when doctor says keyboard access needs the input group, run the command it prints, then I log out and back in once.
   - Windows: if SmartScreen or an antivirus asks about Python, I allow it.
   Run doctor again until it prints "All good." Then check that it is running: on macOS and Linux, stentor service status should print "running". On Windows, this should list a process: Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" | Where-Object { $_.CommandLine -like "*stentor*" }
   If it is not running, run: stentor service install

4. Test transcription
   Make a short WAV of a known English sentence, for example "The quick brown fox jumps over the lazy dog":
   - macOS: say -o /tmp/hd-test.wav --data-format=LEI16@16000 "The quick brown fox jumps over the lazy dog"
   - Linux: espeak-ng -w /tmp/hd-test.wav "The quick brown fox jumps over the lazy dog" (or espeak, or pico2wave)
   - Windows (PowerShell): Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; $s.SetOutputToWaveFile("$env:TEMP\hd-test.wav"); $s.Speak("The quick brown fox jumps over the lazy dog"); $s.Dispose()
   Then run: stentor transcribe <that file>
   It should print roughly the same sentence. If no speech tool is available, skip this step and say so.

5. Test the real thing with me
   Ask me to click into any text field, hold Alt+Space, say a sentence, and let go. A small pill shows at the bottom of the screen while it listens (on macOS, and on Linux with Wayland and quickshell; elsewhere there's a short sound). The text should appear about a second later. Ask me whether it worked.

6. If something fails that you can't fix
   Report it so the maintainers can fix it:
   a. Search existing issues first: gh issue list --repo Can-0f-Tuna/stentor --state all --search "<a few words about the error>"
      If one matches, add a comment with your details (gh issue comment <number> --repo Can-0f-Tuna/stentor --body-file <file>) instead of opening a new one.
   b. Otherwise write a new issue with:
      - Title: a short description of the problem, starting with [agent]
      - Operating system and version, desktop session on Linux (echo $XDG_SESSION_TYPE $XDG_CURRENT_DESKTOP), CPU architecture
      - stentor --version
      - The full output of stentor doctor
      - The step that failed, the exact command, and its full output or error message
      - What you already tried
      Remove the API key and anything else private before posting. Keys look like sk-... or any long random string. Never attach the config file itself.
   c. Show me the issue text and post it once I agree: gh issue create --repo Can-0f-Tuna/stentor --title "<title>" --body-file <file>
      If gh is not installed or not signed in, give me this link to open instead, with the title and body URL-encoded: https://github.com/Can-0f-Tuna/stentor/issues/new?title=<title>&body=<body>
   d. Give me the link to the issue.

7. When you are done, tell me in a few lines: what you installed and which version, what doctor says, whether both tests passed, and any issue you opened. Mention that settings (where speech is transcribed, hotkey, languages, model, recording pill look) are in the Stentor icon next to the clock or in the menu bar, or by running stentor settings, and that updates install themselves.
