from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import time
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.middleware.sessions import SessionMiddleware

from .ai_status import OBJECT_MODEL, get_ai_status
from .ai_detector import YoloV8Detector
from .database import DATA_DIR, connect, initialize_database, rows_to_dicts
from .security import CredentialVault

ROOT_DIR = Path(__file__).resolve().parents[2]
FRONTEND_DIR = ROOT_DIR / "frontend"
PERSON_IMAGE_DIR = DATA_DIR / "person_images"
AUDIO_TEMP_DIR = DATA_DIR / "audio_tmp"
REOLINK_XDG_DIR = DATA_DIR / "reolink_xdg"
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_TALKBACK_BYTES = 8 * 1024 * 1024
FIRST_FRAME_TIMEOUT_SECONDS = 10
MAIN_FRAME_TIMEOUT_SECONDS = 14
MAX_TTS_CHARS = 1000
REOLINK_GATEWAY_PROCESS: asyncio.subprocess.Process | None = None
AI_DETECTOR: YoloV8Detector | None = None
AI_MANAGER_TASK: asyncio.Task | None = None
AI_CAMERA_TASKS: dict[int, asyncio.Task] = {}
AI_TRACKS: dict[int, list[dict[str, Any]]] = {}
AI_INFERENCE_SEMAPHORE = asyncio.Semaphore(1)  # Serialize CPU inference to limit load on modest servers.
AI_RUNTIME_STATE: dict[str, Any] = {"status": "not-started", "message": "AI worker has not started.", "last_frame_at": None, "last_error": "", "last_inference_ms": None, "frames_processed": 0, "detections_total": 0, "camera_errors": {}, "camera_profiles": {}}
CAMERA_STREAM_SEMAPHORE = asyncio.Semaphore(8)
load_dotenv(ROOT_DIR / ".env")

vault: CredentialVault | None = None
logger = logging.getLogger("nexgard.audio")


@asynccontextmanager
async def lifespan(_: FastAPI):
    global vault, AI_MANAGER_TASK
    initialize_database()
    vault = CredentialVault()
    AUDIO_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    REOLINK_XDG_DIR.mkdir(parents=True, exist_ok=True)
    await start_reolink_gateway_if_installed()
    await initialize_ai_detector()
    AI_MANAGER_TASK = asyncio.create_task(ai_camera_manager(), name="nexgard-ai-camera-manager")
    try:
        yield
    finally:
        if AI_MANAGER_TASK is not None:
            AI_MANAGER_TASK.cancel()
            try:
                await AI_MANAGER_TASK
            except asyncio.CancelledError:
                pass
            AI_MANAGER_TASK = None
        for task in list(AI_CAMERA_TASKS.values()):
            task.cancel()
        if AI_CAMERA_TASKS:
            await asyncio.gather(*AI_CAMERA_TASKS.values(), return_exceptions=True)
        AI_CAMERA_TASKS.clear()
        await stop_reolink_gateway()


app = FastAPI(
    title="NexGard Security API",
    version="0.6.5",
    description="Local-first camera, room, storage and AI management for NexGard Security System.",
    lifespan=lifespan,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("NOVATECH_SESSION_SECRET", ""),
    same_site="lax",
    https_only=os.getenv("NOVATECH_HTTPS_ONLY", "false").lower() == "true",
    session_cookie="novatech_session",
    max_age=60 * 60 * 12,
)


def authenticated(request: Request) -> None:
    if request.session.get("authenticated") is not True:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Please sign in.")


def get_vault() -> CredentialVault:
    if vault is None:
        raise HTTPException(status_code=503, detail="Credential vault is not initialized.")
    return vault


def validate_ip(value: str) -> str:
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError as exc:
        raise ValueError("Enter a valid IPv4 or IPv6 address.") from exc


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=12, max_length=200)
    confirm_password: str = Field(min_length=12, max_length=200)

    @model_validator(mode="after")
    def passwords_must_match(self):
        if self.new_password != self.confirm_password:
            raise ValueError("New password and confirmation do not match.")
        if self.new_password == self.current_password:
            raise ValueError("Choose a new password that differs from the current password.")
        return self


class RoomCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=300)


class CameraInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    host: str
    port: int = Field(default=554, ge=1, le=65535)
    stream_path: str = Field(default="/stream1", min_length=1, max_length=300)
    username: str = Field(default="", max_length=200)
    password: str = Field(default="", max_length=300)
    room_id: int | None = None
    vendor: str = Field(default="generic", max_length=20)
    control_port: int = Field(default=9000, ge=1, le=65535)

    @field_validator("vendor")
    @classmethod
    def validate_vendor(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"generic", "reolink"}:
            raise ValueError("Camera vendor must be generic or reolink.")
        return value

    @field_validator("host")
    @classmethod
    def host_must_be_ip(cls, value: str) -> str:
        return validate_ip(value)

    @field_validator("stream_path")
    @classmethod
    def path_must_be_relative_or_absolute_path(cls, value: str) -> str:
        path = value.strip()
        if not path.startswith("/"):
            path = "/" + path
        if "\r" in path or "\n" in path:
            raise ValueError("Invalid stream path.")
        return path


class CameraAIUpdate(BaseModel):
    enabled: bool


class CameraTestInput(BaseModel):
    host: str
    port: int = Field(default=554, ge=1, le=65535)
    stream_path: str = Field(default="/Preview_01_main", min_length=1, max_length=300)
    username: str = Field(default="", max_length=200)
    password: str = Field(default="", max_length=300)
    vendor: str = Field(default="reolink", max_length=20)

    @field_validator("vendor")
    @classmethod
    def validate_vendor(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"generic", "reolink"}:
            raise ValueError("Camera vendor must be generic or reolink.")
        return value

    @field_validator("stream_path")
    @classmethod
    def path_must_be_safe(cls, value: str) -> str:
        path = value.strip()
        if not path.startswith("/"):
            path = "/" + path
        if "\r" in path or "\n" in path:
            raise ValueError("Invalid stream path.")
        return path

    @field_validator("host")
    @classmethod
    def host_must_be_ip(cls, value: str) -> str:
        return validate_ip(value)


class StorageInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    host: str
    protocol: str
    share_path: str = Field(min_length=1, max_length=300)
    mount_path: str = Field(min_length=1, max_length=300)
    username: str = Field(default="", max_length=200)
    password: str = Field(default="", max_length=300)

    @field_validator("host")
    @classmethod
    def host_must_be_ip(cls, value: str) -> str:
        return validate_ip(value)

    @field_validator("protocol")
    @classmethod
    def protocol_must_be_supported(cls, value: str) -> str:
        value = value.lower().strip()
        if value not in {"smb", "nfs"}:
            raise ValueError("Protocol must be SMB or NFS.")
        return value

    @field_validator("mount_path")
    @classmethod
    def mount_path_must_be_absolute(cls, value: str) -> str:
        path = value.strip()
        if not path.startswith("/") or ".." in Path(path).parts:
            raise ValueError("Mount path must be an absolute path without '..'.")
        return path


class AudioTextInput(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TTS_CHARS)

    @field_validator("text")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter text to speak.")
        return value


class EventInput(BaseModel):
    camera_id: int | None = None
    event_type: str = Field(min_length=1, max_length=60)
    summary: str = Field(min_length=1, max_length=500)
    confidence: float | None = Field(default=None, ge=0, le=1)


class PersonObservation(BaseModel):
    """One observation from a temporary, per-camera object tracker.

    tracker_key must be generated by the live object-tracking process. It is
    only used to join consecutive frames from the same camera session; it is
    not a face identifier and must not be reused across visits or cameras.
    """

    camera_id: int
    tracker_key: str = Field(min_length=1, max_length=80)
    confidence: float | None = Field(default=None, ge=0, le=1)
    appearance_description: str = Field(default="", max_length=300)
    snapshot_base64: str | None = Field(default=None, max_length=2_800_000)

    @field_validator("tracker_key")
    @classmethod
    def tracker_key_is_opaque_token(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", value):
            raise ValueError("Tracker key may contain only letters, numbers, dots, underscores, colons and hyphens.")
        return value


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_text(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat(timespec="seconds")


def create_public_person_id() -> str:
    # Numeric and deliberately random. It identifies a single temporary event,
    # not a person across separate visits.
    for _ in range(20):
        candidate = f"PERSON-{secrets.randbelow(100_000_000):08d}"
        with connect() as conn:
            exists = conn.execute("SELECT 1 FROM person_tracks WHERE public_id = ?", (candidate,)).fetchone()
        if not exists:
            return candidate
    raise HTTPException(status_code=503, detail="Could not allocate a unique event number. Try again.")


def decode_snapshot(value: str) -> tuple[bytes, str, str]:
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Snapshot must be valid base64 image data.") from exc
    if not raw or len(raw) > MAX_SNAPSHOT_BYTES:
        raise HTTPException(status_code=413, detail="Snapshot must be between 1 byte and 2 MB.")
    if raw.startswith(b"\xff\xd8\xff"):
        extension, media_type = ".jpg", "image/jpeg"
    elif raw.startswith(b"\x89PNG\r\n\x1a\n"):
        extension, media_type = ".png", "image/png"
    else:
        raise HTTPException(status_code=415, detail="Only JPEG and PNG snapshots are accepted.")
    return raw, extension, media_type


def store_person_snapshot(track_db_id: int, public_id: str, encoded: str) -> dict[str, Any]:
    raw, extension, media_type = decode_snapshot(encoded)
    folder = PERSON_IMAGE_DIR / public_id
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:10]}{extension}"
    destination = folder / filename
    destination.write_bytes(raw)
    relative_path = destination.relative_to(DATA_DIR).as_posix()
    captured_at = utc_text()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        with connect() as conn:
            cursor = conn.execute(
                "INSERT INTO person_snapshots(track_id, relative_path, captured_at, media_type, file_size, sha256) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (track_db_id, relative_path, captured_at, media_type, len(raw), digest),
            )
            snapshot_id = cursor.lastrowid
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return {
        "id": snapshot_id,
        "captured_at": captured_at,
        "media_type": media_type,
        "file_size": len(raw),
        "url": f"/api/person-snapshots/{snapshot_id}",
    }


