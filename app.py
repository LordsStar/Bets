"""
Analista de Apuesta Única — v3.6

Requisitos (requirements.txt):
    streamlit
    pandas
    beautifulsoup4
    requests

Cambios v3.5 -> v3.6:
  [P0] Matching de nombres Odds API ↔ ClubElo (alias + normalización + fuzzy).
  [P0] Validación de suma de probabilidades ClubElo (±2%).
  [P0] Forebet desactivado por defecto (ENABLE_FOREBET=False); scrapers mejorados.
  [P1] Precálculo de EV% y divergencia en backend cuando hay 2º modelo.
  [P1] Umbrales versionados en código e inyectados en el prompt.
  [P1] Modo por deporte como default; conteo de eventos corregido.
  [P1] Tabla de candidatos pre-IA; aviso Gemini sin web search.
  [P2] Lista blanca opcional de deportes; max_uses en Claude web_search;
       log de fallos de matching ClubElo en session_state.
"""

import json
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from io import StringIO

import pandas as pd
import requests
import streamlit as st

# ==============================================================================
# CONSTANTES / UMBRALES (versionados en código, inyectados en el prompt)
# ==============================================================================
EV_MINIMO = 0.04          # 4%
DIVERGENCIA_MAXIMA = 0.09  # 9%
CONFIANZA_MINIMA = 8       # /10
CUOTA_MIN = 1.40
CUOTA_MAX = 2.00
VENTANA_HORAS_DEFAULT = 24

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
ANTHROPIC_API_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"

ODDS_YELLOW_THRESHOLD = 100
ODDS_RED_THRESHOLD = 20
CLAUDE_TOKEN_WARNING = 500_000
GEMINI_TOKEN_WARNING = 500_000
CLAUDE_WEB_SEARCH_MAX_USES = 8

# Forebet: desactivado por defecto (scraper frágil). Activa solo tras validar HTML.
ENABLE_FOREBET = False

CLUBELO_API_BASE = "http://api.clubelo.com"
FOREBET_URL = "https://www.forebet.com/en/football-tips-and-predictions-for-today"

# Lista blanca opcional: si no está vacía, en modo ALL solo se consultan estos
# sport_keys (o prefijos). Vacío = todos los activos.
SPORTS_WHITELIST_PREFIXES = []  # ej: ["soccer", "baseball_mlb", "tennis", "basketball_nba"]

REGISTRY_ULTIMA_REVISION = "2026-08-22"

# ==============================================================================
# 1. SYSTEM PROMPT V3.6 — BLINDADO
# ==============================================================================
SYSTEM_PROMPT_BLINDADO_V3_6 = f"""
PROMPT — Analista Cuantitativo de Apuesta Única (Blindado v3.6)

ROL Y OBJETIVO:
Actúa como Analista Cuantitativo de Deportes y Tipster Profesional. Tu objetivo es
seleccionar UNA sola apuesta —la de mayor confianza estadística— dentro de un rango
de cuota {CUOTA_MIN:.2f}-{CUOTA_MAX:.2f} (moneyline o mercado principal), de TODOS
los eventos recibidos.
Un informe con 0 picks es un resultado VÁLIDO y ESPERADO en la mayoría de los días.
Nunca fuerces un pick para "tener algo que mostrar".

METODOLOGÍA Y REGLAS CLAVE:

1. ANCLA OBLIGATORIA: Usa directamente el campo `_pinnacle_devig` que el backend ya
   calculó. No recalcules el de-vig.

1b. PRECÁLCULO BACKEND (nuevo v3.6):
   Si el evento trae `_backend_ev` y `_backend_divergencia`, USA ESOS NÚMEROS
   directamente. No los recalcules salvo que detectes un error manifiesto
   (entonces descríbelo y descarta). Si no traen esos campos, calcúlalos tú
   con la fórmula estándar:
     EV% = (prob_segundo_modelo * cuota_pinnacle) - 1
     Divergencia% = |prob_pinnacle_devig - prob_segundo_modelo|

2. GATE DE FRESCURA (relativo al tiempo restante, NO un umbral fijo):
   Pinnacle solo actualiza el precio cuando se mueve la línea. Si a un evento le
   faltan muchas horas para empezar, es NORMAL que `_pinnacle_last_update` tenga
   varias horas de antigüedad — eso NO es dato obsoleto, es un mercado tranquilo.
   Compara `_pinnacle_last_update` contra `inicio_utc`, no contra la hora actual:
   - Si al evento le faltan MENOS de 3 horas para empezar Y `_pinnacle_last_update`
     tiene más de 90 minutos de antigüedad → señal real de posible dato
     desactualizado cerca del cierre del mercado → DESCARTA el evento.
   - Si al evento le faltan MÁS de 3 horas, la antigüedad de `_pinnacle_last_update`
     es solo informativa: NO descartes el evento por este motivo.

3. VALIDACIÓN CRUZADA (Segundo Modelo) — EXCLUSIVAMENTE vía Model Registry:
   Cada evento trae `_registry_modelo_secundario` con la fuente autorizada para
   ese deporte específico. Reglas ESTRICTAS según el campo `cobertura`:

   - "externa_directa": debes REALIZAR la búsqueda web real en `fuente_primaria`
     (o `fuente_secundaria` si la primaria falla) ANTES de concluir que no se
     puede verificar. Prohibido responder "no se puede confirmar" sin haber
     intentado la búsqueda.

     >>> [A1] FIX OBLIGATORIO PARA FanGraphs (fuente primaria de béisbol) <<<
     Cuando `fuente_primaria` menciona FanGraphs, la consulta DEBE hacerse con
     fecha explícita en la URL:
         https://www.fangraphs.com/scores?date=YYYY-MM-DD
     usando la fecha derivada de `inicio_utc` del evento. NUNCA uses la URL
     sin el parámetro de fecha.

     >>> [A2] FIX OBLIGATORIO PARA ClubElo (fuente primaria de fútbol) <<<
     Si un evento de fútbol llega con `cobertura` = "externa_directa", y necesitas
     buscar ClubElo tú mismo, usa el subdominio API:
         http://api.clubelo.com/Fixtures
         http://api.clubelo.com/YYYY-MM-DD
     Nunca clubelo.com sin "api.".

     - RESPALDO DOCUMENTADO: si ni primaria ni secundaria exponen un número,
       revisa `fuente_respaldo`. Cítalo EXPLÍCITAMENTE como respaldo.
       Forebet: sin metodología pública verificable → baja el componente de
       calidad de fuente en Confianza.

     - Si NO hay `fuente_respaldo` y ninguna fuente oficial dio número →
       descarta categoría 2. PROHIBIDO improvisar fuentes no listadas.

   - "modelo_interno_elo": usa directamente `probabilidad_elo_home`, `elo_home`,
     `elo_away`, `brier_score_historico`, `muestras_brier`. Cita:
     "Modelo Elo interno (backend), calibrado con {{muestras_brier}} resultados
     reales, Brier histórico {{brier_score_historico}}".

   - "modelo_externo_backend": usa `probabilidad_home` / `probabilidad_draw` /
     `probabilidad_away`. Cita EXACTAMENTE `fuente_primaria` o `fuente_respaldo`.
     Si fue Forebet, incluye "(respaldo documentado, sin metodología pública
     verificable)" y baja calidad de fuente.

   - "pendiente_desarrollo": DESCARTA sin buscar ni improvisar.
   - "excluido_estructural": descarta sin análisis.

   PROHIBIDO ABSOLUTO: usar cualquier fuente no listada en
   `_registry_modelo_secundario`. En particular, "538 SPI" /
   "FiveThirtyEight SPI" queda PROHIBIDO (descontinuado desde 2023).

3b. CHEQUEO DE ESTADO FÍSICO — OBLIGATORIO PARA TENIS, BOXEO Y MMA:
   Búsqueda web adicional (últimas 48-72h) de lesión/retiro/molestia.
   Si hay noticia real → baja Confianza de forma explícita.
   Si no hay nada → decláralo explícitamente en el informe.

4. LIQUIDEZ: Usa `_liquidez_backend` tal cual. Menos de 2 casas → no califica.

5. UMBRALES DE DESCARTE (versionados en backend v3.6):
   - EV < {EV_MINIMO * 100:.0f}% → descartar.
   - Divergencia |Pinnacle - Segundo Modelo| > {DIVERGENCIA_MAXIMA * 100:.0f}% → descartar.
   - Si segundo modelo es "modelo_interno_elo" y brier > 0.23 o muestras < 8 → descartar.

6. CONFIANZA (1-10): desglose visible:
   - Edge estadístico (EV real vs umbral)
   - Calidad/frescura de la fuente del segundo modelo
   - Liquidez del mercado
   - Coherencia con movimiento de línea (si hay datos)
   - Tenis/boxeo/MMA: chequeo de estado físico
   Solo califica si confianza total >= {CONFIANZA_MINIMA}/10.

REGLAS ANTI-FABRICACIÓN:
- Nunca inventes lesiones, alineaciones, clima, cuotas, nombres o resultados.
- Si falta un dato necesario → descarta el evento.
- Cada afirmación estadística lleva fuente (nombre + URL o "Modelo Elo interno").
- Categorías 1/2/3: EV% y divergencia% = "N/A — no se calculó".
- `fuente_respaldo` solo tras intentar primaria (y secundaria) de verdad.

CATEGORIZACIÓN DE DESCARTES — MUTUAMENTE EXCLUYENTE (orden de prioridad):
   1º Gate de frescura
   2º Segundo modelo no disponible
   3º Liquidez insuficiente
   4º EV por debajo del umbral
   5º Divergencia por encima del umbral
   6º Confianza < {CONFIANZA_MINIMA}/10
Un evento NUNCA en dos categorías a la vez.

AUTO-VERIFICACIÓN:
Suma (eventos por categoría de descarte) + (1 si hay pick, 0 si no)
= EXACTAMENTE el número total de eventos del JSON de este prompt.

FORMATO DE SALIDA (obligatorio, en español):
1. Resumen: eventos evaluados + desglose por las 6 categorías + verificación de suma.
2. Si hay pick: Partido | Mercado | Cuota Pinnacle | Prob. implícita de-vigged |
   Prob. segundo modelo (fuente) | EV% | Confianza (desglose) | Justificación 3-4 líneas.
3. Si NO hay pick: "PICK DEL DÍA: NINGUNO" + explicación por categoría.
4. TABLA DE TRANSPARENCIA — solo categorías 4, 5 y 6:
   | Partido | Categoría | EV% | Divergencia% | Confianza | Motivo breve |
5. CASI CALIFICÓ: 1-3 eventos más cercanos al umbral (si hay datos reales).
"""

