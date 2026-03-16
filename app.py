"""
app.py — Entrypoint Railway: Hedge Sim + Web Dashboard en un solo proceso.

Arranca:
  1. hedge_sim.main_loop() como tarea asyncio
  2. Servidor HTTP aiohttp en PORT (Railway lo inyecta)

Dashboard en /        → HTML auto-refresh
API en /api/state     → JSON con estado actual
"""

import asyncio
import csv
import io
import os
import json
from datetime import datetime
from aiohttp import web

import hedge_sim  # accede a estado, pos, eventos (globals del módulo)

PORT = int(os.environ.get("PORT", 8080))

# ── Dashboard HTML ─────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Hedge Sim Dashboard</title>
  <style>
    :root {
      --bg: #0d1117; --card: #161b22; --border: #30363d;
      --text: #c9d1d9; --muted: #8b949e;
      --green: #3fb950; --red: #f85149; --yellow: #d29922;
      --blue: #58a6ff; --purple: #bc8cff;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: var(--bg); color: var(--text); font-family: 'Courier New', monospace; font-size: 14px; }
    header { background: var(--card); border-bottom: 1px solid var(--border); padding: 14px 24px; display: flex; align-items: center; justify-content: space-between; }
    header h1 { font-size: 18px; color: var(--blue); letter-spacing: 1px; }
    header .status { font-size: 12px; color: var(--muted); }
    .container { padding: 20px 24px; max-width: 1200px; margin: 0 auto; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 20px; }
    .card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
    .card .label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px; }
    .card .value { font-size: 24px; font-weight: bold; }
    .card .sub { font-size: 12px; color: var(--muted); margin-top: 4px; }
    .green { color: var(--green); }
    .red { color: var(--red); }
    .yellow { color: var(--yellow); }
    .blue { color: var(--blue); }
    .purple { color: var(--purple); }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 20px; }
    @media (max-width: 700px) { .row { grid-template-columns: 1fr; } }
    .panel { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
    .panel h3 { font-size: 13px; color: var(--blue); margin-bottom: 12px; text-transform: uppercase; letter-spacing: 1px; }
    .pos-badge { display: inline-block; padding: 4px 10px; border-radius: 4px; font-size: 12px; font-weight: bold; margin-bottom: 8px; }
    .pos-active { background: rgba(63,185,80,0.15); color: var(--green); border: 1px solid var(--green); }
    .pos-inactive { background: rgba(139,148,158,0.1); color: var(--muted); border: 1px solid var(--border); }
    .pos-row { display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid var(--border); font-size: 13px; }
    .pos-row:last-child { border-bottom: none; }
    .events { list-style: none; max-height: 280px; overflow-y: auto; }
    .events li { padding: 4px 0; border-bottom: 1px solid var(--border); font-size: 12px; color: var(--muted); }
    .events li:last-child { border-bottom: none; }
    .trades { width: 100%; border-collapse: collapse; font-size: 12px; }
    .trades th { color: var(--muted); text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--border); font-weight: normal; text-transform: uppercase; font-size: 11px; }
    .trades td { padding: 5px 8px; border-bottom: 1px solid var(--border); }
    .trades tr:last-child td { border-bottom: none; }
    .win { color: var(--green); } .loss { color: var(--red); }
    .refresh-bar { height: 3px; background: var(--border); border-radius: 2px; margin-bottom: 16px; }
    .refresh-bar-fill { height: 100%; background: var(--blue); border-radius: 2px; transition: width 5s linear; }
    footer { text-align: center; color: var(--muted); font-size: 11px; padding: 16px; }
  </style>
