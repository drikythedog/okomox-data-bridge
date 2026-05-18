# okomox-data-bridge

Passerelle de données météo et trafic pour l'application Okomox.

## 🎯 Mission

Collecter automatiquement les conditions météo et trafic de **14 grandes villes françaises** + autoroutes (Bison Futé) + IDF (Sytadin), agréger dans un fichier JSON unique, le publier toutes les **30 minutes** sur GitHub Pages et sur `https://okomox.site/data.json`.

L'application Okomox Android télécharge ce JSON 3×/jour (04h30 / 12h00 / 22h30) pour alimenter le calculateur V8.2 (`RideCalcContext.weather` + `.traffic`).

## 🏗️ Architecture

```
APIs publiques (Open-Meteo + Bison Futé RSS + Sytadin)
        ↓ (CRON GitHub Actions toutes les 30 min)
collect.py exécute
        ↓
data/data.json généré
        ↓
Push GitHub + Upload FTP automatique
        ↓
https://drikythedog.github.io/okomox-data-bridge/data.json  (canonical)
https://okomox.site/data.json                                (alias FTP LWS)
        ↓
App Okomox Android pull 3×/jour → cache SharedPreferences
        ↓
Calculateur V8.2 → verdict ajusté selon météo + trafic
```

## 📊 Format JSON

```json
{
  "version": 1,
  "generatedAt": "2026-05-19T10:30:00Z",
  "nextUpdateAt": "2026-05-19T11:00:00Z",
  "cities": {
    "Paris":      { "weather": "DRY",        "temperatureC": 18, "traffic": "DENSE",   "lat": 48.8566, "lng": 2.3522 },
    "Lyon":       { "weather": "LIGHT_RAIN", "temperatureC": 14, "traffic": "NORMAL",  "lat": 45.7640, "lng": 4.8357 },
    ...
  },
  "highways": {
    "A1":  { "status": "DENSE",  "comment": "Travaux Roissy" },
    "A6":  { "status": "FLUID",  "comment": null },
    ...
  }
}
```

Enums alignés sur `app/src/main/java/com/okomox/app/calc/RideCalcContext.kt` :
- `WeatherCondition` : DRY / LIGHT_RAIN / HEAVY_RAIN / EXTREME
- `TrafficCondition` : FLUID / NORMAL / DENSE / BLOCKED

## 🆓 Coûts

| Composant | Coût |
|---|---|
| GitHub Actions (limite 2000 min/mois, on utilise ~50) | 0 € |
| GitHub Pages (hébergement statique) | 0 € |
| Open-Meteo API (gratuit illimité non-commercial) | 0 € |
| Bison Futé RSS officiel | 0 € |
| Sytadin RSS (IDF) | 0 € |
| LWS (déjà payé pour okomox.com) | 0 € |
| **Total** | **0 €** |

## 🛡️ Conformité Google Play (app Okomox)

- L'app **TÉLÉCHARGE** ces données publiques → **PAS de "data collection"**
- "No data collected" Play Store **PRÉSERVÉ**
- Permission `INTERNET` déjà déclarée Manifest

## 🚀 Setup

### GitHub Pages (canonical URL)
1. Settings → Pages → Source: `Deploy from a branch` → `main` / `data/`
2. URL canonical : `https://drikythedog.github.io/okomox-data-bridge/data.json`

### Upload FTP LWS (alias `okomox.site/data.json`) — optionnel
1. Activer domaine `okomox.site` dans panel LWS (5 min)
2. Ajouter secrets GitHub Actions : `FTP_HOST`, `FTP_USER`, `FTP_PASS`
3. Le workflow uploadera automatiquement à chaque mise à jour

## 📝 License

MIT — code public auditable (transparence des données collectées).
