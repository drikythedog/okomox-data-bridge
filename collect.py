"""
okomox-data-bridge — collecte météo et trafic pour l'app Okomox.

Génère data/data.json avec les conditions actuelles dans 14 grandes villes FR
+ état autoroutes Bison Futé + IDF Sytadin.

Format de sortie aligné sur les enums Kotlin de l'app Okomox :
- WeatherCondition : DRY / LIGHT_RAIN / HEAVY_RAIN / EXTREME
- TrafficCondition : FLUID / NORMAL / DENSE / BLOCKED

Exécuté par .github/workflows/cron-collect.yml toutes les 30 minutes.
Aucune clé API requise (Open-Meteo + Bison Futé RSS + Sytadin sont tous publics).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

# ============================================================================
# CONFIGURATION
# ============================================================================

# 14 grandes villes FR (top 10 + IDF étendu) — coordonnées vérifiées.
CITIES: dict[str, tuple[float, float]] = {
    # Top 10 nationales
    "Paris":       (48.8566, 2.3522),
    "Lyon":        (45.7640, 4.8357),
    "Marseille":   (43.2965, 5.3698),
    "Toulouse":    (43.6047, 1.4442),
    "Bordeaux":    (44.8378, -0.5792),
    "Nice":        (43.7102, 7.2620),
    "Nantes":      (47.2184, -1.5536),
    "Strasbourg":  (48.5734, 7.7521),
    "Lille":       (50.6292, 3.0573),
    "Montpellier": (43.6108, 3.8767),
    # IDF étendu
    "Versailles":  (48.8049, 2.1204),
    "Cergy":       (49.0367, 2.0717),
    "Evry":        (48.6294, 2.4413),
    "Creteil":     (48.7903, 2.4555),
}

# URL Open-Meteo (gratuit, illimité, pas de clé). Format JSON natif.
OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lng}"
    "&current=temperature_2m,precipitation,weather_code"
    "&timezone=Europe%2FParis"
)

# URL Bison Futé RSS officiel (trafic temps réel autoroutes FR).
# Fallback si indisponible : on retourne dict vide (l'app gérera défauts neutres).
BISON_FUTE_RSS = "https://www.bison-fute.gouv.fr/rss-bison-fute.xml"

# URL Sytadin RSS (trafic IDF).
SYTADIN_RSS = "https://www.sytadin.fr/rss/etat-du-trafic-en-temps-reel.xml"

# Timeout HTTP en secondes — court car CRON 30 min, faut pas que ça hang.
HTTP_TIMEOUT_SEC = 15

# User-Agent identifiable (politesse + diagnostic côté APIs).
USER_AGENT = "okomox-data-bridge/1.0 (+https://github.com/drikythedog/okomox-data-bridge)"


# ============================================================================
# UTILITAIRES
# ============================================================================

def http_get_json(url: str) -> dict[str, Any] | None:
    """GET JSON avec User-Agent + timeout. Retourne None si échec (pas de crash)."""
    try:
        req = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (URLError, HTTPError, TimeoutError, json.JSONDecodeError) as e:
        print(f"  ⚠ http_get_json({url[:60]}…) failed: {e}", file=sys.stderr)
        return None


def http_get_text(url: str) -> str | None:
    """GET texte (pour RSS XML). None si échec."""
    try:
        req = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except (URLError, HTTPError, TimeoutError) as e:
        print(f"  ⚠ http_get_text({url[:60]}…) failed: {e}", file=sys.stderr)
        return None


# ============================================================================
# CONVERSIONS WMO weather code -> WeatherCondition Kotlin enum
# ============================================================================
# WMO 4677 codes (https://open-meteo.com/en/docs)
# 0=clear, 1-3=mainly clear/partly cloudy, 45-48=fog, 51-57=drizzle,
# 61=light rain, 63=moderate rain, 65=heavy rain, 66-67=freezing rain,
# 71-77=snow, 80-82=rain showers, 85-86=snow showers, 95-99=thunderstorm.
def weather_code_to_okomox(code: int, precip_mm: float) -> str:
    """Mappe le code WMO Open-Meteo vers l'enum WeatherCondition Okomox."""
    if code in (95, 96, 99):
        return "EXTREME"  # Orage
    if code in (65, 67, 75, 82, 86):
        return "HEAVY_RAIN"  # Pluie forte / neige forte
    if code in (51, 53, 55, 56, 57, 61, 63, 66, 71, 73, 80, 81, 85):
        return "LIGHT_RAIN"  # Bruine / pluie légère / neige légère
    if precip_mm >= 2.0:
        return "HEAVY_RAIN"  # Précipitations >= 2mm/h = forte
    if precip_mm >= 0.3:
        return "LIGHT_RAIN"
    return "DRY"


