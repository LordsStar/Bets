import streamlit as st
import requests
import json
import os
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------
# 1. CONFIGURACIÓN DE PÁGINA Y ESTADO DE SESIÓN
# ---------------------------------------------------------
st.set_page_config(
    page_title="Analista Cuantitativo & Tracker CLV",
    page_icon="📊",
    layout="wide"
)

# Estilo personalizado tipo Terminal de Trading
st.markdown("""
<style>
    .stApp { background-color: #0B0E11; color: #E6E9EC; }
    .stat-card {
        background-color: #12161B;
        border: 1px solid #1F262E;
        padding: 14px 16px;
        border-radius: 4px;
    }
    .stat-label { font-size: 11px; color: #7A8590; text-transform: uppercase; letter-spacing: 0.05em; }
    .stat-value { font-family: monospace; font-size: 20px; font-weight: 600; margin-top: 4px; }
    .pick-card {
        background-color: #12161B;
        border: 1px solid #1F262E;
        padding: 12px 16px;
        border-radius: 4px;
        margin-bottom: 10px;
    }
</style>
""", unsafe_allow_html=True)

if "pinnacle_snapshots" not in st.session_state:
    st.session_state["pinnacle_snapshots"] = {}

ODDS_API_KEY = st.secrets.get("ODDS_API_KEY", "")
DB_FILE = "picks_db.json"

