import os
import json
import secrets
from datetime import datetime, timedelta
from functools import wraps
from flask import (Flask, request, render_template, redirect, url_for,
                   session, jsonify, abort, flash, Response, send_file)
from io import BytesIO
from database import (db, init_db, EmpresaModel, ClienteModel, DocumentoModel,
                      SesionModel, TrackingEventModel, Cliente, Documento,
                      Empresa, TrackingStats, slugify)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-prod')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')

init_db(app)

RESERVED_SLUGS = {'admin', 'favicon.ico', 'static', 't', 'robots.txt'}

DEFAULT_DOMAIN_MAP = {
    'documentos.muteado.com': 'muteado',
    'documentos.grupocartago.com': 'cartago',
    'documentos.pragmato.com.ar': 'pragmato',
    'muteado.matiasjardin.com': 'muteado',
    'cartago.matiasjardin.com': 'cartago',
    'pragmato.matiasjardin.com': 'pragmato',
    'operantio.matiasjardin.com': 'pragmato',
}


def _load_domain_map():
    raw = os.environ.get('DOMAIN_MAP', '').strip()
    if not raw:
        return DEFAULT_DOMAIN_MAP
    try:
        return {**DEFAULT_DOMAIN_MAP, **json.loads(raw)}
    except Exception:
        return DEFAULT_DOMAIN_MAP


DOMAIN_MAP = _load_domain_map()


# ─── Helpers ─────────────────────────────────────────────────────────────────

def get_empresa_slug():
    host = request.host.split(':')[0].lower()
    default_empresa = os.environ.get('DEFAULT_EMPRESA', 'muteado')

    if host in DOMAIN_MAP:
        return DOMAIN_MAP[host]

    if (
        host in ('localhost', '127.0.0.1')
        or host.endswith('.localhost')
        or host.endswith('.up.railway.app')
        or host.endswith('.railway.app')
    ):
        # Allow forcing empresa from subdomain in dev: muteado.localhost
        parts = host.split('.')
        if len(parts) >= 2 and parts[0] not in ('localhost', '127'):
            candidate = parts[0]
            if Empresa.get_by_slug(candidate):
                return candidate
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


