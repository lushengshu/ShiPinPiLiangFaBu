#!/usr/bin/env python3
"""隔离验证本地视频发布工具的主按钮、卡片按钮和关键 API。

测试不读取或修改正式 .douyin_batch 队列，不启动真实抖音/视频号发布器。
所有外部进程和文件选择器都由本地替身接管。
"""

from __future__ import annotations

import json
import re
import sys
import subprocess as real_subprocess
import tempfile
import threading
from pathlib import Path
from urllib.request import Request, urlopen

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import app


class Harness:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="douyin-button-matrix-"))
        self._patches: list[tuple[object, str, object]] = []
        self._configure_state()
        self._install_fakes()
        self.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def _configure_state(self) -> None:
        app.STATE_DIR = self.root
        app.QUEUE_FILE = self.root / "queue.json"
        app.COVER_DIR = self.root / "covers"
        app.PUBLISH_STATE_FILE = self.root / "publish_state.json"
        app.ANALYSIS_STATE_FILE = self.root / "analysis_state.json"
        app.ANALYSIS_CANCEL_FILE = self.root / "analysis.cancel"
        app.PAUSE_REQUEST_FILE = self.root / "pause.requested"
        app.STOP_REQUEST_FILE = self.root / "stop.requested"
        app.CHANNELS_STATE_FILE = self.root / "channels_state.json"
        app.MEDIA_ROOT = self.root.resolve()
        (self.root / "normal.mp4").write_bytes(b"")
        (self.root / "panorama.mp4").write_bytes(b"")
        self.reset_queue()
        app.save_publish_state({
            "running": False,
            "paused": False,
            "pause_requested": False,
            "stop_requested": False,
            "terminated": False,
            "auto_paused": False,
            "message": "尚未启动自动发布",
        })
        app.save_channels_state({
            "running": False,
            "status": "idle",
            "message": "尚未启动视频号发布",
            "pid": None,
            "completed": 0,
            "total": 0,
            "current": "",
        })

    def reset_queue(self) -> None:
        normal = app._queue_item_for_path(self.root / "normal.mp4")
        panorama = app._queue_item_for_path(
            self.root / "panorama.mp4",
            default_video_type="panorama",
            default_panorama_format="360_mono",
        )
        app.save_queue([normal, panorama])

    def patch(self, target: object, name: str, replacement: object) -> None:
        self._patches.append((target, name, getattr(target, name)))
        setattr(target, name, replacement)

    def _install_fakes(self) -> None:
        def fake_analyze(item_id: str):
            return {
                "ok": True,
                "analysis": {
                    "mode": "test",
                    "recommended": {
                        "title": "测试推荐",
                        "caption": "隔离测试文案",
                        "hashtags": "测试,现场",
                    },
                    "candidates": [],
                    "evidence": [],
                },
            }, 200

        def fake_batch(ids=None):
            ids = [str(x) for x in (ids or [])]
            state = {
                "running": False,
                "status": "completed",
                "total": len(ids),
                "completed": len(ids),
                "current": "",
                "message": "隔离批量识别完成",
                "results": {
                    item_id: {
                        "status": "completed",
                        "analysis": {
                            "recommended": {
                                "title": "批量推荐",
                                "caption": "批量隔离文案",
                                "hashtags": "批量,测试",
                            },
                            "candidates": [],
                            "evidence": [],
                        },
                    }
                    for item_id in ids
                },
            }
            app.save_analysis_state(state)
            return {"ok": True, "state": state}, 202

        def fake_cancel():
            state = app.load_analysis_state()
            state.update({"running": False, "status": "cancelled", "message": "隔离识别已停止"})
            app.save_analysis_state(state)
            return {"ok": True, "state": state}, 200

        def fake_apply(ids=None):
            requested = {str(x) for x in (ids or [])}
            items = app.load_queue()
            applied = []
            for item in items:
                item_id = str(item.get("id") or item.get("path") or "")
                if item_id in requested:
                    item["caption"] = "批量隔离文案"
                    item["hashtags"] = "批量,测试"
                    applied.append(item_id)
            app.save_queue(items)
            return {
                "ok": True,
                "applied": applied,
                "applied_count": len(applied),
                "skipped": [],
                "skipped_count": 0,
                "error_count": 0,
                "message": f"已采用 {len(applied)} 条推荐",
            }, 200

        def fake_channels(ids):
            ids = [str(x) for x in ids]
            state = {
                "running": False,
                "status": "completed",
                "message": "隔离视频号发布完成",
                "pid": None,
                "completed": len(ids),
                "total": len(ids),
                "current": "",
            }
            app.save_channels_state(state)
            return {"ok": True, "state": state, "count": len(ids), "skipped": []}, 202

        def fake_control(action: str):
            state = app.load_publish_state()
            if action == "pause":
                state.update({"running": True, "paused": True, "message": "隔离任务已暂停"})
            elif action == "resume":
                state.update({"running": True, "paused": False, "message": "隔离任务已恢复"})
            elif action == "stop":
                state.update({"running": False, "paused": False, "terminated": True, "message": "自动发布已强制终止"})
            app.save_publish_state(state)
            return {"ok": True, "state": state}, 200

        class FakeProcess:
            pid = __import__("os").getpid()

            def wait(self):
                return 0

        self.patch(app, "analyze_queue_item", fake_analyze)
        self.patch(app, "start_analysis_batch", fake_batch)
        self.patch(app, "cancel_analysis_batch", fake_cancel)
        self.patch(app, "apply_batch_recommendations", fake_apply)
        self.patch(app, "start_channels_publish_queue", fake_channels)
        self.patch(app, "publish_control", fake_control)
        class SubprocessProxy:
            def __getattr__(self, name):
                return getattr(real_subprocess, name)

            def Popen(self, *args, **kwargs):
                return FakeProcess()

        # Do not mutate the shared stdlib subprocess module: Playwright uses
        # that same module to launch its browser driver.
        self.patch(app, "subprocess", SubprocessProxy())
        self.patch(app, "choose_media_folder", lambda: None)
        self.patch(app, "choose_media_files", lambda: [])

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        for target, name, original in reversed(self._patches):
            setattr(target, name, original)


