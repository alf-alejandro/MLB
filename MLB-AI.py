"""
╔══════════════════════════════════════════════════════════════╗
║          MLB EDGE ALPHA BOT  v1.0                           ║
║  Detecta oportunidades de valor en Polymarket MLB           ║
║                                                              ║
║  FÓRMULA MEA (MLB Edge Alpha):                              ║
║  valor_raw  = 0.50·P_Vegas + 0.25·P_norm + 0.15·R + (±4V) ║
║  penalización pitcher: -12% si as ausente, -7% si titular  ║
║  valor_real = normalizado a 100 entre ambos equipos         ║
║  MEA        = P_Poly - valor_real                           ║
║                                                              ║
║  RESUMEN FINAL:                                             ║
║  ⚾ SCALPING  : MEA ≤ -15 y valor_real ≥ 40               ║
║                 Comprar pre-partido, vender antes first pitch║
║  🏆 QUIEN GANA: equipo con mayor real_value cuando el gap  ║
║                 entre los dos equipos es ≥ REAL_GAP_MIN    ║
║                                                              ║
║  Requiere:                                                   ║
║    pip install requests google-genai                        ║
╚══════════════════════════════════════════════════════════════╝
"""

import os
import re
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── env loader ────────────────────────────────────────────────────────────────

def _cargar_env():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key   = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value

_cargar_env()

# ── dependencias ──────────────────────────────────────────────────────────────

import requests
from google import genai
from google.genai import types

# ── configuración ─────────────────────────────────────────────────────────────

GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL    = "gemini-flash-lite-latest"
GEMINI_CALLS    = 5        # llamadas por partido para promediar
GEMINI_WORKERS  = 4        # llamadas Gemini en paralelo

# Umbrales MEA
SCALPING_MEA    = -15      # MEA ≤ -15¢ → scalping
SCALPING_MIN    = 40       # valor_real ≥ 40¢ para scalping
REAL_GAP_MIN    = 12       # gap ≥ 12¢ entre equipos → pick de ganador
BUY_SIGNAL_MAX  = -5       # MEA entre -20¢ y -5¢ → buy signal
BUY_SIGNAL_MIN  = -20

# Pesos fórmula MEA
W_VEGAS     = 0.50
W_PITCHER   = 0.25
W_RACHA     = 0.15
V_HOME      = 4.0          # bono campo propio
V_AWAY      = -4.0

# Penalizaciones pitcher
PEN_ACE     = 0.12         # as ausente (ERA < 3.00 o Cy Young)
PEN_STARTER = 0.07         # titular normal ausente

# ── Polymarket ────────────────────────────────────────────────────────────────

import importlib.util as _ilu, os as _os
_spec = _ilu.spec_from_file_location(
    "mlb_poly",
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "MLB-POLY.py")
)
_mlb_poly = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mlb_poly)

obtener_partidos_hoy     = _mlb_poly.obtener_partidos_hoy
obtener_precios_paralelo = _mlb_poly.obtener_precios_paralelo
enriquecer_con_gamma     = _mlb_poly.enriquecer_con_gamma
hora_et                  = _mlb_poly.hora_et
centavos                 = _mlb_poly.centavos
diagnosticar_api         = _mlb_poly.diagnosticar_api

# ── Gemini AI ─────────────────────────────────────────────────────────────────

_gemini_client = None

def _get_client():
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini_client


PROMPT_MLB = """
Eres un analista experto en MLB. Analiza el siguiente partido:

  Equipo LOCAL (home): {home}
  Equipo VISITANTE (away): {away}

Busca en internet la información más reciente sobre este partido de HOY y devuelve SOLO un JSON con esta estructura exacta:

{{
  "p_vegas_home": <número 0-100, probabilidad implícita del equipo local según líneas Vegas/DraftKings/FanDuel>,
  "p_vegas_away": <número 0-100, probabilidad implícita del equipo visitante>,
  "pitcher_home": {{
    "nombre": "<nombre del pitcher titular local>",
    "era": <ERA de la temporada, número decimal>,
    "era_norm": <normalizado 0-100, donde ERA 1.00=100, ERA 6.00+=0, escala inversa>,
    "ausente": <true/false si el pitcher previsto no lanzará hoy>,
    "es_as": <true/false si tiene ERA < 3.00 o es All-Star caliber>
  }},
  "pitcher_away": {{
    "nombre": "<nombre del pitcher titular visitante>",
    "era": <ERA>,
    "era_norm": <normalizado 0-100>,
    "ausente": <true/false>,
    "es_as": <true/false>
  }},
  "racha_home": <porcentaje de victorias últimos 10 juegos del equipo local, 0-100>,
  "racha_away": <porcentaje de victorias últimos 10 juegos del equipo visitante, 0-100>,
  "notas": "<observación clave en 1 línea: lesiones importantes, clima, etc.>"
}}

Reglas:
- p_vegas_home + p_vegas_away deben sumar ~100 (pueden ser 99-101 por vig).
- era_norm: usa fórmula ((6.0 - ERA) / 5.0) * 100, clampeado 0-100.
- Si no encuentras datos del pitcher, usa era_norm=50 y ausente=false.
- Si no encuentras récord de últimos 10 juegos, usa 50.
- Devuelve SOLO el JSON, sin texto extra ni markdown.
"""


