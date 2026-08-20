import json
import time
from datetime import datetime, timedelta, timezone

import requests
import streamlit as st

# ==============================================================================
# 1. SYSTEM PROMPT V3.1 — BLINDADO (restaura las salvaguardas de la v2.0
#    que se habían perdido en la v3.0: fuentes por deporte, gate de frescura,
#    reglas anti-fabricación y formato de salida fijo)
# ==============================================================================
SYSTEM_PROMPT_BLINDADO_V3_1 = """
PROMPT — Analista Cuantitativo de Apuesta Única (Blindado v3.1)

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
     tiene más de 90 minutos de antigüedad → sí es señal real de posible dato
     desactualizado cerca del cierre del mercado → DESCARTA el evento.
   - Si al evento le faltan MÁS de 3 horas, la antigüedad de `_pinnacle_last_update`
     es solo informativa: NO descartes el evento por este motivo.
   Registra en el resumen cuántos eventos cayeron específicamente por este gate
   (distinto de "sin segundo modelo" o "fuera de umbral EV").

3. VALIDACIÓN CRUZADA (Segundo Modelo) — fuente específica por deporte, NO genérica:
   - Fútbol (soccer): ClubElo (ratings Elo) o el modelo SPI/Elo de FiveThirtyEight
     si está disponible.
   - Tenis (ATP/WTA): TennisAbstract (Elo por superficie) o ranking oficial ATP/WTA
     como referencia secundaria.
   - MLB: FanGraphs (proyecciones de equipo / pitcher matchup).
   - NBA/NCAAMB: Basketball-Reference (SRS o ratings ofensivo/defensivo).
   - NHL: Hockey-Reference (SRS).
   Debes buscar esta fuente vía web search REAL y citar la URL exacta consultada.
   Si no puedes verificar el segundo modelo para un evento, ese evento queda
   automáticamente descartado (no se asume, no se estima, no se inventa).

4. LIQUIDEZ: Usa el campo `_liquidez_backend` tal cual. No la reinterpretes.

5. UMBRALES DE DESCARTE:
   - EV < 5% → descartar.
   - Divergencia |Pinnacle - Segundo Modelo| > 7% → descartar (señal de posible
     error de datos, no de "value").

6. CONFIANZA (1-10): Calcula con el siguiente desglose visible en el informe:
   - Edge estadístico (EV real vs. umbral)
   - Calidad/frescura de la fuente del segundo modelo
   - Liquidez del mercado
   - Coherencia entre movimiento de línea (si hay datos) y el pick
   Un pick solo califica si la confianza total es >= 8/10.

REGLAS ANTI-FABRICACIÓN (obligatorias, sin excepción):
- Nunca inventes lesiones, alineaciones, clima o noticias que no hayas confirmado
  con una fuente real y citada.
- Nunca inventes cuotas, nombres de equipos/jugadores o resultados históricos que
  no estén en el JSON de entrada o en una fuente web verificada.
- Si falta cualquier dato necesario para completar el análisis de un evento
  (segundo modelo, frescura, liquidez), ese evento se descarta — nunca se rellena
  el vacío con una suposición "razonable".
- Cada afirmación estadística debe llevar su fuente (nombre + URL).

FORMATO DE SALIDA (obligatorio, en español):
1. Resumen: cuántos eventos se evaluaron, cuántos se descartaron y por qué (agrupado
   por motivo: sin segundo modelo, datos obsoletos, fuera de umbral EV, divergencia).
2. Si hay pick: Partido | Mercado | Cuota Pinnacle | Prob. implícita de-vigged |
   Prob. segundo modelo (con fuente y URL) | EV% | Confianza (con desglose) |
   Justificación en 3-4 líneas.
3. Si NO hay pick: decirlo explícitamente en la primera línea ("PICK DEL DÍA:
   NINGUNO") y explicar brevemente por qué ningún evento alcanzó el umbral.
"""

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

# ==============================================================================
# 2. FUNCIONES BACKEND
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
    ignora 'regions' cuando 'bookmakers' está presente — mandarlos juntos
    no aporta nada y puede confundir al leer el código.
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
    """
    Mide la dispersión de probabilidades entre casas de apuestas.
    CORREGIDO: antes solo miraba el lado 'home'; ahora toma el spread máximo
    encontrado en CUALQUIER resultado del mercado (home, away, draw), que es
    una verificación cruzada más honesta entre casas.
    """
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


def filtrar_y_enriquecer(datos_crudos, horas_ventana=24):
    if not datos_crudos or not isinstance(datos_crudos, list):
        return [], "Backend pre-filtró 0 eventos (sin datos recibidos)."

    eventos_validos = []
    descartados_sin_pinnacle = 0
    descartados_fuera_de_rango = 0
    descartados_fecha = 0
    descartados_sin_fecha = 0

    ahora_utc = datetime.now(timezone.utc)
    limite_utc = ahora_utc + timedelta(hours=horas_ventana)

    for evento in datos_crudos:
        if not isinstance(evento, dict):
            continue

        commence_str = evento.get("commence_time")
        if not commence_str:
            # CORREGIDO: antes un evento sin fecha pasaba el filtro igual.
            # Sin fecha no se puede validar el gate de frescura → se descarta.
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

        evento_minificado = {
            "id": evento.get("id"),
            "deporte": evento.get("sport_title") or evento.get("sport_key"),
            "partido": f"{evento.get('home_team')} vs {evento.get('away_team')}",
            "inicio_utc": commence_str,
            "cuotas_pinnacle": cuotas_pinnacle,
            "_pinnacle_devig": pinnacle_devig,
            "_pinnacle_last_update": pinnacle.get("last_update"),
            "_liquidez_backend": liquidez,
            "_dispersion_max_entre_casas": round(dispersion, 4),
            "_n_casas_reportando": n_bookmakers,
        }
        eventos_validos.append(evento_minificado)

    resumen_filtro = (
        f"Backend pre-filtró {len(datos_crudos)} eventos: "
        f"{len(eventos_validos)} candidatos calificados (prx {horas_ventana}h, cuota 1.40-2.00), "
        f"{descartados_fecha} descartados por fecha fuera de ventana, "
        f"{descartados_sin_fecha} descartados por fecha faltante/ilegible, "
        f"{descartados_sin_pinnacle} descartados sin Pinnacle, "
        f"{descartados_fuera_de_rango} descartados fuera de rango de cuota."
    )
    return eventos_validos, resumen_filtro


