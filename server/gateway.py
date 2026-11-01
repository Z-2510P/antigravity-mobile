#!/usr/bin/env python3
"""
Antigravity Mobile Sync Gateway
===============================
Zero-dependency Python 3.13 asyncio HTTP + WebSocket server that bridges
Antigravity Desktop (macOS) with the Antigravity Android App over LAN / Tailscale.

Features:
- 100% real-time conversation & message sync via SQLite (`conversation_summaries.db`)
  and JSONL transcripts (`brain/<id>/.system_generated/logs/transcript.jsonl`).
- Full-text search via SQLite FTS5 (`conversations_fts`).
- Automatic resolution of truncated fields via `transcript_full.jsonl`.
- Live Auxiliary Pane data: Artifacts (`*.md`, images), Subagents, Background Tasks.
- Dual Execution Engine:
  1. `agentapi` (Desktop Live Sync — injects messages directly into the running
     Antigravity 2.0 Desktop App language_server).
  2. `agy` CLI (Custom model/effort/mode selection & standalone execution).
"""

import asyncio
import base64
import contextvars
import gzip
import hashlib
import json
import mimetypes
import os
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Paths & Configuration
# ---------------------------------------------------------------------------
HOME = Path.home()
APP_DATA_DIR = Path(os.environ.get("ANTIGRAVITY_APP_DATA_DIR", str(HOME / ".gemini" / "antigravity")))
CONFIG_DIR = HOME / ".gemini" / "config"
PROJECTS_DIR = CONFIG_DIR / "projects"
BRAIN_DIR = APP_DATA_DIR / "brain"
DB_PATH = APP_DATA_DIR / "conversation_summaries.db"
AGENTAPI_BIN = APP_DATA_DIR / "bin" / "agentapi"
AGY_BIN = Path("/opt/homebrew/bin/agy")
TAILSCALE_BIN = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")

SERVER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SERVER_DIR.parent
WEB_DIR = SERVER_DIR / "web"
DIST_DIR = PROJECT_ROOT / "dist"
LS_ENV_FILE = SERVER_DIR / ".ls_env.json"


def _load_dotenv() -> None:
    """Load optional .env file from project root without external dependencies."""
    env_file = PROJECT_ROOT / ".env"
    if not env_file.is_file():
        return
    try:
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'\"")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


_load_dotenv()

PORT = int(os.environ.get("AGY_MOBILE_PORT", "8899"))
HOST = os.environ.get("AGY_MOBILE_HOST", "0.0.0.0")
VPS_SSH_HOST = os.environ.get("AGY_VPS_SSH_HOST", "").strip()
VPS_PUBLIC_IP = os.environ.get("AGY_VPS_PUBLIC_IP", "").strip()
VPS_PUBLIC_URL = os.environ.get("AGY_VPS_PUBLIC_URL", f"https://{VPS_PUBLIC_IP}" if VPS_PUBLIC_IP else "").strip()
VPS_TUNNEL_CONNECTED = False

HIDDEN_CONVERSATION_IDS: Set[str] = {
    cid.strip()
    for cid in os.environ.get("AGY_HIDDEN_CONV_IDS", "").split(",")
    if cid.strip()
}

AVAILABLE_MODELS = [
    {"id": "gemini-4-argon-high", "name": "Gemini 4 Argon (High)", "tier": "pro", "enum": "MODEL_PLACEHOLDER_M417"},
    {"id": "gemini-4-argon-medium", "name": "Gemini 4 Argon (Medium)", "tier": "pro", "enum": "MODEL_PLACEHOLDER_M418"},
    {"id": "gemini-4-argon-low", "name": "Gemini 4 Argon (Low)", "tier": "pro", "enum": "MODEL_PLACEHOLDER_M419"},
    {"id": "gemini-3.8-flash-high", "name": "Gemini 3.8 Flash (High)", "tier": "flash", "enum": "MODEL_PLACEHOLDER_M318"},
    {"id": "gemini-3.8-flash-medium", "name": "Gemini 3.8 Flash (Medium)", "tier": "flash", "enum": "MODEL_PLACEHOLDER_M319"},
    {"id": "gemini-3.8-flash-low", "name": "Gemini 3.8 Flash (Low)", "tier": "flash_lite", "enum": "MODEL_PLACEHOLDER_M320"},
    {"id": "claude-opus-4-6-thinking", "name": "Claude Opus 4.6 (Thinking)", "tier": "pro", "enum": "MODEL_PLACEHOLDER_M26"},
    {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6 (Thinking)", "tier": "pro", "enum": "MODEL_PLACEHOLDER_M35"},
    {"id": "gpt-oss-120b-medium", "name": "GPT-OSS 120B (Medium)", "tier": "pro", "enum": "MODEL_OPENAI_GPT_OSS_120B_MEDIUM"},
]


# ---------------------------------------------------------------------------
# Language Server Environment Sync
# ---------------------------------------------------------------------------
_CACHED_LS_ENV: Dict[str, str] = {}
_LAST_LS_DISCOVER_TS: float = 0.0


def discover_live_ls_environment() -> Dict[str, str]:
    """Auto-discover the active Antigravity Desktop language_server port & CSRF token via ps + lsof."""
    try:
        out = subprocess.check_output(["ps", "-ax", "-o", "pid=,command="], text=True, timeout=2.0)
        for line in out.splitlines():
            if "Resources/bin/language_server" in line and "--csrf_token" in line:
                m_pid = re.match(r"^\s*(\d+)", line)
                m_tok = re.search(r"--csrf_token\s+([a-fA-F0-9-]+)", line)
                if not (m_pid and m_tok):
                    continue
                pid = m_pid.group(1)
                tok = m_tok.group(1)
                lsof_out = subprocess.check_output(
                    ["/usr/sbin/lsof", "-Pan", "-p", pid, "-iTCP", "-sTCP:LISTEN"],
                    text=True,
                    timeout=2.0,
                )
                ports = re.findall(r"127\.0\.0\.1:(\d+)\s+\(LISTEN\)", lsof_out)
                for port in sorted(set(ports), key=lambda x: int(x), reverse=True):
                    addr = f"localhost:{port}"
                    try:
                        req = urllib.request.Request(
                            f"http://{addr}/exa.language_server_pb.LanguageServerService/GetAllCascadeTrajectories",
                            data=b"{}",
                            headers={"Content-Type": "application/json", "x-codeium-csrf-token": tok},
                            method="POST",
                        )
                        with urllib.request.urlopen(req, timeout=0.45) as r:
                            if r.status == 200:
                                discovered = {
                                    "ANTIGRAVITY_LS_ADDRESS": addr,
                                    "ANTIGRAVITY_CSRF_TOKEN": tok,
                                    "ANTIGRAVITY_APP_DATA_DIR": str(APP_DATA_DIR),
                                    "ANTIGRAVITY_AGENTAPI_EXE": "/Applications/Antigravity.app/Contents/Resources/bin/language_server",
                                }
                                try:
                                    LS_ENV_FILE.write_text(json.dumps(discovered, indent=2), encoding="utf-8")
                                except Exception:
                                    pass
                                return discovered
                    except Exception:
                        pass
    except Exception:
        pass
    return {}


def save_startup_ls_environment() -> Dict[str, str]:
    """Persist ANTIGRAVITY_LS_ADDRESS and ANTIGRAVITY_CSRF_TOKEN on startup or via live discovery."""
    global _CACHED_LS_ENV, _LAST_LS_DISCOVER_TS
    live = discover_live_ls_environment()
    if live:
        _CACHED_LS_ENV = live
        _LAST_LS_DISCOVER_TS = time.time()
        return live

    saved: Dict[str, str] = {}
    if LS_ENV_FILE.exists():
        try:
            saved = json.loads(LS_ENV_FILE.read_text(encoding="utf-8"))
        except Exception:
            saved = {}

    updated = False
    for key in ("ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN", "ANTIGRAVITY_APP_DATA_DIR", "ANTIGRAVITY_AGENTAPI_EXE"):
        val = os.environ.get(key)
        if val and saved.get(key) != val:
            saved[key] = val
            updated = True

    if updated:
        try:
            LS_ENV_FILE.write_text(json.dumps(saved, indent=2), encoding="utf-8")
        except Exception:
            pass

    _CACHED_LS_ENV = saved
    return saved


def load_ls_environment(force_discover: bool = False) -> Dict[str, str]:
    """Always return a verified, fresh Language Server address and CSRF token."""
    global _CACHED_LS_ENV, _LAST_LS_DISCOVER_TS
    now = time.time()
    if force_discover or not _CACHED_LS_ENV.get("ANTIGRAVITY_LS_ADDRESS") or (now - _LAST_LS_DISCOVER_TS > 15.0):
        live = discover_live_ls_environment()
        _LAST_LS_DISCOVER_TS = now
        if live:
            _CACHED_LS_ENV = live
            return live
    if _CACHED_LS_ENV:
        return _CACHED_LS_ENV
    if LS_ENV_FILE.exists():
        try:
            _CACHED_LS_ENV = json.loads(LS_ENV_FILE.read_text(encoding="utf-8"))
            return _CACHED_LS_ENV
        except Exception:
            pass
    return save_startup_ls_environment()


def build_agentapi_env() -> Dict[str, str]:
    """Build clean environment for invoking agentapi without subagent sender spoofing."""
    env = os.environ.copy()
    saved = load_ls_environment()
    for k, v in saved.items():
        if v:
            env[k] = v
    # Remove caller conversation metadata so agentapi messages are treated as user/system input
    for strip_key in ("ANTIGRAVITY_SOURCE_METADATA", "ANTIGRAVITY_CONVERSATION_ID", "ANTIGRAVITY_TRAJECTORY_ID"):
        env.pop(strip_key, None)
    return env


# ---------------------------------------------------------------------------
# Network & System Helpers
# ---------------------------------------------------------------------------
def get_lan_ips() -> List[str]:
    ips: List[str] = []
    try:
        out = subprocess.check_output(["ifconfig"], text=True, timeout=2)
        for m in re.finditer(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out):
            ip = m.group(1)
            if not ip.startswith("127.") and not ip.startswith("100.64.") and not ip.startswith("169.254."):
                if ip not in ips:
                    ips.append(ip)
    except Exception:
        pass
    return ips


def get_tailscale_info() -> Dict[str, Any]:
    """Passively detect Tailscale CGNAT IPv4 (100.64.0.0/10) via ifconfig without launching Tailscale.app GUI."""
    installed = TAILSCALE_BIN.exists() or Path("/opt/homebrew/bin/tailscale").exists() or Path("/usr/local/bin/tailscale").exists()
    try:
        out = subprocess.check_output(["ifconfig"], text=True, timeout=1.5)
        for m in re.finditer(r"inet\s+(100\.(\d+)\.\d+\.\d+)", out):
            ip = m.group(1)
            second_octet = int(m.group(2))
            if 64 <= second_octet <= 127:
                return {"installed": True, "running": True, "ip": ip}
    except Exception:
        pass
    return {"installed": installed, "running": False, "ip": None}


# ---------------------------------------------------------------------------
# Data Access: Projects, Context Options, Workspace Files & Conversations
# ---------------------------------------------------------------------------
BUILTIN_SLASH_COMMANDS = [
    {"cmd": "/compact", "name": "compact", "kind": "command", "desc": "Compact or summarize the context to free up the context window"},
    {"cmd": "/schedule", "name": "schedule", "kind": "command", "desc": "Run an instruction on a recurring schedule or set a one-time timer"},
    {"cmd": "/browser", "name": "browser", "kind": "command", "desc": "Web browsing, searching the web, or interacting with web applications"},
    {"cmd": "/plan", "name": "plan", "kind": "command", "desc": "Plan carefully step-by-step before execution"},
    {"cmd": "/grill-me", "name": "grill-me", "kind": "command", "desc": "Align on a plan through an interactive interview to resolve design decisions"},
    {"cmd": "/teamwork", "name": "teamwork", "kind": "command", "desc": "Coordinate a team of autonomous agents working together"},
    {"cmd": "/goal", "name": "goal", "kind": "command", "desc": "Run a long-running task thoroughly until the goal is fully achieved"},
    {"cmd": "/boost", "name": "boost", "kind": "command", "desc": "Deep thinking, strategic planning, multiple perspectives, and verification"},
    {"cmd": "/learn", "name": "learn", "kind": "command", "desc": "Persist learned behavior or complex setup for future tasks"},
    {"cmd": "/automation", "name": "automation", "kind": "command", "desc": "Design and create a scheduled background automation"},
    {"cmd": "/plugin", "name": "plugin", "kind": "command", "desc": "Manage and create Antigravity plugins"},
]

BUILTIN_SUBAGENTS = [
    {"mention": "@research", "name": "research", "kind": "subagent", "desc": "Read-only research subagent for codebase exploration"},
    {"mention": "@self", "name": "self", "kind": "subagent", "desc": "Full-capability autonomous subagent in separate context"},
    {"mention": "@image-generator", "name": "image-generator", "kind": "subagent", "desc": "Generate or edit images from a brief"},
]


def load_context_options() -> Dict[str, Any]:
    skills: List[Dict[str, Any]] = []
    seen_skills: Set[str] = set()
    skill_roots = [
        CONFIG_DIR / "skills",
        APP_DATA_DIR / "builtin" / "skills",
    ]
    for sroot in skill_roots:
        if not sroot.exists():
            continue
        for smd in sorted(sroot.glob("*/SKILL.md")):
            try:
                text = smd.read_text(encoding="utf-8", errors="replace")[:2000]
                name = smd.parent.name
                desc = "Specialized agent skill"
                m_name = re.search(r"^name:\s*(.+)$", text, re.MULTILINE)
                m_desc = re.search(r"^description:\s*(.+)$", text, re.MULTILINE)
                if m_name:
                    name = m_name.group(1).strip().strip("'\"")
                if m_desc:
                    desc = m_desc.group(1).strip().strip("'\"")[:110]
                if name not in seen_skills:
                    seen_skills.add(name)
                    skills.append({
                        "cmd": f"/{name}",
                        "mention": f"@{name}",
                        "name": name,
                        "kind": "skill",
                        "desc": desc,
                        "path": str(smd),
                    })
            except Exception:
                continue

    mcp_servers: List[Dict[str, Any]] = []
    mcp_root = APP_DATA_DIR / "mcp"
    if mcp_root.exists():
        for mdir in sorted(mcp_root.iterdir()):
            if mdir.is_dir() and not mdir.name.startswith("."):
                tools_count = len(list(mdir.glob("*.json")))
                mcp_servers.append({
                    "mention": f"@{mdir.name}",
                    "name": mdir.name,
                    "kind": "mcp",
                    "desc": f"MCP Server ({tools_count} tools)",
                })

    return {
        "slash_commands": BUILTIN_SLASH_COMMANDS + skills,
        "skills": skills,
        "subagents": BUILTIN_SUBAGENTS,
        "mcp_servers": mcp_servers,
    }


SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "build", "dist", ".next", ".gradle", ".idea"}


