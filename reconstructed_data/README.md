## Archivos

### `locales_con_iiee.csv`

Locales de votación de la ONPE cruzados con una multitud de base de datos: ONPE por pedido de datos, SIGMED del Minedu, Google Geocoding API, Nominatim, y geolocalizacion adhoc manual.

| Columna | Descripción |
|---|---|
| `codigoLocalVotacion` | Código de local ONPE |
| `nombreLocalVotacion` | Nombre del local según ONPE |
| `reniec` / `inei` | Ubigeo de distrito (RENIEC e INEI) |
| `departamento` / `provincia` / `distrito` | Jerarquía geográfica |
| `match_metodo` / `match_detalle` | Método usado para vincular ONPE <-> Local |
| `iiee_*` | Datos del Minedu: código de IE, nombre, dirección, nivel, coordenadas, altitud |

### `onpe_to_inei.csv`

Tabla de corrección de nombres de distrito: mapea los nombres tal como los usa la ONPE a los
nombres que aparecen en `locales_con_iiee.csv` (INEI).

| Columna | Descripción |
|---|---|
| `nombre_onpe` | Nombre del distrito según ONPE |
| `nombre_locales_con_iiee` | Nombre equivalente en `locales_con_iiee.csv` |

---

## Notas

En el detalle de cada acta del API de la ONPE **no tiene codigo de local** en el deta: solo se identifican por ubigeo (`ubigeoNivel01/02/03`) y nombre. **Hay varios locales con exactamente el mismo nombre**, pero nunca comparten a la vez los mismos ubigeos. Para cruzarlos con otras fuentes es necesario usar esos literales como clave de join.

Dado que los nombres de distrito no siempre coinciden entre la ONPE y otras bases, se construyó la tabla `onpe_to_locales.csv` para normalizar las diferencias antes de hacer el cruce.