def serialize_person_track(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["snapshot_url"] = f"/api/person-snapshots/{item['latest_snapshot_id']}" if item.get("latest_snapshot_id") else None
    return item


def reolink_cli_path() -> str | None:
    configured = os.getenv("NEXGARD_REOLINK_CLI", "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        return str(candidate) if candidate.is_file() else shutil.which(configured)
    found = shutil.which("reolink-cli")
    if found:
        return found
    # The official Reolink installer uses ~/.local/bin and systemd's default
    # PATH does not normally include that directory.
    candidate = Path.home() / ".local" / "bin" / "reolink-cli"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def reolink_environment() -> dict[str, str]:
    env = os.environ.copy()
    local_bin = str(Path.home() / ".local" / "bin")
    env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")
    env["XDG_CONFIG_HOME"] = str(REOLINK_XDG_DIR)
    # systemd protects /home by default. Keep the CLI config, cache and state
    # beneath NexGard's explicitly writable data directory so audio commands
    # work when launched by the web service, not just in an interactive shell.
    env["XDG_CACHE_HOME"] = str(REOLINK_XDG_DIR / "cache")
    env["XDG_STATE_HOME"] = str(REOLINK_XDG_DIR / "state")
    env["XDG_DATA_HOME"] = str(REOLINK_XDG_DIR / "data")
    for directory in (env["XDG_CACHE_HOME"], env["XDG_STATE_HOME"], env["XDG_DATA_HOME"]):
        Path(directory).mkdir(parents=True, exist_ok=True)
    env["REOLINK_GATEWAY_ADDR"] = "127.0.0.1:9000"
    return env


async def run_external_command(
    command: list[str],
    *,
    input_bytes: bytes | None = None,
    timeout: float = 30,
    env: dict[str, str] | None = None,
) -> tuple[int, bytes, bytes]:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE if input_bytes is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=424, detail=f"Required program was not found: {command[0]}") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=503, detail=f"The server cannot execute {command[0]}; check file permissions and the NexGard service PATH.") from exc
    except OSError as exc:
        logger.exception("Failed to start external command: %s", command[0])
        raise HTTPException(status_code=502, detail=f"Could not start {Path(command[0]).name}: {exc.strerror or type(exc).__name__}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(input=input_bytes), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise HTTPException(status_code=504, detail="The camera or media command timed out.")
    return int(process.returncode or 0), stdout, stderr


async def start_reolink_gateway_if_installed() -> None:
    """Use an already-running systemd gateway; only spawn one when the port is free."""
    global REOLINK_GATEWAY_PROCESS
    cli = reolink_cli_path()
    if not cli:
        return
    if REOLINK_GATEWAY_PROCESS is not None and REOLINK_GATEWAY_PROCESS.returncode is None:
        return
    # The server may run reolink-gateway as its own systemd service. Do not start a
    # second copy from the web worker and then mistake its bind failure for
    # an unavailable gateway.
    if await tcp_port_open("127.0.0.1", 9000, timeout=0.35):
        return
    REOLINK_GATEWAY_PROCESS = None
    try:
        REOLINK_GATEWAY_PROCESS = await asyncio.create_subprocess_exec(
            cli, "gateway", "start", "--addr", "127.0.0.1:9000",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=reolink_environment(),
            start_new_session=True,
        )
        await wait_for_reolink_gateway(timeout=8)
    except (OSError, asyncio.SubprocessError):
        REOLINK_GATEWAY_PROCESS = None


async def wait_for_reolink_gateway(timeout: float = 8) -> bool:
    """Wait for the loopback-only gateway socket, whether systemd- or app-managed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", 9000), timeout=0.35
            )
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, asyncio.TimeoutError):
            await asyncio.sleep(0.25)
    return False


async def stop_reolink_gateway() -> None:
    global REOLINK_GATEWAY_PROCESS
    process = REOLINK_GATEWAY_PROCESS
    REOLINK_GATEWAY_PROCESS = None
    if process is not None and process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=4)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


def camera_control_record(camera_id: int) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            "SELECT id, name, host, port, stream_path, username, password_encrypted, vendor, control_port, ai_enabled "
            "FROM cameras WHERE id = ? AND enabled = 1",
            (camera_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Camera not found or disabled.")
    item = dict(row)
    item["password"] = get_vault().decrypt(item.pop("password_encrypted"))
    return item


def make_rtsp_url(camera: dict[str, Any], quality: str = "sub", stream_path: str | None = None) -> str:
    selected_path = str(stream_path or camera["stream_path"])
    if stream_path is None and quality == "sub" and selected_path.endswith("_main"):
        selected_path = selected_path[:-5] + "_sub"
    username = quote(str(camera.get("username") or ""), safe="")
    password = quote(str(camera.get("password") or ""), safe="")
    userinfo = f"{username}:{password}@" if username else ""
    return f"rtsp://{userinfo}{camera['host']}:{camera['port']}{selected_path}"


def rtsp_path_candidates(configured_path: str, vendor: str, quality: str = "main") -> list[str]:
    """Candidate RTSP paths. Reolink uses Preview_<channel>_<stream>; the browser never opens RTSP directly."""
    configured = configured_path.strip() or "/Preview_01_main"
    if not configured.startswith("/"):
        configured = "/" + configured
    candidates: list[str] = []

    def add(path: str) -> None:
        if path and path not in candidates:
            candidates.append(path)

    if quality == "sub" and configured.endswith("_main"):
        add(configured[:-5] + "_sub")
        add(configured)
    elif quality == "main" and configured.endswith("_sub"):
        add(configured[:-4] + "_main")
        add(configured)
    else:
        add(configured)

    if vendor == "reolink":
        channel_match = re.search(r"Preview_(\d{2})_", configured)
        channel = channel_match.group(1) if channel_match else "01"
        add(f"/Preview_{channel}_{quality}")
        add(f"/Preview_01_{quality}")
        add(f"/h264Preview_{channel}_{quality}")
        add(f"/h264Preview_01_{quality}")
    return candidates


async def tcp_port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        writer.close()
        await writer.wait_closed()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def probe_rtsp_path(camera: dict[str, Any], stream_path: str, timeout: float = 7.0) -> dict[str, Any]:
    """Probe a stream and require a video track; audio-only RTSP is not a live-video success."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {"connected": False, "streams": [], "video_streams": [], "audio_streams": [],
                "error": "FFprobe is not installed on the server. Install FFmpeg with: sudo apt install ffmpeg"}
    url = make_rtsp_url(camera, "main", stream_path=stream_path)
    command = [
        ffprobe, "-hide_banner", "-v", "error", "-rtsp_transport", "tcp",
        "-show_entries", "stream=index,codec_type,codec_name,width,height,channels,sample_rate",
        "-of", "json", url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout + 1.5)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"connected": False, "streams": [], "video_streams": [], "audio_streams": [],
                "error": "The RTSP request timed out."}
    except OSError as exc:
        return {"connected": False, "streams": [], "video_streams": [], "audio_streams": [],
                "error": f"Could not start FFprobe: {exc}"}

    error_text = stderr.decode("utf-8", errors="replace")[-1200:]
    # Never return a credential-bearing RTSP URL in an API response or browser toast.
    error_text = re.sub(r"rtsp://[^\s]+", "rtsp://[credentials hidden]", error_text)
    if proc.returncode != 0:
        lower = error_text.lower()
        if "401" in lower or "unauthorized" in lower or "authentication" in lower:
            reason = "RTSP is reachable, but the camera rejected the username or password."
        elif "404" in lower or "not found" in lower:
            reason = "RTSP is reachable, but this stream path was not found."
        elif "connection refused" in lower:
            reason = "The camera refused the RTSP connection. RTSP may be disabled or the port may differ."
        elif "timed out" in lower or "timeout" in lower:
            reason = "The camera did not answer the RTSP request before timeout."
        else:
            reason = "The camera did not return a usable RTSP media stream for this path."
        return {"connected": False, "streams": [], "video_streams": [], "audio_streams": [],
                "error": reason, "diagnostic": error_text}
    try:
        streams = json.loads(stdout.decode("utf-8", errors="replace")).get("streams", [])
    except json.JSONDecodeError:
        streams = []
    video_streams = [item for item in streams if item.get("codec_type") == "video"]
    audio_streams = [item for item in streams if item.get("codec_type") == "audio"]
    if not video_streams:
        return {"connected": False, "streams": streams, "video_streams": [], "audio_streams": audio_streams,
                "error": "RTSP answered, but this path did not provide a video track. NexGard skipped it."}
    return {
        "connected": True, "streams": streams, "video_streams": video_streams,
        "audio_streams": audio_streams, "error": "", "stream_path": stream_path,
    }


async def detect_rtsp_stream(camera: dict[str, Any], quality: str = "main") -> dict[str, Any]:
    """Find a usable stream path and distinguish a closed RTSP port from a bad path/login."""
    host, port = str(camera["host"]), int(camera["port"])
    rtsp_open = await tcp_port_open(host, port)
    if not rtsp_open:
        http_open = await tcp_port_open(host, 80)
        https_open = await tcp_port_open(host, 443)
        return {
            "connected": False, "streams": [], "stream_path": None,
            "rtsp_port_open": False, "http_port_open": http_open, "https_port_open": https_open,
            "message": (
                f"Camera IP {host} did not accept an RTSP connection on port {port}. "
                "The Reolink app can still work through Reolink's app connection. NexGard needs the camera's RTSP service enabled on the local network. "
                "In Reolink App, open Device Settings → Network → Advanced → Server Settings and enable RTSP (and ONVIF if available). "
                "If this is an add-on camera connected through an NVR/Home Hub, use the NVR/Home Hub IP and its channel stream instead."
            ),
        }

    errors: list[str] = []
    for candidate in rtsp_path_candidates(str(camera.get("stream_path") or "/Preview_01_main"), str(camera.get("vendor") or "generic"), quality):
        result = await probe_rtsp_path(camera, candidate)
        if result["connected"]:
            return {
                **result,
                "rtsp_port_open": True,
                "http_port_open": await tcp_port_open(host, 80),
                "https_port_open": await tcp_port_open(host, 443),
                "message": f"RTSP video found at {candidate}.",
            }
        errors.append(result.get("error", "Unknown RTSP error"))

    unique_errors = list(dict.fromkeys(errors))
    if any("rejected the username or password" in item for item in unique_errors):
        message = "The RTSP service is open, but camera login failed. Check the local camera username and password entered in NexGard."
    else:
        message = (
            "The camera's RTSP port is open, but none of the tested Reolink stream paths returned video. "
            "Check the exact model, RTSP settings, and whether this camera is an NVR add-on (which needs the NVR IP/channel)."
        )
    return {
        "connected": False, "streams": [], "stream_path": None, "rtsp_port_open": True,
        "http_port_open": await tcp_port_open(host, 80),
        "https_port_open": await tcp_port_open(host, 443),
        "message": message, "details": unique_errors,
    }


def ffmpeg_mjpeg_command(camera: dict[str, Any], quality: str, stream_path: str) -> list[str]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise HTTPException(status_code=424, detail="FFmpeg is not installed on the server. Install it with: sudo apt install ffmpeg")
    # Live tiles use a light substream profile first. The Duo 2V main stream is
    # 5120x1552 HEVC, so decode only when needed and scale immediately for the wall.
    if quality == "main":
        width, fps, q = 1120, 4, 7
    else:
        width, fps, q = 800, 6, 7
    return [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-fflags", "nobuffer", "-flags", "low_delay",
        "-rtsp_transport", "tcp", "-max_delay", "500000",
        "-analyzeduration", "500000", "-probesize", "500000",
        "-i", make_rtsp_url(camera, quality, stream_path=stream_path),
        "-map", "0:v:0", "-an", "-vf", f"fps={fps},scale=w='min({width},iw)':h=-2:flags=fast_bilinear",
        "-threads", "2", "-q:v", str(q), "-c:v", "mjpeg", "-pix_fmt", "yuvj420p",
        "-flush_packets", "1", "-f", "image2pipe", "pipe:1",
    ]


