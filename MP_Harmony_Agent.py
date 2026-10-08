#!/usr/bin/env python3
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
import readline
import threading
import inspect
import wave
import httpx
from http.server import HTTPServer, BaseHTTPRequestHandler
from openai_harmony import (
    load_harmony_encoding,
    HarmonyEncodingName,
    Role,
    Author,
    Message,
    Conversation,
    DeveloperContent,
    SystemContent,
    ToolDescription,
)

# ---------------------------------------------------------------------------
# Global Settings & Configuration
# ---------------------------------------------------------------------------
AGENT_PORT = 11435
NUM_CTX = 8192
NUM_PREDICT = 4096
PRUNE_THRESHOLD = 6000
MAX_STEPS = 16
SAFE_MODE = True
MEMORY_FILE = os.path.expanduser("~/.harmony_memory.json")
JOBS_DIR = "/tmp/ha_jobs"
os.makedirs(JOBS_DIR, exist_ok=True)
AUDIO_OUTPUT_DIR = os.path.expanduser(
    os.getenv("HARMONY_AUDIO_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio_responses"))
)
AUDIO_AUTO_ARCHIVE = os.getenv("HARMONY_AUTO_ARCHIVE_AUDIO", "1").lower() not in ("0", "false", "no")
os.makedirs(AUDIO_OUTPUT_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# 1. Local Tools Implementation
# ---------------------------------------------------------------------------
def read_file(filepath: str, start_line: int = None, end_line: int = None, **kwargs) -> dict:
    """Reads the entire content of a file (<64KB) or a targeted slice if start_line/end_line are specified."""
    if start_line is not None or end_line is not None:
        s = 1 if start_line is None else int(start_line)
        e = (s + 200) if end_line is None else int(end_line)
        return read_file_lines(filepath, start_line=s, end_line=e)
    try:
        path = os.path.expanduser(filepath)
        if not os.path.exists(path):
            return {"error": f"File does not exist: {path}"}
        if os.path.getsize(path) > 65536:
            return {"error": "File exceeds 64KB. Use 'read_file_lines' or 'search_file_regex'."}
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return {"filepath": path, "content": f.read()}
    except Exception as e:
        return {"error": str(e)}


def read_file_lines(filepath: str, start_line: int = 1, end_line: int = 200, **kwargs) -> dict:
    """Reads a targeted slice of lines with line numbers (max 250 lines)."""
    try:
        path = os.path.expanduser(filepath)
        if not os.path.exists(path):
            return {"error": f"File does not exist: {path}"}
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        total = len(lines)
        start = max(1, int(start_line))
        end = min(total, min(int(end_line), start + 249))
        if start > total:
            return {"error": f"start_line ({start}) exceeds total line count ({total})."}
        numbered = [f"{i}: {line}" for i, line in enumerate(lines[start - 1 : end], start=start)]
        return {"filepath": path, "total_lines": total, "range": f"{start}-{end}", "content": "".join(numbered)}
    except Exception as e:
        return {"error": str(e)}


def search_file_regex(filepath: str, pattern: str, max_results: int = 15, **kwargs) -> dict:
    """Searches a file for regex matches returning line numbers and context."""
    try:
        path = os.path.expanduser(filepath)
        if not os.path.exists(path):
            return {"error": f"File does not exist: {path}"}
        regex = re.compile(pattern, re.IGNORECASE)
        matches = []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for idx, line in enumerate(f, start=1):
                if regex.search(line):
                    matches.append({"line": idx, "content": line.strip()})
                    if len(matches) >= int(max_results):
                        break
        return {"filepath": path, "matches": matches, "count": len(matches)}
    except Exception as e:
        return {"error": str(e)}


def write_file(filepath: str, content: str, **kwargs) -> dict:
    """Overwrites or creates a file with automatic .bak backup."""
    try:
        path = os.path.expanduser(filepath)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        backup = None
        if os.path.exists(path):
            backup = f"{path}.bak"
            shutil.copy2(path, backup)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        if path.endswith(".sh"):
            os.chmod(path, 0o755)
        res = {"filepath": path, "status": "written successfully", "bytes": len(content)}
        if backup:
            res["backup"] = backup
        return res
    except Exception as e:
        return {"error": str(e)}


def patch_file(filepath: str, old_str: str, new_str: str, **kwargs) -> dict:
    """Exact string replacement with .bak backup."""
    try:
        path = os.path.expanduser(filepath)
        if not os.path.exists(path):
            return {"error": f"File does not exist: {path}"}
        with open(path, "r", encoding="utf-8") as f:
            orig = f.read()
        if old_str not in orig:
            return {"error": "Exact target string not found in file."}
        backup = f"{path}.bak"
        shutil.copy2(path, backup)
        with open(path, "w", encoding="utf-8") as f:
            f.write(orig.replace(old_str, new_str, 1))
        return {"filepath": path, "status": "patched successfully", "backup": backup}
    except Exception as e:
        return {"error": str(e)}


def apply_unified_diff(orig_content: str, diff_text: str) -> tuple:
    """Applies a unified diff patch to a string in pure Python without external patch utility."""
    lines = orig_content.splitlines(keepends=True)
    diff_lines = diff_text.splitlines(keepends=True)

    hunks = []
    current_hunk = None
    for line in diff_lines:
        if line.startswith("@@"):
            if current_hunk:
                hunks.append(current_hunk)
            current_hunk = [line]
        elif current_hunk is not None:
            current_hunk.append(line)
    if current_hunk:
        hunks.append(current_hunk)

    if not hunks:
        return False, "No unified diff hunks found in patch."

    for hunk in hunks:
        header = hunk[0]
        m = re.match(r"@@\s*-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s*@@", header)
        old_start = int(m.group(1)) - 1 if m else 0

        old_slice = []
        new_slice = []
        for line in hunk[1:]:
            if line.startswith("-"):
                old_slice.append(line[1:])
            elif line.startswith("+"):
                new_slice.append(line[1:])
            elif line.startswith(" ") or line == "\n":
                content = line[1:] if line.startswith(" ") else line
                old_slice.append(content)
                new_slice.append(content)

        window = len(old_slice)
        found_idx = -1
        old_stripped = [l.rstrip("\r\n") for l in old_slice]

        if 0 <= old_start <= len(lines) - window and [l.rstrip("\r\n") for l in lines[old_start:old_start + window]] == old_stripped:
            found_idx = old_start
        else:
            for i in range(len(lines) - window + 1):
                if [l.rstrip("\r\n") for l in lines[i:i + window]] == old_stripped:
                    found_idx = i
                    break

        if found_idx == -1:
            return False, f"Could not match hunk context: {header.strip()}"

        lines[found_idx:found_idx + window] = new_slice

    return True, "".join(lines)


def patch_file_diff(filepath: str, diff_patch: str, **kwargs) -> dict:
    """Applies a standard unified diff patch to a target file via patch or pure Python fallback."""
    try:
        path = os.path.expanduser(filepath)
        if not os.path.exists(path):
            return {"error": f"File does not exist: {path}"}
        backup = f"{path}.bak"
        shutil.copy2(path, backup)

        if shutil.which("patch"):
            proc = subprocess.run(
                ["patch", "-u", path],
                input=diff_patch,
                text=True,
                capture_output=True,
                timeout=15,
            )
            if proc.returncode != 0:
                shutil.copy2(backup, path)
                return {"error": f"Patch failed: {proc.stderr or proc.stdout}. File restored."}
            return {"filepath": path, "status": "unified diff applied via patch", "backup": backup}

        # Fallback to pure Python unified diff applier
        with open(path, "r", encoding="utf-8") as f:
            orig = f.read()
        success, patched_or_err = apply_unified_diff(orig, diff_patch)
        if not success:
            shutil.copy2(backup, path)
            return {"error": f"Unified diff failed ({patched_or_err}). Suggestion: Use 'patch_file' with exact string replacement."}
        with open(path, "w", encoding="utf-8") as f:
            f.write(patched_or_err)
        return {"filepath": path, "status": "unified diff applied via pure python", "backup": backup}
    except Exception as e:
        return {"error": str(e)}


def run_shell_command(command: str, **kwargs) -> dict:
    """Executes a short foreground bash command with safe_mode protection."""
    if SAFE_MODE:
        destructive = [
            r"\brm\s+-[rf]{1,2}\b",
            r"\bmkfs\b",
            r"\bdd\s+if=",
            r"\bshutdown\b",
            r"\breboot\b",
            r"\bgit\s+reset\s+--hard\b",
        ]
        for pat in destructive:
            if re.search(pat, command):
                return {"error": f"Command blocked by safe_mode guardrail: dangerous pattern ('{pat}')."}
    try:
        res = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
        return {"stdout": res.stdout.strip(), "stderr": res.stderr.strip(), "returncode": res.returncode}
    except subprocess.TimeoutExpired:
        return {"error": "Command timed out after 30 seconds. For longer tasks, use 'start_background_task'."}
    except Exception as e:
        return {"error": str(e)}


def start_background_task(command: str, **kwargs) -> dict:
    """Spawns a long-running process in the background and returns a tracking job_id."""
    job_id = f"job_{int(time.time())}_{str(uuid.uuid4())[:4]}"
    log_file = os.path.join(JOBS_DIR, f"{job_id}.log")
    try:
        with open(log_file, "w") as out:
            proc = subprocess.Popen(
                command,
                shell=True,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        return {"job_id": job_id, "pid": proc.pid, "log_file": log_file, "status": "started"}
    except Exception as e:
        return {"error": str(e)}


def check_background_task(job_id: str, tail_lines: int = 20, **kwargs) -> dict:
    """Inspects exit status and output tail of a background task."""
    log_file = os.path.join(JOBS_DIR, f"{job_id}.log")
    if not os.path.exists(log_file):
        return {"error": f"No job found with id '{job_id}'."}
    tail_cmd = f"tail -n {tail_lines} '{log_file}'"
    tail_res = subprocess.run(tail_cmd, shell=True, capture_output=True, text=True)
    return {"job_id": job_id, "recent_logs": tail_res.stdout.strip()}


def get_system_telemetry(**kwargs) -> dict:
    """Collects CPU load, host RAM/swap, and NVIDIA GPU telemetry."""
    telemetry = {}
    try:
        with open("/proc/loadavg", "r") as f:
            telemetry["load_avg_1_5_15m"] = f.read().strip().split()[:3]
        mem = subprocess.run("free -h", shell=True, capture_output=True, text=True)
        telemetry["memory"] = mem.stdout.strip().splitlines()[:2]
    except Exception as e:
        telemetry["cpu_mem_error"] = str(e)

    try:
        nvidia_cmd = "nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu --format=csv,noheader,nounits"
        nv = subprocess.run(nvidia_cmd, shell=True, capture_output=True, text=True)
        if nv.returncode == 0 and nv.stdout.strip():
            fields = [x.strip() for x in nv.stdout.strip().split(",")]
            telemetry["gpu"] = {
                "model": fields[0],
                "utilization": f"{fields[1]}%",
                "vram_used": f"{fields[2]} MB",
                "vram_total": f"{fields[3]} MB",
                "temp": f"{fields[4]} °C",
            }
    except Exception:
        pass
    return telemetry


def git_checkpoint(repo_path: str, message: str, **kwargs) -> dict:
    """Creates a temporary safety commit or stash in a git repository."""
    path = os.path.expanduser(repo_path)
    tag = f"harmony_ckpt_{int(time.time())}"
    cmd = f"cd '{path}' && git add -A && git commit -m 'checkpoint: {message} ({tag})' || git stash create"
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return {"repo": path, "checkpoint_tag": tag, "output": res.stdout.strip() or res.stderr.strip()}


def git_rollback(repo_path: str, **kwargs) -> dict:
    """Reverts changes in a repo back to the previous commit."""
    path = os.path.expanduser(repo_path)
    cmd = f"cd '{path}' && git reset --hard HEAD~1"
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return {"repo": path, "status": "rolled back", "output": res.stdout.strip()}


def set_agent_memory(key: str, value: str, **kwargs) -> dict:
    """Saves a persistent configuration key-value pair to disk."""
    memories = {}
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r") as f:
                memories = json.load(f)
        except Exception:
            pass
    memories[key] = value
    with open(MEMORY_FILE, "w") as f:
        json.dump(memories, f, indent=2)
    return {"status": "saved", "key": key, "value": value}


def get_agent_memory(key: str, **kwargs) -> dict:
    """Retrieves a persistent configuration key-value pair."""
    if not os.path.exists(MEMORY_FILE):
        return {"error": "No memory file initialized."}
    try:
        with open(MEMORY_FILE, "r") as f:
            memories = json.load(f)
        return {"key": key, "value": memories.get(key, None)}
    except Exception as e:
        return {"error": str(e)}


def list_agent_memories(**kwargs) -> dict:
    """Lists all stored persistent configuration facts."""
    if not os.path.exists(MEMORY_FILE):
        return {"memories": {}}
    try:
        with open(MEMORY_FILE, "r") as f:
            return {"memories": json.load(f)}
    except Exception as e:
        return {"error": str(e)}


def web_search(query: str, max_results: int = 5, **kwargs) -> dict:
    """Searches the live web via DuckDuckGo without API keys."""
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            return {"error": "Neither 'ddgs' nor 'duckduckgo_search' installed."}
    try:
        results = []
        with DDGS() as ddgs:
            for item in ddgs.text(query, max_results=int(max_results)):
                results.append({"title": item.get("title", ""), "snippet": item.get("body", "")[:300], "url": item.get("href", "")})
        return {"query": query, "results": results, "count": len(results)}
    except Exception as e:
        return {"error": f"Search failed: {str(e)}"}


def clean_speech_text(text: str) -> str:
    """Sanitizes text for natural-sounding speech synthesis."""
    if not text:
        return ""
    # Strip harmony tokens and thinking tags
    text = re.sub(r"<\|.*?\|>", "", text)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<think>.*", "", text, flags=re.DOTALL)
    # Replace code blocks with brief spoken note
    text = re.sub(r"```[\w]*\n.*?```", " code block omitted ", text, flags=re.DOTALL)
    text = re.sub(r"```.*?```", " code block omitted ", text, flags=re.DOTALL)
    # Remove inline backticks
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # Remove markdown headers and emphasis
    text = re.sub(r"#{1,6}\s*", "", text)
    text = re.sub(r"(\*\*|__)(.*?)\1", r"\2", text)
    text = re.sub(r"(\*|_)(.*?)\1", r"\2", text)
    # Replace markdown links [label](url) with label
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Remove raw URLs
    text = re.sub(r"https?://\S+", "", text)
    # Clean list bullets and numbering
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)
    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


_PIPER_VOICE_LOCK = threading.Lock()
_CACHED_PIPER_VOICE = None


def get_piper_voice_path() -> str | None:
    """Finds available Piper ONNX voice models on the system."""
    env_voice = os.getenv("HARMONY_PIPER_VOICE") or os.getenv("VOICE_AI_TTS_VOICE")
    if env_voice and os.path.exists(os.path.expanduser(env_voice)):
        return os.path.expanduser(env_voice)

    search_dirs = [
        "/var/home/mplanetarian/voice-ai/voices",
        os.path.expanduser("~/voice-ai/voices"),
        os.path.expanduser("~/.local/share/piper-voices"),
        os.path.expanduser("~/MP_REPORTER/share/voices"),
        "/usr/share/piper-voices",
    ]
    pref_names = [
        "en_GB-jenny_dioco-medium.onnx",
        "en_GB-cori-medium.onnx",
        "en_US-lessac-medium.onnx",
        "en_US-amy-medium.onnx",
        "en_GB-alba-medium.onnx",
    ]
    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        if env_voice:
            clean_env = env_voice.lower().strip()
            for f in os.listdir(d):
                if clean_env in f.lower() and f.endswith(".onnx"):
                    return os.path.join(d, f)
        for pref in pref_names:
            p = os.path.join(d, pref)
            if os.path.exists(p):
                return p
        for f in os.listdir(d):
            if f.endswith(".onnx"):
                return os.path.join(d, f)
    return None


def get_cached_piper_voice():
    """Lazily loads and caches the Piper voice model."""
    global _CACHED_PIPER_VOICE
    with _PIPER_VOICE_LOCK:
        if _CACHED_PIPER_VOICE is not None:
            return _CACHED_PIPER_VOICE
        voice_path = get_piper_voice_path()
        if not voice_path:
            return None
        try:
            from piper import PiperVoice
            _CACHED_PIPER_VOICE = PiperVoice.load(voice_path)
            return _CACHED_PIPER_VOICE
        except Exception as e:
            print(f"\033[1;33m[TTS Warning]\033[0m Could not load Piper python model: {e}")
            return None


def notify_audio_saved(audio_path: str, text_path: str):
    """Notifies via terminal console and system notification that speech files have been saved."""
    print(f"\n\033[1;32m[Audio Speech Saved]\033[0m {audio_path}")
    print(f"\033[1;36m[Text Transcript Saved]\033[0m {text_path}\n")
    if shutil.which("notify-send"):
        try:
            subprocess.run(
                [
                    "notify-send",
                    "-a", "Harmony Agent",
                    "-i", "audio-volume-high",
                    "Audio Speech Response Saved",
                    f"Audio: {audio_path}\nText: {text_path}",
                ],
                timeout=3,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass


def save_speech_response(text: str, filename_prefix: str = "agent_speech", **kwargs) -> dict:
    """
    Synthesizes and saves the agent's speech response to an audio file (.wav) and a companion
    text transcript (.txt) in the archive directory. Notifies the user with both paths.
    """
    if not text or not text.strip():
        return {"error": "Empty text provided for speech response"}

    os.makedirs(AUDIO_OUTPUT_DIR, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    unique_suffix = uuid.uuid4().hex[:6]
    base_name = f"{filename_prefix}_{timestamp}_{unique_suffix}"
    audio_path = os.path.join(AUDIO_OUTPUT_DIR, f"{base_name}.wav")
    text_path = os.path.join(AUDIO_OUTPUT_DIR, f"{base_name}.txt")

    # 1. Save companion text transcript
    try:
        with open(text_path, "w", encoding="utf-8") as f:
            f.write(text.strip() + "\n")
    except Exception as e:
        return {"error": f"Failed to write text transcript: {e}"}

    # 2. Synthesize audio
    speech_text = clean_speech_text(text)
    if not speech_text:
        speech_text = text.strip()

    audio_saved = False
    voice = get_cached_piper_voice()
    if voice:
        try:
            with wave.open(audio_path, "wb") as wf:
                voice.synthesize_wav(speech_text, wf)
            audio_saved = os.path.exists(audio_path) and os.path.getsize(audio_path) > 0
        except Exception as e:
            print(f"\033[1;33m[TTS Synthesis Error]\033[0m {e}")

    # Fallback to piper CLI
    if not audio_saved:
        piper_bin = shutil.which("piper") or os.path.expanduser("~/.local/bin/piper")
        voice_path = get_piper_voice_path()
        if voice_path and os.path.exists(piper_bin):
            try:
                subprocess.run(
                    [piper_bin, "-m", voice_path, "-f", audio_path],
                    input=speech_text,
                    text=True,
                    timeout=30,
                    check=True,
                    capture_output=True,
                )
                audio_saved = os.path.exists(audio_path) and os.path.getsize(audio_path) > 0
            except Exception as e:
                print(f"\033[1;33m[Piper CLI Error]\033[0m {e}")

    # Fallback to espeak-ng
    if not audio_saved and shutil.which("espeak-ng"):
        try:
            subprocess.run(
                ["espeak-ng", "-w", audio_path, speech_text],
                timeout=30,
                check=True,
                capture_output=True,
            )
            audio_saved = os.path.exists(audio_path) and os.path.getsize(audio_path) > 0
        except Exception as e:
            print(f"\033[1;33m[espeak-ng Error]\033[0m {e}")

    if audio_saved:
        notify_audio_saved(audio_path, text_path)
        return {
            "status": "saved",
            "audio_path": audio_path,
            "text_path": text_path,
            "size_bytes": os.path.getsize(audio_path),
            "text": text.strip(),
        }
    else:
        print(f"\n\033[1;33m[Warning]\033[0m Audio synthesis failed, but text transcript saved to: {text_path}\n")
        return {
            "status": "partial",
            "error": "Audio synthesis failed",
            "text_path": text_path,
            "text": text.strip(),
        }


def auto_archive_speech(text: str):
    """Automatically schedules background speech synthesis and archiving if enabled."""
    if not AUDIO_AUTO_ARCHIVE or not text or not text.strip():
        return
    t = threading.Thread(target=save_speech_response, args=(text,), daemon=True)
    t.start()


AVAILABLE_TOOLS = {
    "read_file": read_file,
    "read_file_lines": read_file_lines,
    "search_file_regex": search_file_regex,
    "write_file": write_file,
    "patch_file": patch_file,
    "patch_file_diff": patch_file_diff,
    "run_shell_command": run_shell_command,
    "start_background_task": start_background_task,
    "check_background_task": check_background_task,
    "get_system_telemetry": get_system_telemetry,
    "git_checkpoint": git_checkpoint,
    "git_rollback": git_rollback,
    "set_agent_memory": set_agent_memory,
    "get_agent_memory": get_agent_memory,
    "list_agent_memories": list_agent_memories,
    "web_search": web_search,
    "save_speech_response": save_speech_response,
}


def resolve_filepath(filepath: str) -> str:
    """Normalizes file paths, resolving omitted leading slashes, user tildes, and home-relative paths."""
    if not filepath or not isinstance(filepath, str):
        return filepath
    s = filepath.strip()
    path = os.path.expanduser(s)
    if os.path.exists(path):
        return os.path.abspath(path)
    # Check if leading slash was dropped (e.g. "var/home/..." -> "/var/home/...")
    if not s.startswith("/") and os.path.exists("/" + s):
        return os.path.abspath("/" + s)
    # Check if relative to user home directory
    home_rel = os.path.join(os.path.expanduser("~"), s)
    if os.path.exists(home_rel):
        return os.path.abspath(home_rel)
    return path


def dispatch_tool(func_name: str, kwargs: dict) -> dict:
    """Safely executes a registered tool with argument normalization, signature filtering, and error isolation."""
    if func_name not in AVAILABLE_TOOLS:
        return {"error": f"Tool '{func_name}' not found."}

    # Parameter aliases normalization
    if "filepath" not in kwargs:
        for alias in ("path", "file", "filename"):
            if alias in kwargs:
                kwargs["filepath"] = kwargs[alias]
                break

    if "filepath" in kwargs and isinstance(kwargs["filepath"], str):
        kwargs["filepath"] = resolve_filepath(kwargs["filepath"])
    if "repo_path" in kwargs and isinstance(kwargs["repo_path"], str):
        kwargs["repo_path"] = resolve_filepath(kwargs["repo_path"])

    if "command" not in kwargs and "cmd" in kwargs:
        kwargs["command"] = kwargs["cmd"]

    if "diff_patch" not in kwargs:
        for alias in ("patch", "diff"):
            if alias in kwargs:
                kwargs["diff_patch"] = kwargs[alias]
                break

    if "old_str" not in kwargs:
        for alias in ("old_text", "target", "old_string", "search"):
            if alias in kwargs:
                kwargs["old_str"] = kwargs[alias]
                break

    if "new_str" not in kwargs:
        for alias in ("new_text", "replacement", "new_string", "replace"):
            if alias in kwargs:
                kwargs["new_str"] = kwargs[alias]
                break

    func = AVAILABLE_TOOLS[func_name]
    try:
        return func(**kwargs)
    except TypeError as te:
        try:
            sig = inspect.signature(func)
            filtered = {k: v for k, v in kwargs.items() if k in sig.parameters}
            return func(**filtered)
        except Exception as e:
            return {"error": f"Tool '{func_name}' parameter error: {str(e)}"}
    except Exception as e:
        return {"error": f"Tool '{func_name}' execution error: {str(e)}"}

# ---------------------------------------------------------------------------
# 2. Tool Schemas Registered for Harmony
# ---------------------------------------------------------------------------
tool_schemas = [
    ToolDescription(name="read_file", description="Reads small files (<64KB), or a targeted line slice if start_line/end_line are specified.", parameters={"type": "object", "properties": {"filepath": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, "required": ["filepath"]}),
    ToolDescription(name="read_file_lines", description="Reads a line slice (max 250) with line numbers.", parameters={"type": "object", "properties": {"filepath": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, "required": ["filepath"]}),
    ToolDescription(name="search_file_regex", description="Searches massive files for regex patterns.", parameters={"type": "object", "properties": {"filepath": {"type": "string"}, "pattern": {"type": "string"}, "max_results": {"type": "integer"}}, "required": ["filepath", "pattern"]}),
    ToolDescription(name="write_file", description="Writes file with .bak backup.", parameters={"type": "object", "properties": {"filepath": {"type": "string"}, "content": {"type": "string"}}, "required": ["filepath", "content"]}),
    ToolDescription(name="patch_file", description="Replaces exact code block with backup.", parameters={"type": "object", "properties": {"filepath": {"type": "string"}, "old_str": {"type": "string"}, "new_str": {"type": "string"}}, "required": ["filepath", "old_str", "new_str"]}),
    ToolDescription(name="patch_file_diff", description="Applies unified diff patch syntax directly to a file.", parameters={"type": "object", "properties": {"filepath": {"type": "string"}, "diff_patch": {"type": "string"}}, "required": ["filepath", "diff_patch"]}),
    ToolDescription(name="run_shell_command", description="Executes short bash command (<30s).", parameters={"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}),
    ToolDescription(name="start_background_task", description="Launches long job detached; returns job_id.", parameters={"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}),
    ToolDescription(name="check_background_task", description="Checks log tail and status of a background job.", parameters={"type": "object", "properties": {"job_id": {"type": "string"}, "tail_lines": {"type": "integer"}}, "required": ["job_id"]}),
    ToolDescription(name="get_system_telemetry", description="Reads CPU, host RAM, and NVIDIA GPU telemetry (load, VRAM, temp).", parameters={"type": "object", "properties": {}}),
    ToolDescription(name="git_checkpoint", description="Creates safety checkpoint in git repo.", parameters={"type": "object", "properties": {"repo_path": {"type": "string"}, "message": {"type": "string"}}, "required": ["repo_path", "message"]}),
    ToolDescription(name="git_rollback", description="Rolls back repo to previous commit.", parameters={"type": "object", "properties": {"repo_path": {"type": "string"}}, "required": ["repo_path"]}),
    ToolDescription(name="set_agent_memory", description="Saves a key-value fact to persistent storage.", parameters={"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}}, "required": ["key", "value"]}),
    ToolDescription(name="get_agent_memory", description="Retrieves a persistent key-value fact.", parameters={"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}),
    ToolDescription(name="list_agent_memories", description="Lists all persistent key-value facts.", parameters={"type": "object", "properties": {}}),
    ToolDescription(name="web_search", description="Live web search via DuckDuckGo.", parameters={"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}}, "required": ["query"]}),
    ToolDescription(name="save_speech_response", description="Synthesizes and saves the agent's speech response to an audio file (.wav) and companion transcript (.txt) in the archive directory.", parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}),
]

# ---------------------------------------------------------------------------
# 3. Model Inspection, Encodings & Live Streaming Pipeline
# ---------------------------------------------------------------------------
def detect_ollama_model() -> str:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        return args[0]
    try:
        res = httpx.get("http://localhost:11434/api/tags", timeout=5.0)
        models = [m["name"] for m in res.json().get("models", [])]
        for m in models:
            if "gpt-oss" in m.lower():
                return m
        return models[0] if models else "gpt-oss-pinned:latest"
    except Exception:
        return "gpt-oss-pinned:latest"


def get_ollama_model_info(model_name: str) -> dict:
    try:
        res = httpx.post("http://localhost:11434/api/show", json={"name": model_name}, timeout=5.0)
        return res.json() if res.status_code == 200 else {}
    except Exception:
        return {}


def prune_conversation_if_needed(convo: Conversation, enc) -> int:
    tokens = enc.render_conversation_for_completion(convo, Role.ASSISTANT)
    current = len(tokens)
    pinned = 2
    pruned = 0
    while current > PRUNE_THRESHOLD and len(convo.messages) > (pinned + 2):
        convo.messages.pop(pinned)
        pruned += 1
        tokens = enc.render_conversation_for_completion(convo, Role.ASSISTANT)
        current = len(tokens)
    if pruned:
        print(f"\033[1;33m[Context Window]\033[0m Pruned {pruned} messages. Active tokens: {current:,}")
    return current


def stream_ollama_with_callback(prompt_text: str, model_name: str, on_token=None, stop_event: threading.Event = None) -> str:
    """Streams token chunks live from Ollama with repetition penalty and loop-break guardrail."""
    full = []
    recent_sliding = []
    
    with httpx.stream(
        "POST",
        "http://localhost:11434/api/generate",
        json={
            "model": model_name,
            "prompt": prompt_text,
            "raw": True,
            "stream": True,
            "options": {
                "stop": ["<|return|>", "<|call|>"],
                "num_ctx": NUM_CTX,
                "num_predict": NUM_PREDICT,
                "temperature": 0.35,      # Elevated from 0.2 to deter deterministic cyclic attractors
                "repeat_penalty": 1.18,    # Penalizes n-gram loops
                "repeat_last_n": 128,
                "top_p": 0.9,
            },
        },
        timeout=300.0,
    ) as response:
        if response.status_code != 200:
            raise RuntimeError(f"Ollama error {response.status_code}: {response.text}")
        for line in response.iter_lines():
            if stop_event and stop_event.is_set():
                print("\n\033[1;31m[Inference Aborted]\033[0m")
                break
            if not line:
                continue
            chunk = json.loads(line)
            piece = chunk.get("response", "")
            print(piece, end="", flush=True)
            full.append(piece)

            # Circuit breaker against degenerate repetition loops
            recent_sliding.append(piece)
            if len(recent_sliding) > 30:
                recent_sliding.pop(0)
                tail_str = "".join(recent_sliding)
                # Detect identical phrases of 12-25 characters repeated >= 4 times
                m_loop = re.search(r"(.{12,25}?)\1{3,}", tail_str)
                if m_loop:
                    print("\n\033[1;31m[Circuit Breaker]\033[0m Cyclic repetition loop detected. Halting generation turn.")
                    break

            if on_token:
                on_token(piece)
    print()
    return "".join(full)


def extract_content_text(msg) -> str:
    parts = []
    for part in msg.content:
        if hasattr(part, "text"):
            parts.append(part.text)
        elif isinstance(part, str):
            parts.append(part)
    return "".join(parts)


def init_agent():
    enc = load_harmony_encoding(HarmonyEncodingName.HARMONY_GPT_OSS)
    sys_content = SystemContent.new()
    
    memory_summary = ""
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r") as f:
                mem_data = json.load(f)
                if mem_data:
                    memory_summary = " Persistent Environment Memories: " + json.dumps(mem_data)
        except Exception:
            pass

    dev_content = (
        DeveloperContent.new()
        .with_instructions(
            "You are an autonomous local system administration, scripting, and technical research assistant. "
            "When modifying files, running commands, monitoring hardware, or managing long background tasks, "
            "YOU MUST ALWAYS INVOKE YOUR PROVIDED TOOLS. "
            "For large files (>300 lines), NEVER read them entirely: use `search_file_regex` or `read_file_lines`. "
            "For multi-file modifications, use `git_checkpoint`. "
            "For commands running longer than 30s, use `start_background_task`. "
            "When asked to write a review, report, or recommendations to a file, compose the text directly "
            "and invoke `write_file` to save it to disk. Do NOT loop repetitive statements in your analysis scratchpad. "
            "Summarize all voice replies concisely in the final channel."
            + memory_summary
        )
        .with_function_tools(tool_schemas)
    )

    convo = Conversation.from_messages([
        Message.from_role_and_content(Role.SYSTEM, sys_content),
        Message.from_role_and_content(Role.DEVELOPER, dev_content),
    ])
    return enc, convo


# ---------------------------------------------------------------------------
# 4. Voice Bridge Server with Real-Time SSE Forwarding & Socket Safety
# ---------------------------------------------------------------------------
GLOBAL_STATE = {
    "lock": threading.Lock(),
    "abort_event": threading.Event(),
}

class HarmonyBridgeHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        self.write_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):
        pass

    def send_sse_ping(self) -> bool:
        """Sends an SSE keep-alive comment line to keep the client read socket alive."""
        with self.write_lock:
            try:
                self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError):
                return False
            except Exception:
                return False

    def send_sse_chunk(self, content_str: str) -> bool:
        """Sends an SSE delta chunk. Catches BrokenPipeError/ConnectionResetError if client disconnects."""
        with self.write_lock:
            try:
                chunk = {
                    "id": "chatcmpl-harmony",
                    "object": "chat.completion.chunk",
                    "choices": [{"delta": {"content": content_str}, "index": 0}],
                }
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError):
                print("\n\033[1;33m[Client Disconnected]\033[0m Voice client closed the socket prematurely.")
                return False
            except Exception as e:
                print(f"\n\033[1;31m[SSE Write Error]\033[0m {e}")
                return False

    def do_POST(self):
        # 1. Voice Interruption / Barge-in Endpoint
        if self.path == "/v1/abort":
            GLOBAL_STATE["abort_event"].set()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                self.wfile.write(b'{"status":"aborted"}')
            except Exception:
                pass
            print("\033[1;31m[Barge-In]\033[0m Abort signal triggered via /v1/abort.")
            return

        if self.path != "/v1/chat/completions":
            self.send_response(404)
            self.end_headers()
            return

        GLOBAL_STATE["abort_event"].clear()
        content_length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(content_length).decode("utf-8"))

        messages = data.get("messages", [])
        last_user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user_msg = m.get("content", "").strip()
                break

        if not last_user_msg:
            self.send_response(400)
            self.end_headers()
            return

        print(f"\n\033[1;35m[Voice Input Heard]\033[0m {last_user_msg}")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        enc = GLOBAL_STATE["enc"]
        convo = GLOBAL_STATE["convo"]
        model_name = GLOBAL_STATE["model_name"]

        # Voice session reset trigger
        if last_user_msg.lower() in ("/reset", "reset", "clear session", "reset context"):
            with GLOBAL_STATE["lock"]:
                new_enc, new_convo = init_agent()
                GLOBAL_STATE["convo"] = new_convo
            self.send_sse_chunk("Session memory cleared to baseline.")
            try:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception:
                pass
            self.close_connection = True
            return

        stop_heartbeat = threading.Event()

        def heartbeat_worker():
            while not stop_heartbeat.wait(4.0):
                if not self.send_sse_ping():
                    GLOBAL_STATE["abort_event"].set()
                    break

        heartbeat_thread = threading.Thread(target=heartbeat_worker, daemon=True)
        heartbeat_thread.start()

        try:
            with GLOBAL_STATE["lock"]:
                prune_conversation_if_needed(convo, enc)
                convo.messages.append(Message.from_role_and_content(Role.USER, last_user_msg))

                step = 0
                client_alive = True
                accumulated_speech_chunks = []

                while step < MAX_STEPS and client_alive:
                    if GLOBAL_STATE["abort_event"].is_set():
                        break
                    step += 1
                    prompt_tokens = enc.render_conversation_for_completion(convo, Role.ASSISTANT)
                    prompt_text = enc.decode(prompt_tokens)

                    print(f"\033[1;30m[Agent Step {step}] Generating...\033[0m")

                    # Channel parser state machine for real-time streaming
                    raw_token_window = []
                    final_channel_active = [False]
                    sentence_buffer = [""]

                    def live_token_callback(piece: str):
                        nonlocal client_alive
                        if not client_alive or GLOBAL_STATE["abort_event"].is_set():
                            return

                        raw_token_window.append(piece)
                        window = "".join(raw_token_window[-12:])

                        # Identify entry into the final user channel
                        if "final<|message|>" in window or (not final_channel_active[0] and "final" in window and "<|message|>" in piece):
                            final_channel_active[0] = True
                            return

                        # Identify exit from channel
                        if final_channel_active[0] and ("<|end|>" in piece or "<|call|>" in piece):
                            final_channel_active[0] = False
                            return

                        # Forward sentence chunks live
                        if final_channel_active[0]:
                            clean_piece = re.sub(r"<\|.*?\|>", "", piece)
                            if clean_piece:
                                accumulated_speech_chunks.append(clean_piece)
                                sentence_buffer[0] += clean_piece
                                while re.search(r"[\.\!\?\n]\s", sentence_buffer[0]):
                                    m = re.search(r"[\.\!\?\n]\s", sentence_buffer[0])
                                    idx = m.end()
                                    chunk_to_send = sentence_buffer[0][:idx]
                                    sentence_buffer[0] = sentence_buffer[0][idx:]
                                    if not self.send_sse_chunk(chunk_to_send):
                                        client_alive = False
                                        GLOBAL_STATE["abort_event"].set()
                                        break

                    raw_response = stream_ollama_with_callback(
                        prompt_text,
                        model_name,
                        on_token=live_token_callback,
                        stop_event=GLOBAL_STATE["abort_event"],
                    )

                    # Flush any residual text in the buffer
                    if sentence_buffer[0].strip() and client_alive and not GLOBAL_STATE["abort_event"].is_set():
                        self.send_sse_chunk(sentence_buffer[0])

                    resp_tokens = enc.encode(raw_response, allowed_special="all")
                    parsed_messages = enc.parse_messages_from_completion_tokens(resp_tokens, role=Role.ASSISTANT)
                    convo.messages.extend(parsed_messages)

                    tool_called = False
                    for msg in parsed_messages:
                        if msg.recipient and msg.recipient.startswith("functions."):
                            tool_called = True
                            func_name = msg.recipient.split("functions.", 1)[1]
                            raw_args = extract_content_text(msg)
                            try:
                                kwargs = json.loads(raw_args)
                            except Exception:
                                kwargs = {}

                            print(f"\n\033[1;33m[Tool Dispatch]\033[0m {func_name}")
                            print(f"\033[1;30mArgs: {json.dumps(kwargs, indent=2)}\033[0m")

                            res = dispatch_tool(func_name, kwargs)
                            preview = str(res)[:500] + ("..." if len(str(res)) > 500 else "")
                            print(f"\033[1;32m[Tool Result]\033[0m {preview}\n")

                            convo.messages.append(Message.from_author_and_content(author=Author.new(Role.TOOL, f"functions.{func_name}"), content=json.dumps(res)))

                    if not tool_called:
                        break

                # If maximum steps reached without a final response, trigger a final summary
                if client_alive and not GLOBAL_STATE["abort_event"].is_set() and not final_channel_active[0] and not sentence_buffer[0].strip():
                    convo.messages.append(Message.from_role_and_content(
                        Role.DEVELOPER,
                        DeveloperContent.new().with_instructions(
                            "Maximum tool execution steps reached. You MUST now summarize your actions, results, and findings directly in the final channel for the user."
                        )
                    ))
                    prompt_tokens = enc.render_conversation_for_completion(convo, Role.ASSISTANT)
                    prompt_text = enc.decode(prompt_tokens)
                    print("\n\033[1;35m[Final Summary Step] Generating voice reply...\033[0m")
                    raw_response = stream_ollama_with_callback(
                        prompt_text,
                        model_name,
                        on_token=live_token_callback,
                        stop_event=GLOBAL_STATE["abort_event"],
                    )
                    if sentence_buffer[0].strip() and client_alive and not GLOBAL_STATE["abort_event"].is_set():
                        self.send_sse_chunk(sentence_buffer[0])
                    resp_tokens = enc.encode(raw_response, allowed_special="all")
                    parsed_messages = enc.parse_messages_from_completion_tokens(resp_tokens, role=Role.ASSISTANT)
                    convo.messages.extend(parsed_messages)

                # Automatic speech archiving of assistant voice reply
                speech_to_save = "".join(accumulated_speech_chunks).strip()
                if not speech_to_save:
                    speech_parts = []
                    for msg in parsed_messages:
                        if getattr(msg, "channel", None) == "final":
                            pt = extract_content_text(msg).strip()
                            if pt:
                                speech_parts.append(pt)
                    speech_to_save = "\n\n".join(speech_parts).strip()

                if speech_to_save and not GLOBAL_STATE["abort_event"].is_set():
                    auto_archive_speech(speech_to_save)
        except Exception as e:
            print(f"\n\033[1;31m[Agent Error]\033[0m {e}")
            self.send_sse_chunk(f"An error occurred: {e}")
        finally:
            stop_heartbeat.set()
            try:
                heartbeat_thread.join(timeout=1.0)
            except Exception:
                pass
            try:
                with self.write_lock:
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True


# ---------------------------------------------------------------------------
# 5. CLI Banner & Main Loop
# ---------------------------------------------------------------------------
def print_banner(model_name: str, model_info: dict):
    details = model_info.get("details", {})
    raw_info = model_info.get("model_info", {})
    arch = details.get("family", "Unknown")
    param_size = details.get("parameter_size", "Unknown")
    quant = details.get("quantization_level", "Unknown")
    ctx_len = next((f"{v:,} tokens" for k, v in raw_info.items() if k.endswith(".context_length")), "Unknown")

    print("\033[1;36m======================================================================\033[0m")
    print("\033[1;37m   OpenAI Harmony Advanced Autonomous Engineering Agent\033[0m")
    print("\033[1;36m======================================================================\033[0m")
    print(f" \033[1mModel Tag\033[0m         : \033[1;32m{model_name}\033[0m")
    print(f" \033[1mArchitecture\033[0m      : {arch.upper()} | {param_size} | {quant}")
    print(f" \033[1mNative Context\033[0m    : {ctx_len}")
    print(f" \033[1mTools Active\033[0m      : {len(AVAILABLE_TOOLS)} registered (diff, telemetry, jobs, memory, git, web, audio)")
    print(f" \033[1mAudio Archive\033[0m     : {AUDIO_OUTPUT_DIR} (auto-archive: {'ON' if AUDIO_AUTO_ARCHIVE else 'OFF'})")
    print(f" \033[1mGuardrails\033[0m        : safe_mode={'ON' if SAFE_MODE else 'OFF'} | auto-pruner={PRUNE_THRESHOLD} tok")
    print("\033[1;36m----------------------------------------------------------------------\033[0m")


def main():
    model_name = detect_ollama_model()
    model_info = get_ollama_model_info(model_name)
    if not model_info:
        print(f"\033[1;31m[Error]\033[0m Model '{model_name}' not found in Ollama.")
        sys.exit(1)

    print_banner(model_name, model_info)
    enc, convo = init_agent()

    # Pre-warm Piper TTS model in background
    if AUDIO_AUTO_ARCHIVE:
        threading.Thread(target=get_cached_piper_voice, daemon=True).start()

    if "--server" in sys.argv or "-s" in sys.argv:
        GLOBAL_STATE["enc"] = enc
        GLOBAL_STATE["convo"] = convo
        GLOBAL_STATE["model_name"] = model_name

        try:
            server = HTTPServer(("127.0.0.1", AGENT_PORT), HarmonyBridgeHandler)
        except OSError as e:
            if getattr(e, 'errno', None) == 98 or "Address already in use" in str(e):
                print(f"\n\033[1;31m[Error] Port {AGENT_PORT} is already in use by another running instance of MP Harmony Agent.\033[0m")
                print(f"\033[1;33mThe Voice Bridge is already active. To restart, stop the existing instance first via Option 8 or Option 7 in the Manager.\033[0m\n")
            else:
                print(f"\n\033[1;31m[Error starting server]\033[0m {e}\n")
            return

        print(f"\033[1;32m[Voice Bridge Active]\033[0m Listening at http://127.0.0.1:{AGENT_PORT}/v1/chat/completions")
        print(f"\033[1;32m[Abort Endpoint]\033[0m POST http://127.0.0.1:{AGENT_PORT}/v1/abort")
        print("\033[1;36m======================================================================\033[0m\n")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server.")
            server.server_close()
        return

    # Interactive CLI Mode
    print(" Commands: '/reset' to wipe history, 'exit' or Ctrl+C to quit.\n")
    while True:
        try:
            user_input = input("\n\033[1;34mYou >\033[0m ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            break
        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            break
        if user_input.lower() in ("/reset", "reset", "clear"):
            enc, convo = init_agent()
            print("\033[1;32m[Reset]\033[0m Conversation cleared.")
            continue

        prune_conversation_if_needed(convo, enc)
        convo.messages.append(Message.from_role_and_content(Role.USER, user_input))

        step = 0
        cli_speech_parts = []
        while step < MAX_STEPS:
            step += 1
            prompt_tokens = enc.render_conversation_for_completion(convo, Role.ASSISTANT)
            prompt_text = enc.decode(prompt_tokens)

            print(f"\033[1;30m[Agent Step {step}] Generating...\033[0m")
            raw_response = stream_ollama_with_callback(prompt_text, model_name)

            resp_tokens = enc.encode(raw_response, allowed_special="all")
            parsed_messages = enc.parse_messages_from_completion_tokens(resp_tokens, role=Role.ASSISTANT)
            convo.messages.extend(parsed_messages)

            tool_called = False
            for msg in parsed_messages:
                if msg.recipient and msg.recipient.startswith("functions."):
                    tool_called = True
                    func_name = msg.recipient.split("functions.", 1)[1]
                    raw_args = extract_content_text(msg)
                    try:
                        kwargs = json.loads(raw_args)
                    except Exception:
                        kwargs = {}

                    print(f"\n\033[1;33m[Tool Dispatch]\033[0m {func_name}")
                    print(f"\033[1;30mArgs: {json.dumps(kwargs, indent=2)}\033[0m")

                    res = dispatch_tool(func_name, kwargs)
                    preview = str(res)[:500] + ("..." if len(str(res)) > 500 else "")
                    print(f"\033[1;32m[Tool Result]\033[0m {preview}\n")

                    convo.messages.append(Message.from_author_and_content(author=Author.new(Role.TOOL, f"functions.{func_name}"), content=json.dumps(res)))

            if not tool_called:
                for msg in parsed_messages:
                    if getattr(msg, "channel", None) == "final":
                        t = extract_content_text(msg).strip()
                        if t:
                            cli_speech_parts.append(t)
                if not cli_speech_parts:
                    for msg in parsed_messages:
                        if getattr(msg, "channel", None) not in ("thought", "analysis", "commentary") and not (getattr(msg, "recipient", None) and msg.recipient.startswith("functions.")):
                            t = extract_content_text(msg).strip()
                            if t:
                                cli_speech_parts.append(t)
                break

        if tool_called and step >= MAX_STEPS:
            convo.messages.append(Message.from_role_and_content(
                Role.DEVELOPER,
                DeveloperContent.new().with_instructions(
                    "Maximum tool execution steps reached. Please synthesize your findings, actions, and current status directly for the user."
                )
            ))
            prompt_tokens = enc.render_conversation_for_completion(convo, Role.ASSISTANT)
            prompt_text = enc.decode(prompt_tokens)
            print("\n\033[1;35m[Final Summary Step] Generating response...\033[0m")
            raw_response = stream_ollama_with_callback(prompt_text, model_name)
            resp_tokens = enc.encode(raw_response, allowed_special="all")
            parsed_messages = enc.parse_messages_from_completion_tokens(resp_tokens, role=Role.ASSISTANT)
            convo.messages.extend(parsed_messages)
            for msg in parsed_messages:
                if getattr(msg, "channel", None) == "final":
                    t = extract_content_text(msg).strip()
                    if t:
                        cli_speech_parts.append(t)
            if not cli_speech_parts:
                for msg in parsed_messages:
                    if getattr(msg, "channel", None) not in ("thought", "analysis", "commentary") and not (getattr(msg, "recipient", None) and msg.recipient.startswith("functions.")):
                        t = extract_content_text(msg).strip()
                        if t:
                            cli_speech_parts.append(t)

        final_speech = "\n\n".join(cli_speech_parts).strip()
        if final_speech:
            auto_archive_speech(final_speech)


if __name__ == "__main__":
    main()