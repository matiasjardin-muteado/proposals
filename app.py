import os
import json
import secrets
from datetime import datetime, timedelta
from functools import wraps
from flask import (Flask, request, render_template, redirect, url_for,
                   session, jsonify, abort, flash, Response)
from database import (db, init_db, EmpresaModel, ClienteModel, PropuestaModel,
                      SesionModel, TrackingEventModel, Cliente, Propuesta,
                      Empresa, TrackingStats)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-prod')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')

init_db(app)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def get_empresa_slug():
    host = request.host.split(':')[0].lower()
    default_empresa = os.environ.get('DEFAULT_EMPRESA', 'muteado')

    # Railway preview domains and local hosts don't encode the company slug
    # in a stable first subdomain, so we fall back to the configured default.
    if (
        host in ('localhost', '127.0.0.1')
        or host.endswith('.localhost')
        or host.endswith('.up.railway.app')
        or host.endswith('.railway.app')
    ):
        return default_empresa

    parts = host.split('.')
    if len(parts) >= 3:
        return parts[0]
    return default_empresa


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated


def inject_tracking(html_content, propuesta_id):
    script = f"""
<script>
(function() {{
  var PROPUESTA_ID = {propuesta_id};
  var BASE = window.location.protocol + '//' + window.location.host;
  var sessionId = null;
  var startTime = Date.now();
  var activeTime = 0;
  var lastActive = Date.now();
  var isActive = true;
  var maxScroll = 0;

  function send(url, data) {{
    var json = JSON.stringify(data);
    if (navigator.sendBeacon) {{
      var blob = new Blob([json], {{type: 'application/json'}});
      navigator.sendBeacon(url, blob);
    }} else {{
      fetch(url, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:json, keepalive:true}});
    }}
  }}

  function track(type, data) {{
    if (!sessionId) return;
    send(BASE + '/t/event', {{session_id: sessionId, event_type: type, data: data || {{}}}});
  }}

  // Init session
  fetch(BASE + '/t/session', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{
      propuesta_id: PROPUESTA_ID,
      screen: window.screen.width + 'x' + window.screen.height,
      referrer: document.referrer || '',
      tz: Intl.DateTimeFormat().resolvedOptions().timeZone
    }})
  }}).then(function(r) {{ return r.json(); }}).then(function(d) {{
    sessionId = d.session_id;
    track('page_view', {{
      viewport: window.innerWidth + 'x' + window.innerHeight,
      screen: window.screen.width + 'x' + window.screen.height,
      tz: Intl.DateTimeFormat().resolvedOptions().timeZone
    }});
  }});

  // Scroll depth
  window.addEventListener('scroll', function() {{
    var total = document.documentElement.scrollHeight - window.innerHeight;
    if (total <= 0) return;
    var pct = Math.round((window.scrollY / total) * 100);
    if (pct > maxScroll) {{
      maxScroll = pct;
      var milestones = [25, 50, 75, 90, 100];
      milestones.forEach(function(m) {{
        if (pct >= m && maxScroll === pct) track('scroll_' + m, {{percent: pct}});
      }});
    }}
  }}, {{passive: true}});

  // Active time
  document.addEventListener('visibilitychange', function() {{
    if (document.hidden) {{
      if (isActive) {{ activeTime += Date.now() - lastActive; isActive = false; }}
      track('tab_hidden', {{active_s: Math.round(activeTime/1000)}});
    }} else {{
      isActive = true; lastActive = Date.now();
      track('tab_visible', {{}});
    }}
  }});

  window.addEventListener('focus', function() {{
    if (!isActive) {{ isActive = true; lastActive = Date.now(); }}
  }});
  window.addEventListener('blur', function() {{
    if (isActive) {{ activeTime += Date.now() - lastActive; isActive = false; }}
  }});

  // Heartbeat
  setInterval(function() {{
    if (isActive) activeTime += 30000;
    track('heartbeat', {{
      total_s: Math.round((Date.now()-startTime)/1000),
      active_s: Math.round(activeTime/1000),
      scroll: maxScroll
    }});
  }}, 30000);

  // Click tracking
  document.addEventListener('click', function(e) {{
    var el = e.target;
    var tag = el.tagName ? el.tagName.toLowerCase() : '';
    track('click', {{
      tag: tag,
      text: (el.innerText || '').slice(0,60),
      href: el.href || ''
    }});
  }});

  // Exit
  window.addEventListener('beforeunload', function() {{
    if (isActive) activeTime += Date.now() - lastActive;
    send(BASE + '/t/event', {{
      session_id: sessionId,
      event_type: 'page_exit',
      data: {{
        total_s: Math.round((Date.now()-startTime)/1000),
        active_s: Math.round(activeTime/1000),
        scroll: maxScroll
      }}
    }});
  }});
}})();
</script>
"""
    if '</body>' in html_content:
        return html_content.replace('</body>', script + '</body>', 1)
    return html_content + script


# ─── Tracking API ────────────────────────────────────────────────────────────

@app.route('/t/session', methods=['POST'])
def create_session():
    data = request.json or {}
    propuesta_id = data.get('propuesta_id')
    if not propuesta_id:
        return jsonify({'error': 'missing propuesta_id'}), 400
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if ip and ',' in ip:
        ip = ip.split(',')[0].strip()
    ua = request.headers.get('User-Agent', '')
    token = secrets.token_urlsafe(16)
    SesionModel.create(propuesta_id, token, ip, ua, data)
    return jsonify({'session_id': token})


