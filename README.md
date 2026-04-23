# Proposals — Sistema de propuestas con tracking

Panel de administración para servir propuestas HTML con autenticación por cliente y tracking detallado de navegación.

## Estructura del repo

```
app.py             # Flask routes
database.py        # SQLAlchemy models + helpers
requirements.txt
Procfile
nixpacks.toml
templates/
  admin/
    base.html
    login.html
    dashboard.html
    clientes.html
    propuesta_nueva.html
    tracking.html
  client/
    password.html
    expirada.html
```

## Setup en Railway

### 1. Crear proyecto nuevo
- New Project → Deploy from GitHub repo
- Agregar PostgreSQL como servicio separado

### 2. Variables de entorno (web service)
```
SECRET_KEY=<string-aleatorio-largo>
ADMIN_PASSWORD=<tu-contraseña-admin>
DATABASE_URL=<reference variable al DATABASE_URL del servicio Postgres>
DEFAULT_EMPRESA=muteado
```

Notas:
- El servicio ya incluye `gunicorn` en `Procfile` y `nixpacks.toml`, así que Railway debería detectarlo solo.
- Si Railway no detecta el start command, configurarlo manualmente como `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 60`.
- Mientras uses el dominio temporal de Railway (`*.up.railway.app`), la vista cliente va a usar `DEFAULT_EMPRESA`. Cuando apuntes dominios reales por empresa, la app toma el subdominio automáticamente.

### 3. Custom Domains
En Railway → Settings → Domains, agregar:
- `muteado.matiasjardin.com`
- `operantio.matiasjardin.com`
- `cartago.matiasjardin.com`

Railway te muestra los DNS records que tenés que crear. Cargá exactamente los que te indique para cada dominio.

## Setup en Vercel (DNS)

En el dashboard de matiasjardin.com → Settings → Domains → Add:

| Nombre     | Tipo  | Valor                    |
|------------|-------|--------------------------|
| muteado    | CNAME | [CNAME de Railway]       |
| operantio  | CNAME | [CNAME de Railway]       |
| cartago    | CNAME | [CNAME de Railway]       |

## URLs

| Acceso              | URL                                            |
|---------------------|------------------------------------------------|
| Panel admin         | `muteado.matiasjardin.com/admin`               |
| Vista cliente       | `muteado.matiasjardin.com/[slug-cliente]`      |

## Flujo de uso

1. Ir a `/admin` → crear cliente (nombre + slug + empresa + contraseña)
2. Subir propuesta HTML → asignar al cliente
3. Enviarle al cliente: URL + contraseña
4. El cliente ingresa la contraseña → ve la propuesta
5. En `/admin/propuestas/<id>/tracking` → ver todo el comportamiento

## Tracking capturado

- Timestamp de cada sesión
- Dispositivo, pantalla, zona horaria, IP
- Tiempo activo vs. tiempo total en página
- Scroll depth (milestones 25/50/75/90/100%)
- Cambios de pestaña (focus/blur)
- Clicks (tag, texto, href)
- Heartbeat cada 30 segundos
- Evento de salida de página

## Dev local

```bash
pip install -r requirements.txt
export SECRET_KEY=dev
export ADMIN_PASSWORD=admin123
python app.py
```

Acceder en: `http://localhost:5001/admin`

Para testear la vista cliente en local, agregar al archivo `/etc/hosts`:
```
127.0.0.1 muteado.localhost
```
Y acceder en: `http://muteado.localhost:5001/[slug-cliente]`
