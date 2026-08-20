import json
import time
from datetime import datetime, timedelta, timezone

import requests
import streamlit as st


# ==============================================================================
# 1. SYSTEM PROMPT — ANALISTA CUANTITATIVO DE APUESTA ÚNICA
#    BLINDADO V3.2
# ==============================================================================

SYSTEM_PROMPT_BLINDADO_V3_2 = """
PROMPT — Analista Cuantitativo de Apuesta Única (Blindado v3.2)

ROL Y OBJETIVO:
Actúa como Analista Cuantitativo de Deportes y Tipster Profesional.

Tu objetivo es seleccionar UNA sola apuesta —la de mayor confianza estadística y
mejor relación riesgo/retorno— dentro de un rango de cuota 1.40-2.00
(moneyline o mercado principal), considerando TODOS los eventos recibidos.

Un informe con 0 picks es un resultado VÁLIDO y PREFERIBLE a forzar una apuesta.

NO debes seleccionar un pick simplemente porque exista un candidato.

PRIORIDAD ABSOLUTA:
1. Integridad de datos.
2. Verificación del segundo modelo.
3. Valor estadístico.
4. Consistencia entre fuentes.
5. Confianza.
6. Solo después seleccionar el mejor pick.

======================================================================
1. ANCLA OBLIGATORIA — PINNACLE
======================================================================

El backend ya calculó `_pinnacle_devig`.

DEBES utilizar directamente ese campo.

PROHIBIDO:
- recalcular el de-vig;
- sustituirlo por otra casa;
- utilizar Stake, Bet365 u otra casa como modelo principal;
- modificar manualmente la probabilidad Pinnacle.

La probabilidad Pinnacle de-vigged es el ANCLA DEL MERCADO.

======================================================================
2. CUOTA
======================================================================

El backend ya filtró los eventos para que al menos un resultado tenga cuota
entre 1.40 y 2.00.

No vuelvas a ampliar el rango.

Solo puedes recomendar una selección cuya cuota actual esté entre:

1.40 <= cuota <= 2.00

Si la cuota que aparece en el JSON no está dentro del rango, NO la utilices.

======================================================================
3. GATE DE FRESCURA
======================================================================

La frescura debe evaluarse RELATIVAMENTE AL INICIO DEL EVENTO.

Usa:

- `_pinnacle_last_update`
- `inicio_utc`
- `_minutos_hasta_inicio`

REGLA:

A) Si faltan MENOS DE 180 minutos para comenzar:

   Si `_pinnacle_last_update` tiene más de 90 minutos de antigüedad respecto
   a la hora actual de consulta:

   -> DESCARTAR.

B) Si faltan MÁS DE 180 minutos:

   La antigüedad del snapshot NO provoca descarte automático.

   Puede registrarse como información contextual.

IMPORTANTE:

No confundas "última actualización antigua" con "dato inválido".

Pinnacle puede mantener una línea estable durante horas.

El objetivo de este gate es detectar únicamente datos potencialmente obsoletos
cuando el evento está próximo a comenzar.

======================================================================
4. SEGUNDO MODELO — MODEL REGISTRY
======================================================================

Cada evento contiene:

`_registry_modelo_secundario`

Ese objeto determina EXACTAMENTE qué fuente está autorizada.

REGLA ABSOLUTA:

Solo puedes utilizar la fuente que aparezca dentro del registry del evento.

NO puedes sustituirla por:

- ESPN si no está autorizado;
- CBS;
- OddsShark;
- Covers;
- Action Network;
- FiveThirtyEight;
- otro rating;
- otro modelo;
- tu propio modelo;
- conocimiento interno del modelo.

Si la fuente primaria falla:

solo puedes utilizar `fuente_secundaria` SI está explícitamente definida.

----------------------------------------------------------------------
COBERTURA
----------------------------------------------------------------------

Si:

`cobertura = externa_directa`

DEBES intentar realizar búsqueda web real de la fuente autorizada.

No puedes decir:

"No se puede confirmar"

sin haber intentado primero buscar.

Si la fuente primaria no proporciona información suficiente:

1. intenta la fuente secundaria si existe;
2. si tampoco existe información verificable:
   DESCARTA EL EVENTO.

Si:

`cobertura = pendiente_desarrollo`

DESCARTA AUTOMÁTICAMENTE.

NO BUSQUES OTRA FUENTE.

NO INVENTES UN SEGUNDO MODELO.

NO CONSTRUYAS UN RATING PROPIO.

Si:

`cobertura = excluido_estructural`

DESCARTA AUTOMÁTICAMENTE.

======================================================================
5. VALIDACIÓN DEL SEGUNDO MODELO
======================================================================

No basta con encontrar una página sobre el partido.

Debes verificar que la fuente autorizada proporciona información cuantitativa
relevante para evaluar el evento.

Ejemplos válidos:

- probabilidad;
- rating;
- Elo;
- proyección;
- win probability;
- forecast;
- modelo estadístico;
- ranking cuantitativo que pueda convertirse justificadamente en una
  probabilidad.

NO conviertas arbitrariamente un ranking ordinal en una probabilidad.

Si la fuente no permite obtener una probabilidad comparable:

DESCARTA.

======================================================================
6. EV
======================================================================

Para una selección con cuota decimal `Q` y probabilidad del segundo modelo `P`:

EV = P × Q - 1

Ejemplo:

P = 0.60
Q = 1.80

EV = 0.60 × 1.80 - 1
EV = 0.08
EV = +8%

REGLA:

EV < 5%
-> DESCARTAR.

EV >= 5%
-> candidato válido.

NO redondees antes de calcular el EV.

======================================================================
7. DIVERGENCIA
======================================================================

Compara:

probabilidad Pinnacle de-vig
vs.
probabilidad del segundo modelo.

Divergencia absoluta:

abs(P_pinnacle - P_segundo_modelo)

Si:

> 7 puntos porcentuales

-> DESCARTAR.

Esto es una protección contra errores de datos/modelo.

NO interpretes una divergencia >7% automáticamente como "mayor value".

======================================================================
8. LIQUIDEZ
======================================================================

Utiliza `_liquidez_backend` TAL COMO VIENE.

NO la recalcules.

NO la sustituyas.

NO inventes una escala diferente.

También considera:

`_n_casas_reportando`

y

`_dispersion_max_entre_casas`

como información secundaria.

======================================================================
9. MOVIMIENTO DE PINNACLE
======================================================================

Si existe:

MOVIMIENTOS EN PINNACLE DETECTADOS

utilízalo únicamente como evidencia contextual.

NO conviertas automáticamente:

cuota bajando = apuesta buena

ni:

cuota subiendo = apuesta mala.

El movimiento debe ser coherente con el análisis.

======================================================================
10. CONFIANZA 1-10
======================================================================

Calcula una confianza final de 1 a 10 usando:

A) Edge estadístico / EV
B) Calidad del segundo modelo
C) Frescura
D) Liquidez
E) Coherencia del movimiento
F) Ausencia de señales de conflicto

Desglose obligatorio:

- Edge estadístico: X/10
- Segundo modelo: X/10
- Frescura: X/10
- Liquidez: X/10
- Movimiento/coherencia: X/10
- Integridad de datos: X/10

Después calcula la confianza final.

REGLA:

Confianza final < 8/10
-> DESCARTAR.

======================================================================
11. REGLAS ANTI-FABRICACIÓN
======================================================================

PROHIBIDO INVENTAR:

- lesiones;
- alineaciones;
- pitchers;
- porteros;
- clima;
- resultados;
- estadísticas;
- ratings;
- probabilidades;
- cuotas;
- movimientos;
- noticias;
- información histórica.

Si un dato no está en:

A) JSON del backend
o
B) fuente web autorizada y verificable

NO LO USES.

Cada afirmación cuantitativa obtenida mediante web debe incluir:

- nombre de la fuente;
- URL.

======================================================================
12. FECHA Y HORA
======================================================================

Trabaja únicamente con eventos futuros respecto a:

HORA CONSULTA DEL BACKEND.

La hora oficial para presentación es:

República Dominicana / UTC-4.

No analices partidos ya iniciados.

No analices eventos del día anterior.

No analices eventos fuera de la ventana recibida.

======================================================================
13. SELECCIÓN FINAL
======================================================================

Después de evaluar TODOS los eventos:

1. descarta inválidos;
2. descarta sin segundo modelo;
3. descarta por frescura;
4. descarta EV <5%;
5. descarta divergencia >7%;
6. descarta confianza <8;
7. compara los candidatos restantes.

Selecciona SOLO UNO.

El ganador debe maximizar:

CONFianza + EV + calidad de datos + liquidez

sin sacrificar la protección contra falsos positivos.

======================================================================
14. REGLA DE EMPATE
======================================================================

Si dos candidatos son similares:

prioriza:

1. menor divergencia;
2. mayor calidad del segundo modelo;
3. mayor liquidez;
4. mayor frescura;
5. mayor EV;
6. mayor estabilidad/coherencia del mercado.

======================================================================
15. FORMATO DE SALIDA OBLIGATORIO
======================================================================

La respuesta DEBE estar en español.

PRIMERA LÍNEA:

Si existe pick:

PICK DEL DÍA: [SELECCIÓN]

Si no existe:

PICK DEL DÍA: NINGUNO

Después:

==================================================
RESUMEN DE AUDITORÍA
==================================================

Eventos recibidos:
Eventos evaluados:
Descartados:
- sin segundo modelo:
- datos obsoletos:
- EV <5%:
- divergencia >7%:
- confianza <8:
- otros:

==================================================
PICK
==================================================

Partido:
Mercado:
Selección:
Cuota:
Probabilidad Pinnacle de-vig:
Probabilidad segundo modelo:
Segundo modelo:
Fuente:
URL:
EV:
Divergencia:
Liquidez:
Casas reportando:
Movimiento:

CONFIANZA:
- Edge estadístico:
- Segundo modelo:
- Frescura:
- Liquidez:
- Movimiento/coherencia:
- Integridad de datos:
- TOTAL:

JUSTIFICACIÓN:
3-5 líneas máximo.

==================================================
VERIFICACIÓN DE FUENTES
==================================================

Para cada fuente utilizada:

Fuente:
URL:
Dato utilizado:

==================================================
VEREDICTO
==================================================

APOSTAR

o

NO APOSTAR

Si no existe un candidato que cumpla TODOS los criterios:

PICK DEL DÍA: NINGUNO

Y explica brevemente el principal motivo.

NO fuerces una selección.
"""