# ==============================================================================
# 1b. MODEL REGISTRY
# ==============================================================================
MODEL_REGISTRY = [
    {"patron": "americanfootball_nfl_preseason", "fuente_primaria": None, "fuente_secundaria": None,
     "cobertura": "excluido_estructural", "version": "1.0", "usa_elo_interno": False},
    {"patron": "soccer", "fuente_primaria": "ClubElo (api.clubelo.com/Fixtures)",
     "fuente_secundaria": None,
     "fuente_respaldo": "Forebet (sin metodología pública verificable)",
     "cobertura": "externa_directa", "version": "2.1", "usa_elo_interno": False},
    {"patron": "tennis", "fuente_primaria": "TennisAbstract (Elo por superficie)",
     "fuente_secundaria": "Ranking oficial ATP/WTA", "cobertura": "externa_directa", "version": "1.0",
     "usa_elo_interno": False},
    {"patron": "baseball_mlb", "fuente_primaria": "FanGraphs (usar SIEMPRE ?date=YYYY-MM-DD)",
     "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)",
     "cobertura": "externa_directa", "version": "1.1", "usa_elo_interno": False},
    {"patron": "baseball_kbo", "fuente_primaria": None, "fuente_secundaria": None,
     "cobertura": "pendiente_desarrollo", "version": "1.0", "usa_elo_interno": True},
    {"patron": "baseball_npb", "fuente_primaria": None, "fuente_secundaria": None,
     "cobertura": "pendiente_desarrollo", "version": "1.0", "usa_elo_interno": True},
    {"patron": "basketball_nba", "fuente_primaria": "Basketball-Reference", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)",
     "cobertura": "externa_directa", "version": "1.0", "usa_elo_interno": False},
    {"patron": "basketball_wnba", "fuente_primaria": "Basketball-Reference", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)",
     "cobertura": "externa_directa", "version": "1.0", "usa_elo_interno": False},
    {"patron": "basketball_ncaab", "fuente_primaria": "Basketball-Reference (NCAA)", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)",
     "cobertura": "externa_directa", "version": "1.0", "usa_elo_interno": False},
    {"patron": "icehockey_nhl", "fuente_primaria": "Hockey-Reference", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)",
     "cobertura": "externa_directa", "version": "1.0", "usa_elo_interno": False},
    {"patron": "cricket", "fuente_primaria": "ICC Team Ratings", "fuente_secundaria": "ESPN Cricinfo",
     "cobertura": "externa_directa", "version": "1.0", "usa_elo_interno": False},
    {"patron": "boxing", "fuente_primaria": "BoxRec ratings", "fuente_secundaria": None,
     "cobertura": "externa_directa", "version": "1.0", "usa_elo_interno": False},
    {"patron": "mma", "fuente_primaria": None, "fuente_secundaria": None,
     "cobertura": "pendiente_desarrollo", "version": "1.0", "usa_elo_interno": True},
    {"patron": "americanfootball_nfl", "fuente_primaria": "ESPN FPI (Football Power Index)",
     "fuente_secundaria": None, "cobertura": "externa_directa", "version": "1.0", "usa_elo_interno": False},
]

DEFAULT_REGISTRY_ENTRY = {
    "fuente_primaria": None, "fuente_secundaria": None,
    "cobertura": "pendiente_desarrollo", "version": "0.0", "usa_elo_interno": True,
}


def _buscar_base_registry(sport_key):
    if not sport_key:
        return dict(DEFAULT_REGISTRY_ENTRY)
    sport_key_low = sport_key.lower()
    return dict(next((e for e in MODEL_REGISTRY if e["patron"] in sport_key_low), DEFAULT_REGISTRY_ENTRY))


# ==============================================================================
# 1c. MOTOR ELO INTERNO
# ==============================================================================
try:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    _BASE_DIR = os.getcwd()
ELO_STATE_FILE = os.path.join(_BASE_DIR, "elo_state.json")
ELO_INICIAL = 1500.0
ELO_K_FACTOR = 20.0
ELO_VENTAJA_LOCAL = 50.0
ELO_MIN_PARTIDOS_POR_EQUIPO = 5
ELO_MIN_MUESTRAS_BRIER = 8
ELO_BRIER_MAXIMO_ACEPTABLE = 0.23
ELO_DIAS_HISTORIAL_SCORES = 3


def cargar_estado_elo():
    if os.path.exists(ELO_STATE_FILE):
        try:
            with open(ELO_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"ratings": {}, "procesados": {}, "historial_brier": {}}


