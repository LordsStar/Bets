import json
import streamlit as st

def devig_probabilidades(outcomes):
    """
    Recibe los outcomes de un mercado h2h y devuelve un diccionario con 
    las probabilidades implícitas de-vigged (normalizadas a suma 1.0).
    """
    implicitas = {o["name"]: 1.0 / o["price"] for o in outcomes if o.get("price") and o["price"] > 0}
    overround = sum(implicitas.values())
    
    if overround == 0:
        return {}
    
    return {nombre: round(p / overround, 4) for nombre, p in implicitas.items()}


def calcular_dispersion_mercado(evento):
    """
    Calcula la máxima diferencia de probabilidad implícita entre distintas 
    casas de apuestas para un mismo evento (mide la coherencia del mercado).
    """
    home_team = evento.get("home_team")
    probs_home = []

    for b in evento.get("bookmakers", []):
        h2h = next((m for m in b.get("markets", []) if m["key"] == "h2h"), None)
        if h2h:
            devig = devig_probabilidades(h2h.get("outcomes", []))
            if home_team in devig:
                probs_home.append(devig[home_team])

    if len(probs_home) < 2:
        return 0.0

    return max(probs_home) - min(probs_home)


def registrar_y_calcular_movimientos(eventos, deporte_key):
    """
    Compara las cuotas actuales de Pinnacle contra la consulta previa en st.session_state
    para detectar movimientos de dinero inteligente.
    """
    state_key = f"pinnacle_snapshot_{deporte_key}"
    movimientos = {}
    snapshot_actual = {}

    for ev in eventos:
        ev_id = ev.get("id")
        pinnacle = next((b for b in ev.get("bookmakers", []) if b["key"] == "pinnacle"), None)
        if pinnacle:
            h2h = next((m for m in pinnacle.get("markets", []) if m["key"] == "h2h"), None)
            if h2h:
                snapshot_actual[ev_id] = {
                    "matchup": f"{ev.get('home_team')} vs {ev.get('away_team')}",
                    "prices": {o["name"]: o["price"] for o in h2h.get("outcomes", []) if o.get("price")}
                }

    # Comparar con el snapshot guardado en la sesión
    if state_key in st.session_state:
        snapshot_previo = st.session_state[state_key]
        for ev_id, data_curr in snapshot_actual.items():
            if ev_id in snapshot_previo:
                data_prev = snapshot_previo[ev_id]
                for team, price_curr in data_curr["prices"].items():
                    price_prev = data_prev["prices"].get(team)
                    if price_prev and price_prev != price_curr:
                        pct_change = round(((price_curr - price_prev) / price_prev) * 100, 2)
                        direccion = "subió" if pct_change > 0 else "bajó"
                        movimientos[f"{data_curr['matchup']} ({team})"] = (
                            f"Cuota cambió de {price_prev} a {price_curr} ({direccion} {abs(pct_change)}%)"
                        )

    # Actualizar la memoria de sesión
    st.session_state[state_key] = snapshot_actual
    return movimientos