def search_workspace_items(
    conversation_id: str = "",
    query: str = "",
    kind: str = "all",
    limit: int = 40,
) -> List[Dict[str, Any]]:
    roots: List[Path] = []
    if conversation_id:
        row = fetch_conversation_summary_row(conversation_id)
        raw_ws = row.get("workspace_uris") or ""
        if raw_ws:
            try:
                parsed = json.loads(raw_ws)
                if isinstance(parsed, list):
                    for u in parsed:
                        if isinstance(u, str) and u.startswith("file://"):
                            p = Path(urllib.parse.unquote(u[7:]))
                            if p.exists() and p not in roots:
                                roots.append(p)
            except Exception:
                pass
        c_brain = BRAIN_DIR / conversation_id
        if c_brain.exists() and c_brain not in roots:
            roots.append(c_brain)

    for fallback in (APP_DATA_DIR / "scratch", HOME / "Desktop"):
        if fallback.exists() and fallback not in roots:
            roots.append(fallback)

    q_lower = query.strip().lower()
    results: List[Dict[str, Any]] = []
    seen_paths: Set[str] = set()

    for root in roots:
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [
                    d for d in dirnames
                    if not d.startswith(".") and d not in SKIP_DIRS
                ]
                rel_dir = os.path.relpath(dirpath, root)
                depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
                if depth > 4:
                    dirnames[:] = []
                    continue

                if kind in ("all", "folder"):
                    for d in dirnames:
                        full_d = Path(dirpath) / d
                        sp = str(full_d)
                        if sp in seen_paths:
                            continue
                        rel_d = str(full_d.relative_to(root))
                        if not q_lower or q_lower in d.lower() or q_lower in rel_d.lower():
                            seen_paths.add(sp)
                            results.append({
                                "name": d,
                                "rel_path": f"{root.name}/{rel_d}",
                                "path": sp,
                                "kind": "folder",
                            })
                            if len(results) >= limit:
                                return results

                if kind in ("all", "file"):
                    for fn in filenames:
                        if fn.startswith("."):
                            continue
                        full_f = Path(dirpath) / fn
                        sp = str(full_f)
                        if sp in seen_paths:
                            continue
                        rel_f = str(full_f.relative_to(root))
                        if not q_lower or q_lower in fn.lower() or q_lower in rel_f.lower():
                            seen_paths.add(sp)
                            try:
                                sz = full_f.stat().st_size
                            except Exception:
                                sz = 0
                            results.append({
                                "name": fn,
                                "rel_path": f"{root.name}/{rel_f}",
                                "path": sp,
                                "kind": "file",
                                "size": sz,
                            })
                            if len(results) >= limit:
                                return results
        except Exception:
            continue

    return results


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".bmp", ".heic", ".heif", ".avif"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".3gp"}


