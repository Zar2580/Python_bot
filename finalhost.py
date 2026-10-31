import asyncio
import base64
import html
import json
import logging
import os
import re
import shutil
import shlex
import signal
import sqlite3
import subprocess
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
try:
    from pyrogram import Client
    from pyrogram.errors import PasswordHashInvalid, PhoneCodeExpired, PhoneCodeInvalid, SessionPasswordNeeded
except ImportError:  # Allows syntax checks before dependencies are installed.
    Client = None
    PasswordHashInvalid = PhoneCodeExpired = PhoneCodeInvalid = SessionPasswordNeeded = Exception

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Preserved from the supplied sample.
BOT_TOKEN = "8717606762:AAFmiKXHB3mG0Qpj4-vhkhn5hk0FQYdsuRI"
ADMIN_IDS = {6858000955}
ADMIN_IDS.update(int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit())

BASE_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
PROJECT_DIR = BASE_DIR / "projects"
LOG_DIR = BASE_DIR / "logs"
DB_PATH = BASE_DIR / "runner.sqlite3"
for directory in (BASE_DIR, PROJECT_DIR, LOG_DIR):
    directory.mkdir(parents=True, exist_ok=True)

MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "50"))
MAX_PROJECTS = int(os.getenv("MAX_PROJECTS", "10"))
DEFAULT_QUOTA_MB = int(os.getenv("DEFAULT_QUOTA_MB", "500"))
MAX_RUNTIME_SECONDS = int(os.getenv("MAX_RUNTIME_SECONDS", "900"))
MAX_OUTPUT_BYTES = int(os.getenv("MAX_OUTPUT_BYTES", "200000"))
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "2"))
DOCKER_NETWORK = os.getenv("RUNNER_NETWORK", "bridge")
DOCKER_MEMORY = os.getenv("RUNNER_MEMORY", "512m")
DOCKER_CPUS = os.getenv("RUNNER_CPUS", "1.0")
DOCKER_PIDS_LIMIT = os.getenv("RUNNER_PIDS_LIMIT", "128")
DOCKER_BUILD_TIMEOUT = int(os.getenv("DOCKER_BUILD_TIMEOUT", "1200"))
LOGIN_TIMEOUT_SECONDS = int(os.getenv("LOGIN_TIMEOUT_SECONDS", "300"))
MAX_RESTARTS = int(os.getenv("MAX_RESTARTS", "8"))
RUNNER_NETWORK = os.getenv("RUNNER_NETWORK", "none")
DOCKER_IMAGE_PYTHON = os.getenv("DOCKER_IMAGE_PYTHON", "python:3.12-slim")
DOCKER_IMAGE_NODE = os.getenv("DOCKER_IMAGE_NODE", "node:22-slim")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("telegram-vps-runner")


PREMIUM_EMOJI = {
    "🤖": "5255883984151276991", "📁": "5472170432574528133", "💡": "5422439311196834318",
    "🔒": "5296369303661067030", "🆔": "6030656587830399914", "📖": "5388953246486269495",
    "⚙️": "5341715473882955310", "🗑️": "5445267414562389170", "🔑": "5307843983102204243",
    "⏰": "5319272710688226013", "⏱️": "5382194935057372936", "👤": "5373012449597335010",
    "📭": "5352896944496728039", "📌": "5240241223632954241", "🛑": "5413610645142642221", "🔢": "5226929552319594190",
    "📨": "5411225014148014586", "📦": "6023639019290630537", "🔴": "5400362079783770689",
    "🐳": "5226717982230591144", "🐍": "5370577035636786019", "🟩": "5409235997613372119",
    "📄": "5409150592188690356", "⬅️": "5361979468887893611", "🆕": "5413337163100083587",
    "📡": "5231012545799666522", "🔍": "5244837092042750681", "📈": "5246762912428603768",
    "📉": "5366288132834599020", "🖥️": "5298975240708187753", "💽": "5298975240708187753",
    "▶️": "5348125953090403204", "⏹️": "5134537521518085000", "🔄": "5375338737028841420",
    "📜": "5956561916573782596", "📂": "5418265444798721331", "📊": "5203993413346680064",
    "🛡️": "5251203410396458957", "❓": "6298557526560479072", "📥": "5443127283898405358",
    "✏️": "5395444784611480792", "⏳": "5386367538735104399", "✅": "5206607081334906820",
    "❌": "5210952531676504517", "⚠️": "5420323339723881652", "🟢": "5416081784641168838",
    "🚫": "5240241223632954241", "📣": "5424818078833715060",
    "🚫": "5240241223632954241", "📣": "5424818078833715060",
}


def premium_html(text: str) -> str:
    # Keep the original wording/flow and link only mapped emoji markers to Premium IDs.
    for glyph, emoji_id in sorted(PREMIUM_EMOJI.items(), key=lambda item: len(item[0]), reverse=True):
        text = text.replace(glyph, f'<tg-emoji emoji-id="{emoji_id}">{glyph}</tg-emoji>')
    return text


def render_broadcast_text(text: str) -> str:
    """Escape admin-provided text, then render mapped emojis as Telegram custom emoji."""
    return premium_html(html.escape(text, quote=False))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def esc(value: object) -> str:
    return html.escape(str(value))


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '', username TEXT NOT NULL DEFAULT '',
                approved INTEGER NOT NULL DEFAULT 0, blocked INTEGER NOT NULL DEFAULT 0,
                quota_mb INTEGER NOT NULL DEFAULT 500,
                used_mb REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL,
                path TEXT NOT NULL, kind TEXT NOT NULL, entrypoint TEXT,
                status TEXT NOT NULL DEFAULT 'stopped', container TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(user_id, name)
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, user_id INTEGER NOT NULL,
                mode TEXT NOT NULL, status TEXT NOT NULL, command TEXT NOT NULL,
                container TEXT, exit_code INTEGER, started_at TEXT, finished_at TEXT,
                log_path TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS schedules (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, user_id INTEGER NOT NULL,
                interval_seconds INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                next_run REAL NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS env_vars (
                project_id TEXT NOT NULL, name TEXT NOT NULL, value TEXT NOT NULL,
                PRIMARY KEY(project_id, name)
            );
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, action TEXT,
                detail TEXT, created_at TEXT NOT NULL
            );
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "blocked" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0")


def audit(user_id: int, action: str, detail: str = "") -> None:
    with db() as conn:
        conn.execute("INSERT INTO audit_logs(user_id, action, detail, created_at) VALUES(?,?,?,?)", (user_id, action, detail[:500], now()))


def user_row(user_id: int, update: Optional[Update] = None) -> sqlite3.Row:
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            name = update.effective_user.full_name if update and update.effective_user else ""
            username = update.effective_user.username if update and update.effective_user else ""
            conn.execute("INSERT INTO users(user_id,name,username,created_at) VALUES(?,?,?,?)", (user_id, name, username or "", now()))
            row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return row


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def approved(user_id: int) -> bool:
    row = user_row(user_id)
    return is_admin(user_id) or (bool(row["approved"]) and not bool(row["blocked"]))


def guard(update: Update) -> bool:
    return bool(update.effective_user and approved(update.effective_user.id))


def project_for(user_id: int, name: str) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM projects WHERE user_id=? AND name=?", (user_id, name)).fetchone()


