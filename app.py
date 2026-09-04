import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests
import streamlit as st

# soccerdata es OPCIONAL: si no está instalado (pip install soccerdata), el
# backend simplemente no intenta resolver ClubElo por su cuenta y soccer cae
# directo a fuente_primaria = elofootball.com (o ESPN Analytics para ligas no
# europeas) para que la IA lo busque (ver sección 1d más abajo).
try:
    import soccerdata as sd
    _SOCCERDATA_DISPONIBLE = True
except ImportError:
    _SOCCERDATA_DISPONIBLE = False

# ==============================================================================
# 1. SYSTEM PROMPT V3.5 — BLINDADO
#    Restaura y amplía las salvaguardas: fuentes por deporte, gate de frescura
#    relativo, Model Registry obligatorio, motor Elo interno calibrado como
#    segundo modelo válido, reglas anti-fabricación, formato de salida fijo,
#    tabla de transparencia de descartes (v3.3), y ahora (v3.4 + v3.5):
#      - Chequeo obligatorio de lesión/estado físico para tenis, boxeo y MMA.
#      - Fuente de respaldo documentada (campo `fuente_respaldo` en el
#        registry) para cuando la fuente primaria no expone un número público.
#      - Sección de salida "CASI CALIFICÓ" con los eventos más cercanos al
#        umbral que no pasaron.
#      - NUEVO v3.5: Gate de riesgo de empate para fútbol/soccer — obliga a
#        usar Draw No Bet (estimado matemáticamente desde el devig 1X2) o a
#        penalizar la Confianza cuando la probabilidad de empate de Pinnacle
#        es alta, para no perder picks con EV positivo por un empate.
# ==============================================================================
SYSTEM_PROMPT_BLINDADO_V3_2 = """
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
     - RESPALDO DOCUMENTADO (nuevo en v3.4): si ni `fuente_primaria` ni
       `fuente_secundaria` exponen un número públicamente accesible tras un
       intento real de búsqueda, revisa si el evento trae un campo
       `fuente_respaldo` en el registry. Si existe, puedes usarlo — pero
       cítalo EXPLÍCITAMENTE como respaldo, nunca como si fuera la fuente
       primaria. Ejemplo correcto: "Fuente: ESPN Analytics — Matchup
       Predictor (respaldo documentado; FanGraphs no expuso un número
       público tras la búsqueda)".
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

   - "pendiente_desarrollo": no hay fuente externa definida NI historial Elo
     interno suficiente todavía para ese equipo/deporte. DESCARTA
     automáticamente sin buscar en otro lado y sin usar un modelo "propio"
     improvisado — eso sería fabricación.

   - "excluido_estructural": ya debería venir excluido del JSON; si aparece,
     descarta sin análisis (partidos de exhibición/preseason).

   PROHIBIDO ABSOLUTO: usar cualquier fuente, rating o modelo que no aparezca
   literalmente en `_registry_modelo_secundario` de ese evento específico
   (ya sea en `fuente_primaria`, `fuente_secundaria`, o `fuente_respaldo`).

3b. CHEQUEO DE ESTADO FÍSICO — OBLIGATORIO PARA TENIS, BOXEO Y MMA (nuevo en v3.4):
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

3c. GATE DE RIESGO DE EMPATE — SOLO FÚTBOL/SOCCER (nuevo en v3.5):
   El mercado moneyline en fútbol es a 3 resultados: el empate es una fuga
   real de EV incluso en picks matemáticamente positivos. Para TODO evento
   cuyo `sport_key` empiece con "soccer", ANTES de confirmar un pick en
   moneyline, evalúa el campo `_prob_empate_pinnacle` (probabilidad de-vigged
   de empate según Pinnacle):

   - Si `_prob_empate_pinnacle >= 0.30` (30%): el evento NO puede recomendarse
     en moneyline puro. Si el evento pasó los umbrales de EV/divergencia,
     usa en su lugar el campo `_draw_no_bet_estimado` para ese lado:
       - Cuota a usar: `cuota_justa_dnb_estimada`.
       - Probabilidad del segundo modelo: renormalízala tú mismo excluyendo
         el empate, con la misma fórmula que usa el backend:
         P_dnb_modelo(lado) = P_modelo(lado) / (P_modelo(home) + P_modelo(away))
       - Recalcula el EV% con esa cuota y esa probabilidad renormalizada.
       - En el informe, el campo "Mercado" debe decir explícitamente
         "Draw No Bet (estimado desde 1X2, no es cuota real de mercado)" —
         nunca lo presentes como si fuera un moneyline normal.
   - Si `0.25 <= _prob_empate_pinnacle < 0.30`: puedes recomendar moneyline,
     pero DEBES bajar la Confianza en al menos 2 puntos de forma explícita,
     citando el % de empate como motivo (ej. "Confianza reducida por riesgo
     de empate: Pinnacle de-vigged asigna 27% a empate").
   - Si `_prob_empate_pinnacle < 0.25`: no se requiere ajuste por este motivo.

   Esta regla es ADICIONAL a los gates de EV/divergencia/confianza — no los
   reemplaza. Un evento puede pasar EV y divergencia y aun así requerir DNB
   en lugar de moneyline, o ver reducida su Confianza, por esta regla.

4. LIQUIDEZ: Usa el campo `_liquidez_backend` tal cual. No la reinterpretes.
   Un evento con menos de 2 casas reportando NO califica (liquidez insuficiente).

5. UMBRALES DE DESCARTE (ajustados v3.3 — ligeramente más permisivos que v3.2):
   - EV < 4% → descartar (antes 5%).
   - Divergencia |Pinnacle - Segundo Modelo| > 9% → descartar (antes 7%; señal
     de posible error de datos, no de "value").
   - Si el segundo modelo es "modelo_interno_elo" y `brier_score_historico` es
     peor que 0.23 o `muestras_brier` < 8, el backend ya lo habría excluido —
     pero si por alguna razón lo ves con esos valores, descarta igual.

6. CONFIANZA (1-10): Calcula con el siguiente desglose visible en el informe:
   - Edge estadístico (EV real vs. umbral)
   - Calidad/frescura de la fuente del segundo modelo (una fuente externa
     reciente pesa más que un modelo interno con pocas muestras; una fuente
     de respaldo documentada pesa menos que la fuente primaria oficial)
   - Liquidez del mercado
   - Coherencia entre movimiento de línea (si hay datos) y el pick
   - Para tenis/boxeo/MMA: resultado del chequeo de estado físico (regla 3b).
     Una noticia real de lesión/molestia no cuantificable en el rating debe
     bajar este componente de forma explícita.
   - Para fútbol: resultado del gate de riesgo de empate (regla 3c).
   Un pick solo califica si la confianza total es >= 8/10.

REGLAS ANTI-FABRICACIÓN (obligatorias, sin excepción):
- Nunca inventes lesiones, alineaciones, clima o noticias que no hayas confirmado
  con una fuente real y citada.
- Nunca inventes cuotas, nombres de equipos/jugadores o resultados históricos que
  no estén en el JSON de entrada o en una fuente web verificada.
- Si falta cualquier dato necesario para completar el análisis de un evento, ese
  evento se descarta — nunca se rellena el vacío con una suposición "razonable".
- Cada afirmación estadística debe llevar su fuente (nombre + URL, "Modelo Elo
  interno" con sus métricas si aplica, o la fuente de respaldo citada como tal).
- Si un evento se descartó en la categoría 1, 2 o 3 (frescura, pendiente_desarrollo
  /fuente inaccesible sin respaldo, o liquidez), NUNCA calcules ni inventes un EV%
  o divergencia% para él — en esos casos ni siquiera se llegó a evaluar el segundo
  modelo. Repórtalo como "N/A — no se calculó" en la tabla de la sección 4.
- El campo `fuente_respaldo` NUNCA se usa por comodidad o para ahorrar una
  búsqueda — solo se usa después de haber intentado realmente la fuente primaria
  y, si aplica, la secundaria, y haber confirmado que ninguna expone un número
  público.
- El campo `_draw_no_bet_estimado` es una ESTIMACIÓN matemática del backend
  (renormalización del devig 1X2 de Pinnacle sin el empate), no una cuota real
  ofrecida por ningún libro. Nunca la presentes como cuota de mercado — siempre
  aclara que es estimada.

CATEGORIZACIÓN DE DESCARTES — MUTUAMENTE EXCLUYENTE (v3.3, ajustada en v3.4):
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
      o de riesgo de empate no compensado con DNB, regla 3c)
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
   Para fútbol: el campo "Mercado" debe indicar "Moneyline" o "Draw No Bet
   (estimado)" según lo que haya determinado la regla 3c — nunca lo dejes
   ambiguo.
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
     "Confianza 6/10 — riesgo de empate 27% (regla 3c)").
5. CASI CALIFICÓ (nuevo en v3.4): de los eventos en categorías 4, 5 y 6 que SÍ
   tuvieron datos reales calculados (no los marcados "N/A — no se pudo
   verificar"), identifica los 1-3 que estuvieron más cerca de pasar TODOS los
   umbrales — por ejemplo, divergencia apenas sobre el 9%, EV apenas debajo del
   4%, o confianza a 1-2 puntos de 8/10. Preséntalos en una tabla corta,
   ordenada de más cerca a menos cerca del umbral:
   | Partido | Qué faltó | Qué tan cerca (número exacto vs. umbral) |
   Si ningún evento tiene datos reales suficientes para esta comparación,
   omite la tabla y dilo explícitamente: "No hay eventos con datos suficientes
   para evaluar cercanía al umbral en esta corrida."
"""

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
ANTHROPIC_API_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"