</head>
<body>
  <header>
    <h1>&#9654; HEDGE SIM DASHBOARD</h1>
    <div class="status">Auto-refresh: 5s &nbsp;|&nbsp; <span id="last-update">--</span></div>
  </header>

  <div class="container">
    <div class="refresh-bar"><div class="refresh-bar-fill" id="rbar" style="width:100%"></div></div>

    <div class="grid" id="stats-grid">
      <!-- filled by JS -->
    </div>

    <div class="row">
      <div class="panel">
        <h3>Posicion Actual</h3>
        <div id="pos-panel">Cargando...</div>
      </div>
      <div class="panel">
        <h3>Eventos Recientes</h3>
        <ul class="events" id="events-list"><li>Cargando...</li></ul>
      </div>
    </div>

    <div class="panel">
      <h3 style="display:flex;justify-content:space-between;align-items:center;">
        <span>Historial de Trades</span>
        <a href="/api/trades.csv" download style="font-size:11px;color:var(--blue);text-decoration:none;border:1px solid var(--blue);padding:3px 10px;border-radius:4px;">&#11015; CSV</a>
      </h3>
      <div style="overflow-x:auto">
        <table class="trades">
          <thead>
            <tr>
              <th>Hora</th><th>Tipo</th><th>L1</th><th>L1$</th><th>L2</th><th>L2$</th>
              <th>Resolucion</th><th>PnL</th><th>Capital</th><th>Resultado</th>
            </tr>
          </thead>
          <tbody id="trades-body"><tr><td colspan="10" style="color:var(--muted)">Sin trades aun</td></tr></tbody>
        </table>
      </div>
    </div>
  </div>

  <footer>
    Hedge Sim v2 &mdash; Polymarket BTC/SOL Up/Down 5m &mdash; Simulacion &nbsp;|&nbsp;
    <a href="/api/trades.csv" download style="color:var(--blue);text-decoration:none;">&#11015; Descargar CSV</a>
  </footer>

  <script>
    const INTERVAL = 5000;
    let timer;

    function colorPnl(v) {
      const n = parseFloat(v);
      if (isNaN(n)) return '';
      return n >= 0 ? 'green' : 'red';
    }

    function fmtPnl(v) {
      const n = parseFloat(v);
      if (isNaN(n)) return v;
      return (n >= 0 ? '+' : '') + n.toFixed(4);
    }

    async function fetchState() {
      try {
        const r = await fetch('/api/state');
        if (!r.ok) return;
        const d = await r.json();
        render(d);
        document.getElementById('last-update').textContent = new Date().toLocaleTimeString();
      } catch(e) {
        console.error(e);
      }
      resetBar();
    }

    function resetBar() {
      const bar = document.getElementById('rbar');
      bar.style.transition = 'none';
      bar.style.width = '100%';
      setTimeout(() => {
        bar.style.transition = 'width 5s linear';
        bar.style.width = '0%';
      }, 50);
    }

    function render(d) {
      const roiClass = d.roi >= 0 ? 'green' : 'red';
      const pnlClass = d.pnl_total >= 0 ? 'green' : 'red';
      const ddClass  = d.max_drawdown > 0 ? 'red' : 'muted';

      document.getElementById('stats-grid').innerHTML = `
        <div class="card">
          <div class="label">Capital</div>
          <div class="value blue">$${parseFloat(d.capital).toFixed(2)}</div>
          <div class="sub">Inicial: $${d.capital_inicial || '--'}</div>
        </div>
        <div class="card">
          <div class="label">PnL Total</div>
          <div class="value ${pnlClass}">${fmtPnl(d.pnl_total)}</div>
          <div class="sub">ROI: <span class="${roiClass}">${d.roi >= 0 ? '+' : ''}${parseFloat(d.roi).toFixed(2)}%</span></div>
        </div>
        <div class="card">
          <div class="label">Win Rate</div>
          <div class="value ${d.win_rate >= 50 ? 'green' : 'red'}">${parseFloat(d.win_rate).toFixed(0)}%</div>
          <div class="sub">W: ${d.wins} &nbsp; L: ${d.losses}</div>
        </div>
        <div class="card">
          <div class="label">Max Drawdown</div>
          <div class="value ${ddClass}">$${parseFloat(d.max_drawdown).toFixed(2)}</div>
          <div class="sub">Peak: $${parseFloat(d.peak_capital).toFixed(2)}</div>
        </div>
        <div class="card">
          <div class="label">Ciclos</div>
          <div class="value purple">${d.ciclos}</div>
          <div class="sub">Mercados BTC 5m</div>
        </div>
      `;

      // Posicion
      const p = d.posicion || {};
      let posHtml = '';
      if (p.activa) {
        posHtml = `<span class="pos-badge pos-active">&#9679; ACTIVA</span>`;
        posHtml += `<div class="pos-row"><span>Lado 1</span><span>${p.lado1 || '--'}</span></div>`;
        posHtml += `<div class="pos-row"><span>Hedgeado</span><span class="${p.hedgeado ? 'green' : 'yellow'}">${p.hedgeado ? 'SI' : 'NO'}</span></div>`;
        if (p.hedgeado) posHtml += `<div class="pos-row"><span>Lado 2</span><span>${p.lado2 || '--'}</span></div>`;
        posHtml += `<div class="pos-row"><span>Capital en juego</span><span>$${parseFloat(p.capital_usado || 0).toFixed(2)}</span></div>`;
      } else {
        posHtml = `<span class="pos-badge pos-inactive">&#9675; SIN POSICION</span>`;
      }
      document.getElementById('pos-panel').innerHTML = posHtml;

      // Eventos
      const evs = (d.eventos || []).slice().reverse();
      document.getElementById('events-list').innerHTML =
        evs.length ? evs.map(e => `<li>${e}</li>`).join('') : '<li>Sin eventos</li>';

      // Trades
      const trades = (d.trades || []).slice().reverse();
      if (trades.length === 0) {
        document.getElementById('trades-body').innerHTML =
          '<tr><td colspan="10" style="color:var(--muted)">Sin trades aun</td></tr>';
      } else {
        document.getElementById('trades-body').innerHTML = trades.map(t => {
          const ts = t.ts ? t.ts.substring(11, 19) : '--';
          const pnlCls = parseFloat(t.pnl) >= 0 ? 'win' : 'loss';
          return `<tr>
            <td>${ts}</td>
            <td>${t.tipo || '--'}</td>
            <td>${t.lado1_side || '--'}</td>
            <td>$${parseFloat(t.lado1_usd || 0).toFixed(2)}</td>
            <td>${t.lado2_side || '--'}</td>
            <td>$${parseFloat(t.lado2_usd || 0).toFixed(2)}</td>
            <td>${t.resolucion || '--'}</td>
            <td class="${pnlCls}">${fmtPnl(t.pnl)}</td>
            <td>$${parseFloat(t.capital || 0).toFixed(2)}</td>
            <td class="${t.outcome === 'WIN' ? 'win' : 'loss'}">${t.outcome || '--'}</td>
          </tr>`;
        }).join('');
      }
    }

    fetchState();
    resetBar();
    setInterval(fetchState, INTERVAL);
  </script>
