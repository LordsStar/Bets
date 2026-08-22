"""
Analista de Apuesta Única — v3.8.1
Fixes v3.8.1:
  - ClubElo: timeout 4s, fail-fast, UNA sola precarga por corrida
  - Odds API: no escribe session_state dentro de cache
  - ALL: sleep 0.05 + filtro prioritario opcional
  - Consenso min=1, más bookmakers, Brier sidebar OK
Requisitos: streamlit, pandas, beautifulsoup4, requests
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
# CONSTANTES
# ==============================================================================
EV_MINIMO = 0.04
DIVERGENCIA_MAXIMA = 0.09
CONFIANZA_MINIMA = 8
CUOTA_MIN = 1.40
CUOTA_MAX = 2.00
VENTANA_HORAS_DEFAULT = 24

CONSENSO_MIN_CASAS = 1
CONSENSO_DIVERGENCIA_CERCANIA = 0.06
CONSENSO_EV_CERCANIA = 0.02
CONFIANZA_TOPE_CONSENSO = 7

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
ODDS_REGIONS = "us,uk,eu,au"
ODDS_BOOKMAKERS = (
    "pinnacle,bet365,draftkings,fanduel,williamhill,"
    "betfair_ex_uk,unibet_eu,betonlineag,lowvig,bovada,"
    "pointsbetau,tab,neds,sportsbet,ladbrokes_au,"
    "coral,paddypower,skybet,betway,betrivers,caesars"
)
# En modo ALL, solo estas familias (pon [] para todos los deportes activos)
ALL_SPORTS_PRIORITY = ("baseball", "soccer", "basketball", "tennis", "icehockey", "americanfootball")
ALL_SPORTS_SLEEP = 0.05

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
ANTHROPIC_API_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"

ODDS_YELLOW_THRESHOLD = 100
ODDS_RED_THRESHOLD = 20
CLAUDE_TOKEN_WARNING = 500_000
GEMINI_TOKEN_WARNING = 500_000
CLAUDE_WEB_SEARCH_MAX_USES = 8

ENABLE_FOREBET = False
CLUBELO_TIMEOUT = 4
CLUBELO_API_BASES = ("https://api.clubelo.com", "http://api.clubelo.com")
FOREBET_URL = "https://www.forebet.com/en/football-tips-and-predictions-for-today"
SPORTS_WHITELIST_PREFIXES = []
REGISTRY_ULTIMA_REVISION = "2026-08-22"

SYSTEM_PROMPT_BLINDADO_V3_8 = f"""
PROMPT — Analista Cuantitativo de Apuesta Única (Blindado v3.8)

ROL: Selecciona UNA apuesta (cuota {CUOTA_MIN:.2f}-{CUOTA_MAX:.2f}) o 0 picks si nada califica.
Nunca fuerces un pick.

1. Usa `_pinnacle_devig` sin recalcular.
1b. Si hay `_backend_ev` (oficial): úsalo. Si hay `_backend_ev_proxy` (consenso):
    SOLO para sección 5b / ranking. NUNCA pick del día. Confianza tope {CONFIANZA_TOPE_CONSENSO}/10.
    EV = (prob_modelo * cuota) - 1. Divergencia = |pinnacle_devig - prob_modelo|.

2. Frescura: si faltan <3h al inicio Y last_update >90 min → descarta. Si faltan >3h, no descartes por antigüedad.

3. Segundo modelo SOLO vía `_registry_modelo_secundario`:
   - externa_directa: busca fuente_primaria/secundaria (FanGraphs con ?date=YYYY-MM-DD; ClubElo en api.clubelo.com).
   - modelo_interno_elo / modelo_externo_backend: usa campos del JSON.
   - pendiente_desarrollo / excluido_estructural: descarta.
   PROHIBIDO 538 SPI y fuentes no listadas.
   Consenso de mercado NO es 2º modelo oficial.

3b. Tenis/boxeo/MMA: chequeo lesiones 48-72h.

4. Liquidez: <2 casas → no califica.
5. Umbrales: EV<{EV_MINIMO*100:.0f}% cat4; Div>{DIVERGENCIA_MAXIMA*100:.0f}% cat5; Conf<{CONFIANZA_MINIMA} cat6.
6. Pick solo si confianza>={CONFIANZA_MINIMA} Y 2º modelo OFICIAL (no proxy).

