"""
Analista de Apuesta Única — v3.5

Requisitos nuevos respecto a v3.4 (agregar a requirements.txt):
    pandas
    beautifulsoup4

Cambios v3.4 -> v3.5:
  [A1] Fix FanGraphs: el prompt ahora exige URL con fecha explícita
       (fangraphs.com/scores?date=YYYY-MM-DD) — la URL sin fecha puede
       devolver contenido cacheado de otro día sin aviso.
  [A2] Fútbol: se elimina "538 SPI" del Model Registry (fuente
       descontinuada permanentemente desde 2023). ClubElo se mantiene
       como fuente primaria pero ahora el BACKEND intenta resolverlo
       directamente vía la API CSV real (api.clubelo.com/Fixtures, no
       clubelo.com que bloquea bots) antes de pedirle a la IA que
       busque — igual que ya se hacía con el motor Elo interno para
       KBO/NPB/MMA. Si ClubElo no cubre el partido, cae a Forebet como
       respaldo documentado (sin metodología pública verificable, se
       marca explícitamente). Si ambos fallan, el evento sigue el flujo
       normal de "externa_directa" y la IA busca por su cuenta.
  [B3] Sidebar de consumo de APIs con indicadores de color y umbrales
       de alerta para las tres APIs (Odds, Claude, Gemini).
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone
from io import StringIO

import pandas as pd
import requests
import streamlit as st

# ==============================================================================
# 1. SYSTEM PROMPT V3.5 — BLINDADO
#    Restaura y amplía las salvaguardas: fuentes por deporte, gate de frescura
#    relativo, Model Registry obligatorio, motor Elo interno calibrado como
#    segundo modelo válido, reglas anti-fabricación, formato de salida fijo,
#    tabla de transparencia de descartes (v3.3), chequeo de lesión/estado
#    físico para tenis/boxeo/MMA (v3.4), fuente de respaldo documentada
#    (v3.4), y ahora (v3.5):
#      - [A1] Fix obligatorio de URL con fecha para FanGraphs.
#      - [A2] Fix obligatorio de dominio API para ClubElo + nota de Forebet
#        como respaldo sin metodología pública verificable + prohibición
#        explícita de citar "538 SPI" (fuente descontinuada).
#      - Nueva cobertura "modelo_externo_backend": el backend ya resolvió
#        el segundo modelo de fútbol vía ClubElo/Forebet antes de que la IA
#        tenga que buscar nada.
# ==============================================================================
SYSTEM_PROMPT_BLINDADO_V3_5 = """
PROMPT — Analista Cuantitativo de Apuesta Única (Blindado v3.5)

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