# ==============================================================================
# 2. CONFIGURACIÓN
# ==============================================================================

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
ANTHROPIC_API_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"

REGISTRY_ULTIMA_REVISION = "2026-08-20"


# ==============================================================================
# 3. MODEL REGISTRY
# ==============================================================================

MODEL_REGISTRY = [
    {
        "patron": "americanfootball_nfl_preseason",
        "fuente_primaria": None,
        "fuente_secundaria": None,
        "cobertura": "excluido_estructural",
        "version": "1.0",
    },
    {
        "patron": "soccer",
        "fuente_primaria": "ClubElo",
        "fuente_secundaria": "FiveThirtyEight SPI",
        "cobertura": "externa_directa",
        "version": "1.0",
    },
    {
        "patron": "tennis",
        "fuente_primaria": "TennisAbstract (Elo por superficie)",
        "fuente_secundaria": "Ranking oficial ATP/WTA",
        "cobertura": "externa_directa",
        "version": "1.0",
    },
    {
        "patron": "baseball_mlb",
        "fuente_primaria": "FanGraphs",
        "fuente_secundaria": None,
        "cobertura": "externa_directa",
        "version": "1.0",
    },
    {
        "patron": "baseball_kbo",
        "fuente_primaria": None,
        "fuente_secundaria": None,
        "cobertura": "pendiente_desarrollo",
        "version": "0.0",
    },
    {
        "patron": "baseball_npb",
        "fuente_primaria": None,
        "fuente_secundaria": None,
        "cobertura": "pendiente_desarrollo",
        "version": "0.0",
    },
    {
        "patron": "basketball_nba",
        "fuente_primaria": "Basketball-Reference",
        "fuente_secundaria": None,
        "cobertura": "externa_directa",
        "version": "1.0",
    },
    {
        "patron": "basketball_wnba",
        "fuente_primaria": "Basketball-Reference",
        "fuente_secundaria": None,
        "cobertura": "externa_directa",
        "version": "1.0",
    },
    {
        "patron": "basketball_ncaab",
        "fuente_primaria": "Basketball-Reference (NCAA)",
        "fuente_secundaria": None,
        "cobertura": "externa_directa",
        "version": "1.0",
    },
    {
        "patron": "icehockey_nhl",
        "fuente_primaria": "Hockey-Reference",
        "fuente_secundaria": None,
        "cobertura": "externa_directa",
        "version": "1.0",
    },
    {
        "patron": "cricket",
        "fuente_primaria": "ICC Team Ratings",
        "fuente_secundaria": "ESPN Cricinfo",
        "cobertura": "externa_directa",
        "version": "1.0",
    },
    {
        "patron": "boxing",
        "fuente_primaria": "BoxRec ratings",
        "fuente_secundaria": None,
        "cobertura": "externa_directa",
        "version": "1.0",
    },
    {
        "patron": "mma",
        "fuente_primaria": None,
        "fuente_secundaria": None,
        "cobertura": "pendiente_desarrollo",
        "version": "0.0",
    },
    {
        "patron": "americanfootball_nfl",
        "fuente_primaria": "ESPN FPI (Football Power Index)",
        "fuente_secundaria": None,
        "cobertura": "externa_directa",
        "version": "1.0",
    },
]

