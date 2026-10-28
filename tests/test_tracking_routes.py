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
                   VALUES('casino', 'CI offer', '', '', 'https://example.org/landing?sub={click_id}', 1, 0, 'RU')"""
            )
            con.commit()

        app = web.Application()
        app.router.add_get("/go/{offer_id}", bot.go_offer)
        app.router.add_post("/postback", bot.partner_postback)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_offer_redirect_logs_click_and_passes_click_id(self):
        response = await self.client.get("/go/1?utm_source=ci&utm_campaign=smoke", allow_redirects=False)
        self.assertEqual(response.status, 302)
        location = response.headers["Location"]
        self.assertTrue(location.startswith("https://example.org/landing?sub="), location)
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

    async def test_postback_records_conversion_and_deduplicates_event(self):
        with bot.db() as con:
            click_id = bot.log_click(
                con, offer_id=1, source="ci", target_url="https://example.org/landing"
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


if __name__ == "__main__":
    unittest.main()
