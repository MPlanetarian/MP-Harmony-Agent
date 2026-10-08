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
import queue
import io
import urllib.parse
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

# Persistent Ollama HTTP connection pool
_OLLAMA_CLIENT = httpx.Client(
    base_url="http://localhost:11434",
    timeout=httpx.Timeout(300.0, connect=10.0),
    limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
)

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
    meta_file = os.path.join(JOBS_DIR, f"{job_id}.meta")
    try:
        with open(log_file, "w") as out:
            proc = subprocess.Popen(
                command,
                shell=True,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        meta = {"job_id": job_id, "pid": proc.pid, "command": command, "started_at": time.time()}
        with open(meta_file, "w") as mf:
            json.dump(meta, mf)
        return {"job_id": job_id, "pid": proc.pid, "log_file": log_file, "status": "started"}
    except Exception as e:
        return {"error": str(e)}


def stop_background_task(job_id: str, **kwargs) -> dict:
    """Terminates a running background task process by its job_id."""
    meta_file = os.path.join(JOBS_DIR, f"{job_id}.meta")
    pid = None
    if os.path.exists(meta_file):
        try:
            with open(meta_file, "r") as mf:
                meta = json.load(mf)
                pid = meta.get("pid")
        except Exception:
            pass
    if not pid:
        return {"error": f"No job or PID record found for job_id '{job_id}'."}
    try:
        import signal
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        time.sleep(0.5)
        return {"job_id": job_id, "status": "terminated", "pid": pid}
    except ProcessLookupError:
        return {"job_id": job_id, "status": "process already exited", "pid": pid}
    except Exception as e:
        return {"error": f"Failed to stop job '{job_id}': {str(e)}"}


def check_background_task(job_id: str, tail_lines: int = 20, **kwargs) -> dict:
    """Inspects exit status and output tail of a background task."""
    log_file = os.path.join(JOBS_DIR, f"{job_id}.log")
    meta_file = os.path.join(JOBS_DIR, f"{job_id}.meta")
    if not os.path.exists(log_file):
        return {"error": f"No job found with id '{job_id}'."}

    status = "unknown"
    pid = None
    if os.path.exists(meta_file):
        try:
            with open(meta_file, "r") as mf:
                meta = json.load(mf)
                pid = meta.get("pid")
                if pid:
                    os.kill(pid, 0)
                    status = "running"
        except ProcessLookupError:
            status = "completed"
        except Exception:
            pass

    tail_cmd = f"tail -n {tail_lines} '{log_file}'"
    tail_res = subprocess.run(tail_cmd, shell=True, capture_output=True, text=True)
    return {"job_id": job_id, "status": status, "pid": pid, "recent_logs": tail_res.stdout.strip()}


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


def search_agent_memories(query: str = "", **kwargs) -> dict:
    """Searches stored persistent memories using multi-term keyword overlap and relevance ranking."""
    if not os.path.exists(MEMORY_FILE):
        return {"count": 0, "memories": {}}
    try:
        with open(MEMORY_FILE, "r") as f:
            memories = json.load(f)
    except Exception as e:
        return {"error": str(e)}

    q = (query or "").lower().strip()
    if not q:
        return {"count": len(memories), "memories": memories}

    q_tokens = set(re.findall(r"\w+", q))
    scored = []
    for k, v in memories.items():
        text_full = f"{k} {v}".lower()
        tokens_full = set(re.findall(r"\w+", text_full))
        overlap = len(q_tokens & tokens_full)
        if q in text_full or overlap > 0:
            score = (10 if q in text_full else 0) + overlap * 3
            scored.append((score, k, v))

    scored.sort(key=lambda x: x[0], reverse=True)
    results = {k: v for _, k, v in scored[:15]}
    return {"query": query, "count": len(results), "memories": results}


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


def list_directory(path: str = ".", show_hidden: bool = False, max_items: int = 60, **kwargs) -> dict:
    """Lists directory contents with file types, sizes in KB, and modification dates."""
    try:
        p = resolve_filepath(path)
        if not os.path.exists(p):
            return {"error": f"Directory not found: {p}"}
        if not os.path.isdir(p):
            return {"error": f"Path is a file, not a directory: {p}"}

        entries = []
        for name in sorted(os.listdir(p)):
            if not show_hidden and name.startswith("."):
                continue
            full = os.path.join(p, name)
            is_dir = os.path.isdir(full)
            size_kb = round(os.path.getsize(full) / 1024, 1) if not is_dir else 0
            mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(full)))
            entries.append({
                "name": name,
                "type": "directory" if is_dir else "file",
                "size_kb": size_kb if not is_dir else None,
                "modified": mtime,
            })
            if len(entries) >= int(max_items):
                break
        return {"directory": p, "count": len(entries), "entries": entries}
    except Exception as e:
        return {"error": str(e)}


