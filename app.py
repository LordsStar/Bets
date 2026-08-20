import json
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
import streamlit as st


# ==============================================================================
# CONFIGURACIÓN
# ==============================================================================

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
ANTHROPIC_API_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"

REGISTRY_ULTIMA_REVISION = "2026-08-20"

# Modelo secundario mínimo aceptable.
SECONDARY_MODEL_MIN_QUALITY = 0.80
SECONDARY_MODEL_MIN_SAMPLE = 500

# Freshness del mercado.
FRESHNESS_EVENT_WINDOW_HOURS = 3
FRESHNESS_MAX_AGE_MINUTES = 90

# Reglas del pick.
MIN_EV = 0.05
MAX_DIVERGENCE = 0.07
MIN_CONFIDENCE = 8.0


# ==============================================================================
# SYSTEM PROMPT — BLINDADO v4.0
#
# Cambio principal respecto a v3.1:
# La IA YA NO tiene que construir el segundo modelo.
# El backend se lo entrega verificado.
# ==============================================================================

SYSTEM_PROMPT_BLINDADO_V4 = """
PROMPT — Analista Cuantitativo de Apuesta Única (Blindado v4.0)

ROL:
Actúa como Analista Cuantitativo de Deportes y Tipster Profesional.

OBJETIVO:
Seleccionar UNA sola apuesta —la de mayor confianza estadística— entre TODOS
los eventos recibidos, únicamente dentro de cuota 1.40-2.00.

Un resultado con 0 picks es válido y preferible a forzar una apuesta.

======================================================================
1. ANCLA PINNACLE
======================================================================

OBLIGATORIO:

Usa directamente `_pinnacle_devig`.

NO recalcules el de-vig.

La probabilidad de Pinnacle es la referencia de mercado.

======================================================================
2. SEGUNDO MODELO
======================================================================

El backend YA ha ejecutado el Secondary Model Engine.

Cada evento contiene:

`_secondary_model`

Ese objeto es la ÚNICA fuente válida para el segundo modelo.

NO debes construir otro modelo.

NO debes sustituirlo por otro rating.

NO debes utilizar rankings, opiniones, tipsters, intuiciones o estadísticas
externas para reemplazar `_secondary_model`.

El objeto debe tener:

- status
- source
- source_type
- model_name
- model_version
- probability
- quality_score
- sample_size
- validation_status
- last_update

Solo son válidos:

`status = "verified"`

y

`quality_score >= 0.80`

y

`validation_status = "validated"`

Si `_secondary_model.status` no es "verified":
DESCARTAR.

Si `quality_score < 0.80`:
DESCARTAR.

Si `validation_status` no es "validated":
DESCARTAR.

======================================================================
3. FRESHNESS GATE
======================================================================

Comparar `_pinnacle_last_update` contra `inicio_utc`.

Si faltan MENOS de 3 horas para comenzar y `_pinnacle_last_update`
tiene más de 90 minutos de antigüedad:

DESCARTAR.

Si faltan MÁS de 3 horas:

NO descartar únicamente por antigüedad.

======================================================================
4. LIQUIDEZ
======================================================================

Utiliza `_liquidez_backend` tal cual.

No la reinterpretes.

======================================================================
5. EV
======================================================================

Para cada lado:

EV = (_secondary_probability * cuota_pinnacle) - 1

EV < 5%:
DESCARTAR.

======================================================================
6. DIVERGENCIA
======================================================================

Divergencia absoluta:

abs(Pinnacle de-vig - Secondary Model probability)

Si > 7 puntos porcentuales:

DESCARTAR.

======================================================================
7. CONFIANZA
======================================================================

Calcular 1-10 utilizando:

- ventaja EV
- calidad del segundo modelo
- muestra/validación
- liquidez
- frescura
- movimiento Pinnacle cuando exista

Solo un candidato con confianza >= 8/10 puede ser PICK.

======================================================================
8. ANTI-FABRICACIÓN
======================================================================

No inventar:

- lesiones
- alineaciones
- clima
- cuotas
- resultados
- probabilidades
- ratings
- modelos
- movimientos

El backend ya proporciona los datos cuantitativos.

Si un dato no está disponible:
NO inventarlo.

======================================================================
9. RESULTADO
======================================================================

Seleccionar SOLO UNA apuesta.

Si ninguna cumple todos los gates:

PICK DEL DÍA: NINGUNO

Nunca seleccionar una apuesta para evitar un informe vacío.
"""


# ==============================================================================
# MODEL REGISTRY
#
# El Registry ahora describe:
#
# 1. fuente externa
# 2. modelo interno permitido
# 3. calidad mínima
#
# El modelo interno NO significa "inventar una probabilidad".
# Es un Elo reproducible calculado a partir de resultados históricos.
# ==============================================================================

