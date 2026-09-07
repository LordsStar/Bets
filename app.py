import json
import os
import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
import streamlit as st


# ============================================================
# STAKE-FIRST / FREE-SOURCES / BLINDADO
# ============================================================
# No The Odds API.
# No API-Sports.
# No paid sports-data API is required.
#
# Primary execution market:
#   Stake.com GraphQL/public sports data.
#
# Secondary market reference:
#   Bovada public coupon endpoints.
#
# Free/public context sources:
#   Sports Reference / FBref
#   Baseball Savant
#   Tennis Abstract
#   UFC Stats
#   BoxRec
#   HLTV / VLR / Liquipedia / OpenDota / Oracle's Elixir / Octane
#   additional source URLs are registered below.
#
# The app deliberately refuses to manufacture a probability when
# the free-data layer cannot verify enough information.
# ============================================================


APP_VERSION = "5.0-stake-first-free"
UTC = timezone.utc
BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
STATE_DIR.mkdir(exist_ok=True)
STAKE_SNAPSHOT_FILE = STATE_DIR / "stake_snapshots.json"
BOVADA_SNAPSHOT_FILE = STATE_DIR / "bovada_snapshots.json"
PROMOTIONS_FILE = STATE_DIR / "promotions.json"
ELO_FILE = STATE_DIR / "elo_state.json"

STAKE_GRAPHQL_URL = "https://stake.com/_api/graphql"
DEFAULT_TIMEOUT = 20

STAKE_SPORT_SLUGS = [
    "football", "basketball", "baseball", "ice-hockey", "tennis",
    "american-football", "mma", "boxing", "cricket", "rugby",
    "volleyball", "table-tennis", "formula-1", "cycling", "golf",
    "esports",
]

BOVADA_ENDPOINTS = {
    "baseball_mlb": "https://www.bovada.lv/services/sports/event/coupon/events/A/description/baseball/mlb",
    "basketball_nba": "https://www.bovada.lv/services/sports/event/coupon/events/A/description/basketball/nba",
    "icehockey_nhl": "https://www.bovada.lv/services/sports/event/coupon/events/A/description/hockey/nhl",
    "americanfootball_nfl": "https://www.bovada.lv/services/sports/event/coupon/events/A/description/football/nfl",
    "americanfootball_ncaaf": "https://www.bovada.lv/services/sports/event/coupon/events/A/description/football/college-football",
}

# Free/public sources. These are reference pages; not all expose a stable API.
FREE_SOURCES = {
    "mlb": [
        "https://baseballsavant.mlb.com/statcast_search",
        "https://www.fangraphs.com/",
        "https://www.baseball-reference.com/",
    ],
    "nba": [
        "https://www.basketball-reference.com/",
        "https://www.nba.com/stats/",
    ],
    "wnba": [
        "https://www.basketball-reference.com/wnba/",
        "https://stats.wnba.com/",
    ],
    "nfl": [
        "https://www.pro-football-reference.com/",
        "https://github.com/nflverse/nflfastR-data",
    ],
    "nhl": [
        "https://www.hockey-reference.com/",
        "https://www.nhl.com/stats/",
    ],
    "soccer": [
        "https://fbref.com/en/",
        "https://github.com/statsbomb/open-data",
    ],
    "tennis": [
        "https://www.tennisabstract.com/",
        "https://www.atptour.com/",
        "https://www.wtatennis.com/",
    ],
    "mma": [
        "http://ufcstats.com/",
        "https://www.sherdog.com/",
    ],
    "boxing": [
        "https://boxrec.com/",
    ],
    "f1": [
        "https://www.formula1.com/",
        "https://github.com/f1db/f1db",
    ],
    "cricket": [
        "https://cricsheet.org/",
        "https://www.espncricinfo.com/",
    ],
    "esports": [
        "https://liquipedia.net/",
        "https://www.hltv.org/",
        "https://www.vlr.gg/",
        "https://www.opendota.com/",
        "https://oracleselixir.com/",
        "https://octane.gg/",
    ],
    "other": [
        "https://www.espn.com/",
    ],
}


