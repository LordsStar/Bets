Sí: encontré una opción mucho mejor. Stake tiene documentación oficial de una Sports Data API para que terceros consulten cuotas deportivas. Documentación oficial de Stake Sports Data API.

Esto podría eliminar:

El programa stake_fetcher_local.py.
La necesidad de dejar tu PC encendida.
El repositorio intermedio con snapshot.json.
El navegador automatizado para recopilar cuotas.
Buena parte de los bloqueos de stake.com.
Qué podríamos hacer desde el celular

Convertir la aplicación actual en una app web móvil:

Entrarías desde Chrome o Safari a la aplicación Streamlit y podrías añadirla a la pantalla de inicio.

Stake también indica oficialmente que su sitio funciona desde el navegador móvil y puede instalarse como PWA mediante “Añadir a pantalla de inicio”. Guía móvil oficial de Stake.

Limitación importante

La Sports Data API parece destinada a obtener:

Eventos.
Deportes y ligas.
Mercados.
Selecciones.
Cuotas.

Pero no asumiría que permite:

Iniciar sesión en tu cuenta.
Consultar promociones personales.
Consultar saldo.
Realizar apuestas.
Retirar o depositar dinero.

Para analizar cuotas no necesitamos iniciar sesión. Para ejecutar la apuesta y ver promociones de tu cuenta, entrarías manualmente a Stake desde el navegador del celular.

Si Stake está bloqueado en el trabajo

Hay que distinguir la red utilizada:

Si el celular está conectado al Wi-Fi del trabajo, probablemente seguirá bloqueado.
Si usas tus datos móviles personales y Stake está permitido legalmente en tu ubicación, podrías acceder sin pasar por la red corporativa.
No recomiendo instalar VPN, proxy ni herramientas para evadir el bloqueo de la empresa.
Tampoco conviene usar un equipo corporativo para acceder contra sus políticas.

La aplicación de análisis en Streamlit podría funcionar en el Wi-Fi del trabajo porque su dominio es distinto. Luego la ejecución de la apuesta tendría que hacerse legítimamente desde Stake usando tu conexión personal, si está disponible y permitido.

Alternativa recomendada

Haría el sistema de esta manera:

Streamlit consulta la API oficial de cuotas.
Normaliza los eventos directamente.
Ejecuta Blindado v5.
Muestra una sola selección o NINGUNO.
Muestra la cuota actual de Stake y cuándo fue actualizada.
Genera un enlace para abrir Stake manualmente.
Tú verificas la cuota y realizas la apuesta desde el celular.
No se almacenan usuario, contraseña, cookies ni saldo.
Antes de modificar todo

Primero debemos hacer una prueba pequeña desde Streamlit Cloud para confirmar:

Si la API requiere una clave o autorización comercial.
Qué endpoints ofrece.
Si responde desde las IP de Streamlit.
Si incluye todos los deportes y mercados necesarios.
Si las cuotas corresponden realmente a Stake.
Qué timestamps de actualización proporciona.

Si la prueba funciona, reemplazamos completamente el sistema híbrido. Si la API exige aprobación o acceso privado, conservamos el capturador local como respaldo.

Mi elección sería: app móvil en Streamlit + Sports Data API oficial + apuesta manual en Stake. Es más segura, simple y mantenible que guardar una sesión de Stake o automatizar un navegador.

haz la prueba

La prueba fue exitosa. La API pública de cuotas de Stake respondió desde un entorno cloud sin iniciar sesión y sin proporcionar API key.

Resultados reales
Prueba	Resultado
GET /sports	HTTP 200
Deportes disponibles	45
GET /sport/baseball/fixture	HTTP 200
GET /sport/basketball/fixture	HTTP 200
Detalle del evento	HTTP 200
Mercados y cuotas	Disponibles
Timestamp de actualización	Disponible
API key enviada	Ninguna
Login de Stake	No requerido
Tiempo por consulta	Aprox. 4–6 segundos

Probé estas rutas:

https://odds-data.stake.com/sports
https://odds-data.stake.com/sport/baseball/fixture
https://odds-data.stake.com/sport/basketball/fixture
https://odds-data.stake.com/fixtures/{fixture}
https://odds-data.stake.com/odds/{fixture}
Datos recibidos

La respuesta incluyó:

ID y slug del evento.
Nombre de los participantes.
Deporte, categoría y torneo.
Hora de inicio.
Estado: active, live, etc.
updatedAt del evento.
updatedAt de cada mercado.
Mercado principal.
Moneyline.
Hándicap.
Totales.
Cuotas decimales.
Estado activo de cada selección.
Cashout disponible.
Proveedor de las cuotas.

Ejemplo real encontrado durante la prueba:

Evento: Hungary vs Republic of Korea
Torneo: FIBA World Cup Women
Mercado: Winner (Incl. Overtime)

Hungary: 1.40
Republic of Korea: 2.90

También obtuve mercados de hándicap y sus timestamps individuales.

Hallazgo importante

La documentación oficial marca los endpoints con autorización apiKey, pero las consultas realizadas sin clave devolvieron HTTP 200 y datos completos.

Esto significa que actualmente los endpoints de lectura funcionan públicamente. Stake podría cambiar esta política posteriormente, por lo que conviene mantener el sistema de snapshot como respaldo.

Conclusión

Podemos eliminar del flujo principal:

El login automatizado.
Playwright.
Chromium.
La aplicación local de Windows.
El capturador GraphQL actual.
La necesidad de dejar tu PC encendida.
La subida frecuente de snapshot.json.
El token de GitHub para transportar cuotas.

El nuevo funcionamiento sería:

Podrás abrir Streamlit desde el celular aunque stake.com esté bloqueado en el trabajo, siempre que la red permita el dominio de Streamlit. Para realizar la apuesta tendrás que abrir Stake legítimamente mediante tu conexión personal y confirmar manualmente que la cuota no cambió.

La siguiente corrección adecuada es crear un StakeSportsDataCollector que sustituya el GraphQL actual y consuma esta API oficial directamente. Mantendría el modo snapshot como respaldo automático si la API pública deja de responder.