def _llamar_gemini(home: str, away: str, intento: int) -> dict | None:
    """Una sola llamada a Gemini para un partido. Retorna dict o None."""
    client = _get_client()
    prompt = PROMPT_MLB.format(home=home, away=away)
    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.2,
            ),
        )
        raw = response.text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        return data
    except Exception as e:
        print(f"   [Gemini intento {intento+1}] Error: {e}")
        return None


def analizar_con_gemini(home: str, away: str) -> dict | None:
    """
    Llama Gemini GEMINI_CALLS veces en paralelo y promedia los resultados numéricos.
    """
    resultados = []
    with ThreadPoolExecutor(max_workers=GEMINI_WORKERS) as exe:
        futuros = {exe.submit(_llamar_gemini, home, away, i): i
                   for i in range(GEMINI_CALLS)}
        for fut in as_completed(futuros):
            r = fut.result()
            if r:
                resultados.append(r)

    if not resultados:
        return None

    def media(key, nested=None):
        vals = []
        for r in resultados:
            v = r.get(key) if not nested else r.get(key, {}).get(nested)
            if isinstance(v, (int, float)):
                vals.append(float(v))
        return sum(vals) / len(vals) if vals else None

    def mayoria_bool(key, nested=None):
        vals = []
        for r in resultados:
            v = r.get(key) if not nested else r.get(key, {}).get(nested)
            if isinstance(v, bool):
                vals.append(v)
        return sum(vals) > len(vals) / 2 if vals else False

    p_vegas_home = media("p_vegas_home")
    p_vegas_away = media("p_vegas_away")
    # Normalizar a 100
    if p_vegas_home and p_vegas_away:
        total = p_vegas_home + p_vegas_away
        p_vegas_home = (p_vegas_home / total) * 100
        p_vegas_away = (p_vegas_away / total) * 100

    # Mejor nota (la más larga = más informativa)
    notas = max((r.get("notas","") for r in resultados), key=len, default="")

    # Pitcher info (del primer resultado válido con datos)
    def mejor_pitcher(key):
        for r in resultados:
            p = r.get(key, {})
            if p.get("nombre"):
                return p
        return {}

    ph = mejor_pitcher("pitcher_home")
    pa = mejor_pitcher("pitcher_away")

    return {
        "p_vegas_home":  round(p_vegas_home or 50, 2),
        "p_vegas_away":  round(p_vegas_away or 50, 2),
        "pitcher_home": {
            "nombre":   ph.get("nombre", "Desconocido"),
            "era":      ph.get("era", 4.50),
            "era_norm": round(media("pitcher_home", "era_norm") or 50, 1),
            "ausente":  mayoria_bool("pitcher_home", "ausente"),
            "es_as":    mayoria_bool("pitcher_home", "es_as"),
        },
        "pitcher_away": {
            "nombre":   pa.get("nombre", "Desconocido"),
            "era":      pa.get("era", 4.50),
            "era_norm": round(media("pitcher_away", "era_norm") or 50, 1),
            "ausente":  mayoria_bool("pitcher_away", "ausente"),
            "es_as":    mayoria_bool("pitcher_away", "es_as"),
        },
        "racha_home":   round(media("racha_home") or 50, 1),
        "racha_away":   round(media("racha_away") or 50, 1),
        "notas":        notas,
    }


# ── Fórmula MEA ───────────────────────────────────────────────────────────────