BLINDADO_PROMPT = """
PROMPT — Analista Cuantitativo de Apuesta Única (Blindado v4.1 — Stake First)

OBJETIVO:
Seleccionar COMO MÁXIMO UNA sola apuesta ejecutable en Stake.com.
Un resultado de 0 picks es válido y preferido cuando no existe evidencia suficiente.

REGLAS:
1. STAKE ES LA ANCLA DE EJECUCIÓN.
   La cuota final y el mercado recomendado deben existir realmente en Stake.
   EV se calcula con la cuota efectiva de Stake, incluyendo una promoción aplicable.

2. FUENTES DE MERCADO:
   Stake = mercado principal.
   Bovada = referencia secundaria.
   Ninguna cuota de Bovada sustituye una cuota de Stake.

3. DATOS GRATUITOS:
   Usar únicamente fuentes públicas registradas por deporte.
   No rellenar estadísticas faltantes con suposiciones.

4. FRESCURA:
   Si el precio de Stake no tiene timestamp o no puede verificarse su estado,
   el evento puede ser descartado.

5. SEGUNDO MODELO:
   Prioridad:
   a) modelo Elo interno calibrado con resultados reales suficientes;
   b) modelo de mercado Bovada de-vigged como referencia secundaria;
   c) si ninguno está disponible, DESCARTAR.
   No presentar una estimación de mercado como si fuera un modelo estadístico independiente.

6. EV:
   EV = P_modelo * (cuota_efectiva - 1) - (1 - P_modelo)
   Equivalente: EV = P_modelo * cuota_efectiva - 1.
   Umbral mínimo: 4%.

7. DIVERGENCIA:
   Si existe una probabilidad de referencia Bovada/Elo:
   |P_modelo - P_referencia| > 9 puntos porcentuales => DESCARTAR.

8. CONFIANZA:
   Debe ser >= 8/10.
   Considerar edge, calidad/frescura, liquidez, movimiento, datos deportivos y promociones.

9. TENIS / BOXEO / MMA:
   Buscar/confirmar estado físico reciente cuando se disponga de búsqueda web.
   Si no puede verificarse y el dato es crítico, no forzar el pick.

10. FÚTBOL:
   El moneyline 1X2 tiene riesgo de empate.
   Si P(draw) >= 30%, no recomendar ML puro salvo que exista un mercado DNB REAL en Stake
   y pueda calcularse con la cuota real de Stake. Nunca usar una cuota DNB estimada como si
   fuera una cuota ofrecida.

11. PROMOCIONES:
   Odds Boost: usar la cuota boost real.
   Refund/insurance: calcular valor esperado con la probabilidad del evento de refund.
   Bonos de cuenta: solo aplicar si el usuario los declara como elegibles.
   Nunca asumir que una promoción es universal.

12. ANTI-FABRICACIÓN:
   Si falta información crítica, DESCARTAR.
   No inventar cuotas, lesiones, alineaciones, estadísticas, resultados o promociones.

SALIDA:
PICK DEL DÍA: NINGUNO, o una única selección.
Mostrar: evento, mercado, Stake odds, probabilidad modelo, EV, confianza,
fuentes y motivo.
"""


# -----------------------------
# Generic utilities
# -----------------------------
def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            v = float(value)
            if v > 10_000_000_000:
                v /= 1000.0
            return datetime.fromtimestamp(v, UTC)
        except Exception:
            return None
    s = str(value).strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)
    except Exception:
        pass
    try:
        v = float(s)
        if v > 10_000_000_000:
            v /= 1000.0
        return datetime.fromtimestamp(v, UTC)
    except Exception:
        return None


