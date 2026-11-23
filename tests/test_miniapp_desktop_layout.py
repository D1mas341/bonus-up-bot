"""Regression checks for the BONUS UP desktop Mini App layout.

Tests parse the actual HTML embedded in bot.py without starting Telegram,
opening a database, or touching live user / affiliate data.
"""
import ast
import json
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
        # The banner gallery stays three-wide, while desktop offers stay two-wide.
        self.assertIn("#promoBanner.banner{width:100%;display:grid;grid-template-columns:repeat(3,minmax(0,1fr))", html)
        self.assertIn("#offers{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))", html)
        self.assertNotIn("#offers{grid-template-columns:repeat(3,minmax(0,1fr))", html)
        self.assertIn("#offers .icon{width:64px;height:64px", html)
        self.assertIn("#offers .cardbottom{padding:14px 22px", html)
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

    def test_desktop_fullscreen_toggle_and_mobile_suppression(self):
        html = self.html
        self.assertIn('id="expandMiniApp"', html)
        self.assertIn('desktopExpand.addEventListener("click"', html)
        self.assertIn('window.requestAnimationFrame(()=>requestDesktopFullscreen(false))', html)
        self.assertIn('platform==="tdesktop"||platform==="macos"', html)
        self.assertIn('@media(max-width:899px) and (pointer:coarse)', html)
        self.assertIn('tg.onEvent("fullscreenChanged"', html)
        self.assertIn('tg.onEvent("fullscreenFailed"', html)
        self.assertIn('tg.exitFullscreen()', html)
        self.assertIn('.tg-desktop .menu-wrap{display:none!important}', html)
        self.assertIn('class="lang-select" id="languageSelect"', html)

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
 let requests=0, exits=0, ready=0, expand=0;
 const callbacks={}, events={}, alerts=[];
 const classes=[];
 const button={
   hidden:true,
   addEventListener:(ev,cb)=>{callbacks[ev]=cb},
   setAttribute:(key,value)=>{button[key]=value}
 };
 const tg={
  platform:c.platform,initData:"verified-launch",isFullscreen:false,
  ready:()=>ready++,expand:()=>expand++,
  isVersionAtLeast:()=>true,
  requestFullscreen:()=>requests++,
  exitFullscreen:()=>exits++,
  showAlert:(message)=>alerts.push(message),
  onEvent:(name,cb)=>events[name]=cb
 };
 const window={
  Telegram:{WebApp:tg},
  screen:{width:c.width},
  innerWidth:c.width,
  matchMedia:()=>({matches:c.coarse}),
  requestAnimationFrame:cb=>cb()
 };
 const document={
   documentElement:{classList:{add:(name)=>classes.push(name)}},
   getElementById:()=>button
 };
 vm.runInNewContext(source,{window,document},{timeout:1000});
 assert.equal(ready,1,c.platform+" ready");
 assert.equal(expand,1,c.platform+" expand");
 assert.equal(requests,c.expected,c.platform+" auto fullscreen");
 assert.equal(button.hidden,!c.visible,c.platform+" fallback button");
 if(c.visible){
  assert.equal(button.textContent,"⛶",c.platform+" initial button label");
  assert(classes.includes("tg-desktop"));
  callbacks.click();
  assert.equal(requests,c.expected+1,c.platform+" manual expand");
  tg.isFullscreen=true;
  events.fullscreenChanged();
  assert.equal(button.textContent,"↙",c.platform+" full button label");
  callbacks.click();
  assert.equal(exits,1,c.platform+" click must EXIT when fullscreen");
  tg.isFullscreen=false;
  events.fullscreenChanged();
  assert.equal(button.textContent,"⛶",c.platform+" restored button label");
  callbacks.click();
  events.fullscreenFailed({error:"UNSUPPORTED"});
  assert(alerts.some(s=>s.includes("не поддерживается")),c.platform+" failure feedback");
 }else{
  assert(!classes.includes("tg-desktop"));
 }
}
{
 const alerts=[],callbacks={},events={};
 const button={hidden:true,addEventListener:(e,cb)=>callbacks[e]=cb,setAttribute:()=>{}};
 const tg={
  platform:"tdesktop",initData:"verified-launch",isFullscreen:false,
  ready:()=>{},expand:()=>{},showAlert:m=>alerts.push(m),
  isVersionAtLeast:()=>true,onEvent:(n,cb)=>events[n]=cb
 };
 const window={Telegram:{WebApp:tg},requestAnimationFrame:cb=>cb()};
 const document={getElementById:()=>button,documentElement:{classList:{add:()=>{}}}};
 vm.runInNewContext(source,{window,document},{timeout:1000});
 callbacks.click();
 assert(alerts.length>0,"unsupported version must explain why fullscreen is unavailable");
}
"""
        run = subprocess.run(
            ["node", "-e", harness], input=match.group(1),
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_desktop_language_popover(self):
        html = self.html
        self.assertIn('id="desktopLanguageButton"', html)
        self.assertIn('id="desktopLanguageMenu" role="menu"', html)
        self.assertIn('.tg-desktop .lang-select{display:none}', html)
        self.assertIn('.tg-desktop .desktop-language{display:block}', html)
        self.assertIn('desktop-language-popover[hidden]', html)
        for code in ("ru", "en", "de", "tr", "hi"):
            self.assertIn(f'data-lang-option="{code}"', html)
            self.assertRegex(html, rf'data-lang-option="{code}"[^>]*><span[^>]*><svg ')
        self.assertIn('Sprache wählen', html)
        self.assertIn('desktopLanguageFlag").innerHTML=desktopLanguageFlags[current]', html)
        self.assertIn('desktop-language .lang-circle svg', html)
        self.assertIn('<svg xmlns="http://www.w3.org/2000/svg"', html)
        for color in ("#012169", "#0039A6", "#DD0000", "#E30A17", "#138808"):
            self.assertIn(color, html)
        self.assertNotIn('id="desktopLanguageFlag" aria-hidden="true">🇷🇺', html)

        if not shutil.which("node"):
            self.skipTest("Node.js not installed")
        match = re.search(
            r"(const desktopLanguageControl=[\s\S]*?)const menuButton=",
            html,
        )
        self.assertIsNotNone(match)
        harness = r"""
