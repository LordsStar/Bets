"""
Analista de Apuesta Única — v3.7

Requisitos (requirements.txt):
    streamlit
    pandas
    beautifulsoup4
    requests

Cambios v3.6 -> v3.7 (fix "Casi calificó" vacío):
  [C1] Consenso de mercado (de-vig de casas no-Pinnacle) como proxy cuantitativo.
  [C2] Precálculo de EV% y divergencia vs consenso cuando no hay 2º modelo oficial.
  [C3] Prompt: sección CASI CALIFICÓ en dos bloques (5a numérico / 5b bloqueados por fuente).
  [C4] El proxy NUNCA permite pick del día (confianza tope documentada < 8).
  [C5] Tabla de candidatos muestra EV oficial y EV proxy.
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
# CONSTANTES / UMBRALES
# ==============================================================================
EV_MINIMO = 0.04
DIVERGENCIA_MAXIMA = 0.09
CONFIANZA_MINIMA = 8
CUOTA_MIN = 1.40
CUOTA_MAX = 2.00
VENTANA_HORAS_DEFAULT = 24

# Proxy de consenso (solo ranking / casi-calificó; NUNCA pick oficial)
CONSENSO_MIN_CASAS = 2
CONSENSO_DIVERGENCIA_CERCANIA = 0.06   # ≤6% → candidato a 5b
CONSENSO_EV_CERCANIA = 0.02            # EV teórico ≥2% → candidato a 5b
CONFIANZA_TOPE_CONSENSO = 7            # nunca ≥8 solo con consenso

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
ANTHROPIC_API_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"

ODDS_YELLOW_THRESHOLD = 100
ODDS_RED_THRESHOLD = 20
CLAUDE_TOKEN_WARNING = 500_000
GEMINI_TOKEN_WARNING = 500_000
CLAUDE_WEB_SEARCH_MAX_USES = 8

ENABLE_FOREBET = False

CLUBELO_API_BASE = "http://api.clubelo.com"
FOREBET_URL = "https://www.forebet.com/en/football-tips-and-predictions-for-today"

SPORTS_WHITELIST_PREFIXES = []  # ej: ["soccer", "baseball_mlb", "tennis"]

REGISTRY_ULTIMA_REVISION = "2026-08-22"

# ==============================================================================
# 1. SYSTEM PROMPT V3.7
# ==============================================================================
SYSTEM_PROMPT_BLINDADO_V3_7 = f"""
PROMPT — Analista Cuantitativo de Apuesta Única (Blindado v3.7)

ROL Y OBJETIVO:
Actúa como Analista Cuantitativo de Deportes y Tipster Profesional. Tu objetivo es
seleccionar UNA sola apuesta —la de mayor confianza estadística— dentro de un rango
de cuota {CUOTA_MIN:.2f}-{CUOTA_MAX:.2f} (moneyline o mercado principal), de TODOS
los eventos recibidos.
Un informe con 0 picks es un resultado VÁLIDO y ESPERADO en la mayoría de los días.
Nunca fuerces un pick para "tener algo que mostrar".

METODOLOGÍA Y REGLAS CLAVE:

1. ANCLA OBLIGATORIA: Usa directamente `_pinnacle_devig`. No recalcules el de-vig.

1b. PRECÁLCULO BACKEND:
   - Si existe `_backend_ev` (2º modelo oficial: ClubElo/Elo/externo backend):
     USA esos números. No los recalcules salvo error manifiesto.
   - Si existe `_backend_ev_proxy` (consenso de casas no-Pinnacle):
     ÚSALO SOLO para ranking de cercanía (sección 5b) y para rellenar tablas
     de transparencia cuando no hubo fuente oficial. NUNCA como base de un
     pick del día con confianza >= {CONFIANZA_MINIMA}.
   Fórmulas de referencia:
     EV% = (prob_modelo * cuota_pinnacle) - 1
     Divergencia% = |prob_pinnacle_devig - prob_modelo|

2. GATE DE FRESCURA (relativo al tiempo restante):
   Compara `_pinnacle_last_update` contra `inicio_utc`:
   - < 3h para el inicio Y last_update con > 90 min de antigüedad → DESCARTA.
   - > 3h restantes → la antigüedad es solo informativa; NO descartes por esto.

3. VALIDACIÓN CRUZADA — solo vía `_registry_modelo_secundario`:
   Según `cobertura`:

   - "externa_directa": DEBES buscar en fuente_primaria (URL correcta) o
     secundaria. Prohibido "no se puede confirmar" sin intento real.
     FanGraphs: SIEMPRE https://www.fangraphs.com/scores?date=YYYY-MM-DD
     ClubElo: SIEMPRE api.clubelo.com (nunca clubelo.com sin api).
     Si falla primaria/secundaria y hay fuente_respaldo documentada, cítala
     como respaldo. Forebet = caja negra → baja calidad de fuente.
     Si no hay número verificable → categoría 2.

   - "modelo_interno_elo": usa probabilidad_elo_home, elo_*, brier_*, muestras_*.
     Cita: "Modelo Elo interno (backend)...".

   - "modelo_externo_backend": usa probabilidad_home/draw/away. Cita la fuente
     exacta del registry. Si Forebet, advertencia de metodología.

   - "pendiente_desarrollo" / "excluido_estructural": DESCARTA (cat. 2 o estructural).

   PROHIBIDO: fuentes no listadas en el registry. PROHIBIDO "538 SPI".

   IMPORTANTE — CONSENSO DE MERCADO (`_consenso_mercado_devig` / `_backend_ev_proxy`):
   NO es un segundo modelo oficial. NO habilita pick del día.
   Solo sirve para:
     (a) rellenar señales de cercanía en 5b,
     (b) contextualizar eventos en cat. 2 que sí tienen liquidez y frescura.
   Confianza máxima si alguien lo usara mal: {CONFIANZA_TOPE_CONSENSO}/10
   (y aun así NO debe ser el pick).

3b. CHEQUEO FÍSICO — obligatorio tenis / boxeo / MMA (últimas 48-72h).
   Noticia real → baja confianza. Sin hallazgos → decláralo explícitamente.

4. LIQUIDEZ: `_liquidez_backend`. < 2 casas → no califica.