def safe_name(name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", name.strip())
    return name[:40].strip("._-") or "project"


def safe_zip_extract(source: Path, target: Path) -> None:
    max_unpacked = MAX_FILE_MB * 10 * 1024 * 1024
    total = 0
    with zipfile.ZipFile(source) as archive:
        for item in archive.infolist():
            if item.is_dir():
                continue
            total += item.file_size
            if total > max_unpacked:
                raise ValueError("ZIP unpacked size is too large")
            destination = (target / item.filename).resolve()
            if not str(destination).startswith(str(target.resolve()) + os.sep):
                raise ValueError("Unsafe ZIP path detected")
        archive.extractall(target)


def detect_project(root: Path, original_name: str) -> tuple[str, str]:
    manifest = root / "runner.json"
    if manifest.exists():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        kind = data.get("type", "python")
        entry = data.get("entrypoint", "main.py" if kind == "python" else "index.js")
        if kind not in {"python", "node"}:
            raise ValueError("runner.json type must be python or node")
        if not (root / entry).is_file():
            raise ValueError("runner.json entrypoint does not exist")
        return kind, entry
    if original_name.endswith(".py"):
        return "python", original_name
    if original_name.endswith(".js"):
        return "node", original_name
    priority = ["main.py", "bot.py", "app.py", "index.py", "index.js", "main.js", "bot.js", "app.js"]
    files = {p.name: p for p in root.rglob("*") if p.is_file()}
    for item in priority:
        if item in files:
            return ("python" if item.endswith(".py") else "node"), str(files[item].relative_to(root))
    for p in sorted(files.values()):
        if p.suffix == ".py":
            return "python", str(p.relative_to(root))
        if p.suffix == ".js":
            return "node", str(p.relative_to(root))
    raise ValueError("No Python or JavaScript entrypoint found")


def docker_available() -> bool:
    return shutil.which("docker") is not None


def container_command(kind: str, entry: str) -> list[str]:
    if kind == "python":
        return ["python", "-u", entry]
    return ["node", entry]


async def _read_process(process: asyncio.subprocess.Process, log_path: Path, output: bytearray, limit: int) -> int:
    assert process.stdout
    async for line in process.stdout:
        if len(output) < limit:
            output.extend(line[:limit - len(output)])
        with log_path.open("ab") as handle:
            handle.write(line)
    return await process.wait()


class Runner:
    """Build and run uploaded projects in isolated Docker containers."""

    def __init__(self, application: Application):
        self.application = application
        self.tasks: dict[str, asyncio.Task] = {}
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.container_names: dict[str, str] = {}
        self.stop_requests: set[str] = set()

    async def start(self, project: sqlite3.Row, user_id: int, chat_id: int, mode: str = "once") -> str:
        if not docker_available():
            raise RuntimeError("Docker CLI is unavailable")
        job_id = uuid.uuid4().hex[:12]
        log_path = LOG_DIR / f"{job_id}.log"
        command = "docker project runner"
        with db() as conn:
            conn.execute("INSERT INTO jobs(id,project_id,user_id,mode,status,command,log_path,created_at) VALUES(?,?,?,?,?,?,?,?)", (job_id, project["id"], user_id, mode, "queued", command, str(log_path), now()))
            conn.execute("UPDATE projects SET status=?,updated_at=? WHERE id=?", ("queued", now(), project["id"]))
        self.tasks[job_id] = asyncio.create_task(self._run(job_id, project, user_id, chat_id, mode, log_path))
        return job_id

    async def _run(self, job_id: str, project: sqlite3.Row, user_id: int, chat_id: int, mode: str, log_path: Path) -> None:
        async with self.semaphore:
            result_code = 1
            output = bytearray()
            container = f"tg-runner-{job_id}"
            image = f"tg-project-{project['id'].lower()}"
            self.container_names[job_id] = container
            restart_count = 0
            log_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                project_path = Path(project["path"]).resolve()
                if not project_path.is_dir():
                    raise FileNotFoundError(str(project_path))
                env: dict[str, str] = {"PYTHONUNBUFFERED": "1", "NODE_ENV": "production"}
                with db() as conn:
                    env.update({r["name"]: r["value"] for r in conn.execute("SELECT name,value FROM env_vars WHERE project_id=?", (project["id"],)).fetchall()})
                    conn.execute("UPDATE jobs SET status='building',started_at=?,container=? WHERE id=?", (now(), container, job_id))
                    conn.execute("UPDATE projects SET status=?,updated_at=? WHERE id=?", ("building", now(), project["id"]))

                dockerfile = project_path / "Dockerfile"
                if dockerfile.is_file():
                    build_cmd = ["docker", "build", "--pull=false", "-t", image, str(project_path)]
                    with log_path.open("w", encoding="utf-8") as handle:
                        handle.write("$ " + " ".join(shlex.quote(x) for x in build_cmd) + "\n")
                    build = await asyncio.create_subprocess_exec(*build_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                    build_code = await asyncio.wait_for(_read_process(build, log_path, output, MAX_OUTPUT_BYTES), timeout=DOCKER_BUILD_TIMEOUT)
                    if build_code != 0:
                        raise RuntimeError(f"Docker build failed with exit code {build_code}")
                    run_cmd = ["docker", "run", "--name", container, "--rm",
                        "--label", "managed-by=phineas-vps-host",
                        "--cpus", DOCKER_CPUS, "--memory", DOCKER_MEMORY,
                        "--pids-limit", DOCKER_PIDS_LIMIT,
                        "-w", "/app", "--network", DOCKER_NETWORK]
                    for persistent_name in ("data", "downloads"):
                        persistent_path = project_path / persistent_name
                        persistent_path.mkdir(parents=True, exist_ok=True)
                        run_cmd += ["-v", f"{persistent_path}:/app/{persistent_name}:rw"]
                    for key, value in env.items():
                        run_cmd += ["-e", f"{key}={value}"]
                    run_cmd += [image]
                else:
                    if project["kind"] == "python":
                        image = DOCKER_IMAGE_PYTHON
                        inner = "if [ -f requirements.txt ]; then python -m pip install --no-cache-dir -r requirements.txt; fi; exec python -u " + shlex.quote(project["entrypoint"])
                    elif project["kind"] == "node":
                        image = DOCKER_IMAGE_NODE
                        inner = "if [ -f package.json ]; then npm install --omit=dev; fi; exec node " + shlex.quote(project["entrypoint"])
                    else:
                        raise ValueError("Project requires a Dockerfile")
                    run_cmd = ["docker", "run", "--name", container, "--rm",
                        "--label", "managed-by=phineas-vps-host",
                        "--cpus", DOCKER_CPUS, "--memory", DOCKER_MEMORY,
                        "--pids-limit", DOCKER_PIDS_LIMIT,
                        "-v", f"{project_path}:/workspace:rw", "-w", "/workspace",
                        "--network", DOCKER_NETWORK]
                    for key, value in env.items():
                        run_cmd += ["-e", f"{key}={value}"]
                    run_cmd += [image, "sh", "-lc", inner]

                with db() as conn:
                    conn.execute("UPDATE jobs SET status='running',command=?,container=? WHERE id=?", (" ".join(shlex.quote(x) for x in run_cmd), container, job_id))
                    conn.execute("UPDATE projects SET status=?,updated_at=? WHERE id=?", ("running", now(), project["id"]))
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write("\n$ " + " ".join(shlex.quote(x) for x in run_cmd) + "\n")

                while True:
                    process = await asyncio.create_subprocess_exec(*run_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                    self.processes[job_id] = process
                    try:
                        result_code = await asyncio.wait_for(_read_process(process, log_path, output, MAX_OUTPUT_BYTES), timeout=MAX_RUNTIME_SECONDS if mode != "service" else None)
                    except asyncio.TimeoutError:
                        subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=20)
                        output.extend(b"\n[Runner] Maximum runtime exceeded.\n")
                        result_code = 124
                    if mode != "service" or job_id in self.stop_requests or result_code == 124:
                        break
                    if result_code != 0:
                        restart_count += 1
                        if restart_count > MAX_RESTARTS:
                            output.extend(f"\n[Runner] Crash-loop protection stopped the service after {MAX_RESTARTS} restarts.\n".encode())
                            break
                        await asyncio.sleep(min(60, 2 ** min(restart_count, 5)))
                    else:
                        restart_count = 0
                        await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                output.extend(f"\n[Runner] {type(exc).__name__}: {exc}\n".encode())
            finally:
                self.processes.pop(job_id, None)
                self.container_names.pop(job_id, None)
                self.stop_requests.discard(job_id)
                status = "completed" if result_code == 0 else "failed"
                with db() as conn:
                    conn.execute("UPDATE jobs SET status=?,exit_code=?,finished_at=? WHERE id=?", (status, result_code, now(), job_id))
                    conn.execute("UPDATE projects SET status=?,updated_at=? WHERE id=?", ("running" if mode == "service" and result_code == 0 else "stopped", now(), project["id"]))
                message = f"{'✅' if result_code == 0 else '❌'} Job <code>{esc(job_id)}</code> <b>{status}</b>.\n\n🔢 Exit code: <code>{result_code}</code>\n\n<pre>{esc(output.decode(errors='replace')[-3500:])}</pre>"
                try:
                    await self.application.bot.send_message(chat_id, message, parse_mode=ParseMode.HTML)
                except Exception:
                    log.exception("failed to send job result")

    async def stop(self, job_id: str) -> bool:
        self.stop_requests.add(job_id)
        name = self.container_names.get(job_id)
        if not name:
            with db() as conn:
                row = conn.execute("SELECT container FROM jobs WHERE id=?", (job_id,)).fetchone()
            name = row["container"] if row else None
        if not name:
            return False
        graceful = subprocess.run(["docker", "stop", "-t", "10", name], capture_output=True, timeout=20)
        if graceful.returncode != 0:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=20)
        return True


# Login state is deliberately in memory only. OTP and 2FA are never persisted.
pending_logins: dict[tuple[int, str], dict] = {}


def project_env(project_id: str) -> dict[str, str]:
    with db() as conn:
        return {r["name"]: r["value"] for r in conn.execute("SELECT name,value FROM env_vars WHERE project_id=?", (project_id,)).fetchall()}


def mask_secrets(text: str, project_id: str | None = None) -> str:
    if not text:
        return text
    values = []
    if project_id:
        values = [v for v in project_env(project_id).values() if len(v) >= 4]
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, "********")
    return text


async def clear_login(key: tuple[int, str]) -> None:
    state = pending_logins.pop(key, None)
    if state:
        client = state.get("client")
        try:
            if client and client.is_connected:
                await client.disconnect()
        except Exception:
            log.exception("failed to close temporary login client")


async def login_expiry(key: tuple[int, str]) -> None:
    await asyncio.sleep(LOGIN_TIMEOUT_SECONDS)
    if key in pending_logins:
        await clear_login(key)


runner: Runner


def _button_icon(label: str) -> str | None:
    for glyph, emoji_id in sorted(PREMIUM_EMOJI.items(), key=lambda item: len(item[0]), reverse=True):
        if label.startswith(glyph):
            return emoji_id
    return None


def _button_parts(label: str) -> tuple[str, str | None]:
    emoji_id = _button_icon(label)
    if not emoji_id:
        return label, None
    for glyph in sorted(PREMIUM_EMOJI, key=len, reverse=True):
        if label.startswith(glyph):
            return label[len(glyph):].lstrip(), emoji_id
    return label, emoji_id


def custom_inline_button(label: str, callback_data: str) -> InlineKeyboardButton:
    text, emoji_id = _button_parts(label)
    if emoji_id:
        try:
            return InlineKeyboardButton(text=text, callback_data=callback_data, icon_custom_emoji_id=emoji_id)
        except TypeError:
            pass
    return InlineKeyboardButton(text=label, callback_data=callback_data)