def guardar_estado_elo(estado):
    try:
        with open(ELO_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(estado, f, ensure_ascii=False, indent=2)
    except Exception as e:
        st.warning(f"No se pudo guardar el estado del motor Elo interno: {e}")


def _prob_elo(elo_a, elo_b):
    return 1.0 / (1.0 + 10 ** ((elo_b - elo_a) / 400.0))


def calcular_brier(historial_sport):
    if not historial_sport:
        return None
    return sum((h["prob"] - h["resultado"]) ** 2 for h in historial_sport) / len(historial_sport)


@st.cache_data(ttl=3600, show_spinner=False)
def obtener_scores_api(api_key, sport_key, dias=ELO_DIAS_HISTORIAL_SCORES):
    url = f"{ODDS_API_BASE}/sports/{sport_key}/scores/"
    params = {"apiKey": api_key, "daysFrom": dias}
    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code in (401, 422, 429):
            return []
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


def actualizar_elo_sport(api_key, sport_key, estado):
    resultados = obtener_scores_api(api_key, sport_key)
    if not resultados:
        return estado

    ratings = estado["ratings"].setdefault(sport_key, {})
    procesados = set(estado["procesados"].setdefault(sport_key, []))
    historial = estado["historial_brier"].setdefault(sport_key, [])

    for evento in resultados:
        if not isinstance(evento, dict) or not evento.get("completed"):
            continue
        game_id = evento.get("id")
        if not game_id or game_id in procesados:
            continue

        home_team = evento.get("home_team")
        away_team = evento.get("away_team")
        scores = evento.get("scores")
        if not home_team or not away_team or not scores:
            continue

        try:
            score_home = next(float(s["score"]) for s in scores if s.get("name") == home_team)
            score_away = next(float(s["score"]) for s in scores if s.get("name") == away_team)
        except (StopIteration, ValueError, TypeError, KeyError):
            continue

        if score_home == score_away:
            resultado_home = 0.5
        else:
            resultado_home = 1.0 if score_home > score_away else 0.0

        home = ratings.setdefault(home_team, {"elo": ELO_INICIAL, "partidos": 0})
        away = ratings.setdefault(away_team, {"elo": ELO_INICIAL, "partidos": 0})

        # MMA y deportes sin local claro: sin ventaja local
        ventaja = 0.0 if sport_key and "mma" in sport_key.lower() else ELO_VENTAJA_LOCAL
        prob_home_pre = _prob_elo(home["elo"] + ventaja, away["elo"])

        home["elo"] += ELO_K_FACTOR * (resultado_home - prob_home_pre)
        away["elo"] += ELO_K_FACTOR * ((1 - resultado_home) - (1 - prob_home_pre))
        home["partidos"] += 1
        away["partidos"] += 1

        historial.append({"prob": prob_home_pre, "resultado": resultado_home})
        procesados.add(game_id)

    estado["procesados"][sport_key] = list(procesados)
    return estado


def obtener_entrada_modelo_interno(estado, sport_key, home_team, away_team):
    ratings = estado.get("ratings", {}).get(sport_key, {})
    home = ratings.get(home_team)
    away = ratings.get(away_team)
    historial = estado.get("historial_brier", {}).get(sport_key, [])
    brier = calcular_brier(historial)

    calidad_suficiente = (
        home is not None and away is not None
        and home["partidos"] >= ELO_MIN_PARTIDOS_POR_EQUIPO
        and away["partidos"] >= ELO_MIN_PARTIDOS_POR_EQUIPO
        and brier is not None
        and len(historial) >= ELO_MIN_MUESTRAS_BRIER
        and brier <= ELO_BRIER_MAXIMO_ACEPTABLE
    )
    if not calidad_suficiente:
        return None

    ventaja = 0.0 if sport_key and "mma" in sport_key.lower() else ELO_VENTAJA_LOCAL
    prob_home = _prob_elo(home["elo"] + ventaja, away["elo"])
    return {
        "fuente_primaria": "Modelo Elo interno (backend, calculado desde resultados reales vía Odds API /scores)",
        "fuente_secundaria": None,
        "cobertura": "modelo_interno_elo",
        "version": "1.1",
        "ultima_revision": REGISTRY_ULTIMA_REVISION,
        "probabilidad_elo_home": round(prob_home, 4),
        "elo_home": round(home["elo"], 1),
        "elo_away": round(away["elo"], 1),
        "partidos_calificados_home": home["partidos"],
        "partidos_calificados_away": away["partidos"],
        "brier_score_historico": round(brier, 4),
        "muestras_brier": len(historial),
    }


# ==============================================================================
# 1d. CLUBELO — matching de nombres + fixtures
# ==============================================================================

# Mapa Odds API (o variantes comunes) → nombre ClubElo canónico.
# Ampliar según logs de fallos en session_state["clubelo_match_failures"].
CLUBELO_NAME_ALIASES = {
    # Premier League / Inglaterra
    "manchester united": "Man United",
    "manchester city": "Man City",
    "tottenham hotspur": "Tottenham",
    "tottenham": "Tottenham",
    "wolverhampton wanderers": "Wolves",
    "wolverhampton": "Wolves",
    "nottingham forest": "Forest",
    "brighton and hove albion": "Brighton",
    "brighton & hove albion": "Brighton",
    "west ham united": "West Ham",
    "newcastle united": "Newcastle",
    "leicester city": "Leicester",
    "leeds united": "Leeds",
    "aston villa": "Aston Villa",
    "crystal palace": "Crystal Palace",
    "sheffield united": "Sheffield United",
    "ipswich town": "Ipswich",
    "southampton": "Southampton",
    "fulham": "Fulham",
    "brentford": "Brentford",
    "bournemouth": "Bournemouth",
    "afc bournemouth": "Bournemouth",
    "everton": "Everton",
    "chelsea": "Chelsea",
    "arsenal": "Arsenal",
    "liverpool": "Liverpool",
    # España
    "atletico madrid": "Atletico",
    "atlético madrid": "Atletico",
    "atletico de madrid": "Atletico",
    "real madrid": "Real Madrid",
    "barcelona": "Barcelona",
    "fc barcelona": "Barcelona",
    "real sociedad": "Sociedad",
    "athletic club": "Athletic",
    "athletic bilbao": "Athletic",
    "real betis": "Betis",
    "villarreal": "Villarreal",
    "sevilla": "Sevilla",
    "valencia": "Valencia",
    "osasuna": "Osasuna",
    "getafe": "Getafe",
    "girona": "Girona",
    "celta de vigo": "Celta",
    "celta vigo": "Celta",
    "rayo vallecano": "Rayo Vallecano",
    "mallorca": "Mallorca",
    "las palmas": "Las Palmas",
    "alaves": "Alaves",
    "deportivo alaves": "Alaves",
    "leganes": "Leganes",
    "espanyol": "Espanyol",
    "real valladolid": "Valladolid",
    # Italia
    "inter": "Inter",
    "inter milan": "Inter",
    "internazionale": "Inter",
    "ac milan": "Milan",
    "milan": "Milan",
    "juventus": "Juventus",
    "ssc napoli": "Napoli",
    "napoli": "Napoli",
    "as roma": "Roma",
    "roma": "Roma",
    "lazio": "Lazio",
    "atalanta": "Atalanta",
    "fiorentina": "Fiorentina",
    "torino": "Torino",
    "bologna": "Bologna",
    "genoa": "Genoa",
    "udinese": "Udinese",
    "sassuolo": "Sassuolo",
    "cagliari": "Cagliari",
    "empoli": "Empoli",
    "monza": "Monza",
    "lecce": "Lecce",
    "verona": "Verona",
    "hellas verona": "Verona",
    "parma": "Parma",
    "como": "Como",
    "venezia": "Venezia",
    # Alemania
    "bayern munich": "Bayern",
    "bayern münchen": "Bayern",
    "fc bayern munich": "Bayern",
    "borussia dortmund": "Dortmund",
    "bayer leverkusen": "Leverkusen",
    "rb leipzig": "Leipzig",
    "eintracht frankfurt": "Frankfurt",
    "borussia monchengladbach": "Gladbach",
    "borussia mönchengladbach": "Gladbach",
    "vfb stuttgart": "Stuttgart",
    "wolfsburg": "Wolfsburg",
    "werder bremen": "Werder",
    "union berlin": "Union Berlin",
    "1. fc union berlin": "Union Berlin",
    "mainz 05": "Mainz",
    "fsv mainz 05": "Mainz",
    "fc augsburg": "Augsburg",
    "augsburg": "Augsburg",
    "hoffenheim": "Hoffenheim",
    "tsg hoffenheim": "Hoffenheim",
    "sc freiburg": "Freiburg",
    "freiburg": "Freiburg",
    "1. fc heidenheim": "Heidenheim",
    "heidenheim": "Heidenheim",
    "fc st. pauli": "St Pauli",
    "st. pauli": "St Pauli",
    "holstein kiel": "Holstein",
    # Francia
    "paris saint germain": "Paris SG",
    "paris saint-germain": "Paris SG",
    "psg": "Paris SG",
    "olympique de marseille": "Marseille",
    "marseille": "Marseille",
    "olympique lyonnais": "Lyon",
    "lyon": "Lyon",
    "as monaco": "Monaco",
    "monaco": "Monaco",
    "lille": "Lille",
    "losc lille": "Lille",
    "nice": "Nice",
    "ogc nice": "Nice",
    "rennes": "Rennes",
    "stade rennais": "Rennes",
    "lens": "Lens",
    "rc lens": "Lens",
    "nantes": "Nantes",
    "strasbourg": "Strasbourg",
    "toulouse": "Toulouse",
    "brest": "Brest",
    "stade brestois": "Brest",
    "reims": "Reims",
    "montpellier": "Montpellier",
    "auxerre": "Auxerre",
    "angers": "Angers",
    "le havre": "Le Havre",
    "saint-etienne": "Saint-Etienne",
    "saint etienne": "Saint-Etienne",
    # Otros europeos frecuentes
    "ajax": "Ajax",
    "psv": "PSV",
    "psv eindhoven": "PSV",
    "feyenoord": "Feyenoord",
    "benfica": "Benfica",
    "sl benfica": "Benfica",
    "porto": "Porto",
    "fc porto": "Porto",
    "sporting cp": "Sporting",
    "sporting lisbon": "Sporting",
    "celtic": "Celtic",
    "rangers": "Rangers",
    "galatasaray": "Galatasaray",
    "fenerbahce": "Fenerbahce",
    "besiktas": "Besiktas",
    "olympiacos": "Olympiakos",
    "olympiakos": "Olympiakos",
    "shakhtar donetsk": "Shakhtar",
    "dynamo kyiv": "Dynamo Kyiv",
    "red bull salzburg": "Salzburg",
    "fc salzburg": "Salzburg",
    "young boys": "Young Boys",
    "club brugge": "Club Brugge",
    "anderlecht": "Anderlecht",
}


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def _normalizar_nombre(nombre: str) -> str:
    if not nombre:
        return ""
    s = _strip_accents(nombre).lower().strip()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # quitar sufijos legales frecuentes
    for tok in (" fc", " cf", " afc", " sc", " ac", " as", " fk", " nk", " bk", " if"):
        if s.endswith(tok):
            s = s[: -len(tok)].strip()
    return s


def _tokens(nombre: str) -> set:
    stop = {"fc", "cf", "afc", "sc", "ac", "as", "the", "de", "club", "united", "city"}
    return {t for t in _normalizar_nombre(nombre).split() if t and t not in stop}


def resolver_nombre_clubelo(nombre_odds: str, nombres_clubelo_disponibles=None):
    """
    Resuelve un nombre de The Odds API al spelling de ClubElo.
    1) Alias exacto
    2) Match exacto normalizado contra lista de ClubElo
    3) Fuzzy por tokens (Jaccard >= 0.6) si hay lista disponible
    Devuelve (nombre_resuelto_o_None, metodo).
    """
    if not nombre_odds:
        return None, "vacio"

    key = _normalizar_nombre(nombre_odds)
    if key in CLUBELO_NAME_ALIASES:
        return CLUBELO_NAME_ALIASES[key], "alias"

    # Alias parcial: si alguna key está contenida
    for alias_key, canon in CLUBELO_NAME_ALIASES.items():
        if alias_key in key or key in alias_key:
            return canon, "alias_parcial"

    if nombres_clubelo_disponibles:
        norm_map = {_normalizar_nombre(n): n for n in nombres_clubelo_disponibles}
        if key in norm_map:
            return norm_map[key], "exacto_normalizado"

        tok_query = _tokens(nombre_odds)
        best, best_score = None, 0.0
        for norm_n, original in norm_map.items():
            tok_n = _tokens(original)
            if not tok_query or not tok_n:
                continue
            inter = len(tok_query & tok_n)
            union = len(tok_query | tok_n)
            score = inter / union if union else 0.0
            if score > best_score:
                best_score = score
                best = original
        if best is not None and best_score >= 0.6:
            return best, f"fuzzy_{best_score:.2f}"

    return None, "sin_match"


def _log_clubelo_failure(home, away, reason):
    fails = st.session_state.setdefault("clubelo_match_failures", [])
    entry = f"{home} vs {away} — {reason}"
    if entry not in fails:
        fails.append(entry)
        # mantener solo los últimos 50
        st.session_state["clubelo_match_failures"] = fails[-50:]


@st.cache_data(ttl=3600, show_spinner=False)
def obtener_fixtures_clubelo():
    try:
        r = requests.get(f"{CLUBELO_API_BASE}/Fixtures", timeout=12)
        r.raise_for_status()
        texto = r.text.strip()
        if not texto or texto.lower().startswith("site overloaded"):
            return None
        df = pd.read_csv(StringIO(r.text))
        return df if not df.empty else None
    except Exception:
        return None


def _es_columna_gd(nombre_columna):
    """Columnas de diferencia de gol: enteros (posiblemente como string)."""
    try:
        int(str(nombre_columna).strip())
        return True
    except (ValueError, TypeError):
        return False


def _prob_desde_fixtures_clubelo(df, home_team, away_team):
    """
    Busca partido en /Fixtures y agrega probs Home/Draw/Away.
    Valida que sumen ≈ 1.0. Usa matching de nombres robusto.
    """
    if df is None or df.empty:
        return None
    if "Home" not in df.columns or "Away" not in df.columns:
        return None

    nombres_home = df["Home"].dropna().astype(str).unique().tolist()
    nombres_away = df["Away"].dropna().astype(str).unique().tolist()
    todos = list(set(nombres_home + nombres_away))

    home_resuelto, metodo_h = resolver_nombre_clubelo(home_team, todos)
    away_resuelto, metodo_a = resolver_nombre_clubelo(away_team, todos)

    if not home_resuelto or not away_resuelto:
        _log_clubelo_failure(
            home_team, away_team,
            f"match fallido (home={metodo_h}, away={metodo_a})"
        )
        return None

    try:
        match = df[
            (df["Home"].astype(str).str.strip().str.lower() == home_resuelto.strip().lower())
            & (df["Away"].astype(str).str.strip().str.lower() == away_resuelto.strip().lower())
        ]
        if match.empty:
            # intentar sin invertir: a veces Odds API invierte home/away vs ClubElo
            match = df[
                (df["Home"].astype(str).str.strip().str.lower() == away_resuelto.strip().lower())
                & (df["Away"].astype(str).str.strip().str.lower() == home_resuelto.strip().lower())
            ]
            invertido = not match.empty
        else:
            invertido = False

        if match.empty:
            _log_clubelo_failure(
                home_team, away_team,
                f"resueltos a '{home_resuelto}'/'{away_resuelto}' pero no en Fixtures"
            )
            return None

        row = match.iloc[0]
        cols_gd = [c for c in df.columns if _es_columna_gd(c)]
        if not cols_gd:
            return None

        prob_home = 0.0
        prob_away = 0.0
        prob_draw = 0.0
        for c in cols_gd:
            try:
                val = float(row[c])
                gd = int(str(c).strip())
            except (ValueError, TypeError):
                continue
            if gd > 0:
                prob_home += val
            elif gd < 0:
                prob_away += val
            else:
                prob_draw += val

        if invertido:
            prob_home, prob_away = prob_away, prob_home

        total = prob_home + prob_draw + prob_away
        if total <= 0 or abs(total - 1.0) > 0.02:
            _log_clubelo_failure(
                home_team, away_team,
                f"probs no suman 1 (suma={total:.4f})"
            )
            return None

        # renormalizar por si hay ruido numérico leve
        prob_home /= total
        prob_draw /= total
        prob_away /= total

        return {
            "prob_home": round(float(prob_home), 4),
            "prob_draw": round(float(prob_draw), 4),
            "prob_away": round(float(prob_away), 4),
            "clubelo_home": home_resuelto,
            "clubelo_away": away_resuelto,
            "match_method": f"{metodo_h}/{metodo_a}",
        }
    except Exception as e:
        _log_clubelo_failure(home_team, away_team, f"excepción: {e}")
        return None


def obtener_prediccion_forebet(home_team, away_team):
    """Respaldo opcional. ENABLE_FOREBET debe ser True. Scraper frágil."""
    if not ENABLE_FOREBET:
        return None
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None

    try:
        r = requests.get(
            FOREBET_URL, timeout=12,
            headers={"User-Agent": "Mozilla/5.0 (compatible; BlindadoBot/3.6)"},
        )
        r.raise_for_status()
    except Exception:
        return None

    try:
        soup = BeautifulSoup(r.text, "html.parser")
        # selectores observados en scrapers públicos (pueden romperse)
        filas = soup.select("div.rcnt") or soup.select(".rcnt")
        home_n = _normalizar_nombre(home_team)
        away_n = _normalizar_nombre(away_team)

        for fila in filas:
            home_el = fila.select_one(".homeTeam") or fila.select_one("span.homeTeam")
            away_el = fila.select_one(".awayTeam") or fila.select_one("span.awayTeam")
            if not home_el or not away_el:
                texto = fila.get_text(" ", strip=True).lower()
                if home_n not in _normalizar_nombre(texto) or away_n not in _normalizar_nombre(texto):
                    continue
            else:
                if (_normalizar_nombre(home_el.get_text()) != home_n
                        and home_n not in _normalizar_nombre(home_el.get_text())):
                    continue
                if (_normalizar_nombre(away_el.get_text()) != away_n
                        and away_n not in _normalizar_nombre(away_el.get_text())):
                    continue

            # probs: varios layouts históricos
            probs = []
            fprc = fila.select_one(".fprc")
            if fprc:
                spans = fprc.find_all("span")
                probs = [s.get_text(strip=True).replace("%", "") for s in spans[:3]]
            if len(probs) < 3:
                for sel in (".prc_1", ".prc_X", ".prc_2"):
                    el = fila.select_one(sel)
                    if el:
                        probs.append(el.get_text(strip=True).replace("%", ""))
            if len(probs) < 3:
                continue
            try:
                ph, pd_, pa = [float(x) / 100.0 for x in probs[:3]]
            except ValueError:
                continue
            if abs(ph + pd_ + pa - 1.0) > 0.05:
                continue
            return {
                "prob_home": round(ph, 4),
                "prob_draw": round(pd_, 4),
                "prob_away": round(pa, 4),
            }
    except Exception:
        return None
    return None


def obtener_entrada_clubelo_o_forebet(sport_key, home_team, away_team):
    if not sport_key or not sport_key.startswith("soccer") or not home_team or not away_team:
        return None

    fixtures = obtener_fixtures_clubelo()
    resultado_clubelo = _prob_desde_fixtures_clubelo(fixtures, home_team, away_team)
    if resultado_clubelo:
        return {
            "fuente_primaria": "ClubElo (api.clubelo.com/Fixtures) — resuelto directo por backend",
            "fuente_secundaria": None,
            "fuente_respaldo": "Forebet (sin metodología pública verificable)",
            "cobertura": "modelo_externo_backend",
            "version": "1.1",
            "ultima_revision": REGISTRY_ULTIMA_REVISION,
            "probabilidad_home": resultado_clubelo["prob_home"],
            "probabilidad_draw": resultado_clubelo["prob_draw"],
            "probabilidad_away": resultado_clubelo["prob_away"],
            "clubelo_match": resultado_clubelo.get("match_method"),
            "clubelo_nombres": f"{resultado_clubelo.get('clubelo_home')} vs {resultado_clubelo.get('clubelo_away')}",
        }

    if ENABLE_FOREBET:
        resultado_forebet = obtener_prediccion_forebet(home_team, away_team)
        if resultado_forebet:
            return {
                "fuente_primaria": "ClubElo (api.clubelo.com/Fixtures) — sin dato para este partido",
                "fuente_secundaria": None,
                "fuente_respaldo": (
                    "Forebet (respaldo documentado, sin metodología pública verificable) "
                    "— resuelto directo por backend"
                ),
                "cobertura": "modelo_externo_backend",
                "version": "1.1",
                "ultima_revision": REGISTRY_ULTIMA_REVISION,
                "probabilidad_home": resultado_forebet["prob_home"],
                "probabilidad_draw": resultado_forebet["prob_draw"],
                "probabilidad_away": resultado_forebet["prob_away"],
                "metodologia_publica_respaldo": False,
            }

    return None


def obtener_entrada_registry(sport_key, home_team=None, away_team=None, estado_elo=None):
    base = _buscar_base_registry(sport_key)

    if sport_key and sport_key.startswith("soccer") and home_team and away_team:
        resuelto_backend = obtener_entrada_clubelo_o_forebet(sport_key, home_team, away_team)
        if resuelto_backend:
            return resuelto_backend

    if (
        base.get("usa_elo_interno")
        and base["cobertura"] == "pendiente_desarrollo"
        and estado_elo is not None
        and home_team and away_team
    ):
        interno = obtener_entrada_modelo_interno(estado_elo, sport_key, home_team, away_team)
        if interno:
            return interno
        ratings = estado_elo.get("ratings", {}).get(sport_key, {})
        partidos_home = ratings.get(home_team, {}).get("partidos", 0)
        partidos_away = ratings.get(away_team, {}).get("partidos", 0)
        base["nota"] = (
            f"Motor Elo interno activo pero con historial insuficiente todavía "
            f"({home_team}: {partidos_home} partidos, {away_team}: {partidos_away} partidos, "
            f"mínimo requerido: {ELO_MIN_PARTIDOS_POR_EQUIPO})."
        )

    base["ultima_revision"] = REGISTRY_ULTIMA_REVISION
    return base


# ==============================================================================
# 2. BACKEND — Odds API + enriquecimiento + precálculo EV
# ==============================================================================

@st.cache_data(ttl=3600, show_spinner=False)
def obtener_deportes_activos(api_key):
    url = f"{ODDS_API_BASE}/sports/"
    try:
        response = requests.get(url, params={"apiKey": api_key}, timeout=10)
        if response.status_code == 401:
            st.error("❌ API Key de The Odds API inválida o vencida.")
            return []
        if response.status_code == 429:
            st.error("❌ Límite de requests alcanzado en The Odds API (429).")
            return []
        response.raise_for_status()
        return [s for s in response.json() if s.get("active") and not s.get("has_outrights")]
    except Exception as e:
        st.error(f"Error al obtener deportes desde la API: {e}")
        return []


@st.cache_data(ttl=90, show_spinner=False)
def obtener_cuotas_api(api_key, sport_key):
    url = f"{ODDS_API_BASE}/sports/{sport_key}/odds/"
    params = {
        "apiKey": api_key,
        "markets": "h2h",
        "bookmakers": "pinnacle,stake,betonlineag,bet365",
    }
    try:
        response = requests.get(url, params=params, timeout=10)
        restantes = response.headers.get("x-requests-remaining")
        usados = response.headers.get("x-requests-used")
        if restantes is not None:
            st.session_state["odds_api_uso"] = {"restantes": restantes, "usados": usados}

        if response.status_code == 401:
            st.error(f"❌ API Key inválida al consultar {sport_key}.")
            return []
        if response.status_code == 422:
            return []
        if response.status_code == 429:
            st.warning(f"⚠️ Rate limit alcanzado en {sport_key}, se omite este deporte.")
            return []
        response.raise_for_status()
        return response.json()
    except Exception:
        return []


def devig_probabilidades(outcomes):
    if not outcomes:
        return {}
    implicitas = {
        o["name"]: 1.0 / o["price"]
        for o in outcomes
        if isinstance(o, dict) and o.get("price") and o["price"] > 0
    }
    overround = sum(implicitas.values())
    if overround == 0:
        return {}
    return {nombre: round(p / overround, 4) for nombre, p in implicitas.items()}


def calcular_dispersion_mercado(evento):
    if not isinstance(evento, dict):
        return 0.0
    probs_por_resultado = {}
    for b in evento.get("bookmakers", []):
        if not isinstance(b, dict):
            continue
        h2h = next((m for m in b.get("markets", []) if isinstance(m, dict) and m.get("key") == "h2h"), None)
        if not h2h:
            continue
        devig = devig_probabilidades(h2h.get("outcomes", []))
        for nombre, prob in devig.items():
            probs_por_resultado.setdefault(nombre, []).append(prob)
    dispersiones = [
        max(vals) - min(vals) for vals in probs_por_resultado.values() if len(vals) >= 2
    ]
    return max(dispersiones) if dispersiones else 0.0


def precalcular_ev_y_divergencia(evento_minificado):
    """
    Si hay segundo modelo resuelto (backend), calcula EV y divergencia por outcome
    para la cuota Pinnacle correspondiente. La IA no debe recalcular esto.
    """
    registry = evento_minificado.get("_registry_modelo_secundario") or {}
    cobertura = registry.get("cobertura")
    cuotas = evento_minificado.get("cuotas_pinnacle") or {}
    devig = evento_minificado.get("_pinnacle_devig") or {}
    partido = evento_minificado.get("partido", "")
    partes = partido.split(" vs ")
    home_team = partes[0].strip() if len(partes) == 2 else None
    away_team = partes[1].strip() if len(partes) == 2 else None

    probs_modelo = {}
    if cobertura == "modelo_externo_backend":
        if home_team and registry.get("probabilidad_home") is not None:
            probs_modelo[home_team] = registry["probabilidad_home"]
        if away_team and registry.get("probabilidad_away") is not None:
            probs_modelo[away_team] = registry["probabilidad_away"]
        # Draw si aparece en cuotas
        for nombre in cuotas:
            if nombre.lower() in ("draw", "empate", "x") and registry.get("probabilidad_draw") is not None:
                probs_modelo[nombre] = registry["probabilidad_draw"]
    elif cobertura == "modelo_interno_elo":
        if home_team and registry.get("probabilidad_elo_home") is not None:
            probs_modelo[home_team] = registry["probabilidad_elo_home"]
            if away_team:
                probs_modelo[away_team] = round(1.0 - registry["probabilidad_elo_home"], 4)

    if not probs_modelo:
        return None

    detalle = {}
    mejor_ev = None
    mejor_outcome = None
    for nombre, cuota in cuotas.items():
        if not cuota or nombre not in probs_modelo:
            continue
        p_mod = probs_modelo[nombre]
        p_pin = devig.get(nombre)
        ev = (p_mod * float(cuota)) - 1.0
        div = abs(p_pin - p_mod) if p_pin is not None else None
        detalle[nombre] = {
            "prob_modelo": round(p_mod, 4),
            "prob_pinnacle_devig": round(p_pin, 4) if p_pin is not None else None,
            "cuota": cuota,
            "ev": round(ev, 4),
            "divergencia": round(div, 4) if div is not None else None,
        }
        if mejor_ev is None or ev > mejor_ev:
            mejor_ev = ev
            mejor_outcome = nombre

    if not detalle:
        return None

    return {
        "por_outcome": detalle,
        "mejor_outcome": mejor_outcome,
        "mejor_ev": round(mejor_ev, 4) if mejor_ev is not None else None,
        "mejor_divergencia": detalle.get(mejor_outcome, {}).get("divergencia"),
    }


def registrar_y_calcular_movimientos(eventos_minificados, deporte_key):
    if not eventos_minificados:
        return {}
    state_key = f"pinnacle_snapshot_{deporte_key}"
    movimientos = {}
    snapshot_actual = {}

    for ev in eventos_minificados:
        if not isinstance(ev, dict):
            continue
        ev_id = ev.get("id")
        matchup = ev.get("partido")
        prices = ev.get("cuotas_pinnacle", {})
        if ev_id and prices:
            snapshot_actual[ev_id] = {"matchup": matchup, "prices": prices}

    if state_key in st.session_state and isinstance(st.session_state[state_key], dict):
        snapshot_previo = st.session_state[state_key]
        for ev_id, data_curr in snapshot_actual.items():
            if ev_id in snapshot_previo:
                data_prev = snapshot_previo[ev_id]
                for team, price_curr in data_curr.get("prices", {}).items():
                    price_prev = data_prev.get("prices", {}).get(team)
                    if price_prev and price_prev != price_curr:
                        pct_change = round(((price_curr - price_prev) / price_prev) * 100, 2)
                        direccion = "subió" if pct_change > 0 else "bajó"
                        movimientos[f"{data_curr['matchup']} ({team})"] = (
                            f"Cuota cambió de {price_prev} a {price_curr} ({direccion} {abs(pct_change)}%)"
                        )

    st.session_state[state_key] = snapshot_actual
    return movimientos


def _pasa_whitelist(sport_key):
    if not SPORTS_WHITELIST_PREFIXES:
        return True
    if not sport_key:
        return False
    sk = sport_key.lower()
    return any(sk.startswith(p.lower()) or p.lower() in sk for p in SPORTS_WHITELIST_PREFIXES)


def filtrar_y_enriquecer(datos_crudos, estado_elo, horas_ventana=VENTANA_HORAS_DEFAULT):
    if not datos_crudos or not isinstance(datos_crudos, list):
        return [], "Backend pre-filtró 0 eventos (sin datos recibidos)."

    eventos_validos = []
    descartados_sin_pinnacle = 0
    descartados_fuera_de_rango = 0
    descartados_fecha = 0
    descartados_sin_fecha = 0
    descartados_exclusion_estructural = 0
    eventos_con_elo_interno = 0
    eventos_con_backend_directo = 0
    eventos_pendientes_desarrollo = 0
    eventos_con_ev_precalc = 0

    ahora_utc = datetime.now(timezone.utc)
    limite_utc = ahora_utc + timedelta(hours=horas_ventana)

    for evento in datos_crudos:
        if not isinstance(evento, dict):
            continue
        if not _pasa_whitelist(evento.get("sport_key")):
            continue

        home_team = evento.get("home_team")
        away_team = evento.get("away_team")
        registry_entry = obtener_entrada_registry(
            evento.get("sport_key"), home_team=home_team, away_team=away_team, estado_elo=estado_elo
        )
        if registry_entry["cobertura"] == "excluido_estructural":
            descartados_exclusion_estructural += 1
            continue

        commence_str = evento.get("commence_time")
        if not commence_str:
            descartados_sin_fecha += 1
            continue
        try:
            commence_dt = datetime.fromisoformat(commence_str.replace("Z", "+00:00"))
            if not (ahora_utc <= commence_dt <= limite_utc):
                descartados_fecha += 1
                continue
        except Exception:
            descartados_sin_fecha += 1
            continue

        pinnacle = next(
            (b for b in evento.get("bookmakers", []) if isinstance(b, dict) and b.get("key") == "pinnacle"),
            None,
        )
        if not pinnacle:
            descartados_sin_pinnacle += 1
            continue

        h2h = next(
            (m for m in pinnacle.get("markets", []) if isinstance(m, dict) and m.get("key") == "h2h"),
            None,
        )
        if not h2h:
            descartados_sin_pinnacle += 1
            continue

        outcomes = h2h.get("outcomes", [])
        en_rango = any(
            CUOTA_MIN <= o.get("price", 0) <= CUOTA_MAX for o in outcomes if isinstance(o, dict)
        )
        if not en_rango:
            descartados_fuera_de_rango += 1
            continue

        pinnacle_devig = devig_probabilidades(outcomes)
        n_bookmakers = len(evento.get("bookmakers", []))
        dispersion = calcular_dispersion_mercado(evento)

        if n_bookmakers >= 3 and dispersion < 0.05:
            liquidez = "Alta"
        elif n_bookmakers >= 2:
            liquidez = "Media"
        else:
            liquidez = "Media/Baja — evaluar según categoría de liga"

        cuotas_pinnacle = {o.get("name"): o.get("price") for o in outcomes if isinstance(o, dict)}

        if registry_entry["cobertura"] == "modelo_interno_elo":
            eventos_con_elo_interno += 1
        elif registry_entry["cobertura"] == "modelo_externo_backend":
            eventos_con_backend_directo += 1
        elif registry_entry["cobertura"] == "pendiente_desarrollo":
            eventos_pendientes_desarrollo += 1

        evento_minificado = {
            "id": evento.get("id"),
            "deporte": evento.get("sport_title") or evento.get("sport_key"),
            "sport_key": evento.get("sport_key"),
            "partido": f"{home_team} vs {away_team}",
            "inicio_utc": commence_str,
            "cuotas_pinnacle": cuotas_pinnacle,
            "_pinnacle_devig": pinnacle_devig,
            "_pinnacle_last_update": pinnacle.get("last_update"),
            "_liquidez_backend": liquidez,
            "_dispersion_max_entre_casas": round(dispersion, 4),
            "_n_casas_reportando": n_bookmakers,
            "_registry_modelo_secundario": registry_entry,
        }

        precalc = precalcular_ev_y_divergencia(evento_minificado)
        if precalc:
            evento_minificado["_backend_ev"] = precalc
            eventos_con_ev_precalc += 1

        eventos_validos.append(evento_minificado)

    resumen_filtro = (
        f"Backend pre-filtró {len(datos_crudos)} eventos: "
        f"{len(eventos_validos)} candidatos calificados (prx {horas_ventana}h, "
        f"cuota {CUOTA_MIN}-{CUOTA_MAX}), "
        f"{descartados_fecha} descartados por fecha fuera de ventana, "
        f"{descartados_sin_fecha} descartados por fecha faltante/ilegible, "
        f"{descartados_sin_pinnacle} descartados sin Pinnacle, "
        f"{descartados_fuera_de_rango} descartados fuera de rango de cuota, "
        f"{descartados_exclusion_estructural} excluidos estructuralmente. "
        f"De los {len(eventos_validos)} candidatos: {eventos_con_elo_interno} Elo interno, "
        f"{eventos_con_backend_directo} backend directo (ClubElo/Forebet), "
        f"{eventos_pendientes_desarrollo} sin segundo modelo, "
        f"{eventos_con_ev_precalc} con EV/divergencia precalculados."
    )
    return eventos_validos, resumen_filtro


# ==============================================================================
# 2b. MODO POR DEPORTE
# ==============================================================================

def familia_deporte(sport_key):
    if not sport_key:
        return "otros"
    return sport_key.split("_")[0]


def agrupar_eventos_por_familia(eventos):
    grupos = {}
    for ev in eventos:
        familia = familia_deporte(ev.get("sport_key"))
        grupos.setdefault(familia, []).append(ev)
    return grupos


def separar_ia_vs_automatico(eventos_familia):
    necesita_ia, automaticos = [], []
    for ev in eventos_familia:
        cobertura = ev.get("_registry_modelo_secundario", {}).get("cobertura")
        if cobertura in ("pendiente_desarrollo", "excluido_estructural"):
            automaticos.append(ev)
        else:
            necesita_ia.append(ev)
    return necesita_ia, automaticos


def resumen_automatico_grupo(familia, eventos_automaticos):
    if not eventos_automaticos:
        return None
    lineas = [
        f"**{familia.upper()}** — {len(eventos_automaticos)} evento(s), "
        f"0 tokens de IA (descarte automático, categoría 2):"
    ]
    for ev in eventos_automaticos:
        cobertura = ev.get("_registry_modelo_secundario", {}).get("cobertura")
        lineas.append(f"- {ev.get('partido')} ({ev.get('deporte')}) — {cobertura}")
    return "\n".join(lineas)


def construir_prompt_grupo(familia, eventos_grupo, seleccion_label, hora_rd, seccion_movimiento):
    return (
        f"{SYSTEM_PROMPT_BLINDADO_V3_6}\n\n"
        f"==================================================\n"
        f"CONTEXTO DE EJECUCIÓN DEL BACKEND (MODO POR DEPORTE)\n"
        f"==================================================\n"
        f"ÁMBITO GENERAL DE LA CORRIDA: {seleccion_label}\n"
        f"GRUPO ANALIZADO EN ESTE PROMPT: {familia.upper()} "
        f"({len(eventos_grupo)} evento(s))\n"
        f"HORA CONSULTA (RD/UTC-4): {hora_rd}\n\n"
        f"{seccion_movimiento}\n\n"
        f"NOTA: Solo eventos de familia '{familia}' con cobertura "
        f"'externa_directa', 'modelo_interno_elo' o 'modelo_externo_backend'. "
        f"Los 'pendiente_desarrollo'/'excluido_estructural' ya se reportaron "
        f"fuera de la IA (no cuentes categoría 2 de esos aquí).\n\n"
        f"INSTRUCCIÓN TÉCNICA: Usa `_pinnacle_devig`, `_pinnacle_last_update`, "
        f"`_liquidez_backend`, `_dispersion_max_entre_casas`, `_n_casas_reportando`, "
        f"`_registry_modelo_secundario` y, si existe, `_backend_ev` (EV y divergencia "
        f"ya calculados — no recalcules).\n\n"
        f"DATOS JSON PRE-FILTRADOS (familia '{familia}'):\n"
        f"{json.dumps(eventos_grupo, indent=2, ensure_ascii=False)}"
    )


def construir_prompts_por_deporte(eventos_filtrados, seleccion_label, hora_rd, seccion_movimiento):
    grupos = agrupar_eventos_por_familia(eventos_filtrados)
    prompts_por_grupo = {}
    eventos_por_grupo = {}
    resúmenes_automaticos = []

    for familia, eventos_familia in sorted(grupos.items()):
        necesita_ia, automaticos = separar_ia_vs_automatico(eventos_familia)
        if automaticos:
            resumen = resumen_automatico_grupo(familia, automaticos)
            if resumen:
                resúmenes_automaticos.append(resumen)
        if necesita_ia:
            prompts_por_grupo[familia] = construir_prompt_grupo(
                familia, necesita_ia, seleccion_label, hora_rd, seccion_movimiento
            )
            eventos_por_grupo[familia] = necesita_ia

    resumen_automatico_total = (
        "\n\n".join(resúmenes_automaticos)
        if resúmenes_automaticos
        else "Ningún evento cayó en descarte 100% automático en esta corrida."
    )
    return prompts_por_grupo, resumen_automatico_total, eventos_por_grupo


# ==============================================================================
# 3. GEMINI / CLAUDE
# ==============================================================================

@st.cache_data(ttl=1800, show_spinner=False)
def listar_modelos_gemini(gemini_api_key):
    url = f"{GEMINI_API_BASE}/models?key={gemini_api_key}"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        modelos = r.json().get("models", [])
        utilizables = []
        for m in modelos:
            nombre = m.get("name", "").replace("models/", "")
            metodos = m.get("supportedGenerationMethods", [])
            if "generateContent" in metodos and not any(
                x in nombre for x in ["image", "audio", "tts", "embedding", "live", "vision"]
            ):
                utilizables.append(nombre)
        return sorted(utilizables, reverse=True)
    except Exception as e:
        st.error(f"No se pudo obtener la lista de modelos de Gemini: {e}")
        return []


def llamar_gemini_rest(gemini_api_key, modelo, prompt_texto):
    url = f"{GEMINI_API_BASE}/models/{modelo}:generateContent"
    headers = {"x-goog-api-key": gemini_api_key, "Content-Type": "application/json"}
    body = {"contents": [{"parts": [{"text": prompt_texto}]}]}
    r = requests.post(url, headers=headers, json=body, timeout=90)
    r.raise_for_status()
    data = r.json()
    partes = data["candidates"][0]["content"]["parts"]
    texto = "".join(p.get("text", "") for p in partes)
    uso = data.get("usageMetadata", {})
    return texto, uso


@st.cache_data(ttl=1800, show_spinner=False)
def listar_modelos_claude(anthropic_api_key):
    url = f"{ANTHROPIC_API_BASE}/models"
    headers = {"x-api-key": anthropic_api_key, "anthropic-version": ANTHROPIC_VERSION}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        modelos = r.json().get("data", [])
        return [m.get("id") for m in modelos if m.get("id")]
    except Exception as e:
        st.error(f"No se pudo obtener la lista de modelos de Claude: {e}")
        return []


def llamar_claude_rest(anthropic_api_key, modelo, prompt_texto, max_tokens=8000):
    url = f"{ANTHROPIC_API_BASE}/messages"
    headers = {
        "x-api-key": anthropic_api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    body = {
        "model": modelo,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt_texto}],
        "tools": [{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": CLAUDE_WEB_SEARCH_MAX_USES,
        }],
    }
    r = requests.post(url, headers=headers, json=body, timeout=120)
    r.raise_for_status()
    data = r.json()

    partes_texto = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    fuentes_buscadas = [
        b.get("input", {}).get("query")
        for b in data.get("content", [])
        if b.get("type") == "server_tool_use" and b.get("name") == "web_search"
    ]
    texto_final = "\n\n".join(partes_texto)
    queries = [q for q in fuentes_buscadas if q]

    uso = data.get("usage", {})
    ratelimit = {
        "requests_restantes": r.headers.get("anthropic-ratelimit-requests-remaining"),
        "tokens_restantes": r.headers.get("anthropic-ratelimit-tokens-remaining"),
        "tokens_limite": r.headers.get("anthropic-ratelimit-tokens-limit"),
        "reset": r.headers.get("anthropic-ratelimit-tokens-reset"),
    }
    return texto_final, queries, uso, ratelimit


