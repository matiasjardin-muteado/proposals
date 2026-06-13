import os
import json
import hashlib
import re
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text, LargeBinary
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


def init_db(app):
    url = os.environ.get('DATABASE_URL', 'sqlite:///proposals.db')
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql://', 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024
    db.init_app(app)
    with app.app_context():
        if url.startswith('postgresql://'):
            _init_postgres_schema()
        else:
            _create_schema()


def _create_schema():
    _run_migrations()
    db.create_all()
    _run_post_create_migrations()
    _seed_empresas()


def _init_postgres_schema():
    lock_id = 849201735
    with db.engine.connect() as conn:
        conn.execute(text('SELECT pg_advisory_lock(:lock_id)'), {'lock_id': lock_id})
        try:
            _create_schema()
        finally:
            conn.execute(text('SELECT pg_advisory_unlock(:lock_id)'), {'lock_id': lock_id})


def _run_migrations():
    """Migrations that must run BEFORE db.create_all() (renames)."""
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())

    if 'propuestas' in tables and 'documentos' not in tables:
        with db.engine.begin() as conn:
            conn.execute(text('ALTER TABLE propuestas RENAME TO documentos'))
        tables.discard('propuestas')
        tables.add('documentos')

    if 'sesiones' in tables:
        ses_cols = {c['name'] for c in inspector.get_columns('sesiones')}
        if 'propuesta_id' in ses_cols and 'documento_id' not in ses_cols:
            with db.engine.begin() as conn:
                conn.execute(text('ALTER TABLE sesiones RENAME COLUMN propuesta_id TO documento_id'))


def _column_names(inspector, table):
    return {c['name'] for c in inspector.get_columns(table)}


def _add_column(conn, table, column_sql):
    conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {column_sql}'))


def _has_clean_unique(conn, table, cols):
    cols_sql = ', '.join(cols)
    rows = conn.execute(text(
        f'SELECT {cols_sql}, COUNT(*) AS c FROM {table} '
        f'GROUP BY {cols_sql} HAVING COUNT(*) > 1 LIMIT 1'
    )).fetchall()
    return not rows


def _create_unique_index_if_clean(conn, index_name, table, cols):
    if not _has_clean_unique(conn, table, cols):
        return
    cols_sql = ', '.join(cols)
    conn.execute(text(f'CREATE UNIQUE INDEX IF NOT EXISTS {index_name} ON {table} ({cols_sql})'))


def _run_post_create_migrations():
    """Migrations that run AFTER db.create_all() (column additions, data fixes)."""
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    dialect = db.engine.dialect.name

    if 'clientes' in tables:
        cols = _column_names(inspector, 'clientes')
        with db.engine.begin() as conn:
            if 'access_password' not in cols:
                _add_column(conn, 'clientes', 'access_password VARCHAR(255)')
            if 'contacto' not in cols:
                _add_column(conn, 'clientes', 'contacto VARCHAR(150)')
            if dialect == 'postgresql':
                conn.execute(text('ALTER TABLE clientes ALTER COLUMN password_hash TYPE VARCHAR(255)'))

    if 'documentos' in tables:
        cols = _column_names(inspector, 'documentos')
        with db.engine.begin() as conn:
            if 'tipo' not in cols:
                _add_column(conn, 'documentos', "tipo VARCHAR(10) DEFAULT 'html'")
                conn.execute(text("UPDATE documentos SET tipo='html' WHERE tipo IS NULL"))
            if 'slug' not in cols:
                _add_column(conn, 'documentos', 'slug VARCHAR(100)')
                conn.execute(text("UPDATE documentos SET slug = 'doc-' || id WHERE slug IS NULL OR slug = ''"))
            if 'file_data' not in cols:
                col_type = 'BYTEA' if dialect == 'postgresql' else 'BLOB'
                _add_column(conn, 'documentos', f'file_data {col_type}')
            if 'file_name' not in cols:
                _add_column(conn, 'documentos', 'file_name VARCHAR(255)')
            if dialect == 'postgresql':
                conn.execute(text('ALTER TABLE documentos ALTER COLUMN html_content DROP NOT NULL'))

    if 'sesiones' in tables:
        cols = _column_names(inspector, 'sesiones')
        with db.engine.begin() as conn:
            if 'access_type' not in cols:
                _add_column(conn, 'sesiones', "access_type VARCHAR(30) DEFAULT 'cliente'")
            if 'viewer_name' not in cols:
                _add_column(conn, 'sesiones', 'viewer_name VARCHAR(150)')
            if 'access_key_id' not in cols:
                _add_column(conn, 'sesiones', 'access_key_id INTEGER')

    if 'access_keys' in tables:
        cols = _column_names(inspector, 'access_keys')
        with db.engine.begin() as conn:
            if 'last_used_ip' not in cols:
                _add_column(conn, 'access_keys', 'last_used_ip VARCHAR(45)')

    if 'clientes' in tables and 'documentos' in tables:
        with db.engine.begin() as conn:
            _create_unique_index_if_clean(
                conn, 'ux_clientes_empresa_slug', 'clientes', ('empresa_id', 'slug')
            )
            _create_unique_index_if_clean(
                conn, 'ux_documentos_cliente_slug', 'documentos', ('cliente_id', 'slug')
            )

    if 'empresas' in tables:
        _migrate_operantio_to_pragmato()


