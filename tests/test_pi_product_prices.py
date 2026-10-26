"""Optional PI valuation prices are public market data and use the shared price cache."""
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, ".")
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import planetary


class ProductPriceTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(planetary.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.sde = patch.object(planetary, "load_pi_data", return_value={"types": {
            100: {"name": "Water", "pi_tier": 1},
            200: {"name": "Coolant", "pi_tier": 2},
            300: {"name": "Aqueous Liquids", "pi_tier": 0},
        }})
        self.sde.start()
        self.addCleanup(self.sde.stop)

    def test_normal_product_picker_does_not_fetch_market_data(self):
        with patch("app.market.fetch_prices") as prices:
            response = self.client.get("/api/pi-products")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["products"], [
            {"type_id": 100, "name": "Water", "tier": 1},
            {"type_id": 200, "name": "Coolant", "tier": 2},
        ])
        prices.assert_not_called()

    def test_optional_prices_fetch_one_batch_and_preserve_missing_prices(self):
        with patch("app.market.fetch_prices", return_value={100: 123.45}) as prices:
            response = self.client.get("/api/pi-products?include_prices=true")
        self.assertEqual(response.status_code, 200)
        products = response.json()["products"]
        prices.assert_called_once_with([100, 200])
        self.assertEqual(products[0]["sell_price"], 123.45)
        self.assertIsNone(products[1]["sell_price"], "missing prices must not look like zero-value goods")
        self.assertEqual(set(products[0]), {"type_id", "name", "tier", "sell_price"})


if __name__ == "__main__":
    unittest.main()