5. UMBRALES (versionados en backend):
   - EV < {EV_MINIMO * 100:.0f}% → descartar (cat. 4)
   - Divergencia > {DIVERGENCIA_MAXIMA * 100:.0f}% → descartar (cat. 5)
   - Elo interno con brier > 0.23 o muestras < 8 → descartar
   - Confianza < {CONFIANZA_MINIMA}/10 → cat. 6
   El proxy de consenso NO entra en el cálculo del pick del día.

6. CONFIANZA (1-10): edge, calidad de fuente, liquidez, línea, (físico si aplica).
   Solo pick si total >= {CONFIANZA_MINIMA}/10 Y el 2º modelo es oficial
   (no consenso de mercado).

REGLAS ANTI-FABRICACIÓN:
- Nunca inventes datos, lesiones, cuotas ni fuentes.
- Cat. 1/2/3 sin cálculo oficial: EV/divergencia = "N/A — no se calculó"
  (salvo que reportes el proxy claramente etiquetado como "proxy consenso").
- fuente_respaldo solo tras intentar primaria/secundaria de verdad.

CATEGORÍAS DE DESCARTE (mutuamente excluyentes, en orden):
   1º Frescura
   2º Segundo modelo oficial no disponible
   3º Liquidez
   4º EV bajo umbral (solo si hubo 2º modelo oficial)
   5º Divergencia alta (solo si hubo 2º modelo oficial)
   6º Confianza < {CONFIANZA_MINIMA}
Un evento en exactamente UNA categoría.

AUTO-VERIFICACIÓN:
suma(descartes por cat.) + (1 si hay pick else 0) = total de eventos del JSON
de ESTE prompt.

FORMATO DE SALIDA (obligatorio, en español):
1. Resumen: N eventos + desglose exacto por las 6 categorías + verificación de suma.
2. Si hay pick: Partido | Mercado | Cuota Pinnacle | Prob. de-vig | Prob. 2º modelo
   (fuente oficial) | EV% | Confianza (desglose) | Justificación 3-4 líneas.
3. Si NO hay pick: "PICK DEL DÍA: NINGUNO" + explicación breve por categoría.
4. TABLA DE TRANSPARENCIA — solo cat. 4, 5 y 6 (con 2º modelo oficial calculado):
   | Partido | Categoría | EV% | Divergencia% | Confianza | Motivo breve |

5. CASI CALIFICÓ — DOS subsecciones obligatorias:

   5a. CON CÁLCULO NUMÉRICO OFICIAL (categorías 4, 5 y 6):
   Hasta 3 eventos que estuvieron más cerca de pasar TODOS los umbrales.
   | Partido | Qué faltó | Qué tan cerca (número exacto vs umbral) |
   Si no hay ninguno: "Ningún evento con EV/divergencia oficiales estuvo cerca del umbral."

   5b. BLOQUEADOS POR SEGUNDO MODELO OFICIAL (categoría 2 con señal cuantitativa):
   Hasta 3 eventos que SÍ pasaron frescura, liquidez y rango de cuota, pero
   NO tuvieron 2º modelo oficial, Y que traen `_backend_ev_proxy` u otra señal:
   - divergencia |Pinnacle − consenso| ≤ {CONSENSO_DIVERGENCIA_CERCANIA * 100:.0f}%, o
   - EV teórico vs consenso ≥ {CONSENSO_EV_CERCANIA * 100:.0f}%.
   | Partido | Qué faltó (fuente oficial) | Señal de cercanía (proxy) |
   Estos eventos NUNCA son el pick del día.
   Si no hay ninguno con señal cuantitativa: "No hay eventos en cat. 2 con
   proxy de mercado suficiente para rankear cercanía."
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

        resultado_home = 0.5 if score_home == score_away else (1.0 if score_home > score_away else 0.0)

        home = ratings.setdefault(home_team, {"elo": ELO_INICIAL, "partidos": 0})
        away = ratings.setdefault(away_team, {"elo": ELO_INICIAL, "partidos": 0})

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
# 1d. CLUBELO — matching + fixtures
# ==============================================================================
CLUBELO_NAME_ALIASES = {
    "manchester united": "Man United", "manchester city": "Man City",
    "tottenham hotspur": "Tottenham", "tottenham": "Tottenham",
    "wolverhampton wanderers": "Wolves", "wolverhampton": "Wolves",
    "nottingham forest": "Forest", "brighton and hove albion": "Brighton",
    "brighton & hove albion": "Brighton", "west ham united": "West Ham",
    "newcastle united": "Newcastle", "leicester city": "Leicester",
    "leeds united": "Leeds", "aston villa": "Aston Villa",
    "crystal palace": "Crystal Palace", "sheffield united": "Sheffield United",
    "ipswich town": "Ipswich", "southampton": "Southampton", "fulham": "Fulham",
    "brentford": "Brentford", "bournemouth": "Bournemouth", "afc bournemouth": "Bournemouth",
    "everton": "Everton", "chelsea": "Chelsea", "arsenal": "Arsenal", "liverpool": "Liverpool",
    "atletico madrid": "Atletico", "atlético madrid": "Atletico", "atletico de madrid": "Atletico",
    "real madrid": "Real Madrid", "barcelona": "Barcelona", "fc barcelona": "Barcelona",
    "real sociedad": "Sociedad", "athletic club": "Athletic", "athletic bilbao": "Athletic",
    "real betis": "Betis", "villarreal": "Villarreal", "sevilla": "Sevilla", "valencia": "Valencia",
    "osasuna": "Osasuna", "getafe": "Getafe", "girona": "Girona", "celta de vigo": "Celta",
    "celta vigo": "Celta", "rayo vallecano": "Rayo Vallecano", "mallorca": "Mallorca",
    "las palmas": "Las Palmas", "alaves": "Alaves", "deportivo alaves": "Alaves",
    "leganes": "Leganes", "espanyol": "Espanyol", "real valladolid": "Valladolid",
    "inter": "Inter", "inter milan": "Inter", "internazionale": "Inter",
    "ac milan": "Milan", "milan": "Milan", "juventus": "Juventus",
    "ssc napoli": "Napoli", "napoli": "Napoli", "as roma": "Roma", "roma": "Roma",
    "lazio": "Lazio", "atalanta": "Atalanta", "fiorentina": "Fiorentina", "torino": "Torino",
    "bologna": "Bologna", "genoa": "Genoa", "udinese": "Udinese", "sassuolo": "Sassuolo",
    "cagliari": "Cagliari", "empoli": "Empoli", "monza": "Monza", "lecce": "Lecce",
    "verona": "Verona", "hellas verona": "Verona", "parma": "Parma", "como": "Como", "venezia": "Venezia",
    "bayern munich": "Bayern", "bayern münchen": "Bayern", "fc bayern munich": "Bayern",
    "borussia dortmund": "Dortmund", "bayer leverkusen": "Leverkusen", "rb leipzig": "Leipzig",
    "eintracht frankfurt": "Frankfurt", "borussia monchengladbach": "Gladbach",
    "borussia mönchengladbach": "Gladbach", "vfb stuttgart": "Stuttgart", "wolfsburg": "Wolfsburg",
    "werder bremen": "Werder", "union berlin": "Union Berlin", "1. fc union berlin": "Union Berlin",
    "mainz 05": "Mainz", "fsv mainz 05": "Mainz", "fc augsburg": "Augsburg", "augsburg": "Augsburg",
    "hoffenheim": "Hoffenheim", "tsg hoffenheim": "Hoffenheim", "sc freiburg": "Freiburg",
    "freiburg": "Freiburg", "1. fc heidenheim": "Heidenheim", "heidenheim": "Heidenheim",
    "fc st. pauli": "St Pauli", "st. pauli": "St Pauli", "holstein kiel": "Holstein",
    "paris saint germain": "Paris SG", "paris saint-germain": "Paris SG", "psg": "Paris SG",
    "olympique de marseille": "Marseille", "marseille": "Marseille",
    "olympique lyonnais": "Lyon", "lyon": "Lyon", "as monaco": "Monaco", "monaco": "Monaco",
    "lille": "Lille", "losc lille": "Lille", "nice": "Nice", "ogc nice": "Nice",
    "rennes": "Rennes", "stade rennais": "Rennes", "lens": "Lens", "rc lens": "Lens",
    "nantes": "Nantes", "strasbourg": "Strasbourg", "toulouse": "Toulouse",
    "brest": "Brest", "stade brestois": "Brest", "reims": "Reims", "montpellier": "Montpellier",
    "auxerre": "Auxerre", "angers": "Angers", "le havre": "Le Havre",
    "saint-etienne": "Saint-Etienne", "saint etienne": "Saint-Etienne",
    "ajax": "Ajax", "psv": "PSV", "psv eindhoven": "PSV", "feyenoord": "Feyenoord",
    "benfica": "Benfica", "sl benfica": "Benfica", "porto": "Porto", "fc porto": "Porto",
    "sporting cp": "Sporting", "sporting lisbon": "Sporting", "celtic": "Celtic", "rangers": "Rangers",
    "galatasaray": "Galatasaray", "fenerbahce": "Fenerbahce", "besiktas": "Besiktas",
    "olympiacos": "Olympiakos", "olympiakos": "Olympiakos",
    "shakhtar donetsk": "Shakhtar", "dynamo kyiv": "Dynamo Kyiv",
    "red bull salzburg": "Salzburg", "fc salzburg": "Salzburg",
    "young boys": "Young Boys", "club brugge": "Club Brugge", "anderlecht": "Anderlecht",
}


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _normalizar_nombre(nombre: str) -> str:
    if not nombre:
        return ""
    s = _strip_accents(nombre).lower().strip()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for tok in (" fc", " cf", " afc", " sc", " ac", " as", " fk", " nk", " bk", " if"):
        if s.endswith(tok):
            s = s[: -len(tok)].strip()
    return s