3. VALIDACIÓN CRUZADA (Segundo Modelo) — EXCLUSIVAMENTE vía Model Registry:
   Cada evento trae `_registry_modelo_secundario` con la fuente autorizada para
   ese deporte específico. Reglas ESTRICTAS según el campo `cobertura`:

   - "externa_directa": debes REALIZAR la búsqueda web real en `fuente_primaria`
     (o `fuente_secundaria` si la primaria falla) ANTES de concluir que no se
     puede verificar. Prohibido responder "no se puede confirmar" sin haber
     intentado la búsqueda.

     >>> [A1] FIX OBLIGATORIO PARA FanGraphs (fuente primaria de béisbol) <<<
     Cuando `fuente_primaria` = "FanGraphs", la consulta DEBE hacerse con
     fecha explícita en la URL:
         https://www.fangraphs.com/scores?date=YYYY-MM-DD
     usando la fecha derivada de `inicio_utc` del evento. NUNCA uses la URL
     sin el parámetro de fecha (fangraphs.com/scores a secas) — esa variante
     puede devolver contenido cacheado de un día distinto sin ningún aviso
     visible. Si tras aplicar la fecha correcta los equipos/horarios del
     resultado NO coinciden con el partido esperado, trata el intento como
     fallido y sigue el flujo normal de "fuente primaria inaccesible".

     >>> [A2] FIX OBLIGATORIO PARA ClubElo (fuente primaria de fútbol) <<<
     Si un evento de fútbol llega con `cobertura` = "externa_directa" (es
     decir, el backend NO logró resolverlo por su cuenta — ver
     "modelo_externo_backend" más abajo), y necesitas buscar ClubElo tú
     mismo, la consulta DEBE hacerse contra el subdominio API, nunca contra
     el sitio web:
         http://api.clubelo.com/Fixtures   (próximos partidos)
         http://api.clubelo.com/YYYY-MM-DD (ranking Elo de un día)
     El dominio clubelo.com (sin "api.") bloquea el acceso automatizado.

     - RESPALDO DOCUMENTADO: si ni `fuente_primaria` ni `fuente_secundaria`
       exponen un número públicamente accesible tras un intento real de
       búsqueda, revisa si el evento trae un campo `fuente_respaldo` en el
       registry. Si existe, puedes usarlo — pero cítalo EXPLÍCITAMENTE como
       respaldo, nunca como si fuera la fuente primaria. Ejemplo correcto:
       "Fuente: ESPN Analytics — Matchup Predictor (respaldo documentado;
       FanGraphs no expuso un número público tras la búsqueda)".

       >>> [A2] NOTA ESPECÍFICA PARA Forebet (respaldo de fútbol) <<<
       Forebet no publica su metodología, variables de entrada ni backtests
       verificables (es una caja negra algorítmica). Al citarlo como
       fuente_respaldo, la nota debe declarar explícitamente esta
       limitación, por ejemplo: "Fuente: Forebet (respaldo documentado, sin
       metodología pública verificable; ClubElo no expuso un número
       accesible tras la búsqueda)". Esto debe pesar a la baja en el
       desglose de Confianza (regla 6, componente "calidad/frescura de la
       fuente").

     - Si NO hay `fuente_respaldo` documentada y ninguna de las fuentes
       oficiales dio un número verificable, el evento se descarta en la
       categoría 2 (segundo modelo no disponible), con nota "fuente primaria
       inaccesible, sin respaldo documentado". PROHIBIDO improvisar o
       sustituir con una fuente no listada en el registry ni en su campo
       `fuente_respaldo`.

   - "modelo_interno_elo": el backend YA calculó una probabilidad calibrada con
     resultados reales recientes (no es una fuente web). Usa directamente los
     campos `probabilidad_elo_home`, `elo_home`, `elo_away`,
     `brier_score_historico` y `muestras_brier` como segundo modelo — NO hace
     falta buscar en la web para estos eventos. Cita la fuente como:
     "Modelo Elo interno (backend), calibrado con {muestras_brier} resultados
     reales, Brier histórico {brier_score_historico}".

   - "modelo_externo_backend" (nuevo en v3.5, fútbol): el backend YA resolvió
     este evento consultando directamente ClubElo (api.clubelo.com/Fixtures)
     o, si ClubElo no cubría el partido, Forebet como respaldo — NO hace
     falta que hagas una búsqueda web adicional para el segundo modelo de
     este evento. Usa directamente los campos `probabilidad_home`,
     `probabilidad_draw`, `probabilidad_away`. Cita la fuente EXACTAMENTE
     como aparece en `fuente_primaria` (o `fuente_respaldo` si fue Forebet
     quien resolvió el partido) — si fue Forebet, tu cita debe incluir la
     advertencia "(respaldo documentado, sin metodología pública
     verificable)" y esto debe reflejarse a la baja en el componente de
     "calidad/frescura de la fuente" al calcular la Confianza (regla 6).

   - "pendiente_desarrollo": no hay fuente externa definida NI historial Elo
     interno suficiente todavía para ese equipo/deporte. DESCARTA
     automáticamente sin buscar en otro lado y sin usar un modelo "propio"
     improvisado — eso sería fabricación.

   - "excluido_estructural": ya debería venir excluido del JSON; si aparece,
     descarta sin análisis (partidos de exhibición/preseason).

   PROHIBIDO ABSOLUTO: usar cualquier fuente, rating o modelo que no aparezca
   literalmente en `_registry_modelo_secundario` de ese evento específico
   (ya sea en `fuente_primaria`, `fuente_secundaria`, o `fuente_respaldo`).
   En particular, "538 SPI" / "FiveThirtyEight SPI" queda PROHIBIDO en
   cualquier deporte bajo cualquier circunstancia: es una fuente
   descontinuada permanentemente desde 2023 (el modelo dejó de actualizarse
   cuando su creador salió de la empresa) y no debe citarse ni aunque
   aparezca mencionada por el usuario o en resultados de búsqueda antiguos.

