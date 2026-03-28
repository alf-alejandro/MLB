"""
Polymarket — Partidos MLB del día
- Gamma API: eventos y mercados MLB
- CLOB API:  precios reales en paralelo (ThreadPoolExecutor)

Estructura real de los mercados MLB en Polymarket:
  - Evento nivel día: {"id":"21729", "title":"MLB Games: March 28", "markets":[...]}
  - Mercado por partido: {"question":"Will the Orioles beat the Blue Jays?",
                          "outcomes":"[\"Orioles\",\"Blue Jays\"]",    ← JSON string
                          "clobTokenIds":"[\"...\",\"...\"]",          ← JSON string
                          "outcomePrices":"[\"0.55\",\"0.45\"]",       ← JSON string
                          "active":true, "closed":false}
"""

import requests
import json
import re
from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

GAMMA_API      = "https://gamma-api.polymarket.com"
CLOB_API       = "https://clob.polymarket.com"
MLB_SERIES_ID  = 10062
HEADERS        = {"User-Agent": "Mozilla/5.0"}
SESSION        = requests.Session()
SESSION.headers.update(HEADERS)

# ── helpers ────────────────────────────────────────────────────────────────────

def hora_et() -> str:
    return datetime.utcnow().strftime("%H:%M UTC")

def centavos(p: float) -> str:
    return f"{p*100:+.1f}¢" if p is not None else "n/a"

def _parse_json_str(s) -> list:
    """Convierte JSON string o lista nativa en lista Python."""
    if isinstance(s, list):
        return s
    if isinstance(s, str):
        try:
            return json.loads(s)
        except Exception:
            pass
    return []

def _fecha_en_titulo(titulo: str, fecha: date) -> bool:
    """True si el título del evento menciona la fecha dada."""
    meses_en = {1:"january",2:"february",3:"march",4:"april",5:"may",6:"june",
                7:"july",8:"august",9:"september",10:"october",11:"november",12:"december"}
    mes  = meses_en[fecha.month]
    dia  = str(fecha.day)
    t    = titulo.lower()
    return mes in t and dia in t

# ── Gamma API ──────────────────────────────────────────────────────────────────

def obtener_partidos_hoy() -> list[dict]:
    """
    Devuelve lista de partidos MLB de HOY con su mercado moneyline.
    Cada entry: {
        "titulo":    str,
        "evento_id": str,
        "moneyline": {"market_id", "pregunta", "volumen", "tokens":[{token_id, equipo}]}
    }
    """
    hoy = date.today()

    # Buscar en un rango amplio (sin filtro de fecha para no perder eventos)
    eventos = _fetch_eventos_recientes()

    # Filtrar los del día de hoy por título
    eventos_hoy = [e for e in eventos if _fecha_en_titulo(e.get("title",""), hoy)]

    if not eventos_hoy:
        # Intentar buscar explícitamente por slug
        eventos_hoy = _buscar_por_slug(hoy)

    if not eventos_hoy:
        return []

    partidos = []
    for evento in eventos_hoy:
        mercados_raw = evento.get("markets") or []

        # Si el evento no trae los mercados expandidos, pedirlos por separado
        if not mercados_raw or not isinstance(mercados_raw[0], dict):
            mercados_raw = _fetch_mercados_evento(evento["id"])

        for m in mercados_raw:
            if not isinstance(m, dict):
                continue
            if m.get("closed") or not m.get("active", True):
                continue

            token_ids = _parse_json_str(m.get("clobTokenIds", "[]"))
            outcomes  = _parse_json_str(m.get("outcomes",    "[]"))
            prices    = _parse_json_str(m.get("outcomePrices","[]"))

            if len(token_ids) < 2 or len(outcomes) < 2:
                continue

            tokens = [
                {"token_id": str(token_ids[i]), "equipo": str(outcomes[i])}
                for i in range(min(len(token_ids), len(outcomes)))
            ]

            pregunta = m.get("question", "")
            partidos.append({
                "titulo":     pregunta or f"{outcomes[0]} vs {outcomes[1]}",
                "evento_id":  evento.get("id"),
                "moneyline": {
                    "market_id": m.get("id"),
                    "pregunta":  pregunta,
                    "volumen":   float(m.get("volume") or m.get("volumeNum") or 0),
                    "tokens":    tokens,
                },
            })

    return partidos