def filtrar_y_enriquecer(datos_crudos):
    """
    Filtra eventos sin Pinnacle o fuera del rango 1.40-2.00, y enriquece los 
    eventos válidos con metadatos calculados en Python.
    """
    eventos_validos = []
    descartados_sin_pinnacle = 0
    descartados_fuera_de_rango = 0

    for evento in datos_crudos:
        pinnacle = next((b for b in evento.get("bookmakers", []) if b.get("key") == "pinnacle"), None)
        if not pinnacle:
            descartados_sin_pinnacle += 1
            continue

        h2h = next((m for m in pinnacle.get("markets", []) if m.get("key") == "h2h"), None)
        if not h2h:
            descartados_sin_pinnacle += 1
            continue

        outcomes = h2h.get("outcomes", [])
        en_rango = any(1.40 <= o.get("price", 0) <= 2.00 for o in outcomes)
        if not en_rango:
            descartados_fuera_de_rango += 1
            continue

        # Crear copia enriquecida
        evento_enriquecido = dict(evento)
        evento_enriquecido["_pinnacle_devig"] = devig_probabilidades(outcomes)
        evento_enriquecido["_pinnacle_last_update"] = pinnacle.get("last_update")

        # Evaluación cuantitativa de liquidez
        n_bookmakers = len(evento.get("bookmakers", []))
        dispersion = calcular_dispersion_mercado(evento)
        
        if n_bookmakers >= 3 and dispersion < 0.05:
            evento_enriquecido["_liquidez_backend"] = "Alta"
        elif n_bookmakers >= 2:
            evento_enriquecido["_liquidez_backend"] = "Media"
        else:
            evento_enriquecido["_liquidez_backend"] = "Media/Baja — solo Pinnacle, el LLM debe evaluar según la categoría de liga"

        eventos_validos.append(evento_enriquecido)

    resumen_filtro = (
        f"Backend pre-filtró {len(datos_crudos)} eventos: "
        f"{len(eventos_validos)} candidatos calificados (cuota Pinnacle 1.40-2.00), "
        f"{descartados_sin_pinnacle} descartados sin cuota Pinnacle, "
        f"{descartados_fuera_de_rango} descartados fuera de rango."
    )
    return eventos_validos, resumen_filtro

# ----------------------------------------------------------------------
# BLOQUE PRINCIPAL DE INTEGRACIÓN EN STREAMLIT / APP
# ----------------------------------------------------------------------
# (Ejemplo de cómo enlazar con la llamada al modelo)

# 1. Obtener datos crudos de The Odds API
# datos = obtener_cuotas(deporte_seleccionado)

# 2. Filtrar y enriquecer en Python
eventos_filtrados, resumen_filtro = filtrar_y_enriquecer(datos)

# 3. Registrar movimiento de Pinnacle mediante session_state
movimientos_pinnacle = registrar_y_calcular_movimientos(eventos_filtrados, deporte_seleccionado)

if movimientos_pinnacle:
    lineas_mov = "\n".join(f"- {k}: {v}" for k, v in movimientos_pinnacle.items())
    seccion_movimiento = f"MOVIMIENTOS EN PINNACLE (SNAPSHOT EN SESIÓN):\n{lineas_mov}"
else:
    seccion_movimiento = "SIN SNAPSHOT PREVIO EN ESTA SESIÓN (Primera consulta realizada)."

# 4. Construir el prompt optimizado v3.0
prompt_completo = (
    f"{SYSTEM_PROMPT_BLINDADO_V3}\n\n"
    f"==================================================\n"
    f"CONTEXTO DE EJECUCIÓN DEL BACKEND\n"
    f"==================================================\n"
    f"DEPORTE: {deporte_seleccionado_nombre}\n"
    f"HORA LOCAL CONSULTA (RD/UTC-4): {hora_rd}\n\n"
    f"RESUMEN DE PRE-FILTRADO:\n{resumen_filtro}\n\n"
    f"{seccion_movimiento}\n\n"
    f"INSTRUCCIÓN TÉCNICA: Utiliza directamente los campos `_pinnacle_devig`, `_pinnacle_last_update` "
    f"y `_liquidez_backend`. No recalcules el de-vig ni filtres por rango nuevamente.\n\n"
    f"DATOS JSON PRE-FILTRADOS Y ENRIQUECIDOS:\n"
    f"{json.dumps(eventos_filtrados, indent=2, ensure_ascii=False)}"
)

# 5. Salida temprana en interfaz si no hay candidatos
if not eventos_filtrados:
    st.info(f"ℹ️ {resumen_filtro}\n\nNo hay apuestas candidatas dentro del rango 1.40 - 2.00 para hoy. No se realizó llamada al LLM.")
else:
    # Proceder con la llamada al modelo Gemini
    # respuesta = modelo.generate_content(prompt_completo)
    pass