def decimal_from_any(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", ".")
    if not s:
        return None
    # American odds SIEMPRE llevan signo explícito (+150, -200). Hay que
    # detectarlas ANTES de intentar float(s) directo: Python acepta el "+"
    # como prefijo válido en float("+150") == 150.0, lo que las confundiría
    # con una cuota decimal absurda de 150.0 en vez de la 2.5 real.
    m = re.fullmatch(r"([+-])\s*(\d+(?:\.\d+)?)", s)
    if m:
        n = float(m.group(2))
        if m.group(1) == "+":
            return 1.0 + n / 100.0
        if n:
            return 1.0 + 100.0 / n
        return None
    try:
        x = float(s)
        if x > 1.0:
            return x
    except Exception:
        pass
    return None


def devig(odds: Dict[str, float]) -> Dict[str, float]:
    clean = {k: float(v) for k, v in odds.items() if v and v > 1.0}
    if not clean:
        return {}
    raw = {k: 1.0 / v for k, v in clean.items()}
    total = sum(raw.values())
    return {k: p / total for k, p in raw.items()} if total else {}


def ev_decimal(prob: float, odds: float) -> float:
    return prob * odds - 1.0


def kelly_fraction(prob: float, odds: float) -> float:
    b = odds - 1.0
    if b <= 0:
        return 0.0
    q = 1.0 - prob
    return max(0.0, (b * prob - q) / b)


def stake_amount(
    bankroll: float,
    prob: float,
    odds: float,
    fraction: float = 0.25,
    min_pct: float = 0.005,
    max_pct: float = 0.05,
) -> float:
    k = kelly_fraction(prob, odds) * fraction
    k = min(max(k, min_pct), max_pct)
    return round(bankroll * k, 2)


def safe_get(d: Dict[str, Any], *keys: str, default=None):
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur


def save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


# -----------------------------
# HTTP
# -----------------------------
class HttpClient:
    def __init__(self, timeout: int = DEFAULT_TIMEOUT):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "StakeBlindado/5.0 (+public-data; respectful-rate-limit)",
            "Accept": "application/json,text/plain,*/*",
        })

    def get_json(self, url: str, params: Optional[dict] = None) -> Any:
        r = self.session.get(url, params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def post_json(self, url: str, payload: dict, headers: Optional[dict] = None) -> Any:
        h = {"Content-Type": "application/json"}
        if headers:
            h.update(headers)
        r = self.session.post(url, json=payload, headers=h, timeout=self.timeout)
        r.raise_for_status()
        return r.json()


# -----------------------------
# Stake collector
# -----------------------------
STAKE_SPORTS_QUERY = """
query SportsEvents($first: Int, $sportSlug: String) {
  sportsEvents(first: $first, sportSlug: $sportSlug) {
    edges {
      node {
        id
        name
        startTime
        sport { name slug }
        league { name slug }
        competitors { name }
        markets {
          name
          outcomes { name odds active }
        }
      }
    }
  }
}
"""


@dataclass
class NormalizedOutcome:
    selection: str
    odds: float
    active: bool = True
    point: Optional[float] = None


@dataclass
class NormalizedMarket:
    key: str
    name: str
    outcomes: List[NormalizedOutcome]


@dataclass
class NormalizedEvent:
    event_id: str
    source: str
    sport: str
    league: str
    home: str
    away: str
    start_time: Optional[str]
    is_live: bool
    status: str
    last_update: str
    markets: List[NormalizedMarket]
    raw: Dict[str, Any]


def stake_market_key(name: str) -> str:
    n = (name or "").lower()
    if any(x in n for x in ["winner", "moneyline", "match winner", "1x2", "3-way"]):
        return "moneyline"
    if "draw no bet" in n or n == "dnb":
        return "draw_no_bet"
    if "spread" in n or "handicap" in n:
        return "spread"
    if "total" in n or "over/under" in n:
        return "totals"
    return re.sub(r"[^a-z0-9]+", "_", n).strip("_") or "market"


class StakeCollector:
    def __init__(self, client: Optional[HttpClient] = None, delay: float = 1.05):
        self.client = client or HttpClient()
        self.delay = delay

    def fetch_sport(self, sport_slug: str, first: int = 100) -> List[NormalizedEvent]:
        payload = {
            "operationName": "SportsEvents",
            "variables": {"first": first, "sportSlug": sport_slug},
            "query": STAKE_SPORTS_QUERY,
        }
        data = self.client.post_json(STAKE_GRAPHQL_URL, payload)
        if data.get("errors"):
            raise RuntimeError(str(data["errors"]))
        edges = safe_get(data, "data", "sportsEvents", "edges", default=[]) or []
        events = [self.normalize_node(e.get("node", {})) for e in edges]
        time.sleep(self.delay)
        return [e for e in events if e.event_id]

    def fetch_all(self, slugs: Optional[Iterable[str]] = None) -> List[NormalizedEvent]:
        slugs = list(slugs or STAKE_SPORT_SLUGS)
        all_events = []
        for slug in slugs:
            try:
                all_events.extend(self.fetch_sport(slug))
            except Exception as exc:
                st.warning(f"Stake: no se pudo consultar {slug}: {exc}")
        return dedupe_events(all_events)

    @staticmethod
    def normalize_node(node: Dict[str, Any]) -> NormalizedEvent:
        competitors = node.get("competitors") or []
        names = [c.get("name", "") if isinstance(c, dict) else str(c) for c in competitors]
        home = names[0] if names else ""
        away = names[1] if len(names) > 1 else ""
        markets = []
        for m in node.get("markets") or []:
            if not isinstance(m, dict):
                continue
            outs = []
            for o in m.get("outcomes") or []:
                if not isinstance(o, dict):
                    continue
                odd = decimal_from_any(o.get("odds"))
                if odd and odd > 1:
                    outs.append(NormalizedOutcome(
                        selection=str(o.get("name", "")),
                        odds=odd,
                        active=bool(o.get("active", True)),
                    ))
            if outs:
                markets.append(NormalizedMarket(
                    key=stake_market_key(str(m.get("name", ""))),
                    name=str(m.get("name", "")),
                    outcomes=outs,
                ))
        sport = safe_get(node, "sport", "slug", default="other") or "other"
        league = safe_get(node, "league", "slug", default="") or safe_get(node, "league", "name", default="")
        start = parse_dt(node.get("startTime"))
        return NormalizedEvent(
            event_id=str(node.get("id", "")),
            source="stake",
            sport=str(sport),
            league=str(league or ""),
            home=home,
            away=away,
            start_time=start.isoformat() if start else None,
            is_live=False,
            status="scheduled",
            last_update=utc_now().isoformat(),
            markets=markets,
            raw=node,
        )


# -----------------------------
# Bovada collector
# -----------------------------
def _walk_dicts(obj: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_dicts(v)


def _find_event_dicts(payload: Any) -> List[Dict[str, Any]]:
    out = []
    seen = set()
    if isinstance(payload, dict):
        candidates = payload.get("events")
        if isinstance(candidates, list):
            for e in candidates:
                if isinstance(e, dict):
                    out.append(e)
    for d in _walk_dicts(payload):
        if "competitors" in d and ("startTime" in d or "lastModified" in d) and (
            "displayGroups" in d or "markets" in d or "description" in d
        ):
            ident = str(d.get("id", id(d)))
            if ident not in seen:
                seen.add(ident)
                out.append(d)
    return out


def _competitor_names(event: Dict[str, Any]) -> Tuple[str, str]:
    comps = event.get("competitors") or []
    names = []
    for c in comps:
        if isinstance(c, dict):
            names.append(
                c.get("name")
                or c.get("description")
                or c.get("shortName")
                or ""
            )
    if len(names) >= 2:
        home = next((c.get("name") or c.get("description") or c.get("shortName") for c in comps if c.get("home") is True), None)
        away = next((c.get("name") or c.get("description") or c.get("shortName") for c in comps if c.get("home") is False), None)
        if home and away:
            return str(home), str(away)
        return str(names[0]), str(names[1])
    desc = str(event.get("description", ""))
    parts = re.split(r"\s+@\s+|\s+vs\.?\s+|\s+-\s+", desc, maxsplit=1, flags=re.I)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return desc, ""


def _extract_bovada_markets(event: Dict[str, Any]) -> List[NormalizedMarket]:
    containers = []
    if isinstance(event.get("markets"), list):
        containers.extend(event["markets"])
    for group in event.get("displayGroups") or []:
        if not isinstance(group, dict):
            continue
        for key in ("markets", "itemList", "items"):
            val = group.get(key)
            if isinstance(val, list):
                containers.extend(val)
            elif isinstance(val, dict):
                items = val.get("items")
                if isinstance(items, list):
                    containers.extend(items)

    markets = []
    for c in containers:
        if not isinstance(c, dict):
            continue
        market_name = str(c.get("name") or c.get("description") or c.get("key") or "market")
        outcomes_raw = c.get("outcomes") or c.get("selections") or c.get("outcomeList")
        if isinstance(outcomes_raw, dict):
            outcomes_raw = outcomes_raw.get("items", [])
        if not isinstance(outcomes_raw, list):
            if any(k in c for k in ("price", "odds", "americanOdds")):
                outcomes_raw = [c]
            else:
                continue
        outs = []
        for o in outcomes_raw:
            if not isinstance(o, dict):
                continue
            name = str(
                o.get("name")
                or o.get("description")
                or o.get("label")
                or o.get("shortName")
                or ""
            )
            odd = decimal_from_any(
                o.get("odds")
                or o.get("price")
                or o.get("decimalOdds")
                or o.get("americanOdds")
                or o.get("american")
            )
            if name and odd and odd > 1:
                outs.append(NormalizedOutcome(selection=name, odds=odd, active=bool(o.get("active", True))))
        if outs:
            markets.append(NormalizedMarket(
                key=stake_market_key(market_name),
                name=market_name,
                outcomes=outs,
            ))
    return markets


class BovadaCollector:
    """
    Fortalecido (paso 5 del plan): sesión persistente con cabeceras completas
    de navegador, backoff exponencial con jitter en vez de sleep fijo, y
    manejo explícito de 403/429 que marca la fuente como no disponible en
    lugar de reventar toda la corrida.
    """

    def __init__(self, client: Optional[HttpClient] = None, base_delay: float = 1.2, max_retries: int = 3):
        self.client = client or HttpClient()
        self.client.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            "Referer": "https://www.bovada.lv/sports",
        })
        self.base_delay = base_delay
        self.max_retries = max_retries

    def fetch_league(self, sport_key: str) -> List[NormalizedEvent]:
        events, _ok = self.fetch_league_checked(sport_key)
        return events

    def fetch_league_checked(self, sport_key: str) -> Tuple[List[NormalizedEvent], bool]:
        """Igual que fetch_league, pero además indica si la fuente respondió
        con éxito (ok=True) aunque haya devuelto 0 eventos, o si falló por
        bloqueo/timeout (ok=False) — no son el mismo caso: 0 eventos puede
        ser simplemente que no hay partidos programados ahora mismo."""
        url = BOVADA_ENDPOINTS.get(sport_key)
        if not url:
            return [], True  # liga no registrada, no es una falla de red

        params = {"preMatchOnly": "true", "lang": "en"}
        payload = None
        for intento in range(self.max_retries):
            try:
                payload = self.client.get_json(url, params=params)
                break
            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status in (403, 429):
                    return [], False
                if intento < self.max_retries - 1:
                    time.sleep(self.base_delay * (2 ** intento))
                else:
                    return [], False
            except requests.exceptions.RequestException:
                if intento < self.max_retries - 1:
                    time.sleep(self.base_delay * (2 ** intento))
                else:
                    return [], False

        if payload is None:
            return [], False

        events = []
        for e in _find_event_dicts(payload):
            home, away = _competitor_names(e)
            start = parse_dt(e.get("startTime"))
            markets = _extract_bovada_markets(e)
            if not markets:
                continue
            events.append(NormalizedEvent(
                event_id=f"bovada:{e.get('id', '')}",
                source="bovada",
                sport=sport_key.split("_")[0],
                league=sport_key,
                home=home,
                away=away,
                start_time=start.isoformat() if start else None,
                is_live=bool(e.get("live", False)),
                status=str(e.get("status", "scheduled")),
                last_update=(parse_dt(e.get("lastModified")) or utc_now()).isoformat(),
                markets=markets,
                raw=e,
            ))
        time.sleep(self.base_delay)
        return dedupe_events(events), True

    def fetch_all(self, sport_keys: Optional[Iterable[str]] = None) -> Tuple[List[NormalizedEvent], List[str]]:
        """Devuelve (eventos, ligas_no_disponibles) — nunca lanza excepción.
        Una liga solo cuenta como 'no disponible' si la fuente falló
        (403/429/timeout agotado), NO simplemente porque no tuviera eventos
        programados en este momento."""
        keys = list(sport_keys or BOVADA_ENDPOINTS.keys())
        events: List[NormalizedEvent] = []
        no_disponibles: List[str] = []
        for key in keys:
            found, ok = self.fetch_league_checked(key)
            if not ok:
                no_disponibles.append(key)
            events.extend(found)
        return dedupe_events(events), no_disponibles