# ==============================================================================
# 3. GEMINI — modelos SIEMPRE consultados en vivo (nunca hardcodeados,
#    porque Google cambia/retira nombres de modelo cada pocas semanas)
# ==============================================================================

@st.cache_data(ttl=1800, show_spinner=False)
def listar_modelos_gemini(gemini_api_key):
    """Consulta ListModels en vivo y devuelve solo modelos de texto utilizables."""
    url = f"{GEMINI_API_BASE}/models?key={gemini_api_key}"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        modelos = r.json().get("models", [])
        utilizables = []
        for m in modelos:
            nombre = m.get("name", "").replace("models/", "")
            metodos = m.get("supportedGenerationMethods", [])
            # Filtra fuera de imagen/audio/tts/embeddings/live y modelos deprecados
            if "generateContent" in metodos and not any(
                x in nombre for x in ["image", "audio", "tts", "embedding", "live", "vision"]
            ):
                utilizables.append(nombre)
        return sorted(utilizables, reverse=True)
    except Exception as e:
        st.error(f"No se pudo obtener la lista de modelos de Gemini: {e}")
        return []


def llamar_gemini_rest(gemini_api_key, modelo, prompt_texto):
    """Llamada REST directa (evita depender del SDK, que se desactualiza con
    cada modelo nuevo)."""
    url = f"{GEMINI_API_BASE}/models/{modelo}:generateContent"
    headers = {"x-goog-api-key": gemini_api_key, "Content-Type": "application/json"}
    body = {"contents": [{"parts": [{"text": prompt_texto}]}]}
    r = requests.post(url, headers=headers, json=body, timeout=90)
    r.raise_for_status()
    data = r.json()
    partes = data["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in partes)


# ==============================================================================
# 4. INTERFAZ
# ==============================================================================

st.set_page_config(page_title="Analista Cuantitativo de Apuestas", layout="wide")
st.title("📊 Analista de Apuesta Única v3.1 (Multi-IA & Multi-Deporte)")

with st.sidebar:
    st.header("🔑 Configuración de APIs")
    api_key = st.secrets.get("ODDS_API_KEY", "")
    if not api_key:
        api_key = st.text_input("Odds API Key:", type="password")

    gemini_api_key = st.secrets.get("GEMINI_API_KEY", "")
    if not gemini_api_key:
        gemini_api_key = st.text_input("Gemini API Key (Opcional):", type="password")

    if "odds_api_uso" in st.session_state:
        uso = st.session_state["odds_api_uso"]
        st.caption(f"📉 Odds API — usados: {uso['usados']} · restantes: {uso['restantes']}")

if api_key:
    deportes_lista = obtener_deportes_activos(api_key)

    if deportes_lista:
        opciones_deporte = {"🔥 TODOS LOS DEPORTES ACTIVOS": "ALL"}
        for dep in deportes_lista:
            opciones_deporte[f"{dep.get('group')} - {dep.get('title')}"] = dep.get('key')

        seleccion = st.selectbox("Selecciona el deporte o ámbito a analizar:", list(opciones_deporte.keys()))
        deporte_key_seleccionado = opciones_deporte[seleccion]

        if st.button("🚀 Generar Prompt y Procesar Datos", type="primary"):
            with st.spinner("Consultando The Odds API y procesando pre-filtros..."):
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
                        f"{SYSTEM_PROMPT_BLINDADO_V3_1}\n\n"
                        f"==================================================\n"
                        f"CONTEXTO DE EJECUCIÓN DEL BACKEND\n"
                        f"==================================================\n"
                        f"ÁMBITO: {seleccion}\n"
                        f"HORA CONSULTA (RD/UTC-4): {hora_rd}\n\n"
                        f"RESUMEN DE PRE-FILTRADO:\n{resumen_filtro}\n\n"
                        f"{seccion_movimiento}\n\n"
                        f"INSTRUCCIÓN TÉCNICA: Utiliza directamente los campos `_pinnacle_devig`, "
                        f"`_pinnacle_last_update`, `_liquidez_backend`, `_dispersion_max_entre_casas` "
                        f"y `_n_casas_reportando`. No recalcules el de-vig ni filtres por rango nuevamente.\n\n"
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
                            resultado = llamar_gemini_rest(
                                gemini_api_key, modelo_elegido, st.session_state["prompt_generado"]
                            )
                            st.markdown("### 🏆 Resultado del Análisis")
                            st.markdown(resultado)
                        except Exception as e:
                            st.error(f"Error al ejecutar con Gemini API: {e}")
            else:
                st.warning("No se pudo obtener la lista de modelos. Verifica la API Key de Gemini.")