DEFAULT_REGISTRY_ENTRY = {
    "fuente_primaria": None,
    "fuente_secundaria": None,
    "cobertura": "pendiente_desarrollo",
    "version": "0.0",
}


def obtener_entrada_registry(sport_key):
    if not sport_key:
        entrada = dict(DEFAULT_REGISTRY_ENTRY)
    else:
        sport_key_low = sport_key.lower()

        entrada = next(
            (
                e
                for e in MODEL_REGISTRY
                if e["patron"].lower() in sport_key_low
            ),
            dict(DEFAULT_REGISTRY_ENTRY),
        )

    entrada = dict(entrada)
    entrada["ultima_revision"] = REGISTRY_ULTIMA_REVISION

    return entrada


# ==============================================================================
# 4. UTILIDADES DE FECHA
# ==============================================================================

def parsear_fecha_utc(valor):
    if not valor:
        return None

    try:
        dt = datetime.fromisoformat(
            str(valor).replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)

    except Exception:
        return None


def minutos_hasta_inicio(commence_dt, ahora_utc=None):
    if commence_dt is None:
        return None

    if ahora_utc is None:
        ahora_utc = datetime.now(timezone.utc)

    return round(
        (commence_dt - ahora_utc).total_seconds() / 60,
        1,
    )


def antiguedad_minutos(timestamp, ahora_utc=None):
    dt = parsear_fecha_utc(timestamp)

    if dt is None:
        return None

    if ahora_utc is None:
        ahora_utc = datetime.now(timezone.utc)

    return round(
        (ahora_utc - dt).total_seconds() / 60,
        1,
    )


# ==============================================================================
# 5. THE ODDS API
# ==============================================================================

@st.cache_data(ttl=3600, show_spinner=False)
def obtener_deportes_activos(api_key):
    url = f"{ODDS_API_BASE}/sports/"

    params = {
        "apiKey": api_key,
    }

    try:
        response = requests.get(
            url,
            params=params,
            timeout=15,
        )

        if response.status_code == 401:
            st.error("❌ API Key de The Odds API inválida o vencida.")
            return []

        if response.status_code == 429:
            st.error(
                "❌ Límite de requests alcanzado en The Odds API (429)."
            )
            return []

        response.raise_for_status()

        return [
            s
            for s in response.json()
            if s.get("active")
            and not s.get("has_outrights")
        ]

    except Exception as e:
        st.error(
            f"Error al obtener deportes desde la API: {e}"
        )
        return []


@st.cache_data(ttl=90, show_spinner=False)
def obtener_cuotas_api(api_key, sport_key):

    url = f"{ODDS_API_BASE}/sports/{sport_key}/odds/"

    params = {
        "apiKey": api_key,
        "markets": "h2h",
        "bookmakers": "pinnacle,stake,betonlineag,bet365",
        "oddsFormat": "decimal",
    }

    try:
        response = requests.get(
            url,
            params=params,
            timeout=15,
        )

        restantes = response.headers.get(
            "x-requests-remaining"
        )

        usados = response.headers.get(
            "x-requests-used"
        )

        if restantes is not None:
            st.session_state["odds_api_uso"] = {
                "restantes": restantes,
                "usados": usados,
            }

        if response.status_code == 401:
            st.error(
                f"❌ API Key inválida al consultar {sport_key}."
            )
            return []

        if response.status_code == 422:
            return []

        if response.status_code == 429:
            st.warning(
                f"⚠️ Rate limit alcanzado en {sport_key}."
            )
            return []

        response.raise_for_status()

        return response.json()

    except Exception:
        return []


# ==============================================================================
# 6. PROBABILIDADES
# ==============================================================================

def devig_probabilidades(outcomes):

    if not outcomes:
        return {}

    implicitas = {}

    for outcome in outcomes:

        if not isinstance(outcome, dict):
            continue

        nombre = outcome.get("name")
        precio = outcome.get("price")

        if (
            nombre
            and isinstance(precio, (int, float))
            and precio > 1
        ):
            implicitas[nombre] = 1.0 / precio

    overround = sum(implicitas.values())

    if overround <= 0:
        return {}

    return {
        nombre: round(prob / overround, 6)
        for nombre, prob in implicitas.items()
    }


# ==============================================================================
# 7. DISPERSIÓN DEL MERCADO
# ==============================================================================

def calcular_dispersion_mercado(evento):

    if not isinstance(evento, dict):
        return 0.0

    probs_por_resultado = {}

    for bookmaker in evento.get("bookmakers", []):

        if not isinstance(bookmaker, dict):
            continue

        h2h = next(
            (
                m
                for m in bookmaker.get("markets", [])
                if isinstance(m, dict)
                and m.get("key") == "h2h"
            ),
            None,
        )

        if not h2h:
            continue

        devig = devig_probabilidades(
            h2h.get("outcomes", [])
        )

        for nombre, prob in devig.items():

            probs_por_resultado.setdefault(
                nombre,
                [],
            ).append(prob)

    dispersiones = [
        max(vals) - min(vals)
        for vals in probs_por_resultado.values()
        if len(vals) >= 2
    ]

    return max(dispersiones) if dispersiones else 0.0


