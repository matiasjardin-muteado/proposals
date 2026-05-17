# Documentos — Sistema de envío de documentos con tracking

Panel de administración para servir documentos (HTML y PDF) a clientes, con contraseña por cliente y tracking detallado de navegación. Cada empresa usa su propio dominio.

## Empresas

| Empresa  | Dominio de documentos              |
|----------|------------------------------------|
| Muteado  | `documentos.muteado.com`           |
| Cartago  | `documentos.grupocartago.com`      |
| Pragmato | `documentos.pragmato.com.ar`       |

Backward compat: los viejos `*.matiasjardin.com` siguen funcionando (mapeados en `DOMAIN_MAP`). `operantio.matiasjardin.com` redirige a Pragmato.

## Estructura del repo

```
app.py             # Flask routes
database.py        # SQLAlchemy models + helpers + migraciones
requirements.txt
Procfile
nixpacks.toml
templates/
  admin/
    base.html
    login.html
    dashboard.html
    clientes.html
    documento_nuevo.html
    tracking.html
  client/
    password.html
    expirada.html
    landing.html       # lista de documentos del cliente
    pdf_viewer.html    # wrapper con iframe + tracking para PDFs
```

## Setup en Railway

### 1. Variables de entorno (web service)
```
SECRET_KEY=<string-aleatorio-largo>
ADMIN_PASSWORD=<tu-contraseña-admin>
DATABASE_URL=<reference variable al DATABASE_URL del servicio Postgres>
DEFAULT_EMPRESA=muteado
# Opcional: para overridear el mapeo de dominios
# DOMAIN_MAP={"documentos.otraempresa.com":"otra"}
```

### 2. Custom Domains
En Railway → Settings → Domains, agregar:
- `documentos.muteado.com`
- `documentos.grupocartago.com`
- `documentos.pragmato.com.ar`

Cargar los CNAME que indique Railway en el DNS de cada dominio (Vercel / Cloudflare / Namecheap / etc.).

## URLs

| Acceso              | URL                                                       |
|---------------------|-----------------------------------------------------------|
| Panel admin         | `documentos.muteado.com/admin`                            |
| Landing del cliente | `documentos.[empresa]/[slug-cliente]`                     |
| Documento puntual   | `documentos.[empresa]/[slug-cliente]/[slug-documento]`    |

Si el cliente tiene un único documento activo, la landing redirige directamente a él.

## Flujo de uso

1. `/admin` → crear cliente (nombre + slug + empresa + contraseña).
2. Subir documentos (HTML o PDF) → asignar al cliente. Pueden ser varios.
3. Enviarle al cliente: URL + contraseña. Una sola clave abre todos sus documentos.
4. En `/admin/documentos/<id>/tracking` → ver el detalle de sesiones.

## Soporte de archivos

- **HTML**: se sirve directo con tracking inyectado antes de `</body>`.
- **PDF**: se sirve dentro de un wrapper HTML con iframe (`view=FitH`). El wrapper mide sesión, dispositivo, tiempo activo, focus/blur, clicks y heartbeat. El scroll *dentro* del PDF no se trackea (limitación del visor nativo del navegador).

Tamaño máximo de archivo: **25 MB**.

## Tracking capturado

- Timestamp de cada sesión
- Dispositivo, pantalla, zona horaria, IP
- Tiempo activo vs. tiempo total
- Scroll depth (sólo HTML, milestones 25/50/75/90/100%)
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

- Admin: `http://localhost:5001/admin` (usa la empresa default = `muteado`).
- Para testear el subdominio de una empresa puntual, agregar a `/etc/hosts`:
  ```
  127.0.0.1 muteado.localhost cartago.localhost pragmato.localhost
  ```
  y entrar por `http://cartago.localhost:5001/[slug-cliente]`.