# -----------------------------
# Free-source registry / context
# -----------------------------
def sport_family(event: NormalizedEvent) -> str:
    s = (event.sport or "").lower()
    if s in ("baseball",):
        return "mlb"
    if s in ("basketball",):
        return "nba"
    if s in ("american-football", "americanfootball"):
        return "nfl"
    if s in ("ice-hockey", "hockey"):
        return "nhl"
    if s in ("football", "soccer"):
        return "soccer"
    if s in ("tennis",):
        return "tennis"
    if s in ("mma",):
        return "mma"
    if s in ("boxing",):
        return "boxing"
    if s in ("cricket",):
        return "cricket"
    if s in ("formula-1", "f1"):
        return "f1"
    if s.startswith("esports"):
        return "esports"
    return "other"


class FreeSourceRegistry:
    def sources_for(self, event: NormalizedEvent) -> List[str]:
        return FREE_SOURCES.get(sport_family(event), FREE_SOURCES["other"])


# -----------------------------
# Snapshot / line movement
# -----------------------------
def event_to_dict(e: NormalizedEvent) -> Dict[str, Any]:
    return {
        "event_id": e.event_id,
        "source": e.source,
        "sport": e.sport,
        "league": e.league,
        "home": e.home,
        "away": e.away,
        "start_time": e.start_time,
        "is_live": e.is_live,
        "status": e.status,
        "last_update": e.last_update,
        "markets": [
            {
                "key": m.key,
                "name": m.name,
                "outcomes": [asdict(o) for o in m.outcomes],
            }
            for m in e.markets
        ],
    }