def stream_path_for_quality(configured_path: str, quality: str) -> str:
    """Derive Reolink's paired main/sub profile without spending time probing first."""
    path = (configured_path or "/Preview_01_main").strip()
    if not path.startswith("/"):
        path = "/" + path
    if quality == "sub" and path.endswith("_main"):
        return path[:-5] + "_sub"
    if quality == "main" and path.endswith("_sub"):
        return path[:-4] + "_main"
    return path


def pop_jpeg_frame(buffer: bytearray) -> bytes | None:
    start = buffer.find(b"\xff\xd8")
    if start < 0:
        if len(buffer) > 2:
            del buffer[:-2]
        return None
    if start:
        del buffer[:start]
    end = buffer.find(b"\xff\xd9", 2)
    if end < 0:
        if len(buffer) > 8 * 1024 * 1024:
            buffer.clear()
        return None
    frame = bytes(buffer[:end + 2])
    del buffer[:end + 2]
    return frame


def multipart_jpeg(frame: bytes) -> bytes:
    return (
        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
        + str(len(frame)).encode("ascii") + b"\r\n\r\n" + frame + b"\r\n"
    )


async def start_mjpeg_process(camera: dict[str, Any], quality: str, stream_path: str, timeout: float = FIRST_FRAME_TIMEOUT_SECONDS):
    """Start FFmpeg and wait for a real decoded JPEG before reporting the camera live."""
    command = ffmpeg_mjpeg_command(camera, quality, stream_path)
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise HTTPException(status_code=502, detail=f"Could not start FFmpeg: {exc}") from exc

    stderr_task = asyncio.create_task(process.stderr.read()) if process.stderr else None
    buffer = bytearray()
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise HTTPException(
                    status_code=504,
                    detail=f"RTSP was detected, but FFmpeg did not decode a video frame within {int(timeout)} seconds. Try the camera substream or check its video codec/firmware.",
                )
            try:
                chunk = await asyncio.wait_for(process.stdout.read(65536), timeout=remaining) if process.stdout else b""
            except asyncio.TimeoutError:
                raise HTTPException(
                    status_code=504,
                    detail=f"RTSP was detected, but FFmpeg did not decode a video frame within {int(timeout)} seconds. Try the camera substream or check its video codec/firmware.",
                )
            if not chunk:
                err = (await stderr_task) if stderr_task else b""
                message = err.decode("utf-8", errors="replace")[-600:].strip()
                message = re.sub(r"rtsp://[^\s]+", "rtsp://[credentials hidden]", message)
                detail = "FFmpeg connected to RTSP but exited before producing a JPEG frame."
                if message:
                    detail += " FFmpeg: " + message
                raise HTTPException(status_code=502, detail=detail)
            buffer.extend(chunk)
            frame = pop_jpeg_frame(buffer)
            if frame:
                return process, frame, bytes(buffer), stderr_task
    except Exception:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if stderr_task:
            try:
                await asyncio.wait_for(stderr_task, timeout=1)
            except asyncio.TimeoutError:
                stderr_task.cancel()
        raise


async def mjpeg_process_generator(process, first_frame: bytes, initial_buffer: bytes, stderr_task):
    buffer = bytearray(initial_buffer)
    try:
        yield multipart_jpeg(first_frame)
        while True:
            while True:
                frame = pop_jpeg_frame(buffer)
                if frame is None:
                    break
                yield multipart_jpeg(frame)
            chunk = await process.stdout.read(65536) if process.stdout else b""
            if not chunk:
                break
            buffer.extend(chunk)
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if stderr_task:
            try:
                await asyncio.wait_for(stderr_task, timeout=1)
            except asyncio.TimeoutError:
                stderr_task.cancel()
        CAMERA_STREAM_SEMAPHORE.release()


def ai_box_iou(a: list[float], b: list[float]) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0.0


async def initialize_ai_detector() -> None:
    global AI_DETECTOR
    status_data = get_ai_status()
    if not status_data.get("inference_ready"):
        AI_DETECTOR = None
        AI_RUNTIME_STATE.update({
            "status": "not-ready",
            "message": status_data.get("message", "AI runtime/model is not ready."),
            "last_error": status_data.get("message", ""),
        })
        return
    try:
        confidence = float(os.getenv("NEXGARD_AI_CONFIDENCE", "0.42"))
        detector = await asyncio.to_thread(YoloV8Detector, OBJECT_MODEL, confidence=confidence)
        AI_DETECTOR = detector
        AI_RUNTIME_STATE.update({
            "status": "ready",
            "message": "YOLOv8n loaded. Local person, vehicle and common-animal detection is ready.",
            "last_error": "",
            "providers": list(detector.providers),
            "model_version": detector.version,
        })
    except Exception as exc:
        AI_DETECTOR = None
        AI_RUNTIME_STATE.update({"status": "error", "message": str(exc), "last_error": str(exc)[:500]})


async def ai_camera_manager() -> None:
    """Maintain one low-frame-rate local inference worker for each enabled camera."""
    while True:
        try:
            if AI_DETECTOR is not None:
                with connect() as conn:
                    rows = conn.execute("SELECT id FROM cameras WHERE enabled = 1 AND ai_enabled = 1").fetchall()
                wanted = {int(row["id"]) for row in rows}
                for camera_id in wanted:
                    task = AI_CAMERA_TASKS.get(camera_id)
                    if task is None or task.done():
                        AI_CAMERA_TASKS[camera_id] = asyncio.create_task(
                            ai_camera_worker(camera_id), name=f"nexgard-ai-camera-{camera_id}"
                        )
                for camera_id, task in list(AI_CAMERA_TASKS.items()):
                    if camera_id not in wanted:
                        task.cancel()
                        AI_CAMERA_TASKS.pop(camera_id, None)
                active_count = len([task for task in AI_CAMERA_TASKS.values() if not task.done()])
                AI_RUNTIME_STATE["worker_cameras"] = active_count
                frames_seen = int(AI_RUNTIME_STATE.get("frames_processed", 0))
                if not wanted:
                    AI_RUNTIME_STATE["status"] = "ready-no-cameras"
                    AI_RUNTIME_STATE["message"] = "AI model is ready. Add a camera to begin automatic detection."
                elif frames_seen:
                    AI_RUNTIME_STATE["status"] = "running"
                    AI_RUNTIME_STATE["message"] = f"Local AI has processed frames from {active_count} camera worker(s)."
                else:
                    AI_RUNTIME_STATE["status"] = "starting"
                    AI_RUNTIME_STATE["message"] = f"Starting local AI workers for {active_count} camera(s); waiting for the first decoded frame."

            else:
                AI_RUNTIME_STATE["worker_cameras"] = 0
            await asyncio.sleep(8)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            AI_RUNTIME_STATE["last_error"] = str(exc)[:500]
            await asyncio.sleep(5)


async def publish_ai_frame(camera_id: int, frame: bytes, detections: list[dict[str, Any]]) -> None:
    """Associate detections within one continuous camera stream and publish events."""
    now = time.monotonic()
    tracks = AI_TRACKS.setdefault(camera_id, [])
    used_track_keys: set[str] = set()
    frame_base64: str | None = None

    for detection in detections:
        best_track = None
        best_iou = 0.25
        for track in tracks:
            if track["key"] in used_track_keys or track["class_name"] != detection["class_name"]:
                continue
            if now - float(track["last_seen_monotonic"]) > 18:
                continue
            overlap = ai_box_iou(track["bbox"], detection["bbox"])
            if overlap > best_iou:
                best_iou = overlap
                best_track = track

        is_new = best_track is None
        if is_new:
            best_track = {
                "key": f"ai-{uuid.uuid4().hex[:20]}",
                "class_name": detection["class_name"],
                "bbox": detection["bbox"],
                "last_seen_monotonic": now,
                "public_id": None,
            }
            tracks.append(best_track)
        else:
            best_track["bbox"] = detection["bbox"]
            best_track["last_seen_monotonic"] = now
        used_track_keys.add(best_track["key"])

        if detection["class_name"] == "person":
            if frame_base64 is None:
                frame_base64 = base64.b64encode(frame).decode("ascii")
            observation = PersonObservation(
                camera_id=camera_id,
                tracker_key=best_track["key"],
                confidence=float(detection["confidence"]),
                appearance_description="Detected by local YOLO object detector; no facial identification is performed.",
                snapshot_base64=frame_base64,
            )
            result = await observe_person(observation)
            best_track["public_id"] = result.get("track", {}).get("public_id")
        elif is_new:
            with connect() as conn:
                camera = conn.execute("SELECT name FROM cameras WHERE id = ?", (camera_id,)).fetchone()
                if camera:
                    label = detection["class_name"].capitalize()
                    event_type = "Vehicle detected" if detection["class_name"] in {"car", "truck", "bus", "motorcycle", "bicycle"} else "Animal detected"
                    conn.execute(
                        "INSERT INTO events(camera_id, camera_name, event_type, summary, confidence) VALUES (?, ?, ?, ?, ?)",
                        (camera_id, camera["name"], event_type, f"Local AI detected a {detection['class_name']}.", float(detection["confidence"])),
                    )

    stale_tracks = [track for track in tracks if now - float(track["last_seen_monotonic"]) > 22]
    for track in stale_tracks:
        if track["class_name"] == "person" and track.get("public_id"):
            try:
                await end_person_track(str(track["public_id"]))
            except HTTPException:
                pass
    AI_TRACKS[camera_id] = [track for track in tracks if track not in stale_tracks]

    AI_RUNTIME_STATE["last_frame_at"] = utc_text()
    AI_RUNTIME_STATE["status"] = "running"
    AI_RUNTIME_STATE["last_error"] = ""
    AI_RUNTIME_STATE["frames_processed"] = int(AI_RUNTIME_STATE.get("frames_processed", 0)) + 1
    AI_RUNTIME_STATE["detections_total"] = int(AI_RUNTIME_STATE.get("detections_total", 0)) + len(detections)
    AI_RUNTIME_STATE["last_camera_id"] = camera_id
    AI_RUNTIME_STATE["last_detection_summary"] = ", ".join(
        f"{item['class_name']} {int(float(item['confidence']) * 100)}%" for item in detections[:5]
    ) or "No target objects in latest sampled frame."