def calcular_mea(ai: dict, precio_home: float, precio_away: float,
                 home: str, away: str) -> dict:
    """
    Aplica la fórmula MLB Edge Alpha y retorna el análisis completo.
    Todos los valores internos están en escala 0-100.
    Precios Polymarket en 0.0-1.0.
    """
    poly_home = precio_home * 100  # convertir a centavos (0-100)
    poly_away = precio_away * 100

    p_vegas_home = ai["p_vegas_home"]
    p_vegas_away = ai["p_vegas_away"]

    pitcher_home = ai["pitcher_home"]
    pitcher_away = ai["pitcher_away"]

    era_home = pitcher_home["era_norm"]
    era_away = pitcher_away["era_norm"]

    racha_home = ai["racha_home"]
    racha_away = ai["racha_away"]

    # ── valor raw ─────────────────────────────────────────────────────────────
    raw_home = (W_VEGAS * p_vegas_home +
                W_PITCHER * era_home +
                W_RACHA * racha_home +
                V_HOME)

    raw_away = (W_VEGAS * p_vegas_away +
                W_PITCHER * era_away +
                W_RACHA * racha_away +
                V_AWAY)

    # ── penalización pitcher ausente ─────────────────────────────────────────
    if pitcher_home["ausente"]:
        pen = PEN_ACE if pitcher_home["es_as"] else PEN_STARTER
        raw_home *= (1 - pen)
        print(f"   ⚠  Pitcher {home} AUSENTE  ({'AS' if pitcher_home['es_as'] else 'titular'}) "
              f"→ penalización -{pen*100:.0f}%")

    if pitcher_away["ausente"]:
        pen = PEN_ACE if pitcher_away["es_as"] else PEN_STARTER
        raw_away *= (1 - pen)
        print(f"   ⚠  Pitcher {away} AUSENTE  ({'AS' if pitcher_away['es_as'] else 'titular'}) "
              f"→ penalización -{pen*100:.0f}%")

    # ── normalizar a 100 ──────────────────────────────────────────────────────
    total = raw_home + raw_away
    if total == 0:
        total = 1
    valor_home = (raw_home / total) * 100
    valor_away = (raw_away / total) * 100

    # ── MEA ───────────────────────────────────────────────────────────────────
    mea_home = poly_home - valor_home
    mea_away = poly_away - valor_away
    gap      = abs(valor_home - valor_away)

    # ── clasificar señales ────────────────────────────────────────────────────
    señales = []

    # Scalping
    if mea_home <= SCALPING_MEA and valor_home >= SCALPING_MIN:
        señales.append({"tipo": "SCALPING", "equipo": home, "mea": round(mea_home, 1)})
    if mea_away <= SCALPING_MEA and valor_away >= SCALPING_MIN:
        señales.append({"tipo": "SCALPING", "equipo": away, "mea": round(mea_away, 1)})

    # Winner pick
    if gap >= REAL_GAP_MIN:
        ganador = home if valor_home > valor_away else away
        señales.append({"tipo": "GANADOR", "equipo": ganador, "gap": round(gap, 1)})

    # Buy signals
    for eq, mea, val in [(home, mea_home, valor_home), (away, mea_away, valor_away)]:
        if BUY_SIGNAL_MIN <= mea <= BUY_SIGNAL_MAX and val >= 30:
            señales.append({"tipo": "BUY", "equipo": eq, "mea": round(mea, 1)})

    return {
        "home":         home,
        "away":         away,
        "poly_home":    round(poly_home, 1),
        "poly_away":    round(poly_away, 1),
        "valor_home":   round(valor_home, 1),
        "valor_away":   round(valor_away, 1),
        "mea_home":     round(mea_home, 1),
        "mea_away":     round(mea_away, 1),
        "gap":          round(gap, 1),
        "señales":      señales,
        "pitcher_home": pitcher_home,
        "pitcher_away": pitcher_away,
        "racha_home":   ai["racha_home"],
        "racha_away":   ai["racha_away"],
        "p_vegas_home": ai["p_vegas_home"],
        "p_vegas_away": ai["p_vegas_away"],
        "notas":        ai.get("notas", ""),
    }


# ── Output ─────────────────────────────────────────────────────────────────────

