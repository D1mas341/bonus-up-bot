"""Regression checks for the BONUS UP desktop Mini App layout.

Tests parse the actual HTML embedded in bot.py without starting Telegram,
opening a database, or touching live user / affiliate data.
"""
import ast
from pathlib import Path
import re
import shutil
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "bot.py"


def mini_app_html():
    module = ast.parse(SOURCE.read_text(encoding="utf-8"))
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "MINI_APP_HTML"
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("MINI_APP_HTML not found")


class MiniAppDesktopLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = mini_app_html()

    def test_mobile_baseline_is_preserved(self):
        html = self.html
        self.assertIn(".wrap{max-width:760px", html)
        self.assertIn(".banner{position:relative;width:83%", html)
        self.assertIn("count=bannerDesktopQuery.matches?Math.min(3,items.length):1", html)
        self.assertIn("@media(max-width:899px)", html)

    def test_desktop_banner_gallery_and_grid(self):
        html = self.html
        self.assertIn("@media (min-width:900px)", html)
        self.assertIn(".wrap{max-width:1320px", html)
        self.assertIn("grid-template-columns:repeat(3,minmax(0,1fr))", html)
        self.assertIn("#offers{display:grid", html)
        self.assertIn(".banner-tile", html)
        self.assertIn("renderTile(items[(idx+i)%items.length])", html)

    def test_tracking_and_tonplay_actions_preserved(self):
        html = self.html
        for required in (
            "/go/banner/",
            "/go/dual/",
            "/go/'+Number(x.id)",
            "u.searchParams.set(\"track\",trackingToken)",
            "tonplay-banner-trigger",
            "bannerTimer=setInterval",
            "renderBanners(items)",
            "copyPromo(item.promo)",
        ):
            self.assertIn(required, html)

    def test_fullscreen_only_by_user_action(self):
        html = self.html
        self.assertIn('id="expandMiniApp"', html)
        self.assertIn('desktopExpand.addEventListener("click"', html)
        self.assertIn("tg.requestFullscreen()", html)
        self.assertNotIn("tg.ready();tg.requestFullscreen()", html)

    def test_html_inline_script_syntax(self):
        if not shutil.which("node"):
            self.skipTest("Node.js not installed")
        script = re.search(r"<script>\s*(const tg=[\s\S]*?)</script>", self.html)
        self.assertIsNotNone(script)
        run = subprocess.run(
            ["node", "--check", "-"],
            input=script.group(1),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(run.returncode, 0, run.stderr)


if __name__ == "__main__":
    unittest.main()