# ==============================================================================
# 8. MOVIMIENTO DE PINNACLE
# ==============================================================================

def registrar_y_calcular_movimientos(
    eventos_minificados,
    deporte_key,
):

    if not eventos_minificados:
        return {}

    state_key = (
        f"pinnacle_snapshot_{deporte_key}"
    )

    movimientos = {}
    snapshot_actual = {}

    for ev in eventos_minificados:

        if not isinstance(ev, dict):
            continue

        ev_id = ev.get("id")
        matchup = ev.get("partido")
        prices = ev.get("cuotas_pinnacle", {})

        if ev_id and prices:
            snapshot_actual[ev_id] = {
                "matchup": matchup,
                "prices": prices,
            }

    if (
        state_key in st.session_state
        and isinstance(
            st.session_state[state_key],
            dict,
        )
    ):

        snapshot_previo = st.session_state[
            state_key
        ]

        for ev_id, data_curr in snapshot_actual.items():

            if ev_id not in snapshot_previo:
                continue

            data_prev = snapshot_previo[ev_id]

            for team, price_curr in data_curr.get(
                "prices",
                {},
            ).items():

                price_prev = data_prev.get(
                    "prices",
                    {},
                ).get(team)

                if (
                    price_prev
                    and price_curr
                    and price_prev != price_curr
                ):

                    pct_change = round(
                        (
                            (
                                price_curr
                                - price_prev
                            )
                            / price_prev
                        )
                        * 100,
                        2,
                    )

                    direccion = (
                        "subió"
                        if pct_change > 0
                        else "bajó"
                    )

                    movimientos[
                        f"{data_curr['matchup']} ({team})"
                    ] = (
                        f"Cuota cambió de "
                        f"{price_prev} a "
                        f"{price_curr} "
                        f"({direccion} "
                        f"{abs(pct_change)}%)"
                    )

    st.session_state[state_key] = snapshot_actual

    return movimientos


# ==============================================================================
# 9. PRE-FILTRO + ENRIQUECIMIENTO
# ==============================================================================

def filtrar_y_enriquecer(
    datos_crudos,
    horas_ventana=24,
):

    if (
        not datos_crudos
        or not isinstance(datos_crudos, list)
    ):
        return [], {
            "total": 0,
            "candidatos": 0,
            "fecha": 0,
            "sin_fecha": 0,
            "sin_pinnacle": 0,
            "fuera_cuota": 0,
            "estructural": 0,
            "pendiente": 0,
            "frescura": 0,
        }

    eventos_validos = []

    contador = {
        "total": len(datos_crudos),
        "candidatos": 0,
        "fecha": 0,
        "sin_fecha": 0,
        "sin_pinnacle": 0,
        "fuera_cuota": 0,
        "estructural": 0,
        "pendiente": 0,
        "frescura": 0,
    }

    ahora_utc = datetime.now(timezone.utc)

    limite_utc = (
        ahora_utc
        + timedelta(hours=horas_ventana)
    )

    for evento in datos_crudos:

        if not isinstance(evento, dict):
            continue

        sport_key = evento.get("sport_key")

        registry_entry = obtener_entrada_registry(
            sport_key
        )

        # ----------------------------------------------------------
        # EXCLUSIÓN ESTRUCTURAL
        # ----------------------------------------------------------

        if (
            registry_entry["cobertura"]
            == "excluido_estructural"
        ):
            contador["estructural"] += 1
            continue

        # ----------------------------------------------------------
        # FECHA
        # ----------------------------------------------------------

        commence_str = evento.get(
            "commence_time"
        )

        commence_dt = parsear_fecha_utc(
            commence_str
        )

        if commence_dt is None:
            contador["sin_fecha"] += 1
            continue

        if not (
            ahora_utc
            <= commence_dt
            <= limite_utc
        ):
            contador["fecha"] += 1
            continue

        # ----------------------------------------------------------
        # PINNACLE
        # ----------------------------------------------------------

        pinnacle = next(
            (
                b
                for b in evento.get(
                    "bookmakers",
                    [],
                )
                if isinstance(b, dict)
                and b.get("key") == "pinnacle"
            ),
            None,
        )

        if not pinnacle:
            contador["sin_pinnacle"] += 1
            continue

        h2h = next(
            (
                m
                for m in pinnacle.get(
                    "markets",
                    [],
                )
                if isinstance(m, dict)
                and m.get("key") == "h2h"
            ),
            None,
        )

        if not h2h:
            contador["sin_pinnacle"] += 1
            continue

        outcomes = h2h.get(
            "outcomes",
            [],
        )

        # ----------------------------------------------------------
        # CUOTA
        # ----------------------------------------------------------

        en_rango = any(
            isinstance(o, dict)
            and isinstance(
                o.get("price"),
                (int, float),
            )
            and 1.40
            <= o.get("price")
            <= 2.00
            for o in outcomes
        )

        if not en_rango:
            contador["fuera_cuota"] += 1
            continue

        # ----------------------------------------------------------
        # PINNACLE DEVIG
        # ----------------------------------------------------------

        pinnacle_devig = devig_probabilidades(
            outcomes
        )

        # ----------------------------------------------------------
        # FRESCURA
        # ----------------------------------------------------------

        last_update = pinnacle.get(
            "last_update"
        )

        minutos_inicio = minutos_hasta_inicio(
            commence_dt,
            ahora_utc,
        )

        minutos_antiguedad = antiguedad_minutos(
            last_update,
            ahora_utc,
        )

        freshness_status = (
            "NO_VERIFICABLE"
        )

        if minutos_antiguedad is not None:

            if minutos_inicio < 180:

                if minutos_antiguedad > 90:
                    freshness_status = (
                        "DESCARTAR_FRESCURA"
                    )
                    contador["frescura"] += 1
                    continue

                freshness_status = (
                    "FRESCO_CERCA_EVENTO"
                )

            else:

                if minutos_antiguedad <= 90:
                    freshness_status = (
                        "FRESCO"
                    )
                else:
                    freshness_status = (
                        "ANTIGUO_PERO_NO_DESCARTABLE"
                    )

        # ----------------------------------------------------------
        # LIQUIDEZ
        # ----------------------------------------------------------

        n_bookmakers = len(
            evento.get(
                "bookmakers",
                [],
            )
        )

        dispersion = calcular_dispersion_mercado(
            evento
        )

        if (
            n_bookmakers >= 3
            and dispersion < 0.05
        ):
            liquidez = "Alta"

        elif n_bookmakers >= 2:
            liquidez = "Media"

        else:
            liquidez = (
                "Media/Baja — evaluar según "
                "categoría de liga"
            )

        # ----------------------------------------------------------
        # CUOTAS PINNACLE
        # ----------------------------------------------------------

        cuotas_pinnacle = {
            o.get("name"): o.get("price")
            for o in outcomes
            if (
                isinstance(o, dict)
                and o.get("name")
                and o.get("price")
            )
        }

        # ----------------------------------------------------------
        # PENDIENTE DE DESARROLLO
        # ----------------------------------------------------------

        if (
            registry_entry["cobertura"]
            == "pendiente_desarrollo"
        ):
            contador["pendiente"] += 1

        # ----------------------------------------------------------
        # EVENTO MINIFICADO
        # ----------------------------------------------------------

        evento_minificado = {
            "id": evento.get("id"),

            "deporte": (
                evento.get("sport_title")
                or sport_key
            ),

            "sport_key": sport_key,

            "partido": (
                f"{evento.get('home_team')}"
                f" vs "
                f"{evento.get('away_team')}"
            ),

            "home_team": evento.get(
                "home_team"
            ),

            "away_team": evento.get(
                "away_team"
            ),

            "inicio_utc": commence_str,

            "_minutos_hasta_inicio": minutos_inicio,

            "_pinnacle_devig": pinnacle_devig,

            "cuotas_pinnacle": cuotas_pinnacle,

            "_pinnacle_last_update": last_update,

            "_pinnacle_antiguedad_minutos":
                minutos_antiguedad,

            "_freshness_status":
                freshness_status,

            "_liquidez_backend": liquidez,

            "_dispersion_max_entre_casas":
                round(
                    dispersion,
                    4,
                ),

            "_n_casas_reportando":
                n_bookmakers,

            "_registry_modelo_secundario":
                registry_entry,
        }

        eventos_validos.append(
            evento_minificado
        )

    contador["candidatos"] = len(
        eventos_validos
    )

    return eventos_validos, contador