MODEL_REGISTRY = {

    # --------------------------------------------------------------------------
    # FÚTBOL
    # --------------------------------------------------------------------------

    "soccer": {
        "external": [
            "ClubElo",
        ],
        "internal": [
            "ELO_SOCCER"
        ],
        "min_quality": 0.85,
        "version": "soccer-2.0",
    },

    # --------------------------------------------------------------------------
    # TENIS
    # --------------------------------------------------------------------------

    "tennis": {
        "external": [
            "TennisAbstract"
        ],
        "internal": [
            "ELO_TENNIS"
        ],
        "min_quality": 0.85,
        "version": "tennis-2.0",
    },

    # --------------------------------------------------------------------------
    # MLB
    # --------------------------------------------------------------------------

    "baseball_mlb": {
        "external": [
            "FanGraphs"
        ],
        "internal": [
            "ELO_BASEBALL"
        ],
        "min_quality": 0.85,
        "version": "mlb-2.0",
    },

    # --------------------------------------------------------------------------
    # KBO
    # --------------------------------------------------------------------------

    "baseball_kbo": {
        "external": [
            "ESPN KBO"
        ],
        "internal": [
            "ELO_BASEBALL"
        ],
        "min_quality": 0.82,
        "version": "kbo-1.0",
    },

    # --------------------------------------------------------------------------
    # NPB
    # --------------------------------------------------------------------------

    "baseball_npb": {
        "external": [
            "ESPN NPB"
        ],
        "internal": [
            "ELO_BASEBALL"
        ],
        "min_quality": 0.82,
        "version": "npb-1.0",
    },

    # --------------------------------------------------------------------------
    # NBA
    # --------------------------------------------------------------------------

    "basketball_nba": {
        "external": [
            "Basketball-Reference"
        ],
        "internal": [
            "ELO_BASKETBALL"
        ],
        "min_quality": 0.85,
        "version": "nba-2.0",
    },

    # --------------------------------------------------------------------------
    # WNBA
    # --------------------------------------------------------------------------

    "basketball_wnba": {
        "external": [
            "Basketball-Reference"
        ],
        "internal": [
            "ELO_BASKETBALL"
        ],
        "min_quality": 0.82,
        "version": "wnba-2.0",
    },

    # --------------------------------------------------------------------------
    # NCAAB
    # --------------------------------------------------------------------------

    "basketball_ncaab": {
        "external": [
            "Basketball-Reference"
        ],
        "internal": [
            "ELO_BASKETBALL"
        ],
        "min_quality": 0.82,
        "version": "ncaab-1.0",
    },

    # --------------------------------------------------------------------------
    # NHL
    # --------------------------------------------------------------------------

    "icehockey_nhl": {
        "external": [
            "Hockey-Reference"
        ],
        "internal": [
            "ELO_HOCKEY"
        ],
        "min_quality": 0.82,
        "version": "nhl-2.0",
    },

    # --------------------------------------------------------------------------
    # CRICKET
    # --------------------------------------------------------------------------

    "cricket": {
        "external": [
            "ICC",
            "ESPN Cricinfo"
        ],
        "internal": [
            "ELO_CRICKET"
        ],
        "min_quality": 0.80,
        "version": "cricket-1.0",
    },

    # --------------------------------------------------------------------------
    # BOXEO
    # --------------------------------------------------------------------------

    "boxing": {
        "external": [
            "BoxRec"
        ],
        "internal": [
            "ELO_BOXING"
        ],
        "min_quality": 0.80,
        "version": "boxing-1.0",
    },

    # --------------------------------------------------------------------------
    # MMA
    # --------------------------------------------------------------------------

    "mma": {
        "external": [
            "UFC Stats"
        ],
        "internal": [
            "ELO_MMA"
        ],
        "min_quality": 0.82,
        "version": "mma-1.0",
    },

    # --------------------------------------------------------------------------
    # NFL
    # --------------------------------------------------------------------------

    "americanfootball_nfl": {
        "external": [
            "ESPN FPI"
        ],
        "internal": [
            "ELO_FOOTBALL"
        ],
        "min_quality": 0.85,
        "version": "nfl-2.0",
    },

    # --------------------------------------------------------------------------
    # NFL PRESEASON
    # --------------------------------------------------------------------------

    "americanfootball_nfl_preseason": {
        "external": [],
        "internal": [],
        "min_quality": 1.0,
        "version": "excluded",
        "excluded": True,
    },
}


# ==============================================================================
# UTILIDADES
# ==============================================================================

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None

    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).astimezone(timezone.utc)
    except Exception:
        return None


def normalizar_key(value: Optional[str]) -> str:
    return (value or "").lower().strip()


def obtener_registry(sport_key: str) -> Dict[str, Any]:

    sport_key = normalizar_key(sport_key)

    # Importante:
    # preseason debe ganar antes que nfl genérico.
    if "preseason" in sport_key:
        return MODEL_REGISTRY["americanfootball_nfl_preseason"]

    for key, value in MODEL_REGISTRY.items():
        if key in sport_key:
            return value

    return {
        "external": [],
        "internal": [],
        "min_quality": 0.80,
        "version": "unknown",
    }


# ==============================================================================
# PINNACLE DEVIG
# ==============================================================================

def devig_probabilidades(outcomes: List[Dict[str, Any]]) -> Dict[str, float]:

    if not outcomes:
        return {}

    implicitas = {}

    for outcome in outcomes:

        if not isinstance(outcome, dict):
            continue

        nombre = outcome.get("name")
        precio = outcome.get("price")

        if nombre and precio and precio > 0:
            implicitas[nombre] = 1.0 / float(precio)

    total = sum(implicitas.values())

    if total <= 0:
        return {}

    return {
        nombre: round(prob / total, 4)
        for nombre, prob in implicitas.items()
    }


# ==============================================================================
# DISPERSIÓN
# ==============================================================================

