"""
okomox-data-bridge — ce que l'application okomox reçoit comme information publique.

Génère data/data.json :
- météo de ~45 villes (grandes villes, préfectures de région, Île-de-France, aéroports),
  avec prévision horaire 36 h, rafales et probabilité de pluie ;
- synthèse par RÉGION et synthèse NATIONALE ;
- vigilance Météo-France par département (si la clé METEOFRANCE_API_KEY est configurée) ;
- trafic MESURÉ par agglomération (Bison Futé TRAFICOLOR, données ouvertes, 21 agglomérations),
  complété par les événements signalés (Bison Futé RSS, Sytadin) quand ces flux répondent.

Règle d'or (audit du 16/10/2026) : on ne publie JAMAIS une valeur inventée.
- Une ville sans mesure de trafic n'a pas de champ `traffic` (inconnu) ;
  l'app okomox utilise alors son estimation locale.
- Une ville dont la météo n'a pas pu être lue porte `weatherUnavailable: true`.

Format aligné sur les enums Kotlin de l'app :
- WeatherCondition : DRY / LIGHT_RAIN / HEAVY_RAIN / EXTREME
- TrafficCondition : FLUID / NORMAL / DENSE / BLOCKED

Compatibilité : les champs historiques (`cities.<ville>.weather`, `temperatureC`, `lat`, `lng`,
`hourly`, `traffic`, `highways`) gardent leur forme ; les 14 villes historiques gardent leur nom.

Secrets GitHub facultatifs (Settings → Secrets and variables → Actions) :
- OPEN_METEO_API_KEY   : clé de l'offre commerciale Open-Meteo (l'API gratuite est réservée
                         au non commercial ; avec la clé, l'appel passe par customer-api).
- METEOFRANCE_API_KEY  : clé « Bulletin Vigilance » du portail portail-api.meteofrance.fr.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError, HTTPError
from urllib.parse import urljoin
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

# ============================================================================
# CONFIGURATION — villes, régions, départements
# ============================================================================

# nom -> (lat, lng, département, région). Les 14 premières = noms historiques (ne pas renommer :
# l'app et ses réglages « zone principale » s'en servent).
CITIES: dict[str, tuple[float, float, str, str]] = {
    "Paris":        (48.8566, 2.3522, "75", "Île-de-France"),
    "Lyon":         (45.7640, 4.8357, "69", "Auvergne-Rhône-Alpes"),
    "Marseille":    (43.2965, 5.3698, "13", "Provence-Alpes-Côte d'Azur"),
    "Toulouse":     (43.6047, 1.4442, "31", "Occitanie"),
    "Bordeaux":     (44.8378, -0.5792, "33", "Nouvelle-Aquitaine"),
    "Nice":         (43.7102, 7.2620, "06", "Provence-Alpes-Côte d'Azur"),
    "Nantes":       (47.2184, -1.5536, "44", "Pays de la Loire"),
    "Strasbourg":   (48.5734, 7.7521, "67", "Grand Est"),
    "Lille":        (50.6292, 3.0573, "59", "Hauts-de-France"),
    "Montpellier":  (43.6108, 3.8767, "34", "Occitanie"),
    "Versailles":   (48.8049, 2.1204, "78", "Île-de-France"),
    "Cergy":        (49.0367, 2.0717, "95", "Île-de-France"),
    "Evry":         (48.6294, 2.4413, "91", "Île-de-France"),
    "Creteil":      (48.7903, 2.4555, "94", "Île-de-France"),
    # 16/10/2026 — Île-de-France : pôles VTC
    "Roissy-CDG":   (49.0097, 2.5479, "95", "Île-de-France"),
    "Orly":         (48.7262, 2.3652, "94", "Île-de-France"),
    "La Defense":   (48.8920, 2.2380, "92", "Île-de-France"),
    "Saint-Denis":  (48.9362, 2.3574, "93", "Île-de-France"),
    "Marne-la-Vallee": (48.8708, 2.7836, "77", "Île-de-France"),
    # 16/10/2026 — préfectures de région et grandes villes
    "Rennes":       (48.1173, -1.6778, "35", "Bretagne"),
    "Brest":        (48.3904, -4.4861, "29", "Bretagne"),
    "Rouen":        (49.4432, 1.0999, "76", "Normandie"),
    "Caen":         (49.1829, -0.3707, "14", "Normandie"),
    "Le Havre":     (49.4944, 0.1079, "76", "Normandie"),
    "Dijon":        (47.3220, 5.0415, "21", "Bourgogne-Franche-Comté"),
    "Besancon":     (47.2378, 6.0241, "25", "Bourgogne-Franche-Comté"),
    "Orleans":      (47.9030, 1.9093, "45", "Centre-Val de Loire"),
    "Tours":        (47.3941, 0.6848, "37", "Centre-Val de Loire"),
    "Ajaccio":      (41.9192, 8.7386, "2A", "Corse"),
    "Bastia":       (42.6970, 9.4509, "2B", "Corse"),
    "Grenoble":     (45.1885, 5.7245, "38", "Auvergne-Rhône-Alpes"),
    "Saint-Etienne": (45.4397, 4.3872, "42", "Auvergne-Rhône-Alpes"),
    "Clermont-Ferrand": (45.7772, 3.0870, "63", "Auvergne-Rhône-Alpes"),
    "Annecy":       (45.8992, 6.1294, "74", "Auvergne-Rhône-Alpes"),
    "Toulon":       (43.1242, 5.9280, "83", "Provence-Alpes-Côte d'Azur"),
    "Avignon":      (43.9493, 4.8055, "84", "Provence-Alpes-Côte d'Azur"),
    "Perpignan":    (42.6887, 2.8948, "66", "Occitanie"),
    "Nimes":        (43.8367, 4.3601, "30", "Occitanie"),
    "Limoges":      (45.8336, 1.2611, "87", "Nouvelle-Aquitaine"),
    "Poitiers":     (46.5802, 0.3404, "86", "Nouvelle-Aquitaine"),
    "Pau":          (43.2951, -0.3708, "64", "Nouvelle-Aquitaine"),
    "Bayonne":      (43.4929, -1.4748, "64", "Nouvelle-Aquitaine"),
    "Angers":       (47.4784, -0.5632, "49", "Pays de la Loire"),
    "Le Mans":      (48.0061, 0.1996, "72", "Pays de la Loire"),
    "Reims":        (49.2583, 4.0317, "51", "Grand Est"),
    "Metz":         (49.1193, 6.1757, "57", "Grand Est"),
    "Nancy":        (48.6921, 6.1844, "54", "Grand Est"),
    "Mulhouse":     (47.7508, 7.3359, "68", "Grand Est"),
    "Amiens":       (49.8941, 2.2958, "80", "Hauts-de-France"),
    "Calais":       (50.9513, 1.8587, "62", "Hauts-de-France"),
}

REGIONS = sorted({c[3] for c in CITIES.values()})

# Open-Meteo. Gratuit = non commercial uniquement ; clé commerciale via OPEN_METEO_API_KEY.
OPEN_METEO_FREE = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_COMMERCIAL = "https://customer-api.open-meteo.com/v1/forecast"
OPEN_METEO_QUERY = (
    "?latitude={lats}&longitude={lngs}"
    "&current=temperature_2m,precipitation,weather_code,wind_gusts_10m"
    "&hourly=temperature_2m,precipitation,weather_code,precipitation_probability"
    "&forecast_hours=36&timezone=Europe%2FParis"
)
OPEN_METEO_BATCH = 20  # villes par appel (lat/lng séparées par des virgules)

# Météo-France — vigilance en cours (clé « Bulletin Vigilance » du portail, Licence Ouverte 2.0).
METEOFRANCE_VIGILANCE_URL = "https://public-api.meteofrance.fr/public/DPVigilance/v1/cartevigilance/encours"
VIGILANCE_COLORS = {1: "vert", 2: "jaune", 3: "orange", 4: "rouge"}
VIGILANCE_PHENOMENA = {
    1: "vent violent", 2: "pluie-inondation", 3: "orages", 4: "crues", 5: "neige-verglas",
    6: "canicule", 7: "grand froid", 8: "avalanches", 9: "vagues-submersion",
}

# Bison Futé — états du trafic des agglomérations (DATEX II, Licence Ouverte 2.0, 1 à 6 min).
TRAFICOLOR_INDEXES = [
    "https://tipi.bison-fute.gouv.fr/bison-fute-ouvert/publicationsDIR/TRAFICOLOR-DIR/",
    "http://tipi.bison-fute.gouv.fr/bison-fute-ouvert/publicationsDIR/TRAFICOLOR-DIR/",
]
TRAFICOLOR_MAX_FILES = 40

# Agglomération TRAFICOLOR (noms sans accents, majuscules) -> villes okomox qu'elle couvre.
TRAFICOLOR_AGGLOS: dict[str, list[str]] = {
    "PARIS": ["Paris", "Versailles", "Cergy", "Evry", "Creteil", "Roissy-CDG", "Orly",
              "La Defense", "Saint-Denis", "Marne-la-Vallee"],
    "ILE-DE-FRANCE": ["Paris"], "IDF": ["Paris"],
    "LYON": ["Lyon"], "MARSEILLE": ["Marseille"], "TOULOUSE": ["Toulouse"],
    "BORDEAUX": ["Bordeaux"], "NANTES": ["Nantes"], "STRASBOURG": ["Strasbourg"],
    "LILLE": ["Lille"], "MONTPELLIER": ["Montpellier"], "RENNES": ["Rennes"],
    "ROUEN": ["Rouen"], "CAEN": ["Caen"], "GRENOBLE": ["Grenoble"],
    "SAINT-ETIENNE": ["Saint-Etienne"], "ST-ETIENNE": ["Saint-Etienne"], "ST ETIENNE": ["Saint-Etienne"],
    "CLERMONT": ["Clermont-Ferrand"], "LIMOGES": ["Limoges"], "METZ": ["Metz"],
    "NANCY": ["Nancy"], "MULHOUSE": ["Mulhouse"], "CALAIS": ["Calais"], "BRIVE": [],
}

# Valeurs d'état reconnues -> classe (DATEX II TrafficStatusEnum + libellés français).
STATUS_CLASSES = {
    "FREEFLOW": "fluide", "NORMAL": "fluide", "FLUIDE": "fluide",
    "HEAVY": "ralenti", "RALENTI": "ralenti", "DENSE": "ralenti",
    "CONGESTED": "sature", "SATURE": "sature", "QUEUINGTRAFFIC": "sature", "STATIONARYTRAFFIC": "sature",
    "IMPOSSIBLE": "bloque", "BLOQUE": "bloque", "FERME": "bloque",
}

BISON_FUTE_RSS = "https://www.bison-fute.gouv.fr/rss-bison-fute.xml"
SYTADIN_RSS = "https://www.sytadin.fr/rss/etat-du-trafic-en-temps-reel.xml"

HTTP_TIMEOUT_SEC = 15
USER_AGENT = "okomox-data-bridge/3.0 (+https://github.com/drikythedog/okomox-data-bridge)"

# Ordre de sévérité commun.
WEATHER_SEVERITY = ["DRY", "LIGHT_RAIN", "HEAVY_RAIN", "EXTREME"]
TRAFFIC_SEVERITY = ["FLUID", "NORMAL", "DENSE", "BLOCKED"]


# ============================================================================
# UTILITAIRES
# ============================================================================

def _fold(text: str) -> str:
    """Majuscules sans accents."""
    return "".join(ch for ch in unicodedata.normalize("NFD", (text or "").upper())
                   if unicodedata.category(ch) != "Mn")


def http_get_bytes(url: str, headers: dict[str, str] | None = None) -> bytes | None:
    try:
        h = {"User-Agent": USER_AGENT}
        if headers:
            h.update(headers)
        with urlopen(Request(url, headers=h), timeout=HTTP_TIMEOUT_SEC) as resp:
            return resp.read()
    except (URLError, HTTPError, TimeoutError, OSError, ValueError) as e:
        print(f"  ⚠ GET {url[:80]} : {e}", file=sys.stderr)
        return None


def http_get_json(url: str, headers: dict[str, str] | None = None) -> Any:
    raw = http_get_bytes(url, headers)
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        print(f"  ⚠ JSON illisible ({url[:60]}) : {e}", file=sys.stderr)
        return None


def http_get_text(url: str) -> str | None:
    raw = http_get_bytes(url)
    return raw.decode("utf-8", errors="ignore") if raw is not None else None


def _local(tag: str) -> str:
    """Nom d'élément XML sans espace de noms."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