def _fetch_eventos_recientes(dias_adelante: int = 7) -> list[dict]:
    """Trae los eventos MLB más recientes (sin filtro de fecha estricto)."""
    hoy      = date.today()
    manana   = hoy + timedelta(days=1)
    siguiente = hoy + timedelta(days=dias_adelante)

    # Intentar 1: con start_date_min/max
    for params in [
        {"series_id": MLB_SERIES_ID, "start_date_min": hoy.isoformat(),
         "start_date_max": siguiente.isoformat(), "limit": 10},
        {"series_id": MLB_SERIES_ID, "limit": 20},          # sin filtro fecha
    ]:
        try:
            resp = SESSION.get(f"{GAMMA_API}/events", params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data:
                return data if isinstance(data, list) else [data]
        except Exception:
            pass
    return []


def _buscar_por_slug(fecha: date) -> list[dict]:
    """Intenta encontrar el evento MLB del día por slug."""
    meses_en = {1:"january",2:"february",3:"march",4:"april",5:"may",6:"june",
                7:"july",8:"august",9:"september",10:"october",11:"november",12:"december"}
    mes = meses_en[fecha.month]
    dia = str(fecha.day)

    slugs = [
        f"mlb-games-{mes}-{dia}",
        f"mlb-{mes}-{dia}",
    ]

    for slug in slugs:
        try:
            resp = SESSION.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data:
                return data if isinstance(data, list) else [data]
        except Exception:
            pass
    return []


def _fetch_mercados_evento(evento_id: str) -> list[dict]:
    """Trae los mercados individuales de un evento."""
    try:
        resp = SESSION.get(f"{GAMMA_API}/markets",
                           params={"event_id": evento_id, "limit": 50}, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return []


# ── CLOB API — precios en paralelo ─────────────────────────────────────────────

def precio_clob(token_id: str) -> float | None:
    """Devuelve mid-price (0.0–1.0) de un token en Polymarket CLOB."""
    try:
        resp = SESSION.get(f"{CLOB_API}/midpoints",
                           params={"token_id": token_id}, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        mid  = data.get("mid") or data.get("midpoint") or data.get(token_id)
        if mid is not None:
            return float(mid)
        # fallback: book
        resp2 = SESSION.get(f"{CLOB_API}/book",
                            params={"token_id": token_id}, timeout=8)
        resp2.raise_for_status()
        book = resp2.json()
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if bids and asks:
            return (float(bids[0]["price"]) + float(asks[0]["price"])) / 2
    except Exception:
        pass
    return None


def obtener_precios_paralelo(token_ids: list[str], workers: int = 25) -> dict[str, float]:
    """Trae precios de múltiples tokens en paralelo."""
    precios = {}
    with ThreadPoolExecutor(max_workers=workers) as exe:
        futuros = {exe.submit(precio_clob, tid): tid for tid in token_ids}
        for fut in as_completed(futuros):
            tid = futuros[fut]
            try:
                p = fut.result()
                if p is not None:
                    precios[tid] = p
            except Exception:
                pass
    return precios


# ── Diagnóstico ────────────────────────────────────────────────────────────────

def diagnosticar_api():
    """Muestra info de diagnóstico sobre los mercados MLB disponibles."""
    print(f"\n[DIAGNÓSTICO] Consultando API Polymarket para MLB (series_id={MLB_SERIES_ID})...\n")

    # Últimos eventos (sin filtro)
    try:
        resp = SESSION.get(f"{GAMMA_API}/events",
                           params={"series_id": MLB_SERIES_ID, "limit": 5}, timeout=15)
        data = resp.json()
        if isinstance(data, list) and data:
            print(f"Últimos {len(data)} evento(s) MLB encontrados:")
            for e in data:
                estado = "CERRADO" if e.get("closed") else "ABIERTO"
                print(f"  [{estado}] ID={e.get('id')} — {e.get('title')} — vol=${e.get('volume',0):,.0f}")
        else:
            print("Sin eventos MLB en la respuesta.")
    except Exception as ex:
        print(f"Error consultando eventos: {ex}")

    # Mercados abiertos
    try:
        resp = SESSION.get(f"{GAMMA_API}/markets",
                           params={"series_id": MLB_SERIES_ID, "closed": "false", "limit": 5},
                           timeout=15)
        data = resp.json()
        if isinstance(data, list) and data:
            print(f"\nMercados MLB ABIERTOS encontrados: {len(data)}")
            for m in data:
                print(f"  ID={m.get('id')} — {m.get('question','?')[:60]}")
        else:
            print("\nNo hay mercados MLB abiertos en este momento.")
            print("Posibles razones:")
            print("  • La temporada MLB 2026 aún no ha comenzado en Polymarket")
            print("  • Opening Day todavía no está listado (suele aparecer ~3 días antes)")
            print("  • Los partidos de hoy ya cerraron o aún no abrieron")
    except Exception as ex:
        print(f"Error consultando mercados abiertos: {ex}")


# ── Ejecución directa ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[{hora_et()}]  Buscando partidos MLB de hoy en Polymarket...\n")

    partidos = obtener_partidos_hoy()

    if not partidos:
        diagnosticar_api()
    else:
        all_tokens = [t["token_id"] for p in partidos for t in p["moneyline"]["tokens"]]
        print(f"Trayendo precios para {len(all_tokens)} tokens...\n")
        precios = obtener_precios_paralelo(list(set(all_tokens)))

        for p in partidos:
            m = p["moneyline"]
            print(f"  ⚾  {p['titulo']}")
            for t in m["tokens"]:
                precio = precios.get(t["token_id"])
                print(f"     {t['equipo']:30s}  {centavos(precio) if precio is not None else 'sin precio'}")
            print()