def _migrate_operantio_to_pragmato():
    with db.engine.begin() as conn:
        operantio = conn.execute(text("SELECT id FROM empresas WHERE slug='operantio'")).fetchone()
        if not operantio:
            return
        pragmato = conn.execute(text("SELECT id FROM empresas WHERE slug='pragmato'")).fetchone()
        if pragmato:
            conn.execute(
                text("UPDATE clientes SET empresa_id=:new WHERE empresa_id=:old"),
                {'new': pragmato[0], 'old': operantio[0]},
            )
            conn.execute(text("DELETE FROM empresas WHERE slug='operantio'"))
        else:
            conn.execute(text("UPDATE empresas SET nombre='Pragmato', slug='pragmato' WHERE slug='operantio'"))


def _seed_empresas():
    defaults = [
        ('Muteado', 'muteado'),
        ('Pragmato', 'pragmato'),
        ('Cartago', 'cartago'),
    ]
    for nombre, slug in defaults:
        if not EmpresaModel.query.filter_by(slug=slug).first():
            db.session.add(EmpresaModel(nombre=nombre, slug=slug))
    db.session.commit()


def _legacy_hash(pw):
    return hashlib.sha256(pw.encode('utf-8')).hexdigest()


def _hash_password(password):
    return generate_password_hash(password)


def _verify_password(stored_hash, password):
    if not stored_hash:
        return False, False
    if len(stored_hash) == 64 and re.fullmatch(r'[a-f0-9]{64}', stored_hash):
        return stored_hash == _legacy_hash(password), True
    return check_password_hash(stored_hash, password), False


def slugify(value):
    value = (value or '').lower().strip()
    value = re.sub(r'[^a-z0-9\-]+', '-', value)
    value = re.sub(r'-+', '-', value).strip('-')
    return value[:80] or 'doc'


class EmpresaModel(db.Model):
    __tablename__ = 'empresas'
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    slug = db.Column(db.String(50), unique=True, nullable=False)
    clientes = db.relationship('ClienteModel', backref='empresa', lazy=True,
                                cascade='all, delete-orphan')


