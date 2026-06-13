import os
import csv
import json
import secrets
import smtplib
import threading
import hashlib
import hmac
import time
from datetime import datetime, timedelta
from email.message import EmailMessage
from functools import wraps
from io import BytesIO, StringIO
from flask import (Flask, request, render_template, redirect, url_for,
                   session, jsonify, abort, flash, Response, send_file)
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename
from database import (db, init_db, EmpresaModel, ClienteModel, DocumentoModel,
                      AccessKeyModel, SesionModel, TrackingEventModel, Cliente,
                      Documento, Empresa, AccessKey, TrackingStats, slugify)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-prod')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')
ADMIN_PASSWORD_HASH = os.environ.get('ADMIN_PASSWORD_HASH', '')

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

def _env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


def _split_emails(value):
    return [e.strip() for e in (value or '').replace(';', ',').split(',') if e.strip()]


def _client_ip():
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if ip and ',' in ip:
        ip = ip.split(',')[0].strip()
    return ip or 'unknown'


RATE_LIMITS = {}


def _rate_limited(name, limit=8, window_seconds=300):
    now = time.time()
    key = f'{name}:{_client_ip()}'
    attempts = [ts for ts in RATE_LIMITS.get(key, []) if now - ts < window_seconds]
    if len(attempts) >= limit:
        RATE_LIMITS[key] = attempts
        return True
    attempts.append(now)
    RATE_LIMITS[key] = attempts
    return False


def csrf_token():
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['_csrf_token'] = token
    return token


app.jinja_env.globals['csrf_token'] = csrf_token


@app.before_request
def _csrf_protect():
    if request.method != 'POST':
        return None
    if request.endpoint in ('create_session', 'track_event'):
        return None
    form_token = request.form.get('_csrf_token', '')
    if not form_token or not hmac.compare_digest(form_token, session.get('_csrf_token', '')):
        abort(400)
    return None


def _tracking_token(documento_id):
    secret = app.secret_key.encode('utf-8')
    payload = str(documento_id).encode('utf-8')
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _admin_password_ok(password):
    if ADMIN_PASSWORD_HASH:
        return check_password_hash(ADMIN_PASSWORD_HASH, password or '')
    return hmac.compare_digest(password or '', ADMIN_PASSWORD)


def _parse_expiration_date(value):
    value = (value or '').strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').replace(hour=23, minute=59, second=59)
    except ValueError:
        return None


def _read_upload(upload):
    data = upload.read()
    upload.seek(0)
    return data


def _validate_html_file(file_bytes):
    if not file_bytes:
        raise ValueError('El HTML está vacío.')
    try:
        html = file_bytes.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('El HTML debe estar guardado como UTF-8.')
    lowered = html.lower()
    required = ('<html', '<head', '<body', '</html>')
    if not all(tag in lowered for tag in required):
        raise ValueError('El HTML debe ser un documento completo con html, head y body.')
    if '<script' in lowered and 'data-track-section' not in lowered:
        # Scripts are allowed, but this catches many accidental analytics exports.
        app.logger.info('HTML subido contiene scripts propios.')
    return html


def _validate_pdf_file(file_bytes):
    if not file_bytes:
        raise ValueError('El PDF está vacío.')
    if not file_bytes.startswith(b'%PDF'):
        raise ValueError('El archivo no parece ser un PDF válido.')
    if b'%%EOF' not in file_bytes[-4096:]:
        raise ValueError('El PDF parece estar incompleto o corrupto.')
    try:
        from pypdf import PdfReader
        PdfReader(BytesIO(file_bytes), strict=False)
    except ImportError:
        pass
    except Exception:
        raise ValueError('El PDF no se pudo leer correctamente.')
    return file_bytes


def _validate_upload(upload, required=True):
    if not upload or not upload.filename:
        if required:
            raise ValueError('Subí un archivo HTML o PDF.')
        return None
    filename = secure_filename(upload.filename)
    lowered = filename.lower()
    if not (lowered.endswith('.html') or lowered.endswith('.htm') or lowered.endswith('.pdf')):
        raise ValueError('Solo se aceptan archivos .html, .htm o .pdf.')
    file_bytes = _read_upload(upload)
    if lowered.endswith('.pdf'):
        return {
            'tipo': 'pdf',
            'file_data': _validate_pdf_file(file_bytes),
            'file_name': filename,
            'html_content': None,
        }
    return {
        'tipo': 'html',
        'html_content': _validate_html_file(file_bytes),
        'file_data': None,
        'file_name': None,
    }