def save_uploaded_media(
    conversation_id: str,
    filename: str,
    data_b64: str = "",
    raw_bytes: Optional[bytes] = None,
) -> Dict[str, Any]:
    orig_name = Path(filename or "upload.bin").name
    safe_name = re.sub(r"[^a-zA-Z0-9._-\u4e00-\u9fa5]", "_", orig_name) or "upload.bin"
    target_conv = conversation_id or "shared"
    upload_dir = BRAIN_DIR / target_conv / ".user_uploaded"
    upload_dir.mkdir(parents=True, exist_ok=True)
    ts_ms = int(time.time() * 1000)
    out_path = upload_dir / f"mobile_{ts_ms}_{safe_name}"

    if raw_bytes is None:
        if "," in data_b64 and data_b64.startswith("data:"):
            data_b64 = data_b64.split(",", 1)[1]
        raw_bytes = base64.b64decode(data_b64) if data_b64 else b""

    out_path.write_bytes(raw_bytes)
    ext = out_path.suffix.lower()

    # If user picked a .heic/.heif photo from mobile album, convert to .jpg on macOS so browsers & read_file render it natively
    if ext in {".heic", ".heif"} and os.path.exists("/usr/bin/sips"):
        jpg_path = out_path.with_suffix(".jpg")
        try:
            subprocess.run(
                ["/usr/bin/sips", "-s", "format", "jpeg", str(out_path), "--out", str(jpg_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8.0,
                check=True,
            )
            if jpg_path.is_file() and jpg_path.stat().st_size > 0:
                try:
                    out_path.unlink()
                except Exception:
                    pass
                out_path = jpg_path
                safe_name = Path(safe_name).with_suffix(".jpg").name
                ext = ".jpg"
                raw_bytes = out_path.read_bytes()
        except Exception:
            pass

    is_image = ext in IMAGE_EXTS
    is_video = ext in VIDEO_EXTS
    mime_type = mimetypes.guess_type(str(out_path))[0] or (
        "video/mp4" if is_video else ("image/jpeg" if is_image else "application/octet-stream")
    )
    return {
        "ok": True,
        "name": safe_name,
        "path": str(out_path),
        "size": len(raw_bytes),
        "is_image": is_image,
        "is_video": is_video,
        "mime_type": mime_type,
    }


def _relocate_shared_uploads(conversation_id: str, content: str) -> str:
    """If files were uploaded to brain/shared/.user_uploaded before a conversation ID existed, copy them into brain/<conversation_id>/.user_uploaded."""
    if not conversation_id or conversation_id == "shared" or "/shared/.user_uploaded/" not in content:
        return content
    try:
        dest_dir = BRAIN_DIR / conversation_id / ".user_uploaded"
        dest_dir.mkdir(parents=True, exist_ok=True)
        shared_prefix = str(BRAIN_DIR / "shared" / ".user_uploaded") + "/"

        def _replace_match(m: re.Match) -> str:
            full_path = m.group(0)
            src = Path(full_path)
            if src.is_file():
                dst = dest_dir / src.name
                if not dst.exists():
                    dst.write_bytes(src.read_bytes())
                return str(dst)
            return full_path

        pattern = re.escape(shared_prefix) + r"[^\)\]\s\"']+"
        return re.sub(pattern, _replace_match, content)
    except Exception:
        return content


def load_projects() -> List[Dict[str, Any]]:
    projects: List[Dict[str, Any]] = []
    if not PROJECTS_DIR.exists():
        return projects
    for pfile in sorted(PROJECTS_DIR.glob("*.json")):
        try:
            data = json.loads(pfile.read_text(encoding="utf-8"))
            pid = data.get("id") or pfile.stem
            name = data.get("name") or pid
            folders: List[str] = []
            for res in data.get("projectResources", {}).get("resources", []):
                uri = res.get("gitFolder", {}).get("folderUri", "")
                if uri.startswith("file://"):
                    folders.append(urllib.parse.unquote(uri[7:]))
                elif uri:
                    folders.append(uri)
            projects.append({
                "id": pid,
                "name": name,
                "folders": folders,
                "isWorkspaceOnly": bool(data.get("isWorkspaceOnly", False)),
            })
        except Exception:
            continue
    return projects


CONTEXT_FOLDER_RE = re.compile(r"\[Context Folder:\s*([^\]]+)\]\((?:file://)?([^\)]+)\)")


def resolve_project_and_workspace(
    workspace: str = "",
    project_id: str = "",
    text_hint: str = "",
    projects_list: Optional[List[Dict[str, Any]]] = None,
) -> tuple:
    """Resolve (project_id, workspace_path, workspace_uri) from explicit args or `[Context Folder: ...](...)`."""
    projs = projects_list if projects_list is not None else load_projects()
    ws_clean = urllib.parse.unquote(workspace.strip())
    if ws_clean.startswith("file://"):
        ws_clean = ws_clean[7:]
    ws_clean = ws_clean.rstrip("/")

    hint_folder_name = ""
    if text_hint:
        m_cf = CONTEXT_FOLDER_RE.search(text_hint)
        if m_cf:
            hint_folder_name = m_cf.group(1).strip()
            extracted_path = urllib.parse.unquote(m_cf.group(2).strip()).rstrip("/")
            if not ws_clean and extracted_path:
                ws_clean = extracted_path

    # 1. Explicit project_id match
    if project_id and project_id not in ("", "outside-of-project", "all"):
        for p in projs:
            if p["id"] == project_id:
                p_folder = (p.get("folders") or [""])[0].rstrip("/")
                final_ws = ws_clean or p_folder
                ws_uri = f"file://{final_ws}" if final_ws else ""
                return (p["id"], final_ws, ws_uri)

    # 2. Match by workspace folder path against project folders
    if ws_clean:
        for p in projs:
            for fld in p.get("folders") or []:
                fld_clean = fld.rstrip("/")
                if fld_clean and (ws_clean == fld_clean or ws_clean.startswith(fld_clean + "/")):
                    return (p["id"], ws_clean, f"file://{ws_clean}")

    # 3. Match by `[Context Folder: <Name>]` or folder basename against project name
    cand_names = [n.lower() for n in (hint_folder_name, Path(ws_clean).name if ws_clean else "") if n]
    for cname in cand_names:
        for p in projs:
            if p["id"] in ("outside-of-project", "default-cli-project"):
                continue
            if p["name"].strip().lower() == cname:
                p_folder = (p.get("folders") or [""])[0].rstrip("/")
                final_ws = ws_clean or p_folder
                ws_uri = f"file://{final_ws}" if final_ws else ""
                return (p["id"], final_ws, ws_uri)

    ws_uri = f"file://{ws_clean}" if ws_clean else ""
    return ("", ws_clean, ws_uri)


def derive_clean_conversation_title(raw_title: str, raw_preview: str, cid: str) -> str:
    """Derive a clean human-readable title when `title` in conversation_summaries is empty."""
    t = (raw_title or "").strip()
    if t and not t.startswith("[Context Folder:"):
        return t
    prev = (raw_preview or "").strip()
    if prev:
        cleaned_lines = []
        for ln in prev.splitlines():
            ln_s = ln.strip()
            if not ln_s:
                continue
            if ln_s.startswith(("[Context Folder:", "[Attached Image:", "[Attached Video:", "[Attached File:")):
                continue
            cleaned_lines.append(ln_s)
        if cleaned_lines:
            return cleaned_lines[0][:80]
    # Inspect first user step in transcript.jsonl if preview only had the `[Context Folder: ...]` line
    tpath = BRAIN_DIR / cid / ".system_generated" / "logs" / "transcript.jsonl"
    if tpath.exists():
        try:
            with tpath.open("r", encoding="utf-8", errors="replace") as fp:
                for _ in range(6):
                    line = fp.readline()
                    if not line:
                        break
                    obj = json.loads(line)
                    if obj.get("type") == "USER_INPUT":
                        u_txt = clean_user_input(obj.get("content", ""))
                        for ln in u_txt.splitlines():
                            ln_s = ln.strip()
                            if ln_s and not ln_s.startswith(("[Context Folder:", "[Attached Image:", "[Attached Video:", "[Attached File:")):
                                return ln_s[:80]
        except Exception:
            pass
    return t or prev[:80] or "Untitled Conversation"


def query_conversations(project_id: str = "", search_query: str = "", limit: int = 80) -> List[Dict[str, Any]]:
    if not DB_PATH.exists():
        return []
    uri = f"file:{DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        all_projs = load_projects()
        projects_map = {p["id"]: p["name"] for p in all_projs}
        projects_map["outside-of-project"] = "Global / Scratch"

        matched_ids: Optional[Set[str]] = None
        if search_query.strip():
            matched_ids = set()
            q_escaped = '"' + search_query.strip().replace('"', '""') + '"'
            try:
                cur = conn.execute(
                    "SELECT DISTINCT conversation_id FROM conversations_fts WHERE conversations_fts MATCH ? LIMIT 200",
                    (q_escaped,),
                )
                for r in cur:
                    matched_ids.add(r["conversation_id"])
            except Exception:
                pass
            like_q = f"%{search_query.strip()}%"
            cur = conn.execute(
                "SELECT conversation_id FROM conversation_summaries WHERE title LIKE ? OR preview LIKE ? LIMIT 200",
                (like_q, like_q),
            )
            for r in cur:
                matched_ids.add(r["conversation_id"])

        cur = conn.execute(
            """
            SELECT conversation_id, title, preview, step_count, last_modified_time,
                   workspace_uris, status, source, project_id, agent_name,
                   parent_conversation_id, nesting_depth, not_fully_idle, killed,
                   last_user_input_time
            FROM conversation_summaries
            ORDER BY last_modified_time DESC
            LIMIT 400
            """
        )
        rows = [dict(r) for r in cur.fetchall()]

        # Group subagents by parent_conversation_id
        subagents_by_parent: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            parent_id = r.get("parent_conversation_id") or ""
            if parent_id:
                subagents_by_parent.setdefault(parent_id, []).append({
                    "conversation_id": r["conversation_id"],
                    "title": r["title"] or r["agent_name"] or "Subagent",
                    "agent_name": r["agent_name"],
                    "status": r["status"],
                    "step_count": r["step_count"],
                    "not_fully_idle": bool(r["not_fully_idle"]),
                    "last_modified_time": r["last_modified_time"],
                })

        results: List[Dict[str, Any]] = []
        for r in rows:
            cid = r["conversation_id"]
            if cid in HIDDEN_CONVERSATION_IDS:
                continue
            if r.get("parent_conversation_id"):
                continue
            if matched_ids is not None and cid not in matched_ids:
                continue

            # Filter out empty/ghost CLI conversations that have no transcript and no content
            raw_title = (r.get("title") or "").strip()
            raw_preview = (r.get("preview") or "").strip()
            tpath = BRAIN_DIR / cid / ".system_generated" / "logs" / "transcript.jsonl"
            if not tpath.exists():
                continue
            if not raw_title and not raw_preview and int(r.get("step_count") or 0) <= 2:
                continue

            workspaces: List[str] = []
            raw_ws = r.get("workspace_uris") or ""
            if raw_ws:
                try:
                    parsed_ws = json.loads(raw_ws)
                    if isinstance(parsed_ws, list):
                        for u in parsed_ws:
                            if isinstance(u, str) and u.startswith("file://"):
                                workspaces.append(urllib.parse.unquote(u[7:]))
                            elif isinstance(u, str):
                                workspaces.append(u)
                except Exception:
                    pass

            pid = (r.get("project_id") or "").strip()
            if not pid or pid == "outside-of-project":
                inferred_pid, inferred_ws, _ = resolve_project_and_workspace(
                    workspace=workspaces[0] if workspaces else "",
                    project_id=pid,
                    text_hint=raw_preview or raw_title,
                    projects_list=all_projs,
                )
                if inferred_pid:
                    pid = inferred_pid
                    if inferred_ws and inferred_ws not in workspaces:
                        workspaces.append(inferred_ws)
            if not pid:
                pid = "outside-of-project"

            if project_id and project_id != "all" and pid != project_id:
                continue

            display_title = derive_clean_conversation_title(raw_title, raw_preview, cid)

            results.append({
                "conversation_id": cid,
                "title": display_title,
                "preview": raw_preview,
                "step_count": r["step_count"],
                "last_modified_time": r["last_modified_time"],
                "last_user_input_time": r["last_user_input_time"],
                "workspaces": workspaces,
                "status": r["status"],
                "is_running": bool(r["not_fully_idle"]) or r["status"] == "CASCADE_RUN_STATUS_RUNNING",
                "source": "CLI" if "CLI" in (r.get("source") or "") else "Desktop",
                "project_id": pid,
                "project_name": projects_map.get(pid, pid[:8]),
                "subagents": subagents_by_parent.get(cid, []),
            })
            if len(results) >= limit:
                break
        return results
    finally:
        conn.close()


USER_REQ_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
SYS_MSG_RE = re.compile(r"<SYSTEM_MESSAGE>\s*(.*?)\s*</SYSTEM_MESSAGE>", re.DOTALL)
MSG_HEADER_RE = re.compile(r"^\[Message\]\s+timestamp=(\S+)\s+sender=(\S+)\s+priority=(\S+)\s+content=(.*)$", re.DOTALL)


def clean_user_input(raw_content: str) -> str:
    m = USER_REQ_RE.search(raw_content)
    if m:
        return m.group(1).strip()
    # Strip <ADDITIONAL_METADATA> and <USER_SETTINGS_CHANGE> if present
    cleaned = re.sub(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", "", raw_content, flags=re.DOTALL)
    cleaned = re.sub(r"<USER_SETTINGS_CHANGE>.*?</USER_SETTINGS_CHANGE>", "", cleaned, flags=re.DOTALL)
    return cleaned.strip()


from datetime import datetime
import urllib.request

TRANSCRIPT_CACHE: Dict[str, tuple] = {}
LAST_CONV_SIG: Dict[str, Any] = {}
RECENTLY_ACTIVE_CONVS: Dict[str, float] = {}
CONVERSATION_MESSAGE_QUEUES: Dict[str, List[Dict[str, Any]]] = {}
LAST_DEQUEUE_TS: Dict[str, float] = {}


KEEP_TOOL_ARG_KEYS = ("Path", "path", "CommandLine", "query", "Pattern", "toolSummary", "SearchPath", "Url", "TaskId", "DirectoryPath", "questions", "ArtifactMetadata")
COMPLETED_AT_RE = re.compile(r"Completed At:\s*(\S+)")


def parse_iso_ts(ts_str: str) -> float:
    if not ts_str:
        return 0.0
    try:
        s = ts_str.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


def parse_duration_str(dur_str: Any) -> float:
    if not dur_str or not isinstance(dur_str, str):
        return 0.0
    try:
        if dur_str.endswith("s"):
            return round(float(dur_str[:-1]), 1)
    except Exception:
        pass
    return 0.0


def _call_ls_json(method: str, payload_dict: Dict[str, Any], timeout: float = 1.5) -> Optional[Dict[str, Any]]:
    """Call a method on the live Antigravity Desktop Language Server with auto-rediscovery on port/token change."""
    payload = json.dumps(payload_dict).encode("utf-8")
    for attempt in range(2):
        env = load_ls_environment(force_discover=(attempt == 1))
        addr = env.get("ANTIGRAVITY_LS_ADDRESS", "")
        csrf = env.get("ANTIGRAVITY_CSRF_TOKEN", "")
        if not addr or not csrf:
            continue
        url = f"http://{addr}/exa.language_server_pb.LanguageServerService/{method}"
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-codeium-csrf-token": csrf,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace").strip()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as he:
            # If 400/500 from LoadTrajectory or SendUserCascadeMessage on valid port, don't rediscover unless 401/403
            if he.code not in (401, 403) and attempt == 0:
                return None
        except Exception:
            continue
    return None


def fetch_live_ls_tail_steps(conversation_id: str, step_offset: int) -> List[Dict[str, Any]]:
    """Query the live Antigravity Desktop Language Server for tail & in-progress trajectory steps."""
    data = _call_ls_json(
        "GetCascadeTrajectorySteps",
        {"cascadeId": conversation_id, "stepOffset": max(0, step_offset)},
        timeout=0.4,
    )
    if isinstance(data, dict):
        return data.get("steps") or []
    return []


_CACHED_USER_QUOTA: Dict[str, Any] = {}
_LAST_USER_QUOTA_TS: float = 0.0


def fetch_user_quota_and_status(force_refresh: bool = False) -> Dict[str, Any]:
    """Query live Antigravity Language Server GetUserStatus for real-time model quotas and account status."""
    global _CACHED_USER_QUOTA, _LAST_USER_QUOTA_TS
    now = time.time()
    if not force_refresh and _CACHED_USER_QUOTA and (now - _LAST_USER_QUOTA_TS < 8.0):
        return _CACHED_USER_QUOTA

    raw = _call_ls_json("GetUserStatus", {}, timeout=3.0)
    us = (raw or {}).get("userStatus") if isinstance(raw, dict) else None
    if not isinstance(us, dict) or not us:
        if _CACHED_USER_QUOTA:
            return _CACHED_USER_QUOTA
        return {"ok": False, "error": "Language Server GetUserStatus unavailable", "models": [], "user": {}}

    user_tier = us.get("userTier") or {}
    plan_status = us.get("planStatus") or {}
    plan_info = plan_status.get("planInfo") or {}

    user_obj = {
        "name": us.get("name") or "",
        "email": us.get("email") or "",
        "profile_picture_url": us.get("profilePictureUrl") or "",
        "tier_id": user_tier.get("id") or "",
        "tier_name": user_tier.get("name") or plan_info.get("planName") or "Pro",
        "tier_description": user_tier.get("description") or "",
        "upgrade_text": user_tier.get("upgradeSubscriptionText") or "",
        "plan_name": plan_info.get("planName") or "",
        "teams_tier": plan_info.get("teamsTier") or "",
        "available_prompt_credits": plan_status.get("availablePromptCredits"),
        "monthly_prompt_credits": plan_info.get("monthlyPromptCredits"),
        "available_flow_credits": plan_status.get("availableFlowCredits"),
        "monthly_flow_credits": plan_info.get("monthlyFlowCredits"),
    }

    raw_configs = (us.get("cascadeModelConfigData") or {}).get("clientModelConfigs") or []
    models_list: List[Dict[str, Any]] = []
    for cfg in raw_configs:
        if not isinstance(cfg, dict):
            continue
        label = (cfg.get("label") or "").strip()
        model_id = (cfg.get("modelId") or "").strip()
        model_enum = ((cfg.get("modelOrAlias") or {}).get("model") or "").strip()
        if not label:
            continue

        # Parse base_name & effort from e.g. "Gemini 4 Argon (High)"
        base_name = label
        effort = ""
        m_paren = re.match(r"^(.*?)\s*\(([^()]+)\)\s*$", label)
        if m_paren:
            base_name = m_paren.group(1).strip()
            effort = m_paren.group(2).strip()

        qi = cfg.get("quotaInfo") or {}
        reset_time = (qi.get("resetTime") or "").strip()
        if "remainingFraction" in qi and qi.get("remainingFraction") is not None:
            try:
                rem_frac = float(qi.get("remainingFraction"))
            except Exception:
                rem_frac = 1.0
        elif reset_time:
            rem_frac = 0.0 if parse_iso_ts(reset_time) > now else 1.0
        else:
            rem_frac = 1.0

        rem_frac = max(0.0, min(1.0, rem_frac))
        rem_pct = int(round(rem_frac * 100))

        models_list.append({
            "label": label,
            "base_name": base_name,
            "effort": effort,
            "model_id": model_id,
            "model_enum": model_enum,
            "remaining_fraction": rem_frac,
            "remaining_percent": rem_pct,
            "reset_time": reset_time,
            "tag_title": cfg.get("tagTitle") or "",
            "tag_description": cfg.get("tagDescription") or "",
            "supports_images": bool(cfg.get("supportsImages", False)),
            "is_recommended": bool(cfg.get("isRecommended", False)),
        })

    # Prefer displaying High -> Medium -> Low order for same base_name when presenting
    effort_rank = {"High": 0, "Medium": 1, "Low": 2, "Thinking": 0}
    models_sorted = sorted(
        models_list,
        key=lambda m: (
            0 if "Gemini 4 Argon" in m["base_name"] else (1 if "Gemini" in m["base_name"] else 2),
            m["base_name"],
            effort_rank.get(m["effort"], 3),
        ),
    )

    result = {
        "ok": True,
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "user": user_obj,
        "models": models_sorted,
    }
    _CACHED_USER_QUOTA = result
    _LAST_USER_QUOTA_TS = now
    return result


def slim_tool_args(args: Any) -> Dict[str, Any]:
    if not isinstance(args, dict):
        return {}
    out: Dict[str, Any] = {}
    for k in KEEP_TOOL_ARG_KEYS:
        if k in args and args[k] is not None:
            v = args[k]
            if isinstance(v, str) and len(v) > 160:
                out[k] = v[:160] + "..."
            else:
                out[k] = v
    return out


def fetch_conversation_summary_row(conversation_id: str) -> Dict[str, Any]:
    summary: Dict[str, Any] = {"conversation_id": conversation_id}
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=3.0)
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT * FROM conversation_summaries WHERE conversation_id = ?",
                (conversation_id,),
            )
            row = cur.fetchone()
            if row:
                rdict = dict(row)
                rdict.pop("raw_summary", None)
                summary = rdict
                summary["is_running"] = bool(rdict.get("not_fully_idle")) or rdict.get("status") == "CASCADE_RUN_STATUS_RUNNING"
            conn.close()
        except Exception:
            pass
    if time.time() - RECENTLY_ACTIVE_CONVS.get(conversation_id, 0.0) < 6.0:
        summary["is_running"] = True
    return summary