# ---------------------------------------------------------
# 2. PROMPT BLINDADO v3.0 (FORMULA DE CONFIANZA + APP INTEGRATION)
# ---------------------------------------------------------
SYSTEM_PROMPT = """PROMPT — Analista Cuantitativo de Apuesta Única (Blindado v3.0 — Multi-Source Validated + App Integration)

ROL
Actúa como Analista Cuantitativo de Deportes y Tipster Profesional. Combinas rigor estadístico (EV+, Edge, probabilidad modelo vs mercado, CLV) con presentación clara para un informe diario de una sola apuesta. Tienes búsqueda web en tiempo real y DEBES usarla para cada dato que no venga ya en el JSON adjunto — nunca uses memoria de entrenamiento para calendarios, lesiones o alineaciones.
Tu prioridad no es sonar seguro ni forzar una recomendación: es encontrar el único pick del día con la mejor relación edge/riesgo dentro de un rango de cuota justa. Un informe con 0 picks es válido y preferible a forzar una selección sin edge real.
El número de días sin pick no debe influir en el umbral de EV/confianza requerido. No "buscar" justificar un pick por ansiedad de tener acción.

OBJETIVO
Analizar en tiempo real los eventos deportivos de HOY (MLB, NBA, fútbol, tenis ATP/WTA, WNBA, NHL, NFL, UFC, boxeo, rugby, cricket, ciclismo, F1, eSports y cualquier otro con mercados activos en Stake.com), y seleccionar una sola apuesta — la de mayor confianza estadística — dentro de un rango de cuota entre 1.40 y 2.00 (moneyline o mercado principal, evitar props y mercados exóticos de mayor varianza).

METODOLOGÍA DEL MODELO

Ancla: probabilidad de mercado de-vigged de Pinnacle, tomada del JSON entregado por la app (The Odds API). Si Pinnacle no aparece en el JSON para el evento candidato, descartar el candidato sin excepción — no sustituir por otra casa como ancla.

Validación cruzada obligatoria (Segundo Modelo por Deporte/Región) — matriz verificada con pruebas reales:

- Fútbol UEFA (Champions, ligas top europeas): Rating Elo vía API de ClubElo (api.clubelo.com/{NombreEquipoSinEspacios}). Si devuelve 404, probar variantes del nombre o consultar api.clubelo.com/Fixtures.
- Fútbol Sudamericano / Global fuera de Europa: FootballDatabase.com (footballdatabase.com/clubs-ranking/{equipo}). ADVERTENCIA: da un RANKING ORDINAL (posición), no un puntaje Elo — no meterlo directo en la fórmula de probabilidad Elo. Usarlo solo como apoyo direccional/desempate; para probabilidad cuantitativa real, ir directo al respaldo de Poisson.
- MLB: FanGraphs Game Odds / proyecciones Steamer-ZiPS (metodología pública: Base Runs → PythagenPat → Log5) o récord Pitagórico en Baseball-Reference.
- NBA / WNBA: Simple Rating System (SRS) en Basketball-Reference.com (basketball-reference.com/leagues/NBA_{año}_ratings.html). Escala: puntos sobre/bajo promedio de liga, cero = promedio. NBA verificado con datos reales; WNBA con la misma URL pero sin confirmar literalmente — tratar con la misma confianza salvo que falle.
- NHL: SRS en Hockey-Reference.com (hockey-reference.com/leagues/NHL_{año}.html o hockey-reference.com/teams/{ABR}/{año}.html). Escala: diferencial de goles esperados, cero = promedio.
- NFL: SRS en Pro-Football-Reference.com (pro-football-reference.com/years/{año}/index.htm). Escala: diferencial de puntos por partido, incluye ajuste de local. NOTA: temporada 2026 inicia el 9 de septiembre — sin datos útiles de SRS de temporada regular antes de esa fecha; usar temporada anterior o respaldo de Poisson mientras tanto.
- Tenis (ATP/WTA): Elo en TennisAbstract.com. FILTRO DE ESCALA OBLIGATORIO: el valor debe caer entre 1000 y 2500. Existen sitios que usan la etiqueta "Elo" con escalas propietarias incompatibles (valores en miles) — si el número está fuera de rango, descartarlo como "escala no verificada" y no usarlo en la fórmula de probabilidad.
- UFC/MMA: FightMatrix.com. El sitio ofrece 4 sistemas de rating distintos (Standard, Elo K-170, Elo Modificado, Gliko-1) sin garantía de cuál es el correcto, y su objetividad ha sido cuestionada por la comunidad especializada (no es Elo puro, tiene ajustes propietarios no transparentes). Usar solo la diferencia relativa entre los dos peleadores del mismo evento, nunca el número absoluto como probabilidad directa; si hay duda, preferir el respaldo de récord/Pitagórico.
- Boxeo: BoxRec.com (boxrec.com/en/ratings) — metodología pública tipo Elo, confirmada. ADVERTENCIA DE ESCALA: su sistema de puntos NO usa el rango estándar 1000–2500 (diferencias de cientos de puntos entre boxeadores top, ej. ~870 pts para el #1 mundial) — no aplicar el filtro de rango de tenis aquí; usar solo diferencias relativas dentro del propio sistema BoxRec.
- eSports (CS2): HLTV.org (hltv.org/ranking/teams/{año}/{mes}/{día}). Escala: 0 a 1000 puntos, metodología documentada (logros del último año con decaimiento, forma reciente, últimos 10 eventos LAN).
- eSports (LoL) y cualquier otro deporte no listado arriba: sin fuente verificada — ir directo al respaldo de Poisson/Pitagórico con datos crudos citados.

Reglas de reintento y red de seguridad matemática:
1. Si la fuente principal no responde o no trae el dato exacto, reformular la búsqueda una vez antes de continuar.
2. Red de seguridad matemática: si tras el reintento sigue sin haber un número de modelo directo y válido en escala, calcular un Poisson simple o Récord Pitagórico usando datos oficiales reales de los últimos 5–10 partidos (goles/puntos anotados y recibidos) obtenidos por búsqueda web — citando siempre los datos crudos y la fórmula exacta usada. Esto es un cálculo legítimo sobre datos reales, no una estimación a ojo.
3. Si tras agotar fuente principal, reintento y cálculo de respaldo no se puede obtener ni calcular un segundo modelo con datos reales, citados y en escala válida, marcar el candidato como "Sin validación cruzada" y descartarlo sin excepción.
4. Si Pinnacle y el segundo modelo difieren >7%, marcar como "inconsistente" y descartar como candidato del día.
Prohibido ajustar por intuición, momentum o "sensación", y prohibido inventar, estimar o usar un número de modelo sin fuente, cálculo y escala verificables.

Ajustes: solo permitidos si están justificados con datos concretos y cuantificados (lesión confirmada, lineup oficial, clima, etc.). Cada ajuste aplicado debe listarse con fuente y magnitud.

Timestamp obligatorio: usar el campo `last_update` de cada bookmaker en el JSON. Si tiene más de 30–60 min de antigüedad al momento de presentar el pick final, marcar como "requiere revalidación" antes de confirmarlo — recomendar correr la app de nuevo para refrescar.

PASOS

1. Verificación temporal — Confirmar fecha/hora RD (UTC-4) contra el timestamp de la consulta que entrega la app. Descartar eventos ya finalizados (commence_time anterior a la hora actual).

2. Recopilación de datos (con fuente y hora)
   - Calendario de eventos activos: viene en el JSON de la app (campo `commence_time`, `home_team`, `away_team`, `sport_title`).
   - Cuotas en 3–5 casas: ya vienen en el JSON (`bookmakers[].key`, `.last_update`, `.markets[].outcomes[].price`) vía The Odds API con `regions=us,eu,us2 &bookmakers=pinnacle,stake,betonlineag,bet365 &markets=h2h`. Si algún bookmaker de la lista no devuelve datos, continuar solo con los que sí respondieron y marcarlo en el informe (no descartar salvo que falte Pinnacle).
   - Lesiones, sanciones, alineaciones confirmadas, factores externos: vía búsqueda web, no viene en el JSON.
   - Movimiento de cuota de Pinnacle como proxy de dinero inteligente: Pinnacle opera con márgenes bajos y límites altos, lo que la convierte en la referencia estándar de la industria para detectar el peso del dinero profesional — si su cuota se mueve, es porque flujo de apuestas sharp ya entró. Este movimiento reemplaza al "% de apuestas públicas" (que no tiene fuente gratuita confiable) como señal de flujo de dinero. Requiere que la app haya guardado un snapshot anterior de la misma consulta en la sesión (vía `st.session_state`); si no hay snapshot previo disponible, omitir esta señal sin que cuente como filtro de descarte — no es bloqueante, es un dato adicional cuando está disponible.

3. Filtro de madurez por deporte
   - MLB: lineup oficial, 3–4h antes.
   - NBA/NHL/WNBA: lineup confirmado, 1–2h antes.
   - Fútbol: alineación oficial, 1h antes.
   - Tenis: order of play + jugador en cancha, 12–18h antes.
   - UFC/Boxeo: pesaje realizado, 24h antes.
   - F1/Ciclismo: parrilla confirmada.
   Verificar vía búsqueda web. Si falta dato clave → marcar como "pendiente" y descartar como candidato.

4. Filtro de rango de cuota justa
   - Solo se consideran candidatos con cuota Pinnacle (campo `price` del bookmaker `pinnacle` en el JSON) entre 1.40 y 2.00.
   - Fuera de ese rango, descartar sin excepción.

5. Cálculo cuantitativo
   - Prob. implícita de Pinnacle = 1 / cuota decimal, de-vigged (normalizada dividiendo por la suma de probabilidades implícitas de todos los resultados del mercado).
   - Prob. Modelo: del segundo modelo validado en la sección de METODOLOGÍA.
   - Edge = Prob. Modelo − Prob. Mercado.
   - EV = (Prob. Modelo × Cuota decimal) − 1.
   - Umbral elevado: descartar candidatos con EV <5%.
   - EV >8% = sospechoso → verificar dos veces antes de considerarlo.
   - Nivel de confianza (1–10) — CALCULADO CON FÓRMULA EXPLÍCITA, nunca asignado subjetivamente. Sumar los siguientes puntos:
     * Fuentes coincidentes (Pinnacle + segundo modelo + contexto confirmado como lineup/lesión): 3 fuentes = 2 pts, 4 fuentes = 2.5 pts, 5+ fuentes = 3 pts. Mínimo 3 fuentes es obligatorio para continuar; si hay menos de 3, confianza = 0 automáticamente y se descarta.
     * Divergencia entre Pinnacle y el segundo modelo (ya calculada en la validación cruzada): <2% = 3 pts, 2–4% = 2 pts, 4–7% = 1 pt. (>7% ya fue descartado antes de llegar aquí.)
     * Liquidez (según la definición del punto anterior): Alta = 2 pts, Media = 1 pt.
     * Frescura de la cuota (timestamp `last_update` de Pinnacle vs hora actual): menos de 15 min = 1 pt, 15–30 min = 0.5 pts, 30–60 min = 0 pts (y se marca "requiere revalidación").
     * Bonus de precisión: si TODOS los componentes anteriores están en su nivel máximo simultáneamente (5+ fuentes, divergencia <2%, liquidez Alta, frescura <15 min), +1 pt adicional.
     Suma total = Nivel de confianza (máximo 10). Solo calificar como pick final si el total es >=8 — lo cual, por diseño de esta fórmula, solo ocurre cuando casi todos los componentes están en su mejor nivel simultáneamente. Esto es intencional: refuerza que la mayoría de los días el resultado sea 0 picks.
     Mostrar el desglose completo de esta suma en el informe final (no solo el número), para que el usuario pueda auditar cómo se llegó a la confianza reportada.
   - Indicador de liquidez (Alta/Media/Baja) — CALCULADO DIRECTAMENTE DEL JSON, sin fuente adicional. IMPORTANTE: Stake.com no tiene API pública oficial y Bet365 restringe el acceso a terceros — es NORMAL y ESPERADO que solo Pinnacle responda en la mayoría de las consultas; esto NO es una señal de mercado ilíquido, es una limitación estructural de la fuente de datos. Regla ajustada:
     * Alta: 3 bookmakers o más reportan el mercado en el JSON, con dispersión de probabilidad implícita entre ellos menor al 5%.
     * Media: 2 bookmakers reportan el mercado, O solo Pinnacle reporta pero el evento pertenece a una liga/torneo de primer nivel donde Pinnacle acepta límites altos por defecto (ej. MLB, NBA, NFL, NHL, ATP/WTA nivel principal, Champions League, ligas top europeas, Copa Libertadores/Sudamericana en fase avanzada) — el propio modelo de negocio de Pinnacle (límites altos, apuestas de sharps) ya actúa como filtro indirecto de liquidez real del mercado subyacente.
     * Baja: solo Pinnacle reporta Y el evento es de una liga/torneo menor, de bajo perfil o con escaso volumen conocido (ej. ligas menores, fases preliminares, torneos amateurs).
     Descartar el candidato solo si la liquidez es Baja bajo esta definición ajustada.
   - Anti-sesgo de disponibilidad — No priorizar ligas grandes por cobertura mediática; selección basada en edge real.

6. Reglas de desempate (si dos+ candidatos cumplen todos los filtros el mismo día)
   - Mayor liquidez (según el cálculo del paso 5).
   - Menor correlación con la apuesta anterior (mismo deporte/liga/mercado reciente).
   - Cuota más cercana al centro del rango justo (~1.60–1.70).

7. Selección final
   - Un único pick, el de mejor EV/confianza/liquidez dentro del rango de cuota justa, que haya sobrevivido TODOS los filtros anteriores (madurez, rango de cuota, EV>=5%, confianza>=8, liquidez!=Baja, validación cruzada<=7% de diferencia).
   - Si ningún evento cumple todos los filtros → informe de 0 picks (resultado válido y esperado la mayoría de los días).
   - Señalar el candidato descartado más cercano y la razón exacta de su descarte (qué filtro específico no pasó).

8. Verificación pre-apuesta
   - Antes de confirmar el pick, recomendar al usuario correr la app de nuevo para refrescar la cuota de Pinnacle/Stake y comparar contra la cuota original del análisis; notificar si ya se movió >3%.
   - Si el evento se cancela o pospone después de emitido el pick, marcarlo como "anulado — no cuenta para el registro" (no se contabiliza como win/loss).

9. Staking
   - Banca fija de $50, apuesta única — no aplica Kelly fraccionario tradicional (diseñado para series repetidas).
   - Indicar explícitamente si el edge/confianza justifica apostar el 100% de los $50, o si conviene apostar una porción menor (50–75%) dejando colchón, según solidez del pick.
   - Justificar la recomendación de stake con una frase breve.

10. Gestión de banca y continuidad
    - Reinversión: si el pick gana, la siguiente apuesta usa el 100% del capital acumulado (stake + ganancia), no se retira nada. El informe debe indicar siempre el monto exacto disponible antes de calcular el stake sugerido del paso 9.
    - Advertencia de interés compuesto en reversa: reinvertir el total también acelera las pérdidas si vienen rachas negativas.
    - Racha de pérdidas: si ocurren 3 pérdidas consecutivas, pausa obligatoria del sistema — no seguir apostando en automático hasta revisar la metodología.
    - Regla anti-tilt: si el pick anterior perdió, NO se reduce el umbral de EV/confianza requerido ni se fuerza un pick antes de tiempo para "recuperar".
    - Cadencia: análisis diario. Se espera que la mayoría de los días el resultado sea 0 picks, y eso es una señal de que el filtro está funcionando.

11. Registro y calibración
    - Fecha, evento, cuota tomada, prob. modelo, EV, stake, resultado. NOTA: el usuario registrará este pick en el Tracker oficial de la app.
    - Al cierre: CLV = cuota de Pinnacle tomada al momento del pick vs cuota de Pinnacle unos minutos antes del inicio del evento (correr la app de nuevo cerca del inicio para capturar este dato).
    - Revisión cada 10 picks: comparar winrate real vs winrate implícito por las cuotas. Si el sistema pierde sistemáticamente más de lo esperado, es señal de fallo de calibración, no varianza — pausar y revisar metodología.
    - Auditoría de fallos — si el pick pierde, documentar causa probable (dato erróneo, varianza, fallo de calibración).

NOTA DE EXPECTATIVA REALISTA (incluir siempre en el resumen ejecutivo)
Esta selección maximiza la probabilidad de acierto individual, pero una sola apuesta no valida ni invalida el modelo estadístico — eso requiere muestra (100+ apuestas). Un acierto no confirma que "el sistema funciona" y una pérdida no confirma que "el sistema falló"; ninguna conclusión de ese tipo es válida con n=1.

FORMATO DE ENTREGA
📊 Resumen ejecutivo (1–2 líneas: hay pick o no, y por qué) + nota de expectativa realista.
🏆 Ficha única del pick (si existe):
| Deporte/Liga | Evento | Selección | Cuota | Prob. Mercado | Prob. Modelo | Edge | EV | Confianza | Liquidez | Stake sugerido | Fuente/Hora |
🔍 Análisis del pick (hora RD, estadísticas clave, bajas, ajustes aplicados, fuentes del 2º modelo con URL exacta consultada).
⏳ Si no hay pick: nota de qué candidato estuvo más cerca y el filtro exacto que no pasó.
⚠️ Alerta de riesgo si aplica (cuota vencida/movida, evento cancelado, liquidez baja, gap de fuente sin verificar como WNBA-URL/LoL).
📈 Línea de registro para tracking (copiable, para pegar en hoja de cálculo externa).
📌 "Las apuestas conllevan riesgo. Juega responsablemente."
⏱️ Horario recomendado: 11 AM–1 PM RD o 4 PM–6 PM RD."""