class ClienteModel(db.Model):
    __tablename__ = 'clientes'
    __table_args__ = (
        db.UniqueConstraint('empresa_id', 'slug', name='uq_clientes_empresa_slug'),
    )
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    slug = db.Column(db.String(50), nullable=False)
    contacto = db.Column(db.String(150), nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    access_password = db.Column(db.String(255), nullable=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresas.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    documentos = db.relationship('DocumentoModel', backref='cliente', lazy=True,
                                  cascade='all, delete-orphan')


class DocumentoModel(db.Model):
    __tablename__ = 'documentos'
    __table_args__ = (
        db.UniqueConstraint('cliente_id', 'slug', name='uq_documentos_cliente_slug'),
    )
    id = db.Column(db.Integer, primary_key=True)
    titulo = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(100), nullable=False)
    tipo = db.Column(db.String(10), nullable=False, default='html')
    html_content = db.Column(db.Text, nullable=True)
    file_data = db.Column(LargeBinary, nullable=True)
    file_name = db.Column(db.String(255), nullable=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey('clientes.id'), nullable=False)
    activa = db.Column(db.Boolean, default=True)
    expira_en = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    sesiones = db.relationship('SesionModel', backref='documento', lazy=True,
                                cascade='all, delete-orphan')


class AccessKeyModel(db.Model):
    __tablename__ = 'access_keys'
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(150), nullable=False)
    role = db.Column(db.String(80), nullable=False, default='Clave interna')
    scope_type = db.Column(db.String(20), nullable=False, default='global')
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresas.id'), nullable=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey('clientes.id'), nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_used_at = db.Column(db.DateTime, nullable=True)
    last_used_ip = db.Column(db.String(45), nullable=True)
    empresa = db.relationship('EmpresaModel', lazy=True)
    cliente = db.relationship('ClienteModel', lazy=True)