def _tokens(nombre: str) -> set:
    stop = {"fc", "cf", "afc", "sc", "ac", "as", "the", "de", "club", "united", "city"}
    return {t for t in _normalizar_nombre(nombre).split() if t and t not in stop}


def resolver_nombre_clubelo(nombre_odds: str, nombres_clubelo_disponibles=None):
    if not nombre_odds:
        return None, "vacio"
    key = _normalizar_nombre(nombre_odds)
    if key in CLUBELO_NAME_ALIASES:
        return CLUBELO_NAME_ALIASES[key], "alias"
    for alias_key, canon in CLUBELO_NAME_ALIASES.items():
        if alias_key in key or key in alias_key:
            return canon, "alias_parcial"
    if nombres_clubelo_disponibles:
        norm_map = {_normalizar_nombre(n): n for n in nombres_clubelo_disponibles}
        if key in norm_map:
            return norm_map[key], "exacto_normalizado"
        tok_query = _tokens(nombre_odds)
        best, best_score = None, 0.0
        for _, original in norm_map.items():
            tok_n = _tokens(original)
            if not tok_query or not tok_n:
                continue
            inter = len(tok_query & tok_n)
            union = len(tok_query | tok_n)
            score = inter / union if union else 0.0
            if score > best_score:
                best_score, best = score, original
        if best is not None and best_score >= 0.6:
            return best, f"fuzzy_{best_score:.2f}"
    return None, "sin_match"


def _log_clubelo_failure(home, away, reason):
    fails = st.session_state.setdefault("clubelo_match_failures", [])
    entry = f"{home} vs {away} — {reason}"
    if entry not in fails:
        fails.append(entry)
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
    try:
        int(str(nombre_columna).strip())
        return True
    except (ValueError, TypeError):
        return False


def _prob_desde_fixtures_clubelo(df, home_team, away_team):
    if df is None or df.empty or "Home" not in df.columns or "Away" not in df.columns:
        return None

    todos = list(set(
        df["Home"].dropna().astype(str).unique().tolist()
        + df["Away"].dropna().astype(str).unique().tolist()
    ))
    home_resuelto, metodo_h = resolver_nombre_clubelo(home_team, todos)
    away_resuelto, metodo_a = resolver_nombre_clubelo(away_team, todos)
    if not home_resuelto or not away_resuelto:
        _log_clubelo_failure(home_team, away_team, f"match fallido (home={metodo_h}, away={metodo_a})")
        return None

    try:
        match = df[
            (df["Home"].astype(str).str.strip().str.lower() == home_resuelto.strip().lower())
            & (df["Away"].astype(str).str.strip().str.lower() == away_resuelto.strip().lower())
        ]
        invertido = False
        if match.empty:
            match = df[
                (df["Home"].astype(str).str.strip().str.lower() == away_resuelto.strip().lower())
                & (df["Away"].astype(str).str.strip().str.lower() == home_resuelto.strip().lower())
            ]
            invertido = not match.empty
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

        prob_home = prob_away = prob_draw = 0.0
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
            _log_clubelo_failure(home_team, away_team, f"probs no suman 1 (suma={total:.4f})")
            return None

        return {
            "prob_home": round(float(prob_home / total), 4),
            "prob_draw": round(float(prob_draw / total), 4),
            "prob_away": round(float(prob_away / total), 4),
            "clubelo_home": home_resuelto,
            "clubelo_away": away_resuelto,
            "match_method": f"{metodo_h}/{metodo_a}",
        }
    except Exception as e:
        _log_clubelo_failure(home_team, away_team, f"excepción: {e}")
        return None