async def ai_camera_worker(camera_id: int) -> None:
    """Sample a camera substream, fall back to main, and run local CPU inference."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        AI_RUNTIME_STATE.update({"status": "error", "last_error": "FFmpeg is not installed; AI camera sampling cannot start."})
        return

    camera_quality = "sub"
    while True:
        process = None
        failure: str | None = None
        try:
            camera = camera_control_record(camera_id)
            configured = str(camera.get("stream_path") or "/Preview_01_main")
            stream_path = stream_path_for_quality(configured, camera_quality)
            command = [
                ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                "-rtsp_transport", "tcp", "-max_delay", "500000",
                "-i", make_rtsp_url(camera, camera_quality, stream_path=stream_path),
                "-map", "0:v:0", "-an", "-vf", "fps=1/4,scale=640:-2:flags=fast_bilinear",
                "-threads", "1", "-q:v", "7", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
            ]
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            buffer = bytearray()
            while True:
                chunk = await asyncio.wait_for(process.stdout.read(32768), timeout=24) if process.stdout else b""
                if not chunk:
                    stderr = await process.stderr.read() if process.stderr else b""
                    detail = stderr.decode("utf-8", errors="replace")[-400:].strip()
                    detail = re.sub(r"rtsp://[^\s]+", "rtsp://[credentials hidden]", detail)
                    raise RuntimeError(detail or "FFmpeg camera sampling stream ended before another frame arrived.")
                buffer.extend(chunk)
                while True:
                    frame = pop_jpeg_frame(buffer)
                    if frame is None:
                        break
                    detector = AI_DETECTOR
                    if detector is None:
                        raise RuntimeError("AI detector is not loaded. Run bash ./setup-ai.sh as dev8 and restart NexGard.")
                    started = time.perf_counter()
                    async with AI_INFERENCE_SEMAPHORE:
                        detections = await asyncio.to_thread(detector.detect, frame)
                    AI_RUNTIME_STATE["last_inference_ms"] = round((time.perf_counter() - started) * 1000, 1)
                    await publish_ai_frame(camera_id, frame, detections)
                    AI_RUNTIME_STATE.setdefault("camera_profiles", {})[str(camera_id)] = {
                        "quality": camera_quality,
                        "stream_path": stream_path,
                        "last_frame_at": utc_text(),
                    }
                    camera_errors = dict(AI_RUNTIME_STATE.get("camera_errors") or {})
                    camera_errors.pop(str(camera_id), None)
                    AI_RUNTIME_STATE["camera_errors"] = camera_errors
                    if not camera_errors:
                        AI_RUNTIME_STATE["last_error"] = ""

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = str(exc)[:420]
            safe_failure = re.sub(r"rtsp://[^\s]+", "rtsp://[credentials hidden]", failure)
            camera_errors = dict(AI_RUNTIME_STATE.get("camera_errors") or {})
            camera_errors[str(camera_id)] = safe_failure
            AI_RUNTIME_STATE["camera_errors"] = camera_errors
            AI_RUNTIME_STATE["last_error"] = f"Camera {camera_id}: {safe_failure}"
            AI_RUNTIME_STATE["status"] = "degraded"
            logger.warning("AI worker failed for camera_id=%s quality=%s: %s", camera_id, camera_quality, safe_failure)
        finally:
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()

        if failure is not None:
            if camera_quality == "sub":
                camera_quality = "main"
                logger.info("AI camera %s falling back to its main RTSP stream after substream failure.", camera_id)
                await asyncio.sleep(2)
            else:
                camera_quality = "sub"
                await asyncio.sleep(10)
        # Normal FFmpeg streams are long-lived; reaching this point means one ended.


async def probe_rtsp_audio(camera: dict[str, Any]) -> dict[str, Any]:
    """Find an RTSP profile that contains both a video track and a microphone audio track."""
    configured = str(camera.get("stream_path") or "/Preview_01_main")
    checked: list[str] = []
    best_video: dict[str, Any] | None = None
    for quality in ("main", "sub"):
        for candidate in rtsp_path_candidates(configured, str(camera.get("vendor") or "generic"), quality):
            if candidate in checked:
                continue
            checked.append(candidate)
            result = await probe_rtsp_path(camera, candidate, timeout=5.5)
            if result.get("connected") and best_video is None:
                best_video = result
            if result.get("connected") and result.get("audio_streams"):
                return {
                    "available": True,
                    "stream_path": candidate,
                    "video_streams": result.get("video_streams", []),
                    "audio_streams": result.get("audio_streams", []),
                    "message": f"Microphone audio track found at {candidate}.",
                }
    return {
        "available": False,
        "stream_path": (best_video or {}).get("stream_path"),
        "video_streams": (best_video or {}).get("video_streams", []),
        "audio_streams": [],
        "message": (
            "NexGard can reach the camera video, but no microphone audio track was present in either RTSP profile. "
            "In the Reolink app, open Device Settings → Sounds and enable Record Audio, save, then retry. "
            "Reolink notes that live RTSP audio behavior can vary by model and firmware."
        ),
    }


async def test_rtsp_audio_decode(camera: dict[str, Any], stream_path: str) -> tuple[bool, str]:
    """Decode a short audio sample before returning a browser audio stream."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False, "FFmpeg is not installed on the server."
    command = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", make_rtsp_url(camera, "main", stream_path=stream_path),
        "-map", "0:a:0", "-vn", "-t", "0.8", "-f", "null", "-",
    ]
    try:
        code, stdout, stderr = await run_external_command(command, timeout=9)
    except HTTPException as exc:
        return False, str(exc.detail)
    if code == 0:
        return True, "RTSP microphone audio decoded successfully."
    detail = stderr.decode("utf-8", errors="replace")[-500:]
    detail = re.sub(r"rtsp://[^\s]+", "rtsp://[credentials hidden]", detail)
    return False, detail.strip() or "FFmpeg found an audio track but could not decode it."


async def audio_mp3_generator(camera: dict[str, Any], stream_path: str):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return
    url = make_rtsp_url(camera, "main", stream_path=stream_path)
    command = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", url, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "44100",
        "-c:a", "libmp3lame", "-b:a", "96k", "-f", "mp3", "pipe:1",
    ]
    async with CAMERA_STREAM_SEMAPHORE:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            while True:
                chunk = await process.stdout.read(8192) if process.stdout else b""
                if not chunk:
                    break
                yield chunk
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()



def reolink_cli_error(stdout: bytes, stderr: bytes) -> str | None:
    """Return a CLI-level error even when a release exits with code zero."""
    for raw in (stderr, stdout):
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("ok") is False:
            error = payload.get("error") or {}
            if isinstance(error, dict):
                return str(error.get("message") or error.get("code") or "Reolink CLI reported an error")
            return str(error)
    return None

async def ensure_reolink_cli_ready() -> str:
    cli = reolink_cli_path()
    if not cli:
        raise HTTPException(
            status_code=424,
            detail="Reolink control helper is not installed. NexGard's dashboard is independent; install the official Reolink CLI helper to enable camera speaker, siren and talkback controls.",
        )
    REOLINK_XDG_DIR.mkdir(parents=True, exist_ok=True)
    # config init is idempotent and leaves existing registry files alone.
    code, stdout, stderr = await run_external_command(
        [cli, "config", "init"], timeout=15, env=reolink_environment()
    )
    if code != 0:
        message = stderr.decode("utf-8", errors="replace")[-500:] or stdout.decode("utf-8", errors="replace")[-500:]
        raise HTTPException(status_code=503, detail=f"Could not initialize Reolink control: {message or 'config init failed'}")
    await start_reolink_gateway_if_installed()
    if not await wait_for_reolink_gateway(timeout=8):
        raise HTTPException(status_code=503, detail="The Reolink control gateway did not become ready on 127.0.0.1:9000. Check the Reolink CLI installation and service logs.")
    return cli


async def ensure_reolink_camera_registered(camera: dict[str, Any]) -> str:
    """Ensure the camera alias exists and return the Reolink CLI executable path.

    Reolink CLI 0.21.x accepts --password-stdin after the device add/update
    arguments. Older NexGard builds re-registered the alias on every audio
    request and placed that flag before the subcommand. This function first
    checks the same XDG registry used by the app and avoids unnecessary writes.
    """
    cli = await ensure_reolink_cli_ready()
    alias = f"nexgard-cam-{camera['id']}"
    host = f"{camera['host']}:{camera.get('control_port') or 9000}"
    username = camera.get("username") or "admin"
    password = camera.get("password") or ""
    if not password:
        raise HTTPException(status_code=400, detail="Enter and save the Reolink camera password before enabling audio controls.")

    cli_env = reolink_environment()
    list_code, list_stdout, list_stderr = await run_external_command(
        [cli, "device", "list"], timeout=12, env=cli_env
    )
    list_error = reolink_cli_error(list_stdout, list_stderr)
    if list_code != 0 or list_error:
        message = list_error or list_stderr.decode("utf-8", errors="replace")[-500:]
        raise HTTPException(status_code=503, detail=f"Could not read the Reolink camera registry: {message or 'device list failed'}")

    existing = None
    try:
        registry = json.loads(list_stdout.decode("utf-8", errors="replace"))
        devices = registry.get("data", []) if isinstance(registry, dict) else []
        existing = next((item for item in devices if item.get("camera") == alias), None)
    except (json.JSONDecodeError, AttributeError, TypeError):
        existing = None

    if (
        existing
        and existing.get("host") == host
        and existing.get("user", "admin") == username
        and existing.get("hasPassword") is True
        and existing.get("disabled") is not True
    ):
        return cli

    # CLI 0.21 examples put --password-stdin after the add/update command.
    operation = "update" if existing else "add"
    command = [cli, "device", operation, alias, "--host", host, "--user", username, "--password-stdin"]
    code, stdout, stderr = await run_external_command(
        command,
        input_bytes=password.encode("utf-8") + b"\n",
        timeout=25,
        env=cli_env,
    )
    cli_error = reolink_cli_error(stdout, stderr)
    if code != 0 or cli_error:
        message = cli_error or stderr.decode("utf-8", errors="replace")[-800:] or stdout.decode("utf-8", errors="replace")[-800:]
        message = re.sub(r"(?i)(password\s*[:=]\s*)\S+", r"\1[hidden]", message)
        raise HTTPException(status_code=502, detail=f"Could not register this Reolink camera: {message or 'device registration failed'}")

    return cli


async def run_reolink_camera_command(camera_id: int, arguments: list[str], timeout: float = 35) -> dict[str, Any]:
    camera = camera_control_record(camera_id)
    if camera.get("vendor") != "reolink":
        raise HTTPException(status_code=400, detail="This camera is not configured as a Reolink device. Select Reolink as the camera type first.")
    # Resolve/reuse the local CLI alias from NexGard's encrypted camera record.
    # Matching aliases are not rewritten on every audio action.
    # ensure_reolink_camera_registered returns the executable path; the alias
    # is a separate CLI argument and must never be used as argv[0].
    cli = await ensure_reolink_camera_registered(camera)
    alias = f"nexgard-cam-{camera_id}"
    command = [cli, "--camera", alias, *arguments]
    code, stdout, stderr = await run_external_command(command, timeout=timeout, env=reolink_environment())
    cli_error = reolink_cli_error(stdout, stderr)
    if code != 0 or cli_error:
        message = cli_error or stderr.decode("utf-8", errors="replace")[-800:] or stdout.decode("utf-8", errors="replace")[-800:]
        message = re.sub(r"rtsp://[^\s]+", "rtsp://[credentials hidden]", message)
        logger.warning("Reolink command failed for camera_id=%s action=%s: %s", camera_id, " ".join(arguments[:3]), message[:500])
        raise HTTPException(status_code=502, detail=f"Camera command failed: {message or 'the camera rejected the command'}")
    return {"ok": True, "output": stdout.decode("utf-8", errors="replace")[-1200:]}



