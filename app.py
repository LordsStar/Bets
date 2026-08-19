import json
from datetime import datetime, timedelta, timezone
import requests
import streamlit as st

# ==============================================================================
# 1. SYSTEM PROMPT V3.0
# ==============================================================================
SYSTEM_PROMPT_BLINDADO_V3 = """
PROMPT — Analista Cuantitativo de Apuesta Única (Blindado v3.0)

ROL Y OBJETIVO:
Actúa como Analista Cuantitativo de Deportes y Tipster Profesional. Tu objetivo es seleccionar una sola apuesta —la de mayor confianza estadística— dentro de un rango de cuota entre 1.40 y 2.00 (moneyline o mercado principal) de TODOS los eventos recibidos. Un informe con 0 picks es un resultado válido y esperado.

METODOLOGÍA Y REGLAS CLAVE:
1. Ancla: Usarás directamente el campo `_pinnacle_devig` que el backend ya calculó para la probabilidad de mercado.
2. Validación cruzada (Segundo Modelo): Debes buscar mediante búsqueda web el segundo modelo según el deporte (Elo, SRS, Pitagórico, etc.) y citar la fuente exacta.
3. Liquidez: Toma en cuenta el valor del campo `_liquidez_backend`.
4. Umbrales: Descartar si EV < 5% o si la divergencia entre Pinnacle y el segundo modelo es > 7%.
5. Confianza (1-10): Calcula el nivel de confianza de acuerdo a las reglas y muestra el desglose exacto. Un pick solo califica si la confianza es >= 8.
"""

# ==============================================================================
# 2. FUNCIONES DE API Y PRE-FILTRADO
# ==============================================================================

def obtener_deportes_activos(api_key):
    """Obtiene la lista completa de deportes activos hoy en The Odds API."""
    url = f"https://api.the-odds-api.com/v4/sports/?apiKey={api_key}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        # Filtra solo eventos activos y excluye apuestas futuras/outrights
        deportes = [s for s in response.json() if s.get("active") and not s.get("has_outrights")]
        return deportes
    except Exception as e:
        st.error(f"Error al obtener la lista de deportes desde The Odds API: {e}")
        return []


def obtener_cuotas_api(api_key, sport_key):
    """Consulta las cuotas para un deporte específico."""
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
    except Exception:
        return []


def devig_probabilidades(outcomes):
    """Calcula probabilidades de-vigged normalizadas a 1.0."""
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
    """Mide la dispersión de probabilidades entre casas de apuestas."""
    if not isinstance(evento, dict):
        return 0.0
    home_team = evento.get("home_team")
    probs_home = []

    for b in evento.get("bookmakers", []):
        if not isinstance(b, dict):
            continue
        h2h = next((m for m in b.get("markets", []) if isinstance(m, dict) and m.get("key") == "h2h"), None)
        if h2h:
            devig = devig_probabilidades(h2h.get("outcomes", []))
            if home_team in devig:
                probs_home.append(devig[home_team])

    if len(probs_home) < 2:
        return 0.0
    return max(probs_home) - min(probs_home)


def registrar_y_calcular_movimientos(eventos, deporte_key):
    """Detecta cambios de cuota en Pinnacle mediante st.session_state."""
    if not eventos:
        return {}
    state_key = f"pinnacle_snapshot_{deporte_key}"
    movimientos = {}
    snapshot_actual = {}

    for ev in eventos:
        if not isinstance(ev, dict):
            continue
        ev_id = ev.get("id")
        pinnacle = next((b for b in ev.get("bookmakers", []) if isinstance(b, dict) and b.get("key") == "pinnacle"), None)
        if pinnacle:
            h2h = next((m for m in pinnacle.get("markets", []) if isinstance(m, dict) and m.get("key") == "h2h"), None)
            if h2h:
                snapshot_actual[ev_id] = {
                    "matchup": f"{ev.get('home_team')} vs {ev.get('away_team')}",
                    "prices": {o["name"]: o["price"] for o in h2h.get("outcomes", []) if isinstance(o, dict) and o.get("price")}
                }

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


def filtrar_y_enriquecer(datos_crudos):
    """Aplica la regla de cuota 1.40 - 2.00 y enriquece con de-vig."""
    if not datos_crudos or not isinstance(datos_crudos, list):
        return [], "Backend pre-filtró 0 eventos (no se recibieron datos válidos)."

    eventos_validos = []
    descartados_sin_pinnacle = 0
    descartados_fuera_de_rango = 0

    for evento in datos_crudos:
        if not isinstance(evento, dict):
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

        evento_enriquecido = dict(evento)
        evento_enriquecido["_pinnacle_devig"] = devig_probabilidades(outcomes)
        evento_enriquecido["_pinnacle_last_update"] = pinnacle.get("last_update")

        n_bookmakers = len(evento.get("bookmakers", []))
        dispersion = calcular_dispersion_mercado(evento)

        if n_bookmakers >= 3 and dispersion < 0.05:
            evento_enriquecido["_liquidez_backend"] = "Alta"
        elif n_bookmakers >= 2:
            evento_enriquecido["_liquidez_backend"] = "Media"
        else:
            evento_enriquecido["_liquidez_backend"] = "Media/Baja — evaluar según categoría de liga"

        eventos_validos.append(evento_enriquecido)

    resumen_filtro = (
        f"Backend pre-filtró {len(datos_crudos)} eventos totales procesados: "
        f"{len(eventos_validos)} calificados (cuota Pinnacle 1.40-2.00), "
        f"{descartados_sin_pinnacle} descartados sin Pinnacle, "
        f"{descartados_fuera_de_rango} descartados fuera de rango."
    )
    return eventos_validos, resumen_filtro