def _send_email_async(subject, body):
    smtp_host = os.environ.get('SMTP_HOST', '').strip()
    recipients = _split_emails(os.environ.get('NOTIFY_EMAILS'))
    if not smtp_host or not recipients:
        return

    smtp_user = os.environ.get('SMTP_USER', '').strip()
    smtp_password = os.environ.get('SMTP_PASSWORD', '')
    from_addr = os.environ.get('SMTP_FROM', '').strip() or smtp_user or recipients[0]
    try:
        smtp_port = int(os.environ.get('SMTP_PORT', '587'))
    except ValueError:
        smtp_port = 587

    use_ssl = _env_bool('SMTP_SSL', smtp_port == 465)
    use_tls = _env_bool('SMTP_TLS', not use_ssl)

    def worker():
        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From'] = from_addr
        msg['To'] = ', '.join(recipients)
        msg.set_content(body)

        try:
            if use_ssl:
                server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10)
            else:
                server = smtplib.SMTP(smtp_host, smtp_port, timeout=10)
            with server:
                if use_tls:
                    server.starttls()
                if smtp_user and smtp_password:
                    server.login(smtp_user, smtp_password)
                server.send_message(msg)
        except Exception as exc:
            app.logger.warning('No se pudo enviar email de notificacion: %s', exc)

    threading.Thread(target=worker, daemon=True).start()


def _notify_document_open(documento, sesion, extra):
    if not _env_bool('NOTIFY_ON_OPEN', True):
        return

    cliente = documento.cliente
    empresa = cliente.empresa
    document_url = url_for('cliente_documento',
                           slug=cliente.slug, doc_slug=documento.slug,
                           _external=True)
    tracking_url = url_for('admin_tracking', id=documento.id, _external=True)
    subject = f'Documento abierto: {documento.titulo}'
    body = f"""Se abrió un documento.

Empresa: {empresa.nombre}
Cliente: {cliente.nombre}
Contacto: {cliente.contacto or '-'}
Documento: {documento.titulo}
Tipo: {documento.tipo.upper()}
Abierto por: {sesion.viewer_name or '-'}
Tipo de acceso: {sesion.access_type or '-'}

Fecha/hora UTC: {sesion.created_at.strftime('%Y-%m-%d %H:%M:%S')}
IP: {sesion.ip or '-'}
Pantalla: {sesion.screen or '-'}
Zona horaria: {sesion.tz or '-'}
Referrer: {extra.get('referrer') or '-'}
User-Agent: {(sesion.user_agent or '-')[:240]}

Documento:
{document_url}

Tracking:
{tracking_url}
"""
    _send_email_async(subject, body)

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


def _cliente_session_key(cliente_id, suffix):
    return f'auth_cliente_{cliente_id}_{suffix}'


def _set_cliente_access(cliente, access_type, viewer_name, access_key_id=None):
    session[f'auth_cliente_{cliente.id}'] = True
    session[_cliente_session_key(cliente.id, 'access_type')] = access_type
    session[_cliente_session_key(cliente.id, 'viewer_name')] = viewer_name
    session[_cliente_session_key(cliente.id, 'access_key_id')] = access_key_id


def _current_access(cliente_id):
    return {
        'access_type': session.get(_cliente_session_key(cliente_id, 'access_type'), 'cliente'),
        'viewer_name': session.get(_cliente_session_key(cliente_id, 'viewer_name')),
        'access_key_id': session.get(_cliente_session_key(cliente_id, 'access_key_id')),
    }