def obtener_prediccion_forebet(home_team, away_team):
    if not ENABLE_FOREBET:
        return None
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    try:
        r = requests.get(
            FOREBET_URL, timeout=12,
            headers={"User-Agent": "Mozilla/5.0 (compatible; BlindadoBot/3.7)"},
        )
        r.raise_for_status()
    except Exception:
        return None
    try:
        soup = BeautifulSoup(r.text, "html.parser")
        filas = soup.select("div.rcnt") or soup.select(".rcnt")
        home_n = _normalizar_nombre(home_team)
        away_n = _normalizar_nombre(away_team)
        for fila in filas:
            home_el = fila.select_one(".homeTeam") or fila.select_one("span.homeTeam")
            away_el = fila.select_one(".awayTeam") or fila.select_one("span.awayTeam")
            if home_el and away_el:
                if home_n not in _normalizar_nombre(home_el.get_text()):
                    continue
                if away_n not in _normalizar_nombre(away_el.get_text()):
                    continue
            else:
                texto = _normalizar_nombre(fila.get_text(" ", strip=True))
                if home_n not in texto or away_n not in texto:
                    continue
            probs = []
            fprc = fila.select_one(".fprc")
            if fprc:
                probs = [s.get_text(strip=True).replace("%", "") for s in fprc.find_all("span")[:3]]
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
            return {"prob_home": round(ph, 4), "prob_draw": round(pd_, 4), "prob_away": round(pa, 4)}
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
        resuelto = obtener_entrada_clubelo_o_forebet(sport_key, home_team, away_team)
        if resuelto:
            return resuelto
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
        ph = ratings.get(home_team, {}).get("partidos", 0)
        pa = ratings.get(away_team, {}).get("partidos", 0)
        base["nota"] = (
            f"Elo interno insuficiente ({home_team}: {ph}, {away_team}: {pa}, "
            f"mín: {ELO_MIN_PARTIDOS_POR_EQUIPO})."
        )
    base["ultima_revision"] = REGISTRY_ULTIMA_REVISION
    return base


# ==============================================================================
# 2. BACKEND — Odds API + consenso + EV
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
        if response.status_code in (422, 429):
            if response.status_code == 429:
                st.warning(f"⚠️ Rate limit en {sport_key}, se omite.")
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
        for nombre, prob in devig_probabilidades(h2h.get("outcomes", [])).items():
            probs_por_resultado.setdefault(nombre, []).append(prob)
    dispersiones = [max(v) - min(v) for v in probs_por_resultado.values() if len(v) >= 2]
    return max(dispersiones) if dispersiones else 0.0


def calcular_consenso_mercado(evento_crudo):
    """
    Media de-vig de casas NO Pinnacle (mín. CONSENSO_MIN_CASAS).
    Proxy para ranking 5b — NUNCA segundo modelo oficial.
    """
    if not isinstance(evento_crudo, dict):
        return None
    probs_por_nombre = {}
    casas_usadas = []
    for b in evento_crudo.get("bookmakers", []):
        if not isinstance(b, dict) or b.get("key") == "pinnacle":
            continue
        h2h = next((m for m in b.get("markets", []) if isinstance(m, dict) and m.get("key") == "h2h"), None)
        if not h2h:
            continue
        devig = devig_probabilidades(h2h.get("outcomes", []))
        if not devig:
            continue
        casas_usadas.append(b.get("key") or b.get("title") or "unknown")
        for nombre, p in devig.items():
            probs_por_nombre.setdefault(nombre, []).append(p)

    if len(casas_usadas) < CONSENSO_MIN_CASAS:
        return None

    consenso = {
        nombre: round(sum(vals) / len(vals), 4)
        for nombre, vals in probs_por_nombre.items()
        if vals
    }
    if not consenso:
        return None
    return {
        "probs": consenso,
        "n_casas": len(casas_usadas),
        "casas": casas_usadas,
        "nota": (
            "Proxy de mercado (media de-vig no-Pinnacle). "
            "NO es segundo modelo oficial. No habilita pick del día."
        ),
    }


def _precalc_desde_probs(cuotas, devig_pinnacle, probs_modelo):
    detalle = {}
    mejor_ev = None
    mejor_outcome = None
    for nombre, cuota in (cuotas or {}).items():
        if not cuota or nombre not in probs_modelo:
            continue
        p_mod = probs_modelo[nombre]
        p_pin = (devig_pinnacle or {}).get(nombre)
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


def precalcular_ev_oficial(evento_minificado):
    """EV/divergencia con 2º modelo oficial (ClubElo / Elo interno / backend)."""
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
    out = _precalc_desde_probs(cuotas, devig, probs_modelo)
    if out:
        out["tipo"] = "oficial"
        out["fuente"] = registry.get("fuente_primaria") or registry.get("cobertura")
    return out


def precalcular_ev_proxy(evento_minificado, consenso):
    """EV/divergencia vs consenso de mercado — solo ranking 5b."""
    if not consenso or not consenso.get("probs"):
        return None
    out = _precalc_desde_probs(
        evento_minificado.get("cuotas_pinnacle"),
        evento_minificado.get("_pinnacle_devig"),
        consenso["probs"],
    )
    if not out:
        return None
    out["tipo"] = "proxy_consenso"
    out["fuente"] = f"Consenso {consenso.get('n_casas')} casas: {', '.join(consenso.get('casas', []))}"
    out["nota"] = consenso.get("nota")
    # señal de cercanía para 5b
    me = out.get("mejor_ev")
    md = out.get("mejor_divergencia")
    out["senal_cercania_5b"] = bool(
        (md is not None and md <= CONSENSO_DIVERGENCIA_CERCANIA)
        or (me is not None and me >= CONSENSO_EV_CERCANIA)
    )
    return out


