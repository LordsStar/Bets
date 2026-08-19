import json
from datetime import datetime, timedelta, timezone
import requests
import streamlit as st

# ==============================================================================
# 1. DEFINICIÓN DEL SYSTEM PROMPT V3.0 (AJUSTADO PARA TRABAJAR CON EL BACKEND)
# ==============================================================================
SYSTEM_PROMPT_BLINDADO_V3 = """
PROMPT — Analista Cuantitativo de Apuesta Única (Blindado v3.0)

ROL Y OBJETIVO:
Actúa como Analista Cuantitativo de Deportes y Tipster Profesional. Tu objetivo es seleccionar una sola apuesta —la de mayor confianza estadística— dentro de un rango de cuota entre 1.40 y 2.00 (moneyline o mercado principal). Un informe con 0 picks es un resultado válido y esperado.

METODOLOGÍA Y REGLAS CLAVE:
1. Ancla: Usarás directamente el campo `_pinnacle_devig` que el backend ya calculó para la probabilidad de mercado.
2. Validación cruzada (Segundo Modelo): Debes buscar mediante búsqueda web el segundo modelo según el deporte (Elo, SRS, Pitagórico, etc.) y citar la fuente exacta.
3. Liquidez: Toma en cuenta el valor del campo `_liquidez_backend`.
4. Umbrales: Descartar si EV < 5% o si la divergencia entre Pinnacle y el segundo modelo es > 7%.
5. Confianza (1-10): Calcula el nivel de confianza de acuerdo a las reglas y muestra el desglose exacto. Un pick solo califica si la confianza es >= 8.
"""

# ==============================================================================
# 2. FUNCIONES MATEMÁTICAS Y DE PRE-FILTRADO EN PYTHON
# ==============================================================================


def devig_probabilidades(outcomes):
  """Recibe los outcomes de un mercado h2h y devuelve un diccionario con

  las probabilidades implícitas de-vigged (normalizadas a suma 1.0).
  """
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
  """Calcula la máxima diferencia de probabilidad implícita entre distintas

  casas de apuestas para un mismo evento.
  """
  if not isinstance(evento, dict):
    return 0.0

  home_team = evento.get("home_team")
  probs_home = []

  for b in evento.get("bookmakers", []):
    if not isinstance(b, dict):
      continue
    h2h = next(
        (
            m
            for m in b.get("markets", [])
            if isinstance(m, dict) and m.get("key") == "h2h"
        ),
        None,
    )
    if h2h:
      devig = devig_probabilidades(h2h.get("outcomes", []))
      if home_team in devig:
        probs_home.append(devig[home_team])

  if len(probs_home) < 2:
    return 0.0

  return max(probs_home) - min(probs_home)


def registrar_y_calcular_movimientos(eventos, deporte_key):
  """Compara las cuotas actuales de Pinnacle contra la consulta previa en

  st.session_state para detectar movimientos de dinero inteligente.
  """
  if not eventos:
    return {}

  state_key = f"pinnacle_snapshot_{deporte_key}"
  movimientos = {}
  snapshot_actual = {}

  for ev in eventos:
    if not isinstance(ev, dict):
      continue
    ev_id = ev.get("id")
    pinnacle = next(
        (
            b
            for b in ev.get("bookmakers", [])
            if isinstance(b, dict) and b.get("key") == "pinnacle"
        ),
        None,
    )
    if pinnacle:
      h2h = next(
          (
              m
              for m in pinnacle.get("markets", [])
              if isinstance(m, dict) and m.get("key") == "h2h"
          ),
          None,
      )
      if h2h:
        snapshot_actual[ev_id] = {
            "matchup": f"{ev.get('home_team')} vs {ev.get('away_team')}",
            "prices": {
                o["name"]: o["price"]
                for o in h2h.get("outcomes", [])
                if isinstance(o, dict) and o.get("price")
            },
        }

  # Comparar contra snapshot anterior en la sesión de Streamlit
  if state_key in st.session_state:
    snapshot_previo = st.session_state[state_key]
    if isinstance(snapshot_previo, dict):
      for ev_id, data_curr in snapshot_actual.items():
        if ev_id in snapshot_previo:
          data_prev = snapshot_previo[ev_id]
          for team, price_curr in data_curr.get("prices", {}).items():
            price_prev = data_prev.get("prices", {}).get(team)
            if price_prev and price_prev != price_curr:
              pct_change = round(
                  ((price_curr - price_prev) / price_prev) * 100, 2
              )
              direccion = "subió" if pct_change > 0 else "bajó"
              movimientos[f"{data_curr['matchup']} ({team})"] = (
                  f"Cuota cambió de {price_prev} a {price_curr} ({direccion}"
                  f" {abs(pct_change)}%)"
              )

  st.session_state[state_key] = snapshot_actual
  return movimientos