# ==============================================================================
# 10. RESUMEN DEL FILTRO
# ==============================================================================

def construir_resumen_filtro(
    contador,
    horas_ventana,
):

    return (
        f"Backend recibió "
        f"{contador['total']} eventos.\n\n"

        f"Candidatos enviados a IA: "
        f"{contador['candidatos']}\n"

        f"Ventana: próximas "
        f"{horas_ventana} horas.\n\n"

        f"DESCARTES BACKEND:\n"
        f"- Fuera de ventana temporal: "
        f"{contador['fecha']}\n"
        f"- Fecha faltante/ilegible: "
        f"{contador['sin_fecha']}\n"
        f"- Sin Pinnacle/h2h: "
        f"{contador['sin_pinnacle']}\n"
        f"- Sin cuota 1.40-2.00: "
        f"{contador['fuera_cuota']}\n"
        f"- Exclusión estructural: "
        f"{contador['estructural']}\n"
        f"- Gate de frescura: "
        f"{contador['frescura']}\n"
        f"- Sin segundo modelo autorizado: "
        f"{contador['pendiente']}\n\n"

        f"IMPORTANTE:\n"
        f"Los eventos enviados a la IA todavía deben pasar "
        f"la validación del segundo modelo, EV >=5%, "
        f"divergencia <=7% y confianza >=8/10."
    )


# ==============================================================================
# 11. GEMINI — LISTADO DINÁMICO
# ==============================================================================

@st.cache_data(
    ttl=1800,
    show_spinner=False,
)
def listar_modelos_gemini(
    gemini_api_key,
):

    url = (
        f"{GEMINI_API_BASE}/models"
        f"?key={gemini_api_key}"
    )

    try:

        response = requests.get(
            url,
            timeout=15,
        )

        response.raise_for_status()

        modelos = response.json().get(
            "models",
            [],
        )

        utilizables = []

        for modelo in modelos:

            nombre = (
                modelo.get("name", "")
                .replace(
                    "models/",
                    "",
                )
            )

            metodos = modelo.get(
                "supportedGenerationMethods",
                [],
            )

            if (
                "generateContent"
                in metodos
                and nombre
            ):

                if not any(
                    x in nombre.lower()
                    for x in [
                        "embedding",
                        "image",
                        "audio",
                        "tts",
                        "live",
                    ]
                ):

                    utilizables.append(
                        nombre
                    )

        return sorted(
            list(set(utilizables)),
            reverse=True,
        )

    except Exception as e:

        st.error(
            "No se pudo obtener la lista "
            f"de modelos Gemini: {e}"
        )

        return []


# ==============================================================================
# 12. GEMINI REST + GOOGLE SEARCH GROUNDING
# ==============================================================================

def llamar_gemini_rest(
    gemini_api_key,
    modelo,
    prompt_texto,
):

    url = (
        f"{GEMINI_API_BASE}/models/"
        f"{modelo}:generateContent"
    )

    headers = {
        "x-goog-api-key":
            gemini_api_key,

        "Content-Type":
            "application/json",
    }

    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text":
                            prompt_texto
                    }
                ],
            }
        ],

        # --------------------------------------------------------------
        # NUEVO:
        # Gemini API recibe herramienta de búsqueda.
        # Esto permite que el análisis directo en la aplicación pueda
        # investigar las fuentes externas autorizadas.
        # --------------------------------------------------------------
        "tools": [
            {
                "google_search": {}
            }
        ],

        "generationConfig": {
            "temperature": 0.1,
        },
    }

    response = requests.post(
        url,
        headers=headers,
        json=body,
        timeout=180,
    )

    response.raise_for_status()

    data = response.json()

    texto = ""

    candidates = data.get(
        "candidates",
        [],
    )

    if candidates:

        content = candidates[0].get(
            "content",
            {},
        )

        for part in content.get(
            "parts",
            [],
        ):

            if part.get("text"):
                texto += part["text"]

    grounding = (
        candidates[0]
        .get("groundingMetadata", {})
        if candidates
        else {}
    )

    return (
        texto,
        data.get(
            "usageMetadata",
            {},
        ),
        grounding,
    )