def enrich_items_with_live_ls(
    conversation_id: str,
    sliced_items: List[Dict[str, Any]],
    raw_step_count: int,
    summary: Dict[str, Any],
) -> None:
    """Enrich tail steps with exact `thinkingDuration` and append any live generating/running steps from LS."""
    step_offset = max(0, raw_step_count - 25)
    ls_steps = fetch_live_ls_tail_steps(conversation_id, step_offset)
    if not ls_steps:
        return

    by_step_idx: Dict[int, Dict[str, Any]] = {
        it["step_index"]: it for it in sliced_items if "step_index" in it
    }

    for offset_idx, ls_step in enumerate(ls_steps):
        sidx = step_offset + offset_idx
        stype = ls_step.get("type", "")
        sstatus = ls_step.get("status", "CORTEX_STEP_STATUS_DONE")
        is_step_running = sstatus not in ("CORTEX_STEP_STATUS_DONE", "CORTEX_STEP_STATUS_ERROR", "CORTEX_STEP_STATUS_CANCELLED")
        if is_step_running:
            summary["is_running"] = True

        md = ls_step.get("metadata") or {}
        created_at = md.get("createdAt") or ""
        completed_at = md.get("completedAt") or md.get("finishedGeneratingAt") or ""

        if sidx in by_step_idx:
            item = by_step_idx[sidx]
            if item.get("role") == "assistant":
                pr = ls_step.get("plannerResponse") or {}
                tdur = parse_duration_str(pr.get("thinkingDuration"))
                if tdur > 0:
                    item["thinking_duration_sec"] = tdur
                if completed_at:
                    item["completed_at"] = completed_at
                if is_step_running:
                    item["status"] = "RUNNING"
                live_think = (pr.get("thinking") or "").strip()
                if live_think and len(live_think) > len(item.get("thinking") or ""):
                    item["thinking"] = live_think[:1600]
                live_resp = (pr.get("modifiedResponse") or pr.get("response") or "").strip()
                if live_resp and len(live_resp) > len(item.get("content") or ""):
                    item["content"] = live_resp
            continue

        # Step is newer than transcript.jsonl (in-memory live step in Language Server!)
        if sidx >= raw_step_count:
            if stype == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
                pr = ls_step.get("plannerResponse") or {}
                tdur = parse_duration_str(pr.get("thinkingDuration"))
                if tdur <= 0 and created_at:
                    t0 = parse_iso_ts(created_at)
                    t1 = parse_iso_ts(completed_at) if completed_at else time.time()
                    if t0 > 0 and t1 >= t0:
                        tdur = round(t1 - t0, 1)
                raw_tcalls = pr.get("toolCalls") or []
                tcalls: List[Dict[str, Any]] = []
                for tc in raw_tcalls:
                    args_obj: Dict[str, Any] = {}
                    try:
                        args_obj = json.loads(tc.get("argumentsJson") or "{}")
                    except Exception:
                        pass
                    tcalls.append({
                        "name": tc.get("name", "tool"),
                        "args": slim_tool_args(args_obj),
                        "output": "",
                        "status": "RUNNING" if is_step_running else "DONE",
                        "lines_added": 0,
                        "lines_removed": 0,
                    })
                new_item = {
                    "id": f"step-{sidx}",
                    "step_index": sidx,
                    "role": "assistant",
                    "status": "RUNNING" if is_step_running else "DONE",
                    "thinking": (pr.get("thinking") or "").strip()[:1600],
                    "thinking_duration_sec": tdur,
                    "tool_calls": tcalls,
                    "content": (pr.get("modifiedResponse") or pr.get("response") or "").strip(),
                    "created_at": created_at,
                    "completed_at": completed_at,
                }
                sliced_items.append(new_item)
                by_step_idx[sidx] = new_item
            elif stype not in ("CORTEX_STEP_TYPE_USER_INPUT", "CORTEX_STEP_TYPE_SYSTEM_MESSAGE"):
                # Live tool execution step (e.g. VIEW_FILE, RUN_COMMAND, GENERIC)
                tc_meta = md.get("toolCall") or {}
                tc_name = tc_meta.get("name") or stype.replace("CORTEX_STEP_TYPE_", "").lower()
                args_obj = {}
                try:
                    args_obj = json.loads(tc_meta.get("argumentsJson") or "{}")
                except Exception:
                    pass
                if sliced_items and sliced_items[-1].get("role") == "assistant":
                    last_a = sliced_items[-1]
                    if is_step_running:
                        last_a["status"] = "RUNNING"
                    matched = False
                    for existing_tc in last_a.get("tool_calls") or []:
                        if existing_tc.get("name") == tc_name:
                            existing_tc["status"] = "RUNNING" if is_step_running else "DONE"
                            matched = True
                            break
                    if not matched:
                        last_a.setdefault("tool_calls", []).append({
                            "name": tc_name,
                            "args": slim_tool_args(args_obj),
                            "output": "",
                            "status": "RUNNING" if is_step_running else "DONE",
                            "lines_added": 0,
                            "lines_removed": 0,
                        })


def parse_conversation_transcript(conversation_id: str, force_live: bool = False) -> Dict[str, Any]:
    conv_dir = BRAIN_DIR / conversation_id
    logs_dir = conv_dir / ".system_generated" / "logs"
    transcript_path = logs_dir / "transcript.jsonl"
    full_path = logs_dir / "transcript_full.jsonl"

    ts_size = transcript_path.stat().st_size if transcript_path.exists() else 0
    ts_mtime = transcript_path.stat().st_mtime if transcript_path.exists() else 0.0
    cache_sig = (ts_size, ts_mtime)

    cached_entry = TRANSCRIPT_CACHE.get(conversation_id)
    if cached_entry and cached_entry[0] == cache_sig and not force_live:
        result = dict(cached_entry[1])
        summary = fetch_conversation_summary_row(conversation_id)
        result["summary"] = summary
        result["queued_messages"] = list(CONVERSATION_MESSAGE_QUEUES.get(conversation_id, []))
        if summary.get("is_running"):
            cloned_steps = [dict(s) for s in result.get("steps", [])]
            enrich_items_with_live_ls(conversation_id, cloned_steps, result.get("step_count", 0), summary)
            result["steps"] = cloned_steps
        return result

    raw_steps: List[Dict[str, Any]] = []
    full_lines_cache: Optional[List[str]] = None

    if transcript_path.exists():
        lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
        for idx, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue

            # Only load transcript_full.jsonl if visible assistant/user 'content' was truncated
            trunc = obj.get("truncated_fields") or []
            if "content" in trunc and obj.get("type") in ("PLANNER_RESPONSE", "USER_INPUT") and full_path.exists():
                if full_lines_cache is None:
                    try:
                        full_lines_cache = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
                    except Exception:
                        full_lines_cache = []
                if idx < len(full_lines_cache):
                    try:
                        full_obj = json.loads(full_lines_cache[idx])
                        if full_obj.get("step_index") == obj.get("step_index"):
                            obj["content"] = full_obj.get("content", obj.get("content", ""))
                    except Exception:
                        pass
            raw_steps.append(obj)

    # Transform raw steps into UI-ready message turns and tool executions
    items: List[Dict[str, Any]] = []
    total_input_tokens = 0
    total_output_tokens = 0
    total_cache_tokens = 0

    i = 0
    n = len(raw_steps)
    while i < n:
        step = raw_steps[i]
        stype = step.get("type", "")
        sidx = step.get("step_index", i)
        created_at = step.get("created_at", "")

        if stype == "USER_INPUT":
            text = clean_user_input(step.get("content", ""))
            media = step.get("media") or []
            items.append({
                "id": f"step-{sidx}",
                "step_index": sidx,
                "role": "user",
                "origin": "desktop",
                "content": text,
                "media": media,
                "created_at": created_at,
            })
            i += 1

        elif stype == "SYSTEM_MESSAGE":
            raw_c = step.get("content", "")
            m_sys = SYS_MSG_RE.search(raw_c)
            inner = m_sys.group(1).strip() if m_sys else raw_c.strip()
            m_hdr = MSG_HEADER_RE.match(inner)
            sender = "system"
            body = inner
            if m_hdr:
                sender = m_hdr.group(2)
                body = m_hdr.group(4).strip()

            if body.startswith("[MOBILE_USER_INPUT]"):
                user_text = body[len("[MOBILE_USER_INPUT]"):].strip()
                items.append({
                    "id": f"step-{sidx}",
                    "step_index": sidx,
                    "role": "user",
                    "origin": "mobile",
                    "content": user_text,
                    "created_at": created_at,
                })
            else:
                items.append({
                    "id": f"step-{sidx}",
                    "step_index": sidx,
                    "role": "system_event",
                    "sender": sender,
                    "content": body[:1200],
                    "created_at": created_at,
                })
            i += 1

        elif stype == "PLANNER_RESPONSE":
            in_tok = int(step.get("input_tokens") or 0)
            out_tok = int(step.get("output_tokens") or 0)
            cache_tok = int(step.get("cache_read_tokens") or 0)
            total_input_tokens += in_tok
            total_output_tokens += out_tok
            total_cache_tokens += cache_tok

            is_recent = (n - i) <= 40
            thinking = (step.get("thinking") or "").strip()[:(1600 if is_recent else 180)]
            content = (step.get("content") or "").strip()
            raw_tcalls = step.get("tool_calls") or []

            # Estimate thinking duration from timestamp gap to first GENERIC step or next step
            step_start_ts = parse_iso_ts(created_at)
            first_next_ts = 0.0
            if i + 1 < n:
                first_next_ts = parse_iso_ts(raw_steps[i + 1].get("created_at", ""))
            thinking_duration_sec = 0.0
            if step_start_ts > 0 and first_next_ts >= step_start_ts and (first_next_ts - step_start_ts) < 300:
                thinking_duration_sec = round(first_next_ts - step_start_ts, 1)

            # Pair tool calls with subsequent GENERIC steps
            tool_calls: List[Dict[str, Any]] = []
            j = i + 1
            completed_at = created_at
            for tc_idx, tc in enumerate(raw_tcalls):
                tc_name = tc.get("name", "tool")
                orig_args = tc.get("args") or {}
                lines_added = 0
                lines_removed = 0
                if isinstance(orig_args, dict):
                    if tc_name == "edit_file":
                        rep_str = str(orig_args.get("Replacement") or "")
                        tgt_str = str(orig_args.get("Target") or "")
                        lines_added = len(rep_str.splitlines()) if rep_str else 0
                        lines_removed = len(tgt_str.splitlines()) if tgt_str else 0
                    elif tc_name == "write_file":
                        cnt_str = str(orig_args.get("Content") or "")
                        lines_added = len(cnt_str.splitlines()) if cnt_str else 0
                tc_args = slim_tool_args(orig_args)
                tc_output = ""
                if j < n and raw_steps[j].get("type") == "GENERIC":
                    tc_output = raw_steps[j].get("content", "")
                    m_comp = COMPLETED_AT_RE.search(tc_output[:200])
                    if m_comp:
                        completed_at = m_comp.group(1)
                    elif raw_steps[j].get("created_at"):
                        completed_at = raw_steps[j].get("created_at", completed_at)
                    j += 1
                tool_calls.append({
                    "name": tc_name,
                    "args": tc_args,
                    "output": tc_output[:280] if is_recent else "",
                    "status": "DONE",
                    "lines_added": lines_added,
                    "lines_removed": lines_removed,
                })

            items.append({
                "id": f"step-{sidx}",
                "step_index": sidx,
                "role": "assistant",
                "status": step.get("status", "DONE"),
                "thinking": thinking,
                "thinking_duration_sec": thinking_duration_sec,
                "tool_calls": tool_calls,
                "content": content,
                "tokens": {
                    "input": in_tok,
                    "output": out_tok,
                    "cached": cache_tok,
                },
                "created_at": created_at,
                "completed_at": completed_at,
            })
            i = j

        else:
            # Standalone GENERIC step not paired above
            i += 1

    # Collect Artifacts in brain/<conversation_id>/
    artifacts: List[Dict[str, Any]] = []
    if conv_dir.exists():
        for f in sorted(conv_dir.rglob("*")):
            if ".system_generated" in f.parts or f.is_dir() or f.name.startswith("."):
                continue
            rel = str(f.relative_to(conv_dir))
            ext = f.suffix.lower()
            stat = f.stat()
            artifacts.append({
                "name": rel,
                "filename": f.name,
                "path": str(f),
                "ext": ext,
                "size": stat.st_size,
                "modified_at": stat.st_mtime,
                "is_image": ext in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"},
                "is_markdown": ext in {".md", ".txt"},
            })

    # Collect Background Tasks in brain/<conversation_id>/.system_generated/tasks/
    tasks: List[Dict[str, Any]] = []
    tasks_dir = conv_dir / ".system_generated" / "tasks"
    if tasks_dir.exists():
        for tf in sorted(tasks_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True):
            stat = tf.stat()
            tasks.append({
                "name": tf.stem,
                "filename": tf.name,
                "size": stat.st_size,
                "modified_at": stat.st_mtime,
            })

    summary = fetch_conversation_summary_row(conversation_id)
    if not raw_steps:
        _call_ls_json("LoadTrajectory", {"cascadeId": conversation_id}, timeout=1.2)
    sliced_items = items
    enrich_items_with_live_ls(conversation_id, sliced_items, len(raw_steps), summary)

    result = {
        "conversation_id": conversation_id,
        "summary": summary,
        "steps": sliced_items,
        "step_count": len(raw_steps),
        "artifacts": artifacts,
        "tasks": tasks,
        "queued_messages": list(CONVERSATION_MESSAGE_QUEUES.get(conversation_id, [])),
        "token_totals": {
            "input": total_input_tokens,
            "output": total_output_tokens,
            "cached": total_cache_tokens,
        },
    }
    TRANSCRIPT_CACHE[conversation_id] = (cache_sig, result)
    return result


def is_conversation_currently_running(conversation_id: str) -> bool:
    """Accurately check whether the conversation is currently running a turn."""
    summary = fetch_conversation_summary_row(conversation_id)
    if summary.get("is_running"):
        return True
    traj_resp = _call_ls_json("GetCascadeTrajectory", {"cascadeId": conversation_id}, timeout=0.35)
    if isinstance(traj_resp, dict) and traj_resp.get("status") == "CASCADE_RUN_STATUS_RUNNING":
        return True
    return False


async def broadcast_queue_update(conversation_id: str) -> None:
    q_list = list(CONVERSATION_MESSAGE_QUEUES.get(conversation_id, []))
    for client in list(CONNECTED_WS_CLIENTS):
        if client.subscribed_conversation_id == conversation_id:
            await client.send_json({
                "type": "queue_updated",
                "conversation_id": conversation_id,
                "queued_messages": q_list,
            })