def custom_reply_button(label: str) -> KeyboardButton:
    text, emoji_id = _button_parts(label)
    if emoji_id:
        try:
            return KeyboardButton(text=text, icon_custom_emoji_id=emoji_id)
        except TypeError:
            pass
    return KeyboardButton(text=label)


def bottom_menu() -> ReplyKeyboardMarkup:
    # Exact requested layout: two buttons per row.
    return ReplyKeyboardMarkup([
        [custom_reply_button("📦 Dependencies"), custom_reply_button("⏰ Schedule")],
        [custom_reply_button("⏱️ Unschedule"), custom_reply_button("👤 User Information")],
        [custom_reply_button("💽 Storage"), custom_reply_button("🆕 New Project")],
    ], resize_keyboard=True, is_persistent=True)


def menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [custom_inline_button("📂 Projects", callback_data="projects"), custom_inline_button("📊 System", callback_data="system")],
        [custom_inline_button("📨 Add File", callback_data="add_file")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    assert user
    user_row(user.id, update)
    if not approved(user.id):
        keyboard = InlineKeyboardMarkup([[custom_inline_button("🛡️ Request access", callback_data="request_access")]])
        await update.message.reply_text(premium_html(f'🛡️ <b>Access Restricted</b>\n\nYour account is pending approval.\n\n🆔 User ID: <code>{user.id}</code>'), parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return
    await update.message.reply_text(premium_html('🤖 <b>Telegram VPS Runner</b>\n\n📁 Send a <code>.py</code>, <code>.js</code>, or <code>.zip</code> file to create a project.'), parse_mode=ParseMode.HTML, reply_markup=menu())
    await update.message.reply_text(premium_html('Menu'), parse_mode=ParseMode.HTML, reply_markup=bottom_menu())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not guard(update):
        return
    text = ("📂 <b>Projects</b>\n\n"
            "Project ကိုရွေးပြီး Inline buttons နဲ့ လုပ်ဆောင်နိုင်ပါတယ်။\n\n"
            "▶️ Run\n\n"
            "⏹️ Stop\n\n"
            "🔄 Restart\n\n"
            "📜 Logs\n\n"
            "📁 Files\n\n"
            "✏️ Rename\n\n"
            "📊 System status")
    target = update.message or (update.callback_query.message if update.callback_query else None)
    if target is None:
        return
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=menu())
    else:
        await target.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=menu())


async def projects(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not guard(update):
        return
    with db() as conn:
        rows = conn.execute("SELECT id,name,kind,status,created_at FROM projects WHERE user_id=? ORDER BY created_at DESC", (update.effective_user.id,)).fetchall()
    target = update.message or update.callback_query.message
    if not rows:
        if update.callback_query:
            await update.callback_query.edit_message_text(premium_html("📂 Projects\n\nNo projects found."), parse_mode=ParseMode.HTML, reply_markup=menu())
        else:
            await target.reply_text(premium_html("📂 Projects\n\nNo projects found."), parse_mode=ParseMode.HTML, reply_markup=menu())
        return
    keyboard = []
    for row in rows:
        icon = "🟢" if row["status"] == "running" else "⏹️"
        keyboard.append([custom_inline_button(f"{icon} {row['name']}", callback_data=f"project:{row['id']}")])
    keyboard.append([custom_inline_button("📊 System", callback_data="system"), custom_inline_button("❓ Help", callback_data="help")])
    if update.callback_query:
        await update.callback_query.edit_message_text(premium_html("📂 Projects\n\nSelect a project:"), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await target.reply_text(premium_html("📂 Projects\n\nSelect a project:"), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))


async def project_panel(query, project: sqlite3.Row) -> None:
    icon = "🟢" if project["status"] == "running" else "⏹️"
    type_icon = "🐍" if project["kind"] == "python" else "🟩"
    status_label = "Running" if project["status"] == "running" else "Stopped"
    status_icon = "🟢" if project["status"] == "running" else "⏹️"
    text = premium_html(f"📂 Project Details\n\n"
            f"📌 Name: <code>{esc(project['name'])}</code>\n\n"
            f"{type_icon} Type: <code>{esc(project['kind'].title())}</code>\n\n"
            f"📄 Entrypoint: <code>{esc(project['entrypoint'])}</code>\n\n"
            f"{status_icon} Status: <code>{status_label}</code>")
    keyboard = InlineKeyboardMarkup([
        [custom_inline_button("▶️ Run", callback_data=f"run:{project['id']}"), custom_inline_button("⏹️ Stop", callback_data=f"stop:{project['id']}")],
        [custom_inline_button("🔄 Restart", callback_data=f"restart:{project['id']}"), custom_inline_button("📜 Logs", callback_data=f"logs:{project['id']}")],
        [custom_inline_button("🔐 Login Account", callback_data=f"login:{project['id']}"), custom_inline_button("📁 Files", callback_data=f"files:{project['id']}")],
        [custom_inline_button("✏️ Rename", callback_data=f"rename:{project['id']}")],
        [custom_inline_button("🗑️ Delete", callback_data=f"delete:{project['id']}"), custom_inline_button("⬅️ Back", callback_data="projects")],
    ])
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def run_command(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str = "once") -> None:
    if not guard(update):
        return
    if not context.args:
        await update.message.reply_text(premium_html('⚠️ Usage: /run PROJECT_NAME'), parse_mode=ParseMode.HTML)
        return
    project = project_for(update.effective_user.id, context.args[0])
    if not project:
        await update.message.reply_text(premium_html('❌ Project not found.'), parse_mode=ParseMode.HTML)
        return
    job_id = await runner.start(project, update.effective_user.id, update.effective_chat.id, mode)
    await update.message.reply_text(premium_html(f'⏳ <b>Job queued:</b> <code>{job_id}</code>'), parse_mode=ParseMode.HTML)


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not guard(update) or not context.args:
        return
    job_id = context.args[0]
    with db() as conn:
        row = conn.execute("SELECT id FROM jobs WHERE id=? AND user_id=?", (job_id, update.effective_user.id)).fetchone()
    if not row and not is_admin(update.effective_user.id):
        await update.message.reply_text(premium_html('❌ Job not found.'), parse_mode=ParseMode.HTML)
        return
    await update.message.reply_text(premium_html(" Stop requested." if await runner.stop(job_id) else '⚠️ Job is not running.'), parse_mode=ParseMode.HTML)


async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not guard(update) or not context.args:
        return
    with db() as conn:
        row = conn.execute("SELECT log_path,project_id FROM jobs WHERE id=? AND user_id=?", (context.args[0], update.effective_user.id)).fetchone()
    if not row:
        await update.message.reply_text(premium_html('❌ Job not found.'), parse_mode=ParseMode.HTML)
        return
    path = Path(row["log_path"])
    text = mask_secrets(path.read_text(encoding="utf-8", errors="replace")[-3800:], row["project_id"] if "project_id" in row.keys() else None) if path.exists() else ' No logs yet.'
    await update.message.reply_text(premium_html(f'📜 <b>Job Logs:</b>\n\n<pre>{esc(text)}</pre>'), parse_mode=ParseMode.HTML)


def project_size_mb(path: str) -> float:
    root = Path(path)
    total = 0
    try:
        for item in root.rglob("*"):
            if item.is_file():
                total += item.stat().st_size
    except (FileNotFoundError, OSError):
        return 0.0
    return total / 1024**2


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not guard(update) or not context.args:
        return
    project = project_for(update.effective_user.id, context.args[0])
    if not project:
        await update.message.reply_text(premium_html('❌ Project not found.'), parse_mode=ParseMode.HTML)
        return
    with db() as conn:
        active = conn.execute("SELECT id FROM jobs WHERE project_id=? AND status IN ('queued','building','running')", (project["id"],)).fetchall()
    for job in active:
        await runner.stop(job["id"])
    used_mb = project_size_mb(project["path"])
    with db() as conn:
        conn.execute("DELETE FROM env_vars WHERE project_id=?", (project["id"],))
        conn.execute("DELETE FROM schedules WHERE project_id=?", (project["id"],))
        conn.execute("DELETE FROM jobs WHERE project_id=?", (project["id"],))
        conn.execute("DELETE FROM projects WHERE id=?", (project["id"],))
        conn.execute("UPDATE users SET used_mb=MAX(0, used_mb-?) WHERE user_id=?", (used_mb, update.effective_user.id))
    shutil.rmtree(project["path"], ignore_errors=True)
    await update.message.reply_text(premium_html('🗑️ Project deleted successfully.'), parse_mode=ParseMode.HTML)


async def env_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not guard(update) or len(context.args) < 3:
        await update.message.reply_text(premium_html('⚠️ Usage: /env PROJECT KEY VALUE'), parse_mode=ParseMode.HTML)
        return
    project = project_for(update.effective_user.id, context.args[0])
    key = context.args[1]
    if not project or not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,63}", key):
        await update.message.reply_text(premium_html('❌ Invalid project or environment key format.'), parse_mode=ParseMode.HTML)
        return
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO env_vars(project_id,name,value) VALUES(?,?,?)", (project["id"], key, " ".join(context.args[2:])))
    await update.message.reply_text(premium_html(' Environment variable saved.'), parse_mode=ParseMode.HTML)