@app.route('/t/event', methods=['POST'])
def track_event():
    data = request.json or {}
    session_token = data.get('session_id')
    event_type = data.get('event_type')
    payload = data.get('data', {})
    if session_token and event_type:
        TrackingEventModel.create(session_token, event_type, payload)
    return jsonify({'ok': True})


# ─── Admin ───────────────────────────────────────────────────────────────────

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['admin_logged_in'] = True
            return redirect(url_for('admin_dashboard'))
        flash('Contraseña incorrecta')
    return render_template('admin/login.html')


@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    return redirect(url_for('admin_login'))


@app.route('/admin')
@admin_required
def admin_dashboard():
    propuestas = Propuesta.get_all_with_stats()
    clientes = Cliente.get_all()
    return render_template('admin/dashboard.html', propuestas=propuestas, clientes=clientes)


@app.route('/admin/clientes', methods=['GET', 'POST'])
@admin_required
def admin_clientes():
    if request.method == 'POST':
        nombre = request.form.get('nombre', '').strip()
        slug = request.form.get('slug', '').strip().lower()
        empresa_slug = request.form.get('empresa', '').strip()
        password = request.form.get('password', '').strip()
        if nombre and slug and empresa_slug and password:
            empresa = Empresa.get_by_slug(empresa_slug)
            if empresa:
                Cliente.create(nombre, slug, password, empresa.id)
                flash(f'Cliente "{nombre}" creado.')
            else:
                flash('Empresa no encontrada.')
        else:
            flash('Completá todos los campos.')
        return redirect(url_for('admin_clientes'))

    clientes = Cliente.get_all_with_empresa()
    empresas = Empresa.get_all()
    return render_template('admin/clientes.html', clientes=clientes, empresas=empresas)


@app.route('/admin/clientes/<int:id>/delete', methods=['POST'])
@admin_required
def admin_delete_cliente(id):
    Cliente.delete(id)
    flash('Cliente eliminado.')
    return redirect(url_for('admin_clientes'))


@app.route('/admin/propuestas/nueva', methods=['GET', 'POST'])
@admin_required
def admin_propuesta_nueva():
    if request.method == 'POST':
        titulo = request.form.get('titulo', '').strip()
        cliente_id = request.form.get('cliente_id')
        html_file = request.files.get('html_file')
        expira = request.form.get('expira') == 'on'
        dias = int(request.form.get('dias_expiracion', 30))

        if not (titulo and cliente_id and html_file and html_file.filename.endswith('.html')):
            flash('Completá todos los campos y asegurate de subir un archivo .html')
        else:
            html_content = html_file.read().decode('utf-8')
            expira_en = datetime.utcnow() + timedelta(days=dias) if expira else None
            p = Propuesta.create(titulo, html_content, int(cliente_id), expira_en)
            flash(f'Propuesta "{titulo}" creada.')
            return redirect(url_for('admin_dashboard'))

    clientes = Cliente.get_all_with_empresa()
    return render_template('admin/propuesta_nueva.html', clientes=clientes)


@app.route('/admin/propuestas/<int:id>/toggle', methods=['POST'])
@admin_required
def admin_toggle_propuesta(id):
    Propuesta.toggle_activa(id)
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/propuestas/<int:id>/delete', methods=['POST'])
@admin_required
def admin_delete_propuesta(id):
    Propuesta.delete(id)
    flash('Propuesta eliminada.')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/propuestas/<int:id>/tracking')
@admin_required
def admin_tracking(id):
    propuesta = PropuestaModel.query.get_or_404(id)
    stats = TrackingStats.get_for_propuesta(id)
    return render_template('admin/tracking.html', propuesta=propuesta, stats=stats)


# ─── Client ──────────────────────────────────────────────────────────────────

@app.route('/<slug>', methods=['GET', 'POST'])
def propuesta_view(slug):
    if slug in ('admin', 'favicon.ico', 'static', 't'):
        abort(404)

    empresa_slug = get_empresa_slug()
    empresa = Empresa.get_by_slug(empresa_slug)
    if not empresa:
        abort(404)

    cliente = Cliente.get_by_slug_and_empresa(slug, empresa.id)
    if not cliente:
        abort(404)

    propuesta = Propuesta.get_active_by_cliente(cliente.id)
    if not propuesta:
        abort(404)

    if propuesta.expira_en and datetime.utcnow() > propuesta.expira_en:
        return render_template('client/expirada.html', empresa=empresa, cliente=cliente)

    auth_key = f'auth_prop_{propuesta.id}'

    if request.method == 'POST':
        password = request.form.get('password', '')
        if Cliente.check_password(cliente.id, password):
            session[auth_key] = True
        else:
            flash('Contraseña incorrecta')
            return render_template('client/password.html', cliente=cliente, empresa=empresa)

    if not session.get(auth_key):
        return render_template('client/password.html', cliente=cliente, empresa=empresa)

    html = inject_tracking(propuesta.html_content, propuesta.id)
    return Response(html, mimetype='text/html')


# ─── Entry ───────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    app.run(debug=True, port=5001)
