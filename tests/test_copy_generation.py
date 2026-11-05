"""文案库一键填充的隔离回归测试（不启动浏览器、不连平台）。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app


class CopyGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="copy-generate-")
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
                "COPY_LIBRARY_FILE",
            )
        }
        app.STATE_DIR = root
        app.QUEUE_FILE = root / "queue.json"
        app.PUBLISH_STATE_FILE = root / "publish_state.json"
        app.CHANNELS_STATE_FILE = root / "channels_state.json"
        app.ANALYSIS_STATE_FILE = root / "analysis_state.json"
        app.ANALYSIS_CANCEL_FILE = root / "analysis.cancel"
        app.COPY_LIBRARY_FILE = ROOT / "文案" / "song_copy_library.json"
        app._COPY_LIBRARY_CACHE = None
        app.save_queue([
            {
                "id": "2不朽.mp4",
                "name": "2不朽.mp4",
                "path": str(root / "2不朽.mp4"),
                "caption": "2不朽｜演唱会现场 #演唱会 #现场",
                "hashtags": "演唱会,现场",
                "status": "pending",
            },
            {
                "id": "4黑白艺术家.mp4",
                "name": "4黑白艺术家.mp4",
                "path": str(root / "4黑白艺术家.mp4"),
                "work_title": "手写标题",
                "caption": "手写文案，不能覆盖。",
                "hashtags": "手写话题",
                "status": "pending",
            },
            {
                "id": "99未知.mp4",
                "name": "99未知.mp4",
                "path": str(root / "99未知.mp4"),
                "caption": "99未知｜演唱会现场 #演唱会 #现场",
                "hashtags": "演唱会,现场",
                "status": "pending",
            },
        ])

    def tearDown(self) -> None:
        for name, value in self.saved.items():
            setattr(app, name, value)
        app._COPY_LIBRARY_CACHE = None
        self.temp.cleanup()

    def items(self) -> dict[str, dict]:
        return {item["name"]: item for item in json.loads(app.QUEUE_FILE.read_text(encoding="utf-8"))}

    def test_generates_default_copy_and_preserves_manual(self) -> None:
        result, status = app.generate_queue_copy(overwrite=False)
        self.assertEqual(status, 200, result)
        self.assertEqual(len(result["generated"]), 1)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(len(result["unmatched"]), 1)
        self.assertIn("保留手写 1 条", result["message"])
        self.assertIn("未匹配文案库", result["message"])

        items = self.items()
        self.assertEqual(items["2不朽.mp4"]["work_title"], "不朽：一开口就是史诗现场")
        self.assertEqual(items["2不朽.mp4"]["caption"], "有些旋律一响就是史诗，唱的是不灭的热爱。")
        self.assertEqual(items["2不朽.mp4"]["hashtags"], "华晨宇,火星演唱会,演唱会,现场,不朽")

        # 手写条目与未知条目保持原样。
        self.assertEqual(items["4黑白艺术家.mp4"]["caption"], "手写文案，不能覆盖。")
        self.assertEqual(items["4黑白艺术家.mp4"]["work_title"], "手写标题")
        self.assertEqual(items["99未知.mp4"]["caption"], "99未知｜演唱会现场 #演唱会 #现场")

    def test_overwrite_backs_up_and_replaces_manual(self) -> None:
        result, status = app.generate_queue_copy(["4黑白艺术家.mp4"], overwrite=True)
        self.assertEqual(status, 200, result)
        self.assertEqual(len(result["generated"]), 1)
        backup = Path(result["backup"])
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.parent, app.STATE_DIR)

        items = self.items()
        self.assertEqual(items["4黑白艺术家.mp4"]["work_title"], "黑白艺术家：舞台就是他的画布")
        self.assertEqual(items["4黑白艺术家.mp4"]["caption"], "他把舞台当画布，黑与白都能唱出颜色。")
        self.assertEqual(items["4黑白艺术家.mp4"]["hashtags"], "华晨宇,火星演唱会,演唱会,现场,黑白艺术家")

        # 备份保留覆盖前的手写内容。
        backup_items = {item["name"]: item for item in json.loads(backup.read_text(encoding="utf-8"))}
        self.assertEqual(backup_items["4黑白艺术家.mp4"]["caption"], "手写文案，不能覆盖。")

    def test_second_run_is_idempotent(self) -> None:
        app.generate_queue_copy(overwrite=False)
        result, status = app.generate_queue_copy(overwrite=False)
        self.assertEqual(status, 200, result)
        # 已按文案库写入的条目允许重复生成，不算手写跳过。
        self.assertEqual(len(result["generated"]), 1)
        self.assertEqual(len(result["skipped"]), 1)

    def test_unknown_song_is_reported_not_guessed(self) -> None:
        result, status = app.generate_queue_copy(["99未知.mp4"], overwrite=False)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["generated"], [])
        self.assertEqual(len(result["unmatched"]), 1)
        self.assertEqual(result["unmatched"][0]["name"], "99未知.mp4")


if __name__ == "__main__":
    unittest.main(verbosity=2)
