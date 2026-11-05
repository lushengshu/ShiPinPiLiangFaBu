"""封面文件名智能匹配的隔离回归测试（不启动浏览器、不连平台）。"""
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


class CoverScoreTests(unittest.TestCase):
    """歌曲名优先、编号只做辅证，明确“视频NN”编号仍然权威。"""

    def assertMatch(self, video: str, image: str, minimum: float = 0.93) -> None:
        score, reason = app.cover_match_score(video, image)
        self.assertGreaterEqual(score, minimum, f"{video} ↔ {image}: {score} {reason}")

    def assertNoMatch(self, video: str, image: str) -> None:
        score, reason = app.cover_match_score(video, image)
        self.assertLess(score, 0.72, f"{video} ↔ {image}: {score} {reason}")

    def test_design_batch_number_differs_from_video_number(self) -> None:
        # 01_/02_ 是设计批次序号，与视频名里的 2/4 不一致，仍应按歌曲名匹配。
        self.assertMatch("2不朽.mp4", "01_不朽_横版_4比3_20261031.png")
        self.assertMatch("2不朽.mp4", "02_不朽_横版_20261103.png")
        self.assertMatch("4黑白艺术家.mp4", "02_黑白艺术家_横版_20261103.png")
        self.assertMatch("28向阳而生.mp4", "04_向阳而生_横版_4比3_20261101.png")
        self.assertMatch("3七重人格1.mp4", "03_七重人格_横版_20261103.png")

    def test_dates_decoration_and_versions_are_ignored(self) -> None:
        self.assertMatch("1开场.mp4", "01_开场_横版_4比3_20261031.png")
        self.assertMatch("1开场.mp4", "1开场_竖版_3比4_20261102.png")
        self.assertMatch("10卡西莫多的礼物.mp4", "10_卡西莫多的礼物_横版_4比3_20261102.png")
        self.assertMatch("27FOR FOREVER.mp4", "11_FOR FOREVER_横版_20261103.png")
        self.assertMatch("17let YOU go.mp4", "17_let YOU go_竖版_20261103.png")

    def test_explicit_video_number_still_wins(self) -> None:
        # “视频01”是明确身份，不能因为歌名同为向阳而生就贴到 28 号。
        self.assertNoMatch("28向阳而生.mp4", "向阳而生_视频01_横版_4比3.jpg")
        self.assertMatch("10卡西莫多的礼物.mp4", "卡西莫多的礼物_视频10_横版_4比3.jpg", 0.98)

    def test_similar_song_titles_do_not_cross_match(self) -> None:
        self.assertNoMatch("11好想我回来啊.mp4", "12_好想爱这个世界啊_横版_4比3_20261031.png")
        self.assertNoMatch("2不朽.mp4", "03_异类_横版_20261103.png")
        self.assertNoMatch("演唱会终场.mp4", "完全不同的封面.jpg")

    def test_legacy_number_only_names_still_work(self) -> None:
        self.assertMatch("1开场.mp4", "1开场封面.jpg", 0.94)
        self.assertMatch("3比4.mp4", "3比4封面.png", 0.94)
        self.assertMatch("1开场.mp4", "1封面.jpg", 0.84)

    def test_orientation_detection(self) -> None:
        self.assertEqual(app.cover_orientation("01_不朽_横版_4比3_20261031.png"), "landscape")
        self.assertEqual(app.cover_orientation("01_不朽_竖版_3比4_20261031.png"), "portrait")
        self.assertEqual(app.cover_orientation("01_不朽.png"), "")


class CoverImportTests(unittest.TestCase):
    """临时目录内跑完整导入，验证横竖版写入正确视频。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="cover-match-")
        root = Path(self.temp.name)
        self.saved = {
            name: getattr(app, name)
            for name in ("STATE_DIR", "QUEUE_FILE", "COVER_DIR", "PUBLISH_STATE_FILE", "CHANNELS_STATE_FILE")
        }
        app.STATE_DIR = root
        app.QUEUE_FILE = root / "queue.json"
        app.COVER_DIR = root / "covers"
        app.PUBLISH_STATE_FILE = root / "publish_state.json"
        app.CHANNELS_STATE_FILE = root / "channels_state.json"
        videos = ["2不朽.mp4", "1开场.mp4", "4黑白艺术家.mp4", "28向阳而生.mp4"]
        app.save_queue([
            {
                "id": name,
                "name": name,
                "path": str(root / name),
                "video_type": "normal",
                "caption": name,
                "status": "pending",
            }
            for name in videos
        ])

    def tearDown(self) -> None:
        for name, value in self.saved.items():
            setattr(app, name, value)
        self.temp.cleanup()

    def test_import_links_landscape_and_portrait_by_song_title(self) -> None:
        files = [
            (b"landscape-1", "01_不朽_横版_4比3_20261031.png"),
            (b"portrait-1", "01_不朽_竖版_3比4_20261031.png"),
            (b"landscape-2", "01_开场_横版_4比3_20261031.png"),
            (b"portrait-2", "01_开场_竖版_3比4_20261031.png"),
            (b"landscape-3", "02_黑白艺术家_横版_20261103.png"),
            (b"portrait-3", "02_黑白艺术家_竖版_20261103.png"),
            (b"landscape-4", "04_向阳而生_横版_4比3_20261101.png"),
            (b"portrait-4", "04_向阳而生_竖版_3比4_20261101.png"),
            (b"unrelated", "12_好想爱这个世界啊_横版_4比3_20261031.png"),
        ]
        result, status = app.import_cover_images(files)
        self.assertEqual(status, 200, result)
        self.assertEqual(len(result["unmatched"]), 1)
        self.assertIn("好想爱这个世界啊", result["unmatched"][0]["filename"])

        items = {item["name"]: item for item in json.loads(app.QUEUE_FILE.read_text(encoding="utf-8"))}
        expected = {
            "2不朽.mp4": ("01_不朽_横版_4比3_20261031.png", "01_不朽_竖版_3比4_20261031.png"),
            "1开场.mp4": ("01_开场_横版_4比3_20261031.png", "01_开场_竖版_3比4_20261031.png"),
            "4黑白艺术家.mp4": ("02_黑白艺术家_横版_20261103.png", "02_黑白艺术家_竖版_20261103.png"),
            "28向阳而生.mp4": ("04_向阳而生_横版_4比3_20261101.png", "04_向阳而生_竖版_3比4_20261101.png"),
        }
        for name, (landscape, portrait) in expected.items():
            self.assertEqual(items[name]["cover_image_landscape_name"], landscape, name)
            self.assertEqual(items[name]["cover_image_portrait_name"], portrait, name)
            self.assertTrue((app.STATE_DIR / items[name]["cover_image_landscape"]).is_file(), name)
            self.assertTrue((app.STATE_DIR / items[name]["cover_image_portrait"]).is_file(), name)
        # 未匹配到队列的封面不能被错误挂到任何视频上。
        self.assertFalse(items["28向阳而生.mp4"]["cover_image_landscape_name"].startswith("12_"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