const assert=require("assert");
const vm=require("vm");
const source=require("fs").readFileSync(0,"utf8");
let document;
const makeOption=code=>({
  dataset:{langOption:code},attributes:{},listeners:{},
  setAttribute(k,v){this.attributes[k]=v},
  addEventListener(k,cb){this.listeners[k]=cb},
  focus(){document.activeElement=this}
});
const codes=["en","ru","de","tr","hi"];
const options=codes.map(makeOption);
const menu={
 hidden:true,listeners:{},
 querySelectorAll:()=>options,
 querySelector:()=>options.find(o=>o.attributes["aria-checked"]==="true"),
 addEventListener(k,cb){this.listeners[k]=cb}
};
let triggerFocuses=0;
const trigger={
 attributes:{},listeners:{},
 setAttribute(k,v){this.attributes[k]=v},
 addEventListener(k,cb){this.listeners[k]=cb},
 focus(){triggerFocuses++;document.activeElement=this}
};
const control={contains:el=>el===trigger||el===menu||options.includes(el)};
const flag={innerHTML:""};
const title={textContent:""};
const nativeSelect={value:"ru",listeners:{},addEventListener(k,cb){this.listeners[k]=cb}};
const elements={
 desktopLanguageControl:control,desktopLanguageButton:trigger,
 desktopLanguageMenu:menu,desktopLanguageFlag:flag,
 desktopLanguageTitle:title,languageSelect:nativeSelect
};
document={
 activeElement:null,listeners:{},
 getElementById:id=>elements[id],
 addEventListener(k,cb){this.listeners[k]=cb}
};
const stored={};
const context=vm.createContext({
 document,language:"ru",translations:{ru:{},en:{},de:{},tr:{},hi:{}},
 localStorage:{setItem:(k,v)=>stored[k]=v},
});
context.applyLanguage=()=>{
 nativeSelect.value=vm.runInContext("language",context);
 vm.runInContext("syncDesktopLanguage()",context);
};
vm.runInContext(source,context,{timeout:1000});
vm.runInContext("syncDesktopLanguage()",context);
assert(flag.innerHTML.includes("<svg"),"Russian flag must be real SVG");
assert(flag.innerHTML.includes("#D52B1E"),"Russian flag red stripe");
assert.equal(title.textContent,"Выберите язык");
assert.equal(options.find(o=>o.dataset.langOption==="ru").attributes["aria-checked"],"true");
trigger.listeners.click();
assert.equal(menu.hidden,false,"picker should open");
assert.equal(trigger.attributes["aria-expanded"],"true");
options.find(o=>o.dataset.langOption==="de").listeners.click();
assert.equal(vm.runInContext("language",context),"de");
assert.equal(stored.bonusUpLanguage,"de");
assert(flag.innerHTML.includes("#FFCE00"),"German flag gold stripe");
assert(flag.innerHTML.includes("#DD0000"),"German flag red stripe");
assert.equal(title.textContent,"Sprache wählen");
assert.equal(nativeSelect.value,"de");
assert.equal(menu.hidden,true);
assert.equal(trigger.attributes["aria-expanded"],"false");
assert.equal(options.find(o=>o.dataset.langOption==="de").attributes["aria-checked"],"true");
trigger.listeners.click();
document.listeners.pointerdown({target:{}});
assert(menu.hidden,"outside tap should close popup");
trigger.listeners.click();
document.listeners.keydown({key:"Escape"});
assert(menu.hidden,"Escape should close popup");
const beforeFocus=triggerFocuses;
nativeSelect.listeners.change({target:{value:"en"}});
assert.equal(vm.runInContext("language",context),"en");
assert(flag.innerHTML.includes("#012169"),"British flag blue field");
assert(flag.innerHTML.includes("#C8102E"),"British flag red cross");
assert.equal(triggerFocuses,beforeFocus,"mobile language selection must not focus hidden desktop button");
"""
        run = subprocess.run(
            ["node", "-e", harness],
            input=match.group(1), capture_output=True, text=True, check=False,
        )
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_all_interface_taglines_and_notice_are_localized(self):
        html = self.html
        for key in ("subtitle", "footerLine1", "footerLine2", "riskNote"):
            self.assertIn(f'data-i18n="{key}"', html)
        self.assertRegex(html, r'<text[^>]*data-i18n="subtitle"')
        self.assertRegex(html, r'<text[^>]*data-i18n="footerLine1"')
        self.assertRegex(html, r'<text[^>]*data-i18n="footerLine2"')
        self.assertIn('data-i18n-aria-label="footerAria"', html)
        self.assertIn('el.setAttribute("aria-label",t(el.dataset.i18nAriaLabel))', html)
        self.assertIn('el.setAttribute("href","/terms?lang="+language)', html)
        self.assertIn('el.setAttribute("href","/privacy?lang="+language)', html)
        match = re.search(r"const extraTranslations=(\{[^\n]+\});", html)
        self.assertIsNotNone(match)
        translated = json.loads(match.group(1))
        self.assertEqual(set(translated), {"ru", "en", "de", "tr", "hi"})
        for locale, entries in translated.items():
            for key in ("footerLine1", "footerLine2", "riskNote", "footerAria"):
                self.assertTrue(entries[key].strip(), (locale, key))
            if locale != "ru":
                self.assertNotEqual(entries["footerLine1"], translated["ru"]["footerLine1"])
                self.assertNotEqual(entries["riskNote"], translated["ru"]["riskNote"])
        # Static promo/banner artwork is not translated; only interface copy is.
        self.assertIn('Object.assign(translations[code],extraTranslations[code])', html)

    def test_terms_and_privacy_render_in_all_five_languages(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        assignment = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "LEGAL_PAGE_TRANSLATIONS"
                    for t in node.targets)
        )
        pages = ast.literal_eval(assignment.value)
        self.assertEqual(set(pages), {"terms", "privacy"})
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "legal_page_html"
        )
        module = ast.Module(body=[assignment, function], type_ignores=[])
        namespace = {}
        exec(compile(module, str(SOURCE), "exec"), namespace)
        render = namespace["legal_page_html"]
        for page in ("terms", "privacy"):
            self.assertEqual(set(pages[page]), {"ru", "en", "de", "tr", "hi"})
            for language, texts in pages[page].items():
                self.assertEqual(len(texts), 4, (page, language))
                rendered = render(page, language)
                self.assertIn(f'<html lang=\'{language}\'>', rendered)
                for text in texts:
                    self.assertIn(text, rendered)
                self.assertIn("← BONUS UP", rendered)
                self.assertNotIn("<script src=", rendered)
            self.assertEqual(render(page, "unknown"), render(page, "ru"))

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