# ---------------------------------------------------------
# 3. FUNCIONES DE API Y DE DELTAS
# ---------------------------------------------------------

@st.cache_data(ttl=1800)
def obtener_deportes_disponibles():
    if not ODDS_API_KEY:
        return []
    url = "https://api.the-odds-api.com/v4/sports/"
    params = {'apiKey': ODDS_API_KEY}
    try:
        res = requests.get(url, params=params)
        if res.status_code == 200:
            return res.json()
    except Exception:
        pass
    return []

def obtener_cuotas(deporte_key="upcoming"):
    url = f"https://api.the-odds-api.com/v4/sports/{deporte_key}/odds/"
    params = {
        'apiKey': ODDS_API_KEY,
        'regions': 'us,eu,us2',
        'markets': 'h2h',
        'bookmakers': 'pinnacle,stake,betonlineag,bet365',
        'oddsFormat': 'decimal'
    }
    response = requests.get(url, params=params)
    if response.status_code == 200:
        return response.json(), response.headers.get('x-requests-remaining', 'N/A')
    return None, f"Error {response.status_code}: {response.text}"

def procesar_snapshots_y_deltas(eventos, hora_actual_str):
    eventos_procesados = []
    for evento in eventos:
        evento_id = evento.get("id")
        pinnacle_data = next((bm for bm in evento.get("bookmakers", []) if bm.get("key") == "pinnacle"), None)
        delta_info = {"status": "Primer snapshot en esta sesión"}
        
        if pinnacle_data:
            cuotas_actuales = {}
            for market in pinnacle_data.get("markets", []):
                if market.get("key") == "h2h":
                    for outcome in market.get("outcomes", []):
                        cuotas_actuales[outcome.get("name")] = outcome.get("price")
            
            if evento_id in st.session_state["pinnacle_snapshots"]:
                prev_snap = st.session_state["pinnacle_snapshots"][evento_id]
                prev_cuotas = prev_snap.get("cuotas", {})
                deltas = {}
                for equipo, cuota_nueva in cuotas_actuales.items():
                    cuota_vieja = prev_cuotas.get(equipo)
                    if cuota_vieja:
                        diff = round(cuota_nueva - cuota_vieja, 3)
                        deltas[equipo] = {
                            "anterior": cuota_vieja,
                            "actual": cuota_nueva,
                            "variacion": f"{'+' if diff > 0 else ''}{diff}"
                        }
                delta_info = {
                    "status": "Movimiento detectado en sesión",
                    "captura_anterior": prev_snap.get("timestamp"),
                    "deltas": deltas
                }
            
            st.session_state["pinnacle_snapshots"][evento_id] = {
                "timestamp": hora_actual_str,
                "cuotas": cuotas_actuales
            }
        
        evento["line_movement_tracking"] = delta_info
        eventos_procesados.append(evento)
    return eventos_procesados

