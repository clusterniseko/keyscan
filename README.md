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
3. El mapa de ubicaciones ya viene creado (vacío), una zona por tipo de equipo:
   Boots 1–4 (`B1-01`…), Snowboard (`SB-`), Ski 1–2 (`S1-`, `S2-`), Ski Kids (`SK-`) y VIP (`V-`).
   Están colocadas como en el plano del guardaesquís:
   ```
   [ Boots 1 ] [ Boots 2 ] [ Snowboard | Ski 1 ]      ┃ Ski 2
                                                     ┃ (pared derecha)
   [ Boots 3 ] door [ Boots 4 ] door [ VIP | Ski Kids ] door
   ```
   La numeración está en `HALL_LAYOUT`, al principio de `app.py` (por ahora 20 por zona, 10 en VIP).
   El dibujo del plano que muestra la app está en `HALL_PLAN` (`static/index.html`); usa los mismos
   nombres de zona, así que si renombras una zona hay que cambiarlo también allí. Si borras alguna, **Hall setup** (app)
   o **Storage map** (/admin) muestran *Restore default layout* para volver a crear las que falten.
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

## Check-in y storage

- Solo se guardan **boots**, **skis (con sus poles)** y **snowboards**.
- Cada equipo va a **su propia ubicación**, en la zona de su tipo: un cliente con una snowboard y
  un par de boots usa dos ubicaciones. La app propone la primera libre de cada zona y el valet
  puede cambiarla. Los skis de niño van a *Ski Kids*; los clientes VIP usan la zona *VIP*
  (y, si está llena, la zona normal).
- Cada zona tiene un tipo (*Holds*): se elige al crear la fila en Hall setup / Storage map
  o con el selector de cada zona. Las zonas sin tipo no se usan en la asignación automática.
- **Check in by room**: sin tarjeta, solo con el número de habitación (para móviles sin Web NFC).
  La tarjeta se puede añadir después desde la habitación.
- **Fotos**: cada equipo puede llevar una foto. Se reduce en el móvil y se guarda en PostgreSQL
  (tabla `photos`); el registro solo guarda su id. Las fotos que ya no usa ningún equipo se
  borran a los `PHOTO_KEEP_DAYS` días (por defecto 90). Las copias de seguridad JSON no incluyen fotos.

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
