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

    def test_desktop_auto_request_and_mobile_suppression(self):
        html = self.html
        self.assertIn('id="expandMiniApp"', html)
        self.assertIn('desktopExpand.addEventListener("click"', html)
        self.assertIn('window.requestAnimationFrame(()=>requestDesktopFullscreen(false))', html)
        self.assertIn('if(!tg)return false', html)
        self.assertIn('platform==="tdesktop"||platform==="macos"', html)
        self.assertIn('@media(max-width:899px) and (pointer:coarse)', html)
        self.assertIn('tg.isFullscreen', html)

        if not shutil.which("node"):
            self.skipTest("Node.js not installed")
        match = re.search(
            r"<script>\s*(const tg=[\s\S]*?)const translations=",
            html,
        )
        self.assertIsNotNone(match)
        harness = r"""
const assert = require("assert");
const vm = require("vm");
const source = require("fs").readFileSync(0,"utf8");
const cases = [
 {platform:"tdesktop",width:500,coarse:false,expected:1,visible:true},
 {platform:"macos",width:650,coarse:false,expected:1,visible:true},
 {platform:"web",width:1400,coarse:false,expected:1,visible:true},
 {platform:"weba",width:390,coarse:true,expected:0,visible:false},
 {platform:"webk",width:390,coarse:true,expected:0,visible:false},
 {platform:"android",width:390,coarse:true,expected:0,visible:false},
 {platform:"ios",width:390,coarse:true,expected:0,visible:false},
 {platform:"web",width:450,coarse:false,expected:0,visible:false},
];
for (const c of cases){
 let calls=0, ready=0, expand=0;
 const callbacks={};
 const button={hidden:true,addEventListener:(ev,cb)=>{callbacks[ev]=cb}};
 const tg={
  platform:c.platform,initData:"verified-launch",isFullscreen:false,
  ready:()=>ready++,expand:()=>expand++,
  isVersionAtLeast:v=>true,
  requestFullscreen:()=>calls++,showAlert:()=>{}
 };
 const window={
  Telegram:{WebApp:tg},
  screen:{width:c.width},
  innerWidth:c.width,
  matchMedia:()=>({matches:c.coarse}),
  requestAnimationFrame:cb=>cb()
 };
 const document={getElementById:()=>button};
 vm.runInNewContext(source,{window,document}, {timeout:1000});
 assert.equal(ready,1,c.platform+" ready");
 assert.equal(expand,1,c.platform+" expand");
 assert.equal(calls,c.expected,c.platform+" auto fullscreen");
 assert.equal(button.hidden,!c.visible,c.platform+" fallback button");
 if(c.visible){callbacks.click();assert.equal(calls,c.expected+1,c.platform+" manual fallback");}
}
"""
        run = subprocess.run(
            ["node", "-e", harness], input=match.group(1),
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(run.returncode, 0, run.stderr)

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