</body>
</html>
"""

# ── Handlers HTTP ──────────────────────────────────────────────────────────────

async def handle_dashboard(request):
    return web.Response(text=DASHBOARD_HTML, content_type="text/html")


async def handle_state(request):
    total = hedge_sim.estado["wins"] + hedge_sim.estado["losses"]
    wr    = hedge_sim.estado["wins"] / total * 100 if total > 0 else 0.0
    roi   = (hedge_sim.estado["capital"] - hedge_sim.CAPITAL_INICIAL) / hedge_sim.CAPITAL_INICIAL * 100

    payload = {
        "ts":             datetime.now().isoformat(),
        "capital_inicial": round(hedge_sim.CAPITAL_INICIAL, 2),
        "capital":        round(hedge_sim.estado["capital"], 4),
        "pnl_total":      round(hedge_sim.estado["pnl_total"], 4),
        "roi":            round(roi, 2),
        "peak_capital":   round(hedge_sim.estado["peak_capital"], 4),
        "max_drawdown":   round(hedge_sim.estado["max_drawdown"], 4),
        "wins":           hedge_sim.estado["wins"],
        "losses":         hedge_sim.estado["losses"],
        "win_rate":       round(wr, 1),
        "ciclos":         hedge_sim.estado["ciclos"],
        "posicion": {
            "activa":        hedge_sim.pos["activa"],
            "lado1":         hedge_sim.pos["lado1_side"],
            "lado2":         hedge_sim.pos["lado2_side"],
            "hedgeado":      hedge_sim.pos["hedgeado"],
            "capital_usado": round(hedge_sim.pos["capital_usado"], 4),
        },
        "eventos":  list(hedge_sim.eventos)[-40:],
        "trades":   hedge_sim.estado["trades"][-50:],
    }
    return web.Response(
        text=json.dumps(payload, indent=2),
        content_type="application/json",
    )


# ── CSV export ────────────────────────────────────────────────────────────────

async def handle_csv(request):
    trades = hedge_sim.estado["trades"]
    fields = ["ts", "tipo", "lado1_side", "lado1_usd", "lado1_precio",
              "hedgeado", "lado2_side", "lado2_usd", "lado2_precio",
              "exit_precio", "resolucion", "pnl", "capital", "outcome"]

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(trades)

    filename = f"hedge_trades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return web.Response(
        text=buf.getvalue(),
        content_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Servidor web ───────────────────────────────────────────────────────────────

async def start_web():
    app = web.Application()
    app.router.add_get("/", handle_dashboard)
    app.router.add_get("/api/state", handle_state)
    app.router.add_get("/api/trades.csv", handle_csv)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"[dashboard] Escuchando en http://0.0.0.0:{PORT}")


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    await asyncio.gather(
        start_web(),
        hedge_sim.main_loop(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Detenido.")
