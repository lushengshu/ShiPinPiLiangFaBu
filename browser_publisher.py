#!/usr/bin/env python3
"""使用独立 Chrome 登录态逐条上传并发布队列中的视频。

这是浏览器自动化，不读取或保存抖音密码。第一次运行会打开一个专用浏览器目录，
需要用户在该窗口中完成登录；验证码和异常页面必须由用户处理。
"""

from __future__ import annotations

import asyncio
import argparse
import base64
import json
import os
import re

# WorkBuddy injects a Node.js shim via NODE_OPTIONS; Playwright's bundled
# Node.js driver crashes when the shim cannot connect to the broker.
# Strip it before any Playwright code runs.
for _env_key in ("NODE_OPTIONS", "NODE_PATH"):
    os.environ.pop(_env_key, None)

import subprocess
import time
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from playwright.async_api import BrowserContext, Locator, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

import app


PROFILE_DIR = app.ROOT / ".douyin_browser_profile"
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def log(message: str) -> None:
    """Write a flushed, timestamped line so live runs are diagnosable."""
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


class ManualActionRequired(RuntimeError):
    """抖音页面需要用户在当前浏览器页完成操作。"""


class PublishResultUncertain(ManualActionRequired):
    """发布按钮已点击，但结果未确认；恢复时只能观察，不能再次点击。"""


class PublishOptionsError(ManualActionRequired):
    """发布可见范围/下载权限无法安全识别或选择。"""


class PublishScheduleError(ManualActionRequired):
    """抖音定时发布选项/时间无法安全设置或确认。"""


class PanoramaEntryError(ManualActionRequired):
    """全景入口、格式控件或全景页预审无法安全确认。"""


class PublishStopRequested(RuntimeError):
    """网页请求安全终止当前任务；浏览器现场必须保留。"""


def _clear_pause_attention(state: dict) -> None:
    """清除上一轮自动暂停的通知元数据。

    这些字段只描述当前发布任务的一次暂停事件。新任务继续运行或正常
    完成后必须清掉，避免前端在下一次轮询中把旧原因再次弹出来。
    """
    state["auto_paused"] = False
    state["pause_source"] = ""
    state["pause_reason"] = ""
    state["pause_kind"] = ""
    state.pop("pause_event_id", None)
    state.pop("paused_at", None)


def set_publish_state(**changes: object) -> None:
    state = app.load_publish_state()
    state.update(changes)
    # “manual” 只表示用户点击了暂停；“automatic” 由
    # mark_automatic_pause() 写入并带有一次性事件编号。普通进度写入会
    # 清掉旧通知，防止同一原因跨任务残留。
    pause_source = str(changes.get("pause_source") or "")
    if pause_source == "manual":
        state["auto_paused"] = False
        state["pause_kind"] = "manual"
        state.pop("pause_event_id", None)
        state.pop("paused_at", None)
    elif changes.get("auto_paused") is True:
        state["pause_source"] = "automatic"
    elif changes.get("auto_paused") is False or (
        changes.get("action_required") is False
        and pause_source not in {"manual", "automatic"}
    ):
        _clear_pause_attention(state)
    # 由发布器自身持续写入 PID，避免 app.py 在 Popen 后回写 PID 时与
    # 进程启动阶段的状态更新发生竞态；running=false 时清理旧 PID。
    if changes.get("running") is True or (state.get("running") and not state.get("publisher_pid")):
        state["publisher_pid"] = os.getpid()
        state["stop_supported"] = True
        state["stop_requested"] = bool(app.STOP_REQUEST_FILE.exists())
        state["terminated"] = False
    elif changes.get("running") is False:
        state.pop("publisher_pid", None)
        # 任务结束或异常退出后，旧的暂停请求不能影响下一次任务。
        state["paused"] = False
        state["pause_requested"] = False
        state["stop_requested"] = False
        state["stop_supported"] = False
        state["terminated"] = bool(changes.get("terminated", False))
        app.clear_publish_pause()
        app.clear_publish_stop()
    app.save_publish_state(state)


def mark_automatic_pause(
    reason: object,
    *,
    kind: str = "publish_error",
    current: object | None = None,
    completed: object | None = None,
    total: object | None = None,
) -> None:
    """把非人为中断记录为一次可恢复的自动暂停。

    浏览器和当前草稿由调用方保留；这里仅结束发布器进程的发布循环，
    并为网页提供稳定的原因/事件编号。再次点击“继续发布”会按队列状态
    恢复，不会删除源视频。
    """
    detail = str(reason or "发布流程遇到未说明的异常").strip()
    changes: dict[str, object] = {
        "running": False,
        "paused": False,
        "pause_requested": False,
        "message": f"自动暂停：{detail}",
        "action_required": True,
        "auto_paused": True,
        "pause_source": "automatic",
        "pause_reason": detail,
        "pause_kind": str(kind or "publish_error"),
        "pause_event_id": uuid.uuid4().hex,
        "paused_at": app.now(),
        "terminated": False,
    }
    if current is not None:
        changes["current"] = str(current)
    if completed is not None:
        changes["completed"] = completed
    if total is not None:
        changes["total"] = total
    set_publish_state(**changes)


def pause_requested() -> bool:
    """读取暂停控制；独立标记文件用于避免状态写入竞态。"""
    state = app.load_publish_state()
    # 标记文件是控制面的权威来源；JSON 中 pause_requested 是展示快照，
    # 继续操作删除标记后不应被旧快照阻塞。
    return bool(state.get("running") and app.PAUSE_REQUEST_FILE.exists())


def stop_requested() -> bool:
    """读取网页发出的安全终止标记。"""
    state = app.load_publish_state()
    return bool(state.get("running") and app.STOP_REQUEST_FILE.exists())


def check_stop_requested(checkpoint: str) -> None:
    """在浏览器安全检查点抛出终止信号，不关闭页面或上下文。"""
    if stop_requested():
        raise PublishStopRequested(f"已终止自动发布：{checkpoint}；浏览器页面保持打开")


def is_panorama_item(item: dict) -> bool:
    """Whether this queue item explicitly opts into the panoramic flow."""
    return str(item.get("video_type", "normal")).strip().lower() == "panorama"


def panorama_format_label(item: dict) -> str:
    value = str(item.get("panorama_format", "")).strip().lower()
    config = app.PANORAMA_FORMATS.get(value)
    return str(config.get("label")) if config else ""


async def wait_for_panorama_entry(page: Page) -> None:
    """Verify the dedicated panoramic upload page without falling back.

    The current Creator Center route is ``default-tab=4``.  Accounts without
    panoramic permission may redirect to the ordinary uploader, so the
    visible page text and URL are checked before a file is selected.
    """
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        check_stop_requested("等待抖音全景上传入口")
        body = ""
        try:
            body = await page.locator("body").inner_text()
        except Exception:
            pass
        if "/content/upload" in page.url and "default-tab=4" in page.url:
            if await visible_text_present(page, re.compile(r"上传全景视频|发布全景视频|全景视频")):
                return
        if "/content/publish-vr-video" in page.url or "/content/post/vr" in page.url:
            return
        # A permission/login page may still render the menu item while the
        # route is changing; keep observing, but never select the first file
        # input on an ordinary video page.
        if "没有权限" in body or "暂未开放" in body or "无权限" in body:
            raise PanoramaEntryError("当前抖音账号未开放全景视频发布入口")
        await page.wait_for_timeout(800)
    raise PanoramaEntryError("未确认抖音全景视频上传入口，已保留当前浏览器页面")


async def open_upload_page(page: Page, item: dict) -> None:
    """Navigate to the correct upload route for one queue item."""
    url = app.PANORAMA_UPLOAD_URL if is_panorama_item(item) else app.UPLOAD_URL
    await page.goto(url, wait_until="domcontentloaded")
    await wait_until_logged_in(page)
    if is_panorama_item(item):
        await wait_for_panorama_entry(page)


async def wait_if_paused(page: Page, item: dict, checkpoint: str) -> None:
    """在安全检查点协作式暂停，保持当前浏览器页和发布进程。"""
    check_stop_requested(checkpoint)
    if not pause_requested():
        return
    name = item.get("name") or item.get("path") or "（无名）"
    state = app.load_publish_state()
    if not state.get("paused"):
        set_publish_state(
            running=True,
            paused=True,
            pause_requested=True,
            message=f"已暂停：{checkpoint}；浏览器页面保持打开",
            current=name,
            action_required=False,
            auto_paused=False,
            pause_source="manual",
            pause_reason=checkpoint,
            pause_kind="manual",
        )
    while pause_requested():
        check_stop_requested(checkpoint)
        # 不调用 goto/关闭页面/切换队列；上传中的浏览器页面可以自然完成，
        # 但发布器不会进入下一步或下一条视频。
        await page.wait_for_timeout(1000)
    set_publish_state(
        running=True,
        paused=False,
        pause_requested=False,
        message=f"已继续：{checkpoint}",
        current=name,
        action_required=False,
        auto_paused=False,
        pause_source="",
    )


def update_item(item_id: str, status: str, note: str | None = None) -> None:
    items = app.load_queue()
    for item in items:
        if item.get("id") == item_id:
            item["status"] = status
            if note is not None:
                item["note"] = note
            item["updated_at"] = app.now()
            break
    app.save_queue(items)


async def first_visible(*locators: Locator) -> Locator | None:
    for locator in locators:
        try:
            if await locator.count() and await locator.first.is_visible():
                return locator.first
        except Exception:
            continue
    return None


async def last_visible(*locators: Locator) -> Locator | None:
    for locator in locators:
        try:
            count = await locator.count()
        except Exception:
            continue
        for index in range(count - 1, -1, -1):
            try:
                candidate = locator.nth(index)
                if await candidate.is_visible():
                    return candidate
            except Exception:
                continue
    return None


async def visible_text_present(page: Page, pattern: str | re.Pattern[str]) -> bool:
    """判断文本是否有可见匹配，避免命中抖音隐藏模板节点。"""
    try:
        locator = page.get_by_text(pattern)
        for index in range(await locator.count()):
            try:
                if await locator.nth(index).is_visible():
                    return True
            except Exception:
                continue
    except Exception:
        return False
    return False


def _panorama_error_line(text: object) -> str | None:
    """Return a strong, user-facing panoramic validation error line.

    The panoramic upload page contains static help text such as supported
    formats and limits.  Scanning the complete page for a single word (for
    example ``格式`` or ``大小``) therefore produces false failures.  Only
    lines that combine an upload/validation failure with a concrete reason
    are treated as errors here.
    """
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return None
    patterns = (
        re.compile(r"(?:上传|预审|校验|处理).{0,40}(?:失败|错误|不通过|未通过|拒绝)", re.I),
        re.compile(r"(?:文件|视频).{0,24}(?:格式|大小|时长|分辨率).{0,32}(?:不支持|(?<!不)(?:超过|超出)|过大|过长|不符合|错误|失败|限制)", re.I),
        re.compile(r"(?:格式|大小|时长|分辨率).{0,32}(?:不支持|(?<!不)(?:超过|超出)|过大|过长|不符合|错误|失败|限制).{0,24}(?:文件|视频|上传)", re.I),
        re.compile(r"上传失败", re.I),
    )
    if not any(pattern.search(normalized) for pattern in patterns):
        return None
    return normalized[:320]


async def _visible_panorama_validation_error(page: Page) -> str | None:
    """Read only visible error/alert nodes, then visible body lines.

    This deliberately avoids treating hidden React templates or static help
    text as a platform rejection.  The returned text is saved with the page
    diagnostics so a future UI change can be fixed from evidence.
    """
    selectors = (
        '[role="alert"]:visible',
        '[role="status"]:visible',
        '[aria-live]:visible',
        '[class*="toast"]:visible',
        '[class*="error"]:visible',
        '[class*="warning"]:visible',
        '[class*="fail"]:visible',
    )
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = min(await locator.count(), 20)
            for index in range(count):
                try:
                    text = await locator.nth(index).inner_text()
                except Exception:
                    continue
                for line in str(text or "").splitlines():
                    match = _panorama_error_line(line)
                    if match:
                        return match
        except Exception:
            continue
    try:
        body = await page.locator("body").inner_text()
    except Exception:
        return None
    for line in str(body or "").splitlines():
        match = _panorama_error_line(line)
        if match:
            return match
    return None


async def _capture_panorama_diagnostics(page: Page, reason: str) -> str:
    """Persist the visible page evidence without changing the browser page."""
    debug_dir = app.STATE_DIR / "debug"
    if not debug_dir.exists():
        try:
            debug_dir.mkdir(exist_ok=True)
        except PermissionError:
            pass
    base = debug_dir / "panorama_last_error"
    try:
        body = await page.locator("body").inner_text()
    except Exception:
        body = ""
    try:
        html = await page.content()
    except Exception:
        html = ""
    metadata = {
        "captured_at": app.now(),
        "url": page.url,
        "reason": reason,
    }
    try:
        (base.with_suffix(".json")).write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (base.with_suffix(".txt")).write_text(body, encoding="utf-8")
        (base.with_suffix(".html")).write_text(html, encoding="utf-8")
        await page.screenshot(path=str(base.with_suffix(".png")), full_page=False)
        return str(base)
    except Exception:
        return ""


def _item_identity_needles(item: dict) -> list[str]:
    """Return the most distinctive visible strings for this queue item.

    The title is preferred because captions and filenames repeat across old
    works.  Falling back to a short filename like "开场" would match unrelated
    works in content management, so only use a filename when it is long enough.
    """
    needles: list[str] = []
    title = str(item.get("work_title") or "").strip()
    if title:
        needles.append(title)
    caption = str(item.get("caption") or "").strip()
    if caption:
        core = caption.split("#", 1)[0].strip()
        if len(core) >= 3:
            needles.append(core[:24])
    if not needles:
        name = Path(str(item.get("name") or item.get("path") or "")).stem.strip()
        if len(name) >= 4:
            needles.append(name)
    return needles


def _item_identity_in_text(text: object, item: dict) -> bool:
    normalized = _normalize_ui_text(text)
    needles = [_normalize_ui_text(value) for value in _item_identity_needles(item)]
    return any(needle and needle in normalized for needle in needles)


def page_publish_succeeded(
    url: str,
    body: str,
    previous_url: str | None = None,
    *,
    item: dict | None = None,
    before_body: str = "",
) -> bool:
    """Return true only for a *new* success signal tied to this item.

    A stale "发布成功" toast, a previous draft, or the editor's navigation text
    must never mark the current video as published.  Body-only success is
    accepted only when the item's distinctive title/caption is present and the
    phrase was not already on the page before the click.
    """
    success_words = (
        "发布成功",
        "发布完成",
        "作品已发布",
        "提交成功",
        "定时发布成功",
        "定时发布已提交",
        "预约发布成功",
        "已预约发布",
    )
    new_words = [
        word for word in success_words
        if word in body and word not in before_body
    ]
    if not new_words:
        return False
    if previous_url and url != previous_url:
        # 点击发布后发生跳转（通常是内容管理）即可视为本次提交成功，
        # 不需要等作品审核结果或列表卡片挂载。
        return True
    if any(word in new_words for word in ("定时发布成功", "定时发布已提交", "预约发布成功", "已预约发布")):
        return True
    return bool(item is not None and _item_identity_in_text(body, item))


def cover_is_ready(body: str) -> bool:
    """返回页面是否已经明确完成封面选择。"""
    return any(word in body for word in (
        "封面效果检测通过",
        "封面已设置",
        "已选择封面",
        "封面设置成功",
    ))