def calcular_dispersion_mercado(evento):

    if not isinstance(evento, dict):
        return 0.0

    probs = {}

    for bookmaker in evento.get("bookmakers", []):

        if not isinstance(bookmaker, dict):
            continue

        h2h = next(
            (
                market
                for market in bookmaker.get("markets", [])
                if market.get("key") == "h2h"
            ),
            None,
        )

        if not h2h:
            continue

        devig = devig_probabilidades(
            h2h.get("outcomes", [])
        )

        for nombre, prob in devig.items():
            probs.setdefault(nombre, []).append(prob)

    dispersiones = [
        max(values) - min(values)
        for values in probs.values()
        if len(values) >= 2
    ]

    return max(dispersiones) if dispersiones else 0.0


# ==============================================================================
# ELO ENGINE
#
# Este motor NO utiliza cuotas Pinnacle.
#
# Solo utiliza resultados históricos.
#
# Eso evita contaminar el segundo modelo con el mercado que queremos evaluar.
# ==============================================================================

def elo_expected(elo_a: float, elo_b: float) -> float:

    return 1.0 / (
        1.0 + 10.0 ** ((elo_b - elo_a) / 400.0)
    )


def actualizar_elo(
    elo_a: float,
    elo_b: float,
    resultado_a: float,
    k: float = 20.0,
) -> Tuple[float, float]:

    esperado_a = elo_expected(elo_a, elo_b)

    nuevo_a = elo_a + k * (
        resultado_a - esperado_a
    )

    nuevo_b = elo_b + k * (
        (1.0 - resultado_a)
        - (1.0 - esperado_a)
    )

    return nuevo_a, nuevo_b


def calcular_elo_desde_partidos(
    partidos: List[Dict[str, Any]],
    home_team: str,
    away_team: str,
    initial_elo: float = 1500.0,
    k: float = 20.0,
) -> Optional[Dict[str, Any]]:

    if not partidos:
        return None

    equipos = {}

    for partido in partidos:

        home = partido.get("home_team")
        away = partido.get("away_team")

        home_score = partido.get("home_score")
        away_score = partido.get("away_score")

        if not home or not away:
            continue

        if home_score is None or away_score is None:
            continue

        try:
            home_score = float(home_score)
            away_score = float(away_score)
        except Exception:
            continue

        equipos.setdefault(home, initial_elo)
        equipos.setdefault(away, initial_elo)

        if home_score > away_score:
            resultado = 1.0
        elif home_score < away_score:
            resultado = 0.0
        else:
            resultado = 0.5

        equipos[home], equipos[away] = actualizar_elo(
            equipos[home],
            equipos[away],
            resultado,
            k=k,
        )

    if home_team not in equipos:
        return None

    if away_team not in equipos:
        return None

    elo_home = equipos[home_team]
    elo_away = equipos[away_team]

    prob_home = elo_expected(
        elo_home + 50.0,  # ventaja local
        elo_away,
    )

    return {
        "home_elo": round(elo_home, 2),
        "away_elo": round(elo_away, 2),
        "probability_home": round(prob_home, 4),
        "sample_size": len(partidos),
    }


# ==============================================================================
# MODELO SECUNDARIO
#
# Importante:
#
# En esta capa NO se usa Pinnacle.
#
# El modelo se construye exclusivamente con información independiente.
# ==============================================================================

def construir_secondary_model_base(
    evento: Dict[str, Any],
    probability_home: float,
    source: str,
    source_type: str,
    model_name: str,
    model_version: str,
    sample_size: int,
    quality_score: float,
    validation_status: str = "validated",
) -> Dict[str, Any]:

    partido = evento.get("partido", "")

    partes = partido.split(" vs ")

    if len(partes) != 2:
        return {
            "status": "unverified",
            "reason": "No se pudieron separar los equipos.",
        }

    home = partes[0].strip()
    away = partes[1].strip()

    probability_home = max(
        0.001,
        min(0.999, float(probability_home)),
    )

    return {
        "status": "verified",
        "source": source,
        "source_type": source_type,
        "model_name": model_name,
        "model_version": model_version,
        "home_team": home,
        "away_team": away,
        "probability": {
            home: round(probability_home, 4),
            away: round(1.0 - probability_home, 4),
        },
        "quality_score": round(quality_score, 3),
        "sample_size": int(sample_size),
        "validation_status": validation_status,
        "last_update": now_utc().isoformat(),
    }


# ==============================================================================
# VALIDACIÓN DEL SECONDARY MODEL
# ==============================================================================

def validar_secondary_model(model: Dict[str, Any]) -> Tuple[bool, str]:

    if not model:
        return False, "secondary_model ausente"

    if model.get("status") != "verified":
        return False, "status != verified"

    if model.get("validation_status") != "validated":
        return False, "modelo no validado"

    quality = float(model.get("quality_score", 0))

    if quality < SECONDARY_MODEL_MIN_QUALITY:
        return False, (
            f"quality_score {quality:.2f} < "
            f"{SECONDARY_MODEL_MIN_QUALITY:.2f}"
        )

    sample = int(model.get("sample_size", 0))

    if sample < SECONDARY_MODEL_MIN_SAMPLE:
        return False, (
            f"sample_size {sample} < "
            f"{SECONDARY_MODEL_MIN_SAMPLE}"
        )

    probabilities = model.get("probability")

    if not isinstance(probabilities, dict):
        return False, "probability inválida"

    if not probabilities:
        return False, "probability vacía"

    total = sum(
        float(v)
        for v in probabilities.values()
        if isinstance(v, (int, float))
    )

    if not 0.98 <= total <= 1.02:
        return False, "probabilidades no suman aproximadamente 1"

    return True, "OK"


