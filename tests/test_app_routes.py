import os
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["BOT_TOKEN"] = "123456789:TEST_TOKEN_NOT_REAL"
os.environ["DB_PATH"] = str(Path(TEMP_DIR.name) / "test_bonus_up.db")

import bot  # noqa: E402


class MiniAppRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        bot.init_db()
        self.client = TestClient(TestServer(self._make_app()))
        await self.client.start_server()
        with bot.db() as con:
            con.execute("DELETE FROM clicks")
            con.execute("DELETE FROM offers")
            con.execute("DELETE FROM banners")
            con.execute(
                "INSERT INTO offers(category,name,description,bonus,url,enabled,position,geo,promo_code,logo_url) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("casino", "Test Casino", "Test description", "100%", "https://partner.example.org", 1, 1, "RU", "TEST", ""),
            )
            con.execute(
                "INSERT INTO offers(category,name,description,bonus,url,enabled,position,geo,promo_code,logo_url) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("bets", "Real Partner", "Description", "50%", "https://partner.test", 1, 2, "KZ", "", ""),
            )
            con.execute(
                "INSERT INTO offers(category,name,description,bonus,url,enabled,position,geo,promo_code,logo_url) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("casino", "Disabled", "", "", "https://partner.test/disabled", 0, 3, "RU", "", ""),
            )
            con.execute(
                "INSERT INTO banners(title,text,image_url,button_text,url,enabled,position,clicks) "
                "VALUES(?,?,?,?,?,?,?,?)",
                ("Test banner", "Text", "", "Open", "https://banner.test", 1, 1, 0),
            )
            con.commit()

    async def asyncTearDown(self):
        await self.client.close()

    @staticmethod
    def _id_for_offer(name):
        with bot.db() as con:
            return con.execute("SELECT id FROM offers WHERE name=?", (name,)).fetchone()[0]

    @staticmethod
    def _id_for_banner(title):
        with bot.db() as con:
            return con.execute("SELECT id FROM banners WHERE title=?", (title,)).fetchone()[0]

    @staticmethod
    def _make_app():
        app = bot.web.Application()
        app.router.add_get("/app", bot.mini_app)
        app.router.add_get("/api/offers", bot.api_offers)
        app.router.add_get("/api/banners", bot.api_banners)
        app.router.add_get("/go/{offer_id}", bot.go_offer)
        app.router.add_get("/go/banner/{banner_id}", bot.go_banner)
        app.router.add_get("/health", bot.health)
        return app

    async def test_health(self):
        response = await self.client.get("/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.text(), "ok")

    async def test_public_offers_hide_geo_and_disabled_items(self):
        response = await self.client.get("/api/offers")
        self.assertEqual(response.status, 200)
        offers = await response.json()
        self.assertEqual([x["name"] for x in offers], ["Real Partner", "Test Casino"])
        self.assertTrue(all("geo" not in x for x in offers))
        self.assertTrue(all("url" in x and "promo" in x for x in offers))

    async def test_offer_redirect_and_click_count(self):
        offer_id = self._id_for_offer("Real Partner")
        response = await self.client.get(f"/go/{offer_id}", allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["Location"], "https://partner.test")
        with bot.db() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM clicks WHERE offer_id=?", (offer_id,)).fetchone()[0], 1)

    async def test_example_partner_url_is_rejected(self):
        offer_id = self._id_for_offer("Test Casino")
        response = await self.client.get(f"/go/{offer_id}", allow_redirects=False)
        self.assertEqual(response.status, 404)
        with bot.db() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM clicks WHERE offer_id=?", (offer_id,)).fetchone()[0], 0)

    async def test_disabled_offer_is_not_redirected(self):
        offer_id = self._id_for_offer("Disabled")
        response = await self.client.get(f"/go/{offer_id}", allow_redirects=False)
        self.assertEqual(response.status, 404)

    async def test_banner_redirect_and_count(self):
        banner_id = self._id_for_banner("Test banner")
        response = await self.client.get(f"/go/banner/{banner_id}", allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["Location"], "https://banner.test")
        with bot.db() as con:
            self.assertEqual(con.execute("SELECT clicks FROM banners WHERE id=?", (banner_id,)).fetchone()[0], 1)

    async def test_mini_app_route_serves_html(self):
        response = await self.client.get("/app")
        self.assertEqual(response.status, 200)
        html = await response.text()
        self.assertIn("BONUS UP", html)
        self.assertIn("function openModal(id)", html)
        self.assertIn('id="languageSelect"', html)


if __name__ == "__main__":
    unittest.main()
