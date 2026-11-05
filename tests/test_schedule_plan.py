"""定时计划前端解析与生成的隔离回归测试。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app
from playwright.sync_api import sync_playwright


def local_input_value(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M")


class SchedulePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="schedule-plan-")
        root = Path(self.temp.name)
        self.saved = {
            name: getattr(app, name)
            for name in (
                "STATE_DIR",
                "QUEUE_FILE",
                "PUBLISH_STATE_FILE",
                "CHANNELS_STATE_FILE",
                "ANALYSIS_STATE_FILE",
                "ANALYSIS_CANCEL_FILE",
            )
        }
        app.STATE_DIR = root
        app.QUEUE_FILE = root / "queue.json"
        app.PUBLISH_STATE_FILE = root / "publish_state.json"
        app.CHANNELS_STATE_FILE = root / "channels_state.json"
        app.ANALYSIS_STATE_FILE = root / "analysis_state.json"
        app.ANALYSIS_CANCEL_FILE = root / "analysis.cancel"
        app.save_queue([
            {
                "id": f"video-{index}.mp4",
                "name": f"{index}测试.mp4",
                "path": str(root / f"{index}测试.mp4"),
                "caption": f"{index}测试｜演唱会现场 #演唱会 #现场",
                "hashtags": "演唱会,现场",
                "status": "pending",
                "schedule_mode": "immediate",
            }
            for index in range(1, 4)
        ])
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(channel="chrome", headless=True)
        self.page = self.browser.new_page()
        self.page.goto(self.base, wait_until="networkidle")
        self.page.wait_for_selector(".item", timeout=15000)

    def tearDown(self) -> None:
        self.browser.close()
        self.playwright.stop()
        self.server.shutdown()
        for name, value in self.saved.items():
            setattr(app, name, value)
        self.temp.cleanup()

    def queue(self) -> list[dict]:
        return json.loads(app.QUEUE_FILE.read_text(encoding="utf-8"))

    def test_parse_accepts_seconds_and_slashes(self) -> None:
        result = self.page.evaluate("""() => ({
            standard: Boolean(parseLocalDateTime('2026-11-04T13:42')),
            seconds: Boolean(parseLocalDateTime('2026-11-04T13:42:30')),
            slash: Boolean(parseLocalDateTime('2026/11/04 13:42')),
            fullwidth: Boolean(parseLocalDateTime('２０２６-１１-０４T１４：００')),
            hidden: Boolean(parseLocalDateTime('2026-11-04T14:00\\u200b')),
            hiddenInside: Boolean(parseLocalDateTime('2026-11-04T14:\\u200b00')),
            invalidDay: parseLocalDateTime('2026-02-30T13:42'),
            fallback: parseDateTimeInput({value: 'bad', valueAsNumber: 1793800800000})?.getTime()
                === new Date(2026, 10, 4, 14, 0, 0, 0).getTime(),
        })""")
        self.assertTrue(result["standard"])
        self.assertTrue(result["seconds"])
        self.assertTrue(result["slash"])
        self.assertTrue(result["fullwidth"])
        self.assertTrue(result["hidden"])
        self.assertTrue(result["hiddenInside"])
        self.assertIsNone(result["invalidDay"])
        self.assertTrue(result["fallback"])

    def test_preview_and_generate_plan(self) -> None:
        first = datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=10)
        value = local_input_value(first)
        self.page.fill("#scheduleStartAt", value)
        self.page.dispatch_event("#scheduleStartAt", "input")
        preview = self.page.locator("#schedulePlanPreview").inner_text()
        self.assertIn("首条", preview)
        self.assertIn("共 3 条", preview)

        self.page.click("#generateScheduleBtn")
        self.page.wait_for_function(
            "document.querySelector('#schedulePlanHint')?.textContent.includes('已为 3 条')",
            timeout=15000,
        )
        items = self.queue()
        self.assertTrue(all(item["schedule_mode"] == "scheduled" for item in items))
        self.assertEqual(items[0]["schedule_at"], value)
        self.assertEqual(items[0]["schedule_interval_minutes"], "10")
        self.assertEqual(items[1]["schedule_at"], local_input_value(first + timedelta(minutes=10)))
        self.assertEqual(items[2]["schedule_interval_minutes"], "")

    def test_seconds_value_generates_and_normalizes(self) -> None:
        first = datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=10)
        value = local_input_value(first)
        self.page.evaluate(
            "value => { const input = document.querySelector('#scheduleStartAt'); input.value = value; input.dispatchEvent(new Event('input', {bubbles: true})); }",
            value + ":30",
        )
        self.page.click("#generateScheduleBtn")
        self.page.wait_for_function(
            "document.querySelector('#schedulePlanHint')?.textContent.includes('已为 3 条')",
            timeout=15000,
        )
        items = self.queue()
        self.assertEqual(items[0]["schedule_at"], value)
        self.assertTrue(all(item["schedule_mode"] == "scheduled" for item in items))

    def test_default_button_fills_a_valid_time(self) -> None:
        self.page.fill("#scheduleStartAt", "")
        self.page.click("#defaultScheduleBtn")
        value = self.page.input_value("#scheduleStartAt")
        self.assertTrue(value)
        self.assertIn("已填入默认时间", self.page.locator("#schedulePlanHint").inner_text())
        self.assertIn("首条", self.page.locator("#schedulePlanPreview").inner_text())

    def test_empty_input_falls_back_to_default_instead_of_blocking(self) -> None:
        self.page.fill("#scheduleStartAt", "")
        self.page.click("#generateScheduleBtn")
        self.page.wait_for_function(
            "document.querySelector('#schedulePlanHint')?.textContent.includes('已为 3 条')",
            timeout=15000,
        )
        self.assertTrue(self.page.input_value("#scheduleStartAt"))
        self.assertTrue(all(item["schedule_mode"] == "scheduled" for item in self.queue()))

    def test_past_time_warns_and_editing_clears_warning(self) -> None:
        past = datetime.now().replace(second=0, microsecond=0) - timedelta(minutes=5)
        self.page.fill("#scheduleStartAt", local_input_value(past))
        self.page.click("#generateScheduleBtn")
        self.page.wait_for_function(
            "document.querySelector('#schedulePlanHint')?.textContent.includes('必须晚于当前时间')",
            timeout=5000,
        )
        self.assertTrue(self.page.locator("#schedulePlanHint").evaluate("el => el.classList.contains('warning')"))

        future = datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=15)
        self.page.fill("#scheduleStartAt", local_input_value(future))
        self.page.dispatch_event("#scheduleStartAt", "input")
        self.assertEqual(self.page.locator("#schedulePlanHint").inner_text(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
