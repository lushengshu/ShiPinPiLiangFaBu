"""Isolated browser regression: never connects to Douyin or uploads media."""
import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import browser_publisher as publisher
from playwright.async_api import async_playwright


class TitleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(channel='chrome', headless=True)
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def test_title_separate_from_description(self):
        await self.page.set_content('<input placeholder="填写作品标题"><textarea placeholder="添加作品描述"></textarea>')
        await publisher.configure_title_and_caption(self.page, {'work_title': '独立标题', 'caption': '正文'})
        self.assertEqual(await self.page.locator('input').input_value(), '独立标题')
        self.assertEqual(await self.page.locator('textarea').input_value(), '正文')

    async def test_title_textarea_does_not_capture_description(self):
        await self.page.set_content('<textarea placeholder="作品标题"></textarea><div contenteditable="true" data-placeholder="描述"></div>')
        await publisher.configure_title_and_caption(self.page, {'work_title': '标题', 'caption': '描述'})
        self.assertEqual(await self.page.locator('textarea').input_value(), '标题')
        self.assertEqual(await self.page.locator('div').inner_text(), '描述')

    async def test_missing_title_pauses_without_merging(self):
        await self.page.set_content('<textarea placeholder="描述">原文</textarea>')
        with self.assertRaises(publisher.ManualActionRequired):
            await publisher.configure_title_and_caption(self.page, {'work_title': '标题', 'caption': '描述'})
        self.assertEqual(await self.page.locator('textarea').input_value(), '原文')

    async def test_empty_title_clears_stale_draft(self):
        await self.page.set_content('<input placeholder="标题" value="旧标题"><textarea></textarea>')
        await publisher.configure_title_and_caption(self.page, {'work_title': '', 'caption': '新描述'})
        self.assertEqual(await self.page.locator('input').input_value(), '')
        self.assertEqual(await self.page.locator('textarea').input_value(), '新描述')

    async def test_optional_missing_title(self):
        await self.page.set_content('<textarea></textarea>')
        await publisher.configure_title_and_caption(self.page, {'caption': '描述'})
        self.assertEqual(await self.page.locator('textarea').input_value(), '描述')

    async def test_component_rejects_title(self):
        await self.page.set_content('<input placeholder="标题" onblur="this.value=\'\'"><textarea></textarea>')
        with self.assertRaises(publisher.ManualActionRequired):
            await publisher.configure_title_and_caption(self.page, {'work_title': '被清空', 'caption': '描述'})

    async def test_topics_are_not_plain_text_when_no_panel(self):
        await self.page.set_content('<textarea placeholder="添加作品描述"></textarea>')
        with self.assertRaises(publisher.ManualActionRequired):
            await publisher.configure_title_and_caption(
                self.page,
                {'caption': '正文', 'hashtags': '现场'},
            )
        self.assertNotIn('#现场', await self.page.locator('textarea').input_value())

    def test_all_publish_paths_use_shared_configuration(self):
        tree = ast.parse(Path(publisher.__file__).read_text())
        for name in ['upload_one', 'upload_panorama_one', 'resume_current_draft']:
            node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
            self.assertTrue(any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'configure_title_and_caption' for n in ast.walk(node)), name)


if __name__ == '__main__':
    unittest.main()
