# MP Harmony Agent v1.0.0 by Dreamworlds Productions (MPlanetarian)

> 📅 **Last Updated:** October 9, 2026  
> 🏷️ **Version:** v1.0.0  
> 👤 **Author:** Dreamworlds Productions (MPlanetarian)

[![Version](https://img.shields.io/badge/version-v1.0.0-blue.svg)](#)
[![Author](https://img.shields.io/badge/author-Dreamworlds%20Productions%20(MPlanetarian)-purple.svg)](#)
[![Last Updated](https://img.shields.io/badge/last%20updated-October%209%2C%202026-brightgreen.svg)](#)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![OpenAI Harmony](https://img.shields.io/badge/protocol-OpenAI%20Harmony-orange.svg)](https://github.com/openai/harmony)
[![Ollama](https://img.shields.io/badge/backend-Ollama%20Raw-black.svg)](https://ollama.com/)
[![Open WebUI Compatible](https://img.shields.io/badge/Open%20WebUI-Compatible-emerald.svg)](https://openwebui.com/)
[![TTS Engine](https://img.shields.io/badge/TTS-Piper%20ONNX-purple.svg)](https://github.com/rhasspy/piper)
[![Audio Subsystem](https://img.shields.io/badge/audio-PipeWire%20%2F%20WirePlumber-cyan.svg)](https://pipewire.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](#license)

A local autonomous system administration, scripting, DJ Mix Studio, and research agent powered by the **OpenAI Harmony** specification and **Ollama**. `MP_Harmony_Agent.py` bridges low-level host OS management (file inspection, unified diff patching, regex search, live web search, background job supervision, and shell execution) with DJ mix library intelligence, hands-free voice control via `local-voice-talk`, and full **OpenAI API** compatibility for frontends such as **Open WebUI**.

---

## 🚀 Highlighted Features & What's New

### 🎛️ Embedded Web HUD Dashboard (`/hud`)
- **Live Hardware Telemetry:** Real-time host load averages (1, 5, 15m), memory utilization (`free -h`), NVIDIA GPU stats (model, utilization, VRAM usage, temperature), and PipeWire sink volume.
- **DJ Mix Archive Status:** Real-time storage stats across all configured host drive roots, audio format counts, and newest recorded mixes.
- **Interactive Chat & Voice Sandbox:** Web console featuring SSE streaming toggle, prompt suggestion chips, and an in-browser **🔊 Speak** button powered by the speech API.
- **Audio Archive Player:** Direct in-browser playback of archived assistant speech files alongside full transcripts.
- Accessible directly at `http://127.0.0.1:11435/` or `http://127.0.0.1:11435/hud`.

### 🌐 Universal OpenAI API & Open WebUI Compatibility
- **Drop-in OpenAI Endpoint:** Point any standard OpenAI client or **Open WebUI** to `http://127.0.0.1:11435/v1`.
- **Model Discovery (`GET /v1/models`):** Automatically advertises `mp-harmony-agent` (MP Harmony AI Agent) and the underlying Ollama model tag for automatic dropdown selection in Open WebUI.
- **Streaming & SSE Keep-Alive:** Streams responses chunk-by-chunk with periodic `: keep-alive\n\n` comments to prevent client timeouts during multi-step tool execution.
- **OpenAI Speech API (`POST /v1/audio/speech`):** Standard OpenAI speech endpoint generating `audio/wav` on the fly using local Piper ONNX voices with custom speed and voice selection.
- **Instant Barge-In Interruption (`POST /v1/abort`):** Immediately stops active LLM generation when the user interrupts or speaks.

### 🎧 DJ Mix Studio Intelligence & Playback Control
- **Multi-Root Archive Scanner:** Automatically searches mixes across all configured disks (internal drives, external drives, cloud sync).
- **Deep Tracklist & Cue Sheet Parsing:** Extracts timestamped tracks, artist names, and song titles from companion `.txt`, cue sheets (`.cue`), and Traktor `.nml` collection files.
- **Cross-Archive Track Finder:** Search your entire historical library of mixes for specific song titles, remixes, or artists.
- **Playback Control:** Direct IPC control (`play`, `pause`, `toggle`, `stop`, `latest`) via `MP_Audio_Player.py` with fallback to `pw-play`, `paplay`, or `mpv`.

### 🎙️ Automatic Audio Speech Archiving & Desktop Alerts
- **Background Serialized Synthesis:** A dedicated worker thread (`_TTS_QUEUE`) renders assistant speech responses into `.wav` audio files without stalling the agent loop.
- **Transcripts & Archiving:** Saves timestamped `.wav` files and matched `.txt` transcripts in `audio_responses/`.
- **Desktop & Console Notifications:** Dispatches desktop alerts via `notify-send` with high-visibility console summaries upon file completion.
- **In-Memory Phrase Caching:** LRU cache for short spoken sentences for instant replay and reduced CPU/GPU synthesis load.

### 🛡️ 26 Native System Tools & Pure Python Resilience
- **Unified Diff Engine:** Built-in Python diff applier fallback (`apply_unified_diff`) if the host system lacks the GNU `patch` binary.
- **Background Job Manager:** Spawns, tracks, monitors logs (`tail`), and cleanly terminates (`SIGTERM` process group) detached background tasks via tracking `job_id`s.
- **Persistent Key-Value Agent Memory:** Store, query, dump, and multi-term search persistent system facts saved to `~/.harmony_memory.json`.
- **Git Safety Checkpoints:** Automatically create tagged commit checkpoints (`git_checkpoint`) or hard revert changes (`git_rollback`).
- **Live Web Search:** Zero-API-key DuckDuckGo search returning snippets and source URLs.

### ⚡ Performance & Connection Pooling
- **Ollama Connection Pooling:** Persistent `httpx.Client` pool with HTTP keep-alive limits to minimize connection setup latency.
- **Anti-Repetition Circuit Breaker:** Detects degenerate cyclic n-gram repetition loops and terminates the turn early.
- **Sliding-Window Context Pruning:** FIFO token pruning with permanently pinned system and developer prompts to keep inference within configured token ceilings.
- **Multi-Session Isolation:** Sessions tracked via `X-Session-Id` with automatic 24-hour LRU garbage collection.

---

## 🛠️ Complete Tool Engine (26 Registered Tools)

| Tool | Category | Description & Behavior | Safeguards |
| :--- | :--- | :--- | :--- |
| `read_file` | Filesystem | Reads whole file content (<64KB) or targeted line range | 64 KB limit for whole file |
| `read_file_lines` | Filesystem | Reads targeted line range with 1-based line numbers | Capped at 250 lines per call |
| `search_file_regex` | Filesystem | Case-insensitive regex scan across massive files | Max result caps, line numbers |
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

## 🖥️ Web HUD Dashboard

The Web HUD runs automatically alongside the server at `http://127.0.0.1:11435/`:

```text
┌────────────────────────────────────────────────────────────────────────┐
│  MP Harmony AI Agent • Engineering & Music Hub            [● ONLINE]  │
├───────────────────────────────────┬────────────────────────────────────┤
│ HOST & HARDWARE TELEMETRY         │ DJ MIX ARCHIVE STATUS              │
│ Load Avg: 0.42, 0.58, 0.65        │ Archive Roots: 4 configured roots  │
│ Memory: 18.2G / 64G               │ Total Files: 1,420 audio mixes     │
│ GPU: RTX 3090 (42°C, 12% Util)    │ Total Storage: 840 GB              │
│ VRAM: 4,120 MB / 24,576 MB        │ Formats: FLAC: 980 | MP3: 440      │
├───────────────────────────────────┴────────────────────────────────────┤
│ INTERACTIVE CHAT & VOICE SANDBOX  │ AUDIO SPEECH ARCHIVE               │
│ [ Chat Output Stream & Tools ]    │ [ agent_speech_20261008.wav ] ▶    │
│ [ Prompt Input Area          ]    │ Transcript: "Found 12 mixes..."    │
│ [ Stream: ✓ ] [ 🔊 Speak ]        │ [ agent_speech_20261007.wav ] ▶    │
└───────────────────────────────────┴────────────────────────────────────┘
```

---

## 📋 Prerequisites

1. **Python 3.10+**
2. **Ollama** running locally on port `11434` with an OpenAI Harmony-compatible model:
   ```bash
   ollama list
   # e.g., gpt-oss-pinned:latest
   ```
3. **Python Dependencies:**
   ```bash
   pip install openai-harmony httpx pydantic ddgs
   ```
4. **Recommended System Packages:**
   - **Piper TTS**: Local neural speech synthesis (`piper` binary or `piper-tts` package with `.onnx` models).
   - **PipeWire / WirePlumber**: `wpctl` for hardware volume inspection and setting.
   - **Desktop Notifications**: `notify-send` for speech response alerts.
   - **GNU Patch**: `patch` (pure Python fallback is built-in).

---

## 🚦 Usage & Operating Modes

### 1. Interactive Terminal Mode
Run directly in your shell for local system scripting and administration:

```bash
# Auto-detects model from Ollama
python3 MP_Harmony_Agent.py

# Or specify a target model tag explicitly
python3 MP_Harmony_Agent.py gpt-oss-pinned:latest
```

**Commands during interactive chat:**
- `/reset` or `clear`: Wipes conversational history back to baseline.
- `exit` or `quit`: Closes the session.

---

### 2. Server & Voice Bridge Mode
Launch the OpenAI-compatible HTTP server:

```bash
# Start server on default port 11435
python3 MP_Harmony_Agent.py --server

# Or specify target model tag
python3 MP_Harmony_Agent.py --server gpt-oss-pinned:latest
```

The server exposes:
- **Web HUD Dashboard**: `http://127.0.0.1:11435/`
- **OpenAI Chat Completions**: `POST http://127.0.0.1:11435/v1/chat/completions`
- **OpenAI Speech API**: `POST http://127.0.0.1:11435/v1/audio/speech`
- **Model Discovery**: `GET http://127.0.0.1:11435/v1/models`
- **Barge-In Abort**: `POST http://127.0.0.1:11435/v1/abort`
- **Telemetry JSON**: `GET http://127.0.0.1:11435/api/telemetry`
- **Mix Stats JSON**: `GET http://127.0.0.1:11435/api/mix_stats`
- **Audio Serving**: `GET http://127.0.0.1:11435/audio/<filename>`

---

### 3. Open WebUI Integration

You can integrate MP Harmony Agent as a backend provider in **Open WebUI**:

1. In Open WebUI, navigate to **Admin Panel > Settings > Connections**.
2. Under **OpenAI API**, add a new connection:
   - **URL**: `http://127.0.0.1:11435/v1`
   - **API Key**: `none` (or any string)
3. Click Save and refresh models. Open WebUI will discover:
   - `mp-harmony-agent` (MP Harmony AI Agent)
   - Underlying Ollama model tag (e.g. `gpt-oss-pinned:latest`)
4. Open a chat, choose **MP Harmony AI Agent**, and interact directly with your host system!

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
Speakers / Audio Output (Immediate sentence-by-sentence playback)
```

- **Voice Session Reset:** Say *"reset session"*, *"clear session"*, or *"reset context"* into your microphone.
- **Voice Barge-In / Interruption:** Speaking while the agent generates automatically triggers `POST /v1/abort`, interrupting inference immediately.

---

## 📺 Live Console Session

```text
$ python ~/OpenAI/MP_Harmony_Agent/MP_Harmony_Agent.py --server gpt-oss-pinned:latest
======================================================================
   OpenAI Harmony Advanced Autonomous Engineering Agent
======================================================================
 Model Tag         : gpt-oss-pinned:latest
 Architecture      : GPTOSS | 20.9B | Q4_K_M
 Native Context    : 131,072 tokens
 Tools Active      : 26 registered (diff, telemetry, jobs, memory, git, web, audio)
 Audio Archive     : /var/home/mplanetarian/OpenAI/MP_Harmony_Agent/audio_responses (auto-archive: ON)
 Guardrails        : safe_mode=ON | auto-pruner=3200 tok
----------------------------------------------------------------------
[Voice Bridge Active] Listening at http://127.0.0.1:11435/v1/chat/completions
[Abort Endpoint] POST http://127.0.0.1:11435/v1/abort
======================================================================

$ OLLAMA_URL="http://127.0.0.1:11435" local-voice-talk --model mp-harmony-agent
╭─────────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│ Local Voice AI Assistant                                                                                    │
│ Ollama Endpoint: http://127.0.0.1:11435                                                                     │
│ LLM Model: mp-harmony-agent                                                                                 │
│ Whisper STT: base.en (cuda/float16)                                                                         │
│ Voice TTS: en_GB-jenny_dioco-medium (Mode: piper, Speed: 1.0x)                                              │
│ Speech Output: ON                                                                                           │
│ Controls: Press <Space> to record / stop | Type text directly                                               │
│ Commands: /voice, /speed, /model, /tts, /clear, /quit                                                       │
╰─────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
Loading Whisper STT model...
✓ Whisper STT initialized on GPU

Voice conversation ready!

[Press Enter to Speak | Type text | /quit]:
```

---

## ⚙️ Configuration & Environment Variables

Tune runtime settings using environment variables:

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
| `HARMONY_PIPER_VOICE` | Auto-detected | Path to a custom Piper `.onnx` voice model |

---

## 🛡️ Architecture & Safety Guardrails

- **Enforced Action over Chat:** Developer instructions direct the model to apply changes directly via tools instead of outputting raw Markdown code blocks for manual copying.
- **Automatic File Backups:** File-writing tools (`write_file`, `patch_file`, `patch_file_diff`) create timestamped `.bak` files before altering files on disk.
- **Safe Mode Inspection:** Blocks shell commands matching dangerous patterns (`rm -rf`, `mkfs`, `dd if=`, `shutdown`, `reboot`, `git reset --hard`).
- **Connection Pooling:** Uses persistent `httpx.Client` connection pools with keep-alive limits to communicate with Ollama with minimal latency overhead.
- **Sliding-Window Circuit Breaker:** Stops cyclic n-gram token repetition loops.

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