def inject_tracking(html_content, documento_id):
    tracking_token = _tracking_token(documento_id)
    script = f"""
<script>
(function() {{
  var DOC_ID = {documento_id};
  var TRACKING_TOKEN = "{tracking_token}";
  var BASE = window.location.protocol + '//' + window.location.host;
  var sessionId = null;
  var startTime = Date.now();
  var activeTime = 0;
  var lastActive = Date.now();
  var isActive = true;
  var maxScroll = 0;
  var sentScroll = {{}};
  var sectionTimes = {{}};
  var currentSection = 'Documento';
  var markers = [];

  function textLabel(text) {{
    return (text || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
  }}

  function labelFor(el, index) {{
    return textLabel(
      el.getAttribute('data-track-section') ||
      el.getAttribute('aria-label') ||
      el.getAttribute('id') ||
      el.innerText
    ) || ('Seccion ' + (index + 1));
  }}

  function markerTop(marker) {{
    return marker.el.getBoundingClientRect().top + window.scrollY;
  }}

  function collectMarkers() {{
    var nodes = Array.prototype.slice.call(
      document.querySelectorAll('[data-track-section], section, article, h1, h2, h3')
    ).filter(function(el) {{
      var rect = el.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    }});

    markers = nodes.map(function(el, index) {{
      return {{el: el, label: labelFor(el, index)}};
    }});

    if (!markers.length) {{
      markers = [{{el: document.body, label: 'Documento'}}];
    }}
    currentSection = getCurrentSection();
  }}

  function getCurrentSection() {{
    if (!markers.length) return 'Documento';
    var pos = window.scrollY + (window.innerHeight * 0.35);
    var current = markers[0].label;
    markers.forEach(function(marker) {{
      if (markerTop(marker) <= pos) current = marker.label;
    }});
    return current;
  }}

  function sectionTimesSeconds() {{
    var out = {{}};
    Object.keys(sectionTimes).forEach(function(label) {{
      out[label] = Math.round(sectionTimes[label] / 1000);
    }});
    return out;
  }}

  function addActiveTime() {{
    if (!isActive) return;
    var now = Date.now();
    var delta = now - lastActive;
    if (delta > 0 && delta < 120000) {{
      activeTime += delta;
      sectionTimes[currentSection] = (sectionTimes[currentSection] || 0) + delta;
    }}
    lastActive = now;
  }}

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

  collectMarkers();

  fetch(BASE + '/t/session', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{
      documento_id: DOC_ID,
      tracking_token: TRACKING_TOKEN,
      screen: window.screen.width + 'x' + window.screen.height,
      referrer: document.referrer || '',
      tz: Intl.DateTimeFormat().resolvedOptions().timeZone
    }})
  }}).then(function(r) {{ return r.json(); }}).then(function(d) {{
    sessionId = d.session_id;
    track('page_view', {{
      viewport: window.innerWidth + 'x' + window.innerHeight,
      screen: window.screen.width + 'x' + window.screen.height,
      tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
      section: currentSection
    }});
    track('section_view', {{section: currentSection}});
  }});

  var scrollTimer = null;
  window.addEventListener('scroll', function() {{
    var total = document.documentElement.scrollHeight - window.innerHeight;
    if (total > 0) {{
      var pct = Math.round((window.scrollY / total) * 100);
      if (pct > maxScroll) {{
        maxScroll = pct;
        var milestones = [25, 50, 75, 90, 100];
        milestones.forEach(function(m) {{
          if (pct >= m && !sentScroll[m]) {{
            sentScroll[m] = true;
            track('scroll_' + m, {{percent: pct}});
          }}
        }});
      }}
    }}
    if (scrollTimer) window.clearTimeout(scrollTimer);
    scrollTimer = window.setTimeout(function() {{
      addActiveTime();
      var next = getCurrentSection();
      if (next !== currentSection) {{
        currentSection = next;
        track('section_view', {{section: currentSection}});
      }}
    }}, 120);
  }}, {{passive: true}});

  document.addEventListener('visibilitychange', function() {{
    if (document.hidden) {{
      addActiveTime();
      isActive = false;
      track('tab_hidden', {{
        active_s: Math.round(activeTime/1000),
        section_times: sectionTimesSeconds()
      }});
    }} else {{
      isActive = true;
      lastActive = Date.now();
      currentSection = getCurrentSection();
      track('tab_visible', {{}});
    }}
  }});

  window.addEventListener('focus', function() {{
    if (!isActive) {{
      isActive = true;
      lastActive = Date.now();
      currentSection = getCurrentSection();
    }}
  }});
  window.addEventListener('blur', function() {{
    if (isActive) {{
      addActiveTime();
      isActive = false;
    }}
  }});

  setInterval(function() {{
    addActiveTime();
    track('heartbeat', {{
      total_s: Math.round((Date.now()-startTime)/1000),
      active_s: Math.round(activeTime/1000),
      scroll: maxScroll,
      section: currentSection,
      section_times: sectionTimesSeconds()
    }});
  }}, 30000);

  document.addEventListener('click', function(e) {{
    var el = e.target;
    var tag = el.tagName ? el.tagName.toLowerCase() : '';
    track('click', {{
      tag: tag,
      text: (el.innerText || '').slice(0,60),
      href: el.href || '',
      section: currentSection
    }});
  }});

  window.addEventListener('resize', function() {{
    collectMarkers();
  }});

  window.addEventListener('beforeunload', function() {{
    addActiveTime();
    send(BASE + '/t/event', {{
      session_id: sessionId,
      event_type: 'page_exit',
      data: {{
        total_s: Math.round((Date.now()-startTime)/1000),
        active_s: Math.round(activeTime/1000),
        scroll: maxScroll,
        section: currentSection,
        section_times: sectionTimesSeconds()
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
    try:
        documento_id = int(documento_id)
    except (TypeError, ValueError):
        return jsonify({'error': 'invalid documento_id'}), 400

    documento = DocumentoModel.query.get(documento_id)
    if not documento:
        return jsonify({'error': 'documento not found'}), 404
    if not hmac.compare_digest(data.get('tracking_token', ''),
                               _tracking_token(documento_id)):
        return jsonify({'error': 'invalid tracking token'}), 403

    access = _current_access(documento.cliente_id)
    if not access.get('viewer_name'):
        access['viewer_name'] = documento.cliente.contacto or documento.cliente.nombre
    data.update(access)
    ip = _client_ip()
    ua = request.headers.get('User-Agent', '')
    token = secrets.token_urlsafe(16)
    sesion = SesionModel.create(documento_id, token, ip, ua, data)
    _notify_document_open(documento, sesion, data)
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
        if _rate_limited('admin_login', limit=8, window_seconds=300):
            flash('Demasiados intentos. Esperá unos minutos y probá de nuevo.')
            return render_template('admin/login.html'), 429
        if _admin_password_ok(request.form.get('password')):
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
    filters = {
        'empresa': request.args.get('empresa', '').strip(),
        'cliente_id': request.args.get('cliente_id', '').strip(),
        'tipo': request.args.get('tipo', '').strip(),
        'estado': request.args.get('estado', '').strip(),
        'opened_by': request.args.get('opened_by', '').strip(),
    }
    filters = {k: v for k, v in filters.items() if v}
    if filters.get('cliente_id') and not filters['cliente_id'].isdigit():
        filters.pop('cliente_id', None)
    documentos = Documento.get_all_with_stats(filters)
    clientes = Cliente.get_all()
    empresas = Empresa.get_all()
    return render_template('admin/dashboard.html',
                           documentos=documentos, clientes=clientes,
                           empresas=empresas, filters=filters,
                           domain_for=_domain_for_empresa)


def _domain_for_empresa(empresa_slug):
    reverse = {v: k for k, v in DOMAIN_MAP.items() if k.startswith('documentos.')}
    return reverse.get(empresa_slug, f'{empresa_slug}.matiasjardin.com')


@app.route('/admin/clientes', methods=['GET', 'POST'])
@admin_required
def admin_clientes():
    if request.method == 'POST':
        nombre = request.form.get('nombre', '').strip()
        slug = slugify(request.form.get('slug', '').strip())
        empresa_slug = request.form.get('empresa', '').strip()
        password = request.form.get('password', '').strip()
        contacto = request.form.get('contacto', '').strip()
        if nombre and slug and empresa_slug and password:
            empresa = Empresa.get_by_slug(empresa_slug)
            if empresa:
                if not Cliente.slug_available(slug, empresa.id):
                    flash('Ese slug ya existe para esa empresa.')
                else:
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


@app.route('/admin/claves', methods=['GET', 'POST'])
@admin_required
def admin_claves():
    generated_password = None
    if request.method == 'POST':
        nombre = request.form.get('nombre', '').strip()
        role = request.form.get('role', '').strip() or 'Clave interna'
        scope_type = request.form.get('scope_type', 'global').strip()
        empresa_id = request.form.get('empresa_id') or None
        cliente_id = request.form.get('cliente_id') or None
        password = request.form.get('password', '').strip()

        if scope_type not in ('global', 'empresa', 'cliente'):
            flash('Alcance inválido.')
            return redirect(url_for('admin_claves'))
        if scope_type == 'global':
            empresa_id = None
            cliente_id = None
        elif scope_type == 'empresa':
            cliente_id = None
            if not empresa_id:
                flash('Elegí una empresa para esta clave.')
                return redirect(url_for('admin_claves'))
        elif scope_type == 'cliente':
            empresa_id = None
            if not cliente_id:
                flash('Elegí un cliente para esta clave.')
                return redirect(url_for('admin_claves'))

        if not nombre:
            flash('Ingresá el nombre de la persona.')
            return redirect(url_for('admin_claves'))

        if not password:
            password = secrets.token_urlsafe(10)
            generated_password = password

        AccessKey.create(nombre, role, scope_type, password,
                         int(empresa_id) if empresa_id else None,
                         int(cliente_id) if cliente_id else None)
        if generated_password:
            flash(f'Clave creada para {nombre}: {generated_password}')
        else:
            flash(f'Clave creada para {nombre}.')
        return redirect(url_for('admin_claves'))

    keys = AccessKey.get_all()
    empresas = Empresa.get_all()
    clientes = Cliente.get_all_with_empresa()
    return render_template('admin/claves.html', keys=keys,
                           empresas=empresas, clientes=clientes)


@app.route('/admin/claves/<int:id>/toggle', methods=['POST'])
@admin_required
def admin_toggle_clave(id):
    key = AccessKey.toggle_active(id)
    if key:
        flash(f'Clave {"activada" if key.active else "pausada"} para {key.nombre}.')
    return redirect(url_for('admin_claves'))


@app.route('/admin/claves/<int:id>/regenerate', methods=['POST'])
@admin_required
def admin_regenerate_clave(id):
    key = AccessKey.get_by_id(id)
    if not key:
        flash('Clave no encontrada.')
        return redirect(url_for('admin_claves'))
    password = secrets.token_urlsafe(10)
    AccessKey.update_password(id, password)
    flash(f'Nueva clave para {key.nombre}: {password}')
    return redirect(url_for('admin_claves'))


@app.route('/admin/claves/<int:id>/delete', methods=['POST'])
@admin_required
def admin_delete_clave(id):
    AccessKey.delete(id)
    flash('Clave interna eliminada.')
    return redirect(url_for('admin_claves'))


@app.route('/admin/documentos/nuevo', methods=['GET', 'POST'])
@admin_required
def admin_documento_nuevo():
    if request.method == 'POST':
        titulo = request.form.get('titulo', '').strip()
        cliente_id = request.form.get('cliente_id')
        slug_input = slugify(request.form.get('slug', '').strip())
        upload = request.files.get('archivo')
        expira = request.form.get('expira') == 'on'
        try:
            dias = int(request.form.get('dias_expiracion', 30))
        except ValueError:
            dias = 30
        dias = max(1, min(dias, 365))

        if not (titulo and cliente_id and upload and upload.filename):
            flash('Completá todos los campos y subí un archivo.')
            return redirect(url_for('admin_documento_nuevo'))

        try:
            cliente_id_int = int(cliente_id)
        except (TypeError, ValueError):
            flash('Cliente inválido.')
            return redirect(url_for('admin_documento_nuevo'))

        base_slug = slug_input if slug_input else titulo
        slug = Documento.unique_slug(cliente_id_int, base_slug)
        expira_en = datetime.utcnow() + timedelta(days=dias) if expira else None

        try:
            prepared = _validate_upload(upload)
        except ValueError as exc:
            flash(str(exc))
            return redirect(url_for('admin_documento_nuevo'))

        if prepared['tipo'] == 'pdf':
            Documento.create_pdf(titulo, slug, prepared['file_data'],
                                 prepared['file_name'], cliente_id_int, expira_en)
        else:
            Documento.create_html(titulo, slug, prepared['html_content'],
                                  cliente_id_int, expira_en)

        flash(f'Documento "{titulo}" creado.')
        return redirect(url_for('admin_dashboard'))

    clientes = Cliente.get_all_with_empresa()
    return render_template('admin/documento_nuevo.html', clientes=clientes)


@app.route('/admin/documentos/<int:id>/toggle', methods=['POST'])
@admin_required
def admin_toggle_documento(id):
    Documento.toggle_activa(id)
    return redirect(request.referrer or url_for('admin_dashboard'))


@app.route('/admin/documentos/<int:id>/delete', methods=['POST'])
@admin_required
def admin_delete_documento(id):
    Documento.delete(id)
    flash('Documento eliminado.')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/documentos/<int:id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_edit_documento(id):
    documento = DocumentoModel.query.get_or_404(id)
    if request.method == 'POST':
        titulo = request.form.get('titulo', '').strip()
        slug = slugify(request.form.get('slug', '').strip())
        activa = request.form.get('activa') == 'on'
        expira_raw = request.form.get('expira_en', '').strip()
        expira_en = _parse_expiration_date(expira_raw)
        upload = request.files.get('archivo')
        replacement = None

        if not titulo or not slug:
            flash('Título y slug son obligatorios.')
            return redirect(url_for('admin_edit_documento', id=id))
        if expira_raw and not expira_en:
            flash('La fecha de expiración debe tener formato YYYY-MM-DD.')
            return redirect(url_for('admin_edit_documento', id=id))
        if not Documento.slug_available(documento.cliente_id, slug, exclude_id=id):
            flash('Ese slug ya existe para este cliente.')
            return redirect(url_for('admin_edit_documento', id=id))
        if upload and upload.filename:
            try:
                replacement = _validate_upload(upload, required=False)
            except ValueError as exc:
                flash(str(exc))
                return redirect(url_for('admin_edit_documento', id=id))

        Documento.update(id, titulo, slug, expira_en, activa, replacement)
        flash('Documento actualizado.')
        return redirect(url_for('admin_dashboard'))

    return render_template('admin/documento_editar.html', documento=documento)


@app.route('/admin/documentos/<int:id>/tracking')
@admin_required
def admin_tracking(id):
    documento = DocumentoModel.query.get_or_404(id)
    stats = TrackingStats.get_for_documento(id)
    return render_template('admin/tracking.html',
                           documento=documento, stats=stats,
                           domain_for=_domain_for_empresa)


@app.route('/admin/documentos/<int:id>/tracking.csv')
@admin_required
def admin_tracking_csv(id):
    documento = DocumentoModel.query.get_or_404(id)
    stats = TrackingStats.get_for_documento(id)
    out = StringIO()
    writer = csv.writer(out)
    writer.writerow([
        'fecha_hora', 'abierto_por', 'tipo_acceso', 'dispositivo', 'pantalla',
        'zona_horaria', 'ip', 'tiempo_activo_s', 'scroll_max', 'eventos',
        'top_secciones', 'top_paginas', 'user_agent',
    ])
    for s in stats['sesiones']:
        writer.writerow([
            s['created_at'].strftime('%Y-%m-%d %H:%M:%S'),
            s['viewer_name'],
            s['access_type'],
            s['device'],
            s['screen'],
            s['tz'],
            s['ip'],
            s['active_s'],
            s['max_scroll'],
            s['eventos_count'],
            s['top_sections_text'],
            s['top_pages_text'],
            s['ua'],
        ])
    filename = f'tracking-{documento.slug}.csv'
    return Response(
        out.getvalue(),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


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


def _authenticate_cliente(cliente, password):
    if _rate_limited(f'cliente_login_{cliente.id}', limit=12, window_seconds=300):
        return 'limited'
    if Cliente.check_password(cliente.id, password):
        _set_cliente_access(
            cliente, 'cliente',
            cliente.contacto or cliente.nombre,
            access_key_id=None,
        )
        return 'ok'
    key = AccessKey.check_for_cliente(password, cliente, ip=_client_ip())
    if key:
        _set_cliente_access(cliente, 'clave interna', key.nombre, access_key_id=key.id)
        return 'ok'
    return 'failed'


@app.route('/<slug>', methods=['GET', 'POST'])
def cliente_landing(slug):
    if slug in RESERVED_SLUGS:
        abort(404)

    empresa, cliente = _resolve_cliente(slug)

    if request.method == 'POST':
        password = request.form.get('password', '')
        auth_result = _authenticate_cliente(cliente, password)
        if auth_result == 'limited':
            flash('Demasiados intentos. Esperá unos minutos y probá de nuevo.')
            return _render_password_page(empresa, cliente), 429
        if auth_result != 'ok':
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
        auth_result = _authenticate_cliente(cliente, password)
        if auth_result == 'limited':
            flash('Demasiados intentos. Esperá unos minutos y probá de nuevo.')
            return _render_password_page(empresa, cliente), 429
        if auth_result != 'ok':
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
                               documento=documento, pdf_url=pdf_url,
                               tracking_token=_tracking_token(documento.id))

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