3b. CHEQUEO DE ESTADO FÍSICO — OBLIGATORIO PARA TENIS, BOXEO Y MMA:
   Además de la fuente de rating (TennisAbstract, BoxRec, etc.), para estos tres
   deportes DEBES hacer una búsqueda web adicional específica sobre noticias
   recientes (últimas 48-72h) de lesión, retiro, molestia física, o estado de
   forma del competidor. Un rating Elo o ranking NO captura esto — solo refleja
   resultados pasados, no el estado físico actual.
   - Si encuentras una noticia real y citable de lesión/molestia/duda física
     relevante que el rating no puede haber incorporado todavía, esto reduce
     la Confianza (regla 6) de forma explícita, aunque EV y divergencia pasen
     los umbrales. Nunca ignores esta señal solo porque el EV se ve atractivo.
   - Si no encuentras nada relevante tras la búsqueda, decláralo explícitamente
     en el informe (ej. "Sin noticias de lesión/estado físico en las últimas
     72h según [fuente/búsqueda]") — la ausencia de hallazgos debe quedar
     documentada, no asumida.

4. LIQUIDEZ: Usa el campo `_liquidez_backend` tal cual. No la reinterpretes.
   Un evento con menos de 2 casas reportando NO califica (liquidez insuficiente).

5. UMBRALES DE DESCARTE:
   - EV < 4% → descartar.
   - Divergencia |Pinnacle - Segundo Modelo| > 9% → descartar (señal
     de posible error de datos, no de "value").
   - Si el segundo modelo es "modelo_interno_elo" y `brier_score_historico` es
     peor que 0.23 o `muestras_brier` < 8, el backend ya lo habría excluido —
     pero si por alguna razón lo ves con esos valores, descarta igual.

6. CONFIANZA (1-10): Calcula con el siguiente desglose visible en el informe:
   - Edge estadístico (EV real vs. umbral)
   - Calidad/frescura de la fuente del segundo modelo (una fuente externa
     reciente pesa más que un modelo interno con pocas muestras; una fuente
     de respaldo documentada pesa menos que la fuente primaria oficial; una
     fuente de respaldo SIN metodología pública verificable —p. ej.
     Forebet— pesa menos todavía que una fuente de respaldo transparente
     —p. ej. ESPN Analytics, que sí documenta que pondera abridor/lineup
     del día—)
   - Liquidez del mercado
   - Coherencia entre movimiento de línea (si hay datos) y el pick
   - Para tenis/boxeo/MMA: resultado del chequeo de estado físico (regla 3b).
     Una noticia real de lesión/molestia no cuantificable en el rating debe
     bajar este componente de forma explícita.
   Un pick solo califica si la confianza total es >= 8/10.

REGLAS ANTI-FABRICACIÓN (obligatorias, sin excepción):
- Nunca inventes lesiones, alineaciones, clima o noticias que no hayas confirmado
  con una fuente real y citada.
- Nunca inventes cuotas, nombres de equipos/jugadores o resultados históricos que
  no estén en el JSON de entrada o en una fuente web verificada.
- Si falta cualquier dato necesario para completar el análisis de un evento, ese
  evento se descarta — nunca se rellena el vacío con una suposición "razonable".
- Cada afirmación estadística debe llevar su fuente (nombre + URL, "Modelo Elo
  interno" con sus métricas si aplica, o la fuente de respaldo citada como tal,
  incluyendo la advertencia de caja negra cuando corresponda).
- Si un evento se descartó en la categoría 1, 2 o 3 (frescura, pendiente_desarrollo
  /fuente inaccesible sin respaldo, o liquidez), NUNCA calcules ni inventes un EV%
  o divergencia% para él — en esos casos ni siquiera se llegó a evaluar el segundo
  modelo. Repórtalo como "N/A — no se calculó" en la tabla de la sección 4.
- El campo `fuente_respaldo` NUNCA se usa por comodidad o para ahorrar una
  búsqueda — solo se usa después de haber intentado realmente la fuente primaria
  (con la URL correcta según las notas A1/A2 de arriba) y, si aplica, la
  secundaria, y haber confirmado que ninguna expone un número público.

CATEGORIZACIÓN DE DESCARTES — MUTUAMENTE EXCLUYENTE:
Cada evento descartado cae en EXACTAMENTE UNA categoría, evaluada en este orden
de prioridad (aplica la primera que corresponda y detente ahí, no evalúes las
siguientes para ese evento):
   1º Gate de frescura (regla 2)
   2º Segundo modelo no disponible — "pendiente_desarrollo" O "externa_directa"
      sin fuente primaria/secundaria accesible NI `fuente_respaldo` documentada
      (regla 3)
   3º Liquidez insuficiente — menos de 2 casas (regla 4)
   4º EV por debajo del umbral (regla 5)
   5º Divergencia por encima del umbral (regla 5)
   6º Confianza por debajo de 8/10, aun con EV y divergencia dentro de rango
      (incluye el caso de una señal de estado físico no cuantificable, regla 3b,
      y el caso de respaldo sin metodología pública verificable)
Un evento NUNCA debe contarse en dos categorías a la vez.

AUTO-VERIFICACIÓN OBLIGATORIA ANTES DE ENTREGAR EL INFORME:
Suma (eventos por cada categoría de descarte) + (1 si hay pick, 0 si no) debe
ser EXACTAMENTE igual al número total de eventos evaluados que recibiste en el
JSON. Si no cuadra, revisa tu categorización y corrígela antes de responder —
no entregues un informe con números que no concilien.

FORMATO DE SALIDA (obligatorio, en español):
1. Resumen: cuántos eventos se evaluaron y el desglose EXACTO por categoría
   (las 6 de arriba), con la verificación de que suman el total.
2. Si hay pick: Partido | Mercado | Cuota Pinnacle | Prob. implícita de-vigged |
   Prob. segundo modelo (con fuente/métricas) | EV% | Confianza (con desglose) |
   Justificación en 3-4 líneas.
3. Si NO hay pick: decirlo explícitamente en la primera línea ("PICK DEL DÍA:
   NINGUNO") y explicar brevemente, por categoría, por qué ningún evento
   alcanzó el umbral.
4. TABLA DE TRANSPARENCIA — solo para eventos que SÍ llegaron a calcularse
   (categorías 4, 5 y 6 — EV insuficiente, divergencia excesiva, o confianza
   <8/10). Para las categorías 1, 2 y 3 (frescura, pendiente_desarrollo/fuente
   inaccesible, liquidez insuficiente) NO se arma tabla evento por evento —
   repórtalas solo como conteo agregado en el resumen (punto 1), porque en esas
   categorías nunca se llegó a calcular EV ni divergencia y desglosarlas no
   aporta información nueva.

   Formato de la tabla (una fila por evento de las categorías 4/5/6):
   | Partido | Categoría | EV% | Divergencia% | Confianza | Motivo breve (1 línea) |

   - EV% y Divergencia%: el número real calculado, con 1-2 decimales.
   - Confianza: solo aplica si el evento llegó a la categoría 6 (si fue
     descartado en 4 o 5, escribe "N/A — descartado antes de este cálculo").
   - Motivo breve: la razón puntual (ej. "EV 0.1%, por debajo del umbral 4%",
     "Divergencia 9.7% vs Pinnacle, fuente TennisAbstract hElo", "Confianza
     6/10 — fuente externa reciente pero noticia de lesión no cuantificable",
     "Confianza 6/10 — respaldo Forebet sin metodología pública verificable").
5. CASI CALIFICÓ: de los eventos en categorías 4, 5 y 6 que SÍ tuvieron datos
   reales calculados (no los marcados "N/A — no se pudo verificar"), identifica
   los 1-3 que estuvieron más cerca de pasar TODOS los umbrales — por ejemplo,
   divergencia apenas sobre el 9%, EV apenas debajo del 4%, o confianza a 1-2
   puntos de 8/10. Preséntalos en una tabla corta, ordenada de más cerca a
   menos cerca del umbral:
   | Partido | Qué faltó | Qué tan cerca (número exacto vs. umbral) |
   Si ningún evento tiene datos reales suficientes para esta comparación,
   omite la tabla y dilo explícitamente: "No hay eventos con datos suficientes
   para evaluar cercanía al umbral en esta corrida."
"""

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
ANTHROPIC_API_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"

# Umbrales para el sidebar de consumo de APIs (sección 5).
ODDS_YELLOW_THRESHOLD = 100
ODDS_RED_THRESHOLD = 20
CLAUDE_TOKEN_WARNING = 500_000   # tokens acumulados en la SESIÓN, no cuota real del plan
GEMINI_TOKEN_WARNING = 500_000   # ídem

# ==============================================================================
# 1b. MODEL REGISTRY — fuentes autorizadas de segundo modelo, por deporte.
#     Vive en código (versionado, editable a mano), NO en el prompt.
#     "cobertura" posibles:
#       - "externa_directa"        → fuente pública conocida, la IA debe
#                                     buscarla ella misma si el backend no
#                                     logró resolverla antes.
#       - "modelo_externo_backend" → (nuevo v3.5) el backend YA resolvió el
#                                     dato consultando la fuente real
#                                     directamente (hoy: ClubElo/Forebet
#                                     para fútbol). Cero búsqueda de IA.
#       - "pendiente_desarrollo"   → sin fuente externa Y sin Elo interno
#                                     maduro todavía. Se descarta, nunca se
#                                     improvisa.
#       - "excluido_estructural"   → se excluye antes de llegar a la IA.
#     Los deportes marcados abajo como "usa_elo_interno": True son candidatos a
#     que el motor Elo interno los resuelva automáticamente cuando acumule
#     suficiente historial (ver sección 1c).
#
#     "fuente_respaldo": fuente de respaldo DOCUMENTADA que la IA puede citar
#     (explícitamente como respaldo, nunca como si fuera la primaria) SOLO si
#     tanto `fuente_primaria` como `fuente_secundaria` (si existe) no exponen
#     un número público tras un intento real de búsqueda.
#
#     v3.5: se elimina "FiveThirtyEight SPI" del registry de fútbol (fuente
#     descontinuada permanentemente desde 2023) y se agrega "Forebet" como
#     fuente_respaldo, con nota explícita de que no tiene metodología pública
#     verificable. ClubElo se mantiene como fuente_primaria, resuelta ahora
#     preferentemente por el backend (ver sección 1d) en vez de por la IA.
# ==============================================================================
REGISTRY_ULTIMA_REVISION = "2026-08-22"

MODEL_REGISTRY = [
    {"patron": "americanfootball_nfl_preseason", "fuente_primaria": None, "fuente_secundaria": None,
     "cobertura": "excluido_estructural", "version": "1.0", "usa_elo_interno": False},
    {"patron": "soccer", "fuente_primaria": "ClubElo (api.clubelo.com/Fixtures)",
     "fuente_secundaria": None,
     "fuente_respaldo": "Forebet (sin metodología pública verificable)",
     "cobertura": "externa_directa", "version": "2.0", "usa_elo_interno": False},
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


# ==============================================================================
# 1d. CLUBELO / FOREBET (fútbol) — resolución directa desde el backend
#     [NUEVO v3.5]
#
#     Antes, el registry solo indicaba "busca ClubElo" y la IA tenía que
#     buscarlo por su cuenta en cada corrida (gastando tokens y sujeto a
#     errores de búsqueda). Ahora el backend intenta resolverlo DIRECTO,
#     igual que ya hace con el motor Elo interno para KBO/NPB/MMA:
#
#     1. Intenta ClubElo vía la API CSV real: http://api.clubelo.com/Fixtures
#        (NO clubelo.com, que bloquea acceso automatizado).
#     2. Si ClubElo no cubre el partido o el servicio está caído (se ha visto
#        "Site overloaded" en el pasado), cae a Forebet como respaldo
#        documentado — sin metodología pública verificable, se marca así
#        explícitamente para que la IA lo refleje en su cálculo de Confianza.
#     3. Si ninguno resuelve el partido, se devuelve None y el evento sigue
#        el flujo normal de "externa_directa": la IA busca por su cuenta,
#        con las URLs correctas indicadas en el prompt (sección A2).
# ==============================================================================

CLUBELO_API_BASE = "http://api.clubelo.com"
FOREBET_URL = "https://www.forebet.com/en/football-tips-and-predictions-for-today"


@st.cache_data(ttl=3600, show_spinner=False)
def obtener_fixtures_clubelo():
    """Descarga /Fixtures de ClubElo (probabilidades pre-calculadas para
    todos los próximos partidos). Cacheado 1h. Devuelve None si el
    servicio está inaccesible."""
    try:
        r = requests.get(f"{CLUBELO_API_BASE}/Fixtures", timeout=10)
        r.raise_for_status()
        texto = r.text.strip()
        if not texto or texto.lower().startswith("site overloaded"):
            return None
        df = pd.read_csv(StringIO(r.text))
        return df if not df.empty else None
    except Exception:
        return None


def _es_diferencia_positiva(nombre_columna):
    try:
        return int(nombre_columna) > 0
    except ValueError:
        return False


def _es_diferencia_negativa(nombre_columna):
    try:
        return int(nombre_columna) < 0
    except ValueError:
        return False


def _prob_desde_fixtures_clubelo(df, home_team, away_team):
    """Busca un partido específico dentro del dataframe de /Fixtures y
    agrega las columnas de diferencia de gol en Home/Draw/Away.

    NOTA: la agregación asume el esquema de columnas descrito en
    clubelo.com/API (columnas numéricas de diferencia de gol -5..+5).
    Verificar contra una corrida real antes de confiar 100% en producción
    — si el esquema no calza exactamente, esta función simplemente no
    encuentra el partido (devuelve None) y el flujo cae a Forebet/IA sin
    romper nada."""
    if df is None or df.empty:
        return None
    try:
        match = df[
            (df["Home"].str.strip().str.lower() == home_team.strip().lower())
            & (df["Away"].str.strip().str.lower() == away_team.strip().lower())
        ]
        if match.empty:
            return None
        row = match.iloc[0]
        cols_gol = [c for c in df.columns if c not in ("Date", "League", "Home", "Away")]
        prob_home = sum(row[c] for c in cols_gol if _es_diferencia_positiva(c))
        prob_away = sum(row[c] for c in cols_gol if _es_diferencia_negativa(c))
        prob_draw = max(0.0, 1.0 - prob_home - prob_away)
        return {
            "prob_home": round(float(prob_home), 4),
            "prob_draw": round(float(prob_draw), 4),
            "prob_away": round(float(prob_away), 4),
        }
    except Exception:
        return None


def obtener_prediccion_forebet(home_team, away_team):
    """Respaldo cuando ClubElo no está disponible o no cubre el partido.
    Forebet no tiene API oficial -> scraping liviano, inherentemente frágil
    (puede romperse si Forebet cambia su HTML; requiere `beautifulsoup4`).
    Se marca explícitamente como fuente sin metodología pública verificable.

    NOTA: los selectores CSS (".rcnt", etc.) son de ejemplo — inspecciona
    el HTML real de Forebet y ajústalos antes de confiar en esto en
    producción. Si falla la extracción, devuelve None sin romper el flujo
    general (el evento cae a "externa_directa" normal)."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None

    try:
        r = requests.get(
            FOREBET_URL, timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (compatible; BlindadoBot/1.0)"},
        )
        r.raise_for_status()
    except Exception:
        return None

    try:
        soup = BeautifulSoup(r.text, "html.parser")
        filas = soup.select(".rcnt")  # selector de ejemplo — ajustar contra HTML real
        for fila in filas:
            texto_fila = fila.get_text(" ", strip=True).lower()
            if home_team.lower() in texto_fila and away_team.lower() in texto_fila:
                prob_home_el = fila.select_one(".prc_1")
                prob_draw_el = fila.select_one(".prc_X")
                prob_away_el = fila.select_one(".prc_2")
                if not (prob_home_el and prob_draw_el and prob_away_el):
                    return None
                return {
                    "prob_home": round(float(prob_home_el.get_text(strip=True).replace("%", "")) / 100, 4),
                    "prob_draw": round(float(prob_draw_el.get_text(strip=True).replace("%", "")) / 100, 4),
                    "prob_away": round(float(prob_away_el.get_text(strip=True).replace("%", "")) / 100, 4),
                }
    except Exception:
        return None

    return None


def obtener_entrada_clubelo_o_forebet(sport_key, home_team, away_team):
    """Intenta resolver el segundo modelo de un evento de fútbol
    DIRECTAMENTE desde el backend (sin gastar tokens de IA), en este orden:
    1. ClubElo API (fuente primaria real, vía api.clubelo.com)
    2. Forebet (respaldo documentado, sin metodología pública verificable)
    Si ninguno resuelve el partido, devuelve None y el evento sigue el
    flujo normal de 'externa_directa' (la IA hace la búsqueda web ella
    misma, como en versiones anteriores)."""
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
            "version": "1.0",
            "ultima_revision": REGISTRY_ULTIMA_REVISION,
            "probabilidad_home": resultado_clubelo["prob_home"],
            "probabilidad_draw": resultado_clubelo["prob_draw"],
            "probabilidad_away": resultado_clubelo["prob_away"],
        }

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
            "version": "1.0",
            "ultima_revision": REGISTRY_ULTIMA_REVISION,
            "probabilidad_home": resultado_forebet["prob_home"],
            "probabilidad_draw": resultado_forebet["prob_draw"],
            "probabilidad_away": resultado_forebet["prob_away"],
            "metodologia_publica_respaldo": False,
        }

    return None