# ==============================================================================
# FRESHNESS GATE
# ==============================================================================

def comprobar_frescura(
    inicio_utc: str,
    pinnacle_last_update: str,
) -> Tuple[bool, str]:

    inicio = parse_dt(inicio_utc)
    update = parse_dt(pinnacle_last_update)

    if not inicio:
        return False, "inicio_utc inválido"

    if not update:
        return False, "pinnacle_last_update inválido"

    ahora = now_utc()

    horas_restantes = (
        inicio - ahora
    ).total_seconds() / 3600.0

    antiguedad_minutos = (
        ahora - update
    ).total_seconds() / 60.0

    # Si el partido ya empezó.
    if horas_restantes < 0:
        return False, "evento ya iniciado"

    # Regla Blindado.
    if (
        horas_restantes < FRESHNESS_EVENT_WINDOW_HOURS
        and antiguedad_minutos > FRESHNESS_MAX_AGE_MINUTES
    ):
        return False, (
            f"actualización {antiguedad_minutos:.1f} min antigua "
            f"con {horas_restantes:.2f} h restantes"
        )

    return True, (
        f"{horas_restantes:.2f}h restantes / "
        f"actualización {antiguedad_minutos:.1f} min"
    )


# ==============================================================================
# CALIDAD DEL MODELO
# ==============================================================================

def calcular_quality_elo(
    sample_size: int,
    sport_key: str,
) -> float:

    # Calidad conservadora.
    #
    # No queremos que un modelo con 30 partidos se comporte como uno con
    # 10.000.
    #
    # 500 = mínimo.
    # 5000 = saturación.

    if sample_size < 500:
        return 0.0

    ratio = min(
        sample_size / 5000.0,
        1.0,
    )

    base = 0.80 + (
        0.10 * ratio
    )

    return round(
        min(base, 0.90),
        3,
    )


# ==============================================================================
# ADAPTADOR PARA HISTÓRICO
#
# El backend puede recibir partidos históricos desde una fuente externa.
#
# Esta función está separada deliberadamente para que puedas conectar después
# ESPN/ClubElo/FanGraphs/etc. sin tocar el resto del sistema.
# ==============================================================================

def obtener_historico_espn(
    sport_key: str,
    league: str,
    limit: int = 1000,
) -> List[Dict[str, Any]]:
    """
    Adaptador genérico.

    Si la fuente no devuelve datos compatibles, devuelve [].

    IMPORTANTE:
    No inventa datos.

    La URL puede variar por liga/deporte; por eso el adaptador se mantiene
    aislado y el engine nunca trata un [] como modelo válido.
    """

    # Mapeo de ligas conocidas.
    endpoints = {

        "baseball_mlb": (
            "https://site.api.espn.com/apis/site/v2/sports/"
            "baseball/mlb/scoreboard"
        ),

        "basketball_nba": (
            "https://site.api.espn.com/apis/site/v2/sports/"
            "basketball/nba/scoreboard"
        ),

        "basketball_wnba": (
            "https://site.api.espn.com/apis/site/v2/sports/"
            "basketball/wnba/scoreboard"
        ),

        "icehockey_nhl": (
            "https://site.api.espn.com/apis/site/v2/sports/"
            "hockey/nhl/scoreboard"
        ),

        "americanfootball_nfl": (
            "https://site.api.espn.com/apis/site/v2/sports/"
            "football/nfl/scoreboard"
        ),
    }

    url = endpoints.get(sport_key)

    if not url:
        return []

    try:
        r = requests.get(
            url,
            timeout=15,
        )

        if r.status_code != 200:
            return []

        data = r.json()

    except Exception:
        return []

    partidos = []

    for event in data.get("events", []):

        try:
            competitions = event.get(
                "competitions",
                [],
            )

            if not competitions:
                continue

            competition = competitions[0]

            competitors = competition.get(
                "competitors",
                [],
            )

            if len(competitors) < 2:
                continue

            home = None
            away = None

            for c in competitors:

                team = c.get("team", {}).get("displayName")

                score = c.get("score")

                if c.get("homeAway") == "home":
                    home = {
                        "name": team,
                        "score": score,
                    }

                elif c.get("homeAway") == "away":
                    away = {
                        "name": team,
                        "score": score,
                    }

            if not home or not away:
                continue

            if home["score"] is None or away["score"] is None:
                continue

            partidos.append(
                {
                    "home_team": home["name"],
                    "away_team": away["name"],
                    "home_score": float(home["score"]),
                    "away_score": float(away["score"]),
                }
            )

        except Exception:
            continue

    return partidos[-limit:]


# ==============================================================================
# FALLBACK CONTROLADO
#
# SOLO se permite si el histórico supera los requisitos.
# ==============================================================================