async def run_audio_command_with_diagnostics(camera_id: int, action: str, arguments: list[str], timeout: float = 35) -> dict[str, Any]:
    try:
        return await run_reolink_camera_command(camera_id, arguments, timeout=timeout)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Unhandled NexGard audio failure: camera_id=%s action=%s", camera_id, action)
        raise HTTPException(
            status_code=502,
            detail=f"NexGard could not complete the {action} request ({type(exc).__name__}). Check the NexGard service log for the detailed cause.",
        ) from exc

def record_audio_action(camera_id: int, action: str, preview: str, status_text: str, details: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO audio_actions(camera_id, action, content_preview, status, details) VALUES (?, ?, ?, ?, ?)",
            (camera_id, action, preview[:300], status_text, details[:800]),
        )


def ollama_status_sync() -> dict[str, Any]:
    model = os.getenv("NEXGARD_OLLAMA_MODEL", "").strip()
    endpoint = os.getenv("NEXGARD_OLLAMA_URL", "http://127.0.0.1:11434").strip().rstrip("/")
    parsed = urlsplit(endpoint)
    if not model:
        return {"available": False, "model": "", "message": "No local Ollama model configured."}
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return {"available": False, "model": model, "message": "Text correction endpoint must be local to this server."}
    request = urllib.request.Request(endpoint + "/api/tags", headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=1.5) as response:
            payload = json.loads(response.read(500_000).decode("utf-8"))
        installed = {str(item.get("name", "")) for item in payload.get("models", [])}
        available = model in installed or any(name.split(":", 1)[0] == model.split(":", 1)[0] for name in installed)
        return {
            "available": available,
            "model": model,
            "message": "Local model ready." if available else "Ollama is running, but the configured model is not pulled yet.",
        }
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return {"available": False, "model": model, "message": "Ollama is not responding on this server."}


def ollama_correct_text_sync(text: str) -> str:
    endpoint = os.getenv("NEXGARD_OLLAMA_URL", "http://127.0.0.1:11434").strip().rstrip("/")
    model = os.getenv("NEXGARD_OLLAMA_MODEL", "").strip()
    parsed = urlsplit(endpoint)
    if not model:
        raise RuntimeError("Local AI text correction is not configured. Set NEXGARD_OLLAMA_MODEL after installing a local Ollama model.")
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("For privacy, NexGard text correction only accepts an Ollama service on this server (localhost).")
    prompt = (
        "Correct spelling, punctuation and obvious grammar in the text below. "
        "Preserve the original meaning, names and tone. Do not add claims or instructions. "
        "Return only the corrected text, without quotation marks or explanations.\n\nTEXT:\n" + text
    )
    payload = json.dumps({"model": model, "prompt": prompt, "stream": False, "options": {"temperature": 0.1, "num_predict": 256}}).encode("utf-8")
    request = urllib.request.Request(endpoint + "/api/generate", data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read(1_000_000).decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Local text-correction model could not be reached: {exc}") from exc
    corrected = str(result.get("response", "")).strip().strip('"')
    if not corrected or len(corrected) > MAX_TTS_CHARS:
        raise RuntimeError("The local AI returned an empty or oversized correction.")
    return corrected


async def create_camera_speech_file(text: str) -> Path:
    espeak = shutil.which("espeak-ng") or shutil.which("espeak")
    ffmpeg = shutil.which("ffmpeg")
    if not espeak or not ffmpeg:
        raise HTTPException(status_code=424, detail="Text-to-speech needs espeak-ng and FFmpeg. Install them with: sudo apt install espeak-ng ffmpeg")
    wav_path = AUDIO_TEMP_DIR / f"tts-{uuid.uuid4().hex}.wav"
    pcm_path = AUDIO_TEMP_DIR / f"tts-{uuid.uuid4().hex}.pcm"
    code, _, stderr = await run_external_command([espeak, "-v", "en-us", "-s", "165", "-w", str(wav_path), text], timeout=15)
    if code != 0 or not wav_path.exists():
        raise HTTPException(status_code=500, detail=f"Speech synthesis failed: {stderr.decode('utf-8', errors='replace')[-400:]}")
    code, _, stderr = await run_external_command(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav_path), "-t", "20", "-ar", "16000", "-ac", "1", "-f", "s16le", str(pcm_path)],
        timeout=15,
    )
    wav_path.unlink(missing_ok=True)
    if code != 0 or not pcm_path.exists():
        pcm_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Speech audio conversion failed: {stderr.decode('utf-8', errors='replace')[-400:]}")
    return pcm_path


async def convert_talkback_to_pcm(raw_audio: bytes, media_type: str) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise HTTPException(status_code=424, detail="FFmpeg is required to prepare microphone audio for the camera.")
    input_suffix = ".webm" if "webm" in media_type else ".ogg" if "ogg" in media_type else ".wav" if "wav" in media_type else ".bin"
    input_path = AUDIO_TEMP_DIR / f"talk-{uuid.uuid4().hex}{input_suffix}"
    output_path = AUDIO_TEMP_DIR / f"talk-{uuid.uuid4().hex}.pcm"
    input_path.write_bytes(raw_audio)
    try:
        code, _, stderr = await run_external_command(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(input_path), "-t", "10", "-ar", "16000", "-ac", "1", "-f", "s16le", str(output_path)],
            timeout=18,
        )
    finally:
        input_path.unlink(missing_ok=True)
    if code != 0 or not output_path.exists() or output_path.stat().st_size == 0:
        output_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Could not decode the microphone recording: {stderr.decode('utf-8', errors='replace')[-400:]}")
    return output_path


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "online", "application": "NexGard Security System", "version": "0.6.5"}


@app.post("/api/login")
async def login(data: LoginRequest, request: Request) -> dict[str, bool]:
    expected = os.getenv("NOVATECH_ADMIN_PASSWORD", "")
    if not expected or not hmac.compare_digest(data.password, expected):
        raise HTTPException(status_code=401, detail="Incorrect password.")
    request.session.clear()
    request.session["authenticated"] = True
    return {"authenticated": True}


@app.post("/api/settings/password", dependencies=[Depends(authenticated)])
async def change_admin_password(data: PasswordChangeRequest) -> dict[str, bool]:
    """Change the dashboard administrator password without exposing it in the UI."""
    current = os.getenv("NOVATECH_ADMIN_PASSWORD", "")
    if not current or not hmac.compare_digest(data.current_password, current):
        raise HTTPException(status_code=400, detail="Current administrator password is incorrect.")
    env_path = ROOT_DIR / ".env"
    if not env_path.is_file():
        raise HTTPException(status_code=500, detail="The server .env file is missing; password was not changed.")
    try:
        from dotenv import set_key
        set_key(str(env_path), "NOVATECH_ADMIN_PASSWORD", data.new_password, quote_mode="always")
        os.chmod(env_path, 0o600)
        os.environ["NOVATECH_ADMIN_PASSWORD"] = data.new_password
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not safely save the new password: {type(exc).__name__}.") from exc
    return {"changed": True}


@app.post("/api/logout")
async def logout(request: Request) -> dict[str, bool]:
    request.session.clear()
    return {"authenticated": False}


@app.get("/api/session")
async def session_status(request: Request) -> dict[str, bool]:
    return {"authenticated": request.session.get("authenticated") is True}


@app.get("/api/summary", dependencies=[Depends(authenticated)])
async def summary() -> dict[str, Any]:
    with connect() as conn:
        cameras = conn.execute("SELECT COUNT(*) FROM cameras").fetchone()[0]
        rooms = conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
        storage = conn.execute("SELECT COUNT(*) FROM storage_targets").fetchone()[0]
        events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        person_tracks = conn.execute("SELECT COUNT(*) FROM person_tracks").fetchone()[0]
        active_person_tracks = conn.execute("SELECT COUNT(*) FROM person_tracks WHERE ended_at IS NULL").fetchone()[0]
        person_snapshots = conn.execute("SELECT COUNT(*) FROM person_snapshots").fetchone()[0]
    usage = shutil.disk_usage(ROOT_DIR)
    return {
        "cameras": cameras,
        "rooms": rooms,
        "storage_targets": storage,
        "events": events,
        "person_tracks": person_tracks,
        "active_person_tracks": active_person_tracks,
        "person_snapshots": person_snapshots,
        "disk_total_bytes": usage.total,
        "disk_used_bytes": usage.used,
        "disk_free_bytes": usage.free,
    }


@app.get("/api/rooms", dependencies=[Depends(authenticated)])
async def list_rooms() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT r.*, (SELECT COUNT(*) FROM cameras c WHERE c.room_id = r.id) AS camera_count "
            "FROM rooms r ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return rows_to_dicts(rows)


@app.post("/api/rooms", dependencies=[Depends(authenticated)])
async def create_room(data: RoomCreate) -> dict[str, Any]:
    try:
        with connect() as conn:
            cursor = conn.execute(
                "INSERT INTO rooms(name, description) VALUES (?, ?)",
                (data.name.strip(), data.description.strip()),
            )
            row = conn.execute("SELECT * FROM rooms WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise HTTPException(status_code=409, detail="A room with that name already exists.") from exc
        raise


@app.delete("/api/rooms/{room_id}", dependencies=[Depends(authenticated)])
async def delete_room(room_id: int) -> dict[str, bool]:
    with connect() as conn:
        cursor = conn.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Room not found.")
    return {"deleted": True}


@app.get("/api/cameras", dependencies=[Depends(authenticated)])
async def list_cameras() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT c.id, c.name, c.host, c.port, c.stream_path, c.room_id, c.enabled, c.ai_enabled, c.vendor, c.control_port, "
            "c.connection_status, c.created_at, r.name AS room_name "
            "FROM cameras c LEFT JOIN rooms r ON r.id = c.room_id ORDER BY c.id DESC"
        ).fetchall()
    return rows_to_dicts(rows)


@app.patch("/api/cameras/{camera_id}/ai", dependencies=[Depends(authenticated)])
async def update_camera_ai(camera_id: int, data: CameraAIUpdate) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT id, name FROM cameras WHERE id = ?", (camera_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Camera not found.")
        conn.execute("UPDATE cameras SET ai_enabled = ? WHERE id = ?", (1 if data.enabled else 0, camera_id))
    if not data.enabled:
        task = AI_CAMERA_TASKS.pop(camera_id, None)
        if task and not task.done():
            task.cancel()
        AI_TRACKS.pop(camera_id, None)
    return {
        "camera_id": camera_id,
        "name": row["name"],
        "ai_enabled": data.enabled,
        "message": f"Local AI {'enabled' if data.enabled else 'disabled'} for {row['name']}.",
    }