def get_mix_archive_roots() -> list[str]:
    """Finds all configured Mix Archive directories on the host."""
    roots = []
    cfg_paths = [
        "/var/home/mplanetarian/MP_Mix_Manager_v0.3/config.env",
        os.path.expanduser("~/MP_Mix_Manager_v0.3/config.env"),
    ]
    for cfg in cfg_paths:
        if os.path.exists(cfg):
            try:
                with open(cfg, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("MIX_ARCHIVE_DIR="):
                            val = line.split("=", 1)[1].strip('"\'; ')
                            if val and os.path.isdir(val) and val not in roots:
                                roots.append(val)
                        elif line.startswith("EXTRA_MIX_ARCHIVE_DIRS="):
                            val = line.split("=", 1)[1].strip('"\'; ')
                            for p in val.split(":"):
                                p = p.strip()
                                if p and os.path.isdir(p) and p not in roots:
                                    roots.append(p)
            except Exception:
                pass

    defaults = [
        "/run/media/mplanetarian/WD BLACK B/MIX_ARCHIVE",
        "/var/home/mplanetarian/MP_Mix_Manager_v0.3/MIX_ARCHIVE",
        "/run/media/mplanetarian/DATA/MIX_ARCHIVE2",
        "/var/home/mplanetarian/GoogleDrive/MIX_ARCHIVE",
    ]
    for d in defaults:
        if os.path.isdir(d) and d not in roots:
            roots.append(d)
    return roots


def search_mix_archive(query: str = "", max_results: int = 15, **kwargs) -> dict:
    """Searches the user's Mix Archive for audio mixes, companion tracklists, and spectrograms."""
    roots = get_mix_archive_roots()
    if not roots:
        return {"error": "No accessible Mix Archive directory found on host."}

    q = (query or "").lower().strip()
    matches = []
    audio_exts = (".flac", ".wav", ".mp3", ".m4a")

    for root in roots:
        for dirpath, _, filenames in os.walk(root):
            for fname in filenames:
                if any(fname.lower().endswith(ext) for ext in audio_exts):
                    if not q or q in fname.lower():
                        full_path = os.path.join(dirpath, fname)
                        size_mb = round(os.path.getsize(full_path) / (1024 * 1024), 1)
                        base_stem = os.path.splitext(fname)[0]
                        txt_path = os.path.join(dirpath, f"{base_stem}.txt")
                        has_tracklist = os.path.exists(txt_path)
                        matches.append({
                            "title": fname,
                            "path": full_path,
                            "size_mb": size_mb,
                            "folder": os.path.basename(dirpath),
                            "tracklist": txt_path if has_tracklist else None,
                        })
                        if len(matches) >= int(max_results):
                            break
            if len(matches) >= int(max_results):
                break

    return {"query": query, "count": len(matches), "archive_roots": roots, "results": matches}


def play_mix_or_audio(target: str = "latest", command: str = "play", **kwargs) -> dict:
    """
    Controls playback or plays a specific mix using MP Audio Player.
    Commands: 'play', 'pause', 'toggle', 'stop', 'latest', 'status'.
    Target: filepath, mix title keyword, or 'latest'.
    """
    player_script = "/var/home/mplanetarian/MP_Mix_Manager_v0.3/MP_Audio_Player.py"
    if not os.path.exists(player_script):
        player_script = os.path.expanduser("~/MP_Mix_Manager_v0.3/MP_Audio_Player.py")

    cmd = (command or "play").lower().strip()

    # Direct IPC commands if user specifies pause, toggle, stop, status
    if cmd in ("pause", "toggle", "stop", "status"):
        if os.path.exists(player_script):
            flag = f"--{cmd}"
            res = subprocess.run([sys.executable, player_script, flag], capture_output=True, text=True, timeout=10)
            return {"command": cmd, "status": "executed", "output": res.stdout.strip() or res.stderr.strip()}

    # Resolve target audio file
    target_path = None
    target_str = (target or "").strip()
    if target_str in ("latest", "newest", "recent", "") or cmd == "latest":
        if os.path.exists(player_script):
            res = subprocess.run([sys.executable, player_script, "--get-latest-mix"], capture_output=True, text=True, timeout=10)
            latest = res.stdout.strip()
            if latest and os.path.exists(latest):
                target_path = latest
    elif os.path.exists(os.path.expanduser(target_str)):
        target_path = os.path.abspath(os.path.expanduser(target_str))
    else:
        # Search mix archive for best keyword match
        search_res = search_mix_archive(target_str, max_results=1)
        if search_res.get("results"):
            target_path = search_res["results"][0]["path"]

    if not target_path or not os.path.exists(target_path):
        return {"error": f"Audio file not found for target '{target}'."}

    # Dispatch to MP Audio Player
    if os.path.exists(player_script):
        subprocess.Popen(
            [sys.executable, player_script, "--play", target_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return {
            "status": "playback started",
            "player": "MP_Audio_Player",
            "mix": os.path.basename(target_path),
            "path": target_path,
        }

    # Fallback to system audio player
    for fallback in ("pw-play", "paplay", "mpv"):
        if shutil.which(fallback):
            args = [fallback, target_path] if fallback != "mpv" else ["mpv", "--no-video", target_path]
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return {"status": "playback started", "player": fallback, "mix": os.path.basename(target_path), "path": target_path}

    return {"error": "No compatible audio player found on system."}


def get_mix_archive_stats(**kwargs) -> dict:
    """Returns statistics about the user's Mix Archive including track counts, storage used, and recent mixes."""
    roots = get_mix_archive_roots()
    if not roots:
        return {"error": "No accessible Mix Archive found on host."}

    total_files = 0
    total_bytes = 0
    format_counts = {"flac": 0, "wav": 0, "mp3": 0, "mp4": 0, "other": 0}
    recent_mixes = []

    for root in roots:
        for dirpath, _, filenames in os.walk(root):
            for fname in filenames:
                ext = os.path.splitext(fname)[1].lower().lstrip(".")
                if ext in format_counts:
                    format_counts[ext] += 1
                    total_files += 1
                    full_p = os.path.join(dirpath, fname)
                    sz = os.path.getsize(full_p)
                    total_bytes += sz
                    mtime = os.path.getmtime(full_p)
                    recent_mixes.append((mtime, fname, full_p, round(sz / (1024 * 1024), 1)))

    recent_mixes.sort(key=lambda x: x[0], reverse=True)
    top_recent = [
        {"title": x[1], "path": x[2], "size_mb": x[3], "date": time.strftime("%Y-%m-%d %H:%M", time.localtime(x[0]))}
        for x in recent_mixes[:3]
    ]

    total_gb = round(total_bytes / (1024 ** 3), 2)
    return {
        "archive_roots": roots,
        "total_audio_files": total_files,
        "total_storage_gb": total_gb,
        "formats": format_counts,
        "latest_mixes": top_recent,
    }


def get_mix_tracklist(target: str, **kwargs) -> dict:
    """Retrieves the full timestamped tracklist for a specific DJ mix from companion .txt, .cue, or Traktor .nml files."""
    roots = get_mix_archive_roots()
    if not roots:
        return {"error": "No accessible Mix Archive found on host."}

    target_clean = (target or "").strip()
    if not target_clean:
        return {"error": "Target mix name or path cannot be empty."}

    # 1. If target is already a filepath to a tracklist file
    if os.path.isfile(target_clean) and target_clean.lower().endswith((".txt", ".cue", ".nml")):
        tracklist_file = target_clean
    else:
        # 2. Search for candidate companion files
        candidates = []
        target_stem = os.path.splitext(os.path.basename(target_clean))[0].lower()
        target_words = [w for w in re.split(r"[\s_\-]+", target_stem) if len(w) > 2]

        for root in roots:
            for dirpath, _, filenames in os.walk(root):
                for fname in filenames:
                    ext = os.path.splitext(fname)[1].lower()
                    if ext in (".txt", ".cue", ".nml"):
                        fn_lower = fname.lower()
                        if target_stem and target_stem in fn_lower:
                            candidates.append((10, os.path.join(dirpath, fname)))
                        elif target_words:
                            matches = sum(1 for w in target_words if w in fn_lower)
                            if matches >= max(1, len(target_words) - 1):
                                candidates.append((matches, os.path.join(dirpath, fname)))

        if not candidates:
            return {"error": f"No companion tracklist (.txt, .cue, .nml) found matching '{target}' in mix archives."}

        candidates.sort(key=lambda x: x[0], reverse=True)
        tracklist_file = candidates[0][1]

    # Parse the tracklist file
    parsed_tracks = []
    ext = os.path.splitext(tracklist_file)[1].lower()

    if ext == ".txt":
        try:
            with open(tracklist_file, "r", encoding="utf-8", errors="replace") as f:
                in_tracklist = False
                for line in f:
                    sline = line.strip()
                    if "TRACKLIST" in sline.upper():
                        in_tracklist = True
                        continue
                    if not sline or sline.startswith("="):
                        continue
                    m = re.match(r"^(?:\[([0-9:]+)\]\s*)?(?:(\d+)[\.\)]\s*)?(.*?)\s*[-–—]\s*(.*)$", sline)
                    if m:
                        timestamp = m.group(1) or ""
                        idx = int(m.group(2)) if m.group(2) else len(parsed_tracks) + 1
                        artist = m.group(3).strip()
                        title = m.group(4).strip()
                        parsed_tracks.append({"index": idx, "time": timestamp, "artist": artist, "title": title, "raw": sline})
                    elif in_tracklist:
                        parsed_tracks.append({"index": len(parsed_tracks) + 1, "raw": sline})
        except Exception as e:
            return {"error": f"Error reading tracklist text: {e}"}

    elif ext == ".cue":
        try:
            with open(tracklist_file, "r", encoding="utf-8", errors="replace") as f:
                cur_track = {}
                for line in f:
                    sline = line.strip()
                    if sline.startswith("TRACK"):
                        if cur_track:
                            parsed_tracks.append(cur_track)
                        m_idx = re.search(r"TRACK\s+(\d+)", sline)
                        cur_track = {"index": int(m_idx.group(1)) if m_idx else len(parsed_tracks) + 1, "time": "", "artist": "", "title": ""}
                    elif sline.startswith("TITLE") and cur_track:
                        cur_track["title"] = sline.split("TITLE", 1)[1].strip(' "')
                    elif sline.startswith("PERFORMER") and cur_track:
                        cur_track["artist"] = sline.split("PERFORMER", 1)[1].strip(' "')
                    elif sline.startswith("INDEX 01") and cur_track:
                        cur_track["time"] = sline.split("INDEX 01", 1)[1].strip()
                if cur_track:
                    parsed_tracks.append(cur_track)
        except Exception as e:
            return {"error": f"Error reading cue sheet: {e}"}

    elif ext == ".nml":
        try:
            with open(tracklist_file, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            for m in re.finditer(r'<ENTRY[^>]+TITLE="([^"]*)"[^>]+ARTIST="([^"]*)"', content):
                parsed_tracks.append({
                    "index": len(parsed_tracks) + 1,
                    "artist": m.group(2),
                    "title": m.group(1),
                    "raw": f"{m.group(2)} - {m.group(1)}",
                })
        except Exception as e:
            return {"error": f"Error reading Traktor NML: {e}"}

    return {
        "mix_target": target,
        "tracklist_file": tracklist_file,
        "total_tracks": len(parsed_tracks),
        "tracks": parsed_tracks[:50],
    }


def find_track_in_mixes(query: str, max_results: int = 15, **kwargs) -> dict:
    """Searches across all DJ mix tracklists (.txt, .cue, Traktor history) for a specific song title, remix, or artist."""
    roots = get_mix_archive_roots()
    if not roots:
        return {"error": "No accessible Mix Archive found on host."}

    q = (query or "").lower().strip()
    if not q:
        return {"error": "Query cannot be empty."}

    matches = []
    limit = max(1, min(50, int(max_results)))

    for root in roots:
        if len(matches) >= limit:
            break
        for dirpath, _, filenames in os.walk(root):
            if len(matches) >= limit:
                break
            for fname in filenames:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in (".txt", ".cue", ".nml"):
                    continue
                full_path = os.path.join(dirpath, fname)
                try:
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        if ext == ".txt":
                            first_chunk = f.read(1024)
                            if "TRACKLIST" not in first_chunk.upper() and "ARTIST:" not in first_chunk.upper():
                                continue
                            f.seek(0)
                            for line_idx, line in enumerate(f, 1):
                                if q in line.lower() and not line.startswith("="):
                                    matches.append({
                                        "mix": os.path.splitext(fname)[0],
                                        "source_type": "companion_txt",
                                        "track": line.strip(),
                                        "file": full_path,
                                    })
                                    if len(matches) >= limit:
                                        break
                        elif ext == ".cue":
                            f.seek(0)
                            content = f.read()
                            if q in content.lower():
                                for track_block in content.split("TRACK "):
                                    if q in track_block.lower():
                                        t_title = re.search(r'TITLE\s+"([^"]*)"', track_block)
                                        t_artist = re.search(r'PERFORMER\s+"([^"]*)"', track_block)
                                        t_idx = re.search(r"^(\d+)", track_block.strip())
                                        artist_str = t_artist.group(1) if t_artist else "Unknown"
                                        title_str = t_title.group(1) if t_title else "Unknown"
                                        idx_str = t_idx.group(1) if t_idx else "?"
                                        matches.append({
                                            "mix": os.path.splitext(fname)[0],
                                            "source_type": "cue_sheet",
                                            "track": f"{idx_str}. {artist_str} - {title_str}",
                                            "file": full_path,
                                        })
                                        if len(matches) >= limit:
                                            break
                        elif ext == ".nml":
                            content = f.read()
                            if q in content.lower():
                                for m in re.finditer(r'<ENTRY[^>]+TITLE="([^"]*)"[^>]+ARTIST="([^"]*)"', content):
                                    t_title = m.group(1)
                                    t_artist = m.group(2)
                                    combined = f"{t_artist} - {t_title}"
                                    if q in combined.lower():
                                        matches.append({
                                            "mix": os.path.splitext(fname)[0],
                                            "source_type": "traktor_history",
                                            "track": combined,
                                            "file": full_path,
                                        })
                                        if len(matches) >= limit:
                                            break
                except Exception:
                    continue

    return {
        "query": query,
        "match_count": len(matches),
        "matches": matches,
    }


def system_audio_volume(action: str = "status", level: int = None, **kwargs) -> dict:
    """Controls or inspects host audio volume via PipeWire / WirePlumber (wpctl)."""
    act = (action or "status").lower().strip()
    try:
        if act == "status":
            res = subprocess.run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"], capture_output=True, text=True, timeout=5)
            out = res.stdout.strip()
            muted = "[MUTED]" in out
            m = re.search(r"Volume:\s*([0-9.]+)", out)
            vol_pct = round(float(m.group(1)) * 100) if m else None
            return {"status": "ok", "volume_percent": vol_pct, "muted": muted, "raw": out}

        elif act == "set" and level is not None:
            lvl = max(0, min(150, int(level)))
            subprocess.run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{lvl}%"], check=True, timeout=5)
            return {"status": "volume set", "level_percent": lvl}

        elif act in ("mute", "unmute", "toggle_mute", "toggle"):
            subprocess.run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"], check=True, timeout=5)
            status = system_audio_volume(action="status")
            return {"status": "mute toggled", "muted": status.get("muted")}

        return {"error": f"Unsupported action '{action}'. Use 'status', 'set' with level, or 'toggle_mute'."}
    except Exception as e:
        return {"error": f"Volume control failed: {str(e)}"}


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


_AUDIO_PHRASE_CACHE = {}


def synthesize_piper_wav_bytes(text: str, voice_name: str = None, speed: float = 1.0) -> bytes:
    """Synthesizes speech to in-memory WAV audio bytes with voice selection, speed scaling, and LRU phrase caching."""
    clean_text = clean_speech_text(text) or text.strip()
    if not clean_text:
        return b""

    spd = max(0.4, min(2.5, float(speed)))
    cache_key = (clean_text, voice_name or "default", round(spd, 2))
    if cache_key in _AUDIO_PHRASE_CACHE:
        return _AUDIO_PHRASE_CACHE[cache_key]

    voice = get_cached_piper_voice()
    buf = io.BytesIO()
    if voice:
        try:
            from piper.config import SynthesisConfig
            syn_cfg = SynthesisConfig(length_scale=1.0 / spd) if spd != 1.0 else None
            with wave.open(buf, "wb") as wf:
                voice.synthesize_wav(clean_text, wf, syn_config=syn_cfg)
            raw = buf.getvalue()
            if raw:
                if len(clean_text) < 140 and len(_AUDIO_PHRASE_CACHE) < 200:
                    _AUDIO_PHRASE_CACHE[cache_key] = raw
                return raw
        except Exception as e:
            print(f"\033[1;33m[TTS Python Synthesis Error]\033[0m {e}")

    # Fallback to piper CLI
    piper_bin = shutil.which("piper") or os.path.expanduser("~/.local/bin/piper")
    voice_path = get_piper_voice_path()
    if voice_path and os.path.exists(piper_bin):
        try:
            length_scale = str(round(1.0 / spd, 2))
            cmd = [piper_bin, "-m", voice_path, "--length-scale", length_scale, "--output_file", "-"]
            p = subprocess.run(cmd, input=clean_text.encode("utf-8"), capture_output=True, timeout=15)
            if p.returncode == 0 and p.stdout:
                if len(clean_text) < 140 and len(_AUDIO_PHRASE_CACHE) < 200:
                    _AUDIO_PHRASE_CACHE[cache_key] = p.stdout
                return p.stdout
        except Exception as e:
            print(f"\033[1;33m[TTS CLI Fallback Error]\033[0m {e}")

    # Fallback to espeak-ng
    if shutil.which("espeak-ng"):
        try:
            temp_wav = f"/tmp/espeak_{uuid.uuid4().hex[:6]}.wav"
            subprocess.run(["espeak-ng", "-w", temp_wav, clean_text], timeout=15, check=True, capture_output=True)
            if os.path.exists(temp_wav):
                with open(temp_wav, "rb") as tf:
                    raw = tf.read()
                try:
                    os.remove(temp_wav)
                except Exception:
                    pass
                return raw
        except Exception as e:
            print(f"\033[1;33m[espeak-ng Fallback Error]\033[0m {e}")

    return b""


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
    speech_text = clean_speech_text(text) or text.strip()
    wav_bytes = synthesize_piper_wav_bytes(speech_text)
    if wav_bytes:
        try:
            with open(audio_path, "wb") as wf:
                wf.write(wav_bytes)
            notify_audio_saved(audio_path, text_path)
            return {
                "status": "saved",
                "audio_path": audio_path,
                "text_path": text_path,
                "size_bytes": len(wav_bytes),
                "text": text.strip(),
            }
        except Exception as e:
            print(f"\033[1;33m[Audio Write Error]\033[0m {e}")
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


_TTS_QUEUE = queue.Queue()

def _tts_worker_loop():
    while True:
        try:
            text = _TTS_QUEUE.get()
            if text is None:
                break
            save_speech_response(text)
        except Exception as e:
            print(f"\033[1;33m[TTS Queue Error]\033[0m {e}")
        finally:
            _TTS_QUEUE.task_done()

_TTS_WORKER_THREAD = threading.Thread(target=_tts_worker_loop, daemon=True)
_TTS_WORKER_THREAD.start()


def auto_archive_speech(text: str):
    """Automatically schedules background speech synthesis and archiving through a serialized queue."""
    if not AUDIO_AUTO_ARCHIVE or not text or not text.strip():
        return
    _TTS_QUEUE.put(text.strip())


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
    "stop_background_task": stop_background_task,
    "list_directory": list_directory,
    "get_system_telemetry": get_system_telemetry,
    "git_checkpoint": git_checkpoint,
    "git_rollback": git_rollback,
    "set_agent_memory": set_agent_memory,
    "get_agent_memory": get_agent_memory,
    "list_agent_memories": list_agent_memories,
    "web_search": web_search,
    "save_speech_response": save_speech_response,
    "search_mix_archive": search_mix_archive,
    "get_mix_tracklist": get_mix_tracklist,
    "find_track_in_mixes": find_track_in_mixes,
    "play_mix_or_audio": play_mix_or_audio,
    "get_mix_archive_stats": get_mix_archive_stats,
    "system_audio_volume": system_audio_volume,
    "search_agent_memories": search_agent_memories,
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

    if "query" not in kwargs:
        for alias in ("search", "q", "term", "mix_query", "track_query", "song", "artist"):
            if alias in kwargs:
                kwargs["query"] = kwargs[alias]
                break

    if "target" not in kwargs:
        for alias in ("mix", "track", "audio", "file", "filename", "mix_target"):
            if alias in kwargs:
                kwargs["target"] = kwargs[alias]
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
    ToolDescription(name="search_agent_memories", description="Searches persistent key-value facts using multi-term keyword overlap and ranking.", parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}),
    ToolDescription(name="web_search", description="Live web search via DuckDuckGo.", parameters={"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}}, "required": ["query"]}),
    ToolDescription(name="list_directory", description="Lists directory contents with file types, sizes in KB, and modified dates.", parameters={"type": "object", "properties": {"path": {"type": "string"}, "show_hidden": {"type": "boolean"}, "max_items": {"type": "integer"}}, "required": ["path"]}),
    ToolDescription(name="stop_background_task", description="Terminates a running background task process by its job_id.", parameters={"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]}),
    ToolDescription(name="save_speech_response", description="Synthesizes and saves the agent's speech response to an audio file (.wav) and companion transcript (.txt) in the archive directory.", parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}),
    ToolDescription(name="search_mix_archive", description="Searches user's Mix Archive for audio mixes, companion tracklists, and spectrograms.", parameters={"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}}, "required": ["query"]}),
    ToolDescription(name="get_mix_tracklist", description="Retrieves timestamped tracklist for a DJ mix from companion .txt, .cue, or Traktor .nml files.", parameters={"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]}),
    ToolDescription(name="find_track_in_mixes", description="Searches across all DJ mix tracklists (.txt, .cue, Traktor history) for a specific song title, remix, or artist.", parameters={"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}}, "required": ["query"]}),
    ToolDescription(name="play_mix_or_audio", description="Controls DJ playback or plays an audio mix (using MP Audio Player). Commands: 'play', 'pause', 'toggle', 'stop', 'latest', 'status'. Target: filepath, keyword, or 'latest'.", parameters={"type": "object", "properties": {"target": {"type": "string"}, "command": {"type": "string"}}, "required": []}),
    ToolDescription(name="get_mix_archive_stats", description="Returns statistics about the user's Mix Archive (counts by format, total GB, and latest 3 mixes).", parameters={"type": "object", "properties": {}}),
    ToolDescription(name="system_audio_volume", description="Inspects or controls host audio volume via PipeWire / WirePlumber. Actions: 'status', 'set' (with level 0-100), 'toggle_mute'.", parameters={"type": "object", "properties": {"action": {"type": "string"}, "level": {"type": "integer"}}, "required": ["action"]}),
]

# ---------------------------------------------------------------------------
# 3. Model Inspection, Encodings & Live Streaming Pipeline
# ---------------------------------------------------------------------------
def detect_ollama_model() -> str:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        return args[0].strip("'\" ;\t\r\n")
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
    if current <= PRUNE_THRESHOLD:
        return current

    pinned = 2
    pruned = 0
    excess = current - PRUNE_THRESHOLD
    est_to_drop = max(2, min(len(convo.messages) - (pinned + 2), excess // 160))
    for _ in range(est_to_drop):
        if len(convo.messages) > (pinned + 2):
            convo.messages.pop(pinned)
            pruned += 1

    tokens = enc.render_conversation_for_completion(convo, Role.ASSISTANT)
    current = len(tokens)

    while current > PRUNE_THRESHOLD and len(convo.messages) > (pinned + 2):
        convo.messages.pop(pinned)
        pruned += 1
        tokens = enc.render_conversation_for_completion(convo, Role.ASSISTANT)
        current = len(tokens)

    if pruned:
        print(f"\033[1;33m[Context Window]\033[0m Pruned {pruned} messages. Active tokens: {current:,}")
    return current


def stream_ollama_with_callback(prompt_text: str, model_name: str, on_token=None, stop_event: threading.Event = None) -> str:
    """Streams token chunks live from Ollama with persistent connection pooling and loop-break guardrail."""
    full = []
    recent_sliding = []
    
    with _OLLAMA_CLIENT.stream(
        "POST",
        "/api/generate",
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

    dev_content = (
        DeveloperContent.new()
        .with_instructions(
            "You are an autonomous engineering, local administration, and DJ Mix Studio AI assistant. "
            "Invoke your tools to inspect files, execute bash commands, monitor telemetry, manage jobs, or control music. "
            "Tools include: `list_directory` for directory contents, `read_file_lines`/`search_file_regex` for files, "
            "`start_background_task`/`stop_background_task` for background jobs, `git_checkpoint` for git repositories, "
            "`search_agent_memories`/`set_agent_memory` for persistent facts, "
            "`search_mix_archive`, `get_mix_tracklist`, `find_track_in_mixes`, `play_mix_or_audio`, `system_audio_volume` for music studio operations. "
            "When speaking via voice in the final channel, reply conversationally without raw markdown syntax."
        )
        .with_function_tools(tool_schemas)
    )

    convo = Conversation.from_messages([
        Message.from_role_and_content(Role.SYSTEM, sys_content),
        Message.from_role_and_content(Role.DEVELOPER, dev_content),
    ])
    return enc, convo


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 4. Web HUD Dashboard & OpenAI Bridge Server
# ---------------------------------------------------------------------------
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MP Harmony AI Agent • Engineering & Music Hub</title>
<style>
  :root {
    --bg: #090d16;
    --card: #111827;
    --card-border: #1f293d;
    --text: #f1f5f9;
    --muted: #94a3b8;
    --accent: #38bdf8;
    --green: #10b981;
    --magenta: #d946ef;
    --yellow: #f59e0b;
    --red: #ef4444;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    padding: 24px;
    line-height: 1.5;
  }
  .container { max-width: 1280px; margin: 0 auto; }
  header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 16px;
    padding-bottom: 20px;
    border-bottom: 1px solid var(--card-border);
    margin-bottom: 24px;
  }
  .title-group { display: flex; align-items: center; gap: 14px; }
  .logo {
    width: 42px; height: 42px; background: linear-gradient(135deg, var(--accent), var(--magenta));
    border-radius: 10px; display: flex; align-items: center; justify-content: center;
    font-weight: 900; font-size: 20px; color: #fff; box-shadow: 0 0 16px rgba(56, 189, 248, 0.4);
  }
  h1 { font-size: 22px; font-weight: 700; letter-spacing: -0.5px; }
  .badges { display: flex; gap: 8px; flex-wrap: wrap; }
  .badge {
    font-size: 12px; font-weight: 600; padding: 4px 10px; border-radius: 9999px;
    display: inline-flex; align-items: center; gap: 6px;
  }
  .badge-online { background: rgba(16, 185, 129, 0.15); color: var(--green); border: 1px solid rgba(16, 185, 129, 0.3); }
  .badge-model { background: rgba(56, 189, 248, 0.15); color: var(--accent); border: 1px solid rgba(56, 189, 248, 0.3); }
  .badge-port { background: rgba(217, 70, 239, 0.15); color: var(--magenta); border: 1px solid rgba(217, 70, 239, 0.3); }

  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 20px; margin-bottom: 24px; }
  .card {
    background: var(--card); border: 1px solid var(--card-border);
    border-radius: 12px; padding: 20px; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.2);
  }
  .card h2 { font-size: 15px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: var(--muted); margin-bottom: 16px; display: flex; justify-content: space-between; align-items: center; }
  .telemetry-row { display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid rgba(255, 255, 255, 0.05); font-size: 14px; }
  .telemetry-row:last-child { border-bottom: none; }
  .val { font-weight: 600; color: #fff; font-family: monospace; }

  /* Audio player card */
  .audio-item {
    background: rgba(255, 255, 255, 0.03); border: 1px solid var(--card-border);
    border-radius: 8px; padding: 12px; margin-bottom: 12px;
  }
  .audio-header { display: flex; justify-content: space-between; font-size: 12px; color: var(--muted); margin-bottom: 6px; }
  .audio-transcript { font-size: 13px; color: var(--text); margin-bottom: 8px; font-style: italic; }
  audio { width: 100%; height: 32px; outline: none; }

  /* Chat Sandbox */
  .chat-box {
    display: flex; flex-direction: column; height: 460px;
  }
  .chat-output {
    flex: 1; background: #06090e; border: 1px solid var(--card-border); border-radius: 8px;
    padding: 14px; overflow-y: auto; font-size: 14px; font-family: monospace; line-height: 1.6;
    white-space: pre-wrap; word-break: break-word; color: #e2e8f0; margin-bottom: 12px;
  }
  .input-bar { display: flex; gap: 8px; }
  textarea {
    flex: 1; background: #06090e; border: 1px solid var(--card-border); border-radius: 8px;
    color: #fff; padding: 10px 14px; font-size: 14px; font-family: inherit; resize: none; height: 50px; outline: none;
  }
  textarea:focus { border-color: var(--accent); }
  button {
    background: var(--accent); color: #000; border: none; font-weight: 600; border-radius: 8px;
    padding: 0 18px; cursor: pointer; transition: opacity 0.15s; font-size: 14px;
  }
  button:hover { opacity: 0.9; }
  button.btn-speak { background: var(--magenta); color: #fff; margin-left: 6px; }
  button.btn-stop { background: var(--red); color: #fff; }
  .actions { display: flex; justify-content: space-between; align-items: center; margin-top: 8px; font-size: 13px; color: var(--muted); }
  .quick-chips { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
  .chip { background: rgba(56, 189, 248, 0.1); color: var(--accent); border: 1px solid rgba(56, 189, 248, 0.2); padding: 3px 8px; border-radius: 6px; font-size: 11px; cursor: pointer; }
  .chip:hover { background: rgba(56, 189, 248, 0.2); }
</style>
</head>
<body>
<div class="container">
  <header>
    <div class="title-group">
      <div class="logo">H</div>
      <div>
        <h1>MP Harmony AI Agent</h1>
        <p style="font-size: 13px; color: var(--muted);">Autonomous Engineering & DJ Studio Engine</p>
      </div>
    </div>
    <div class="badges">
      <span class="badge badge-online">● ONLINE</span>
      <span class="badge badge-port">PORT: 11435</span>
      <span class="badge badge-model" id="model-badge">MODEL: gpt-oss-pinned</span>
    </div>
  </header>

  <div class="grid">
    <!-- Telemetry Card -->
    <div class="card">
      <h2>Host & Hardware Telemetry <span id="refresh-indicator" style="font-size: 11px; color: var(--accent);">● LIVE</span></h2>
      <div class="telemetry-row"><span>Load Average (1, 5, 15m)</span><span class="val" id="val-load">--</span></div>
      <div class="telemetry-row"><span>Host Memory (RAM)</span><span class="val" id="val-mem">--</span></div>
      <div class="telemetry-row"><span>GPU Hardware</span><span class="val" id="val-gpu-model">--</span></div>
      <div class="telemetry-row"><span>GPU VRAM</span><span class="val" id="val-gpu-vram">--</span></div>
      <div class="telemetry-row"><span>GPU Utilization</span><span class="val" id="val-gpu-util">--</span></div>
      <div class="telemetry-row"><span>GPU Temperature</span><span class="val" id="val-gpu-temp">--</span></div>
      <div class="telemetry-row"><span>Audio Sink Volume</span><span class="val" id="val-audio-vol">--</span></div>
    </div>

    <!-- DJ Mix Studio Card -->
    <div class="card">
      <h2>DJ Mix Archive Status</h2>
      <div class="telemetry-row"><span>Archive Roots</span><span class="val" id="val-mix-roots">--</span></div>
      <div class="telemetry-row"><span>Total Audio Files</span><span class="val" id="val-mix-total">--</span></div>
      <div class="telemetry-row"><span>Total Storage</span><span class="val" id="val-mix-gb">--</span></div>
      <div class="telemetry-row"><span>Formats</span><span class="val" id="val-mix-formats">--</span></div>
      <div style="margin-top: 14px; font-size: 13px; font-weight: 600; color: var(--muted);">Latest Mixes:</div>
      <div id="latest-mixes-list" style="margin-top: 6px; font-size: 12px; color: var(--text);"></div>
    </div>
  </div>

  <div class="grid">
    <!-- Chat Sandbox Card -->
    <div class="card" style="grid-column: span 1;">
      <h2>Interactive Chat & Voice Sandbox</h2>
      <div class="chat-box">
        <div class="chat-output" id="chat-output">Ready. Type a prompt or click a quick suggestion below...</div>
        <div class="input-bar">
          <textarea id="prompt-input" placeholder="Ask MP Harmony Agent anything (e.g. check tracklist, audio volume, telemetry)..."></textarea>
          <button id="send-btn" onclick="sendChat()">Send</button>
          <button id="abort-btn" class="btn-stop" style="display:none;" onclick="abortChat()">Abort</button>
        </div>
        <div class="actions">
          <label style="display:flex; align-items:center; gap:6px; cursor:pointer;">
            <input type="checkbox" id="stream-toggle" checked> Stream Response (SSE)
          </label>
          <button class="btn-speak" id="speak-btn" style="padding:4px 12px; font-size:12px;" onclick="speakResponse()">🔊 Speak</button>
        </div>
        <div class="quick-chips">
          <span class="chip" onclick="quickPrompt('What is the current system audio volume? Use your tool.')">Audio Volume</span>
          <span class="chip" onclick="quickPrompt('Get mix archive stats')">Mix Archive Stats</span>
          <span class="chip" onclick="quickPrompt('Find track in mixes for Dreamy')">Find 'Dreamy' Tracks</span>
          <span class="chip" onclick="quickPrompt('Get system telemetry')">Telemetry</span>
        </div>
      </div>
    </div>

    <!-- Recent Speech Card -->
    <div class="card" style="grid-column: span 1;">
      <h2>Audio Speech Archive <button style="background:transparent; color:var(--accent); font-size:12px; padding:0;" onclick="loadRecentAudio()">↻ Refresh</button></h2>
      <div id="recent-audio-container" style="max-height: 420px; overflow-y: auto;">
        <p style="color:var(--muted); font-size:13px;">Loading recent voice responses...</p>
      </div>
    </div>
  </div>
</div>

<script>
  let lastAssistantReply = "";
  let activeAbortController = null;

  async function fetchTelemetry() {
    try {
      const res = await fetch('/api/telemetry');
      const data = await res.json();
      if (data.load_avg_1_5_15m) {
        document.getElementById('val-load').textContent = data.load_avg_1_5_15m.join(', ');
      }
      if (data.memory && data.memory[1]) {
        document.getElementById('val-mem').textContent = data.memory[1].replace(/\\s+/g, ' ');
      }
      if (data.gpu) {
        document.getElementById('val-gpu-model').textContent = data.gpu.model || '--';
        document.getElementById('val-gpu-vram').textContent = (data.gpu.vram_used || '') + ' / ' + (data.gpu.vram_total || '');
        document.getElementById('val-gpu-util').textContent = data.gpu.utilization || '--';
        document.getElementById('val-gpu-temp').textContent = data.gpu.temp || '--';
      }
    } catch(e){}
  }

  async function fetchMixStats() {
    try {
      const res = await fetch('/api/mix_stats');
      const data = await res.json();
      if (data.archive_roots) {
        document.getElementById('val-mix-roots').textContent = data.archive_roots.length + ' configured root(s)';
      }
      if (data.total_audio_files) {
        document.getElementById('val-mix-total').textContent = data.total_audio_files.toLocaleString() + ' files';
      }
      if (data.total_storage_gb) {
        document.getElementById('val-mix-gb').textContent = data.total_storage_gb + ' GB';
      }
      if (data.formats) {
        document.getElementById('val-mix-formats').textContent = Object.entries(data.formats).map(([k,v]) => k.toUpperCase() + ':' + v).join(' | ');
      }
      if (data.latest_mixes && data.latest_mixes.length) {
        document.getElementById('latest-mixes-list').innerHTML = data.latest_mixes.map(m =>
          '<div style="padding:4px 0; border-bottom:1px solid rgba(255,255,255,0.05);"><b>' + m.title + '</b> (' + m.size_mb + ' MB) <span style="color:var(--muted);">' + m.date + '</span></div>'
        ).join('');
      }
    } catch(e){}
  }

  async function loadRecentAudio() {
    const container = document.getElementById('recent-audio-container');
    try {
      const res = await fetch('/api/recent_audio');
      const data = await res.json();
      if (!data.audio_responses || !data.audio_responses.length) {
        container.innerHTML = '<p style="color:var(--muted); font-size:13px;">No recorded audio files found.</p>';
        return;
      }
      container.innerHTML = data.audio_responses.map(item => `
        <div class="audio-item">
          <div class="audio-header">
            <span>${item.filename}</span>
            <span>${item.mtime}</span>
          </div>
          <div class="audio-transcript">${item.transcript ? '"' + item.transcript + '"' : '(No transcript)'}</div>
          <audio controls preload="none" src="/audio/${item.filename}"></audio>
        </div>
      `).join('');
    } catch(e) {
      container.innerHTML = '<p style="color:var(--red); font-size:13px;">Error loading audio archive.</p>';
    }
  }

  function quickPrompt(text) {
    document.getElementById('prompt-input').value = text;
    sendChat();
  }

  async function sendChat() {
    const input = document.getElementById('prompt-input');
    const text = input.value.trim();
    if (!text) return;

    const out = document.getElementById('chat-output');
    out.textContent = "Agent thinking & generating...\\n";
    lastAssistantReply = "";

    const stream = document.getElementById('stream-toggle').checked;
    document.getElementById('send-btn').style.display = 'none';
    document.getElementById('abort-btn').style.display = 'inline-block';

    activeAbortController = new AbortController();

    try {
      const resp = await fetch('/v1/chat/completions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Session-Id': 'web-hud' },
        body: JSON.stringify({
          model: 'gpt-oss-pinned:latest',
          messages: [{ role: 'user', content: text }],
          stream: stream
        }),
        signal: activeAbortController.signal
      });

      if (stream) {
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        out.textContent = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          const chunk = decoder.decode(value, { stream: true });
          const lines = chunk.split('\\n');
          for (const line of lines) {
            if (line.startsWith('data: ') && !line.includes('[DONE]')) {
              try {
                const parsed = JSON.parse(line.substring(6));
                const delta = parsed.choices?.[0]?.delta?.content || "";
                out.textContent += delta;
                lastAssistantReply += delta;
                out.scrollTop = out.scrollHeight;
              } catch(e){}
            }
          }
        }
      } else {
        const data = await resp.json();
        const content = data.choices?.[0]?.message?.content || JSON.stringify(data, null, 2);
        out.textContent = content;
        lastAssistantReply = content;
      }
    } catch (e) {
      if (e.name !== 'AbortError') {
        out.textContent += "\\n[Error: " + e.message + "]";
      } else {
        out.textContent += "\\n[Interrupted by user]";
      }
    } finally {
      document.getElementById('send-btn').style.display = 'inline-block';
      document.getElementById('abort-btn').style.display = 'none';
      loadRecentAudio();
    }
  }

  async function abortChat() {
    if (activeAbortController) activeAbortController.abort();
    fetch('/v1/abort', { method: 'POST' });
  }

  async function speakResponse() {
    if (!lastAssistantReply) return;
    try {
      const res = await fetch('/v1/audio/speech', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ input: lastAssistantReply, voice: 'amy', speed: 1.0 })
      });
      const blob = await res.blob();
      const audioUrl = URL.createObjectURL(blob);
      const audio = new Audio(audioUrl);
      audio.play();
    } catch(e) {
      alert("TTS Error: " + e);
    }
  }

  document.getElementById('prompt-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendChat();
    }
  });

  fetchTelemetry();
  fetchMixStats();
  loadRecentAudio();
  setInterval(fetchTelemetry, 5000);
</script>
</body>
</html>
"""

GLOBAL_STATE = {
    "sessions": {},  # session_id -> {"convo": Conversation, "last_used": float, "lock": threading.Lock()}
    "global_lock": threading.Lock(),
    "abort_event": threading.Event(),
    "model_name": "gpt-oss-pinned:latest",
    "enc": None,
    "convo": None,
}


def get_session_data(session_id: str, incoming_messages: list = None) -> dict:
    """Retrieves or creates an isolated conversation session with multi-turn history reconciliation."""
    with GLOBAL_STATE["global_lock"]:
        now = time.time()
        # Evict inactive sessions (>24 hours)
        stale = [s for s, data in GLOBAL_STATE["sessions"].items() if now - data.get("last_used", 0) > 86400]
        for s in stale:
            del GLOBAL_STATE["sessions"][s]

        if session_id not in GLOBAL_STATE["sessions"]:
            _, new_convo = init_agent()
            GLOBAL_STATE["sessions"][session_id] = {
                "convo": new_convo,
                "last_used": now,
                "lock": threading.Lock(),
            }

        sess = GLOBAL_STATE["sessions"][session_id]
        sess["last_used"] = now
        convo = sess["convo"]

        # Reconcile if client started a fresh chat thread with 1 message
        if incoming_messages and len(incoming_messages) == 1 and incoming_messages[0].get("role") == "user":
            user_turn_count = len([m for m in convo.messages if getattr(m, "role", None) == Role.USER])
            if user_turn_count > 1:
                _, fresh_convo = init_agent()
                sess["convo"] = fresh_convo

        return sess


class HarmonyBridgeHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        self.write_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):
        pass

    def send_cors_headers(self):
        """Injects cross-origin resource sharing headers for Open WebUI & web harnesses."""
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With, X-Session-Id, X-Conversation-Id")
        self.send_header("Access-Control-Max-Age", "86400")

    def do_OPTIONS(self):
        """Handles browser pre-flight CORS verification requests."""
        self.send_response(200)
        self.send_cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        """Handles OpenAI model discovery, Web HUD dashboard, static audio serving, and health checks."""
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path.rstrip("/")
        if not path:
            path = "/"

        # 1. Static Audio File Serving: /audio/<filename>
        if path.startswith("/audio/"):
            fname = os.path.basename(path.split("/audio/", 1)[1])
            fpath = os.path.join(AUDIO_OUTPUT_DIR, fname)
            if os.path.isfile(fpath) and os.path.exists(fpath):
                content_type = "audio/wav" if fname.endswith(".wav") else "text/plain"
                try:
                    with open(fpath, "rb") as f:
                        data_bytes = f.read()
                    self.send_response(200)
                    self.send_cors_headers()
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(data_bytes)))
                    self.end_headers()
                    self.wfile.write(data_bytes)
                    return
                except Exception:
                    pass
            self.send_response(404)
            self.send_cors_headers()
            self.end_headers()
            return

        # 2. Telemetry API: /api/telemetry
        if path == "/api/telemetry":
            body = json.dumps(get_system_telemetry()).encode("utf-8")
            self.send_response(200)
            self.send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # 3. Recent Audio Responses API: /api/recent_audio
        if path == "/api/recent_audio":
            recent = []
            if os.path.exists(AUDIO_OUTPUT_DIR):
                wavs = [f for f in os.listdir(AUDIO_OUTPUT_DIR) if f.endswith(".wav")]
                wavs.sort(key=lambda x: os.path.getmtime(os.path.join(AUDIO_OUTPUT_DIR, x)), reverse=True)
                for w in wavs[:10]:
                    full_w = os.path.join(AUDIO_OUTPUT_DIR, w)
                    txt_w = full_w[:-4] + ".txt"
                    transcript = ""
                    if os.path.exists(txt_w):
                        try:
                            with open(txt_w, "r", encoding="utf-8", errors="replace") as tf:
                                transcript = tf.read().strip()
                        except Exception:
                            pass
                    recent.append({
                        "filename": w,
                        "size_bytes": os.path.getsize(full_w),
                        "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(full_w))),
                        "transcript": transcript[:200] + ("..." if len(transcript) > 200 else ""),
                    })
            body = json.dumps({"audio_responses": recent}).encode("utf-8")
            self.send_response(200)
            self.send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # 4. Mix Stats API: /api/mix_stats
        if path == "/api/mix_stats":
            body = json.dumps(get_mix_archive_stats()).encode("utf-8")
            self.send_response(200)
            self.send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # 5. OpenAI Model Discovery: /v1/models or /v1/models/<id>
        if path in ("/v1/models", "/models") or path.startswith(("/v1/models/", "/models/")):
            model_name = GLOBAL_STATE.get("model_name", "gpt-oss-pinned:latest")
            models_data = [
                {
                    "id": "mp-harmony-agent",
                    "name": "MP Harmony AI Agent",
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "mp-harmony",
                    "permission": [],
                    "root": "mp-harmony-agent",
                    "parent": None,
                },
                {
                    "id": model_name,
                    "name": f"MP Harmony ({model_name})",
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "mp-harmony",
                    "permission": [],
                    "root": model_name,
                    "parent": None,
                },
            ]
            if path.startswith(("/v1/models/", "/models/")):
                req_id = path.split("/models/", 1)[1]
                matched = next((m for m in models_data if m["id"] == req_id), models_data[0])
                body = json.dumps(matched).encode("utf-8")
            else:
                resp = {
                    "object": "list",
                    "data": models_data,
                }
                body = json.dumps(resp).encode("utf-8")
            self.send_response(200)
            self.send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # 6. Raw Status JSON: /status, /health
        if path in ("/health", "/status"):
            model_name = GLOBAL_STATE.get("model_name", "gpt-oss-pinned:latest")
            resp = {
                "status": "online",
                "agent": "MP Harmony AI Agent",
                "model": model_name,
                "port": AGENT_PORT,
                "tools_count": len(AVAILABLE_TOOLS),
                "active_sessions": len(GLOBAL_STATE.get("sessions", {})),
            }
            body = json.dumps(resp, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # 7. Web HUD & Diagnostic Dashboard: / or /dashboard or /hud
        if path in ("/", "/dashboard", "/hud"):
            accept = self.headers.get("Accept", "")
            if "application/json" in accept and "text/html" not in accept:
                model_name = GLOBAL_STATE.get("model_name", "gpt-oss-pinned:latest")
                resp = {
                    "status": "online",
                    "agent": "MP Harmony AI Agent",
                    "model": model_name,
                    "port": AGENT_PORT,
                    "tools_count": len(AVAILABLE_TOOLS),
                }
                body = json.dumps(resp, indent=2).encode("utf-8")
                self.send_response(200)
                self.send_cors_headers()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            body = DASHBOARD_HTML.encode("utf-8")
            self.send_response(200)
            self.send_cors_headers()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.send_cors_headers()
        self.send_header("Content-Type", "application/json")
        body = b'{"error": "Endpoint not found"}'
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
                    "created": int(time.time()),
                    "model": getattr(self, "current_model", "mp-harmony-agent"),
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
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path.rstrip("/")

        # 1. Voice Interruption / Barge-in Endpoint
        if path == "/v1/abort":
            GLOBAL_STATE["abort_event"].set()
            self.send_response(200)
            self.send_cors_headers()
            self.send_header("Content-Type", "application/json")
            body = b'{"status":"aborted"}'
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass
            print("\033[1;31m[Barge-In]\033[0m Abort signal triggered via /v1/abort.")
            return

        # 2. OpenAI Speech Endpoint: /v1/audio/speech
        if path == "/v1/audio/speech":
            content_length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
            input_text = data.get("input", "")
            voice_param = data.get("voice", "amy")
            speed_param = float(data.get("speed", 1.0))

            wav_bytes = synthesize_piper_wav_bytes(input_text, voice_name=voice_param, speed=speed_param)
            if not wav_bytes:
                self.send_response(500)
                self.send_cors_headers()
                self.send_header("Content-Type", "application/json")
                body = b'{"error":"TTS synthesis failed"}'
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except Exception:
                    pass
                return

            self.send_response(200)
            self.send_cors_headers()
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(wav_bytes)))
            self.end_headers()
            self.wfile.write(wav_bytes)
            return

        if path not in ("/v1/chat/completions", "/chat/completions"):
            self.send_response(404)
            self.send_cors_headers()
            self.send_header("Content-Type", "application/json")
            body = b'{"error": "Endpoint not found"}'
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass
            return

        GLOBAL_STATE["abort_event"].clear()
        content_length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(content_length).decode("utf-8"))

        is_stream = bool(data.get("stream", True))

        messages = data.get("messages", [])
        last_user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user_msg = m.get("content", "").strip()
                break

        if not last_user_msg:
            self.send_response(400)
            self.send_cors_headers()
            self.send_header("Content-Type", "application/json")
            body = b'{"error":"No user message provided"}'
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass
            return

        # Resolve isolated session
        session_id = (
            self.headers.get("X-Session-Id")
            or self.headers.get("X-Conversation-Id")
            or data.get("user")
            or "default"
        )
        sess = get_session_data(session_id, messages)
        convo = sess["convo"]
        sess_lock = sess["lock"]

        print(f"\n\033[1;35m[Input Received]\033[0m [Session: {session_id}] {last_user_msg} (stream={is_stream})")

        enc = GLOBAL_STATE["enc"]
        model_name = GLOBAL_STATE["model_name"]
        self.current_model = data.get("model", "mp-harmony-agent")

        # Voice session reset trigger
        if last_user_msg.lower() in ("/reset", "reset", "clear session", "reset context"):
            with sess_lock:
                new_enc, new_convo = init_agent()
                sess["convo"] = new_convo
            if is_stream:
                self.send_response(200)
                self.send_cors_headers()
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                self.send_sse_chunk("Session memory cleared to baseline.")
                try:
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except Exception:
                    pass
                self.close_connection = True
            else:
                resp = {
                    "id": "chatcmpl-reset",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model_name,
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": "Session memory cleared to baseline."},
                        "finish_reason": "stop"
                    }],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                }
                body = json.dumps(resp).encode("utf-8")
                self.send_response(200)
                self.send_cors_headers()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            return

        stop_heartbeat = threading.Event()
        heartbeat_thread = None

        if is_stream:
            self.send_response(200)
            self.send_cors_headers()
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            def heartbeat_worker():
                while not stop_heartbeat.wait(4.0):
                    if not self.send_sse_ping():
                        GLOBAL_STATE["abort_event"].set()
                        break

            heartbeat_thread = threading.Thread(target=heartbeat_worker, daemon=True)
            heartbeat_thread.start()

        try:
            with sess_lock:
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

                    raw_token_window = []
                    final_channel_active = [False]

                    def live_token_callback(piece: str):
                        nonlocal client_alive
                        if not client_alive or GLOBAL_STATE["abort_event"].is_set():
                            return

                        raw_token_window.append(piece)

                        if not final_channel_active[0]:
                            window = "".join(raw_token_window[-12:])
                            if "final<|message|>" in window or ("final" in window and "<|message|>" in piece):
                                final_channel_active[0] = True
                                if "<|message|>" in piece:
                                    piece = piece.split("<|message|>", 1)[1]
                                else:
                                    return
                            else:
                                return

                        if "<|end|>" in piece or "<|call|>" in piece or "<|start|>" in piece:
                            final_channel_active[0] = False
                            piece = re.split(r"<\|(?:end|call|start)\|>", piece)[0]

                        clean_piece = re.sub(r"<\|.*?\|>", "", piece)
                        if clean_piece:
                            accumulated_speech_chunks.append(clean_piece)
                            if is_stream:
                                if not self.send_sse_chunk(clean_piece):
                                    client_alive = False
                                    GLOBAL_STATE["abort_event"].set()

                    raw_response = stream_ollama_with_callback(
                        prompt_text,
                        model_name,
                        on_token=live_token_callback,
                        stop_event=GLOBAL_STATE["abort_event"],
                    )

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

                if client_alive and not GLOBAL_STATE["abort_event"].is_set() and not final_channel_active[0] and not accumulated_speech_chunks:
                    convo.messages.append(Message.from_role_and_content(
                        Role.DEVELOPER,
                        DeveloperContent.new().with_instructions(
                            "Maximum tool execution steps reached. You MUST now summarize your actions, results, and findings directly in the final channel for the user."
                        )
                    ))
                    prompt_tokens = enc.render_conversation_for_completion(convo, Role.ASSISTANT)
                    prompt_text = enc.decode(prompt_tokens)
                    print("\n\033[1;35m[Final Summary Step] Generating reply...\033[0m")
                    raw_response = stream_ollama_with_callback(
                        prompt_text,
                        model_name,
                        on_token=live_token_callback,
                        stop_event=GLOBAL_STATE["abort_event"],
                    )
                    resp_tokens = enc.encode(raw_response, allowed_special="all")
                    parsed_messages = enc.parse_messages_from_completion_tokens(resp_tokens, role=Role.ASSISTANT)
                    convo.messages.extend(parsed_messages)

                speech_to_save = "".join(accumulated_speech_chunks).strip()
                if not speech_to_save:
                    speech_parts = []
                    for msg in parsed_messages:
                        if getattr(msg, "channel", None) == "final":
                            pt = extract_content_text(msg).strip()
                            if pt:
                                speech_parts.append(pt)
                    speech_to_save = "\n\n".join(speech_parts).strip()
                if not speech_to_save:
                    for msg in reversed(parsed_messages):
                        if getattr(msg, "role", None) == Role.ASSISTANT:
                            pt = extract_content_text(msg).strip()
                            if pt:
                                speech_to_save = pt
                                break

                # Ensure stream gets the speech if live tokens were missed by edge parser
                if is_stream and not accumulated_speech_chunks and speech_to_save and client_alive and not GLOBAL_STATE["abort_event"].is_set():
                    self.send_sse_chunk(speech_to_save)

                if speech_to_save and not GLOBAL_STATE["abort_event"].is_set():
                    auto_archive_speech(speech_to_save)

                if not is_stream:
                    resp_json = {
                        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": getattr(self, "current_model", model_name),
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": speech_to_save,
                                },
                                "finish_reason": "stop"
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "total_tokens": 0,
                        }
                    }
                    body = json.dumps(resp_json).encode("utf-8")
                    self.send_response(200)
                    self.send_cors_headers()
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

        except Exception as e:
            print(f"\n\033[1;31m[Agent Error]\033[0m {e}")
            if is_stream:
                self.send_sse_chunk(f"An error occurred: {e}")
            else:
                err_resp = {"error": {"message": str(e), "type": "agent_error"}}
                body = json.dumps(err_resp).encode("utf-8")
                self.send_response(500)
                self.send_cors_headers()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except Exception:
                    pass
        finally:
            if is_stream:
                stop_heartbeat.set()
                if heartbeat_thread is not None:
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