def _normalize_allow_download(value: object) -> bool:
    """把队列中的布尔/中文配置统一成严格的下载权限布尔值。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return True
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "是", "允许", "开启", "开", "on"}:
        return True
    if text in {"false", "0", "no", "n", "否", "不允许", "禁止", "关闭", "关", "off"}:
        return False
    raise PublishOptionsError(f"allow_download 配置无效：{value!r}，请设置为允许或不允许")


async def _visible_option_label(group: Locator, option_text: str) -> Locator | None:
    """在指定设置分组中找带 input 的可见 label，避免命中其他分组同名文字。"""
    labels = group.locator("label")
    for index in range(await labels.count()):
        label = labels.nth(index)
        try:
            if not await label.is_visible():
                continue
            text = " ".join((await label.inner_text()).split())
            if text != option_text:
                continue
            if await label.locator("input").count() == 0:
                continue
            return label
        except Exception:
            continue
    return None


def _normalized_option_text(text: object) -> str:
    return " ".join(str(text or "").replace("\u3000", " ").split()).strip()


async def _visible_option_control(group: Locator, option_text: str) -> Locator | None:
    """Find native labels and the self-drawn radio cards used by VR pages."""
    option = await _visible_option_label(group, option_text)
    if option is not None:
        return option
    selectors = (
        '[role="radio"]',
        '[role="option"]',
        '[data-value]',
        '[data-option]',
        '[data-checked]',
        'button',
    )
    for selector in selectors:
        controls = group.locator(selector)
        for index in range(await controls.count()):
            control = controls.nth(index)
            try:
                if not await control.is_visible():
                    continue
                if _normalized_option_text(await control.inner_text()) == option_text:
                    return control
            except Exception:
                continue
    # Last fallback: a visible text node whose closest clickable ancestor is
    # the card.  This covers Semi UI/React builds with no stable role.
    texts = group.get_by_text(option_text, exact=True)
    for index in range(await texts.count()):
        text_node = texts.nth(index)
        try:
            if not await text_node.is_visible():
                continue
            for ancestor_selector in (
                'ancestor::label[1]',
                'ancestor::*[@role="radio"][1]',
                'ancestor::button[1]',
                'ancestor::div[@data-checked][1]',
            ):
                ancestor = text_node.locator(f"xpath={ancestor_selector}")
                if await ancestor.count() and await ancestor.first.is_visible():
                    return ancestor.first
            return text_node
        except Exception:
            continue
    return None


async def _option_is_selected(label: Locator) -> bool:
    """读取抖音自绘单选项的选中状态；优先使用 label 的 data/aria 状态。"""
    try:
        return bool(
            await label.evaluate(
                """el => {
                    const root = el;
                    let node = el;
                    for (let i = 0; node && i < 5; i += 1, node = node.parentElement) {
                        const dataChecked = node.getAttribute('data-checked');
                        const ariaChecked = node.getAttribute('aria-checked');
                        const dataSelected = node.getAttribute('data-selected');
                        const ariaSelected = node.getAttribute('aria-selected');
                        const cls = String(node.className || '').toLowerCase();
                        if (dataChecked === 'true' || ariaChecked === 'true' || dataSelected === 'true' || ariaSelected === 'true') return true;
                        if (/(^|[-_\\s])(selected|active|checked)([-_\\s]|$)/.test(cls)) return true;
                        if (node.matches && node.matches('input:checked')) return true;
                        // 只检查当前选项自身的后代控件。对外层分组
                        // 不能 querySelector 任意后代，否则同组另一个
                        // 已选项会把当前选项误判为已选。
                        if (node === root && node.querySelector && node.querySelector('input:checked, [aria-checked="true"], [data-checked="true"], [data-selected="true"]')) return true;
                    }
                    return false;
                }"""
            )
        )
    except Exception:
        return False


async def _find_option_for_heading(
    page: Page,
    heading_text: str,
    option_text: str,
) -> Locator | None:
    """按可见标题定位设置分组，再从该分组内取目标 label。"""
    headings = page.get_by_text(heading_text, exact=True)
    for index in range(await headings.count() - 1, -1, -1):
        heading = headings.nth(index)
        try:
            if not await heading.is_visible():
                continue
            # 先尝试旧版的最近 input 分组，再逐层向外寻找自绘卡片分组。
            groups: list[Locator] = []
            native_group = heading.locator("xpath=ancestor::div[.//input][1]")
            if await native_group.count():
                groups.append(native_group.first)
            ancestors = heading.locator("xpath=ancestor::div")
            for ancestor_index in range(min(await ancestors.count(), 8)):
                groups.append(ancestors.nth(ancestor_index))
            seen_groups: set[str] = set()
            for group in groups:
                try:
                    if not await group.is_visible():
                        continue
                    group_text = _normalized_option_text(await group.inner_text())
                    if heading_text not in group_text or option_text not in group_text:
                        continue
                    signature = await group.evaluate("el => el.outerHTML.slice(0, 300)")
                    if signature in seen_groups:
                        continue
                    seen_groups.add(signature)
                    option = await _visible_option_control(group, option_text)
                    if option is not None:
                        return option
                except Exception:
                    continue
        except Exception:
            continue
    # A final semantic fallback handles a heading rendered outside the card
    # container.  Keep the search constrained to an ancestor that contains
    # both the heading and the requested option, so “公开” in another block is
    # not accidentally selected.
    candidates = page.get_by_text(option_text, exact=True)
    for index in range(await candidates.count()):
        candidate = candidates.nth(index)
        try:
            if not await candidate.is_visible():
                continue
            ancestors = candidate.locator("xpath=ancestor::div")
            for ancestor_index in range(min(await ancestors.count(), 8)):
                ancestor = ancestors.nth(ancestor_index)
                if not await ancestor.is_visible():
                    continue
                text = _normalized_option_text(await ancestor.inner_text())
                if heading_text not in text:
                    continue
                control = await _visible_option_control(ancestor, option_text)
                if control is not None:
                    return control
        except Exception:
            continue
    return None


def _publish_options_failure(message: str) -> None:
    """把设置识别失败写入发布日志/状态，并抛出可识别异常。"""
    print(f"[publish-options] {message}", flush=True)
    try:
        set_publish_state(message=message, action_required=True)
    except Exception:
        pass
    raise PublishOptionsError(message)


async def configure_publish_options(page: Page, item: dict) -> None:
    """按队列配置选择“谁可以看”和“保存权限”，再允许点击发布。"""
    check_stop_requested("设置抖音发布权限")
    # 权限缺失时宁可暂停，也不替用户猜测为公开/允许下载。
    if "visibility" not in item or not str(item.get("visibility") or "").strip():
        _publish_options_failure("队列未配置可见范围，无法安全发布")
    if "allow_download" not in item or item.get("allow_download") is None:
        _publish_options_failure("队列未配置下载权限，无法安全发布")
    visibility = str(item.get("visibility") or "").strip()
    visibility_label = {
        "全部可见": "公开",
        "公开": "公开",
        "好友可见": "好友可见",
        "仅自己可见": "仅自己可见",
    }.get(visibility)
    if visibility_label is None:
        _publish_options_failure(f"可见范围配置无效：{visibility!r}，无法安全发布")
    try:
        allow_download = _normalize_allow_download(item.get("allow_download", True))
    except PublishOptionsError as exc:
        _publish_options_failure(str(exc))
    download_label = "允许" if allow_download else "不允许"

    for heading_text, option_text, setting_name in (
        ("谁可以看", visibility_label, "可见范围"),
        ("保存权限", download_label, "下载权限"),
    ):
        if heading_text == "保存权限" and is_panorama_item(item):
            # The current VR editor does not render a download-permission
            # group at all (it exposes PICO authorization and visibility only).
            # Absence is a platform capability difference, not a failed
            # selection; continue without inventing a hidden setting.  If the
            # group is present in a future build, the normal strict selector
            # below still configures and verifies it.
            save_heading = page.get_by_text("保存权限", exact=True)
            visible_save_heading = False
            for heading_index in range(await save_heading.count()):
                try:
                    if await save_heading.nth(heading_index).is_visible():
                        visible_save_heading = True
                        break
                except Exception:
                    continue
            if not visible_save_heading:
                print("[publish-options] 全景发布页未提供“保存权限”设置，按页面能力继续", flush=True)
                continue
        option = await _find_option_for_heading(page, heading_text, option_text)
        if option is None:
            _publish_options_failure(
                f"未找到抖音“{heading_text}”中的可见“{option_text}”选项，"
                f"无法安全设置{setting_name}，已暂停本条视频"
            )
        try:
            if not await _option_is_selected(option):
                await option.scroll_into_view_if_needed()
                await option.click(force=True)
                for _ in range(10):
                    check_stop_requested(f"设置抖音{setting_name}")
                    await page.wait_for_timeout(150)
                    if await _option_is_selected(option):
                        break
            if not await _option_is_selected(option):
                _publish_options_failure(
                    f"抖音“{heading_text}”未确认选中“{option_text}”，已暂停本条视频"
                )
        except PublishStopRequested:
            raise
        except PublishOptionsError:
            raise
        except Exception as exc:
            _publish_options_failure(
                f"设置抖音{setting_name}“{option_text}”失败：{exc}"
            )


def _activity_normalize_text(value: object) -> str:
    """Return a compact value suitable for conservative activity matching.

    The Creator Center changes spacing/punctuation between builds (and often
    renders a hashtag with or without ``#``).  Removing those presentation
    differences lets us compare a configured keyword with the visible card
    without doing fuzzy matching that could select an unrelated campaign.
    """
    text = str(value or "").strip().lower().replace("＃", "#")
    text = text.replace("#", "")
    return re.sub(r"[^\w\u3400-\u9fff]+", "", text, flags=re.UNICODE)


def _activity_terms(item: dict) -> list[str]:
    """Build explicit, evidence-bound terms for official activity matching.

    An explicitly configured keyword always wins.  With the default ``auto``
    mode, queue hashtags are the primary signal and short caption phrases are
    a secondary signal.  We intentionally do not use a broad fuzzy score: a
    visible official activity is selected only when its card text contains a
    configured term.  Generic words such as “现场” are ignored in automatic
    mode; the user can still enter one explicitly in the keyword field.
    """
    # ``official_topic_auto``/``official_topic_keywords`` are the persisted
    # queue names.  The older activity aliases remain accepted so a hand-
    # edited queue from an intermediate build can still be resumed safely.
    auto_value = item.get("official_topic_auto", None)
    if auto_value is not None:
        auto_enabled = app.normalize_bool(auto_value, default=True)
    else:
        mode = str(item.get("official_activity_mode", "auto") or "auto").strip().lower()
        auto_enabled = mode not in {"none", "off", "disabled", "manual", "不选择", "关闭"}
    if not auto_enabled:
        return []
    raw_values: list[object] = []
    explicit_terms = bool(str(
        item.get("official_topic_keywords", item.get("official_activity_keyword", "")) or ""
    ).strip())
    explicit = str(
        item.get("official_topic_keywords", item.get("official_activity_keyword", "")) or ""
    ).strip()
    if explicit:
        raw_values.extend(re.split(r"[,，、;；\n]+", explicit))
    else:
        raw_values.extend(
            tag.strip().lstrip("#")
            for tag in str(item.get("hashtags", "") or "").split(",")
            if tag.strip()
        )
        # Caption clauses are useful when an official campaign uses a phrase
        # rather than the exact hashtag.  Keep only meaningful, bounded
        # clauses so a generic one-character word cannot trigger a card.
        caption = str(item.get("caption", "") or "")
        raw_values.extend(
            part
            for part in re.split(r"[，。！？!?,.;；：:\n]+", caption)
            if len(part.strip()) >= 2
        )
    # Generic concert words are useful for ordinary captions but are not
    # evidence that a particular official campaign is relevant.  Keep them
    # only when the user explicitly entered them in the keyword field.
    generic_terms = {
        "现场", "演唱会", "音乐", "舞台", "视频", "作品", "活动", "演出",
        "直播", "记录", "氛围", "粉丝", "观众", "全景", "抖音",
    }
    terms: list[str] = []
    for value in raw_values:
        term = _activity_normalize_text(value)
        if len(term) < 2 or term in terms:
            continue
        if not explicit_terms and term in generic_terms:
            continue
        terms.append(term)
    return terms


async def _activity_selected(control: Locator) -> bool:
    """Read the selected state of an official-activity card/control."""
    try:
        return bool(
            await control.evaluate(
                """el => {
                  let node = el;
                  for (let i = 0; node && i < 8; i++, node = node.parentElement) {
                    const attrs = ["aria-checked", "aria-selected", "aria-pressed", "data-checked", "data-selected", "data-state"];
                    for (const name of attrs) {
                      const value = node.getAttribute && node.getAttribute(name);
                      if (["true", "checked", "selected", "active", "on"].includes(String(value).toLowerCase())) return true;
                    }
                    const cls = String(node.className || "").toLowerCase();
                    if (/(^|[-_\\s])(selected|active|checked|chosen|on)([-_\\s]|$)/.test(cls)) return true;
                    if (node.matches && node.matches("input:checked")) return true;
                    if (node.querySelector && node.querySelector("input:checked, [aria-checked=\\\"true\\\"], [aria-selected=\\\"true\\\"], [data-checked=\\\"true\\\"]")) return true;
                  }
                  return false;
                }"""
            )
        )
    except Exception:
        return False


async def _activity_candidate_locators(page: Page) -> list[Locator]:
    """Find visible, reasonably card-like controls under ``官方活动/话题``.

    There is no stable public selector across Creator Center builds.  Search
    only the visible section and prefer semantic/clickable elements, keeping
    the fallback bounded so a page-wide text search cannot click a menu item.
    """
    result: list[Locator] = []
    # The live Creator page currently uses cards such as
    # ``container-* activity-right-*`` inside an ``activity-list-*`` wrapper.
    # Search for the card-like descendants first.  A broad ``:scope > *``
    # fallback used to return the whole list wrapper, whose text contained
    # every card and therefore could not be selected reliably.
    activity_selector = (
        '[class*="activity-right"], [class*="activity-card"], '
        '[class*="activity-item"], [data-activity]'
    )
    semantic_selector = '[role="option"], [role="button"], button, a'
    generic_selector = '[class*="activity"], [class*="card"], [class*="item"]'

    async def expand_visible_activity_section() -> None:
        """Expand a compact ``+N``/``更多`` activity row when available.

        Creator Center commonly shows only three official activities and puts
        the rest behind a small ``+6`` control.  The control is intentionally
        searched only inside the nearest ancestor that already contains the
        activity-card selectors; this prevents the similarly named topic
        recommendation ``+15`` above the section from being clicked.  A
        failed or absent expansion is harmless: the visible-card matching
        below remains the safe fallback.
        """
        headings_for_expand = page.get_by_text(re.compile(r"^官方(?:活动|话题)$"))
        for heading_index in range(await headings_for_expand.count()):
            heading = headings_for_expand.nth(heading_index)
            try:
                if not await heading.is_visible():
                    continue
                ancestors = heading.locator("xpath=ancestor::*")
                # Playwright returns XPath ancestors from the outermost node;
                # walk backwards so the first matching group is the local
                # official-activity section rather than the whole page.
                ancestor_count = await ancestors.count()
                for level in range(ancestor_count - 1, -1, -1):
                    group = ancestors.nth(level)
                    if not await group.is_visible():
                        continue
                    if await group.locator(activity_selector).count() == 0:
                        continue
                    controls = group.locator(
                        'button, [role="button"], a, [class*="more"], [class*="expand"]'
                    )
                    for control_index in range(min(await controls.count(), 20)):
                        control = controls.nth(control_index)
                        if not await control.is_visible():
                            continue
                        text = " ".join((await control.inner_text()).split())
                        classes = str(await control.get_attribute("class") or "").lower()
                        is_more = bool(
                            re.fullmatch(r"\+\s*\d+", text)
                            or text in {"更多", "展开", "查看全部", "全部"}
                            or ("more" in classes and len(text) <= 12)
                            or ("expand" in classes and len(text) <= 12)
                        )
                        if not is_more:
                            continue
                        try:
                            await control.scroll_into_view_if_needed()
                            await control.click(force=True)
                            await page.wait_for_timeout(350)
                            heading_text = await heading.inner_text()
                            heading_label = "活动" if "活动" in heading_text else "话题"
                            print(
                                f"[publish-options] 已展开官方{heading_label}列表：{text}",
                                flush=True,
                            )
                        except Exception as exc:
                            # Expansion is an enhancement, not a reason to
                            # abandon a page that still has visible cards.
                            print(f"[publish-options] 展开官方活动列表失败，继续检查已显示项：{exc}", flush=True)
                        return
            except Exception:
                continue

    async def collect(group: Locator, selector: str, *, exclude_wrappers: bool = False) -> list[Locator]:
        found: list[Locator] = []
        controls = group.locator(selector)
        for control_index in range(min(await controls.count(), 100)):
            control = controls.nth(control_index)
            try:
                if not await control.is_visible():
                    continue
                text = " ".join((await control.inner_text()).split())
                if len(text) < 3 or len(text) > 180 or re.fullmatch(r"官方(?:活动|话题)", text):
                    continue
                if exclude_wrappers:
                    classes = str(await control.get_attribute("class") or "").lower()
                    if any(token in classes for token in ("list", "wrapper", "section", "heading")):
                        continue
                    # Do not return a parent whose text is just the combined
                    # text of nested cards.
                    nested = control.locator(activity_selector)
                    if await nested.count():
                        continue
                found.append(control)
            except Exception:
                continue
        return found

    headings = page.get_by_text(re.compile(r"^官方(?:活动|话题)$"))
    # The page can collapse additional official cards behind ``+N``.  Expand
    # once before collecting candidates so a matching hidden card can be
    # selected when the platform exposes it.
    await expand_visible_activity_section()
    headings = page.get_by_text(re.compile(r"^官方(?:活动|话题)$"))
    for index in range(await headings.count()):
        heading = headings.nth(index)
        try:
            if not await heading.is_visible():
                continue
            ancestors = heading.locator("xpath=ancestor::*")
            for level in range(min(await ancestors.count(), 10)):
                group = ancestors.nth(level)
                if not await group.is_visible():
                    continue
                # Prefer selectors that are explicitly tied to the activity
                # cards.  Generic buttons/links are deliberately deferred;
                # at a higher ancestor they also include the page navigation
                # (for example “作品发布”), which must never be treated as an
                # official activity card.
                local = await collect(group, activity_selector)
                if not local:
                    local = await collect(group, generic_selector, exclude_wrappers=True)
                if local:
                    result.extend(local)
                    break
            if not result:
                # Last-resort semantic builds often expose cards only as
                # role=option/button.  Restrict this fallback to an ancestor
                # whose class/text identifies the official-activity section;
                # never search the entire page for a generic button.
                for level in range(min(await ancestors.count(), 10)):
                    group = ancestors.nth(level)
                    try:
                        classes = str(await group.get_attribute("class") or "").lower()
                        if "activity" not in classes:
                            continue
                    except Exception:
                        continue
                    local = await collect(group, semantic_selector)
                    if local:
                        result.extend(local)
                        break
        except Exception:
            continue
        if result:
            break
    return result


async def configure_official_activity(page: Page, item: dict) -> None:
    """Select a matching visible official activity for the current item.

    No matching card is a normal outcome and leaves the page untouched.  If a
    matching card is found but the click cannot be verified, raise a manual
    action error so the page remains open instead of silently publishing with
    an uncertain campaign selection.
    """
    terms = _activity_terms(item)
    if not terms:
        print("[publish-options] 官方活动匹配已关闭或没有有效关键词，保持不选", flush=True)
        return
    candidates = await _activity_candidate_locators(page)
    matched: list[tuple[Locator, str]] = []
    seen_texts: set[str] = set()
    for control in candidates:
        try:
            text = " ".join((await control.inner_text()).split())
        except Exception:
            continue
        normalized = _activity_normalize_text(text)
        if not normalized or normalized in seen_texts:
            continue
        seen_texts.add(normalized)
        # Only accept a configured term when it is visibly present in the
        # activity card.  The reverse containment check (card text contained
        # in a long caption) creates false positives such as a generic card
        # title matching an unrelated sentence, which is unsafe for an
        # official campaign selection.
        if any(term in normalized for term in terms):
            matched.append((control, text))
    # Ancestor cards often contain the concatenated text of every child card
    # (for example ``activity-list``).  When a shorter matching card exists,
    # discard the ancestor so the click targets one activity rather than the
    # whole list.  Distinct cards with equal/independent labels remain
    # ambiguous and are handled by the safety check below.
    if len(matched) > 1:
        normalized_matches = [(_activity_normalize_text(label), control, label) for control, label in matched]
        leaf_matches = [entry for entry in normalized_matches if not any(
            entry[0] != other[0] and entry[0] in other[0]
            for other in normalized_matches
        )]
        if leaf_matches:
            matched = [(control, label) for _, control, label in leaf_matches]
    if not matched:
        print(f"[publish-options] 官方活动没有匹配项（关键词：{', '.join(terms)}），保持不选", flush=True)
        return
    # If the user already selected exactly one of several matching cards while
    # the page was paused, that is now explicit evidence and can be accepted.
    # Otherwise selecting among multiple live campaigns would be ambiguous;
    # leave the page for manual confirmation instead.
    selected_matches: list[tuple[Locator, str]] = []
    for control, label in matched:
        try:
            if await _activity_selected(control):
                selected_matches.append((control, label))
        except Exception:
            continue
    if len(selected_matches) == 1:
        print(f"[publish-options] 官方活动已由页面确认选中：{selected_matches[0][1]}", flush=True)
        return
    if len(selected_matches) > 1:
        labels = "、".join(text for _, text in selected_matches[:4])
        raise PublishOptionsError(f"官方活动同时选中多个匹配项（{labels}），请在页面保留一个")
    if len(matched) > 1:
        labels = "、".join(text for _, text in matched[:4])
        raise PublishOptionsError(f"官方活动存在多个匹配项（{labels}），请在页面确认后继续")
    control, label = matched[0]
    try:
        if await _activity_selected(control):
            print(f"[publish-options] 官方活动已选中：{label}", flush=True)
            return
        await control.scroll_into_view_if_needed()
        await control.click(force=True)
        for _ in range(12):
            check_stop_requested("选择抖音官方活动")
            await page.wait_for_timeout(150)
            if await _activity_selected(control):
                print(f"[publish-options] 已选择匹配官方活动：{label}", flush=True)
                return
    except PublishStopRequested:
        raise
    except Exception as exc:
        raise PublishOptionsError(f"选择官方活动“{label}”失败：{exc}") from exc
    raise PublishOptionsError(f"官方活动“{label}”点击后未确认选中，页面已保留")


async def _find_pico_switch(page: Page) -> Locator | None:
    """Locate the switch next to the visible PICO authorization row."""
    labels = page.get_by_text(re.compile(r"^PICO(?:协议)?$", re.I))
    for index in range(await labels.count()):
        label = labels.nth(index)
        try:
            if not await label.is_visible():
                continue
            ancestors = label.locator("xpath=ancestor::*")
            for level in range(min(await ancestors.count(), 8)):
                group = ancestors.nth(level)
                if not await group.is_visible():
                    continue
                for selector in (
                    '[role="switch"]', '[role="checkbox"]', 'input[type="checkbox"]',
                    'button[aria-pressed]', '[data-state]', '[class*="switch"]',
                ):
                    controls = group.locator(selector)
                    for control_index in range(await controls.count()):
                        control = controls.nth(control_index)
                        if await control.is_visible():
                            return control
        except Exception:
            continue
    return None


async def _pico_enabled(control: Locator) -> bool:
    try:
        return bool(
            await control.evaluate(
                """el => {
                  if (el.matches && el.matches('input[type=checkbox]')) return !!el.checked;
                  for (const node of [el, el.parentElement, el.parentElement && el.parentElement.parentElement, el.parentElement && el.parentElement.parentElement && el.parentElement.parentElement.parentElement]) {
                    if (!node) continue;
                    const values = [node.getAttribute && node.getAttribute('aria-checked'), node.getAttribute && node.getAttribute('aria-pressed'), node.getAttribute && node.getAttribute('data-state')];
                    if (values.some(value => ['true','checked','on'].includes(String(value).toLowerCase()))) return true;
                    if (/(^|[-_\\s])(checked|active|on|enabled)([-_\\s]|$)/.test(String(node.className || '').toLowerCase())) return true;
                  }
                  return false;
                }"""
            )
        )
    except Exception:
        return False


async def configure_pico_authorization(page: Page, item: dict) -> None:
    """Make the visible PICO consent switch match the queue's explicit choice.

    ``False`` is intentional: a resumed draft may retain a previously checked
    switch, so leaving it untouched could publish with an authorization the
    queue no longer requests.  A missing switch is tolerated when the desired
    state is off, but is a hard manual-action error when the user opted in.
    """
    if not is_panorama_item(item):
        return
    desired = app.normalize_bool(item.get("pico_enabled", False), default=False)
    control = await _find_pico_switch(page)
    if control is None:
        if not desired:
            return
        raise PublishOptionsError("队列要求授权 PICO 播放，但发布页没有找到 PICO 开关，页面已保留")
    try:
        current = await _pico_enabled(control)
        if current == desired:
            print(f"[publish-options] PICO 授权状态已确认：{'开启' if desired else '关闭'}", flush=True)
            return
        await control.scroll_into_view_if_needed()
        await control.click(force=True)
        for _ in range(12):
            check_stop_requested("设置 PICO 授权")
            await page.wait_for_timeout(150)
            if await _pico_enabled(control) == desired:
                print(f"[publish-options] 已将 PICO 授权设为{'开启' if desired else '关闭'}", flush=True)
                return
    except PublishStopRequested:
        raise
    except Exception as exc:
        raise PublishOptionsError(f"设置 PICO 授权失败：{exc}") from exc
    raise PublishOptionsError(
        f"PICO 开关点击后未确认{'开启' if desired else '关闭'}，页面已保留"
    )


def _schedule_mode(item: dict) -> str:
    """Return a strict queue schedule mode without silently guessing."""
    try:
        return app.normalize_schedule_mode(item.get("schedule_mode", "immediate"))
    except (AttributeError, ValueError) as exc:
        raise PublishScheduleError(f"定时发布方式无效：{item.get('schedule_mode')!r}") from exc


def _schedule_at_value(item: dict) -> datetime:
    """Parse the queue's local minute schedule as a naive wall-clock value."""
    raw = str(item.get("schedule_at") or "").strip()
    if not raw:
        raise PublishScheduleError("已选择定时发布，但没有配置发布时间")
    try:
        normalized = app.normalize_schedule_at(raw)
        value = datetime.fromisoformat(normalized)
    except (AttributeError, ValueError) as exc:
        raise PublishScheduleError(
            f"定时发布时间格式无效：{raw!r}；请使用 YYYY-MM-DDTHH:MM"
        ) from exc
    if value.tzinfo is not None:
        value = value.astimezone().replace(tzinfo=None)
    return value.replace(second=0, microsecond=0)


def publish_success_note(item: dict) -> str:
    """Return the queue note appropriate to the platform action just done."""
    try:
        if _schedule_mode(item) == "scheduled":
            target = _schedule_at_value(item)
            return f"已预约抖音定时发布（{target:%Y-%m-%d %H:%M}），等待平台执行"
    except PublishScheduleError:
        # A malformed item should already have been stopped before clicking;
        # keep the fallback useful if this helper is called while recovering an
        # older queue record.
        return "已预约抖音定时发布，等待平台执行"
    return "已发布，平台审核中"


async def _browser_timezone(page: Page) -> str | None:
    """Read the browser's IANA timezone when the page exposes it."""
    try:
        value = await page.evaluate("() => Intl.DateTimeFormat().resolvedOptions().timeZone")
    except Exception:
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


async def _schedule_target_for_browser(page: Page, item: dict) -> tuple[datetime, str]:
    """Convert the queue's local wall time to the browser's local wall time.

    The queue is written by the local app, while Playwright may be attached to a
    Chrome profile with a different timezone.  Passing a UTC string to Douyin's
    date picker is unsafe because the picker accepts local wall-clock text.
    """
    target = _schedule_at_value(item)
    host_now = datetime.now().astimezone()
    host_tz = host_now.tzinfo
    browser_zone_name = await _browser_timezone(page)
    browser_tz = host_tz
    if browser_zone_name:
        try:
            browser_tz = ZoneInfo(browser_zone_name)
        except (ZoneInfoNotFoundError, ValueError):
            # Keep the host offset if the browser reports an unknown zone.
            browser_zone_name = host_now.tzname() or "本机时区"
    else:
        browser_zone_name = host_now.tzname() or "本机时区"
    if host_tz is None:
        return target, browser_zone_name
    try:
        aware = target.replace(tzinfo=host_tz)
        converted = aware.astimezone(browser_tz).replace(tzinfo=None)
    except (OverflowError, ValueError):
        converted = target
    return converted.replace(second=0, microsecond=0), browser_zone_name


async def _find_schedule_scope(page: Page) -> Locator | None:
    """Find the visible Douyin publish-time block without using hash classes."""
    # The current page uses a CSS-module class beginning with schedule-part.
    # Keep this as a first, narrow candidate, but verify the visible labels so a
    # stale/hidden template cannot be selected.
    parts = page.locator('div[class*="schedule-part"]')
    for index in range(await parts.count() - 1, -1, -1):
        part = parts.nth(index)
        try:
            if not await part.is_visible():
                continue
            text = " ".join((await part.inner_text()).split())
            if "立即发布" in text and "定时发布" in text:
                return part
        except Exception:
            continue

    # Fallback for a future class-name change: locate a visible label pair and
    # return their nearest common div ancestor.
    labels = page.locator("label")
    matching: list[Locator] = []
    for index in range(await labels.count()):
        label = labels.nth(index)
        try:
            if not await label.is_visible() or await label.locator("input").count() == 0:
                continue
            text = " ".join((await label.inner_text()).split())
            if text in {"立即发布", "定时发布"}:
                matching.append(label)
        except Exception:
            continue
    if len(matching) < 2:
        return None
    first = matching[0]
    ancestors = first.locator("xpath=ancestor::div")
    for index in range(await ancestors.count()):
        ancestor = ancestors.nth(index)
        try:
            if not await ancestor.is_visible():
                continue
            text = " ".join((await ancestor.inner_text()).split())
            if "立即发布" in text and "定时发布" in text:
                return ancestor
        except Exception:
            continue
    return None


async def _find_schedule_option(scope: Locator, option_text: str) -> Locator | None:
    labels = scope.locator("label")
    for index in range(await labels.count() - 1, -1, -1):
        label = labels.nth(index)
        try:
            if not await label.is_visible() or await label.locator("input").count() == 0:
                continue
            if " ".join((await label.inner_text()).split()) == option_text:
                return label
        except Exception:
            continue
    return None


async def _wait_for_schedule_scope(page: Page, timeout_ms: int = 12000) -> Locator:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        check_stop_requested("识别抖音发布时间设置")
        scope = await _find_schedule_scope(page)
        if scope is not None:
            return scope
        await page.wait_for_timeout(250)
    raise PublishScheduleError("未找到抖音“发布时间”设置，已保留当前页面")


async def _wait_for_schedule_option(
    page: Page,
    scope: Locator,
    option_text: str,
    *,
    select: bool,
) -> Locator:
    option = await _find_schedule_option(scope, option_text)
    if option is None:
        raise PublishScheduleError(f"未找到抖音“{option_text}”选项，已保留当前页面")
    if not select or await _option_is_selected(option):
        return option
    try:
        await option.scroll_into_view_if_needed()
        await option.click(force=True)
        for _ in range(24):
            check_stop_requested(f"选择抖音{option_text}")
            await page.wait_for_timeout(150)
            fresh_scope = await _find_schedule_scope(page)
            if fresh_scope is not None:
                scope = fresh_scope
                option = await _find_schedule_option(scope, option_text) or option
            if await _option_is_selected(option):
                return option
    except PublishStopRequested:
        raise
    except Exception as exc:
        raise PublishScheduleError(f"选择抖音“{option_text}”失败：{exc}") from exc
    raise PublishScheduleError(f"抖音未确认选中“{option_text}”，已保留当前页面")


async def _find_schedule_inputs(scope: Locator, page: Page | None = None) -> list[Locator]:
    """Find dynamic DatePicker inputs mounted after radio selection.

    Douyin has shipped both a single ``日期和时间`` text input and a paired
    date/time input revision.  Search the schedule block first, then the
    visible page-level DatePicker portal (some revisions render the popup next
    to the document root).  Never fall back to an arbitrary caption input.
    """
    selectors = (
        'input[placeholder="日期和时间"]',
        'input[placeholder*="日期"]',
        'input[placeholder*="时间"]',
        'input[aria-label*="日期"]',
        'input[aria-label*="时间"]',
        '[class*="date-picker"] input',
        '[class*="date-"] input',
    )
    roots = [scope]
    if page is not None:
        roots.append(page.locator('div[class*="date-picker"]'))
    for root in roots:
        # A class/placeholder change should not make us accidentally fill the
        # title or caption.  Only accept generic inputs inside a DatePicker.
        date_picker = root if root is not scope else root.locator('[class*="date-picker"]')
        for index in range(await date_picker.count()):
            node = date_picker.nth(index)
            try:
                if not await node.is_visible():
                    continue
                inputs = node.locator("input")
                found: list[Locator] = []
                for input_index in range(await inputs.count()):
                    candidate = inputs.nth(input_index)
                    if not await candidate.is_visible() or not await candidate.is_enabled():
                        continue
                    type_name = (await candidate.get_attribute("type") or "text").lower()
                    if type_name in {"checkbox", "radio", "file", "range"}:
                        continue
                    found.append(candidate)
                if found:
                    return found
            except Exception:
                continue
        # Some revisions omit the date-picker class but expose useful
        # placeholders/aria labels.  Keep this search scoped to the schedule
        # block (or the explicitly supplied portal root).
        for selector in selectors:
            candidates = root.locator(selector)
            found = []
            for index in range(await candidates.count()):
                candidate = candidates.nth(index)
                try:
                    if not await candidate.is_visible() or not await candidate.is_enabled():
                        continue
                    type_name = (await candidate.get_attribute("type") or "text").lower()
                    if type_name in {"checkbox", "radio", "file", "range"}:
                        continue
                    found.append(candidate)
                except Exception:
                    continue
            if found:
                return found
    return []


async def _find_schedule_input(scope: Locator, page: Page | None = None) -> Locator | None:
    """Compatibility wrapper returning the first DatePicker input."""
    inputs = await _find_schedule_inputs(scope, page)
    return inputs[0] if inputs else None


def _parse_schedule_display(value: str) -> datetime | None:
    """Parse the visible DatePicker value in Chinese or ISO-like formats."""
    text = " ".join(str(value or "").split())
    match = re.search(
        r"(20\d{2})\s*(?:[-/]\s*|年\s*)(\d{1,2})\s*(?:[-/]\s*|月\s*)(\d{1,2})"
        r"\s*(?:日\s*)?(?:[T ]+)(\d{1,2})\s*[:：]\s*(\d{2})",
        text,
    )
    if match is not None:
        year, month, day, hour, minute = (int(match.group(index)) for index in range(1, 6))
    else:
        # A browser/profile using an English locale may expose the same
        # control as ``MM/DD/YYYY HH:MM``.  Accept it for read-back only; the
        # value we type remains the unambiguous ISO-like form above.
        us_match = re.search(
            r"(\d{1,2})\s*[/\-]\s*(\d{1,2})\s*[/\-]\s*(20\d{2})"
            r"\s*(?:[T ]+)(\d{1,2})\s*[:：]\s*(\d{2})",
            text,
        )
        if us_match is None:
            return None
        month, day, year, hour, minute = (int(us_match.group(index)) for index in range(1, 6))
    try:
        return datetime(year, month, day, hour, minute)
    except ValueError:
        return None


async def _schedule_input_value(input_node: Locator) -> str:
    try:
        return (await input_node.input_value()).strip()
    except Exception:
        return (await input_node.get_attribute("value") or "").strip()


async def _fill_schedule_input(input_node: Locator, target_text: str) -> None:
    """Fill the controlled DatePicker and commit it through blur/Enter."""
    # A few page revisions expose a native ``datetime-local`` control instead
    # of the usual text DatePicker.  Native controls expect the ISO ``T``
    # separator; the visible Semi input accepts a space.  Adapt only the
    # native variant so both revisions receive the format they understand.
    try:
        input_type = (await input_node.get_attribute("type") or "text").lower()
    except Exception:
        input_type = "text"
    fill_text = target_text.replace(" ", "T", 1) if input_type == "datetime-local" else target_text
    await input_node.scroll_into_view_if_needed()
    try:
        await input_node.click()
        await input_node.fill(fill_text)
    except Exception:
        # Some revisions render a readonly-looking input but still accept
        # keyboard input after focus.
        await input_node.click(force=True)
        await input_node.press("ControlOrMeta+A")
        await input_node.type(fill_text, delay=10)
    try:
        await input_node.press("Enter")
    except Exception:
        pass
    try:
        await input_node.press("Tab")
    except Exception:
        pass


async def _fill_schedule_inputs(inputs: list[Locator], target: datetime) -> None:
    """Fill either a combined DatePicker or separate date/time inputs."""
    if not inputs:
        raise PublishScheduleError("未找到可填写的定时日期控件")
    if len(inputs) == 1:
        await _fill_schedule_input(inputs[0], target.strftime("%Y-%m-%d %H:%M"))
        return
    # The paired revision normally renders date first and time second.  A
    # localized revision can reverse them; use placeholder/aria hints when
    # available, then fall back to the documented date-first order.
    first_hint = " ".join(
        filter(
            None,
            [
                await inputs[0].get_attribute("placeholder"),
                await inputs[0].get_attribute("aria-label"),
                await inputs[0].get_attribute("name"),
            ],
        )
    ).lower()
    second_hint = " ".join(
        filter(
            None,
            [
                await inputs[1].get_attribute("placeholder"),
                await inputs[1].get_attribute("aria-label"),
                await inputs[1].get_attribute("name"),
            ],
        )
    ).lower()
    date_first = not (
        ("时间" in first_hint or "time" in first_hint)
        and ("日期" in second_hint or "date" in second_hint)
    )
    ordered = inputs if date_first else [inputs[1], inputs[0]]
    # Use keyboard entry so controlled inputs receive the same events as a
    # user typing.
    await _fill_schedule_input(ordered[0], target.strftime("%Y-%m-%d"))
    await _fill_schedule_input(ordered[1], target.strftime("%H:%M"))


async def _read_schedule_inputs(inputs: list[Locator]) -> tuple[datetime | None, str]:
    values: list[str] = []
    for input_node in inputs:
        try:
            value = await _schedule_input_value(input_node)
        except Exception:
            value = ""
        if value:
            values.append(value)
    combined = " ".join(values)
    return _parse_schedule_display(combined), combined


async def configure_publish_schedule(page: Page, item: dict) -> None:
    """Configure and verify Douyin's *发布​​时间* control before clicking 发布.

    The current Creator page uses custom checkbox-backed radio labels.  Selecting
    “定时发布” mounts a dynamic Semi DatePicker (`placeholder="日期和时间"`),
    which is absent from the immediate-publish DOM snapshot.  We therefore
    re-discover the input after the radio click and require a matching read-back;
    a missing/clamped value raises a manual-action error and leaves the page open.
    """
    check_stop_requested("设置抖音发布时间")
    mode = _schedule_mode(item)
    scope = await _wait_for_schedule_scope(page)
    if mode == "immediate":
        await _wait_for_schedule_option(page, scope, "立即发布", select=True)
        # Re-read the section after React has rendered the selected state.
        scope = await _wait_for_schedule_scope(page, timeout_ms=3000)
        immediate = await _find_schedule_option(scope, "立即发布")
        if immediate is None or not await _option_is_selected(immediate):
            raise PublishScheduleError("抖音未确认“立即发布”，已保留当前页面")
        return

    target, browser_zone = await _schedule_target_for_browser(page, item)
    now_local = datetime.now().astimezone()
    now_in_browser = now_local
    if browser_zone:
        try:
            now_in_browser = now_local.astimezone(ZoneInfo(browser_zone))
        except (ZoneInfoNotFoundError, ValueError):
            pass
    if target <= now_in_browser.replace(tzinfo=None):
        raise PublishScheduleError(
            f"定时发布时间 {target:%Y-%m-%d %H:%M} 已经过期（页面时区 {browser_zone}），已保留当前页面"
        )

    await _wait_for_schedule_option(page, scope, "定时发布", select=True)
    # The DatePicker is mounted only after the radio state changes.
    input_nodes: list[Locator] = []
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        check_stop_requested("等待抖音定时日期控件")
        fresh_scope = await _find_schedule_scope(page)
        if fresh_scope is not None:
            scope = fresh_scope
        input_nodes = await _find_schedule_inputs(scope, page)
        if input_nodes:
            break
        await page.wait_for_timeout(250)
    if not input_nodes:
        raise PublishScheduleError("选择“定时发布”后未出现“日期和时间”控件，已保留当前页面")

    try:
        await _fill_schedule_inputs(input_nodes, target)
    except PublishStopRequested:
        raise
    except Exception as exc:
        raise PublishScheduleError(f"填写抖音定时发布时间失败：{exc}") from exc

    # Allow the controlled component to normalize/validate the value.  A
    # platform minimum (currently commonly 2h, but account versions may allow
    # 10m) is intentionally learned from the page rather than hard-coded.
    await page.wait_for_timeout(700)
    read_back, value = await _read_schedule_inputs(input_nodes)
    if read_back is None:
        # The input may have been re-mounted after the date selection.
        fresh_scope = await _find_schedule_scope(page)
        if fresh_scope is not None:
            input_nodes = await _find_schedule_inputs(fresh_scope, page)
            if input_nodes:
                read_back, value = await _read_schedule_inputs(input_nodes)
    if read_back is None:
        raise PublishScheduleError(
            f"抖音定时控件未回读有效时间（当前值：{value or '空'}），已保留当前页面"
        )
    if read_back != target:
        body = ""
        try:
            body = await page.locator("body").inner_text()
        except Exception:
            pass
        hint = ""
        for marker in ("发布时间不能", "时间不能", "请选择", "定时发布"):
            if marker in body:
                hint = f"；页面提示包含“{marker}”"
                break
        raise PublishScheduleError(
            f"抖音将定时发布时间调整为 {read_back:%Y-%m-%d %H:%M}，"
            f"目标为 {target:%Y-%m-%d %H:%M}（页面时区 {browser_zone}）{hint}；已保留当前页面"
        )

    scope = await _wait_for_schedule_scope(page, timeout_ms=3000)
    scheduled = await _find_schedule_option(scope, "定时发布")
    if scheduled is None or not await _option_is_selected(scheduled):
        raise PublishScheduleError("定时发布时间已填写，但抖音未确认“定时发布”，已保留当前页面")
    set_publish_state(
        running=True,
        message=f"已设置抖音定时发布时间 {target:%Y-%m-%d %H:%M}（{browser_zone}）",
        current=item.get("name", ""),
        action_required=False,
    )


async def submission_visible_in_management(page: Page, item: dict) -> bool:
    """在内容管理页按作品卡片核对本条，审核中/预约中也算提交成功。

    旧实现会在整个 body 里搜索歌名/文件名；账号里有 179 条作品时，旧的
    “开场”“不朽”作品会立刻命中，导致没有发布也被标记为已提交。这里
    改为：只查作品卡片，优先使用带日期的作品标题，并要求卡片带有今天
    的日期或审核/预约状态。
    """
    if "/creator-micro/content/manage" not in page.url:
        return False
    needles = [_normalize_ui_text(value) for value in _item_identity_needles(item)]
    needles = [needle for needle in needles if needle]
    if not needles:
        return False
    await page.wait_for_timeout(1200)
    now = datetime.now()
    date_markers = (
        f"{now.year}年{now.month}月{now.day}日",
        f"{now.year}-{now.month:02d}-{now.day:02d}",
    )
    cards = page.locator('[class*="video-card-content"]')
    count = await cards.count()
    if not count:
        # 发布后跳转到 manage?enter_from=publish 时，列表首屏可能还没挂载；
        # 用页面正文里的完整作品标题/当天日期兜底，避免把已成功的发布
        # 误判成“未返回明确结果”而空等。
        body_text = await page.locator("body").inner_text()
        normalized_body = _normalize_ui_text(body_text)
        if any(needle in normalized_body for needle in needles):
            if "enter_from=publish" in page.url:
                return True
            if any(marker in body_text for marker in date_markers):
                return True
        return False
    for index in range(count):
        try:
            text = await cards.nth(index).inner_text()
        except Exception:
            continue
        normalized = _normalize_ui_text(text)
        matched = next((needle for needle in needles if needle in normalized), "")
        if not matched:
            continue
        if any(marker in text for marker in date_markers):
            return True
        if any(word in text for word in ("审核中", "预约中", "定时发布", "审核通过")):
            return True
        # 卡片可能折叠日期。只有命中的是带场次/日期的完整标题时才放行；
        # 仅凭简介/歌名命中旧作品不能算本次提交。
        if matched == needles[0] and len(matched) >= 8:
            return True
    return False


async def management_page_confirms(page: Page, item: dict, initial_url: str | None = None) -> bool:
    """Check every tab for a management page that confirms this item."""
    try:
        pages = list(page.context.pages)
    except Exception:
        pages = [page]
    for candidate in pages:
        try:
            if candidate.is_closed():
                continue
            if "/creator-micro/content/manage" not in candidate.url:
                continue
            if initial_url and candidate.url == initial_url:
                continue
            if await submission_visible_in_management(candidate, item):
                return True
        except Exception:
            continue
    return False


async def find_publish_button(page: Page) -> Locator | None:
    """Find the real submit button, never the left-navigation "发布视频" tab.

    抖音编辑器同时存在导航栏“发布视频”和底部主按钮“发布”。旧实现按 DOM
    倒序取第一个匹配，可能点到导航，直接离开编辑页，造成“上传完就关闭、
    开始下一条”的假象。这里优先精确文本“发布”，并排除 nav/header/tab。
    """
    exact: list[tuple[int, Locator]] = []
    fallback: list[tuple[int, Locator]] = []
    for collection in (page.locator("button"), page.locator('[role="button"]')):
        try:
            count = await collection.count()
        except Exception:
            continue
        for index in range(count):
            button = collection.nth(index)
            try:
                if not await button.is_visible() or not await button.is_enabled():
                    continue
                text = " ".join((await button.inner_text()).split())
                aria = (await button.get_attribute("aria-label") or "").strip()
                title = (await button.get_attribute("title") or "").strip()
                label = text or aria or title
                if label not in {"发布", "发布视频"}:
                    continue
                info = await button.evaluate("""el => {
                    let node = el, nav = false, cls = '';
                    for (let i = 0; i < 5 && node; i++) {
                        const c = typeof node.className === 'string' ? node.className : '';
                        cls += ' ' + c;
                        if (['NAV', 'HEADER', 'ASIDE'].includes(node.tagName)
                            || /nav|sidebar|menu|tab/i.test(c)) nav = true;
                        node = node.parentElement;
                    }
                    return {nav, cls, tag: el.tagName};
                }""")
                score = 0
                if info.get("tag") == "BUTTON":
                    score += 20
                if re.search(r"primary|publish|submit|confirm|footer|fixed", str(info.get("cls")), re.I):
                    score += 100
                if info.get("nav"):
                    score -= 200
                (exact if label == "发布" else fallback).append((score, button))
            except Exception:
                continue
    for pool in (exact, fallback):
        for score, button in sorted(pool, key=lambda item: item[0], reverse=True):
            try:
                if score < 0:
                    continue
                return button
            except Exception:
                continue
    return None

async def click_publish_button(page: Page) -> Locator:
    button = await find_publish_button(page)
    if button is None:
        raise RuntimeError("没有找到发布按钮，页面可能需要先完成其他必填项")
    try:
        await button.scroll_into_view_if_needed()
    except Exception:
        pass
    label = " ".join((await button.inner_text()).split())
    log(f"点击发布按钮：{label!r}")
    await button.click()
    return button


async def wait_for_publish_result(
    page: Page,
    item: dict,
    timeout_seconds: int = 45,
    previous_url: str | None = None,
    before_body: str = "",
) -> bool:
    """点击发布后确认“提交回执”，不等待作品审核结果。

    抖音点击发布后通常跳到 /content/manage?enter_from=publish；看到这个
    跳转或新的成功提示即可继续下一条。作品处于“审核中”不影响队列推进。
    旧提示、旧作品和导航文字仍不算提交成功。
    """
    previous_url = previous_url or page.url
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        await wait_if_paused(page, item, "等待抖音提交回执")
        await page.wait_for_timeout(1500)
        body = await page.locator("body").inner_text()
        # 抖音提交后通常会跳到 /content/manage?enter_from=publish。这个跳转
        # 本身就是提交回执；不再等作品卡片或审核状态，直接进入下一条。
        if previous_url and page.url != previous_url and "/content/manage" in page.url:
            log(f"已跳转内容管理，提交成功：{item.get('name', '')}")
            return True
        if page_publish_succeeded(
            page.url,
            body,
            previous_url,
            item=item,
            before_body=before_body,
        ):
            log(f"页面出现新的提交成功提示：{item.get('name', '')}")
            return True
        if any(word in body for word in ("需要上传封面", "请上传封面", "上传封面后才能发布", "封面不能为空")):
            raise ManualActionRequired("抖音要求上传封面")
        set_publish_state(
            running=True,
            message=f"已点击发布，正在确认提交回执 {item.get('name', '')}",
            current=item.get("name", ""),
            action_required=False,
        )
    raise PublishResultUncertain("点击发布后未收到提交回执")


async def wait_for_manual_publish(
    page: Page,
    item: dict,
    reason: str = "页面需要人工处理",
    timeout_seconds: int = 30 * 60,
    allow_click: bool = True,
) -> bool:
    """异常时保留当前页面，并在封面/权限/发布时间就绪后自动继续点击发布。

    发布时间配置是点击发布的硬门槛：如果定时单选项或日期时间回读失败，
    本轮只继续保留并观察页面，绝不会落入按钮点击分支。
    """
    set_publish_state(
        running=True,
        message=f"{reason}；正在保留页面并尝试自动完成发布",
        current=item.get("name") or item.get("path") or "（无名）",
        action_required=True,
    )
    deadline = time.monotonic() + timeout_seconds
    clicked = False
    cover_attempted = False
    panorama_ready = not is_panorama_item(item)
    panorama_next_retry = 0.0
    initial_url = page.url
    initial_body = await page.locator("body").inner_text()
    while time.monotonic() < deadline:
        try:
            await wait_if_paused(page, item, "等待人工校验完成")
            body = await page.locator("body").inner_text()
            # 只有 URL 真的离开当前编辑页后才查内容管理；否则页面里可能
            # 还留着上一条作品的旧卡片/旧提示。
            if await management_page_confirms(page, item, initial_url):
                return True
            if page_publish_succeeded(
                page.url,
                body,
                initial_url,
                item=item,
                before_body=initial_body,
            ):
                return True
            if is_panorama_item(item) and not panorama_ready and time.monotonic() >= panorama_next_retry:
                try:
                    await configure_panorama_format(page, item)
                    panorama_ready = True
                except PanoramaEntryError:
                    # Keep polling the same page so a user can select the
                    # format manually after a UI redesign or permission prompt.
                    panorama_next_retry = time.monotonic() + 5

            # 全景页可能在格式确认后仍显示大文件上传进度，或已经打开了
            # 封面抽帧弹窗。人工处理轮询期间也要沿用同一套安全门：只有
            # 上传进度消失后才尝试自动选帧，不能把遮罩层下的发布按钮当成
            # 可点击状态。
            if is_panorama_item(item) and not clicked and panorama_ready:
                upload_active, upload_percent = await _visible_panorama_upload_progress(
                    page, str(item.get("name") or "")
                )
                if upload_active:
                    suffix = f"（{upload_percent}%）" if upload_percent is not None else ""
                    set_publish_state(
                        running=True,
                        message=f"正在等待全景视频 {item.get('name', '')} 上传完成{suffix}",
                        current=item.get("name", ""),
                        action_required=True,
                    )
                    await page.wait_for_timeout(3000)
                    continue
                if await cover_modal_visible(page):
                    video_path = app.safe_video_path(item.get("path", ""))
                    if video_path is not None:
                        try:
                            await configure_cover(page, item, video_path)
                            body = await page.locator("body").inner_text()
                        except Exception:
                            # 抽帧仍在切换时保留页面，下一轮继续检查；
                            # 不关闭浏览器，也不放行被遮罩拦截的发布按钮。
                            await page.wait_for_timeout(1500)
                            continue
            # 发布校验可能在点击后才返回“请上传封面”。继续使用同一
            # 个编辑页自动打开/保存封面，不要求用户接管最后一步。
            cover_required = await visible_text_present(
                page,
                re.compile(r"需要上传封面|请上传封面|上传封面后才能发布|封面不能为空|横/竖双封面缺失"),
            )
            if cover_required and not cover_attempted:
                cover_attempted = True
                video_path = app.safe_video_path(item.get("path", ""))
                if video_path is not None:
                    try:
                        await configure_cover(page, item, video_path)
                        body = await page.locator("body").inner_text()
                    except Exception:
                        # 页面改版/弹窗未准备好时继续轮询；页面保持打开，
                        # 后续仍可由用户或下一次恢复任务处理。
                        pass
            # 用户可能已经在页面上选择了封面；只要检测到封面通过，
            # 自动化就继续点击发布，不把最后一步交回用户。
            if not clicked and allow_click and panorama_ready and (cover_is_ready(body) or (cover_required and not await cover_modal_visible(page))):
                # 人工恢复也不能在标题/描述缺失时点击发布。先复核当前编辑器，
                # 不一致时再填一次；仍不一致就继续保留页面，不进入点击分支。
                content_ok = await _editor_content_verified(page, item)
                if not content_ok:
                    try:
                        await configure_title_and_caption(page, item)
                    except Exception as exc:
                        log(f"人工恢复：重新填写标题/描述失败：{exc}")
                    content_ok = await _editor_content_verified(page, item)
                if not content_ok:
                    log(f"人工恢复：标题/描述仍未通过回读，暂不点击发布：{item.get('name', '')}")
                    await page.wait_for_timeout(1500)
                    continue
                try:
                    # The first attempt may have been paused because the
                    # official-activity card or the PICO consent switch was
                    # still rendering.  Re-run both panorama-only gates on
                    # every recovery pass; otherwise this fallback could
                    # click 发布 after a manual page change without proving
                    # the requested platform settings again.
                    if is_panorama_item(item):
                        await configure_official_activity(page, item)
                        await configure_pico_authorization(page, item)
                    await configure_publish_options(page, item)
                    # 和权限一样，发布时间必须在真正查找/点击发布按钮前
                    # 完成并回读确认。尤其是定时控件动态挂载失败时，
                    # 不能因为页面上仍有“发布”按钮而误点成立即发布。
                    await configure_publish_schedule(page, item)
                except PublishScheduleError:
                    # The appointment cannot be verified.  Return control to
                    # the local queue instead of retrying a deterministic
                    # schedule error for the full manual-polling window.
                    return False
                except PublishOptionsError:
                    # 权限控件未出现或未确认时继续保留页面，等待页面
                    # 渲染完成/用户处理；绝不带着未知权限/时间点击发布。
                    await page.wait_for_timeout(1500)
                    continue
                button = await find_publish_button(page)
                if button is not None:
                    await click_publish_button(page)
                    clicked = True
                    set_publish_state(
                        running=True,
                        message=f"已自动点击发布，正在等待 {item.get('name', '')} 的结果",
                        current=item.get("name", ""),
                        action_required=False,
                    )
        except PublishStopRequested:
            raise
        except Exception:
            # 页面短暂刷新或按钮重新渲染时继续观察，避免误关窗口。
            await page.wait_for_timeout(1500)
            continue
        await page.wait_for_timeout(3000)
    return False


def parse_cover_seconds(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if ":" in text:
            minutes, seconds = text.split(":", 1)
            return max(0.0, float(minutes) * 60 + float(seconds))
        return max(0.0, float(text))
    except ValueError:
        return None


def read_video_duration(video_path: Path) -> float | None:
    """在 macOS 上读取本地视频时长；读取失败不影响人工选封面。"""
    try:
        result = subprocess.run(
            ["mdls", "-raw", "-name", "kMDItemDurationSeconds", str(video_path)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        value = float(result.stdout.strip())
        return value if value > 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


async def capture_cover_frame(page: Page) -> Path | None:
    """从已上传的视频元素截取当前帧，作为自动封面文件。"""
    check_stop_requested("截取自动封面")
    debug_dir = app.STATE_DIR / "debug"
    if not debug_dir.exists():
        try:
            debug_dir.mkdir(exist_ok=True)
        except PermissionError:
            pass
    target = debug_dir / "auto_cover.jpg"
    video = page.locator("video").first
    try:
        if await video.count():
            data_url = await video.evaluate(
                """video => {
                  try {
                    const width = video.videoWidth || 1280;
                    const height = video.videoHeight || 720;
                    const canvas = document.createElement('canvas');
                    canvas.width = width;
                    canvas.height = height;
                    const ctx = canvas.getContext('2d');
                    ctx.drawImage(video, 0, 0, width, height);
                    return canvas.toDataURL('image/jpeg', 0.9);
                  } catch (_) { return ''; }
                }"""
            )
            if isinstance(data_url, str) and data_url.startswith("data:image/"):
                target.write_bytes(base64.b64decode(data_url.split(",", 1)[1]))
                return target
            await video.screenshot(path=str(target), type="jpeg", quality=90)
            return target if target.exists() else None
    except PublishStopRequested:
        raise
    except Exception:
        try:
            await page.screenshot(path=str(target), type="jpeg", quality=90)
            return target if target.exists() else None
        except Exception:
            return None
    return None


async def upload_cover_image(page: Page, cover_path: Path | None = None) -> bool:
    """Upload a cover through the normal cover editor.

    2026 版封面编辑器里的“上传封面”会打开原生文件选择器，页面中同时存在
    “付费内容封面”的隐藏 input。旧实现直接找可见 input，结果把图片传到了
    付费内容区域，普通封面仍是视频首帧。优先走 expect_file_chooser；找不到
    选择器时才回退到 accept 含 bmp/webp 的封面 input。
    """
    check_stop_requested("上传自定义封面" if cover_path else "上传自动封面")
    frame_path = cover_path if cover_path is not None else await capture_cover_frame(page)
    if frame_path is None:
        return False

    upload_tab = await last_visible(
        page.get_by_text("上传封面", exact=True),
        page.get_by_role("button", name=re.compile(r"^上传封面$")),
    )
    if upload_tab is not None:
        try:
            async with page.expect_file_chooser(timeout=6000) as chooser_info:
                await upload_tab.click(force=True)
            chooser = await chooser_info.value
            await chooser.set_files(str(frame_path))
            await page.wait_for_timeout(2500)
            log(f"已通过文件选择器上传封面：{frame_path.name}")
            return True
        except PlaywrightTimeoutError:
            log("点击“上传封面”未触发文件选择器，尝试页面内的隐藏封面 input")
        except PublishStopRequested:
            raise
        except Exception as exc:
            log(f"文件选择器上传封面失败：{exc}")

    candidates: list[Locator] = []
    image_inputs = page.locator('input[type="file"][accept*="image"]')
    for index in range(await image_inputs.count()):
        candidate = image_inputs.nth(index)
        try:
            accept = str(await candidate.get_attribute("accept") or "")
            cls = str(await candidate.get_attribute("class") or "")
            # 普通封面 input 的 accept 含 bmp/webp；付费内容封面只有 png/jpeg/jpg。
            if "image/bmp" in accept or "image/webp" in accept or "semi-upload-hidden-input" in cls:
                paid = await candidate.evaluate(
                    """el => {
                      let node = el;
                      for (let i = 0; i < 6 && node; i++) {
                        const text = (node.innerText || '') + ' ' + (node.className || '');
                        if (/付费|paid/i.test(text)) return true;
                        node = node.parentElement;
                      }
                      return false;
                    }"""
                )
                if not paid:
                    candidates.append(candidate)
        except Exception:
            continue
    for candidate in candidates:
        try:
            await candidate.set_input_files(str(frame_path))
            await page.wait_for_timeout(2500)
            log(f"已通过隐藏 input 上传封面：{frame_path.name}")
            return True
        except PublishStopRequested:
            raise
        except Exception as exc:
            log(f"封面上传控件尝试失败：{exc}")
            continue
    log("没有找到可用的普通封面上传控件")
    return False

async def cover_modal_visible(page: Page) -> bool:
    """识别新版“设置封面”弹窗（新版没有稳定的 dialog role）。"""
    # The current Creator Center uses Semi UI's ``role=modal`` and labels the
    # two tabs “选取封面/上传封面”.  During frame extraction the “完成” button
    # is disabled, so checking only for an enabled save button incorrectly
    # reports that the cover entry was never opened.
    try:
        modal = page.locator('[role="modal"]:visible')
        for index in range(await modal.count()):
            text = " ".join((await modal.nth(index).inner_text()).split())
            if ("选取封面" in text and "上传封面" in text) or (
                "完成" in text and ("选取封面" in text or "上传封面" in text)
            ):
                return True
    except Exception:
        pass
    try:
        pick_tab = page.get_by_text("选取封面", exact=True)
        upload_tab = page.get_by_text("上传封面", exact=True)
        if await last_visible(pick_tab, upload_tab) is not None:
            if await last_visible(pick_tab) is not None and await last_visible(upload_tab) is not None:
                return True
    except Exception:
        pass
    # 时间轴版弹窗使用“设置横封面/设置竖封面”两个顶部标签，
    # 没有 dialog/aria-modal 属性，也可能没有“保存/取消”文字。
    horizontal_tab = await last_visible(
        page.get_by_role("button", name=re.compile(r"^设置横封面$")),
        page.get_by_text("设置横封面", exact=True),
    )
    vertical_tab = await last_visible(
        page.get_by_role("button", name=re.compile(r"^设置竖封面$")),
        page.get_by_text("设置竖封面", exact=True),
    )
    if horizontal_tab is not None and vertical_tab is not None:
        # 编辑页底层通常没有“封面检测/完成”组合；用它确认确实在
        # 时间轴弹窗中，减少同名入口造成的误判。
        if await visible_text_present(page, re.compile(r"封面检测|^完成$")):
            return True
    recommendation_title = await last_visible(
        page.get_by_text(re.compile(r"^设置横封面获得更多流量\s*$")),
    )
    recommendation_cancel = await last_visible(
        page.get_by_role("button", name=re.compile(r"^暂不设置$")),
        page.get_by_text("暂不设置", exact=True),
    )
    if recommendation_title is not None and recommendation_cancel is not None:
        return True
    save = await last_visible(
        page.get_by_role("button", name=re.compile(r"^保存$")),
        page.get_by_text(re.compile(r"^保存$")),
    )
    cancel = await last_visible(
        page.get_by_role("button", name=re.compile(r"^取消$")),
        page.get_by_text(re.compile(r"^取消$")),
    )
    # 页面本身有“保存权限”文字，但不会同时出现“取消”按钮；成对判断
    # 可以避免误把编辑页底层文字当成弹窗。
    if save is not None and cancel is not None:
        return True
    title = await last_visible(
        page.get_by_role("heading", name=re.compile(r"^设置封面$")),
        page.get_by_text("设置封面", exact=True),
    )
    return title is not None and save is not None


async def finish_cover_dialog(
    page: Page,
    *,
    requested_seconds: float | None = None,
    duration: float | None = None,
) -> bool:
    """完成当前封面弹窗；新版页面没有稳定的 dialog/slider role。

    新版 Semi 弹窗在抽帧完成后会展示若干 ``frameItemWrapper``，但不会
    默认选中任何一帧；未选帧时“完成”按钮会一直 disabled。这里显式选取
    与队列封面时间最接近的缩略图，再等待按钮启用，避免把一个可恢复的
    页面误判成需要用户接管。
    """
    horizontal_action_clicked = False
    frame_selected = False

    async def select_extracted_frame() -> bool:
        """选择新版抽帧弹窗中的一个缩略图。

        类名带构建 hash，故只依赖稳定的 ``frameItemWrapper``/``frameItem``
        片段；不点击时间轴拖拽柄，避免把时间轴位置误当作已选封面。
        """
        nonlocal frame_selected
        if frame_selected:
            return True
        try:
            modal = page.locator('[role="modal"]:visible').last
            if await modal.count() == 0:
                modal = page
            wrappers = modal.locator('[class*="frameItemWrapper"]:visible')
            count = await wrappers.count()
            if count == 0:
                wrappers = modal.locator('[class*="frameItem"]:visible')
                count = await wrappers.count()
            if count == 0:
                return False

            # 若平台已经标记选中态，不重复点击，避免触发不必要的重新抽帧。
            for index in range(count):
                node = wrappers.nth(index)
                try:
                    state = await node.evaluate(
                        """el => {
                          const nodes = [el, ...el.querySelectorAll('*')];
                          return nodes.some(n => {
                            const cls = String(n.className || '');
                            return n.getAttribute('aria-selected') === 'true' ||
                              n.getAttribute('data-selected') === 'true' ||
                              /(?:^|\\s)(?:active|selected)-?[A-Za-z0-9_-]*/.test(cls);
                          });
                        }"""
                    )
                    if state:
                        frame_selected = True
                        return True
                except Exception:
                    continue

            target_index = 0
            if requested_seconds is not None and duration and duration > 0 and count > 1:
                ratio = max(0.0, min(1.0, requested_seconds / duration))
                target_index = min(count - 1, max(0, round(ratio * (count - 1))))
            target = wrappers.nth(target_index)
            await target.scroll_into_view_if_needed()
            await target.click(force=True)
            await page.wait_for_timeout(700)
            try:
                state = await target.evaluate(
                    """el => {
                      const nodes = [el, ...el.querySelectorAll('*')];
                      return nodes.some(n => n.getAttribute('aria-selected') === 'true' ||
                        n.getAttribute('data-selected') === 'true' ||
                        /(?:^|\\s)(?:active|selected)-?[A-Za-z0-9_-]*/.test(String(n.className || '')));
                    }"""
                )
            except Exception:
                state = False
            done_probe = await last_visible(
                modal.get_by_role("button", name=re.compile(r"^完成$|^确定$|^保存$")),
                modal.get_by_text(re.compile(r"^完成$|^确定$|^保存$")),
            )
            frame_selected = bool(state) or bool(done_probe and await done_probe.is_enabled())
            return frame_selected
        except PublishStopRequested:
            raise
        except Exception:
            return False

    # Large panoramic files may need several minutes for frame extraction.
    # Keep the modal authoritative for up to ten minutes; publishing must not
    # continue while its overlay still intercepts the page.
    for step in range(600):
        check_stop_requested("处理封面弹窗")
        body = await page.locator("body").inner_text()
        modal_markers = ("设置横封面", "选取封面", "上传封面")
        active = await cover_modal_visible(page)
        if not active and not any(marker in body for marker in modal_markers):
            return True

        # 某些账号会先弹出“设置横封面获得更多流量”的二级提示，
        # 需要先点“设置横封面”才能进入时间轴编辑器。
        if "获得更多流量" in body and "暂不设置" in body:
            recommend_button = await last_visible(
                page.get_by_role("button", name=re.compile(r"^设置横封面$")),
                page.get_by_text("设置横封面", exact=True),
            )
            if recommend_button is not None:
                try:
                    await recommend_button.click(force=True)
                    await page.wait_for_timeout(1000)
                    continue
                except PublishStopRequested:
                    raise
                except Exception:
                    pass

        # 时间轴版底部会先显示黑色“设置横封面”动作，点击后才会
        # 进入可保存的裁剪弹窗；它与顶部标签同名，所以取最后一个
        # 可见控件并只执行一次。
        if "封面检测" in body and not horizontal_action_clicked:
            horizontal_action = await last_visible(
                page.get_by_role("button", name=re.compile(r"^设置横封面$")),
                page.get_by_text("设置横封面", exact=True),
            )
            if horizontal_action is not None:
                try:
                    await horizontal_action.click(force=True)
                    horizontal_action_clicked = True
                    await page.wait_for_timeout(1000)
                    continue
                except PublishStopRequested:
                    raise
                except Exception:
                    pass

        # 抽帧内容异步挂载；只有缩略图真正可见时才尝试选帧。对大尺寸
        # 全景视频，前几十轮可能只有 loading-container，继续等待即可。
        if not frame_selected:
            await select_extracted_frame()

        # 选择器弹窗通常在 DOM 尾部，优先取最后一个可见同名控件，
        # 避免点到页面底层的同名入口。
        vertical = await last_visible(
            page.get_by_role("button", name=re.compile(r"^设置竖封面$")),
            page.get_by_text(re.compile(r"^设置竖封面$")),
        )
        if vertical is not None and step == 0:
            try:
                if await vertical.is_enabled():
                    await vertical.click()
                    await page.wait_for_timeout(1000)
                    continue
            except PublishStopRequested:
                raise
            except Exception:
                pass

        done = await last_visible(
            page.get_by_role("button", name=re.compile(r"^完成$|^确定$|^保存$")),
            page.get_by_text(re.compile(r"^完成$|^确定$|^保存$")),
        )
        if done is None:
            # 抽帧完成前，部分版本连底部按钮也会延迟挂载；只要弹窗
            # 仍在，就继续观察而不是立刻把现场交回人工。
            await page.wait_for_timeout(1200)
            continue
        try:
            if not await done.is_enabled():
                await page.wait_for_timeout(1200)
                continue
            await done.scroll_into_view_if_needed()
            await done.click(force=True)
            for _ in range(20):
                await page.wait_for_timeout(500)
                if not await cover_modal_visible(page):
                    return True
        except PublishStopRequested:
            raise
        except Exception:
            await page.wait_for_timeout(800)
    body = await page.locator("body").inner_text()
    return not await cover_modal_visible(page) and not any(
        marker in body for marker in ("设置横封面", "选取封面", "上传封面")
    )


async def configure_cover(page: Page, item: dict, video_path: Path) -> None:
    """自动选择横/竖封面并完成封面弹窗。"""
    check_stop_requested("开始设置封面")
    requested_seconds = parse_cover_seconds(item.get("cover_tsp"))
    duration = await asyncio.to_thread(read_video_duration, video_path)

    def custom_cover_path_for(orientation: str) -> Path | None:
        variant_field = "cover_image_landscape" if orientation == "landscape" else "cover_image_portrait"
        value = str(item.get(variant_field) or item.get("cover_image") or "")
        return app.safe_cover_path(value) if value else None

    custom_landscape_path = custom_cover_path_for("landscape")
    custom_portrait_path = custom_cover_path_for("portrait")
    for field in ("cover_image", "cover_image_landscape", "cover_image_portrait"):
        value = str(item.get(field) or "")
        if value and app.safe_cover_path(value) is None:
            raise ManualActionRequired("自定义封面文件不存在或格式不支持，请重新导入封面")

    async def find_slider(scope: Locator | Page) -> Locator | None:
        return await first_visible(
            scope.locator('input[type="range"]'),
            scope.locator('[role="slider"]'),
            scope.locator('input[aria-label*="时间"]'),
            scope.locator('input[aria-label*="帧"]'),
        )

    async def find_button(scope: Locator | Page, pattern: str) -> Locator | None:
        return await last_visible(
            scope.get_by_role("button", name=re.compile(pattern)),
            scope.get_by_text(re.compile(pattern)),
        )

    async def open_cover_picker(label: str | None = None) -> bool:
        """打开横/竖封面卡片；如果抖音已经打开弹窗则直接复用。"""
        check_stop_requested("打开封面设置")
        if await cover_modal_visible(page):
            return True
        if label is not None:
            label_node = await last_visible(page.get_by_text(label, exact=True))
            if label_node is not None:
                # “横封面4:3/竖封面3:4”位于卡片的两层父节点内，
                # 点击卡片比点击底层同名“选择封面”文字稳定。
                card = label_node.locator("xpath=../..")
                try:
                    await card.scroll_into_view_if_needed()
                    await card.click(force=True)
                    await page.wait_for_timeout(1000)
                    return await cover_modal_visible(page)
                except PublishStopRequested:
                    raise
                except Exception:
                    pass
            return False
        for trigger in (
            page.get_by_role("button", name=re.compile(r"^设置横封面$|^设置封面$")),
            page.get_by_text(re.compile(r"^设置横封面$|^设置封面$")),
            page.get_by_text("设置封面", exact=True),
            page.get_by_role("button", name=re.compile(r"选择封面")),
            page.get_by_text("选择封面", exact=True),
        ):
            try:
                trigger_button = await last_visible(trigger)
                if trigger_button is None:
                    continue
                await trigger_button.scroll_into_view_if_needed()
                await trigger_button.click(force=True)
                await page.wait_for_timeout(1000)
                if await cover_modal_visible(page):
                    return True
            except PublishStopRequested:
                raise
            except Exception:
                continue
        return False

    async def set_slider(slider: Locator) -> None:
        check_stop_requested("定位封面时间")
        try:
            min_value = float(await slider.get_attribute("min") or 0)
            max_value = float(await slider.get_attribute("max") or 100)
        except (TypeError, ValueError):
            min_value, max_value = 0.0, 100.0
        if max_value <= min_value:
            return
        target_seconds = requested_seconds
        if target_seconds is None:
            target_value = min_value + (max_value - min_value) * 0.5
        elif duration and max_value >= duration * 500:
            target_value = target_seconds * 1000
        elif duration and max_value >= duration * 0.5:
            target_value = target_seconds
        elif duration:
            target_value = min_value + (max_value - min_value) * min(1.0, target_seconds / duration)
        else:
            target_value = min_value + min(max_value - min_value, target_seconds)
        target_value = max(min_value, min(max_value, target_value))
        tag_name = await slider.evaluate("el => el.tagName.toLowerCase()")
        if tag_name == "input":
            await slider.evaluate(
                """(el, value) => {
                  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                  setter.call(el, String(value));
                  el.dispatchEvent(new Event('input', {bubbles: true}));
                  el.dispatchEvent(new Event('change', {bubbles: true}));
                }""",
                target_value,
            )
        else:
            # 新版页面把时间轴绘制成 div[role=slider]；读取 aria 范围后
            # 在轨道对应位置点击，避免对非 input 元素调用 value setter。
            try:
                aria_min = float(await slider.get_attribute("aria-valuemin") or min_value)
                aria_max = float(await slider.get_attribute("aria-valuemax") or max_value)
                ratio = 0.5 if aria_max <= aria_min else (target_value - aria_min) / (aria_max - aria_min)
                ratio = max(0.0, min(1.0, ratio))
                box = await slider.bounding_box()
                if box and box["width"] > 0 and box["height"] > 0:
                    await slider.click(
                        position={
                            "x": max(1.0, min(box["width"] - 1.0, box["width"] * ratio)),
                            "y": box["height"] / 2,
                        },
                        force=True,
                    )
            except PublishStopRequested:
                raise
            except Exception:
                # 没有可操作的轨道时继续使用平台默认中段封面；
                # 不把一个可选的时间点设置导致整条视频失败。
                return
        check_stop_requested("定位封面时间")
        await page.wait_for_timeout(700)

    dialog: Locator | None = None
    scope: Locator | Page = page
    opened = False
    # 先处理已经打开的新版弹窗。抖音在部分账号/版本中会在上传完成后
    # 自动弹出“设置封面”，此时页面上根本没有可再次点击的入口。
    opened = await open_cover_picker()
    if opened:
        dialog = await first_visible(
            page.locator('[role="modal"]:visible'),
            page.locator('[role="dialog"]'),
            page.locator('[aria-modal="true"]'),
        )
        scope = dialog if dialog is not None else page
        initial_cover_path = custom_landscape_path or custom_portrait_path
        if initial_cover_path is not None:
            # 明确切到对应方向，避免编辑页默认标签与素材方向不一致。
            initial_tab_label = "设置横封面" if custom_landscape_path is not None else "设置竖封面"
            initial_tab = await first_visible(
                page.get_by_text(initial_tab_label, exact=True),
                page.get_by_role("button", name=re.compile(rf"^{initial_tab_label}$")),
            )
            if initial_tab is not None:
                try:
                    await initial_tab.click(force=True)
                    await page.wait_for_timeout(800)
                except PublishStopRequested:
                    raise
                except Exception:
                    pass
            if not await upload_cover_image(page, initial_cover_path):
                raise ManualActionRequired("自定义封面未能上传，页面已保留")
        # 横、竖两套封面都在队列里时，必须分别在“设置横封面/设置竖封面”
        # 两个标签上传各自的原图；不能只传横版让平台把横版裁成竖版。
        if custom_landscape_path is not None and custom_portrait_path is not None:
            vertical_tab = await first_visible(
                page.get_by_text("设置竖封面", exact=True),
                page.get_by_role("button", name=re.compile(r"^设置竖封面$")),
            )
            if vertical_tab is not None:
                try:
                    await vertical_tab.click(force=True)
                    await page.wait_for_timeout(1200)
                    if not await upload_cover_image(page, custom_portrait_path):
                        raise ManualActionRequired("竖版封面未能上传，页面已保留")
                    log("已上传竖版封面原图")
                except PublishStopRequested:
                    raise

    if not opened:
        # 先按横版、竖版卡片分别尝试一次；这覆盖了当前页面的
        # “横封面4:3/竖封面3:4”双封面流程。
        for card_label in ("横封面4:3", "竖封面3:4"):
            check_stop_requested("尝试设置封面")
            if await open_cover_picker(card_label):
                opened = True
                orientation = "portrait" if "竖" in card_label else "landscape"
                custom_path = custom_cover_path_for(orientation) or custom_cover_path_for("landscape") or custom_cover_path_for("portrait")
                if custom_path is not None and not await upload_cover_image(page, custom_path):
                    raise ManualActionRequired("自定义封面未能上传，页面已保留")
                if await finish_cover_dialog(
                    page,
                    requested_seconds=requested_seconds,
                    duration=duration,
                ):
                    await page.wait_for_timeout(1200)
                    body = await page.locator("body").inner_text()
                    if cover_is_ready(body) or "横/竖双封面缺失" not in body:
                        return
        if not opened:
            if is_panorama_item(item):
                diagnostics = await _capture_panorama_diagnostics(page, "未找到抖音封面设置入口")
                suffix = f"；已保存诊断：{diagnostics}" if diagnostics else "；页面已保留"
                raise ManualActionRequired(f"没有找到抖音封面设置入口{suffix}")
            raise ManualActionRequired("没有找到抖音封面设置入口")

    # 先用文本/可见性完成当前新版弹窗；它通常没有稳定的 dialog role。
    if await finish_cover_dialog(
        page,
        requested_seconds=requested_seconds,
        duration=duration,
    ):
        await page.wait_for_timeout(1000)
        body = await page.locator("body").inner_text()
        if (
            not await cover_modal_visible(page)
            and "设置横封面" not in body
            and "选取封面" not in body
            and "上传封面" not in body
            and "横/竖双封面缺失" not in body
        ):
            return

    # Never fall through to the publish button while a Semi cover modal is
    # still mounted.  Its overlay otherwise intercepts the click and leaves a
    # misleading timeout in the queue state.
    if await cover_modal_visible(page):
        diagnostics = await _capture_panorama_diagnostics(page, "封面抽帧或确认尚未完成") if is_panorama_item(item) else ""
        suffix = f"；已保存诊断：{diagnostics}" if diagnostics else "；页面已保留"
        raise ManualActionRequired(f"抖音封面弹窗仍在处理，未继续发布{suffix}")

    # 保存横版后如果仍提示双封面缺失，继续自动打开竖版卡片并保存；
    # 不把这个步骤交回用户。
    for card_label in ("横封面4:3", "竖封面3:4"):
        check_stop_requested("补充设置封面")
        body = await page.locator("body").inner_text()
        if cover_is_ready(body) or "横/竖双封面缺失" not in body:
            return
        if await open_cover_picker(card_label):
            if await finish_cover_dialog(
                page,
                requested_seconds=requested_seconds,
                duration=duration,
            ):
                await page.wait_for_timeout(1200)

    if await cover_modal_visible(page):
        diagnostics = await _capture_panorama_diagnostics(page, "补充封面后弹窗仍未关闭") if is_panorama_item(item) else ""
        suffix = f"；已保存诊断：{diagnostics}" if diagnostics else "；页面已保留"
        raise ManualActionRequired(f"抖音封面弹窗仍未关闭，未继续发布{suffix}")

    # 当前抖音版本的时间轴是自绘元素，没有 input[type=range]；默认中间帧
    # 已经选中，只需按页面流程完成横/竖封面即可。
    slider = await find_slider(scope)
    if slider is not None:
        await set_slider(slider)

    # 若弹窗提供“设置竖封面”，先进入竖版步骤；随后再点“完成”。
    vertical_button = await find_button(scope, r"^设置竖封面$")
    if vertical_button is not None:
        try:
            if await vertical_button.is_enabled():
                await vertical_button.click()
                await page.wait_for_timeout(900)
                dialog = await first_visible(
                    page.locator('[role="modal"]:visible'),
                    page.locator('[role="dialog"]'),
                    page.locator('[aria-modal="true"]'),
                )
                scope = dialog if dialog is not None else page
                slider = await find_slider(scope)
                if slider is not None:
                    await set_slider(slider)
        except PublishStopRequested:
            raise
        except Exception:
            # 竖版是可选步骤时继续尝试保存横版；发布校验会给出明确提示。
            pass

    confirm = await find_button(scope, r"^完成$|^确定$|^保存$")
    if confirm is None:
        # 保存一份现场信息，便于适配抖音页面改版；不关闭浏览器也不改变页面。
        try:
            debug_dir = app.STATE_DIR / "debug"
            if not debug_dir.exists():
                try:
                    debug_dir.mkdir(exist_ok=True)
                except PermissionError:
                    pass
            (debug_dir / "cover_body.txt").write_text(await page.locator("body").inner_text(), encoding="utf-8")
            (debug_dir / "cover_html.html").write_text(await page.content(), encoding="utf-8")
            await page.screenshot(path=str(debug_dir / "cover_picker.png"), full_page=False)
        except Exception:
            pass
        raise ManualActionRequired("封面时间已定位，但未找到确认按钮，请在当前页面手动完成封面选择")
    # “完成”有时在封面检测完成前暂时禁用，最多等半分钟再点击。
    for _ in range(30):
        check_stop_requested("等待封面确认")
        try:
            if await confirm.is_enabled():
                await confirm.click()
                break
        except PublishStopRequested:
            raise
        except Exception:
            pass
        await page.wait_for_timeout(1000)
    else:
        raise ManualActionRequired("封面检测尚未完成，无法自动确认封面")
    check_stop_requested("完成封面设置")
    await page.wait_for_timeout(1000)


async def wait_until_logged_in(page: Page) -> None:
    """等待上传控件出现；不尝试处理验证码或登录。"""
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        check_stop_requested("等待抖音登录页面")
        if await page.locator('input[type="file"]').count():
            return
        body = (await page.locator("body").inner_text())[:2000]
        if "登录" not in body and "验证码" not in body:
            # 页面可能正在加载，继续等待。
            await page.wait_for_timeout(1000)
        else:
            set_publish_state(message="请在自动打开的 Chrome 窗口完成抖音登录/验证码", action_required=True)
            await page.wait_for_timeout(2000)
    raise RuntimeError("等待登录超时，请先完成抖音创作者中心登录")


def _panorama_format_patterns(format_key: str) -> list[re.Pattern[str]]:
    """Return aliases used by the current and older Creator Center UIs.

    The local queue uses compact keys (for example ``360_mono``), while the
    live modal currently displays labels such as ``普通360°全景视频（360°2D）``.
    Keeping the aliases here makes the browser adapter tolerant of that UI
    wording without weakening the explicit four-way format choice.
    """
    aliases = {
        "360_mono": (
            r"普通\s*360\s*[°度]?\s*全景视频",
            r"360\s*[°度]?\s*(?:普通|单目|单眼)\b",
            r"360\s*[°度]?\s*2D\b",
        ),
        "360_stereo": (
            r"立体\s*360\s*[°度]?\s*全景视频",
            r"360\s*[°度]?\s*(?:立体|双目|双眼)\b",
            r"360\s*[°度]?\s*3D\b",
        ),
        "180_mono": (
            r"普通\s*180\s*[°度]?\s*(?:全景)?视频",
            r"180\s*[°度]?\s*(?:普通|单目|单眼)\b",
            r"180\s*[°度]?\s*2D\b",
        ),
        "180_stereo": (
            r"立体\s*180\s*[°度]?\s*(?:全景)?视频",
            r"180\s*[°度]?\s*(?:立体|双目|双眼)\b",
            r"180\s*[°度]?\s*3D\b",
        ),
    }
    return [re.compile(pattern, re.I) for pattern in aliases.get(format_key, ())]


def _panorama_format_labels(format_key: str) -> tuple[str, ...]:
    """Exact labels for card-only builds which expose no radio semantics."""
    labels = {
        "360_mono": ("360°普通单目", "普通360°全景视频（360°2D）", "普通360°全景视频(360°2D)"),
        "360_stereo": ("360°立体双目", "立体360°全景视频（360°3D）", "立体360°全景视频(360°3D)"),
        "180_mono": ("180°普通单目", "普通180°视频（180°2D）", "普通180°视频(180°2D)"),
        "180_stereo": ("180°立体双目", "立体180°视频（180°3D）", "立体180°视频(180°3D)"),
    }
    return labels.get(format_key, ())


def _panorama_text_matches(text: object, patterns: list[re.Pattern[str]]) -> bool:
    normalized = " ".join(str(text or "").replace("\u3000", " ").split())
    return bool(normalized and any(pattern.search(normalized) for pattern in patterns))


async def _panorama_control_is_selected(control: Locator, format_key: str) -> bool:
    """Best-effort selected-state check for radios/cards/selects."""
    try:
        # React/Vue versions put the state on different nodes.  Walk a small
        # ancestor chain and inspect descendants so a click on the visible
        # card is treated the same as a click on its native radio input.
        return bool(
            await control.evaluate(
                """el => {
                    const nodes = [];
                    let node = el;
                    for (let i = 0; node && i < 5; i += 1, node = node.parentElement) nodes.push(node);
                    for (const current of nodes) {
                        const ariaChecked = current.getAttribute('aria-checked');
                        const ariaSelected = current.getAttribute('aria-selected');
                        const dataSelected = current.getAttribute('data-selected');
                        const dataChecked = current.getAttribute('data-checked');
                        const cls = String(current.className || '').toLowerCase();
                        if (ariaChecked === 'true' || ariaSelected === 'true' || dataSelected === 'true' || dataSelected === '1' || dataChecked === 'true' || dataChecked === '1') return true;
                        if (/(^|[-_\\s])(selected|active|checked|current)([-_\\s]|$)/.test(cls)) return true;
                        if (current.matches && current.matches('input[type="radio"]:checked, input[type="checkbox"]:checked')) return true;
                        if (current.querySelector && current.querySelector('input[type="radio"]:checked, input[type="checkbox"]:checked, [aria-checked="true"], [aria-selected="true"], [data-selected="true"], [data-checked="true"]')) return true;
                    }
                    return false;
                }"""
            )
        )
    except Exception:
        return False


async def _panorama_selection_confirmed(control: Locator, format_key: str) -> bool:
    """Check a clicked node and several card ancestors for selected state."""
    current = control
    for _ in range(5):
        if await _panorama_control_is_selected(current, format_key):
            return True
        try:
            parent = current.locator("xpath=..")
            if not await parent.count():
                break
            current = parent
        except Exception:
            break
    return False


async def _panorama_format_dialog_visible(page: Page) -> bool:
    """Whether the four-way format modal is still visible."""
    try:
        marker = page.get_by_text(re.compile(r"全景视频格式设置|全景视频设置"))
        for index in range(await marker.count()):
            if await marker.nth(index).is_visible():
                return True
    except Exception:
        pass
    return False


async def _confirm_panorama_format(page: Page) -> bool:
    """Click the modal's enabled ``确定`` button after a format is selected."""
    for _ in range(12):
        check_stop_requested("确认全景视频格式")
        candidates: list[Locator] = []
        try:
            candidates.extend([
                page.get_by_role("button", name=re.compile(r"^确定$")),
                page.get_by_text("确定", exact=True),
            ])
        except Exception:
            pass
        clicked = False
        for locator in candidates:
            for index in range(await locator.count()):
                try:
                    candidate = locator.nth(index)
                    if not await candidate.is_visible():
                        continue
                    # If the exact text is a span, promote it to its nearest
                    # button so disabled state and click semantics are correct.
                    tag = await candidate.evaluate("el => el.tagName.toLowerCase()")
                    if tag not in {"button", "input"}:
                        parent = candidate.locator("xpath=ancestor::button[1]")
                        if await parent.count() and await parent.first.is_visible():
                            candidate = parent.first
                    disabled = await candidate.get_attribute("disabled")
                    aria_disabled = await candidate.get_attribute("aria-disabled")
                    if disabled is not None or aria_disabled == "true":
                        continue
                    try:
                        if not await candidate.is_enabled():
                            continue
                    except Exception:
                        pass
                    await candidate.scroll_into_view_if_needed()
                    await candidate.click(force=True)
                    clicked = True
                    break
                except PublishStopRequested:
                    raise
                except Exception:
                    continue
            if clicked:
                break
        if clicked:
            await page.wait_for_timeout(800)
            if not await _panorama_format_dialog_visible(page):
                return True
            # Some builds keep the heading while transitioning to the editor;
            # an editor route/description field is sufficient confirmation.
            if any(token in page.url for token in ("/content/publish-vr-video", "/content/post/vr")):
                return True
            if await _find_caption_editor(page) is not None:
                return True
        elif not await _panorama_format_dialog_visible(page):
            return True
        await page.wait_for_timeout(500)
    return not await _panorama_format_dialog_visible(page)


async def configure_panorama_format(page: Page, item: dict) -> None:
    """Choose and verify the four-way panoramic format control.

    This intentionally refuses to guess.  If the current page uses a new
    control shape, the browser remains open and the user can select the format
    manually before pressing “继续发布”.
    """
    format_key = str(item.get("panorama_format", "")).strip().lower()
    if format_key not in app.PANORAMA_FORMATS:
        raise PanoramaEntryError("队列未选择有效的全景视频格式")
    patterns = _panorama_format_patterns(format_key)
    if not patterns:
        raise PanoramaEntryError(f"队列未选择有效的全景视频格式：{format_key}")
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        check_stop_requested("设置全景视频格式")
        # Native select implementation, if present.
        selects = page.locator("select:visible")
        for index in range(await selects.count()):
            select = selects.nth(index)
            try:
                options = await select.locator("option").all_text_contents()
                matching = next((text for text in options if _panorama_text_matches(text, patterns)), None)
                if matching:
                    await select.select_option(label=matching)
                    selected = await select.locator("option:checked").inner_text()
                    if _panorama_text_matches(selected, patterns) and await _confirm_panorama_format(page):
                        return
            except Exception:
                continue

        # Radio inputs expose the most stable semantics across Creator Center
        # redesigns; inspect their surrounding label text before clicking.
        radios = page.locator('input[type="radio"]:visible')
        for index in range(await radios.count()):
            radio = radios.nth(index)
            try:
                text = await radio.evaluate(
                    "el => (el.closest('label,[role=radio],li,div') || el.parentElement || el).innerText || ''"
                )
                value = " ".join(filter(None, [await radio.get_attribute("value"), await radio.get_attribute("aria-label"), text]))
                if not _panorama_text_matches(value, patterns):
                    continue
                try:
                    await radio.check(force=True)
                except Exception:
                    await radio.click(force=True)
                await page.wait_for_timeout(250)
                if await _panorama_selection_confirmed(radio, format_key) and await _confirm_panorama_format(page):
                    return
            except Exception:
                continue

        # Card/button based controls.  Prefer exact-ish text and verify an
        # active/aria-checked state after the click instead of trusting the
        # mere presence of a label (the page renders all four options at once).
        controls = page.locator('button:visible,[role="radio"]:visible,[role="option"]:visible,label:visible')
        for index in range(await controls.count()):
            control = controls.nth(index)
            try:
                text = " ".join((await control.inner_text()).split())
                if not _panorama_text_matches(text, patterns):
                    continue
                await control.click()
                await page.wait_for_timeout(250)
                if await _panorama_selection_confirmed(control, format_key) and await _confirm_panorama_format(page):
                    return
            except Exception:
                continue

        # Some builds render the option as a plain clickable span/div with no
        # role.  Use exact label text only (never the whole format panel), then
        # verify the selected state on the element or its nearest card.
        exact_labels = list(_panorama_format_labels(format_key))
        exact_labels.append(panorama_format_label(item))
        if exact_labels[-1]:
            exact_labels.append(exact_labels[-1].replace("°", ""))
        for text_value in dict.fromkeys(exact_labels):
            if not text_value:
                continue
            candidates = page.get_by_text(text_value, exact=True)
            for index in range(await candidates.count()):
                control = candidates.nth(index)
                try:
                    if not await control.is_visible():
                        continue
                    await control.click()
                    await page.wait_for_timeout(250)
                    if await _panorama_selection_confirmed(control, format_key) and await _confirm_panorama_format(page):
                        return
                except Exception:
                    continue

        await page.wait_for_timeout(1000)
    raise PanoramaEntryError(
        f"未找到或未确认抖音全景格式“{panorama_format_label(item) or format_key}”，已保留当前页面"
    )


async def _find_title_editor(page: Page) -> Locator | None:
    return await first_visible(
        page.get_by_role("textbox", name=re.compile(r"标题")),
        page.locator('input[placeholder*="标题"], textarea[placeholder*="标题"]'),
        page.locator('[contenteditable="true"][data-placeholder*="标题"]'),
    )


async def _find_caption_editor(page: Page) -> Locator | None:
    # Never use a title textarea as the description fallback. 2026 版抖音
    # 使用 Slate（data-slate-editor）承载描述，优先精确匹配它。
    return await first_visible(
        page.locator('[data-slate-editor="true"][contenteditable="true"]'),
        page.locator('textarea[placeholder*="描述"], input[placeholder*="描述"]'),
        page.locator('[contenteditable="true"][data-placeholder*="描述"]'),
        page.locator('input[placeholder*="文案"]'),
        page.locator('textarea:not([placeholder*="标题"]):not([aria-label*="标题"])'),
        page.locator('[contenteditable="true"]:not([data-placeholder*="标题"]):not([aria-label*="标题"])'),
    )


def caption_with_hashtags(item: dict) -> str:
    caption = str(item.get("caption", "")).strip()
    hashtags = [tag.strip().lstrip("#") for tag in str(item.get("hashtags", "")).split(",") if tag.strip()]
    for tag in hashtags:
        if f"#{tag}" not in caption:
            caption += f" #{tag}"
    return caption


def _normalize_ui_text(value: object) -> str:
    """Normalize platform text before readback comparison.

    Douyin uses Slate/contenteditable for the description; after typing it can
    add zero-width spaces, turn ``#话题`` into chips and insert extra spaces.
    Comparing raw strings therefore reports a false mismatch even when the
    visible text is correct.
    """
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"[\u200b-\u200f\u2060-\u2064\ufeff]", "", text)
    text = text.replace("\u00a0", " ")
    return re.sub(r"\s+", "", text)


def _text_matches(actual: object, expected: object, label: str) -> bool:
    actual_norm = _normalize_ui_text(actual)
    expected_norm = _normalize_ui_text(expected)
    if actual_norm == expected_norm:
        return True
    if label == "描述":
        expected_core = _normalize_ui_text(str(expected or "").split("#", 1)[0])
        actual_core = _normalize_ui_text(str(actual or "").split("#", 1)[0])
        tags = [tag for tag in re.findall(r"#([^#\s\u200b]+)", str(expected or "")) if tag]
        core_ok = bool(expected_core) and bool(actual_norm) and (
            expected_core in actual_norm
            or expected_core in actual_core
            or actual_core in expected_core
        )
        tags_ok = all(_normalize_ui_text(tag) in actual_norm for tag in tags)
        return core_ok and tags_ok
    return (
        bool(expected_norm)
        and bool(actual_norm)
        and (expected_norm in actual_norm or actual_norm in expected_norm)
    )


async def _read_control_value(control: Locator) -> str:
    tag = await control.evaluate("el => el.tagName")
    if tag in {"INPUT", "TEXTAREA"}:
        return await control.input_value()
    return await control.inner_text()


async def _fill_verified_text(control: Locator, value: str, label: str) -> None:
    expected = str(value or "")
    actual = ""
    for attempt in range(2):
        try:
            if attempt == 0:
                await control.fill(expected)
            else:
                # Slate/contenteditable 对 fill 的兼容性不一致；改用真实键盘输入。
                await control.click()
                await control.press("Meta+A")
                await control.press("Backspace")
                await control.page.keyboard.insert_text(expected)
            await control.press("Tab")
        except Exception as exc:
            log(f"[{label}] 第 {attempt + 1} 次写入失败：{exc}")
            continue
        await asyncio.sleep(0.35)
        for _ in range(4):
            actual = await _read_control_value(control)
            if _text_matches(actual, expected, label):
                return
            await asyncio.sleep(0.25)
        log(
            f"[{label}] 回读不一致 attempt={attempt + 1}: "
            f"expected={expected[:60]!r} actual={actual[:60]!r}"
        )
    raise ManualActionRequired(
        f"抖音{label}填写后回读不一致（期望：{expected[:40]!r}，实际：{actual[:40]!r}），"
        "已保留页面，未点击发布"
    )


def caption_text(item: dict) -> str:
    return str(item.get("caption") or "").strip()


def topic_tags(item: dict) -> list[str]:
    raw = str(item.get("hashtags") or "")
    return [
        tag.strip().lstrip("#")
        for tag in re.split(r"[,，、;；]+", raw)
        if tag.strip()
    ]


async def _topic_chip_texts(editor: Locator) -> list[str]:
    chips = editor.locator("[data-mention]")
    result: list[str] = []
    for index in range(await chips.count()):
        try:
            text = await chips.nth(index).inner_text()
        except Exception:
            continue
        normalized = _normalize_ui_text(text).lstrip("#")
        if normalized:
            result.append(normalized)
    return result


async def _place_caret_at_end(editor: Locator) -> None:
    await editor.evaluate("""el => {
      el.focus();
      const range = document.createRange();
      range.selectNodeContents(el);
      range.collapse(false);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    }""")


async def add_topics(page: Page, editor: Locator, item: dict) -> list[str]:
    """Add hashtags through the topic suggestion panel, not as plain text.

    Douyin only treats a hashtag as a real topic when it is picked from the
    ``#添加话题`` suggestion panel and rendered as a data-mention chip. Typing
    ``#话题`` as ordinary text looks similar in the editor but is not a topic.
    """
    tags = topic_tags(item)
    if not tags:
        return []
    missing: list[str] = []
    for tag in tags:
        existing = {_normalize_ui_text(chip) for chip in await _topic_chip_texts(editor)}
        if _normalize_ui_text(tag) in existing:
            continue
        await _place_caret_at_end(editor)
        # 话题必须另起一行输入 #tag；如果直接在正文同一行点“#添加话题”，
        # 面板会把整行正文当作搜索词，出现“#测试文案”这类错误候选。
        if (await editor.inner_text()).strip():
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(400)
        await page.keyboard.type(f"#{tag}", delay=70)
        await page.wait_for_timeout(500)
        selected = False
        for _ in range(12):
            await page.wait_for_timeout(350)
            panel = page.locator('[class*="mention-suggest-item-container"]:visible').last
            if not await panel.count():
                continue
            option = panel.get_by_text(f"#{tag}", exact=True)
            if await option.count():
                try:
                    await option.first.click(force=True)
                    await page.wait_for_timeout(900)
                    selected = True
                    break
                except Exception:
                    continue
            first = panel.locator('[class*="tag-hash-"]').first
            if await first.count():
                first_text = " ".join((await first.inner_text()).split())
                log(f"话题 {tag!r} 没有精确候选，未选择（首个候选：{first_text[:40]!r}）")
                break
        if not selected:
            missing.append(tag)
            # 删除刚键入的纯文本 #tag，绝不能把它当成话题发布。
            for _ in range(len(tag) + 1):
                await page.keyboard.press("Backspace")
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
        else:
            log(f"已选择话题：#{tag}")
    if missing:
        raise ManualActionRequired(
            "以下话题没有在抖音话题面板中找到精确候选，已保留页面，未点击发布："
            + "、".join(f"#{tag}" for tag in missing)
        )
    return tags


async def _editor_content_verified(page: Page, item: dict) -> bool:
    """Check that the editor still holds this item's title, caption and topics."""
    title = str(item.get("work_title") or "").strip()
    title_editor = await _find_title_editor(page)
    caption_editor = await _find_caption_editor(page)
    if title and title_editor is None:
        return False
    if caption_editor is None:
        return False
    if title_editor is not None:
        actual_title = await _read_control_value(title_editor)
        if not _text_matches(actual_title, title, "标题"):
            return False
    actual_caption = await _read_control_value(caption_editor)
    if not _text_matches(actual_caption, caption_text(item), "描述"):
        return False
    tags = topic_tags(item)
    if tags:
        chips = {_normalize_ui_text(chip) for chip in await _topic_chip_texts(caption_editor)}
        if not all(_normalize_ui_text(tag) in chips for tag in tags):
            return False
    return True


async def configure_title_and_caption(page: Page, item: dict) -> None:
    title = str(item.get("work_title") or "").strip()
    title_editor = await _find_title_editor(page)
    if title and title_editor is None:
        raise ManualActionRequired("未找到抖音标题输入框，已保留页面；标题不会写入描述，未点击发布")
    editor = await _find_caption_editor(page)
    if editor is None:
        raise ManualActionRequired("未找到抖音描述输入框，已保留页面，未点击发布")
    if title_editor is not None:
        same = await editor.evaluate("(el, other) => el === other", await title_editor.element_handle())
        if same:
            raise ManualActionRequired("抖音标题与描述定位到同一控件，已暂停并保留页面")
        await _fill_verified_text(title_editor, title, "标题")
    # 描述只填正文；话题必须走站内话题面板，不能拼成普通文本。
    await _fill_verified_text(editor, caption_text(item), "描述")
    await add_topics(page, editor, item)
    log(f"已填写标题/描述/话题：{item.get('name', '')}")

async def _visible_upload_progress(page: Page) -> bool:
    locator = page.get_by_text(re.compile(r"上传中|正在上传|上传过程中|剩余时间|取消上传|已上传："))
    for index in range(await locator.count()):
        try:
            if await locator.nth(index).is_visible():
                return True
        except Exception:
            continue
    progress = page.locator('[role="progressbar"]:visible')
    for index in range(await progress.count()):
        try:
            text = await progress.nth(index).evaluate(
                "el => (el.parentElement && el.parentElement.innerText) || el.innerText || ''"
            )
            if any(word in str(text) for word in ("上传", "剩余", "转码", "处理视频")):
                value = await progress.nth(index).get_attribute("aria-valuenow")
                if value is None or float(value) < 100:
                    return True
        except Exception:
            continue
    return False


async def _visible_panorama_upload_progress(
    page: Page,
    item_name: str = "",
) -> tuple[bool, int | None]:
    """读取全景编辑页真正可见的上传进度。

    全景页会把上传卡片留在编辑器 DOM 中；不能因为存在“取消上传”文字
    就永久等待，也不能把隐藏的旧草稿进度当成当前文件。这里只接受可见
    百分比/取消控件，并把 100% 视为已完成。
    """
    percent: int | None = None
    active = False
    # 优先从当前文件的“取消上传”卡片向上读取百分比；不要扫描整个
    # body 后取最大值，否则页面其他统计数字可能掩盖真正的上传进度。
    cancel_nodes = page.get_by_text(re.compile(r"^取消上传$"))
    cancel_count = await cancel_nodes.count()
    for index in range(await cancel_nodes.count()):
        node = cancel_nodes.nth(index)
        try:
            if not await node.is_visible():
                continue
            ancestor = await node.evaluate(
                """el => {
                  let cur = el;
                  for (let i = 0; i < 5 && cur; i++, cur = cur.parentElement) {
                    const text = cur.innerText || '';
                    if (text.includes('%') ||
                        (text.includes('取消上传') && text.trim() !== (el.innerText || '').trim())) return text;
                  }
                  return el.parentElement ? el.parentElement.innerText || '' : '';
                }"""
            )
            text = str(ancestor or "")
            if item_name and item_name not in text and cancel_count > 1:
                continue
            match = re.search(r"(\d{1,3})%", text)
            if match:
                value = max(0, min(100, int(match.group(1))))
                percent = value
                if value < 100:
                    active = True
                else:
                    active = False
            else:
                active = True
            return active, percent
        except Exception:
            continue

    # 有些版本把取消文字换成图标，只留下一个精确的百分比 span。
    percent_nodes = page.get_by_text(re.compile(r"^\s*\d{1,3}%\s*$"))
    for index in range(await percent_nodes.count()):
        node = percent_nodes.nth(index)
        try:
            if not await node.is_visible():
                continue
            text = " ".join((await node.inner_text()).split())
            match = re.search(r"(\d{1,3})%", text)
            if not match:
                continue
            ancestor = await node.evaluate(
                """el => {
                  let cur = el;
                  for (let i = 0; i < 5 && cur; i++, cur = cur.parentElement) {
                    const text = cur.innerText || '';
                    if (text.includes('上传') || text.includes('%')) return text;
                  }
                  return el.parentElement ? el.parentElement.innerText || '' : '';
                }"""
            )
            ancestor_text = str(ancestor or "")
            if (
                item_name
                and item_name not in ancestor_text
                and "上传" not in ancestor_text
                and await percent_nodes.count() > 1
            ):
                continue
            value = max(0, min(100, int(match.group(1))))
            return value < 100, value
        except Exception:
            continue

    # 兼容没有百分比文本、只有 aria-valuenow 的版本。
    progress = page.locator('[role="progressbar"]:visible')
    for index in range(await progress.count()):
        try:
            value_text = await progress.nth(index).get_attribute("aria-valuenow")
            if value_text is None:
                continue
            value = max(0, min(100, int(float(value_text))))
            percent = value
            if value < 100:
                active = True
        except Exception:
            continue
    if percent is not None and percent >= 100:
        active = False
    return active, percent


async def wait_for_panorama_upload_completion(page: Page, item: dict) -> None:
    """格式确认后再等待文件上传完成，避免封面抽帧抢跑。

    抖音可能先显示全景格式弹窗，再在编辑页继续上传大文件。旧流程把
    弹窗当作“上传完成”直接进入封面，导致封面弹窗在 39% 时一直 disabled。
    """
    deadline = time.monotonic() + 30 * 60
    seen_progress = False
    quiet_rounds = 0
    item_name = str(item.get("name") or "")
    while time.monotonic() < deadline:
        await wait_if_paused(page, item, "等待全景视频上传完成")
        validation_error = await _visible_panorama_validation_error(page)
        if validation_error:
            diagnostics = await _capture_panorama_diagnostics(page, validation_error)
            suffix = f"；已保存诊断：{diagnostics}" if diagnostics else "；页面已保留"
            raise PanoramaEntryError(f"抖音全景页返回校验提示：{validation_error}{suffix}")
        active, percent = await _visible_panorama_upload_progress(page, item_name)
        editor = await _find_caption_editor(page)
        body = await page.locator("body").inner_text()
        editor_ready = editor is not None and (
            item_name in body
            or Path(item_name).stem in body
            or any(token in page.url for token in ("/content/publish-vr-video", "/content/post/vr"))
        )
        processing = await visible_text_present(
            page,
            re.compile(r"转码中|处理视频|视频处理中|正在处理"),
        )
        if active or processing:
            seen_progress = True
            quiet_rounds = 0
            suffix = f"（{percent}%）" if percent is not None else ""
            set_publish_state(
                message=f"正在等待全景视频 {item_name} 上传完成{suffix}",
                current=item_name,
                action_required=False,
            )
        elif editor_ready:
            quiet_rounds += 1
            if quiet_rounds >= 3 and (seen_progress or editor_ready):
                return
        else:
            quiet_rounds = 0
        await page.wait_for_timeout(2000)
    diagnostics = await _capture_panorama_diagnostics(page, "全景视频上传进度未完成")
    suffix = f"；已保存诊断：{diagnostics}" if diagnostics else "；页面已保留"
    raise PanoramaEntryError(f"全景视频上传或预审仍未完成，已等待 30 分钟{suffix}")


async def upload_panorama_one(page: Page, item: dict) -> None:
    """Upload one item through the dedicated panoramic editor.

    The panoramic page is not treated as a normal video page: if its route,
    format selector, or preflight cannot be verified, a ManualActionRequired
    error keeps the page open and the queue item pending for recovery.
    """
    video_path = app.safe_video_path(item.get("path", ""))
    if video_path is None:
        raise PanoramaEntryError(f"找不到全景视频文件：{item.get('path', '')}")
    await wait_if_paused(page, item, "准备上传全景视频")
    await open_upload_page(page, item)
    abandon = page.get_by_text("放弃", exact=True)
    if await abandon.count() and await abandon.first.is_visible():
        await abandon.first.click()
        await page.wait_for_timeout(800)
    file_input = await first_visible(page.locator('input[type="file"]'))
    if file_input is None:
        raise PanoramaEntryError("全景上传页没有找到文件选择控件，已保留当前页面")
    await file_input.set_input_files(str(video_path))

    deadline = time.monotonic() + 30 * 60
    quiet_rounds = 0
    while time.monotonic() < deadline:
        await wait_if_paused(page, item, "等待全景视频上传和预审")
        body = await page.locator("body").inner_text()
        # The Creator Center can leave a stale "上传中" progress label in the
        # DOM while the four-way panoramic format modal is already visible.
        # The modal is the stronger state: stop waiting immediately so the
        # selected format and its enabled “确定” button can be handled.
        if await _panorama_format_dialog_visible(page):
            break
        validation_error = await _visible_panorama_validation_error(page)
        if validation_error:
            diagnostics = await _capture_panorama_diagnostics(page, validation_error)
            suffix = f"；已保存诊断：{diagnostics}" if diagnostics else "；页面已保留"
            raise PanoramaEntryError(f"抖音全景页返回校验提示：{validation_error}{suffix}")
        uploading = await _visible_upload_progress(page)
        editor = await _find_caption_editor(page)
        editor_route = any(token in page.url for token in ("/content/publish-vr-video", "/content/post/vr"))
        format_marker = any(token in body for token in ("360", "180", "全景格式", "视频格式"))
        preview_ready = await page.locator("video").count() > 0 or item.get("name", "") in body
        completion_marker = any(token in body for token in ("上传完成", "上传成功", "处理完成", "预审通过"))
        # 抖音有时先弹出格式选择层，再挂载文案编辑器；出现当前文件
        # 的预览和格式控件即可进入 configure_panorama_format。
        if not uploading and format_marker and (editor is not None or editor_route or (preview_ready and completion_marker)):
            quiet_rounds += 1
        else:
            quiet_rounds = 0
        if quiet_rounds >= 2:
            break
        set_publish_state(
            message=f"正在等待全景视频 {item.get('name', '')} 上传和预审",
            current=item.get("name", ""),
            action_required=False,
        )
        await page.wait_for_timeout(3000)
    else:
        raise PanoramaEntryError("全景视频上传或预审超时，已保留当前页面")

    await configure_panorama_format(page, item)
    # 格式弹窗出现并不代表大文件已经上传完毕；先等待当前文件的
    # 可见进度归零，再进入文案/封面步骤。
    await wait_for_panorama_upload_completion(page, item)
    editor = await _find_caption_editor(page)
    editor_deadline = time.monotonic() + 60
    while editor is None and time.monotonic() < editor_deadline:
        await wait_if_paused(page, item, "等待全景编辑页加载")
        await page.wait_for_timeout(1000)
        editor = await _find_caption_editor(page)
    if editor is None:
        raise PanoramaEntryError("全景编辑页没有找到文案输入框，已保留当前页面")
    await configure_title_and_caption(page, item)
    await wait_if_paused(page, item, "全景格式和文案已准备，处理封面")
    body = await page.locator("body").inner_text()
    # 全景编辑页可能只展示“封面”说明文字，并没有可操作的封面控件。
    # 只有出现明确的设置/上传/选择/必填提示时才进入封面自动化，避免
    # 把普通说明文字误当成硬门槛。
    cover_trigger = await visible_text_present(
        page,
        re.compile(r"需要上传封面|请上传封面|设置(?:横|竖)?封面|上传封面|选择封面|横/竖双封面缺失"),
    )
    if not cover_is_ready(body) and cover_trigger:
        await configure_cover(page, item, video_path)
    if await cover_modal_visible(page):
        raise ManualActionRequired("抖音封面弹窗仍在页面上，未点击发布；页面已保留")
    # 全景专属平台项：官方活动按文案/话题做保守匹配；PICO 只有在
    # 队列明确授权时才会勾选，避免把授权 consent 当成普通发布设置。
    await configure_official_activity(page, item)
    await configure_pico_authorization(page, item)
    await configure_publish_options(page, item)
    await configure_publish_schedule(page, item)
    await wait_if_paused(page, item, "全景视频已完成设置，准备点击发布")
    publish_start_url = page.url
    publish_before_body = await page.locator("body").inner_text()
    await click_publish_button(page)
    await wait_for_publish_result(
        page,
        item,
        previous_url=publish_start_url,
        before_body=publish_before_body,
    )


async def upload_one(page: Page, item: dict) -> None:
    if is_panorama_item(item):
        await upload_panorama_one(page, item)
        return
    video_path = app.safe_video_path(item["path"])
    if video_path is None:
        raise RuntimeError(f"找不到视频文件：{item['path']}")

    await wait_if_paused(page, item, "准备上传当前视频")
    await open_upload_page(page, item)
    # 上一次失败可能留下“未发布草稿”。为避免把旧草稿误当成当前文件，
    # 先尝试放弃旧草稿，再从当前队列文件开始。
    for pattern in (r"^放弃$", r"^放弃编辑$", r"^放弃发布$"):
        abandon = page.get_by_text(re.compile(pattern))
        try:
            if await abandon.count() and await abandon.first.is_visible():
                await abandon.first.click()
                await page.wait_for_timeout(800)
                break
        except Exception:
            continue
    # 记录选择文件前的页面文本：之前遗留的“上传成功”不能作为本次依据。
    before_upload_body = await page.locator("body").inner_text()
    stale_success = any(
        word in before_upload_body for word in ("极速上传成功", "上传成功", "上传完成")
    )
    file_input = page.locator('input[type="file"]').first
    await file_input.set_input_files(str(video_path))
    log(f"已选择文件：{video_path.name}")

    # 大视频上传和预审可能需要较长时间；编辑页出现才代表文件进入发布流程。
    # 同时要求标题/描述编辑器和发布按钮存在，不再把任意 textarea 当编辑器。
    editor = None
    video_ready = False
    upload_seen = False
    structure_since: float | None = None
    deadline = time.monotonic() + 15 * 60
    no_upload_progress_rounds = 0
    last_log = 0.0
    while time.monotonic() < deadline:
        await wait_if_paused(page, item, "等待当前视频上传完成")
        title_editor = await _find_title_editor(page)
        caption_editor = await _find_caption_editor(page)
        editor = title_editor or caption_editor
        publish_button = await find_publish_button(page)
        structure_ready = editor is not None and publish_button is not None
        body = await page.locator("body").inner_text()
        stem = Path(str(item.get("name") or "")).stem
        name_in_body = bool(stem) and stem in body
        has_preview = await page.locator("video").count() > 0 or name_in_body

        # 封面弹窗出现 = 视频已交给编辑器，可直接进入下一步。
        cover_open = await cover_modal_visible(page)
        if cover_open and (structure_ready or name_in_body):
            upload_seen = True
            video_ready = True
            break

        # 只信选择文件之后新出现的成功标记，旧提示不能作为本次上传依据。
        upload_success_marker = (not stale_success) and await visible_text_present(
            page,
            re.compile(r"极速上传成功|上传成功|上传完成"),
        )
        if upload_success_marker and structure_ready and has_preview:
            video_ready = True
            break

        # 检测可见的上传提示文本
        upload_text_active = False
        upload_text_locator = page.get_by_text(
            re.compile(r"上传中|正在上传|上传过程中|剩余时间|取消上传|已上传：")
        )
        for text_index in range(await upload_text_locator.count()):
            try:
                if await upload_text_locator.nth(text_index).is_visible():
                    upload_text_active = True
                    break
            except Exception:
                continue

        # 检测上传百分比
        upload_percent: int | None = None
        percent_locator = page.get_by_text(re.compile(r"\d{1,3}%\s*已上传："))
        for percent_index in range(await percent_locator.count()):
            try:
                if await percent_locator.nth(percent_index).is_visible():
                    match = re.search(
                        r"(\d{1,3})%\s*已上传：",
                        await percent_locator.nth(percent_index).inner_text(),
                    )
                    if match:
                        upload_percent = int(match.group(1))
                        break
            except Exception:
                continue
        if upload_percent is not None and upload_percent < 100:
            upload_text_active = True
        elif upload_percent == 100:
            upload_text_active = False

        # 编辑器未出现时，"处理中/转码中"也算上传状态
        if editor is None and any(word in body for word in ("处理中", "转码中", "视频处理中")):
            upload_text_active = True

        # 跟踪是否曾看到上传状态（防止把旧草稿误判为当前文件已就绪）
        if has_preview and upload_text_active:
            upload_seen = True

        # 检查可见 progressbar（上传相关的）
        uploading = upload_text_active
        progress = page.locator('[role="progressbar"]:visible')
        if await progress.count() and not upload_success_marker:
            for idx in range(await progress.count()):
                try:
                    value = await progress.nth(idx).get_attribute("aria-valuenow")
                    ancestor_text = await progress.nth(idx).evaluate(
                        "el => (el.parentElement && el.parentElement.innerText) || el.innerText || ''"
                    )
                    is_upload_progress = any(word in str(ancestor_text) for word in (
                        "上传", "剩余", "转码", "处理视频",
                    ))
                    if is_upload_progress and (value is None or float(value) < 100):
                        uploading = True
                except Exception:
                    pass

        # 编辑器 + 文件名/预览 + 发布按钮都已出现，且上传状态消失，认为完成。
        if structure_ready and has_preview:
            upload_seen = (
                upload_seen
                or upload_text_active
                or upload_percent is not None
                or upload_success_marker
            )
            if upload_success_marker or not uploading:
                no_upload_progress_rounds += 1
            else:
                no_upload_progress_rounds = 0
            if upload_seen and no_upload_progress_rounds >= 2:
                video_ready = True
                break
            # 极快上传可能没有进度条/成功提示；结构、文件名稳定 3 秒后放行。
            if structure_since is None:
                structure_since = time.monotonic()
            if (
                not uploading
                and not stale_success
                and name_in_body
                and time.monotonic() - structure_since >= 3
            ):
                video_ready = True
                break
        else:
            structure_since = None

        if time.monotonic() - last_log > 5:
            log(
                f"等待上传 {item.get('name', '')}: percent={upload_percent} "
                f"editor={bool(editor)} name_in_body={name_in_body} "
                f"publish={bool(publish_button)} uploading={uploading}"
            )
            last_log = time.monotonic()
        set_publish_state(
            message=f"正在等待 {item.get('name', '')} 上传完成"
            + (f" ({upload_percent}%)" if upload_percent is not None else ""),
            current=item.get("name", ""),
            action_required=False,
        )
        await page.wait_for_timeout(1500 if uploading else 700)
    if editor is None or not video_ready:
        raise RuntimeError("视频上传或预审超时，没有确认视频已进入编辑页")
    log(f"进入编辑页：{item.get('name', '')}")
    await wait_if_paused(page, item, "视频已上传，准备填写文案")
    await configure_title_and_caption(page, item)

    # 封面时间为空时自动选中视频中段；填写时间时按秒定位。
    # 抖音页面版本差异较大，无法识别选择器时交给人工，并保持页面打开。
    await configure_cover(page, item, video_path)
    # 权限必须按队列配置写入，且在真正点击发布前完成核验。
    await configure_publish_options(page, item)
    # 发布时间（立即/定时）必须在真正点击发布前完成设置并回读确认。
    await configure_publish_schedule(page, item)

    await wait_if_paused(page, item, "已完成文案和封面设置，准备点击发布")
    publish_start_url = page.url
    publish_before_body = await page.locator("body").inner_text()
    await click_publish_button(page)
    # 点击发布后，抖音可能先显示校验提示，也可能需要一段时间才跳转。
    await wait_for_publish_result(
        page,
        item,
        previous_url=publish_start_url,
        before_body=publish_before_body,
    )


async def wait_for_draft_editor(page: Page, item: dict, timeout_seconds: int = 90) -> Locator | None:
    """等待上传页里的“继续编辑”草稿进入编辑器。"""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        await wait_if_paused(page, item, "等待草稿编辑页")
        # 先处理抖音在上传页展示的未发布草稿卡片。
        continue_locators = (
            page.get_by_role("button", name=re.compile(r"继续(?:编辑|发布|创作)")),
            page.get_by_text(re.compile(r"^继续(?:编辑|发布|创作)$")),
        )
        continue_button = await first_visible(*continue_locators)
        if continue_button is not None:
            try:
                await continue_button.click()
                await page.wait_for_timeout(1200)
            except Exception:
                pass

        editor = await first_visible(
            page.locator("textarea"),
            page.locator('[contenteditable="true"]'),
            page.locator('input[placeholder*="文案"]'),
            page.locator('input[placeholder*="描述"]'),
        )
        body = await page.locator("body").inner_text()
        editor_page = any(token in page.url for token in ("/content/post/video", "/content/upload", "/content/publish-vr-video", "/content/post/vr"))
        has_edit_marker = any(token in body for token in (
            "作品描述", "添加话题", "设置封面", "封面效果检测通过", "发布视频", "全景视频", "全景格式",
        ))
        if editor is not None and (editor_page or has_edit_marker):
            return editor

        # 登录失效时给出明确状态，但仍保持这个浏览器窗口。
        if any(token in body for token in ("登录", "验证码", "扫码")) and not editor_page:
            set_publish_state(
                running=True,
                message="请在自动打开的 Chrome 窗口完成抖音登录/验证码",
                current=item.get("name", ""),
                action_required=True,
            )
        await page.wait_for_timeout(1500)
    return None


async def resume_current_draft(page: Page, item: dict) -> None:
    """恢复已上传但停在编辑页的草稿，然后自动点击发布。"""
    video_path = app.safe_video_path(item["path"])
    await wait_if_paused(page, item, "准备恢复当前草稿")
    # If the previous run was stopped while the format modal was visible,
    # preserve that page and continue in place.  Navigating back to the upload
    # route would discard the uploaded draft and force another multi-GB upload.
    format_modal_already_open = is_panorama_item(item) and await _panorama_format_dialog_visible(page)
    cover_modal_already_open = is_panorama_item(item) and await cover_modal_visible(page)
    editor_route_already_open = is_panorama_item(item) and any(
        token in page.url for token in ("/content/publish-vr-video", "/content/post/vr")
    )
    # 恢复时若页面已经停在全景编辑器/封面弹窗，必须原地接管；重新
    # goto 上传入口会丢掉正在上传的草稿，正是之前“卡住后无法继续”的
    # 主要来源。
    preserve_current_editor = format_modal_already_open or cover_modal_already_open or editor_route_already_open
    if not preserve_current_editor:
        await open_upload_page(page, item)
    editor = None if format_modal_already_open else await wait_for_draft_editor(page, item)

    if is_panorama_item(item) and await _panorama_format_dialog_visible(page):
        await configure_panorama_format(page, item)
        await wait_for_panorama_upload_completion(page, item)
        editor = await _find_caption_editor(page)
    elif is_panorama_item(item) and preserve_current_editor:
        # 进程异常退出后可能只留下编辑器/封面弹窗；在恢复文案之前
        # 重新确认上传已经完成，避免继续点击被上传遮罩拦截。
        await wait_for_panorama_upload_completion(page, item)
    if editor is None:
        editor = await wait_for_draft_editor(page, item)
    if editor is None:
        raise RuntimeError("没有找到可继续编辑的抖音草稿，未重新上传视频")

    await configure_title_and_caption(page, item)

    body = await page.locator("body").inner_text()
    if not cover_is_ready(body):
        if video_path is None:
            raise RuntimeError(f"找不到视频文件，无法自动设置封面：{item['path']}")
        await configure_cover(page, item, video_path)

    if await cover_modal_visible(page):
        raise ManualActionRequired("抖音封面弹窗仍在页面上，未点击发布；页面已保留")
    await configure_official_activity(page, item)
    await configure_pico_authorization(page, item)
    await configure_publish_options(page, item)
    # 恢复草稿也必须重新核对发布时间，避免旧草稿/页面默认值把定时项
    # 意外发布成“立即发布”。
    await configure_publish_schedule(page, item)

    # 封面已通过（包括用户刚刚手动选好的封面）后，自动完成最后一步。
    await wait_if_paused(page, item, "草稿已准备完成，准备点击发布")
    publish_start_url = page.url
    publish_before_body = await page.locator("body").inner_text()
    await click_publish_button(page)
    await wait_for_publish_result(
        page,
        item,
        previous_url=publish_start_url,
        before_body=publish_before_body,
    )


async def run(check_only: bool = False, only: str | None = None, resume: bool = False) -> None:
    statuses = {"pending", "failed"}
    if resume:
        statuses.add("publishing")
    items = [item for item in app.load_queue() if item.get("status") in statuses]
    if only:
        items = [item for item in items if item.get("path") == only or item.get("name") == only]
    if check_only:
        items = items[:1] or [{"id": "__check__", "path": "", "name": "页面检查"}]
    if not items:
        set_publish_state(
            running=False,
            message="没有待发布视频",
            completed=0,
            total=0,
            action_required=False,
            auto_paused=False,
            pause_source="",
        )
        return
    if not check_only:
        panorama_error = app.validate_queue_panorama(items)
        if panorama_error:
            mark_automatic_pause(
                f"全景视频配置有误：{panorama_error}",
                kind="panorama_preflight",
                current=items[0].get("name", "") if items else "",
                completed=0,
                total=len(items),
            )
            return
    if not PROFILE_DIR.exists():
        try:
            PROFILE_DIR.mkdir(exist_ok=True)
        except PermissionError:
            pass
    app.clear_publish_pause()
    app.clear_publish_stop()
    set_publish_state(
        running=True,
        paused=False,
        pause_requested=False,
        message="正在打开 Chrome",
        current="",
        completed=0,
        total=len(items),
        action_required=False,
        auto_paused=False,
        pause_source="",
    )
    # 手动管理 Playwright 生命周期：失败/需要人工处理时不执行
    # playwright.stop()，让专用 Chrome 窗口继续保留在现场。
    # 清理可能残留的 Chrome 锁文件，避免"已有浏览器会话"错误。
    for lock_file in (PROFILE_DIR / "SingletonLock", PROFILE_DIR / "SingletonSocket", PROFILE_DIR / "SingletonCookie"):
        try:
            lock_file.unlink()
        except FileNotFoundError:
            pass
    # Playwright 启动偶发失败（Chrome 锁竞争、资源未释放），重试一次。
    playwright = None
    last_error = None
    for attempt in range(2):
        try:
            playwright = await async_playwright().start()
            break
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                await asyncio.sleep(1.5)
    if playwright is None:
        raise RuntimeError(f"Playwright 启动失败（已重试 1 次）：{last_error}")
    launch_kwargs = {
        "headless": False,
        "accept_downloads": False,
        "args": [
            # 网络优化：恢复后台网络功能，提高上传吞吐量
            "--enable-features=NetworkService,NetworkServiceInProcess",
            "--disable-features=Translate,BackForwardCache,OptimizationHints",
            # 禁用节流，保持后台标签页活跃（上传期间页面可能在后台）
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            # 文件上传优化
            "--allow-file-access-from-files",
            "--disable-site-isolation-trials",
        ],
    }
    if CHROME.exists():
        launch_kwargs["executable_path"] = str(CHROME)
    context: BrowserContext = None
    for attempt in range(2):
        try:
            context = await playwright.chromium.launch_persistent_context(str(PROFILE_DIR), **launch_kwargs)
            break
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                await asyncio.sleep(1.5)
    if context is None:
        try:
            await playwright.stop()
        except Exception:
            pass
        raise RuntimeError(f"Chrome 启动失败（已重试 1 次）：{last_error}")
    keep_browser_open = False
    try:
            page = context.pages[0] if context.pages else await context.new_page()
            if check_only:
                check_item = next((entry for entry in items if is_panorama_item(entry)), items[0])
                await open_upload_page(page, check_item)
                mode_text = "全景视频入口和上传控件" if is_panorama_item(check_item) else "视频上传控件"
                set_publish_state(running=False, message=f"页面检查通过：已登录并识别到{mode_text}；选择视频后会继续识别文案控件", current="", completed=0, total=0, action_required=False, auto_paused=False, pause_source="")
                return
            for index, item in enumerate(items, 1):
                await wait_if_paused(page, item, "准备处理下一条视频")
                log(f"开始第 {index}/{len(items)} 条：{item.get('name', '')}")
                set_publish_state(running=True, message=f"正在发布第 {index}/{len(items)} 条", current=item.get("name", item.get("path", "")), completed=index - 1, total=len(items), action_required=False)
                resume_item = resume and item.get("status") == "publishing"
                update_item(item["id"], "publishing")
                try:
                    if resume_item:
                        await resume_current_draft(page, item)
                    else:
                        await upload_one(page, item)
                except PublishStopRequested as exc:
                    # 终止是用户明确请求，不进入“人工处理”轮询；保留
                    # 当前草稿/页面和 publishing 状态，便于检查或稍后恢复。
                    keep_browser_open = True
                    update_item(item["id"], "publishing", "已终止自动发布，浏览器页面保持打开")
                    set_publish_state(
                        running=False,
                        message=str(exc),
                        current=item.get("name") or item.get("path") or "（无名）",
                        completed=index - 1,
                        total=len(items),
                        action_required=False,
                        terminated=True,
                    )
                    return
                except Exception as exc:
                    reason = str(exc)
                    item_name = item.get("name") or item.get("path") or "（无名）"
                    # Detect browser disconnects (crashed, closed, or killed).
                    # Do not enter a 30-minute manual-polling loop when the
                    # page is already dead.
                    browser_disconnected = any(
                        phrase in reason.lower()
                        for phrase in (
                            "connection closed",
                            "target closed",
                            "browser has been closed",
                            "browser closed",
                            "page closed",
                            "protocol error",
                            "websocket",
                            "cdp",
                        )
                    )
                    if browser_disconnected:
                        keep_browser_open = False
                        update_item(item["id"], "failed", f"浏览器已断开：{reason}")
                        mark_automatic_pause(
                            f"浏览器连接已断开：{reason}；请检查 Chrome 是否被关闭或崩溃，然后点击继续发布重试",
                            kind="browser_disconnect",
                            current=item_name,
                            completed=index - 1,
                            total=len(items),
                        )
                        return
                    # A schedule error is deterministic (past/malformed time,
                    # missing control, or a platform-clamped value).  Leave
                    # the editor open, mark this item failed, and let the
                    # user edit the time before clicking Continue instead of
                    # keeping the queue locked in a long polling loop.
                    if isinstance(exc, PublishScheduleError):
                        keep_browser_open = True
                        update_item(item["id"], "failed", f"需要调整定时设置：{reason}")
                        mark_automatic_pause(
                            f"{reason}；请调整定时设置后点击继续发布",
                            kind="schedule_error",
                            current=item_name,
                            completed=index - 1,
                            total=len(items),
                        )
                        return
                    # 旧的 publishing 状态可能来自已被抖音清理的草稿。
                    # 找不到草稿时自动回退到当前文件重新上传，不让任务
                    # 空等 30 分钟，也不要求用户手动复制文件。
                    if resume_item and "没有找到可继续编辑的抖音草稿" in reason:
                        try:
                            update_item(item["id"], "publishing", "草稿已失效，正在重新上传")
                            await upload_one(page, item)
                            update_item(item["id"], "published", publish_success_note(item))
                            set_publish_state(
                                running=True,
                                message=f"{publish_success_note(item)}（第 {index}/{len(items)} 条）",
                                current=item_name,
                                completed=index,
                                total=len(items),
                                action_required=False,
                            )
                            continue
                        except PublishStopRequested:
                            keep_browser_open = True
                            update_item(item["id"], "publishing", "已终止自动发布，浏览器页面保持打开")
                            set_publish_state(
                                running=False,
                                message="已终止自动发布：恢复草稿过程中收到终止请求；浏览器页面保持打开",
                                current=item_name,
                                completed=index - 1,
                                total=len(items),
                                action_required=False,
                                terminated=True,
                            )
                            return
                        except Exception as fresh_exc:
                            reason = f"恢复草稿失败，重新上传也失败：{fresh_exc}"
                    update_item(item["id"], "publishing", f"需要人工处理：{reason}")
                    manual_timeout = (
                        3 * 60
                        if isinstance(exc, PublishResultUncertain)
                        else (10 * 60 if is_panorama_item(item) else 30 * 60)
                    )
                    resolved = await wait_for_manual_publish(
                        page,
                        item,
                        reason,
                        timeout_seconds=manual_timeout,
                        allow_click=not isinstance(exc, PublishResultUncertain),
                    )
                    if resolved:
                        update_item(item["id"], "published", publish_success_note(item))
                        set_publish_state(running=True, message=f"{publish_success_note(item)}（第 {index}/{len(items)} 条）", current=item_name, completed=index, total=len(items), action_required=False)
                        continue
                    # 等待超时或仍需人工处理时，保留 Chrome 窗口，不把现场关掉。
                    keep_browser_open = True
                    update_item(item["id"], "failed", f"自动发布暂停：{exc}")
                    timeout_detail = (
                        f"{reason}；全景视频人工处理等待超过 10 分钟，已自动暂停"
                        if is_panorama_item(item)
                        else str(exc)
                    )
                    mark_automatic_pause(
                        timeout_detail,
                        kind="page_error",
                        current=item_name,
                        completed=index - 1,
                        total=len(items),
                    )
                    return
                log(f"第 {index} 条已确认发布/预约：{item.get('name', '')}")
                update_item(item["id"], "published", publish_success_note(item))
                set_publish_state(running=True, message=f"{publish_success_note(item)}（第 {index}/{len(items)} 条）", current=item.get("name", ""), completed=index, total=len(items), action_required=False)
            set_publish_state(running=False, message="全部待发布视频已提交/预约，等待平台处理", current="", completed=len(items), total=len(items), action_required=False, auto_paused=False, pause_source="")
    except PublishStopRequested as exc:
        # 终止请求可能发生在登录、页面导航等队列循环之外；同样保留
        # 浏览器现场并写入可供网页展示的最终状态。
        keep_browser_open = True
        set_publish_state(
            running=False,
            message=str(exc),
            current=app.load_publish_state().get("current", ""),
            completed=app.load_publish_state().get("completed", 0),
            total=app.load_publish_state().get("total", len(items)),
            action_required=False,
            terminated=True,
        )
        return
    except BaseException:
        # 未处理异常也要保留页面，便于用户看到抖音返回的具体校验信息。
        keep_browser_open = True
        raise
    finally:
        if not keep_browser_open:
            await context.close()
            await playwright.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只检查页面，不上传、不发布")
    parser.add_argument("--only", help="只处理指定的视频相对路径")
    parser.add_argument("--resume", action="store_true", help="恢复队列中已上传但停在编辑页的草稿")
    args = parser.parse_args()
    try:
        asyncio.run(run(check_only=args.check, only=args.only, resume=args.resume))
    except Exception as exc:
        mark_automatic_pause(str(exc), kind="process_error")
        raise
