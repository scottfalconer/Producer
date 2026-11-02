# Bundled chat-overlay fonts

The compact simulated-chat painter uses unmodified **Noto Sans** and monochrome **Noto Emoji** from the official Google Fonts repository. They are bundled so ordinary fixture punctuation and emoji do not depend on the host machine's font installation or a runtime network request.

- Noto Sans: Copyright 2022 The Noto Project Authors. [SIL OFL 1.1](NotoSans-OFL.txt).
- Noto Emoji: Copyright 2013 Google LLC. [SIL OFL 1.1](NotoEmoji-OFL.txt).
- Exact upstream download URLs, byte sizes and SHA-256 values are retained in [sources.json](sources.json), retrieved October 31, 2026. Font bytes are unmodified; local filenames are shortened for loading.

Keep the accompanying license/copyright files with redistributed font files. These font licenses do not establish a license for Producer's original code or other media.

Common emoji are drawn in monochrome. This implementation does not promise full shaping or glyph coverage for every Unicode sequence or language. The legacy `panel-v1` painter retains the old Pillow font and original archived pixels.