@app.post("/api/cameras/{camera_id}/ai/test", dependencies=[Depends(authenticated)])
async def test_camera_ai(camera_id: int) -> dict[str, Any]:
    """Capture one real frame and run inference without creating a stored event."""
    if AI_DETECTOR is None:
        raise HTTPException(status_code=424, detail="Local AI model is not loaded. Run bash ./setup-ai.sh as dev8, then restart NexGard.")
    camera = camera_control_record(camera_id)
    attempts: list[str] = []
    configured = str(camera.get("stream_path") or "/Preview_01_main")
    for quality in ("sub", "main"):
        stream_path = stream_path_for_quality(configured, quality)
        probe = await probe_rtsp_path(camera, stream_path, timeout=6)
        if not probe.get("connected"):
            attempts.append(f"{quality}: {probe.get('error') or 'video stream unavailable'}")
            continue
        process = None
        stderr_task = None
        await CAMERA_STREAM_SEMAPHORE.acquire()
        try:
            process, first_frame, _initial_buffer, stderr_task = await start_mjpeg_process(
                camera, quality, stream_path, timeout=12
            )
            started = time.perf_counter()
            async with AI_INFERENCE_SEMAPHORE:
                detections = await asyncio.to_thread(AI_DETECTOR.detect, first_frame)
            elapsed = round((time.perf_counter() - started) * 1000, 1)
            return {
                "ok": True,
                "camera_id": camera_id,
                "camera_name": camera["name"],
                "quality": quality,
                "stream_path": stream_path,
                "frame_bytes": len(first_frame),
                "inference_ms": elapsed,
                "detection_count": len(detections),
                "detections": [
                    {"label": item["class_name"], "confidence": round(float(item["confidence"]), 3)}
                    for item in detections
                ],
                "message": (
                    f"AI successfully analyzed a real {quality} frame in {elapsed} ms. "
                    + (f"Detected: {', '.join(item['class_name'] for item in detections)}." if detections else "No supported objects were visible in this frame.")
                ),
            }
        except HTTPException as exc:
            attempts.append(f"{quality}: {exc.detail}")
        finally:
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            if stderr_task and not stderr_task.done():
                stderr_task.cancel()
            CAMERA_STREAM_SEMAPHORE.release()
    raise HTTPException(status_code=502, detail="AI could not capture a decodable frame from this camera. " + " | ".join(attempts[-2:]))


@app.post("/api/cameras/{camera_id}/auto-configure", dependencies=[Depends(authenticated)])
async def auto_configure_camera(camera_id: int) -> dict[str, Any]:
    camera = camera_control_record(camera_id)
    result = await detect_rtsp_stream(camera, quality="main")
    with connect() as conn:
        if result.get("connected") and result.get("stream_path"):
            conn.execute(
                "UPDATE cameras SET stream_path = ?, connection_status = 'online' WHERE id = ?",
                (result["stream_path"], camera_id),
            )
        else:
            conn.execute("UPDATE cameras SET connection_status = 'offline' WHERE id = ?", (camera_id,))
    return result


@app.get("/api/cameras/{camera_id}/stream/diagnostics", dependencies=[Depends(authenticated)])
async def camera_stream_diagnostics(camera_id: int) -> dict[str, Any]:
    camera = camera_control_record(camera_id)
    attempts: list[str] = []
    await CAMERA_STREAM_SEMAPHORE.acquire()
    try:
        # Try both profiles all the way through a real FFmpeg decode. An RTSP
        # DESCRIBE response alone is not enough to claim that browser video works.
        for actual_quality in ("sub", "main"):
            result = await detect_rtsp_stream(camera, quality=actual_quality)
            if not result.get("connected"):
                attempts.append(f"{actual_quality}: {result.get('message', 'No RTSP video path.')}")
                continue
            process = None
            stderr_task = None
            try:
                process, first_frame, _buffer, stderr_task = await start_mjpeg_process(
                    camera, actual_quality, result["stream_path"], timeout=10
                )
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=2)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()
                if stderr_task:
                    try:
                        await asyncio.wait_for(stderr_task, timeout=1)
                    except asyncio.TimeoutError:
                        stderr_task.cancel()
                with connect() as conn:
                    conn.execute(
                        "UPDATE cameras SET stream_path = ?, connection_status = 'online' WHERE id = ?",
                        (result["stream_path"], camera_id),
                    )
                return {
                    **result,
                    "video_decode_ok": True,
                    "quality_used": actual_quality,
                    "first_frame_bytes": len(first_frame),
                    "message": f"Video decoded successfully from {result['stream_path']} ({actual_quality} profile).",
                }
            except HTTPException as exc:
                attempts.append(f"{actual_quality}: {exc.detail}")
            finally:
                if process is not None and process.returncode is None:
                    process.kill()
                    await process.wait()
                if stderr_task and not stderr_task.done():
                    stderr_task.cancel()
        with connect() as conn:
            conn.execute("UPDATE cameras SET connection_status = 'offline' WHERE id = ?", (camera_id,))
        return {
            "connected": False,
            "video_decode_ok": False,
            "message": "RTSP was reachable, but NexGard could not decode a browser video frame from either profile. " + " | ".join(attempts),
            "details": attempts,
        }
    finally:
        CAMERA_STREAM_SEMAPHORE.release()


@app.post("/api/cameras/test", dependencies=[Depends(authenticated)])
async def test_camera(data: CameraTestInput) -> dict[str, Any]:
    camera = {
        "host": data.host,
        "port": data.port,
        "stream_path": data.stream_path,
        "username": data.username,
        "password": data.password,
        "vendor": data.vendor,
    }
    result = await detect_rtsp_stream(camera, quality="main")
    if result["connected"]:
        result["message"] = f"Connected. NexGard found the stream at {result['stream_path']}."
    return result


@app.post("/api/cameras", dependencies=[Depends(authenticated)])
async def create_camera(data: CameraInput) -> dict[str, Any]:
    if data.room_id is not None:
        with connect() as conn:
            exists = conn.execute("SELECT 1 FROM rooms WHERE id = ?", (data.room_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=400, detail="Selected room does not exist.")
    encrypted = get_vault().encrypt(data.password)
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO cameras(name, host, port, stream_path, username, password_encrypted, vendor, control_port, room_id, ai_enabled) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (data.name.strip(), data.host, data.port, data.stream_path, data.username, encrypted, data.vendor, data.control_port, data.room_id),
        )
        row = conn.execute(
            "SELECT c.id, c.name, c.host, c.port, c.stream_path, c.room_id, c.enabled, c.ai_enabled, c.vendor, c.control_port, "
            "c.connection_status, c.created_at, r.name AS room_name FROM cameras c "
            "LEFT JOIN rooms r ON r.id = c.room_id WHERE c.id = ?",
            (cursor.lastrowid,),
        ).fetchone()
    return dict(row)


@app.get("/api/audio/status", dependencies=[Depends(authenticated)])
async def get_audio_status() -> dict[str, Any]:
    cli = reolink_cli_path()
    cli_version = ""
    if cli:
        try:
            code, stdout, stderr = await run_external_command([cli, "--version"], timeout=4, env=reolink_environment())
            cli_version = (stdout or stderr).decode("utf-8", errors="replace")[:200].strip() if code == 0 else "version check failed"
        except Exception:
            cli_version = "version check failed"
    ollama = await asyncio.to_thread(ollama_status_sync)
    return {
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "tts_engine": bool(shutil.which("espeak-ng") or shutil.which("espeak")),
        "reolink_cli": bool(cli),
        "reolink_cli_path": cli or "",
        "reolink_cli_version": cli_version,
        "reolink_gateway_running": await tcp_port_open("127.0.0.1", 9000, timeout=0.5),
        "local_ai_text_correction": bool(ollama["available"]),
        "ollama_model": ollama["model"],
        "ollama_message": ollama["message"],
        "talkback_requires_secure_browser": True,
    }


@app.get("/api/audio/actions", dependencies=[Depends(authenticated)])
async def list_audio_actions(limit: int = 40) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 100))
    with connect() as conn:
        rows = conn.execute(
            "SELECT a.*, c.name AS camera_name FROM audio_actions a "
            "LEFT JOIN cameras c ON c.id = a.camera_id ORDER BY a.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return rows_to_dicts(rows)


@app.get("/api/cameras/{camera_id}/stream", dependencies=[Depends(authenticated)])
async def camera_live_stream(camera_id: int, quality: str = "sub") -> StreamingResponse:
    if quality not in {"sub", "main"}:
        raise HTTPException(status_code=400, detail="Stream quality must be 'sub' or 'main'.")
    camera = camera_control_record(camera_id)

    # The camera was already tested/saved in NexGard. Try its paired substream
    # directly first (no extra FFprobe round-trip); only run stream discovery if
    # the fast path fails. This reduces the first visible frame delay on LAN.
    await CAMERA_STREAM_SEMAPHORE.acquire()
    process = None
    selected_path: str | None = None
    selected_quality: str | None = None
    selected_first_frame: bytes | None = None
    selected_buffer: bytes | None = None
    selected_stderr_task = None
    failures: list[str] = []
    configured_path = str(camera.get("stream_path") or "/Preview_01_main")
    other_quality = "main" if quality == "sub" else "sub"
    direct_attempts: list[tuple[str, str, float]] = [
        (quality, stream_path_for_quality(configured_path, quality), FIRST_FRAME_TIMEOUT_SECONDS),
        (other_quality, stream_path_for_quality(configured_path, other_quality), MAIN_FRAME_TIMEOUT_SECONDS),
    ]
    seen: set[tuple[str, str]] = set()
    try:
        for candidate_quality, candidate_path, timeout in direct_attempts:
            key = (candidate_quality, candidate_path)
            if key in seen:
                continue
            seen.add(key)
            try:
                process, first_frame, initial_buffer, stderr_task = await start_mjpeg_process(
                    camera, candidate_quality, candidate_path, timeout=timeout
                )
                selected_path = candidate_path
                selected_quality = candidate_quality
                selected_first_frame = first_frame
                selected_buffer = initial_buffer
                selected_stderr_task = stderr_task
                break
            except HTTPException as exc:
                failures.append(f"{candidate_quality} ({candidate_path}): {exc.detail}")

        # For nonstandard RTSP paths, do one discovery pass after the paired
        # Reolink profiles fail. This is a recovery path, not the normal startup path.
        if process is None:
            discovered = await detect_rtsp_stream(camera, quality=quality)
            if discovered.get("connected"):
                candidate_quality = quality
                candidate_path = discovered["stream_path"]
                if (candidate_quality, candidate_path) not in seen:
                    try:
                        process, first_frame, initial_buffer, stderr_task = await start_mjpeg_process(
                            camera, candidate_quality, candidate_path, timeout=MAIN_FRAME_TIMEOUT_SECONDS
                        )
                        selected_path = candidate_path
                        selected_quality = candidate_quality
                        selected_first_frame = first_frame
                        selected_buffer = initial_buffer
                        selected_stderr_task = stderr_task
                    except HTTPException as exc:
                        failures.append(f"discovered ({candidate_path}): {exc.detail}")
            else:
                failures.append(discovered.get("message", "No supported RTSP path was found."))

        if process is None or selected_path is None or selected_first_frame is None:
            raise HTTPException(
                status_code=502,
                detail="NexGard could not decode the first camera frame. " + " | ".join(failures[-3:]),
            )
    except Exception:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        if selected_stderr_task and not selected_stderr_task.done():
            selected_stderr_task.cancel()
        CAMERA_STREAM_SEMAPHORE.release()
        raise

    with connect() as conn:
        conn.execute(
            "UPDATE cameras SET stream_path = ?, connection_status = 'online' WHERE id = ?",
            (selected_path, camera_id),
        )
    return StreamingResponse(
        mjpeg_process_generator(process, selected_first_frame, selected_buffer or b"", selected_stderr_task),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Content-Type-Options": "nosniff",
            "X-NexGard-Stream-Quality": selected_quality or quality,
        },
    )