# ============================================================================
# COLLECTE MÉTÉO (Open-Meteo)
# ============================================================================

def fetch_city_weather(name: str, lat: float, lng: float) -> dict[str, Any]:
    """Récupère météo + température pour une ville. Defaults neutres si échec."""
    url = OPEN_METEO_URL.format(lat=lat, lng=lng)
    data = http_get_json(url)
    if not data or "current" not in data:
        print(f"  ⚠ Météo {name} : fallback DRY 18°C")
        return {"weather": "DRY", "temperatureC": 18, "lat": lat, "lng": lng}

    current = data["current"]
    temp = int(round(current.get("temperature_2m", 18)))
    precip = float(current.get("precipitation", 0))
    code = int(current.get("weather_code", 0))
    weather = weather_code_to_okomox(code, precip)

    print(f"  ✓ {name:12} {weather:10} {temp}°C (precip {precip}mm code {code})")
    return {
        "weather": weather,
        "temperatureC": temp,
        "lat": lat,
        "lng": lng,
    }


def collect_all_cities() -> dict[str, dict[str, Any]]:
    """Boucle sur les 14 villes. Robuste — une erreur sur une ville ne casse pas le reste."""
    print("\n📍 Collecte météo (Open-Meteo)...")
    result = {}
    for name, (lat, lng) in CITIES.items():
        try:
            result[name] = fetch_city_weather(name, lat, lng)
        except Exception as e:
            print(f"  ✗ {name} : exception {e}, défaut neutre")
            result[name] = {"weather": "DRY", "temperatureC": 18, "lat": lat, "lng": lng}
    return result


# ============================================================================
# COLLECTE TRAFIC AUTOROUTES (Bison Futé)
# ============================================================================

# Mapping simple mots-clés -> sévérité TrafficCondition.
TRAFFIC_KEYWORDS = {
    "BLOCKED":  ["fermé", "ferme", "fermeture", "bloqué", "bloque", "accident", "coupure"],
    "DENSE":    ["dense", "saturé", "sature", "bouchon", "ralentissement", "ralenti", "embouteillage"],
    "FLUID":    ["fluide", "normale", "normal"],
}

# Autoroutes à tracker (matching dans le titre/description du RSS).
HIGHWAYS = ["A1", "A4", "A6", "A7", "A8", "A9", "A10", "A11", "A13", "A14",
            "A15", "A16", "A20", "A26", "A31", "A35", "A36", "A40", "A43", "A61", "A62",
            "A63", "A75", "A89"]


def classify_traffic(text: str) -> str:
    """Mappe le texte d'un item RSS vers TrafficCondition."""
    t = text.lower()
    for status, keywords in TRAFFIC_KEYWORDS.items():
        for kw in keywords:
            if kw in t:
                return status
    return "NORMAL"  # défaut neutre


def collect_highways() -> dict[str, dict[str, Any]]:
    """Récupère le RSS Bison Futé et extrait l'état par autoroute."""
    print("\n🛣  Collecte trafic autoroutes (Bison Futé)...")
    result: dict[str, dict[str, Any]] = {h: {"status": "NORMAL", "comment": None} for h in HIGHWAYS}

    rss = http_get_text(BISON_FUTE_RSS)
    if not rss:
        print("  ⚠ Bison Futé indisponible — tous défauts NORMAL")
        return result

    try:
        root = ET.fromstring(rss)
        items = root.findall(".//item")
        print(f"  → {len(items)} items RSS reçus")

        for item in items:
            title_el = item.find("title")
            desc_el = item.find("description")
            title = (title_el.text or "") if title_el is not None else ""
            desc = (desc_el.text or "") if desc_el is not None else ""
            full = f"{title} {desc}"
            full_upper = full.upper()

            # Cherche quelle autoroute est mentionnée
            for hw in HIGHWAYS:
                if hw in full_upper:
                    status = classify_traffic(full)
                    # Update seulement si plus sévère que ce qu'on a déjà
                    severity = ["FLUID", "NORMAL", "DENSE", "BLOCKED"]
                    if severity.index(status) > severity.index(result[hw]["status"]):
                        result[hw] = {
                            "status": status,
                            "comment": title.strip()[:120] if title else None,
                        }
                        print(f"  ✓ {hw} : {status} ({title[:60]})")
    except ET.ParseError as e:
        print(f"  ⚠ Bison Futé RSS parse error : {e}")

    return result


