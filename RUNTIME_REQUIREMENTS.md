# AstroPhLLMWiki Ubuntu x86_64 Package

Run on Ubuntu:

```bash
cd packaged/AstroPhLLMWiki-Ubuntu-x86_64
./AstroPhLLMWiki
```

The executable opens a standalone desktop window by default. It includes the
Python runtime and Qt/PySide6 desktop runtime used by pywebview.

Browser fallback:

```bash
./AstroPhLLMWiki --browser
```

Smoke test:

```bash
./AstroPhLLMWiki --smoke-test
```

The app uses this local URL internally:

```text
http://127.0.0.1:8765/app
```

If port `8765` is busy, the next available port is used automatically.

Required Ubuntu system libraries if the desktop window does not open:

```bash
sudo apt update
sudo apt install -y \
  libgl1 libegl1 libxkbcommon0 libxkbcommon-x11-0 libxcb-cursor0 \
  libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 \
  libxcb-shape0 libxcb-xinerama0 libnss3 libdbus-1-3 libasound2t64
```

On Ubuntu versions where `libasound2t64` is not available:

```bash
sudo apt install -y libasound2
```

LLM connection:

- Use local Ollama, or
- Enter an OpenAI-compatible API base URL and API key in Settings.

User settings are stored outside the package:

```text
~/.config/AstroPhLLMWiki/local_settings.json
```