def request_json(base: str, path: str, *, method="GET", payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    request = Request(base + path, data=data, headers=headers, method=method)
    try:
        response = urlopen(request, timeout=8)
        raw = response.read()
        return response.status, response.headers, raw
    except Exception as exc:
        response = getattr(exc, "read", lambda: b"{}")()
        return getattr(exc, "code", 599), {}, response


def assert_button_inventory() -> None:
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    js = (ROOT / "static/app.js").read_text(encoding="utf-8")
    ids = re.findall(r'<button\s+id="([^"]+)"', html)
    assert len(ids) == len(set(ids)), "HTML button ids must be unique"
    missing = [button_id for button_id in ids if button_id not in js]
    assert not missing, f"button ids without JS binding/reference: {missing}"
    for class_name in (
        "import-caption", "open-upload", "channels-upload",
        "publish", "open-video", "copy-caption", "delete-item",
    ):
        assert f".{class_name}" in js, f"missing dynamic button handler: {class_name}"


def assert_api_matrix(harness: Harness) -> None:
    base = harness.base_url
    for path in ("/", "/app.js", "/style.css", "/api/state", "/api/publish-status", "/api/channels-status", "/api/analyze-status"):
        status, _, _ = request_json(base, path)
        assert status == 200, (path, status)

    status, headers, raw = request_json(base, "/api/caption-template", method="POST", payload={"items": []})
    assert status == 200 and "spreadsheet" in headers.get("Content-Type", "") and raw[:2] == b"PK"
    status, headers, raw = request_json(base, "/api/export-unmatched", method="POST", payload={"unmatched": [], "available_names": []})
    assert status == 200 and "spreadsheet" in headers.get("Content-Type", "") and raw[:2] == b"PK"

    queue = app.load_queue()
    ids = [str(item["id"]) for item in queue]
    status, _, _ = request_json(base, "/api/items/bulk-preferences", method="POST", payload={"ids": ids, "field": "allow_download", "value": False})
    assert status == 200
    status, _, _ = request_json(base, f"/api/items/{ids[0]}", method="PATCH", payload={"caption": "按钮矩阵测试", "work_title": "按钮测试"})
    assert status == 200
    status, _, _ = request_json(base, "/api/schedule-plan", method="POST", payload={"updates": [{"id": ids[0], "schedule_mode": "immediate", "schedule_at": "", "schedule_interval_minutes": ""}]})
    assert status == 200
    assert app.cover_match_score("1开场.mp4", "1开场封面.jpg")[0] >= 0.94
    assert app.cover_match_score("3比4.mp4", "3比4封面.png")[0] >= 0.94
    assert app.cover_match_score("1开场.mp4", "1封面.jpg")[0] >= 0.84
    assert app.cover_match_score("10卡西莫多的礼物.mp4", "卡西莫多的礼物_视频10_横版_4比3.jpg")[0] >= 0.98
    assert app.cover_match_score("4黑白艺术家.mp4", "._黑白艺术家_视频04_竖版_3比4.jpg")[0] >= 0.98
    assert app.cover_match_score("28向阳而生.mp4", "向阳而生_视频01_横版_4比3.jpg")[0] < 0.72
    assert app.cover_match_score("演唱会终场.mp4", "完全不同的封面.jpg")[0] < 0.72
    cover_payload, cover_status = app.import_cover_images([
        (b"fake-image", "normal.jpg"),
        (b"fake-image", "missing-video.png"),
    ])
    assert cover_status == 200 and len(cover_payload["matched"]) == 1 and len(cover_payload["unmatched"]) == 1
    assert app.load_queue()[0].get("cover_image_name") == "normal.jpg"
    variant_payload, variant_status = app.import_cover_images([
        (b"fake-landscape", "normal_横版_4比3.jpg"),
        (b"fake-portrait", "normal_竖版_3比4.jpg"),
    ])
    assert variant_status == 200 and len(variant_payload["matched"]) == 2
    assert len(variant_payload["added"]) == 2 and len(variant_payload["replaced"]) == 0
    assert app.load_queue()[0].get("cover_image_landscape_name") == "normal_横版_4比3.jpg"
    assert app.load_queue()[0].get("cover_image_portrait_name") == "normal_竖版_3比4.jpg"
    # 分批增量导入：只上传新一轮中的横版时，旧竖版必须保留；同一版本再次导入时替换。
    incremental_payload, incremental_status = app.import_cover_images([
        (b"replacement-landscape", "normal_横版_4比3.jpg"),
        (b"new-video-cover", "panorama_横版_4比3.jpg"),
    ])
    assert incremental_status == 200
    assert len(incremental_payload["replaced"]) == 1 and len(incremental_payload["added"]) == 1
    current = app.load_queue()
    assert current[0].get("cover_image_landscape_name") == "normal_横版_4比3.jpg"
    assert current[0].get("cover_image_portrait_name") == "normal_竖版_3比4.jpg"
    assert current[1].get("cover_image_landscape_name") == "panorama_横版_4比3.jpg"
    unmatched_payload, unmatched_status = app.import_cover_images([
        (b"unknown", "完全不匹配_横版_4比3.jpg"),
    ])
    assert unmatched_status == 200 and not unmatched_payload["matched"]
    assert "旧封面已保留" in unmatched_payload["message"]

    for path, payload in (
        ("/api/select-folder", {}),
        ("/api/select-files", {"video_type": "normal", "panorama_format": ""}),
        ("/api/select-videos", {"video_type": "normal", "panorama_format": ""}),
        ("/api/analyze-batch", {"ids": ids}),
        ("/api/analyze-cancel", None),
        ("/api/analyze-apply", {"ids": ids}),
        ("/api/publish-check", None),
        ("/api/publish", None),
        ("/api/publish-pause", None),
        ("/api/publish-resume", None),
        ("/api/publish-stop", None),
        ("/api/channels-publish", {"item_id": ids[0]}),
        ("/api/channels-publish-queue", {"ids": ids}),
    ):
        status, _, _ = request_json(base, path, method="POST", payload=payload)
        assert status in {200, 202, 409}, (path, status)

    status, _, _ = request_json(base, "/api/open?path=not-a-video.txt")
    assert status == 400
    status, _, _ = request_json(base, "/api/import-captions", method="POST", payload={})
    assert status == 400

    # 删除和清空只在隔离队列执行，验证两条入口都能完成。
    harness.reset_queue()
    ids = [str(item["id"]) for item in app.load_queue()]
    status, _, _ = request_json(base, "/api/items/delete", method="POST", payload={"ids": [ids[0]]})
    assert status == 200 and len(app.load_queue()) == 1
    status, _, _ = request_json(base, "/api/clear-queue", method="POST")
    assert status == 200 and app.load_queue() == []


def assert_ui_matrix(harness: Harness) -> None:
    harness.reset_queue()
    app.save_publish_state({
        "running": False, "paused": False, "pause_requested": False,
        "stop_requested": False, "terminated": False, "auto_paused": False,
        "message": "尚未启动自动发布",
    })
    app.save_channels_state({
        "running": False, "status": "idle", "message": "尚未启动视频号发布",
        "pid": None, "completed": 0, "total": 0, "current": "",
    })
    app.save_analysis_state(app._default_analysis_state())
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True, permissions=["clipboard-read", "clipboard-write"])
        page = context.new_page()
        page.set_default_timeout(3000)
        page.add_init_script("""
            Object.defineProperty(navigator, 'clipboard', {
              configurable: true,
              value: {writeText: async value => { window.__copiedText = String(value); }}
            });
        """)
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(harness.base_url + "/", wait_until="domcontentloaded")
        page.wait_for_selector("article.item")
        assert page.locator("article.item").count() == 2
        for removed_id in ("#scanBtn", "#batchAnalyzeBtn", "#cancelBatchAnalyzeBtn", "#batchAdoptBtn", "#analysisBatchStatus"):
            assert page.locator(removed_id).count() == 0, f"removed control still rendered: {removed_id}"
        initial_actions = page.locator("article.item").first.locator(".item-actions-primary button").all_text_contents()
        assert initial_actions.index("发布视频号") < initial_actions.index("打开视频") < initial_actions.index("复制文案")
        assert initial_actions.index("复制文案") < initial_actions.index("跳过") < initial_actions.index("删除")
        assert page.locator("article.item").first.locator(".analyze-item, .analysis-details, .item-more-details").count() == 0
        assert page.locator("article.item details.item-editor-details[open]").count() == 2
        tone_classes = page.locator("article.item").evaluate_all("els => els.map(el => [...el.classList].find(name => name.startsWith('queue-tone-')))")
        assert tone_classes == ["queue-tone-0", "queue-tone-1"], tone_classes

        # 主按钮：菜单、选择器取消、批量设置、定时设置。
        page.locator("#mediaPickerBtn").click()
        assert page.locator("#mediaPickerMenu").get_attribute("hidden") is None
        page.keyboard.press("Escape")
        page.locator("#mediaPickerBtn").click()
        page.locator("#folderBtn").click()
        page.locator("#mediaPickerBtn").click()
        page.locator("#filesBtn").click()
        page.locator("#selectAllQueue").check()
        assert "已选择 2 条" in page.locator("#selectionSummary").inner_text()
        page.locator("#bulkDownload").select_option("false")
        page.locator("#applyBulkDownload").click()
        page.wait_for_timeout(150)
        assert all(item.get("allow_download") is False for item in app.load_queue())
        page.locator("#bulkOfficialTopic").select_option("false")
        page.locator("#applyBulkOfficialTopic").click()
        page.wait_for_timeout(150)
        assert app.load_queue()[1].get("official_topic_auto") is False

        page.locator("#bulkVideoType").select_option("normal")
        page.locator("#applyBulkVideoType").click()
        page.wait_for_timeout(150)
        assert all(item.get("video_type") == "normal" for item in app.load_queue())

        page.locator("#schedulePlanMode").select_option("immediate")
        page.locator("#allImmediateBtn").click()
        page.wait_for_timeout(200)
        assert all(item.get("schedule_mode") == "immediate" for item in app.load_queue())

        # 环境检查、模板下载、文案导入。
        page.locator("#checkBtn").click()
        page.wait_for_timeout(150)
        # 检查按钮的真实子进程会在完成后落盘 idle；替身进程不执行浏览器
        # 检查，因此在继续验证可编辑按钮前显式写回完成态。
        app.save_publish_state({
            "running": False, "paused": False, "pause_requested": False,
            "stop_requested": False, "terminated": False, "auto_paused": False,
            "message": "页面检查通过",
        })
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("article.item", timeout=8000)
        page.locator("#copyMenuBtn").click()
        with page.expect_download() as download_info:
            page.locator("#downloadTemplateBtn").click()
        assert download_info.value.suggested_filename.endswith(".xlsx")
        page.locator("article.item").first.locator(".item-checkbox").check()
        page.locator("#copyMenuBtn").click()
        page.once("dialog", lambda dialog: dialog.accept())
        with page.expect_file_chooser() as chooser_info:
            page.locator("#importCopyBtn").click()
        chooser_info.value.set_files({"name": "copy.txt", "mimeType": "text/plain", "buffer": "按钮导入文案 #测试".encode("utf-8")})
        page.wait_for_timeout(250)
        assert "按钮导入文案" in app.load_queue()[0].get("caption", "")
        with page.expect_file_chooser() as cover_chooser_info:
            page.locator("#coverImportBtn").click()
        cover_chooser_info.value.set_files({"name": "normal.jpg", "mimeType": "image/jpeg", "buffer": b"fake-image"})
        page.wait_for_timeout(250)
        assert app.load_queue()[0].get("cover_image_name") == "normal.jpg"
        assert page.locator("article.item").first.locator(".cover-image-value.has-cover").count() == 1
        # 前端也要支持分两轮导入：先导入横版，再导入竖版；第二轮不能清除第一轮。
        with page.expect_file_chooser() as landscape_chooser_info:
            page.locator("#coverImportBtn").click()
        landscape_chooser_info.value.set_files({"name": "normal_横版_4比3.jpg", "mimeType": "image/jpeg", "buffer": b"landscape-round-1"})
        page.wait_for_timeout(250)
        assert app.load_queue()[0].get("cover_image_landscape_name") == "normal_横版_4比3.jpg"
        with page.expect_file_chooser() as portrait_chooser_info:
            page.locator("#coverImportBtn").click()
        portrait_chooser_info.value.set_files({"name": "normal_竖版_3比4.jpg", "mimeType": "image/jpeg", "buffer": b"portrait-round-1"})
        page.wait_for_timeout(250)
        assert app.load_queue()[0].get("cover_image_landscape_name") == "normal_横版_4比3.jpg"
        assert app.load_queue()[0].get("cover_image_portrait_name") == "normal_竖版_3比4.jpg"
        assert "替换 0 张，新增 1 张" in page.locator("#coverImportHint").text_content()

        # 顶部跳转、视频号发布，以及卡片级功能。
        with page.expect_popup() as popup_info:
            page.locator("#uploadBtn").click()
        popup_info.value.close()
        assert page.locator("#channelsPublishBtn").is_hidden()
        card = page.locator("article.item").first
        card.locator(".copy-caption").click()
        page.wait_for_function("Boolean(window.__copiedText)", timeout=2000)
        card.locator(".open-video").click()
        card.locator(".publish").click()
        page.wait_for_timeout(150)
        assert app.load_queue()[0].get("status") == "published"
        card.locator(".publish").click()
        page.wait_for_timeout(150)
        assert app.load_queue()[0].get("status") == "pending"
        card.locator(".skip-item").click()
        page.wait_for_timeout(150)
        assert app.load_queue()[0].get("status") == "skipped"

        # 顶部发布状态按钮：开始 -> 暂停 -> 继续 -> 终止；终止后回到重新开始。
        harness.reset_queue()
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("article.item")
        page.locator("#autoPublishBtn").click()
        page.wait_for_timeout(150)
        assert app.load_publish_state().get("running") is True
        assert page.locator("#autoPublishBtn").inner_text() == "暂停发布"
        page.locator("#autoPublishBtn").click()
        page.wait_for_timeout(150)
        assert app.load_publish_state().get("paused") is True
        page.locator("#autoPublishBtn").click()
        page.wait_for_timeout(150)
        assert app.load_publish_state().get("paused") is False
        page.locator("#stopPublishBtn").click()
        page.wait_for_timeout(150)
        assert app.load_publish_state().get("terminated") is True
        assert page.locator("#autoPublishBtn").inner_text() == "重新开始发布"

        # 选择删除与清空在隔离队列内执行，避免触碰正式数据。
        harness.reset_queue()
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("article.item")
        page.locator("article.item").first.locator(".item-checkbox").check()
        page.locator("#deleteSelectedBtn").click()
        page.wait_for_timeout(150)
        assert len(app.load_queue()) == 1
        page.locator("#clearQueueBtn").click()
        page.wait_for_timeout(150)
        assert app.load_queue() == []
        context.close()
        browser.close()


def main() -> None:
    assert_button_inventory()
    harness = Harness()
    try:
        assert_api_matrix(harness)
        assert_ui_matrix(harness)
    finally:
        harness.close()
    print("PASS: button inventory, API matrix, isolated UI clicks, queue mutations, and overall flow")


if __name__ == "__main__":
    main()