# ==============================================================================
# 4. INTERFAZ
# ==============================================================================

st.set_page_config(page_title="Analista Cuantitativo de Apuestas", layout="wide")
st.title(
    "📊 Analista de Apuesta Única v3.6 "
    "(Multi-IA · Elo Interno · ClubElo Matching · EV Precalc · Modo por Deporte)"
)

with st.sidebar:
    st.header("🔑 Configuración de APIs")
    api_key = st.secrets.get("ODDS_API_KEY", "")
    if not api_key:
        api_key = st.text_input("Odds API Key:", type="password")

    gemini_api_key = st.secrets.get("GEMINI_API_KEY", "")
    if not gemini_api_key:
        gemini_api_key = st.text_input("Gemini API Key (Opcional):", type="password")

    anthropic_api_key = st.secrets.get("ANTHROPIC_API_KEY", "")
    if not anthropic_api_key:
        anthropic_api_key = st.text_input("Anthropic (Claude) API Key (Opcional):", type="password")

    st.divider()
    st.subheader("📊 Consumo de APIs")

    if "odds_api_uso" in st.session_state:
        uso = st.session_state["odds_api_uso"]
        try:
            restantes = int(uso["restantes"])
            if restantes > ODDS_YELLOW_THRESHOLD:
                icono = "🟢"
            elif restantes > ODDS_RED_THRESHOLD:
                icono = "🟡"
            else:
                icono = "🔴"
            st.metric(f"{icono} The Odds API — restantes", restantes)
            if restantes <= ODDS_RED_THRESHOLD:
                st.warning("⚠️ Quedan pocas requests en The Odds API.")
        except (TypeError, ValueError, KeyError):
            st.caption("The Odds API: header de consumo no legible")
    else:
        st.caption("The Odds API: sin llamadas registradas aún")

    if "claude_tokens_acumulados" in st.session_state:
        c = st.session_state["claude_tokens_acumulados"]
        st.metric("Claude — tokens entrada (sesión)", f"{c['entrada']:,}")
        st.metric("Claude — tokens salida (sesión)", f"{c['salida']:,}")
        if c["total"] >= CLAUDE_TOKEN_WARNING:
            st.warning(
                f"⚠️ Consumo de Claude en esta sesión superó {CLAUDE_TOKEN_WARNING:,} tokens."
            )
        if "claude_ratelimit" in st.session_state:
            rl = st.session_state["claude_ratelimit"]
            st.caption(
                f"⏱️ Rate-limit — tokens: {rl['tokens_restantes']}/{rl['tokens_limite']} · "
                f"requests: {rl['requests_restantes']}"
            )

    if "gemini_tokens_acumulados" in st.session_state:
        g = st.session_state["gemini_tokens_acumulados"]
        st.metric("Gemini — tokens totales (sesión)", f"{g['total']:,}")
        st.caption(f"Entrada: {g['prompt']:,} · Salida: {g['salida']:,}")
        if g["total"] >= GEMINI_TOKEN_WARNING:
            st.warning(
                f"⚠️ Consumo de Gemini en esta sesión superó {GEMINI_TOKEN_WARNING:,} tokens."
            )

    if "claude_tokens_acumulados" in st.session_state or "gemini_tokens_acumulados" in st.session_state:
        st.caption(
            "Nota: Claude y Gemini no exponen cuota 'restante' real vía API. "
            "Estos números son consumo de sesión."
        )

    if st.session_state.get("clubelo_match_failures"):
        with st.expander("⚠️ Fallos de matching ClubElo (últimos)"):
            for f in st.session_state["clubelo_match_failures"][-15:]:
                st.caption(f)
            st.caption("Añade alias en CLUBELO_NAME_ALIASES para mejorar cobertura.")

    estado_elo_sidebar = cargar_estado_elo()
    if estado_elo_sidebar.get("ratings"):
        with st.expander("🧠 Motor Elo interno — estado actual"):
            for sport_key, ratings in estado_elo_sidebar["ratings"].items():
                brier = calcular_brier(
                    estado_elo_sidebar.get("historial_brier", {}).get(sport_key, [])
                )
                n_muestras = len(
                    estado_elo_sidebar.get("historial_brier", {}).get(sport_key, [])
                )
                brier_txt = f"{brier:.4f}" if brier is not None else "N/A"
                st.write(
                    f"**{sport_key}** — {len(ratings)} equipos, "
                    f"Brier: {brier_txt} ({n_muestras} muestras)"
                )

    st.divider()
    st.caption(f"Forebet backend: {'ON' if ENABLE_FOREBET else 'OFF (recomendado)'}")
    st.caption(f"Umbrales: EV≥{EV_MINIMO*100:.0f}% · Div≤{DIVERGENCIA_MAXIMA*100:.0f}% · Conf≥{CONFIANZA_MINIMA}")

