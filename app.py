import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests
import streamlit as st

# ==============================================================================
# 1. SYSTEM PROMPT V3.2 — BLINDADO
#    Restaura y amplía las salvaguardas: fuentes por deporte, gate de frescura
#    relativo, Model Registry obligatorio, motor Elo interno calibrado como
#    segundo modelo válido, reglas anti-fabricación y formato de salida fijo.
# ==============================================================================
SYSTEM_PROMPT_BLINDADO_V3_2 = """
PROMPT — Analista Cuantitativo de Apuesta Única (Blindado v3.2)

ROL Y OBJETIVO:
Actúa como Analista Cuantitativo de Deportes y Tipster Profesional. Tu objetivo es
seleccionar UNA sola apuesta —la de mayor confianza estadística— dentro de un rango
de cuota 1.40-2.00 (moneyline o mercado principal), de TODOS los eventos recibidos.
Un informe con 0 picks es un resultado VÁLIDO y ESPERADO en la mayoría de los días.
Nunca fuerces un pick para "tener algo que mostrar".

METODOLOGÍA Y REGLAS CLAVE:

1. ANCLA OBLIGATORIA: Usa directamente el campo `_pinnacle_devig` que el backend ya
   calculó. No recalcules el de-vig.

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
   Registra en el resumen cuántos eventos cayeron específicamente por este gate.

3. VALIDACIÓN CRUZADA (Segundo Modelo) — EXCLUSIVAMENTE vía Model Registry:
   Cada evento trae `_registry_modelo_secundario` con la fuente autorizada para
   ese deporte específico. Reglas ESTRICTAS según el campo `cobertura`:

   - "externa_directa": debes REALIZAR la búsqueda web real en `fuente_primaria`
     (o `fuente_secundaria` si la primaria falla) ANTES de concluir que no se
     puede verificar. Prohibido responder "no se puede confirmar" sin haber
     intentado la búsqueda.

   - "modelo_interno_elo": el backend YA calculó una probabilidad calibrada con
     resultados reales recientes (no es una fuente web). Usa directamente los
     campos `probabilidad_elo_home`, `elo_home`, `elo_away`,
     `brier_score_historico` y `muestras_brier` como segundo modelo — NO hace
     falta buscar en la web para estos eventos. Cita la fuente como:
     "Modelo Elo interno (backend), calibrado con {muestras_brier} resultados
     reales, Brier histórico {brier_score_historico}".

   - "pendiente_desarrollo": no hay fuente externa definida NI historial Elo
     interno suficiente todavía para ese equipo/deporte. DESCARTA
     automáticamente sin buscar en otro lado y sin usar un modelo "propio"
     improvisado — eso sería fabricación.

   - "excluido_estructural": ya debería venir excluido del JSON; si aparece,
     descarta sin análisis (partidos de exhibición/preseason).

   PROHIBIDO ABSOLUTO: usar cualquier fuente, rating o modelo que no aparezca
   literalmente en `_registry_modelo_secundario` de ese evento específico.

4. LIQUIDEZ: Usa el campo `_liquidez_backend` tal cual. No la reinterpretes.

5. UMBRALES DE DESCARTE:
   - EV < 5% → descartar.
   - Divergencia |Pinnacle - Segundo Modelo| > 7% → descartar (señal de posible
     error de datos, no de "value").
   - Si el segundo modelo es "modelo_interno_elo" y `brier_score_historico` es
     peor que 0.23 o `muestras_brier` < 8, el backend ya lo habría excluido —
     pero si por alguna razón lo ves con esos valores, descarta igual.

6. CONFIANZA (1-10): Calcula con el siguiente desglose visible en el informe:
   - Edge estadístico (EV real vs. umbral)
   - Calidad/frescura de la fuente del segundo modelo (una fuente externa
     reciente pesa más que un modelo interno con pocas muestras)
   - Liquidez del mercado
   - Coherencia entre movimiento de línea (si hay datos) y el pick
   Un pick solo califica si la confianza total es >= 8/10.

REGLAS ANTI-FABRICACIÓN (obligatorias, sin excepción):
- Nunca inventes lesiones, alineaciones, clima o noticias que no hayas confirmado
  con una fuente real y citada.
- Nunca inventes cuotas, nombres de equipos/jugadores o resultados históricos que
  no estén en el JSON de entrada o en una fuente web verificada.
- Si falta cualquier dato necesario para completar el análisis de un evento, ese
  evento se descarta — nunca se rellena el vacío con una suposición "razonable".
- Cada afirmación estadística debe llevar su fuente (nombre + URL, o "Modelo Elo
  interno" con sus métricas si aplica).

FORMATO DE SALIDA (obligatorio, en español):
1. Resumen: cuántos eventos se evaluaron, cuántos se descartaron y por qué
   (agrupado por motivo: sin segundo modelo, datos obsoletos, fuera de umbral
   EV, divergencia).
2. Si hay pick: Partido | Mercado | Cuota Pinnacle | Prob. implícita de-vigged |
   Prob. segundo modelo (con fuente/métricas) | EV% | Confianza (con desglose) |
   Justificación en 3-4 líneas.
3. Si NO hay pick: decirlo explícitamente en la primera línea ("PICK DEL DÍA:
   NINGUNO") y explicar brevemente por qué ningún evento alcanzó el umbral.
"""

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
ANTHROPIC_API_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"