def dedupe_events(events: List[NormalizedEvent]) -> List[NormalizedEvent]:
    seen = {}
    for e in events:
        key = (e.source, e.event_id) if e.event_id else (
            e.source, e.sport, e.home, e.away, e.start_time
        )
        seen[key] = e
    return list(seen.values())


def market_odds(event: NormalizedEvent, preferred=("moneyline", "draw_no_bet")) -> Dict[str, float]:
    for key in preferred:
        for m in event.markets:
            if m.key == key:
                return {o.selection: o.odds for o in m.outcomes if o.active and o.odds > 1}
    return {}


def snapshot_market_movements(events: List[NormalizedEvent], path: Path) -> Dict[str, Any]:
    previous = load_json(path, {})
    current = {}
    movements = []
    now = utc_now().isoformat()

    for e in events:
        odds = market_odds(e)
        if not odds:
            continue
        current[e.event_id] = {
            "timestamp": now,
            "home": e.home,
            "away": e.away,
            "odds": odds,
        }
        prev = previous.get(e.event_id, {})
        for sel, curr in odds.items():
            old = (prev.get("odds") or {}).get(sel)
            if old and curr != old:
                movements.append({
                    "event_id": e.event_id,
                    "match": f"{e.home} vs {e.away}",
                    "selection": sel,
                    "old": old,
                    "new": curr,
                    "pct": round((curr / old - 1) * 100, 2),
                    "direction": "up" if curr > old else "down",
                })
    save_json(path, current)
    return {"timestamp": now, "movements": movements}


