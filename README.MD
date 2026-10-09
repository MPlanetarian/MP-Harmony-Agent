# MP Harmony Agent (`MP_Harmony_Agent.py`)

A local autonomous system administration, scripting, DJ Mix Studio, and research agent powered by the **OpenAI Harmony** specification and **Ollama**. `MP_Harmony_Agent.py` bridges low-level host OS management (file inspection, unified diff patching, regex search, live web search, process supervision, and shell execution) with DJ mix library intelligence, hands-free voice control via `local-voice-talk`, and full **OpenAI API** compatibility for integration into frontends like **Open WebUI**.

---

## Key Features

- **Harmony Protocol Native:** Direct interaction with raw Harmony token channels (`analysis` for internal Chain-of-Thought, `commentary` for tool dispatching, and `final` for spoken user responses) with native function-calling boundary tokens (`<|call|>`, `<|return|>`).
- **Dual Operating Modes:** Run as an interactive conversational CLI agent directly in your terminal, or launch as a high-performance background HTTP server (`--server` / `-s`).
- **Full OpenAI API Compatibility:**
  - `POST /v1/chat/completions`: Full SSE streaming (`text/event-stream`) with sentence-boundary flushing, keep-alive heartbeats, and non-streaming fallback.
  - `POST /v1/audio/speech`: OpenAI-compatible TTS endpoint synthesizing speech on the fly via local Piper ONNX voices.
  - `GET /v1/models`: Exposes dedicated model IDs (`mp-harmony-agent` and underlying Ollama tags) for automatic discovery in **Open WebUI**.
  - `POST /v1/abort`: Barge-in / interruption endpoint to instantly cancel active generation turns upon user voice speech.
- **Web HUD Diagnostic Dashboard:** Embedded web dashboard running on `http://127.0.0.1:11435/` (`/hud`, `/dashboard`) featuring real-time hardware telemetry, DJ Mix Archive status, an interactive chat sandbox with audio playback, and an audio speech history player.
- **Multi-Session Isolation:** Full conversation isolation via `X-Session-Id` and `X-Conversation-Id` HTTP headers with multi-turn history reconciliation and automatic 24-hour LRU session garbage collection.
- **26 Specialized System Tools:** Comprehensive tool suite covering code inspection, patch application, background job supervision, hardware telemetry, git safety checkpoints, persistent agent memories, live web search, and DJ mix studio operations.
- **DJ Mix Studio Intelligence:** Deep search across mix archives (`.flac`, `.wav`, `.mp3`, `.m4a`), tracklist parsing from companion `.txt`, cue sheets (`.cue`), and Traktor `.nml` files, with playback control via MP Audio Player (`MP_Audio_Player.py`) or system media players.
- **Speech Synthesis & Automatic Archiving:** Background asynchronous queue (`_TTS_QUEUE`) synthesizes every response to `.wav` and companion `.txt` transcript in `audio_responses/`, with in-memory phrase LRU caching and desktop notifications (`notify-send`).
- **Hardware Telemetry & Volume Control:** Live monitoring of CPU load averages, RAM/swap utilization, NVIDIA GPU thermals/VRAM, and PipeWire / WirePlumber audio volume management (`wpctl`).
- **Resilient Context Management:** Sliding-window context pruner using native Harmony token encoding keeps active conversation below configured thresholds while pinning system instructions and developer tool schemas.
- **Safe Mode & Anti-Loop Guardrails:** Built-in pattern guardrails block destructive commands (`rm -rf`, `mkfs`, `dd if=`, `shutdown`, `reboot`, `git reset --hard`). A sliding-window circuit breaker stops degenerative model token loops.

---

## Tool Engine

The agent registers 26 native tools exposed to the Harmony engine:

