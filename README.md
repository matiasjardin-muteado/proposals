# Documentos — Sistema de envío de documentos con tracking

Panel de administración para servir documentos (HTML y PDF) a clientes, con contraseña por cliente y tracking detallado de navegación. Cada empresa usa su propio dominio.

El sistema soporta claves internas por persona, tracking por sección en HTML, tracking por página en PDF, notificaciones por email, filtros de dashboard y exportación CSV.

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
    pdf_viewer.html    # visor PDF.js + tracking para PDFs
```

## Setup en Railway

### 1. Variables de entorno (web service)
```
SECRET_KEY=<string-aleatorio-largo>
ADMIN_PASSWORD=<tu-contraseña-admin>
# Opcional y recomendado: usar hash Werkzeug en vez de contraseña plana para admin
# ADMIN_PASSWORD_HASH=<hash-generado-con-werkzeug>
DATABASE_URL=<reference variable al DATABASE_URL del servicio Postgres>
DEFAULT_EMPRESA=muteado

# Notificaciones por email al abrir documentos
SMTP_HOST=smtp.tudominio.com
SMTP_PORT=587
SMTP_USER=usuario
SMTP_PASSWORD=contraseña
SMTP_FROM=documentos@tudominio.com
NOTIFY_EMAILS=ventas@tudominio.com,socia@tudominio.com
NOTIFY_ON_OPEN=true

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
2. `/admin/claves` → crear claves internas por persona si querés identificar socias o accesos propios.
3. Subir documentos (HTML o PDF) → asignar al cliente. Pueden ser varios.
4. Enviarle al cliente: URL + contraseña. Una clave interna también puede abrir documentos según su alcance.
5. En `/admin/documentos/<id>/tracking` → ver sesiones, quién abrió, tiempo por sección/página y exportar CSV.

## Soporte de archivos

- **HTML**: se sirve directo con tracking inyectado antes de `</body>`.
- **PDF**: se sirve dentro de un visor HTML con PDF.js. El visor mide sesión, dispositivo, tiempo activo, focus/blur, clicks, heartbeat y tiempo por página.

Tamaño máximo de archivo: **25 MB**.

## Tracking capturado

- Timestamp de cada sesión
- Dispositivo, pantalla, zona horaria, IP
- Tiempo activo vs. tiempo total
- Scroll depth (sólo HTML, milestones 25/50/75/90/100%)
- Tiempo por sección en HTML (`data-track-section`, `section`, `article`, `h1`, `h2`, `h3`)
- Tiempo por página en PDF
- Cambios de pestaña (focus/blur)
- Clicks (tag, texto, href)
- Heartbeat cada 30 segundos
- Evento de salida de página

Para medir secciones con nombres más claros en HTML, envolver bloques importantes con:

```html
<section data-track-section="Propuesta económica">
  ...
</section>
```

En PDFs, el sistema mide tiempo por página. Para reportar secciones reales del PDF, conviene mantener una tabla interna de equivalencias tipo "páginas 1-2 = Contexto", "páginas 3-5 = Presupuesto".

## Prompt recomendado para HTML

Usar cuando generes una propuesta HTML:

```text
Generá una propuesta comercial en un único archivo HTML autónomo, responsive y listo para subir a un sistema de tracking.

Requisitos técnicos obligatorios:
- Entregar un solo archivo HTML completo, con <!DOCTYPE html>, <html>, <head> y <body>.
- Incluir todo el CSS dentro de una etiqueta <style> en el mismo archivo.
- No depender de archivos locales externos, carpetas, imágenes locales, JS externo ni CSS externo.
- Si usás imágenes, deben ser URLs públicas absolutas o base64, pero preferir diseño HTML/CSS liviano.
- No incluir scripts de analytics ni tracking propios.
- No usar scroll interno en cajas, modales o contenedores con overflow; el documento debe scrollear con la página principal.
- El HTML debe verse bien en celular, tablet y desktop.

Requisitos de tracking:
- Dividir toda la propuesta en secciones principales usando <section>.
- Cada sección principal debe tener un atributo data-track-section con un nombre corto y claro.
- Cada sección debe tener un título visible con h2.
- Usar nombres de sección simples, por ejemplo:
  "Portada", "Contexto", "Diagnóstico", "Objetivos", "Solución propuesta", "Alcance", "Cronograma", "Propuesta económica", "Próximos pasos".
- No poner contenido importante como una sola imagen grande; el texto debe ser HTML real.
```

## Seguridad

- Formularios protegidos con CSRF.
- Contraseñas de clientes y claves internas guardadas con hash Werkzeug.
- Hashes SHA-256 viejos se aceptan una vez y se migran automáticamente al siguiente login correcto.
- Rate limit básico para login admin y acceso cliente.
- Índices únicos para evitar slugs repetidos por empresa/cliente.
- Uploads validados por tipo, UTF-8 en HTML y estructura básica de PDF.

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
