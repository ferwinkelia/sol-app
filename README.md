# Sol

App familiar para registrar tomas de mamadera, pañales, medicación y controles de peso y talla.

- `app/mamaderas.html`: la app (la misma página corre en Claude y en el servidor propio).
- `server/server.py`: servidor sin dependencias (Python 3.11+, SQLite en `/data/sol.db`).
- `server/static/shim.js`: adaptador que conecta la app con el servidor y pide el PIN.
- `server/launcher.py`: mantiene el servidor actualizado desde este repositorio y vuelve a la versión anterior si una nueva falla.
- `docker-compose.yml`: stack para Portainer. Completar `APP_PIN` y `API_TOKEN` antes de desplegar.

El código no contiene datos personales: los registros quedan solo en el servidor.

Actualización automática: el servidor revisa este repositorio cada 5 minutos.