@app.get("/api/cameras/{camera_id}/audio/diagnostics", dependencies=[Depends(authenticated)])
async def camera_audio_diagnostics(camera_id: int) -> dict[str, Any]:
    camera = camera_control_record(camera_id)
    result = await probe_rtsp_audio(camera)
    audio_decode_ok = False
    audio_decode_message = result.get("message", "No microphone audio track found.")
    if result.get("available"):
        audio_decode_ok, audio_decode_message = await test_rtsp_audio_decode(camera, result["stream_path"])
    cli = reolink_cli_path()
    listen_ready = bool(result.get("available") and audio_decode_ok)
    if result.get("available") and not audio_decode_ok:
        display_message = "A microphone audio track exists, but The server could not decode it: " + audio_decode_message
    elif listen_ready:
        display_message = "Camera microphone audio is present and FFmpeg decoded a test sample successfully."
    else:
        display_message = result.get("message", "No microphone audio track was detected.")
    return {
        **result,
        "listen_ready": listen_ready,
        "message": display_message,
        "audio_decode_ok": audio_decode_ok,
        "audio_decode_message": audio_decode_message,
        "ffmpeg_available": bool(shutil.which("ffmpeg")),
        "reolink_control_available": bool(cli),
        "reolink_gateway_running": await tcp_port_open("127.0.0.1", 9000, timeout=0.5),
        "talkback_message": (
            "Reolink LAN control helper is installed. Use Enable camera audio controls to register this camera."
            if cli else "Reolink LAN control helper is not installed; microphone-to-camera talkback, TTS and siren commands need it."
        ),
    }


@app.get("/api/cameras/{camera_id}/audio/listen", dependencies=[Depends(authenticated)])
async def camera_audio_listen(camera_id: int) -> StreamingResponse:
    if not shutil.which("ffmpeg"):
        raise HTTPException(status_code=424, detail="FFmpeg is not installed on the server. Install it with: sudo apt install ffmpeg")
    camera = camera_control_record(camera_id)
    result = await probe_rtsp_audio(camera)
    if not result.get("available"):
        raise HTTPException(status_code=424, detail=result.get("message", "No RTSP microphone audio track was found."))
    decoded, decode_message = await test_rtsp_audio_decode(camera, result["stream_path"])
    if not decoded:
        raise HTTPException(status_code=424, detail=f"An RTSP audio track exists, but audio decoding failed: {decode_message}")
    return StreamingResponse(
        audio_mp3_generator(camera, result["stream_path"]),
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store, no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/cameras/{camera_id}/audio/connect", dependencies=[Depends(authenticated)])
async def connect_reolink_audio(camera_id: int) -> dict[str, Any]:
    camera = camera_control_record(camera_id)
    # Keep the executable and camera alias separate: cli is argv[0].
    cli = await ensure_reolink_camera_registered(camera)
    alias = f"nexgard-cam-{camera_id}"
    commands = {
        "capabilities": [cli, "--camera", alias, "capabilities"],
        "audio_config": [cli, "--camera", alias, "audio", "config", "get"],
    }
    reports: dict[str, str] = {}
    failures: list[str] = []
    for name, command in commands.items():
        code, stdout, stderr = await run_external_command(
            command, timeout=22, env=reolink_environment()
        )
        output = (stdout if code == 0 else stderr).decode("utf-8", errors="replace")[-1800:]
        reports[name] = output
        cli_error = reolink_cli_error(stdout, stderr)
        if code != 0 or cli_error:
            failures.append(name)
            if cli_error:
                reports[name + "_error"] = cli_error[:500]

    # The capability bitmap is authoritative for talkback/siren support.
    # Siren linkage/status GET is optional on some Reolink firmware; it must
    # not block the manual audio-talk or bounded siren-play controls.
    supports_talkback = False
    supports_siren = False
    try:
        capability_reply = json.loads(reports.get("capabilities", "{}"))
        capability_data = capability_reply.get("data", {})
        supports_talkback = str(capability_data.get("audioTalk", "0")) == "1"
        supports_siren = str(capability_data.get("audioAlarm", "0")) == "1"
    except (json.JSONDecodeError, AttributeError, TypeError):
        pass

    siren_code, siren_out, siren_err = await run_external_command(
        [cli, "--camera", alias, "audio", "siren", "enable", "get"],
        timeout=15, env=reolink_environment()
    )
    reports["siren_linkage_status"] = (siren_out if siren_code == 0 else siren_err).decode(
        "utf-8", errors="replace"
    )[-1200:]
    siren_linkage_error = reolink_cli_error(siren_out, siren_err)
    reports["siren_linkage_status_note"] = (
        "Available" if siren_code == 0 and not siren_linkage_error else
        "This firmware does not expose siren-linkage status; manual siren playback is separately gated by audioAlarm."
    )

    status_text = "success" if not failures else "partial"
    detail = json.dumps(reports, ensure_ascii=False)[:800]
    record_audio_action(camera_id, "connect", "Audio capability check", status_text, detail)
    if failures:
        message = "Camera registered, but required capability/configuration checks failed: " + ", ".join(failures)
    else:
        available = []
        if supports_talkback:
            available.append("two-way talkback / TTS")
        if supports_siren:
            available.append("manual camera siren")
        message = "Reolink controls connected. Camera reports support for " + (", ".join(available) if available else "no talkback or siren features") + "."
    return {
        "connected": not failures,
        "registered": True,
        "alias": alias,
        "reports": reports,
        "failed_checks": failures,
        "supports_talkback": supports_talkback,
        "supports_siren": supports_siren,
        "siren_linkage_status_available": siren_code == 0,
        "message": message,
    }


@app.post("/api/cameras/{camera_id}/audio/siren", dependencies=[Depends(authenticated)])
async def trigger_camera_siren(camera_id: int) -> dict[str, Any]:
    # Deliberately bounded to a single siren playback; no indefinite siren loop.
    result = await run_audio_command_with_diagnostics(camera_id, "siren playback", ["audio", "siren", "play", "--times", "1", "--wait"], timeout=25)
    record_audio_action(camera_id, "siren", "One-shot siren", "success", result.get("output", ""))
    return {"triggered": True, "message": "One-shot siren command sent to the camera."}


@app.post("/api/cameras/{camera_id}/audio/siren/stop", dependencies=[Depends(authenticated)])
async def stop_camera_siren(camera_id: int) -> dict[str, Any]:
    result = await run_audio_command_with_diagnostics(camera_id, "siren stop", ["audio", "siren", "stop"], timeout=15)
    record_audio_action(camera_id, "siren-stop", "", "success", result.get("output", ""))
    return {"stopped": True, "message": "Siren stop command sent."}


@app.post("/api/cameras/{camera_id}/audio/talk", dependencies=[Depends(authenticated)])
async def send_camera_talkback(camera_id: int, request: Request) -> dict[str, Any]:
    raw_audio = await request.body()
    if not raw_audio:
        raise HTTPException(status_code=400, detail="No microphone audio was received.")
    if len(raw_audio) > MAX_TALKBACK_BYTES:
        raise HTTPException(status_code=413, detail="Talkback recording is too large. Keep recordings to 10 seconds or less.")
    media_type = request.headers.get("content-type", "application/octet-stream").split(";", 1)[0].lower()
    pcm_path = await convert_talkback_to_pcm(raw_audio, media_type)
    try:
        result = await run_audio_command_with_diagnostics(
            camera_id, "microphone talkback",
            ["audio", "talk", "--file", str(pcm_path), "--sample-rate", "16000"],
            timeout=25,
        )
    finally:
        pcm_path.unlink(missing_ok=True)
    record_audio_action(camera_id, "talkback", "Live microphone clip", "success", result.get("output", ""))
    return {"sent": True, "message": "Microphone audio sent to the camera speaker."}


@app.post("/api/cameras/{camera_id}/audio/speak", dependencies=[Depends(authenticated)])
async def speak_through_camera(camera_id: int, data: AudioTextInput) -> dict[str, Any]:
    pcm_path = await create_camera_speech_file(data.text)
    try:
        result = await run_audio_command_with_diagnostics(
            camera_id, "text-to-speech",
            ["audio", "talk", "--file", str(pcm_path), "--sample-rate", "16000"],
            timeout=30,
        )
    finally:
        pcm_path.unlink(missing_ok=True)
    record_audio_action(camera_id, "text-to-speech", data.text, "success", result.get("output", ""))
    return {"spoken": True, "text": data.text, "message": "Text-to-speech audio sent to the camera speaker."}


@app.post("/api/audio/correct-text", dependencies=[Depends(authenticated)])
async def correct_announcement_text(data: AudioTextInput) -> dict[str, Any]:
    if not os.getenv("NEXGARD_OLLAMA_MODEL", "").strip():
        return {
            "available": False,
            "corrected": data.text,
            "changed": False,
            "message": "Local AI text correction is not configured. The original text is unchanged; configure a local Ollama model to enable correction.",
        }
    try:
        corrected = await asyncio.to_thread(ollama_correct_text_sync, data.text)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"available": True, "corrected": corrected, "changed": corrected != data.text, "message": "Text checked by the local AI model."}


@app.delete("/api/cameras/{camera_id}", dependencies=[Depends(authenticated)])
async def delete_camera(camera_id: int) -> dict[str, bool]:
    with connect() as conn:
        cursor = conn.execute("DELETE FROM cameras WHERE id = ?", (camera_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Camera not found.")
    return {"deleted": True}


@app.post("/api/discovery/onvif", dependencies=[Depends(authenticated)])
async def discover_onvif() -> dict[str, Any]:
    try:
        from wsdiscovery.discovery import ThreadedWSDiscovery  # type: ignore

        discovery = ThreadedWSDiscovery()
        discovery.start()
        try:
            services = discovery.searchServices(timeout=4)
        finally:
            discovery.stop()
        found: list[dict[str, str]] = []
        seen: set[str] = set()
        for service in services:
            for address in service.getXAddrs() or []:
                host = urlsplit(address).hostname or ""
                if host and host not in seen:
                    seen.add(host)
                    found.append({"host": host, "xaddr": address})
        return {"devices": found, "count": len(found)}
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="ONVIF discovery could not complete. Confirm the server and cameras are on the same LAN and multicast discovery is allowed.",
        ) from exc


@app.get("/api/storage", dependencies=[Depends(authenticated)])
async def list_storage() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, name, host, protocol, share_path, mount_path, status, created_at "
            "FROM storage_targets ORDER BY id DESC"
        ).fetchall()
    return rows_to_dicts(rows)


