import os
import json
import hashlib
import re
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text, LargeBinary

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

    # propuestas → documentos
    if 'propuestas' in tables and 'documentos' not in tables:
        with db.engine.begin() as conn:
            conn.execute(text('ALTER TABLE propuestas RENAME TO documentos'))
        tables.discard('propuestas')
        tables.add('documentos')

    # sesiones.propuesta_id → documento_id
    if 'sesiones' in tables:
        ses_cols = {c['name'] for c in inspector.get_columns('sesiones')}
        if 'propuesta_id' in ses_cols and 'documento_id' not in ses_cols:
            with db.engine.begin() as conn:
                conn.execute(text('ALTER TABLE sesiones RENAME COLUMN propuesta_id TO documento_id'))


def _run_post_create_migrations():
    """Migrations that run AFTER db.create_all() (column additions, data fixes)."""
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    dialect = db.engine.dialect.name

    if 'clientes' in tables:
        cols = {c['name'] for c in inspector.get_columns('clientes')}
        with db.engine.begin() as conn:
            if 'access_password' not in cols:
                conn.execute(text('ALTER TABLE clientes ADD COLUMN access_password VARCHAR(255)'))
            if 'contacto' not in cols:
                conn.execute(text('ALTER TABLE clientes ADD COLUMN contacto VARCHAR(150)'))

    if 'documentos' in tables:
        cols = {c['name'] for c in inspector.get_columns('documentos')}
        with db.engine.begin() as conn:
            if 'tipo' not in cols:
                conn.execute(text("ALTER TABLE documentos ADD COLUMN tipo VARCHAR(10) DEFAULT 'html'"))
                conn.execute(text("UPDATE documentos SET tipo='html' WHERE tipo IS NULL"))
            if 'slug' not in cols:
                conn.execute(text("ALTER TABLE documentos ADD COLUMN slug VARCHAR(100)"))
                conn.execute(text("UPDATE documentos SET slug = 'doc-' || id WHERE slug IS NULL OR slug = ''"))
            if 'file_data' not in cols:
                col_type = 'BYTEA' if dialect == 'postgresql' else 'BLOB'
                conn.execute(text(f'ALTER TABLE documentos ADD COLUMN file_data {col_type}'))
            if 'file_name' not in cols:
                conn.execute(text('ALTER TABLE documentos ADD COLUMN file_name VARCHAR(255)'))
            if dialect == 'postgresql':
                conn.execute(text('ALTER TABLE documentos ALTER COLUMN html_content DROP NOT NULL'))

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


def _hash(pw):
    return hashlib.sha256(pw.encode('utf-8')).hexdigest()


def slugify(value):
    value = (value or '').lower().strip()
    value = re.sub(r'[^a-z0-9\-]+', '-', value)
    value = re.sub(r'-+', '-', value).strip('-')
    return value[:80] or 'doc'


# ─── Models ──────────────────────────────────────────────────────────────────

class EmpresaModel(db.Model):
    __tablename__ = 'empresas'
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    slug = db.Column(db.String(50), unique=True, nullable=False)
    clientes = db.relationship('ClienteModel', backref='empresa', lazy=True,
                                cascade='all, delete-orphan')


class ClienteModel(db.Model):
    __tablename__ = 'clientes'
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    slug = db.Column(db.String(50), nullable=False)
    contacto = db.Column(db.String(150), nullable=True)
    password_hash = db.Column(db.String(64), nullable=False)
    access_password = db.Column(db.String(255), nullable=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresas.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    documentos = db.relationship('DocumentoModel', backref='cliente', lazy=True,
                                  cascade='all, delete-orphan')


class DocumentoModel(db.Model):
    __tablename__ = 'documentos'
    id = db.Column(db.Integer, primary_key=True)
    titulo = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(100), nullable=False)
    tipo = db.Column(db.String(10), nullable=False, default='html')  # 'html' | 'pdf'
    html_content = db.Column(db.Text, nullable=True)
    file_data = db.Column(LargeBinary, nullable=True)
    file_name = db.Column(db.String(255), nullable=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey('clientes.id'), nullable=False)
    activa = db.Column(db.Boolean, default=True)
    expira_en = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    sesiones = db.relationship('SesionModel', backref='documento', lazy=True,
                                cascade='all, delete-orphan')


