# reconstructed_data

Datos enriquecidos o reconstruidos a partir de fuentes primarias (ONPE, RENIEC, INEI, Minedu).
El objetivo es producir tablas que no existen en ninguna fuente original pero que son necesarias
para el análisis electoral.

---

## Archivos

### `locales_con_iiee.csv`

Locales de votación de la ONPE cruzados con una multitud de bases de datos: ONPE por pedido de
acceso a datos, SIGMED del Minedu, Google Geocoding API, Nominatim, y geolocalización ad hoc
manual.

| Columna | Descripción |
|---|---|
| `codigoLocalVotacion` | Código de local ONPE |
| `nombreLocalVotacion` | Nombre del local según ONPE |
| `reniec` / `inei` | Ubigeo de distrito (RENIEC e INEI) |
| `departamento` / `provincia` / `distrito` | Jerarquía geográfica |
| `match_metodo` / `match_detalle` | Método usado para vincular ONPE ↔ Local |
| `iiee_*` | Datos del Minedu: código de IE, nombre, dirección, nivel, coordenadas, altitud |

### `onpe_to_inei.csv`

Tabla de corrección de nombres de distrito: mapea los nombres tal como los usa la ONPE a los
nombres que aparecen en `locales_con_iiee.csv` (nomenclatura INEI).

| Columna | Descripción |
|---|---|
| `nombre_onpe` | Nombre del distrito según ONPE |
| `nombre_locales_con_iiee` | Nombre equivalente en `locales_con_iiee.csv` |

---

## Notas metodológicas

El detalle de cada acta en el API de la ONPE **no incluye código de local**: los locales solo se
identifican por ubigeo (`ubigeoNivel01/02/03`) y nombre. **Hay varios locales con exactamente el
mismo nombre**, pero nunca comparten a la vez los mismos ubigeos. Para cruzarlos con otras fuentes
es necesario usar esos literales como clave de join.

Dado que los nombres de distrito no siempre coinciden entre la ONPE y otras bases, se construyó
la tabla `onpe_to_inei.csv` para normalizar las diferencias antes de hacer el cruce.
