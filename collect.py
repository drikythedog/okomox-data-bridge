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
import re
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
# Lot I 12/10/2026 : current + prévision horaire 36 h (rétrocompat : champs plats inchangés).
OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lng}"
    "&current=temperature_2m,precipitation,weather_code"
    "&hourly=temperature_2m,precipitation,weather_code"
    "&forecast_hours=36"
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

def build_hourly_slots(hourly_block: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Série horaire Open-Meteo → slots {at, weather, temperatureC}. Vide si absent."""
    if not hourly_block:
        return []
    times = hourly_block.get("time") or []
    temps = hourly_block.get("temperature_2m") or []
    precips = hourly_block.get("precipitation") or []
    codes = hourly_block.get("weather_code") or []
    out: list[dict[str, Any]] = []
    for i, t in enumerate(times):
        if not t:
            continue
        temp = int(round(temps[i])) if i < len(temps) and temps[i] is not None else 18
        precip = float(precips[i]) if i < len(precips) and precips[i] is not None else 0.0
        code = int(codes[i]) if i < len(codes) and codes[i] is not None else 0
        # Open-Meteo renvoie "2026-10-12T14:00" en Europe/Paris (timezone=) — l'app parse en LocalDateTime Paris.
        out.append({
            "at": t,
            "weather": weather_code_to_okomox(code, precip),
            "temperatureC": temp,
        })
    return out


def fetch_city_weather(name: str, lat: float, lng: float) -> dict[str, Any]:
    """Récupère météo + température (+ hourly 36 h) pour une ville. Defaults neutres si échec."""
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
    hourly = build_hourly_slots(data.get("hourly"))

    print(
        f"  ✓ {name:12} {weather:10} {temp}°C "
        f"(precip {precip}mm code {code}, hourly={len(hourly)})"
    )
    result: dict[str, Any] = {
        "weather": weather,
        "temperatureC": temp,
        "lat": lat,
        "lng": lng,
    }
    if hourly:
        result["hourly"] = hourly
    return result


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
            "A15", "A16", "A20", "A22", "A25", "A26", "A31", "A35", "A36", "A40", "A43",
            "A46", "A47", "A50", "A55", "A61", "A62", "A63", "A64", "A75", "A83", "A86",
            "A89", "A630"]  # 16/10/2026 : + autoroutes utilisées par les villes mais jamais suivies


def classify_traffic(text: str) -> str:
    """Mappe le texte d'un item RSS vers TrafficCondition."""
    t = text.lower()
    for status, keywords in TRAFFIC_KEYWORDS.items():
        for kw in keywords:
            if kw in t:
                return status
    return "NORMAL"  # défaut neutre


def _fold(text: str) -> str:
    """Majuscules sans accents (comparaison de noms de lieux)."""
    import unicodedata
    return "".join(ch for ch in unicodedata.normalize("NFD", text.upper()) if unicodedata.category(ch) != "Mn")


def collect_highways() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Récupère le RSS Bison Futé et extrait les ÉVÉNEMENTS par autoroute.

    16/10/2026 (audit météo/trafic) : une autoroute sans événement n'est plus déclarée « NORMAL »
    (on n'en sait rien) mais `status: null`. Seuls DENSE / BLOCKED signalés par la source comptent.
    Retourne (autoroutes, état de la source).
    """
    print("\n🛣  Collecte trafic autoroutes (Bison Futé)...")
    result: dict[str, dict[str, Any]] = {h: {"status": None, "comment": None} for h in HIGHWAYS}
    source = {"name": "Bison Futé RSS", "ok": False, "items": 0, "events": []}

    rss = http_get_text(BISON_FUTE_RSS)
    if not rss:
        print("  ⚠ Bison Futé indisponible — trafic autoroutes INCONNU (aucun NORMAL inventé)")
        return result, source

    try:
        root = ET.fromstring(rss)
        items = root.findall(".//item")
        source["ok"] = True
        source["items"] = len(items)
        print(f"  → {len(items)} items RSS reçus")
        severity = [None, "DENSE", "BLOCKED"]

        for item in items:
            title_el = item.find("title")
            desc_el = item.find("description")
            title = (title_el.text or "") if title_el is not None else ""
            desc = (desc_el.text or "") if desc_el is not None else ""
            full = f"{title} {desc}"
            full_upper = full.upper()
            status = classify_traffic(full)
            if status not in ("DENSE", "BLOCKED"):
                continue
            for hw in HIGHWAYS:
                # Mot entier : « A1 » ne doit pas capter « A13 ».
                if re.search(rf"\b{hw}\b", full_upper):
                    source["events"].append({"hw": hw, "status": status, "text": _fold(full)})
                    if severity.index(status) > severity.index(result[hw]["status"]):
                        result[hw] = {"status": status, "comment": title.strip()[:120] or None}
                        print(f"  ✓ {hw} : {status} ({title[:60]})")
    except ET.ParseError as e:
        source["ok"] = False
        print(f"  ⚠ Bison Futé RSS parse error : {e}")

    return result, source


# ============================================================================
# COLLECTE TRAFIC IDF (Sytadin)
# ============================================================================

def fetch_sytadin_summary() -> tuple[str | None, dict[str, Any]]:
    """Résumé global trafic IDF : DENSE / BLOCKED si signalé, sinon None (inconnu).

    16/10/2026 : correction d'un bug — `if (title_el or desc_el)` valait toujours Faux (un élément XML
    sans enfant est « faux » en Python) : le texte lu était vide, donc toujours NORMAL.
    """
    print("\n🚦 Collecte trafic IDF (Sytadin)...")
    source = {"name": "Sytadin RSS", "ok": False, "items": 0}
    rss = http_get_text(SYTADIN_RSS)
    if not rss:
        print("  ⚠ Sytadin indisponible — trafic IDF INCONNU")
        return None, source

    try:
        root = ET.fromstring(rss)
        items = root.findall(".//item")
        source["ok"] = True
        source["items"] = len(items)
        for item in items[:5]:  # 5 premiers items les plus récents
            title_el = item.find("title")
            desc_el = item.find("description")
            title = (title_el.text or "") if title_el is not None else ""
            desc = (desc_el.text or "") if desc_el is not None else ""
            status = classify_traffic(f"{title} {desc}")
            if status in ("DENSE", "BLOCKED"):
                print(f"  ✓ IDF status agrégé : {status}")
                return status, source
        return None, source
    except ET.ParseError as e:
        source["ok"] = False
        print(f"  ⚠ Sytadin RSS parse error : {e}")
        return None, source


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


# Noms qui rattachent un événement d'autoroute à une ville (texte sans accents, majuscules).
CITY_PLACE_NAMES = {
    "Paris": ["PARIS", "PERIPHERIQUE", "ILE-DE-FRANCE", "ILE DE FRANCE"],
    "Versailles": ["VERSAILLES"], "Cergy": ["CERGY", "PONTOISE"], "Evry": ["EVRY"],
    "Creteil": ["CRETEIL"], "Lyon": ["LYON"], "Marseille": ["MARSEILLE"], "Toulouse": ["TOULOUSE"],
    "Bordeaux": ["BORDEAUX"], "Nice": ["NICE"], "Nantes": ["NANTES"], "Strasbourg": ["STRASBOURG"],
    "Lille": ["LILLE"], "Montpellier": ["MONTPELLIER"],
}


def enrich_cities_with_traffic(
    cities: dict[str, dict[str, Any]],
    events: list[dict[str, Any]],
    sytadin_idf: str | None,
    observed_at: str,
) -> None:
    """Ajoute 'traffic' + 'trafficAt' aux villes pour lesquelles une source a SIGNALÉ un problème.

    16/10/2026 (audit météo/trafic) :
    - sans signalement, la ville n'a PAS de champ 'traffic' (inconnu). L'app Okomox (vc163+) utilise
      alors son trafic local (heure + vitesses mesurées) ; les anciennes versions lisent l'absence
      comme NORMAL, exactement comme avant ;
    - un événement d'autoroute ne compte pour une ville que s'il est sur une de ses autoroutes ET
      qu'il nomme la ville (avant : un bouchon A6 à Wissous rendait Lyon « dense ») ;
    - 'trafficAt' = heure de la mesure : l'app ignore un état de plus de 90 min.
    """
    severity = [None, "DENSE", "BLOCKED"]
    idf_cities = {"Paris", "Versailles", "Cergy", "Evry", "Creteil"}

    for city, info in cities.items():
        worst: str | None = None
        hws = set(CITY_HIGHWAYS.get(city, []))
        names = CITY_PLACE_NAMES.get(city, [_fold(city)])
        for ev in events:
            if ev["hw"] in hws and any(n in ev["text"] for n in names):
                if severity.index(ev["status"]) > severity.index(worst):
                    worst = ev["status"]
        if city in idf_cities and sytadin_idf in severity and severity.index(sytadin_idf) > severity.index(worst):
            worst = sytadin_idf

        info.pop("traffic", None)
        info.pop("trafficAt", None)
        if worst is not None:
            info["traffic"] = worst
            info["trafficAt"] = observed_at


# ============================================================================
# MAIN
# ============================================================================

def main() -> int:
    """Génère data/data.json. Retourne code de sortie (0 = succès, 1 = échec)."""
    start = datetime.now(timezone.utc)
    print(f"=== okomox-data-bridge collect @ {start.isoformat()} ===\n")

    cities = collect_all_cities()
    highways, bison_source = collect_highways()
    sytadin_idf, sytadin_source = fetch_sytadin_summary()
    enrich_cities_with_traffic(cities, bison_source.pop("events"), sytadin_idf, datetime.now(timezone.utc).isoformat())

    # GitHub Actions throttle les cron « */30 » : en pratique ~3–4 runs/jour.
    # nextUpdateAt honnête = +6 h (plus de +30 min mensonger). Les apps anciennes ignorent le champ.
    next_update = start + timedelta(hours=6)
    payload = {
        "version": 2,
        "generatedAt": start.isoformat(),
        "nextUpdateAt": next_update.isoformat(),
        "cities": cities,
        "highways": highways,
        "_meta": {
            "sources": ["Open-Meteo", "Bison Futé RSS", "Sytadin RSS"],
            "cityCount": len(cities),
            "highwayCount": len(highways),
            "hourlyForecastHours": 36,
            "nextUpdateNote": "GitHub scheduled workflows are throttled; expect ~6h between publishes",
            # 16/10/2026 : état réel des sources trafic (diagnostic) ; « traffic » absent = inconnu.
            "trafficSources": [bison_source, sytadin_source],
            "trafficNote": "city 'traffic' only when a source reported DENSE/BLOCKED; absent = unknown",
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