def imprimir_resultado(r: dict):
    sep = "─" * 60
    print(f"\n{sep}")
    print(f"  ⚾  {r['away']}  @  {r['home']}")
    print(sep)
    print(f"  {'':30s}  {'LOCAL':>10s}  {'VISIT':>10s}")
    print(f"  {'Precio Polymarket':30s}  {r['poly_home']:>9.1f}¢  {r['poly_away']:>9.1f}¢")
    print(f"  {'Valor Real (MEA)':30s}  {r['valor_home']:>9.1f}¢  {r['valor_away']:>9.1f}¢")
    print(f"  {'MEA':30s}  {r['mea_home']:>+9.1f}¢  {r['mea_away']:>+9.1f}¢")
    print(f"  {'P_Vegas':30s}  {r['p_vegas_home']:>9.1f}¢  {r['p_vegas_away']:>9.1f}¢")
    print(f"  {'Pitcher (ERA)':30s}  {r['pitcher_home']['nombre'][:10]:>10s}  {r['pitcher_away']['nombre'][:10]:>10s}")
    print(f"  {'  ERA':30s}  {r['pitcher_home']['era']:>9.2f}   {r['pitcher_away']['era']:>9.2f}")
    print(f"  {'Racha (últ.10)':30s}  {r['racha_home']:>9.1f}%  {r['racha_away']:>9.1f}%")
    if r["notas"]:
        print(f"\n  📋 {r['notas']}")
    if r["señales"]:
        print()
        for s in r["señales"]:
            if s["tipo"] == "SCALPING":
                print(f"  🎰 SCALPING   ► {s['equipo']:25s}  MEA={s['mea']:+.1f}¢")
            elif s["tipo"] == "GANADOR":
                print(f"  🏆 GANADOR    ► {s['equipo']:25s}  gap={s['gap']:.1f}¢")
            elif s["tipo"] == "BUY":
                print(f"  ✅ BUY SIGNAL ► {s['equipo']:25s}  MEA={s['mea']:+.1f}¢")
    else:
        print("\n  ⛔  Sin oportunidad clara")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"""
╔══════════════════════════════════════════════════╗
║   MLB EDGE ALPHA BOT  v1.0  —  {hora_et()}   ║
╚══════════════════════════════════════════════════╝
""")

    if not GEMINI_API_KEY:
        print("❌  Falta GEMINI_API_KEY en .env")
        return

    # ── 1) Traer partidos de Polymarket ───────────────────────────────────────
    print("🔍  Buscando partidos MLB de hoy en Polymarket...")
    partidos = obtener_partidos_hoy()

    if not partidos:
        print("  Sin partidos MLB disponibles para hoy.")
        print()
        diagnosticar_api()
        print()
        print("  ℹ️  El bot funcionará automáticamente cuando Polymarket")
        print("      publique los mercados MLB (normalmente 1-3 días antes")
        print("      de cada partido). Vuelve a intentarlo más tarde.")
        return

    print(f"  Encontrados {len(partidos)} partido(s).\n")

    # ── 2) Traer precios CLOB + fallback Gamma ────────────────────────────────
    all_tokens = list({tid for p in partidos for tid in p["token_ids"]})

    print(f"💰  Trayendo precios para {len(all_tokens)} tokens (paralelo)...")
    precios_clob = obtener_precios_paralelo(all_tokens)
    precios      = enriquecer_con_gamma(partidos, precios_clob)
    print(f"  CLOB: {len(precios_clob)} | Gamma: {len(precios)-len(precios_clob)} | Total: {len(precios)}/{len(all_tokens)}\n")

    # ── 3) Analizar cada partido ───────────────────────────────────────────────
    resultados = []

    for i, partido in enumerate(partidos):
        outcomes   = partido["outcomes"]
        token_ids  = partido["token_ids"]

        if len(outcomes) < 2 or len(token_ids) < 2:
            continue

        # En MLB Polymarket: outcomes[0]=equipo visitante, outcomes[1]=equipo local
        # (la pregunta dice "Will [away] beat [home]?")
        away = outcomes[0]
        home = outcomes[1]
        tid_away = token_ids[0]
        tid_home = token_ids[1]

        p_home = precios.get(tid_home)
        p_away = precios.get(tid_away)

        if p_home is None or p_away is None:
            print(f"  [{i+1}] Sin precios para {away} @ {home}, skip.")
            continue

        print(f"🤖  [{i+1}/{len(partidos)}]  Analizando con Gemini: {away} @ {home}  "
              f"({GEMINI_CALLS} llamadas en paralelo)...")

        ai = analizar_con_gemini(home, away)

        if ai is None:
            print(f"   ❌ Gemini falló para este partido.")
            continue

        resultado = calcular_mea(ai, p_home, p_away, home, away)
        imprimir_resultado(resultado)
        resultados.append(resultado)

    # ── 4) Exportar JSON para el dashboard ───────────────────────────────────
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resultados.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(resultados, f, ensure_ascii=False, indent=2)

    print(f"\n\n{'═'*60}")
    print(f"  Análisis completado — {len(resultados)} partido(s)")
    scalping = sum(1 for r in resultados for s in r["señales"] if s["tipo"] == "SCALPING")
    ganadores = sum(1 for r in resultados for s in r["señales"] if s["tipo"] == "GANADOR")
    buys = sum(1 for r in resultados for s in r["señales"] if s["tipo"] == "BUY")
    print(f"  🎰 Scalping:     {scalping}")
    print(f"  🏆 Picks ganador: {ganadores}")
    print(f"  ✅ Buy signals:  {buys}")
    print(f"  Resultados guardados en resultados.json")
    print(f"{'═'*60}\n")


if __name__ == "__main__":
    main()