@app.post("/api/storage", dependencies=[Depends(authenticated)])
async def create_storage(data: StorageInput) -> dict[str, Any]:
    encrypted = get_vault().encrypt(data.password)
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO storage_targets(name, host, protocol, share_path, mount_path, username, password_encrypted) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (data.name.strip(), data.host, data.protocol, data.share_path.strip(), data.mount_path, data.username, encrypted),
        )
        row = conn.execute(
            "SELECT id, name, host, protocol, share_path, mount_path, status, created_at "
            "FROM storage_targets WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    return dict(row)


@app.delete("/api/storage/{storage_id}", dependencies=[Depends(authenticated)])
async def delete_storage(storage_id: int) -> dict[str, bool]:
    with connect() as conn:
        cursor = conn.execute("DELETE FROM storage_targets WHERE id = ?", (storage_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Storage target not found.")
    return {"deleted": True}


@app.get("/api/ai/status", dependencies=[Depends(authenticated)])
async def ai_status() -> dict[str, Any]:
    data = get_ai_status()
    data.update(AI_RUNTIME_STATE)
    data["worker_camera_count"] = len([task for task in AI_CAMERA_TASKS.values() if not task.done()])
    data["worker_camera_ids"] = sorted(camera_id for camera_id, task in AI_CAMERA_TASKS.items() if not task.done())
    return data


@app.post("/api/ai/reload", dependencies=[Depends(authenticated)])
async def ai_reload() -> dict[str, Any]:
    await initialize_ai_detector()
    return await ai_status()


@app.get("/api/events", dependencies=[Depends(authenticated)])
async def list_events(limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 250))
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, camera_id, camera_name, event_type, summary, confidence, occurred_at "
            "FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return rows_to_dicts(rows)


@app.post("/api/events", dependencies=[Depends(authenticated)])
async def create_event(data: EventInput) -> dict[str, Any]:
    camera_name = "Unknown camera"
    if data.camera_id is not None:
        with connect() as conn:
            camera = conn.execute("SELECT name FROM cameras WHERE id = ?", (data.camera_id,)).fetchone()
        if not camera:
            raise HTTPException(status_code=400, detail="Camera not found.")
        camera_name = camera["name"]
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO events(camera_id, camera_name, event_type, summary, confidence) VALUES (?, ?, ?, ?, ?)",
            (data.camera_id, camera_name, data.event_type, data.summary, data.confidence),
        )
        row = conn.execute("SELECT * FROM events WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row)



@app.get("/api/person-tracks", dependencies=[Depends(authenticated)])
async def list_person_tracks(limit: int = 100, status_filter: str = "all") -> list[dict[str, Any]]:
    limit = max(1, min(limit, 250))
    if status_filter not in {"all", "active", "complete"}:
        raise HTTPException(status_code=400, detail="Status must be all, active or complete.")
    where = "" if status_filter == "all" else "WHERE p.status = ?"
    params: tuple[Any, ...] = (limit,) if status_filter == "all" else (status_filter, limit)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT p.id, p.public_id, p.camera_id, p.camera_name, p.first_seen, p.last_seen, "
            f"p.ended_at, p.confidence, p.appearance_description, p.status, "
            f"(SELECT COUNT(*) FROM person_snapshots s WHERE s.track_id = p.id) AS snapshot_count, "
            f"(SELECT s.id FROM person_snapshots s WHERE s.track_id = p.id ORDER BY s.id DESC LIMIT 1) AS latest_snapshot_id "
            f"FROM person_tracks p {where} ORDER BY p.last_seen DESC LIMIT ?",
            params,
        ).fetchall()
    return [serialize_person_track(row) for row in rows]


@app.get("/api/person-tracks/{public_id}", dependencies=[Depends(authenticated)])
async def get_person_track(public_id: str) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            "SELECT p.id, p.public_id, p.camera_id, p.camera_name, p.first_seen, p.last_seen, p.ended_at, "
            "p.confidence, p.appearance_description, p.status, "
            "(SELECT COUNT(*) FROM person_snapshots s WHERE s.track_id = p.id) AS snapshot_count "
            "FROM person_tracks p WHERE p.public_id = ?", (public_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Person event not found.")
        snapshots = conn.execute(
            "SELECT id, captured_at, media_type, file_size FROM person_snapshots "
            "WHERE track_id = ? ORDER BY captured_at DESC", (row["id"],)
        ).fetchall()
    item = dict(row)
    item["snapshots"] = [
        {**dict(snapshot), "url": f"/api/person-snapshots/{snapshot['id']}"}
        for snapshot in snapshots
    ]
    return item


@app.post("/api/person-tracks/observe", dependencies=[Depends(authenticated)])
async def observe_person(data: PersonObservation) -> dict[str, Any]:
    """Create or update a temporary per-camera person track.

    A tracker_key is only valid for one continuous camera tracking session.
    It is generated by the local object detector and is not a face identifier.
    """
    now = utc_now()
    now_text = utc_text(now)
    with connect() as conn:
        camera = conn.execute("SELECT id, name FROM cameras WHERE id = ? AND enabled = 1", (data.camera_id,)).fetchone()
        if not camera:
            raise HTTPException(status_code=404, detail="Enabled camera not found.")
        row = conn.execute(
            "SELECT * FROM person_tracks WHERE camera_id = ? AND tracker_key = ? AND ended_at IS NULL",
            (data.camera_id, data.tracker_key),
        ).fetchone()
        # A tracker key is session-scoped. If the detector stopped reporting it
        # for more than 45 seconds, close the old event rather than reusing it
        # for a later appearance.
        if row is not None:
            try:
                previous_seen = datetime.fromisoformat(row["last_seen"])
                if previous_seen.tzinfo is None:
                    previous_seen = previous_seen.replace(tzinfo=timezone.utc)
                if (now - previous_seen).total_seconds() > 45:
                    conn.execute(
                        "UPDATE person_tracks SET ended_at = ?, status = 'complete' WHERE id = ?",
                        (now_text, row["id"]),
                    )
                    row = None
            except ValueError:
                row = None
        created = row is None
        if created:
            public_id = create_public_person_id()
            cursor = conn.execute(
                "INSERT INTO person_tracks(public_id, camera_id, camera_name, tracker_key, first_seen, last_seen, confidence, appearance_description) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (public_id, data.camera_id, camera["name"], data.tracker_key, now_text, now_text,
                 data.confidence, data.appearance_description.strip()),
            )
            track_db_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO events(camera_id, camera_name, event_type, summary, confidence) VALUES (?, ?, ?, ?, ?)",
                (data.camera_id, camera["name"], "Person detected", f"Person event {public_id} started.", data.confidence),
            )
        else:
            public_id = row["public_id"]
            track_db_id = int(row["id"])
            conn.execute(
                "UPDATE person_tracks SET last_seen = ?, confidence = COALESCE(?, confidence), "
                "appearance_description = CASE WHEN ? = '' THEN appearance_description ELSE ? END WHERE id = ?",
                (now_text, data.confidence, data.appearance_description.strip(), data.appearance_description.strip(), track_db_id),
            )
        snapshot_allowed = True
        if data.snapshot_base64:
            latest = conn.execute(
                "SELECT captured_at FROM person_snapshots WHERE track_id = ? ORDER BY id DESC LIMIT 1",
                (track_db_id,),
            ).fetchone()
            if latest:
                try:
                    previous = datetime.fromisoformat(latest["captured_at"])
                    if previous.tzinfo is None:
                        previous = previous.replace(tzinfo=timezone.utc)
                    snapshot_allowed = (now - previous).total_seconds() >= 10
                except ValueError:
                    snapshot_allowed = True
        else:
            snapshot_allowed = False
    snapshot = None
    if data.snapshot_base64 and snapshot_allowed:
        snapshot = store_person_snapshot(track_db_id, public_id, data.snapshot_base64)
    with connect() as conn:
        result = conn.execute(
            "SELECT p.id, p.public_id, p.camera_id, p.camera_name, p.first_seen, p.last_seen, p.ended_at, "
            "p.confidence, p.appearance_description, p.status, "
            "(SELECT COUNT(*) FROM person_snapshots s WHERE s.track_id = p.id) AS snapshot_count "
            "FROM person_tracks p WHERE p.id = ?", (track_db_id,)
        ).fetchone()
    return {"created": created, "snapshot_saved": snapshot is not None, "snapshot": snapshot, "track": dict(result)}


@app.post("/api/person-tracks/{public_id}/end", dependencies=[Depends(authenticated)])
async def end_person_track(public_id: str) -> dict[str, Any]:
    now_text = utc_text()
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE person_tracks SET ended_at = ?, status = 'complete' "
            "WHERE public_id = ? AND ended_at IS NULL",
            (now_text, public_id),
        )
        if cursor.rowcount == 0:
            row = conn.execute("SELECT public_id, status FROM person_tracks WHERE public_id = ?", (public_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Person event not found.")
            return {"ended": False, "message": "This event was already completed.", "status": row["status"]}
        conn.execute(
            "INSERT INTO events(camera_id, camera_name, event_type, summary) "
            "SELECT camera_id, camera_name, 'Person left view', 'Person event ' || public_id || ' completed.' "
            "FROM person_tracks WHERE public_id = ?", (public_id,)
        )
    return {"ended": True, "public_id": public_id, "ended_at": now_text}


@app.get("/api/person-snapshots/{snapshot_id}", dependencies=[Depends(authenticated)])
async def get_person_snapshot(snapshot_id: int) -> FileResponse:
    with connect() as conn:
        row = conn.execute(
            "SELECT relative_path, media_type FROM person_snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Snapshot not found.")
    target = (DATA_DIR / row["relative_path"]).resolve()
    image_root = PERSON_IMAGE_DIR.resolve()
    if image_root not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="Snapshot file is unavailable.")
    return FileResponse(target, media_type=row["media_type"], headers={"Cache-Control": "private, max-age=60"})


@app.delete("/api/person-tracks/{public_id}", dependencies=[Depends(authenticated)])
async def delete_person_track(public_id: str) -> dict[str, bool]:
    with connect() as conn:
        row = conn.execute("SELECT id FROM person_tracks WHERE public_id = ?", (public_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Person event not found.")
        paths = conn.execute("SELECT relative_path FROM person_snapshots WHERE track_id = ?", (row["id"],)).fetchall()
        conn.execute("DELETE FROM person_tracks WHERE id = ?", (row["id"],))
    for item in paths:
        target = (DATA_DIR / item["relative_path"]).resolve()
        if PERSON_IMAGE_DIR.resolve() in target.parents:
            target.unlink(missing_ok=True)
    folder = PERSON_IMAGE_DIR / public_id
    if folder.exists() and not any(folder.iterdir()):
        folder.rmdir()
    return {"deleted": True}

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard() -> HTMLResponse:
    index_file = FRONTEND_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse("NexGard Security frontend is missing.", status_code=500)
    return HTMLResponse(index_file.read_text(encoding="utf-8"))


app.mount("/assets", StaticFiles(directory=FRONTEND_DIR), name="assets")