class SesionModel(db.Model):
    __tablename__ = 'sesiones'
    id = db.Column(db.Integer, primary_key=True)
    documento_id = db.Column(db.Integer, db.ForeignKey('documentos.id'), nullable=False)
    session_token = db.Column(db.String(64), unique=True, nullable=False)
    ip = db.Column(db.String(45))
    user_agent = db.Column(db.Text)
    screen = db.Column(db.String(20))
    tz = db.Column(db.String(60))
    access_type = db.Column(db.String(30), default='cliente')
    viewer_name = db.Column(db.String(150), nullable=True)
    access_key_id = db.Column(db.Integer, db.ForeignKey('access_keys.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    access_key = db.relationship('AccessKeyModel', lazy=True)
    eventos = db.relationship('TrackingEventModel', backref='sesion', lazy=True,
                               foreign_keys='TrackingEventModel.session_token',
                               primaryjoin='SesionModel.session_token == TrackingEventModel.session_token',
                               cascade='all, delete-orphan')

    @classmethod
    def create(cls, documento_id, token, ip, user_agent, extra=None):
        extra = extra or {}
        s = cls(
            documento_id=documento_id,
            session_token=token,
            ip=ip,
            user_agent=user_agent,
            screen=extra.get('screen'),
            tz=extra.get('tz'),
            access_type=extra.get('access_type') or 'cliente',
            viewer_name=extra.get('viewer_name'),
            access_key_id=extra.get('access_key_id'),
        )
        db.session.add(s)
        db.session.commit()
        return s


class TrackingEventModel(db.Model):
    __tablename__ = 'tracking_events'
    id = db.Column(db.Integer, primary_key=True)
    session_token = db.Column(db.String(64), db.ForeignKey('sesiones.session_token'), nullable=False)
    event_type = db.Column(db.String(50), nullable=False)
    data = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @classmethod
    def create(cls, session_token, event_type, data):
        e = cls(session_token=session_token, event_type=event_type, data=json.dumps(data))
        db.session.add(e)
        db.session.commit()
        return e


class Empresa:
    @staticmethod
    def get_all():
        return EmpresaModel.query.order_by(EmpresaModel.nombre).all()

    @staticmethod
    def get_by_slug(slug):
        return EmpresaModel.query.filter_by(slug=slug).first()


class Cliente:
    @staticmethod
    def get_all():
        return ClienteModel.query.order_by(ClienteModel.nombre).all()

    @staticmethod
    def get_all_with_empresa():
        return (ClienteModel.query
                .join(EmpresaModel)
                .order_by(EmpresaModel.nombre, ClienteModel.nombre)
                .all())

    @staticmethod
    def get_by_id(cliente_id):
        return ClienteModel.query.get(cliente_id)

    @staticmethod
    def get_by_slug_and_empresa(slug, empresa_id):
        return ClienteModel.query.filter_by(slug=slug, empresa_id=empresa_id).first()

    @staticmethod
    def slug_available(slug, empresa_id, exclude_id=None):
        q = ClienteModel.query.filter_by(slug=slug, empresa_id=empresa_id)
        if exclude_id:
            q = q.filter(ClienteModel.id != exclude_id)
        return q.first() is None

    @staticmethod
    def create(nombre, slug, password, empresa_id, contacto=None):
        c = ClienteModel(
            nombre=nombre,
            slug=slug,
            contacto=contacto or None,
            password_hash=_hash_password(password),
            access_password=None,
            empresa_id=empresa_id,
        )
        db.session.add(c)
        db.session.commit()
        return c

    @staticmethod
    def check_password(cliente_id, password):
        c = ClienteModel.query.get(cliente_id)
        if not c:
            return False
        ok, needs_rehash = _verify_password(c.password_hash, password)
        if ok and needs_rehash:
            c.password_hash = _hash_password(password)
            c.access_password = None
            db.session.commit()
        return ok

    @staticmethod
    def update_password(cliente_id, password):
        c = ClienteModel.query.get(cliente_id)
        if c:
            c.password_hash = _hash_password(password)
            c.access_password = None
            db.session.commit()
        return c

    @staticmethod
    def delete(cliente_id):
        c = ClienteModel.query.get(cliente_id)
        if c:
            db.session.delete(c)
            db.session.commit()


class AccessKey:
    @staticmethod
    def get_all():
        return AccessKeyModel.query.order_by(AccessKeyModel.active.desc(), AccessKeyModel.nombre).all()

    @staticmethod
    def get_by_id(key_id):
        return AccessKeyModel.query.get(key_id)

    @staticmethod
    def create(nombre, role, scope_type, password, empresa_id=None, cliente_id=None):
        key = AccessKeyModel(
            nombre=nombre,
            role=role or 'Clave interna',
            scope_type=scope_type,
            empresa_id=empresa_id,
            cliente_id=cliente_id,
            password_hash=_hash_password(password),
            active=True,
        )
        db.session.add(key)
        db.session.commit()
        return key

    @staticmethod
    def update_password(key_id, password):
        key = AccessKeyModel.query.get(key_id)
        if key:
            key.password_hash = _hash_password(password)
            db.session.commit()
        return key

    @staticmethod
    def toggle_active(key_id):
        key = AccessKeyModel.query.get(key_id)
        if key:
            key.active = not key.active
            db.session.commit()
        return key

    @staticmethod
    def delete(key_id):
        key = AccessKeyModel.query.get(key_id)
        if key:
            db.session.delete(key)
            db.session.commit()

    @staticmethod
    def _allowed_for_cliente(key, cliente):
        if not key.active:
            return False
        if key.scope_type == 'global':
            return True
        if key.scope_type == 'empresa':
            return key.empresa_id == cliente.empresa_id
        if key.scope_type == 'cliente':
            return key.cliente_id == cliente.id
        return False

    @staticmethod
    def check_for_cliente(password, cliente, ip=None):
        if not password or not cliente:
            return None
        keys = AccessKeyModel.query.filter_by(active=True).all()
        for key in keys:
            ok, needs_rehash = _verify_password(key.password_hash, password)
            if ok and AccessKey._allowed_for_cliente(key, cliente):
                key.last_used_at = datetime.utcnow()
                key.last_used_ip = ip
                if needs_rehash:
                    key.password_hash = _hash_password(password)
                db.session.commit()
                return key
        return None


class Documento:
    @staticmethod
    def get_all_with_stats(filters=None):
        filters = filters or {}
        q = (DocumentoModel.query
             .join(ClienteModel)
             .join(EmpresaModel))

        if filters.get('empresa'):
            q = q.filter(EmpresaModel.slug == filters['empresa'])
        if filters.get('cliente_id'):
            q = q.filter(DocumentoModel.cliente_id == int(filters['cliente_id']))
        if filters.get('tipo'):
            q = q.filter(DocumentoModel.tipo == filters['tipo'])
        if filters.get('estado') == 'activo':
            q = q.filter(DocumentoModel.activa.is_(True))
        elif filters.get('estado') == 'inactivo':
            q = q.filter(DocumentoModel.activa.is_(False))

        documentos = q.order_by(DocumentoModel.created_at.desc()).all()
        result = []
        opened_by_filter = (filters.get('opened_by') or '').strip().lower()

        for d in documentos:
            sessions = SesionModel.query.filter_by(documento_id=d.id)
            views = sessions.count()
            last = sessions.order_by(SesionModel.created_at.desc()).first()
            viewers = [
                r[0] for r in (db.session.query(SesionModel.viewer_name)
                               .filter_by(documento_id=d.id)
                               .filter(SesionModel.viewer_name.isnot(None))
                               .distinct()
                               .order_by(SesionModel.viewer_name)
                               .all())
            ]
            if opened_by_filter and not any(opened_by_filter in v.lower() for v in viewers):
                continue
            result.append({
                'obj': d,
                'views': views,
                'last_view': last.created_at if last else None,
                'opened_by': viewers,
            })
        return result

    @staticmethod
    def get_active_for_cliente(cliente_id):
        return (DocumentoModel.query
                .filter_by(cliente_id=cliente_id, activa=True)
                .order_by(DocumentoModel.created_at.desc())
                .all())

    @staticmethod
    def get_by_cliente_and_slug(cliente_id, slug):
        return (DocumentoModel.query
                .filter_by(cliente_id=cliente_id, slug=slug)
                .first())

    @staticmethod
    def get_by_id(doc_id):
        return DocumentoModel.query.get(doc_id)

    @staticmethod
    def slug_available(cliente_id, slug, exclude_id=None):
        q = DocumentoModel.query.filter_by(cliente_id=cliente_id, slug=slug)
        if exclude_id:
            q = q.filter(DocumentoModel.id != exclude_id)
        return q.first() is None

    @staticmethod
    def unique_slug(cliente_id, base_slug):
        slug = slugify(base_slug)
        candidate = slug
        i = 2
        while not Documento.slug_available(cliente_id, candidate):
            candidate = f'{slug}-{i}'
            i += 1
        return candidate

    @staticmethod
    def create_html(titulo, slug, html_content, cliente_id, expira_en=None):
        d = DocumentoModel(
            titulo=titulo, slug=slug, tipo='html',
            html_content=html_content,
            file_data=None, file_name=None,
            cliente_id=cliente_id, expira_en=expira_en,
        )
        db.session.add(d)
        db.session.commit()
        return d

    @staticmethod
    def create_pdf(titulo, slug, file_data, file_name, cliente_id, expira_en=None):
        d = DocumentoModel(
            titulo=titulo, slug=slug, tipo='pdf',
            file_data=file_data, file_name=file_name,
            html_content=None,
            cliente_id=cliente_id, expira_en=expira_en,
        )
        db.session.add(d)
        db.session.commit()
        return d

    @staticmethod
    def update(doc_id, titulo, slug, expira_en, activa, replacement=None):
        d = DocumentoModel.query.get(doc_id)
        if not d:
            return None
        d.titulo = titulo
        d.slug = slug
        d.expira_en = expira_en
        d.activa = activa
        if replacement:
            d.tipo = replacement['tipo']
            d.file_name = replacement.get('file_name')
            d.file_data = replacement.get('file_data')
            d.html_content = replacement.get('html_content')
        db.session.commit()
        return d

    @staticmethod
    def toggle_activa(doc_id):
        d = DocumentoModel.query.get(doc_id)
        if d:
            d.activa = not d.activa
            db.session.commit()

    @staticmethod
    def delete(doc_id):
        d = DocumentoModel.query.get(doc_id)
        if d:
            db.session.delete(d)
            db.session.commit()


class TrackingStats:
    @staticmethod
    def _event_data(event):
        try:
            return json.loads(event.data or '{}')
        except Exception:
            return {}

    @staticmethod
    def _merge_max(target, values):
        if not isinstance(values, dict):
            return
        for label, seconds in values.items():
            label = str(label or '').strip()
            if not label:
                continue
            try:
                seconds = int(seconds)
            except (TypeError, ValueError):
                continue
            target[label] = max(target.get(label, 0), seconds)

    @staticmethod
    def _add_totals(target, values):
        for label, seconds in values.items():
            target[label] = target.get(label, 0) + seconds

    @staticmethod
    def _int_value(value, default=0):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _top_times(values, limit=8):
        return [
            {'label': label, 'seconds': seconds}
            for label, seconds in sorted(values.items(), key=lambda item: item[1], reverse=True)[:limit]
            if seconds > 0
        ]

    @staticmethod
    def _format_top(values):
        return '; '.join(f"{item['label']} ({item['seconds']}s)"
                         for item in TrackingStats._top_times(values, limit=5))

    @staticmethod
    def get_for_documento(documento_id):
        sesiones = (SesionModel.query
                    .filter_by(documento_id=documento_id)
                    .order_by(SesionModel.created_at.desc())
                    .all())

        total_active_s = 0
        dispositivos = {}
        section_totals = {}
        page_totals = {}
        detail = []

        for s in sesiones:
            ua = s.user_agent or ''
            if 'Mobile' in ua or 'Android' in ua or 'iPhone' in ua:
                dev = 'Móvil'
            elif 'iPad' in ua or 'Tablet' in ua:
                dev = 'Tablet'
            else:
                dev = 'Desktop'
            dispositivos[dev] = dispositivos.get(dev, 0) + 1

            eventos = (TrackingEventModel.query
                       .filter_by(session_token=s.session_token)
                       .order_by(TrackingEventModel.created_at.desc())
                       .all())

            active_s = 0
            max_scroll = 0
            section_times = {}
            page_times = {}

            for e in eventos:
                d = TrackingStats._event_data(e)
                if e.event_type in ('page_exit', 'heartbeat', 'tab_hidden'):
                    event_active_s = TrackingStats._int_value(d.get('active_s'))
                    event_scroll = TrackingStats._int_value(d.get('scroll'))
                    if event_active_s > active_s:
                        active_s = event_active_s
                    if event_scroll > max_scroll:
                        max_scroll = event_scroll
                    TrackingStats._merge_max(section_times, d.get('section_times'))
                    TrackingStats._merge_max(page_times, d.get('page_times'))

            total_active_s += active_s
            if not section_times and page_times:
                section_times = dict(page_times)
            TrackingStats._add_totals(section_totals, section_times)
            TrackingStats._add_totals(page_totals, page_times)

            detail.append({
                'token': s.session_token[:8],
                'created_at': s.created_at,
                'ip': s.ip,
                'device': dev,
                'screen': s.screen or '-',
                'tz': s.tz or '-',
                'access_type': s.access_type or 'cliente',
                'viewer_name': s.viewer_name or '-',
                'active_s': active_s,
                'max_scroll': max_scroll,
                'eventos_count': len(eventos),
                'ua': ua[:120] if ua else '-',
                'top_sections': TrackingStats._top_times(section_times, limit=3),
                'top_pages': TrackingStats._top_times(page_times, limit=3),
                'top_sections_text': TrackingStats._format_top(section_times),
                'top_pages_text': TrackingStats._format_top(page_times),
            })

        avg_active_s = round(total_active_s / len(sesiones)) if sesiones else 0

        return {
            'total_sesiones': len(sesiones),
            'dispositivos': dispositivos,
            'avg_active_s': avg_active_s,
            'ultimo_acceso': sesiones[0].created_at if sesiones else None,
            'top_sections': TrackingStats._top_times(section_totals),
            'top_pages': TrackingStats._top_times(page_totals),
            'sesiones': detail,
        }