def obtener_entrada_registry(sport_key, home_team=None, away_team=None, estado_elo=None):
    """Punto único de verdad para el segundo modelo de un evento:
    1. Fútbol: intenta resolución directa vía ClubElo/Forebet (backend).
    2. Deportes con motor Elo interno marcado y calidad suficiente: usa Elo interno.
    3. Si nada de lo anterior aplica: devuelve la entrada estática del registry
       (la IA deberá buscar por su cuenta si la cobertura es 'externa_directa')."""
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
    eventos_con_backend_directo = 0
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
        f"Elo interno, {eventos_con_backend_directo} resueltos directo por el backend "
        f"(ClubElo/Forebet, fútbol) y {eventos_pendientes_desarrollo} siguen sin segundo modelo "
        f"disponible (la IA los descartará)."
    )
    return eventos_validos, resumen_filtro


# ==============================================================================
# 2b. MODO "POR DEPORTE"
#
#     PROBLEMA QUE RESUELVE: con "TODOS LOS DEPORTES ACTIVOS" en un solo
#     prompt, el presupuesto de búsqueda de la IA se reparte entre 30-40+
#     eventos. En la práctica, esto significa que varios eventos con valor
#     real (EV/divergencia buenos) terminan sin verificar simplemente porque
#     la IA se quedó sin tiempo/tokens antes de llegar a ellos — no porque
#     hayan fallado ningún umbral. Este modo divide el trabajo en varios
#     prompts más pequeños, uno por familia de deporte, para que cada evento
#     reciba una búsqueda real.
#
#     BONUS DE EFICIENCIA: los eventos con cobertura "pendiente_desarrollo" o
#     "excluido_estructural" NUNCA necesitan que la IA busque nada — el
#     resultado ya es 100% determinístico. Este modo los separa y genera su
#     resumen directamente en código, con CERO tokens de IA gastados en ellos.
#     Lo mismo aplica ahora (v3.5) a los eventos "modelo_externo_backend": el
#     backend ya trae el número, la IA solo tiene que citarlo y calcular
#     EV/divergencia/confianza — no necesita gastar una búsqueda web en ellos.
# ==============================================================================