async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not guard(update) or len(context.args) < 2:
        await update.message.reply_text(premium_html('⚠️ Usage: /schedule PROJECT_NAME MINUTES'), parse_mode=ParseMode.HTML)
        return
    project = project_for(update.effective_user.id, context.args[0])
    try:
        minutes = int(context.args[1])
    except ValueError:
        minutes = 0
    if not project or minutes < 1 or minutes > 10080:
        await update.message.reply_text(premium_html('❌ Project not found or interval must be between 1–10080 minutes.'), parse_mode=ParseMode.HTML)
        return
    with db() as conn:
        conn.execute("DELETE FROM schedules WHERE project_id=?", (project["id"],))
        conn.execute("INSERT INTO schedules(id,project_id,user_id,interval_seconds,next_run,created_at) VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex[:12], project["id"], update.effective_user.id, minutes * 60, time.time() + minutes * 60, now()))
    await update.message.reply_text(premium_html(f' Schedule enabled for <b>{esc(project["name"])}</b> every {minutes} minutes.'), parse_mode=ParseMode.HTML)


async def unschedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not guard(update) or not context.args:
        await update.message.reply_text(premium_html('⚠️ Usage: /unschedule PROJECT_NAME'), parse_mode=ParseMode.HTML)
        return
    project = project_for(update.effective_user.id, context.args[0])
    if not project:
        await update.message.reply_text(premium_html('❌ Project not found.'), parse_mode=ParseMode.HTML)
        return
    with db() as conn:
        conn.execute("DELETE FROM schedules WHERE project_id=?", (project["id"],))
    await update.message.reply_text(premium_html(' Schedule removed successfully.'), parse_mode=ParseMode.HTML)


async def monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not guard(update):
        return
    disk = shutil.disk_usage(BASE_DIR)
    running = len(runner.processes)
    docker_status = '🟢 available' if docker_available() else '🔴 missing'
    text = premium_html(f'📊 <b>VPS Runner Status</b>\n\n'
            f'🔄 Running jobs: <code>{running}</code>\n\n'
            f'💾 Disk used: <code>{disk.used / 1024**3:.2f} GB</code> / <code>{disk.total / 1024**3:.2f} GB</code>\n\n'
            f'🐳 Docker: {docker_status}')
    target = update.message or update.callback_query.message
    await target.reply_text(text, parse_mode=ParseMode.HTML)


async def myinfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not guard(update):
        return
    row = user_row(update.effective_user.id)
    text = premium_html(f'👤 <b>Account Information</b>\n\n'
            f'🆔 User ID: <code>{row["user_id"]}</code>\n\n'
            f'✅ Approved: <code>{"yes" if row["approved"] else "no"}</code>')
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def storage_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not guard(update):
        return
    row = user_row(update.effective_user.id)
    with db() as conn:
        project_count = conn.execute("SELECT COUNT(*) AS n FROM projects WHERE user_id=?", (update.effective_user.id,)).fetchone()["n"]
    text = premium_html(f'💽 <b>Storage Information</b>\n\n'
            f'💾 Used: <code>{row["used_mb"]:.2f} MB</code>\n\n'
            f'💾 Quota: <code>{row["quota_mb"]} MB</code>\n\n'
            f'📂 Projects: <code>{project_count}</code>')
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def admin_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    text = premium_html("🛡️ <b>Admin Dashboard</b>\n\nAdmin action တစ်ခုရွေးပါ။")
    keyboard = InlineKeyboardMarkup([
        [custom_inline_button("👤 Users", "admin_users"), custom_inline_button("📊 System", "admin_system")],
        [custom_inline_button("📈 Activity", "admin_activity"), custom_inline_button("💽 Quotas", "admin_quotas")],
        [custom_inline_button("📣 Broadcast", "admin_broadcast"), custom_inline_button("🛡️ Approvals", "admin_approvals")],
        [custom_inline_button("🚫 Blocked", "admin_blocked"), custom_inline_button("⬅️ Back", "admin_back")],
    ])
    target = update.message or (update.callback_query.message if update.callback_query else None)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    elif target:
        await target.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def admin_broadcast_prompt(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["pending_broadcast"] = True
    await query.edit_message_text(
        premium_html("📣 <b>Broadcast</b>\n\nApproved users အားလုံးဆီ ပို့မယ့်စာကို ရိုးရိုးပို့ပါ။\n\nMapped emoji တွေကို Premium custom emoji အဖြစ် render လုပ်ပေးပါမယ်။\n\nမလုပ်တော့ရင် <code>/cancel</code> ရိုက်ပါ။"),
        parse_mode=ParseMode.HTML,
    )


def admin_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[custom_inline_button("⬅️ Back", "admin")]])