# ==============================================================================
# 13. CLAUDE — LISTADO DINÁMICO
# ==============================================================================

@st.cache_data(
    ttl=1800,
    show_spinner=False,
)
def listar_modelos_claude(
    anthropic_api_key,
):

    url = (
        f"{ANTHROPIC_API_BASE}/models"
    )

    headers = {
        "x-api-key":
            anthropic_api_key,

        "anthropic-version":
            ANTHROPIC_VERSION,
    }

    try:

        response = requests.get(
            url,
            headers=headers,
            timeout=15,
        )

        response.raise_for_status()

        modelos = response.json().get(
            "data",
            [],
        )

        return [
            m.get("id")
            for m in modelos
            if m.get("id")
        ]

    except Exception as e:

        st.error(
            "No se pudo obtener la lista "
            f"de modelos Claude: {e}"
        )

        return []


# ==============================================================================
# 14. CLAUDE — WEB SEARCH + PAUSE TURN
# ==============================================================================

def llamar_claude_rest(
    anthropic_api_key,
    modelo,
    prompt_texto,
    max_tokens=4096,
):

    url = (
        f"{ANTHROPIC_API_BASE}/messages"
    )

    headers = {
        "x-api-key":
            anthropic_api_key,

        "anthropic-version":
            ANTHROPIC_VERSION,

        "content-type":
            "application/json",
    }

    tools = [
        {
            "type":
                "web_search_20250305",

            "name":
                "web_search",

            "max_uses":
                20,
        }
    ]

    messages = [
        {
            "role":
                "user",

            "content":
                prompt_texto,
        }
    ]

    todas_las_queries = []

    max_turnos_web = 5

    for _ in range(
        max_turnos_web
    ):

        body = {
            "model":
                modelo,

            "max_tokens":
                max_tokens,

            "messages":
                messages,

            "tools":
                tools,
        }

        response = requests.post(
            url,
            headers=headers,
            json=body,
            timeout=180,
        )

        response.raise_for_status()

        data = response.json()

        # ----------------------------------------------------------
        # Registrar búsquedas realizadas
        # ----------------------------------------------------------

        for block in data.get(
            "content",
            [],
        ):

            if (
                block.get("type")
                == "server_tool_use"
                and block.get("name")
                == "web_search"
            ):

                query = (
                    block.get(
                        "input",
                        {},
                    )
                    .get(
                        "query"
                    )
                )

                if query:
                    todas_las_queries.append(
                        query
                    )

        stop_reason = data.get(
            "stop_reason"
        )

        # ----------------------------------------------------------
        # Respuesta completa
        # ----------------------------------------------------------

        if stop_reason != "pause_turn":

            textos = [
                block.get(
                    "text",
                    "",
                )
                for block in data.get(
                    "content",
                    [],
                )
                if block.get(
                    "type"
                ) == "text"
            ]

            return (
                "\n\n".join(textos),
                todas_las_queries,
                data.get(
                    "usage",
                    {},
                ),
            )

        # ----------------------------------------------------------
        # PAUSE TURN:
        # conservar exactamente la respuesta del assistant
        # y continuar.
        # ----------------------------------------------------------

        messages.append(
            {
                "role":
                    "assistant",

                "content":
                    data.get(
                        "content",
                        [],
                    ),
            }
        )

        messages.append(
            {
                "role":
                    "user",

                "content":
                    (
                        "Continúa la investigación web "
                        "y completa el análisis siguiendo "
                        "estrictamente todas las reglas "
                        "del prompt original. No inventes "
                        "ningún dato."
                    ),
            }
        )

    raise RuntimeError(
        "Claude superó el máximo de turnos "
        "permitidos para la búsqueda web."
    )


# ==============================================================================
# 15. CONSTRUIR PROMPT FINAL
# ==============================================================================

def construir_prompt_final(
    seleccion,
    hora_rd,
    ahora_utc,
    resumen_filtro,
    seccion_movimiento,
    eventos_filtrados,
):

    return (
        f"{SYSTEM_PROMPT_BLINDADO_V3_2}\n\n"

        f"==================================================\n"
        f"CONTEXTO DE EJECUCIÓN DEL BACKEND\n"
        f"==================================================\n\n"

        f"ÁMBITO:\n"
        f"{seleccion}\n\n"

        f"HORA CONSULTA RD:\n"
        f"{hora_rd}\n\n"

        f"HORA CONSULTA UTC:\n"
        f"{ahora_utc.isoformat()}\n\n"

        f"RESUMEN DE PRE-FILTRADO:\n"
        f"{resumen_filtro}\n\n"

        f"{seccion_movimiento}\n\n"

        f"==================================================\n"
        f"REGLA DE INTEGRIDAD DEL JSON\n"
        f"==================================================\n\n"

        f"Los siguientes campos son calculados por el backend "
        f"y son de SOLO LECTURA:\n\n"

        f"`_pinnacle_devig`\n"
        f"`_pinnacle_last_update`\n"
        f"`_pinnacle_antiguedad_minutos`\n"
        f"`_minutos_hasta_inicio`\n"
        f"`_freshness_status`\n"
        f"`_liquidez_backend`\n"
        f"`_dispersion_max_entre_casas`\n"
        f"`_n_casas_reportando`\n"
        f"`_registry_modelo_secundario`\n\n"

        f"NO los recalcules.\n"
        f"NO los modifiques.\n"
        f"NO los sustituyas.\n\n"

        f"El segundo modelo DEBE obtenerse mediante búsqueda web "
        f"real y exclusivamente desde la fuente autorizada "
        f"en `_registry_modelo_secundario`.\n\n"

        f"==================================================\n"
        f"DATOS JSON PRE-FILTRADOS Y ENRIQUECIDOS\n"
        f"==================================================\n\n"

        f"{json.dumps(eventos_filtrados, indent=2, ensure_ascii=False)}"
    )


# ==============================================================================
# 16. INTERFAZ STREAMLIT
# ==============================================================================

st.set_page_config(
    page_title=
        "Analista Cuantitativo de Apuestas",

    layout=
        "wide",
)

st.title(
    "📊 Analista de Apuesta Única "
    "v3.2 — Multi-IA & Multi-Deporte"
)


# ==============================================================================
# SIDEBAR
# ==============================================================================