def inject_tracking(html_content, documento_id):
    script = f"""
<script>
(function() {{
  var DOC_ID = {documento_id};
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

  fetch(BASE + '/t/session', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{
      documento_id: DOC_ID,
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

  setInterval(function() {{
    if (isActive) activeTime += 30000;
    track('heartbeat', {{
      total_s: Math.round((Date.now()-startTime)/1000),
      active_s: Math.round(activeTime/1000),
      scroll: maxScroll
    }});
  }}, 30000);

  document.addEventListener('click', function(e) {{
    var el = e.target;
    var tag = el.tagName ? el.tagName.toLowerCase() : '';
    track('click', {{
      tag: tag,
      text: (el.innerText || '').slice(0,60),
      href: el.href || ''
    }});
  }});

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
    documento_id = data.get('documento_id') or data.get('propuesta_id')
    if not documento_id:
        return jsonify({'error': 'missing documento_id'}), 400
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if ip and ',' in ip:
        ip = ip.split(',')[0].strip()
    ua = request.headers.get('User-Agent', '')
    token = secrets.token_urlsafe(16)
    SesionModel.create(documento_id, token, ip, ua, data)
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
    documentos = Documento.get_all_with_stats()
    clientes = Cliente.get_all()
    return render_template('admin/dashboard.html',
                           documentos=documentos, clientes=clientes,
                           domain_for=_domain_for_empresa)


def _domain_for_empresa(empresa_slug):
    reverse = {v: k for k, v in DOMAIN_MAP.items() if k.startswith('documentos.')}
    return reverse.get(empresa_slug, f'{empresa_slug}.matiasjardin.com')


@app.route('/admin/clientes', methods=['GET', 'POST'])
@admin_required
def admin_clientes():
    if request.method == 'POST':
        nombre = request.form.get('nombre', '').strip()
        slug = request.form.get('slug', '').strip().lower()
        empresa_slug = request.form.get('empresa', '').strip()
        password = request.form.get('password', '').strip()
        contacto = request.form.get('contacto', '').strip()
        if nombre and slug and empresa_slug and password:
            empresa = Empresa.get_by_slug(empresa_slug)
            if empresa:
                Cliente.create(nombre, slug, password, empresa.id, contacto=contacto)
                flash(f'Cliente "{nombre}" creado.')
            else:
                flash('Empresa no encontrada.')
        else:
            flash('Completá los campos obligatorios.')
        return redirect(url_for('admin_clientes'))

    clientes = Cliente.get_all_with_empresa()
    empresas = Empresa.get_all()
    return render_template('admin/clientes.html', clientes=clientes,
                           empresas=empresas, domain_for=_domain_for_empresa)


@app.route('/admin/clientes/<int:id>/delete', methods=['POST'])
@admin_required
def admin_delete_cliente(id):
    Cliente.delete(id)
    flash('Cliente eliminado.')
    return redirect(url_for('admin_clientes'))


@app.route('/admin/clientes/<int:id>/password', methods=['POST'])
@admin_required
def admin_update_cliente_password(id):
    password = request.form.get('password', '').strip()
    if not password:
        flash('Ingresá una nueva contraseña.')
        return redirect(url_for('admin_clientes'))

    cliente = Cliente.update_password(id, password)
    if cliente:
        flash(f'Contraseña actualizada para "{cliente.nombre}".')
    else:
        flash('Cliente no encontrado.')
    return redirect(url_for('admin_clientes'))


@app.route('/admin/documentos/nuevo', methods=['GET', 'POST'])
@admin_required
def admin_documento_nuevo():
    if request.method == 'POST':
        titulo = request.form.get('titulo', '').strip()
        cliente_id = request.form.get('cliente_id')
        slug_input = request.form.get('slug', '').strip().lower()
        upload = request.files.get('archivo')
        expira = request.form.get('expira') == 'on'
        dias = int(request.form.get('dias_expiracion', 30))

        if not (titulo and cliente_id and upload and upload.filename):
            flash('Completá todos los campos y subí un archivo.')
            return redirect(url_for('admin_documento_nuevo'))

        filename = upload.filename.lower()
        if not (filename.endswith('.html') or filename.endswith('.htm') or filename.endswith('.pdf')):
            flash('Solo se aceptan archivos .html o .pdf')
            return redirect(url_for('admin_documento_nuevo'))

        base_slug = slug_input if slug_input else titulo
        slug = Documento.unique_slug(int(cliente_id), base_slug)
        expira_en = datetime.utcnow() + timedelta(days=dias) if expira else None

        if filename.endswith('.pdf'):
            file_bytes = upload.read()
            Documento.create_pdf(titulo, slug, file_bytes, upload.filename,
                                 int(cliente_id), expira_en)
        else:
            html_content = upload.read().decode('utf-8')
            Documento.create_html(titulo, slug, html_content,
                                  int(cliente_id), expira_en)

        flash(f'Documento "{titulo}" creado.')
        return redirect(url_for('admin_dashboard'))

    clientes = Cliente.get_all_with_empresa()
    return render_template('admin/documento_nuevo.html', clientes=clientes)


@app.route('/admin/documentos/<int:id>/toggle', methods=['POST'])
@admin_required
def admin_toggle_documento(id):
    Documento.toggle_activa(id)
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/documentos/<int:id>/delete', methods=['POST'])
@admin_required
def admin_delete_documento(id):
    Documento.delete(id)
    flash('Documento eliminado.')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/documentos/<int:id>/tracking')
@admin_required
def admin_tracking(id):
    documento = DocumentoModel.query.get_or_404(id)
    stats = TrackingStats.get_for_documento(id)
    return render_template('admin/tracking.html',
                           documento=documento, stats=stats,
                           domain_for=_domain_for_empresa)


# ─── Client ──────────────────────────────────────────────────────────────────

def _resolve_cliente(slug):
    empresa_slug = get_empresa_slug()
    empresa = Empresa.get_by_slug(empresa_slug)
    if not empresa:
        abort(404)
    cliente = Cliente.get_by_slug_and_empresa(slug, empresa.id)
    if not cliente:
        abort(404)
    return empresa, cliente


def _cliente_authed(cliente_id):
    return session.get(f'auth_cliente_{cliente_id}', False)


def _render_password_page(empresa, cliente):
    return render_template('client/password.html', cliente=cliente, empresa=empresa)


@app.route('/<slug>', methods=['GET', 'POST'])
def cliente_landing(slug):
    if slug in RESERVED_SLUGS:
        abort(404)

    empresa, cliente = _resolve_cliente(slug)

    if request.method == 'POST':
        password = request.form.get('password', '')
        if Cliente.check_password(cliente.id, password):
            session[f'auth_cliente_{cliente.id}'] = True
        else:
            flash('Contraseña incorrecta')
            return _render_password_page(empresa, cliente)

    if not _cliente_authed(cliente.id):
        return _render_password_page(empresa, cliente)

    documentos = Documento.get_active_for_cliente(cliente.id)
    documentos = [d for d in documentos if not (d.expira_en and datetime.utcnow() > d.expira_en)]

    if not documentos:
        return render_template('client/expirada.html', empresa=empresa, cliente=cliente)

    if len(documentos) == 1:
        return redirect(url_for('cliente_documento', slug=cliente.slug, doc_slug=documentos[0].slug))

    return render_template('client/landing.html',
                           empresa=empresa, cliente=cliente, documentos=documentos)


@app.route('/<slug>/<doc_slug>', methods=['GET', 'POST'])
def cliente_documento(slug, doc_slug):
    if slug in RESERVED_SLUGS:
        abort(404)

    empresa, cliente = _resolve_cliente(slug)

    if request.method == 'POST':
        password = request.form.get('password', '')
        if Cliente.check_password(cliente.id, password):
            session[f'auth_cliente_{cliente.id}'] = True
        else:
            flash('Contraseña incorrecta')
            return _render_password_page(empresa, cliente)

    if not _cliente_authed(cliente.id):
        return _render_password_page(empresa, cliente)

    documento = Documento.get_by_cliente_and_slug(cliente.id, doc_slug)
    if not documento or not documento.activa:
        abort(404)

    if documento.expira_en and datetime.utcnow() > documento.expira_en:
        return render_template('client/expirada.html', empresa=empresa, cliente=cliente)

    if documento.tipo == 'pdf':
        pdf_url = url_for('cliente_documento_pdf', slug=cliente.slug, doc_slug=documento.slug)
        return render_template('client/pdf_viewer.html',
                               empresa=empresa, cliente=cliente,
                               documento=documento, pdf_url=pdf_url)

    html = inject_tracking(documento.html_content or '', documento.id)
    return Response(html, mimetype='text/html')


@app.route('/<slug>/<doc_slug>/file.pdf')
def cliente_documento_pdf(slug, doc_slug):
    if slug in RESERVED_SLUGS:
        abort(404)

    empresa, cliente = _resolve_cliente(slug)
    if not _cliente_authed(cliente.id):
        abort(403)

    documento = Documento.get_by_cliente_and_slug(cliente.id, doc_slug)
    if not documento or documento.tipo != 'pdf' or not documento.activa:
        abort(404)
    if documento.expira_en and datetime.utcnow() > documento.expira_en:
        abort(410)

    return send_file(
        BytesIO(documento.file_data),
        mimetype='application/pdf',
        as_attachment=False,
        download_name=documento.file_name or f'{documento.slug}.pdf',
    )


# ─── Entry ───────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    app.run(debug=True, port=5001)