def construir_modelo_elo_para_evento(
    evento: Dict[str, Any],
) -> Dict[str, Any]:

    sport_key = evento.get("sport_key", "")

    registry = obtener_registry(
        sport_key
    )

    if "ELO_" not in " ".join(
        registry.get("internal", [])
    ):
        return {
            "status": "unverified",
            "reason": "No existe modelo interno autorizado",
        }

    partido = evento.get("partido", "")

    partes = partido.split(" vs ")

    if len(partes) != 2:
        return {
            "status": "unverified",
            "reason": "partido inválido",
        }

    home_team = partes[0].strip()
    away_team = partes[1].strip()

    historico = obtener_historico_espn(
        sport_key=sport_key,
        league=sport_key,
    )

    if len(historico) < SECONDARY_MODEL_MIN_SAMPLE:
        return {
            "status": "unverified",
            "reason": (
                f"histórico insuficiente: "
                f"{len(historico)} < "
                f"{SECONDARY_MODEL_MIN_SAMPLE}"
            ),
        }

    resultado = calcular_elo_desde_partidos(
        partidos=historico,
        home_team=home_team,
        away_team=away_team,
    )

    if not resultado:
        return {
            "status": "unverified",
            "reason": "equipos no encontrados en histórico",
        }

    quality = calcular_quality_elo(
        len(historico),
        sport_key,
    )

    if quality < SECONDARY_MODEL_MIN_QUALITY:
        return {
            "status": "unverified",
            "reason": f"quality_score={quality}",
        }

    return construir_secondary_model_base(
        evento=evento,
        probability_home=resultado["probability_home"],
        source="ESPN historical results",
        source_type="validated_internal_model",
        model_name=f"{sport_key} Elo",
        model_version=registry.get(
            "version",
            "unknown",
        ),
        sample_size=len(historico),
        quality_score=quality,
    )


# ==============================================================================
# SECONDARY MODEL ENGINE
# ==============================================================================

@st.cache_data(
    ttl=900,
    show_spinner=False,
)
def ejecutar_secondary_model_engine(
    eventos: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:

    resultados = []

    stats = {
        "eventos": 0,
        "verified": 0,
        "unverified": 0,
        "preseason": 0,
        "historico_insuficiente": 0,
    }

    for evento in eventos:

        stats["eventos"] += 1

        registry = obtener_registry(
            evento.get("sport_key", "")
        )

        if registry.get("excluded"):
            stats["preseason"] += 1
            continue

        # ----------------------------------------------------------------------
        # Intento modelo interno reproducible.
        #
        # Para las fuentes externas directas, el JSON informa al modelo IA
        # cuál debe verificarse.
        #
        # No inventamos una probabilidad externa.
        # ----------------------------------------------------------------------

        secondary_model = construir_modelo_elo_para_evento(
            evento
        )

        if secondary_model.get("status") != "verified":

            stats["unverified"] += 1

            if "histórico insuficiente" in secondary_model.get(
                "reason",
                "",
            ):
                stats["historico_insuficiente"] += 1

            # No enviar eventos sin segundo modelo.
            continue

        valid, reason = validar_secondary_model(
            secondary_model
        )

        if not valid:

            stats["unverified"] += 1
            continue

        evento_nuevo = dict(evento)

        evento_nuevo[
            "_secondary_model"
        ] = secondary_model

        resultados.append(
            evento_nuevo
        )

        stats["verified"] += 1

    return resultados, stats


# ==============================================================================
# PRE-FILTRO PRINCIPAL
# ==============================================================================

def filtrar_y_enriquecer(
    datos_crudos,
    horas_ventana=24,
):

    if not datos_crudos or not isinstance(
        datos_crudos,
        list,
    ):
        return [], "Backend pre-filtró 0 eventos."

    eventos_validos = []

    descartados_fecha = 0
    descartados_sin_fecha = 0
    descartados_sin_pinnacle = 0
    descartados_fuera_rango = 0
    descartados_preseason = 0

    ahora_utc = now_utc()

    limite_utc = (
        ahora_utc
        + timedelta(hours=horas_ventana)
    )

    for evento in datos_crudos:

        if not isinstance(evento, dict):
            continue

        sport_key = evento.get(
            "sport_key",
            "",
        )

        registry = obtener_registry(
            sport_key
        )

        if registry.get("excluded"):
            descartados_preseason += 1
            continue

        commence_str = evento.get(
            "commence_time"
        )

        if not commence_str:
            descartados_sin_fecha += 1
            continue

        commence_dt = parse_dt(
            commence_str
        )

        if not commence_dt:
            descartados_sin_fecha += 1
            continue

        if not (
            ahora_utc
            <= commence_dt
            <= limite_utc
        ):
            descartados_fecha += 1
            continue

        pinnacle = next(
            (
                b
                for b in evento.get(
                    "bookmakers",
                    [],
                )
                if b.get("key") == "pinnacle"
            ),
            None,
        )

        if not pinnacle:
            descartados_sin_pinnacle += 1
            continue

        h2h = next(
            (
                m
                for m in pinnacle.get(
                    "markets",
                    [],
                )
                if m.get("key") == "h2h"
            ),
            None,
        )

        if not h2h:
            descartados_sin_pinnacle += 1
            continue

        outcomes = h2h.get(
            "outcomes",
            [],
        )

        if not any(
            1.40
            <= o.get("price", 0)
            <= 2.00
            for o in outcomes
            if isinstance(o, dict)
        ):
            descartados_fuera_rango += 1
            continue

        pinnacle_devig = devig_probabilidades(
            outcomes
        )

        cuotas_pinnacle = {
            o.get("name"): o.get("price")
            for o in outcomes
            if isinstance(o, dict)
        }

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
                "Media/Baja — evaluar "
                "según categoría de liga"
            )

        evento_minificado = {

            "id": evento.get("id"),

            "deporte": (
                evento.get("sport_title")
                or sport_key
            ),

            "sport_key": sport_key,

            "partido": (
                f"{evento.get('home_team')} "
                f"vs "
                f"{evento.get('away_team')}"
            ),

            "inicio_utc": commence_str,

            "cuotas_pinnacle": cuotas_pinnacle,

            "_pinnacle_devig": pinnacle_devig,

            "_pinnacle_last_update":
                pinnacle.get("last_update"),

            "_liquidez_backend": liquidez,

            "_dispersion_max_entre_casas":
                round(
                    dispersion,
                    4,
                ),

            "_n_casas_reportando":
                n_bookmakers,

            "_registry_modelo_secundario":
                registry,
        }

        eventos_validos.append(
            evento_minificado
        )

    resumen = (
        f"Backend pre-filtró {len(datos_crudos)} eventos: "
        f"{len(eventos_validos)} candidatos iniciales. "
        f"{descartados_fecha} fuera de ventana, "
        f"{descartados_sin_fecha} sin fecha válida, "
        f"{descartados_sin_pinnacle} sin Pinnacle, "
        f"{descartados_fuera_rango} fuera de cuota 1.40-2.00, "
        f"{descartados_preseason} excluidos estructuralmente."
    )

    return eventos_validos, resumen