# ==============================================================================
# 3. INTERFAZ Y EJECUCIÓN EN STREAMLIT
# ==============================================================================

st.set_page_config(page_title="Analista Cuantitativo de Apuestas", layout="wide")
st.title("📊 Analista de Apuesta Única v3.0 (Multi-Deporte)")

# Gestión de API Key
api_key = st.secrets.get("ODDS_API_KEY", "")
if not api_key:
    api_key = st.text_input("Ingresa tu Odds API Key:", type="password")

if api_key:
    deportes_lista = obtener_deportes_activos(api_key)
    
    if deportes_lista:
        opciones_deporte = {"🔥 TODOS LOS DEPORTES ACTIVOS": "ALL"}
        for dep in deportes_lista:
            opciones_deporte[f"{dep.get('group')} - {dep.get('title')}"] = dep.get('key')

        seleccion = st.selectbox("Selecciona el deporte o ámbito de análisis:", list(opciones_deporte.keys()))
        deporte_key_seleccionado = opciones_deporte[seleccion]

        if st.button("Ejecutar Análisis"):
            with st.spinner("Consultando The Odds API y analizando mercados..."):
                datos_acumulados = []

                if deporte_key_seleccionado == "ALL":
                    # Iterar sobre todos los deportes activos
                    progress_bar = st.progress(0)
                    total_deps = len(deportes_lista)
                    for idx, dep in enumerate(deportes_lista):
                        key = dep.get('key')
                        cuotas = obtener_cuotas_api(api_key, key)
                        if cuotas:
                            datos_acumulados.extend(cuotas)
                        progress_bar.progress((idx + 1) / total_deps)
                    progress_bar.empty()
                else:
                    datos_acumulados = obtener_cuotas_api(api_key, deporte_key_seleccionado)

                # Hora de Santo Domingo UTC-4
                tz_rd = timezone(timedelta(hours=-4))
                hora_rd = datetime.now(tz_rd).strftime("%Y-%m-%d %H:%M:%S AST (UTC-4)")

                # Filtrado y movimientos
                eventos_filtrados, resumen_filtro = filtrar_y_enriquecer(datos_acumulados)
                movimientos_pinnacle = registrar_y_calcular_movimientos(eventos_filtrados, deporte_key_seleccionado)

                seccion_movimiento = "SIN SNAPSHOT PREVIO EN ESTA SESIÓN."
                if movimientos_pinnacle:
                    lineas_mov = "\n".join(f"- {k}: {v}" for k, v in movimientos_pinnacle.items())
                    seccion_movimiento = f"MOVIMIENTOS EN PINNACLE DETECTADOS:\n{lineas_mov}"

                st.write("### Resultado del Procesamiento de Datos")
                st.info(resumen_filtro)

                if not eventos_filtrados:
                    st.warning("⚠️ No se encontraron candidatos válidos en el rango 1.40 - 2.00 para los partidos de hoy.")
                else:
                    prompt_completo = (
                        f"{SYSTEM_PROMPT_BLINDADO_V3}\n\n"
                        f"==================================================\n"
                        f"CONTEXTO DE EJECUCIÓN DEL BACKEND\n"
                        f"==================================================\n"
                        f"ÁMBITO: {seleccion}\n"
                        f"HORA CONSULTA (RD/UTC-4): {hora_rd}\n\n"
                        f"RESUMEN DE PRE-FILTRADO:\n{resumen_filtro}\n\n"
                        f"{seccion_movimiento}\n\n"
                        f"INSTRUCCIÓN TÉCNICA: Utiliza directamente los campos `_pinnacle_devig`, `_pinnacle_last_update` "
                        f"y `_liquidez_backend`. No recalcules el de-vig ni filtres por rango nuevamente.\n\n"
                        f"DATOS JSON PRE-FILTRADOS Y ENRIQUECIDOS:\n"
                        f"{json.dumps(eventos_filtrados, indent=2, ensure_ascii=False)}"
                    )

                    st.success(f"✅ Se consolidaron {len(eventos_filtrados)} eventos aptos para el prompt.")
                    
                    with st.expander("Ver Prompt completo enviado al LLM"):
                        st.code(prompt_completo, language="text")
    else:
        st.error("No se pudieron cargar los deportes activos. Verifica que tu API Key sea correcta.")