| Tool | Category | Purpose & Behavior | Safeguards |
| :--- | :--- | :--- | :--- |
| `read_file` | Filesystem | Reads whole file content or line range | 64 KB limit for whole file |
| `read_file_lines` | Filesystem | Reads line slice with 1-based line numbers | Capped at 250 lines per call |
| `search_file_regex` | Filesystem | Case-insensitive regex scan across files | Max result caps, line numbers |
| `write_file` | Filesystem | Writes or overwrites scripts/files on disk | Creates `.bak` backup; auto `chmod +x` on `.sh` |
| `patch_file` | Filesystem | Performs exact string replacement | Creates `.bak` backup; verifies presence |
| `patch_file_diff` | Filesystem | Applies standard unified diff patches | Uses `patch -u` with pure Python fallback; `.bak` |
| `list_directory` | Filesystem | Lists entries with types, sizes (KB), and timestamps | Capped results; filters hidden files |
| `run_shell_command` | Process | Executes short Bash commands in foreground | 30s timeout; blocked by `SAFE_MODE` |
| `start_background_task`| Process | Spawns long-running detached background jobs | Returns `job_id`, tracks PID and logs |
| `check_background_task`| Process | Inspects status and log tail of background tasks | Tails recent lines; verifies active PID |
| `stop_background_task` | Process | Terminates running background job process group | Sends SIGTERM to PID process group |
| `get_system_telemetry` | Telemetry | Reads CPU load, host RAM, and NVIDIA GPU metrics | Graceful fallbacks if GPU is absent |
| `system_audio_volume` | Telemetry | Inspects/controls host volume via PipeWire (`wpctl`) | Supports `status`, `set` (0-150%), `toggle_mute` |
| `git_checkpoint` | Git | Creates safety commit tag or stash in a repository | Auto-tags with timestamp checkpoint |
| `git_rollback` | Git | Reverts repository changes back to `HEAD~1` | Hard reset rollback to previous commit |
| `set_agent_memory` | Memory | Saves persistent key-value configuration facts | Persisted to `~/.harmony_memory.json` |
| `get_agent_memory` | Memory | Retrieves a saved persistent memory by key | Returns saved value or null |
| `list_agent_memories` | Memory | Lists all stored persistent configuration facts | JSON map of all stored keys |
| `search_agent_memories`| Memory | Multi-term keyword overlap search with ranking | Relevance scoring and token overlap |
| `web_search` | Research | Live DuckDuckGo search without API keys | Clean snippets and source URLs |
| `search_mix_archive` | DJ Studio | Recursively discovers mix audio, tracklists, spectrograms | Searches across all configured archive roots |
| `get_mix_tracklist` | DJ Studio | Parses tracklists from companion `.txt`, `.cue`, or `.nml` | Extracts track index, timestamp, artist, title |
| `find_track_in_mixes` | DJ Studio | Searches all tracklists/cues for specific tracks or artists | Deep search across text, cue, and Traktor logs |
| `play_mix_or_audio` | DJ Studio | Controls playback (`play`, `pause`, `toggle`, `stop`, `latest`) | Integrates with `MP_Audio_Player.py` / system audio |
| `get_mix_archive_stats`| DJ Studio | Returns total mixes, storage (GB), formats, and recent mixes | Aggregates storage across multiple storage roots |
| `save_speech_response`| Voice | Synthesizes speech to `.wav` and companion `.txt` transcript | Background queue, cache, desktop notification |

---

## Prerequisites

1. **Python 3.10+**
2. **Ollama** running locally on port `11434` with an OpenAI Harmony-compatible model:
   ```bash
   ollama list
   # Recommended: gpt-oss-pinned:latest or any Harmony-trained model
   ```
3. **Python Dependencies:**
   ```bash
   pip install openai-harmony httpx pydantic ddgs
   ```
4. **Optional System Tools & Audio Engines:**
   - **Piper TTS**: Local neural speech synthesis (`piper-tts` or `piper` binary and ONNX voice models in `~/voice-ai/voices/` or `~/.local/share/piper-voices/`).
   - **PipeWire / WirePlumber**: `wpctl` for hardware volume control.
   - **Desktop Notifications**: `notify-send` for speech response alerts.
   - **Unified Diff Utility**: `patch` command (pure Python fallback is built-in).
   - **Voice Talk**: `local-voice-talk` for hands-free conversational voice loops.

---

## Usage

### 1. Interactive CLI Mode

Run the agent directly in your terminal:

```bash
# Uses default / auto-discovered model from Ollama
python3 MP_Harmony_Agent.py

# Or specify a target model tag explicitly
python3 MP_Harmony_Agent.py gpt-oss-pinned:latest
```

**Commands during interactive chat:**
- `/reset` or `clear`: Wipes conversational history back to baseline.
- `exit` or `quit`: Exits the agent session.

**Example prompts:**
```text
You > What is the current system audio volume? Check using your tool.
You > Find all DJ mixes that include tracks by "Above & Beyond" across the archive.
You > Read lines 40 to 120 of Mix_Archive_Manager.sh and check for syntax issues.
You > Search the web for PipeWire wireplumber lua scripts examples.
```

---

### 2. Server & Voice Bridge Mode

Start `MP_Harmony_Agent.py` as an HTTP server:

```bash
# Start server with auto-detected model
python3 MP_Harmony_Agent.py --server

# Or specify model tag
python3 MP_Harmony_Agent.py --server gpt-oss-pinned:latest
```