# ==============================================================================
# MOVIMIENTOS PINNACLE
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

        ev_id = ev.get("id")

        prices = ev.get(
            "cuotas_pinnacle",
            {},
        )

        if ev_id and prices:

            snapshot_actual[ev_id] = {
                "matchup":
                    ev.get("partido"),
                "prices":
                    prices,
            }

    if state_key in st.session_state:

        snapshot_previo = (
            st.session_state[state_key]
        )

        for ev_id, actual in snapshot_actual.items():

            if ev_id not in snapshot_previo:
                continue

            previo = snapshot_previo[
                ev_id
            ]

            for team, price_actual in actual[
                "prices"
            ].items():

                price_prev = previo[
                    "prices"
                ].get(team)

                if (
                    price_prev
                    and price_prev != price_actual
                ):

                    cambio = (
                        (
                            price_actual
                            - price_prev
                        )
                        / price_prev
                    ) * 100

                    direccion = (
                        "subió"
                        if cambio > 0
                        else "bajó"
                    )

                    movimientos[
                        f"{actual['matchup']} ({team})"
                    ] = (
                        f"Cuota cambió de "
                        f"{price_prev} a "
                        f"{price_actual} "
                        f"({direccion} "
                        f"{abs(cambio):.2f}%)"
                    )

    st.session_state[
        state_key
    ] = snapshot_actual

    return movimientos


# ==============================================================================
# THE ODDS API
# ==============================================================================

@st.cache_data(
    ttl=3600,
    show_spinner=False,
)
def obtener_deportes_activos(api_key):

    url = (
        f"{ODDS_API_BASE}/sports/"
        f"?apiKey={api_key}"
    )

    try:

        response = requests.get(
            url,
            timeout=10,
        )

        if response.status_code == 401:

            st.error(
                "❌ API Key inválida."
            )

            return []

        if response.status_code == 429:

            st.error(
                "❌ Rate limit."
            )

            return []

        response.raise_for_status()

        return [
            sport
            for sport in response.json()
            if sport.get("active")
            and not sport.get("has_outrights")
        ]

    except Exception as e:

        st.error(
            f"Error The Odds API: {e}"
        )

        return []


@st.cache_data(
    ttl=90,
    show_spinner=False,
)
def obtener_cuotas_api(
    api_key,
    sport_key,
):

    url = (
        f"{ODDS_API_BASE}/sports/"
        f"{sport_key}/odds/"
    )

    params = {

        "apiKey": api_key,

        "markets": "h2h",

        "bookmakers":
            "pinnacle,stake,betonlineag,bet365",
    }

    try:

        response = requests.get(
            url,
            params=params,
            timeout=10,
        )

        restantes = response.headers.get(
            "x-requests-remaining"
        )

        usados = response.headers.get(
            "x-requests-used"
        )

        if restantes is not None:

            st.session_state[
                "odds_api_uso"
            ] = {
                "restantes": restantes,
                "usados": usados,
            }

        if response.status_code == 401:
            return []

        if response.status_code == 422:
            return []

        if response.status_code == 429:
            return []

        response.raise_for_status()

        return response.json()

    except Exception:
        return []


# ==============================================================================
# GEMINI
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

        r = requests.get(
            url,
            timeout=10,
        )

        r.raise_for_status()

        modelos = r.json().get(
            "models",
            [],
        )

        utilizables = []

        for model in modelos:

            nombre = model.get(
                "name",
                "",
            ).replace(
                "models/",
                "",
            )

            metodos = model.get(
                "supportedGenerationMethods",
                [],
            )

            if (
                "generateContent"
                in metodos
                and not any(
                    x in nombre
                    for x in [
                        "image",
                        "audio",
                        "tts",
                        "embedding",
                        "live",
                        "vision",
                    ]
                )
            ):

                utilizables.append(
                    nombre
                )

        return sorted(
            utilizables,
            reverse=True,
        )

    except Exception as e:

        st.error(
            f"Error Gemini: {e}"
        )

        return []


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
                "parts": [
                    {
                        "text":
                            prompt_texto
                    }
                ]
            }
        ]
    }

    r = requests.post(
        url,
        headers=headers,
        json=body,
        timeout=90,
    )

    r.raise_for_status()

    data = r.json()

    partes = (
        data[
            "candidates"
        ][0][
            "content"
        ][
            "parts"
        ]
    )

    texto = "".join(
        p.get("text", "")
        for p in partes
    )

    uso = data.get(
        "usageMetadata",
        {},
    )

    return texto, uso