# ---------------------------------------------------------
# 4. FUNCIONES DE PERSISTENCIA Y CÁLCULOS DEL TRACKER
# ---------------------------------------------------------

def load_picks():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_picks(picks):
    with open(DB_FILE, "w") as f:
        json.dump(picks, f, indent=2)

def calc_clv(cuota_tomada, cuota_cierre):
    if not cuota_tomada or not cuota_cierre or cuota_tomada <= 1 or cuota_cierre <= 1:
        return None
    p_tomada = 1.0 / cuota_tomada
    p_cierre = 1.0 / cuota_cierre
    return ((p_cierre - p_tomada) / p_tomada) * -100.0

# ---------------------------------------------------------
# 5. ESTRUCTURA DE NAVEGACIÓN EN PESTAÑAS (TABS)
# ---------------------------------------------------------

tab_prompt, tab_tracker = st.tabs(["🚀 Generador de Prompt (v3.0)", "📊 Tracker de Picks & CLV"])

# =========================================================
# TAB 1: GENERADOR DE PROMPT
# =========================================================
with tab_prompt:
    st.title("📊 Analista Cuantitativo de Deportes")
    st.caption("Filtro EV+ en tiempo real (Blindado v3.0 — Multi-Source Validated + Confidence Score Formula)")

    if not ODDS_API_KEY:
        st.error("⚠️ Registra tu 'ODDS_API_KEY' en los Secrets de Streamlit.")

    deportes_lista = obtener_deportes_disponibles()
    opciones_deporte = {"🌐 Todos los deportes (Próximos eventos)": "upcoming"}

    for dep in deportes_lista:
        nombre_legible = f"{dep.get('group', '')} - {dep.get('title', '')}"
        opciones_deporte[nombre_legible] = dep.get('key')

    deporte_seleccionado_nombre = st.selectbox(
        "Selecciona el deporte o liga a analizar:",
        options=list(opciones_deporte.keys())
    )

    deporte_key = opciones_deporte[deporte_seleccionado_nombre]

    if st.button("🚀 Obtener Cuotas y Generar Prompt", type="primary", use_container_width=True):
        with st.spinner(f"Consultando cuotas para {deporte_seleccionado_nombre}..."):
            datos, rest_o_error = obtener_cuotas(deporte_key)

        if datos is not None and isinstance(datos, list):
            if len(datos) == 0:
                st.warning("⚠️ La API respondió correctamente, pero hay 0 eventos disponibles actualmente. Prueba con otro deporte.")
            else:
                tz_rd = timezone(timedelta(hours=-4))
                hora_rd = datetime.now(tz_rd).strftime("%Y-%m-%d %I:%M %p (Hora RD)")
                datos_con_deltas = procesar_snapshots_y_deltas(datos, hora_rd)

                prompt_completo = (
                    f"{SYSTEM_PROMPT}\n\n---\n"
                    f"DEPORTE FILTRADO: {deporte_seleccionado_nombre}\n"
                    f"FECHA/HORA DE LA CONSULTA: {hora_rd}\n\n"
                    f"DATOS RECOLECTADOS DE THE ODDS API EN TIEMPO REAL (JSON CON DELTAS DE SESIÓN):\n"
                    f"{json.dumps(datos_con_deltas, indent=2)}"
                )

                st.success(f"✓ Cuotas descargadas ({len(datos)} eventos). Consultas restantes en API: **{rest_o_error}**")
                st.subheader("1. Copia el Prompt Generado")
                st.code(prompt_completo, language="text")

                st.divider()
                st.subheader("2. Abre tu IA para analizar")
                col1, col2, col3 = st.columns(3)
                with col1:
                    st.link_button("Abrir ChatGPT ↗", "https://chatgpt.com", use_container_width=True)
                with col2:
                    st.link_button("Abrir Claude ↗", "https://claude.ai", use_container_width=True)
                with col3:
                    st.link_button("Abrir Copilot ↗", "https://copilot.microsoft.com", use_container_width=True)
        else:
            st.error(f"Error al obtener cuotas: {rest_o_error}")

