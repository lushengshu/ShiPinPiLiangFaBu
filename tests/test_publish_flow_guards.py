"""发布流程关键门的隔离测试：成功判定、描述回读、内容管理核对。"""
from __future__ import annotations

import sys
import threading
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import browser_publisher as publisher
from playwright.async_api import async_playwright


TODAY = datetime.now()
TODAY_TEXT = f"{TODAY.year}年{TODAY.month}月{TODAY.day}日"
OLD_DATE = "2026年10月26日"

MANAGEMENT_HTML = f"""<!doctype html><meta charset="utf-8">
<div class="video-card-content-old">
  <div>风之海 *海风浪花和自由，这首歌就是夏天的代名词。 #火星演唱会 #华晨宇</div>
  <div>{OLD_DATE} 17:10</div><div>已发布</div>
</div>
<div class="video-card-content-new">
  <div>灯一暗，鼓点一响，火星的夜晚正式开场。 #华晨宇 #火星演唱会 #演唱会 #现场 #开场</div>
  <div>{TODAY_TEXT} 13:10</div><div>已发布</div>
</div>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence test server
        pass

    def do_GET(self):
        body = MANAGEMENT_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class PublishGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(channel="chrome", headless=True)
        self.page = await self.browser.new_page()
        base = f"http://127.0.0.1:{self.server.server_port}"
        await self.page.goto(base + "/creator-micro/content/manage", wait_until="domcontentloaded")

    async def asyncTearDown(self) -> None:
        await self.browser.close()
        await self.playwright.stop()
        self.server.shutdown()

    def test_description_match_tolerates_zero_width_and_tag_chips(self) -> None:
        expected = "灯一暗，鼓点一响，火星的夜晚正式开场。 #华晨宇 #现场"
        self.assertTrue(publisher._text_matches(
            "灯一暗， 鼓点一响，火星的夜晚正式开场。\u200b #华晨宇 #现场",
            expected,
            "描述",
        ))
        # 平台把话题变成不带 # 的 chip。
        self.assertTrue(publisher._text_matches(
            "灯一暗，鼓点一响，火星的夜晚正式开场。 华晨宇 现场",
            expected,
            "描述",
        ))
        self.assertFalse(publisher._text_matches(
            "完全不同的描述。 #华晨宇 #现场",
            expected,
            "描述",
        ))

    async def test_slate_fill_tolerates_zero_width_inserted_by_editor(self) -> None:
        await self.page.set_content(
            '<div id="ed" contenteditable="true" data-slate-editor="true"></div>'
            '<script>'
            'document.getElementById("ed").addEventListener("input", e => {'
            '  if (!e.target.innerText.endsWith("\\u200b")) e.target.innerText += "\\u200b";'
            '});'
            '</script>'
        )
        control = self.page.locator("#ed")
        await publisher._fill_verified_text(
            control,
            "灯一暗，鼓点一响，火星的夜晚正式开场。 #华晨宇 #现场",
            "描述",
        )
        self.assertIn("灯一暗", await control.inner_text())

    async def test_post_publish_body_fallback_without_loaded_cards(self) -> None:
        base = self.page.url.split("/creator-micro")[0]
        await self.page.goto(base + "/creator-micro/content/manage?enter_from=publish", wait_until="domcontentloaded")
        await self.page.set_content(
            "<div>开场丨2026.10.06南京 灯一暗，鼓点一响，火星的夜晚正式开场。</div>"
        )
        item = {
            "work_title": "开场丨2026.10.06南京",
            "caption": "灯一暗，鼓点一响，火星的夜晚正式开场。",
            "name": "1开场.mp4",
        }
        self.assertTrue(await publisher.submission_visible_in_management(self.page, item))

    async def test_management_card_check_ignores_old_same_title_work(self) -> None:
        old_item = {
            "work_title": "风之海丨2026.10.06南京",
            "caption": "海风浪花和自由，这首歌就是夏天的代名词。",
            "hashtags": "华晨宇,火星演唱会,演唱会,现场,风之海",
            "name": "24风之海.mp4",
        }
        self.assertFalse(await publisher.submission_visible_in_management(self.page, old_item))

        new_item = {
            "work_title": "开场丨2026.10.06南京",
            "caption": "灯一暗，鼓点一响，火星的夜晚正式开场。",
            "hashtags": "华晨宇,火星演唱会,演唱会,现场,开场",
            "name": "1开场.mp4",
        }
        self.assertTrue(await publisher.submission_visible_in_management(self.page, new_item))

    async def test_publish_button_prefers_footer_not_navigation_tab(self) -> None:
        await self.page.set_content(
            '<nav><button>发布视频</button></nav>'
            '<footer><button class="button-dhlUZE primary-cECiOJ fixed-J9O8Yw">发布</button></footer>'
        )
        button = await publisher.find_publish_button(self.page)
        self.assertIsNotNone(button)
        self.assertEqual((await button.inner_text()).strip(), "发布")
        self.assertEqual(await button.evaluate("el => el.closest('nav') === null"), True)

    async def test_navigation_only_publish_is_not_clicked(self) -> None:
        await self.page.set_content('<nav><button>发布视频</button></nav><button>其他</button>')
        self.assertIsNone(await publisher.find_publish_button(self.page))

    def test_success_requires_new_signal_and_item_identity(self) -> None:
        item = {
            "work_title": "开场丨2026.10.06南京",
            "caption": "灯一暗，鼓点一响，火星的夜晚正式开场。",
            "name": "1开场.mp4",
        }
        url = "https://creator.douyin.com/creator-micro/content/post/video"
        # 点击前页面已有“发布成功”，不能算本次成功。
        self.assertFalse(publisher.page_publish_succeeded(
            url,
            "发布成功 开场丨2026.10.06南京",
            url,
            item=item,
            before_body="发布成功 开场丨2026.10.06南京",
        ))
        # 新提示但没有本条身份，仍不能算。
        self.assertFalse(publisher.page_publish_succeeded(
            url,
            "发布成功",
            url,
            item=item,
            before_body="",
        ))
        # 新的成功提示 + 本条标题。
        self.assertTrue(publisher.page_publish_succeeded(
            url,
            "发布成功 开场丨2026.10.06南京",
            url,
            item=item,
            before_body="",
        ))
        # 点击后跳转到内容管理即视为提交成功，不需要等作品卡片/审核。
        self.assertTrue(publisher.page_publish_succeeded(
            "https://creator.douyin.com/creator-micro/content/manage?enter_from=publish",
            "发布成功",
            url,
            item=item,
            before_body="",
        ))


if __name__ == "__main__":
    unittest.main(verbosity=2)