# ---------------------------------------------------------------------------
# Action Execution: Send Message (with Queued Message support) & New Conversation
# ---------------------------------------------------------------------------
ATTACHED_MEDIA_LINK_RE = re.compile(
    r"\[Attached\s+(Image|Video|File):\s*([^\]]+)\]\((?:file://)?([^\)]+)\)",
    re.IGNORECASE,
)


def _resolve_ls_model_enum(model_id: str, effort: str = "high") -> str:
    """Map frontend model_id (plus effort) to Language Server native Model enum string."""
    mid = (model_id or "gemini-4-argon-high").strip().lower()
    eff = (effort or "").strip().lower()

    # If mid is a base family + effort override, normalize
    if mid.startswith("gemini-4-argon") and eff in ("high", "medium", "low"):
        mid = f"gemini-4-argon-{eff}"
    elif mid.startswith("gemini-3.8-flash") and eff in ("high", "medium", "low"):
        mid = f"gemini-3.8-flash-{eff}"

    # Check live quota models cache first
    quota_models = (_CACHED_USER_QUOTA or {}).get("models") or []
    for qm in quota_models:
        if (qm.get("model_id") or "").lower() == mid and qm.get("model_enum"):
            return qm["model_enum"]

    # Static fallback & legacy aliases
    static_map = {
        "gemini-4-argon-high": "MODEL_PLACEHOLDER_M417",
        "gemini-4-argon-medium": "MODEL_PLACEHOLDER_M418",
        "gemini-4-argon-low": "MODEL_PLACEHOLDER_M419",
        "gemini-3.8-flash-high": "MODEL_PLACEHOLDER_M318",
        "gemini-3.8-flash-medium": "MODEL_PLACEHOLDER_M319",
        "gemini-3.8-flash-low": "MODEL_PLACEHOLDER_M320",
        "claude-opus-4-6-thinking": "MODEL_PLACEHOLDER_M26",
        "claude-opus-5-5-high": "MODEL_PLACEHOLDER_M26",
        "claude-opus-5-5-medium": "MODEL_PLACEHOLDER_M26",
        "claude-opus-5-5-low": "MODEL_PLACEHOLDER_M26",
        "claude-sonnet-4-6": "MODEL_PLACEHOLDER_M35",
        "claude-sonnet-5-5-high": "MODEL_PLACEHOLDER_M35",
        "claude-sonnet-5-5-medium": "MODEL_PLACEHOLDER_M35",
        "claude-sonnet-5-5-low": "MODEL_PLACEHOLDER_M35",
        "gpt-oss-120b-medium": "MODEL_OPENAI_GPT_OSS_120B_MEDIUM",
    }
    return static_map.get(mid, "MODEL_PLACEHOLDER_M417")


def _build_cascade_config(model_id: str, effort: str = "high") -> Dict[str, Any]:
    """Build SendUserCascadeMessageRequest.cascadeConfig with the selected model enum."""
    model_enum = _resolve_ls_model_enum(model_id, effort)
    return {
        "plannerConfig": {
            "requestedModel": {
                "model": model_enum,
            }
        }
    }


def _extract_native_media_items(content: str) -> List[Dict[str, Any]]:
    """Extract attached image/media files from content so SendUserCascadeMessage receives native multimodal `media`."""
    media_items: List[Dict[str, Any]] = []
    seen_uris: Set[str] = set()
    for m in ATTACHED_MEDIA_LINK_RE.finditer(content or ""):
        kind = (m.group(1) or "").strip().lower()
        label = (m.group(2) or "").strip()
        raw_path = urllib.parse.unquote((m.group(3) or "").strip())
        if raw_path.startswith("file://"):
            raw_path = raw_path[7:]
        fpath = Path(raw_path)
        if not fpath.is_file():
            continue
        uri = f"file://{fpath.resolve()}"
        if uri in seen_uris:
            continue
        seen_uris.add(uri)
        ext = fpath.suffix.lower()
        mime = mimetypes.guess_type(str(fpath))[0] or (
            "image/jpeg" if ext in IMAGE_EXTS else ("video/mp4" if ext in VIDEO_EXTS else "application/octet-stream")
        )
        # Attach inlineData for images <= 12MB so multimodal vision & transcript media work natively
        if kind == "image" or ext in IMAGE_EXTS:
            try:
                sz = fpath.stat().st_size
                item_obj: Dict[str, Any] = {
                    "mimeType": mime,
                    "uri": uri,
                    "description": label or fpath.name,
                }
                if 0 < sz <= 12 * 1024 * 1024:
                    item_obj["inlineData"] = base64.b64encode(fpath.read_bytes()).decode("ascii")
                media_items.append(item_obj)
            except Exception:
                pass
    return media_items


