import os, json, sqlite3, requests, logging
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
CORS(app, origins="*")
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

DB = os.path.join(os.environ.get('DATA_DIR', '/tmp'), 'bot.db')
BASE = 'https://crrtenis.haceclic.club'

# ── DB ────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS config (
            id INTEGER PRIMARY KEY,
            usuario TEXT, password TEXT,
            fecha TEXT, slots TEXT, game_type TEXT,
            players TEXT, court INTEGER, fallbacks TEXT,
            auto_enabled INTEGER DEFAULT 0,
            ultimo_resultado TEXT, ultima_ejecucion TEXT
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS grupos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT, tipo TEXT, jugadores TEXT
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS log_reservas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, mensaje TEXT, tipo TEXT
        )''')
        # Insert default config if none
        cur = db.execute('SELECT COUNT(*) FROM config')
        if cur.fetchone()[0] == 0:
            db.execute('''INSERT INTO config (id, usuario, password, fecha, slots, game_type,
                          players, court, fallbacks, auto_enabled)
                          VALUES (1, '', '', '', '[]', 'singles', '[]', 1, '[]', 0)''')
        db.commit()

# ── LOG ───────────────────────────────────────────────
def log_reserva(msg, tipo='info'):
    log.info(f"[{tipo.upper()}] {msg}")
    with get_db() as db:
        db.execute('INSERT INTO log_reservas (timestamp, mensaje, tipo) VALUES (?, ?, ?)',
                   (datetime.now().isoformat(), msg, tipo))
        # Keep only last 100 logs
        db.execute('DELETE FROM log_reservas WHERE id NOT IN (SELECT id FROM log_reservas ORDER BY id DESC LIMIT 100)')
        db.commit()

# ── API ROUTES ────────────────────────────────────────

@app.route('/health')
def health():
    return jsonify({'ok': True, 'time': datetime.now().isoformat()})

@app.route('/config', methods=['GET'])
def get_config():
    with get_db() as db:
        row = db.execute('SELECT * FROM config WHERE id=1').fetchone()
        if row:
            cfg = dict(row)
            cfg['password'] = '••••••••' if cfg['password'] else ''
            cfg['slots'] = json.loads(cfg['slots'] or '[]')
            cfg['players'] = json.loads(cfg['players'] or '[]')
            cfg['fallbacks'] = json.loads(cfg['fallbacks'] or '[]')
            return jsonify(cfg)
    return jsonify({'error': 'No config'}), 404

@app.route('/credenciales', methods=['POST'])
def save_credenciales():
    data = request.json
    usuario  = data.get('usuario', '').strip()
    password = data.get('password', '')
    with get_db() as db:
        if usuario:
            db.execute('UPDATE config SET usuario=? WHERE id=1', (usuario,))
        if password and not password.startswith('•'):
            db.execute('UPDATE config SET password=? WHERE id=1', (password,))
        db.commit()
    log_reserva(f'Credenciales actualizadas: usuario={usuario}', 'info')
    return jsonify({'ok': True})

@app.route('/config', methods=['POST'])
def save_config():
    data = request.json
    with get_db() as db:
        # Only skip password update if it's the placeholder dots
        pwd = data.get('password', '')
        if pwd.startswith('•') or pwd == '••••••••':
            row = db.execute('SELECT password FROM config WHERE id=1').fetchone()
            pwd = row['password'] if row else ''
        data['password'] = pwd
        db.execute('''UPDATE config SET
            usuario=?, password=?, fecha=?, slots=?, game_type=?,
            players=?, court=?, fallbacks=?, auto_enabled=?
            WHERE id=1''', (
            data.get('usuario', ''),
            data.get('password', ''),
            data.get('fecha', ''),
            json.dumps(data.get('slots', [])),
            data.get('game_type', 'singles'),
            json.dumps(data.get('players', [])),
            data.get('court', 1),
            json.dumps(data.get('fallbacks', [])),
            1 if data.get('auto_enabled') else 0
        ))
        db.commit()
    log_reserva('Configuración guardada', 'info')
    return jsonify({'ok': True})

@app.route('/reservar', methods=['POST'])
def reservar_manual():
    """Ejecutar reserva manualmente"""
    resultado = ejecutar_reserva()
    return jsonify(resultado)

@app.route('/logs')
def get_logs():
    with get_db() as db:
        rows = db.execute('SELECT * FROM log_reservas ORDER BY id DESC LIMIT 50').fetchall()
        return jsonify([dict(r) for r in rows])

@app.route('/debug')
def debug_config():
    with get_db() as db:
        row = db.execute('SELECT id, usuario, game_type, fecha, slots, court, auto_enabled FROM config WHERE id=1').fetchone()
        if row:
            d = dict(row)
            d['tiene_password'] = bool(db.execute('SELECT password FROM config WHERE id=1').fetchone()['password'])
            return jsonify(d)
    return jsonify({'error': 'no config'})

@app.route('/status')
def get_status():
    with get_db() as db:
        row = db.execute('SELECT ultimo_resultado, ultima_ejecucion, auto_enabled FROM config WHERE id=1').fetchone()
        next_run = None
        jobs = scheduler.get_jobs()
        if jobs:
            next_run = jobs[0].next_run_time.isoformat() if jobs[0].next_run_time else None
        return jsonify({
            'ultimo_resultado': row['ultimo_resultado'] if row else None,
            'ultima_ejecucion': row['ultima_ejecucion'] if row else None,
            'auto_enabled': bool(row['auto_enabled']) if row else False,
            'next_run': next_run
        })

# GRUPOS
@app.route('/grupos', methods=['GET'])
def get_grupos():
    with get_db() as db:
        rows = db.execute('SELECT * FROM grupos ORDER BY id').fetchall()
        grupos = []
        for r in rows:
            g = dict(r)
            g['jugadores'] = json.loads(g['jugadores'])
            grupos.append(g)
        return jsonify(grupos)

@app.route('/grupos', methods=['POST'])
def save_grupo():
    data = request.json
    with get_db() as db:
        db.execute('INSERT INTO grupos (label, tipo, jugadores) VALUES (?, ?, ?)',
                   (data['label'], data.get('tipo', 'singles'), json.dumps(data['jugadores'])))
        db.commit()
    return jsonify({'ok': True})

@app.route('/grupos/<int:gid>', methods=['DELETE'])
def delete_grupo(gid):
    with get_db() as db:
        db.execute('DELETE FROM grupos WHERE id=?', (gid,))
        db.commit()
    return jsonify({'ok': True})

# ── LÓGICA DE RESERVA ─────────────────────────────────

def ejecutar_reserva():
    log_reserva('🤖 Iniciando reserva automática', 'info')

    with get_db() as db:
        cfg = db.execute('SELECT * FROM config WHERE id=1').fetchone()
        if not cfg:
            return {'ok': False, 'msg': 'Sin configuración'}
        cfg = dict(cfg)

    usuario  = cfg['usuario']
    password = cfg['password']
    fecha    = cfg['fecha']
    slots    = json.loads(cfg['slots'])
    gtype    = cfg['game_type']
    players  = json.loads(cfg['players'])
    court    = cfg['court']
    fallbacks = json.loads(cfg['fallbacks'])

    if not usuario or not password:
        log_reserva('Sin credenciales configuradas', 'error')
        return {'ok': False, 'msg': 'Sin credenciales'}

    if not fecha:
        # Default: reservar para mañana
        fecha = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
        log_reserva(f'Fecha no configurada, usando mañana: {fecha}', 'warn')

    if not slots:
        log_reserva('Sin horarios configurados', 'error')
        return {'ok': False, 'msg': 'Sin horarios'}

    # LOGIN
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Mobile Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'es-AR,es-419;q=0.9,es;q=0.8',
        'Content-Type': 'application/json;charset=UTF-8',
        'Origin': BASE,
        'Referer': f'{BASE}//login/login.php',
        'Sec-Fetch-Site': 'same-origin',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Dest': 'empty',
        'sec-ch-ua': '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        'sec-ch-ua-mobile': '?1',
        'sec-ch-ua-platform': '"Android"',
    })

    try:
        r = session.post(f'{BASE}//login/loguearse.php',
                        json={'usuario': usuario, 'password': password}, timeout=10)
        data = r.json()
        if not data.get('Ejecucion'):
            msg = 'Login fallido: ' + data.get('Mensaje', 'credenciales incorrectas')
            log_reserva(msg, 'error')
            return {'ok': False, 'msg': msg}
        log_reserva('✓ Login OK', 'ok')
    except Exception as e:
        log_reserva(f'Error de conexión al login: {e}', 'error')
        return {'ok': False, 'msg': str(e)}

    # INTENTAR CADA SLOT + CANCHA
    courts = [court] + [f for f in fallbacks if f != court]
    dur_min = 60 if gtype == 'singles' else 90

    for slot in sorted(slots):
        for crt in courts:
            log_reserva(f'Intentando cancha {crt} · {slot}hs...', 'info')
            result = intentar_reserva(session, fecha, slot, crt, gtype, players, dur_min)
            if result['ok']:
                msg = f'✓ RESERVA CONFIRMADA — Cancha {crt} · {slot}hs · {fecha}'
                log_reserva(msg, 'ok')
                guardar_resultado(msg)
                return {'ok': True, 'msg': msg}
            else:
                log_reserva(f'✗ {result["msg"]}', 'error')

    msg = 'No se pudo reservar en ningún horario/cancha'
    log_reserva(msg, 'error')
    guardar_resultado(msg)
    return {'ok': False, 'msg': msg}


def intentar_reserva(session, fecha, slot, court, gtype, players, dur_min):
    fecha_obj = datetime.strptime(fecha, '%Y-%m-%d')
    dia_id = fecha_obj.weekday() + 2  # Python: 0=Mon, haceclic: 1=Dom,2=Lun...
    if dia_id == 8: dia_id = 1        # Domingo

    fecha_ddmm = fecha_obj.strftime('%d-%m-%Y')

    # Turnos
    try:
        r = session.get(f'{BASE}//ingresodereserva/obtenerDatosTurnos.php',
                       params={'ReservaFecha': fecha}, timeout=10)
        turnos = r.json().get('Tabla', {}).get('Turnos', [])
    except Exception as e:
        return {'ok': False, 'msg': f'Error turnos: {e}'}

    turno = None
    for t in turnos:
        if t.get('PuedeReservarSocio') != '1': continue
        if not t.get('TurnoInicio', '').startswith(slot): continue
        try:
            hi, mi = map(int, t['TurnoInicio'].split(':'))
            hf, mf = map(int, t['TurnoFin'].split(':'))
            dur = (hf*60+mf) - (hi*60+mi)
            if dur == dur_min: turno = t; break
            if not turno: turno = t
        except: pass

    if not turno:
        av = [t['TurnoNombre'] for t in turnos if t.get('PuedeReservarSocio') == '1'][:4]
        return {'ok': False, 'msg': f'Sin turno en {slot}hs. Disponibles: {", ".join(av)}'}

    log_reserva(f'  Turno: {turno["TurnoNombre"]} (ID {turno["TurnoId"]})', 'info')

    # Canchas
    try:
        r = session.get(f'{BASE}//ingresodereserva/obtenerDatosCanchasPorTurnos.php',
                       params={'TurnoId': turno['TurnoId'], 'Fecha': fecha,
                               'DiaId': dia_id, 'PuedeReservarSocio': 1, 'PuedeReservarProfesor': 1},
                       timeout=10)
        canchas = r.json().get('Tabla', {}).get('Canchas', {})
    except Exception as e:
        return {'ok': False, 'msg': f'Error canchas: {e}'}

    cancha = canchas.get(str(court))
    if not cancha:
        return {'ok': False, 'msg': f'Cancha {court} no existe en este turno'}
    if 'btn-free' not in (cancha.get('UsoCSS') or ''):
        return {'ok': False, 'msg': f'Cancha {court} no disponible ({cancha.get("UsoCSS", "?")} )'}

    log_reserva(f'  Cancha {court} libre (HorarioId={cancha["HorarioId"]})', 'info')

    # Buscar jugadores
    ujs = []
    for p in players:
        q = p['nombre'].split()[0] if p.get('nombre') else ''
        if not q: continue
        try:
            r = session.get(f'{BASE}//ingresodereserva/obtenerUsuariosSinProfesor.php',
                           params={'UsuarioNombre': q}, timeout=10)
            lista = r.json().get('Tabla', {}).get('Usuarios', [])
            match = next((u for u in lista if u['UsuarioNumero'] == p.get('socio', '')), lista[0] if lista else None)
            if match:
                ujs.append({'UsuarioId': match['UsuarioId']})
                log_reserva(f'  Jugador: {match["UsuarioNombre"]}', 'info')
            else:
                log_reserva(f'  ⚠ No encontré "{p["nombre"]}"', 'warn')
        except Exception as e:
            log_reserva(f'  ⚠ Error buscando "{p.get("nombre")}": {e}', 'warn')

    if not ujs:
        return {'ok': False, 'msg': 'No se encontró ningún jugador'}

    # Prereservar
    try:
        session.get(f'{BASE}//ingresodereserva/prereservarCancha.php',
                   params={'HorarioId': cancha['HorarioId'], 'UsuarioId': ujs[0]['UsuarioId'],
                           'ReservaFecha': fecha}, timeout=10)
    except: pass

    # Reserva final
    try:
        r = session.post(f'{BASE}//ingresodereserva/realizarReserva.php', json={
            'CanchaId':          cancha['CanchaId'],
            'ProfesorId':        0,
            'TurnoId':           turno['TurnoId'],
            'ReservaFecha':      fecha_ddmm,
            'HorarioId':         cancha['HorarioId'],
            'UsuariosJugadores': ujs,
            'UsoId':             cancha['UsoId'],
            'UsoIdReal':         cancha['UsoIdReal'],
        }, timeout=15)
        data = r.json()
        if data.get('Ejecucion') and 'Error' not in (data.get('Mensaje') or ''):
            return {'ok': True}
        return {'ok': False, 'msg': data.get('Mensaje', 'Reserva rechazada')}
    except Exception as e:
        return {'ok': False, 'msg': str(e)}


def guardar_resultado(msg):
    with get_db() as db:
        db.execute('UPDATE config SET ultimo_resultado=?, ultima_ejecucion=? WHERE id=1',
                   (msg, datetime.now().isoformat()))
        db.commit()


# ── SCHEDULER ─────────────────────────────────────────
scheduler = BackgroundScheduler(timezone='America/Argentina/Buenos_Aires')

def job_reserva():
    log_reserva('🕘 Scheduler disparado — 21:00 ART', 'info')
    ejecutar_reserva()

scheduler.add_job(job_reserva, 'cron', hour=21, minute=0,
                  id='reserva_diaria', replace_existing=True)

def job_ping():
    """Keep-alive: evita que Render duerma el servidor en plan gratuito"""
    try:
        import os
        url = os.environ.get('RENDER_URL', '')
        if url:
            requests.get(url + '/health', timeout=5)
            log.info('Keep-alive ping OK')
    except: pass

scheduler.add_job(job_ping, 'interval', minutes=10, id='keep_alive', replace_existing=True)

# ── MAIN ──────────────────────────────────────────────
if __name__ == '__main__':
    init_db()
    scheduler.start()
    log.info('Scheduler iniciado — reserva automática a las 21:00 ART')
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
else:
    init_db()
    scheduler.start()