# -----------------------------
# Matching Stake vs Bovada
# -----------------------------
def normalize_name(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\b(fc|cf|sc|bc|club|the)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def name_similarity(a: str, b: str) -> float:
    ta, tb = set(normalize_name(a).split()), set(normalize_name(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def match_event(stake_event: NormalizedEvent, references: List[NormalizedEvent]) -> Optional[NormalizedEvent]:
    best, best_score = None, 0.0
    for r in references:
        if sport_family(stake_event) != sport_family(r):
            continue
        score = (
            name_similarity(stake_event.home, r.home)
            + name_similarity(stake_event.away, r.away)
        ) / 2.0
        if stake_event.start_time and r.start_time:
            a, b = parse_dt(stake_event.start_time), parse_dt(r.start_time)
            if a and b:
                minutes = abs((a - b).total_seconds()) / 60
                if minutes <= 90:
                    score += 0.25
                elif minutes > 720:
                    score -= 0.25
        if score > best_score:
            best_score, best = score, r
    return best if best_score >= 0.35 else None


# -----------------------------
# Elo internal model
# -----------------------------
ELO_INITIAL = 1500.0
ELO_K = 20.0
ELO_HOME = 50.0
ELO_MIN_GAMES = 5
BRIER_MAX = 0.23
BRIER_MIN = 8


class EloModel:
    def __init__(self, path: Path = ELO_FILE):
        self.path = path
        self.state = load_json(path, {"ratings": {}, "brier": {}})

    @staticmethod
    def probability(home_elo: float, away_elo: float) -> float:
        return 1 / (1 + 10 ** ((away_elo - (home_elo + ELO_HOME)) / 400))

    def probability_for(self, sport: str, home: str, away: str) -> Optional[float]:
        ratings = self.state.get("ratings", {}).get(sport, {})
        h, a = ratings.get(home), ratings.get(away)
        if not h or not a:
            return None
        if h.get("games", 0) < ELO_MIN_GAMES or a.get("games", 0) < ELO_MIN_GAMES:
            return None
        hist = self.state.get("brier", {}).get(sport, [])
        if len(hist) < BRIER_MIN:
            return None
        brier = sum((x["p"] - x["y"]) ** 2 for x in hist) / len(hist)
        if brier > BRIER_MAX:
            return None
        return self.probability(float(h["elo"]), float(a["elo"]))

    def update(self, sport: str, home: str, away: str, home_win: float, game_id: str) -> None:
        ratings = self.state.setdefault("ratings", {}).setdefault(sport, {})
        processed = self.state.setdefault("processed", {}).setdefault(sport, [])
        if game_id in processed:
            return
        h = ratings.setdefault(home, {"elo": ELO_INITIAL, "games": 0})
        a = ratings.setdefault(away, {"elo": ELO_INITIAL, "games": 0})
        p = self.probability(h["elo"], a["elo"])
        h["elo"] += ELO_K * (home_win - p)
        a["elo"] += ELO_K * ((1 - home_win) - (1 - p))
        h["games"] += 1
        a["games"] += 1
        self.state.setdefault("brier", {}).setdefault(sport, []).append({"p": p, "y": home_win})
        processed.append(game_id)

    def save(self):
        save_json(self.path, self.state)


# -----------------------------
# Promotions
# -----------------------------
def load_promotions() -> List[Dict[str, Any]]:
    return load_json(PROMOTIONS_FILE, [])


def save_promotions(items: List[Dict[str, Any]]) -> None:
    save_json(PROMOTIONS_FILE, items)


def effective_odds(base_odds: float, promotion: Optional[Dict[str, Any]]) -> Tuple[float, str]:
    if not promotion:
        return base_odds, "sin promoción"
    kind = promotion.get("type")
    if kind == "odds_boost":
        boost = float(promotion.get("boost_percent", 0))
        return round(base_odds * (1 + boost / 100), 4), f"Odds Boost +{boost:.1f}%"
    return base_odds, str(promotion.get("name", "promoción no modelada"))


def promo_expected_value(prob: float, base_odds: float, promotion: Optional[Dict[str, Any]]) -> Tuple[float, float, str]:
    if not promotion:
        return base_odds, ev_decimal(prob, base_odds), "sin promoción"

    kind = promotion.get("type")
    if kind == "odds_boost":
        odds, label = effective_odds(base_odds, promotion)
        return odds, ev_decimal(prob, odds), label

    if kind in ("refund", "insurance"):
        refund_fraction = float(promotion.get("refund_fraction", 1.0))
        trigger_prob = float(promotion.get("trigger_probability", 0.0))
        p_loss = 1 - prob
        ev = prob * (base_odds - 1) + p_loss * trigger_prob * refund_fraction - p_loss
        return base_odds, ev, f"{promotion.get('name', 'Refund/Insurance')}"

    return base_odds, ev_decimal(prob, base_odds), "promoción no modelada; EV base"


# -----------------------------
# Blindado engine
# -----------------------------
@dataclass
class Candidate:
    event: NormalizedEvent
    selection: str
    stake_odds: float
    model_prob: float
    reference_prob: Optional[float]
    ev: float
    confidence: float
    promotion: Optional[Dict[str, Any]]
    effective_odds: float
    reason: str


class BlindadoEngine:
    MIN_ODDS = 1.40
    MAX_ODDS = 2.00
    MIN_EV = 0.04
    MAX_DIVERGENCE = 0.09
    MIN_CONFIDENCE = 8.0

    def __init__(self, bankroll: float = 100.0):
        self.bankroll = bankroll

    def _confidence(
        self,
        ev: float,
        reference_prob: Optional[float],
        model_prob: float,
        has_bovada: bool,
        has_stats: bool,
        movement_ok: bool,
    ) -> float:
        score = 5.0
        if ev >= 0.08:
            score += 2
        elif ev >= 0.04:
            score += 1
        if reference_prob is not None:
            div = abs(model_prob - reference_prob)
            if div <= 0.03:
                score += 1
            elif div > 0.07:
                score -= 1
        if has_bovada:
            score += 0.5
        if has_stats:
            score += 1
        if movement_ok:
            score += 0.5
        return max(0.0, min(10.0, score))

    def evaluate_event(
        self,
        event: NormalizedEvent,
        model_probs: Dict[str, float],
        bovada_event: Optional[NormalizedEvent] = None,
        promotions: Optional[List[Dict[str, Any]]] = None,
        stats_verified: bool = False,
        movement_ok: bool = True,
    ) -> List[Candidate]:
        market = next((m for m in event.markets if m.key in ("moneyline", "draw_no_bet")), None)
        if not market:
            return []

        ref_odds = market_odds(bovada_event) if bovada_event else {}
        ref_probs = devig(ref_odds)

        out = []
        for o in market.outcomes:
            if not o.active or not (self.MIN_ODDS <= o.odds <= self.MAX_ODDS):
                continue
            p = model_probs.get(o.selection)
            if p is None or not (0 < p < 1):
                continue

            promo = select_promotion(event, o.selection, promotions or [])
            eff_odds, ev, promo_label = promo_expected_value(p, o.odds, promo)
            if not (self.MIN_ODDS <= eff_odds <= self.MAX_ODDS):
                continue
            if ev < self.MIN_EV:
                continue

            ref_p = ref_probs.get(o.selection)
            if ref_p is not None and abs(p - ref_p) > self.MAX_DIVERGENCE:
                continue

            conf = self._confidence(
                ev, ref_p, p, bool(bovada_event), stats_verified, movement_ok
            )
            if conf < self.MIN_CONFIDENCE:
                continue

            out.append(Candidate(
                event=event,
                selection=o.selection,
                stake_odds=o.odds,
                model_prob=p,
                reference_prob=ref_p,
                ev=ev,
                confidence=conf,
                promotion=promo,
                effective_odds=eff_odds,
                reason=promo_label,
            ))
        return out

    def choose_one(self, candidates: List[Candidate]) -> Optional[Candidate]:
        if not candidates:
            return None
        return sorted(candidates, key=lambda c: (c.confidence, c.ev), reverse=True)[0]


def select_promotion(event: NormalizedEvent, selection: str, promotions: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    now = utc_now()
    for p in promotions:
        if not p.get("enabled", True):
            continue
        exp = parse_dt(p.get("expires_at"))
        if exp and exp < now:
            continue
        if p.get("event_id") and p["event_id"] != event.event_id:
            continue
        if p.get("selection") and normalize_name(p["selection"]) != normalize_name(selection):
            continue
        return p
    return None


# -----------------------------
# Model helpers
# -----------------------------
def model_from_market_consensus(stake_event: NormalizedEvent, bovada_event: Optional[NormalizedEvent]) -> Dict[str, float]:
    stake_odds = market_odds(stake_event)
    stake_p = devig(stake_odds)
    if bovada_event:
        bovada_p = devig(market_odds(bovada_event))
        names = set(stake_p) | set(bovada_p)
        combined = {}
        for n in names:
            vals = [p for p in (stake_p.get(n), bovada_p.get(n)) if p is not None]
            if vals:
                combined[n] = sum(vals) / len(vals)
        total = sum(combined.values())
        if total:
            return {k: v / total for k, v in combined.items()}
    return stake_p


def select_reference_model(
    event: NormalizedEvent,
    elo: EloModel,
    bovada_event: Optional[NormalizedEvent],
) -> Tuple[Dict[str, float], str, bool]:
    p_home = elo.probability_for(event.sport, event.home, event.away)
    if p_home is not None:
        return {
            event.home: p_home,
            event.away: 1 - p_home,
        }, "Elo interno calibrado", True

    if bovada_event:
        probs = model_from_market_consensus(event, bovada_event)
        return probs, "Referencia de mercado Stake+Bovada (NO modelo estadístico independiente)", False

    return {}, "sin segundo modelo", False


# -----------------------------
# Candidate preparation
# -----------------------------
def prepare_candidates(
    stake_events: List[NormalizedEvent],
    bovada_events: List[NormalizedEvent],
    promotions: List[Dict[str, Any]],
    bankroll: float,
) -> Tuple[List[Candidate], Dict[str, int]]:
    engine = BlindadoEngine(bankroll)
    elo = EloModel()
    stats = FreeSourceRegistry()
    candidates = []
    counts = {
        "stake_events": len(stake_events),
        "no_market": 0,
        "no_model": 0,
        "ev_or_divergence": 0,
        "qualified": 0,
    }

    for e in stake_events:
        if not e.start_time:
            continue
        bov = match_event(e, bovada_events)
        model, model_label, stats_verified = select_reference_model(e, elo, bov)
        if not model:
            counts["no_model"] += 1
            continue

        if sport_family(e) == "soccer":
            odds = market_odds(e)
            p_draw = next((p for n, p in devig(odds).items() if normalize_name(n) == "draw"), None)
            if p_draw is not None and p_draw >= 0.30:
                if not any(m.key == "draw_no_bet" for m in e.markets):
                    counts["ev_or_divergence"] += 1
                    continue

        before = len(candidates)
        cs = engine.evaluate_event(
            e,
            model,
            bovada_event=bov,
            promotions=promotions,
            stats_verified=stats_verified,
            movement_ok=True,
        )
        if not cs:
            counts["ev_or_divergence"] += 1
        candidates.extend(cs)
        if len(candidates) > before:
            counts["qualified"] += len(candidates) - before

    return candidates, counts


# -----------------------------
# UI / reports
# -----------------------------
def candidate_report(c: Candidate, bankroll: float) -> Dict[str, Any]:
    stake = stake_amount(bankroll, c.model_prob, c.effective_odds)
    return {
        "PICK": f"{c.event.home} vs {c.event.away}",
        "Mercado": c.selection,
        "Stake odds": round(c.stake_odds, 3),
        "Odds efectivas": round(c.effective_odds, 3),
        "Prob. modelo": round(c.model_prob * 100, 2),
        "Prob. referencia": None if c.reference_prob is None else round(c.reference_prob * 100, 2),
        "EV %": round(c.ev * 100, 2),
        "Confianza": round(c.confidence, 1),
        "Stake sugerido": stake,
        "Promoción": c.reason,
        "Fuente mercado": "Stake",
        "Referencia": "Bovada / Elo interno según disponibilidad",
    }


def build_prompt(events: List[NormalizedEvent], movements: Dict[str, Any]) -> str:
    payload = []
    for e in events:
        payload.append(event_to_dict(e))
    return (
        BLINDADO_PROMPT
        + "\n\nDATOS DE STAKE:\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n\nMOVIMIENTOS:\n"
        + json.dumps(movements, ensure_ascii=False, indent=2)
    )


def init_promotions_file():
    if not PROMOTIONS_FILE.exists():
        save_promotions([])


def main():
    st.set_page_config(page_title="Blindado v5 — Stake First", layout="wide")
    st.title("🎯 Blindado v5 — Stake First / Fuentes Gratuitas")
    st.caption(
        "Sin The Odds API · sin API-Sports · Stake = mercado ejecutable · Bovada = referencia · "
        "fuentes estadísticas públicas = contexto"
    )

    init_promotions_file()

    with st.sidebar:
        st.header("Configuración")
        bankroll = st.number_input("Bankroll USD", min_value=1.0, value=100.0, step=10.0)
        stake_delay = st.number_input("Delay Stake (seg)", min_value=0.5, value=1.05, step=0.05)
        bovada_enabled = st.checkbox("Usar Bovada como referencia", value=True)
        selected = st.multiselect(
            "Deportes Stake",
            STAKE_SPORT_SLUGS,
            default=STAKE_SPORT_SLUGS,
        )

        st.divider()
        st.subheader("Promociones Stake")
        st.caption("Se cargan desde state/promotions.json. No se asumen promociones universales.")
        if st.button("Recargar promociones"):
            st.rerun()

        promotions = load_promotions()
        st.write(f"Promociones activas configuradas: **{len(promotions)}**")

        with st.expander("Añadir Odds Boost"):
            event_id = st.text_input("Event ID (opcional)", key="promo_event")
            selection = st.text_input("Selección (opcional)", key="promo_selection")
            boost = st.number_input("Boost %", min_value=0.0, value=10.0, step=0.5)
            if st.button("Guardar boost"):
                promotions.append({
                    "enabled": True,
                    "type": "odds_boost",
                    "name": f"Stake Odds Boost +{boost}%",
                    "event_id": event_id or None,
                    "selection": selection or None,
                    "boost_percent": boost,
                })
                save_promotions(promotions)
                st.success("Boost guardado.")

        with st.expander("Añadir Refund/Insurance"):
            r_event = st.text_input("Event ID", key="refund_event")
            r_selection = st.text_input("Selección", key="refund_selection")
            r_name = st.text_input("Nombre", value="Stake Refund/Insurance")
            r_trigger = st.number_input(
                "Probabilidad del trigger de refund (0-1)",
                min_value=0.0, max_value=1.0, value=0.10, step=0.01
            )
            r_fraction = st.number_input(
                "Fracción del stake reembolsada",
                min_value=0.0, max_value=1.0, value=1.0, step=0.05
            )
            if st.button("Guardar refund"):
                promotions.append({
                    "enabled": True,
                    "type": "refund",
                    "name": r_name,
                    "event_id": r_event or None,
                    "selection": r_selection or None,
                    "trigger_probability": r_trigger,
                    "refund_fraction": r_fraction,
                })
                save_promotions(promotions)
                st.success("Refund guardado.")

        st.divider()
        st.write("### Fuentes gratuitas")
        fams = sorted(set(FREE_SOURCES.keys()))
        st.write(", ".join(fams))

    stake = StakeCollector(delay=float(stake_delay))
    bovada = BovadaCollector() if bovada_enabled else None

    if st.button("🚀 Buscar todos los deportes en Stake", type="primary"):
        with st.spinner("Consultando Stake y normalizando mercados..."):
            stake_events = stake.fetch_all(selected)
        st.session_state["stake_events"] = stake_events
        movement = snapshot_market_movements(stake_events, STAKE_SNAPSHOT_FILE)
        st.session_state["movement"] = movement
        st.success(f"Stake: {len(stake_events)} eventos normalizados.")
        if movement["movements"]:
            st.info(f"Movimientos detectados: {len(movement['movements'])}")

        bovada_events = []
        no_disponibles = []
        if bovada_enabled:
            with st.spinner("Consultando referencias Bovada..."):
                leagues = sorted({
                    f"{sport_family(e)}_{e.league.split('_')[-1]}"
                    for e in stake_events
                    if sport_family(e) in ("mlb", "nba", "nhl", "nfl")
                })
                keys_conocidas = [k for k in leagues if k in BOVADA_ENDPOINTS]
                bovada_events, no_disponibles = bovada.fetch_all(keys_conocidas)
        st.session_state["bovada_events"] = dedupe_events(bovada_events)
        st.success(f"Bovada: {len(st.session_state['bovada_events'])} eventos de referencia.")
        if no_disponibles:
            st.warning(
                f"Bovada no respondió para: {', '.join(no_disponibles)} "
                f"(bloqueo/timeout) — esos deportes seguirán sin referencia Bovada "
                f"en esta corrida, pero el resto del pipeline continúa."
            )

    stake_events = st.session_state.get("stake_events", [])
    bovada_events = st.session_state.get("bovada_events", [])
    movements = st.session_state.get("movement", {"movements": []})

    if stake_events:
        st.subheader("📊 Cobertura")
        cols = st.columns(4)
        cols[0].metric("Eventos Stake", len(stake_events))
        cols[1].metric("Eventos Bovada", len(bovada_events))
        cols[2].metric("Mercados Stake", sum(len(e.markets) for e in stake_events))
        cols[3].metric("Movimientos", len(movements.get("movements", [])))

        with st.expander("Fuentes estadísticas por deporte"):
            for e in stake_events[:30]:
                srcs = FreeSourceRegistry().sources_for(e)
                st.write(f"**{e.sport}/{e.league} — {e.home} vs {e.away}**")
                for src in srcs:
                    st.write(f"- {src}")

        if st.button("🧠 Ejecutar Blindado v5", type="primary"):
            promotions = load_promotions()
            candidates, counts = prepare_candidates(
                stake_events, bovada_events, promotions, float(bankroll)
            )
            engine = BlindadoEngine(float(bankroll))
            pick = engine.choose_one(candidates)

            st.subheader("🏆 Resultado")
            if pick is None:
                st.error("PICK DEL DÍA: NINGUNO")
                st.write(
                    "No se encontró una apuesta que cumpla simultáneamente "
                    "cuota, EV, divergencia, confianza y disponibilidad de mercado."
                )
            else:
                report = candidate_report(pick, float(bankroll))
                st.success("PICK DEL DÍA: 1 selección")
                st.json(report)

            st.subheader("🔎 Auditoría")
            st.json(counts)

        if st.button("📋 Generar prompt Blindado para IA"):
            prompt = build_prompt(stake_events, movements)
            st.session_state["prompt"] = prompt

    if "prompt" in st.session_state:
        st.subheader("📋 Prompt listo")
        st.code(st.session_state["prompt"], language="text")

    if st.checkbox("Mostrar eventos normalizados"):
        rows = []
        for e in stake_events:
            for m in e.markets:
                rows.append({
                    "sport": e.sport,
                    "league": e.league,
                    "event": f"{e.home} vs {e.away}",
                    "market": m.name,
                    "outcomes": ", ".join(f"{o.selection}: {o.odds:.2f}" for o in m.outcomes),
                    "start": e.start_time,
                })
        st.dataframe(rows, use_container_width=True)

    st.divider()
    st.caption(
        f"Blindado {APP_VERSION}. Los collectors dependen de interfaces públicas que pueden cambiar. "
        "No se utilizan técnicas de evasión de bloqueos ni automatización de apuestas."
    )


if __name__ == "__main__":
    main()
