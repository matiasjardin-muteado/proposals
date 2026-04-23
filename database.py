import os
import json
import hashlib
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text

db = SQLAlchemy()


def init_db(app):
    url = os.environ.get('DATABASE_URL', 'sqlite:///proposals.db')
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql://', 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
    db.init_app(app)
    with app.app_context():
        if url.startswith('postgresql://'):
            _init_postgres_schema()
        else:
            _create_schema()


def _create_schema():
    db.create_all()
    _ensure_schema_updates()
    _seed_empresas()


def _init_postgres_schema():
    lock_id = 849201735
    with db.engine.connect() as conn:
        conn.execute(text('SELECT pg_advisory_lock(:lock_id)'), {'lock_id': lock_id})
        try:
            _create_schema()
        finally:
            conn.execute(text('SELECT pg_advisory_unlock(:lock_id)'), {'lock_id': lock_id})


def _ensure_schema_updates():
    inspector = inspect(db.engine)
    if 'clientes' not in inspector.get_table_names():
        return

    columns = {col['name'] for col in inspector.get_columns('clientes')}
    if 'access_password' not in columns:
        with db.engine.begin() as conn:
            conn.execute(text('ALTER TABLE clientes ADD COLUMN access_password VARCHAR(255)'))


def _seed_empresas():
    defaults = [
        ('Muteado', 'muteado'),
        ('Operantio', 'operantio'),
        ('Cartago', 'cartago'),
    ]
    for nombre, slug in defaults:
        if not EmpresaModel.query.filter_by(slug=slug).first():
            db.session.add(EmpresaModel(nombre=nombre, slug=slug))
    db.session.commit()


def _hash(pw):
    return hashlib.sha256(pw.encode('utf-8')).hexdigest()


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
    password_hash = db.Column(db.String(64), nullable=False)
    access_password = db.Column(db.String(255), nullable=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresas.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    propuestas = db.relationship('PropuestaModel', backref='cliente', lazy=True,
                                  cascade='all, delete-orphan')


class PropuestaModel(db.Model):
    __tablename__ = 'propuestas'
    id = db.Column(db.Integer, primary_key=True)
    titulo = db.Column(db.String(200), nullable=False)
    html_content = db.Column(db.Text, nullable=False)
    cliente_id = db.Column(db.Integer, db.ForeignKey('clientes.id'), nullable=False)
    activa = db.Column(db.Boolean, default=True)
    expira_en = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    sesiones = db.relationship('SesionModel', backref='propuesta', lazy=True,
                                cascade='all, delete-orphan')


class SesionModel(db.Model):
    __tablename__ = 'sesiones'
    id = db.Column(db.Integer, primary_key=True)
    propuesta_id = db.Column(db.Integer, db.ForeignKey('propuestas.id'), nullable=False)
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
    def create(cls, propuesta_id, token, ip, user_agent, extra=None):
        extra = extra or {}
        s = cls(
            propuesta_id=propuesta_id,
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
        return EmpresaModel.query.all()

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
    def create(nombre, slug, password, empresa_id):
        c = ClienteModel(
            nombre=nombre,
            slug=slug,
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


class Propuesta:
    @staticmethod
    def get_all_with_stats():
        propuestas = (PropuestaModel.query
                      .join(ClienteModel)
                      .join(EmpresaModel)
                      .order_by(PropuestaModel.created_at.desc())
                      .all())
        result = []
        for p in propuestas:
            views = SesionModel.query.filter_by(propuesta_id=p.id).count()
            last = (SesionModel.query.filter_by(propuesta_id=p.id)
                    .order_by(SesionModel.created_at.desc()).first())
            result.append({
                'obj': p,
                'views': views,
                'last_view': last.created_at if last else None,
            })
        return result

    @staticmethod
    def get_active_by_cliente(cliente_id):
        return (PropuestaModel.query
                .filter_by(cliente_id=cliente_id, activa=True)
                .order_by(PropuestaModel.created_at.desc())
                .first())

    @staticmethod
    def create(titulo, html_content, cliente_id, expira_en=None):
        p = PropuestaModel(titulo=titulo, html_content=html_content,
                           cliente_id=cliente_id, expira_en=expira_en)
        db.session.add(p)
        db.session.commit()
        return p

    @staticmethod
    def toggle_activa(propuesta_id):
        p = PropuestaModel.query.get(propuesta_id)
        if p:
            p.activa = not p.activa
            db.session.commit()

    @staticmethod
    def delete(propuesta_id):
        p = PropuestaModel.query.get(propuesta_id)
        if p:
            db.session.delete(p)
            db.session.commit()


class TrackingStats:
    @staticmethod
    def get_for_propuesta(propuesta_id):
        sesiones = (SesionModel.query
                    .filter_by(propuesta_id=propuesta_id)
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
