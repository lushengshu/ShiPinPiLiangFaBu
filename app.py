#!/usr/bin/env python3
"""本地抖音批量发布助手：管理视频队列，并把重复操作集中到一个页面。"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import io
import json
import mimetypes
import os
import plistlib
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

try:
    import openpyxl
except ImportError:
    openpyxl = None


ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / ".douyin_batch"
QUEUE_FILE = STATE_DIR / "queue.json"
COVER_DIR = STATE_DIR / "covers"
CHANNELS_STATE_FILE = STATE_DIR / "channels_state.json"
CHANNELS_IDS_FILE = STATE_DIR / "channels_queue_ids.json"
CHANNELS_PROFILE_DIR = ROOT / ".channels_browser_profile"
CHANNELS_UI_PORT = 8786
CHANNELS_UI_URL = f"http://127.0.0.1:{CHANNELS_UI_PORT}/"
COPY_LIBRARY_FILE = ROOT / "文案" / "song_copy_library.json"


def _ensure_state_dir() -> None:
    """Create STATE_DIR if missing; tolerate sandbox PermissionError on existing dirs."""
    if not STATE_DIR.exists():
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            pass
PUBLISH_STATE_FILE = STATE_DIR / "publish_state.json"
ANALYSIS_STATE_FILE = STATE_DIR / "analysis_state.json"
ANALYSIS_CANCEL_FILE = STATE_DIR / "analysis.cancel"
PAUSE_REQUEST_FILE = STATE_DIR / "pause.requested"
STOP_REQUEST_FILE = STATE_DIR / "stop.requested"
PUBLISHER_SCRIPT = ROOT / "browser_publisher.py"
MEDIA_ROOT_FILE = STATE_DIR / "media_root.txt"
_configured_media_root = os.environ.get("DOUYIN_MEDIA_DIR")
if not _configured_media_root and MEDIA_ROOT_FILE.exists():
    try:
        _configured_media_root = MEDIA_ROOT_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        _configured_media_root = None
MEDIA_ROOT = Path(_configured_media_root or str(ROOT)).expanduser().resolve()
STATIC_DIR = ROOT / "static"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"
# 抖音创作者中心把全景上传放在普通上传页的独立 tab=4 路由。这个入口
# 仍由浏览器页面负责校验账号权限；如果账号没有全景能力，发布器必须停在
# 页面上交给人工处理，不能悄悄回退到普通视频上传。
PANORAMA_UPLOAD_URL = f"{UPLOAD_URL}?default-tab=4"
SCHEDULE_MODES = {"immediate", "scheduled"}
PUBLISH_PLATFORMS = ("douyin", "channels")
MAX_SCHEDULE_INTERVAL_MINUTES = 60 * 24 * 30

VIDEO_TYPES = {"normal", "panorama"}
PANORAMA_FORMATS = {
    # view_size / dimention 是抖音公开全景视频参数的对应关系。浏览器
    # 自动化只使用 label 选择页面控件，不直接调用接口。
    "360_mono": {"label": "360°普通单目", "view_size": 360, "dimention": 0},
    "360_stereo": {"label": "360°立体双目", "view_size": 360, "dimention": 1},
    "180_mono": {"label": "180°普通单目", "view_size": 180, "dimention": 0},
    "180_stereo": {"label": "180°立体双目", "view_size": 180, "dimention": 2},
}
PANORAMA_MAX_BYTES = 16 * 1024 * 1024 * 1024
PANORAMA_MAX_DURATION_SECONDS = 10 * 60
PANORAMA_RECOMMENDED_WIDTH = 3840
PANORAMA_RECOMMENDED_HEIGHT = 1920


# V2 优先做本机 AVFoundation + Vision 关键帧分析；若系统后端不可用，
# 才降级到明确标注的文件名/元数据候选，绝不把降级结果冒充成画面识别。
# Bump this whenever the evidence rules or native helper output changes.  The
# version is part of the cache key, so an older OCR result cannot silently
# survive a stricter batch-analysis implementation.
# Candidate copy is generated from the per-video evidence fingerprint as well
# as the raw Vision/SoundAnalysis result.  Bumping this version invalidates
# older cached analyses whose three captions were identical or overly
# diagnostic across clips.
ANALYSIS_VERSION = "v2.3.1-natural-evidence-copy"
ANALYSIS_MAX_BATCH = 100
VISION_HELPER_SOURCE = ROOT / "tools" / "video_vision.swift"
VISION_HELPER_BINARY = STATE_DIR / "video_vision"
SOUND_HELPER_SOURCE = ROOT / "tools" / "sound_analysis.swift"
SOUND_HELPER_BINARY = STATE_DIR / "sound_analysis"
ANALYSIS_CACHE_DIR = STATE_DIR / "analysis_cache"
_VISION_BUILD_LOCK = threading.Lock()
_QUEUE_SAVE_LOCK = threading.RLock()
_PANORAMA_PROFILE_CACHE: dict[tuple[str, int, int], dict] = {}
_PANORAMA_PROFILE_CACHE_LOCK = threading.RLock()

# 批量识别与发布是两条独立控制面：识别只生成只读候选，不写回队列，
# 因此可以在页面上显示进度/取消，而不会误触发浏览器发布。
_ANALYSIS_STATE_LOCK = threading.RLock()
_ANALYSIS_THREAD: threading.Thread | None = None


def choose_media_folder() -> Path | None:
    """打开 macOS 原生文件夹选择器；取消时返回 None。"""
    if sys.platform != "darwin":
        return None
    result = subprocess.run(
        ["osascript", "-e", 'POSIX path of (choose folder with prompt "选择视频所在文件夹")'],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    selected = result.stdout.strip()
    return Path(selected).expanduser().resolve() if selected else None


def choose_media_files() -> list[Path]:
    """打开 macOS 原生文件选择器，允许一次选择多个视频文件。

    文件选择器只负责返回用户明确选择的路径；路径的扩展名、文件类型和
    是否仍然存在会在加入队列前再次校验。取消选择或非 macOS 环境返回空
    列表，不会修改当前队列。
    """
    if sys.platform != "darwin":
        return []
    script = (
        'set selectedFiles to choose file with prompt "选择要加入的视频文件" '
        'with multiple selections allowed\n'
        'set output to ""\n'
        'repeat with selectedFile in selectedFiles\n'
        'set output to output & (POSIX path of selectedFile) & linefeed\n'
        'end repeat\n'
        'return output'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []
    selected: list[Path] = []
    seen: set[str] = set()
    for raw in result.stdout.splitlines():
        value = raw.strip()
        if not value:
            continue
        path = Path(value).expanduser().resolve()
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        selected.append(path)
    return selected


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_schedule_mode(value: object) -> str:
    mode = str(value or "immediate").strip().lower()
    if mode not in SCHEDULE_MODES:
        raise ValueError("发布方式必须是 immediate 或 scheduled")
    return mode


def normalize_schedule_at(value: object) -> str:
    """Normalize a queue schedule to local ``YYYY-MM-DDTHH:MM`` wall time."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("定时发布时间格式无效，请使用 YYYY-MM-DDTHH:MM") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed.replace(second=0, microsecond=0).isoformat(timespec="minutes")


def normalize_schedule_interval(value: object) -> str:
    """Normalize an optional per-item interval in minutes."""
    text = str(value or "").strip()
    if not text:
        return ""
    if not re.fullmatch(r"\d+", text):
        raise ValueError("间隔必须是正整数分钟")
    minutes = int(text)
    if minutes < 1 or minutes > MAX_SCHEDULE_INTERVAL_MINUTES:
        raise ValueError(f"间隔必须在 1 到 {MAX_SCHEDULE_INTERVAL_MINUTES} 分钟之间")
    return str(minutes)


def validate_schedule_fields(mode: object, schedule_at: object, interval: object = "") -> tuple[str, str, str]:
    """Normalize one queue item's schedule fields as a consistent tuple.

    ``schedule_at`` is a local wall-clock value (the same convention used by
    an HTML ``datetime-local`` control).  The browser publisher converts that
    value to the local browser time zone immediately before filling Douyin's
    form.  An immediate item deliberately has no active appointment time;
    clearing it here prevents a stale date from being reused after a user
    switches a card back to immediate publishing.
    """
    normalized_mode = normalize_schedule_mode(mode)
    if normalized_mode == "immediate":
        # An interval has no meaning for immediate publishing.  Clear stale
        # values from older queue records instead of rejecting an otherwise
        # valid mode switch because a dormant field is malformed.
        return normalized_mode, "", ""
    normalized_at = normalize_schedule_at(schedule_at)
    normalized_interval = normalize_schedule_interval(interval)
    if not normalized_at:
        raise ValueError("选择定时发布后必须填写具体发布时间")
    return normalized_mode, normalized_at, normalized_interval


def normalize_video_type(value: object) -> str:
    """Normalize the queue's publishing mode while keeping old queues valid."""
    mode = str(value or "normal").strip().lower()
    if mode not in VIDEO_TYPES:
        raise ValueError("发布类型必须是 normal 或 panorama")
    return mode


def normalize_panorama_format(value: object) -> str:
    """Normalize one of the four platform panoramic format choices."""
    fmt = str(value or "").strip().lower()
    if not fmt:
        return ""
    if fmt not in PANORAMA_FORMATS:
        raise ValueError("全景视频格式无效，请选择 360/180 的单目或双目格式")
    return fmt


def normalize_bool(value: object, *, default: bool = False) -> bool:
    """Normalize a user-editable checkbox value without trusting JSON types.

    Queue records are edited by the local browser, but older versions and
    hand-edited queue files may contain strings such as ``"false"``.  Using
    Python's plain ``bool("false")`` would incorrectly enable a setting, so
    the two panorama consent/matching flags use this explicit parser.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y", "on", "checked", "是", "启用", "开启"}:
        return True
    if text in {"0", "false", "no", "n", "off", "unchecked", "否", "禁用", "关闭", ""}:
        return False
    return default


def normalize_platform_states(value: object, previous: dict | None = None, video_type: object = "normal") -> dict:
    """Keep per-platform publishing state independent and backward-compatible."""
    source = value if isinstance(value, dict) else {}
    result: dict[str, dict] = {}
    for platform in PUBLISH_PLATFORMS:
        raw = source.get(platform) if isinstance(source.get(platform), dict) else {}
        if platform == "channels" and normalize_video_type(video_type) == "panorama":
            result[platform] = {
                "status": "unsupported",
                "note": "视频号暂不支持全景视频",
                "updated_at": "",
            }
            continue
        if platform == "channels" and normalize_video_type(video_type) == "normal" and raw.get("status") == "unsupported":
            raw = {}
        # Migrate records created before per-platform states existed, and
        # repair the first migration where the legacy published flag was
        # copied after the platform object had already been initialized.
        migrated_status = raw.get("status")
        if platform == "douyin" and migrated_status in {None, "", "pending"} and previous:
            if previous.get("status") == "published" or "已提交抖音" in str(previous.get("note") or ""):
                migrated_status = "published"
        result[platform] = {
            "status": str(migrated_status or ("published" if previous and previous.get("status") == "published" and platform == "douyin" else "pending")),
            "note": str(raw.get("note") or ""),
            "updated_at": str(raw.get("updated_at") or ""),
        }
    return result


def normalize_official_topic_keywords(value: object) -> str:
    """Keep an optional official-activity hint bounded and text-only."""
    return str(value or "").strip()[:500]


def normalize_queue_text(value: object, limit: int) -> str:
    """Normalize optional queue metadata without allowing unbounded text."""
    return str(value or "").strip()[:limit]


def validate_schedule_future(mode: str, schedule_at: str) -> None:
    """Reject an appointment that is already in the past.

    The platform remains the source of truth for its minimum lead-time rule,
    but a local appointment must at least be later than the current local
    minute.  Keeping this separate from ``validate_schedule_fields`` lets the
    normalizer remain useful for loading and displaying older queue records.
    """
    if mode != "scheduled" or not schedule_at:
        return
    parsed = datetime.fromisoformat(schedule_at)
    current_minute = datetime.now().replace(second=0, microsecond=0)
    if parsed <= current_minute:
        raise ValueError("定时发布时间必须晚于当前时间")


def validate_queue_schedule(items: list[dict]) -> str | None:
    """Return a user-facing error for the first invalid scheduled queue item."""
    for item in items:
        mode = item.get("schedule_mode", "immediate")
        schedule_at = item.get("schedule_at", "")
        interval = item.get("schedule_interval_minutes", "")
        try:
            normalized_mode, normalized_at, normalized_interval = validate_schedule_fields(
                mode, schedule_at, interval
            )
        except ValueError as exc:
            return f"{item.get('name') or item.get('path') or '队列视频'}：{exc}"
        try:
            validate_schedule_future(normalized_mode, normalized_at)
        except ValueError as exc:
            return f"{item.get('name') or item.get('path') or '队列视频'}：{exc}"
        # Keep the check deliberately format-focused.  The platform may have
        # its own minimum lead time, and the actual page is the source of
        # truth for that rule; the publisher rechecks it immediately before
        # clicking the platform's submit button.
        if normalized_mode == "scheduled" and not normalized_at:
            return f"{item.get('name') or item.get('path') or '队列视频'}：缺少定时发布时间"
        if normalized_mode not in SCHEDULE_MODES or normalized_interval is None:
            return f"{item.get('name') or item.get('path') or '队列视频'}：定时配置无效"
    return None


def validate_queue_panorama(items: list[dict]) -> str | None:
    """Validate explicit panoramic items before a browser task is launched.

    The local check only blocks conditions that the current panoramic page
    advertises as hard limits.  A 2:1 frame remains an advisory clue; the
    user must still choose a format and the page must complete its own
    projection/preview preflight.
    """
    for item in items:
        try:
            video_type = normalize_video_type(item.get("video_type", "normal"))
            panorama_format = normalize_panorama_format(item.get("panorama_format", ""))
        except ValueError as exc:
            return f"{item.get('name') or item.get('path') or '队列视频'}：{exc}"
        if video_type != "panorama":
            continue
        name = item.get("name") or item.get("path") or "队列视频"
        if not panorama_format:
            return f"{name}：请选择全景视频格式（360/180，单目/双目）"
        path = safe_video_path(str(item.get("path", "")))
        if path is None:
            return f"{name}：源视频不存在或路径无效"
        profile = detect_panorama_profile(path)
        blocking = profile.get("blocking") if isinstance(profile.get("blocking"), list) else []
        if blocking:
            return f"{name}：{'；'.join(str(value) for value in blocking)}"
    return None


def load_queue() -> list[dict]:
    if not QUEUE_FILE.exists():
        return []
    try:
        data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_queue(items: list[dict]) -> None:
    """Atomically persist the local queue without colliding concurrent PATCHes.

    The UI autosaves caption/topic edits while the publisher and polling
    endpoints may write the same file.  A shared ``queue.tmp`` allowed one
    request to replace another request's temporary file, producing a
    historical ``FileNotFoundError`` and an apparently stale list.  Use a
    process/thread-unique temporary path plus a short in-process lock; the
    final replace remains atomic for readers.
    """
    if not STATE_DIR.exists():
        try:
            _ensure_state_dir()
        except PermissionError:
            pass
    tmp = QUEUE_FILE.with_name(
        f".{QUEUE_FILE.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    with _QUEUE_SAVE_LOCK:
        try:
            tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(QUEUE_FILE)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass


def _default_analysis_state() -> dict:
    return {
        "running": False,
        "status": "idle",
        "total": 0,
        "completed": 0,
        "current": "",
        "message": "尚未开始批量识别",
        "results": {},
        "errors": [],
        "started_at": "",
        "finished_at": "",
    }


def load_analysis_state() -> dict:
    """Load the read-only batch-analysis progress snapshot.

    Analysis runs in a daemon thread owned by this local server.  If the
    server was restarted while a previous snapshot still said ``running``,
    mark it interrupted instead of leaving the UI permanently locked.
    """
    default = _default_analysis_state()
    if not ANALYSIS_STATE_FILE.exists():
        return default
    try:
        raw = json.loads(ANALYSIS_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    if not isinstance(raw, dict):
        return default
    state = {**default, **raw}
    if not isinstance(state.get("results"), dict):
        state["results"] = {}
    if not isinstance(state.get("errors"), list):
        state["errors"] = []
    # A process-local worker is the source of truth for ``running``.  Do not
    # trust a stale flag left by a previous server process.
    worker = _ANALYSIS_THREAD
    if state.get("running") and (worker is None or not worker.is_alive()):
        state.update({
            "running": False,
            "status": "interrupted",
            "message": "批量识别进程已中断，可重新开始未完成的识别",
            "current": "",
            "finished_at": now(),
        })
        try:
            _save_analysis_state(state)
        except OSError:
            pass
    return state


def analysis_state_payload(*, include_results: bool = True) -> dict:
    """Return an API-safe analysis snapshot.

    Raw Vision/SoundAnalysis evidence can be large for a queue of many
    videos.  Progress polling therefore uses a compact summary, while the
    browser requests full results once a batch reaches a terminal state.
    """
    state = load_analysis_state()
    if include_results:
        return state
    compact = dict(state)
    results = state.get("results") if isinstance(state.get("results"), dict) else {}
    compact["results"] = {
        str(item_id): {
            key: value
            for key, value in result.items()
            if key in {"status", "name", "error", "analyzed_at"}
        }
        for item_id, result in results.items()
        if isinstance(result, dict)
    }
    compact["result_count"] = len(results)
    return compact


def _save_analysis_state(state: dict) -> None:
    _ensure_state_dir()
    tmp = ANALYSIS_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(ANALYSIS_STATE_FILE)


def save_analysis_state(state: dict) -> None:
    """Atomically save a batch-analysis snapshot under a process lock."""
    with _ANALYSIS_STATE_LOCK:
        _save_analysis_state(state)


def request_analysis_cancel() -> None:
    _ensure_state_dir()
    tmp = ANALYSIS_CANCEL_FILE.with_suffix(".tmp")
    tmp.write_text(now(), encoding="utf-8")
    tmp.replace(ANALYSIS_CANCEL_FILE)


def clear_analysis_cancel() -> None:
    try:
        ANALYSIS_CANCEL_FILE.unlink()
    except FileNotFoundError:
        pass


def analysis_cancel_requested() -> bool:
    return ANALYSIS_CANCEL_FILE.exists()


def _analysis_item_key(item: dict) -> str:
    return str(item.get("id") or item.get("path") or "")


def _analysis_worker(ids: list[str], token: str) -> None:
    """Analyze queue items one at a time and persist progress/results.

    This deliberately calls the shared ``analyze_item`` function rather than
    embedding an analysis strategy here.  A future frame/audio/model-backed
    implementation can replace that function without changing the batch
    control plane or the browser publisher.
    """
    global _ANALYSIS_THREAD
    try:
        state = load_analysis_state()
        items_by_key = {_analysis_item_key(item): item for item in load_queue()}
        for index, item_id in enumerate(ids):
            if analysis_cancel_requested():
                state.update({
                    "running": False,
                    "status": "cancelled",
                    "message": f"已停止批量识别（已完成 {state.get('completed', 0)}/{state.get('total', len(ids))} 条）",
                    "current": "",
                    "finished_at": now(),
                })
                save_analysis_state(state)
                break
            item = items_by_key.get(str(item_id))
            if item is None:
                state.setdefault("errors", []).append({
                    "id": str(item_id),
                    "name": str(item_id),
                    "error": "队列中已找不到这条视频",
                })
                state["completed"] = int(state.get("completed", 0)) + 1
                continue
            name = str(item.get("name") or item.get("path") or item_id)
            state["current"] = name
            state["message"] = f"正在识别：{name}"
            save_analysis_state(state)
            try:
                video_path = safe_video_path(str(item.get("path", "")))
                if video_path is None:
                    raise ValueError("视频文件不存在或路径无效")
                result = analyze_item(item, video_path)
                state.setdefault("results", {})[str(item_id)] = {
                    "status": "completed",
                    "name": name,
                    "analysis": result,
                    "analyzed_at": now(),
                }
            except Exception as exc:  # keep the remaining queue items moving
                state.setdefault("errors", []).append({
                    "id": str(item_id),
                    "name": name,
                    "error": str(exc) or exc.__class__.__name__,
                })
                state.setdefault("results", {})[str(item_id)] = {
                    "status": "failed",
                    "name": name,
                    "error": str(exc) or exc.__class__.__name__,
                }
            state["completed"] = int(state.get("completed", 0)) + 1
            save_analysis_state(state)
        else:
            errors = state.get("errors") or []
            state.update({
                "running": False,
                "status": "completed" if not errors else "completed_with_errors",
                "message": (
                    f"批量识别完成：{state.get('completed', 0)}/{state.get('total', len(ids))} 条"
                    + (f"，{len(errors)} 条失败待重试" if errors else "")
                ),
                "current": "",
                "finished_at": now(),
            })
            save_analysis_state(state)
    except Exception as exc:
        # Keep a diagnosable terminal snapshot even if the worker itself
        # encounters an unexpected error outside an individual item.
        try:
            state = load_analysis_state()
            state.update({
                "running": False,
                "status": "failed",
                "message": f"批量识别任务异常：{exc}",
                "current": "",
                "finished_at": now(),
            })
            save_analysis_state(state)
        except Exception:
            pass
    finally:
        clear_analysis_cancel()
        if _ANALYSIS_THREAD is threading.current_thread():
            _ANALYSIS_THREAD = None


def start_analysis_batch(ids: list[str] | None = None) -> tuple[dict, int]:
    """Start a non-mutating background analysis batch."""
    global _ANALYSIS_THREAD
    with _ANALYSIS_STATE_LOCK:
        existing = load_analysis_state()
        if existing.get("running") or (_ANALYSIS_THREAD is not None and _ANALYSIS_THREAD.is_alive()):
            return {"error": "批量识别已经在运行", "state": existing}, HTTPStatus.CONFLICT
        items = load_queue()
        by_key = {_analysis_item_key(item): item for item in items}
        requested = [str(value) for value in (ids or []) if str(value)]
        if requested:
            target_ids = list(dict.fromkeys(key for key in requested if key in by_key))
            if not target_ids:
                return {"error": "没有找到可识别的视频队列项"}, HTTPStatus.NOT_FOUND
        else:
            # Default to all non-skipped items so the user does not need to
            # open every card.  Explicit selection can narrow this list later.
            target_ids = [_analysis_item_key(item) for item in items if item.get("status") != "skipped"]
        if not target_ids:
            return {"error": "当前没有可识别的视频"}, HTTPStatus.BAD_REQUEST
        if len(target_ids) > ANALYSIS_MAX_BATCH:
            return {"error": f"一次最多识别 {ANALYSIS_MAX_BATCH} 条视频"}, HTTPStatus.BAD_REQUEST
        clear_analysis_cancel()
        token = uuid.uuid4().hex
        state = {
            **_default_analysis_state(),
            "running": True,
            "status": "running",
            "total": len(target_ids),
            "message": f"正在准备识别 {len(target_ids)} 条视频",
            "batch_token": token,
            "item_ids": target_ids,
            "started_at": now(),
        }
        # New batch results must not be mixed with a previous queue selection.
        save_analysis_state(state)
        worker = threading.Thread(
            target=_analysis_worker,
            args=(target_ids, token),
            name="douyin-analysis-batch",
            daemon=True,
        )
        _ANALYSIS_THREAD = worker
        worker.start()
        return {"ok": True, "state": state}, HTTPStatus.ACCEPTED


def cancel_analysis_batch() -> tuple[dict, int]:
    state = load_analysis_state()
    if not state.get("running"):
        return {"ok": True, "state": state}, HTTPStatus.OK
    request_analysis_cancel()
    state["status"] = "cancelling"
    state["message"] = "正在停止批量识别；当前视频完成后停止"
    save_analysis_state(state)
    return {"ok": True, "state": state}, HTTPStatus.ACCEPTED


def load_publish_state() -> dict:
    if not PUBLISH_STATE_FILE.exists():
        return {
            "running": False,
            "paused": False,
            "pause_requested": False,
            "stop_requested": False,
            "stop_supported": False,
            "terminated": False,
            "auto_paused": False,
            "pause_source": "",
            "pause_reason": "",
            "pause_kind": "",
            "message": "尚未启动自动发布",
        }
    try:
        data = json.loads(PUBLISH_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {
                "running": False,
                "paused": False,
                "pause_requested": False,
                "stop_requested": False,
                "stop_supported": False,
                "terminated": False,
                "auto_paused": False,
                "pause_source": "",
                "pause_reason": "",
                "pause_kind": "",
            }

        # 新增的暂停字段对旧版状态文件保持向后兼容。暂停请求另外落盘
        # 到标记文件，避免发布子进程正在写进度时覆盖掉网页发出的请求。
        data["paused"] = bool(data.get("paused", False))
        # 暂停标记文件是控制面的权威来源；JSON 中的字段只是给前端
        # 展示的快照，避免“继续”删除标记后仍被旧快照卡住。
        data["pause_requested"] = bool(PAUSE_REQUEST_FILE.exists() and data.get("running"))
        # 终止请求与暂停请求一样使用独立标记文件，避免网页请求和发布器
        # 的进度写入互相覆盖。只有仍在运行的发布进程才显示该标记。
        data["stop_requested"] = bool(STOP_REQUEST_FILE.exists() and data.get("running"))
        # 保留 stop_supported 字段供旧版页面兼容，但强制终止由服务端
        # 直接管理发布器 PID，不再以这个字段作为终止门槛。
        data["stop_supported"] = bool(data.get("stop_supported", False))
        data["terminated"] = bool(data.get("terminated", False))
        data["auto_paused"] = bool(data.get("auto_paused", False))
        data["pause_source"] = str(data.get("pause_source") or "")
        data["pause_reason"] = str(data.get("pause_reason") or "")
        data["pause_kind"] = str(data.get("pause_kind") or "")
        if data["auto_paused"] and not data["pause_source"]:
            # 兼容只写了 auto_paused 的中间版本。
            data["pause_source"] = "automatic"
        # 强制终止是一个已经结束的终态。旧版本可能在终止前先写入了
        # paused=true，不能让这份残留快照继续锁住清空/编辑队列。
        if data["terminated"] and not data.get("running"):
            data["paused"] = False
            data["pause_requested"] = False
            data["stop_requested"] = False

        # 发布器是独立子进程。若服务重启或子进程异常退出，旧状态不能
        # 永远卡在“运行中”，否则前端将无法再次启动任务。
        user_stop_in_flight = False
        user_pause_in_flight = False
        publisher_pid = data.get("publisher_pid")
        if data.get("running") and publisher_pid is not None:
            try:
                pid = int(publisher_pid)
            except (TypeError, ValueError):
                pid = 0
            process_exited = pid <= 0
            if not process_exited:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    process_exited = True
                except PermissionError:
                    # 进程存在但当前用户没有检查权限，不能误判为已退出。
                    process_exited = False
                except OSError:
                    process_exited = False
            if process_exited:
                user_stop_in_flight = bool(data.get("stop_requested") or STOP_REQUEST_FILE.exists())
                user_pause_in_flight = bool(data.get("pause_requested") or PAUSE_REQUEST_FILE.exists() or data.get("paused"))
                data["running"] = False
                if user_stop_in_flight:
                    # 用户已经明确点击终止；发布器来不及写最终快照时，
                    # 也不能把这次人为操作误报成自动暂停。
                    data["action_required"] = False
                    data["message"] = "自动发布已终止；浏览器页面保持打开"
                    data["auto_paused"] = False
                    data["pause_source"] = "manual"
                    data["pause_reason"] = "用户终止自动发布"
                    data["pause_kind"] = "manual"
                    data.pop("pause_event_id", None)
                    data.pop("paused_at", None)
                    data["terminated"] = True
                    data["paused"] = False
                elif user_pause_in_flight:
                    # 手动暂停期间发布器异常退出时保留“暂停”语义，
                    # 继续按钮仍可按队列状态恢复，但不弹异常提醒。
                    data["action_required"] = False
                    data["message"] = "已暂停：发布器进程已结束，浏览器页面保持打开"
                    data["auto_paused"] = False
                    data["pause_source"] = "manual"
                    data["pause_reason"] = "用户手动暂停"
                    data["pause_kind"] = "manual"
                    data.pop("pause_event_id", None)
                    data.pop("paused_at", None)
                    data["terminated"] = False
                    data["paused"] = True
                else:
                    data["action_required"] = True
                    reason = "上次自动发布进程已结束，请检查浏览器页面后继续处理"
                    data["message"] = f"自动暂停：{reason}"
                    data["auto_paused"] = True
                    data["pause_source"] = "automatic"
                    data["pause_reason"] = reason
                    data["pause_kind"] = "process_error"
                    data["pause_event_id"] = uuid.uuid4().hex
                    data["paused_at"] = now()
                    data["terminated"] = False
                    data["paused"] = False
                data["pause_requested"] = False
                data["stop_requested"] = False
                data["stop_supported"] = False
                data.pop("publisher_pid", None)
                try:
                    PAUSE_REQUEST_FILE.unlink()
                except FileNotFoundError:
                    pass
                try:
                    save_publish_state(data)
                except OSError:
                    # 状态读取仍应成功；下次读取会再次尝试修复。
                    pass
        elif data.get("publisher_pid") is not None:
            # 已结束的任务不再需要 PID；清理旧版本留下的元数据，避免
            # 页面或诊断信息看起来像仍关联着一个运行中的进程。
            data.pop("publisher_pid", None)
            try:
                save_publish_state(data)
            except OSError:
                pass
        if not data.get("running") and PAUSE_REQUEST_FILE.exists():
            # 进程已经结束时，遗留的暂停标记不能影响下一次新任务。
            try:
                PAUSE_REQUEST_FILE.unlink()
            except OSError:
                pass
            data["pause_requested"] = False
            if not user_pause_in_flight:
                data["paused"] = False
        if not data.get("running") and STOP_REQUEST_FILE.exists():
            # 发布进程已经结束后，遗留的终止标记不能影响下一次新任务。
            try:
                STOP_REQUEST_FILE.unlink()
            except OSError:
                pass
            data["stop_requested"] = False
        return data
    except (OSError, json.JSONDecodeError):
        return {
            "running": False,
            "paused": False,
            "pause_requested": False,
            "stop_requested": False,
            "stop_supported": False,
            "terminated": False,
            "auto_paused": False,
            "pause_source": "",
            "pause_reason": "",
            "pause_kind": "",
            "message": "发布状态文件无法读取",
        }


def load_channels_state() -> dict:
    default = {"running": False, "status": "idle", "item_id": "", "message": "尚未启动视频号发布", "pid": None, "completed": 0, "total": 0, "current": ""}
    try:
        data = json.loads(CHANNELS_STATE_FILE.read_text(encoding="utf-8"))
        state = {**default, **data} if isinstance(data, dict) else default
        pid = int(state.get("pid") or 0)
        if state.get("running") and pid:
            try:
                os.kill(pid, 0)
                process_state = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, check=False).stdout.strip()
                if not process_state or process_state.startswith("Z"):
                    raise ProcessLookupError
            except (ProcessLookupError, OSError):
                state = {**default, "status": "idle", "message": "上次视频号发布已退出，可重新启动"}
                try:
                    save_channels_state(state)
                except OSError:
                    pass
        return state
    except (OSError, json.JSONDecodeError):
        return default


def save_channels_state(state: dict) -> None:
    CHANNELS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = CHANNELS_STATE_FILE.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(CHANNELS_STATE_FILE)


def start_channels_publish_queue(item_ids: list[str] | None = None) -> tuple[dict, int]:
    """Start Video Account publishing from the shared Douyin queue."""
    current = load_channels_state()
    if current.get('running'):
        return {"error": "已有视频号发布任务正在进行"}, HTTPStatus.CONFLICT
    if current.get('paused') and current.get('pid') and _channel_pid_alive(int(current.get('pid') or 0)):
        return {
            "error": "上次视频号任务失败/暂停且浏览器仍保留，请先点“终止视频号”再重新开始",
            "state": current,
        }, HTTPStatus.CONFLICT
    items = load_queue()
    requested = [str(value) for value in (item_ids or []) if str(value).strip()]
    requested_set = set(requested)
    eligible: list[dict] = []
    skipped: list[dict] = []
    for item in items:
        item_id = str(item.get("id") or item.get("path") or "")
        if requested_set and item_id not in requested_set:
            continue
        video_type = normalize_video_type(item.get("video_type", "normal"))
        item["platforms"] = normalize_platform_states(item.get("platforms"), item, video_type)
        if video_type == "panorama":
            item["platforms"]["channels"] = {
                "status": "unsupported",
                "note": "视频号暂不支持全景视频",
                "updated_at": now(),
            }
            skipped.append({"id": item_id, "name": item.get("name", ""), "reason": "视频号暂不支持全景视频"})
            continue
        channel_status = str(item["platforms"]["channels"].get("status") or "pending")
        if channel_status in {"pending", "failed", "publishing"}:
            eligible.append(item)
    if skipped:
        save_queue(items)
    if not eligible:
        return {"error": "当前选择中没有可发布到视频号的普通视频", "skipped": skipped}, HTTPStatus.BAD_REQUEST
    env = {k: v for k, v in os.environ.items() if k not in ('NODE_OPTIONS', 'NODE_PATH')}
    env['PYTHONUNBUFFERED'] = '1'
    python_bin = ROOT / '.venv' / 'bin' / 'python'
    if not python_bin.is_file():
        python_bin = Path(sys.executable)
    CHANNELS_IDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_ids = CHANNELS_IDS_FILE.with_suffix('.tmp')
    tmp_ids.write_text(json.dumps([str(item.get("id") or item.get("path") or "") for item in eligible], ensure_ascii=False), encoding="utf-8")
    tmp_ids.replace(CHANNELS_IDS_FILE)
    log_path = STATE_DIR / 'channels_publisher.log'
    log = log_path.open('ab')
    process = subprocess.Popen(
        [str(python_bin), str(ROOT / 'channels_publisher.py'), '--shared-queue', '--ids-file', str(CHANNELS_IDS_FILE)],
        cwd=str(ROOT), env=env, stdout=log, stderr=log, start_new_session=True,
    )
    log.close()
    state = {
        "running": True, "status": "starting", "mode": "shared_queue",
        "item_id": str(eligible[0].get('id') or ''),
        "message": f"正在启动视频号发布（{len(eligible)} 条）", "pid": process.pid,
        "completed": 0, "total": len(eligible), "current": "", "skipped": skipped,
    }
    save_channels_state(state)
    return {"ok": True, "state": state, "skipped": skipped, "count": len(eligible)}, HTTPStatus.ACCEPTED


def start_channels_publish(item: dict) -> tuple[dict, int]:
    """Compatibility wrapper for the per-card Video Account button."""
    item_id = str(item.get("id") or item.get("path") or "")
    return start_channels_publish_queue([item_id])


def _channels_ui_alive() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", CHANNELS_UI_PORT), timeout=0.4):
            return True
    except OSError:
        return False


def open_channels_ui() -> tuple[dict, int]:
    """Start the independent Channels page if needed and return its URL."""
    if not _channels_ui_alive():
        python_bin = ROOT / ".venv" / "bin" / "python"
        if not python_bin.is_file():
            python_bin = Path(sys.executable)
        log_path = ROOT / ".channels_batch" / "channels_ui.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k not in ("NODE_OPTIONS", "NODE_PATH")}
        env["PYTHONUNBUFFERED"] = "1"
        try:
            with log_path.open("ab") as log:
                subprocess.Popen(
                    [str(python_bin), str(ROOT / "channels_app.py")],
                    cwd=str(ROOT), env=env, stdout=log, stderr=log,
                    start_new_session=True,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            return {"error": f"视频号页面启动失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if _channels_ui_alive():
                break
            time.sleep(0.2)
    if not _channels_ui_alive():
        return {"error": "视频号页面未能在 8786 端口启动，请查看 .channels_batch/channels_ui.log"}, HTTPStatus.INTERNAL_SERVER_ERROR
    return {"ok": True, "url": CHANNELS_UI_URL}, HTTPStatus.OK


def _channels_profile_chrome_pids() -> list[int]:
    profile = str(CHANNELS_PROFILE_DIR)
    try:
        output = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids: list[int] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_text, _, command = line.partition(" ")
        if f"--user-data-dir={profile}" not in command:
            continue
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid != os.getpid():
            pids.append(pid)
    return pids


def _stop_channels_profile_chrome() -> None:
    pids = _channels_profile_chrome_pids()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not any(_channel_pid_alive(pid) for pid in pids):
            break
        time.sleep(0.2)
    for pid in pids:
        if _channel_pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        try:
            (CHANNELS_PROFILE_DIR / name).unlink()
        except OSError:
            pass


def _channel_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def channels_publish_control(action: str) -> tuple[dict, int]:
    """Pause/resume/terminate the shared Video Account publisher."""
    state = load_channels_state()
    running = bool(state.get("running"))
    if action == "pause":
        if not running:
            return {"error": "当前没有正在运行的视频号发布任务"}, HTTPStatus.CONFLICT
        if state.get("paused"):
            return {"ok": True, "state": state}
        state.update({
            "pause_requested": True,
            "message": "已请求暂停：当前视频完成后暂停，浏览器页面保持打开",
        })
        save_channels_state(state)
        return {"ok": True, "state": state}, HTTPStatus.OK
    if action == "resume":
        if not running:
            return {"error": "当前没有正在运行的视频号发布任务"}, HTTPStatus.CONFLICT
        state.update({
            "paused": False,
            "pause_requested": False,
            "message": "继续视频号发布",
        })
        save_channels_state(state)
        return {"ok": True, "state": state}, HTTPStatus.OK
    if action == "terminate":
        state.update({"stop_requested": True, "message": "正在终止视频号发布"})
        save_channels_state(state)
        pid = int(state.get("pid") or 0)
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if not _channel_pid_alive(pid):
                    break
                time.sleep(0.2)
            if _channel_pid_alive(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        _stop_channels_profile_chrome()
        current = str(state.get("current") or "")
        if current:
            items = load_queue()
            changed = False
            for item in items:
                if str(item.get("name") or "") == current:
                    video_type = normalize_video_type(item.get("video_type", "normal"))
                    item["platforms"] = normalize_platform_states(item.get("platforms"), item, video_type)
                    if str(item["platforms"]["channels"].get("status") or "") in {"publishing", "pending", "failed"}:
                        item["platforms"]["channels"] = {
                            "status": "failed",
                            "note": "视频号发布已终止，可重新发布",
                            "updated_at": now(),
                        }
                        changed = True
                    break
            if changed:
                save_queue(items)
        state.update({
            "running": False,
            "paused": False,
            "status": "terminated",
            "pause_requested": False,
            "stop_requested": False,
            "terminated": True,
            "current": "",
            "message": "视频号发布已终止；可重新启动",
        })
        state.pop("pid", None)
        save_channels_state(state)
        return {"ok": True, "forced": True, "state": state}, HTTPStatus.OK
    return {"error": "未知的视频号控制操作"}, HTTPStatus.BAD_REQUEST


def save_publish_state(state: dict) -> None:
    _ensure_state_dir()
    tmp = PUBLISH_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PUBLISH_STATE_FILE)


def request_publish_pause() -> None:
    """以独立标记记录暂停请求，避免与发布进度写入产生丢失更新。"""
    _ensure_state_dir()
    tmp = PAUSE_REQUEST_FILE.with_suffix(".tmp")
    tmp.write_text(now(), encoding="utf-8")
    tmp.replace(PAUSE_REQUEST_FILE)


def clear_publish_pause() -> None:
    """清除暂停请求；发布器会在下一个安全检查点继续。"""
    try:
        PAUSE_REQUEST_FILE.unlink()
    except FileNotFoundError:
        pass


def request_publish_stop() -> None:
    """记录安全终止请求；发布器在下一个检查点保留页面后退出。"""
    _ensure_state_dir()
    tmp = STOP_REQUEST_FILE.with_suffix(".tmp")
    tmp.write_text(now(), encoding="utf-8")
    tmp.replace(STOP_REQUEST_FILE)


def clear_publish_stop() -> None:
    """清除终止请求，供任务结束和下一次新任务使用。"""
    try:
        STOP_REQUEST_FILE.unlink()
    except FileNotFoundError:
        pass


def record_publisher_pid(process: subprocess.Popen) -> None:
    """把发布子进程 PID 写入状态，但不覆盖已结束进程的最终状态。"""
    state = load_publish_state()
    if not state.get("running"):
        return
    state["publisher_pid"] = process.pid
    save_publish_state(state)


def reap_background_process(process: subprocess.Popen) -> None:
    """在后台回收发布子进程，避免任务结束后留下僵尸 PID。

    发布器需要独立于 HTTP 请求运行，主线程不能同步 ``wait``；但如果
    完全不回收，macOS 会把已完成的发布器保留为 ``<defunct>``，后续
    状态检查和强制终止判断都会变得不可靠。单次 daemon 线程只负责
    ``wait``，不修改发布状态文件。
    """
    def _wait() -> None:
        try:
            process.wait()
        except (OSError, subprocess.SubprocessError):
            pass

    threading.Thread(
        target=_wait,
        name=f"reap-publisher-{process.pid}",
        daemon=True,
    ).start()


def _read_process_command(pid: int) -> str | None:
    """读取指定 PID 的完整命令行；失败时返回 None。

    终止操作不能只相信状态文件中的数字 PID，因为进程结束后操作系统
    可能很快把同一个 PID 分配给别的程序。每次发信号前都重新读取命令行，
    并由 ``_is_publisher_command`` 做项目脚本校验。
    """
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return lines[0] if lines else None


def _is_publisher_command(command: str | None) -> bool:
    """确认命令行确实运行本项目的 ``browser_publisher.py``。

    只接受“Python 解释器直接执行本项目脚本”的 argv 形态，避免仅因
    其他参数/字符串里出现脚本路径就误杀同名脚本、浏览器进程或 PID
    已复用后的其他用户程序。当前启动器使用的命令形态是：
    ``<python> <绝对路径>/browser_publisher.py [参数]``。
    """
    if not command:
        return False
    expected = PUBLISHER_SCRIPT.resolve()
    try:
        tokens = shlex.split(command)
    except ValueError:
        # 命令行极少数情况下包含未闭合引号；退回到空白切分仍需通过
        # 后面的绝对路径比较，不会因此放宽到只比较文件名。
        tokens = command.split()
    tokens = [token.strip().strip("'\"") for token in tokens if token.strip()]
    if len(tokens) < 2:
        return False

    # app.py 通过 subprocess.Popen 直接启动 Python，不经过 shell/env 包装；
    # 因此第一个 argv 必须是 Python 解释器。允许 macOS 的 ``Python`` 大写
    # 可执行文件名，以及常见的 python3.x 版本名。
    executable_name = Path(tokens[0]).name.lower()
    if not (executable_name == "python" or executable_name.startswith("python3")):
        return False

    # 只允许解释器的简单启动参数出现在脚本前；禁止 ``-c``/``-m``，也不
    # 遍历后续参数寻找脚本路径，否则 ``python -c ... <脚本路径>`` 会被误判。
    script_index = 1
    while script_index < len(tokens) and tokens[script_index].startswith("-"):
        option = tokens[script_index]
        if option in {"-c", "--command", "-m", "--module"}:
            return False
        # 当前启动器不会传带值的解释器选项；遇到不认识的选项直接拒绝，
        # 让未来若改变启动命令时必须显式更新这段安全校验。
        if option not in {"-u", "-B", "-E", "-s", "-S", "-I", "-O", "-OO"}:
            return False
        script_index += 1
    if script_index >= len(tokens):
        return False
    script_token = tokens[script_index]
    try:
        return Path(script_token).expanduser().resolve() == expected
    except (OSError, RuntimeError, ValueError):
        return False


def _publisher_alive(pid: int) -> bool:
    """判断发布器是否仍在运行，不发送业务信号。

    macOS/Linux 上子进程收到 SIGINT 后、被父进程 ``wait`` 回收前，短暂
    可能呈现为 zombie；它已经不会继续执行发布动作，应按“已退出”处理，
    否则强制终止流程会把 zombie 误判为仍存活并返回失败。
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 进程存在但当前用户不能探查，不能把它当成已退出。
        return True
    except OSError:
        return False
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        status = result.stdout.strip()
        if result.returncode != 0 or not status:
            return False
        if status.startswith("Z"):
            return False
    except (OSError, subprocess.SubprocessError):
        # kill(pid, 0) 已确认 PID 存在；状态读取失败时保守地视为存活，
        # 避免把未知进程误报为已终止。
        return True
    return True


def _wait_publisher_exit(pid: int, timeout: float) -> bool:
    """等待目标发布器退出；命令消失或 PID 不再存活都视为完成。"""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        command = _read_process_command(pid)
        if not _publisher_alive(pid):
            return True
        # PID 仍存活但命令暂时无法读取/已变成其他程序时，不能把它误报为
        # 已终止；调用方会按失败处理并保留状态，避免覆盖未知进程。
        if command is None or not _is_publisher_command(command):
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.1, remaining))


def _signal_publisher(pid: int, signum: signal.Signals) -> tuple[bool, str]:
    """向已校验的发布器发送一个信号。

    返回 ``(True, ...)`` 表示信号已发送或目标已经自行退出；返回 False
    表示 PID 命令行校验失败/权限不足，此时绝不继续尝试更强的信号。
    """
    command = _read_process_command(pid)
    if command is None:
        if not _publisher_alive(pid):
            return True, "目标进程已经退出"
        return False, "无法确认发布进程命令行，未发送终止信号"
    if not _is_publisher_command(command):
        return False, "状态中的 PID 已不再运行本项目发布器，未发送终止信号"
    try:
        os.kill(pid, signum)
    except ProcessLookupError:
        return True, "目标进程在发送信号前已退出"
    except PermissionError:
        return False, "没有权限终止发布进程"
    except OSError as exc:
        return False, f"发送终止信号失败：{exc}"
    return True, f"已发送 {signum.name}"


def _mark_current_item_terminated(state: dict) -> None:
    """给当前条目留下可恢复的终止备注，不改变源文件和其他队列项。"""
    current = str(state.get("current") or "").strip()
    if not current:
        return
    items = load_queue()
    changed = False
    for item in items:
        values = {
            str(item.get("id") or ""),
            str(item.get("name") or ""),
            str(item.get("path") or ""),
            Path(str(item.get("path") or "")).name,
        }
        if current not in values:
            continue
        # 已提交的条目不回退；未完成条目保留 publishing 状态，下一次
        # “继续发布”可以按原有恢复逻辑接着处理。
        if item.get("status") not in {"published", "skipped"}:
            item["status"] = "publishing"
        item["note"] = "已强制终止自动发布，浏览器页面保持打开"
        item["updated_at"] = now()
        changed = True
        break
    if changed:
        try:
            save_queue(items)
        except OSError:
            # 状态收尾仍然优先；队列写失败会在页面中由状态提示，且不
            # 删除任何源视频。
            pass


