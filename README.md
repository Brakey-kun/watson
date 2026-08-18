#  Watson OSINT Workbench

You give it a target (username, email, domain, phone number, location, or business name) and it goes ham — runs a ton of searches, uses a local AI to analyze what it finds, generates follow-up google dorks, and keeps digging until it has a solid profile. then it spits out a clean visual HTML report.

it's all local. no cloud APIs, no data leaving your machine. just you, your browser, and a local LLM.

To clarify, this is an experimental LLM coding quality benchmarking project (for big pickle and nemotron 3 ultra from opencode zen), that I used to check how far I could push public semi-frontier level LLMs to code cybersecurity related tasks, so even if the tool is useable, it's not human Engineer-level quality or Claude-level quality, as it's been mostly coded using opencode's big pickle model and nemotron 3 ultra, of which the latter is not reaaaally a coding model. 
If I were to ever take the project more seriously and start working on it more manually, I'll mention it here :D .

But if you have any suggestion for improvements, fixes, features, or tweaks, I'd love to hear them out, as I'd love to push this thing a little further, and also at the same time test out new prompting techniques, figure out more novel instructions and approaches on how it should actually work (giving the AI the actual human engineering and architecture, and letting it do the syntaxing), and for benchmarking more models and tools, maybe to even transform this into an actual serious project someday xD.  

---

## what it does

- **multi-category search** — supports username, domain, email, phone, location, and business lookups across 1100+ sources
- **auto-detect mode** — don't know what category your target is? let the AI figure it out
- **google dorking** — 490+ pre-built dorks for domains, plus the AI generates custom ones on the fly based on what it finds
- **multimedia extraction** — feed the RAG context uploader an image (EXIF, GPS, OCR text) or a document (PDF/DOCX/text) and it pulls structured findings out to steer the investigation with
- **multi-round research** — the AI analyzes findings after each round and decides what to search next (up to 50 rounds)
- **visual reports** — generates slick HTML reports with dark mode, sidebar TOC, card grids, and metadata stats
- **web dashboard** — flask-based GUI with real-time logs, history viewer, and report previewer
- **urgency modes** — bump up the intensity for time-sensitive cases
- **context steering (RAG)** — drag-drop a file or paste a note mid-run and it's tagged by trust tier, then steered into the research loop as a pheromone-weighted hint that reinforces itself the more it pays off. every investigation is its own independent, isolated case — hints and insights never leak into a different investigation
- **investigation insights panel** — live sidebar view into what's actually steering the research: active hints with their weight and trust tier, doubt-search claims under verification, and the current plan timeline
- **research strategies** — pick `auto` (the AI decides when it's done), `focused` (fast, lowest-noise, skips the divergent burst-search pass), or `exhaustive_osint_sweep` (broader per-round category checklist, resists stopping early, appends a deterministic claims ledger to the report) — docs are one click away in the dashboard
- **VRAM-aware model assignment** — detect available GPU VRAM and get a suggested context length, then assign a model straight from a dropdown of what's actually loaded on your backend
- **model tiers** — assign different models to the thinker/default/small roles independently and hot-swap them mid-session, no restart needed
- **skills panel** — promote a lesson learned in one investigation into a reusable skill, then toggle which ones are active for future runs
- **clear data / reset** — "Clear Data" on the insights panel deletes just the current investigation's case record and uploaded context hints (run history/claims survive); "Reset All Data" in Settings wipes every investigation, project, hint, claim, plan, and learned skill plus all generated reports, no backup, without touching your LLM backend config. Both refuse to run while an investigation is active.

---

## getting started

### prerequisites

- **Python 3.10+**
- **LM Studio** (or any OpenAI-compatible local inference server)
  - load up whatever model you prefer -- Watson detects what's loaded and auto-selects it (or asks you to pick one in Settings > Assign Model to Backend if there's more than one)
  - start the local server (default port `1234`)

### run it

**Windows:**
```cmd
run.bat
```

**macOS / Linux:**
```bash
chmod +x run.sh
./run.sh
```

that's it. the script creates a venv, installs deps, and opens the dashboard at `http://localhost:5000`.

### first time setup

on first launch you'll get a setup wizard that walks you through:
1. picking your LLM backend
2. entering the endpoint URL
3. testing the connection
4. saving the config

you can re-run the wizard anytime from the settings gear icon.

---

## configuration

watson keeps your API keys, investigation history, and generated reports outside the repo entirely - this is a public source tree, so nothing you configure or investigate should ever end up committed or zipped into a release by accident. On first launch it auto-creates `config.json` (plus `investigations.db` and a `reports/` folder) in your OS's per-user data directory:

- windows: `%LOCALAPPDATA%\Watson\`
- macOS: `~/Library/Application Support/Watson/`
- linux: `$XDG_DATA_HOME/watson/` (or `~/.local/share/watson/`)

easiest way to configure it is the in-app setup wizard (gear icon): pick your backend, enter the endpoint, test the connection, save. `config.example.json` in this repo documents the full schema if you'd rather edit the file by hand.

```json
{
  "llm": { "backend": "lm_studio" },
  "backends": {
    "lm_studio": {
      "endpoint": "http://127.0.0.1:1234/v1",
      "api_key": "lm-studio",
      "model": "your-model-name",
      "temperature": 0.5
    }
  }
}
```

(a bare `"llm": { "host", "port", "model", "temperature" }` block also still loads — it's migrated into the `backends` shape above automatically — but new configs should use `backends` directly. `config.example.json` also documents `tiers` for assigning separate models to the thinker/default/small roles.)

you can also change the backend URL directly from the GUI sidebar without editing any files.

set the `WATSON_DATA_DIR` environment variable to point config/history/reports at a different directory instead - e.g. to run fully self-contained out of a portable install.

---

## project structure

```
├── main.py              # standalone one-shot CLI (wraps osint_workbench's engine)
├── gui.py               # flask web dashboard
├── visual_report.py     # HTML report generator (odysseus theme)
├── sources.json         # 1100+ OSINT source URLs by category
├── system-prompt.md     # AI system prompt (customize research style here)
├── config.example.json  # config schema reference (actual config.json lives outside the repo - see the configuration section above)
├── requirements.txt     # python dependencies
├── templates/           # jinja2 templates - dashboard.html (web UI) + report.html/report.md (report output)
├── osint_workbench/     # modular package (core, multimedia, reporting, api)
├── run.bat              # windows launcher
└── run.sh              # unix launcher
```

---

## tech stack

- python + flask for the backend
- openai python client (talking to local LM Studio)
- beautifulsoup4 for html parsing
- jinja2 + markdown + nh3 for report generation
- tailwind css for the frontend

---

## disclaimer

this is a research/educational tool. use it responsibly and legally. don't be weird with it.
