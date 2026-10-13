const assert = require("node:assert/strict");
const fs = require("node:fs");
const { JSDOM, VirtualConsole } = require("jsdom");
const { execFileSync } = require("node:child_process");

const html = execFileSync("python", ["-c", [
  "import ast, pathlib",
  "tree=ast.parse(pathlib.Path('bot.py').read_text(encoding='utf-8'))",
  "node=next(n for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id=='MINI_APP_HTML' for t in n.targets))",
  "print(ast.literal_eval(node.value))"
].join(";")], { encoding: "utf8", maxBuffer: 2 * 1024 * 1024 });

const offers = [
  { id: 101, category: "casino", name: "Casino Test", description: "Test description", bonus: "100 FS", url: "https://partner.test/offer", promo: "GOLD100", logo_url: "" },
  { id: 102, category: "casino", name: "Second Casino", description: "Second description", bonus: "50%", url: "https://partner.test/second", promo: "", logo_url: "" },
  { id: 103, category: "bets", name: "Sports Test", description: "Sports description", bonus: "50%", url: "https://partner.test/sports", promo: "SPORT50", logo_url: "" }
];

(async () => {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", e => errors.push(e.message));
  let copiedText = null;
  const popups = [];
  const dom = new JSDOM(html, {
    url: "https://bonus-up-test.local/app",
    runScripts: "dangerously",
    pretendToBeVisual: true,
    virtualConsole: vc,
    beforeParse(window) {
      window.fetch = async (url) => ({
        ok: true,
        json: async () => String(url).includes("/api/banners") ? [] : offers
      });
      Object.defineProperty(window.navigator, "clipboard", {
        configurable: true,
        value: { writeText: async value => { copiedText = value; } }
      });
      window.Telegram = { WebApp: {
        ready() {}, expand() {},
        showPopup: options => popups.push(options),
        showAlert: message => popups.push({ message })
      }};
    }
  });

  try {
    await new Promise(resolve => setTimeout(resolve, 30));
    const { document, Event, MouseEvent, KeyboardEvent } = dom.window;

    assert.equal(document.querySelectorAll(".card").length, 2, "casino category should render two cards");
    const firstCard = [...document.querySelectorAll(".card")].find(x => x.textContent.includes("Casino Test"));
    assert.ok(firstCard, "first casino card should be visible");
    firstCard.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    assert.ok(document.getElementById("modalBackdrop").classList.contains("open"), "clicking a card opens modal");
    assert.match(document.getElementById("modalContent").textContent, /GOLD100/, "modal shows promo code");
    assert.match(document.getElementById("modalContent").textContent, /PLAY/, "modal shows partner CTA");
    assert.equal(document.querySelector('#modalContent a.play').getAttribute("href"), "/go/101", "CTA uses tracked redirect");

    const language = document.getElementById("languageSelect");
    language.value = "de";
    language.dispatchEvent(new Event("change", { bubbles: true }));
    assert.equal(document.documentElement.lang, "de", "language switch updates document language");
    assert.match(document.getElementById("modalContent").textContent, /SPIELEN/, "open modal is translated after language switch");
    assert.match(document.getElementById("sectionTitle").textContent, /Spiele/, "category heading is translated");

    document.getElementById("modalCopy").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.equal(copiedText, "GOLD100", "copy button sends promo to clipboard");
    assert.ok(popups.some(p => /kopiert/i.test(p.message || p.message || "" ) || /kopiert/i.test(p.title || "")), "copy action gives user feedback");

    document.getElementById("closeModal").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    assert.ok(!document.getElementById("modalBackdrop").classList.contains("open"), "close button closes modal");

    const sportTab = [...document.querySelectorAll(".tab")].find(x => x.dataset.cat === "bets");
    sportTab.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    assert.equal(document.querySelectorAll(".card").length, 1, "sports tab shows sports offers");
    assert.match(document.getElementById("sectionTitle").textContent, /Sport/, "sports category label is translated");

    const partnerAnchor = document.querySelector('.card a.arrow[href="/go/103"]');
    assert.ok(partnerAnchor, "partner link is rendered as tracked /go route");
    assert.equal(partnerAnchor.getAttribute("target"), "_blank", "partner link opens separately");
    assert.deepEqual(errors, [], "no uncaught JSDOM errors");

    console.log("PASS: card rendering, modal open/close, language switch, promo copy, category switch, tracked partner links");
  } finally {
    dom.window.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
