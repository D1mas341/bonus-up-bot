import os
import tempfile
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

# Set required configuration before importing the bot module.
_tmp = tempfile.TemporaryDirectory()
os.environ["BOT_TOKEN"] = "ci-test-token"
os.environ["DB_PATH"] = os.path.join(_tmp.name, "tracking-test.db")
os.environ["POSTBACK_SECRET"] = "ci-postback-secret"

import bot  # noqa: E402


class TrackingRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if os.path.exists(os.environ["DB_PATH"]):
            os.remove(os.environ["DB_PATH"])
        bot.init_db()
        with bot.db() as con:
            con.execute(
                """INSERT INTO offers
                   (category, name, description, bonus, url, enabled, position, geo)
                   VALUES('casino', 'CI offer', '', '', 'https://affiliate.test/landing?sub={click_id}', 1, 0, 'RU')"""
            )
            con.execute(
                """UPDATE offers SET mini_app_url=?, site_url=? WHERE name='CI offer'""",
                ("https://affiliate.test/mini?sub={click_id}", "https://affiliate.test/site?sub={click_id}"),
            )
            con.execute(
                """INSERT INTO banners (title, url, enabled, position, clicks)
                   VALUES ('CI banner', 'https://affiliate.test/banner?sub={click_id}', 1, 0, 0)"""
            )
            con.execute(
                """INSERT INTO campaigns (title, button_url, enabled, position)
                   VALUES ('CI campaign', 'https://affiliate.test/campaign?sub={click_id}', 1, 0)"""
            )
            con.commit()

        app = web.Application()
        app.router.add_get("/go/{offer_id}", bot.go_offer)
        app.router.add_get("/go/dual/{offer_id}/{kind}", bot.go_dual_offer)
        app.router.add_get("/go/banner/{banner_id}", bot.go_banner)
        app.router.add_get("/go/campaign/{campaign_id}", bot.go_campaign)
        app.router.add_post("/postback", bot.partner_postback)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()


    async def test_migration_preserves_legacy_offer_and_click_records(self):
        # Rebuild a disposable SQLite file using the legacy schema from main.
        with bot.db() as con:
            con.execute("DROP TABLE IF EXISTS clicks")
            con.execute("DROP TABLE IF EXISTS offers")
            con.execute("""
                CREATE TABLE offers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    bonus TEXT DEFAULT '',
                    url TEXT NOT NULL,
                    enabled INTEGER DEFAULT 1,
                    position INTEGER DEFAULT 0,
                    geo TEXT DEFAULT ''
                )
            """)
            con.execute("""
                CREATE TABLE clicks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    offer_id INTEGER,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            con.execute(
                "INSERT INTO offers(id,category,name,description,bonus,url,enabled,position,geo) VALUES(41,'casino','Legacy partner','existing description','existing bonus','https://partner.example/keep',1,7,'RU')"
            )
            con.execute("INSERT INTO clicks(id,user_id,offer_id) VALUES(73,12345,41)")
            con.commit()

        bot.init_db()

        with bot.db() as con:
            offer = con.execute(
                "SELECT id,name,description,bonus,url,enabled,position,geo,mini_app_url,site_url FROM offers WHERE id=41"
            ).fetchone()
            click = con.execute(
                "SELECT id,user_id,offer_id,click_id,source,utm_source FROM clicks WHERE id=73"
            ).fetchone()
            offer_columns = {r[1] for r in con.execute("PRAGMA table_info(offers)")}
            click_columns = {r[1] for r in con.execute("PRAGMA table_info(clicks)")}
        self.assertEqual(tuple(offer[:8]), (41,"Legacy partner","existing description","existing bonus","https://partner.example/keep",1,7,"RU"))
        self.assertEqual(tuple(click[:3]), (73,12345,41))
        self.assertIsNone(click["click_id"])
        self.assertTrue({"promo_code","logo_url","hidden","mini_app_url","site_url"} <= offer_columns)
        self.assertTrue({"click_id","source","click_type","partner","banner_id","campaign_id","target_url","utm_source","utm_campaign"} <= click_columns)

    async def test_offer_redirect_logs_click_and_passes_click_id(self):
        response = await self.client.get("/go/1?utm_source=ci&utm_campaign=smoke", allow_redirects=False)
        self.assertEqual(response.status, 302)
        location = response.headers["Location"]
        self.assertTrue(location.startswith("https://affiliate.test/landing?sub="), location)
        click_id = location.split("sub=", 1)[1]
        self.assertTrue(click_id)

        with bot.db() as con:
            row = con.execute(
                "SELECT click_id, source, utm_source, utm_campaign, offer_id FROM clicks WHERE click_id=?",
                (click_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["click_id"], click_id)
        self.assertEqual(row["source"], "ci")
        self.assertEqual(row["utm_source"], "ci")
        self.assertEqual(row["utm_campaign"], "smoke")
        self.assertEqual(row["offer_id"], 1)

    async def test_dual_offer_redirect_tracks_click(self):
        response = await self.client.get("/go/dual/1/mini?utm_source=telegram", allow_redirects=False)
        self.assertEqual(response.status, 302)
        location = response.headers["Location"]
        self.assertTrue(location.startswith("https://affiliate.test/mini?sub="), location)
        click_id = location.split("sub=", 1)[1]
        with bot.db() as con:
            row = con.execute("SELECT click_type, source, offer_id FROM clicks WHERE click_id=?", (click_id,)).fetchone()
        self.assertEqual(tuple(row), ("offer_mini", "telegram", 1))

    async def test_banner_and_campaign_redirects_increment_click_counts(self):
        banner = await self.client.get("/go/banner/1?utm_source=telegram", allow_redirects=False)
        self.assertEqual(banner.status, 302)
        self.assertIn("sub=", banner.headers["Location"])
        campaign = await self.client.get("/go/campaign/1?utm_source=telegram", allow_redirects=False)
        self.assertEqual(campaign.status, 302)
        self.assertIn("sub=", campaign.headers["Location"])
        with bot.db() as con:
            banner_clicks = con.execute("SELECT clicks FROM banners WHERE id=1").fetchone()[0]
            campaign_clicks = con.execute("SELECT click_count FROM campaigns WHERE id=1").fetchone()[0]
            tracked = con.execute("SELECT COUNT(*) FROM clicks WHERE click_type IN ('banner','campaign')").fetchone()[0]
        self.assertEqual(banner_clicks, 1)
        self.assertEqual(campaign_clicks, 1)
        self.assertEqual(tracked, 2)

    async def test_postback_records_conversion_and_deduplicates_event(self):
        with bot.db() as con:
            click_id = bot.log_click(
                con, offer_id=1, source="ci", partner="ci-network",
                target_url="https://affiliate.test/landing"
            )
            con.commit()

        payload = {
            "click_id": click_id,
            "partner": "ci-network",
            "event_id": "event-001",
            "event": "registration",
            "amount": 12.5,
            "currency": "EUR",
        }
        headers = {"X-Postback-Secret": os.environ["POSTBACK_SECRET"]}
        first = await self.client.post("/postback", json=payload, headers=headers)
        self.assertEqual(first.status, 200)
        self.assertEqual(await first.json(), {"ok": True, "recorded": True})

        duplicate = await self.client.post("/postback", json=payload, headers=headers)
        self.assertEqual(duplicate.status, 200)
        self.assertEqual(await duplicate.json(), {"ok": True, "duplicate": True})

        with bot.db() as con:
            count = con.execute(
                "SELECT COUNT(*) FROM conversion_events WHERE partner=? AND event_id=?",
                ("ci-network", "event-001"),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    async def test_postback_rejects_wrong_secret(self):
        response = await self.client.post(
            "/postback",
            json={"click_id": "fake", "partner": "ci", "event_id": "event-002"},
            headers={"X-Postback-Secret": "wrong"},
        )
        self.assertEqual(response.status, 401)

    async def test_postback_rejects_unknown_click_id(self):
        response = await self.client.post(
            "/postback",
            json={"click_id": "unknown-click", "partner": "ci", "event_id": "event-003"},
            headers={"X-Postback-Secret": os.environ["POSTBACK_SECRET"]},
        )
        self.assertEqual(response.status, 404)

    async def test_postback_rejects_partner_mismatch_for_known_click(self):
        with bot.db() as con:
            click_id = bot.log_click(
                con, offer_id=1, source="ci", partner="network-a",
                target_url="https://affiliate.test/landing"
            )
            con.commit()

        response = await self.client.post(
            "/postback",
            json={"click_id": click_id, "partner": "network-b", "event_id": "event-wrong-partner"},
            headers={"X-Postback-Secret": os.environ["POSTBACK_SECRET"]},
        )
        self.assertEqual(response.status, 403)
        with bot.db() as con:
            count = con.execute(
                "SELECT COUNT(*) FROM conversion_events WHERE event_id=?",
                ("event-wrong-partner",),
            ).fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()

# CI trigger: rerun branch checks after fixing the workflow base-branch filter.