Categorías (una sola, en orden): 1 frescura, 2 sin 2º modelo oficial, 3 liquidez, 4 EV, 5 divergencia, 6 confianza.
Auto-check: suma descartes + (1 si pick) = total eventos del JSON.

FORMATO:
1. Resumen + 6 categorías + verificación.
2. Pick o "PICK DEL DÍA: NINGUNO".
3. Tabla transparencia solo cat 4/5/6.
5a. CASI CALIFICÓ con números oficiales (cat 4/5/6).
5b. CASI CALIFICÓ bloqueados por fuente (cat2) con `_backend_ev_proxy` si div<=6% o EV proxy>=2%. Nunca son pick.
"""

MODEL_REGISTRY = [
    {"patron": "americanfootball_nfl_preseason", "fuente_primaria": None, "fuente_secundaria": None,
     "cobertura": "excluido_estructural", "version": "1.0", "usa_elo_interno": False},
    {"patron": "soccer", "fuente_primaria": "ClubElo (api.clubelo.com/Fixtures)", "fuente_secundaria": None,
     "fuente_respaldo": "Forebet (sin metodología pública verificable)",
     "cobertura": "externa_directa", "version": "2.2", "usa_elo_interno": False},
    {"patron": "tennis", "fuente_primaria": "TennisAbstract (Elo por superficie)",
     "fuente_secundaria": "Ranking oficial ATP/WTA", "cobertura": "externa_directa", "version": "1.0", "usa_elo_interno": False},
    {"patron": "baseball_mlb", "fuente_primaria": "FanGraphs (usar SIEMPRE ?date=YYYY-MM-DD)", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)", "cobertura": "externa_directa", "version": "1.1", "usa_elo_interno": False},
    {"patron": "baseball_kbo", "fuente_primaria": None, "fuente_secundaria": None,
     "cobertura": "pendiente_desarrollo", "version": "1.0", "usa_elo_interno": True},
    {"patron": "baseball_npb", "fuente_primaria": None, "fuente_secundaria": None,
     "cobertura": "pendiente_desarrollo", "version": "1.0", "usa_elo_interno": True},
    {"patron": "basketball_nba", "fuente_primaria": "Basketball-Reference", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)", "cobertura": "externa_directa", "version": "1.0", "usa_elo_interno": False},
    {"patron": "basketball_wnba", "fuente_primaria": "Basketball-Reference", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)", "cobertura": "externa_directa", "version": "1.0", "usa_elo_interno": False},
    {"patron": "basketball_ncaab", "fuente_primaria": "Basketball-Reference (NCAA)", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)", "cobertura": "externa_directa", "version": "1.0", "usa_elo_interno": False},
    {"patron": "icehockey_nhl", "fuente_primaria": "Hockey-Reference", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)", "cobertura": "externa_directa", "version": "1.0", "usa_elo_interno": False},
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
    sk = sport_key.lower()
    return dict(next((e for e in MODEL_REGISTRY if e["patron"] in sk), DEFAULT_REGISTRY_ENTRY))

# ==============================================================================
# ELO INTERNO
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
        st.warning(f"No se pudo guardar Elo: {e}")

def _prob_elo(elo_a, elo_b):
    return 1.0 / (1.0 + 10 ** ((elo_b - elo_a) / 400.0))

def calcular_brier(historial_sport):
    if not historial_sport:
        return None
    return sum((h["prob"] - h["resultado"]) ** 2 for h in historial_sport) / len(historial_sport)

@st.cache_data(ttl=3600, show_spinner=False)
def obtener_scores_api(api_key, sport_key, dias=ELO_DIAS_HISTORIAL_SCORES):
    url = f"{ODDS_API_BASE}/sports/{sport_key}/scores/"
    try:
        r = requests.get(url, params={"apiKey": api_key, "daysFrom": dias}, timeout=8)
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
        home_team, away_team, scores = evento.get("home_team"), evento.get("away_team"), evento.get("scores")
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
    home, away = ratings.get(home_team), ratings.get(away_team)
    historial = estado.get("historial_brier", {}).get(sport_key, [])
    brier = calcular_brier(historial)
    ok = (
        home and away
        and home["partidos"] >= ELO_MIN_PARTIDOS_POR_EQUIPO
        and away["partidos"] >= ELO_MIN_PARTIDOS_POR_EQUIPO
        and brier is not None
        and len(historial) >= ELO_MIN_MUESTRAS_BRIER
        and brier <= ELO_BRIER_MAXIMO_ACEPTABLE
    )
    if not ok:
        return None
    ventaja = 0.0 if sport_key and "mma" in sport_key.lower() else ELO_VENTAJA_LOCAL
    prob_home = _prob_elo(home["elo"] + ventaja, away["elo"])
    return {
        "fuente_primaria": "Modelo Elo interno (backend)",
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
# CLUBELO (rápido)
# ==============================================================================
CLUBELO_NAME_ALIASES = {
    "manchester united": "Man United", "manchester city": "Man City",
    "tottenham hotspur": "Tottenham", "tottenham": "Tottenham",
    "wolverhampton wanderers": "Wolves", "wolverhampton": "Wolves",
    "nottingham forest": "Forest", "brighton and hove albion": "Brighton",
    "brighton & hove albion": "Brighton", "west ham united": "West Ham",
    "newcastle united": "Newcastle", "leicester city": "Leicester",
    "leeds united": "Leeds", "aston villa": "Aston Villa", "crystal palace": "Crystal Palace",
    "sheffield united": "Sheffield United", "ipswich town": "Ipswich", "southampton": "Southampton",
    "fulham": "Fulham", "brentford": "Brentford", "bournemouth": "Bournemouth",
    "afc bournemouth": "Bournemouth", "everton": "Everton", "chelsea": "Chelsea",
    "arsenal": "Arsenal", "liverpool": "Liverpool",
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
    "lazio": "Lazio", "atalanta": "Atalanta", "atalanta bc": "Atalanta",
    "fiorentina": "Fiorentina", "torino": "Torino", "bologna": "Bologna",
    "genoa": "Genoa", "udinese": "Udinese", "sassuolo": "Sassuolo",
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
    "galatasaray": "Galatasaray", "fenerbahce": "Fenerbahce", "besiktas": "Besiktas", "besiktas jk": "Besiktas",
    "olympiacos": "Olympiakos", "olympiakos": "Olympiakos",
    "shakhtar donetsk": "Shakhtar", "dynamo kyiv": "Dynamo Kyiv",
    "red bull salzburg": "Salzburg", "fc salzburg": "Salzburg",
    "young boys": "Young Boys", "club brugge": "Club Brugge", "anderlecht": "Anderlecht",
    "inter miami": "Inter Miami", "atlanta united": "Atlanta", "la galaxy": "LA Galaxy",
    "los angeles fc": "Los Angeles FC", "lafc": "Los Angeles FC",
    "seattle sounders": "Seattle", "seattle sounders fc": "Seattle",
    "portland timbers": "Portland", "vancouver whitecaps": "Vancouver",
    "vancouver whitecaps fc": "Vancouver", "fc dallas": "Dallas",
    "columbus crew": "Columbus", "columbus crew sc": "Columbus",
    "nashville sc": "Nashville", "charlotte fc": "Charlotte",
    "dc united": "DC United", "d.c. united": "DC United",
    "fc cincinnati": "Cincinnati", "new york city fc": "NYCFC",
    "new york red bulls": "NY Red Bulls", "orlando city": "Orlando",
    "philadelphia union": "Philadelphia", "chicago fire": "Chicago Fire",
    "sporting kansas city": "Sporting KC", "real salt lake": "Salt Lake",
    "minnesota united": "Minnesota", "houston dynamo": "Houston",
    "san jose earthquakes": "San Jose", "colorado rapids": "Colorado",
    "cf montreal": "Montreal", "toronto fc": "Toronto",
    "guadalajara": "Guadalajara", "chivas": "Guadalajara",
    "cruz azul": "Cruz Azul", "tijuana": "Tijuana", "atlas": "Atlas",
    "fluminense": "Fluminense", "remo": "Remo", "ceara": "Ceara", "ceará": "Ceara",
    "londrina": "Londrina", "gimnasia la plata": "Gimnasia LP",
    "gimnasia mendoza": "Gimnasia Mza", "universidad catolica": "U Catolica",
    "universidad católica (chi)": "U Catolica", "ñublense": "Nublense",
    "nublense": "Nublense", "la serena": "La Serena", "cobresal": "Cobresal",
    "paok thessaloniki": "PAOK", "levadiakos": "Levadiakos",
    "frosinone": "Frosinone", "heerenveen": "Heerenveen", "fc zwolle": "Zwolle",
    "go ahead eagles": "Go Ahead Eagles", "ado den haag": "Den Haag",
    "gks katowice": "Katowice", "wisla plock": "Wisla Plock", "wisła płock": "Wisla Plock",
    "vitoria sc": "Vitoria", "vitória sc": "Vitoria", "nacional": "Nacional",
    "fc dynamo makhachkala": "Makhachkala", "fc krasnodar": "Krasnodar",
    "cd castellon": "Castellon", "cd castellón": "Castellon", "sabadell fc": "Sabadell",
    "hammarby if": "Hammarby", "gais": "GAIS", "ik oddevold": "Oddevold",
    "helsingborgs if": "Helsingborg", "alanyaspor": "Alanyaspor",
    "goztepe": "Goztepe", "genclerbirligi sk": "Genclerbirligi",
    "fc machida zelvia": "Machida", "urawa red diamonds": "Urawa",
    "gwangju fc": "Gwangju", "incheon united": "Incheon",
    "hjk helsinki": "HJK", "if gnistan": "Gnistan", "tps turku": "TPS",
    "fc inter turku": "Inter Turku", "sonderjyske": "SonderjyskE",
    "fc nordsjaelland": "Nordsjaelland", "agf aarhus": "AGF",
    "ob odense bk": "Odense", "fc midtjylland": "Midtjylland", "randers fc": "Randers",
}

def _strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))

def _normalizar_nombre(nombre):
    if not nombre:
        return ""
    s = _strip_accents(nombre).lower().strip()
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for tok in (" fc", " cf", " afc", " sc", " ac", " as", " fk", " nk", " bk", " if"):
        if s.endswith(tok):
            s = s[: -len(tok)].strip()
    return s

def _tokens(nombre):
    stop = {"fc", "cf", "afc", "sc", "ac", "as", "the", "de", "club", "united", "city"}
    return {t for t in _normalizar_nombre(nombre).split() if t and t not in stop}

def resolver_nombre_clubelo(nombre_odds, nombres_clubelo_disponibles=None):
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
        tok_q = _tokens(nombre_odds)
        best, best_score = None, 0.0
        for original in norm_map.values():
            tok_n = _tokens(original)
            if not tok_q or not tok_n:
                continue
            inter, union = len(tok_q & tok_n), len(tok_q | tok_n)
            score = inter / union if union else 0.0
            if score > best_score:
                best_score, best = score, original
        if best is not None and best_score >= 0.6:
            return best, f"fuzzy_{best_score:.2f}"
    return None, "sin_match"

def _fuzzy_clubelo_libre(nombre, nombres_disp, umbral=0.45):
    if not nombre or not nombres_disp:
        return None, "vacio"
    limpio = _normalizar_nombre(nombre)
    tok_q = {t for t in limpio.split() if len(t) > 2}
    if not tok_q:
        return None, "sin_tokens"
    best, best_score = None, 0.0
    for original in nombres_disp:
        tok_n = {t for t in _normalizar_nombre(original).split() if len(t) > 2}
        if not tok_n:
            continue
        inter = len(tok_q & tok_n)
        union = len(tok_q | tok_n)
        score = inter / union if union else 0.0
        longest = max(tok_q, key=len)
        if longest in _normalizar_nombre(original):
            score += 0.15
        if score > best_score:
            best_score, best = score, original
    if best is not None and best_score >= umbral:
        return best, f"fuzzy_libre_{best_score:.2f}"
    return None, "sin_match"

def _log_clubelo_failure(home, away, reason):
    fails = st.session_state.setdefault("clubelo_match_failures", [])
    entry = f"{home} vs {away} — {reason}"
    if entry not in fails:
        fails.append(entry)
        st.session_state["clubelo_match_failures"] = fails[-50:]

@st.cache_data(ttl=1800, show_spinner=False)
def obtener_fixtures_clubelo(fecha_yyyy_mm_dd=None):
    """Timeout corto. Si ClubElo no responde, None en pocos segundos."""
    paths = []
    if fecha_yyyy_mm_dd:
        paths.append(f"/Fixtures/{fecha_yyyy_mm_dd}")
    paths.append("/Fixtures")
    for base in CLUBELO_API_BASES:
        for path in paths:
            try:
                r = requests.get(
                    base + path,
                    timeout=CLUBELO_TIMEOUT,
                    headers={"User-Agent": "BlindadoBot/3.8.1", "Accept": "text/csv"},
                )
                if r.status_code != 200:
                    continue
                texto = (r.text or "").strip()
                if not texto or texto.lower().startswith("site overloaded"):
                    continue
                df = pd.read_csv(StringIO(r.text))
                if df is not None and not df.empty and "Home" in df.columns:
                    return df
            except Exception:
                continue
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
    home_r, mh = resolver_nombre_clubelo(home_team, todos)
    away_r, ma = resolver_nombre_clubelo(away_team, todos)
    if not home_r or not away_r:
        home_r2, mh2 = _fuzzy_clubelo_libre(home_team, todos)
        away_r2, ma2 = _fuzzy_clubelo_libre(away_team, todos)
        if home_r2 and away_r2:
            home_r, away_r, mh, ma = home_r2, away_r2, mh2, ma2
        else:
            _log_clubelo_failure(home_team, away_team, f"match fallido ({mh}/{ma})")
            return None

    def _eq(a, b):
        return str(a).strip().lower() == str(b).strip().lower()

    try:
        match = df[df["Home"].map(lambda x: _eq(x, home_r)) & df["Away"].map(lambda x: _eq(x, away_r))]
        invertido = False
        if match.empty:
            match = df[df["Home"].map(lambda x: _eq(x, away_r)) & df["Away"].map(lambda x: _eq(x, home_r))]
            invertido = not match.empty
        if match.empty:
            _log_clubelo_failure(home_team, away_team, f"no en Fixtures ({home_r}/{away_r})")
            return None
        row = match.iloc[0]
        cols_gd = [c for c in df.columns if _es_columna_gd(c)]
        if not cols_gd:
            return None
        ph = pa = pd_ = 0.0
        for c in cols_gd:
            try:
                val, gd = float(row[c]), int(str(c).strip())
            except (ValueError, TypeError):
                continue
            if gd > 0:
                ph += val
            elif gd < 0:
                pa += val
            else:
                pd_ += val
        if invertido:
            ph, pa = pa, ph
        total = ph + pd_ + pa
        if total <= 0 or abs(total - 1.0) > 0.05:
            _log_clubelo_failure(home_team, away_team, f"suma probs={total:.4f}")
            return None
        return {
            "prob_home": round(ph / total, 4),
            "prob_draw": round(pd_ / total, 4),
            "prob_away": round(pa / total, 4),
            "clubelo_home": home_r,
            "clubelo_away": away_r,
            "match_method": f"{mh}/{ma}",
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
            FOREBET_URL, timeout=8,
            headers={"User-Agent": "Mozilla/5.0 (compatible; BlindadoBot/3.8.1)"},
        )
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        filas = soup.select("div.rcnt") or soup.select(".rcnt")
        hn, an = _normalizar_nombre(home_team), _normalizar_nombre(away_team)
        for fila in filas:
            he = fila.select_one(".homeTeam") or fila.select_one("span.homeTeam")
            ae = fila.select_one(".awayTeam") or fila.select_one("span.awayTeam")
            if he and ae:
                if hn not in _normalizar_nombre(he.get_text()) or an not in _normalizar_nombre(ae.get_text()):
                    continue
            else:
                tx = _normalizar_nombre(fila.get_text(" ", strip=True))
                if hn not in tx or an not in tx:
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
                a, b, c = [float(x) / 100.0 for x in probs[:3]]
            except ValueError:
                continue
            if abs(a + b + c - 1.0) > 0.05:
                continue
            return {"prob_home": round(a, 4), "prob_draw": round(b, 4), "prob_away": round(c, 4)}
    except Exception:
        return None
    return None

def obtener_entrada_clubelo_o_forebet(sport_key, home_team, away_team, commence_utc=None, df_preloaded=None):
    if not sport_key or not sport_key.startswith("soccer") or not home_team or not away_team:
        return None
    df = df_preloaded
    if df is None:
        fecha = None
        if commence_utc:
            try:
                fecha = datetime.fromisoformat(commence_utc.replace("Z", "+00:00")).strftime("%Y-%m-%d")
            except Exception:
                fecha = None
        df = obtener_fixtures_clubelo(fecha)
        if df is None and fecha:
            df = obtener_fixtures_clubelo(None)
    res = _prob_desde_fixtures_clubelo(df, home_team, away_team)
    if res:
        return {
            "fuente_primaria": "ClubElo (api.clubelo.com/Fixtures) — resuelto directo por backend",
            "fuente_secundaria": None,
            "fuente_respaldo": "Forebet (sin metodología pública verificable)",
            "cobertura": "modelo_externo_backend",
            "version": "1.2",
            "ultima_revision": REGISTRY_ULTIMA_REVISION,
            "probabilidad_home": res["prob_home"],
            "probabilidad_draw": res["prob_draw"],
            "probabilidad_away": res["prob_away"],
            "clubelo_match": res.get("match_method"),
            "clubelo_nombres": f"{res.get('clubelo_home')} vs {res.get('clubelo_away')}",
        }
    if ENABLE_FOREBET:
        fb = obtener_prediccion_forebet(home_team, away_team)
        if fb:
            return {
                "fuente_primaria": "ClubElo — sin dato",
                "fuente_secundaria": None,
                "fuente_respaldo": "Forebet (respaldo) — backend",
                "cobertura": "modelo_externo_backend",
                "version": "1.2",
                "ultima_revision": REGISTRY_ULTIMA_REVISION,
                "probabilidad_home": fb["prob_home"],
                "probabilidad_draw": fb["prob_draw"],
                "probabilidad_away": fb["prob_away"],
                "metodologia_publica_respaldo": False,
            }
    return None

def obtener_entrada_registry(
    sport_key, home_team=None, away_team=None, estado_elo=None, commence_utc=None, df_clubelo=None
):
    base = _buscar_base_registry(sport_key)
    if sport_key and sport_key.startswith("soccer") and home_team and away_team:
        r = obtener_entrada_clubelo_o_forebet(
            sport_key, home_team, away_team,
            commence_utc=commence_utc, df_preloaded=df_clubelo,
        )
        if r:
            return r
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
        base["nota"] = (
            f"Elo insuficiente ({home_team}: {ratings.get(home_team, {}).get('partidos', 0)}, "
            f"{away_team}: {ratings.get(away_team, {}).get('partidos', 0)})"
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
    """Devuelve (eventos, headers_uso). No escribe session_state dentro del cache."""
    url = f"{ODDS_API_BASE}/sports/{sport_key}/odds/"
    params = {
        "apiKey": api_key,
        "regions": ODDS_REGIONS,
        "markets": "h2h",
        "oddsFormat": "decimal",
        "bookmakers": ODDS_BOOKMAKERS,
    }
    try:
        response = requests.get(url, params=params, timeout=12)
        headers_uso = {
            "restantes": response.headers.get("x-requests-remaining"),
            "usados": response.headers.get("x-requests-used"),
        }
        if response.status_code == 401:
            return [], headers_uso
        if response.status_code == 422:
            params.pop("bookmakers", None)
            response = requests.get(url, params=params, timeout=12)
            headers_uso = {
                "restantes": response.headers.get("x-requests-remaining"),
                "usados": response.headers.get("x-requests-used"),
            }
            if response.status_code != 200:
                return [], headers_uso
        if response.status_code == 429:
            return [], headers_uso
        response.raise_for_status()
        return response.json(), headers_uso
    except Exception:
        return [], {}


def _aplicar_headers_odds(headers_uso):
    if headers_uso and headers_uso.get("restantes") is not None:
        st.session_state["odds_api_uso"] = headers_uso


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
        h2h = next(
            (m for m in b.get("markets", []) if isinstance(m, dict) and m.get("key") == "h2h"),
            None,
        )
        if not h2h:
            continue
        for nombre, prob in devig_probabilidades(h2h.get("outcomes", [])).items():
            probs_por_resultado.setdefault(nombre, []).append(prob)
    dispersiones = [max(v) - min(v) for v in probs_por_resultado.values() if len(v) >= 2]
    return max(dispersiones) if dispersiones else 0.0


def calcular_consenso_mercado(evento_crudo):
    """Media de-vig de casas NO Pinnacle. Proxy para 5b — NUNCA 2º modelo oficial."""
    if not isinstance(evento_crudo, dict):
        return None
    probs_por_nombre = {}
    casas_usadas = []
    for b in evento_crudo.get("bookmakers", []):
        if not isinstance(b, dict) or b.get("key") == "pinnacle":
            continue
        h2h = next(
            (m for m in b.get("markets", []) if isinstance(m, dict) and m.get("key") == "h2h"),
            None,
        )
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


def _pasa_prioridad_all(sport_key):
    if not ALL_SPORTS_PRIORITY:
        return True
    if not sport_key:
        return False
    sk = sport_key.lower()
    return any(sk.startswith(p.lower()) for p in ALL_SPORTS_PRIORITY)


def filtrar_y_enriquecer(datos_crudos, estado_elo, horas_ventana=VENTANA_HORAS_DEFAULT):
    if not datos_crudos or not isinstance(datos_crudos, list):
        return [], "Backend pre-filtró 0 eventos (sin datos recibidos)."

    # UNA sola precarga ClubElo (evita N × timeout)
    df_clubelo_cache = obtener_fixtures_clubelo(None)

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
        commence_str = evento.get("commence_time")

        registry_entry = obtener_entrada_registry(
            evento.get("sport_key"),
            home_team=home_team,
            away_team=away_team,
            estado_elo=estado_elo,
            commence_utc=commence_str,
            df_clubelo=df_clubelo_cache,
        )
        if registry_entry["cobertura"] == "excluido_estructural":
            c["estructural"] += 1
            continue

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

        pre_oficial = precalcular_ev_oficial(evento_minificado)
        if pre_oficial:
            evento_minificado["_backend_ev"] = pre_oficial
            c["ev_oficial"] += 1

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

    clubelo_ok = "sí" if df_clubelo_cache is not None else "no (timeout/sin datos)"
    resumen = (
        f"Backend pre-filtró {len(datos_crudos)} eventos: "
        f"{len(eventos_validos)} candidatos (prx {horas_ventana}h, cuota {CUOTA_MIN}-{CUOTA_MAX}), "
        f"{c['fecha']} fuera de ventana, {c['sin_fecha']} sin fecha, "
        f"{c['sin_pinnacle']} sin Pinnacle, {c['fuera_rango']} fuera de rango, "
        f"{c['estructural']} estructurales. "
        f"De los candidatos: {c['elo']} Elo interno, {c['backend']} ClubElo/backend, "
        f"{c['pendiente']} pendiente, {c['ev_oficial']} con EV oficial, "
        f"{c['ev_proxy']} con EV proxy (consenso), "
        f"{c['proxy_5b']} elegibles para Casi-calificó 5b. "
        f"ClubElo precarga: {clubelo_ok}."
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
        f"{SYSTEM_PROMPT_BLINDADO_V3_8}\n\n"
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
# 4. INTERFAZ STREAMLIT
# ==============================================================================

st.set_page_config(page_title="Analista Cuantitativo de Apuestas", layout="wide")
st.title(
    "📊 Analista de Apuesta Única v3.8.1 "
    "(ClubElo rápido · proxy 5b · Multi-IA)"
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
            icono = (
                "🟢" if restantes > ODDS_YELLOW_THRESHOLD
                else ("🟡" if restantes > ODDS_RED_THRESHOLD else "🔴")
            )
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
                brier = calcular_brier(
                    estado_elo_sidebar.get("historial_brier", {}).get(sk, [])
                )
                n = len(estado_elo_sidebar.get("historial_brier", {}).get(sk, []))
                brier_txt = f"{brier:.4f}" if brier is not None else "N/A"
                st.write(
                    f"**{sk}** — {len(ratings)} equipos, Brier: {brier_txt} ({n})"
                )

    st.divider()
    st.caption(f"Forebet: {'ON' if ENABLE_FOREBET else 'OFF'}")
    st.caption(f"ClubElo timeout: {CLUBELO_TIMEOUT}s")
    st.caption(
        f"EV≥{EV_MINIMO*100:.0f}% · Div≤{DIVERGENCIA_MAXIMA*100:.0f}% · Conf≥{CONFIANZA_MINIMA}"
    )
    st.caption(
        f"Proxy 5b: div≤{CONSENSO_DIVERGENCIA_CERCANIA*100:.0f}% "
        f"o EV≥{CONSENSO_EV_CERCANIA*100:.0f}% · min casas={CONSENSO_MIN_CASAS}"
    )
    if ALL_SPORTS_PRIORITY:
        st.caption(f"ALL prioriza: {', '.join(ALL_SPORTS_PRIORITY)}")

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
            with st.spinner("Odds API + ClubElo (1 precarga) + Elo + consenso + EV..."):
                datos = []
                if deporte_key == "ALL":
                    deps = [
                        d for d in deportes_lista
                        if _pasa_whitelist(d.get("key")) and _pasa_prioridad_all(d.get("key"))
                    ]
                    bar = st.progress(0)
                    status = st.empty()
                    for i, dep in enumerate(deps):
                        status.caption(f"Consultando {dep.get('key')} ({i+1}/{len(deps)})…")
                        cuotas, headers_uso = obtener_cuotas_api(api_key, dep.get("key"))
                        _aplicar_headers_odds(headers_uso)
                        if cuotas:
                            datos.extend(cuotas)
                        bar.progress((i + 1) / max(len(deps), 1))
                        time.sleep(ALL_SPORTS_SLEEP)
                    bar.empty()
                    status.empty()
                else:
                    cuotas, headers_uso = obtener_cuotas_api(api_key, deporte_key)
                    _aplicar_headers_odds(headers_uso)
                    datos = cuotas or []

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
                    "MOVIMIENTOS PINNACLE:\n"
                    + "\n".join(f"- {k}: {v}" for k, v in movs.items())
                    if movs
                    else "SIN SNAPSHOT PREVIO EN ESTA SESIÓN."
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
                                "N casas": ev.get("_n_casas_reportando"),
                                "EV oficial": (
                                    f"{ofi['mejor_ev']*100:.1f}%"
                                    if ofi.get("mejor_ev") is not None
                                    else "—"
                                ),
                                "EV proxy": (
                                    f"{prx['mejor_ev']*100:.1f}%"
                                    if prx.get("mejor_ev") is not None
                                    else "—"
                                ),
                                "5b?": (
                                    "Sí"
                                    if prx.get("senal_cercania_5b") and not ofi
                                    else "—"
                                ),
                            })
                        st.dataframe(pd.DataFrame(filas), use_container_width=True)

                if not eventos:
                    st.warning(
                        f"⚠️ Sin candidatos en rango {CUOTA_MIN}-{CUOTA_MAX} / "
                        f"{VENTANA_HORAS_DEFAULT}h."
                    )
                elif modo.startswith("Todo en un solo prompt"):
                    for k in ("prompts_por_grupo", "resumen_automatico_grupo", "eventos_por_grupo"):
                        st.session_state.pop(k, None)
                    prompt = (
                        f"{SYSTEM_PROMPT_BLINDADO_V3_8}\n\n"
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
            st.warning(
                "⚠️ Gemini REST en esta app NO busca web. Usa Claude o gemini.google.com."
            )
            mods = listar_modelos_gemini(gemini_api_key)
            if mods:
                default = next(
                    (m for m in mods if "flash" in m and "lite" not in m), mods[0]
                )
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
                                "gemini_tokens_acumulados",
                                {"prompt": 0, "salida": 0, "total": 0},
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
                default_c = next(
                    (m for m in mods_c if "sonnet" in m.lower()), mods_c[0]
                )
                modelo_c = st.selectbox(
                    "Modelo Claude:", mods_c, index=mods_c.index(default_c)
                )
                if st.button("🤖 Analizar con Claude", type="primary"):
                    with st.spinner(f"Analizando con {modelo_c}..."):
                        try:
                            res, qs, uso, rl = llamar_claude_rest(
                                anthropic_api_key,
                                modelo_c,
                                st.session_state["prompt_generado"],
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
                                "claude_tokens_acumulados",
                                {"entrada": 0, "salida": 0, "total": 0},
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
                    "Modelo Claude (por deporte):",
                    mods_g,
                    index=mods_g.index(def_g),
                    key="modelo_por_deporte",
                )
                if st.button(
                    "🤖 Analizar TODOS los grupos", type="primary", key="btn_todos"
                ):
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