with st.sidebar:

    st.header(
        "🔑 Configuración de APIs"
    )

    api_key = st.secrets.get(
        "ODDS_API_KEY",
        "",
    )

    if not api_key:

        api_key = st.text_input(
            "Odds API Key:",
            type="password",
        )

    gemini_api_key = st.secrets.get(
        "GEMINI_API_KEY",
        "",
    )

    if not gemini_api_key:

        gemini_api_key = st.text_input(
            "Gemini API Key (Opcional):",
            type="password",
        )

    anthropic_api_key = st.secrets.get(
        "ANTHROPIC_API_KEY",
        "",
    )

    if not anthropic_api_key:

        anthropic_api_key = st.text_input(
            "Anthropic / Claude API Key (Opcional):",
            type="password",
        )

    st.divider()

    st.caption(
        f"Model Registry: "
        f"{REGISTRY_ULTIMA_REVISION}"
    )

    if "odds_api_uso" in st.session_state:

        uso = st.session_state[
            "odds_api_uso"
        ]

        st.caption(
            "📉 Odds API — "
            f"usados: {uso['usados']} · "
            f"restantes: {uso['restantes']}"
        )


# ==============================================================================
# PROCESAMIENTO PRINCIPAL
# ==============================================================================

if api_key:

    deportes_lista = obtener_deportes_activos(
        api_key
    )

    if deportes_lista:

        opciones_deporte = {
            "🔥 TODOS LOS DEPORTES ACTIVOS":
                "ALL"
        }

        for dep in deportes_lista:

            label = (
                f"{dep.get('group')} - "
                f"{dep.get('title')}"
            )

            opciones_deporte[
                label
            ] = dep.get("key")

        seleccion = st.selectbox(
            "Selecciona el deporte o ámbito a analizar:",
            list(
                opciones_deporte.keys()
            ),
        )

        deporte_key_seleccionado = (
            opciones_deporte[
                seleccion
            ]
        )

        st.divider()

        col_a, col_b = st.columns(2)

        with col_a:

            horas_ventana = st.number_input(
                "Ventana de eventos futuros (horas):",
                min_value=1,
                max_value=72,
                value=24,
                step=1,
            )

        with col_b:

            st.info(
                "🎯 Filtros principales: "
                "cuota 1.40–2.00 · EV mínimo 5% · "
                "divergencia máxima 7% · "
                "confianza mínima 8/10"
            )

        if st.button(
            "🚀 Generar Prompt y Procesar Datos",
            type="primary",
            use_container_width=True,
        ):

            with st.spinner(
                "Consultando The Odds API "
                "y procesando pre-filtros..."
            ):

                datos_acumulados = []

                # ------------------------------------------------------
                # TODOS LOS DEPORTES
                # ------------------------------------------------------

                if (
                    deporte_key_seleccionado
                    == "ALL"
                ):

                    progress_bar = st.progress(
                        0
                    )

                    total_deps = len(
                        deportes_lista
                    )

                    for idx, dep in enumerate(
                        deportes_lista
                    ):

                        cuotas = (
                            obtener_cuotas_api(
                                api_key,
                                dep.get(
                                    "key"
                                ),
                            )
                        )

                        if cuotas:
                            datos_acumulados.extend(
                                cuotas
                            )

                        progress_bar.progress(
                            (idx + 1)
                            / total_deps
                        )

                        time.sleep(
                            0.15
                        )

                    progress_bar.empty()

                # ------------------------------------------------------
                # UN DEPORTE
                # ------------------------------------------------------

                else:

                    datos_acumulados = (
                        obtener_cuotas_api(
                            api_key,
                            deporte_key_seleccionado,
                        )
                    )

                # ------------------------------------------------------
                # HORAS
                # ------------------------------------------------------

                tz_rd = timezone(
                    timedelta(hours=-4)
                )

                ahora_rd = datetime.now(
                    tz_rd
                )

                ahora_utc = datetime.now(
                    timezone.utc
                )

                hora_rd = (
                    ahora_rd.strftime(
                        "%Y-%m-%d %H:%M:%S "
                        "AST (UTC-4)"
                    )
                )

                # ------------------------------------------------------
                # FILTRAR
                # ------------------------------------------------------

                (
                    eventos_filtrados,
                    contador,
                ) = filtrar_y_enriquecer(
                    datos_acumulados,
                    horas_ventana,
                )

                resumen_filtro = (
                    construir_resumen_filtro(
                        contador,
                        horas_ventana,
                    )
                )

                # ------------------------------------------------------
                # MOVIMIENTOS
                # ------------------------------------------------------

                movimientos_pinnacle = (
                    registrar_y_calcular_movimientos(
                        eventos_filtrados,
                        deporte_key_seleccionado,
                    )
                )

                seccion_movimiento = (
                    "SIN SNAPSHOT PREVIO EN ESTA SESIÓN."
                )

                if movimientos_pinnacle:

                    lineas_mov = "\n".join(
                        f"- {k}: {v}"
                        for k, v
                        in movimientos_pinnacle.items()
                    )

                    seccion_movimiento = (
                        "MOVIMIENTOS EN PINNACLE "
                        "DETECTADOS:\n"
                        f"{lineas_mov}"
                    )

                # ------------------------------------------------------
                # MOSTRAR RESUMEN
                # ------------------------------------------------------

                st.write(
                    "### 📌 Resumen de Filtrado Backend"
                )

                st.info(
                    resumen_filtro
                )

                # ------------------------------------------------------
                # CREAR PROMPT
                # ------------------------------------------------------

                if not eventos_filtrados:

                    st.warning(
                        "⚠️ No se encontraron "
                        "candidatos válidos."
                    )

                    st.session_state[
                        "prompt_generado"
                    ] = None

                else:

                    prompt_completo = (
                        construir_prompt_final(
                            seleccion=
                                seleccion,

                            hora_rd=
                                hora_rd,

                            ahora_utc=
                                ahora_utc,

                            resumen_filtro=
                                resumen_filtro,

                            seccion_movimiento=
                                seccion_movimiento,

                            eventos_filtrados=
                                eventos_filtrados,
                        )
                    )

                    st.session_state[
                        "prompt_generado"
                    ] = prompt_completo

                    st.session_state[
                        "eventos_filtrados"
                    ] = eventos_filtrados

                    st.success(
                        "✅ Se consolidaron "
                        f"{len(eventos_filtrados)} "
                        "eventos aptos para el prompt."
                    )