# ==============================================================================
# 1b. MODEL REGISTRY — fuentes autorizadas de segundo modelo, por deporte.
#
# CAMBIO (este parche): soccer ya NO es una única entrada genérica que le pide
# a la IA buscar en elofootball.com para CUALQUIER liga. elofootball cubre
# ~55 países europeos únicamente — para una liga sudamericana, MLS, etc., pedir
# esa fuente primaria no es "un intento que puede fallar", es estructuralmente
# imposible que tenga el dato. Eso gastaba búsquedas/tokens en vano y empujaba
# eventos a categoría 2 sin necesidad.
#
# Ahora:
#   - Ligas que SÍ cubre elofootball → fuente_primaria = elofootball.com,
#     fuente_respaldo = ESPN Analytics (Matchup Predictor), igual que MLB.
#   - Ligas conocidas que elofootball NO cubre (Sudamérica, MLS, Liga MX, etc.)
#     → fuente_primaria = ESPN Analytics directamente (sin intento fútil a
#     elofootball primero).
#   - Catch-all genérico "soccer" al FINAL de la lista (importa el orden: ver
#     _buscar_base_registry, que usa "in" sobre el sport_key y toma el primer
#     match) para cualquier liga no mapeada explícitamente todavía. Por
#     seguridad, este catch-all también apunta a ESPN Analytics como primaria
#     — es la fuente con mejor cobertura global, más razonable como default
#     para una liga que no identificamos de antemano que sea europea.
# ==============================================================================
REGISTRY_ULTIMA_REVISION = "2026-08-27"