The server binds to `127.0.0.1:11435` by default and exposes:
- **Web HUD Dashboard**: `http://127.0.0.1:11435/`
- **OpenAI Chat Completions**: `http://127.0.0.1:11435/v1/chat/completions`
- **OpenAI Speech API**: `http://127.0.0.1:11435/v1/audio/speech`
- **Model Discovery**: `http://127.0.0.1:11435/v1/models`
- **Barge-In Abort**: `http://127.0.0.1:11435/v1/abort`
- **Health / Status**: `http://127.0.0.1:11435/health`

---

### 3. Open WebUI Integration

You can add MP Harmony Agent as an OpenAI connection in **Open WebUI**:

1. In Open WebUI, navigate to **Admin Panel > Settings > Connections**.
2. Under **OpenAI API**, add a new connection:
   - **URL**: `http://127.0.0.1:11435/v1`
   - **API Key**: `none` (or any string)
3. Save and refresh models. Open WebUI will discover:
   - `mp-harmony-agent` (MP Harmony AI Agent)
   - Underlying Ollama model tag (e.g., `gpt-oss-pinned:latest`)
4. Select `MP Harmony AI Agent` in your chat window. All tool executions, telemetry queries, and script modifications will execute directly on the host machine.

---

### 4. Hands-Free Voice Control (`local-voice-talk`)

Connect `local-voice-talk` directly to the agent's SSE streaming endpoint:

```bash
OLLAMA_URL="http://127.0.0.1:11435" local-voice-talk --model mp-harmony-agent
```

**Voice Workflow Architecture:**

```text
Microphone (Spoken Input)
   │
   ▼
[local-voice-talk / Whisper]
   │
   ▼ POST /v1/chat/completions (Port 11435)
[MP_Harmony_Agent Server] ──────────────► [Ollama Raw Engine (Port 11434)]
   │                                              │
   │◄─── Executes Local / System Tools ───────────┘
   │     (search_mix_archive, patch_file, telemetry, etc.)
   │
   ▼ Sentence-Buffered SSE Stream (msg.channel == "final")
[local-voice-talk / Piper TTS]
   │
   ▼
Speakers / Audio Output (Immediate playback per sentence)
```

- **Voice Session Reset:** Say *"reset session"*, *"clear session"*, or *"reset context"* into the microphone to reset conversation memory.
- **Voice Interruption:** If you speak while the agent is answering, the voice frontend issues `POST /v1/abort` to immediately halt token generation.
- **Audio Cleanliness:** Raw Chain-of-Thought (`analysis`) and tool payloads are displayed in the server terminal log and suppressed from the TTS audio stream.

---

## Configuration & Environment Variables

Tune agent behavior using environment variables:

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `HARMONY_AGENT_PORT` | `11435` | HTTP server listening port |
| `HARMONY_MODEL` | Auto-detected | Ollama model tag (overrides auto-detection) |
| `HARMONY_NUM_CTX` | `4096` | Context window token ceiling |
| `HARMONY_NUM_PREDICT` | `2048` | Maximum tokens generated per step |
| `HARMONY_PRUNE_THRESHOLD` | `3200` | Token limit triggering FIFO sliding-window pruning |
| `HARMONY_MAX_STEPS` | `16` | Maximum autonomous tool execution steps per user turn |
| `HARMONY_AUDIO_DIR` | `./audio_responses` | Destination directory for archived speech `.wav` and `.txt` |
| `HARMONY_AUTO_ARCHIVE_AUDIO` | `1` | Enable/disable automatic background TTS archiving (`1` or `0`) |
| `HARMONY_PIPER_VOICE` | Auto-detected | Explicit path to a Piper `.onnx` voice model |

---

## Architecture & Safety Guardrails

- **Enforced Action over Chat:** Developer system instructions guide the model to execute modifications directly on disk rather than presenting raw code blocks for manual copying.
- **Automatic File Backups:** File-writing tools (`write_file`, `patch_file`, `patch_file_diff`) create timestamped `.bak` files before altering files on disk.
- **Safe Mode Inspection:** Intercepts shell commands containing destructive patterns (`rm -rf`, `mkfs`, `dd if=`, `shutdown`, `reboot`, `git reset --hard`).
- **Connection Pooling:** Uses persistent `httpx.Client` connection pools with keep-alive limits to communicate with Ollama with minimal latency overhead.
- **Circuit Breaker:** Sliding-window n-gram analyzer detects repetitive looping output and breaks generation early.