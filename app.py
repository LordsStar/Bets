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
# 2. FUNCIONES BACKEND CON CACHÉ Y FILTRADO
# ==============================================================================

@st.cache_data(ttl=3600)
def obtener_deportes_activos(api_key):
    """Obtiene la lista completa de deportes activos hoy en The Odds API guardando en caché por 1 hora."""
    url = f"https://api.the-odds-api.com/v4/sports/?apiKey={api_key}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return [s for s in response.json() if s.get("active") and not s.get("has_outrights")]
    except Exception as e:
        st.error(f"Error al obtener deportes desde la API: {e}")
        return []

@st.cache_data(ttl=1800)
def obtener_cuotas_api(api_key, sport_key):
    """Consulta cuotas para un deporte específico utilizando caché por 30 min (protege tus créditos)."""
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
    """Calcula probabilidades de-vigged normalizadas."""
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

def registrar_y_calcular_movimientos(eventos_minificados, deporte_key):
    """Detecta cambios de cuota en Pinnacle mediante st.session_state sobre datos minificados."""
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
            snapshot_actual[ev_id] = {
                "matchup": matchup,
                "prices": prices
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

def filtrar_y_enriquecer(datos_crudos, horas_ventana=24):
    """
    Filtra eventos de las próximas 24 horas, rango de cuota 1.40-2.00
    y genera un JSON minificado optimizado para ahorrar tokens en la IA.
    """
    if not datos_crudos or not isinstance(datos_crudos, list):
        return [], "Backend pre-filtró 0 eventos (sin datos recibidos)."

    eventos_validos = []
    descartados_sin_pinnacle = 0
    descartados_fuera_de_rango = 0
    descartados_fecha = 0

    ahora_utc = datetime.now(timezone.utc)
    limite_utc = ahora_utc + timedelta(hours=horas_ventana)

    for evento in datos_crudos:
        if not isinstance(evento, dict):
            continue

        # 1. Filtro de Fecha: Únicamente partidos de las próximas 24 horas
        commence_str = evento.get("commence_time")
        if commence_str:
            try:
                commence_dt = datetime.fromisoformat(commence_str.replace("Z", "+00:00"))
                if not (ahora_utc <= commence_dt <= limite_utc):
                    descartados_fecha += 1
                    continue
            except Exception:
                pass

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

        # Cálculos del backend
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

        # 2. Minificación para reducir tamaño de Prompt
        evento_minificado = {
            "id": evento.get("id"),
            "deporte": evento.get("sport_title") or evento.get("sport_key"),
            "partido": f"{evento.get('home_team')} vs {evento.get('away_team')}",
            "inicio_utc": commence_str,
            "cuotas_pinnacle": cuotas_pinnacle,
            "_pinnacle_devig": pinnacle_devig,
            "_pinnacle_last_update": pinnacle.get("last_update"),
            "_liquidez_backend": liquidez
        }

        eventos_validos.append(evento_minificado)

    resumen_filtro = (
        f"Backend pre-filtró {len(datos_crudos)} eventos: "
        f"{len(eventos_validos)} candidatos calificados (hoy / prx 24h, cuota 1.40-2.00), "
        f"{descartados_fecha} descartados por fecha (eventos futuros), "
        f"{descartados_sin_pinnacle} descartados sin Pinnacle, "
        f"{descartados_fuera_de_rango} descartados fuera de rango."
    )
    return eventos_validos, resumen_filtro

# ==============================================================================
# 3. INTERFAZ DE USUARIO Y SELECCIÓN DE IA
# ==============================================================================

st.set_page_config(page_title="Analista Cuantitativo de Apuestas", layout="wide")
st.title("📊 Analista de Apuesta Única v3.0 (Multi-IA & Multi-Deporte)")

# Configuración de API Keys
with st.sidebar:
    st.header("🔑 Configuración de APIs")
    api_key = st.secrets.get("ODDS_API_KEY", "")
    if not api_key:
        api_key = st.text_input("Odds API Key:", type="password")
    
    gemini_api_key = st.secrets.get("GEMINI_API_KEY", "")
    if not gemini_api_key:
        gemini_api_key = st.text_input("Gemini API Key (Opcional):", type="password")

if api_key:
    deportes_lista = obtener_deportes_activos(api_key)
    
    if deportes_lista:
        opciones_deporte = {"🔥 TODOS LOS DEPORTES ACTIVOS": "ALL"}
        for dep in deportes_lista:
            opciones_deporte[f"{dep.get('group')} - {dep.get('title')}"] = dep.get('key')

        seleccion = st.selectbox("Selecciona el deporte o ámbito a analizar:", list(opciones_deporte.keys()))
        deporte_key_seleccionado = opciones_deporte[seleccion]

        if st.button("🚀 Generar Prompt y Procesar Datos", type="primary"):
            with st.spinner("Consultando The Odds API (o usando caché) y procesando pre-filtros..."):
                datos_acumulados = []

                if deporte_key_seleccionado == "ALL":
                    progress_bar = st.progress(0)
                    total_deps = len(deportes_lista)
                    for idx, dep in enumerate(deportes_lista):
                        cuotas = obtener_cuotas_api(api_key, dep.get('key'))
                        if cuotas:
                            datos_acumulados.extend(cuotas)
                        progress_bar.progress((idx + 1) / total_deps)
                    progress_bar.empty()
                else:
                    datos_acumulados = obtener_cuotas_api(api_key, deporte_key_seleccionado)

                tz_rd = timezone(timedelta(hours=-4))
                hora_rd = datetime.now(tz_rd).strftime("%Y-%m-%d %H:%M:%S AST (UTC-4)")

                eventos_filtrados, resumen_filtro = filtrar_y_enriquecer(datos_acumulados)
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

                    st.session_state["prompt_generado"] = prompt_completo
                    st.success(f"✅ Se consolidaron {len(eventos_filtrados)} eventos aptos para el prompt.")

    # ==============================================================================
    # 4. BOTONES PARA ABRIR Y EJECUTAR EN CADA IA
    # ==============================================================================
    if "prompt_generado" in st.session_state:
        st.divider()
        st.subheader("🤖 Selecciona la IA para ejecutar el Análisis")

        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            st.link_button("🌐 Abrir ChatGPT", "https://chatgpt.com", use_container_width=True)
        with col2:
            st.link_button("🌐 Abrir Claude", "https://claude.ai", use_container_width=True)
        with col3:
            st.link_button("🌐 Abrir Gemini Web", "https://gemini.google.com", use_container_width=True)
        with col4:
            st.link_button("🌐 Abrir DeepSeek", "https://chat.deepseek.com", use_container_width=True)

        st.write("#### 📋 Prompt Listo para Copiar")
        st.code(st.session_state["prompt_generado"], language="markdown")

        if gemini_api_key:
            st.divider()
            st.subheader("⚡ Ejecución Directa en App (Google Gemini API)")
            if st.button("🤖 Analizar directamente con Gemini API", type="primary"):
                with st.spinner("Gemini está analizando las apuestas con la metodología Blindada v3.0..."):
                    try:
                        import google.generativeai as genai
                        genai.configure(api_key=gemini_api_key)
                        model = genai.GenerativeModel("gemini-1.5-pro")
                        response = model.generate_content(st.session_state["prompt_generado"])
                        st.markdown("### 🏆 Resultado del Análisis")
                        st.markdown(response.text)
                    except Exception as e:
                        st.error(f"Error al ejecutar con Gemini API: {e}")
