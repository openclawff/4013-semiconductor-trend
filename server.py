#!/usr/bin/env python3
"""4013 server — serves static files + proxies 3422 bond-hedge + monitor API."""
import http.server, json, urllib.request, os, sys, time, subprocess, threading
from pathlib import Path
from datetime import datetime, timezone, timedelta

PORT = 4013
BOND_HEDGE_URL = 'http://192.168.25.134:3422/api/hedge_trs?days=7'
SURGE_GLOBAL_URL = 'http://192.168.25.134:3402/api/volume-surge/global'
SURGE_COMMODITY_URL = 'http://192.168.25.134:3402/api/commodity-surge'
SOXL_5050_URL = 'http://192.168.25.134:5050/query'
BOND_CACHE = {}
BOND_CACHE_TTL = 300  # 5min
SURGE_CACHE = {'global': None, 'commodity': None, 'ts': 0}
SOXL_CACHE = {'data': None, 'ts': 0}
SOXL_CACHE_TTL = 60  # 1min refresh

# Monitor cache - stores latest analysis output
MONITOR_CACHE = {'data': None, 'ts': 0}
MONITOR_LOCK = threading.Lock()

def run_monitor():
    """Run monitor.py and capture output."""
    global MONITOR_CACHE
    script = Path(__file__).parent / 'monitor.py'
    try:
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, timeout=120
        )
        # Parse the last JSON block (summary)
        lines = result.stdout.strip().split('\n')
        for line in reversed(lines):
            try:
                data = json.loads(line)
                if data.get('type') == 'analysis_summary':
                    with MONITOR_LOCK:
                        MONITOR_CACHE['data'] = data
                        MONITOR_CACHE['ts'] = time.time()
                    return data
            except json.JSONDecodeError:
                continue
    except Exception as e:
        return {'error': str(e)}
    return {'error': 'no summary found'}

class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/api/bond-hedge'):
            self.proxy_bond_hedge()
        elif self.path.startswith('/api/surge'):
            self.proxy_surge()
        elif self.path.startswith('/api/soxl'):
            self.proxy_soxl()
        elif self.path.startswith('/api/monitor'):
            self.handle_monitor()
        elif self.path.startswith('/api/analysis'):
            self.handle_analysis()
        else:
            super().do_GET()

    def handle_monitor(self):
        """GET /api/monitor - run fresh analysis."""
        data = run_monitor()
        self.send_json(data)

    def handle_analysis(self):
        """GET /api/analysis - return cached analysis (fast)."""
        now = time.time()
        with MONITOR_LOCK:
            if MONITOR_CACHE['data'] and now - MONITOR_CACHE['ts'] < 120:
                self.send_json(MONITOR_CACHE['data'])
                return
        # Cache expired, run fresh
        data = run_monitor()
        self.send_json(data)

    def proxy_bond_hedge(self):
        now = time.time()
        if BOND_CACHE.get('data') and now - BOND_CACHE.get('ts', 0) < BOND_CACHE_TTL:
            data = BOND_CACHE['data']
        else:
            try:
                req = urllib.request.Request(BOND_HEDGE_URL)
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = json.loads(r.read().decode())
                BOND_CACHE['data'] = data
                BOND_CACHE['ts'] = now
            except Exception as e:
                self.send_response(502)
                self.send_header('Content-Type','application/json')
                self.send_header('Access-Control-Allow-Origin','*')
                self.end_headers()
                self.wfile.write(json.dumps({'error':str(e)}).encode())
                return
        self.send_json(data)

    def proxy_surge(self):
        now = time.time()
        if SURGE_CACHE.get('global') and now - SURGE_CACHE.get('ts', 0) < 60:
            self.send_json({'global': SURGE_CACHE['global'], 'commodity': SURGE_CACHE['commodity']})
            return
        result = {}
        # 期货放量（优先，快）
        for kind, url in [('commodity', SURGE_COMMODITY_URL)]:
            try:
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = json.loads(r.read().decode())
                SURGE_CACHE[kind] = data
                result[kind] = data
            except Exception as e:
                result[kind] = {'error': str(e)}
        # 全球放量（可能慢）
        try:
            req = urllib.request.Request(SURGE_GLOBAL_URL)
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read().decode())
            SURGE_CACHE['global'] = data
            result['global'] = data
        except Exception as e:
            result['global'] = {'error': str(e)}
        SURGE_CACHE['ts'] = now
        self.send_json(result)

    def proxy_soxl(self):
        """GET /api/soxl - fetch SOXL swap klines from 5050, cache 60s."""
        now = time.time()
        if SOXL_CACHE.get('data') and now - SOXL_CACHE.get('ts', 0) < SOXL_CACHE_TTL:
            self.send_json(SOXL_CACHE['data'])
            return
        try:
            sql = ("SELECT period_start, open, high, low, close, volume "
                   "FROM stock_swap_klines "
                   "WHERE contract_symbol='RSOXLUSDT' AND stock_ticker='NYSE:SOXL' "
                   "ORDER BY period_start ASC")
            payload = json.dumps({'sql': sql, 'project': '3400'}).encode()
            req = urllib.request.Request(SOXL_5050_URL, data=payload,
                                        headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as r:
                result = json.loads(r.read().decode())
            # Convert to LW Charts format: [{time: epoch_sec, open, high, low, close, volume}]
            bars = []
            for row in result.get('rows', []):
                # period_start is HKT string "YYYY-MM-DDTHH:MM:SS"
                dt = datetime.strptime(row['period_start'], '%Y-%m-%dT%H:%M:%S')
                epoch = int(dt.replace(tzinfo=timezone(timedelta(hours=8))).timestamp())
                bars.append({
                    'time': epoch,
                    'open': float(row['open']),
                    'high': float(row['high']),
                    'low': float(row['low']),
                    'close': float(row['close']),
                    'volume': float(row['volume'])
                })
            SOXL_CACHE['data'] = bars
            SOXL_CACHE['ts'] = now
            self.send_json(bars)
        except Exception as e:
            self.send_response(502)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())

    def send_json(self, data):
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Access-Control-Allow-Origin','*')
        self.send_header('Cache-Control','no-cache')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin','*')
        super().end_headers()

    def log_message(self, format, *args):
        if '/api/' in str(args[0]) if args else False:
            super().log_message(format, *args)

# Run initial analysis in background
threading.Thread(target=run_monitor, daemon=True).start()

os.chdir(Path(__file__).parent)
server = http.server.HTTPServer(('0.0.0.0', PORT), Handler)
print(f'4013 server running on port {PORT}')
server.serve_forever()