def familia_deporte(sport_key):
    """Agrupa eventos en familias amplias usando el prefijo del sport_key de
    The Odds API (ej. 'soccer_belgium_first_div' -> 'soccer',
    'baseball_mlb' -> 'baseball'). Sirve para dividir un prompt gigante en
    varios prompts manejables, uno por familia."""
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
    """Divide los eventos de una familia entre los que SÍ necesitan que la IA
    busque y analice ('externa_directa', 'modelo_interno_elo',
    'modelo_externo_backend') y los que ya son descarte 100% determinístico
    por reglas del backend ('pendiente_desarrollo', 'excluido_estructural').
    Estos últimos NUNCA necesitan gastar tokens de IA — el resultado ya se
    sabe de antemano."""
    necesita_ia, automaticos = [], []
    for ev in eventos_familia:
        cobertura = ev.get("_registry_modelo_secundario", {}).get("cobertura")
        if cobertura in ("pendiente_desarrollo", "excluido_estructural"):
            automaticos.append(ev)
        else:
            necesita_ia.append(ev)
    return necesita_ia, automaticos


def resumen_automatico_grupo(familia, eventos_automaticos):
    """Genera, SIN usar IA, el resumen de descarte para eventos ya
    determinísticos (categoría 2) — ahorra el 100% de los tokens de esos
    eventos porque el resultado no depende de ninguna búsqueda."""
    if not eventos_automaticos:
        return None
    lineas = [
        f"**{familia.upper()}** — {len(eventos_automaticos)} evento(s), "
        f"0 tokens de IA usados (descarte automático, categoría 2):"
    ]
    for ev in eventos_automaticos:
        cobertura = ev.get("_registry_modelo_secundario", {}).get("cobertura")
        lineas.append(f"- {ev.get('partido')} ({ev.get('deporte')}) — {cobertura}")
    return "\n".join(lineas)