def filtrar_y_enriquecer(datos_crudos):
  """Filtra eventos sin Pinnacle o fuera del rango 1.40-2.00, y enriquece los

  eventos válidos con metadatos calculados en Python.
  """
  if not datos_crudos or not isinstance(datos_crudos, list):
    return (
        [],
        "Backend pre-filtró 0 eventos (no se recibieron datos válidos de la"
        " API).",
    )

  eventos_validos = []
  descartados_sin_pinnacle = 0
  descartados_fuera_de_rango = 0

  for evento in datos_crudos:
    if not isinstance(evento, dict):
      continue

    pinnacle = next(
        (
            b
            for b in evento.get("bookmakers", [])
            if isinstance(b, dict) and b.get("key") == "pinnacle"
        ),
        None,
    )
    if not pinnacle:
      descartados_sin_pinnacle += 1
      continue

    h2h = next(
        (
            m
            for m in pinnacle.get("markets", [])
            if isinstance(m, dict) and m.get("key") == "h2h"
        ),
        None,
    )
    if not h2h:
      descartados_sin_pinnacle += 1
      continue

    outcomes = h2h.get("outcomes", [])
    en_rango = any(
        1.40 <= o.get("price", 0) <= 2.00
        for o in outcomes
        if isinstance(o, dict)
    )
    if not en_rango:
      descartados_fuera_de_rango += 1
      continue

    # Enriquecer diccionario
    evento_enriquecido = dict(evento)
    evento_enriquecido["_pinnacle_devig"] = devig_probabilidades(outcomes)
    evento_enriquecido["_pinnacle_last_update"] = pinnacle.get("last_update")

    # Evaluación de liquidez
    n_bookmakers = len(evento.get("bookmakers", []))
    dispersion = calcular_dispersion_mercado(evento)

    if n_bookmakers >= 3 and dispersion < 0.05:
      evento_enriquecido["_liquidez_backend"] = "Alta"
    elif n_bookmakers >= 2:
      evento_enriquecido["_liquidez_backend"] = "Media"
    else:
      evento_enriquecido["_liquidez_backend"] = (
          "Media/Baja — solo Pinnacle, el LLM debe evaluar según la categoría"
          " de liga"
      )

    eventos_validos.append(evento_enriquecido)

  resumen_filtro = (
      f"Backend pre-filtró {len(datos_crudos)} eventos: "
      f"{len(eventos_validos)} candidatos calificados (cuota Pinnacle"
      " 1.40-2.00), "
      f"{descartados_sin_pinnacle} descartados sin cuota Pinnacle, "
      f"{descartados_fuera_de_rango} descartados fuera de rango."
  )
  return eventos_validos, resumen_filtro


def obtener_cuotas_api(api_key, sport_key):
  """Realiza la petición a The Odds API para el deporte seleccionado."""
  url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
  params = {
      "apiKey": api_key,
      "regions": "us,eu,us2",
      "markets": "h2h",
      "bookmakers": "pinnacle,stake,betonlineag,bet365",
  }
  try:
    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()
    return response.json()
  except Exception as e:
    st.error(f"Error al consultar The Odds API: {e}")
    return []


# ==============================================================================
# 3. INTERFAZ DE STREAMLIT Y FLUJO PRINCIPAL DE EJECUCIÓN
# ==============================================================================

st.title("📊 Analista de Apuesta Única v3.0")

