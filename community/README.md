# Community tools (beta)

Tools made by Open500 users, listed on the website under **Community tools**. They are built from the source in this
folder; they are **not tested by Open500**: try them with your work saved. The sharing system is new in 0.2 and
in beta: if something doesn't work, open an issue.

## Share your tool
**Without git (recommended):** open an issue with the [Submit a tool](https://github.com/M45K/open500/issues/new?template=submit-tool.yml)
form and paste your `.py`. Made it with Claude Code? Ask it to "prepare the Open500 submission".
When the maintainer approves it (label `approved`), GitHub builds the WAV from your source, checks it, and opens a
pull request; when it is merged the tool appears on the website.

**With git:** fork, add `community/<id>/tool.py` and `community/<id>/tool.json`, run
`python3 tools/community.py build <id>` (it builds `docs/community/<id>/<WAV>` and the catalog; no firmware needed),
commit everything and open a pull request. A check rebuilds every tool and fails if the WAVs are not exactly what the
sources build.

## Rules
- The source must define `pg = PageAddon("name8", ...)` like `sdk/examples/hello_page.py`; the 8-character name and the
  WAV name must not be used by an official or another community tool (checked automatically).
- Run your tool's emulator test (see `sdk/README.md`) before submitting; say if you tried it on a real MPC500.
- Never include Akai firmware or other people's samples.
- Tools are published under the **GPL-3.0**, like Open500.

## tool.json
```json
{
 "name": "Chord Pads",
 "wav": "CHORDPAD.WAV",
 "author": "your-github-name",
 "description": "One or two sentences for musicians.",
 "how_to_use": ["MODE -> OTHER -> Chord Pads", "Pick the chord, DO IT, hit the source pad"],
 "lcd": ["CHORD PADS      ", "MIN7    C  >B   "],
 "video": "https://...",
 "hardware_tested": true,
 "version": "1"
}
```
The folder name `<id>` is the WAV name in lower case (`chordpad`).