if api_key:
    deportes_lista = obtener_deportes_activos(api_key)

    if deportes_lista:
        opciones_deporte = {"🔥 TODOS LOS DEPORTES ACTIVOS": "ALL"}
        for dep in deportes_lista:
            opciones_deporte[f"{dep.get('group')} - {dep.get('title')}"] = dep.get("key")

        seleccion = st.selectbox(
            "Selecciona el deporte o ámbito a analizar:", list(opciones_deporte.keys())
        )
        deporte_key_seleccionado = opciones_deporte[seleccion]

        modo_ejecucion = st.radio(
            "Modo de análisis:",
            [
                "Separado por deporte (recomendado — cobertura completa)",
                "Todo en un solo prompt (rápido, menos preciso)",
            ],
            help=(
                "Por deporte: un prompt por familia, garantiza búsqueda real por evento "
                "y no gasta tokens en descartes automáticos. "
                "Un solo prompt: más barato pero con muchos eventos la IA puede quedarse "
                "sin presupuesto de búsqueda."
            ),
        )

        if st.button("🚀 Generar Prompt y Procesar Datos", type="primary"):
            st.session_state.pop("clubelo_match_failures", None)
            with st.spinner(
                "Consultando The Odds API, ClubElo, actualizando Elo y precalculando EV..."
            ):
                datos_acumulados = []

                if deporte_key_seleccionado == "ALL":
                    deps_a_consultar = [
                        d for d in deportes_lista if _pasa_whitelist(d.get("key"))
                    ]
                    progress_bar = st.progress(0)
                    total_deps = max(len(deps_a_consultar), 1)
                    for idx, dep in enumerate(deps_a_consultar):
                        cuotas = obtener_cuotas_api(api_key, dep.get("key"))
                        if cuotas:
                            datos_acumulados.extend(cuotas)
                        progress_bar.progress((idx + 1) / total_deps)
                        time.sleep(0.15)
                    progress_bar.empty()
                else:
                    datos_acumulados = obtener_cuotas_api(api_key, deporte_key_seleccionado)

                estado_elo = cargar_estado_elo()
                sport_keys_presentes = {
                    ev.get("sport_key") for ev in datos_acumulados if isinstance(ev, dict)
                }
                for sk in sport_keys_presentes:
                    base = _buscar_base_registry(sk)
                    if base.get("usa_elo_interno"):
                        estado_elo = actualizar_elo_sport(api_key, sk, estado_elo)
                guardar_estado_elo(estado_elo)

                tz_rd = timezone(timedelta(hours=-4))
                hora_rd = datetime.now(tz_rd).strftime("%Y-%m-%d %H:%M:%S AST (UTC-4)")

                eventos_filtrados, resumen_filtro = filtrar_y_enriquecer(
                    datos_acumulados, estado_elo
                )
                movimientos_pinnacle = registrar_y_calcular_movimientos(
                    eventos_filtrados, deporte_key_seleccionado
                )

                seccion_movimiento = "SIN SNAPSHOT PREVIO EN ESTA SESIÓN."
                if movimientos_pinnacle:
                    lineas_mov = "\n".join(f"- {k}: {v}" for k, v in movimientos_pinnacle.items())
                    seccion_movimiento = f"MOVIMIENTOS EN PINNACLE DETECTADOS:\n{lineas_mov}"

                st.write("### 📌 Resumen de Filtrado Backend")
                st.info(resumen_filtro)

                if eventos_filtrados:
                    with st.expander(
                        f"📋 Tabla de candidatos ({len(eventos_filtrados)})", expanded=True
                    ):
                        filas = []
                        for ev in eventos_filtrados:
                            reg = ev.get("_registry_modelo_secundario") or {}
                            pre = ev.get("_backend_ev") or {}
                            filas.append({
                                "Partido": ev.get("partido"),
                                "Deporte": ev.get("deporte"),
                                "Cobertura": reg.get("cobertura"),
                                "Liquidez": ev.get("_liquidez_backend"),
                                "Casas": ev.get("_n_casas_reportando"),
                                "Mejor EV (backend)": (
                                    f"{pre['mejor_ev']*100:.1f}%"
                                    if pre.get("mejor_ev") is not None else "—"
                                ),
                                "Divergencia": (
                                    f"{pre['mejor_divergencia']*100:.1f}%"
                                    if pre.get("mejor_divergencia") is not None else "—"
                                ),
                                "Outcome EV": pre.get("mejor_outcome") or "—",
                            })
                        st.dataframe(pd.DataFrame(filas), use_container_width=True)

                if not eventos_filtrados:
                    st.warning(
                        f"⚠️ No se encontraron candidatos válidos en el rango "
                        f"{CUOTA_MIN} - {CUOTA_MAX} para la ventana de {VENTANA_HORAS_DEFAULT}h."
                    )
                elif modo_ejecucion.startswith("Todo en un solo prompt"):
                    st.session_state.pop("prompts_por_grupo", None)
                    st.session_state.pop("resumen_automatico_grupo", None)
                    st.session_state.pop("eventos_por_grupo", None)
                    prompt_completo = (
                        f"{SYSTEM_PROMPT_BLINDADO_V3_6}\n\n"
                        f"==================================================\n"
                        f"CONTEXTO DE EJECUCIÓN DEL BACKEND\n"
                        f"==================================================\n"
                        f"ÁMBITO: {seleccion}\n"
                        f"HORA CONSULTA (RD/UTC-4): {hora_rd}\n\n"
                        f"RESUMEN DE PRE-FILTRADO:\n{resumen_filtro}\n\n"
                        f"{seccion_movimiento}\n\n"
                        f"INSTRUCCIÓN TÉCNICA: Usa `_pinnacle_devig`, "
                        f"`_pinnacle_last_update`, `_liquidez_backend`, "
                        f"`_dispersion_max_entre_casas`, `_n_casas_reportando`, "
                        f"`_registry_modelo_secundario` y `_backend_ev` si existe.\n\n"
                        f"DATOS JSON PRE-FILTRADOS Y ENRIQUECIDOS:\n"
                        f"{json.dumps(eventos_filtrados, indent=2, ensure_ascii=False)}"
                    )
                    st.session_state["prompt_generado"] = prompt_completo
                    st.success(
                        f"✅ Se consolidaron {len(eventos_filtrados)} eventos aptos para el prompt."
                    )
                else:
                    st.session_state.pop("prompt_generado", None)
                    prompts_por_grupo, resumen_auto, eventos_por_grupo = construir_prompts_por_deporte(
                        eventos_filtrados, seleccion, hora_rd, seccion_movimiento
                    )
                    st.session_state["prompts_por_grupo"] = prompts_por_grupo
                    st.session_state["resumen_automatico_grupo"] = resumen_auto
                    st.session_state["eventos_por_grupo"] = eventos_por_grupo
                    n_grupos = len(prompts_por_grupo)
                    n_eventos_ia = sum(len(v) for v in eventos_por_grupo.values())
                    st.success(
                        f"✅ {n_grupos} prompt(s) por familia · {n_eventos_ia} eventos requieren IA. "
                        f"Descartes automáticos sin tokens de IA."
                    )
                    if resumen_auto:
                        with st.expander("📋 Descartes automáticos (0 tokens de IA)"):
                            st.markdown(resumen_auto)

    # --- Prompt único ---
    if "prompt_generado" in st.session_state:
        st.divider()
        st.subheader("🤖 Selecciona la IA para ejecutar el Análisis")
        st.caption(
            "Botones web: pega el prompt y elige el modelo más reciente. "
            "Claude API en app incluye web_search forzado."
        )

        col1, col2, col3, col4, col5 = st.columns(5)
        with col1:
            st.link_button("🌐 ChatGPT", "https://chatgpt.com", use_container_width=True)
        with col2:
            st.link_button("🌐 Claude", "https://claude.ai", use_container_width=True)
        with col3:
            st.link_button("🌐 Gemini Web", "https://gemini.google.com", use_container_width=True)
        with col4:
            st.link_button("🌐 DeepSeek", "https://chat.deepseek.com", use_container_width=True)
        with col5:
            st.link_button("🌐 Copilot", "https://copilot.microsoft.com", use_container_width=True)

        st.write("#### 📋 Prompt Listo para Copiar")
        st.code(st.session_state["prompt_generado"], language="markdown")

        if gemini_api_key:
            st.divider()
            st.subheader("⚡ Ejecución Directa en App (Gemini API)")
            st.warning(
                "⚠️ La integración REST de Gemini en esta app **NO tiene búsqueda web**. "
                "No podrá verificar FanGraphs/TennisAbstract/etc. "
                "Para fuentes externas usa Claude API o pega el prompt en gemini.google.com."
            )
            modelos_disponibles = listar_modelos_gemini(gemini_api_key)
            if modelos_disponibles:
                modelo_default = next(
                    (m for m in modelos_disponibles if "flash" in m and "lite" not in m),
                    modelos_disponibles[0],
                )
                modelo_elegido = st.selectbox(
                    "Modelo Gemini (lista en vivo):",
                    modelos_disponibles,
                    index=modelos_disponibles.index(modelo_default),
                )
                if st.button("🤖 Analizar directamente con Gemini API", type="primary"):
                    with st.spinner(f"Analizando con {modelo_elegido}..."):
                        try:
                            resultado, uso_tokens = llamar_gemini_rest(
                                gemini_api_key, modelo_elegido, st.session_state["prompt_generado"]
                            )
                            st.markdown("### 🏆 Resultado del Análisis")
                            st.markdown(resultado)
                            prev = st.session_state.get(
                                "gemini_tokens_acumulados", {"prompt": 0, "salida": 0, "total": 0}
                            )
                            prev["prompt"] += uso_tokens.get("promptTokenCount", 0) or 0
                            prev["salida"] += uso_tokens.get("candidatesTokenCount", 0) or 0
                            prev["total"] += uso_tokens.get("totalTokenCount", 0) or 0
                            st.session_state["gemini_tokens_acumulados"] = prev
                            st.caption(
                                f"Esta llamada: {uso_tokens.get('promptTokenCount', 0):,} entrada · "
                                f"{uso_tokens.get('candidatesTokenCount', 0):,} salida."
                            )
                        except Exception as e:
                            st.error(f"Error al ejecutar con Gemini API: {e}")
            else:
                st.warning("No se pudo obtener la lista de modelos de Gemini.")

        if anthropic_api_key:
            st.divider()
            st.subheader("⚡ Ejecución Directa en App (Claude API — búsqueda forzada)")
            st.caption(
                f"Tool web_search activo (max_uses={CLAUDE_WEB_SEARCH_MAX_USES}). "
                "Eventos ya resueltos por backend no requieren búsqueda extra."
            )
            modelos_claude = listar_modelos_claude(anthropic_api_key)
            if modelos_claude:
                modelo_claude_default = next(
                    (m for m in modelos_claude if "sonnet" in m.lower()), modelos_claude[0]
                )
                modelo_claude_elegido = st.selectbox(
                    "Modelo Claude (lista en vivo):",
                    modelos_claude,
                    index=modelos_claude.index(modelo_claude_default),
                )
                if st.button("🤖 Analizar directamente con Claude API", type="primary"):
                    with st.spinner(
                        f"Analizando con {modelo_claude_elegido} (web search activo)..."
                    ):
                        try:
                            resultado, queries_buscadas, uso_tokens, ratelimit = llamar_claude_rest(
                                anthropic_api_key,
                                modelo_claude_elegido,
                                st.session_state["prompt_generado"],
                                max_tokens=8000,
                            )
                            st.markdown("### 🏆 Resultado del Análisis")
                            st.markdown(resultado)
                            if queries_buscadas:
                                with st.expander(
                                    f"🔍 Búsquedas web realizadas ({len(queries_buscadas)})"
                                ):
                                    for q in queries_buscadas:
                                        st.write(f"- {q}")
                            else:
                                st.info(
                                    "Claude no ejecutó búsquedas web — posible si todo venía "
                                    "resuelto por backend o si descartó por falta de datos."
                                )
                            entrada_tok = uso_tokens.get("input_tokens", 0) or 0
                            salida_tok = uso_tokens.get("output_tokens", 0) or 0
                            prev = st.session_state.get(
                                "claude_tokens_acumulados",
                                {"entrada": 0, "salida": 0, "total": 0},
                            )
                            prev["entrada"] += entrada_tok
                            prev["salida"] += salida_tok
                            prev["total"] += entrada_tok + salida_tok
                            st.session_state["claude_tokens_acumulados"] = prev
                            st.session_state["claude_ratelimit"] = ratelimit
                            st.caption(
                                f"Esta llamada: {entrada_tok:,} entrada · {salida_tok:,} salida."
                            )
                        except Exception as e:
                            st.error(f"Error al ejecutar con Claude API: {e}")
            else:
                st.warning("No se pudo obtener la lista de modelos de Anthropic.")

    # --- Modo por deporte ---
    if "prompts_por_grupo" in st.session_state and st.session_state["prompts_por_grupo"]:
        st.divider()
        st.subheader("🧩 Modo por deporte — prompts individuales")
        prompts_por_grupo = st.session_state["prompts_por_grupo"]
        eventos_por_grupo = st.session_state.get("eventos_por_grupo", {})

        for familia, prompt_texto in prompts_por_grupo.items():
            n_ev = len(eventos_por_grupo.get(familia, []))
            with st.expander(f"📋 Prompt — {familia.upper()} ({n_ev} eventos)"):
                st.code(prompt_texto, language="markdown")

        if anthropic_api_key:
            st.divider()
            st.subheader("⚡ Ejecutar TODOS los grupos con Claude API")
            st.caption(
                "Una llamada por familia (web search forzado). "
                "Al final se muestra un consolidado de tokens."
            )
            modelos_claude_grp = listar_modelos_claude(anthropic_api_key)
            if modelos_claude_grp:
                modelo_default_grp = next(
                    (m for m in modelos_claude_grp if "sonnet" in m.lower()),
                    modelos_claude_grp[0],
                )
                modelo_grp_elegido = st.selectbox(
                    "Modelo Claude para el modo por deporte:",
                    modelos_claude_grp,
                    index=modelos_claude_grp.index(modelo_default_grp),
                    key="modelo_por_deporte",
                )
                if st.button(
                    "🤖 Analizar TODOS los grupos", type="primary", key="btn_todos_grupos"
                ):
                    total_entrada, total_salida = 0, 0
                    resultados_consolidados = []
                    for familia, prompt_texto in prompts_por_grupo.items():
                        with st.spinner(f"Analizando {familia.upper()}..."):
                            try:
                                resultado, queries, uso_tokens, _ = llamar_claude_rest(
                                    anthropic_api_key,
                                    modelo_grp_elegido,
                                    prompt_texto,
                                    max_tokens=8000,
                                )
                                st.markdown(f"### 🏆 Resultado — {familia.upper()}")
                                st.markdown(resultado)
                                resultados_consolidados.append(
                                    f"## {familia.upper()}\n\n{resultado}"
                                )
                                if queries:
                                    with st.expander(
                                        f"🔍 Búsquedas en {familia.upper()} ({len(queries)})"
                                    ):
                                        for q in queries:
                                            st.write(f"- {q}")
                                entrada_tok = uso_tokens.get("input_tokens", 0) or 0
                                salida_tok = uso_tokens.get("output_tokens", 0) or 0
                                total_entrada += entrada_tok
                                total_salida += salida_tok
                                st.caption(
                                    f"{familia.upper()}: {entrada_tok:,} entrada · "
                                    f"{salida_tok:,} salida."
                                )
                                prev = st.session_state.get(
                                    "claude_tokens_acumulados",
                                    {"entrada": 0, "salida": 0, "total": 0},
                                )
                                prev["entrada"] += entrada_tok
                                prev["salida"] += salida_tok
                                prev["total"] += entrada_tok + salida_tok
                                st.session_state["claude_tokens_acumulados"] = prev
                            except Exception as e:
                                st.error(f"Error al analizar {familia.upper()}: {e}")
                    st.divider()
                    st.success(
                        f"✅ Corrida completa — TOTAL: {total_entrada:,} entrada · "
                        f"{total_salida:,} salida · "
                        f"{total_entrada + total_salida:,} tokens "
                        f"({len(prompts_por_grupo)} grupo(s))."
                    )
                    if resultados_consolidados:
                        with st.expander("📦 Informe consolidado (todos los grupos)"):
                            st.markdown("\n\n---\n\n".join(resultados_consolidados))
            else:
                st.warning("No se pudo obtener la lista de modelos de Anthropic.")