# Selección de deporte
deportes_disponibles = {
    "MLB (Béisbol)": "baseball_mlb",
    "NBA (Baloncesto)": "basketball_nba",
    "Fútbol (UEFA Champions)": "soccer_uefa_champs_league",
    "Fútbol (EPL - Inglaterra)": "soccer_epl",
    "NHL (Hockey)": "icehockey_nhl",
    "Tennis ATP": "tennis_atp_us_open",
}

deporte_seleccionado_nombre = st.selectbox(
    "Selecciona el deporte/liga a analizar:", list(deportes_disponibles.keys())
)
deporte_seleccionado_key = deportes_disponibles[deporte_seleccionado_nombre]

# Configuración de API Key (vía Secrets de Streamlit o Input manual)
api_key = st.secrets.get("ODDS_API_KEY", "")
if not api_key:
  api_key = st.text_input("Ingresa tu Odds API Key:", type="password")

if st.button("Ejecutar Análisis"):
  if not api_key:
    st.warning("Por favor ingresa una API Key válida para continuar.")
  else:
    with st.spinner("Obteniendo cuotas y procesando pre-filtro..."):
      # 1. Obtener datos crudos de la API
      datos = obtener_cuotas_api(api_key, deporte_seleccionado_key)

      # 2. Hora local de Santo Domingo (UTC-4)
      tz_rd = timezone(timedelta(hours=-4))
      hora_rd = datetime.now(tz_rd).strftime("%Y-%m-%d %H:%M:%S AST (UTC-4)")

      # 3. Filtrar y enriquecer datos en backend (Python)
      eventos_filtrados, resumen_filtro = filtrar_y_enriquecer(datos)

      # 4. Movimientos de cuota Pinnacle
      movimientos_pinnacle = registrar_y_calcular_movimientos(
          eventos_filtrados, deporte_seleccionado_key
      )

      if movimientos_pinnacle:
        lineas_mov = "\n".join(
            f"- {k}: {v}" for k, v in movimientos_pinnacle.items()
        )
        seccion_movimiento = (
            f"MOVIMIENTOS EN PINNACLE (SNAPSHOT EN SESIÓN):\n{lineas_mov}"
        )
      else:
        seccion_movimiento = (
            "SIN SNAPSHOT PREVIO EN ESTA SESIÓN (Primera consulta realizada)."
        )

      # 5. Salida en interfaz
      st.write("### Resumen del Backend")
      st.info(resumen_filtro)

      if not eventos_filtrados:
        st.warning(
            "ℹ️ No hay apuestas candidatas dentro del rango 1.40 - 2.00 para"
            " hoy. No se requiere llamada al modelo."
        )
      else:
        # 6. Construir Prompt Completo
        prompt_completo = (
            f"{SYSTEM_PROMPT_BLINDADO_V3}\n\n"
            f"==================================================\n"
            f"CONTEXTO DE EJECUCIÓN DEL BACKEND\n"
            f"==================================================\n"
            f"DEPORTE: {deporte_seleccionado_nombre}\n"
            f"HORA LOCAL CONSULTA (RD/UTC-4): {hora_rd}\n\n"
            f"RESUMEN DE PRE-FILTRADO:\n{resumen_filtro}\n\n"
            f"{seccion_movimiento}\n\n"
            f"INSTRUCCIÓN TÉCNICA: Utiliza directamente los campos"
            " `_pinnacle_devig`, `_pinnacle_last_update` y `_liquidez_backend`."
            " No recalcules el de-vig ni filtres por rango nuevamente.\n\n"
            f"DATOS JSON PRE-FILTRADOS Y ENRIQUECIDOS:\n"
            f"{json.dumps(eventos_filtrados, indent=2, ensure_ascii=False)}"
        )

        st.success(
            f"✅ Se encontraron {len(eventos_filtrados)} candidatos listos"
            " para análisis."
        )

        with st.expander("Ver Prompt Completo generado para el LLM"):
          st.code(prompt_completo, language="text")

        # Aquí integrarías la llamada final a tu modelo (ej. Gemini o Claude):
        # respuesta = modelo.generate_content(prompt_completo)
        # st.markdown(respuesta.text)