async def admin_users_panel(query) -> None:
    with db() as conn:
        rows = conn.execute("SELECT user_id,name,approved,blocked,used_mb,quota_mb FROM users ORDER BY created_at DESC LIMIT 50").fetchall()
    keyboard = [[custom_inline_button(f"👤 {r['name'] or r['user_id']}", f"admin_user:{r['user_id']}")] for r in rows]
    keyboard.append([custom_inline_button("⬅️ Back", "admin")])
    body = "👤 <b>Users</b>\n\nUser တစ်ယောက်ရွေးပါ။" if rows else "👤 <b>Users</b>\n\n📭 No users found."
    await query.edit_message_text(premium_html(body), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_approvals_panel(query) -> None:
    with db() as conn:
        rows = conn.execute("SELECT user_id,name FROM users WHERE approved=0 AND blocked=0 ORDER BY created_at DESC LIMIT 50").fetchall()
    keyboard = [[custom_inline_button(f"👤 {r['name'] or r['user_id']}", f"admin_user:{r['user_id']}")] for r in rows]
    keyboard.append([custom_inline_button("⬅️ Back", "admin")])
    body = "🛡️ <b>Pending Approvals</b>\n\nUser တစ်ယောက်ရွေးပါ။" if rows else "🛡️ <b>Pending Approvals</b>\n\n📭 No pending users."
    await query.edit_message_text(premium_html(body), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_blocked_panel(query) -> None:
    with db() as conn:
        rows = conn.execute("SELECT user_id,name FROM users WHERE blocked=1 ORDER BY created_at DESC LIMIT 50").fetchall()
    keyboard = [[custom_inline_button(f"👤 {r['name'] or r['user_id']}", f"admin_user:{r['user_id']}")] for r in rows]
    keyboard.append([custom_inline_button("⬅️ Back", "admin")])
    body = "🚫 <b>Blocked Users</b>\n\nUser တစ်ယောက်ရွေးပါ။" if rows else "🚫 <b>Blocked Users</b>\n\n📭 No blocked users."
    await query.edit_message_text(premium_html(body), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_quotas_panel(query) -> None:
    with db() as conn:
        rows = conn.execute("SELECT user_id,name,used_mb,quota_mb FROM users ORDER BY created_at DESC LIMIT 50").fetchall()
    keyboard = [[custom_inline_button(f"💽 {r['name'] or r['user_id']} ({r['quota_mb']} MB)", f"admin_quota:{r['user_id']}")] for r in rows]
    keyboard.append([custom_inline_button("⬅️ Back", "admin")])
    await query.edit_message_text(premium_html("💽 <b>Storage Quotas</b>\n\nပြင်မယ့် user ကိုရွေးပါ။"), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_user_panel(query, user_id: int) -> None:
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        await query.edit_message_text(premium_html("❌ User not found."), parse_mode=ParseMode.HTML, reply_markup=admin_back_keyboard())
        return
    status = "blocked" if row["blocked"] else ("approved" if row["approved"] else "pending")
    text = premium_html(f"👤 <b>User Information</b>\n\n🆔 ID: <code>{row['user_id']}</code>\n\n✅ Status: <code>{status}</code>\n\n💽 Storage: <code>{row['used_mb']:.2f} / {row['quota_mb']} MB</code>")
    approve_label = "✅ Approve" if not row["approved"] else "🟢 Approved"
    block_label = "✅ Unblock" if row["blocked"] else "🚫 Block"
    keyboard = InlineKeyboardMarkup([
        [custom_inline_button(approve_label, f"admin_approve:{user_id}"), custom_inline_button(block_label, f"admin_block:{user_id}")],
        [custom_inline_button("💽 Set Quota", f"admin_quota:{user_id}")],
        [custom_inline_button("⬅️ Back", "admin_users")],
    ])
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def admin_quota_prompt(query, context, user_id: int) -> None:
    context.user_data["pending_quota"] = user_id
    await query.edit_message_text(premium_html("💽 <b>Set Quota</b>\n\nQuota အသစ်ကို MB နဲ့ ရိုးရိုးပို့ပါ။\n\nဥပမာ: <code>1000</code>\n\nမလုပ်တော့ရင် <code>/cancel</code> ရိုက်ပါ။"), parse_mode=ParseMode.HTML)


async def send_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, message: str) -> None:
    if not update.effective_user or not is_admin(update.effective_user.id) or not update.message:
        return
    context.user_data.pop("pending_broadcast", None)
    if not message.strip():
        await update.message.reply_text(premium_html("⚠️ Broadcast message is empty."), parse_mode=ParseMode.HTML)
        return
    if len(message) > 2500:
        await update.message.reply_text(premium_html("⚠️ Broadcast message is too long. Maximum is 2500 characters."), parse_mode=ParseMode.HTML)
        context.user_data["pending_broadcast"] = True
        return
    rendered = render_broadcast_text(message)
    with db() as conn:
        recipients = [r["user_id"] for r in conn.execute("SELECT user_id FROM users WHERE approved=1 AND blocked=0").fetchall()]
    delivered = 0
    failed = 0
    for user_id in recipients:
        try:
            await context.bot.send_message(user_id, rendered, parse_mode=ParseMode.HTML)
            delivered += 1
        except Exception:
            failed += 1
            log.exception("broadcast delivery failed for user %s", user_id)
    audit(update.effective_user.id, "broadcast", f"delivered={delivered},failed={failed}")
    await update.message.reply_text(
        premium_html(f"✅ Broadcast completed.\n\n📨 Delivered: <code>{delivered}</code>\n\n❌ Failed: <code>{failed}</code>"),
        parse_mode=ParseMode.HTML,
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not guard(update) or not update.message:
        return
    document = update.message.document
    name = document.file_name or "upload"
    lower = name.lower()
    if not lower.endswith((".py", ".js", ".zip")):
        await update.message.reply_text(premium_html("❌ Only .py, .js, and .zip files are supported."), parse_mode=ParseMode.HTML)
        return
    if not document.file_size or document.file_size > MAX_FILE_MB * 1024 * 1024:
        await update.message.reply_text(premium_html(f"⚠️ File is too large. Maximum allowed: {MAX_FILE_MB} MB."), parse_mode=ParseMode.HTML)
        return
    context.user_data["pending_upload"] = {"file_id": document.file_id, "name": name, "size": document.file_size}
    await update.message.reply_text(premium_html("📨 File received!\n\nProject name ကို ရိုးရိုးပို့ပါ။\nဥပမာ: my_bot"), parse_mode=ParseMode.HTML)


async def create_project_from_pending(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_name: str) -> None:
    if not guard(update) or not update.message:
        return
    pending = context.user_data.pop("pending_upload", None)
    if not pending:
        await update.message.reply_text(premium_html("📨 အရင်ဆုံး .py, .js, သို့မဟုတ် .zip ဖိုင် ပို့ပါ။"), parse_mode=ParseMode.HTML)
        return
    project_name = safe_name(raw_name)
    if not raw_name.strip() or project_name != raw_name.strip():
        await update.message.reply_text(premium_html("⚠️ Project name မှာ English letters, numbers, dot, dash, underscore ပဲသုံးပါ။"), parse_mode=ParseMode.HTML)
        context.user_data["pending_upload"] = pending
        return
    if project_for(update.effective_user.id, project_name):
        await update.message.reply_text(premium_html("❌ That project name already exists. နာမည်အသစ်ပို့ပါ။"), parse_mode=ParseMode.HTML)
        context.user_data["pending_upload"] = pending
        return
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM projects WHERE user_id=?", (update.effective_user.id,)).fetchone()["n"]
        user = user_row(update.effective_user.id)
    if count >= MAX_PROJECTS:
        await update.message.reply_text(premium_html(f"⚠️ Project limit reached: {MAX_PROJECTS}"), parse_mode=ParseMode.HTML)
        context.user_data["pending_upload"] = pending
        return
    if user["used_mb"] + pending["size"] / 1024**2 > user["quota_mb"]:
        await update.message.reply_text(premium_html("⚠️ Storage quota exceeded."), parse_mode=ParseMode.HTML)
        context.user_data["pending_upload"] = pending
        return
    project_id = uuid.uuid4().hex[:16]
    root = PROJECT_DIR / str(update.effective_user.id) / project_id
    root.mkdir(parents=True, exist_ok=True)
    temp = root / "upload.bin"
    try:
        tg_file = await context.bot.get_file(pending["file_id"])
        await tg_file.download_to_drive(temp)
        if pending["name"].lower().endswith(".zip"):
            safe_zip_extract(temp, root)
            temp.unlink(missing_ok=True)
        else:
            destination = root / Path(pending["name"]).name
            temp.rename(destination)
        kind, entry = detect_project(root, pending["name"].lower())
        with db() as conn:
            conn.execute("INSERT INTO projects(id,user_id,name,path,kind,entrypoint,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (project_id, update.effective_user.id, project_name, str(root), kind, entry, now(), now()))
            conn.execute("UPDATE users SET used_mb=used_mb+? WHERE user_id=?", (pending["size"] / 1024**2, update.effective_user.id))
        audit(update.effective_user.id, "project_created", project_name)
        await update.message.reply_text(premium_html(f"✅ Project Created Successfully!\n\n📁 Name: {project_name}\n\n📦 Type: {kind}\n\n📄 Entrypoint: {entry}"), parse_mode=ParseMode.HTML, reply_markup=menu())
    except Exception as exc:
        shutil.rmtree(root, ignore_errors=True)
        await update.message.reply_text(premium_html(f"❌ Upload rejected: {exc}"), parse_mode=ParseMode.HTML)


async def plain_project_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await create_project_from_pending(update, context, (update.message.text or "").strip())


async def name_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(premium_html("📨 ဖိုင်ပို့ပြီး project name ကို ရိုးရိုးပို့ပါ။ Command မလိုပါ။"), parse_mode=ParseMode.HTML)
        return
    await create_project_from_pending(update, context, context.args[0])


async def request_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = query.from_user
    user_row(user.id)
    with db() as conn:
        conn.execute("UPDATE users SET name=?,username=? WHERE user_id=?", (user.full_name, user.username or "", user.id))

    admin_ids = sorted(ADMIN_IDS)
    if not admin_ids:
        await query.edit_message_text(premium_html("Access request could not be sent: no administrator is configured."), parse_mode=ParseMode.HTML)
        log.error("access request from %s failed: ADMIN_IDS is empty", user.id)
        return

    request_text = (
        "🛡️ <b>Access Request</b>\n\n"
        f"User: <code>{esc(user.full_name)}</code>\n\n"
        f"Username: <code>{esc('@' + user.username if user.username else 'not set')}</code>\n\n"
        f"User ID: <code>{user.id}</code>\n\n"
        f"Approve: <code>/approve {user.id}</code>\n\n"
        f"Reject/block: <code>/removeus {user.id}</code>"
    )
    delivered = 0
    failed = []
    for admin_id in admin_ids:
        try:
            await context.bot.send_message(admin_id, request_text, parse_mode=ParseMode.HTML)
            delivered += 1
        except Exception as exc:
            failed.append(f"{admin_id}: {exc}")
            log.exception("failed to send access request to admin %s", admin_id)

    if delivered:
        await query.edit_message_text(premium_html("📨 Access request sent to the administrator."), parse_mode=ParseMode.HTML)
    else:
        await query.edit_message_text(premium_html("Access request could not be delivered. Ask the administrator to open this bot and press /start first."), parse_mode=ParseMode.HTML)


async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id) or not context.args:
        return
    target = int(context.args[0])
    user_row(target)
    with db() as conn:
        conn.execute("UPDATE users SET approved=1 WHERE user_id=?", (target,))
    await update.message.reply_text(premium_html(f'✅ Approved user ID: <code>{target}</code>'), parse_mode=ParseMode.HTML)
    try:
        await context.bot.send_message(
            target,
            premium_html('✅ <b>Access Approved</b>\n\nYour VPS Runner access has been approved.\n\n🤖 You can now use the bot.'),
            parse_mode=ParseMode.HTML,
            reply_markup=menu(),
        )
    except Exception:
        log.exception("failed to notify approved user %s", target)


async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await approve(update, context)


async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id) or not context.args:
        return
    target = int(context.args[0])
    if target in ADMIN_IDS:
        await update.message.reply_text(premium_html('⚠️ Administrators cannot be removed.'), parse_mode=ParseMode.HTML)
        return
    with db() as conn:
        conn.execute("UPDATE users SET approved=0 WHERE user_id=?", (target,))
    await update.message.reply_text(premium_html(f'🚫 Access removed for user ID: <code>{target}</code>'), parse_mode=ParseMode.HTML)


async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    with db() as conn:
        rows = conn.execute("SELECT user_id,name,approved,used_mb,quota_mb FROM users ORDER BY created_at DESC").fetchall()
    
    # ပြင်ဆင်ထားသော Ternary operator သုံး၍ status ဖော်ပြချက်
    user_list_text = []
    for r in rows:
        status_str = "🟢 approved" if r["approved"] else "pending"
        user_list_text.append(f'• <code>{r["user_id"]}</code> | {esc(r["name"])} | {status_str} | {r["used_mb"]:.1f}/{r["quota_mb"]} MB')
    
    text = '👥 <b>Registered Users</b>\n\n' + "\n\n".join(user_list_text)
    await update.message.reply_text(text[:4000] if rows else ' No users found.', parse_mode=ParseMode.HTML)


async def schedule_menu(query, user_id: int) -> None:
    with db() as conn:
        rows = conn.execute("SELECT id,name,status FROM projects WHERE user_id=? ORDER BY created_at DESC", (user_id,)).fetchall()
    keyboard = [[custom_inline_button(f"⏰ {row['name']}", callback_data=f"schedule_project:{row['id']}")] for row in rows]
    keyboard.append([custom_inline_button("⬅️ Back", callback_data="projects")])
    await query.edit_message_text(premium_html("⏰ Schedule\n\nProject ကိုရွေးပါ။"), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))


async def unschedule_menu(query, user_id: int) -> None:
    with db() as conn:
        rows = conn.execute("SELECT id,name FROM projects WHERE user_id=? AND status='running' ORDER BY created_at DESC", (user_id,)).fetchall()
    keyboard = [[custom_inline_button(f"⏱️ {row['name']}", callback_data=f"unschedule_project:{row['id']}")] for row in rows]
    keyboard.append([custom_inline_button("⬅️ Back", callback_data="projects")])
    await query.edit_message_text(premium_html("⏱️ Unschedule\n\n24/7 service ရပ်မည့် project ကိုရွေးပါ။"), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_pending_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.message or not update.message.text:
        return False
    text = update.message.text.strip()
    if context.user_data.get("pending_quota") and is_admin(update.effective_user.id):
        target = int(context.user_data.pop("pending_quota"))
        try:
            quota = int(text)
            if quota < 1 or quota > 10_000_000:
                raise ValueError
            with db() as conn:
                conn.execute("UPDATE users SET quota_mb=? WHERE user_id=?", (quota, target))
            audit(update.effective_user.id, "quota_updated", f"user={target},quota_mb={quota}")
            await update.message.reply_text(premium_html(f"✅ Quota updated.\n\n🆔 User: <code>{target}</code>\n\n💽 Quota: <code>{quota} MB</code>"), parse_mode=ParseMode.HTML)
        except ValueError:
            await update.message.reply_text(premium_html("⚠️ Quota must be a whole number between 1 and 10000000 MB."), parse_mode=ParseMode.HTML)
        return True
    if context.user_data.get("pending_broadcast") and is_admin(update.effective_user.id):
        await send_broadcast(update, context, text)
        return True
    rename = context.user_data.pop("pending_rename", None)
    if rename:
        new_name = safe_name(text)
        if new_name != text.strip() or not new_name:
            await update.message.reply_text(premium_html("⚠️ Invalid name. Use letters, numbers, dot, dash, or underscore."), parse_mode=ParseMode.HTML)
            return True
        if rename["kind"] == "project":
            with db() as conn:
                duplicate = conn.execute("SELECT id FROM projects WHERE user_id=? AND name=? AND id<>?", (update.effective_user.id, new_name, rename["project_id"])).fetchone()
                row = conn.execute("SELECT path FROM projects WHERE id=? AND user_id=?", (rename["project_id"], update.effective_user.id)).fetchone()
                if duplicate or not row:
                    await update.message.reply_text(premium_html("❌ Project name already exists or project was not found."), parse_mode=ParseMode.HTML)
                    return True
                conn.execute("UPDATE projects SET name=?,updated_at=? WHERE id=?", (new_name, now(), rename["project_id"]))
            await update.message.reply_text(premium_html(f"✏️ Project renamed to <b>{esc(new_name)}</b>."), parse_mode=ParseMode.HTML)
            return True
        old_path = Path(rename["path"])
        new_path = old_path.with_name(new_name)
        if new_path.exists():
            await update.message.reply_text(premium_html("❌ That file name already exists."), parse_mode=ParseMode.HTML)
            return True
        try:
            old_path.rename(new_path)
            await update.message.reply_text(premium_html(f"✏️ File renamed to <code>{esc(new_name)}</code>."), parse_mode=ParseMode.HTML)
        except Exception as exc:
            await update.message.reply_text(premium_html(f"❌ Rename failed: <code>{esc(exc)}</code>"), parse_mode=ParseMode.HTML)
        return True
    return False


async def begin_account_login(query, context: ContextTypes.DEFAULT_TYPE, project: sqlite3.Row) -> None:
    if Client is None:
        await query.edit_message_text("❌ Pyrogram is not installed in the VPS bot image.")
        return
    values = project_env(project["id"])
    try:
        api_id = int(values["API_ID"])
        api_hash = values["API_HASH"]
    except (KeyError, ValueError):
        await query.edit_message_text(premium_html("❌ Set API_ID and API_HASH for this project first with /env."), parse_mode=ParseMode.HTML)
        return
    key = (query.from_user.id, project["id"])
    await clear_login(key)
    context.user_data["pending_login"] = {"project_id": project["id"], "step": "phone"}
    await query.edit_message_text(premium_html("🔐 <b>Telegram Account Login</b>\n\nPhone number ကို <code>+959...</code> ပုံစံနဲ့ ပို့ပါ။\n\nOTP/2FA ကို file ထဲမသိမ်းပါ။ Login ပြီးမှ session file ပဲ save ပါမယ်။\n\nCancel: <code>/cancel</code>"), parse_mode=ParseMode.HTML)


async def handle_login_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    pending = context.user_data.get("pending_login")
    if not pending or not update.message or not update.message.text:
        return False
    uid = update.effective_user.id
    project_id = pending["project_id"]
    key = (uid, project_id)
    text = update.message.text.strip()
    with db() as conn:
        project = conn.execute("SELECT * FROM projects WHERE id=? AND user_id=?", (project_id, uid)).fetchone()
    if not project:
        context.user_data.pop("pending_login", None)
        await update.message.reply_text("❌ Project not found.")
        return True
    try:
        if pending["step"] == "phone":
            if not re.fullmatch(r"\+[1-9][0-9]{6,14}", text):
                await update.message.reply_text("⚠️ Phone number format မမှန်ပါ။ +959... ပုံစံသုံးပါ။")
                return True
            values = project_env(project_id)
            api_id, api_hash = int(values["API_ID"]), values["API_HASH"]
            data_dir = Path(project["path"]) / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            session_path = data_dir / "account"
            client = Client(str(session_path), api_id=api_id, api_hash=api_hash, in_memory=False)
            await client.connect()
            sent = await client.send_code(text)
            pending_logins[key] = {"client": client, "phone": text, "phone_code_hash": sent.phone_code_hash, "project_id": project_id}
            pending["step"] = "otp"
            context.application.create_task(login_expiry(key))
            await update.message.reply_text("📨 OTP ပို့ပြီးပါပြီ။ OTP ကို ဒီ chat ထဲ ပို့ပါ။ ၅ မိနစ်အတွင်း ထည့်ပါ။")
            return True
        state = pending_logins.get(key)
        if not state:
            context.user_data.pop("pending_login", None)
            await update.message.reply_text("⌛ Login expired. ပြန်စပါ။")
            return True
        client = state["client"]
        if pending["step"] == "otp":
            code = re.sub(r"\s+", "", text)
            if not re.fullmatch(r"[0-9]{4,8}", code):
                await update.message.reply_text("⚠️ OTP code မမှန်ပါ။")
                return True
            try:
                await client.sign_in(state["phone"], state["phone_code_hash"], code)
            except SessionPasswordNeeded:
                pending["step"] = "2fa"
                await update.message.reply_text("🔑 ဒီ account မှာ 2FA ရှိပါတယ်။ 2FA password ကို ပို့ပါ။ Password ကို file/log ထဲ မသိမ်းပါ။")
                return True
            pending["step"] = "done"
        elif pending["step"] == "2fa":
            password = text
            try:
                await client.check_password(password)
            finally:
                password = None
            pending["step"] = "done"
        else:
            return True
        await client.disconnect()
        pending_logins.pop(key, None)
        context.user_data.pop("pending_login", None)
        await update.message.reply_text(premium_html("✅ Telegram account login အောင်မြင်ပါပြီ။ Session file ကို project ရဲ့ <code>data/</code> folder ထဲ save ထားပါတယ်။"), parse_mode=ParseMode.HTML)
    except (PhoneCodeInvalid, PhoneCodeExpired):
        await clear_login(key)
        context.user_data.pop("pending_login", None)
        await update.message.reply_text("❌ OTP မှားနေသည် သို့မဟုတ် expire ဖြစ်သွားပါပြီ။ Login အသစ်ပြန်စပါ။")
    except (PasswordHashInvalid, KeyError, ValueError) as exc:
        if pending.get("step") == "2fa":
            await clear_login(key)
            context.user_data.pop("pending_login", None)
        await update.message.reply_text("❌ Login information မမှန်ပါ။")
        log.warning("account login failed: %s", type(exc).__name__)
    except Exception as exc:
        await clear_login(key)
        context.user_data.pop("pending_login", None)
        await update.message.reply_text(f"❌ Login failed: {esc(type(exc).__name__)}", parse_mode=ParseMode.HTML)
    return True


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await handle_login_text(update, context):
        return
    if await handle_pending_text(update, context):
        return
    if context.user_data.get("pending_upload"):
        await plain_project_name(update, context)
        return
    if not guard(update) or not update.message:
        return
    text = (update.message.text or "").strip()
    if text in ("📦 Dependencies", "Dependencies"):
        await update.message.reply_text(premium_html("📦 Dependencies\n\nrequirements.txt သို့မဟုတ် package.json ပါတဲ့ project ကို Projects ထဲကရွေးပြီး ▶️ Run နှိပ်ပါ။"), parse_mode=ParseMode.HTML)
    elif text in ("⏰ Schedule", "⏱️ Unschedule", "Schedule", "Unschedule"):
        rows = []
        with db() as conn:
            rows = conn.execute("SELECT id,name FROM projects WHERE user_id=? ORDER BY created_at DESC", (update.effective_user.id,)).fetchall()
        cb = "schedule_project:" if text in ("⏰ Schedule", "Schedule") else "unschedule_project:"
        title = "⏰ Schedule" if text in ("⏰ Schedule", "Schedule") else "⏱️ Unschedule"
        keyboard = [[custom_inline_button(f"{title.split()[0]} {row['name']}", callback_data=f"{cb}{row['id']}")] for row in rows]
        keyboard.append([custom_inline_button("⬅️ Back", callback_data="projects")])
        await update.message.reply_text(premium_html(f"{title}\n\nProject ကိုရွေးပါ။"), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
    elif text in ("👤 User Information", "User Information"):
        await myinfo(update, context)
    elif text in ("💽 Storage", "Storage"):
        await storage_info(update, context)
    elif text in ("🆕 New Project", "New Project"):
        await update.message.reply_text(premium_html("🆕 New Project\n\n.py, .js, သို့မဟုတ် .zip ဖိုင်ကို ဒီ chat ထဲ ပို့ပါ။\nဖိုင်ပို့ပြီး project name ကို ရိုးရိုးပို့ပါ။"), parse_mode=ParseMode.HTML)


async def cancel_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user:
        pending = context.user_data.pop("pending_login", None)
        if pending:
            await clear_login((update.effective_user.id, pending["project_id"]))
        if is_admin(update.effective_user.id):
            context.user_data.pop("pending_broadcast", None)
            context.user_data.pop("pending_quota", None)
        await update.message.reply_text(premium_html("⬅️ Pending operation cancelled."), parse_mode=ParseMode.HTML)


def file_token(relative_path: str) -> str:
    return base64.urlsafe_b64encode(relative_path.encode()).decode().rstrip("=")


def file_from_token(root: Path, token: str) -> Path:
    padded = token + "=" * (-len(token) % 4)
    relative = base64.urlsafe_b64decode(padded.encode()).decode()
    path = (root / relative).resolve()
    if not str(path).startswith(str(root.resolve()) + os.sep) or not path.is_file():
        raise ValueError("Invalid file path")
    return path


async def file_panel(query, project_id: str, root: Path, relative_path: str) -> None:
    path = file_from_token(root, file_token(relative_path))
    token = file_token(relative_path)
    keyboard = InlineKeyboardMarkup([
        [custom_inline_button("📥 Download", callback_data=f"download:{project_id}:{token}")],
        [custom_inline_button("✏️ Rename", callback_data=f"frename:{project_id}:{token}"), custom_inline_button("🗑️ Delete", callback_data=f"fdelete:{project_id}:{token}")],
        [custom_inline_button("⬅️ Back", callback_data=f"files:{project_id}")],
    ])
    await query.edit_message_text(premium_html(f"📄 <b>{esc(relative_path)}</b>"), parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    data = query.data or ""
    if data == "request_access":
        await request_access(update, context)
    elif data == "add_file":
        await query.edit_message_text(premium_html("📨 Add File\n\n.py, .js, သို့မဟုတ် .zip ဖိုင်ကို ပို့ပါ။\n\nဖိုင်ကို ဒီ chat ထဲ ပို့လိုက်ပါ။"), parse_mode=ParseMode.HTML)
    elif data == "projects":
        await projects(update, context)
    elif data == "help":
        await help_command(update, context)
    elif data == "system":
        await monitor(update, context)
    elif data == "admin_back":
        await query.edit_message_text(premium_html("🤖 Telegram VPS Runner"), parse_mode=ParseMode.HTML, reply_markup=menu())
    elif data == "admin_system":
        await monitor(update, context)
    elif data == "admin_users" and is_admin(uid):
        await admin_users_panel(query)
    elif data == "admin_approvals" and is_admin(uid):
        await admin_approvals_panel(query)
    elif data == "admin_blocked" and is_admin(uid):
        await admin_blocked_panel(query)
    elif data == "admin_quotas" and is_admin(uid):
        await admin_quotas_panel(query)
    elif data == "admin_broadcast":
        if is_admin(uid):
            await admin_broadcast_prompt(query, context)
    elif data == "admin_activity":
        if is_admin(uid):
            with db() as conn:
                rows = conn.execute("SELECT user_id,action,detail,created_at FROM audit_logs ORDER BY id DESC LIMIT 20").fetchall()
            body = "\n\n".join(f"📈 <code>{r['user_id']}</code> · {esc(r['action'])}\n{esc(r['detail'])}" for r in rows) or "📭 No activity yet."
            await query.edit_message_text(premium_html(f"📈 <b>Activity</b>\n\n{body}"), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[custom_inline_button("⬅️ Back", "admin")]]))
    elif data.startswith("admin_user:") and is_admin(uid):
        await admin_user_panel(query, int(data.split(":", 1)[1]))
    elif data.startswith("admin_quota:") and is_admin(uid):
        await admin_quota_prompt(query, context, int(data.split(":", 1)[1]))
    elif data.startswith("admin_approve:") and is_admin(uid):
        target = int(data.split(":", 1)[1])
        user_row(target)
        with db() as conn:
            conn.execute("UPDATE users SET approved=1,blocked=0 WHERE user_id=?", (target,))
        audit(uid, "user_approved", str(target))
        try:
            await context.bot.send_message(target, premium_html("✅ <b>Access Approved</b>\n\nYour VPS Runner access has been approved.\n\n🤖 You can now use the bot."), parse_mode=ParseMode.HTML, reply_markup=menu())
        except Exception:
            log.exception("failed to notify approved user %s", target)
        await query.edit_message_text(premium_html(f"✅ User <code>{target}</code> approved."), parse_mode=ParseMode.HTML, reply_markup=admin_back_keyboard())
    elif data.startswith("admin_block:") and is_admin(uid):
        target = int(data.split(":", 1)[1])
        if target in ADMIN_IDS:
            await query.answer("Administrators cannot be blocked.", show_alert=True)
            return
        user_row(target)
        with db() as conn:
            row = conn.execute("SELECT blocked FROM users WHERE user_id=?", (target,)).fetchone()
            new_blocked = 0 if row["blocked"] else 1
            conn.execute("UPDATE users SET blocked=?,approved=? WHERE user_id=?", (new_blocked, 0 if new_blocked else 1, target))
        audit(uid, "user_unblocked" if not new_blocked else "user_blocked", str(target))
        await query.edit_message_text(premium_html(f"{'✅ User unblocked.' if not new_blocked else '🚫 User blocked.'}\n\n🆔 ID: <code>{target}</code>"), parse_mode=ParseMode.HTML, reply_markup=admin_back_keyboard())
    elif data in ("admin", "admin_approvals", "admin_blocked", "admin_quotas"):
        if is_admin(uid):
            await admin_dashboard(update, context)
    elif data == "schedule":
        await schedule_menu(query, uid)
    elif data == "unschedule":
        await unschedule_menu(query, uid)
    elif data.startswith("schedule_project:"):
        project_id = data.split(":", 1)[1]
        with db() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id=? AND user_id=?", (project_id, uid)).fetchone()
        if not row:
            await query.edit_message_text(premium_html("Project not found."), parse_mode=ParseMode.HTML)
            return
        keyboard = InlineKeyboardMarkup([
            [custom_inline_button("5 minutes", callback_data=f"schedule_time:{project_id}:5")],
            [custom_inline_button("15 minutes", callback_data=f"schedule_time:{project_id}:15")],
            [custom_inline_button("30 minutes", callback_data=f"schedule_time:{project_id}:30")],
            [custom_inline_button("60 minutes", callback_data=f"schedule_time:{project_id}:60")],
            [custom_inline_button("24/7 Service", callback_data=f"schedule_247:{project_id}")],
            [custom_inline_button("⬅️ Back", callback_data="projects")],
        ])
        await query.edit_message_text(premium_html("⏰ Schedule\n\nRun interval ကိုရွေးပါ။"), parse_mode=ParseMode.HTML, reply_markup=keyboard)
    elif data.startswith("schedule_time:"):
        _, project_id, minutes_text = data.split(":", 2)
        minutes = int(minutes_text)
        with db() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id=? AND user_id=?", (project_id, uid)).fetchone()
            if row:
                conn.execute("DELETE FROM schedules WHERE project_id=? AND user_id=?", (project_id, uid))
                conn.execute("INSERT INTO schedules(id,project_id,user_id,interval_seconds,next_run,created_at) VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex[:12], project_id, uid, minutes * 60, time.time() + minutes * 60, now()))
        await query.edit_message_text(premium_html(f"⏰ Schedule enabled\n\nEvery {minutes} minutes."), parse_mode=ParseMode.HTML)
    elif data.startswith("schedule_247:"):
        project_id = data.split(":", 1)[1]
        with db() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id=? AND user_id=?", (project_id, uid)).fetchone()
        if not row:
            await query.edit_message_text(premium_html("Project not found."), parse_mode=ParseMode.HTML)
            return
        job_id = await runner.start(row, uid, query.message.chat_id, "service")
        await query.edit_message_text(premium_html(f"⚙️ 24/7 Service mode enabled\n\n⏳ Job queued: <code>{job_id}</code>"), parse_mode=ParseMode.HTML)
    elif data.startswith("unschedule_project:"):
        project_id = data.split(":", 1)[1]
        with db() as conn:
            jobs = conn.execute("SELECT id FROM jobs WHERE project_id=? AND user_id=? AND mode='service' AND status IN ('queued','running')", (project_id, uid)).fetchall()
            conn.execute("DELETE FROM schedules WHERE project_id=? AND user_id=?", (project_id, uid))
        for job in jobs:
            await runner.stop(job["id"])
        await query.edit_message_text(premium_html("⏱️ 24/7 Service stopped."), parse_mode=ParseMode.HTML)
    elif data.startswith("file:"):
        _, project_id, token = data.split(":", 2)
        with db() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id=? AND user_id=?", (project_id, uid)).fetchone()
        if not row:
            await query.edit_message_text(premium_html("Project not found."), parse_mode=ParseMode.HTML)
            return
        try:
            root = Path(row["path"])
            relative = base64.urlsafe_b64decode((token + "=" * (-len(token) % 4)).encode()).decode()
            await file_panel(query, project_id, root, relative)
        except Exception:
            await query.edit_message_text(premium_html("❌ File not found."), parse_mode=ParseMode.HTML)
    elif data.startswith("download:"):
        _, project_id, token = data.split(":", 2)
        with db() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id=? AND user_id=?", (project_id, uid)).fetchone()
        try:
            path = file_from_token(Path(row["path"]), token)
            await query.message.reply_document(document=str(path), caption=f"📥 {path.name}")
        except Exception:
            await query.edit_message_text(premium_html("❌ File not found."), parse_mode=ParseMode.HTML)
    elif data.startswith("frename:"):
        _, project_id, token = data.split(":", 2)
        with db() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id=? AND user_id=?", (project_id, uid)).fetchone()
        try:
            path = file_from_token(Path(row["path"]), token)
            context.user_data["pending_rename"] = {"kind": "file", "path": str(path)}
            await query.edit_message_text(premium_html("✏️ Rename File\n\nFile name အသစ်ကို ရိုးရိုးပို့ပါ။"), parse_mode=ParseMode.HTML)
        except Exception:
            await query.edit_message_text(premium_html("❌ File not found."), parse_mode=ParseMode.HTML)
    elif data.startswith("fdelete:"):
        _, project_id, token = data.split(":", 2)
        with db() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id=? AND user_id=?", (project_id, uid)).fetchone()
        try:
            path = file_from_token(Path(row["path"]), token)
            path.unlink()
            await query.edit_message_text(premium_html("🗑️ File deleted successfully."), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[custom_inline_button("⬅️ Back", callback_data=f"files:{project_id}")]]))
        except Exception:
            await query.edit_message_text(premium_html("❌ File could not be deleted."), parse_mode=ParseMode.HTML)
    elif data == "delete_confirm":
        pass
    elif data.startswith("delete_confirm:"):
        project_id = data.split(":", 1)[1]
        with db() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id=? AND user_id=?", (project_id, uid)).fetchone()
            active = conn.execute("SELECT id FROM jobs WHERE project_id=? AND status IN ('queued','building','running')", (project_id,)).fetchall()
        for job in active:
            await runner.stop(job["id"])
        if row:
            used_mb = project_size_mb(row["path"])
            with db() as conn:
                conn.execute("DELETE FROM env_vars WHERE project_id=?", (project_id,))
                conn.execute("DELETE FROM schedules WHERE project_id=?", (project_id,))
                conn.execute("DELETE FROM jobs WHERE project_id=?", (project_id,))
                conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
                conn.execute("UPDATE users SET used_mb=MAX(0, used_mb-?) WHERE user_id=?", (used_mb, uid))
            shutil.rmtree(row["path"], ignore_errors=True)
            await query.edit_message_text(premium_html("🗑️ Project deleted successfully."), parse_mode=ParseMode.HTML, reply_markup=menu())
        else:
            await query.edit_message_text(premium_html("❌ Project not found."), parse_mode=ParseMode.HTML)
    elif data.startswith("login:"):
        project_id = data.split(":", 1)[1]
        with db() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id=? AND user_id=?", (project_id, uid)).fetchone()
        if row:
            await begin_account_login(query, context, row)
        else:
            await query.edit_message_text(premium_html("❌ Project not found."), parse_mode=ParseMode.HTML)
    elif data.startswith("project:"):
        with db() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id=? AND user_id=?", (data.split(":", 1)[1], uid)).fetchone()
        if row:
            await project_panel(query, row)
    elif data.startswith("files:"):
        project_id = data.split(":", 1)[1]
        with db() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id=? AND user_id=?", (project_id, uid)).fetchone()
        if row:
            root = Path(row["path"])
            files = [p for p in root.rglob("*") if p.is_file() and p.name != "upload.bin"]
            keyboard = [[custom_inline_button(f"📄 {str(p.relative_to(root))[:50]}", callback_data=f"file:{project_id}:{file_token(str(p.relative_to(root)))}")] for p in files[:100]]
            keyboard.append([custom_inline_button("⬅️ Back", callback_data=f"project:{project_id}")])
            await query.edit_message_text(premium_html("📁 Files\n\nဖိုင်တစ်ခုရွေးပါ။"), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
    elif ":" in data:
        action, project_id = data.split(":", 1)
        with db() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id=? AND user_id=?", (project_id, uid)).fetchone()
        if not row:
            await query.edit_message_text(premium_html("Project not found."), parse_mode=ParseMode.HTML)
            return
        if action == "run":
            job_id = await runner.start(row, uid, query.message.chat_id, "once")
            await query.edit_message_text(premium_html(f"⏳ Job queued: <code>{job_id}</code>"), parse_mode=ParseMode.HTML)
        elif action == "restart":
            with db() as conn:
                running = conn.execute("SELECT id FROM jobs WHERE project_id=? AND status='running' ORDER BY started_at DESC LIMIT 1", (project_id,)).fetchone()
            if running:
                await runner.stop(running["id"])
            job_id = await runner.start(row, uid, query.message.chat_id, "service")
            await query.edit_message_text(premium_html(f"🔄 Restart requested.\n\n⏳ Job queued: <code>{job_id}</code>"), parse_mode=ParseMode.HTML)
        elif action == "stop":
            with db() as conn:
                running = conn.execute("SELECT id FROM jobs WHERE project_id=? AND status='running' ORDER BY started_at DESC LIMIT 1", (project_id,)).fetchone()
            await query.edit_message_text(premium_html("⏹️ Stop requested." if running and await runner.stop(running["id"]) else "Project is not running."), parse_mode=ParseMode.HTML)
        elif action == "logs":
            with db() as conn:
                latest = conn.execute("SELECT log_path FROM jobs WHERE project_id=? ORDER BY created_at DESC LIMIT 1", (project_id,)).fetchone()
            path = Path(latest["log_path"]) if latest else None
            text = mask_secrets(path.read_text(encoding="utf-8", errors="replace")[-3500:], project_id) if path and path.exists() else "No logs yet."
            await query.edit_message_text(premium_html(f"📜 Logs\n\n<pre>{esc(text)}</pre>"), parse_mode=ParseMode.HTML)
        elif action == "files":
            root = Path(row["path"])
            files = [p for p in root.rglob("*") if p.is_file() and p.name != "upload.bin"]
            keyboard = [[custom_inline_button(f"📄 {str(p.relative_to(root))[:50]}", callback_data=f"file:{project_id}:{file_token(str(p.relative_to(root)))}")] for p in files[:100]]
            keyboard.append([custom_inline_button("⬅️ Back", callback_data=f"project:{project_id}")])
            await query.edit_message_text(premium_html("📁 Files\n\nဖိုင်တစ်ခုရွေးပါ။"), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
        elif action == "delete":
            await query.edit_message_text(premium_html("🗑️ Delete Project?\n\nProject ကိုဖျက်မလား?"), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[custom_inline_button("🗑️ Confirm Delete", callback_data=f"delete_confirm:{project_id}"), custom_inline_button("⬅️ Cancel", callback_data=f"project:{project_id}")]]))
        elif action == "rename":
            context.user_data["pending_rename"] = {"kind": "project", "project_id": project_id}
            await query.edit_message_text(premium_html("✏️ Rename\n\nProject name အသစ်ကို ရိုးရိုးပို့ပါ။"), parse_mode=ParseMode.HTML)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("telegram error", exc_info=context.error)


async def scheduler_loop(application: Application) -> None:
    while True:
        await asyncio.sleep(30)
        with db() as conn:
            rows = conn.execute("SELECT s.id AS schedule_id,s.interval_seconds,s.next_run,p.* FROM schedules s JOIN projects p ON p.id=s.project_id WHERE s.enabled=1 AND s.next_run<=?", (time.time(),)).fetchall()
            for row in rows:
                await runner.start(row, row["user_id"], row["user_id"], "scheduled")
                conn.execute("UPDATE schedules SET next_run=? WHERE id=?", (time.time() + row["interval_seconds"], row["schedule_id"]))


async def restore_services() -> None:
    with db() as conn:
        rows = conn.execute("SELECT j.id AS old_job_id, p.* FROM jobs j JOIN projects p ON p.id=j.project_id WHERE j.mode='service' AND j.status IN ('queued','building','running')").fetchall()
        conn.execute("UPDATE jobs SET status='failed',exit_code=125,finished_at=? WHERE mode='service' AND status IN ('queued','building','running')", (now(),))
        conn.execute("UPDATE projects SET status='stopped',updated_at=? WHERE status IN ('queued','building','running')", (now(),))
    for row in rows:
        try:
            await runner.start(row, row["user_id"], row["user_id"], "service")
        except Exception:
            log.exception("failed to restore service for project %s", row["id"])


async def post_init(application: Application) -> None:
    global runner
    runner = Runner(application)
    application.create_task(scheduler_loop(application))
    application.create_task(restore_services())


def main() -> None:
    init_db()
    if not docker_available():
        log.warning("Docker is not installed or not in PATH")
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin", admin_dashboard))
    application.add_handler(CommandHandler("cancel", cancel_pending))
    application.add_handler(CommandHandler("projects", projects))
    application.add_handler(CommandHandler("run", run_command))
    application.add_handler(CommandHandler("service", lambda u, c: run_command(u, c, "service")))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("logs", logs_command))
    application.add_handler(CommandHandler("delete", delete_command))
    application.add_handler(CommandHandler("env", env_command))
    application.add_handler(CommandHandler("schedule", schedule_command))
    application.add_handler(CommandHandler("unschedule", unschedule_command))
    application.add_handler(CommandHandler("name", name_command))
    application.add_handler(CommandHandler("monitor", monitor))
    application.add_handler(CommandHandler("myinfo", myinfo))
    application.add_handler(CommandHandler("approve", approve))
    application.add_handler(CommandHandler("addus", add_user))
    application.add_handler(CommandHandler("removeus", remove_user))
    application.add_handler(CommandHandler("listus", list_users))
    application.add_handler(CallbackQueryHandler(button))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_error_handler(error_handler)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
