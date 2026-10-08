# MP Harmony Agent (`MP_Harmony_Agent.py`)

A local autonomous system administration, scripting, and research agent powered by the **OpenAI Harmony** specification and **Ollama**. `HA.py` bridges low-level OS management (file inspection, code patching, regex search, live web search, and shell execution) with hands-free voice control via `local-voice-talk`.

---

## Key Features

- **Harmony Protocol Native:** Direct interaction with raw Harmony token channels (`analysis`, `commentary`, `final`) and function-calling boundary tokens (`<|call|>`, `<|return|>`).
- **Live Web Search:** Built-in `web_search` tool powered by DuckDuckGo (`ddgs`) requires zero API keys and returns real-time documentation, release details, and troubleshooting context.
- **Low-Latency Voice Streaming:** Buffers and flushes assistant responses at natural sentence boundaries (`. `, `! `, `? `, `\n`) across an SSE `/v1/chat/completions` stream. Piper / Kokoro begins speaking in sub-800ms rather than waiting for multi-paragraph generation to finish.
- **Sliding-Window Context Pruning:** Automatically counts tokens via native Harmony encodings and prunes older conversational turns once history exceeds 6,000 tokens, keeping system instructions and developer tool definitions permanently pinned at indices 0 and 1.
- **Safe Mode Guardrails:** Built-in pattern inspection blocks destructive operations (`rm -rf`, `mkfs`, `dd if=`, `shutdown`, `reboot`, `git reset --hard`) from executing through voice or shell tools.
- **Voice & CLI Session Reset:** Say *"clear session"*, *"reset context"*, or type `/reset` to purge conversational history back to baseline without terminating the server.
- **Large Script Navigation:** Employs windowed reads (`read_file_lines`) and targeted regex search (`search_file_regex`) to inspect multi-thousand-line scripts without blowing out context.

---

## Tool Engine

| Tool | Purpose | Safeguards & Behavior |
| :--- | :--- | :--- |
| `web_search` | Searches the live web for syntax, documentation, releases, and facts. | Zero API keys required; returns clean snippets and URLs. |
| `search_file_regex` | Locates subroutines, variables, and keywords in massive files. | Returns line numbers and 1-line context snippets; capped results. |
| `read_file_lines` | Reads targeted line ranges with 1-based line numbers. | Capped at 250 lines per step; protects context limits. |
| `read_file` | Reads entire file contents for small files. | Enforces a 64 KB limit to prevent context exhaustion. |
| `write_file` | Writes or overwrites scripts/files on disk. | Creates `.bak` backup first; auto-applies `chmod +x` on `.sh`. |
| `patch_file` | Replaces exact code snippets with updated logic. | Backs up to `.bak` first; requires exact string matching. |
| `run_shell_command` | Executes arbitrary Bash commands. | 30s timeout; intercepted by `SAFE_MODE` for destructive patterns. |

---

## Prerequisites

1. **Python 3.10+**
2. **Ollama** running locally on port `11434` with a Harmony-compatible model:
   ```bash
   ollama list
   # e.g., gpt-oss-pinned:latest

   Python Dependencies:

Bash
pip install openai-harmony httpx pydantic ddgs
(Optional) Voice Pipeline:
local-voice-talk with faster-whisper and TTS (Piper/Kokoro) installed.

Usage
1. Interactive CLI Mode
Run the agent in terminal mode:

Bash
# Uses default / auto-discovered model tag
python HA.py

# Or specify a target model tag explicitly
python HA.py gpt-oss-pinned:latest
Useful commands during chat:

/reset or clear: Wipes conversational history back to baseline.

exit or quit: Closes the session.

Example prompts:

Plaintext
You > Search the web for the latest kernel parameters for VFIO passthrough on Fedora.
You > Use search_file_regex to find all functions matching 'convert_' in ~/MP_Mix_Manager_v0.3/Mix_Archive_Manager.sh
2. Voice Bridge Server Mode
To enable hands-free voice interaction:

Terminal 1 — Start the Agent Server
Bash
python HA.py --server gpt-oss-pinned:latest
Listens at http://127.0.0.1:11435/v1/chat/completions with sentence-buffered streaming.

Terminal 2 — Start the Voice Frontend
Direct local-voice-talk to the agent bridge:

Bash
OLLAMA_URL="[http://127.0.0.1:11435](http://127.0.0.1:11435)" local-voice-talk --model gpt-oss-pinned:latest
Voice Commands & Architecture
Plaintext
Microphone (Spoken Input)
   │
   ▼
[local-voice-talk / Whisper]
   │
   ▼ POST /v1/chat/completions (Port 11435)
[HA.py Agent Server] ───────────────► [Ollama Raw Engine (Port 11434)]
   │                                       │
   │◄─── Executes Local / Web Tools ───────┘
   │     (web_search, search_file_regex, read_file_lines, patch_file, etc.)
   │
   ▼ Sentence-Buffered SSE Stream (msg.channel == "final")
[local-voice-talk / Piper TTS]
   │
   ▼
Speakers / Audio Output (Immediate playback per sentence)
Voice Session Reset: Say "reset session", "clear session", or "reset context" into the microphone to clear conversational history without restarting either process.

Audio Cleanliness: Internal chain-of-thought (<|channel|>analysis) and tool call payloads are printed to the terminal console and completely suppressed from the TTS audio output.

Configuration & Tuning
Key runtime constants are located at the top of HA.py:

Python
AGENT_PORT = 11435      # Voice bridge HTTP port
NUM_CTX = 8192          # Agent context limit
NUM_PREDICT = 4096      # Token generation ceiling
PRUNE_THRESHOLD = 6000  # Conversation token threshold before FIFO pruning
SAFE_MODE = True        # Destructive bash pattern guardrails

RUN COMMANDS:

RUN COMMANDS:
mplanetarian@X299ROG:~/OpenAI/MP_Harmony_Agent$ python ~/OpenAI/MP_Harmony_Agent/MP_Harmony_Agent.py --server gpt-oss-pinned:latest
======================================================================
   OpenAI Harmony Advanced Autonomous Engineering Agent
======================================================================
 Model Tag         : gpt-oss-pinned:latest
 Architecture      : GPTOSS | 20.9B | unknown
 Native Context    : 131,072 tokens
 Tools Active      : 16 registered (diff, telemetry, jobs, memory, git, web)
 Guardrails        : safe_mode=ON | auto-pruner=6000 tok
----------------------------------------------------------------------
[Voice Bridge Active] Listening at http://127.0.0.1:11435/v1/chat/completions
[Abort Endpoint] POST http://127.0.0.1:11435/v1/abort
======================================================================

mplanetarian@X299ROG:/var/home/mplanetarian/voice-ai$ OLLAMA_URL="http://127.0.0.1:11435" local-voice-talk --model gpt-oss-pinned:latest
╭─────────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│ Local Voice AI Assistant                                                                                    │
│ Ollama Endpoint: http://127.0.0.1:11435                                                                     │
│ LLM Model: gpt-oss-pinned:latest                                                                            │
│ Whisper STT: base.en (cuda/float16)                                                                         │
│ Voice TTS: en_GB-jenny_dioco-medium (Mode: piper, Speed: 1.0x)                                              │
│ Speech Output: ON                                                                                           │
│ Controls: Press  to record / stop | Type text directly                                                      │
│ Commands: /voice, /speed, /model, /tts, /clear, /quit                                                       │
╰─────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
Loading Whisper STT model...
✓ Whisper STT initialized on GPU

Voice conversation ready!

[Press Enter to Speak | Type text | /quit]:
