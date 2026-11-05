#!/usr/bin/env python3
"""微信视频号独立批量发布器。

登录、验证码、风控和页面改版需要人工处理；工具只在确认页面控件和提交结果后更新状态。
"""
from __future__ import annotations
import argparse, asyncio, json, os, re, signal, subprocess, time
from pathlib import Path
from datetime import datetime
from typing import Any
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import app

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / ".channels_batch"
QUEUE_FILE = STATE_DIR / "queue.json"
STATE_FILE = STATE_DIR / "publish_state.json"
PROFILE_DIR = ROOT / ".channels_browser_profile"
CREATE_URL = "https://channels.weixin.qq.com/platform/post/create"
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm"}

# 本轮是否在跑共享队列（由抖音队列触发）。不能用状态文件里的 mode 判断：
# 上一轮共享发布留下的 mode=shared_queue 会让独立发布把进度写到错误的文件。
_SHARED_MODE = False
CHANNELS_SHORT_TITLE_LIMIT = 16
CHANNELS_SHORT_TITLE_MIN = 6


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def set_channels_stage(
    item: dict,
    message: str,
    *,
    stage: int | None = None,
    percent: float | None = None,
    elapsed: int | None = None,
) -> None:
    """Update the stage/progress message for both shared and independent runs.

    ``stage``/``percent`` 让页面能画出“当前视频做到哪一步”，而不是只等整条
    完成才动一次进度条。
    """
    extra = ""
    if percent is not None:
        extra += f" {int(percent)}%"
    if elapsed is not None:
        extra += f" · 已用 {elapsed // 60}:{elapsed % 60:02d}"
    name = str(item.get("name") or "")
    text = f"{message}{extra}：{name}" if name else f"{message}{extra}"
    try:
        if _SHARED_MODE:
            state = app.load_channels_state()
            state["message"] = text
            if stage is not None:
                state["stage"] = stage
            state["stage_percent"] = percent
            app.save_channels_state(state)
            return
        independent = load_json(STATE_FILE, {})
        independent["message"] = text
        independent["current"] = name
        if stage is not None:
            independent["stage"] = stage
        independent["stage_percent"] = percent
        save_json(STATE_FILE, independent)
    except Exception:
        pass


def _profile_chrome_pids() -> list[int]:
    """Return Chrome processes using the dedicated Channels profile."""
    profile = str(PROFILE_DIR)
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    pids: list[int] = []
    for line in result.stdout.splitlines():
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


def _clear_profile_locks() -> None:
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        try:
            (PROFILE_DIR / name).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def close_stale_channels_chrome(reason: str = "") -> None:
    """Close a leftover Chrome that owns the profile, otherwise Playwright fails.

    Chrome refuses a second persistent context on the same profile: it opens a
    window in the existing session and Playwright gets TargetClosed before
    ``goto(CREATE_URL)``.  The profile still keeps the login; closing the stale
    window is safe and lets the new run navigate to the post page.
    """
    pids = _profile_chrome_pids()
    if not pids:
        _clear_profile_locks()
        return
    log(f"关闭占用视频号配置的旧 Chrome：{pids} {reason}".strip())
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(_pid_alive(pid) for pid in pids):
        time.sleep(0.2)
    for pid in pids:
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    _clear_profile_locks()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


async def launch_channels_context(pw, kwargs: dict):
    """Launch the persistent Channels context, reclaiming the profile once."""
    for attempt in range(2):
        if attempt == 0:
            close_stale_channels_chrome("启动前")
        try:
            return await pw.chromium.launch_persistent_context(str(PROFILE_DIR), **kwargs)
        except Exception as exc:
            log(f"视频号 Chrome 启动失败（第 {attempt + 1} 次）：{exc}")
            if attempt == 0:
                close_stale_channels_chrome("启动失败后")
                await asyncio.sleep(2)
            else:
                raise


def sanitize_channels_title(item: dict) -> str:
    """Return a short title accepted by the Channels form.

    Channels rejects Chinese commas and several other separators in the short
    title.  Douyin titles use ``，``/``丨`` as a song/date separator, so convert
    those to spaces instead of sending an invalid value that disables 发表.
    """
    raw = str(item.get("title") or item.get("work_title") or "").strip()
    if not raw:
        raw = Path(str(item.get("name") or "")).stem
    text = re.sub(r"[丨|，,、/\\]+", " ", raw)
    # Keep Chinese/letters/digits, spaces, dots and the punctuation Channels
    # explicitly allows in the short title.
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff .《》“”\"'‘’：:＋+？?%℃]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or "演唱会现场"


def split_channels_title(title: str) -> tuple[str, str]:
    """Split a long title into song name and trailing date/location text.

    The short title should keep the song name; the trailing date/venue/other
    text is prepended to the description when the platform rejects the full
    title for exceeding the character limit.
    """
    text = str(title or "").strip()
    if not text:
        return "", ""
    date_match = re.search(r"(?<!\d)(20\d{2}[年./-]\d{1,2}[月./-]\d{1,2}日?.*)$", text)
    if date_match and date_match.start() > 0:
        song = text[:date_match.start()].strip(" ，,丨|、")
        extra = text[date_match.start():].strip()
        if song:
            return song, extra
    parts = re.split(r"[，,丨|、]", text, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return text, ""


def fit_channels_short_title(title: str) -> tuple[str, str]:
    """Fit a title into the platform's 6-16 character short-title window.

    Keep the song name when it is long enough.  If the song name is shorter
    than 6 characters but the whole title is longer than 16, take the first 6
    characters as the short title and return the remaining text for the front
    of the description (as requested).
    """
    text = str(title or "").strip()
    song, extra = split_channels_title(text)
    if len(text) <= CHANNELS_SHORT_TITLE_LIMIT:
        return text, ""
    if len(song) >= CHANNELS_SHORT_TITLE_MIN and extra:
        return song, extra
    if len(text) > CHANNELS_SHORT_TITLE_MIN:
        prefix = text[:CHANNELS_SHORT_TITLE_MIN]
        suffix = text[CHANNELS_SHORT_TITLE_MIN:].strip()
        if prefix.strip() and suffix:
            return prefix, suffix
    return song or text, extra


async def channels_short_title_error(page) -> bool:
    """Return True when Channels shows a short-title length error."""
    markers = ("字数超出", "超出字数", "字数超", "不能超过", "最多输入", "标题过长", "超过限制", "字数超过")
    for frame in page.frames:
        try:
            text = await frame.locator("body").first.inner_text()
        except Exception:
            continue
        if any(marker in text for marker in markers):
            if "标题" in text or "短标题" in text:
                return True
        # 视频号实际提示形如“标题超过16字限制”“标题不能超过16个字”。
        if re.search(r"标题.{0,12}(?:超过|超出|不能超过).{0,12}\d+\s*字", text):
            return True
        if re.search(r"超过\s*\d+\s*字(?:限制|以内|上限)?", text):
            return True
        if re.search(r"少于\s*\d+\s*字", text):
            return True
    return False


def load_json(path: Path, default: Any):
    try: return json.loads(path.read_text(encoding="utf-8"))
    except Exception: return default


def save_json(path: Path, value: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def normalize_item(item: dict) -> dict:
    item = dict(item)
    item.setdefault("id", item.get("path") or item.get("name"))
    item.setdefault("name", Path(item.get("path", "")).name)
    item.setdefault("caption", "")
    item.setdefault("hashtags", [])
    item.setdefault("visibility", "公开")
    item.setdefault("schedule_mode", "immediate")
    item.setdefault("schedule_at", "")
    item.setdefault("status", "pending")
    item.setdefault("message", "")
    item.setdefault("updated_at", "")
    return item


def load_queue() -> list[dict]:
    return [normalize_item(x) for x in load_json(QUEUE_FILE, []) if isinstance(x, dict)]


def save_queue(items: list[dict]): save_json(QUEUE_FILE, items)


def set_state(**kwargs):
    state = load_json(STATE_FILE, {"running": False, "paused": False, "current": "", "completed": 0, "total": 0, "message": ""})
    state.update(kwargs); save_json(STATE_FILE, state)


def text_for(item: dict) -> str:
    caption = str(item.get("caption") or "").strip()
    tags = item.get("hashtags") or []
    if isinstance(tags, str): tags = re.findall(r"#[^#\s]+|[^,，\s]+", tags)
    tags = [str(x).strip() for x in tags if str(x).strip()]
    return (caption + ("\n" if caption and tags else "") + " ".join(x if x.startswith("#") else "#" + x for x in tags)).strip()


async def first_visible(page, selectors):
    for selector in selectors:
        loc = page.locator(selector).first
        try:
            if await loc.is_visible(): return loc
        except Exception: pass
    return None


async def success_marker_any(page) -> bool:
    for frame in page.frames:
        for text in ("已发表", "发表成功", "发布成功", "已发布"):
            try:
                loc = frame.get_by_text(text, exact=True)
                for i in range(await loc.count()):
                    if await loc.nth(i).is_visible(): return True
            except Exception: pass
    try:
        return any(token in page.url for token in ("/platform/manage", "/platform/content", "/platform/post/list"))
    except Exception: return False

async def visible_text_any(page, tokens):
    for frame in page.frames:
        try:
            text = (await frame.locator("body").first.inner_text()).strip()
        except Exception:
            continue
        if any(token in text for token in tokens): return text
    return ""

async def first_visible_any(page, selectors):
    for frame in page.frames:
        loc = await first_visible(frame, selectors)
        if loc is not None: return loc
    return None

async def fill_and_verify(page, selectors, value: str) -> bool:
    if not value: return False
    loc = await first_visible_any(page, selectors)
    if not loc: return False
    try:
        await loc.click()
        await loc.press("Meta+A")
        await loc.press_sequentially(value, delay=20)
        await loc.press("Tab")
        await page.wait_for_timeout(500)
    except Exception:
        return False
    try:
        tag = await loc.evaluate("e=>e.tagName")
        current = await loc.input_value() if tag in ("INPUT", "TEXTAREA") else await loc.inner_text()
        return value in str(current)
    except Exception:
        return False


async def fill_if_present(page, selectors, value: str) -> bool:
    if not value: return False
    loc = await first_visible_any(page, selectors)
    if not loc: return False
    try:
        await loc.fill(value); return True
    except Exception: return False


async def capture_diagnostics(page, reason: str):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        (STATE_DIR / "channels_page_text.txt").write_text((await page.locator("body").first.inner_text())[:20000], encoding="utf-8")
        await page.screenshot(path=str(STATE_DIR / "channels_page.png"), full_page=True)
        (STATE_DIR / "channels_page_reason.txt").write_text(reason, encoding="utf-8")
        debug=[]
        for frame in page.frames:
            try:
                vals=await frame.locator('input,textarea,[contenteditable="true"],[data-placeholder]').evaluate_all("els=>els.map(e=>({tag:e.tagName,ph:e.getAttribute('placeholder'),dp:e.getAttribute('data-placeholder'),value:e.value||e.innerText||'',disabled:e.disabled||false})).filter(x=>x.value||x.ph||x.dp)")
                btns=await frame.locator('button').evaluate_all("els=>els.map(e=>({text:(e.innerText||'').trim(),disabled:e.disabled,cls:e.className})).filter(x=>x.text)")
                debug.append({'url':frame.url,'fields':vals,'buttons':btns})
            except Exception: pass
        (STATE_DIR / "channels_dom_debug.json").write_text(json.dumps(debug,ensure_ascii=False,indent=2),encoding="utf-8")
    except Exception:
        pass

async def channels_upload_busy(page) -> tuple[bool, str]:
    """Detect upload/processing via visible DOM text across the Channels iframe.

    The upload progress lives inside an iframe/shadow-ish mount; ``body.inner_text``
    often only returns the outer shell, while ``53%``/``取消上传`` are still
    visible.  Use Playwright text locators per frame so we don't start editing a
    cover or description while the video is still uploading.
    """
    for frame in page.frames:
        for token in ("取消上传", "文件上传中", "上传中", "正在上传", "处理中", "解析中", "校验中"):
            try:
                loc = frame.get_by_text(token, exact=False)
                for index in range(await loc.count()):
                    if await loc.nth(index).is_visible():
                        return True, token
            except Exception:
                continue
        try:
            pct = frame.get_by_text(re.compile(r"^\d{1,3}%$"))
            for index in range(await pct.count()):
                if await pct.nth(index).is_visible():
                    return True, "progress"
        except Exception:
            continue
    return False, ""


async def channels_upload_percent(page) -> int | None:
    """读取视频号上传/处理进度里的百分比；读不到时返回 None。"""
    pattern = re.compile(r"^(\d{1,3})%$")
    for frame in page.frames:
        try:
            loc = frame.get_by_text(pattern)
            for index in range(await loc.count()):
                node = loc.nth(index)
                if not await node.is_visible():
                    continue
                match = pattern.match((await node.inner_text()).strip())
                if match:
                    return max(0, min(100, int(match.group(1))))
        except Exception:
            continue
    return None


async def wait_channels_form_ready(page, timeout_seconds: int = 600) -> None:
    """Wait until upload/processing finishes and the 发表 button is enabled.

    The cover entry and description fields are mounted before the video is fully
    uploaded; clicking 编辑 then only shows an upload toast.  The enabled 发表
    button is the reliable signal that the cover entry can be used.
    """
    for _ in range(timeout_seconds):
        busy, _ = await channels_upload_busy(page)
        publish = await first_visible_any(page, ['button:has-text("发表")', 'button:has-text("发布")'])
        if publish is not None and not busy:
            try:
                if await publish.is_enabled():
                    return
            except Exception:
                pass
        await page.wait_for_timeout(1000)
    raise RuntimeError("视频号上传/处理未完成，发表按钮一直不可用，浏览器窗口已保留")


async def _visible_text(page, text: str):
    for frame in page.frames:
        try:
            loc = frame.get_by_text(text, exact=False)
            for index in range(await loc.count()):
                if await loc.nth(index).is_visible():
                    return loc.nth(index)
        except Exception:
            continue
    return None


async def _cover_dialog_kind(page) -> str:
    for text in ("编辑封面", "编辑个人主页卡片", "编辑分享卡片"):
        if await _visible_text(page, text):
            return text
    return ""


async def _visible_cover_edits(page) -> list:
    edits = []
    for frame in page.frames:
        try:
            loc = frame.get_by_text("编辑", exact=True)
            for index in range(await loc.count()):
                if await loc.nth(index).is_visible():
                    box = await loc.nth(index).bounding_box()
                    if box:
                        edits.append((box["x"], loc.nth(index)))
        except Exception:
            continue
    edits.sort(key=lambda pair: pair[0])
    return [loc for _, loc in edits]


async def _cover_dialog_ready(page) -> bool:
    has_image = False
    for frame in page.frames:
        try:
            if await frame.locator('input[type=file][accept*="image"]').count():
                has_image = True
                break
        except Exception:
            continue
    if not has_image:
        return False
    for frame in page.frames:
        try:
            btn = frame.get_by_role("button", name=re.compile("确认"))
            for index in range(await btn.count()):
                if await btn.nth(index).is_visible():
                    return True
        except Exception:
            continue
    return False


async def _fill_cover_dialog(page, cover: Path) -> None:
    image_input = None
    for frame in page.frames:
        try:
            loc = frame.locator('input[type=file][accept*="image"]')
            if await loc.count():
                image_input = loc.last
                break
        except Exception:
            continue
    if image_input is None:
        raise RuntimeError("视频号封面弹窗未找到图片上传控件，浏览器窗口已保留")
    await image_input.set_input_files(str(cover))
    await page.wait_for_timeout(3000)
    for attempt in range(25):
        # Pick the lowest visible 确认 button: the cover dialog's footer button is
        # below the page-level controls, while unrelated hidden confirm buttons
        # can appear earlier in the DOM and do nothing.
        buttons = []
        for frame in page.frames:
            try:
                candidates = [
                    frame.get_by_role("button", name=re.compile("确认")),
                    frame.get_by_text("确认", exact=False),
                ]
                for group in candidates:
                    for index in range(await group.count()):
                        btn = group.nth(index)
                        if not await btn.is_visible():
                            continue
                        box = await btn.bounding_box()
                        if box:
                            buttons.append((box["y"], box["x"], btn))
            except Exception:
                continue
        buttons.sort(key=lambda row: (row[0], row[1]))
        if buttons:
            try:
                await buttons[-1][2].click(force=True)
                await page.wait_for_timeout(1500)
            except Exception:
                pass
        # A real cover confirmation closes the editor and updates the preview.
        if not await _cover_dialog_kind(page):
            return
        for frame in page.frames:
            try:
                toast = frame.get_by_text("封面已更新", exact=False)
                if await toast.count() and await toast.first.is_visible():
                    return
            except Exception:
                continue
        await page.wait_for_timeout(1000)
    raise RuntimeError("视频号封面确认未生效，浏览器窗口已保留")

async def upload_channels_cover(page, item: dict) -> str:
    """Upload matching 3:4 and 4:3 covers after video upload finishes.

    Channels has two layouts:
      * one combined "编辑封面" dialog, where one image feeds both cards;
      * separate "编辑个人主页卡片" / "编辑分享卡片" dialogs for horizontal video.
    Wait for the cover entry (it only appears after upload), use portrait for the
    3:4 card and landscape for the 4:3 card, and confirm each dialog.
    """
    portrait = app.safe_cover_path(str(item.get("cover_image_portrait") or ""))
    landscape = app.safe_cover_path(str(item.get("cover_image_landscape") or ""))
    if portrait is None and landscape is None:
        log("视频号封面：本条没有可用的自定义封面，保留视频帧")
        return ""
    edits = []
    for _ in range(90):
        edits = await _visible_cover_edits(page)
        if edits:
            break
        await page.wait_for_timeout(1000)
    if not edits:
        raise RuntimeError("视频号封面编辑入口在视频上传完成后仍未出现，浏览器窗口已保留")
    await edits[0].scroll_into_view_if_needed()
    await edits[0].click(force=True)
    await page.wait_for_timeout(1200)
    kind = ""
    for _ in range(12):
        kind = await _cover_dialog_kind(page)
        if kind:
            break
        try:
            await edits[0].click(force=True)
        except Exception:
            pass
        await page.wait_for_timeout(1000)
    if not kind:
        raise RuntimeError("视频号封面弹窗未打开，浏览器窗口已保留")
    if kind == "编辑封面":
        cover = portrait or landscape
        await _fill_cover_dialog(page, cover)
        log(f"视频号封面已上传（合并卡片）：{cover.name}")
        return cover.name
    # Separate cards: first 3:4 personal card, then 4:3 share card.
    personal = portrait or landscape
    await _fill_cover_dialog(page, personal)
    log(f"视频号个人主页封面已上传：{personal.name}")
    if landscape is None:
        return personal.name
    await page.wait_for_timeout(1500)
    edits = await _visible_cover_edits(page)
    for _ in range(20):
        if len(edits) >= 2:
            break
        await page.wait_for_timeout(1000)
        edits = await _visible_cover_edits(page)
    if len(edits) < 2:
        raise RuntimeError("视频号分享卡片封面入口未出现，封面未完整，浏览器窗口已保留")
    shared_kind = ""
    for candidate in reversed(edits):
        try:
            await candidate.scroll_into_view_if_needed()
            await candidate.click(force=True)
        except Exception:
            continue
        await page.wait_for_timeout(1200)
        # 先设置 3:4 后再点 4:3，视频号会询问“使用此素材作为封面?”；
        # 必须点“直接编辑”进入 4:3 自己的上传页，不能直接用 3:4 裁切。
        if await _visible_text(page, "使用此素材作为封面"):
            direct = await _visible_text(page, "直接编辑")
            if direct:
                await direct.click(force=True)
                log("视频号分享卡片：选择直接编辑 4:3 封面")
                for _ in range(12):
                    await page.wait_for_timeout(1000)
                    if await _cover_dialog_ready(page):
                        await _fill_cover_dialog(page, landscape)
                        log(f"视频号分享卡片封面已上传：{landscape.name}")
                        return f"{personal.name},{landscape.name}"
        shared_kind = await _cover_dialog_kind(page)
        if shared_kind == "编辑分享卡片":
            break
        # 点错卡片时关闭弹窗，再试另一个。
        if shared_kind:
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            await page.wait_for_timeout(800)
        shared_kind = ""
    if shared_kind == "编辑分享卡片":
        await _fill_cover_dialog(page, landscape)
        log(f"视频号分享卡片封面已上传：{landscape.name}")
        return f"{personal.name},{landscape.name}"
    return personal.name


async def select_video_annotation(page) -> str:
    """Open the required 视频标注 dropdown and pick a no-annotation option."""
    trigger = None
    for selector in ('text=选择视频标注', '[placeholder="选择视频标注"]'):
        try:
            loc = page.locator(selector)
            for index in range(await loc.count()):
                if await loc.nth(index).is_visible():
                    trigger = loc.nth(index)
                    break
        except Exception:
            continue
        if trigger is not None:
            break
    for attempt in range(20):
        if trigger is None:
            for selector in ('text=选择视频标注', '[placeholder="选择视频标注"]'):
                try:
                    loc = page.locator(selector)
                    for index in range(await loc.count()):
                        if await loc.nth(index).is_visible():
                            trigger = loc.nth(index)
                            break
                except Exception:
                    continue
                if trigger is not None:
                    break
        if trigger is not None:
            try:
                await trigger.scroll_into_view_if_needed()
                await trigger.click(force=True)
            except Exception:
                pass
        await page.wait_for_timeout(900)
        for label in ("无需标注", "无标注", "不需要标注", "无"):
            for frame in page.frames:
                try:
                    opt = frame.get_by_text(label, exact=True)
                    for index in range(await opt.count()):
                        if await opt.nth(index).is_visible():
                            await opt.nth(index).click(force=True)
                            await page.wait_for_timeout(600)
                            log(f"视频号视频标注已选择：{label}")
                            return label
                except Exception:
                    continue
        trigger = None
        await page.wait_for_timeout(700)
    # Last resort: pick the first visible option so the required field is not
    # left empty (which disables/ignores 发表 on the current page).
    for frame in page.frames:
        try:
            opts = frame.locator('[role="option"]:visible, [class*="option"]:visible')
            for index in range(await opts.count()):
                text = " ".join((await opts.nth(index).inner_text()).split())
                if text:
                    await opts.nth(index).click(force=True)
                    await page.wait_for_timeout(600)
                    log(f"视频号视频标注已选择首个选项：{text[:30]}")
                    return text
        except Exception:
            continue
    return ""


async def check_channels_environment() -> dict:
    kwargs = {
        "headless": False,
        "args": ["--disable-blink-features=AutomationControlled", "--new-window", "--window-size=1440,1000", "--force-device-scale-factor=1", "--disable-extensions", "--disable-features=SidePanelV2,SidePanel"],
    }
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if chrome.is_file():
        kwargs["executable_path"] = str(chrome)
    async with async_playwright() as pw:
        context = await launch_channels_context(pw, kwargs)
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            try:
                await page.goto(CREATE_URL, wait_until="domcontentloaded", timeout=60000)
            except PlaywrightTimeoutError:
                await page.wait_for_timeout(3000)
            has_input = False
            has_collection = None
            for _ in range(20):
                await page.wait_for_timeout(1000)
                url = page.url
                login = "login" in url.lower()
                has_input = bool(await page.locator('input[type="file"]').count())
                has_collection = await _visible_text(page, "选择合集")
                if has_input and has_collection is not None:
                    break
                if login:
                    break
            url = page.url
            login = "login" in url.lower()
            ok = bool(has_input and has_collection is not None and not login)
            return {
                "ok": ok,
                "logged_in": not login,
                "has_upload": has_input,
                "has_publish_form": has_collection is not None,
                "url": url,
                "message": "视频号登录态正常，发布页控件可用" if ok else ("需要扫码登录视频号" if login else "视频号发布页控件未就绪"),
            }
        finally:
            await context.close()


async def list_channels_collections() -> list[dict]:
    """Read the account's Channels collections from the publish form."""
    kwargs = {
        "headless": False,
        "args": ["--disable-blink-features=AutomationControlled", "--new-window", "--window-size=1440,1000", "--force-device-scale-factor=1", "--disable-extensions", "--disable-features=SidePanelV2,SidePanel"],
    }
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if chrome.is_file():
        kwargs["executable_path"] = str(chrome)
    async with async_playwright() as pw:
        context = await launch_channels_context(pw, kwargs)
        page = context.pages[0] if context.pages else await context.new_page()
        collection_payloads: list[dict] = []
        async def capture_collection_response(response):
            if "get_collection_list" in response.url:
                try:
                    collection_payloads.append(await response.json())
                except Exception:
                    pass
        page.on("response", lambda response: asyncio.create_task(capture_collection_response(response)))
        try:
            try:
                await page.goto(CREATE_URL, wait_until="domcontentloaded", timeout=60000)
            except PlaywrightTimeoutError:
                await page.wait_for_timeout(3000)
            await page.wait_for_timeout(2500)
            trigger = await _visible_text(page, "选择合集")
            if trigger is None:
                # The collection selector is reliably mounted after a video is
                # accepted; use a tiny local probe and never publish it.
                probe = ROOT / "工具测试_20261030" / "DiffusionBear" / "docs" / "model-arena" / "images" / "arena_flux2_vs_juggernaut.mp4"
                if probe.is_file():
                    inputs = page.locator('input[type="file"]')
                    if await inputs.count():
                        try:
                            await inputs.first.set_input_files(str(probe))
                            for _ in range(90):
                                busy, _ = await channels_upload_busy(page)
                                if not busy:
                                    break
                                await page.wait_for_timeout(1000)
                            await page.wait_for_timeout(2500)
                        except Exception:
                            pass
                trigger = await _visible_text(page, "选择合集")
            if trigger is not None:
                await trigger.click(force=True)
                await page.wait_for_timeout(1500)
            await page.wait_for_timeout(1500)
            found: dict[str, dict] = {}
            for payload in collection_payloads:
                data = payload.get("data") if isinstance(payload, dict) else None
                rows = data.get("collectionList") if isinstance(data, dict) else None
                for row in rows or []:
                    if not isinstance(row, dict):
                        continue
                    name = str(row.get("name") or "").strip()
                    if name and name not in {"选择合集", "创建新合集"}:
                        found[name] = {
                            "name": name,
                            "id": str(row.get("id") or ""),
                            "feed_count": row.get("feedCount"),
                        }
            if found:
                return list(found.values())
            # Fallback for page versions without the JSON response hook.
            for frame in page.frames:
                try:
                    loc = frame.get_by_text(re.compile(r"共\s*\d+\s*个内容"))
                    for index in range(await loc.count()):
                        if not await loc.nth(index).is_visible():
                            continue
                        try:
                            parent = loc.nth(index).locator("xpath=..")
                            text = " ".join((await parent.inner_text()).split())
                        except Exception:
                            text = " ".join((await loc.nth(index).inner_text()).split())
                        name = re.split(r"\s*共\s*\d+\s*个内容", text)[0].strip()
                        if name and name not in {"选择合集", "创建新合集"}:
                            found[name] = {"name": name, "raw": text}
                except Exception:
                    continue
            return list(found.values())
        finally:
            await context.close()


async def select_channels_collection(page, item: dict) -> str:
    """Select the configured collection on the publish form."""
    name = str(item.get("collection_name") or "").strip()
    if not name:
        return ""
    trigger = await _visible_text(page, "选择合集")
    if trigger is None:
        raise RuntimeError("视频号未找到合集选择入口，浏览器窗口已保留")
    await trigger.click(force=True)
    await page.wait_for_timeout(1000)
    for frame in page.frames:
        try:
            loc = frame.get_by_text(re.compile(rf"^{re.escape(name)}(?:\s|$)"), exact=False)
            for index in range(await loc.count()):
                if await loc.nth(index).is_visible():
                    await loc.nth(index).click(force=True)
                    await page.wait_for_timeout(800)
                    log(f"视频号合集已选择：{name}")
                    return name
        except Exception:
            continue
    raise RuntimeError(f"视频号合集中没有找到「{name}」，浏览器窗口已保留")


async def select_channels_schedule(page, item: dict) -> str:
    """Switch the publish form to scheduled mode and fill date + time via its calendar."""
    if str(item.get("schedule_mode") or "immediate") != "scheduled":
        return ""
    raw = str(item.get("schedule_at") or "").strip()
    if not raw:
        raise RuntimeError("视频号定时发布缺少时间，浏览器窗口已保留")
    try:
        target_dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise RuntimeError("视频号定时发布时间格式不正确，浏览器窗口已保留")
    timed = None
    for frame in page.frames:
        try:
            loc = frame.get_by_text("定时", exact=True)
            for index in range(await loc.count()):
                if await loc.nth(index).is_visible():
                    timed = loc.nth(index)
                    break
        except Exception:
            continue
        if timed is not None:
            break
    if timed is None:
        raise RuntimeError("视频号未找到定时发表选项，浏览器窗口已保留")
    await timed.click(force=True)
    await page.wait_for_timeout(800)
    field = page.locator('input[placeholder="请选择发表时间"], input[placeholder*="发表时间"]').first
    if not await field.count():
        raise RuntimeError("视频号定时时间输入框未出现，浏览器窗口已保留")
    await field.click(force=True)
    await page.wait_for_timeout(800)
    panel = page.locator('.weui-desktop-picker__panel:visible').last
    if not await panel.count():
        raise RuntimeError("视频号定时日期选择器未打开，浏览器窗口已保留")
    # Navigate the calendar to the target month.
    for _ in range(24):
        labels = panel.locator('.weui-desktop-picker__panel__label')
        texts = [(await labels.nth(i).inner_text()).strip() for i in range(await labels.count())]
        years = [int(re.sub(r"\D", "", x)) for x in texts if "年" in x and re.sub(r"\D", "", x)]
        months = [int(re.sub(r"\D", "", x)) for x in texts if "月" in x and re.sub(r"\D", "", x)]
        if not years or not months:
            break
        current = (years[0], months[0]); target = (target_dt.year, target_dt.month)
        if current == target:
            break
        cls = "weui-desktop-btn__icon__right" if current < target else "weui-desktop-btn__icon__left"
        arrow = panel.locator(f"button.{cls}").first
        if not await arrow.count() or not await arrow.is_visible():
            break
        await arrow.click(force=True)
        await page.wait_for_timeout(500)
    day = target_dt.day
    clicked = False
    for index in range(await panel.locator("a").count()):
        el = panel.locator("a").nth(index)
        try:
            text = (await el.inner_text()).strip()
        except Exception:
            continue
        classes = await el.get_attribute("class") or ""
        if text == str(day) and "disabled" not in classes and await el.is_visible():
            await el.click(force=True)
            clicked = True
            await page.wait_for_timeout(600)
            break
    if not clicked:
        raise RuntimeError(f"视频号定时日历中找不到 {day} 日，浏览器窗口已保留")
    time_field = page.locator('input[placeholder="请选择时间"], input[placeholder*="选择时间"]').first
    if await time_field.count():
        await time_field.click(force=True)
        await time_field.fill(target_dt.strftime("%H:%M"))
        await time_field.press("Tab")
        await page.wait_for_timeout(500)
    actual = await field.input_value()
    expected = target_dt.strftime("%Y-%m-%d %H:%M")
    if actual.strip() != expected:
        raise RuntimeError(f"视频号定时时间未生效（期望 {expected}，实际 {actual}），浏览器窗口已保留")
    log(f"视频号定时发布已设置：{expected}")
    return expected

async def confirm_channels_dialog(page) -> bool:
    """Click a post-publish confirmation dialog if Channels shows one."""
    for frame in page.frames:
        try:
            dialog = frame.locator('[role="dialog"]:visible, [class*="dialog"]:visible, [class*="modal"]:visible, [class*="popover"]:visible')
            if not await dialog.count():
                continue
            for text in ("确认发表", "确认发布", "继续发表", "确定发表", "确认", "确定", "继续"):
                btn = dialog.get_by_role("button", name=text, exact=True)
                if not await btn.count():
                    btn = dialog.get_by_text(text, exact=True)
                for index in range(await btn.count()):
                    if await btn.nth(index).is_visible():
                        await btn.nth(index).click(force=True)
                        await page.wait_for_timeout(1200)
                        log(f"视频号发表确认弹窗已点击：{text}")
                        return True
        except Exception:
            continue
    return False


async def channels_publish_succeeded(page) -> bool:
    if await visible_text_any(page, ("发表成功", "发布成功", "已发表", "已发布")):
        return True
    if await success_marker_any(page):
        return True
    try:
        for candidate in page.context.pages:
            url = candidate.url or ""
            if "/platform/post/list" in url or "/platform/manage" in url or "/platform/content" in url:
                return True
            text = await candidate.locator("body").first.inner_text()
            if any(token in text for token in ("发表成功", "发布成功", "已发表", "已发布")):
                return True
    except Exception:
        pass
    return False


async def publish_item(page, item: dict):
    video = Path(item["path"]).expanduser().resolve()
    if not video.is_file(): raise FileNotFoundError(video)
    if video.suffix.lower() not in VIDEO_EXTS: raise ValueError(f"不支持的视频格式: {video.suffix}")
    try:
        await page.goto(CREATE_URL, wait_until="domcontentloaded", timeout=60000)
    except PlaywrightTimeoutError:
        log("视频号发表页加载超时，等待后重试一次")
        await page.wait_for_timeout(5000)
        if "/post/create" not in page.url:
            await page.goto(CREATE_URL, wait_until="domcontentloaded", timeout=60000)
    for _ in range(30):
        try:
            if len(await page.content()) > 1000 and len(await page.locator("body").first.inner_text()) > 20:
                break
        except Exception:
            pass
        await page.wait_for_timeout(1000)
    if len(await page.content()) <= 1000:
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
    if "login" in page.url.lower() or await page.get_by_text("登录", exact=True).count():
        set_state(running=True, paused=False, current=item.get("name", ""), message="请在视频号窗口扫码登录，登录后将自动继续")
        try:
            await page.wait_for_url("**/platform/post/create**", timeout=300000)
        except Exception:
            raise RuntimeError("等待视频号扫码登录超时，浏览器窗口已保留")
        await page.wait_for_timeout(1500)
    # 清理页面上残留的旧预览/草稿，确保本次文件真正进入上传状态。
    old_delete = await first_visible_any(page, ['button:has-text("删除")','text=删除'])
    if old_delete:
        try: await old_delete.click(); await page.wait_for_timeout(800)
        except Exception: pass
    set_channels_stage(item, "步骤1/6 正在上传视频")
    log(f"步骤1/6 上传视频：{item.get('name', '')}")
    inputs = page.locator('input[type="file"]')
    button = await first_visible_any(page, ['button:has-text("上传视频")','button:has-text("选择视频")','text=上传视频','text=选择视频'])
    if button:
        try:
            async with page.expect_file_chooser(timeout=8000) as chooser:
                await button.click()
            await (await chooser.value).set_files(str(video))
        except Exception:
            if await inputs.count(): await inputs.first.set_input_files(str(video))
            else: raise RuntimeError("视频号上传按钮未能打开文件选择器，浏览器窗口已保留")
    else:
        for _ in range(120):
            if await inputs.count() > 0: break
            await page.wait_for_timeout(1000)
        if await inputs.count() == 0:
            await capture_diagnostics(page, "未找到视频号上传控件")
            raise RuntimeError("未找到视频号上传控件，浏览器窗口已保留")
        await inputs.first.set_input_files(str(video))
    editor_selectors = ['div[data-placeholder="添加描述"]','textarea[placeholder*="描述"]','textarea[placeholder*="说点什么"]','input[placeholder*="描述"]']
    upload_started = time.monotonic()
    upload_percent: int | None = None
    for tick in range(900):
        busy, frame_text = await channels_upload_busy(page)
        if any(x in frame_text for x in ("上传失败", "格式不支持", "文件过大")):
            raise RuntimeError(f"视频号页面报告上传失败：{frame_text[-120:]}")
        editor_ready = bool(await first_visible_any(page, editor_selectors))
        # 上传完成后视频号常停在“预览/下一步”层，描述框还没挂载。只等
        # editor_ready 会一直空转到超时（用户看到的就是进度条不动），
        # 所以“下一步/完成”按钮出现时也结束等待，由后面的逻辑点进去。
        next_button = None
        if not editor_ready:
            next_button = await first_visible_any(page, ['button:has-text("下一步")','button:has-text("完成")','button:has-text("继续")'])
        if not busy and (editor_ready or next_button is not None):
            break
        if tick % 2 == 0:
            percent = await channels_upload_percent(page)
            if percent is not None:
                upload_percent = percent
            set_channels_stage(item, "步骤1/6 正在上传视频", stage=1, percent=upload_percent,
                               elapsed=int(time.monotonic() - upload_started))
        await page.wait_for_timeout(1000)
    else:
        raise RuntimeError("上传处理超时，已保留页面")
    # 上传进度到 100% 后，视频号有时还会停在预览层，需要点击下一步/完成才挂载编辑器。
    editor = await first_visible_any(page, editor_selectors)
    if not editor:
        next_button = await first_visible_any(page, ['button:has-text("下一步")','button:has-text("完成")','button:has-text("继续")','button:has-text("进入编辑")','button:has-text("编辑视频")'])
        if next_button:
            await next_button.click(); await page.wait_for_timeout(2000)
        for _ in range(90):
            editor = await first_visible_any(page, editor_selectors)
            if editor: break
            await page.wait_for_timeout(1000)
    await capture_diagnostics(page, "上传完成后进入字段填写前的页面现场")
    if not editor:
        raise RuntimeError("视频已上传完成，但视频号编辑器未出现，浏览器窗口已保留")
    await wait_channels_form_ready(page)
    set_channels_stage(item, "步骤2/6 视频上传完成，正在设置封面", stage=2)
    log(f"步骤2/6 设置封面（3:4 + 4:3）：{item.get('name', '')}")
    cover_name = ""
    last_cover_error = None
    for cover_attempt in range(3):
        try:
            cover_name = await upload_channels_cover(page, item)
            last_cover_error = None
            break
        except Exception as exc:
            last_cover_error = exc
            log(f"视频号封面第 {cover_attempt + 1} 次失败：{exc}")
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            await page.wait_for_timeout(1500)
    if last_cover_error is not None:
        raise last_cover_error
    if cover_name:
        await page.wait_for_timeout(1200)
        log(f"步骤2/6 封面设置完成：{cover_name}")
        await capture_diagnostics(page, "封面设置完成后")
        # 封面确认后表单可能重新挂载，重新定位编辑器再填文案/标题。
        await page.wait_for_timeout(1200)
        editor = await first_visible_any(page, editor_selectors)
        if not editor:
            await page.wait_for_timeout(2000)
            editor = await first_visible_any(page, editor_selectors)
        if not editor:
            raise RuntimeError("视频号封面已设置，但描述编辑器未重新出现，浏览器窗口已保留")
    set_channels_stage(item, "步骤3/6 正在填写标题", stage=3)
    log(f"步骤3/6 填写标题：{item.get('name', '')}")
    caption_value = text_for(item) or "1"
    title_value = sanitize_channels_title(item)
    full_title_value = title_value
    title_selectors = ['input[placeholder="填写短标题有机会获得更多流量"]','input[placeholder*="标题"]']
    adjusted = False
    if len(title_value) > CHANNELS_SHORT_TITLE_LIMIT:
        short, suffix = fit_channels_short_title(title_value)
        if suffix:
            title_value = short
            caption_value = suffix + "\n" + caption_value
            adjusted = True
            log(f"视频号短标题限制：短标题「{short.strip()}」，其余内容移到描述开头")
    title_ok = await fill_and_verify(page, title_selectors, title_value)
    if not title_ok and len(title_value.strip()) < CHANNELS_SHORT_TITLE_MIN:
        # 输入框可能把末尾空格裁掉，导致“6字”回读不一致；用非空白前6字重试。
        compact = re.sub(r"\s+", "", full_title_value)
        short = compact[:CHANNELS_SHORT_TITLE_MIN]
        suffix = compact[CHANNELS_SHORT_TITLE_MIN:]
        if len(short) == CHANNELS_SHORT_TITLE_MIN and suffix:
            title_value = short
            caption_value = suffix + "\n" + caption_value
            title_ok = await fill_and_verify(page, title_selectors, title_value)
            if title_ok:
                adjusted = True
                log(f"视频号短标题少于6字：截取前6字「{short}」，其余移到描述开头")
    if not title_ok:
        # 平台可能把输入框截断到字数上限，导致回读不一致；再按歌名重试。
        song, extra = split_channels_title(full_title_value)
        if song and extra:
            title_value = song
            title_ok = await fill_and_verify(page, title_selectors, title_value)
            if title_ok:
                caption_value = extra + "\n" + caption_value
                adjusted = True
                log(f"视频号短标题超字数：保留歌名「{song}」，日期/地点移到描述开头")
    if not title_ok:
        raise RuntimeError("视频号短标题未能写入内容，浏览器窗口已保留")
    await page.wait_for_timeout(700)
    if await channels_short_title_error(page):
        song, extra = split_channels_title(full_title_value)
        if len(title_value.strip()) < CHANNELS_SHORT_TITLE_MIN:
            compact = re.sub(r"\s+", "", full_title_value)
            short = compact[:CHANNELS_SHORT_TITLE_MIN]
            suffix = compact[CHANNELS_SHORT_TITLE_MIN:]
            if len(short) == CHANNELS_SHORT_TITLE_MIN and suffix:
                title_value = short
                caption_value = suffix + "\n" + caption_value
                if not await fill_and_verify(page, title_selectors, title_value):
                    raise RuntimeError("视频号短标题少于6字后未能写入6字标题，浏览器窗口已保留")
                log(f"视频号短标题少于6字：截取前6字「{short}」，其余移到描述开头")
                adjusted = True
        if not adjusted and song and extra:
            title_value = song
            if not await fill_and_verify(page, title_selectors, title_value):
                raise RuntimeError("视频号短标题超字数后未能写入歌名，浏览器窗口已保留")
            caption_value = extra + "\n" + caption_value
            log(f"视频号短标题超字数：保留歌名「{song}」，日期/地点移到描述开头")
        elif not adjusted:
            log("视频号短标题超字数，但未能拆分歌名和日期，保留原值")
    set_channels_stage(item, "步骤4/6 正在填写描述", stage=4)
    log(f"步骤4/6 填写描述：{item.get('name', '')}")
    if not await fill_and_verify(page, editor_selectors, caption_value):
        raise RuntimeError("视频号描述框未能写入内容，浏览器窗口已保留")
    # 视频标注是视频号页面的必选项；必须先打开下拉并选中，否则发表按钮
    # 看似可用但提交不会生效。标题里的中文逗号等字符也必须先清洗。
    body_text = await page.locator("body").first.inner_text()
    if "标题包含特殊字符" in body_text:
        raise RuntimeError("视频号短标题仍包含不支持的特殊字符，浏览器窗口已保留")
    set_channels_stage(item, "步骤5/6 正在选择视频标注", stage=5)
    log(f"步骤5/6 选择视频标注：{item.get('name', '')}")
    annotation = await select_video_annotation(page)
    if not annotation:
        raise RuntimeError("视频号视频标注为必填项，未能选择任何标注选项，浏览器窗口已保留")
    try: await page.keyboard.press("Escape")
    except Exception: pass
    publish = await first_visible_any(page, ['button:has-text("发表")','button:has-text("发布")','[role="button"]:has-text("发表")'])
    if not publish: raise RuntimeError("已上传但未找到明确的发表按钮，已保留页面")
    if item.get("collection_name"):
        set_channels_stage(item, "步骤5/6 正在选择合集", stage=5)
        log(f"步骤5/6 选择合集：{item.get('collection_name')}")
        await select_channels_collection(page, item)
    schedule_time = await select_channels_schedule(page, item)
    set_channels_stage(item, "步骤6/6 正在发表", stage=6)
    log(f"步骤6/6 点击发表：{item.get('name', '')}")
    await capture_diagnostics(page, "发表前表单")
    for _ in range(120):
        try:
            if await publish.is_enabled(): break
        except Exception: pass
        await page.wait_for_timeout(1000)
    else:
        raise RuntimeError("发表按钮持续不可用，平台仍未确认视频上传完成，浏览器窗口已保留")
    try:
        await publish.scroll_into_view_if_needed()
        await publish.click(timeout=10000)
    except Exception:
        # 视频号的“视频标注”浮层可能覆盖底部按钮；按正常用户路径先关闭浮层再重试。
        try: await page.keyboard.press("Escape")
        except Exception: pass
        try: await page.mouse.click(1200, 120)
        except Exception: pass
        await page.wait_for_timeout(500)
        await publish.scroll_into_view_if_needed()
        await publish.click(timeout=15000)
    for _ in range(45):
        if await confirm_channels_dialog(page):
            await page.wait_for_timeout(1200)
        if await channels_publish_succeeded(page):
            return "已提交"
        await page.wait_for_timeout(1000)
    if await channels_publish_succeeded(page):
        return "已提交"
    # 页面按钮有时接收到了 Playwright click，但没有触发前端事件；使用同一按钮的屏幕坐标做一次正常备用点击。
    try:
        box = await publish.bounding_box()
        if box:
            await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            await page.wait_for_timeout(2500)
    except Exception: pass
    if await channels_publish_succeeded(page):
        return "已提交"
    # 最后使用同一按钮的 DOM click 事件，兼容组件只监听事件派发的页面实现。
    try:
        await publish.dispatch_event("click")
        await page.wait_for_timeout(2500)
    except Exception: pass
    body = await visible_text_any(page, ("发表成功", "发布成功", "已发表", "已发布"))
    if body or await success_marker_any(page): return "已提交"
    try:
        await publish.click(force=True, timeout=10000)
        await page.wait_for_timeout(2500)
        body = await visible_text_any(page, ("发表成功", "发布成功", "已发表", "已发布"))
        if body or await success_marker_any(page): return "已提交"
    except Exception: pass
    # 兼容页面只在键盘激活时提交的按钮实现。
    try:
        await publish.focus(); await publish.press("Enter"); await page.wait_for_timeout(2000)
        body = await visible_text_any(page, ("发表成功", "发布成功", "已发表", "已发布"))
        if body or await success_marker_any(page): return "已提交"
        await publish.focus(); await publish.press("Space"); await page.wait_for_timeout(2000)
    except Exception: pass
    if await channels_publish_succeeded(page):
        return "已提交"
    body = await visible_text_any(page, ("失败", "错误", "请上传视频"))
    if "失败" in body or "错误" in body or "请上传视频" in body:
        await capture_diagnostics(page, "发表后页面未接受请求")
        raise RuntimeError("视频号页面未接受发表请求：请确认视频上传状态，浏览器窗口已保留")
    await capture_diagnostics(page, "发表后无成功回执")
    raise RuntimeError("已点击发表但未确认成功结果，已保留页面")


class ChannelsTerminated(Exception):
    pass


async def wait_channels_control(shared: bool = True) -> None:
    """Pause after the current video, or stop before the next one."""
    while True:
        if shared:
            state = app.load_channels_state()
        else:
            state = load_json(STATE_FILE, {})
        if state.get("stop_requested") or state.get("terminated"):
            raise ChannelsTerminated("视频号发布已终止")
        if state.get("pause_requested") and not state.get("paused"):
            state.update({
                "paused": True,
                "pause_requested": False,
                "message": "视频号发布已暂停；当前视频已完成，可继续或终止",
            })
            if shared:
                app.save_channels_state(state)
            else:
                save_json(STATE_FILE, state)
            log("视频号发布已暂停，等待继续或终止")
        if not state.get("paused"):
            return
        await asyncio.sleep(1)


def clear_stale_control_flags() -> None:
    """清掉上一轮「终止」留下的控制位。

    终止会把 terminated/stop_requested 写进状态文件。新一轮开始时如果
    不清掉，第一条视频还没开始就会被判定为终止，用户看到的就是“点了
    开始发布却没有任何动作”。
    """
    previous = load_json(STATE_FILE, {})
    if previous.get("terminated") or previous.get("stop_requested"):
        log("已清除上一轮的终止标记，开始新的视频号发布")
    previous.update({"paused": False, "pause_requested": False, "stop_requested": False, "terminated": False})
    save_json(STATE_FILE, previous)


async def run_queue():
    items = load_queue(); todo = [x for x in items if x.get("status") in {"pending", "failed", "publishing"}]
    if not todo: set_state(running=False, message="没有待发布视频", total=0); return
    clear_stale_control_flags()
    set_state(running=True, paused=False, current="", completed=0, total=len(todo), message="正在启动视频号浏览器", pid=os.getpid(), stage=0, stage_percent=None)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        kwargs = {"headless": False, "args": ["--disable-blink-features=AutomationControlled", "--new-window", "--window-size=1440,1000", "--force-device-scale-factor=1", "--disable-extensions", "--disable-features=SidePanelV2,SidePanel"]}
        chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        if chrome.is_file(): kwargs["executable_path"] = str(chrome)
        context = await launch_channels_context(pw, kwargs)
        page = context.pages[0] if context.pages else await context.new_page()
        completed = 0
        for item in todo:
            try:
                await wait_channels_control(shared=False)
            except ChannelsTerminated:
                log("视频号发布已终止：收到终止指令")
                set_state(running=False, paused=False, current="", completed=completed, total=len(todo), message="视频号发布已终止", stage=0, stage_percent=None)
                await context.close()
                return
            item["status"] = "publishing"; item["message"] = "正在上传并发表"; save_queue(items)
            set_state(running=True, status="publishing", current=item.get("name", ""), completed=completed, total=len(todo), message=item["message"], stage=1, stage_percent=None)
            try:
                result = await publish_item(page, item)
                item["status"] = "published"; item["message"] = result; completed += 1
                set_state(running=True, status="publishing", current="", completed=completed, total=len(todo), message=f"已提交 {item.get('name','')}，继续下一条")
            except Exception as exc:
                if "Target page" in str(exc) or "browser has been closed" in str(exc) or "context has been closed" in str(exc):
                    try:
                        await context.close()
                    except Exception: pass
                    await asyncio.sleep(2)
                    context = await launch_channels_context(pw, kwargs)
                    page = context.pages[0] if context.pages else await context.new_page()
                    try:
                        result = await publish_item(page, item)
                        item["status"] = "published"; item["message"] = result; completed += 1
                        save_queue(items); continue
                    except Exception as retry_exc:
                        exc = retry_exc
                item["status"] = "failed"; item["message"] = str(exc)
                save_queue(items); set_state(running=False, paused=True, current=item.get("name", ""), completed=completed, message=str(exc), stage=0, stage_percent=None);
                await capture_diagnostics(page, str(exc))
                print(str(exc), flush=True)
                await asyncio.Event().wait()
                return
            save_queue(items)
        await context.close()
    set_state(running=False, paused=False, current="", completed=completed, total=len(todo), message="本轮视频号发布完成", stage=0, stage_percent=None)


def shared_item_id(item: dict) -> str:
    return str(item.get("id") or item.get("path") or "")


def shared_update_platform(items: list[dict], item: dict, status: str, note: str) -> None:
    video_type = app.normalize_video_type(item.get("video_type", "normal"))
    item["platforms"] = app.normalize_platform_states(item.get("platforms"), item, video_type)
    item["platforms"]["channels"] = {
        "status": status,
        "note": note,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    app.save_queue(items)


def set_shared_state(**changes: object) -> None:
    state = app.load_channels_state()
    state.update(changes)
    state["mode"] = "shared_queue"
    if changes.get("running") is False and not changes.get("paused"):
        state.pop("pid", None)
    elif changes.get("running") is True:
        state["pid"] = os.getpid()
    app.save_channels_state(state)


async def run_shared_queue(ids_file: str) -> None:
    global _SHARED_MODE
    _SHARED_MODE = True
    try:
        raw_ids = load_json(Path(ids_file), [])
    except Exception:
        raw_ids = []
    requested = {str(value) for value in raw_ids if str(value).strip()} if isinstance(raw_ids, list) else set()
    items = app.load_queue()
    targets: list[dict] = []
    skipped: list[dict] = []
    for item in items:
        if requested and shared_item_id(item) not in requested:
            continue
        video_type = app.normalize_video_type(item.get("video_type", "normal"))
        item["platforms"] = app.normalize_platform_states(item.get("platforms"), item, video_type)
        if video_type == "panorama":
            shared_update_platform(items, item, "unsupported", "视频号暂不支持全景视频")
            skipped.append(shared_item_id(item))
            continue
        channel_status = str(item["platforms"]["channels"].get("status") or "pending")
        if channel_status in {"pending", "failed", "publishing"}:
            targets.append(item)
    if not targets:
        set_shared_state(running=False, status="idle", message="当前队列没有可发布到视频号的普通视频", completed=0, total=0, current="", skipped=skipped)
        return
    set_shared_state(running=True, status="starting", message=f"正在启动视频号发布（{len(targets)} 条）", completed=0, total=len(targets), current="", skipped=skipped)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        kwargs = {"headless": False, "args": ["--disable-blink-features=AutomationControlled", "--new-window", "--window-size=1440,1000", "--force-device-scale-factor=1", "--disable-extensions", "--disable-features=SidePanelV2,SidePanel"]}
        chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        if chrome.is_file(): kwargs["executable_path"] = str(chrome)
        context = await launch_channels_context(pw, kwargs)
        page = context.pages[0] if context.pages else await context.new_page()
        completed = 0
        for item in targets:
            try:
                await wait_channels_control(shared=True)
            except ChannelsTerminated:
                set_shared_state(running=False, status="terminated", paused=False, current="", completed=completed, total=len(targets), message="视频号发布已终止")
                await context.close()
                return
            name = str(item.get("name") or shared_item_id(item))
            shared_update_platform(items, item, "publishing", "正在上传并发表到视频号")
            set_shared_state(running=True, status="publishing", current=name, item_id=shared_item_id(item), completed=completed, total=len(targets), message=f"正在发布视频号：{name}")
            runtime_item = dict(item)
            resolved = app.safe_video_path(str(item.get("path") or ""))
            if resolved is not None:
                runtime_item["path"] = str(resolved)
            try:
                result = await publish_item(page, runtime_item)
            except Exception as exc:
                note = f"视频号发布失败：{exc}"
                shared_update_platform(items, item, "failed", note)
                set_shared_state(running=False, status="failed", paused=True, current=name, completed=completed, total=len(targets), message=note)
                await capture_diagnostics(page, note)
                print(note, flush=True)
                await asyncio.Event().wait()
                return
            shared_update_platform(items, item, "published", f"已提交视频号（{result}）")
            completed += 1
        await context.close()
    set_shared_state(running=False, status="published", paused=False, current="", completed=completed, total=len(targets), message="视频号队列已全部提交", skipped=skipped)


def main():
    p = argparse.ArgumentParser(); p.add_argument("--queue", action="store_true"); p.add_argument("--shared-queue", action="store_true"); p.add_argument("--ids-file", default=""); p.add_argument("--list-collections", action="store_true"); p.add_argument("--check", action="store_true"); p.add_argument("video", nargs="?")
    args = p.parse_args()
    if args.check:
        print(json.dumps(asyncio.run(check_channels_environment()), ensure_ascii=False), flush=True)
        return
    if args.list_collections:
        collections = asyncio.run(list_channels_collections())
        print(json.dumps({"collections": collections}, ensure_ascii=False), flush=True)
        return
    if args.shared_queue:
        asyncio.run(run_shared_queue(args.ids_file)); return
    if args.queue: asyncio.run(run_queue()); return
    if args.video:
        item = normalize_item({"path": str(Path(args.video).expanduser().resolve())})
        asyncio.run(run_single(item)); return
    p.error("请使用 --queue 或提供视频路径")


async def run_single(item):
    save_queue([item]); await run_queue()

if __name__ == "__main__": main()