# ==============================================================================
# CLAUDE
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

        r = requests.get(
            url,
            headers=headers,
            timeout=10,
        )

        r.raise_for_status()

        modelos = r.json().get(
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
            f"Error Claude: {e}"
        )

        return []


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

    body = {

        "model":
            modelo,

        "max_tokens":
            max_tokens,

        "messages": [
            {
                "role": "user",
                "content":
                    prompt_texto,
            }
        ],

        "tools": [
            {
                "type":
                    "web_search_20250305",

                "name":
                    "web_search",
            }
        ],
    }

    r = requests.post(
        url,
        headers=headers,
        json=body,
        timeout=120,
    )

    r.raise_for_status()

    data = r.json()

    partes_texto = [
        block.get("text", "")
        for block in data.get(
            "content",
            [],
        )
        if block.get("type") == "text"
    ]

    return "\n\n".join(
        partes_texto
    )


# ==============================================================================
# STREAMLIT UI
# ==============================================================================

st.set_page_config(
    page_title=
        "Analista Cuantitativo",
    layout="wide",
)

st.title(
    "📊 Analista de Apuesta Única v4.0"
)

with st.sidebar:

    st.header(
        "🔑 Configuración APIs"
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
            "Gemini API Key:",
            type="password",
        )

    anthropic_api_key = st.secrets.get(
        "ANTHROPIC_API_KEY",
        "",
    )

    if not anthropic_api_key:

        anthropic_api_key = st.text_input(
            "Anthropic API Key:",
            type="password",
        )

    st.divider()

    st.caption(
        f"Model Registry: "
        f"{REGISTRY_ULTIMA_REVISION}"
    )

    st.caption(
        f"Minimum model quality: "
        f"{SECONDARY_MODEL_MIN_QUALITY}"
    )

    st.caption(
        f"Minimum sample: "
        f"{SECONDARY_MODEL_MIN_SAMPLE}"
    )