# ============================================================================
# COLLECTE TRAFIC IDF (Sytadin)
# ============================================================================

def fetch_sytadin_summary() -> str:
    """Récupère un résumé global trafic IDF. Surcharge les villes IDF si dispo."""
    print("\n🚦 Collecte trafic IDF (Sytadin)...")
    rss = http_get_text(SYTADIN_RSS)
    if not rss:
        print("  ⚠ Sytadin indisponible")
        return "NORMAL"

    try:
        root = ET.fromstring(rss)
        items = root.findall(".//item")
        for item in items[:5]:  # 5 premiers items les plus récents
            title_el = item.find("title")
            desc_el = item.find("description")
            text = ((title_el.text or "") + " " + (desc_el.text or "")) if (title_el or desc_el) else ""
            status = classify_traffic(text)
            if status in ("DENSE", "BLOCKED"):
                print(f"  ✓ IDF status agrégé : {status}")
                return status
        return "NORMAL"
    except ET.ParseError as e:
        print(f"  ⚠ Sytadin RSS parse error : {e}")
        return "NORMAL"


# ============================================================================
# AGGREGATION + ENRICHISSEMENT VILLES AVEC TRAFFIC
# ============================================================================

# Pour chaque ville, on calcule un trafic dégradé à partir du trafic autoroutier proche.
# Mapping ville -> autoroutes principales (approximatif, à raffiner).
CITY_HIGHWAYS = {
    "Paris":       ["A1", "A4", "A6", "A13", "A14", "A15", "A86"],
    "Lyon":        ["A6", "A7", "A43", "A46", "A47"],
    "Marseille":   ["A7", "A50", "A55"],
    "Toulouse":    ["A61", "A62", "A64", "A20"],
    "Bordeaux":    ["A10", "A62", "A63", "A630"],
    "Nice":        ["A8"],
    "Nantes":      ["A11", "A83"],
    "Strasbourg":  ["A4", "A35"],
    "Lille":       ["A1", "A22", "A25"],
    "Montpellier": ["A9", "A75"],
    "Versailles":  ["A13", "A86"],
    "Cergy":       ["A15"],
    "Evry":        ["A6"],
    "Creteil":     ["A4", "A86"],
}


def enrich_cities_with_traffic(
    cities: dict[str, dict[str, Any]],
    highways: dict[str, dict[str, Any]],
    sytadin_idf: str,
) -> None:
    """Ajoute le champ 'traffic' à chaque ville selon les autoroutes proches + Sytadin pour IDF."""
    severity = ["FLUID", "NORMAL", "DENSE", "BLOCKED"]
    idf_cities = {"Paris", "Versailles", "Cergy", "Evry", "Creteil"}

    for city, info in cities.items():
        relevant_hws = CITY_HIGHWAYS.get(city, [])
        # Trafic max sur les autoroutes proches
        max_status = "NORMAL"
        for hw in relevant_hws:
            hw_status = highways.get(hw, {}).get("status", "NORMAL")
            if severity.index(hw_status) > severity.index(max_status):
                max_status = hw_status

        # Si ville IDF, on prend le pire entre autoroutes et Sytadin global
        if city in idf_cities:
            if severity.index(sytadin_idf) > severity.index(max_status):
                max_status = sytadin_idf

        info["traffic"] = max_status


# ============================================================================
# MAIN
# ============================================================================

def main() -> int:
    """Génère data/data.json. Retourne code de sortie (0 = succès, 1 = échec)."""
    start = datetime.now(timezone.utc)
    print(f"=== okomox-data-bridge collect @ {start.isoformat()} ===\n")

    cities = collect_all_cities()
    highways = collect_highways()
    sytadin_idf = fetch_sytadin_summary()
    enrich_cities_with_traffic(cities, highways, sytadin_idf)

    payload = {
        "version": 1,
        "generatedAt": start.isoformat(),
        "nextUpdateAt": (start + timedelta(minutes=30)).isoformat(),
        "cities": cities,
        "highways": highways,
        "_meta": {
            "sources": ["Open-Meteo", "Bison Futé RSS", "Sytadin RSS"],
            "cityCount": len(cities),
            "highwayCount": len(highways),
        },
    }

    # Écriture du fichier de sortie
    out_dir = Path(__file__).parent / "data"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / "data.json"
    out_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    print(f"\n✅ Écrit : {out_file} ({out_file.stat().st_size} octets) en {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