# ==============================================================================
# 1b. MODEL REGISTRY — fuentes autorizadas de segundo modelo, por deporte.
#     Vive en código (versionado, editable a mano), NO en el prompt.
#     "cobertura" posibles:
#       - "externa_directa"      → fuente pública conocida, la IA debe buscarla.
#       - "pendiente_desarrollo" → sin fuente externa Y sin Elo interno maduro
#                                  todavía. Se descarta, nunca se improvisa.
#       - "excluido_estructural" → se excluye antes de llegar a la IA.
#     Los deportes marcados abajo como "usa_elo_interno": True son candidatos a
#     que el motor Elo interno los resuelva automáticamente cuando acumule
#     suficiente historial (ver sección 1c).
# ==============================================================================
REGISTRY_ULTIMA_REVISION = "2026-08-20"

MODEL_REGISTRY = [
    {"patron": "americanfootball_nfl_preseason", "fuente_primaria": None, "fuente_secundaria": None,
     "cobertura": "excluido_estructural", "version": "1.0", "usa_elo_interno": False},
    {"patron": "soccer", "fuente_primaria": "ClubElo", "fuente_secundaria": "FiveThirtyEight SPI",
     "cobertura": "externa_directa", "version": "1.0", "usa_elo_interno": False},
    {"patron": "tennis", "fuente_primaria": "TennisAbstract (Elo por superficie)",
     "fuente_secundaria": "Ranking oficial ATP/WTA", "cobertura": "externa_directa", "version": "1.0",
     "usa_elo_interno": False},
    {"patron": "baseball_mlb", "fuente_primaria": "FanGraphs", "fuente_secundaria": None,
     "cobertura": "externa_directa", "version": "1.0", "usa_elo_interno": False},
    {"patron": "baseball_kbo", "fuente_primaria": None, "fuente_secundaria": None,
     "cobertura": "pendiente_desarrollo", "version": "1.0", "usa_elo_interno": True},
    {"patron": "baseball_npb", "fuente_primaria": None, "fuente_secundaria": None,
     "cobertura": "pendiente_desarrollo", "version": "1.0", "usa_elo_interno": True},
    {"patron": "basketball_nba", "fuente_primaria": "Basketball-Reference", "fuente_secundaria": None,
     "cobertura": "externa_directa", "version": "1.0", "usa_elo_interno": False},
    {"patron": "basketball_wnba", "fuente_primaria": "Basketball-Reference", "fuente_secundaria": None,
     "cobertura": "externa_directa", "version": "1.0", "usa_elo_interno": False},
    {"patron": "basketball_ncaab", "fuente_primaria": "Basketball-Reference (NCAA)", "fuente_secundaria": None,
     "cobertura": "externa_directa", "version": "1.0", "usa_elo_interno": False},
    {"patron": "icehockey_nhl", "fuente_primaria": "Hockey-Reference", "fuente_secundaria": None,
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
# 1c. MOTOR ELO INTERNO — para deportes sin fuente externa confiable
#     (KBO, NPB, MMA, y cualquier otro no cubierto).
#
#     CÓMO FUNCIONA:
#     - Cada corrida consulta el endpoint /scores de The Odds API (resultados
#       reales de los últimos días) para los deportes marcados "usa_elo_interno".
#     - Actualiza un rating Elo por equipo/luchador usando la fórmula estándar,
#       y guarda un historial de (probabilidad_pre_partido, resultado_real) para
#       calcular un Brier Score histórico real — no inventado.
#     - Solo se usa como segundo modelo válido si: (a) ambos competidores tienen
#       un mínimo de partidos calificados, Y (b) el Brier Score histórico del
#       modelo para ese deporte es mejor que el umbral aceptable. Si no se
#       cumple, el evento sigue cayendo en "pendiente_desarrollo" — el sistema
#       jamás usa un rating con historial insuficiente.
#     - El estado se persiste en un archivo JSON local. OJO: si despliegas en
#       una plataforma con filesystem efímero (ej. Streamlit Community Cloud
#       sin volumen persistente), este archivo se reinicia en cada redeploy y
#       el aprendizaje empieza de cero. Para producción real, migra
#       `ELO_STATE_FILE` a una base de datos o almacenamiento persistente.
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
ELO_BRIER_MAXIMO_ACEPTABLE = 0.23  # peor que esto = descartar (naive/azar = 0.25)
ELO_DIAS_HISTORIAL_SCORES = 3      # máximo permitido por el endpoint /scores


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
    """Resultados reales recientes (partidos ya jugados) para alimentar el motor
    Elo interno. Cacheado 1h porque los resultados no cambian dentro de esa
    ventana y cada llamada consume cuota de The Odds API."""
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
    """Descarga resultados reales y actualiza ratings + historial de Brier
    IN-PLACE sobre `estado`. Evita reprocesar el mismo partido dos veces."""
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

        prob_home_pre = _prob_elo(home["elo"] + ELO_VENTAJA_LOCAL, away["elo"])

        home["elo"] += ELO_K_FACTOR * (resultado_home - prob_home_pre)
        away["elo"] += ELO_K_FACTOR * ((1 - resultado_home) - (1 - prob_home_pre))
        home["partidos"] += 1
        away["partidos"] += 1

        historial.append({"prob": prob_home_pre, "resultado": resultado_home})
        procesados.add(game_id)

    estado["procesados"][sport_key] = list(procesados)
    return estado


def obtener_entrada_modelo_interno(estado, sport_key, home_team, away_team):
    """Devuelve la entrada de registry basada en Elo interno SOLO si hay
    suficiente historial calificado y calibración aceptable. Si no, devuelve
    None (el llamador debe entonces dejarlo como 'pendiente_desarrollo')."""
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

    prob_home = _prob_elo(home["elo"] + ELO_VENTAJA_LOCAL, away["elo"])
    return {
        "fuente_primaria": "Modelo Elo interno (backend, calculado desde resultados reales vía Odds API /scores)",
        "fuente_secundaria": None,
        "cobertura": "modelo_interno_elo",
        "version": "1.0",
        "ultima_revision": REGISTRY_ULTIMA_REVISION,
        "probabilidad_elo_home": round(prob_home, 4),
        "elo_home": round(home["elo"], 1),
        "elo_away": round(away["elo"], 1),
        "partidos_calificados_home": home["partidos"],
        "partidos_calificados_away": away["partidos"],
        "brier_score_historico": round(brier, 4),
        "muestras_brier": len(historial),
    }


def obtener_entrada_registry(sport_key, home_team=None, away_team=None, estado_elo=None):
    """Punto único de verdad para el segundo modelo de un evento: primero mira
    el registry estático; si ese deporte está marcado para usar Elo interno y
    hay suficiente calidad, lo reemplaza por la entrada calculada."""
    base = _buscar_base_registry(sport_key)

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
            f"mínimo requerido: {ELO_MIN_PARTIDOS_POR_EQUIPO}). Se acumula automáticamente "
            f"con cada corrida del sistema."
        )

    base["ultima_revision"] = REGISTRY_ULTIMA_REVISION
    return base


# ==============================================================================
# 2. FUNCIONES BACKEND — The Odds API (cuotas)
# ==============================================================================

@st.cache_data(ttl=3600, show_spinner=False)
def obtener_deportes_activos(api_key):
    """Lista de deportes activos hoy. Cacheado 1h: esto casi no cambia en el día."""
    url = f"{ODDS_API_BASE}/sports/?apiKey={api_key}"
    try:
        response = requests.get(url, timeout=10)
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
    """
    Consulta cuotas para un deporte específico.
    NOTA: no se envía 'regions' junto con 'bookmakers' porque The Odds API
    ignora 'regions' cuando 'bookmakers' está presente.
    Cacheado 90s para no quemar cuota si el usuario da clic varias veces seguidas.
    """
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
            return []  # deporte sin mercado h2h disponible, no es un error real
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
    """Mide la dispersión de probabilidades entre casas de apuestas, tomando el
    spread máximo encontrado en CUALQUIER resultado del mercado (home, away,
    draw) — no solo el local."""
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


def filtrar_y_enriquecer(datos_crudos, estado_elo, horas_ventana=24):
    if not datos_crudos or not isinstance(datos_crudos, list):
        return [], "Backend pre-filtró 0 eventos (sin datos recibidos)."

    eventos_validos = []
    descartados_sin_pinnacle = 0
    descartados_fuera_de_rango = 0
    descartados_fecha = 0
    descartados_sin_fecha = 0
    descartados_exclusion_estructural = 0
    eventos_con_elo_interno = 0
    eventos_pendientes_desarrollo = 0

    ahora_utc = datetime.now(timezone.utc)
    limite_utc = ahora_utc + timedelta(hours=horas_ventana)

    for evento in datos_crudos:
        if not isinstance(evento, dict):
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

        pinnacle = next((b for b in evento.get("bookmakers", []) if isinstance(b, dict) and b.get("key") == "pinnacle"), None)
        if not pinnacle:
            descartados_sin_pinnacle += 1
            continue

        h2h = next((m for m in pinnacle.get("markets", []) if isinstance(m, dict) and m.get("key") == "h2h"), None)
        if not h2h:
            descartados_sin_pinnacle += 1
            continue

        outcomes = h2h.get("outcomes", [])
        en_rango = any(1.40 <= o.get("price", 0) <= 2.00 for o in outcomes if isinstance(o, dict))
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
        eventos_validos.append(evento_minificado)

    resumen_filtro = (
        f"Backend pre-filtró {len(datos_crudos)} eventos: "
        f"{len(eventos_validos)} candidatos calificados (prx {horas_ventana}h, cuota 1.40-2.00), "
        f"{descartados_fecha} descartados por fecha fuera de ventana, "
        f"{descartados_sin_fecha} descartados por fecha faltante/ilegible, "
        f"{descartados_sin_pinnacle} descartados sin Pinnacle, "
        f"{descartados_fuera_de_rango} descartados fuera de rango de cuota, "
        f"{descartados_exclusion_estructural} excluidos estructuralmente (preseason/exhibición). "
        f"De los {len(eventos_validos)} candidatos: {eventos_con_elo_interno} resueltos por el motor "
        f"Elo interno (calibrado) y {eventos_pendientes_desarrollo} siguen sin segundo modelo "
        f"disponible (la IA los descartará)."
    )
    return eventos_validos, resumen_filtro


# ==============================================================================
# 3. GEMINI — modelos SIEMPRE consultados en vivo (nunca hardcodeados).
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
    """Llamada REST directa. Devuelve también el uso de tokens reportado en
    'usageMetadata'. Google no expone el saldo/crédito restante de la cuenta
    por esta vía — eso solo se ve en Google AI Studio / Cloud Console."""
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


# ==============================================================================
# 3b. CLAUDE (Anthropic) — búsqueda web FORZADA vía tool en la propia llamada.
#     No depende de ningún toggle de interfaz: el tool viaja en el request.
# ==============================================================================

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


def llamar_claude_rest(anthropic_api_key, modelo, prompt_texto, max_tokens=4096):
    """
    Llama a la API de Claude con el tool de web_search FORZADO en el propio
    request — no depende de ningún toggle manual de interfaz.

    Devuelve: (texto, queries_buscadas, uso_tokens, ratelimit)
    - uso_tokens: tokens consumidos en ESTA llamada (campo 'usage').
    - ratelimit: headers 'anthropic-ratelimit-*' — son ventanas de tasa
      (requests/tokens por minuto), NO el saldo en dólares de la cuenta. El
      saldo prepagado solo se ve en console.anthropic.com → Billing.
    """
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
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
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
st.title("📊 Analista de Apuesta Única v3.2 (Multi-IA, Multi-Deporte & Motor Elo Interno)")

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

    if "odds_api_uso" in st.session_state:
        uso = st.session_state["odds_api_uso"]
        st.caption(f"📉 Odds API — usados: {uso['usados']} · restantes: {uso['restantes']}")

    if "gemini_tokens_acumulados" in st.session_state:
        g = st.session_state["gemini_tokens_acumulados"]
        st.caption(
            f"🧮 Gemini (sesión) — prompt: {g['prompt']:,} · salida: {g['salida']:,} · "
            f"total: {g['total']:,} tokens"
        )
        st.caption("Saldo/crédito real: solo visible en Google AI Studio / Cloud Console.")

    if "claude_tokens_acumulados" in st.session_state:
        c = st.session_state["claude_tokens_acumulados"]
        st.caption(
            f"🧮 Claude (sesión) — entrada: {c['entrada']:,} · salida: {c['salida']:,} · "
            f"total: {c['total']:,} tokens"
        )
        if "claude_ratelimit" in st.session_state:
            rl = st.session_state["claude_ratelimit"]
            st.caption(
                f"⏱️ Ventana de rate-limit — tokens restantes: {rl['tokens_restantes']}/"
                f"{rl['tokens_limite']} · requests restantes: {rl['requests_restantes']}"
            )
        st.caption("Saldo/crédito prepagado real: solo visible en console.anthropic.com → Billing.")

    estado_elo_sidebar = cargar_estado_elo()
    if estado_elo_sidebar.get("ratings"):
        with st.expander("🧠 Motor Elo interno — estado actual"):
            for sport_key, ratings in estado_elo_sidebar["ratings"].items():
                brier = calcular_brier(estado_elo_sidebar.get("historial_brier", {}).get(sport_key, []))
                n_muestras = len(estado_elo_sidebar.get("historial_brier", {}).get(sport_key, []))
                brier_txt = f"{brier:.4f}" if brier is not None else "N/A"
                st.write(f"**{sport_key}** — {len(ratings)} equipos rateados, "
                         f"Brier: {brier_txt} ({n_muestras} muestras)")

if api_key:
    deportes_lista = obtener_deportes_activos(api_key)

    if deportes_lista:
        opciones_deporte = {"🔥 TODOS LOS DEPORTES ACTIVOS": "ALL"}
        for dep in deportes_lista:
            opciones_deporte[f"{dep.get('group')} - {dep.get('title')}"] = dep.get('key')

        seleccion = st.selectbox("Selecciona el deporte o ámbito a analizar:", list(opciones_deporte.keys()))
        deporte_key_seleccionado = opciones_deporte[seleccion]

        if st.button("🚀 Generar Prompt y Procesar Datos", type="primary"):
            with st.spinner("Consultando The Odds API, actualizando motor Elo y procesando pre-filtros..."):
                datos_acumulados = []

                if deporte_key_seleccionado == "ALL":
                    progress_bar = st.progress(0)
                    total_deps = len(deportes_lista)
                    for idx, dep in enumerate(deportes_lista):
                        cuotas = obtener_cuotas_api(api_key, dep.get('key'))
                        if cuotas:
                            datos_acumulados.extend(cuotas)
                        progress_bar.progress((idx + 1) / total_deps)
                        time.sleep(0.15)  # evita ráfaga -> 429
                    progress_bar.empty()
                else:
                    datos_acumulados = obtener_cuotas_api(api_key, deporte_key_seleccionado)

                # --- Actualizar motor Elo interno para los deportes presentes
                # que están marcados como "usa_elo_interno" en el registry ---
                estado_elo = cargar_estado_elo()
                sport_keys_presentes = {ev.get("sport_key") for ev in datos_acumulados if isinstance(ev, dict)}
                for sk in sport_keys_presentes:
                    base = _buscar_base_registry(sk)
                    if base.get("usa_elo_interno"):
                        estado_elo = actualizar_elo_sport(api_key, sk, estado_elo)
                guardar_estado_elo(estado_elo)

                tz_rd = timezone(timedelta(hours=-4))
                hora_rd = datetime.now(tz_rd).strftime("%Y-%m-%d %H:%M:%S AST (UTC-4)")

                eventos_filtrados, resumen_filtro = filtrar_y_enriquecer(datos_acumulados, estado_elo)
                movimientos_pinnacle = registrar_y_calcular_movimientos(eventos_filtrados, deporte_key_seleccionado)

                seccion_movimiento = "SIN SNAPSHOT PREVIO EN ESTA SESIÓN."
                if movimientos_pinnacle:
                    lineas_mov = "\n".join(f"- {k}: {v}" for k, v in movimientos_pinnacle.items())
                    seccion_movimiento = f"MOVIMIENTOS EN PINNACLE DETECTADOS:\n{lineas_mov}"

                st.write("### 📌 Resumen de Filtrado Backend")
                st.info(resumen_filtro)

                if not eventos_filtrados:
                    st.warning("⚠️ No se encontraron candidatos válidos en el rango 1.40 - 2.00 para los partidos de hoy.")
                else:
                    prompt_completo = (
                        f"{SYSTEM_PROMPT_BLINDADO_V3_2}\n\n"
                        f"==================================================\n"
                        f"CONTEXTO DE EJECUCIÓN DEL BACKEND\n"
                        f"==================================================\n"
                        f"ÁMBITO: {seleccion}\n"
                        f"HORA CONSULTA (RD/UTC-4): {hora_rd}\n\n"
                        f"RESUMEN DE PRE-FILTRADO:\n{resumen_filtro}\n\n"
                        f"{seccion_movimiento}\n\n"
                        f"INSTRUCCIÓN TÉCNICA: Utiliza directamente los campos `_pinnacle_devig`, "
                        f"`_pinnacle_last_update`, `_liquidez_backend`, `_dispersion_max_entre_casas`, "
                        f"`_n_casas_reportando` y `_registry_modelo_secundario`. No recalcules el "
                        f"de-vig ni filtres por rango nuevamente.\n\n"
                        f"DATOS JSON PRE-FILTRADOS Y ENRIQUECIDOS:\n"
                        f"{json.dumps(eventos_filtrados, indent=2, ensure_ascii=False)}"
                    )
                    st.session_state["prompt_generado"] = prompt_completo
                    st.success(f"✅ Se consolidaron {len(eventos_filtrados)} eventos aptos para el prompt.")

    if "prompt_generado" in st.session_state:
        st.divider()
        st.subheader("🤖 Selecciona la IA para ejecutar el Análisis")
        st.caption("Estos botones abren la web de cada IA — pega el prompt y elige tú el modelo más reciente disponible en cada plataforma.")

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

            modelos_disponibles = listar_modelos_gemini(gemini_api_key)
            if modelos_disponibles:
                modelo_default = next(
                    (m for m in modelos_disponibles if "flash" in m and "lite" not in m),
                    modelos_disponibles[0],
                )
                modelo_elegido = st.selectbox(
                    "Modelo Gemini (lista obtenida en vivo desde la API — siempre actualizada):",
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
                                f"Esta llamada: {uso_tokens.get('promptTokenCount', 0):,} tokens de "
                                f"entrada · {uso_tokens.get('candidatesTokenCount', 0):,} de salida."
                            )
                        except Exception as e:
                            st.error(f"Error al ejecutar con Gemini API: {e}")
            else:
                st.warning("No se pudo obtener la lista de modelos. Verifica la API Key de Gemini.")

        if anthropic_api_key:
            st.divider()
            st.subheader("⚡ Ejecución Directa en App (Claude API — búsqueda forzada)")
            st.caption(
                "Esta llamada incluye el tool `web_search` directamente en el request, "
                "así que Claude SÍ puede buscar en ClubElo/FanGraphs/TennisAbstract "
                "aunque el toggle de búsqueda en claude.ai estuviera apagado."
            )

            modelos_claude = listar_modelos_claude(anthropic_api_key)
            if modelos_claude:
                modelo_claude_default = next(
                    (m for m in modelos_claude if "sonnet" in m.lower()), modelos_claude[0]
                )
                modelo_claude_elegido = st.selectbox(
                    "Modelo Claude (lista obtenida en vivo desde la API):",
                    modelos_claude,
                    index=modelos_claude.index(modelo_claude_default),
                )
                if st.button("🤖 Analizar directamente con Claude API", type="primary"):
                    with st.spinner(f"Analizando con {modelo_claude_elegido} (con búsqueda web activa)..."):
                        try:
                            resultado, queries_buscadas, uso_tokens, ratelimit = llamar_claude_rest(
                                anthropic_api_key, modelo_claude_elegido, st.session_state["prompt_generado"]
                            )
                            st.markdown("### 🏆 Resultado del Análisis")
                            st.markdown(resultado)
                            if queries_buscadas:
                                with st.expander(f"🔍 Búsquedas web realizadas ({len(queries_buscadas)})"):
                                    for q in queries_buscadas:
                                        st.write(f"- {q}")
                            else:
                                st.warning(
                                    "⚠️ Claude no ejecutó ninguna búsqueda web en esta corrida — "
                                    "revisa el resultado, es posible que haya descartado todo por "
                                    "falta de datos verificables en vez de fabricarlos."
                                )

                            entrada_tok = uso_tokens.get("input_tokens", 0) or 0
                            salida_tok = uso_tokens.get("output_tokens", 0) or 0
                            prev = st.session_state.get(
                                "claude_tokens_acumulados", {"entrada": 0, "salida": 0, "total": 0}
                            )
                            prev["entrada"] += entrada_tok
                            prev["salida"] += salida_tok
                            prev["total"] += entrada_tok + salida_tok
                            st.session_state["claude_tokens_acumulados"] = prev
                            st.session_state["claude_ratelimit"] = ratelimit
                            st.caption(f"Esta llamada: {entrada_tok:,} tokens de entrada · {salida_tok:,} de salida.")
                        except Exception as e:
                            st.error(f"Error al ejecutar con Claude API: {e}")
            else:
                st.warning("No se pudo obtener la lista de modelos. Verifica la API Key de Anthropic.")