if api_key:

    deportes_lista = (
        obtener_deportes_activos(
            api_key
        )
    )

    if deportes_lista:

        opciones_deporte = {
            "🔥 TODOS LOS DEPORTES ACTIVOS":
                "ALL"
        }

        for dep in deportes_lista:

            opciones_deporte[
                f"{dep.get('group')} - "
                f"{dep.get('title')}"
            ] = dep.get("key")

        seleccion = st.selectbox(
            "Selecciona ámbito:",
            list(
                opciones_deporte.keys()
            ),
        )

        deporte_key_seleccionado = (
            opciones_deporte[
                seleccion
            ]
        )

        if st.button(
            "🚀 Generar análisis",
            type="primary",
        ):

            with st.spinner(
                "Consultando mercados..."
            ):

                datos_acumulados = []

                if (
                    deporte_key_seleccionado
                    == "ALL"
                ):

                    progress = st.progress(
                        0
                    )

                    total = len(
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

                        progress.progress(
                            (idx + 1)
                            / total
                        )

                        time.sleep(
                            0.15
                        )

                    progress.empty()

                else:

                    datos_acumulados = (
                        obtener_cuotas_api(
                            api_key,
                            deporte_key_seleccionado,
                        )
                    )

                # --------------------------------------------------------------
                # PREFILTRO
                # --------------------------------------------------------------

                eventos_filtrados, resumen = (
                    filtrar_y_enriquecer(
                        datos_acumulados
                    )
                )

                st.subheader(
                    "📌 Prefiltro"
                )

                st.info(
                    resumen
                )

                if not eventos_filtrados:

                    st.warning(
                        "No hay candidatos."
                    )

                else:

                    # ----------------------------------------------------------
                    # SECONDARY MODEL ENGINE
                    # ----------------------------------------------------------

                    with st.spinner(
                        "Construyendo segundo modelo independiente..."
                    ):

                        eventos_modelados, stats = (
                            ejecutar_secondary_model_engine(
                                eventos_filtrados
                            )
                        )

                    st.subheader(
                        "🧠 Secondary Model Engine"
                    )

                    c1, c2, c3, c4 = st.columns(4)

                    c1.metric(
                        "Candidatos",
                        stats["eventos"],
                    )

                    c2.metric(
                        "Modelo verificado",
                        stats["verified"],
                    )

                    c3.metric(
                        "Sin modelo",
                        stats["unverified"],
                    )

                    c4.metric(
                        "Histórico insuficiente",
                        stats[
                            "historico_insuficiente"
                        ],
                    )

                    if not eventos_modelados:

                        st.warning(
                            "⚠️ Ningún evento tiene "
                            "un segundo modelo suficientemente "
                            "validado. Se genera PICK DEL DÍA: NINGUNO."
                        )

                        st.session_state[
                            "prompt_generado"
                        ] = (
                            SYSTEM_PROMPT_BLINDADO_V4
                            + "\n\n"
                            + "PICK DEL DÍA: NINGUNO"
                        )

                    else:

                        # ------------------------------------------------------
                        # MOVIMIENTOS
                        # ------------------------------------------------------

                        movimientos = (
                            registrar_y_calcular_movimientos(
                                eventos_modelados,
                                deporte_key_seleccionado,
                            )
                        )

                        if movimientos:

                            movimiento_texto = (
                                "MOVIMIENTOS PINNACLE:\n"
                                + "\n".join(
                                    f"- {k}: {v}"
                                    for k, v in movimientos.items()
                                )
                            )

                        else:

                            movimiento_texto = (
                                "SIN SNAPSHOT PREVIO "
                                "EN ESTA SESIÓN."
                            )

                        # ------------------------------------------------------
                        # HORA RD
                        # ------------------------------------------------------

                        tz_rd = timezone(
                            timedelta(
                                hours=-4
                            )
                        )

                        hora_rd = (
                            datetime.now(
                                tz_rd
                            ).strftime(
                                "%Y-%m-%d %H:%M:%S "
                                "AST (UTC-4)"
                            )
                        )

                        # ------------------------------------------------------
                        # PROMPT FINAL
                        # ------------------------------------------------------

                        prompt_completo = (

                            f"{SYSTEM_PROMPT_BLINDADO_V4}\n\n"

                            "==================================================\n"
                            "CONTEXTO DE EJECUCIÓN\n"
                            "==================================================\n\n"

                            f"ÁMBITO: {seleccion}\n"

                            f"HORA CONSULTA RD: "
                            f"{hora_rd}\n\n"

                            f"RESUMEN PREFILTRO:\n"
                            f"{resumen}\n\n"

                            f"{movimiento_texto}\n\n"

                            "INSTRUCCIÓN TÉCNICA:\n"
                            "Utiliza directamente:\n"
                            "- `_pinnacle_devig`\n"
                            "- `_pinnacle_last_update`\n"
                            "- `_liquidez_backend`\n"
                            "- `_dispersion_max_entre_casas`\n"
                            "- `_n_casas_reportando`\n"
                            "- `_secondary_model`\n\n"

                            "NO recalcules el de-vig.\n"
                            "NO construyas otro segundo modelo.\n\n"

                            "DATOS JSON:\n"

                            + json.dumps(
                                eventos_modelados,
                                indent=2,
                                ensure_ascii=False,
                            )
                        )

                        st.session_state[
                            "prompt_generado"
                        ] = prompt_completo

                        st.success(
                            f"✅ {len(eventos_modelados)} "
                            f"eventos con segundo modelo "
                            f"verificado."
                        )


# ==============================================================================
# EJECUCIÓN IA
# ==============================================================================

if (
    "prompt_generado"
    in st.session_state
):

    st.divider()

    st.subheader(
        "🤖 Ejecutar análisis"
    )

    col1, col2, col3, col4, col5 = (
        st.columns(5)
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
            "🌐 Gemini",
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

    st.subheader(
        "📋 Prompt generado"
    )

    st.code(
        st.session_state[
            "prompt_generado"
        ],
        language="markdown",
    )


# ==============================================================================
# GEMINI DIRECTO
# ==============================================================================

if gemini_api_key and (
    "prompt_generado"
    in st.session_state
):

    st.divider()

    st.subheader(
        "⚡ Gemini API"
    )

    modelos = listar_modelos_gemini(
        gemini_api_key
    )

    if modelos:

        modelo_default = next(
            (
                m
                for m in modelos
                if "flash" in m
                and "lite" not in m
            ),
            modelos[0],
        )

        modelo = st.selectbox(
            "Modelo Gemini:",
            modelos,
            index=modelos.index(
                modelo_default
            ),
        )

        if st.button(
            "🤖 Analizar con Gemini",
            type="primary",
        ):

            with st.spinner(
                "Analizando..."
            ):

                try:

                    resultado, uso = (
                        llamar_gemini_rest(
                            gemini_api_key,
                            modelo,
                            st.session_state[
                                "prompt_generado"
                            ],
                        )
                    )

                    st.subheader(
                        "🏆 Resultado"
                    )

                    st.markdown(
                        resultado
                    )

                except Exception as e:

                    st.error(
                        f"Error Gemini: {e}"
                    )


# ==============================================================================
# CLAUDE DIRECTO
# ==============================================================================

if anthropic_api_key and (
    "prompt_generado"
    in st.session_state
):

    st.divider()

    st.subheader(
        "⚡ Claude API"
    )

    modelos = listar_modelos_claude(
        anthropic_api_key
    )

    if modelos:

        modelo_default = next(
            (
                m
                for m in modelos
                if "sonnet"
                in m.lower()
            ),
            modelos[0],
        )

        modelo = st.selectbox(
            "Modelo Claude:",
            modelos,
            index=modelos.index(
                modelo_default
            ),
        )

        if st.button(
            "🤖 Analizar con Claude",
            type="primary",
        ):

            with st.spinner(
                "Analizando..."
            ):

                try:

                    resultado = (
                        llamar_claude_rest(
                            anthropic_api_key,
                            modelo,
                            st.session_state[
                                "prompt_generado"
                            ],
                        )
                    )

                    st.subheader(
                        "🏆 Resultado"
                    )

                    st.markdown(
                        resultado
                    )

                except Exception as e:

                    st.error(
                        f"Error Claude: {e}"
                    )