def _has_current_publish_item(state: dict) -> bool:
    """判断状态是否仍保留可终止的当前视频现场。"""
    current = state.get("current")
    if isinstance(current, dict):
        return any(
            str(current.get(key) or "").strip()
            for key in ("id", "name", "path")
        )
    return bool(str(current or "").strip())


def _finalize_forced_stop(message: str) -> dict:
    """原子写入强制终止后的最终状态并清理控制标记。"""
    state = load_publish_state()
    _mark_current_item_terminated(state)
    state.update({
        "running": False,
        "paused": False,
        "pause_requested": False,
        "stop_requested": False,
        "stop_supported": False,
        "terminated": True,
        "terminated_at": now(),
        "message": message,
        "action_required": False,
        "auto_paused": False,
        "pause_source": "",
        "pause_reason": "",
        "pause_kind": "",
    })
    state.pop("pause_event_id", None)
    state.pop("paused_at", None)
    state.pop("publisher_pid", None)
    save_publish_state(state)
    clear_publish_pause()
    clear_publish_stop()
    # 再读一次，确保服务端返回的快照已经反映清理后的文件状态。
    return load_publish_state()


def force_terminate_publisher() -> tuple[dict, int]:
    """强制结束当前发布器，但只允许命中本项目的发布脚本 PID。

    顺序为 SIGINT -> SIGTERM -> SIGKILL。这里只终止发布器进程，不杀
    Chrome 进程，因此抖音页面/草稿会留在浏览器中供用户查看。队列和
    视频源文件均保留；当前未完成条目会留下 ``publishing`` 终止备注。
    """
    state = load_publish_state()
    if not state.get("running"):
        # 自动暂停/需要人工处理时，发布器可能已经退出，但浏览器现场和
        # 当前视频仍然保留。允许用户直接收尾终止；真正空闲/已完成状态
        # 仍然拒绝，避免误改历史结果。
        interrupted = (
            not state.get("terminated")
            and _has_current_publish_item(state)
            and bool(
                state.get("paused")
                or state.get("pause_requested")
                or state.get("auto_paused")
                or state.get("action_required")
            )
        )
        if interrupted:
            final = _finalize_forced_stop("自动发布已强制终止；浏览器页面保持打开")
            return {"ok": True, "forced": True, "state": final}, HTTPStatus.OK
        return {
            "error": "自动发布当前没有运行中的任务",
            "state": state,
        }, HTTPStatus.CONFLICT
    try:
        pid = int(state.get("publisher_pid"))
    except (TypeError, ValueError):
        pid = 0
    if pid <= 0 or pid == os.getpid():
        return {
            "error": "找不到可安全终止的发布进程，未修改当前任务",
            "state": state,
        }, HTTPStatus.CONFLICT

    command = _read_process_command(pid)
    if command is None:
        # 只有同时确认 PID 已经消失，才能把 ps 的空结果视为自然退出；
        # 若 PID 仍存活但暂时读不到命令行，必须拒绝发信号并保留状态。
        if not _publisher_alive(pid):
            final = _finalize_forced_stop("自动发布进程已结束，任务已终止；浏览器页面保持打开")
            return {"ok": True, "forced": True, "state": final}, HTTPStatus.OK
        return {
            "error": "暂时无法确认发布进程命令行，未发送终止信号",
            "state": state,
        }, HTTPStatus.CONFLICT
    if not _is_publisher_command(command):
        return {
            "error": "状态中的进程不是本项目发布器，未发送终止信号",
            "state": state,
        }, HTTPStatus.CONFLICT

    # 先落盘请求，便于仍在安全检查点的新版发布器自行收尾；随后由
    # 服务端继续发信号，旧版发布器也能在一次点击中结束。
    request_publish_stop()
    state = load_publish_state()
    state.update({
        "running": True,
        "stop_requested": True,
        "message": "正在强制终止自动发布，浏览器页面保持打开",
    })
    save_publish_state(state)

    attempts = (
        (signal.SIGINT, 0.8),
        (signal.SIGTERM, 1.5),
        (signal.SIGKILL, 1.5),
    )
    last_error = ""
    for signum, wait_seconds in attempts:
        ok, detail = _signal_publisher(pid, signum)
        last_error = detail
        if not ok:
            return {
                "error": detail,
                "state": load_publish_state(),
            }, HTTPStatus.CONFLICT
        if _wait_publisher_exit(pid, wait_seconds):
            final = _finalize_forced_stop("自动发布已强制终止；浏览器页面保持打开")
            return {"ok": True, "forced": True, "state": final}, HTTPStatus.OK

    # 即使三种信号都已发送，也不把“仍存活”误报为完成；保留终止标记，
    # 前端会继续显示处理中，用户可稍后再次点击。
    current = load_publish_state()
    current["message"] = f"强制终止尚未完成：{last_error or '发布进程仍在运行'}"
    current["stop_requested"] = True
    save_publish_state(current)
    return {
        "error": current["message"],
        "state": load_publish_state(),
    }, HTTPStatus.CONFLICT


def publish_control(action: str) -> tuple[dict, int]:
    """处理网页发来的暂停/继续请求，并返回状态与 HTTP 状态码。

    控制操作只写独立标记，不直接回写整份发布状态。这样发布器恰好
    完成最后一条视频时，网页的旧状态不会把已结束任务“复活”。发布器
    在下一个检查点看到标记后，再负责把 paused/message 写入状态文件。
    """
    state = load_publish_state()
    # 终止是独立的强制控制，即使发布器已因异常/人工处理退出，也要允许
    # 用户用当前页面上的“终止当前任务”完成收尾；其他操作仍要求运行中。
    if action == "stop":
        return force_terminate_publisher()
    if not state.get("running"):
        return {
            "error": "自动发布当前没有运行中的任务，请先点击开始发布",
            "state": state,
        }, HTTPStatus.CONFLICT

    if action == "pause":
        request_publish_pause()
        # 重新读取一次，确认发布器没有在请求落盘的瞬间结束；即使它
        # 随后结束，也只会返回一次短暂的成功，不会改变最终状态。
        current = load_publish_state()
        if not current.get("running"):
            clear_publish_pause()
            return {
                "error": "自动发布刚刚结束，未建立暂停状态",
                "state": current,
            }, HTTPStatus.CONFLICT
        current["message"] = (
            "自动发布已暂停，浏览器页面保持打开"
            if current.get("paused")
            else "已请求暂停，将在当前视频安全检查点停住"
        )
        return {"ok": True, "state": current}, HTTPStatus.OK
    elif action == "resume":
        if state.get("stop_requested"):
            return {
                "error": "当前任务正在终止，请等待浏览器页面保留完成",
                "state": state,
            }, HTTPStatus.CONFLICT
        clear_publish_pause()
        # 发布器会在最多约一秒的轮询周期内清除 paused 状态并继续；不
        # 直接回写 JSON，避免覆盖它刚写入的 current/completed 信息。
        current = load_publish_state()
        current["message"] = "已请求继续自动发布，正在恢复当前视频"
        return {"ok": True, "state": current}, HTTPStatus.OK
    else:
        return {"error": "未知的发布控制操作"}, HTTPStatus.BAD_REQUEST


def _queue_path_key(item: dict) -> str | None:
    """将队列中的相对/绝对路径归一化，便于选择文件时去重。

    旧版队列保存的是相对 MEDIA_ROOT 的路径；直接选择文件后可能保存为
    绝对路径（文件可以来自 A 盘、D 盘或多个目录）。两种表示都映射到
    同一个真实路径，避免重复加入同一文件。
    """
    raw = item.get("path")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        candidate = Path(unquote(raw)).expanduser()
        if not candidate.is_absolute():
            candidate = MEDIA_ROOT / candidate
        return str(candidate.resolve())
    except (OSError, RuntimeError, ValueError):
        return raw


def _queue_path_value(video_path: Path) -> str:
    """为新队列条目选择可移植的路径表示。

    当前媒体根目录下的文件继续使用相对路径，保持原有队列兼容；根目录
    之外的直接选择文件使用绝对路径，以支持来自不同磁盘/目录的文件。
    """
    try:
        root = MEDIA_ROOT.resolve()
        return video_path.resolve().relative_to(root).as_posix()
    except (ValueError, OSError, RuntimeError):
        return str(video_path.resolve())


def normalize_selection_profile(video_type: object = "normal", panorama_format: object = "") -> tuple[str, str]:
    """Normalize the workflow defaults used when new files enter the queue.

    The first workflow step chooses a publishing route.  It is deliberately a
    *default for newly discovered files*, not an implicit edit to existing
    queue records.  Panorama files must carry an explicit 180/360 format so a
    direct API caller cannot create an unpublishable item by bypassing the UI.
    """
    normalized_type = normalize_video_type(video_type)
    normalized_format = normalize_panorama_format(panorama_format)
    if normalized_type == "panorama" and not normalized_format:
        raise ValueError("选择全景视频后必须先选择 360/180 的单目或双目格式")
    return normalized_type, normalized_format if normalized_type == "panorama" else ""


def _queue_item_for_path(
    video_path: Path,
    previous: dict | None = None,
    *,
    path_value: str | None = None,
    default_video_type: str = "normal",
    default_panorama_format: str = "",
) -> dict:
    """根据视频路径生成队列条目，同时保留已有配置和发布状态。"""
    previous = dict(previous or {})
    path_value = path_value or _queue_path_value(video_path)
    title = video_path.stem.replace("_", " ").replace("-", " ").strip()
    previous_status = previous.get("status", "pending")
    if previous.get("note") == "浏览器自动发布完成":
        previous_status = "published"
    # An explicit existing queue record wins over the workflow default.  New
    # records inherit the type/format selected before the file picker opened.
    has_existing_type = "video_type" in previous
    try:
        stored_video_type = normalize_video_type(
            previous.get("video_type") if has_existing_type else default_video_type
        )
    except ValueError:
        stored_video_type = "normal"
    try:
        stored_panorama_format = normalize_panorama_format(
            previous.get("panorama_format") if "panorama_format" in previous else default_panorama_format
        )
    except ValueError:
        stored_panorama_format = ""
    return {
        **previous,
        "id": previous.get("id", path_value),
        "path": path_value,
        "name": video_path.name,
        # Old queue files without publishing-mode fields inherit the current
        # workflow default; records that already have a type keep it above.
        "video_type": stored_video_type,
        "panorama_format": stored_panorama_format if stored_video_type == "panorama" else "",
        "panorama_check_status": previous.get("panorama_check_status", "unknown"),
        # Panorama-only publish preferences.  Official-activity matching is
        # opt-out by default for panorama items; PICO is an explicit consent
        # and therefore remains unchecked unless the user enables it.
        "official_topic_auto": normalize_bool(previous.get("official_topic_auto", True), default=True),
        "official_topic_keywords": normalize_official_topic_keywords(previous.get("official_topic_keywords", "")),
        "pico_enabled": normalize_bool(previous.get("pico_enabled", False), default=False),
        "work_title": normalize_queue_text(previous.get("work_title", ""), 30),
        "caption": previous.get("caption", f"{title}｜演唱会现场 #演唱会 #现场"),
        "hashtags": previous.get("hashtags", "演唱会,现场"),
        "cover_tsp": previous.get("cover_tsp", ""),
        "cover_image": previous.get("cover_image", ""),
        "cover_image_name": previous.get("cover_image_name", ""),
        "cover_image_landscape": previous.get("cover_image_landscape", ""),
        "cover_image_landscape_name": previous.get("cover_image_landscape_name", ""),
        "cover_image_portrait": previous.get("cover_image_portrait", ""),
        "cover_image_portrait_name": previous.get("cover_image_portrait_name", ""),
        "collection_name": normalize_queue_text(previous.get("collection_name", ""), 120),
        "self_declaration": normalize_queue_text(previous.get("self_declaration", ""), 200),
        "location": normalize_queue_text(previous.get("location", ""), 120),
        "hot_topics": normalize_queue_text(previous.get("hot_topics", ""), 500),
        "visibility": previous.get("visibility", "全部可见"),
        "allow_download": previous.get("allow_download", True),
        "schedule_mode": previous.get("schedule_mode", "immediate"),
        "schedule_at": previous.get("schedule_at", ""),
        "schedule_interval_minutes": previous.get("schedule_interval_minutes", ""),
        "status": previous_status,
        "platforms": normalize_platform_states(previous.get("platforms"), previous, stored_video_type),
        "note": previous.get("note", ""),
        "updated_at": previous.get("updated_at", now()),
    }


def queue_mutation_locked(state: dict | None = None) -> bool:
    """Return whether a publish task currently owns the queue.

    The browser disables the same controls, but the server must enforce the
    rule too because an old tab or a direct request can otherwise change the
    queue while the publisher is reading it.
    """
    current = state if state is not None else load_publish_state()
    terminated = bool(current.get("terminated")) and not bool(current.get("running"))
    publish_locked = False if terminated else any(bool(current.get(key)) for key in (
        "running", "paused", "pause_requested", "stop_requested",
    ))
    # Batch recognition only writes its own analysis snapshot, but queue
    # edits are held until it finishes so a deleted/replaced item cannot make
    # a result appear on the wrong card.  The cancel endpoint remains
    # available because it does not call this helper.
    return publish_locked or bool(load_analysis_state().get("running"))


def update_bulk_preferences(payload: object) -> tuple[dict, int]:
    if not isinstance(payload, dict):
        return {"error": "请求数据必须是对象"}, HTTPStatus.BAD_REQUEST
    ids = payload.get("ids")
    field, value = payload.get("field"), payload.get("value")
    if not isinstance(ids, list) or not ids or not all(isinstance(x, str) and x for x in ids):
        return {"error": "请先勾选视频"}, HTTPStatus.BAD_REQUEST
    if field not in {"allow_download", "official_topic_auto"} or not isinstance(value, bool):
        return {"error": "批量设置字段或开关值无效"}, HTTPStatus.BAD_REQUEST
    with _QUEUE_SAVE_LOCK:
        if queue_mutation_locked() or load_channels_state().get("running"):
            return {"error": "发布或识别任务进行中，暂不能批量设置"}, HTTPStatus.CONFLICT
        items = load_queue()
        selected = [x for x in items if str(x.get("id") or x.get("path", "")) in set(ids)]
        if len(selected) != len(set(ids)):
            return {"error": "部分视频已不在队列，请刷新后重试"}, HTTPStatus.BAD_REQUEST
        targets = [x for x in selected if field != "official_topic_auto" or x.get("video_type") == "panorama"]
        if not targets:
            return {"error": "官方活动/话题匹配仅适用于全景视频，请勾选全景视频"}, HTTPStatus.BAD_REQUEST
        for item in targets:
            item[field] = value
            item["updated_at"] = now()
        save_queue(items)
    return {"ok": True, "updated": len(targets), "skipped": len(selected) - len(targets)}, HTTPStatus.OK


def reconcile_idle_publish_message(items: list[dict] | None = None) -> None:
    """让队列变更后发布提示与当前队列保持一致。

    发布完成后再选择/扫描新视频时，旧的“全部待发布视频已提交”提示
    不能继续覆盖新出现的待发布条目；否则用户会看到队列里有待处理视频，
    但状态栏仍像上一轮已经结束。只在任务确实空闲且没有人工/异常暂停
    语义时更新提示，不覆盖需要用户处理的现场。
    """
    current = load_publish_state()
    if any(bool(current.get(key)) for key in (
        "running", "paused", "pause_requested", "stop_requested",
        "action_required", "auto_paused", "terminated",
    )):
        return
    queue = items if items is not None else load_queue()
    resumable = [item for item in queue if item.get("status") in {"pending", "failed", "publishing"}]
    message = str(current.get("message") or "")
    completion_message = (
        "全部待发布视频已提交" in message
        or "全部待发布视频已提交/预约" in message
        or "没有待发布视频" in message
        or (current.get("total", 0) and current.get("completed", 0) >= current.get("total", 0))
    )
    if resumable and completion_message:
        current.update({
            "message": f"队列已更新，有 {len(resumable)} 条视频待处理",
            "current": "",
            "completed": 0,
            "total": len(resumable),
        })
        try:
            save_publish_state(current)
        except OSError:
            pass
    elif not queue and (completion_message or not message):
        current.update({
            "message": "队列为空，请先选择视频",
            "current": "",
            "completed": 0,
            "total": 0,
        })
        try:
            save_publish_state(current)
        except OSError:
            pass


def _default_caption_for_item(item: dict) -> str:
    """Return the caption created for a newly scanned queue item."""
    name = str(item.get("name") or item.get("path") or "视频")
    title = Path(name).stem.replace("_", " ").replace("-", " ").strip()
    return f"{title}｜演唱会现场 #演唱会 #现场"


def _copy_is_untouched(item: dict) -> bool:
    """Whether a queue item still has the app's initial caption/topic pair.

    Batch adoption is deliberately conservative: a user who edited either
    field should not have that work silently overwritten.  Empty fields count
    as untouched because they are also safe to fill with the recommendation.
    """
    caption = str(item.get("caption") or "").strip()
    hashtags = re.sub(r"[#\s]+", "", str(item.get("hashtags") or ""))
    hashtags = hashtags.replace("，", ",").replace("、", ",").replace(";", ",").replace("；", ",")
    normalized_tags = ",".join(part for part in hashtags.split(",") if part)
    return caption in {"", _default_caption_for_item(item)} and normalized_tags in {"", "演唱会,现场"}


_COPY_LIBRARY_LOCK = threading.RLock()
_COPY_LIBRARY_CACHE: tuple[float, dict] | None = None


def normalize_copy_key(value: object) -> str:
    """Normalize a video filename or song alias to a copy-library key."""
    stem = unicodedata.normalize("NFKC", Path(str(value or "")).stem).casefold()
    stem = re.sub(r"^(?:\._|\.)+", "", stem)
    # 去掉文件名开头的视频序号：2不朽、17let YOU go、27FOR FOREVER。
    stem = re.sub(r"^0*\d{1,3}(?=[a-z\u4e00-\u9fff])", "", stem)
    stem = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", stem)
    # 末尾的 1/2 多为同一首歌的版本号（如“3七重人格1”），不参与匹配。
    stripped = re.sub(r"\d+$", "", stem)
    return stripped or stem