class SesionModel(db.Model):
    __tablename__ = 'sesiones'
    id = db.Column(db.Integer, primary_key=True)
    documento_id = db.Column(db.Integer, db.ForeignKey('documentos.id'), nullable=False)
    session_token = db.Column(db.String(32), unique=True, nullable=False)
    ip = db.Column(db.String(45))
    user_agent = db.Column(db.Text)
    screen = db.Column(db.String(20))
    tz = db.Column(db.String(60))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
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
        )
        db.session.add(s)
        db.session.commit()
        return s


class TrackingEventModel(db.Model):
    __tablename__ = 'tracking_events'
    id = db.Column(db.Integer, primary_key=True)
    session_token = db.Column(db.String(32), db.ForeignKey('sesiones.session_token'), nullable=False)
    event_type = db.Column(db.String(50), nullable=False)
    data = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @classmethod
    def create(cls, session_token, event_type, data):
        e = cls(session_token=session_token, event_type=event_type, data=json.dumps(data))
        db.session.add(e)
        db.session.commit()
        return e


# ─── Service helpers ─────────────────────────────────────────────────────────

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
    def get_by_slug_and_empresa(slug, empresa_id):
        return ClienteModel.query.filter_by(slug=slug, empresa_id=empresa_id).first()

    @staticmethod
    def create(nombre, slug, password, empresa_id, contacto=None):
        c = ClienteModel(
            nombre=nombre,
            slug=slug,
            contacto=contacto or None,
            password_hash=_hash(password),
            access_password=password,
            empresa_id=empresa_id,
        )
        db.session.add(c)
        db.session.commit()
        return c

    @staticmethod
    def check_password(cliente_id, password):
        c = ClienteModel.query.get(cliente_id)
        return c is not None and c.password_hash == _hash(password)

    @staticmethod
    def update_password(cliente_id, password):
        c = ClienteModel.query.get(cliente_id)
        if c:
            c.password_hash = _hash(password)
            c.access_password = password
            db.session.commit()
        return c

    @staticmethod
    def delete(cliente_id):
        c = ClienteModel.query.get(cliente_id)
        if c:
            db.session.delete(c)
            db.session.commit()


class Documento:
    @staticmethod
    def get_all_with_stats():
        documentos = (DocumentoModel.query
                      .join(ClienteModel)
                      .join(EmpresaModel)
                      .order_by(DocumentoModel.created_at.desc())
                      .all())
        result = []
        for d in documentos:
            views = SesionModel.query.filter_by(documento_id=d.id).count()
            last = (SesionModel.query.filter_by(documento_id=d.id)
                    .order_by(SesionModel.created_at.desc()).first())
            result.append({
                'obj': d,
                'views': views,
                'last_view': last.created_at if last else None,
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
            cliente_id=cliente_id, expira_en=expira_en,
        )
        db.session.add(d)
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
    def get_for_documento(documento_id):
        sesiones = (SesionModel.query
                    .filter_by(documento_id=documento_id)
                    .order_by(SesionModel.created_at.desc())
                    .all())

        total_active_s = 0
        dispositivos = {}
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
            last_heartbeat = None

            for e in eventos:
                if e.event_type in ('page_exit', 'heartbeat'):
                    try:
                        d = json.loads(e.data or '{}')
                        if d.get('active_s', 0) > active_s:
                            active_s = d.get('active_s', 0)
                        if d.get('scroll', 0) > max_scroll:
                            max_scroll = d.get('scroll', 0)
                        if e.event_type == 'heartbeat' and last_heartbeat is None:
                            last_heartbeat = e.created_at
                    except Exception:
                        pass

            total_active_s += active_s

            detail.append({
                'token': s.session_token[:8],
                'created_at': s.created_at,
                'ip': s.ip,
                'device': dev,
                'screen': s.screen or '–',
                'tz': s.tz or '–',
                'active_s': active_s,
                'max_scroll': max_scroll,
                'eventos_count': len(eventos),
                'ua': ua[:80] if ua else '–',
            })

        avg_active_s = round(total_active_s / len(sesiones)) if sesiones else 0

        return {
            'total_sesiones': len(sesiones),
            'dispositivos': dispositivos,
            'avg_active_s': avg_active_s,
            'ultimo_acceso': sesiones[0].created_at if sesiones else None,
            'sesiones': detail,
        }
