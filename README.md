# Ski Valet — Flask + PostgreSQL (Railway)

Gestión de guardaesquís: tarjetas de habitación → habitación → ubicación → equipo.
Los datos viven en PostgreSQL; el navegador ya no guarda nada (ni datos, ni contraseñas, ni PIN).

## Estructura

```
app.py             Backend Flask: API, sesiones, esquema de base de datos
static/index.html  App del valet
static/admin.html  Consola de administración
static/sync.js     Sincronización con el servidor (compartida por las dos páginas)
requirements.txt   Procfile   railway.json
```

## Despliegue en Railway

1. Sube el repositorio a GitHub y crea un proyecto en Railway desde ese repo.
2. Añade un servicio **PostgreSQL** al proyecto.
3. En el servicio web, crea estas variables:
   - `DATABASE_URL` = `${{Postgres.DATABASE_URL}}` (referencia al servicio de Postgres)
   - `ADMIN_PASSWORD` = contraseña del admin (**defínela antes del primer arranque**)
   - Opcionales: `ADMIN_USERNAME` (por defecto `Admin`), `STAFF_PIN` (4 dígitos), `SECRET_KEY`, `SESSION_HOURS` (por defecto 12)
4. Genera un dominio público (Settings → Networking). Web NFC necesita HTTPS, que Railway ya da.

Las tablas se crean solas al arrancar. `ADMIN_PASSWORD`, `ADMIN_USERNAME` y `STAFF_PIN` solo se usan
la primera vez; después se cambian desde la app (Access). Si no defines `ADMIN_PASSWORD`, se usa el
login de admin que ya venía por defecto en la versión anterior.
Si no defines `SECRET_KEY`, se genera una y se guarda en la base de datos.

## Primer uso (base de datos vacía)

1. Abre `/` → **Admin sign in**.
2. Cuenta → **Access & staff**: pon el PIN del personal y añade los nombres.
3. El mapa de ubicaciones ya viene creado (vacío): Main Hall S-101–112, Ski Wall S-120–131,
   Boot Room S-201–208. Si borras alguna, **Hall setup** (app) o **Storage map** (/admin) muestran
   el botón *Restore default layout* para volver a crear las que falten. El mapa se define en
   `HALL_LAYOUT`, al principio de `app.py`.
4. Los valets entran con el PIN y tocan su nombre.

La consola de administración está en `/admin` (misma sesión que la app).

## En local

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://usuario:clave@localhost/skivalet
export COOKIE_SECURE=0          # solo en local (http)
export ADMIN_PASSWORD=unaclave123
python app.py                   # http://localhost:5000
```

## Cómo funciona la sincronización

- Cada registro tiene una revisión (`rev`). `save()` envía solo los registros que cambiaron.
- Cada dispositivo pide los cambios de los demás cada 5 segundos.
- Si dos dispositivos cambian el mismo registro a la vez, el segundo recibe un aviso,
  recarga los datos y repite la acción. Nada se sobrescribe en silencio.
- Sin conexión, sale un aviso rojo y los cambios se reintentan solos.
- Restaurar una copia, importar un backup o borrar el historial hace que todos los dispositivos recarguen.
- El historial es de solo añadir y siempre registra a la persona con sesión iniciada.
- Las copias de seguridad automáticas (últimas 15, antes de cada cambio en la consola) se guardan en el servidor.

## Seguridad

- Contraseña del admin y PIN del personal guardados como hash (Werkzeug), solo en el servidor.
- Cookie de sesión HttpOnly, Secure y SameSite=Lax; caduca tras 12 h sin uso.
- Límite de 10 intentos fallidos de login por IP cada 10 minutos.
- Cambiar la contraseña del admin cierra las demás sesiones de admin.
- Archivar a un miembro del personal cierra su sesión.