# ============================================================================
# MÉTÉO (Open-Meteo, appels groupés)
# ============================================================================
# WMO 4677 : 0 clair, 1-3 nuageux, 45-48 brouillard, 51-57 bruine, 61-67 pluie,
# 71-77 neige, 80-82 averses, 85-86 averses de neige, 95-99 orage.

def weather_code_to_okomox(code: int, precip_mm: float) -> str:
    if code in (95, 96, 99):
        return "EXTREME"
    if code in (65, 67, 75, 82, 86):
        return "HEAVY_RAIN"
    if code in (51, 53, 55, 56, 57, 61, 63, 66, 71, 73, 80, 81, 85):
        return "LIGHT_RAIN"
    if precip_mm >= 2.0:
        return "HEAVY_RAIN"
    if precip_mm >= 0.3:
        return "LIGHT_RAIN"
    return "DRY"


def build_hourly_slots(hourly_block: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Série horaire → slots {at, weather, temperatureC, precipProbability}. Heures sans
    température ni code sont ignorées (jamais de valeur inventée)."""
    if not hourly_block:
        return []
    times = hourly_block.get("time") or []
    temps = hourly_block.get("temperature_2m") or []
    precips = hourly_block.get("precipitation") or []
    codes = hourly_block.get("weather_code") or []
    probs = hourly_block.get("precipitation_probability") or []
    out: list[dict[str, Any]] = []
    for i, t in enumerate(times):
        temp = temps[i] if i < len(temps) else None
        code = codes[i] if i < len(codes) else None
        if not t or temp is None or code is None:
            continue
        precip = precips[i] if i < len(precips) and precips[i] is not None else 0.0
        slot = {"at": t, "weather": weather_code_to_okomox(int(code), float(precip)),
                "temperatureC": int(round(temp))}
        if i < len(probs) and probs[i] is not None:
            slot["precipProbability"] = int(probs[i])
        out.append(slot)
    return out


def _city_from_open_meteo(name: str, block: dict[str, Any] | None) -> dict[str, Any]:
    lat, lng, dept, region = CITIES[name]
    base: dict[str, Any] = {"lat": lat, "lng": lng, "dept": dept, "region": region}
    cur = (block or {}).get("current") or {}
    if cur.get("temperature_2m") is None or cur.get("weather_code") is None:
        print(f"  ⚠ Météo {name} : indisponible")
        # Rétrocompat : les anciennes versions lisent weather/temperatureC ; on garde des valeurs
        # neutres MAIS on le dit (weatherUnavailable) pour que l'écran et la page l'affichent.
        base.update({"weather": "DRY", "temperatureC": 18, "weatherUnavailable": True})
        return base
    precip = float(cur.get("precipitation") or 0.0)
    code = int(cur["weather_code"])
    base["weather"] = weather_code_to_okomox(code, precip)
    base["temperatureC"] = int(round(cur["temperature_2m"]))
    base["precipitationMm"] = round(precip, 1)
    if cur.get("wind_gusts_10m") is not None:
        base["windGustKmh"] = int(round(cur["wind_gusts_10m"]))
    hourly = build_hourly_slots(block.get("hourly"))
    if hourly:
        base["hourly"] = hourly
        nxt = [s.get("precipProbability") for s in hourly[:3] if s.get("precipProbability") is not None]
        if nxt:
            base["precipProbabilityNext3h"] = max(nxt)
    print(f"  ✓ {name:16} {base['weather']:10} {base['temperatureC']}°C")
    return base


def collect_weather(get_json=http_get_json) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Météo de toutes les villes par lots. Retourne (villes, état de la source)."""
    key = os.environ.get("OPEN_METEO_API_KEY", "").strip()
    host = OPEN_METEO_COMMERCIAL if key else OPEN_METEO_FREE
    print(f"\n📍 Météo Open-Meteo ({'offre commerciale' if key else 'API gratuite'}) — {len(CITIES)} villes")
    names = list(CITIES)
    blocks: dict[str, dict[str, Any] | None] = {}
    calls_ok = 0
    for i in range(0, len(names), OPEN_METEO_BATCH):
        batch = names[i:i + OPEN_METEO_BATCH]
        url = host + OPEN_METEO_QUERY.format(
            lats=",".join(f"{CITIES[n][0]:.4f}" for n in batch),
            lngs=",".join(f"{CITIES[n][1]:.4f}" for n in batch),
        ) + (f"&apikey={key}" if key else "")
        data = get_json(url)
        if isinstance(data, dict):  # un seul lieu
            data = [data]
        if isinstance(data, list) and len(data) == len(batch):
            calls_ok += 1
            for n, block in zip(batch, data):
                blocks[n] = block
        else:
            for n in batch:
                blocks[n] = None
    cities = {n: _city_from_open_meteo(n, blocks.get(n)) for n in names}
    ok = sum(1 for c in cities.values() if not c.get("weatherUnavailable"))
    source = {"name": "Open-Meteo", "ok": ok > 0, "cities": ok,
              "plan": "commercial" if key else "gratuit (usage non commercial)"}
    return cities, source


# ============================================================================
# VIGILANCE MÉTÉO-FRANCE (facultative, clé requise)
# ============================================================================

def parse_vigilance(data: Any) -> dict[str, dict[str, Any]]:
    """JSON « cartevigilance/encours » -> {département: {level, color, phenomena[]}} pour la
    période en cours (J). Tolérant : ignore tout ce qui ne correspond pas au format attendu."""
    out: dict[str, dict[str, Any]] = {}
    periods = (((data or {}).get("product") or {}).get("periods")) or []
    if not periods:
        return out
    current = next((p for p in periods if str(p.get("echeance", "")).upper() == "J"), periods[0])
    domains = ((current.get("timelaps") or {}).get("domain_ids")) or []
    for d in domains:
        dept = str(d.get("domain_id", "")).strip()
        if not re.fullmatch(r"\d{2}|2A|2B|97\d", dept):
            continue  # zones côtières (« 3010 »…) et agrégats ignorés
        level = int(d.get("max_color_id") or 1)
        phen = []
        for p in d.get("phenomenon_items") or []:
            c = int(p.get("phenomenon_max_color_id") or 1)
            if c >= 2:
                phen.append({"phenomenon": VIGILANCE_PHENOMENA.get(int(p.get("phenomenon_id") or 0), "autre"),
                             "level": c, "color": VIGILANCE_COLORS.get(c, "?")})
        out[dept] = {"level": level, "color": VIGILANCE_COLORS.get(level, "?"), "phenomena": phen}
    return out


def collect_vigilance(get_json=http_get_json) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    key = os.environ.get("METEOFRANCE_API_KEY", "").strip()
    source = {"name": "Météo-France Vigilance", "ok": False, "departments": 0}
    if not key:
        print("\n⚠ Vigilance Météo-France : clé METEOFRANCE_API_KEY absente — ignorée")
        source["note"] = "clé non configurée"
        return {}, source
    print("\n🟠 Vigilance Météo-France…")
    vig = parse_vigilance(get_json(METEOFRANCE_VIGILANCE_URL, {"apikey": key}))
    source.update({"ok": bool(vig), "departments": len(vig)})
    return vig, source


# ============================================================================
# TRAFIC MESURÉ — Bison Futé TRAFICOLOR (DATEX II)
# ============================================================================

def list_traficolor_files(index_html: str, base_url: str) -> tuple[list[str], list[str]]:
    """Liens .xml d'un index Apache (et d'un niveau de sous-dossiers si besoin)."""
    hrefs = re.findall(r'href="([^"?#]+)"', index_html or "")
    files = [urljoin(base_url, h) for h in hrefs if h.lower().endswith(".xml")]
    dirs = [urljoin(base_url, h) for h in hrefs
            if h.endswith("/") and not h.startswith(("/", "..", "http")) and h != "./"]
    return files, dirs


def agglos_in_text(folded: str) -> list[str]:
    return [a for a in TRAFICOLOR_AGGLOS if re.search(rf"(?<![A-Z]){re.escape(a)}(?![A-Z])", folded)]


def parse_traficolor_xml(xml_text: str, file_hint: str = "") -> tuple[dict[str, Counter], dict[str, Any]]:
    """Compte les états (fluide / ralenti / saturé / bloqué) par agglomération dans un fichier.

    Format DATEX II non documenté ici : lecture générique.
    - Un « état » = élément feuille dont le texte (sans accents, sans espaces) est une valeur
      reconnue dans STATUS_CLASSES et dont la balise évoque un statut (status/level/state/value).
    - Agglomération : nom trouvé dans les ancêtres proches de l'élément ; sinon, nom unique du
      fichier (texte ou nom de fichier) ; sinon non attribué.
    Retourne (compteurs par agglo, diagnostic)."""
    diag: dict[str, Any] = {"file": file_hint.rsplit("/", 1)[-1], "states": 0, "attributed": 0}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        diag["error"] = f"XML illisible : {e}"
        return {}, diag
    parent: dict[ET.Element, ET.Element] = {}
    for p in root.iter():
        for c in p:
            parent[c] = p
    file_agglos = agglos_in_text(_fold(file_hint)) or agglos_in_text(_fold(" ".join(root.itertext()))[:200000])
    file_default = file_agglos[0] if len(file_agglos) == 1 else None
    pub = None
    tags = Counter()
    counts: dict[str, Counter] = {}
    for el in root.iter():
        name = _local(el.tag)
        tags[name] += 1
        if name.lower() == "publicationtime" and el.text and not pub:
            pub = el.text.strip()
        if len(el) or not el.text:
            continue
        val = re.sub(r"[\s_\-]", "", _fold(el.text))
        cls = STATUS_CLASSES.get(val)
        if not cls or not re.search(r"status|level|state|etat|value|traffic", name, re.I):
            continue
        diag["states"] += 1
        agglo = None
        node, depth = el, 0
        while node in parent and depth < 6 and agglo is None:
            node = parent[node]
            depth += 1
            found = agglos_in_text(_fold(" ".join(t for t in node.itertext())[:4000]))
            if len(found) == 1:
                agglo = found[0]
        agglo = agglo or file_default
        if agglo:
            diag["attributed"] += 1
            counts.setdefault(agglo, Counter())[cls] += 1
    diag["publicationTime"] = pub
    diag["topTags"] = [t for t, _ in tags.most_common(12)]
    diag["agglos"] = sorted(counts)
    return counts, diag


def traffic_from_shares(c: Counter) -> tuple[str | None, dict[str, Any]]:
    """Compteurs d'états -> (TrafficCondition, détail en %). Au moins 5 mesures, sinon inconnu.
    Seuils (à recalibrer après quelques jours de données) :
    - BLOQUÉ : ≥ 10 % bloqué, ou ≥ 35 % saturé + bloqué
    - DENSE  : ≥ 25 % ralenti + saturé + bloqué
    - FLUIDE : ≤ 5 % ralenti + saturé + bloqué
    - NORMAL : entre les deux."""
    total = sum(c.values())
    if total < 5:
        return None, {}
    pct = {k: round(100 * c.get(k, 0) / total) for k in ("fluide", "ralenti", "sature", "bloque")}
    bad = c.get("ralenti", 0) + c.get("sature", 0) + c.get("bloque", 0)
    if c.get("bloque", 0) / total >= 0.10 or (c.get("sature", 0) + c.get("bloque", 0)) / total >= 0.35:
        status = "BLOCKED"
    elif bad / total >= 0.25:
        status = "DENSE"
    elif bad / total <= 0.05:
        status = "FLUID"
    else:
        status = "NORMAL"
    detail = {"mesures": total, **{f"{k}Pct": v for k, v in pct.items()}}
    return status, detail


def collect_traficolor(get_text=http_get_text) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Trafic mesuré par agglomération -> {ville: {traffic, trafficDetail, trafficSource}}."""
    print("\n🚦 Trafic mesuré (Bison Futé TRAFICOLOR)…")
    source: dict[str, Any] = {"name": "Bison Futé TRAFICOLOR", "ok": False, "files": 0, "diagnostic": []}
    files: list[str] = []
    for idx in TRAFICOLOR_INDEXES:
        html = get_text(idx)
        if not html:
            continue
        f, dirs = list_traficolor_files(html, idx)
        files.extend(f)
        for d in dirs[:30]:
            sub = get_text(d)
            if sub:
                files.extend(list_traficolor_files(sub, d)[0])
        if files:
            break
    files = list(dict.fromkeys(files))[:TRAFICOLOR_MAX_FILES]
    if not files:
        print("  ⚠ TRAFICOLOR : index illisible ou vide — trafic mesuré INCONNU")
        return {}, source
    totals: dict[str, Counter] = {}
    for url in files:
        xml = get_text(url)
        if not xml:
            continue
        source["files"] += 1
        counts, diag = parse_traficolor_xml(xml, url)
        source["diagnostic"].append(diag)
        for agglo, c in counts.items():
            totals.setdefault(agglo, Counter()).update(c)
    source["diagnostic"] = source["diagnostic"][:8]
    out: dict[str, dict[str, Any]] = {}
    for agglo, c in totals.items():
        status, detail = traffic_from_shares(c)
        if not status:
            continue
        for city in TRAFICOLOR_AGGLOS.get(agglo, []):
            if city in out and out[city]["trafficDetail"]["mesures"] >= detail["mesures"]:
                continue
            out[city] = {"traffic": status, "trafficDetail": detail,
                         "trafficSource": f"Bison Futé TRAFICOLOR ({agglo.title()})"}
            print(f"  ✓ {city:16} {status:8} {detail}")
    source["ok"] = bool(out)
    source["agglomerations"] = sorted(totals)
    return out, source


# ============================================================================
# ÉVÉNEMENTS SIGNALÉS — Bison Futé RSS / Sytadin (si ces flux existent encore)
# ============================================================================

TRAFFIC_KEYWORDS = {
    "BLOCKED": ["fermé", "ferme", "fermeture", "bloqué", "bloque", "accident", "coupure"],
    "DENSE": ["dense", "saturé", "sature", "bouchon", "ralentissement", "ralenti", "embouteillage"],
    "FLUID": ["fluide", "normale", "normal"],
}

HIGHWAYS = ["A1", "A4", "A6", "A7", "A8", "A9", "A10", "A11", "A13", "A14",
            "A15", "A16", "A20", "A22", "A25", "A26", "A31", "A35", "A36", "A40", "A43",
            "A46", "A47", "A50", "A55", "A61", "A62", "A63", "A64", "A75", "A83", "A86",
            "A89", "A630"]

CITY_HIGHWAYS = {
    "Paris": ["A1", "A4", "A6", "A13", "A14", "A15", "A86"], "Lyon": ["A6", "A7", "A43", "A46", "A47"],
    "Marseille": ["A7", "A50", "A55"], "Toulouse": ["A61", "A62", "A64", "A20"],
    "Bordeaux": ["A10", "A62", "A63", "A630"], "Nice": ["A8"], "Nantes": ["A11", "A83"],
    "Strasbourg": ["A4", "A35"], "Lille": ["A1", "A22", "A25"], "Montpellier": ["A9", "A75"],
    "Versailles": ["A13", "A86"], "Cergy": ["A15"], "Evry": ["A6"], "Creteil": ["A4", "A86"],
    "Roissy-CDG": ["A1"], "Orly": ["A6", "A106"], "La Defense": ["A14", "A86"],
    "Saint-Denis": ["A1", "A86"], "Marne-la-Vallee": ["A4"],
}

CITY_PLACE_NAMES = {
    "Paris": ["PARIS", "PERIPHERIQUE", "ILE-DE-FRANCE", "ILE DE FRANCE"],
    "Roissy-CDG": ["ROISSY", "CHARLES DE GAULLE"], "Orly": ["ORLY"], "La Defense": ["DEFENSE"],
    "Marne-la-Vallee": ["MARNE-LA-VALLEE", "MARNE LA VALLEE"], "Cergy": ["CERGY", "PONTOISE"],
}


def classify_traffic(text: str) -> str:
    t = text.lower()
    for status, keywords in TRAFFIC_KEYWORDS.items():
        for kw in keywords:
            if kw in t:
                return status
    return "NORMAL"


def collect_highways(get_text=http_get_text) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Événements Bison Futé RSS. Autoroute sans événement = status null (inconnu)."""
    print("\n🛣  Événements autoroutes (Bison Futé RSS)…")
    result: dict[str, dict[str, Any]] = {h: {"status": None, "comment": None} for h in HIGHWAYS}
    source: dict[str, Any] = {"name": "Bison Futé RSS", "ok": False, "items": 0, "events": []}
    rss = get_text(BISON_FUTE_RSS)
    if not rss:
        print("  ⚠ Bison Futé RSS indisponible")
        return result, source
    try:
        items = ET.fromstring(rss).findall(".//item")
    except ET.ParseError as e:
        print(f"  ⚠ Bison Futé RSS illisible : {e}")
        return result, source
    source.update({"ok": True, "items": len(items)})
    severity = [None, "DENSE", "BLOCKED"]
    for item in items:
        title_el, desc_el = item.find("title"), item.find("description")
        title = (title_el.text or "") if title_el is not None else ""
        desc = (desc_el.text or "") if desc_el is not None else ""
        full = f"{title} {desc}"
        status = classify_traffic(full)
        if status not in ("DENSE", "BLOCKED"):
            continue
        for hw in HIGHWAYS:
            if re.search(rf"\b{hw}\b", full.upper()):
                source["events"].append({"hw": hw, "status": status, "text": _fold(full)})
                if severity.index(status) > severity.index(result[hw]["status"]):
                    result[hw] = {"status": status, "comment": title.strip()[:120] or None}
    return result, source


def fetch_sytadin_summary(get_text=http_get_text) -> tuple[str | None, dict[str, Any]]:
    """Résumé trafic IDF : DENSE / BLOCKED si signalé, sinon None (inconnu)."""
    print("\n🚦 Sytadin (Île-de-France)…")
    source = {"name": "Sytadin RSS", "ok": False, "items": 0}
    rss = get_text(SYTADIN_RSS)
    if not rss:
        print("  ⚠ Sytadin indisponible")
        return None, source
    try:
        items = ET.fromstring(rss).findall(".//item")
    except ET.ParseError as e:
        print(f"  ⚠ Sytadin illisible : {e}")
        return None, source
    source.update({"ok": True, "items": len(items)})
    for item in items[:5]:
        title_el, desc_el = item.find("title"), item.find("description")
        title = (title_el.text or "") if title_el is not None else ""
        desc = (desc_el.text or "") if desc_el is not None else ""
        status = classify_traffic(f"{title} {desc}")
        if status in ("DENSE", "BLOCKED"):
            return status, source
    return None, source


IDF_CITIES = {n for n, c in CITIES.items() if c[3] == "Île-de-France"}


def events_traffic(events: list[dict[str, Any]], sytadin_idf: str | None) -> dict[str, str]:
    """Pire événement signalé par ville (autoroute de la ville ET ville nommée), + Sytadin en IDF."""
    severity = [None, "DENSE", "BLOCKED"]
    out: dict[str, str] = {}
    for city in CITIES:
        worst = None
        hws = set(CITY_HIGHWAYS.get(city, []))
        names = CITY_PLACE_NAMES.get(city, [_fold(city).replace("-", " "), _fold(city)])
        for ev in events:
            if ev["hw"] in hws and any(n in ev["text"] for n in names):
                if severity.index(ev["status"]) > severity.index(worst):
                    worst = ev["status"]
        if city in IDF_CITIES and sytadin_idf in severity and severity.index(sytadin_idf) > severity.index(worst):
            worst = sytadin_idf
        if worst:
            out[city] = worst
    return out


# ============================================================================
# FUSION, RÉGIONS, NATIONAL
# ============================================================================

def apply_traffic(cities: dict[str, dict[str, Any]], measured: dict[str, dict[str, Any]],
                  events: dict[str, str], observed_at: str) -> None:
    """Mesure TRAFICOLOR d'abord ; un événement signalé plus grave l'aggrave. Sans rien : pas de
    champ `traffic` (inconnu) — l'app utilise alors son estimation locale."""
    for name, info in cities.items():
        for k in ("traffic", "trafficAt", "trafficDetail", "trafficSource"):
            info.pop(k, None)
        status, detail, src = None, None, None
        if name in measured:
            status = measured[name]["traffic"]
            detail = measured[name]["trafficDetail"]
            src = measured[name]["trafficSource"]
        ev = events.get(name)
        if ev and (status is None or TRAFFIC_SEVERITY.index(ev) > TRAFFIC_SEVERITY.index(status)):
            status = ev
            src = (src + " + " if src else "") + "événement signalé"
        if status:
            info["traffic"] = status
            info["trafficAt"] = observed_at
            info["trafficSource"] = src
            if detail:
                info["trafficDetail"] = detail


def apply_vigilance(cities: dict[str, dict[str, Any]], vig: dict[str, dict[str, Any]]) -> None:
    for info in cities.values():
        v = vig.get(info.get("dept", ""))
        if v:
            info["vigilance"] = v


def summarize_regions(cities: dict[str, dict[str, Any]], vig: dict[str, dict[str, Any]]) -> dict[str, Any]:
    regions: dict[str, Any] = {}
    for region in REGIONS:
        members = {n: c for n, c in cities.items() if c.get("region") == region}
        ok = [c for c in members.values() if not c.get("weatherUnavailable")]
        r: dict[str, Any] = {"cities": sorted(members)}
        if ok:
            temps = [c["temperatureC"] for c in ok]
            r["weather"] = max((c["weather"] for c in ok), key=WEATHER_SEVERITY.index)
            r["rainingCities"] = sum(1 for c in ok if c["weather"] != "DRY")
            r["temperatureMinC"], r["temperatureMaxC"] = min(temps), max(temps)
            gusts = [c["windGustKmh"] for c in ok if "windGustKmh" in c]
            if gusts:
                r["windGustMaxKmh"] = max(gusts)
        traffic = [c["traffic"] for c in members.values() if c.get("traffic")]
        if traffic:
            r["trafficWorst"] = max(traffic, key=TRAFFIC_SEVERITY.index)
        depts = {c.get("dept") for c in members.values()}
        levels = [vig[d]["level"] for d in depts if d in vig]
        if levels:
            r["vigilanceMax"] = VIGILANCE_COLORS.get(max(levels), "?")
        regions[region] = r
    return regions


def summarize_national(cities: dict[str, dict[str, Any]], vig: dict[str, dict[str, Any]]) -> dict[str, Any]:
    ok = [c for c in cities.values() if not c.get("weatherUnavailable")]
    n: dict[str, Any] = {"cityCount": len(cities), "weatherCities": len(ok)}
    if ok:
        n["weatherCounts"] = dict(Counter(c["weather"] for c in ok))
        n["temperatureMinC"] = min(c["temperatureC"] for c in ok)
        n["temperatureMaxC"] = max(c["temperatureC"] for c in ok)
    tr = [c["traffic"] for c in cities.values() if c.get("traffic")]
    n["trafficMeasuredCities"] = len(tr)
    if tr:
        n["trafficCounts"] = dict(Counter(tr))
    if vig:
        alerts = {d: v for d, v in vig.items() if v["level"] >= 3}
        n["vigilanceMax"] = VIGILANCE_COLORS.get(max(v["level"] for v in vig.values()), "?")
        n["vigilanceOrangeRouge"] = [{"dept": d, "color": v["color"],
                                      "phenomena": [p["phenomenon"] for p in v["phenomena"] if p["level"] >= 3]}
                                     for d, v in sorted(alerts.items())]
    return n


# ============================================================================
# MAIN
# ============================================================================

def build_payload(get_json=http_get_json, get_text=http_get_text,
                  now: datetime | None = None) -> dict[str, Any]:
    start = now or datetime.now(timezone.utc)
    cities, weather_src = collect_weather(get_json)
    vig, vig_src = collect_vigilance(get_json)
    measured, tc_src = collect_traficolor(get_text)
    highways, bison_src = collect_highways(get_text)
    sytadin, syt_src = fetch_sytadin_summary(get_text)
    observed = datetime.now(timezone.utc).isoformat() if now is None else start.isoformat()
    apply_traffic(cities, measured, events_traffic(bison_src.pop("events"), sytadin), observed)
    apply_vigilance(cities, vig)
    return {
        "version": 3,
        "generatedAt": start.isoformat(),
        "nextUpdateAt": (start + timedelta(hours=6)).isoformat(),
        "cities": cities,
        "regions": summarize_regions(cities, vig),
        "national": summarize_national(cities, vig),
        "highways": highways,
        "_meta": {
            "sources": [weather_src, vig_src, tc_src, bison_src, syt_src],
            "cityCount": len(cities),
            "regionCount": len(REGIONS),
            "hourlyForecastHours": 36,
            "attribution": "Météo : Open-Meteo (CC BY 4.0) · Vigilance : Météo-France (Licence Ouverte 2.0) · "
                           "Trafic : Bison Futé / DIR (Licence Ouverte 2.0)",
            "trafficNote": "traffic présent seulement si mesuré ou signalé ; absent = inconnu ; "
                           "l'app ignore un état de plus de 90 min",
            "nextUpdateNote": "GitHub scheduled workflows are throttled; expect 2-6 h between publishes",
        },
    }


def main() -> int:
    t0 = datetime.now(timezone.utc)
    print(f"=== okomox-data-bridge collect @ {t0.isoformat()} ===")
    payload = build_payload()
    out_dir = Path(__file__).parent / "data"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / "data.json"
    out_file.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    secs = (datetime.now(timezone.utc) - t0).total_seconds()
    print(f"\n✅ {out_file} ({out_file.stat().st_size} octets) en {secs:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