async def send_message_to_conversation(
    conversation_id: str,
    content: str,
    model: str = "gemini-4-argon-high",
    effort: str = "high",
    mode: str = "auto",
    engine: str = "auto",
    force_immediate: bool = False,
) -> Dict[str, Any]:
    """
    Send a user message into an existing conversation.
    If the conversation is currently running (or already has queued messages) and `force_immediate` is False,
    the message is placed into the conversation's Queued Messages waiting area and automatically sent
    as soon as the current task finishes!
    """
    content = content.strip()
    if not content:
        return {"ok": False, "error": "Empty message content"}
    content = _relocate_shared_uploads(conversation_id, content)

    existing_queue = CONVERSATION_MESSAGE_QUEUES.setdefault(conversation_id, [])
    if not force_immediate and (existing_queue or is_conversation_currently_running(conversation_id)):
        q_item = {
            "id": f"q-{int(time.time() * 1000)}-{os.urandom(3).hex()}",
            "content": content,
            "model": model,
            "effort": effort,
            "mode": mode,
            "created_at": datetime.utcnow().isoformat() + "Z",
        }
        existing_queue.append(q_item)
        await broadcast_queue_update(conversation_id)
        return {
            "ok": True,
            "queued": True,
            "queue_item": q_item,
            "queued_messages": list(existing_queue),
            "conversation_id": conversation_id,
            "detail": "Message added to queue; will send automatically when current task finishes.",
        }

    RECENTLY_ACTIVE_CONVS[conversation_id] = time.time()
    use_agentapi = engine in ("auto", "agentapi")

    # 1. Primary path: LoadTrajectory (wakes up cold/old conversations after account switch or restart)
    #    followed by SendUserCascadeMessage directly on the live Antigravity Language Server,
    #    with `cascadeConfig` (selected model & effort) and native `media` attachments!
    if use_agentapi:
        _call_ls_json("LoadTrajectory", {"cascadeId": conversation_id}, timeout=2.5)
        cascade_req: Dict[str, Any] = {
            "cascadeId": conversation_id,
            "items": [{"text": content}],
            "cascadeConfig": _build_cascade_config(model, effort),
        }
        media_items = _extract_native_media_items(content)
        if media_items:
            cascade_req["media"] = media_items
        ls_res = _call_ls_json(
            "SendUserCascadeMessage",
            cascade_req,
            timeout=4.5,
        )
        if ls_res is not None:
            return {
                "ok": True,
                "queued": False,
                "queued_messages": list(CONVERSATION_MESSAGE_QUEUES.get(conversation_id, [])),
                "engine": "ls-cascade",
                "model": model,
                "model_enum": _resolve_ls_model_enum(model, effort),
                "conversation_id": conversation_id,
                "detail": "Delivered via LanguageServer LoadTrajectory + SendUserCascadeMessage",
            }

    # 2. Secondary path: agentapi send-message (with live-discovered port & CSRF token)
    if use_agentapi and AGENTAPI_BIN.exists():
        payload = f"[MOBILE_USER_INPUT]\n{content}"
        env = build_agentapi_env()
        proc = await asyncio.create_subprocess_exec(
            str(AGENTAPI_BIN),
            "send-message",
            conversation_id,
            payload,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()
        out_str = stdout.decode("utf-8", errors="replace").strip()
        err_str = stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode == 0:
            return {
                "ok": True,
                "engine": "agentapi",
                "conversation_id": conversation_id,
                "detail": out_str,
            }
        elif engine == "agentapi":
            return {
                "ok": False,
                "engine": "agentapi",
                "error": err_str or out_str or f"agentapi exited with {proc.returncode}",
            }

    # 3. Fallback or explicit `agy` CLI execution
    if AGY_BIN.exists():
        cmd = [
            str(AGY_BIN),
            "--conversation",
            conversation_id,
            "--model",
            model,
            "--effort",
            effort,
            "--mode",
            mode,
            "--dangerously-skip-permissions",
            "-p",
            content,
        ]
        asyncio.create_task(_run_background_cmd(cmd))
        return {
            "ok": True,
            "engine": "agy",
            "conversation_id": conversation_id,
            "detail": f"Started agy CLI turn with model={model}",
        }

    return {"ok": False, "error": "Neither LanguageServer, agentapi, nor agy binary could execute the request."}


def _persist_conversation_project_in_db(conversation_id: str, project_id: str, workspace_uri: str, clean_title: str = "") -> None:
    if not conversation_id or not DB_PATH.exists():
        return
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
        ws_json = json.dumps([workspace_uri]) if workspace_uri else ""
        if project_id and ws_json and clean_title:
            conn.execute(
                "UPDATE conversation_summaries SET project_id = ?, workspace_uris = ?, title = CASE WHEN title = '' THEN ? ELSE title END WHERE conversation_id = ?",
                (project_id, ws_json, clean_title, conversation_id),
            )
        elif project_id and ws_json:
            conn.execute(
                "UPDATE conversation_summaries SET project_id = ?, workspace_uris = ? WHERE conversation_id = ?",
                (project_id, ws_json, conversation_id),
            )
        elif project_id:
            conn.execute(
                "UPDATE conversation_summaries SET project_id = ? WHERE conversation_id = ?",
                (project_id, conversation_id),
            )
        elif ws_json:
            conn.execute(
                "UPDATE conversation_summaries SET workspace_uris = ? WHERE conversation_id = ?",
                (ws_json, conversation_id),
            )
        conn.commit()
        conn.close()
    except Exception:
        pass


async def create_new_conversation(
    prompt: str,
    title: str = "",
    model: str = "gemini-4-argon-high",
    effort: str = "high",
    mode: str = "auto",
    engine: str = "auto",
    workspace: str = "",
    project_id: str = "",
) -> Dict[str, Any]:
    prompt = prompt.strip()
    if not prompt:
        return {"ok": False, "error": "Empty prompt"}

    matched_pid, final_ws, ws_uri = resolve_project_and_workspace(
        workspace=workspace,
        project_id=project_id,
        text_hint=prompt,
    )
    fallback_title = title.strip() or derive_clean_conversation_title("", prompt, "")

    tier = "pro"
    for m in AVAILABLE_MODELS:
        if m["id"] == model:
            tier = m["tier"]
            break

    use_agentapi = engine in ("auto", "agentapi")

    # 1. Primary path: Use Language Server's native `StartCascade` + `SendUserCascadeMessage`
    #    so `projectEnvConfig`, `workspaceUris`, `cascadeConfig` (selected model & effort),
    #    and `media` (native image attachments) all take effect on Turn 0!
    if use_agentapi:
        start_req: Dict[str, Any] = {
            "source": "CORTEX_TRAJECTORY_SOURCE_CASCADE_CLIENT",
        }
        if matched_pid and matched_pid != "outside-of-project":
            start_req["projectEnvConfig"] = {
                "projectId": matched_pid,
                "defaultProjectEnvironment": {},
            }
        elif ws_uri:
            start_req["workspaceUris"] = [ws_uri]

        start_res = _call_ls_json("StartCascade", start_req, timeout=3.5)
        if isinstance(start_res, dict) and start_res.get("cascadeId"):
            new_cid = start_res["cascadeId"]
            RECENTLY_ACTIVE_CONVS[new_cid] = time.time()
            prompt_relocated = _relocate_shared_uploads(new_cid, prompt)
            msg_req: Dict[str, Any] = {
                "cascadeId": new_cid,
                "items": [{"text": prompt_relocated}],
                "cascadeConfig": _build_cascade_config(model, effort),
            }
            media_items = _extract_native_media_items(prompt_relocated)
            if media_items:
                msg_req["media"] = media_items
            _call_ls_json("SendUserCascadeMessage", msg_req, timeout=4.5)
            if title.strip():
                _call_ls_json(
                    "UpdateConversationAnnotations",
                    {"cascadeId": new_cid, "annotations": {"title": title.strip()}, "mergeAnnotations": True},
                    timeout=2.0,
                )
            await asyncio.sleep(0.25)
            if matched_pid or ws_uri or fallback_title:
                _persist_conversation_project_in_db(new_cid, matched_pid, ws_uri, fallback_title)
            return {
                "ok": True,
                "engine": "ls-start-cascade",
                "conversation_id": new_cid,
                "project_id": matched_pid,
                "workspace": final_ws,
                "model": model,
            }

    # 2. Secondary path: agentapi new-conversation
    if use_agentapi and AGENTAPI_BIN.exists():
        cmd = [str(AGENTAPI_BIN), "new-conversation", f"--model={tier}"]
        if title.strip():
            cmd.append(f"--title={title.strip()}")
        cmd.append(prompt)
        env = build_agentapi_env()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()
        out_str = stdout.decode("utf-8", errors="replace").strip()
        err_str = stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode == 0:
            new_cid = ""
            try:
                parsed = json.loads(out_str)
                new_cid = parsed.get("response", {}).get("newConversation", {}).get("conversationId", "")
            except Exception:
                pass
            if new_cid and (matched_pid or ws_uri):
                _persist_conversation_project_in_db(new_cid, matched_pid, ws_uri, fallback_title)
            return {
                "ok": True,
                "engine": "agentapi",
                "conversation_id": new_cid,
                "project_id": matched_pid,
                "detail": out_str,
            }
        elif engine == "agentapi":
            return {"ok": False, "engine": "agentapi", "error": err_str or out_str}

    if AGY_BIN.exists():
        cmd = [
            str(AGY_BIN),
            "--model",
            model,
            "--effort",
            effort,
            "--mode",
            mode,
            "--dangerously-skip-permissions",
        ]
        if final_ws and os.path.isdir(final_ws):
            cmd.extend(["--add-dir", final_ws])
        cmd.extend(["-p", prompt])
        asyncio.create_task(_run_background_cmd(cmd, cwd=final_ws if final_ws and os.path.isdir(final_ws) else None))
        await asyncio.sleep(1.2)
        recent = query_conversations(limit=1)
        new_cid = recent[0]["conversation_id"] if recent else ""
        if new_cid and (matched_pid or ws_uri):
            _persist_conversation_project_in_db(new_cid, matched_pid, ws_uri, fallback_title)
        return {
            "ok": True,
            "engine": "agy",
            "conversation_id": new_cid,
            "project_id": matched_pid,
        }

    return {"ok": False, "error": "Failed to create conversation"}


def rename_conversation(conversation_id: str, new_title: str) -> Dict[str, Any]:
    """Rename a conversation both in Language Server annotations and in conversation_summaries.db."""
    clean_t = (new_title or "").strip()
    if not conversation_id or not clean_t:
        return {"ok": False, "error": "Missing conversation_id or title"}
    _call_ls_json(
        "UpdateConversationAnnotations",
        {"cascadeId": conversation_id, "annotations": {"title": clean_t}, "mergeAnnotations": True},
        timeout=2.0,
    )
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
            conn.execute(
                "UPDATE conversation_summaries SET title = ? WHERE conversation_id = ?",
                (clean_t, conversation_id),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            return {"ok": False, "error": str(e)}
    TRANSCRIPT_CACHE.pop(conversation_id, None)
    return {"ok": True, "conversation_id": conversation_id, "title": clean_t}


def move_conversation_to_project(conversation_id: str, project_id: str, workspace: str = "") -> Dict[str, Any]:
    """Move an existing conversation into a Project folder (or Global / Scratch)."""
    if not conversation_id:
        return {"ok": False, "error": "Missing conversation_id"}
    if project_id in ("outside-of-project", "global", ""):
        matched_pid = "outside-of-project"
        ws_uri = ""
    else:
        matched_pid, _, ws_uri = resolve_project_and_workspace(workspace=workspace, project_id=project_id)
        if not matched_pid:
            matched_pid = project_id
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
            if ws_uri:
                conn.execute(
                    "UPDATE conversation_summaries SET project_id = ?, workspace_uris = ? WHERE conversation_id = ?",
                    (matched_pid, json.dumps([ws_uri]), conversation_id),
                )
            else:
                conn.execute(
                    "UPDATE conversation_summaries SET project_id = ? WHERE conversation_id = ?",
                    (matched_pid, conversation_id),
                )
            conn.commit()
            conn.close()
        except Exception as e:
            return {"ok": False, "error": str(e)}
    TRANSCRIPT_CACHE.pop(conversation_id, None)
    return {"ok": True, "conversation_id": conversation_id, "project_id": matched_pid, "workspace_uri": ws_uri}


def delete_conversation(conversation_id: str) -> Dict[str, Any]:
    """Delete a conversation from Language Server, conversation_summaries.db, and local cache."""
    if not conversation_id:
        return {"ok": False, "error": "Missing conversation_id"}
    _call_ls_json("CancelCascadeInvocation", {"cascadeId": conversation_id}, timeout=1.5)
    _call_ls_json("DeleteCascadeTrajectory", {"cascadeId": conversation_id}, timeout=2.5)
    HIDDEN_CONVERSATION_IDS.add(conversation_id)
    TRANSCRIPT_CACHE.pop(conversation_id, None)
    CONVERSATION_MESSAGE_QUEUES.pop(conversation_id, None)
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
            conn.execute("DELETE FROM conversation_summaries WHERE conversation_id = ?", (conversation_id,))
            try:
                conn.execute("DELETE FROM conversations_fts WHERE conversation_id = ?", (conversation_id,))
            except Exception:
                pass
            conn.commit()
            conn.close()
        except Exception:
            pass
    return {"ok": True, "conversation_id": conversation_id}


async def _run_background_cmd(cmd: List[str], cwd: Optional[str] = None) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=cwd,
        )
        await proc.wait()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Pure Python RFC 6455 WebSocket & HTTP Server
# ---------------------------------------------------------------------------
WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketClient:
    def __init__(self, writer: asyncio.StreamWriter):
        self.writer = writer
        self.subscribed_conversation_id: Optional[str] = None
        self.closed = False

    async def send_json(self, data: Any) -> None:
        if self.closed:
            return
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        length = len(payload)
        header = bytearray([0x81])  # FIN + text frame
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(127)
            header.extend(struct.pack("!Q", length))
        try:
            self.writer.write(header + payload)
            await self.writer.drain()
        except Exception:
            self.closed = True


CONNECTED_WS_CLIENTS: Set[WebSocketClient] = set()


async def read_ws_frame(reader: asyncio.StreamReader) -> Optional[str]:
    try:
        b1, b2 = await reader.readexactly(2)
    except Exception:
        return None
    opcode = b1 & 0x0F
    if opcode == 0x8:  # Close frame
        return None
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        ext = await reader.readexactly(2)
        length = struct.unpack("!H", ext)[0]
    elif length == 127:
        ext = await reader.readexactly(8)
        length = struct.unpack("!Q", ext)[0]
    mask_key = await reader.readexactly(4) if masked else b""
    data = bytearray(await reader.readexactly(length))
    if masked:
        for i in range(length):
            data[i] ^= mask_key[i % 4]
    if opcode == 0x1:
        return data.decode("utf-8", errors="replace")
    return ""


_CACHED_APK_BADGING: Dict[str, Any] = {"sig": "", "code": 0, "name": ""}


def get_apk_version_info() -> Dict[str, Any]:
    apk_file = DIST_DIR / "AntigravityMobile.apk"
    manifest_file = PROJECT_ROOT / "android" / "AndroidManifest.xml"
    v_code = 4
    v_name = "2.2.0"
    apk_exists = apk_file.is_file() and apk_file.stat().st_size > 10000
    apk_size = 0
    apk_mtime = 0
    apk_sha256 = ""
    if apk_exists:
        try:
            st = apk_file.stat()
            apk_size = st.st_size
            apk_mtime = int(st.st_mtime)
            raw_bytes = apk_file.read_bytes()
            apk_sha256 = hashlib.sha256(raw_bytes).hexdigest()[:12]
            sig = f"{apk_mtime}:{apk_size}:{apk_sha256}"
            if _CACHED_APK_BADGING.get("sig") == sig and _CACHED_APK_BADGING.get("code"):
                v_code = _CACHED_APK_BADGING["code"]
                v_name = _CACHED_APK_BADGING["name"]
            else:
                aapt_bin = Path("/opt/homebrew/share/android-commandlinetools/build-tools/34.0.0/aapt")
                if aapt_bin.exists():
                    out = subprocess.check_output([str(aapt_bin), "dump", "badging", str(apk_file)], text=True, timeout=2.0)
                    mc = re.search(r"versionCode='(\d+)'", out)
                    mn = re.search(r"versionName='([^']+)'", out)
                    if mc and mn:
                        v_code = int(mc.group(1))
                        v_name = mn.group(1)
                        _CACHED_APK_BADGING.update({"sig": sig, "code": v_code, "name": v_name})
        except Exception:
            pass
    if v_code <= 4 and manifest_file.is_file():
        try:
            m_txt = manifest_file.read_text(encoding="utf-8", errors="replace")
            mc = re.search(r'android:versionCode="(\d+)"', m_txt)
            mn = re.search(r'android:versionName="([^"]+)"', m_txt)
            if mc:
                v_code = int(mc.group(1))
            if mn:
                v_name = mn.group(1)
        except Exception:
            pass
    return {
        "apk_available": apk_exists,
        "apk_size": apk_size,
        "apk_mtime": apk_mtime,
        "apk_sha256": apk_sha256,
        "apk_version_code": v_code,
        "apk_version_name": v_name,
    }


async def live_watcher_loop() -> None:
    """Monitors DB, APK build updates, auto-dequeues queued messages when tasks finish, and pushes real-time deltas over WebSocket."""
    last_db_mtime = 0.0
    last_apk_mtime = 0.0
    apk_init = DIST_DIR / "AntigravityMobile.apk"
    if apk_init.is_file():
        last_apk_mtime = apk_init.stat().st_mtime
    last_convs_sig: Any = None

    while True:
        await asyncio.sleep(0.5)

        # 0. Auto-dequeue queued messages for any conversation whose running task has completed
        try:
            for q_cid in list(CONVERSATION_MESSAGE_QUEUES.keys()):
                q_list = CONVERSATION_MESSAGE_QUEUES.get(q_cid)
                if not q_list:
                    continue
                now = time.time()
                if now - LAST_DEQUEUE_TS.get(q_cid, 0.0) < 2.5 or now - RECENTLY_ACTIVE_CONVS.get(q_cid, 0.0) < 2.5:
                    continue
                if not is_conversation_currently_running(q_cid):
                    next_msg = q_list.pop(0)
                    LAST_DEQUEUE_TS[q_cid] = now
                    await broadcast_queue_update(q_cid)
                    await send_message_to_conversation(
                        conversation_id=q_cid,
                        content=next_msg.get("content", ""),
                        model=next_msg.get("model", "gemini-4-argon-high"),
                        effort=next_msg.get("effort", "high"),
                        mode=next_msg.get("mode", "auto"),
                        force_immediate=True,
                    )
        except Exception:
            pass

        if not CONNECTED_WS_CLIENTS:
            continue

        try:
            # 0b. Check if a new Android APK was built on the Mac
            apk_file = DIST_DIR / "AntigravityMobile.apk"
            if apk_file.is_file():
                amtime = apk_file.stat().st_mtime
                if amtime != last_apk_mtime:
                    last_apk_mtime = amtime
                    apk_info = get_apk_version_info()
                    for client in list(CONNECTED_WS_CLIENTS):
                        await client.send_json({"type": "apk_updated", **apk_info})

            # 1. Check if conversation_summaries.db changed
            if DB_PATH.exists():
                mtime = DB_PATH.stat().st_mtime
                wal_path = Path(str(DB_PATH) + "-wal")
                if wal_path.exists():
                    mtime = max(mtime, wal_path.stat().st_mtime)
                if mtime != last_db_mtime:
                    last_db_mtime = mtime
                    convs = query_conversations(limit=50)
                    convs_sig = tuple(
                        (c["conversation_id"], c["title"], c["is_running"], c["project_id"])
                        for c in convs
                    )
                    if convs_sig != last_convs_sig:
                        last_convs_sig = convs_sig
                        for client in list(CONNECTED_WS_CLIENTS):
                            await client.send_json({"type": "conversations_updated", "conversations": convs})

            # 2. Check subscribed conversations' transcript.jsonl & live running steps
            watched_cids = {
                c.subscribed_conversation_id
                for c in CONNECTED_WS_CLIENTS
                if c.subscribed_conversation_id
            }
            for cid in watched_cids:
                if not cid:
                    continue
                tpath = BRAIN_DIR / cid / ".system_generated" / "logs" / "transcript.jsonl"
                ts_size = tpath.stat().st_size if tpath.exists() else 0
                ts_mtime = tpath.stat().st_mtime if tpath.exists() else 0.0
                summary_now = fetch_conversation_summary_row(cid)
                is_active_now = bool(summary_now.get("is_running")) or (time.time() - RECENTLY_ACTIVE_CONVS.get(cid, 0.0) < 45.0)

                if is_active_now:
                    detail = parse_conversation_transcript(cid, force_live=True)
                    tail_steps = (detail.get("steps") or [])[-3:]
                    live_sig = (
                        ts_size,
                        ts_mtime,
                        bool(detail.get("summary", {}).get("is_running")),
                        len(detail.get("steps") or []),
                        json.dumps(tail_steps, sort_keys=True, ensure_ascii=False),
                    )
                    if LAST_CONV_SIG.get(cid) != live_sig:
                        LAST_CONV_SIG[cid] = live_sig
                        for client in list(CONNECTED_WS_CLIENTS):
                            if client.subscribed_conversation_id == cid:
                                await client.send_json({"type": "conversation_detail", "data": detail})
                else:
                    sig = (ts_size, ts_mtime, False)
                    if LAST_CONV_SIG.get(cid) != sig:
                        LAST_CONV_SIG[cid] = sig
                        detail = parse_conversation_transcript(cid)
                        for client in list(CONNECTED_WS_CLIENTS):
                            if client.subscribed_conversation_id == cid:
                                await client.send_json({"type": "conversation_detail", "data": detail})
        except Exception:
            pass


_ACCEPT_GZIP_VAR: contextvars.ContextVar[bool] = contextvars.ContextVar("accept_gzip", default=False)


def send_http_response(
    writer: asyncio.StreamWriter,
    status: int,
    body: bytes,
    content_type: str = "application/json; charset=utf-8",
    extra_headers: Optional[Dict[str, str]] = None,
) -> None:
    reasons = {200: "OK", 204: "No Content", 206: "Partial Content", 400: "Bad Request", 404: "Not Found", 500: "Internal Server Error"}
    reason = reasons.get(status, "OK")
    is_compressible = content_type.startswith(("application/json", "text/html", "text/plain"))
    compressed = False
    if _ACCEPT_GZIP_VAR.get() and is_compressible and len(body) > 1024:
        body = gzip.compress(body, compresslevel=5)
        compressed = True

    headers = [
        f"HTTP/1.1 {status} {reason}",
        f"Content-Type: {content_type}",
        f"Content-Length: {len(body)}",
        "Access-Control-Allow-Origin: *",
        "Access-Control-Allow-Methods: GET, POST, OPTIONS",
        "Access-Control-Allow-Headers: Content-Type, Authorization, Range",
        "Cache-Control: no-cache",
        "Connection: close",
    ]
    if compressed:
        headers.append("Content-Encoding: gzip")
    if extra_headers:
        for k, v in extra_headers.items():
            headers.append(f"{k}: {v}")
    raw = ("\r\n".join(headers) + "\r\n\r\n").encode("utf-8") + body
    writer.write(raw)


async def stream_file_response(
    writer: asyncio.StreamWriter,
    target: Path,
    content_type: str,
    status: int = 200,
    start_b: int = 0,
    end_b: Optional[int] = None,
    extra_headers: Optional[Dict[str, str]] = None,
) -> None:
    """Stream large files in 256KB chunks without buffering the entire file in RAM."""
    total_size = target.stat().st_size
    if end_b is None:
        end_b = max(0, total_size - 1)
    content_len = max(0, end_b - start_b + 1) if total_size > 0 else 0
    reason = "Partial Content" if status == 206 else "OK"
    headers = [
        f"HTTP/1.1 {status} {reason}",
        f"Content-Type: {content_type}",
        f"Content-Length: {content_len}",
        "Access-Control-Allow-Origin: *",
        "Access-Control-Allow-Methods: GET, POST, OPTIONS",
        "Access-Control-Allow-Headers: Content-Type, Authorization, Range",
        "Cache-Control: no-cache",
        "Connection: close",
    ]
    if extra_headers:
        for k, v in extra_headers.items():
            headers.append(f"{k}: {v}")
    writer.write(("\r\n".join(headers) + "\r\n\r\n").encode("utf-8"))
    await writer.drain()
    if content_len <= 0:
        return
    chunk_size = 256 * 1024
    remaining = content_len
    with target.open("rb") as fp:
        if start_b > 0:
            fp.seek(start_b)
        while remaining > 0:
            chunk = fp.read(min(chunk_size, remaining))
            if not chunk:
                break
            writer.write(chunk)
            remaining -= len(chunk)
            await writer.drain()


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=10.0)
        if not request_line:
            writer.close()
            return
        parts = request_line.decode("utf-8", errors="replace").strip().split(" ")
        if len(parts) < 2:
            writer.close()
            return
        method, raw_target = parts[0].upper(), parts[1]

        headers: Dict[str, str] = {}
        while True:
            hline = await reader.readline()
            if not hline or hline in (b"\r\n", b"\n"):
                break
            hstr = hline.decode("utf-8", errors="replace").strip()
            if ":" in hstr:
                hk, hv = hstr.split(":", 1)
                headers[hk.strip().lower()] = hv.strip()

        _ACCEPT_GZIP_VAR.set("gzip" in headers.get("accept-encoding", "").lower())

        if method == "OPTIONS":
            send_http_response(writer, 204, b"")
            await writer.drain()
            writer.close()
            return

        parsed_url = urllib.parse.urlparse(raw_target)
        path = parsed_url.path
        qs = urllib.parse.parse_qs(parsed_url.query)

        # WebSocket Upgrade (/ws)
        if headers.get("upgrade", "").lower() == "websocket" and path == "/ws":
            ws_key = headers.get("sec-websocket-key", "")
            accept_raw = hashlib.sha1((ws_key + WS_MAGIC).encode("utf-8")).digest()
            accept_key = base64.b64encode(accept_raw).decode("utf-8")
            handshake = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept_key}\r\n\r\n"
            )
            writer.write(handshake.encode("utf-8"))
            await writer.drain()

            ws_client = WebSocketClient(writer)
            CONNECTED_WS_CLIENTS.add(ws_client)
            try:
                await ws_client.send_json({
                    "type": "connected",
                    "conversations": query_conversations(limit=50),
                })
                while not ws_client.closed:
                    msg_text = await read_ws_frame(reader)
                    if msg_text is None:
                        break
                    if not msg_text:
                        continue
                    try:
                        cmd = json.loads(msg_text)
                        if cmd.get("action") == "subscribe":
                            cid = cmd.get("conversation_id")
                            ws_client.subscribed_conversation_id = cid
                            if cid:
                                tpath = BRAIN_DIR / cid / ".system_generated" / "logs" / "transcript.jsonl"
                                ts_size = tpath.stat().st_size if tpath.exists() else 0
                                ts_mtime = tpath.stat().st_mtime if tpath.exists() else 0.0
                                LAST_CONV_SIG[cid] = (ts_size, ts_mtime)
                                if not cmd.get("skip_initial"):
                                    detail = parse_conversation_transcript(cid)
                                    await ws_client.send_json({"type": "conversation_detail", "data": detail})
                        elif cmd.get("action") == "ping":
                            await ws_client.send_json({"type": "pong", "ts": time.time()})
                    except Exception:
                        pass
            finally:
                ws_client.closed = True
                CONNECTED_WS_CLIENTS.discard(ws_client)
                writer.close()
            return

        # Read request body if POST
        body_bytes = b""
        content_len = int(headers.get("content-length", "0") or "0")
        if content_len > 0:
            body_bytes = await reader.readexactly(content_len)

        # Route HTTP endpoints
        if path in ("/", "/index.html"):
            index_file = WEB_DIR / "index.html"
            if index_file.exists():
                send_http_response(writer, 200, index_file.read_bytes(), "text/html; charset=utf-8")
            else:
                send_http_response(writer, 404, b"index.html not found", "text/plain")

        elif path in ("/favicon.png", "/favicon.ico"):
            fav_file = WEB_DIR / "favicon.png"
            if fav_file.exists():
                send_http_response(writer, 200, fav_file.read_bytes(), "image/png")
            else:
                send_http_response(writer, 404, b"", "text/plain")

        elif path == "/download/AntigravityMobile.apk":
            apk_file = DIST_DIR / "AntigravityMobile.apk"
            if apk_file.exists():
                send_http_response(
                    writer,
                    200,
                    apk_file.read_bytes(),
                    "application/vnd.android.package-archive",
                    {
                        "Content-Disposition": 'attachment; filename="AntigravityMobile.apk"',
                        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                        "Pragma": "no-cache",
                    },
                )
            else:
                send_http_response(writer, 404, b"APK not built yet", "text/plain")

        elif path in ("/api/status", "/api/app/version"):
            ls_env = load_ls_environment()
            lan_ips = get_lan_ips()
            ts_info = get_tailscale_info()
            apk_info = get_apk_version_info()
            quota_info = fetch_user_quota_and_status(force_refresh=False)
            resp = {
                "ok": True,
                "hostname": socket.gethostname(),
                "port": PORT,
                "home_dir": str(HOME),
                "brain_dir": str(BRAIN_DIR),
                "lan_ips": lan_ips,
                "vps_public_url": VPS_PUBLIC_URL,
                "vps_tunnel_connected": VPS_TUNNEL_CONNECTED,
                "tailscale": ts_info,
                "ls_address": ls_env.get("ANTIGRAVITY_LS_ADDRESS", ""),
                "desktop_bridge_ready": bool(ls_env.get("ANTIGRAVITY_LS_ADDRESS")),
                "models": AVAILABLE_MODELS,
                "quota": quota_info,
                **apk_info,
            }
            send_http_response(writer, 200, json.dumps(resp, ensure_ascii=False).encode("utf-8"))

        elif path == "/api/quota":
            force_ref = (qs.get("refresh") or ["0"])[0] == "1"
            quota_resp = fetch_user_quota_and_status(force_refresh=force_ref)
            send_http_response(writer, 200, json.dumps(quota_resp, ensure_ascii=False).encode("utf-8"))

        elif path == "/api/projects":
            resp = {"ok": True, "projects": load_projects()}
            send_http_response(writer, 200, json.dumps(resp, ensure_ascii=False).encode("utf-8"))

        elif path == "/api/context/options":
            resp = {"ok": True, **load_context_options()}
            send_http_response(writer, 200, json.dumps(resp, ensure_ascii=False).encode("utf-8"))

        elif path == "/api/workspace/files":
            cid = (qs.get("conversation_id") or [""])[0]
            q = (qs.get("q") or [""])[0]
            kind = (qs.get("kind") or ["all"])[0]
            limit = int((qs.get("limit") or ["40"])[0])
            items = search_workspace_items(conversation_id=cid, query=q, kind=kind, limit=limit)
            send_http_response(writer, 200, json.dumps({"ok": True, "items": items}, ensure_ascii=False).encode("utf-8"))

        elif path == "/api/upload" and method == "POST":
            ctype = headers.get("content-type", "").lower()
            q_cid = (qs.get("conversation_id") or [""])[0]
            q_fn = (qs.get("filename") or [""])[0]
            if q_fn or ctype.startswith("application/octet-stream") or not body_bytes.lstrip().startswith(b"{"):
                res = save_uploaded_media(
                    conversation_id=q_cid or "shared",
                    filename=urllib.parse.unquote(q_fn or "upload.bin"),
                    raw_bytes=body_bytes,
                )
            else:
                payload = json.loads(body_bytes.decode("utf-8", errors="replace") or "{}")
                res = save_uploaded_media(
                    conversation_id=payload.get("conversation_id", "") or q_cid,
                    filename=payload.get("filename", "upload.bin"),
                    data_b64=payload.get("data_base64", ""),
                )
            send_http_response(writer, 200, json.dumps(res, ensure_ascii=False).encode("utf-8"))

        elif path == "/api/conversations":
            project_id = (qs.get("project_id") or [""])[0]
            q = (qs.get("q") or [""])[0]
            limit = int((qs.get("limit") or ["80"])[0])
            convs = query_conversations(project_id=project_id, search_query=q, limit=limit)
            send_http_response(writer, 200, json.dumps({"ok": True, "conversations": convs}, ensure_ascii=False).encode("utf-8"))

        elif path == "/api/conversations/new" and method == "POST":
            payload = json.loads(body_bytes.decode("utf-8", errors="replace") or "{}")
            res = await create_new_conversation(
                prompt=payload.get("prompt", ""),
                title=payload.get("title", ""),
                model=payload.get("model", "gemini-4-argon-high"),
                effort=payload.get("effort", "high"),
                mode=payload.get("mode", "auto"),
                engine=payload.get("engine", "auto"),
                workspace=payload.get("workspace", ""),
                project_id=payload.get("project_id", ""),
            )
            send_http_response(writer, 200, json.dumps(res, ensure_ascii=False).encode("utf-8"))

        elif path.startswith("/api/conversations/"):
            subparts = [p for p in path.split("/") if p]
            # ["api", "conversations", "<id>", ...]
            if len(subparts) == 3 and method == "GET":
                cid = subparts[2]
                detail = parse_conversation_transcript(cid)
                send_http_response(writer, 200, json.dumps({"ok": True, **detail}, ensure_ascii=False).encode("utf-8"))

            elif len(subparts) == 4 and subparts[3] == "message" and method == "POST":
                cid = subparts[2]
                payload = json.loads(body_bytes.decode("utf-8", errors="replace") or "{}")
                res = await send_message_to_conversation(
                    conversation_id=cid,
                    content=payload.get("content", ""),
                    model=payload.get("model", "gemini-4-argon-high"),
                    effort=payload.get("effort", "high"),
                    mode=payload.get("mode", "auto"),
                    engine=payload.get("engine", "auto"),
                    force_immediate=bool(payload.get("force_immediate", False)),
                )
                send_http_response(writer, 200, json.dumps(res, ensure_ascii=False).encode("utf-8"))

            elif len(subparts) == 5 and subparts[3] == "queue" and subparts[4] == "delete" and method == "POST":
                cid = subparts[2]
                payload = json.loads(body_bytes.decode("utf-8", errors="replace") or "{}")
                qid = payload.get("id", "")
                q_list = CONVERSATION_MESSAGE_QUEUES.setdefault(cid, [])
                if qid == "all":
                    q_list.clear()
                else:
                    CONVERSATION_MESSAGE_QUEUES[cid] = [it for it in q_list if it.get("id") != qid]
                await broadcast_queue_update(cid)
                send_http_response(
                    writer,
                    200,
                    json.dumps({
                        "ok": True,
                        "conversation_id": cid,
                        "queued_messages": list(CONVERSATION_MESSAGE_QUEUES.get(cid, [])),
                    }, ensure_ascii=False).encode("utf-8"),
                )

            elif len(subparts) == 5 and subparts[3] == "queue" and subparts[4] == "send-now" and method == "POST":
                cid = subparts[2]
                payload = json.loads(body_bytes.decode("utf-8", errors="replace") or "{}")
                qid = payload.get("id", "")
                q_list = CONVERSATION_MESSAGE_QUEUES.setdefault(cid, [])
                target_item = None
                if qid and qid != "all":
                    for idx_q, it in enumerate(q_list):
                        if it.get("id") == qid:
                            target_item = q_list.pop(idx_q)
                            break
                elif q_list:
                    merged_text = "\n\n".join(it.get("content", "") for it in q_list if it.get("content"))
                    first = q_list[0]
                    q_list.clear()
                    target_item = {
                        "content": merged_text,
                        "model": first.get("model", "gemini-4-argon-high"),
                        "effort": first.get("effort", "high"),
                        "mode": first.get("mode", "auto"),
                    }
                await broadcast_queue_update(cid)
                if target_item and target_item.get("content"):
                    res = await send_message_to_conversation(
                        conversation_id=cid,
                        content=target_item["content"],
                        model=target_item.get("model", "gemini-4-argon-high"),
                        effort=target_item.get("effort", "high"),
                        mode=target_item.get("mode", "auto"),
                        force_immediate=True,
                    )
                    send_http_response(writer, 200, json.dumps(res, ensure_ascii=False).encode("utf-8"))
                else:
                    send_http_response(writer, 200, json.dumps({"ok": False, "error": "Queue item not found"}).encode("utf-8"))

            elif len(subparts) == 4 and subparts[3] == "cancel" and method == "POST":
                cid = subparts[2]
                _call_ls_json("CancelCascadeInvocation", {"cascadeId": cid}, timeout=2.0)
                send_http_response(writer, 200, json.dumps({"ok": True, "conversation_id": cid}).encode("utf-8"))

            elif len(subparts) == 4 and subparts[3] == "rename" and method == "POST":
                cid = subparts[2]
                payload = json.loads(body_bytes.decode("utf-8", errors="replace") or "{}")
                res = rename_conversation(cid, payload.get("title", ""))
                send_http_response(writer, 200, json.dumps(res, ensure_ascii=False).encode("utf-8"))

            elif len(subparts) == 4 and subparts[3] == "move" and method == "POST":
                cid = subparts[2]
                payload = json.loads(body_bytes.decode("utf-8", errors="replace") or "{}")
                res = move_conversation_to_project(
                    cid,
                    project_id=payload.get("project_id", ""),
                    workspace=payload.get("workspace", ""),
                )
                send_http_response(writer, 200, json.dumps(res, ensure_ascii=False).encode("utf-8"))

            elif len(subparts) == 4 and subparts[3] == "delete" and method == "POST":
                cid = subparts[2]
                res = delete_conversation(cid)
                send_http_response(writer, 200, json.dumps(res, ensure_ascii=False).encode("utf-8"))

            elif len(subparts) == 4 and subparts[3] == "artifact" and method == "GET":
                cid = subparts[2]
                name = (qs.get("name") or [""])[0]
                target = (BRAIN_DIR / cid / name).resolve()
                conv_root = (BRAIN_DIR / cid).resolve()
                if str(target).startswith(str(conv_root)) and target.is_file():
                    raw = qs.get("raw", ["0"])[0] == "1"
                    download = qs.get("download", ["0"])[0] == "1"
                    ext = target.suffix.lower()
                    extra_hdrs: Dict[str, str] = {}
                    if download:
                        ascii_fn = re.sub(r"[^a-zA-Z0-9._-]", "_", target.name)
                        quoted_fn = urllib.parse.quote(target.name)
                        extra_hdrs["Content-Disposition"] = f"attachment; filename=\"{ascii_fn}\"; filename*=UTF-8''{quoted_fn}"
                    if download or raw or ext in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".pdf", ".mp4", ".mov", ".webm", ".mp3", ".wav", ".zip", ".apk"}:
                        mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                        send_http_response(writer, 200, target.read_bytes(), mime, extra_headers=extra_hdrs)
                    else:
                        text = target.read_text(encoding="utf-8", errors="replace")
                        send_http_response(
                            writer,
                            200,
                            json.dumps({"ok": True, "name": name, "path": str(target), "content": text}, ensure_ascii=False).encode("utf-8"),
                        )
                else:
                    send_http_response(writer, 404, json.dumps({"ok": False, "error": "Artifact not found"}).encode("utf-8"))

            elif len(subparts) == 4 and subparts[3] == "task" and method == "GET":
                cid = subparts[2]
                name = (qs.get("name") or [""])[0]
                target = (BRAIN_DIR / cid / ".system_generated" / "tasks" / name).resolve()
                if target.is_file():
                    text = target.read_text(encoding="utf-8", errors="replace")[-30000:]
                    send_http_response(
                        writer,
                        200,
                        json.dumps({"ok": True, "name": name, "content": text}, ensure_ascii=False).encode("utf-8"),
                    )
                else:
                    send_http_response(writer, 404, json.dumps({"ok": False, "error": "Task log not found"}).encode("utf-8"))
            else:
                send_http_response(writer, 404, json.dumps({"ok": False, "error": "Unknown endpoint"}).encode("utf-8"))

        elif path in ("/api/file", "/api/media") and method == "GET":
            fpath = (qs.get("path") or [""])[0].strip()
            cid = (qs.get("conversation_id") or [""])[0].strip()
            if fpath.startswith("file://"):
                fpath = urllib.parse.unquote(fpath[7:])
            # Strip #L10-L20 anchor or :line:col suffix if present
            if "#" in fpath:
                fpath = fpath.split("#", 1)[0]
            m_line_suffix = re.match(r"^(.+\.[a-zA-Z0-9_+-]+):\d+(?::\d+)?$", fpath)
            if m_line_suffix:
                fpath = m_line_suffix.group(1)

            target: Optional[Path] = None
            cand = Path(fpath).expanduser()
            if cand.is_absolute() and cand.is_file():
                target = cand.resolve()
            else:
                # Resolve relative path or filename against conversation workspace roots & common roots
                roots: List[Path] = []
                if cid:
                    row = fetch_conversation_summary_row(cid)
                    raw_ws = row.get("workspace_uris") or ""
                    if raw_ws:
                        try:
                            for u in json.loads(raw_ws):
                                if isinstance(u, str) and u.startswith("file://"):
                                    p = Path(urllib.parse.unquote(u[7:]))
                                    if p.exists() and p not in roots:
                                        roots.append(p)
                        except Exception:
                            pass
                    proj_id = row.get("project_id") or ""
                    if proj_id:
                        for pr in load_projects():
                            if pr["id"] == proj_id:
                                for fld in pr.get("folders") or []:
                                    p = Path(fld)
                                    if p.exists() and p not in roots:
                                        roots.append(p)
                    c_brain = BRAIN_DIR / cid
                    if c_brain.exists() and c_brain not in roots:
                        roots.append(c_brain)
                for fb in (APP_DATA_DIR / "scratch", HOME / "Desktop", HOME):
                    if fb.exists() and fb not in roots:
                        roots.append(fb)
                rel_clean = fpath.lstrip("/")
                for r in roots:
                    c_try = (r / rel_clean)
                    if c_try.is_file():
                        target = c_try.resolve()
                        break
                    # Also check one level inside scratch subdirectories
                    if r.name == "scratch" and r.is_dir():
                        for sub in r.iterdir():
                            if sub.is_dir() and (sub / rel_clean).is_file():
                                target = (sub / rel_clean).resolve()
                                break
                        if target:
                            break
                if not target and rel_clean:
                    matches = search_workspace_items(conversation_id=cid, query=Path(rel_clean).name, kind="file", limit=15)
                    for m_it in matches:
                        mp = Path(m_it["path"])
                        if mp.is_file() and (mp.name == Path(rel_clean).name or str(mp).endswith(rel_clean)):
                            target = mp.resolve()
                            break

            if target and target.is_file():
                raw = (qs.get("raw") or ["0"])[0] == "1" or path == "/api/media"
                download = (qs.get("download") or ["0"])[0] == "1"
                ext = target.suffix.lower()
                binary_exts = {
                    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico", ".bmp", ".avif",
                    ".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".3gp", ".mp3", ".wav", ".m4a", ".ogg",
                    ".pdf", ".zip", ".tar", ".gz", ".tgz", ".7z", ".rar",
                    ".apk", ".dmg", ".pkg", ".exe", ".bin", ".db", ".sqlite", ".sqlite3", ".woff", ".woff2", ".ttf",
                }
                extra_hdrs = {}
                quoted_fn = urllib.parse.quote(target.name)
                extra_hdrs["X-Resolved-Filename"] = quoted_fn
                extra_hdrs["Access-Control-Expose-Headers"] = "Content-Disposition, X-Resolved-Filename, Content-Range, Accept-Ranges"
                if download:
                    ascii_fn = re.sub(r"[^a-zA-Z0-9._-]", "_", target.name)
                    extra_hdrs["Content-Disposition"] = f"attachment; filename=\"{ascii_fn}\"; filename*=UTF-8''{quoted_fn}"
                if download or raw or ext in IMAGE_EXTS:
                    mime = "application/octet-stream" if download else (mimetypes.guess_type(str(target))[0] or "application/octet-stream")
                    extra_hdrs["Accept-Ranges"] = "bytes"
                    total_size = target.stat().st_size
                    range_hdr = headers.get("range", "").strip()
                    if range_hdr.startswith("bytes=") and not download and total_size > 0:
                        try:
                            r_spec = range_hdr[6:].split(",")[0].strip()
                            s_str, e_str = r_spec.split("-", 1)
                            start_b = int(s_str) if s_str else max(0, total_size - int(e_str))
                            end_b = int(e_str) if (s_str and e_str) else (total_size - 1)
                            start_b = max(0, min(start_b, total_size - 1))
                            end_b = max(start_b, min(end_b, total_size - 1))
                            extra_hdrs["Content-Range"] = f"bytes {start_b}-{end_b}/{total_size}"
                            await stream_file_response(writer, target, mime, status=206, start_b=start_b, end_b=end_b, extra_headers=extra_hdrs)
                        except Exception:
                            await stream_file_response(writer, target, mime, status=200, extra_headers=extra_hdrs)
                    else:
                        await stream_file_response(writer, target, mime, status=200, extra_headers=extra_hdrs)
                elif ext in binary_exts:
                    stat = target.stat()
                    send_http_response(
                        writer,
                        200,
                        json.dumps({
                            "ok": True,
                            "path": str(target),
                            "name": target.name,
                            "size": stat.st_size,
                            "ext": ext,
                            "is_binary": True,
                            "content": "",
                        }, ensure_ascii=False).encode("utf-8"),
                    )
                else:
                    stat = target.stat()
                    text = target.read_text(encoding="utf-8", errors="replace")[:120000]
                    send_http_response(
                        writer,
                        200,
                        json.dumps({
                            "ok": True,
                            "path": str(target),
                            "name": target.name,
                            "size": stat.st_size,
                            "ext": ext,
                            "is_binary": False,
                            "content": text,
                        }, ensure_ascii=False).encode("utf-8"),
                    )
            else:
                send_http_response(writer, 404, json.dumps({"ok": False, "error": f"File not found: {fpath}"}).encode("utf-8"))

        else:
            send_http_response(writer, 404, json.dumps({"ok": False, "error": "Not found"}).encode("utf-8"))

        await writer.drain()
        if writer.can_write_eof():
            try:
                writer.write_eof()
            except Exception:
                pass
    except Exception as e:
        try:
            send_http_response(writer, 500, json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))
            await writer.drain()
        except Exception:
            pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def vps_reverse_tunnel_loop() -> None:
    """Keeps an optional persistent SSH reverse tunnel from a cloud relay host -> local gateway port."""
    global VPS_TUNNEL_CONNECTED
    if not VPS_SSH_HOST:
        return
    while True:
        try:
            clean_proc = await asyncio.create_subprocess_exec(
                "/usr/bin/ssh",
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=6",
                VPS_SSH_HOST,
                "sudo fuser -k 18899/tcp 2>/dev/null || true; sudo systemctl start agy-vps-relay.service 2>/dev/null || true",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(clean_proc.wait(), timeout=8.0)
        except Exception:
            pass

        try:
            tunnel_proc = await asyncio.create_subprocess_exec(
                "/usr/bin/ssh",
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=8",
                "-o", "ServerAliveInterval=15",
                "-o", "ServerAliveCountMax=3",
                "-o", "ExitOnForwardFailure=yes",
                "-N",
                "-R", f"127.0.0.1:18899:127.0.0.1:{PORT}",
                VPS_SSH_HOST,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            VPS_TUNNEL_CONNECTED = True
            await tunnel_proc.wait()
        except Exception:
            pass
        finally:
            VPS_TUNNEL_CONNECTED = False
        await asyncio.sleep(3.0)


async def prewarm_transcript_cache() -> None:
    await asyncio.sleep(0.2)
    try:
        convs = query_conversations(limit=35)
        for c in convs:
            cid = c.get("conversation_id")
            if cid:
                parse_conversation_transcript(cid)
            await asyncio.sleep(0.01)
    except Exception:
        pass


async def main() -> None:
    save_startup_ls_environment()
    server = await asyncio.start_server(handle_client, HOST, PORT)
    lan_ips = get_lan_ips()
    ts = get_tailscale_info()
    print(f"🚀 Antigravity Mobile Gateway listening on {HOST}:{PORT}", flush=True)
    print(f"   • Public Cloud URL: {VPS_PUBLIC_URL}", flush=True)
    for ip in lan_ips:
        print(f"   • LAN URL:          http://{ip}:{PORT}", flush=True)
    if ts.get("ip"):
        print(f"   • Tailscale URL:    http://{ts['ip']}:{PORT}", flush=True)
    asyncio.create_task(prewarm_transcript_cache())
    asyncio.create_task(live_watcher_loop())
    asyncio.create_task(vps_reverse_tunnel_loop())
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