def construir_prompt_grupo(familia, eventos_grupo, seleccion_label, hora_rd, seccion_movimiento):
    """Arma un prompt scoped a UNA familia de deporte, reutilizando el mismo
    SYSTEM_PROMPT_BLINDADO_V3_5 (reglas idénticas), pero con el JSON limitado
    a los eventos de esa familia que sí necesitan verificación de IA."""
    return (
        f"{SYSTEM_PROMPT_BLINDADO_V3_5}\n\n"
        f"==================================================\n"
        f"CONTEXTO DE EJECUCIÓN DEL BACKEND (MODO POR DEPORTE)\n"
        f"==================================================\n"
        f"ÁMBITO GENERAL DE LA CORRIDA: {seleccion_label}\n"
        f"GRUPO ANALIZADO EN ESTE PROMPT: {familia.upper()} "
        f"({len(eventos_grupo)} evento(s))\n"
        f"HORA CONSULTA (RD/UTC-4): {hora_rd}\n\n"
        f"{seccion_movimiento}\n\n"
        f"NOTA IMPORTANTE: Este prompt contiene ÚNICAMENTE los eventos de la "
        f"familia '{familia}' que ya pasaron el pre-filtrado de frescura y "
        f"liquidez y tienen cobertura 'externa_directa', 'modelo_interno_elo' "
        f"o 'modelo_externo_backend'. Los eventos de esta misma familia con "
        f"cobertura 'pendiente_desarrollo' o 'excluido_estructural' YA fueron "
        f"descartados por el backend sin usar IA (ver resumen aparte) — no "
        f"vienen en este JSON, y por lo tanto NO deben aparecer en tu conteo "
        f"de categoría 2 de este prompt (ese conteo se reporta aparte, fuera "
        f"de la IA).\n\n"
        f"INSTRUCCIÓN TÉCNICA: Utiliza directamente los campos `_pinnacle_devig`, "
        f"`_pinnacle_last_update`, `_liquidez_backend`, `_dispersion_max_entre_casas`, "
        f"`_n_casas_reportando` y `_registry_modelo_secundario`. No recalcules el "
        f"de-vig ni filtres por rango nuevamente. Para eventos con cobertura "
        f"'modelo_externo_backend', usa directamente `probabilidad_home` / "
        f"`probabilidad_draw` / `probabilidad_away` sin buscar en la web.\n\n"
        f"DATOS JSON PRE-FILTRADOS Y ENRIQUECIDOS (solo familia '{familia}'):\n"
        f"{json.dumps(eventos_grupo, indent=2, ensure_ascii=False)}"
    )