# ==============================================================================
# RESULTADO / PROMPT
# ==============================================================================

if (
    "prompt_generado"
    in st.session_state
    and st.session_state[
        "prompt_generado"
    ]
):

    st.divider()

    st.subheader(
        "🤖 Selecciona la IA"
    )

    st.caption(
        "Los botones web permiten pegar el mismo prompt "
        "en distintas IAs. Las ejecuciones directas de "
        "Gemini y Claude incorporan búsqueda web."
    )

    col1, col2, col3, col4, col5 = st.columns(
        5
    )

    with col1:

        st.link_button(
            "🌐 ChatGPT",
            "https://chatgpt.com",
            use_container_width=True,
        )

    with col2:

        st.link_button(
            "🌐 Claude",
            "https://claude.ai",
            use_container_width=True,
        )

    with col3:

        st.link_button(
            "🌐 Gemini Web",
            "https://gemini.google.com",
            use_container_width=True,
        )

    with col4:

        st.link_button(
            "🌐 DeepSeek",
            "https://chat.deepseek.com",
            use_container_width=True,
        )

    with col5:

        st.link_button(
            "🌐 Copilot",
            "https://copilot.microsoft.com",
            use_container_width=True,
        )

    # --------------------------------------------------------------------------
    # PROMPT
    # --------------------------------------------------------------------------

    st.write(
        "#### 📋 Prompt generado"
    )

    st.code(
        st.session_state[
            "prompt_generado"
        ],
        language="markdown",
    )

    # ==========================================================================
    # GEMINI API
    # ==========================================================================

    if gemini_api_key:

        st.divider()

        st.subheader(
            "⚡ Gemini API — Web Search activo"
        )

        st.caption(
            "La ejecución directa de Gemini utiliza "
            "Google Search grounding para investigar "
            "las fuentes externas exigidas por el Model Registry."
        )

        modelos_disponibles = (
            listar_modelos_gemini(
                gemini_api_key
            )
        )

        if modelos_disponibles:

            modelo_default = next(
                (
                    m
                    for m
                    in modelos_disponibles
                    if (
                        "flash" in m.lower()
                        and "lite"
                        not in m.lower()
                    )
                ),
                modelos_disponibles[0],
            )

            modelo_elegido = (
                st.selectbox(
                    "Modelo Gemini:",
                    modelos_disponibles,
                    index=
                        modelos_disponibles.index(
                            modelo_default
                        ),
                )
            )

            if st.button(
                "🤖 Analizar directamente con Gemini API",
                type="primary",
                use_container_width=True,
            ):

                with st.spinner(
                    f"Analizando con "
                    f"{modelo_elegido} "
                    "y búsqueda web..."
                ):

                    try:

                        (
                            resultado,
                            uso,
                            grounding,
                        ) = llamar_gemini_rest(
                            gemini_api_key,
                            modelo_elegido,
                            st.session_state[
                                "prompt_generado"
                            ],
                        )

                        st.markdown(
                            "### 🏆 Resultado Gemini"
                        )

                        st.markdown(
                            resultado
                        )

                        if uso:

                            with st.expander(
                                "📊 Uso reportado por Gemini"
                            ):

                                st.json(
                                    uso
                                )

                        if grounding:

                            with st.expander(
                                "🔎 Metadatos de búsqueda / grounding"
                            ):

                                st.json(
                                    grounding
                                )

                    except Exception as e:

                        st.error(
                            "Error al ejecutar "
                            f"Gemini API: {e}"
                        )

        else:

            st.warning(
                "No se pudo obtener la lista "
                "de modelos Gemini."
            )


    # ==========================================================================
    # CLAUDE API
    # ==========================================================================

    if anthropic_api_key:

        st.divider()

        st.subheader(
            "⚡ Claude API — Web Search activo"
        )

        st.caption(
            "La llamada incluye el server-side web_search "
            "directamente en la API y maneja automáticamente "
            "los turnos pausados de búsqueda."
        )

        modelos_claude = (
            listar_modelos_claude(
                anthropic_api_key
            )
        )

        if modelos_claude:

            modelo_claude_default = next(
                (
                    m
                    for m
                    in modelos_claude
                    if "sonnet"
                    in m.lower()
                ),
                modelos_claude[0],
            )

            modelo_claude_elegido = (
                st.selectbox(
                    "Modelo Claude:",
                    modelos_claude,
                    index=
                        modelos_claude.index(
                            modelo_claude_default
                        ),
                )
            )

            if st.button(
                "🤖 Analizar directamente con Claude API",
                type="primary",
                use_container_width=True,
            ):

                with st.spinner(
                    f"Analizando con "
                    f"{modelo_claude_elegido} "
                    "y búsqueda web..."
                ):

                    try:

                        (
                            resultado,
                            queries_buscadas,
                            uso,
                        ) = llamar_claude_rest(
                            anthropic_api_key,
                            modelo_claude_elegido,
                            st.session_state[
                                "prompt_generado"
                            ],
                        )

                        st.markdown(
                            "### 🏆 Resultado Claude"
                        )

                        st.markdown(
                            resultado
                        )

                        if queries_buscadas:

                            with st.expander(
                                "🔍 Búsquedas web realizadas "
                                f"({len(queries_buscadas)})"
                            ):

                                for q in queries_buscadas:

                                    st.write(
                                        f"- {q}"
                                    )

                        else:

                            st.warning(
                                "⚠️ Claude no registró "
                                "búsquedas web en esta corrida."
                            )

                        if uso:

                            with st.expander(
                                "📊 Uso reportado por Claude"
                            ):

                                st.json(
                                    uso
                                )

                    except Exception as e:

                        st.error(
                            "Error al ejecutar "
                            f"Claude API: {e}"
                        )

        else:

            st.warning(
                "No se pudo obtener la lista "
                "de modelos Claude."
            )


# ==============================================================================
# PIE
# ==============================================================================

st.divider()

st.caption(
    "Analista Cuantitativo de Apuesta Única "
    "— Blindado v3.2"
)

st.caption(
    "0 picks es un resultado válido. "
    "El sistema prioriza evitar falsos positivos "
    "antes que forzar una apuesta."
)