def registrar_y_calcular_movimientos(eventos_minificados, deporte_key):
    if not eventos_minificados:
        return {}
    state_key = f"pinnacle_snapshot_{deporte_key}"
    movimientos = {}
    snapshot_actual = {}
    for ev in eventos_minificados:
        if not isinstance(ev, dict):
            continue
        ev_id, matchup, prices = ev.get("id"), ev.get("partido"), ev.get("cuotas_pinnacle", {})
        if ev_id and prices:
            snapshot_actual[ev_id] = {"matchup": matchup, "prices": prices}
    if state_key in st.session_state and isinstance(st.session_state[state_key], dict):
        prev = st.session_state[state_key]
        for ev_id, data_curr in snapshot_actual.items():
            if ev_id not in prev:
                continue
            data_prev = prev[ev_id]
            for team, price_curr in data_curr.get("prices", {}).items():
                price_prev = data_prev.get("prices", {}).get(team)
                if price_prev and price_prev != price_curr:
                    pct = round(((price_curr - price_prev) / price_prev) * 100, 2)
                    dir_ = "subió" if pct > 0 else "bajó"
                    movimientos[f"{data_curr['matchup']} ({team})"] = (
                        f"Cuota cambió de {price_prev} a {price_curr} ({dir_} {abs(pct)}%)"
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
    c = dict(
        sin_pinnacle=0, fuera_rango=0, fecha=0, sin_fecha=0, estructural=0,
        elo=0, backend=0, pendiente=0, ev_oficial=0, ev_proxy=0, proxy_5b=0,
    )
    ahora_utc = datetime.now(timezone.utc)
    limite_utc = ahora_utc + timedelta(hours=horas_ventana)

    for evento in datos_crudos:
        if not isinstance(evento, dict) or not _pasa_whitelist(evento.get("sport_key")):
            continue

        home_team = evento.get("home_team")
        away_team = evento.get("away_team")
        registry_entry = obtener_entrada_registry(
            evento.get("sport_key"), home_team=home_team, away_team=away_team, estado_elo=estado_elo
        )
        if registry_entry["cobertura"] == "excluido_estructural":
            c["estructural"] += 1
            continue

        commence_str = evento.get("commence_time")
        if not commence_str:
            c["sin_fecha"] += 1
            continue
        try:
            commence_dt = datetime.fromisoformat(commence_str.replace("Z", "+00:00"))
            if not (ahora_utc <= commence_dt <= limite_utc):
                c["fecha"] += 1
                continue
        except Exception:
            c["sin_fecha"] += 1
            continue

        pinnacle = next(
            (b for b in evento.get("bookmakers", []) if isinstance(b, dict) and b.get("key") == "pinnacle"),
            None,
        )
        if not pinnacle:
            c["sin_pinnacle"] += 1
            continue
        h2h = next(
            (m for m in pinnacle.get("markets", []) if isinstance(m, dict) and m.get("key") == "h2h"),
            None,
        )
        if not h2h:
            c["sin_pinnacle"] += 1
            continue

        outcomes = h2h.get("outcomes", [])
        if not any(CUOTA_MIN <= o.get("price", 0) <= CUOTA_MAX for o in outcomes if isinstance(o, dict)):
            c["fuera_rango"] += 1
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
            c["elo"] += 1
        elif registry_entry["cobertura"] == "modelo_externo_backend":
            c["backend"] += 1
        elif registry_entry["cobertura"] == "pendiente_desarrollo":
            c["pendiente"] += 1

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

        # EV oficial
        pre_oficial = precalcular_ev_oficial(evento_minificado)
        if pre_oficial:
            evento_minificado["_backend_ev"] = pre_oficial
            c["ev_oficial"] += 1

        # Consenso + EV proxy (siempre que haya ≥2 casas no-Pinnacle)
        consenso = calcular_consenso_mercado(evento)
        if consenso:
            evento_minificado["_consenso_mercado_devig"] = consenso
            pre_proxy = precalcular_ev_proxy(evento_minificado, consenso)
            if pre_proxy:
                evento_minificado["_backend_ev_proxy"] = pre_proxy
                c["ev_proxy"] += 1
                if pre_proxy.get("senal_cercania_5b") and not pre_oficial:
                    c["proxy_5b"] += 1

        eventos_validos.append(evento_minificado)

    resumen = (
        f"Backend pre-filtró {len(datos_crudos)} eventos: "
        f"{len(eventos_validos)} candidatos (prx {horas_ventana}h, cuota {CUOTA_MIN}-{CUOTA_MAX}), "
        f"{c['fecha']} fuera de ventana, {c['sin_fecha']} sin fecha, "
        f"{c['sin_pinnacle']} sin Pinnacle, {c['fuera_rango']} fuera de rango, "
        f"{c['estructural']} estructurales. "
        f"De los candidatos: {c['elo']} Elo interno, {c['backend']} ClubElo/backend, "
        f"{c['pendiente']} pendiente, {c['ev_oficial']} con EV oficial, "
        f"{c['ev_proxy']} con EV proxy (consenso), "
        f"{c['proxy_5b']} elegibles para Casi-calificó 5b."
    )
    return eventos_validos, resumen


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
        grupos.setdefault(familia_deporte(ev.get("sport_key")), []).append(ev)
    return grupos


def separar_ia_vs_automatico(eventos_familia):
    necesita_ia, automaticos = [], []
    for ev in eventos_familia:
        cob = ev.get("_registry_modelo_secundario", {}).get("cobertura")
        if cob in ("pendiente_desarrollo", "excluido_estructural"):
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
        cob = ev.get("_registry_modelo_secundario", {}).get("cobertura")
        lineas.append(f"- {ev.get('partido')} ({ev.get('deporte')}) — {cob}")
    return "\n".join(lineas)


def construir_prompt_grupo(familia, eventos_grupo, seleccion_label, hora_rd, seccion_movimiento):
    return (
        f"{SYSTEM_PROMPT_BLINDADO_V3_7}\n\n"
        f"==================================================\n"
        f"CONTEXTO BACKEND (MODO POR DEPORTE)\n"
        f"==================================================\n"
        f"ÁMBITO: {seleccion_label}\n"
        f"GRUPO: {familia.upper()} ({len(eventos_grupo)} evento(s))\n"
        f"HORA (RD/UTC-4): {hora_rd}\n\n"
        f"{seccion_movimiento}\n\n"
        f"NOTA: Solo eventos de '{familia}' con cobertura externa_directa / "
        f"modelo_interno_elo / modelo_externo_backend. Los pendiente/excluido "
        f"ya se reportaron fuera de la IA.\n\n"
        f"INSTRUCCIÓN: Usa `_pinnacle_devig`, `_liquidez_backend`, "
        f"`_registry_modelo_secundario`, `_backend_ev` (oficial) y "
        f"`_backend_ev_proxy` / `_consenso_mercado_devig` (solo ranking 5b).\n\n"
        f"DATOS JSON (familia '{familia}'):\n"
        f"{json.dumps(eventos_grupo, indent=2, ensure_ascii=False)}"
    )


def construir_prompts_por_deporte(eventos_filtrados, seleccion_label, hora_rd, seccion_movimiento):
    grupos = agrupar_eventos_por_familia(eventos_filtrados)
    prompts_por_grupo, eventos_por_grupo, autos = {}, {}, []
    for familia, eventos_familia in sorted(grupos.items()):
        necesita_ia, automaticos = separar_ia_vs_automatico(eventos_familia)
        if automaticos:
            r = resumen_automatico_grupo(familia, automaticos)
            if r:
                autos.append(r)
        if necesita_ia:
            prompts_por_grupo[familia] = construir_prompt_grupo(
                familia, necesita_ia, seleccion_label, hora_rd, seccion_movimiento
            )
            eventos_por_grupo[familia] = necesita_ia
    resumen_auto = (
        "\n\n".join(autos) if autos
        else "Ningún evento cayó en descarte 100% automático en esta corrida."
    )
    return prompts_por_grupo, resumen_auto, eventos_por_grupo


# ==============================================================================
# 3. GEMINI / CLAUDE
# ==============================================================================

@st.cache_data(ttl=1800, show_spinner=False)
def listar_modelos_gemini(gemini_api_key):
    url = f"{GEMINI_API_BASE}/models?key={gemini_api_key}"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        utilizables = []
        for m in r.json().get("models", []):
            nombre = m.get("name", "").replace("models/", "")
            metodos = m.get("supportedGenerationMethods", [])
            if "generateContent" in metodos and not any(
                x in nombre for x in ["image", "audio", "tts", "embedding", "live", "vision"]
            ):
                utilizables.append(nombre)
        return sorted(utilizables, reverse=True)
    except Exception as e:
        st.error(f"No se pudo obtener modelos de Gemini: {e}")
        return []


def llamar_gemini_rest(gemini_api_key, modelo, prompt_texto):
    url = f"{GEMINI_API_BASE}/models/{modelo}:generateContent"
    headers = {"x-goog-api-key": gemini_api_key, "Content-Type": "application/json"}
    body = {"contents": [{"parts": [{"text": prompt_texto}]}]}
    r = requests.post(url, headers=headers, json=body, timeout=90)
    r.raise_for_status()
    data = r.json()
    texto = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    return texto, data.get("usageMetadata", {})


@st.cache_data(ttl=1800, show_spinner=False)
def listar_modelos_claude(anthropic_api_key):
    url = f"{ANTHROPIC_API_BASE}/models"
    headers = {"x-api-key": anthropic_api_key, "anthropic-version": ANTHROPIC_VERSION}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        return [m.get("id") for m in r.json().get("data", []) if m.get("id")]
    except Exception as e:
        st.error(f"No se pudo obtener modelos de Claude: {e}")
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
    partes = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    queries = [
        b.get("input", {}).get("query")
        for b in data.get("content", [])
        if b.get("type") == "server_tool_use" and b.get("name") == "web_search"
    ]
    uso = data.get("usage", {})
    ratelimit = {
        "requests_restantes": r.headers.get("anthropic-ratelimit-requests-remaining"),
        "tokens_restantes": r.headers.get("anthropic-ratelimit-tokens-remaining"),
        "tokens_limite": r.headers.get("anthropic-ratelimit-tokens-limit"),
        "reset": r.headers.get("anthropic-ratelimit-tokens-reset"),
    }
    return "\n\n".join(partes), [q for q in queries if q], uso, ratelimit


# ==============================================================================
# 4. INTERFAZ
# ==============================================================================

st.set_page_config(page_title="Analista Cuantitativo de Apuestas", layout="wide")
st.title(
    "📊 Analista de Apuesta Única v3.7 "
    "(Consenso proxy · Casi-calificó 5a/5b · ClubElo · Elo · Multi-IA)"
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
        try:
            restantes = int(st.session_state["odds_api_uso"]["restantes"])
            icono = "🟢" if restantes > ODDS_YELLOW_THRESHOLD else ("🟡" if restantes > ODDS_RED_THRESHOLD else "🔴")
            st.metric(f"{icono} The Odds API — restantes", restantes)
            if restantes <= ODDS_RED_THRESHOLD:
                st.warning("⚠️ Pocas requests en The Odds API.")
        except (TypeError, ValueError, KeyError):
            st.caption("The Odds API: header no legible")
    else:
        st.caption("The Odds API: sin llamadas aún")

    if "claude_tokens_acumulados" in st.session_state:
        ctok = st.session_state["claude_tokens_acumulados"]
        st.metric("Claude — entrada (sesión)", f"{ctok['entrada']:,}")
        st.metric("Claude — salida (sesión)", f"{ctok['salida']:,}")
        if ctok["total"] >= CLAUDE_TOKEN_WARNING:
            st.warning(f"⚠️ Claude superó {CLAUDE_TOKEN_WARNING:,} tokens en sesión.")
        if "claude_ratelimit" in st.session_state:
            rl = st.session_state["claude_ratelimit"]
            st.caption(
                f"⏱️ Rate-limit tokens: {rl['tokens_restantes']}/{rl['tokens_limite']} · "
                f"req: {rl['requests_restantes']}"
            )

    if "gemini_tokens_acumulados" in st.session_state:
        g = st.session_state["gemini_tokens_acumulados"]
        st.metric("Gemini — total (sesión)", f"{g['total']:,}")
        st.caption(f"Entrada: {g['prompt']:,} · Salida: {g['salida']:,}")

    if st.session_state.get("clubelo_match_failures"):
        with st.expander("⚠️ Fallos matching ClubElo"):
            for f in st.session_state["clubelo_match_failures"][-15:]:
                st.caption(f)

    estado_elo_sidebar = cargar_estado_elo()
    if estado_elo_sidebar.get("ratings"):
        with st.expander("🧠 Motor Elo interno"):
            for sk, ratings in estado_elo_sidebar["ratings"].items():
                brier = calcular_brier(estado_elo_sidebar.get("historial_brier", {}).get(sk, []))
                n = len(estado_elo_sidebar.get("historial_brier", {}).get(sk, []))
                st.write(f"**{sk}** — {len(ratings)} equipos, Brier: {brier:.4f if brier else 'N/A'} ({n})")

    st.divider()
    st.caption(f"Forebet: {'ON' if ENABLE_FOREBET else 'OFF'}")
    st.caption(f"EV≥{EV_MINIMO*100:.0f}% · Div≤{DIVERGENCIA_MAXIMA*100:.0f}% · Conf≥{CONFIANZA_MINIMA}")
    st.caption(f"Proxy 5b: div≤{CONSENSO_DIVERGENCIA_CERCANIA*100:.0f}% o EV≥{CONSENSO_EV_CERCANIA*100:.0f}%")

if api_key:
    deportes_lista = obtener_deportes_activos(api_key)
    if deportes_lista:
        opciones = {"🔥 TODOS LOS DEPORTES ACTIVOS": "ALL"}
        for dep in deportes_lista:
            opciones[f"{dep.get('group')} - {dep.get('title')}"] = dep.get("key")
        seleccion = st.selectbox("Selecciona deporte o ámbito:", list(opciones.keys()))
        deporte_key = opciones[seleccion]

        modo = st.radio(
            "Modo de análisis:",
            [
                "Separado por deporte (recomendado — cobertura completa)",
                "Todo en un solo prompt (rápido, menos preciso)",
            ],
        )

        if st.button("🚀 Generar Prompt y Procesar Datos", type="primary"):
            st.session_state.pop("clubelo_match_failures", None)
            with st.spinner("Odds API + ClubElo + Elo + consenso de mercado + EV..."):
                datos = []
                if deporte_key == "ALL":
                    deps = [d for d in deportes_lista if _pasa_whitelist(d.get("key"))]
                    bar = st.progress(0)
                    for i, dep in enumerate(deps):
                        cuotas = obtener_cuotas_api(api_key, dep.get("key"))
                        if cuotas:
                            datos.extend(cuotas)
                        bar.progress((i + 1) / max(len(deps), 1))
                        time.sleep(0.15)
                    bar.empty()
                else:
                    datos = obtener_cuotas_api(api_key, deporte_key)

                estado_elo = cargar_estado_elo()
                for sk in {ev.get("sport_key") for ev in datos if isinstance(ev, dict)}:
                    if _buscar_base_registry(sk).get("usa_elo_interno"):
                        estado_elo = actualizar_elo_sport(api_key, sk, estado_elo)
                guardar_estado_elo(estado_elo)

                tz_rd = timezone(timedelta(hours=-4))
                hora_rd = datetime.now(tz_rd).strftime("%Y-%m-%d %H:%M:%S AST (UTC-4)")
                eventos, resumen = filtrar_y_enriquecer(datos, estado_elo)
                movs = registrar_y_calcular_movimientos(eventos, deporte_key)
                seccion_mov = (
                    "MOVIMIENTOS PINNACLE:\n" + "\n".join(f"- {k}: {v}" for k, v in movs.items())
                    if movs else "SIN SNAPSHOT PREVIO EN ESTA SESIÓN."
                )

                st.write("### 📌 Resumen de Filtrado Backend")
                st.info(resumen)

                if eventos:
                    with st.expander(f"📋 Candidatos ({len(eventos)})", expanded=True):
                        filas = []
                        for ev in eventos:
                            reg = ev.get("_registry_modelo_secundario") or {}
                            ofi = ev.get("_backend_ev") or {}
                            prx = ev.get("_backend_ev_proxy") or {}
                            filas.append({
                                "Partido": ev.get("partido"),
                                "Deporte": ev.get("deporte"),
                                "Cobertura": reg.get("cobertura"),
                                "Liquidez": ev.get("_liquidez_backend"),
                                "EV oficial": (
                                    f"{ofi['mejor_ev']*100:.1f}%" if ofi.get("mejor_ev") is not None else "—"
                                ),
                                "EV proxy": (
                                    f"{prx['mejor_ev']*100:.1f}%" if prx.get("mejor_ev") is not None else "—"
                                ),
                                "5b?": "Sí" if prx.get("senal_cercania_5b") and not ofi else "—",
                            })
                        st.dataframe(pd.DataFrame(filas), use_container_width=True)

                if not eventos:
                    st.warning(f"⚠️ Sin candidatos en rango {CUOTA_MIN}-{CUOTA_MAX} / {VENTANA_HORAS_DEFAULT}h.")
                elif modo.startswith("Todo en un solo prompt"):
                    for k in ("prompts_por_grupo", "resumen_automatico_grupo", "eventos_por_grupo"):
                        st.session_state.pop(k, None)
                    prompt = (
                        f"{SYSTEM_PROMPT_BLINDADO_V3_7}\n\n"
                        f"==================================================\n"
                        f"CONTEXTO BACKEND\n"
                        f"==================================================\n"
                        f"ÁMBITO: {seleccion}\nHORA (RD/UTC-4): {hora_rd}\n\n"
                        f"RESUMEN:\n{resumen}\n\n{seccion_mov}\n\n"
                        f"INSTRUCCIÓN: Usa `_backend_ev` (oficial) y "
                        f"`_backend_ev_proxy`/`_consenso_mercado_devig` (solo 5b).\n\n"
                        f"DATOS JSON:\n{json.dumps(eventos, indent=2, ensure_ascii=False)}"
                    )
                    st.session_state["prompt_generado"] = prompt
                    st.success(f"✅ {len(eventos)} eventos en un solo prompt.")
                else:
                    st.session_state.pop("prompt_generado", None)
                    prompts, auto, por_g = construir_prompts_por_deporte(
                        eventos, seleccion, hora_rd, seccion_mov
                    )
                    st.session_state["prompts_por_grupo"] = prompts
                    st.session_state["resumen_automatico_grupo"] = auto
                    st.session_state["eventos_por_grupo"] = por_g
                    n_ia = sum(len(v) for v in por_g.values())
                    st.success(f"✅ {len(prompts)} grupo(s) · {n_ia} eventos requieren IA.")
                    if auto:
                        with st.expander("📋 Descartes automáticos (0 tokens)"):
                            st.markdown(auto)

    if "prompt_generado" in st.session_state:
        st.divider()
        st.subheader("🤖 IA para el análisis")
        cols = st.columns(5)
        for col, (lab, url) in zip(cols, [
            ("🌐 ChatGPT", "https://chatgpt.com"),
            ("🌐 Claude", "https://claude.ai"),
            ("🌐 Gemini Web", "https://gemini.google.com"),
            ("🌐 DeepSeek", "https://chat.deepseek.com"),
            ("🌐 Copilot", "https://copilot.microsoft.com"),
        ]):
            with col:
                st.link_button(lab, url, use_container_width=True)
        st.write("#### 📋 Prompt")
        st.code(st.session_state["prompt_generado"], language="markdown")

        if gemini_api_key:
            st.divider()
            st.subheader("⚡ Gemini API")
            st.warning("⚠️ Gemini REST en esta app NO busca web. Usa Claude o gemini.google.com.")
            mods = listar_modelos_gemini(gemini_api_key)
            if mods:
                default = next((m for m in mods if "flash" in m and "lite" not in m), mods[0])
                modelo = st.selectbox("Modelo Gemini:", mods, index=mods.index(default))
                if st.button("🤖 Analizar con Gemini", type="primary"):
                    with st.spinner(f"Analizando con {modelo}..."):
                        try:
                            res, uso = llamar_gemini_rest(
                                gemini_api_key, modelo, st.session_state["prompt_generado"]
                            )
                            st.markdown("### 🏆 Resultado")
                            st.markdown(res)
                            prev = st.session_state.get(
                                "gemini_tokens_acumulados", {"prompt": 0, "salida": 0, "total": 0}
                            )
                            prev["prompt"] += uso.get("promptTokenCount", 0) or 0
                            prev["salida"] += uso.get("candidatesTokenCount", 0) or 0
                            prev["total"] += uso.get("totalTokenCount", 0) or 0
                            st.session_state["gemini_tokens_acumulados"] = prev
                        except Exception as e:
                            st.error(f"Error Gemini: {e}")

        if anthropic_api_key:
            st.divider()
            st.subheader("⚡ Claude API (web search forzado)")
            mods_c = listar_modelos_claude(anthropic_api_key)
            if mods_c:
                default_c = next((m for m in mods_c if "sonnet" in m.lower()), mods_c[0])
                modelo_c = st.selectbox("Modelo Claude:", mods_c, index=mods_c.index(default_c))
                if st.button("🤖 Analizar con Claude", type="primary"):
                    with st.spinner(f"Analizando con {modelo_c}..."):
                        try:
                            res, qs, uso, rl = llamar_claude_rest(
                                anthropic_api_key, modelo_c, st.session_state["prompt_generado"]
                            )
                            st.markdown("### 🏆 Resultado")
                            st.markdown(res)
                            if qs:
                                with st.expander(f"🔍 Búsquedas ({len(qs)})"):
                                    for q in qs:
                                        st.write(f"- {q}")
                            else:
                                st.info("Sin búsquedas web en esta corrida.")
                            ent = uso.get("input_tokens", 0) or 0
                            sal = uso.get("output_tokens", 0) or 0
                            prev = st.session_state.get(
                                "claude_tokens_acumulados", {"entrada": 0, "salida": 0, "total": 0}
                            )
                            prev["entrada"] += ent
                            prev["salida"] += sal
                            prev["total"] += ent + sal
                            st.session_state["claude_tokens_acumulados"] = prev
                            st.session_state["claude_ratelimit"] = rl
                            st.caption(f"Tokens: {ent:,} in · {sal:,} out")
                        except Exception as e:
                            st.error(f"Error Claude: {e}")

    if "prompts_por_grupo" in st.session_state and st.session_state["prompts_por_grupo"]:
        st.divider()
        st.subheader("🧩 Modo por deporte")
        prompts = st.session_state["prompts_por_grupo"]
        por_g = st.session_state.get("eventos_por_grupo", {})
        for fam, txt in prompts.items():
            with st.expander(f"📋 {fam.upper()} ({len(por_g.get(fam, []))} eventos)"):
                st.code(txt, language="markdown")

        if anthropic_api_key:
            st.subheader("⚡ Ejecutar TODOS los grupos (Claude)")
            mods_g = listar_modelos_claude(anthropic_api_key)
            if mods_g:
                def_g = next((m for m in mods_g if "sonnet" in m.lower()), mods_g[0])
                modelo_g = st.selectbox(
                    "Modelo Claude (por deporte):", mods_g,
                    index=mods_g.index(def_g), key="modelo_por_deporte",
                )
                if st.button("🤖 Analizar TODOS los grupos", type="primary", key="btn_todos"):
                    tot_in = tot_out = 0
                    consolidados = []
                    for fam, txt in prompts.items():
                        with st.spinner(f"Analizando {fam.upper()}..."):
                            try:
                                res, qs, uso, _ = llamar_claude_rest(
                                    anthropic_api_key, modelo_g, txt
                                )
                                st.markdown(f"### 🏆 {fam.upper()}")
                                st.markdown(res)
                                consolidados.append(f"## {fam.upper()}\n\n{res}")
                                if qs:
                                    with st.expander(f"🔍 {fam.upper()} ({len(qs)})"):
                                        for q in qs:
                                            st.write(f"- {q}")
                                ent = uso.get("input_tokens", 0) or 0
                                sal = uso.get("output_tokens", 0) or 0
                                tot_in += ent
                                tot_out += sal
                                prev = st.session_state.get(
                                    "claude_tokens_acumulados",
                                    {"entrada": 0, "salida": 0, "total": 0},
                                )
                                prev["entrada"] += ent
                                prev["salida"] += sal
                                prev["total"] += ent + sal
                                st.session_state["claude_tokens_acumulados"] = prev
                            except Exception as e:
                                st.error(f"Error {fam.upper()}: {e}")
                    st.success(
                        f"✅ Total: {tot_in:,} in · {tot_out:,} out · "
                        f"{tot_in + tot_out:,} tokens ({len(prompts)} grupos)."
                    )
                    if consolidados:
                        with st.expander("📦 Informe consolidado"):
                            st.markdown("\n\n---\n\n".join(consolidados))