def load_copy_library() -> dict:
    """Load the editable song copy library, cached by file mtime."""
    global _COPY_LIBRARY_CACHE
    with _COPY_LIBRARY_LOCK:
        try:
            mtime = COPY_LIBRARY_FILE.stat().st_mtime
        except OSError as exc:
            raise RuntimeError(f"文案库不存在：{COPY_LIBRARY_FILE}") from exc
        if _COPY_LIBRARY_CACHE and _COPY_LIBRARY_CACHE[0] == mtime:
            return _COPY_LIBRARY_CACHE[1]
        try:
            data = json.loads(COPY_LIBRARY_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"文案库无法读取：{exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("songs"), list):
            raise RuntimeError("文案库格式不正确：缺少 songs 列表")
        _COPY_LIBRARY_CACHE = (mtime, data)
        return data


def _copy_library_index(library: dict) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for entry in library.get("songs") or []:
        if not isinstance(entry, dict):
            continue
        aliases = [entry.get("song"), *(entry.get("aliases") or [])]
        for alias in aliases:
            key = normalize_copy_key(alias)
            if key:
                index.setdefault(key, entry)
    return index


def _normalize_copy_tags(value: object) -> str:
    text = re.sub(r"[#\s]+", "", str(value or ""))
    text = text.replace("，", ",").replace("、", ",").replace(";", ",").replace("；", ",")
    return ",".join(part for part in text.split(",") if part)


def _copy_entry_tags(library: dict, entry: dict) -> str:
    tags = [str(tag).strip().lstrip("#") for tag in library.get("base_tags") or []]
    tags += [str(tag).strip().lstrip("#") for tag in entry.get("extra_tags") or []]
    return ",".join(dict.fromkeys(tag for tag in tags if tag))


def _copy_matches_entry(item: dict, entry: dict, library: dict) -> bool:
    """Whether the item already holds exactly this library entry."""
    return (
        str(item.get("work_title") or "").strip() == str(entry.get("title") or "").strip()
        and str(item.get("caption") or "").strip() == str(entry.get("caption") or "").strip()
        and _normalize_copy_tags(item.get("hashtags")) == _normalize_copy_tags(_copy_entry_tags(library, entry))
    )


def generate_queue_copy(ids: list[str] | None = None, overwrite: bool = False) -> tuple[dict, int]:
    """Fill title/caption/topics from the editable song copy library.

    Matching is by the song name in the filename, so it is deterministic and
    fast.  Manual copy is preserved unless ``overwrite`` is true; overwriting
    backs up queue.json first.  No upload or publish is triggered here.
    """
    publish_state = load_publish_state()
    if queue_mutation_locked(publish_state) or load_channels_state().get("running"):
        return {
            "error": "发布或识别任务正在运行/暂停中，请先完成后再生成文案",
            "state": publish_state,
        }, HTTPStatus.CONFLICT
    try:
        library = load_copy_library()
    except RuntimeError as exc:
        return {"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR
    index = _copy_library_index(library)
    requested = {str(value) for value in (ids or []) if str(value).strip()}
    items = load_queue()
    generated: list[dict] = []
    skipped: list[dict] = []
    unmatched: list[dict] = []
    changed = False
    for item in items:
        item_id = str(item.get("id") or item.get("path") or "")
        if requested and item_id not in requested:
            continue
        if not requested and str(item.get("status") or "") in {"skipped", "published"}:
            continue
        entry = None
        for candidate in (item.get("name"), item.get("path")):
            entry = index.get(normalize_copy_key(candidate))
            if entry:
                break
        if entry is None:
            unmatched.append({
                "id": item_id,
                "name": item.get("name") or item_id,
                "reason": "文案库中没有匹配的歌名",
            })
            continue
        if not overwrite and not _copy_is_untouched(item) and not _copy_matches_entry(item, entry, library):
            skipped.append({
                "id": item_id,
                "name": item.get("name") or item_id,
                "reason": "已有手写文案",
            })
            continue
        item["work_title"] = str(entry.get("title") or "")
        item["caption"] = str(entry.get("caption") or "")
        item["hashtags"] = _copy_entry_tags(library, entry)
        item["updated_at"] = now()
        generated.append({
            "id": item_id,
            "name": item.get("name") or item_id,
            "song": str(entry.get("song") or ""),
        })
        changed = True

    backup_path: Path | None = None
    if changed:
        if overwrite and QUEUE_FILE.exists():
            backup_path = STATE_DIR / f"queue.before_copy_{datetime.now():%Y%m%d_%H%M%S}.json"
            shutil.copy2(QUEUE_FILE, backup_path)
        save_queue(items)

    message = f"已生成 {len(generated)} 条文案"
    if skipped:
        message += f"，保留手写 {len(skipped)} 条"
    if unmatched:
        message += f"，{len(unmatched)} 条未匹配文案库"
    if backup_path is not None:
        message += f"；已备份 {backup_path.name}"
    return {
        "ok": True,
        "generated": generated,
        "skipped": skipped,
        "unmatched": unmatched,
        "backup": str(backup_path) if backup_path else "",
        "items": public_queue_items(items),
        "message": message,
    }, HTTPStatus.OK


def apply_batch_recommendations(ids: list[str] | None = None) -> tuple[dict, int]:
    """Adopt completed recommendations without overwriting manual edits.

    This is a separate explicit action from recognition.  It applies only the
    recommended caption/topic pair for completed batch results, and skips any
    queue item whose existing copy differs from the initial defaults.
    """
    publish_state = load_publish_state()
    if queue_mutation_locked(publish_state):
        return {
            "error": "自动发布或批量识别正在运行/暂停中，请先完成或停止当前任务",
            "state": publish_state,
        }, HTTPStatus.CONFLICT
    analysis_state = load_analysis_state()
    if analysis_state.get("running"):
        return {"error": "批量识别正在进行，请等待完成后再采用推荐", "state": analysis_state}, HTTPStatus.CONFLICT
    requested = {str(value) for value in (ids or []) if str(value)}
    if ids is not None and not requested:
        return {"error": "至少选择一条已完成识别的视频"}, HTTPStatus.BAD_REQUEST
    items = load_queue()
    results = analysis_state.get("results") if isinstance(analysis_state.get("results"), dict) else {}
    applied: list[str] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    changed = False
    for item in items:
        item_id = _analysis_item_key(item)
        if requested and item_id not in requested:
            continue
        result = results.get(item_id)
        if not isinstance(result, dict) or result.get("status") != "completed":
            continue
        analysis = result.get("analysis") if isinstance(result.get("analysis"), dict) else {}
        recommendation = analysis.get("recommended") if isinstance(analysis.get("recommended"), dict) else {}
        caption = str(recommendation.get("caption") or recommendation.get("text") or "").strip()
        tags_value = recommendation.get("hashtags") or recommendation.get("tags") or recommendation.get("topics") or []
        if isinstance(tags_value, list):
            tags = ",".join(str(tag).replace("#", "").strip() for tag in tags_value if str(tag).strip())
        else:
            tags = re.sub(r"[#\s]+", "", str(tags_value)).replace("，", ",").replace("、", ",")
            tags = ",".join(part for part in tags.split(",") if part)
        if not caption and not tags:
            errors.append({"id": item_id, "name": item.get("name") or item_id, "error": "推荐结果没有可写入的文案或话题"})
            continue
        if not _copy_is_untouched(item):
            skipped.append({"id": item_id, "name": item.get("name") or item_id, "reason": "已手动编辑文案或话题"})
            continue
        if caption:
            item["caption"] = caption
        if tags:
            item["hashtags"] = tags
        item["updated_at"] = now()
        applied.append(item_id)
        changed = True
    if changed:
        save_queue(items)
    return {
        "ok": True,
        "applied": applied,
        "skipped": skipped,
        "errors": errors,
        "applied_count": len(applied),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "message": f"已采用 {len(applied)} 条推荐" + (f"，跳过 {len(skipped)} 条手动编辑项" if skipped else ""),
    }, HTTPStatus.OK


def select_media_files(
    *,
    default_video_type: str = "normal",
    default_panorama_format: str = "",
) -> tuple[dict, int]:
    """选择一个或多个视频并追加到队列，不删除或覆盖源文件。

    发布任务运行（包括安全暂停）时拒绝修改队列，避免发布器读取到半份
    队列。选择器取消、没有有效视频或全是重复项时，原队列保持不变。
    """
    try:
        default_video_type, default_panorama_format = normalize_selection_profile(
            default_video_type, default_panorama_format
        )
    except ValueError as exc:
        return {"error": str(exc)}, HTTPStatus.BAD_REQUEST

    state = load_publish_state()
    if queue_mutation_locked(state):
        return {
            "error": "自动发布正在运行或暂停中，请先完成/停止当前任务后再选择视频",
            "state": state,
        }, HTTPStatus.CONFLICT

    selected = choose_media_files()
    if not selected:
        return {"cancelled": True, "root": str(MEDIA_ROOT), "items": public_queue_items(load_queue())}, HTTPStatus.OK

    old_items = load_queue()
    old_by_path = {
        key: item for item in old_items
        if (key := _queue_path_key(item)) is not None
    }
    existing_keys = set(old_by_path)
    added: list[dict] = []
    skipped: list[dict] = []
    invalid: list[str] = []
    for selected_path in selected:
        try:
            video_path = selected_path.expanduser().resolve()
        except (OSError, RuntimeError):
            invalid.append(str(selected_path))
            continue
        if not video_path.is_file() or video_path.suffix.lower() not in VIDEO_EXTENSIONS:
            invalid.append(str(video_path))
            continue
        key = str(video_path)
        if key in existing_keys:
            skipped.append({"path": str(video_path), "reason": "已在队列中"})
            continue
        path_value = _queue_path_value(video_path)
        item = _queue_item_for_path(
            video_path,
            path_value=path_value,
            default_video_type=default_video_type,
            default_panorama_format=default_panorama_format,
        )
        added.append(item)
        existing_keys.add(key)

    if added:
        save_queue(old_items + added)
    items = old_items + added
    reconcile_idle_publish_message(items)
    return {
        "cancelled": False,
        "added": len(added),
        "skipped": skipped,
        "invalid": invalid,
        "items": public_queue_items(items),
        "root": str(MEDIA_ROOT),
    }, HTTPStatus.OK


def clear_queue() -> tuple[dict, int]:
    """清空本地队列，不删除任何视频源文件。"""
    state = load_publish_state()
    if queue_mutation_locked(state):
        return {
            "error": "自动发布正在运行或暂停中，暂不能清空队列",
            "state": state,
        }, HTTPStatus.CONFLICT
    items = load_queue()
    save_queue([])
    reconcile_idle_publish_message([])
    return {
        "ok": True,
        "cleared": len(items),
        "items": [],
        "root": str(MEDIA_ROOT),
        "message": "队列已清空，源视频文件未删除",
    }, HTTPStatus.OK


def delete_queue_items(ids: object) -> tuple[dict, int]:
    """从本地队列移除指定条目，不删除任何源视频文件。"""
    state = load_publish_state()
    if queue_mutation_locked(state):
        return {
            "error": "自动发布正在运行或暂停中，暂不能删除队列视频",
            "state": state,
        }, HTTPStatus.CONFLICT
    if not isinstance(ids, list):
        return {"error": "删除请求缺少有效的视频 id 列表"}, HTTPStatus.BAD_REQUEST
    requested = []
    seen: set[str] = set()
    for value in ids:
        if not isinstance(value, (str, int)):
            continue
        item_id = str(value).strip()
        if item_id and item_id not in seen:
            seen.add(item_id)
            requested.append(item_id)
    if not requested:
        return {"error": "请至少选择一条视频"}, HTTPStatus.BAD_REQUEST

    items = load_queue()
    remove_ids = set(requested)
    kept = [item for item in items if str(item.get("id") or item.get("path", "")) not in remove_ids]
    removed = len(items) - len(kept)
    if removed:
        save_queue(kept)
        reconcile_idle_publish_message(kept)
    return {
        "ok": True,
        "removed": removed,
        "requested": len(requested),
        "items": public_queue_items(kept),
        "root": str(MEDIA_ROOT),
        "message": f"已从队列移除 {removed} 条记录，源视频文件未删除",
    }, HTTPStatus.OK


def update_schedule_plan(updates: object) -> tuple[dict, int]:
    """Atomically apply schedule fields to several queue items.

    The UI uses this for the global plan generator so a failed validation does
    not leave the first half of a queue scheduled and the second half
    untouched.  It only changes schedule metadata; captions, source paths and
    publish status remain intact.
    """
    state = load_publish_state()
    if queue_mutation_locked(state):
        return {
            "error": "自动发布正在运行或暂停中，暂不能修改定时计划",
            "state": state,
        }, HTTPStatus.CONFLICT
    if not isinstance(updates, list) or not updates:
        return {"error": "定时计划缺少有效的更新列表"}, HTTPStatus.BAD_REQUEST
    items = load_queue()
    by_id = {
        str(item.get("id") or item.get("path", "")): item
        for item in items
    }
    normalized_updates: list[tuple[dict, str, str, str]] = []
    seen: set[str] = set()
    for raw in updates:
        if not isinstance(raw, dict):
            return {"error": "定时计划更新格式不正确"}, HTTPStatus.BAD_REQUEST
        item_id = str(raw.get("id") or raw.get("path") or "").strip()
        if not item_id or item_id in seen:
            return {"error": "定时计划包含重复或无效的视频 id"}, HTTPStatus.BAD_REQUEST
        seen.add(item_id)
        item = by_id.get(item_id)
        if item is None:
            return {"error": f"没有找到定时计划对应的视频：{item_id}"}, HTTPStatus.NOT_FOUND
        try:
            mode, schedule_at, interval = validate_schedule_fields(
                raw.get("schedule_mode", item.get("schedule_mode", "immediate")),
                raw.get("schedule_at", item.get("schedule_at", "")),
                raw.get("schedule_interval_minutes", item.get("schedule_interval_minutes", "")),
            )
        except ValueError as exc:
            return {
                "error": f"{item.get('name') or item.get('path') or item_id}：{exc}",
            }, HTTPStatus.BAD_REQUEST
        try:
            validate_schedule_future(mode, schedule_at)
        except ValueError as exc:
            return {
                "error": f"{item.get('name') or item.get('path') or item_id}：{exc}",
            }, HTTPStatus.BAD_REQUEST
        normalized_updates.append((item, mode, schedule_at, interval))
    for item, mode, schedule_at, interval in normalized_updates:
        item["schedule_mode"] = mode
        item["schedule_at"] = schedule_at
        item["schedule_interval_minutes"] = interval
        item["updated_at"] = now()
    save_queue(items)
    return {
        "ok": True,
        "updated": len(normalized_updates),
        "items": public_queue_items(items),
        "root": str(MEDIA_ROOT),
        "message": f"已更新 {len(normalized_updates)} 条定时计划",
    }, HTTPStatus.OK


def scan_videos(
    *,
    default_video_type: str = "normal",
    default_panorama_format: str = "",
) -> list[dict]:
    """递归扫描项目目录，保留原有文案和状态。

    The workflow profile is only applied to files that do not already have a
    queue record.  This makes a re-scan safe after a user has manually set a
    per-video publishing route.
    """
    default_video_type, default_panorama_format = normalize_selection_profile(
        default_video_type, default_panorama_format
    )
    old = {item.get("path"): item for item in load_queue()}
    found: list[dict] = []
    for path in sorted(MEDIA_ROOT.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        if STATE_DIR in path.parents:
            continue
        rel = path.relative_to(MEDIA_ROOT).as_posix()
        previous = old.get(rel, {})
        title = path.stem.replace("_", " ").replace("-", " ").strip()
        previous_status = previous.get("status", "pending")
        # 扫描和发布进程可能在同一时间写队列；成功备注是发布进程的最终信号，
        # 避免重新扫描时把已完成条目恢复成“待发布”。
        if previous.get("note") == "浏览器自动发布完成":
            previous_status = "published"
        try:
            stored_video_type = normalize_video_type(
                previous.get("video_type") if "video_type" in previous else default_video_type
            )
        except ValueError:
            stored_video_type = default_video_type
        try:
            stored_panorama_format = normalize_panorama_format(
                previous.get("panorama_format")
                if "panorama_format" in previous
                else default_panorama_format
            )
        except ValueError:
            stored_panorama_format = default_panorama_format
        found.append({
            "id": previous.get("id", rel),
            "path": rel,
            "name": path.name,
            "video_type": stored_video_type,
            "panorama_format": stored_panorama_format if stored_video_type == "panorama" else "",
            "panorama_check_status": previous.get("panorama_check_status", "unknown"),
            "official_topic_auto": normalize_bool(previous.get("official_topic_auto", True), default=True),
            "official_topic_keywords": normalize_official_topic_keywords(previous.get("official_topic_keywords", "")),
            "pico_enabled": normalize_bool(previous.get("pico_enabled", False), default=False),
            "work_title": normalize_queue_text(previous.get("work_title", ""), 30),
            "caption": previous.get("caption", f"{title}｜演唱会现场 #演唱会 #现场"),
            "hashtags": previous.get("hashtags", "演唱会,现场"),
            "cover_tsp": previous.get("cover_tsp", ""),
            "cover_image": previous.get("cover_image", ""),
            "cover_image_name": previous.get("cover_image_name", ""),
            "cover_image_landscape": previous.get("cover_image_landscape", ""),
            "cover_image_landscape_name": previous.get("cover_image_landscape_name", ""),
            "cover_image_portrait": previous.get("cover_image_portrait", ""),
            "cover_image_portrait_name": previous.get("cover_image_portrait_name", ""),
            "collection_name": normalize_queue_text(previous.get("collection_name", ""), 120),
            "self_declaration": normalize_queue_text(previous.get("self_declaration", ""), 200),
            "location": normalize_queue_text(previous.get("location", ""), 120),
            "hot_topics": normalize_queue_text(previous.get("hot_topics", ""), 500),
            "visibility": previous.get("visibility", "全部可见"),
            "allow_download": previous.get("allow_download", True),
            "schedule_mode": previous.get("schedule_mode", "immediate"),
            "schedule_at": previous.get("schedule_at", ""),
            "schedule_interval_minutes": previous.get("schedule_interval_minutes", ""),
            "status": previous_status,
            "platforms": normalize_platform_states(previous.get("platforms"), previous, stored_video_type),
            "note": previous.get("note", ""),
            "updated_at": previous.get("updated_at", now()),
        })
    save_queue(found)
    reconcile_idle_publish_message(found)
    return found


def safe_video_path(relative: str) -> Path | None:
    """解析队列中的视频路径。

    历史条目使用相对 MEDIA_ROOT 的路径；“选择视频文件”允许来自其他磁盘
    或目录，因此新条目也可能保存绝对路径。绝对路径只接受现存的、允许的
    视频扩展名，其他相对路径仍严格限制在 MEDIA_ROOT 内，避免路径穿越。
    """
    try:
        raw = Path(unquote(str(relative))).expanduser()
        if raw.is_absolute():
            candidate = raw.resolve()
            # 队列外的绝对路径不应被 /api/open 或其他请求任意读取。
            # 允许当前媒体根目录内的文件，以及明确由“添加视频文件”写入
            # 队列的外部文件；两者都仍需通过文件和扩展名校验。
            try:
                candidate.relative_to(MEDIA_ROOT.expanduser().resolve())
                allowed = True
            except ValueError:
                allowed = any(_queue_path_key(item) == str(candidate) for item in load_queue())
            if not allowed:
                return None
        else:
            candidate = (MEDIA_ROOT / raw).resolve()
            candidate.relative_to(MEDIA_ROOT)
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate if candidate.is_file() and candidate.suffix.lower() in VIDEO_EXTENSIONS else None


COVER_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
_COVER_DECORATION_RE = re.compile(
    r"(?:封面|封图|主图|cover|thumbnail|thumb|poster|视频|横版|横向|竖版|竖向|"
    r"landscape|portrait|horizontal|vertical)",
    re.IGNORECASE,
)
_COVER_EXTRA_DECORATION_RE = re.compile(
    r"(?:真人实拍|真人合成|真人优化|真人|实拍|合成返修|合成|脸部优化|脸部|"
    r"重做|返修|优化|定稿|终稿|初稿|备份|副本|最终|高清|无水印|无字版)",
    re.IGNORECASE,
)
_COVER_DATE_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?:[-_.]?\d{2}){1,2}(?!\d)")
_COVER_ASPECT_TOKEN_RE = re.compile(
    r"(?<!\d)\d{1,3}\s*(?:比|[:：]|x|×)\s*\d{1,3}(?!\d)", re.IGNORECASE
)
_COVER_WHOLE_ASPECT_RE = re.compile(r"^\d{1,3}(?:比|[:：x×])\d{1,3}$", re.IGNORECASE)
# “视频10 / video 10”是明确的视频编号；与视频自身编号不一致时必须拒绝，
# 避免把“向阳而生_视频01”贴到“28向阳而生”。普通前缀编号（01_、02_）
# 往往是设计批次序号，不能当作视频身份。
_COVER_EXPLICIT_NUMBER_RE = re.compile(r"(?:视频|video)\s*0*(\d{1,3})", re.IGNORECASE)


def safe_cover_path(relative: str) -> Path | None:
    """Resolve a stored custom cover image inside the local state directory."""
    try:
        raw = Path(unquote(str(relative))).expanduser()
        candidate = raw if raw.is_absolute() else (STATE_DIR / raw)
        candidate = candidate.resolve()
        candidate.relative_to(STATE_DIR.resolve())
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate if candidate.is_file() and candidate.suffix.lower() in COVER_EXTENSIONS else None


def cover_filename_key(value: object) -> str:
    """Normalize a video/image filename stem for custom-cover matching."""
    stem = unicodedata.normalize("NFKC", Path(str(value or "")).stem).casefold()
    return re.sub(r"[\s_\-]+", "", stem)


def _cover_name_parts(value: object) -> dict:
    """Split a cover/video filename into the parts that matter for matching.

    Cover files use several numbering schemes: ``2不朽``, ``01_不朽_横版``,
    ``02_不朽_横版``, ``不朽_视频10_横版``.  The song title is the stable
    identity; plain ordinal numbers are only a tie-breaker because design
    batches renumber the same songs.  An explicit ``视频NN``/``video NN``
    marker is authoritative, however, and must agree with the video number.
    """
    stem = unicodedata.normalize("NFKC", Path(str(value or "")).stem)
    stem = re.sub(r"^(?:\._|\.)+", "", stem).casefold()

    explicit = None
    marker = _COVER_EXPLICIT_NUMBER_RE.search(stem)
    if marker:
        explicit = int(marker.group(1))
        stem = _COVER_EXPLICIT_NUMBER_RE.sub(" ", stem)

    stem = _COVER_DATE_RE.sub(" ", stem)
    stem = _COVER_DECORATION_RE.sub(" ", stem)
    stem = _COVER_EXTRA_DECORATION_RE.sub(" ", stem)
    stem = re.sub(r"[_\-\s·.]+", " ", stem).strip()

    whole = re.sub(r"[\s_\-·.]+", "", stem)
    if _COVER_WHOLE_ASPECT_RE.fullmatch(whole):
        # 文件名本身就是“3比4”时，保留它作为作品名，不能当比例删掉。
        return {"key": whole, "core": whole, "serial": None, "explicit": explicit, "variant": ""}

    without_aspect = _COVER_ASPECT_TOKEN_RE.sub(" ", stem)
    if re.search(r"[0-9a-z\u4e00-\u9fff]", without_aspect):
        stem = without_aspect

    tokens = [token for token in stem.split() if token]
    # “视频10”本身就是视频编号；同时作为编号参与“编号+歌名”加分。
    serial = explicit
    variants: list[str] = []
    for token in tokens:
        if token.isdigit():
            if serial is None:
                serial = int(token)
            continue
        # “2不朽”“17let”这种编号和标题粘连的写法。
        glued = re.match(r"0*(\d{1,3})(?=[a-z\u4e00-\u9fff])", token)
        if glued:
            if serial is None:
                serial = int(glued.group(1))
            token = token[glued.end():]
        if not token:
            continue
        # “七重人格1”末尾的数字是版本变体，保留用于同名视频的区分。
        suffix = re.search(r"(\d{1,3})$", token)
        if suffix and token[:suffix.start()] and re.search(r"[a-z\u4e00-\u9fff]", token[:suffix.start()]):
            variants.append(str(int(suffix.group(1))))

    key = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", stem)
    core = re.sub(r"\d+", "", key)
    if not key:
        key = re.sub(
            r"[^0-9a-z\u4e00-\u9fff]+", "",
            unicodedata.normalize("NFKC", Path(str(value or "")).stem).casefold(),
        )
        core = re.sub(r"\d+", "", key)
    return {"key": key, "core": core, "serial": serial, "explicit": explicit, "variant": "".join(variants)}


def cover_orientation(value: object) -> str:
    """Identify a cover's intended layout from filename markers."""
    text = unicodedata.normalize("NFKC", Path(str(value or "")).stem).casefold()
    if re.search(r"横版|横向|landscape|horizontal|4\s*(?:比|x|×)\s*3", text, re.IGNORECASE):
        return "landscape"
    if re.search(r"竖版|竖向|portrait|vertical|3\s*(?:比|x|×)\s*4", text, re.IGNORECASE):
        return "portrait"
    return ""


def _cover_title_match(video: dict, image: dict) -> tuple[float, str]:
    """Score two parsed names by their song titles, with numbers as support."""
    if video["core"] == image["core"]:
        if video["variant"] and image["variant"]:
            if video["variant"] == image["variant"]:
                return 0.99, "编号、版本号和歌曲名匹配"
            # 同名但版本号不同：仍然可以匹配给当前唯一视频，但不要压过
            # 真正对应那个版本号的其他候选。
            return 0.90, "歌曲名相同但版本号不同"
        if (
            video["serial"] is not None
            and image["serial"] is not None
            and video["serial"] == image["serial"]
        ):
            return 0.99, "编号和歌曲名匹配"
        return 0.97, "歌曲名匹配"

    shorter, longer = sorted((video["core"], image["core"]), key=len)
    if len(shorter) >= 3 and shorter in longer:
        ratio = len(shorter) / max(1, len(longer))
        if ratio >= 0.6:
            return 0.93, "歌曲名包含关系匹配"
    ratio = difflib.SequenceMatcher(None, video["core"], image["core"]).ratio()
    if ratio >= 0.82:
        return round(min(0.92, ratio * 0.95), 3), "歌曲名相似匹配"
    return 0.0, "歌曲名不一致"


def cover_match_score(video_name: object, image_name: object) -> tuple[float, str]:
    """Return a conservative filename similarity score and match explanation.

    匹配优先级：歌曲名 > 版本号 > 编号。封面文件名里的 ``01_``/``02_``
    通常是设计批次序号，不同批次的编号会漂移；只要歌曲名一致就应匹配，
    编号只用于同名多视频之间的消歧。明确写出的“视频NN”除外。
    """
    video = _cover_name_parts(video_name)
    image = _cover_name_parts(image_name)
    if not video["key"] or not image["key"]:
        return 0.0, "缺少可匹配文件名"
    if (
        image["explicit"] is not None
        and video["serial"] is not None
        and image["explicit"] != video["serial"]
    ):
        return 0.0, "视频编号不一致"
    if (
        video["explicit"] is not None
        and image["serial"] is not None
        and video["explicit"] != image["serial"]
    ):
        return 0.0, "视频编号不一致"
    if video["core"] and image["core"]:
        return _cover_title_match(video, image)
    video_key, image_key = video["key"], image["key"]
    if video_key == image_key:
        return 1.0, "去除封面标记后完全匹配"
    shorter, longer = sorted((video_key, image_key), key=len)
    if len(shorter) >= 2 and shorter in longer:
        ratio = len(shorter) / max(1, len(longer))
        if ratio >= 0.55:
            return 0.94, "文件名包含关系匹配"
    video_leading_number = re.match(r"\d+", video_key)
    image_leading_number = re.match(r"\d+", image_key)
    if video_leading_number and image_leading_number:
        if int(video_leading_number.group()) == int(image_leading_number.group()):
            return 0.84, "文件名开头编号匹配"
    video_numbers = re.findall(r"\d+(?:\.\d+)?", video_key)
    image_numbers = re.findall(r"\d+(?:\.\d+)?", image_key)
    sequence = difflib.SequenceMatcher(None, video_key, image_key).ratio()
    shared_chars = len(set(video_key) & set(image_key)) / max(1, len(set(video_key)))
    if video_numbers and video_numbers == image_numbers and shared_chars >= 0.5 and sequence >= 0.58:
        return max(0.78, sequence), "编号和名称片段匹配"
    if sequence >= 0.78 and shared_chars >= 0.65:
        return sequence, "文件名相似度匹配"
    return 0.0, "文件名相似度不足"


def cover_image_name(item: dict) -> str:
    path = safe_cover_path(str(item.get("cover_image") or ""))
    return path.name if path else ""


def cover_variant_name(item: dict, orientation: str) -> str:
    field = "cover_image_landscape_name" if orientation == "landscape" else "cover_image_portrait_name"
    path_field = "cover_image_landscape" if orientation == "landscape" else "cover_image_portrait"
    value = str(item.get(field) or "").strip()
    if value:
        return value
    path = safe_cover_path(str(item.get(path_field) or ""))
    return path.name if path else ""


def remove_cover_file(relative: object) -> None:
    path = safe_cover_path(str(relative or ""))
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _format_bytes(value: int | None) -> str:
    if value is None or value < 0:
        return "未知"
    units = ("B", "KB", "MB", "GB", "TB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return "未知"


def _format_duration(value: float | None) -> str:
    if value is None or value < 0:
        return "未知"
    total = int(round(value))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes:02d}分{seconds:02d}秒"
    return f"{minutes}分{seconds:02d}秒"


def _run_command_json(command: list[str], timeout: float = 5.0) -> dict | None:
    """读取本地媒体工具输出；命令不存在/失败时返回 None，不抛出。"""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=False,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    try:
        value = plistlib.loads(result.stdout) if command[0].endswith("mdls") else json.loads(result.stdout.decode("utf-8", "replace"))
    except (ValueError, TypeError, plistlib.InvalidFileException, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _read_media_metadata(video_path: Path) -> tuple[dict, dict]:
    """读取尽可能少的本地元数据，并返回 facts 与依赖状态。"""
    facts: dict[str, object] = {}
    capabilities = {
        "mdls": bool(shutil.which("mdls")),
        "ffprobe": bool(shutil.which("ffprobe")),
        "audio_transcription": False,
        "visual_understanding": False,
    }
    try:
        stat = video_path.stat()
        facts.update({
            "size_bytes": stat.st_size,
            "size": _format_bytes(stat.st_size),
            "modified_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
        })
    except OSError as exc:
        facts["filesystem_error"] = str(exc)

    metadata: dict = {}
    if capabilities["mdls"]:
        metadata = _run_command_json([
            "mdls",
            "-plist",
            "-",
            "-name",
            "kMDItemDurationSeconds",
            "-name",
            "kMDItemPixelWidth",
            "-name",
            "kMDItemPixelHeight",
            "-name",
            "kMDItemCodecs",
            str(video_path),
        ]) or {}
    duration = metadata.get("kMDItemDurationSeconds")
    width = metadata.get("kMDItemPixelWidth")
    height = metadata.get("kMDItemPixelHeight")
    codecs = metadata.get("kMDItemCodecs")

    # 外接盘未建立 Spotlight 索引时 mdls 可能没有结果；若本机装有
    # ffprobe，再用它补一次，不把缺失依赖当成识别失败。
    if (duration is None or width is None or height is None or codecs is None) and capabilities["ffprobe"]:
        probe = _run_command_json([
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height",
            "-of",
            "json",
            str(video_path),
        ], timeout=8.0) or {}
        streams = probe.get("streams") if isinstance(probe, dict) else []
        if isinstance(streams, list):
            video_stream = next((stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "video"), {})
            width = width or video_stream.get("width")
            height = height or video_stream.get("height")
            codecs = codecs or video_stream.get("codec_name")
        fmt = probe.get("format") if isinstance(probe, dict) else {}
        if isinstance(fmt, dict):
            duration = duration or fmt.get("duration")

    try:
        duration_value = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_value = None
    try:
        width_value = int(width) if width is not None else None
    except (TypeError, ValueError):
        width_value = None
    try:
        height_value = int(height) if height is not None else None
    except (TypeError, ValueError):
        height_value = None
    if duration_value is not None and duration_value >= 0:
        facts.update({"duration_seconds": round(duration_value, 3), "duration": _format_duration(duration_value)})
    if width_value and height_value:
        facts.update({"width": width_value, "height": height_value, "resolution": f"{width_value}×{height_value}"})
        facts["orientation"] = "竖屏" if height_value > width_value else "横屏" if width_value > height_value else "方形"
    if codecs:
        if isinstance(codecs, (list, tuple, set)):
            codec_values = [str(value).strip() for value in codecs if str(value).strip()]
        else:
            codec_values = [str(codecs).strip()]
        if codec_values:
            facts["codecs"] = codec_values
            facts["video_codec"] = next(
                (value for value in codec_values if value.lower() not in {"mpeg-4 aac", "aac", "audio"}),
                codec_values[0],
            )
    return facts, capabilities


def detect_panorama_profile(video_path: Path) -> dict:
    """Return a conservative local preflight for a panoramic upload.

    A 2:1 frame is only a *dimension clue*, not proof that the file contains
    an equirectangular 180°/360° projection.  The final authority remains the
    currently logged-in Douyin panoramic editor, which can reject the file or
    ask for a different format.  This helper therefore separates hard local
    limits from advisory warnings and never auto-selects a format.
    """
    try:
        stat = video_path.stat()
        cache_key = (str(video_path.resolve()), int(stat.st_size), int(stat.st_mtime_ns))
    except (OSError, RuntimeError):
        cache_key = None
    if cache_key is not None:
        with _PANORAMA_PROFILE_CACHE_LOCK:
            cached = _PANORAMA_PROFILE_CACHE.get(cache_key)
        if cached is not None:
            return dict(cached)

    facts, capabilities = _read_media_metadata(video_path)
    extension = video_path.suffix.lower()
    width = facts.get("width")
    height = facts.get("height")
    duration = facts.get("duration_seconds")
    size_bytes = facts.get("size_bytes")
    codecs = facts.get("codecs")
    blocking: list[str] = []
    warnings: list[str] = []
    if extension not in {".mp4", ".mov"}:
        warnings.append("全景页当前推荐 MP4 或 MOV；其他格式可能被平台拒绝")
    if isinstance(size_bytes, (int, float)) and size_bytes > PANORAMA_MAX_BYTES:
        blocking.append("文件超过抖音全景页提示的 16GB 上限")
    if isinstance(duration, (int, float)) and duration > PANORAMA_MAX_DURATION_SECONDS:
        blocking.append("时长超过抖音全景页提示的 10 分钟上限")
    codec_text = " ".join(str(value) for value in codecs) if isinstance(codecs, (list, tuple, set)) else str(codecs or "")
    if re.search(r"\b(?:hevc|h\.?265)\b", codec_text, re.I):
        warnings.append("检测到 HEVC/H.265 编码；抖音全景页是否接受该编码仍以页面预审为准")
    ratio = None
    equirectangular_clue = False
    if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
        ratio = round(width / height, 4)
        equirectangular_clue = abs(ratio - 2.0) <= 0.03
        if not equirectangular_clue:
            warnings.append("画面比例不是典型 2:1 等距矩形，仅凭尺寸无法确认全景投影")
        if width < PANORAMA_RECOMMENDED_WIDTH or height < PANORAMA_RECOMMENDED_HEIGHT:
            warnings.append("分辨率低于抖音全景页建议的 4K（3840×1920），最终以页面预审为准")
    else:
        warnings.append("暂时无法读取画面尺寸，请让抖音全景页进行最终校验")
    if blocking:
        status = "fail"
    elif equirectangular_clue:
        status = "hint"
    else:
        status = "unknown"
    profile = {
        "status": status,
        "label": {
            "fail": "本地预检不通过",
            "hint": "2:1 尺寸线索，待页面确认",
            "unknown": "待页面确认",
        }[status],
        "extension": extension,
        "width": width,
        "height": height,
        "resolution": facts.get("resolution", ""),
        "duration_seconds": duration,
        "duration": facts.get("duration", ""),
        "codecs": codecs or [],
        "video_codec": facts.get("video_codec", ""),
        "size_bytes": size_bytes,
        "size": facts.get("size", ""),
        "ratio": ratio,
        "equirectangular_clue": equirectangular_clue,
        "blocking": blocking,
        "warnings": warnings,
        "capabilities": capabilities,
        "message": (
            blocking[0] if blocking else
            "尺寸符合 2:1 线索，但仍需在抖音全景编辑页选择格式并通过预审。"
            if equirectangular_clue else
            "未能从本地元数据确认全景投影；可手动选择格式并让抖音页面校验。"
        ),
    }
    if cache_key is not None:
        with _PANORAMA_PROFILE_CACHE_LOCK:
            # Keep the process-local cache bounded when a user browses many
            # external disks; stale entries are harmless but unbounded growth
            # is not useful for a local helper.
            if len(_PANORAMA_PROFILE_CACHE) >= 256:
                _PANORAMA_PROFILE_CACHE.pop(next(iter(_PANORAMA_PROFILE_CACHE)))
            _PANORAMA_PROFILE_CACHE[cache_key] = dict(profile)
    return profile


def queue_item_with_panorama_profile(item: dict) -> dict:
    """Add an ephemeral preflight snapshot to API responses.

    The profile is intentionally not persisted in ``queue.json``: dimensions
    and platform warnings can change after a file is replaced, while the
    user's chosen ``video_type``/``panorama_format`` must remain explicit.
    """
    result = dict(item)
    result.setdefault("video_type", "normal")
    result.setdefault("panorama_format", "")
    result["platforms"] = normalize_platform_states(result.get("platforms"), result, result.get("video_type"))
    result.setdefault("panorama_check_status", "unknown")
    result["official_topic_auto"] = normalize_bool(result.get("official_topic_auto", True), default=True)
    result["official_topic_keywords"] = normalize_official_topic_keywords(result.get("official_topic_keywords", ""))
    result["pico_enabled"] = normalize_bool(result.get("pico_enabled", False), default=False)
    result["cover_image_name"] = str(item.get("cover_image_name") or cover_image_name(item) or "")
    result["cover_image_landscape_name"] = cover_variant_name(item, "landscape")
    result["cover_image_portrait_name"] = cover_variant_name(item, "portrait")
    path = safe_video_path(str(item.get("path", "")))
    if path is None:
        result["panorama_profile"] = {
            "status": "fail",
            "label": "找不到源视频",
            "blocking": ["源视频不存在或路径无效"],
            "warnings": [],
            "message": "源视频不存在或路径无效",
        }
    else:
        try:
            result["panorama_profile"] = detect_panorama_profile(path)
        except Exception as exc:  # metadata is advisory; never break the queue
            result["panorama_profile"] = {
                "status": "unknown",
                "label": "待页面确认",
                "blocking": [],
                "warnings": [f"本地预检异常：{exc}"],
                "message": "本地预检未完成，请让抖音页面校验",
            }
    return result


def public_queue_items(items: list[dict]) -> list[dict]:
    """Return queue records enriched for the browser without mutating storage."""
    return [queue_item_with_panorama_profile(item) for item in items]


def _vision_frame_count(duration: object) -> int:
    """Choose a bounded temporal sample count for local Vision analysis.

    This is intentionally key-frame sampling rather than pretending that a
    general-purpose laptop can semantically inspect every decoded frame.  The
    samples cover the whole timeline and are enough to distinguish stage,
    performer, crowd, lighting and outdoor/indoor changes in concert footage.
    """
    try:
        seconds = float(duration)
    except (TypeError, ValueError):
        seconds = 0.0
    # Very short clips cannot support the same ten evenly spaced requests as
    # a minute-long clip.  Keep a small minimum for temporal context, but do
    # not ask AVAssetImageGenerator for dozens of timestamps that collapse to
    # the same key frame.
    if seconds <= 0:
        return 3
    if seconds < 3:
        return max(3, min(6, int(seconds * 2) + 1))
    if seconds <= 10:
        return 6
    if seconds <= 60:
        return 10
    if seconds <= 300:
        return 14
    return 20


def _safe_analysis_stem(item: dict, video_path: Path) -> str:
    raw = str(item.get("id") or item.get("path") or video_path.name)
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "_", raw).strip("._") or "video"
    return cleaned[:120]


def _ensure_vision_helper() -> tuple[Path | None, str]:
    """Compile the small macOS AVFoundation/Vision helper on first use."""
    if sys.platform != "darwin":
        return None, "本机不是 macOS，未找到 AVFoundation/Vision 分析后端"
    if not VISION_HELPER_SOURCE.is_file():
        return None, "缺少本机 Vision 分析脚本"
    swiftc = shutil.which("swiftc")
    if not swiftc:
        return None, "未找到 swiftc，无法启用本机逐帧视觉分析"
    try:
        needs_build = (
            not VISION_HELPER_BINARY.is_file()
            or VISION_HELPER_BINARY.stat().st_mtime < VISION_HELPER_SOURCE.stat().st_mtime
        )
    except OSError:
        needs_build = True
    if not needs_build:
        return VISION_HELPER_BINARY, ""
    with _VISION_BUILD_LOCK:
        try:
            needs_build = (
                not VISION_HELPER_BINARY.is_file()
                or VISION_HELPER_BINARY.stat().st_mtime < VISION_HELPER_SOURCE.stat().st_mtime
            )
        except OSError:
            needs_build = True
        if not needs_build:
            return VISION_HELPER_BINARY, ""
        _ensure_state_dir()
        temporary_binary = VISION_HELPER_BINARY.with_suffix(".tmp")
        try:
            result = subprocess.run(
                [swiftc, "-O", str(VISION_HELPER_SOURCE), "-o", str(temporary_binary)],
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return None, f"编译本机 Vision 后端失败：{exc}"
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()[-1:]
            return None, f"编译本机 Vision 后端失败：{' '.join(detail)}".strip()
        try:
            Path(temporary_binary).replace(VISION_HELPER_BINARY)
        except OSError as exc:
            return None, f"保存本机 Vision 后端失败：{exc}"
    return VISION_HELPER_BINARY, ""


def _ensure_sound_helper() -> tuple[Path | None, str]:
    """Compile the optional macOS SoundAnalysis helper on first use."""
    if sys.platform != "darwin":
        return None, "本机不是 macOS，未找到 SoundAnalysis 音频后端"
    if not SOUND_HELPER_SOURCE.is_file():
        return None, "缺少本机 SoundAnalysis 分析脚本"
    swiftc = shutil.which("swiftc")
    if not swiftc:
        return None, "未找到 swiftc，无法启用本机音频场景分析"
    try:
        needs_build = (
            not SOUND_HELPER_BINARY.is_file()
            or SOUND_HELPER_BINARY.stat().st_mtime < SOUND_HELPER_SOURCE.stat().st_mtime
        )
    except OSError:
        needs_build = True
    if not needs_build:
        return SOUND_HELPER_BINARY, ""
    with _VISION_BUILD_LOCK:
        try:
            needs_build = (
                not SOUND_HELPER_BINARY.is_file()
                or SOUND_HELPER_BINARY.stat().st_mtime < SOUND_HELPER_SOURCE.stat().st_mtime
            )
        except OSError:
            needs_build = True
        if not needs_build:
            return SOUND_HELPER_BINARY, ""
        _ensure_state_dir()
        temporary_binary = SOUND_HELPER_BINARY.with_suffix(".tmp")
        try:
            result = subprocess.run(
                [swiftc, "-O", str(SOUND_HELPER_SOURCE), "-o", str(temporary_binary)],
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return None, f"编译本机音频后端失败：{exc}"
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()[-1:]
            return None, f"编译本机音频后端失败：{' '.join(detail)}".strip()
        try:
            Path(temporary_binary).replace(SOUND_HELPER_BINARY)
        except OSError as exc:
            return None, f"保存本机音频后端失败：{exc}"
    return SOUND_HELPER_BINARY, ""


def _vision_cache_key(video_path: Path, frame_count: int) -> str:
    stat = video_path.stat()
    payload = f"{video_path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{ANALYSIS_VERSION}|{frame_count}"
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:24]


def _run_local_vision(video_path: Path, item: dict, duration: object) -> tuple[dict | None, str]:
    """Extract temporal frames and run macOS Vision OCR/classification.

    The returned payload is evidence-first.  It never invents a performer or
    song name: names are only proposed when OCR/model evidence actually
    contains them.  A missing helper is reported to the caller so the legacy
    metadata-only fallback can remain explicit.
    """
    helper, helper_error = _ensure_vision_helper()
    if helper is None:
        return None, helper_error
    frame_count = _vision_frame_count(duration)
    try:
        cache_key = _vision_cache_key(video_path, frame_count)
    except OSError as exc:
        return None, f"无法读取视频文件信息：{exc}"
    cache_dir = ANALYSIS_CACHE_DIR / _safe_analysis_stem(item, video_path) / cache_key
    result_file = cache_dir / "result.json"
    if result_file.is_file():
        try:
            cached = json.loads(result_file.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.get("ok"):
                return cached, ""
        except (OSError, json.JSONDecodeError):
            pass
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            [str(helper), str(video_path), str(cache_dir), str(frame_count)],
            capture_output=True,
            text=True,
            timeout=max(120, frame_count * 12),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"本机 Vision 分析失败：{exc}"
    raw_output = (completed.stdout or "").strip().splitlines()
    raw_json = raw_output[-1] if raw_output else ""
    try:
        payload = json.loads(raw_json)
    except (TypeError, json.JSONDecodeError):
        payload = None
    if completed.returncode != 0 or not isinstance(payload, dict) or not payload.get("ok"):
        detail = (completed.stderr or raw_json or "没有提取到有效视频帧").strip().splitlines()[-1:]
        return None, f"本机 Vision 分析失败：{' '.join(detail)}".strip()
    try:
        result_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        # Evidence is still usable in memory; a cache write failure should not
        # turn a successful analysis into a false failure.
        pass
    return payload, ""


def _run_local_sound(video_path: Path, item: dict) -> tuple[dict | None, str]:
    """Run Apple's built-in sound scene classifier on the original media."""
    helper, helper_error = _ensure_sound_helper()
    if helper is None:
        return None, helper_error
    try:
        cache_key = _vision_cache_key(video_path, 5)
    except OSError as exc:
        return None, f"无法读取音频文件信息：{exc}"
    cache_dir = ANALYSIS_CACHE_DIR / _safe_analysis_stem(item, video_path) / cache_key
    result_file = cache_dir / "sound.json"
    if result_file.is_file():
        try:
            cached = json.loads(result_file.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.get("ok"):
                return cached, ""
        except (OSError, json.JSONDecodeError):
            pass
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            [str(helper), str(video_path), "5", "8"],
            capture_output=True,
            text=True,
            timeout=max(180, int(_read_media_metadata(video_path)[0].get("duration_seconds") or 0) * 2 + 60),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"本机音频场景分析失败：{exc}"
    raw_output = (completed.stdout or "").strip().splitlines()
    try:
        payload = json.loads(raw_output[-1]) if raw_output else None
    except (TypeError, json.JSONDecodeError):
        payload = None
    if completed.returncode != 0 or not isinstance(payload, dict) or not payload.get("ok"):
        detail = (completed.stderr or (raw_output[-1] if raw_output else "") or "没有提取到有效音频").strip().splitlines()[-1:]
        return None, f"本机音频场景分析失败：{' '.join(detail)}".strip()
    try:
        result_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return payload, ""


_VISION_LABELS = {
    "concert": "演唱会",
    "performance": "舞台演出",
    "singer": "歌手/演唱者",
    "entertainer": "表演者",
    "crowd": "观众人群",
    "people": "人物",
    "microphone": "麦克风",
    "spotlight": "聚光灯",
    "light": "舞台灯光",
    "outdoor": "户外场景",
    "indoor": "室内场景",
    "night_sky": "夜景",
    "stage": "舞台",
}


def _clean_ocr_text(value: object, confidence: object = 0) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    try:
        score = float(confidence)
    except (TypeError, ValueError):
        score = 0.0
    if score < 0.45 or len(text) < 2:
        return ""
    # Drop obvious OCR noise while keeping Chinese, Latin names and digits.
    meaningful = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9·'’！？!?、，。,.：:《》<> ]", "", text)
    if len(meaningful.replace(" ", "")) < 2:
        return ""
    return meaningful[:80]


def _build_vision_analysis(
    item: dict,
    video_path: Path,
    raw: dict,
    facts: dict,
    capabilities: dict,
    sound: dict | None = None,
) -> dict:
    raw_frames = raw.get("frames") if isinstance(raw.get("frames"), list) else []
    # AVAssetImageGenerator can return the same actual timestamp for several
    # requests in a very short/GOP-heavy clip.  Deduplicate before aggregating
    # labels or OCR so one frame cannot masquerade as repeated evidence.
    frames: list[dict] = []
    seen_timestamps: set[int] = set()
    for frame in raw_frames:
        if not isinstance(frame, dict):
            continue
        try:
            timestamp = float(frame.get("timestamp") or 0)
        except (TypeError, ValueError):
            timestamp = 0.0
        timestamp_key = int(round(timestamp * 100))
        if timestamp_key in seen_timestamps:
            continue
        seen_timestamps.add(timestamp_key)
        frames.append(frame)
    label_scores: dict[str, float] = {}
    label_hits: dict[str, int] = {}
    evidence: list[dict] = []
    frame_evidence_ids: list[str] = []
    frame_signal_rows: list[dict] = []
    # Keep all readable OCR in per-frame evidence, but only promote text to a
    # caption when it repeats at least twice and every occurrence is strong.
    ocr_observations: dict[str, list[float]] = {}
    ocr_display: dict[str, str] = {}
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        timestamp = float(frame.get("timestamp") or 0)
        frame_labels = []
        for label_entry in frame.get("labels") or []:
            if not isinstance(label_entry, dict):
                continue
            identifier = str(label_entry.get("identifier") or "").lower()
            try:
                score = float(label_entry.get("confidence") or 0)
            except (TypeError, ValueError):
                score = 0.0
            label_scores[identifier] = max(label_scores.get(identifier, 0.0), score)
            display_label = _VISION_LABELS.get(identifier)
            if score >= 0.18 and display_label:
                label_hits[identifier] = label_hits.get(identifier, 0) + 1
                frame_labels.append({
                    "label": display_label,
                    "identifier": identifier,
                    "confidence": round(score, 3),
                })
        frame_ocr: list[str] = []
        frame_ocr_keys: set[str] = set()
        confidences = frame.get("ocrConfidence") or frame.get("ocr_confidence") or []
        for index, value in enumerate(frame.get("ocr") or []):
            confidence = confidences[index] if index < len(confidences) else 0.0
            cleaned = _clean_ocr_text(value, confidence)
            if not cleaned:
                continue
            frame_ocr.append(cleaned)
            key = re.sub(r"\s+", " ", cleaned).strip().casefold()
            try:
                score = float(confidence)
            except (TypeError, ValueError):
                score = 0.0
            # Count a text observation once per distinct key frame.  Vision
            # can occasionally return the same line more than once within a
            # single frame; that must not satisfy the cross-frame repetition
            # rule by itself.
            if key in frame_ocr_keys:
                continue
            frame_ocr_keys.add(key)
            ocr_observations.setdefault(key, []).append(score)
            ocr_display.setdefault(key, cleaned)
        if frame_labels or frame_ocr:
            evidence_id = f"frame-{len(evidence) + 1}"
            evidence.append({
                "id": evidence_id,
                "type": "video_frame",
                "label": f"关键帧 {timestamp:.1f} 秒",
                "title": f"关键帧 {timestamp:.1f} 秒",
                "value": "、".join(entry["label"] for entry in frame_labels[:6]) or "未识别到稳定视觉标签",
                "detail": (
                    f"帧文件：{frame.get('path', '')}"
                    + (f"；画面文字：{' / '.join(frame_ocr)}" if frame_ocr else "")
                ),
                "source": "macOS Vision",
                "confidence": "medium" if frame_labels else "low",
                "timestamp": round(timestamp, 2),
                "labels": frame_labels,
                "ocr": frame_ocr,
            })
            frame_evidence_ids.append(evidence_id)
        frame_signal_rows.append({
            "timestamp": round(timestamp, 2),
            "labels": frame_labels,
            "ocr": frame_ocr,
        })
    # Collapse repeated OCR from adjacent samples; repeated text is stronger
    # evidence than a one-frame hallucination.
    stable_ocr = [
        ocr_display[key]
        for key, scores in sorted(
            ocr_observations.items(),
            key=lambda entry: (-len(entry[1]), entry[0]),
        )
        if len(scores) >= 2 and min(scores) >= 0.7
    ][:3]
    strong_labels = {key for key, score in label_scores.items() if score >= 0.25}
    signals: list[str] = []
    for key in ("concert", "performance", "singer", "entertainer", "crowd", "microphone", "spotlight", "light", "outdoor", "indoor", "night_sky"):
        if key in strong_labels or label_hits.get(key, 0) >= 2:
            label = _VISION_LABELS.get(key, key)
            if label not in signals:
                signals.append(label)
    audio_scores: dict[str, float] = {}
    audio_hits: dict[str, int] = {}
    audio_evidence: list[dict] = []
    audio_evidence_ids: list[str] = []
    audio_segment_rows: list[dict] = []
    audio_label_map = {
        "music": "音乐",
        "singing": "演唱",
        "choir_singing": "合唱",
        "speech": "讲话",
        "crowd": "现场人群",
        "cheering": "欢呼",
        "applause": "掌声",
        "guitar": "吉他",
        "electric_guitar": "电吉他",
        "acoustic_guitar": "木吉他",
        "bass_guitar": "贝斯",
        "violin_fiddle": "小提琴",
        "bowed_string_instrument": "弦乐",
        "cello": "大提琴",
        "orchestra": "管弦乐",
        "drum": "鼓点",
        "drum_kit": "鼓组",
        "percussion": "打击乐",
        "keyboard_musical": "键盘",
        "organ": "风琴",
        "wind_instrument": "管乐",
        "shout": "喊声",
        "yell": "喊声",
    }
    if isinstance(sound, dict):
        for segment in sound.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            try:
                start = float(segment.get("start") or 0)
            except (TypeError, ValueError):
                start = 0.0
            segment_labels = []
            for label_entry in segment.get("labels") or []:
                if not isinstance(label_entry, dict):
                    continue
                identifier = str(label_entry.get("label") or "").lower()
                try:
                    score = float(label_entry.get("confidence") or 0)
                except (TypeError, ValueError):
                    score = 0.0
                # A single weak audio window is not enough to become a caption
                # fact; retain it as raw evidence but aggregate conservatively.
                if score >= 0.45:
                    audio_scores[identifier] = audio_scores.get(identifier, 0.0) + score
                    audio_hits[identifier] = audio_hits.get(identifier, 0) + 1
                    segment_labels.append({
                        "label": audio_label_map.get(identifier, identifier),
                        "identifier": identifier,
                        "confidence": round(score, 3),
                    })
            if segment_labels:
                evidence_id = f"audio-{len(audio_evidence) + 1}"
                audio_evidence.append({
                    "id": evidence_id,
                    "type": "audio_segment",
                    "label": f"声音片段 {start:.1f} 秒",
                    "title": f"声音片段 {start:.1f} 秒",
                    "value": "、".join(entry["label"] for entry in segment_labels[:5]),
                    "detail": "来自 macOS SoundAnalysis 场景分类，不是歌曲名或歌手识别。",
                    "source": "macOS SoundAnalysis",
                    "confidence": "medium",
                    "timestamp": round(start, 2),
                    "labels": segment_labels,
                })
                audio_evidence_ids.append(evidence_id)
            audio_segment_rows.append({
                "start": round(start, 2),
                "labels": segment_labels,
            })
    # Require repeated coverage before promoting sound labels into copy.  This
    # prevents isolated classifier noise (for example animal/cowbell labels)
    # from leaking into a public caption.
    audio_signals: list[str] = []
    total_segments = max(1, len((sound or {}).get("segments") or []))
    for identifier, total_score in sorted(audio_scores.items(), key=lambda entry: entry[1], reverse=True):
        coverage = audio_hits.get(identifier, 0) / total_segments
        average = total_score / max(1, audio_hits.get(identifier, 0))
        # Unknown SoundAnalysis identifiers are useful raw evidence, but are
        # not stable user-facing copy.  Only mapped labels can be promoted.
        if coverage >= 0.15 and average >= 0.55 and identifier in audio_label_map:
            label = audio_label_map[identifier]
            if label not in audio_signals:
                audio_signals.append(label)
    for label in audio_signals[:4]:
        if label not in signals:
            signals.append(label)
    if audio_evidence:
        evidence.extend(audio_evidence)
    if not signals:
        signals = ["视频画面"]

    # Build copy from a small, deterministic fingerprint of this video's
    # actual evidence.  The previous implementation used three fixed prose
    # templates, so clips with the same broad concert labels received the
    # same captions even when their sound, duration, or timeline differed.
    # Generic labels are retained as fallback context, but specific visual
    # labels and mapped sound labels get priority in the public copy.
    generic_visual_ids = {"people", "outdoor", "indoor", "night_sky"}
    ranked_visual = sorted(
        (
            (identifier, label_scores.get(identifier, 0.0), label_hits.get(identifier, 0))
            for identifier in label_scores
            if identifier in _VISION_LABELS and (label_scores.get(identifier, 0.0) >= 0.25 or label_hits.get(identifier, 0) >= 2)
        ),
        key=lambda row: (row[0] in generic_visual_ids, -row[2], -row[1], row[0]),
    )
    visual_focus = [_VISION_LABELS[identifier] for identifier, _, _ in ranked_visual]
    if not visual_focus:
        visual_focus = [label for label in signals if label not in audio_signals] or ["视频画面"]
    scene_text = "、".join(visual_focus[:3])

    # The evidence summary above is intentionally literal, but a publish
    # caption should read like a short Douyin hook rather than a classifier
    # report.  Keep generic labels (people/crowd/concert/outdoor) available in
    # the evidence panel while choosing one concrete visual anchor for copy.
    visual_priority = ("light", "spotlight", "microphone", "singer", "performance", "crowd", "outdoor", "night_sky")
    visual_anchor_id = next(
        (identifier for identifier in visual_priority if identifier in strong_labels or label_hits.get(identifier, 0) >= 2),
        "",
    )
    visual_anchor_labels = {
        "light": "舞台灯光",
        "spotlight": "聚光灯下的舞台",
        "microphone": "麦克风前的舞台",
        "singer": "歌手与舞台",
        "performance": "舞台与人群",
        "crowd": "现场人群",
        "outdoor": "户外夜景",
        "night_sky": "夜色现场",
    }
    visual_anchor = visual_anchor_labels.get(visual_anchor_id, "现场画面")

    try:
        duration_seconds = float(facts.get("duration_seconds") or raw.get("duration") or 0)
    except (TypeError, ValueError):
        duration_seconds = 0.0
    if duration_seconds < 0:
        duration_seconds = 0.0

    def phase_for(timestamp: object) -> str:
        try:
            value = float(timestamp or 0)
        except (TypeError, ValueError):
            value = 0.0
        if duration_seconds <= 0:
            return "现场"
        ratio = min(1.0, max(0.0, value / duration_seconds))
        if ratio < 0.34:
            return "前段"
        if ratio < 0.67:
            return "中段"
        return "后段"

    frame_times = [row["timestamp"] for row in frame_signal_rows if row.get("timestamp") is not None]
    first_phase = phase_for(min(frame_times)) if frame_times else "现场"
    last_phase = phase_for(max(frame_times)) if frame_times else "现场"
    first_visual = ""
    last_visual = ""
    if frame_signal_rows:
        for row in frame_signal_rows:
            labels = [entry.get("label") for entry in row.get("labels", []) if entry.get("label")]
            if labels and not first_visual:
                first_visual = labels[0]
            if labels:
                last_visual = labels[0]
    if first_visual and last_visual and first_visual != last_visual:
        temporal_text = f"从{first_phase}{first_visual}到{last_phase}{last_visual}"
    elif frame_times:
        temporal_text = f"关键帧覆盖{first_phase}至{last_phase}"
    else:
        temporal_text = "关键帧现场记录"

    duration_text = str(facts.get("duration") or "").strip()
    if not duration_text and duration_seconds > 0:
        duration_text = f"约{duration_seconds:.0f}秒"
    duration_suffix = f"（全片{duration_text}）" if duration_text else ""

    audio_text = "、".join(audio_signals[:3])
    # Collapse related SoundAnalysis labels into one natural phrase.  The raw
    # labels remain in audio_evidence; only the strongest one or two signals
    # are promoted to public copy.
    audio_phrase_map = {
        "演唱": "歌声", "合唱": "合唱声", "吉他": "吉他声", "电吉他": "电吉他声",
        "木吉他": "木吉他声", "贝斯": "贝斯声", "小提琴": "弦乐", "弦乐": "弦乐",
        "大提琴": "弦乐", "管弦乐": "管弦乐", "鼓点": "鼓点", "鼓组": "鼓点",
        "打击乐": "鼓点", "键盘": "键盘声", "风琴": "风琴声", "管乐": "管乐声",
    }
    audio_terms: list[str] = []
    for label in audio_signals:
        term = audio_phrase_map.get(label)
        if term and term not in audio_terms:
            audio_terms.append(term)
        if len(audio_terms) >= 2:
            break
    audio_phrase = "与".join(audio_terms) if audio_terms else "现场声音"
    audio_peak_start = None
    if audio_segment_rows and audio_signals:
        audio_label_set = set(audio_signals)
        scored_segments = []
        for row in audio_segment_rows:
            labels = [entry for entry in row.get("labels", []) if entry.get("label") in audio_label_set]
            if labels:
                scored_segments.append((sum(float(entry.get("confidence") or 0) for entry in labels), row.get("start", 0)))
        if scored_segments:
            audio_peak_start = max(scored_segments, key=lambda row: row[0])[1]
    audio_phase = phase_for(audio_peak_start) if audio_peak_start is not None else (first_phase if audio_text else "现场")
    ocr_suffix = f"；画面文字“{stable_ocr[0]}”" if stable_ocr else ""
    audio_caption_suffix = f"；声音场景为{audio_text}" if audio_text else ""
    transition_suffix = f"；{temporal_text}" if first_visual and last_visual and first_visual != last_visual else ""

    # A compact, human-readable timeline phrase is kept separate from the
    # literal evidence sentence.  It gives the caption a useful visual hook
    # without claiming an unverified song, artist or event detail.
    timeline_label_map = {
        "人物": "人物", "观众人群": "人群", "户外场景": "户外夜景", "夜景": "夜色",
        "舞台演出": "舞台", "舞台灯光": "灯光", "聚光灯": "聚光灯",
        "演唱会": "舞台", "歌手/演唱者": "歌手",
    }
    first_timeline_label = timeline_label_map.get(first_visual, first_visual or "现场")
    last_timeline_label = timeline_label_map.get(last_visual, last_visual or "现场")
    if first_timeline_label != last_timeline_label and first_visual and last_visual:
        natural_timeline = f"镜头从{first_timeline_label}带到{last_timeline_label}"
    else:
        natural_timeline = f"{visual_anchor}持续入镜"

    # Keep generic concert tags, then put the strongest item-specific audio
    # tag first.  This keeps shared discovery tags while making each video's
    # recommendation visibly different when its sound evidence differs.
    hashtags = ["演唱会", "现场"]
    audio_hashtag_map = {
        "吉他": "吉他现场", "电吉他": "电吉他", "木吉他": "木吉他",
        "小提琴": "弦乐现场", "弦乐": "弦乐现场", "大提琴": "弦乐现场", "管弦乐": "管弦现场",
        "鼓点": "鼓点", "鼓组": "鼓点", "掌声": "现场掌声",
    }
    for label in audio_signals:
        tag = audio_hashtag_map.get(label)
        if tag:
            hashtags.append(tag)
    if any(label in visual_focus for label in ("舞台演出", "演唱会", "舞台")):
        hashtags.append("舞台")
    if "观众人群" in visual_focus:
        hashtags.append("现场氛围")
    if any(label in visual_focus for label in ("歌手/演唱者", "麦克风")) or "演唱" in audio_signals:
        hashtags.append("音乐现场")
    if "户外场景" in visual_focus:
        hashtags.append("户外演出")
    hashtags = list(dict.fromkeys(hashtags))[:5]
    hashtag_text = " ".join(f"#{tag}" for tag in hashtags[:4])

    evidence_fingerprint = hashlib.sha1(
        json.dumps(
            {
                "visual": [(identifier, round(score, 3), hits) for identifier, score, hits in ranked_visual[:8]],
                "audio": audio_signals,
                "ocr": stable_ocr,
                "duration": round(duration_seconds, 2),
                "frames": [round(float(value), 2) for value in frame_times[:24]],
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8", "replace")
    ).hexdigest()[:10]
    candidate_basis = [*frame_evidence_ids[:4], *audio_evidence_ids[:4]]
    confidence = "high" if (len(visual_focus) >= 2 and (len(audio_signals) >= 1 or stable_ocr)) else "medium" if visual_focus else "low"
    # Keep diagnostic details (duration, frame count, classifier labels) in
    # the evidence/reason fields.  The visible caption uses three distinct
    # natural templates so a batch does not look like one copied sentence.
    caption_templates = [
        f"{visual_anchor}同框，{audio_phrase}把现场感带出来",
        f"{audio_phrase}响起，{visual_anchor}的演出氛围慢慢铺开",
        f"{natural_timeline}，{audio_phrase}贯穿这段演出",
    ]
    if stable_ocr:
        caption_templates[0] += f"，画面出现“{stable_ocr[0]}”"
    candidates = [
        {
            "id": "画面钩子",
            "label": "画面钩子",
            "title": "画面钩子",
            "text": caption_templates[0],
            "caption": f"{caption_templates[0]} {hashtag_text}",
            "hashtags": hashtags[:4],
            "confidence": confidence,
            "source": "vision_frames",
            "evidence_ids": candidate_basis,
        },
        {
            "id": "声音钩子",
            "label": "声音钩子",
            "title": "声音钩子",
            "text": caption_templates[1],
            "caption": f"{caption_templates[1]} {hashtag_text}",
            "hashtags": hashtags[:4],
            "confidence": "medium" if audio_text else "low",
            "source": "vision_frames_sound" if audio_text else "vision_frames",
            "evidence_ids": audio_evidence_ids[:8] or frame_evidence_ids[:4],
        },
        {
            "id": "时间线钩子",
            "label": "时间线钩子",
            "title": "时间线钩子",
            "text": caption_templates[2],
            "caption": f"{caption_templates[2]} {hashtag_text}",
            "hashtags": hashtags[:4],
            "confidence": "medium" if frame_times else "low",
            "source": "vision_frames",
            "evidence_ids": frame_evidence_ids[:8],
        },
    ]
    recommended = {
        **candidates[int(evidence_fingerprint[-2:], 16) % len(candidates)],
        "title": "推荐组合",
        "reason": (
            f"根据本视频的关键帧（{len(frames)} 个时间点）"
            + (f"、声音片段（{len(audio_segment_rows)} 个时间点）" if audio_segment_rows else "")
            + (f"和稳定画面文字（{len(stable_ocr)} 条）" if stable_ocr else "")
            + (f"；时长{duration_text}" if duration_text else "")
            + "生成；发布文案只保留已映射的画面/声音信号，未确认的歌手/歌曲不会自动补写。"
        ),
    }
    sound_available = bool(
        isinstance(sound, dict)
        and sound.get("ok") is not False
        and isinstance(sound.get("segments"), list)
        and any(isinstance(segment, dict) and segment.get("labels") for segment in sound.get("segments") or [])
    )
    visual_capabilities = dict(capabilities)
    visual_capabilities.update({
        "frame_sampling": True,
        # Vision classification/OCR is evidence extraction, not a general
        # multimodal semantic-understanding model.
        "visual_scene_classification": True,
        "visual_understanding": False,
        "ocr": True,
        "audio_transcription": False,
        "sound_classification": sound_available,
        "multimodal_generation": False,
    })
    limitations = [
        "这是本机 Vision 的关键帧分析，不是对视频每一帧逐字逐画面的人工式推理。",
        "未启用多模态生成模型，歌手、歌曲和地点等专名仍需人工核对。",
        "OCR 可能受演唱会灯光、字幕大小和运动模糊影响，单帧文字不会自动当作事实。",
    ]
    if sound_available:
        limitations.insert(1, "已加入本机声音场景分类，但不是语音转写，也不能据此确认歌手或歌曲名。")
    else:
        limitations.insert(1, "本次未取得有效声音场景结果，文案仅依据画面关键帧与可重复文字。")
    analysis_mode = "vision_frames_sound" if sound_available else "vision_frames"
    degraded_reason = (
        "已完成本机关键帧视觉与声音场景分析；语音转写和多模态生成模型尚未启用。"
        if sound_available
        else "已完成本机关键帧视觉分析；声音场景分析未取得有效结果，语音转写和多模态生成模型尚未启用。"
    )
    return {
        "version": ANALYSIS_VERSION,
        "mode": analysis_mode,
        "degraded": True,
        "degraded_reason": degraded_reason,
        "analyzed_at": now(),
        "file": {"name": str(item.get("name") or video_path.name), "path": item.get("path", ""), "extension": video_path.suffix.lower()},
        "facts": facts,
        "capabilities": visual_capabilities,
        "evidence": evidence,
        "audio_evidence": audio_evidence,
        "audio_signals": audio_signals,
        "frames": [{"timestamp": frame.get("timestamp"), "path": frame.get("path"), "labels": frame.get("labels", []), "ocr": frame.get("ocr", [])} for frame in frames],
        # Only expose labels that have a defined, user-facing mapping.  Vision
        # also emits generic object guesses (for example ``umbrella`` or
        # ``utensil``); retaining those as raw evidence would make a concert
        # recommendation look less trustworthy even though they are not used
        # to build the copy.
        "visual_signals": [{"label": _VISION_LABELS[key], "identifier": key, "confidence": round(label_scores[key], 3), "hits": label_hits.get(key, 0)} for key in sorted(label_scores, key=label_scores.get, reverse=True) if key in _VISION_LABELS][:12],
        "ocr_text": stable_ocr,
        "evidence_fingerprint": evidence_fingerprint,
        "candidates": candidates,
        "recommended": recommended,
        "recommended_tags": hashtags,
        "tag_recommendations": [{"tag": tag, "hashtag": f"#{tag}", "label": f"#{tag}", "text": f"#{tag}", "reason": "来自多个关键帧的视觉信号", "source": analysis_mode, "confidence": "medium" if tag in hashtags[:3] else "low"} for tag in hashtags],
        "limitations": limitations,
    }


def _folder_clue(video_path: Path) -> str:
    """从视频上级目录提取明确的演出线索；跳过磁盘、日期和通用目录名。"""
    generic = {
        "videos", "video", "movies", "movie", "media", "dcim", "100apple",
        "100msdcf", "下载", "桌面", "文稿", "视频", "视频发布工具", "工作",
    }
    date_pattern = re.compile(
        r"^(?:19|20)\d{2}[-_年]?\d{2}[-_月]?\d{2}(?:[-_日].*)?$|^\d{6,}$"
    )
    preferred: list[str] = []
    fallback: list[str] = []
    for parent in video_path.parents:
        label = parent.name.strip()
        if not label or label.lower() in generic or date_pattern.fullmatch(label):
            continue
        if label.startswith(".") or len(label) < 2:
            continue
        if any(token in label.lower() for token in ("演唱会", "现场", "舞台", "演出", "concert", "live", "音乐")):
            preferred.append(label)
        elif re.search(r"[\u4e00-\u9fff]", label):
            fallback.append(label)
    return (preferred or fallback or [""])[0]


def _filename_title(name: str, folder_clue: str = "") -> tuple[str, str, list[str]]:
    """把文件名转成保守标题，返回标题、置信度和线索。"""
    stem = Path(name).stem
    normalized = re.sub(r"[\s_\-]+", " ", stem).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    generic = bool(re.fullmatch(
        r"(?i)(?:video|vid|clip|movie|mov|mp4|dji|cam|dcim|img|image|untitled)(?:\s+\d[\w\s]*)?",
        normalized,
    ))
    if not normalized or generic:
        if folder_clue:
            return f"{folder_clue}现场片段", "low", [stem or "（无文件名）", folder_clue]
        return "演唱会现场片段", "low", [stem or "（无文件名）"]
    return normalized, "low", [normalized]


def _analysis_tags(filename: str) -> tuple[list[str], str, str]:
    lowered = filename.lower()
    concert_tokens = ("演唱会", "现场", "concert", "live", "舞台", "演出")
    music_tokens = ("音乐", "music", "song", "歌曲")
    matched = [token for token in concert_tokens if token in lowered]
    tags = ["演唱会", "现场"]
    if any(token in lowered for token in music_tokens):
        tags.append("音乐现场")
    if matched or len(tags) > 2:
        return tags, "filename", "medium"
    # 当前工具面向演唱会视频，但没有画面/音频证据时仍明确标低置信度；
    # 默认只给两个场景相关词，避免用过多泛标签稀释内容信号。
    return tags, "workflow_default", "low"


def _analyze_item_metadata(item: dict, video_path: Path) -> dict:
    """Legacy metadata-only fallback kept explicit when Vision is unavailable."""
    name = str(item.get("name") or video_path.name)
    folder_clue = _folder_clue(video_path)
    title, title_confidence, filename_clues = _filename_title(name, folder_clue)
    facts, capabilities = _read_media_metadata(video_path)
    tags, tag_source, tag_confidence = _analysis_tags(f"{name} {folder_clue}")
    evidence: list[dict] = [
        {
            "id": "filename",
            "type": "filename",
            "label": "文件名线索",
            "title": "文件名线索",
            "value": name,
            "detail": "仅用于生成候选文案，不代表已经识别出歌手、歌曲或地点。",
            "source": "filename",
            "confidence": title_confidence,
        },
        {
            "id": "file",
            "type": "filesystem",
            "label": "文件类型与大小",
            "title": "文件类型与大小",
            "value": f"{video_path.suffix.lower().lstrip('.').upper()} · {facts.get('size', '未知')}",
            "detail": "来自本地文件系统元数据。",
            "source": "filesystem",
            "confidence": "high" if "filesystem_error" not in facts else "low",
        },
    ]
    if folder_clue:
        evidence.append({
            "id": "folder",
            "type": "folder_name",
            "label": "上级文件夹线索",
            "title": "上级文件夹线索",
            "value": folder_clue,
            "detail": "来自本地路径名称；仅作为候选线索，不代表已确认演出信息。",
            "source": "folder_name",
            "confidence": "low",
        })
    if facts.get("duration"):
        evidence.append({
            "id": "duration",
            "type": "media_metadata",
            "label": "视频时长",
            "title": "视频时长",
            "value": facts["duration"],
            "detail": "来自 macOS 媒体元数据或 ffprobe。",
            "source": "media_metadata",
            "confidence": "high",
        })
    if facts.get("resolution"):
        evidence.append({
            "id": "resolution",
            "type": "media_metadata",
            "label": "画面尺寸",
            "title": "画面尺寸",
            "value": f"{facts['resolution']}（{facts.get('orientation', '未知方向')}）",
            "detail": "来自本地媒体元数据；未分析画面内容。",
            "source": "media_metadata",
            "confidence": "high",
        })
    evidence.append({
        "id": "scope",
        "type": "analysis_policy",
        "label": "识别范围",
        "title": "识别范围",
        "value": "V1 仅使用文件名、上级文件夹名和本地元数据",
        "detail": "未进行音频转写、歌手/歌曲识别或画面理解。",
        "source": "analysis_policy",
        "confidence": "high",
    })

    hashtag_text = " ".join(f"#{tag}" for tag in tags)
    metadata_suffix_parts: list[str] = []
    if facts.get("duration"):
        metadata_suffix_parts.append(f"时长{facts['duration']}")
    if facts.get("orientation"):
        metadata_suffix_parts.append(str(facts["orientation"]))
    elif facts.get("resolution"):
        metadata_suffix_parts.append(str(facts["resolution"]))
    metadata_suffix = f"（{' · '.join(metadata_suffix_parts)}）" if metadata_suffix_parts else ""
    candidates = [
        {
            "id": "现场记录",
            "label": "现场记录",
            "title": "现场记录",
            "text": f"{title}｜演唱会现场{metadata_suffix}",
            "caption": f"{title}｜演唱会现场{metadata_suffix} {hashtag_text}",
            "hashtags": tags[:2],
            "confidence": title_confidence,
            "source": "filename_metadata",
            "evidence_ids": ["filename", *(["folder"] if folder_clue else []), "scope"],
        },
        {
            "id": "氛围版",
            "label": "氛围版",
            "title": "氛围版",
            "text": f"把这一刻留在现场：{title}{metadata_suffix}",
            "caption": f"把这一刻留在现场：{title}{metadata_suffix} {hashtag_text}",
            "hashtags": tags[:2],
            "confidence": title_confidence,
            "source": "filename_metadata",
            "evidence_ids": ["filename", *(["folder"] if folder_clue else []), "scope"],
        },
        {
            "id": "片段版",
            "label": "片段版",
            "title": "片段版",
            "text": f"音乐现场片段｜{title}{metadata_suffix}",
            "caption": f"音乐现场片段｜{title}{metadata_suffix} {hashtag_text}",
            "hashtags": [tags[2], tags[0]] if len(tags) >= 3 else tags[:2],
            "confidence": title_confidence,
            "source": "filename_metadata",
            "evidence_ids": ["filename", *(["folder"] if folder_clue else []), "scope"],
        },
    ]
    tag_recommendations = [
        {
            "tag": tag,
            "hashtag": f"#{tag}",
            "label": f"#{tag}",
            "text": f"#{tag}",
            "reason": "文件名包含现场/演出线索" if tag_source == "filename" else "沿用当前工具的演唱会场景默认标签",
            "source": tag_source,
            "confidence": tag_confidence,
        }
        for tag in tags
    ]
    # 对外的 recommended 是一个可直接采用的“文案+话题”组合；详细的
    # 话题依据另放在 tag_recommendations，避免前端把列表误当成文案对象。
    recommended = {
        **candidates[0],
        "title": "推荐组合",
        "reason": "优先使用保守的现场记录表达；话题只保留与当前场景相关的通用词。",
        "hashtags": tags[:2],
        "source": "filename_metadata",
    }
    limitations = [
        "当前结果是可编辑初稿，不是对视频内容的事实确认。",
        "未配置本地语音转写、视觉模型或外部 AI 服务。",
    ]
    if not facts.get("duration") or not facts.get("resolution"):
        limitations.append("部分媒体元数据不可用，已降级为文件名和文件系统信息。")
    return {
        "version": ANALYSIS_VERSION,
        "mode": "filename_metadata",
        "degraded": True,
        "degraded_reason": "未启用音频转写或画面理解，仅根据文件名、文件夹线索和元数据生成保守候选。",
        "analyzed_at": now(),
        "file": {"name": name, "path": item.get("path", ""), "extension": video_path.suffix.lower()},
        "facts": facts,
        "filename_clues": filename_clues,
        "capabilities": capabilities,
        "evidence": evidence,
        "candidates": candidates,
        "recommended": recommended,
        "recommended_tags": tags,
        "tag_recommendations": tag_recommendations,
        "limitations": limitations,
    }


def analyze_item(item: dict, video_path: Path) -> dict:
    """Analyze actual video frames first, then fall back transparently.

    The batch worker and single-item endpoint share this function so the UI no
    longer has two different notions of "识别文案".  No result is written into
    the queue here; the user must still review and adopt a candidate.
    """
    facts, capabilities = _read_media_metadata(video_path)
    try:
        duration = facts.get("duration_seconds")
        visual_payload, visual_error = _run_local_vision(video_path, item, duration)
        if visual_payload:
            sound_payload, sound_error = _run_local_sound(video_path, item)
            analysis = _build_vision_analysis(item, video_path, visual_payload, facts, capabilities, sound_payload)
            if sound_error:
                analysis.setdefault("limitations", []).append(f"声音场景分析未完成：{sound_error}")
            return analysis
    except Exception as exc:
        visual_error = f"本机 Vision 分析异常：{exc}"
    fallback = _analyze_item_metadata(item, video_path)
    fallback["degraded_reason"] = (
        f"未能完成本机关键帧分析（{visual_error or '未知原因'}），仅使用文件名和元数据生成候选。"
    )
    fallback["limitations"] = [
        "当前结果是文件名/元数据降级候选，不是视频内容事实确认。",
        "请安装并允许 macOS Vision 后端运行后重新识别。",
        *[item for item in fallback.get("limitations", []) if item not in {"当前结果是可编辑初稿，不是对视频内容的事实确认。", "未配置本地语音转写、视觉模型或外部 AI 服务。"}],
    ]
    return fallback


def analyze_queue_item(item_id: str) -> tuple[dict, int]:
    """按队列条目 ID 分析；此函数只读队列，不调用 scan/save/publish。"""
    decoded_id = unquote(item_id)
    items = load_queue()
    item = next((entry for entry in items if str(entry.get("id", "")) == decoded_id), None)
    if item is None:
        # 兼容旧队列中 ID 缺失或前端传 path 的情况，但仍只允许队列已有路径。
        item = next((entry for entry in items if str(entry.get("path", "")) == decoded_id), None)
    if item is None:
        return {"error": "没有找到这条视频队列项"}, HTTPStatus.NOT_FOUND
    video_path = safe_video_path(str(item.get("path", "")))
    if video_path is None:
        return {"error": "视频文件不存在或路径无效，无法分析"}, HTTPStatus.UNPROCESSABLE_ENTITY
    analysis = analyze_item(item, video_path)
    # 同时提供顶层字段和 analysis 包装，便于旧版/新版前端逐步接入。
    return {
        "ok": True,
        "item_id": item.get("id", decoded_id),
        "analysis": analysis,
        "mode": analysis["mode"],
        "degraded": analysis["degraded"],
        "degraded_reason": analysis["degraded_reason"],
        "capabilities": analysis["capabilities"],
        "limitations": analysis["limitations"],
        "recommended_tags": analysis["recommended_tags"],
        "tag_recommendations": analysis["tag_recommendations"],
        "evidence": analysis["evidence"],
        "candidates": analysis["candidates"],
        "recommended": analysis["recommended"],
    }, HTTPStatus.OK


def import_cover_images(files: list[tuple[bytes, str]]) -> tuple[dict, int]:
    """Match uploaded cover images to queue videos by normalized filename."""
    state = load_publish_state()
    if queue_mutation_locked(state) or load_channels_state().get("running"):
        return {"error": "发布任务进行中，暂不能导入封面图片", "state": state}, HTTPStatus.CONFLICT
    items = load_queue()
    if not files:
        return {"error": "未读取到封面图片"}, HTTPStatus.BAD_REQUEST
    COVER_DIR.mkdir(parents=True, exist_ok=True)
    matched: list[dict] = []
    added: list[dict] = []
    replaced: list[dict] = []
    unmatched: list[dict] = []
    changed = False
    for data, filename in files:
        clean_name = Path(str(filename or "")).name
        if clean_name.startswith("._"):
            unmatched.append({"filename": clean_name, "reason": "跳过 macOS 隐藏副本，请选择正式图片文件"})
            continue
        extension = Path(clean_name).suffix.lower()
        if extension not in COVER_EXTENSIONS:
            unmatched.append({"filename": clean_name, "reason": "只支持 JPG、JPEG、PNG、WEBP 图片"})
            continue
        if not data:
            unmatched.append({"filename": clean_name, "reason": "图片文件为空"})
            continue
        if len(data) > 20 * 1024 * 1024:
            unmatched.append({"filename": clean_name, "reason": "图片不能超过 20 MB"})
            continue
        scored: list[tuple[float, str, dict]] = []
        for item in items:
            name_score, name_reason = cover_match_score(item.get("name"), clean_name)
            path_score, path_reason = cover_match_score(item.get("path"), clean_name)
            score, reason = max((name_score, name_reason), (path_score, path_reason), key=lambda value: value[0])
            if score > 0:
                scored.append((score, reason, item))
        scored.sort(key=lambda value: value[0], reverse=True)
        if not scored or scored[0][0] < 0.72:
            unmatched.append({"filename": clean_name, "reason": "没有找到足够相似的视频文件名"})
            continue
        if len(scored) > 1 and scored[1][0] >= scored[0][0] - 0.08:
            options = "、".join(str(row[2].get("name") or row[2].get("path")) for row in scored[:3])
            unmatched.append({"filename": clean_name, "reason": f"多个视频相似度接近，请确认：{options}"})
            continue
        score, reason, item = scored[0]
        item_id = str(item.get("id") or item.get("path") or item.get("name") or "video")
        safe_stem = re.sub(r"[^0-9A-Za-z._-]+", "_", item_id).strip("._")[:100] or "video"
        orientation = cover_orientation(clean_name)
        if orientation == "landscape":
            path_field, name_field = "cover_image_landscape", "cover_image_landscape_name"
            target = COVER_DIR / f"{safe_stem}_landscape{extension}"
        elif orientation == "portrait":
            path_field, name_field = "cover_image_portrait", "cover_image_portrait_name"
            target = COVER_DIR / f"{safe_stem}_portrait{extension}"
        else:
            path_field, name_field = "cover_image", "cover_image_name"
            target = COVER_DIR / f"{safe_stem}{extension}"
        old_cover = str(item.get(path_field) or "").strip()
        old_cover_name = str(item.get(name_field) or "").strip()
        action = "replaced" if old_cover or old_cover_name else "added"
        target.write_bytes(data)
        item[path_field] = target.relative_to(STATE_DIR).as_posix()
        item[name_field] = clean_name
        item["updated_at"] = now()
        if old_cover and old_cover != item[path_field]:
            remove_cover_file(old_cover)
        if item.get("cover_image") and item.get("cover_image_landscape") and item.get("cover_image_portrait"):
            legacy_cover = item.get("cover_image")
            item["cover_image"] = ""
            item["cover_image_name"] = ""
            remove_cover_file(legacy_cover)
        changed = True
        match_row = {
            "video": item.get("name") or item.get("path"),
            "cover": clean_name,
            "orientation": orientation or "unknown",
            "score": round(score, 3),
            "reason": reason,
            "action": action,
        }
        matched.append(match_row)
        (replaced if action == "replaced" else added).append(match_row)
    if changed:
        save_queue(items)
    action_text = f"替换 {len(replaced)} 张，新增 {len(added)} 张"
    if unmatched:
        action_text += f"，{len(unmatched)} 张待确认"
    return {
        "ok": True,
        "matched": matched,
        "added": added,
        "replaced": replaced,
        "unmatched": unmatched,
        "items": public_queue_items(items),
        "message": f"本轮导入完成：{action_text}；未出现在本轮的旧封面已保留",
    }, HTTPStatus.OK


def _parse_multipart_uploads(handler: BaseHTTPRequestHandler) -> tuple[list[tuple[bytes, str]], str | None]:
    """Parse all file parts from a multipart/form-data upload."""
    content_type = handler.headers.get("Content-Type", "")
    if "multipart/form-data" not in content_type:
        return [], "请求必须是 multipart/form-data 格式"
    match = re.search(r'boundary=([^;]+)', content_type)
    if not match:
        return [], "缺少 boundary"
    boundary = match.group(1).strip().strip('"')
    boundary_bytes = ("--" + boundary).encode("utf-8")
    size = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(size)
    files: list[tuple[bytes, str]] = []
    parts = raw.split(boundary_bytes)
    for part in parts[1:]:
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        header_end = part.find(b"\r\n\r\n")
        if header_end == -1:
            header_end = part.find(b"\n\n")
            if header_end == -1:
                continue
            headers = part[:header_end].decode("utf-8", errors="replace")
            body = part[header_end + 2:]
        else:
            headers = part[:header_end].decode("utf-8", errors="replace")
            body = part[header_end + 4:]
        if "filename=" in headers.lower():
            fname_match = re.search(r'filename="([^"]+)"', headers, re.IGNORECASE)
            if not fname_match:
                fname_match = re.search(r"filename='([^']+)'", headers, re.IGNORECASE)
            filename = fname_match.group(1) if fname_match else "unknown.xlsx"
            files.append((body, filename))
    return files, None if files else "未找到上传的文件"


def _parse_multipart_upload(handler: BaseHTTPRequestHandler) -> tuple[bytes | None, str | None, str | None]:
    """Backward-compatible single-file multipart parser."""
    files, error = _parse_multipart_uploads(handler)
    if error or not files:
        return None, None, error or "未找到上传的文件"
    data, filename = files[0]
    return data, filename, None


def _parse_caption_xlsx(data: bytes) -> list[dict]:
    """Parse an xlsx workbook and return rows as dicts with name/title/caption/hashtags."""
    if openpyxl is None:
        raise RuntimeError("缺少 openpyxl，请安装：pip install openpyxl")
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    ws = wb["文案导入模板"] if "文案导入模板" in wb.sheetnames else wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    headers = [str(h or "").strip() for h in rows[0]]
    header_map: dict[int, str] = {}
    for i, h in enumerate(headers):
        hl = h.lower()
        if hl in ("文件名", "filename", "file", "name", "视频", "视频名"):
            header_map[i] = "name"
        elif hl in ("标题", "title", "主题", "作品标题"):
            header_map[i] = "title"
        elif hl in ("文案", "caption", "text", "description", "desc", "content", "作品描述"):
            header_map[i] = "caption"
        elif hl in ("话题", "hashtags", "hashtag", "tags", "topics", "标签"):
            header_map[i] = "hashtags"
    result: list[dict] = []
    for row in rows[1:]:
        # Older templates appended these instructions to the data sheet.
        # Stop at the exact marker while still allowing ordinary blank rows.
        if row and str(row[0] or "").strip() == "说明：" and not any(row[1:]):
            break
        record: dict[str, str] = {}
        has_value = False
        for i, val in enumerate(row):
            if i in header_map:
                if val is None:
                    value = ""
                elif isinstance(val, datetime):
                    # Excel may auto-format filenames as dates; try to keep the
                    # original display text by reading the raw cell value.
                    value = val.strftime("%Y%m%d")
                else:
                    value = str(val).strip()
                # Aggressively clean filename fields to avoid invisible chars.
                if header_map[i] == "name":
                    value = value.replace("\u00a0", " ").replace("\u200b", "").replace("\u200c", "").replace("\u200d", "").replace("\ufeff", "").strip()
                record[header_map[i]] = value
                if value:
                    has_value = True
        if has_value:
            result.append(record)
    return result


def _generate_caption_template(items: list[dict] | None = None) -> bytes:
    """Generate an xlsx template for caption import.

    If ``items`` is provided, pre-fill the template with current queue data
    so the user only needs to edit captions and hashtags.
    """
    if openpyxl is None:
        raise RuntimeError("缺少 openpyxl，请安装：pip install openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "文案导入模板"
    ws.append(["文件名", "标题", "文案", "话题"])
    if items:
        for item in items:
            name = str(item.get("name") or "").strip()
            title = str(item.get("work_title") or "").strip()
            caption = str(item.get("caption") or "").strip()
            hashtags = str(item.get("hashtags") or "").strip()
            row_idx = ws.max_row + 1
            ws.append([name, title, caption, hashtags])
            # Force the filename column to text so Excel does not auto-format
            # dates or numbers (e.g. 20260926.MOV -> date).
            ws.cell(row=row_idx, column=1).number_format = "@"
    else:
        ws.append(["video1.mp4", "示例标题1", "这是第一条视频的发布文案", "话题1,话题2"])
        ws.append(["video2.mp4", "示例标题2", "这是第二条视频的发布文案", "话题3,话题4"])
    ws.column_dimensions["A"].width = 25
    ws.column_dimensions["B"].width = 30
    ws.column_dimensions["C"].width = 50
    ws.column_dimensions["D"].width = 40
    instructions = wb.create_sheet("填写说明")
    instructions.column_dimensions["A"].width = 85
    instructions.append(["1. 文件名：与队列中视频文件名保持一致，用于自动匹配；重名时请单独选择目标视频导入"])
    instructions.append(["2. 标题：30字以内，单独填写到抖音标题栏"])
    instructions.append(["3. 文案：视频发布文案/作品描述"])
    instructions.append(["4. 话题：多个话题用逗号分隔，不需要加#号"])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _generate_unmatched_report(unmatched: list[dict], available_names: list[str]) -> bytes:
    """Generate an xlsx report for unmatched caption rows.

    Sheet 1: Unmatched rows (what the user tried to import).
    Sheet 2: Available filenames in the queue (for comparison).
    """
    if openpyxl is None:
        raise RuntimeError("缺少 openpyxl，请安装：pip install openpyxl")
    wb = openpyxl.Workbook()

    # Sheet 1: Unmatched rows
    ws1 = wb.active
    ws1.title = "未匹配记录"
    ws1.append(["文件名", "标题", "文案", "话题", "可能原因"])
    for row in unmatched:
        name = str(row.get("name") or "").strip()
        title = str(row.get("title") or "").strip()
        caption = str(row.get("caption") or "").strip()
        hashtags = str(row.get("hashtags") or "").strip()
        reason = str(row.get("reason") or "文件名与队列中任何视频都不匹配")
        ws1.append([name, title, caption, hashtags, reason])
        ws1.cell(row=ws1.max_row, column=1).number_format = "@"
    ws1.column_dimensions["A"].width = 25
    ws1.column_dimensions["B"].width = 30
    ws1.column_dimensions["C"].width = 50
    ws1.column_dimensions["D"].width = 40
    ws1.column_dimensions["E"].width = 40

    # Sheet 2: Available filenames
    ws2 = wb.create_sheet(title="队列可用文件名")
    ws2.append(["序号", "文件名"])
    for idx, name in enumerate(available_names, start=1):
        ws2.append([idx, name])
        ws2.cell(row=ws2.max_row, column=2).number_format = "@"
    ws2.column_dimensions["A"].width = 8
    ws2.column_dimensions["B"].width = 50

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def json_response(handler: BaseHTTPRequestHandler, payload: object, status: int = 200) -> None:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


class Handler(BaseHTTPRequestHandler):
    server_version = "DouyinBatch/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def do_GET(self) -> None:  # noqa: N802
        global MEDIA_ROOT
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            items = load_queue()
            json_response(self, {
                "items": public_queue_items(items),
                "upload_url": UPLOAD_URL,
                "panorama_upload_url": PANORAMA_UPLOAD_URL,
                "root": str(MEDIA_ROOT),
                "publish": load_publish_state(),
                "channels": load_channels_state(),
                "analysis_batch": analysis_state_payload(include_results=False),
            })
            return
        if parsed.path == "/api/publish-status":
            json_response(self, load_publish_state())
            return
        if parsed.path == "/api/channels-status":
            json_response(self, load_channels_state())
            return
        if parsed.path == "/api/analyze-status":
            query = parse_qs(parsed.query)
            include_results = query.get("include_results", ["0"])[0].lower() in {"1", "true", "yes"}
            json_response(self, analysis_state_payload(include_results=include_results))
            return
        if parsed.path == "/api/caption-template":
            try:
                data = _generate_caption_template()
            except RuntimeError as exc:
                json_response(self, {"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%E6%96%87%E6%A1%88%E5%AF%BC%E5%85%A5%E6%A8%A1%E6%9D%BF.xlsx")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/api/scan":
            # Scanning rewrites queue.json, so it is a mutating operation even
            # though the result is read-like.  Keep it POST-only just like
            # folder/file selection and queue edits; this prevents a browser
            # prefetch or pasted URL from changing the user's queue.
            json_response(self, {"error": "此接口只接受 POST 请求"}, HTTPStatus.METHOD_NOT_ALLOWED)
            return
        if parsed.path in {
            "/api/select-folder", "/api/select-files", "/api/select-videos", "/api/clear-queue", "/api/queue/clear",
            "/api/items/delete", "/api/queue/delete",
            "/api/schedule-plan",
            "/api/scan",
            "/api/copy-generate",
            "/api/analyze-batch", "/api/analyze-cancel", "/api/analyze-apply",
            "/api/channels-publish", "/api/channels-publish-queue",
            "/api/channels-publish/pause", "/api/channels-publish/resume", "/api/channels-publish/terminate",
            "/api/open-channels-ui",
            "/api/publish", "/api/publish-check",
            "/api/publish-stop", "/api/publish/stop", "/api/publish-terminate", "/api/publish/terminate",
        }:
            # 这些接口会打开选择器、改变队列或启动/控制任务，必须使用
            # POST，避免浏览器预取或用户误打开链接触发副作用。
            json_response(self, {"error": "此接口只接受 POST 请求"}, HTTPStatus.METHOD_NOT_ALLOWED)
            return
        if parsed.path == "/api/open":
            query = parse_qs(parsed.query)
            target = query.get("target", [""])[0]
            if target == "upload":
                # 浏览器端负责在当前会话中新开标签；服务端不再调用系统默认浏览器，
                # 避免用户点击后看不到跳转。
                json_response(self, {"ok": True, "url": UPLOAD_URL})
                return
            video = safe_video_path(query.get("path", [""])[0])
            if video is None:
                json_response(self, {"error": "视频路径无效"}, HTTPStatus.BAD_REQUEST)
                return
            subprocess.Popen(["open", str(video)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            json_response(self, {"ok": True})
            return
        self.serve_static(parsed.path)

    def do_POST(self) -> None:  # noqa: N802
        global MEDIA_ROOT
        parsed = urlparse(self.path)
        analyze_prefix = "/api/items/"
        if parsed.path == "/api/covers/import":
            files, error = _parse_multipart_uploads(self)
            if error:
                json_response(self, {"error": error}, HTTPStatus.BAD_REQUEST)
                return
            payload, status = import_cover_images(files)
            json_response(self, payload, status)
            return
        if parsed.path == "/api/copy-generate":
            try:
                size = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(size) or b"{}")
            except (ValueError, json.JSONDecodeError):
                json_response(self, {"error": "请求数据格式不正确"}, HTTPStatus.BAD_REQUEST)
                return
            if not isinstance(body, dict):
                json_response(self, {"error": "请求数据必须是对象"}, HTTPStatus.BAD_REQUEST)
                return
            ids = body.get("ids") if isinstance(body.get("ids"), list) else None
            overwrite = normalize_bool(body.get("overwrite", False))
            payload, status = generate_queue_copy(
                [str(value) for value in ids] if ids is not None else None,
                overwrite,
            )
            json_response(self, payload, status)
            return
        if parsed.path == "/api/items/bulk-preferences":
            try:
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            except (ValueError, json.JSONDecodeError):
                json_response(self, {"error": "请求数据格式不正确"}, HTTPStatus.BAD_REQUEST)
                return
            result, status = update_bulk_preferences(payload)
            json_response(self, result, status)
            return
        if parsed.path.startswith(analyze_prefix) and parsed.path.endswith("/analyze"):
            item_id = parsed.path[len(analyze_prefix):-len("/analyze")].strip("/")
            payload, status = analyze_queue_item(item_id)
            json_response(self, payload, status)
            return
        if parsed.path == "/api/open-channels-ui":
            payload, status = open_channels_ui()
            json_response(self, payload, status)
            return
        if parsed.path in {
            "/api/channels-publish/pause",
            "/api/channels-publish/resume",
            "/api/channels-publish/terminate",
        }:
            action = parsed.path.rsplit("/", 1)[-1]
            payload, status = channels_publish_control(action)
            json_response(self, payload, status)
            return
        if parsed.path == "/api/channels-publish":
            try:
                size = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(size) or b"{}")
            except (ValueError, json.JSONDecodeError):
                json_response(self, {"error": "请求数据格式不正确"}, HTTPStatus.BAD_REQUEST)
                return
            item_id = str(body.get("item_id") or "") if isinstance(body, dict) else ""
            item = next((x for x in load_queue() if str(x.get("id") or x.get("path") or "") == item_id), None)
            if item is None:
                json_response(self, {"error": "没有找到这条视频"}, HTTPStatus.NOT_FOUND)
                return
            payload, status = start_channels_publish(item)
            json_response(self, payload, status)
            return
        if parsed.path == "/api/channels-publish-queue":
            try:
                size = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(size) or b"{}")
            except (ValueError, json.JSONDecodeError):
                json_response(self, {"error": "请求数据格式不正确"}, HTTPStatus.BAD_REQUEST)
                return
            ids = body.get("ids") if isinstance(body, dict) and isinstance(body.get("ids"), list) else []
            payload, status = start_channels_publish_queue([str(value) for value in ids])
            json_response(self, payload, status)
            return
        if parsed.path == "/api/caption-template":
            try:
                size = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(size) or b"{}")
            except (ValueError, json.JSONDecodeError):
                json_response(self, {"error": "请求数据格式不正确"}, HTTPStatus.BAD_REQUEST)
                return
            if not isinstance(body, dict):
                body = {}
            try:
                items = body.get("items") if isinstance(body.get("items"), list) else None
                data = _generate_caption_template(items)
            except RuntimeError as exc:
                json_response(self, {"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%E6%96%87%E6%A1%88%E5%AF%BC%E5%85%A5%E6%A8%A1%E6%9D%BF.xlsx")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/api/export-unmatched":
            try:
                size = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(size) or b"{}")
            except (ValueError, json.JSONDecodeError):
                json_response(self, {"error": "请求数据格式不正确"}, HTTPStatus.BAD_REQUEST)
                return
            if not isinstance(body, dict):
                body = {}
            try:
                unmatched = body.get("unmatched") if isinstance(body.get("unmatched"), list) else []
                available_names = body.get("available_names") if isinstance(body.get("available_names"), list) else []
                data = _generate_unmatched_report(unmatched, available_names)
            except RuntimeError as exc:
                json_response(self, {"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%E6%9C%AA%E5%8C%B9%E9%85%8D%E8%AE%B0%E5%BD%95.xlsx")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/api/analyze-batch":
            try:
                size = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(size) or b"{}")
            except (ValueError, json.JSONDecodeError):
                json_response(self, {"error": "请求数据格式不正确"}, HTTPStatus.BAD_REQUEST)
                return
            if not isinstance(body, dict):
                body = {}
            publish_state = load_publish_state()
            if queue_mutation_locked(publish_state) and not load_analysis_state().get("running"):
                # A running/paused browser publisher owns the queue.  Do not
                # start a batch that would inspect files while it is changing.
                json_response(self, {
                    "error": "自动发布正在运行或暂停中，请先完成或停止发布任务",
                    "state": publish_state,
                }, HTTPStatus.CONFLICT)
                return
            requested_ids = body.get("ids")
            if requested_ids is not None and not isinstance(requested_ids, list):
                json_response(self, {"error": "ids 必须是数组"}, HTTPStatus.BAD_REQUEST)
                return
            payload, status = start_analysis_batch(requested_ids)
            json_response(self, payload, status)
            return
        if parsed.path == "/api/analyze-cancel":
            payload, status = cancel_analysis_batch()
            json_response(self, payload, status)
            return
        if parsed.path == "/api/analyze-apply":
            try:
                size = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(size) or b"{}")
            except (ValueError, json.JSONDecodeError):
                json_response(self, {"error": "请求数据格式不正确"}, HTTPStatus.BAD_REQUEST)
                return
            if not isinstance(body, dict):
                body = {}
            requested_ids = body.get("ids")
            if requested_ids is not None and not isinstance(requested_ids, list):
                json_response(self, {"error": "ids 必须是数组"}, HTTPStatus.BAD_REQUEST)
                return
            payload, status = apply_batch_recommendations(requested_ids)
            json_response(self, payload, status)
            return
        if parsed.path in {"/api/publish-pause", "/api/publish/pause"}:
            payload, status = publish_control("pause")
            json_response(self, payload, status)
            return
        if parsed.path in {
            "/api/publish-stop", "/api/publish/stop",
            "/api/publish-terminate", "/api/publish/terminate",
        }:
            payload, status = publish_control("stop")
            json_response(self, payload, status)
            return
        if parsed.path in {"/api/publish-resume", "/api/publish/resume"}:
            payload, status = publish_control("resume")
            json_response(self, payload, status)
            return
        if parsed.path == "/api/publish":
            state = load_publish_state()
            if state.get("running"):
                json_response(self, {"error": "自动发布已经在运行"}, HTTPStatus.CONFLICT)
                return
            analysis_state = load_analysis_state()
            if analysis_state.get("running"):
                json_response(self, {
                    "error": "批量识别正在进行，请等待识别完成或停止后再发布",
                    "analysis": analysis_state,
                }, HTTPStatus.CONFLICT)
                return
            queue_items = load_queue()
            restart_after_termination = bool(state.get("terminated") and not state.get("running"))
            if restart_after_termination:
                # 终止意味着放弃当前浏览器草稿。下一次点击按钮应从队列
                # 重新开始上传，而不是隐式进入 --resume 草稿恢复路径。
                changed = False
                for item in queue_items:
                    if item.get("status") == "publishing":
                        item["status"] = "pending"
                        item["note"] = "上次发布已终止，重新开始时将重新上传"
                        item["updated_at"] = now()
                        changed = True
                if changed:
                    save_queue(queue_items)
            work_items = [item for item in queue_items if item.get("status") in {"pending", "failed", "publishing"}]
            if not work_items:
                json_response(self, {"error": "没有待发布视频"}, HTTPStatus.BAD_REQUEST)
                return
            panorama_error = validate_queue_panorama(work_items)
            if panorama_error:
                json_response(self, {
                    "error": f"全景视频配置有误：{panorama_error}",
                    "state": state,
                }, HTTPStatus.BAD_REQUEST)
                return
            schedule_error = validate_queue_schedule(work_items)
            if schedule_error:
                json_response(self, {
                    "error": f"定时发布配置有误：{schedule_error}",
                    "state": state,
                }, HTTPStatus.BAD_REQUEST)
                return
            resume = (not restart_after_termination) and any(item.get("status") == "publishing" for item in work_items)
            clear_publish_pause()
            clear_publish_stop()
            save_publish_state({"running": True, "paused": False, "pause_requested": False, "stop_requested": False, "stop_supported": False, "terminated": False, "auto_paused": False, "pause_source": "", "pause_reason": "", "pause_kind": "", "message": "正在启动浏览器自动发布", "current": "", "completed": 0, "total": len(work_items)})
            python_bin = ROOT / ".venv" / "bin" / "python"
            if not python_bin.exists():
                python_bin = Path(sys.executable)
            log_file = STATE_DIR / "publisher.log"
            try:
                with log_file.open("ab") as log:
                    command = [str(python_bin), str(ROOT / "browser_publisher.py")]
                    if resume:
                        command.append("--resume")
                    env = {k: v for k, v in os.environ.items() if k not in ("NODE_OPTIONS", "NODE_PATH")}
                    env["PYTHONUNBUFFERED"] = "1"
                    process = subprocess.Popen(command, cwd=str(ROOT), stdout=log, stderr=log, start_new_session=True, env=env)
            except (OSError, subprocess.SubprocessError) as exc:
                # Do not leave a failed launch stuck in running=true with no
                # PID; that would lock queue editing and make the stop button
                # unable to recover the task.
                clear_publish_pause()
                clear_publish_stop()
                failed_state = {
                    "running": False,
                    "paused": False,
                    "pause_requested": False,
                    "stop_requested": False,
                    "stop_supported": False,
                    "terminated": False,
                    "auto_paused": True,
                    "pause_source": "automatic",
                    "pause_reason": f"自动发布进程启动失败：{exc}",
                    "pause_kind": "process_error",
                    "pause_event_id": uuid.uuid4().hex,
                    "paused_at": now(),
                    "message": f"自动发布进程启动失败：{exc}",
                    "action_required": True,
                }
                save_publish_state(failed_state)
                json_response(self, {"error": failed_state["message"], "state": failed_state}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            record_publisher_pid(process)
            reap_background_process(process)
            json_response(self, {"ok": True, "state": load_publish_state()})
            return
        if parsed.path == "/api/publish-check":
            state = load_publish_state()
            if state.get("running"):
                json_response(self, {"error": "自动发布或检查已经在运行"}, HTTPStatus.CONFLICT)
                return
            analysis_state = load_analysis_state()
            if analysis_state.get("running"):
                json_response(self, {
                    "error": "批量识别正在进行，请等待识别完成或停止后再检查发布环境",
                    "analysis": analysis_state,
                }, HTTPStatus.CONFLICT)
                return
            clear_publish_pause()
            clear_publish_stop()
            save_publish_state({"running": True, "paused": False, "pause_requested": False, "stop_requested": False, "stop_supported": False, "terminated": False, "auto_paused": False, "pause_source": "", "pause_reason": "", "pause_kind": "", "message": "正在检查浏览器登录态和页面控件", "current": "", "completed": 0, "total": 0, "action_required": False})
            python_bin = ROOT / ".venv" / "bin" / "python"
            if not python_bin.exists():
                python_bin = Path(sys.executable)
            log_file = STATE_DIR / "publisher.log"
            try:
                with log_file.open("ab") as log:
                    env = {k: v for k, v in os.environ.items() if k not in ("NODE_OPTIONS", "NODE_PATH")}
                    env["PYTHONUNBUFFERED"] = "1"
                    process = subprocess.Popen([str(python_bin), str(ROOT / "browser_publisher.py"), "--check"], cwd=str(ROOT), stdout=log, stderr=log, start_new_session=True, env=env)
            except (OSError, subprocess.SubprocessError) as exc:
                clear_publish_pause()
                clear_publish_stop()
                failed_state = {
                    "running": False,
                    "paused": False,
                    "pause_requested": False,
                    "stop_requested": False,
                    "stop_supported": False,
                    "terminated": False,
                    "auto_paused": True,
                    "pause_source": "automatic",
                    "pause_reason": f"自动发布检查进程启动失败：{exc}",
                    "pause_kind": "process_error",
                    "pause_event_id": uuid.uuid4().hex,
                    "paused_at": now(),
                    "message": f"自动发布检查进程启动失败：{exc}",
                    "action_required": True,
                }
                save_publish_state(failed_state)
                json_response(self, {"error": failed_state["message"], "state": failed_state}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            record_publisher_pid(process)
            reap_background_process(process)
            json_response(self, {"ok": True, "state": load_publish_state()})
            return
        if parsed.path == "/api/import-captions":
            if queue_mutation_locked(load_publish_state()):
                json_response(self, {"error": "自动发布正在运行或暂停中，暂不能导入文案"}, HTTPStatus.CONFLICT)
                return
            file_bytes, filename, error = _parse_multipart_upload(self)
            if error:
                json_response(self, {"error": error}, HTTPStatus.BAD_REQUEST)
                return
            if not file_bytes:
                json_response(self, {"error": "未读取到文件内容"}, HTTPStatus.BAD_REQUEST)
                return
            if openpyxl is None:
                json_response(self, {"error": "缺少 openpyxl，请安装：pip install openpyxl"}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            try:
                rows = _parse_caption_xlsx(file_bytes)
            except Exception as exc:
                json_response(self, {"error": f"解析 Excel 失败：{exc}"}, HTTPStatus.BAD_REQUEST)
                return
            json_response(self, {"rows": rows, "filename": filename})
            return
        if parsed.path == "/api/scan":
            state = load_publish_state()
            if queue_mutation_locked(state):
                json_response(self, {
                    "error": "自动发布正在运行或暂停中，请先完成当前任务后再扫描",
                    "state": state,
                }, HTTPStatus.CONFLICT)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(size) or b"{}")
            except (ValueError, json.JSONDecodeError):
                json_response(self, {"error": "请求数据格式不正确"}, HTTPStatus.BAD_REQUEST)
                return
            if not isinstance(body, dict):
                body = {}
            try:
                default_type, default_format = normalize_selection_profile(
                    body.get("video_type", "normal"), body.get("panorama_format", "")
                )
                items = scan_videos(
                    default_video_type=default_type,
                    default_panorama_format=default_format,
                )
            except ValueError as exc:
                json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            json_response(self, {"items": public_queue_items(items), "root": str(MEDIA_ROOT)})
            return
        if parsed.path == "/api/select-folder":
            state = load_publish_state()
            if queue_mutation_locked(state):
                json_response(self, {
                    "error": "自动发布正在运行或暂停中，请先完成当前任务后再选择文件夹",
                    "state": state,
                }, HTTPStatus.CONFLICT)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(size) or b"{}")
            except (ValueError, json.JSONDecodeError):
                json_response(self, {"error": "请求数据格式不正确"}, HTTPStatus.BAD_REQUEST)
                return
            if not isinstance(body, dict):
                body = {}
            try:
                default_type, default_format = normalize_selection_profile(
                    body.get("video_type", "normal"), body.get("panorama_format", "")
                )
            except ValueError as exc:
                json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            selected = choose_media_folder()
            if selected is None or not selected.is_dir():
                json_response(self, {"cancelled": True, "root": str(MEDIA_ROOT), "items": public_queue_items(load_queue())})
                return
            MEDIA_ROOT = selected
            _ensure_state_dir()
            MEDIA_ROOT_FILE.write_text(str(MEDIA_ROOT), encoding="utf-8")
            json_response(self, {
                "cancelled": False,
                "root": str(MEDIA_ROOT),
                "items": public_queue_items(scan_videos(
                    default_video_type=default_type,
                    default_panorama_format=default_format,
                )),
            })
            return
        if parsed.path in {"/api/select-files", "/api/select-videos"}:
            # 只读取很小的类型配置；视频仍留在原位置，由原生文件选择器
            # 返回路径，不会把大文件复制/上传到本地服务。
            try:
                size = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(size) or b"{}")
            except (ValueError, json.JSONDecodeError):
                json_response(self, {"error": "请求数据格式不正确"}, HTTPStatus.BAD_REQUEST)
                return
            if not isinstance(body, dict):
                body = {}
            payload, status = select_media_files(
                default_video_type=body.get("video_type", "normal"),
                default_panorama_format=body.get("panorama_format", ""),
            )
            json_response(self, payload, status)
            return
        if parsed.path in {"/api/clear-queue", "/api/queue/clear"}:
            payload, status = clear_queue()
            json_response(self, payload, status)
            return
        if parsed.path in {"/api/items/delete", "/api/queue/delete"}:
            try:
                size = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(size) or b"{}")
            except (ValueError, json.JSONDecodeError):
                json_response(self, {"error": "请求数据格式不正确"}, HTTPStatus.BAD_REQUEST)
                return
            payload, status = delete_queue_items(body.get("ids") if isinstance(body, dict) else None)
            json_response(self, payload, status)
            return
        if parsed.path == "/api/schedule-plan":
            try:
                size = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(size) or b"{}")
            except (ValueError, json.JSONDecodeError):
                json_response(self, {"error": "请求数据格式不正确"}, HTTPStatus.BAD_REQUEST)
                return
            payload, status = update_schedule_plan(
                body.get("updates") if isinstance(body, dict) else None
            )
            json_response(self, payload, status)
            return
        if parsed.path == "/api/open":
            # 兼容前端使用 POST 打开页面。
            self.do_GET()
            return
        json_response(self, {"error": "未找到接口"}, HTTPStatus.NOT_FOUND)

    def do_PATCH(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/items/"):
            json_response(self, {"error": "未找到接口"}, HTTPStatus.NOT_FOUND)
            return
        state = load_publish_state()
        if queue_mutation_locked(state):
            json_response(self, {
                "error": "自动发布正在运行或暂停中，暂不能修改队列视频",
                "state": state,
            }, HTTPStatus.CONFLICT)
            return
        item_id = unquote(parsed.path.removeprefix("/api/items/"))
        try:
            size = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(size) or b"{}")
        except (ValueError, json.JSONDecodeError):
            json_response(self, {"error": "请求数据格式不正确"}, HTTPStatus.BAD_REQUEST)
            return
        if not isinstance(payload, dict):
            json_response(self, {"error": "请求数据必须是对象"}, HTTPStatus.BAD_REQUEST)
            return
        items = load_queue()
        for item in items:
            if str(item.get("id") or item.get("path", "")) == item_id:
                # Normalize against the *final* merged item, so switching a
                # card to scheduled is atomic from the browser's perspective
                # (mode and datetime can be sent in one PATCH), while old
                # queue records without schedule fields remain immediate.
                candidate_mode = payload.get("schedule_mode", item.get("schedule_mode", "immediate"))
                candidate_at = payload.get("schedule_at", item.get("schedule_at", ""))
                candidate_interval = payload.get(
                    "schedule_interval_minutes",
                    item.get("schedule_interval_minutes", ""),
                )
                try:
                    normalized_mode, normalized_at, normalized_interval = validate_schedule_fields(
                        candidate_mode, candidate_at, candidate_interval
                    )
                except ValueError as exc:
                    json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                if {"schedule_mode", "schedule_at", "schedule_interval_minutes"}.intersection(payload):
                    try:
                        validate_schedule_future(normalized_mode, normalized_at)
                    except ValueError as exc:
                        json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                        return
                for key in (
                    "caption", "hashtags", "cover_tsp", "cover_image", "cover_image_name", "cover_image_landscape", "cover_image_landscape_name", "cover_image_portrait", "cover_image_portrait_name", "visibility", "allow_download", "note", "status",
                    "video_type", "panorama_format", "panorama_check_status",
                    "official_topic_auto", "official_topic_keywords", "pico_enabled",
                    "work_title", "collection_name", "self_declaration", "location", "hot_topics",
                    "platforms",
                    "schedule_mode", "schedule_at", "schedule_interval_minutes",
                ):
                    if key in payload:
                        if key == "video_type":
                            try:
                                value = normalize_video_type(payload[key])
                            except ValueError as exc:
                                json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                                return
                        elif key == "panorama_format":
                            try:
                                value = normalize_panorama_format(payload[key])
                            except ValueError as exc:
                                json_response(self, {"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                                return
                        elif key == "panorama_check_status":
                            value = str(payload[key] or "unknown").strip().lower()
                            if value not in {"unknown", "hint", "pass", "warn", "fail"}:
                                value = "unknown"
                        elif key == "official_topic_auto":
                            value = normalize_bool(payload[key], default=normalize_bool(item.get(key, True), default=True))
                        elif key == "official_topic_keywords":
                            value = normalize_official_topic_keywords(payload[key])
                        elif key == "pico_enabled":
                            value = normalize_bool(payload[key], default=normalize_bool(item.get(key, False)))
                        elif key == "work_title":
                            value = normalize_queue_text(payload[key], 30)
                        elif key == "cover_image":
                            value = str(payload[key] or "").strip()
                            if value and safe_cover_path(value) is None:
                                json_response(self, {"error": "自定义封面文件无效或已不存在"}, HTTPStatus.BAD_REQUEST)
                                return
                            if not value:
                                remove_cover_file(item.get("cover_image"))
                                item["cover_image_name"] = ""
                        elif key in {"cover_image_landscape", "cover_image_portrait"}:
                            value = str(payload[key] or "").strip()
                            if value and safe_cover_path(value) is None:
                                json_response(self, {"error": "自定义封面文件无效或已不存在"}, HTTPStatus.BAD_REQUEST)
                                return
                            if not value:
                                remove_cover_file(item.get(key))
                                item[f"{key}_name"] = ""
                        elif key in {"cover_image_name", "cover_image_landscape_name", "cover_image_portrait_name"}:
                            value = normalize_queue_text(payload[key], 200)
                        elif key == "collection_name":
                            value = normalize_queue_text(payload[key], 120)
                        elif key == "self_declaration":
                            value = normalize_queue_text(payload[key], 200)
                        elif key == "location":
                            value = normalize_queue_text(payload[key], 120)
                        elif key == "hot_topics":
                            value = normalize_queue_text(payload[key], 500)
                        elif key == "platforms":
                            value = normalize_platform_states(payload[key], item)
                        elif key == "schedule_mode":
                            value = normalized_mode
                        elif key == "schedule_at":
                            value = normalized_at
                        elif key == "schedule_interval_minutes":
                            value = normalized_interval
                        else:
                            value = payload[key] if key == "allow_download" else str(payload[key]).strip()
                        if key == "status" and value not in {"pending", "publishing", "published", "skipped", "failed"}:
                            continue
                        item[key] = value
                if normalize_video_type(item.get("video_type", "normal")) == "normal":
                    # A stale format must not be reused if the user switches
                    # the card back to ordinary video publishing.
                    item["panorama_format"] = ""
                # A mode-only switch to immediate must also discard a stale
                # appointment.  This is intentionally done after the whitelist
                # loop so the UI cannot accidentally leave an old date active.
                if "schedule_mode" in payload and normalized_mode == "immediate":
                    item["schedule_at"] = ""
                    item["schedule_interval_minutes"] = ""
                item["updated_at"] = now()
                save_queue(items)
                json_response(self, queue_item_with_panorama_profile(item))
                return
        json_response(self, {"error": "没有找到这条视频"}, HTTPStatus.NOT_FOUND)

    def serve_static(self, request_path: str) -> None:
        if request_path == "/README.md":
            target = ROOT / "README.md"
        else:
            relative = "index.html" if request_path in {"", "/"} else request_path.removeprefix("/")
            target = (STATIC_DIR / relative).resolve()
        try:
            target.relative_to(ROOT)
        except ValueError:
            target = STATIC_DIR / "index.html"
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        # 本地工具会在开发/修复过程中更新前端脚本；禁止浏览器继续使用
        # 旧的 app.js/style.css，避免用户看到旧按钮或旧的发布控制逻辑。
        if target.name in {"index.html", "app.js", "style.css"}:
            self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    parser = argparse.ArgumentParser(description="启动本地抖音批量发布助手")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    # 发布器与 Web 服务是两个进程。重启 Web 服务时不要重新扫描并覆盖
    # 正在发布/暂停的队列，尤其不能把浏览器草稿中的当前条目改回待发布。
    # 队列文件已经存在时（包括用户明确清空后的 []）也保持当前选择，
    # 只有首次启动、尚未建立队列文件时才自动扫描；之后可通过“重新扫描视频”
    # 或“选择视频”显式刷新/重建队列。
    publish_snapshot = load_publish_state()
    if publish_snapshot.get("running"):
        print("检测到自动发布仍在运行，启动时保留现有队列；需要刷新时请点击“重新扫描视频”。")
    elif not QUEUE_FILE.exists():
        scan_videos()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"视频发布助手已启动：{url}")
    print("请在浏览器打开该地址；按 Ctrl+C 停止。")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