MODEL_REGISTRY = [
    {"patron": "americanfootball_nfl_preseason", "fuente_primaria": None, "fuente_secundaria": None,
     "cobertura": "excluido_estructural", "version": "1.0", "usa_elo_interno": False},

    # --- SOCCER: ligas europeas cubiertas por elofootball.com -------------
    # (patrones de sport_key típicos de The Odds API; ajusta/agrega según los
    # que realmente veas en tu cuenta con obtener_deportes_activos)
    {"patron": "soccer_epl", "fuente_primaria": "elofootball.com", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)",
     "cobertura": "externa_directa", "version": "1.2", "usa_elo_interno": False},
    {"patron": "soccer_efl_champ", "fuente_primaria": "elofootball.com", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)",
     "cobertura": "externa_directa", "version": "1.2", "usa_elo_interno": False},
    {"patron": "soccer_germany_bundesliga", "fuente_primaria": "elofootball.com", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)",
     "cobertura": "externa_directa", "version": "1.2", "usa_elo_interno": False},
    {"patron": "soccer_germany_bundesliga2", "fuente_primaria": "elofootball.com", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)",
     "cobertura": "externa_directa", "version": "1.2", "usa_elo_interno": False},
    {"patron": "soccer_italy_serie_a", "fuente_primaria": "elofootball.com", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)",
     "cobertura": "externa_directa", "version": "1.2", "usa_elo_interno": False},
    {"patron": "soccer_italy_serie_b", "fuente_primaria": "elofootball.com", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)",
     "cobertura": "externa_directa", "version": "1.2", "usa_elo_interno": False},
    {"patron": "soccer_spain_la_liga", "fuente_primaria": "elofootball.com", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)",
     "cobertura": "externa_directa", "version": "1.2", "usa_elo_interno": False},
    {"patron": "soccer_spain_segunda_division", "fuente_primaria": "elofootball.com", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)",
     "cobertura": "externa_directa", "version": "1.2", "usa_elo_interno": False},
    {"patron": "soccer_france_ligue_one", "fuente_primaria": "elofootball.com", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)",
     "cobertura": "externa_directa", "version": "1.2", "usa_elo_interno": False},
    {"patron": "soccer_france_ligue_two", "fuente_primaria": "elofootball.com", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)",
     "cobertura": "externa_directa", "version": "1.2", "usa_elo_interno": False},
    {"patron": "soccer_netherlands_eredivisie", "fuente_primaria": "elofootball.com", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)",
     "cobertura": "externa_directa", "version": "1.2", "usa_elo_interno": False},
    {"patron": "soccer_portugal_primeira_liga", "fuente_primaria": "elofootball.com", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)",
     "cobertura": "externa_directa", "version": "1.2", "usa_elo_interno": False},
    {"patron": "soccer_belgium_first_div", "fuente_primaria": "elofootball.com", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)",
     "cobertura": "externa_directa", "version": "1.2", "usa_elo_interno": False},
    {"patron": "soccer_turkey_super_league", "fuente_primaria": "elofootball.com", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)",
     "cobertura": "externa_directa", "version": "1.2", "usa_elo_interno": False},
    {"patron": "soccer_switzerland_superleague", "fuente_primaria": "elofootball.com", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)",
     "cobertura": "externa_directa", "version": "1.2", "usa_elo_interno": False},
    {"patron": "soccer_austria_bundesliga", "fuente_primaria": "elofootball.com", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)",
     "cobertura": "externa_directa", "version": "1.2", "usa_elo_interno": False},
    {"patron": "soccer_greece_super_league", "fuente_primaria": "elofootball.com", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)",
     "cobertura": "externa_directa", "version": "1.2", "usa_elo_interno": False},
    {"patron": "soccer_denmark_superliga", "fuente_primaria": "elofootball.com", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)",
     "cobertura": "externa_directa", "version": "1.2", "usa_elo_interno": False},
    {"patron": "soccer_sweden_allsvenskan", "fuente_primaria": "elofootball.com", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)",
     "cobertura": "externa_directa", "version": "1.2", "usa_elo_interno": False},
    {"patron": "soccer_uefa_champs_league", "fuente_primaria": "elofootball.com", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)",
     "cobertura": "externa_directa", "version": "1.2", "usa_elo_interno": False},
    {"patron": "soccer_uefa_europa_league", "fuente_primaria": "elofootball.com", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)",
     "cobertura": "externa_directa", "version": "1.2", "usa_elo_interno": False},
    {"patron": "soccer_uefa_europa_conference_league", "fuente_primaria": "elofootball.com",
     "fuente_secundaria": None, "fuente_respaldo": "ESPN Analytics (Matchup Predictor)",
     "cobertura": "externa_directa", "version": "1.2", "usa_elo_interno": False},

    # --- SOCCER: ligas NO cubiertas por elofootball.com --------------------
    # (Sudamérica, Norteamérica no-europea, etc.) → directo a ESPN Analytics
    # como fuente_primaria, sin pasar por un intento fútil en elofootball.
    {"patron": "soccer_brazil_campeonato", "fuente_primaria": "ESPN Analytics (Matchup Predictor)",
     "fuente_secundaria": None, "cobertura": "externa_directa", "version": "1.0", "usa_elo_interno": False},
    {"patron": "soccer_brazil_serie_b", "fuente_primaria": "ESPN Analytics (Matchup Predictor)",
     "fuente_secundaria": None, "cobertura": "externa_directa", "version": "1.0", "usa_elo_interno": False},
    {"patron": "soccer_argentina_primera_division", "fuente_primaria": "ESPN Analytics (Matchup Predictor)",
     "fuente_secundaria": None, "cobertura": "externa_directa", "version": "1.0", "usa_elo_interno": False},
    {"patron": "soccer_mexico_ligamx", "fuente_primaria": "ESPN Analytics (Matchup Predictor)",
     "fuente_secundaria": None, "cobertura": "externa_directa", "version": "1.0", "usa_elo_interno": False},
    {"patron": "soccer_usa_mls", "fuente_primaria": "ESPN Analytics (Matchup Predictor)",
     "fuente_secundaria": None, "cobertura": "externa_directa", "version": "1.0", "usa_elo_interno": False},
    {"patron": "soccer_conmebol_copa_libertadores", "fuente_primaria": "ESPN Analytics (Matchup Predictor)",
     "fuente_secundaria": None, "cobertura": "externa_directa", "version": "1.0", "usa_elo_interno": False},
    {"patron": "soccer_conmebol_copa_sudamericana", "fuente_primaria": "ESPN Analytics (Matchup Predictor)",
     "fuente_secundaria": None, "cobertura": "externa_directa", "version": "1.0", "usa_elo_interno": False},
    {"patron": "soccer_chile_campeonato", "fuente_primaria": "ESPN Analytics (Matchup Predictor)",
     "fuente_secundaria": None, "cobertura": "externa_directa", "version": "1.0", "usa_elo_interno": False},
    {"patron": "soccer_colombia_primera_a", "fuente_primaria": "ESPN Analytics (Matchup Predictor)",
     "fuente_secundaria": None, "cobertura": "externa_directa", "version": "1.0", "usa_elo_interno": False},

    # --- SOCCER: catch-all genérico — DEBE IR AL FINAL de todos los "soccer_*"
    # de arriba, porque _buscar_base_registry hace matching por substring
    # ("patron" in sport_key_low) y toma el primer match de la lista. Si este
    # catch-all quedara antes, interceptaría a TODAS las ligas específicas.
    # Por defecto apunta a ESPN Analytics (mejor cobertura global) en vez de
    # elofootball, para no asumir de entrada que una liga no mapeada es
    # europea.
    {"patron": "soccer", "fuente_primaria": "ESPN Analytics (Matchup Predictor)", "fuente_secundaria": None,
     "cobertura": "externa_directa", "version": "1.3", "usa_elo_interno": False},

    {"patron": "tennis", "fuente_primaria": "TennisAbstract (Elo por superficie)",
     "fuente_secundaria": "Ranking oficial ATP/WTA", "cobertura": "externa_directa", "version": "1.0",
     "usa_elo_interno": False},
    {"patron": "baseball_mlb", "fuente_primaria": "FanGraphs", "fuente_secundaria": None,
     "fuente_respaldo": "ESPN Analytics (Matchup Predictor)",
     "cobertura": "externa_directa", "version": "1.0", "usa_elo_interno": False},
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
    hay suficiente calidad, lo reemplaza por la entrada calculada.

    Para soccer, ANTES de mirar el registry estático se intenta ClubElo vía
    soccerdata (resuelto 100% por backend, sin gastar búsqueda de IA). Si eso
    falla o no está disponible, se cae al registry estático — que ahora, tras
    este parche, ya distingue entre ligas europeas (elofootball + respaldo
    ESPN) y ligas no europeas (ESPN directo), en vez de mandar todo a
    elofootball.com sin importar la liga."""
    base = _buscar_base_registry(sport_key)

    if sport_key and sport_key.lower().startswith("soccer") and home_team and away_team:
        resuelto_por_backend = obtener_entrada_clubelo_backend(home_team, away_team)
        if resuelto_por_backend:
            return resuelto_por_backend

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
# 1d. MOTOR CLUBELO POR BACKEND (soccerdata)
# ==============================================================================

CLUBELO_VENTAJA_LOCAL = 60.0
CLUBELO_CACHE_TTL_SEGUNDOS = 6 * 3600
CLUBELO_JACCARD_MINIMO = 0.5


@st.cache_data(ttl=CLUBELO_CACHE_TTL_SEGUNDOS, show_spinner=False)
def _descargar_ranking_clubelo(fecha_iso):
    if not _SOCCERDATA_DISPONIBLE:
        return None
    try:
        elo = sd.ClubElo()
        return elo.read_by_date(fecha_iso)
    except Exception:
        return None


def _buscar_elo_equipo(df_ranking, nombre_equipo):
    if df_ranking is None or getattr(df_ranking, "empty", True) or not nombre_equipo:
        return None

    columnas = list(df_ranking.columns)
    columna_club = "team" if "team" in columnas else "Club" if "Club" in columnas else columnas[0]
    columna_elo = "elo" if "elo" in columnas else "Elo" if "Elo" in columnas else None
    if columna_elo is None:
        return None

    exacto = df_ranking[df_ranking[columna_club].astype(str).str.lower() == nombre_equipo.lower()]
    if not exacto.empty:
        return float(exacto.iloc[0][columna_elo])

    tokens_buscado = set(nombre_equipo.lower().split())
    mejor_score, mejor_elo = 0.0, None
    for _, fila in df_ranking.iterrows():
        tokens_candidato = set(str(fila[columna_club]).lower().split())
        union = tokens_buscado | tokens_candidato
        if not union:
            continue
        score = len(tokens_buscado & tokens_candidato) / len(union)
        if score > mejor_score:
            mejor_score, mejor_elo = score, float(fila[columna_elo])

    return mejor_elo if mejor_score >= CLUBELO_JACCARD_MINIMO else None


def obtener_entrada_clubelo_backend(home_team, away_team):
    if not _SOCCERDATA_DISPONIBLE or not home_team or not away_team:
        return None

    fecha_hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    df_ranking = _descargar_ranking_clubelo(fecha_hoy)
    if df_ranking is None:
        return None

    elo_home = _buscar_elo_equipo(df_ranking, home_team)
    elo_away = _buscar_elo_equipo(df_ranking, away_team)
    if elo_home is None or elo_away is None:
        return None

    prob_home = _prob_elo(elo_home + CLUBELO_VENTAJA_LOCAL, elo_away)

    return {
        "fuente_primaria": (
            "ClubElo (resuelto por el backend vía soccerdata/api.clubelo.com "
            "— sin búsqueda de IA)"
        ),
        "fuente_secundaria": None,
        "cobertura": "modelo_interno_elo",
        "version": "1.0",
        "ultima_revision": REGISTRY_ULTIMA_REVISION,
        "probabilidad_elo_home": round(prob_home, 4),
        "elo_home": round(elo_home, 1),
        "elo_away": round(elo_away, 1),
        "nota": (
            f"Elo tomado directamente de ClubElo (fecha {fecha_hoy}), sin "
            f"muestras/Brier propio — es el rating oficial de la fuente, no "
            f"un modelo entrenado por Blindado."
        ),
    }


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


# ------------------------------------------------------------------------------
# DIAGNÓSTICO DE LIQUIDEZ
#
# POR QUÉ EXISTE: en una corrida reciente, los 28 eventos evaluados en TODAS
# las familias de deporte (tenis, soccer, cricket, WNBA, MLB) mostraron
# exactamente `_n_casas_reportando: 1` — es decir, solo Pinnacle. Que el 100%
# de eventos en 5 deportes tan distintos (incluyendo MLB, que normalmente es
# de los mercados más líquidos que existen) tengan CERO cobertura de stake,
# betonlineag y bet365 es sospechoso de un problema de fetch/plan/región, no
# de un patrón real de mercado.
#
# Esta función NO decide nada por sí sola ni cambia el comportamiento de
# filtrado — solo registra qué bookmakers vinieron realmente en la respuesta
# cruda de la API para cada evento, para poder diagnosticarlo con datos reales
# en vez de adivinar. Se muestra en un expander de la barra lateral.
#
# NOTA: esta instrumentación ya es GENÉRICA — corre sobre `datos_acumulados`
# en la sección 4 sin importar qué deporte se haya seleccionado (soccer
# incluido). No hace falta duplicarla por deporte.
# ------------------------------------------------------------------------------

def _extraer_bookmaker_keys(evento):
    if not isinstance(evento, dict):
        return []
    return [b.get("key") for b in evento.get("bookmakers", []) if isinstance(b, dict) and b.get("key")]


# Regiones soportadas por The Odds API. Se incluye "eu" siempre que se use
# modo "regions" porque Pinnacle (ancla obligatoria del sistema, regla 1)
# reporta bajo esa región — si se omite, los eventos empiezan a caer en
# "descartados_sin_pinnacle" en vez de en el gate de liquidez.
ODDS_API_REGIONS_COBERTURA_AMPLIA = "eu,us,us2,uk,au"
ODDS_API_BOOKMAKERS_FIJOS = "pinnacle,stake,betonlineag,bet365"


@st.cache_data(ttl=90, show_spinner=False)
def obtener_cuotas_api(api_key, sport_key, modo_cobertura="bookmakers_fijos"):
    """
    Consulta cuotas para un deporte específico.

    modo_cobertura:
      - "bookmakers_fijos" (default, más barato en cuota): pide únicamente los
        4 bookmakers listados en ODDS_API_BOOKMAKERS_FIJOS. NOTA: no se envía
        'regions' junto con 'bookmakers' porque The Odds API ignora 'regions'
        cuando 'bookmakers' está presente. Si tu plan de The Odds API no
        incluye alguno de esos 4 (ver expander "🔍 Diagnóstico de liquidez"),
        ese bookmaker simplemente nunca aparecerá, silenciosamente.
      - "regiones_amplias" (más cobertura, MÁS COSTOSO EN CUOTA): pide
        'regions' en vez de 'bookmakers' fijos, devolviendo TODOS los
        bookmakers disponibles en tu plan para esas regiones. The Odds API
        cobra más cuota por request mientras más regiones se piden — revisa
        tu consumo en la sidebar ("📉 Odds API — usados/restantes") después
        de probar este modo.

    Cacheado 90s para no quemar cuota si el usuario da clic varias veces
    seguidas. OJO: el cache_data de Streamlit indexa por TODOS los argumentos,
    así que cambiar modo_cobertura entre corridas SÍ dispara una llamada nueva
    (no reutiliza el caché del otro modo).

    NOTA DE DIAGNÓSTICO: si notas que `_n_casas_reportando` sale bajo en el
    informe final, revisa el expander "🔍 Diagnóstico de liquidez" en la
    barra lateral tras ejecutar una corrida — ahí se ve, evento por evento, la
    lista CRUDA de bookmaker keys que realmente devolvió la API antes de
    cualquier filtrado.
    """
    url = f"{ODDS_API_BASE}/sports/{sport_key}/odds/"
    params = {"apiKey": api_key, "markets": "h2h"}
    if modo_cobertura == "regiones_amplias":
        params["regions"] = ODDS_API_REGIONS_COBERTURA_AMPLIA
    else:
        params["bookmakers"] = ODDS_API_BOOKMAKERS_FIJOS
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


def calcular_draw_no_bet(pinnacle_devig, home_name, away_name):
    """
    Calcula la cuota JUSTA de Draw No Bet (DNB) a partir de las probabilidades
    de-vigged de Pinnacle en el mercado 1X2 (ya viene calculado en
    `_pinnacle_devig`, no se pide nada nuevo a la API).

    QUÉ RESUELVE: en el mercado moneyline de fútbol, un pick con EV positivo
    puede perderse igual si el partido termina empatado — el empate es una
    fuga de EV real que el moneyline puro no protege. DNB es un mercado donde,
    si hay empate, se devuelve el stake (push): elimina esa fuga.

    FÓRMULA: al quitar el empate del universo de resultados, las probabilidades
    de home/away se renormalizan entre sí:
        P_dnb(home) = P(home) / (P(home) + P(away))
        P_dnb(away) = P(away) / (P(home) + P(away))
    y la cuota justa es 1 / P_dnb. Esto es una ESTIMACIÓN matemática desde el
    1X2 — no es la cuota real de un libro en su mercado DNB (que puede diferir
    levemente por el margen propio de ese mercado). Se etiqueta como tal.
    """
    p_home = pinnacle_devig.get(home_name)
    p_away = pinnacle_devig.get(away_name)
    if p_home is None or p_away is None or (p_home + p_away) <= 0:
        return None

    p_dnb_home = p_home / (p_home + p_away)
    p_dnb_away = p_away / (p_home + p_away)

    return {
        "nota": (
            "Cuota justa DNB ESTIMADA matemáticamente desde el devig 1X2 de "
            "Pinnacle (renormalizando sin el empate) — no es una cuota de "
            "mercado real, es un techo teórico. Úsala solo como referencia "
            "de rango, no la cites como si fuera cuota ofrecida por un libro."
        ),
        home_name: {
            "prob_dnb": round(p_dnb_home, 4),
            "cuota_justa_dnb_estimada": round(1 / p_dnb_home, 3) if p_dnb_home > 0 else None,
        },
        away_name: {
            "prob_dnb": round(p_dnb_away, 4),
            "cuota_justa_dnb_estimada": round(1 / p_dnb_away, 3) if p_dnb_away > 0 else None,
        },
    }


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

        # --- Riesgo de empate (solo fútbol, mercados a 3 resultados) -------
        # Se calcula acá, en el backend, para que la IA no tenga que inventar
        # o recalcular el devig de DNB por su cuenta — reduce fabricación y
        # gasta 0 tokens de búsqueda. Ver regla 3c del prompt.
        es_soccer = bool(evento.get("sport_key", "").lower().startswith("soccer"))
        prob_empate_pinnacle = pinnacle_devig.get("Draw") if es_soccer else None
        draw_no_bet_estimado = (
            calcular_draw_no_bet(pinnacle_devig, home_team, away_team)
            if es_soccer and prob_empate_pinnacle is not None
            else None
        )

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

        if es_soccer and prob_empate_pinnacle is not None:
            evento_minificado["_prob_empate_pinnacle"] = prob_empate_pinnacle
            evento_minificado["_draw_no_bet_estimado"] = draw_no_bet_estimado

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
# 2b. MODO "POR DEPORTE" (v3.5)
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
        f"0 tokens de IA usados (descarte automático, categoría 2):"
    ]
    for ev in eventos_automaticos:
        cobertura = ev.get("_registry_modelo_secundario", {}).get("cobertura")
        lineas.append(f"- {ev.get('partido')} ({ev.get('deporte')}) — {cobertura}")
    return "\n".join(lineas)


def construir_prompt_grupo(familia, eventos_grupo, seleccion_label, hora_rd, seccion_movimiento):
    return (
        f"{SYSTEM_PROMPT_BLINDADO_V3_2}\n\n"
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
        f"liquidez y tienen cobertura 'externa_directa' o 'modelo_interno_elo'. "
        f"Los eventos de esta misma familia con cobertura 'pendiente_desarrollo' "
        f"o 'excluido_estructural' YA fueron descartados por el backend sin "
        f"usar IA (ver resumen aparte) — no vienen en este JSON, y por lo "
        f"tanto NO deben aparecer en tu conteo de categoría 2 de este prompt "
        f"(ese conteo se reporta aparte, fuera de la IA).\n\n"
        f"INSTRUCCIÓN TÉCNICA: Utiliza directamente los campos `_pinnacle_devig`, "
        f"`_pinnacle_last_update`, `_liquidez_backend`, `_dispersion_max_entre_casas`, "
        f"`_n_casas_reportando`, `_registry_modelo_secundario` y, para fútbol, "
        f"`_prob_empate_pinnacle` / `_draw_no_bet_estimado` (regla 3c). No "
        f"recalcules el de-vig ni filtres por rango nuevamente.\n\n"
        f"DATOS JSON PRE-FILTRADOS Y ENRIQUECIDOS (solo familia '{familia}'):\n"
        f"{json.dumps(eventos_grupo, indent=2, ensure_ascii=False)}"
    )


def construir_prompts_por_deporte(eventos_filtrados, seleccion_label, hora_rd, seccion_movimiento):
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
st.title("📊 Analista de Apuesta Única v3.5 (Multi-IA, Multi-Deporte, Motor Elo Interno, Modo por Deporte & Gate de Empate)")

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
    modo_cobertura_label = st.radio(
        "Cobertura de bookmakers (The Odds API):",
        [
            "Bookmakers fijos (barato en cuota)",
            "Regiones amplias (más cobertura, MÁS cuota)",
        ],
        help=(
            "'Bookmakers fijos' pide únicamente pinnacle/stake/betonlineag/bet365 — "
            "si tu plan no incluye alguno de esos, simplemente no aparece, sin aviso "
            "de la API. 'Regiones amplias' pide todos los bookmakers disponibles en "
            "tu plan para eu+us+us2+uk+au — trae más cobertura real, pero The Odds "
            "API cobra más cuota por request mientras más regiones se piden. Revisa "
            "'📉 Odds API — usados/restantes' abajo después de probarlo."
        ),
    )
    modo_cobertura = (
        "regiones_amplias" if modo_cobertura_label.startswith("Regiones") else "bookmakers_fijos"
    )

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

        modo_ejecucion = st.radio(
            "Modo de análisis:",
            ["Todo en un solo prompt (rápido, menos preciso)", "Separado por deporte (más lento, cobertura completa)"],
            help=(
                "'Todo en un prompt' es más barato pero, con muchos eventos, la IA puede quedarse sin "
                "presupuesto de búsqueda antes de verificar todos — algunos con valor real pueden quedar "
                "sin analizar. 'Separado por deporte' cuesta más tokens en total pero garantiza que cada "
                "evento reciba una búsqueda real, y no gasta tokens de IA en eventos ya descartables "
                "automáticamente (CFL, AFL, NRL, KBO, NPB sin historial, etc.)."
            ),
        )

        if st.button("🚀 Generar Prompt y Procesar Datos", type="primary"):
            with st.spinner("Consultando The Odds API, actualizando motor Elo y procesando pre-filtros..."):
                datos_acumulados = []

                if deporte_key_seleccionado == "ALL":
                    progress_bar = st.progress(0)
                    total_deps = len(deportes_lista)
                    for idx, dep in enumerate(deportes_lista):
                        cuotas = obtener_cuotas_api(api_key, dep.get('key'), modo_cobertura=modo_cobertura)
                        if cuotas:
                            datos_acumulados.extend(cuotas)
                        progress_bar.progress((idx + 1) / total_deps)
                        time.sleep(0.15)  # evita ráfaga -> 429
                    progress_bar.empty()
                else:
                    datos_acumulados = obtener_cuotas_api(api_key, deporte_key_seleccionado, modo_cobertura=modo_cobertura)

                # --- Diagnóstico de liquidez: se construye AQUÍ, a partir de
                # datos_acumulados ya recibido, en vez de dentro de
                # obtener_cuotas_api(). Así se genera SIEMPRE, incluso cuando
                # obtener_cuotas_api sirvió la respuesta desde caché
                # (@st.cache_data no vuelve a ejecutar el cuerpo de la función
                # en un cache hit, así que cualquier efecto secundario ahí
                # dentro — como registrar en session_state — se pierde).
                # Genérico: cubre TODOS los deportes de la corrida, soccer
                # incluido, sin necesidad de código específico por deporte. ---
                diagnostico_bookmakers = []
                for ev in datos_acumulados:
                    if not isinstance(ev, dict):
                        continue
                    keys = _extraer_bookmaker_keys(ev)
                    diagnostico_bookmakers.append({
                        "sport_key": ev.get("sport_key"),
                        "partido": f"{ev.get('home_team')} vs {ev.get('away_team')}",
                        "bookmakers_presentes": keys,
                        "n_bookmakers": len(keys),
                    })
                st.session_state["diagnostico_bookmakers_crudo"] = diagnostico_bookmakers

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

                st.caption(f"Modo de cobertura usado en esta corrida: **{modo_cobertura_label}**")
                st.write("### 📌 Resumen de Filtrado Backend")
                st.info(resumen_filtro)

                # --- Aviso temprano de liquidez sospechosa, visible sin tener
                # que abrir el expander de diagnóstico ---
                if eventos_filtrados:
                    n_con_1_casa = sum(1 for ev in eventos_filtrados if ev.get("_n_casas_reportando", 0) < 2)
                    if n_con_1_casa == len(eventos_filtrados):
                        st.warning(
                            f"⚠️ Los {len(eventos_filtrados)} eventos candidatos tienen "
                            f"`_n_casas_reportando` < 2 (solo Pinnacle) — TODOS caerán en "
                            f"'liquidez insuficiente' antes de llegar a validación cruzada. "
                            f"Revisa el expander '🔍 Diagnóstico de liquidez' en la barra "
                            f"lateral para ver si esto es un problema de la llamada a la API "
                            f"o un hecho real de mercado."
                        )

                    n_soccer_riesgo_empate_alto = sum(
                        1 for ev in eventos_filtrados
                        if ev.get("_prob_empate_pinnacle") is not None
                        and ev["_prob_empate_pinnacle"] >= 0.30
                    )
                    if n_soccer_riesgo_empate_alto:
                        st.info(
                            f"⚽ {n_soccer_riesgo_empate_alto} evento(s) de fútbol tienen "
                            f"probabilidad de empate (Pinnacle de-vigged) ≥ 30% — el prompt "
                            f"instruye a la IA a usar Draw No Bet estimado en vez de "
                            f"moneyline para esos casos (regla 3c)."
                        )

                if not eventos_filtrados:
                    st.warning("⚠️ No se encontraron candidatos válidos en el rango 1.40 - 2.00 para los partidos de hoy.")
                elif modo_ejecucion.startswith("Todo en un solo prompt"):
                    st.session_state.pop("prompts_por_grupo", None)
                    st.session_state.pop("resumen_automatico_grupo", None)
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
                        f"`_n_casas_reportando`, `_registry_modelo_secundario` y, para fútbol, "
                        f"`_prob_empate_pinnacle` / `_draw_no_bet_estimado` (regla 3c). No "
                        f"recalcules el de-vig ni filtres por rango nuevamente.\n\n"
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

# ==============================================================================
# 5. DIAGNÓSTICO DE LIQUIDEZ — se renderiza AQUÍ, al final del script, a
#    propósito.
#
#    POR QUÉ AQUÍ Y NO ARRIBA EN LA SIDEBAR JUNTO A LO DEMÁS: Streamlit
#    ejecuta el archivo completo de arriba a abajo en cada interacción. El
#    diagnóstico se calcula DENTRO del bloque del botón "Generar Prompt y
#    Procesar Datos" (sección 4, más arriba). Si este expander se colocara
#    antes de ese bloque en el archivo — como en la sidebar original — se
#    dibujaría usando los datos de ANTES del clic, y el diagnóstico recién
#    calculado quedaría un paso atrás (por eso "desaparecía": en la misma
#    pasada del clic, se pintaba con la sesión vieja/vacía).
#
#    Al colocarlo aquí, después de todo el flujo del botón, Streamlit ya
#    ejecutó el cálculo y guardó `st.session_state["diagnostico_bookmakers_crudo"]`
#    ANTES de llegar a este punto — así que siempre muestra el resultado de
#    la corrida que se acaba de hacer, en la misma pasada, sin necesitar un
#    segundo clic ni un st.rerun().
#
#    `with st.sidebar:` se puede invocar varias veces en un mismo script —
#    cada vez que se usa, agrega contenido al final de lo que ya hay en la
#    barra lateral. No reemplaza lo anterior. Esto aplica a TODOS los
#    deportes de la corrida (soccer incluido), no solo MLB.
# ==============================================================================
with st.sidebar:
    if "diagnostico_bookmakers_crudo" in st.session_state and st.session_state["diagnostico_bookmakers_crudo"]:
        with st.expander("🔍 Diagnóstico de liquidez (bookmakers crudos por evento)", expanded=True):
            registro = st.session_state["diagnostico_bookmakers_crudo"]
            todas_las_keys_vistas = set()
            for r in registro:
                todas_las_keys_vistas.update(r["bookmakers_presentes"])

            st.write(
                f"**{len(registro)} eventos consultados** en la última corrida. "
                f"Bookmaker keys distintas vistas: "
                f"{sorted(todas_las_keys_vistas) if todas_las_keys_vistas else '— ninguna —'}"
            )
            if todas_las_keys_vistas == {"pinnacle"} or not todas_las_keys_vistas:
                st.warning(
                    "⚠️ Solo 'pinnacle' apareció en TODOS los eventos consultados. "
                    "Esto sugiere que 'stake', 'betonlineag' y/o 'bet365' no están "
                    "siendo devueltos por tu API key — revisa en "
                    "https://the-odds-api.com/account si tu plan actual incluye "
                    "esos bookmakers, y confirma que esos son los 'key' exactos que "
                    "usa la API (no el nombre visible del libro)."
                )
            st.divider()
            for r in registro[-50:]:  # limita a los últimos 50 para no saturar la UI
                st.write(
                    f"- `{r['sport_key']}` — {r['partido']}: "
                    f"{r['bookmakers_presentes'] if r['bookmakers_presentes'] else '(ninguno)'}"
                )
            if st.button("🗑️ Limpiar diagnóstico"):
                st.session_state["diagnostico_bookmakers_crudo"] = []
                st.rerun()
