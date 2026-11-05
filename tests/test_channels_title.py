"""Pure helpers for the Video Account short-title fallback."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import channels_publisher as channels


class ChannelsTitleTests(unittest.TestCase):
    def test_split_keeps_song_and_returns_date_place(self):
        cases = {
            "开场，2026.10.06南京": ("开场", "2026.10.06南京"),
            "点燃银河尽头的篝火 2026年10月06日南京": ("点燃银河尽头的篝火", "2026年10月06日南京"),
            "好想爱这个世界啊丨2026.10.06 南京": ("好想爱这个世界啊", "2026.10.06 南京"),
        }
        for value, expected in cases.items():
            self.assertEqual(channels.split_channels_title(value), expected)

    def test_short_title_without_date_is_not_split(self):
        self.assertEqual(channels.split_channels_title("风之海"), ("风之海", ""))

    def test_fit_keeps_long_song_or_first_six_for_short_song(self):
        self.assertEqual(
            channels.fit_channels_short_title("好想爱这个世界啊 2026年10月06日南京"),
            ("好想爱这个世界啊", "2026年10月06日南京"),
        )
        short, suffix = channels.fit_channels_short_title("黑白艺术家 2026年10月06日南京")
        self.assertEqual(short, "黑白艺术家 ")
        self.assertEqual(suffix, "2026年10月06日南京")

    def test_sanitize_keeps_song_and_does_not_truncate_to_28(self):
        long_name = "点燃银河尽头的篝火" * 2
        value = channels.sanitize_channels_title({"work_title": f"{long_name}，2026.10.06南京"})
        self.assertIn("点燃银河尽头的篝火", value)
        self.assertGreater(len(value), 28)
        self.assertNotIn("，", value)


if __name__ == "__main__":
    unittest.main()