def construir_prompts_por_deporte(eventos_filtrados, seleccion_label, hora_rd, seccion_movimiento):
    """Devuelve (prompts_por_grupo: dict[str, str], resumen_automatico: str).
    prompts_por_grupo solo incluye familias con al menos 1 evento que
    necesita IA — las familias 100% automáticas no generan prompt."""
    grupos = agrupar_eventos_por_familia(eventos_filtrados)
    prompts_por_grupo = {}
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

    resumen_automatico_total = (
        "\n\n".join(resúmenes_automaticos)
        if resúmenes_automaticos
        else "Ningún evento cayó en descarte 100% automático en esta corrida."
    )
    return prompts_por_grupo, resumen_automatico_total


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


def llamar_claude_rest(anthropic_api_key, modelo, prompt_texto, max_tokens=8000):
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
st.title("📊 Analista de Apuesta Única v3.5 (Multi-IA, Multi-Deporte, Motor Elo Interno, ClubElo Directo & Modo por Deporte)")

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

    # --- 5. Sidebar de consumo de APIs (con indicadores de color y alertas) ---
    st.divider()
    st.subheader("📊 Consumo de APIs")

    # The Odds API: sí tiene un "restante" real vía headers.
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
                st.warning("⚠️ Quedan pocas requests en The Odds API. Considera rotar la key.")
        except (TypeError, ValueError, KeyError):
            st.caption("The Odds API: header de consumo no legible")
    else:
        st.caption("The Odds API: sin llamadas registradas aún")

    # Claude y Gemini: NO exponen cuota real restante vía API — solo tokens
    # acumulados en esta sesión del navegador. El saldo/crédito real
    # siempre hay que revisarlo en el dashboard de facturación de cada
    # proveedor (console.anthropic.com / Google AI Studio).
    if "claude_tokens_acumulados" in st.session_state:
        c = st.session_state["claude_tokens_acumulados"]
        st.metric("Claude — tokens entrada (sesión)", f"{c['entrada']:,}")
        st.metric("Claude — tokens salida (sesión)", f"{c['salida']:,}")
        if c["total"] >= CLAUDE_TOKEN_WARNING:
            st.warning(
                f"⚠️ Consumo de Claude en esta sesión superó {CLAUDE_TOKEN_WARNING:,} tokens. "
                "Revisa tu dashboard de facturación de Anthropic."
            )
        if "claude_ratelimit" in st.session_state:
            rl = st.session_state["claude_ratelimit"]
            st.caption(
                f"⏱️ Ventana de rate-limit — tokens restantes: {rl['tokens_restantes']}/"
                f"{rl['tokens_limite']} · requests restantes: {rl['requests_restantes']}"
            )

    if "gemini_tokens_acumulados" in st.session_state:
        g = st.session_state["gemini_tokens_acumulados"]
        st.metric("Gemini — tokens totales (sesión)", f"{g['total']:,}")
        st.caption(f"Entrada: {g['prompt']:,} · Salida: {g['salida']:,}")
        if g["total"] >= GEMINI_TOKEN_WARNING:
            st.warning(
                f"⚠️ Consumo de Gemini en esta sesión superó {GEMINI_TOKEN_WARNING:,} tokens. "
                "Revisa tu dashboard de facturación de Google."
            )

    if "claude_tokens_acumulados" in st.session_state or "gemini_tokens_acumulados" in st.session_state:
        st.caption(
            "Nota: Claude y Gemini no exponen cuota 'restante' real vía API. "
            "Estos números son el consumo acumulado de esta sesión, no tu "
            "límite total del plan."
        )

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

        modo_ejecucion = st.radio(
            "Modo de análisis:",
            ["Todo en un solo prompt (rápido, menos preciso)", "Separado por deporte (más lento, cobertura completa)"],
            help=(
                "'Todo en un prompt' es más barato pero, con muchos eventos, la IA puede quedarse sin "
                "presupuesto de búsqueda antes de verificar todos — algunos con valor real pueden quedar "
                "sin analizar. 'Separado por deporte' cuesta más tokens en total pero garantiza que cada "
                "evento reciba una búsqueda real, y no gasta tokens de IA en eventos ya descartables "
                "automáticamente (CFL, AFL, NRL, KBO, NPB sin historial, etc.) ni en eventos de fútbol "
                "que el backend ya resolvió directo vía ClubElo/Forebet."
            ),
        )

        if st.button("🚀 Generar Prompt y Procesar Datos", type="primary"):
            with st.spinner("Consultando The Odds API, ClubElo, actualizando motor Elo y procesando pre-filtros..."):
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
                elif modo_ejecucion.startswith("Todo en un solo prompt"):
                    st.session_state.pop("prompts_por_grupo", None)
                    st.session_state.pop("resumen_automatico_grupo", None)
                    prompt_completo = (
                        f"{SYSTEM_PROMPT_BLINDADO_V3_5}\n\n"
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
                        f"de-vig ni filtres por rango nuevamente. Para eventos con cobertura "
                        f"'modelo_externo_backend', usa directamente `probabilidad_home` / "
                        f"`probabilidad_draw` / `probabilidad_away` sin buscar en la web.\n\n"
                        f"DATOS JSON PRE-FILTRADOS Y ENRIQUECIDOS:\n"
                        f"{json.dumps(eventos_filtrados, indent=2, ensure_ascii=False)}"
                    )
                    st.session_state["prompt_generado"] = prompt_completo
                    st.success(f"✅ Se consolidaron {len(eventos_filtrados)} eventos aptos para el prompt.")
                else:
                    st.session_state.pop("prompt_generado", None)
                    prompts_por_grupo, resumen_auto = construir_prompts_por_deporte(
                        eventos_filtrados, seleccion, hora_rd, seccion_movimiento
                    )
                    st.session_state["prompts_por_grupo"] = prompts_por_grupo
                    st.session_state["resumen_automatico_grupo"] = resumen_auto
                    n_grupos = len(prompts_por_grupo)
                    n_eventos_ia = sum(
                        p.count('"partido"') for p in prompts_por_grupo.values()
                    )
                    st.success(
                        f"✅ Se armaron {n_grupos} prompt(s) por familia de deporte "
                        f"({n_eventos_ia} eventos requieren IA). Los descartes 100% "
                        f"automáticos se resolvieron sin gastar tokens de IA — revísalos abajo."
                    )
                    if resumen_auto:
                        with st.expander("📋 Descartes automáticos (0 tokens de IA)"):
                            st.markdown(resumen_auto)

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
                "así que Claude SÍ puede buscar en FanGraphs/TennisAbstract/etc. aunque "
                "el toggle de búsqueda en claude.ai estuviera apagado. Para fútbol, la "
                "mayoría de los eventos ya vienen resueltos por el backend (ClubElo/Forebet) "
                "y no requieren que Claude busque nada."
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
                                anthropic_api_key, modelo_claude_elegido, st.session_state["prompt_generado"],
                                max_tokens=8000,
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
                                    "revisa el resultado: puede ser porque todos los eventos venían "
                                    "resueltos directo por el backend (Elo interno/ClubElo/Forebet), "
                                    "o porque descartó todo por falta de datos verificables."
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

    # ==========================================================================
    # MODO "POR DEPORTE" — un prompt (y opcionalmente una llamada a Claude)
    # por cada familia de deporte que sí necesita verificación de IA.
    # ==========================================================================
    if "prompts_por_grupo" in st.session_state and st.session_state["prompts_por_grupo"]:
        st.divider()
        st.subheader("🧩 Modo por deporte — prompts individuales")
        prompts_por_grupo = st.session_state["prompts_por_grupo"]

        for familia, prompt_texto in prompts_por_grupo.items():
            with st.expander(f"📋 Prompt — {familia.upper()}"):
                st.code(prompt_texto, language="markdown")

        if anthropic_api_key:
            st.divider()
            st.subheader("⚡ Ejecutar TODOS los grupos con Claude API")
            st.caption(
                "Corre una llamada por cada familia de deporte (cada una con su propia "
                "búsqueda web forzada) y acumula el resultado y el uso de tokens de todas."
            )
            modelos_claude_grp = listar_modelos_claude(anthropic_api_key)
            if modelos_claude_grp:
                modelo_default_grp = next(
                    (m for m in modelos_claude_grp if "sonnet" in m.lower()), modelos_claude_grp[0]
                )
                modelo_grp_elegido = st.selectbox(
                    "Modelo Claude para el modo por deporte:",
                    modelos_claude_grp,
                    index=modelos_claude_grp.index(modelo_default_grp),
                    key="modelo_por_deporte",
                )
                if st.button("🤖 Analizar TODOS los grupos", type="primary", key="btn_todos_grupos"):
                    total_entrada, total_salida = 0, 0
                    for familia, prompt_texto in prompts_por_grupo.items():
                        with st.spinner(f"Analizando {familia.upper()} (con búsqueda web activa)..."):
                            try:
                                resultado, queries, uso_tokens, _ = llamar_claude_rest(
                                    anthropic_api_key, modelo_grp_elegido, prompt_texto, max_tokens=8000,
                                )
                                st.markdown(f"### 🏆 Resultado — {familia.upper()}")
                                st.markdown(resultado)
                                if queries:
                                    with st.expander(f"🔍 Búsquedas realizadas en {familia.upper()} ({len(queries)})"):
                                        for q in queries:
                                            st.write(f"- {q}")
                                entrada_tok = uso_tokens.get("input_tokens", 0) or 0
                                salida_tok = uso_tokens.get("output_tokens", 0) or 0
                                total_entrada += entrada_tok
                                total_salida += salida_tok
                                st.caption(
                                    f"{familia.upper()}: {entrada_tok:,} tokens de entrada · "
                                    f"{salida_tok:,} de salida."
                                )

                                prev = st.session_state.get(
                                    "claude_tokens_acumulados", {"entrada": 0, "salida": 0, "total": 0}
                                )
                                prev["entrada"] += entrada_tok
                                prev["salida"] += salida_tok
                                prev["total"] += entrada_tok + salida_tok
                                st.session_state["claude_tokens_acumulados"] = prev
                            except Exception as e:
                                st.error(f"Error al analizar {familia.upper()}: {e}")
                    st.divider()
                    st.success(
                        f"✅ Corrida por deporte completa — TOTAL: {total_entrada:,} tokens de "
                        f"entrada · {total_salida:,} de salida · {total_entrada + total_salida:,} tokens "
                        f"en total (sobre {len(prompts_por_grupo)} grupo(s))."
                    )
            else:
                st.warning("No se pudo obtener la lista de modelos. Verifica la API Key de Anthropic.")
