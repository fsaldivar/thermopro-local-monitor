# CLAUDE.md

Monitor de un termómetro BLE **ThermoPro TP359S** sobre una **Raspberry Pi 3
Model B**, con registro local en SQLite y panel en la LCD del HAT.

## Restricción que manda sobre todo lo demás

**Nada sale de la Raspberry.** Sin nube, sin brokers externos, sin telemetría.
El subcomando `mqtt` existe y funciona, pero ahora mismo no se usa. Si algo
necesita salir a la red, pregunta antes.

## Hardware

| | |
| --- | --- |
| Sensor | ThermoPro TP359S, MAC `FB:E7:C4:CF:3A:F6` |
| Placa | Raspberry Pi 3 Model B, 905 MB RAM, 4 núcleos |
| Pantalla | Waveshare 1.3inch LCD HAT, **ST7789 240×240** |
| Pines LCD | CS=**CE0**, DC=25, RST=27, BL=24 |
| Botones | KEY1=21, KEY2=20, KEY3=16 |
| Joystick | arriba=6, abajo=19, izq=5, der=26, pulsar=13 |
| WiFi | **solo 2.4 GHz** (chip BCM43430A1) — verificado, no es una suposición |

La Pi arranca en `multi-user.target`, sin escritorio, con 16 units de usuario
enmascarados. No reintroduzcas nada gráfico: el panel va por SPI directo.

## Cómo está montado

```
sensor BLE ──anuncios──▶ thermopro-recorder ──▶ /var/lib/thermopro/thermopro.db
                                                       ├──▶ thermopro-display ──▶ LCD
                                                       └──▶ grafana-server :3000
```

Tres servicios systemd, todos `enabled`:

- `thermopro-recorder` — usa el venv del proyecto (`.venv/bin/python`), necesita bleak
- `thermopro-display` — usa **`/usr/bin/python3` del sistema**, porque PIL, numpy,
  spidev y gpiozero vienen empaquetados por apt. No lo pases al venv.
- `grafana-server` — opcional, ~230 MB. Párala si necesitas memoria.

## Trampas que ya costaron horas — no las reintroduzcas

**1. BlueZ acumula company id rancios.** Los datos del sensor van en el
`manufacturer data`, y el byte alto del "company id" es el byte bajo de la
temperatura. BlueZ suma cada id que ve y nunca los caduca, así que un anuncio
acaba trayendo la lectura actual y otra vieja, sin orden fiable entre ellas.
Por eso `measurements_in_advertisement()` descarta la trama cuando hay más de
un valor distinto y purga el dispositivo con `RemoveDevice` por D-Bus. **Nunca
itheres el dict de manufacturer_data cogiendo el primero o el último.**

**2. No te conectes por GATT para leer.** El anuncio ya trae los mismos valores
(verificado byte a byte). Conectarse gasta batería del sensor, ocupa su única
ranura de conexión y bloquea la app móvil.

**3-bis. El estilo del panel vive en `ui.py`**, no repartido por las vistas.
Todo se dibuja a **3× y se reduce con LANCZOS** (`Lienzo.terminar()`): PIL no
suaviza bordes, y sin eso los arcos y diagonales salen dentados. Medido en el
Pi 3: 116 ms por fotograma a 3×, 14 % de un núcleo en marcha. Las coordenadas
de las vistas van en el espacio lógico de 240×240; `Lienzo.p()` las escala.

Los iconos son glifos de **Font Awesome 4.7**, que viene en el paquete apt
`fonts-font-awesome` — sin descargas, coherente con la regla de todo local.
Los codepoints están en `ui.ICO`. La tipografía es **Inter** (`fonts-inter`),
con `InterDisplay` para las cifras grandes. Si añades tamaños nuevos, mételos
en `ui.precargar()`: cargar una fuente la primera vez cuesta casi un segundo.

**3. El ST7789 necesita inversión (`0x21`)** o los colores salen en negativo.
Y su RAM es 240×**320** sobre un panel de 240×240, así que las rotaciones que
invierten un eje necesitan **desplazar 80 píxeles** (tabla `ROTATIONS` en
`st7789.py`). La rotación buena es **270**: imagen derecha con los botones a la
izquierda.

**4. La consulta de Grafana es asimétrica a propósito:**

```sql
SELECT ts AS time, ... WHERE ts*1000 >= $__from AND ts*1000 <= $__to
```

El plugin `frser-sqlite-datasource` convierte la columna de tiempo de segundos
a milisegundos **por su cuenta** (pasarle `ts*1000` manda las marcas al año
13500 y el panel dice *"Data outside time range"*), pero `$__from`/`$__to` sí
llegan en milisegundos. Tampoco entiende `$__timeFrom()`, `$__timeFilter()` ni
`$__unixEpochFilter()`: fallan con *missing named argument*.

**5. La base vive en `/var/lib/thermopro/`, no en el home.** El home es `700` y
el usuario `grafana` no puede atravesarlo. Además **SQLite en modo WAL no
admite lectores de solo lectura**: necesitan escribir `-shm` y `-wal`. De ahí
el grupo `thermopro`, al que pertenecen `fermax` y `grafana`.

**6. `pkill -f` por SSH se mata a sí mismo**: el patrón coincide con la línea
de comandos del propio shell remoto. Usa systemd o filtra el PID.

## Comandos

```bash
# tests del decodificador: no necesitan BLE ni bleak
python3 -m unittest discover -s tests -v

# ver el sensor en crudo
.venv/bin/python thermopro_monitor.py scan
.venv/bin/python thermopro_monitor.py watch --address FB:E7:C4:CF:3A:F6

# consultar el histórico
sqlite3 /var/lib/thermopro/thermopro.db "SELECT * FROM readings_local LIMIT 10"

# servicios
journalctl -u thermopro-recorder -f
systemctl restart thermopro-display
```

Para trabajar en la pantalla sin tener que mirarla: las vistas son funciones
puras que devuelven una imagen PIL, así que se pueden volcar a PNG y revisar.
Así se encontró un solape que a simple vista no se veía.

## Convenciones

- Comentarios y mensajes en español, sin tildes en el código fuente.
- Comenta **por qué**, no qué hace la línea. Los comentarios que hay explican
  decisiones no obvias; mantén ese listón.
- Si no llegan datos, **no escribas nada**: un hueco en la tabla es el registro
  honesto de "no había dato". Rellenarlo con el último valor es inventárselo.
- Los tests del decodificador usan tramas reales capturadas del sensor. Si
  cambias el decodificador, captura tramas nuevas en vez de ajustar los tests.

## Pendiente

- **Batería**: el sensor no la expone por BLE (no hay Battery Service; leídas
  todas las características). Los únicos candidatos son bytes sin identificar:
  `22 13 01` en el anuncio y `2c` en la notificación. Plan: poner pilas nuevas
  y comparar tramas antes/después.
- Confirmar si la imagen llena el panel o queda franja negra en algún borde.
- Panel web propio como alternativa ligera a Grafana.