# =========================================================
# TAB 2: TRACKER DE PICKS & CLV (PERSISTENTE)
# =========================================================
with tab_tracker:
    st.title("📟 Registro y Calibración CLV")
    st.caption("Terminal Cuantitativa Persistente — Guardado Automático en `picks_db.json`")

    picks = load_picks()

    # Cálculo de métricas globales
    resueltos = [p for p in picks if p.get("resultado") in ["ganado", "perdido"]]
    ganados = [p for p in resueltos if p.get("resultado") == "ganado"]
    winrate_real = (len(ganados) / len(resueltos)) if resueltos else None

    probs_imp = [1.0 / p["cuota"] for p in resueltos if p.get("cuota") and p["cuota"] > 1]
    winrate_imp = (sum(probs_imp) / len(probs_imp)) if probs_imp else None

    banca_inicial = 50.0
    banca_actual = banca_inicial
    for p in reversed(picks):
        res = p.get("resultado")
        stk = p.get("stake", 0)
        quo = p.get("cuota", 0)
        if res == "ganado":
            banca_actual = banca_actual - stk + (stk * quo)
        elif res == "perdido":
            banca_actual = banca_actual - stk

    clvs = [calc_clv(p.get("cuota"), p.get("cuota_cierre")) for p in picks if p.get("cuota_cierre")]
    clvs_validos = [c for c in clvs if c is not None]
    clv_promedio = (sum(clvs_validos) / len(clvs_validos)) if clvs_validos else None

    # Panel de Métricas
    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        st.markdown(f'<div class="stat-card"><div class="stat-label">Banca Actual</div><div class="stat-value" style="color:{"#1FD98A" if banca_actual>=banca_inicial else "#FF5C5C"}">${banca_actual:.2f}</div></div>', unsafe_allow_html=True)
    with col2:
        st.markdown(f'<div class="stat-card"><div class="stat-label">Winrate Real</div><div class="stat-value">{(winrate_real*100):.1f}%</div></div>' if winrate_real is not None else '<div class="stat-card"><div class="stat-label">Winrate Real</div><div class="stat-value">—</div></div>', unsafe_allow_html=True)
    with col3:
        st.markdown(f'<div class="stat-card"><div class="stat-label">Winrate Implícito</div><div class="stat-value">{(winrate_imp*100):.1f}%</div></div>' if winrate_imp is not None else '<div class="stat-card"><div class="stat-label">Winrate Implícito</div><div class="stat-value">—</div></div>', unsafe_allow_html=True)
    with col4:
        st.markdown(f'<div class="stat-card"><div class="stat-label">CLV Promedio</div><div class="stat-value" style="color:{"#1FD98A" if (clv_promedio or 0)>=0 else "#FF5C5C"}">{"+" if (clv_promedio or 0)>=0 else ""}{clv_promedio:.1f}%</div></div>' if clv_promedio is not None else '<div class="stat-card"><div class="stat-label">CLV Promedio</div><div class="stat-value">—</div></div>', unsafe_allow_html=True)
    with col5:
        st.markdown(f'<div class="stat-card"><div class="stat-label">Resueltos (G / P)</div><div class="stat-value">{len(ganados)} / {len(resueltos)-len(ganados)}</div></div>', unsafe_allow_html=True)

    st.write("")

    # Alertas de Calibración del Paso 11
    if winrate_real is not None and winrate_imp is not None and len(resueltos) >= 10 and winrate_real < (winrate_imp - 0.10):
        st.error(f"🚨 **Alerta de Fallo de Calibración:** Tu winrate real ({(winrate_real*100):.1f}%) está más de 10 puntos por debajo del implícito ({(winrate_imp*100):.1f}%) tras {len(resueltos)} picks. El Paso 11 exige pausar el sistema y revisar la metodología.")

    if len(resueltos) > 0 and len(resueltos) % 10 == 0:
        st.info(f"🔍 **Punto de Control Alcanzado ({len(resueltos)} picks resueltos):** Revisa si tu winrate sostiene el EV esperado antes de realizar la siguiente apuesta.")

    # Formulario para agregar nuevo Pick
    with st.expander("➕ Registrar Nuevo Pick Recomendado", expanded=False):
        with st.form("form_add_pick"):
            fc1, fc2, fc3 = st.columns(3)
            with fc1:
                fecha = st.date_input("Fecha", datetime.now()).strftime("%Y-%m-%d")
                deporte = st.text_input("Deporte / Liga", placeholder="MLB")
                evento = st.text_input("Evento", placeholder="Cubs vs White Sox")
            with fc2:
                seleccion = st.text_input("Selección", placeholder="Cubs ML")
                cuota = st.number_input("Cuota Tomada (Pinnacle/Stake)", min_value=1.01, step=0.01, value=1.75)
                prob_modelo = st.number_input("Prob. Modelo (ej. 0.62)", min_value=0.0, max_value=1.0, step=0.01, value=0.60)
            with fc3:
                ev = st.number_input("EV % (ej. 5.5)", step=0.1, value=5.0)
                confianza = st.number_input("Confianza (1-10)", min_value=1, max_value=10, value=8)
                stake = st.number_input("Stake ($)", min_value=1.0, step=5.0, value=50.0)

            submitted = st.form_submit_button("Guardar Pick en DB", type="primary")
            if submitted:
                nuevo_pick = {
                    "id": str(int(datetime.now().timestamp())),
                    "fecha": fecha,
                    "deporte": deporte,
                    "evento": evento,
                    "seleccion": seleccion,
                    "cuota": float(cuota),
                    "prob_modelo": float(prob_modelo),
                    "ev": float(ev),
                    "confianza": int(confianza),
                    "stake": float(stake),
                    "resultado": "pendiente",
                    "cuota_cierre": None
                }
                picks.insert(0, nuevo_pick)
                save_picks(picks)
                st.success("Pick guardado exitosamente.")
                st.rerun()

    # Tabla interactiva de Picks Registrados
    st.subheader("📋 Historial de Registros")
    if not picks:
        st.caption("No hay picks en la base de datos. Cuando el prompt te entregue un pick, regístralo aquí.")
    else:
        for p in picks:
            pid = p["id"]
            res_actual = p.get("resultado", "pendiente")
            color_borde = "#1FD98A" if res_actual == "ganado" else "#FF5C5C" if res_actual == "perdido" else "#7A8590"

            with st.container():
                st.markdown(f"""
                <div class="pick-card" style="border-left: 4px solid {color_borde};">
                    <b>{p['fecha']} · {p['deporte']}</b> — {p['evento']} | <i>{p['seleccion']} @ {p['cuota']:.2f}</i><br>
                    <small style="color: #7A8590;">EV: {p['ev']}% | Confianza: {p['confianza']}/10 | Stake: ${p['stake']:.2f}</small>
                </div>
                """, unsafe_allow_html=True)

                rc1, rc2, rc3 = st.columns([2, 2, 1])
                with rc1:
                    nuevo_res = st.selectbox(
                        "Resultado",
                        options=["pendiente", "ganado", "perdido", "anulado"],
                        index=["pendiente", "ganado", "perdido", "anulado"].index(res_actual),
                        key=f"res_{pid}"
                    )
                    if nuevo_res != res_actual:
                        p["resultado"] = nuevo_res
                        save_picks(picks)
                        st.rerun()

                with rc2:
                    c_cierre_val = p.get("cuota_cierre") or 0.0
                    nueva_cierre = st.number_input(
                        "Cuota de Cierre Pinnacle (CLV)",
                        min_value=0.0,
                        step=0.01,
                        value=float(c_cierre_val),
                        key=f"cierre_{pid}"
                    )
                    if nueva_cierre > 0 and nueva_cierre != c_cierre_val:
                        p["cuota_cierre"] = nueva_cierre
                        save_picks(picks)
                        st.rerun()

                with rc3:
                    clv_item = calc_clv(p.get("cuota"), p.get("cuota_cierre"))
                    if clv_item is not None:
                        col_clv = "green" if clv_item >= 0 else "red"
                        st.markdown(f"**CLV:** :{col_clv}[{'+' if clv_item>=0 else ''}{clv_item:.2f}%]")
                    if st.button("🗑️", key=f"del_{pid}"):
                        picks = [x for x in picks if x["id"] != pid]
                        save_picks(picks)
                        st.rerun()
            st.divider()
