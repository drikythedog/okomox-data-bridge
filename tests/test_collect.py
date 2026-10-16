"""Tests hors ligne de collect.py (aucun accès réseau) : python -m unittest discover -s tests"""
import importlib.util, json, os, sys, unittest
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("collect", ROOT / "collect.py")
c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c)

def om_block(code=61, temp=12.4, gust=40):
    return {"current": {"temperature_2m": temp, "precipitation": 0.5, "weather_code": code, "wind_gusts_10m": gust},
            "hourly": {"time": ["2026-10-16T10:00", "2026-10-16T11:00"], "temperature_2m": [12, None],
                       "precipitation": [0.4, 0], "weather_code": [61, 3], "precipitation_probability": [70, 20]}}

def fake_json(url, headers=None):
    if "open-meteo" in url:
        n = url.split("latitude=")[1].split("&")[0].count(",") + 1
        return [om_block() for _ in range(n)]
    if "meteofrance" in url:
        return {"product": {"periods": [
            {"echeance": "J", "timelaps": {"domain_ids": [
                {"domain_id": "13", "max_color_id": 3, "phenomenon_items": [{"phenomenon_id": "3", "phenomenon_max_color_id": 3}]},
                {"domain_id": "75", "max_color_id": 1, "phenomenon_items": []},
                {"domain_id": "3010", "max_color_id": 2, "phenomenon_items": []}]}},
            {"echeance": "J1", "timelaps": {"domain_ids": []}}]}}
    return None

INDEX = '<html><a href="?C=N">x</a><a href="../">..</a><a href="DIRIF/">DIRIF/</a><a href="lyon.xml">lyon.xml</a></html>'
SUB = '<a href="../">..</a><a href="trafficolor_paris.xml">t</a>'
NS = 'xmlns="http://datex2.eu/schema/2/2_0"'
def datex(n_free, n_heavy, n_cong, name):
    recs = "".join(f"<siteMeasurements><measurementSiteReference>{name}-{i}</measurementSiteReference>"
                   f"<trafficStatus><trafficStatusValue>{v}</trafficStatusValue></trafficStatus></siteMeasurements>"
                   for i, v in enumerate(["freeFlow"]*n_free + ["heavy"]*n_heavy + ["congested"]*n_cong))
    return f'<d2LogicalModel {NS}><payloadPublication><publicationTime>2026-10-16T08:00:00Z</publicationTime>{recs}</payloadPublication></d2LogicalModel>'

def fake_text(url):
    if url.endswith("TRAFICOLOR-DIR/"):
        return INDEX if url.startswith("https") else None
    if url.endswith("DIRIF/"):
        return SUB
    if url.endswith("trafficolor_paris.xml"):
        return datex(2, 3, 5, "Paris")          # 80 % ralenti+ → BLOQUÉ (50 % saturé)
    if url.endswith("lyon.xml"):
        return datex(19, 1, 0, "Lyon")           # 5 % → FLUIDE
    return None

class T(unittest.TestCase):
    def setUp(self):
        os.environ.pop("OPEN_METEO_API_KEY", None); os.environ["METEOFRANCE_API_KEY"] = "k"
        self.p = c.build_payload(fake_json, fake_text, now=datetime(2026, 10, 16, 8, 5, tzinfo=timezone.utc))

    def test_historic_cities_kept(self):
        for n in ["Paris","Lyon","Marseille","Toulouse","Bordeaux","Nice","Nantes","Strasbourg","Lille",
                  "Montpellier","Versailles","Cergy","Evry","Creteil"]:
            self.assertIn(n, self.p["cities"])
        self.assertGreaterEqual(len(self.p["cities"]), 45)

    def test_weather_batch(self):
        paris = self.p["cities"]["Paris"]
        self.assertEqual(paris["weather"], "LIGHT_RAIN"); self.assertEqual(paris["temperatureC"], 12)
        self.assertEqual(paris["windGustKmh"], 40); self.assertEqual(len(paris["hourly"]), 1)  # heure sans temp ignorée
        self.assertEqual(paris["precipProbabilityNext3h"], 70)

    def test_weather_unavailable_flagged(self):
        p = c.build_payload(lambda u, h=None: None, lambda u: None)
        self.assertTrue(all(v["weatherUnavailable"] for v in p["cities"].values()))
        self.assertFalse(any("traffic" in v for v in p["cities"].values()))

    def test_vigilance(self):
        self.assertEqual(self.p["cities"]["Marseille"]["vigilance"]["color"], "orange")
        self.assertEqual(self.p["regions"]["Provence-Alpes-Côte d'Azur"]["vigilanceMax"], "orange")
        self.assertEqual(self.p["national"]["vigilanceOrangeRouge"][0]["dept"], "13")
        self.assertNotIn("3010", json.dumps(self.p["national"]))

    def test_traficolor_measured(self):
        paris, lyon = self.p["cities"]["Paris"], self.p["cities"]["Lyon"]
        self.assertEqual(paris["traffic"], "BLOCKED"); self.assertEqual(paris["trafficDetail"]["mesures"], 10)
        self.assertEqual(self.p["cities"]["Orly"]["traffic"], "BLOCKED")
        self.assertEqual(lyon["traffic"], "FLUID")
        self.assertIn("trafficAt", lyon)
        self.assertNotIn("traffic", self.p["cities"]["Nice"])

    def test_shares(self):
        self.assertEqual(c.traffic_from_shares(Counter(fluide=4))[0], None)
        self.assertEqual(c.traffic_from_shares(Counter(fluide=7, ralenti=3))[0], "DENSE")
        self.assertEqual(c.traffic_from_shares(Counter(fluide=9, ralenti=1))[0], "NORMAL")

    def test_regions_national(self):
        self.assertEqual(len(self.p["regions"]), 13)
        self.assertEqual(self.p["national"]["trafficMeasuredCities"], 11)

    def test_events_attribution(self):
        ev = [{"hw": "A6", "status": "DENSE", "text": c._fold("A6 bouchon vers Évry")}]
        out = c.events_traffic(ev, None)
        self.assertEqual(out.get("Evry"), "DENSE"); self.assertNotIn("Lyon", out)

if __name__ == "__main__":
    unittest.main()
