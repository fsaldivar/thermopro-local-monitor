# ThermoPro BLE Monitor

Lee temperatura y humedad de un termómetro **ThermoPro TP357 / TP358 / TP359**,
las guarda en SQLite y las muestra en la pantalla LCD del propio Raspberry Pi.

**Todo ocurre en la Raspberry.** No hay nube, no hay broker externo, no sale
ni un byte a internet.

Verificado sobre hardware real: **TP359S** (`FB:E7:C4:CF:3A:F6`) + Raspberry Pi
3 Model B + **Waveshare 1.3inch LCD HAT** (ST7789 240×240), con BlueZ 5.82,
bleak 3.0.2 y Python 3.13.

## Cómo está montado

```
   sensor BLE  ──anuncios──▶  thermopro-recorder  ──▶  thermopro.db (SQLite)
                                                            │
                                                            ├──▶ thermopro-display  ──▶ LCD
                                                            └──▶ consultas / export
```

Dos servicios independientes: el panel lee la base en **solo lectura**, así que
si la pantalla falla el registro sigue intacto.

## Por qué no se conecta al sensor

El TP359 emite temperatura y humedad en el `manufacturer data` de cada anuncio
BLE, cada 1,5–3 s. Escucharlos da el mismo valor que abrir una sesión GATT
(comprobado byte a byte) y además no gasta batería del sensor, no ocupa su
única ranura de conexión, no bloquea la app móvil y no hay nada que reconectar.

### Formato de trama

Los dos primeros bytes del anuncio los interpreta BlueZ como *company id*, pero
aquí son datos: **el byte alto del id es el byte bajo de la temperatura**.

```
anuncio (7 bytes reconstruidos)      notificación GATT (7 bytes)
c2 39 01 2f 22 13 01                 c2 00 00 39 01 2f 2c
 |  \___/  |                          |     \___/  |
 |    |    humedad (uint8, %)         |       |    humedad
 |    temperatura (int16 LE, décimas) |       temperatura
 prefijo fijo                         prefijo fijo
        -> 31.3 °C, 47 %                     -> 31.3 °C, 47 %
```

### La trampa: BlueZ acumula ids rancios

BlueZ va sumando cada *company id* que ve y **no los caduca nunca**. Como el id
lleva medio valor de temperatura, al rato un mismo anuncio trae dos lecturas
—la actual y una vieja— sin orden fiable entre ellas (activo y pasivo las
ordenan al revés):

```
n=2  0x39c2:012e221301 | 0x3ac2:012e221301     -> 31.3 °C y 31.4 °C
```

El monitor detecta la ambigüedad, **descarta la trama y purga el dispositivo de
la caché de BlueZ** (`RemoveDevice` por D-Bus, sin root); la lectura se reanuda
en ~1 s. Aquí la diferencia era de 0,1 °C, pero el byte ambiguo es el bajo:
nada impide que sean 31,3 y 39,0.

## Instalación

```bash
git clone https://github.com/fsaldivar/thermopro-ble-monitor.git
cd thermopro-ble-monitor
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

El panel LCD usa el `python3` del sistema, que en Raspberry Pi OS ya trae PIL,
numpy, spidev y gpiozero empaquetados.

### Preparar el hardware

```bash
sudo raspi-config nonint do_spi 0        # habilitar SPI (persistente)
sudo rfkill unblock bluetooth            # el adaptador puede venir bloqueado
sudo sed -i 's/^#AutoEnable=true/AutoEnable=true/' /etc/bluetooth/main.conf
sudo systemctl restart bluetooth
```

## Uso

```bash
# Buscar el sensor (marca los ThermoPro y ya decodifica su lectura)
.venv/bin/python thermopro_monitor.py scan

# Ver lecturas en crudo, una línea JSON por lectura
.venv/bin/python thermopro_monitor.py watch --address FB:E7:C4:CF:3A:F6

# Guardar en la base local
.venv/bin/python thermopro_monitor.py record --db thermopro.db

# Panel en la pantalla del HAT
python3 display_app.py --db thermopro.db
```

Sin `--address` se autodetecta cualquier dispositivo cuyo nombre empiece por
`TP`. Todas las opciones tienen variable de entorno equivalente: ver
`.env.example`.

También existe el subcomando `mqtt` para publicar en un broker, con
descubrimiento de Home Assistant. No hace falta para el uso local.

## La base de datos

```sql
devices  (device_id, address, name, first_seen, last_seen)
readings (ts, device_id, temperature_c, humidity, rssi)   -- ts en epoch UTC
```

Más una vista `readings_local` con la hora ya en local y legible:

```bash
sqlite3 thermopro.db "SELECT * FROM readings_local LIMIT 10"
sqlite3 -csv thermopro.db "SELECT * FROM readings_local" > export.csv
```

Está en modo **WAL**, para que el panel y cualquier consulta lean mientras el
grabador escribe, y con `synchronous=NORMAL` para no castigar la tarjeta SD.

Una lectura por minuto son unas 525.000 filas al año, **≈30 MB**. `--retention-days`
purga lo más viejo si algún día bajas mucho el intervalo.

Cuando no llegan datos **no se escribe nada**: un hueco en la tabla es el
registro honesto de "no había dato"; rellenarlo con el último valor sería
inventárselo.

## La pantalla

`st7789.py` es un driver propio para el **Waveshare 1.3inch LCD HAT**:

| | |
| --- | --- |
| Controlador | ST7789, 240×240 |
| CS / DC / RST / BL | CE0 / GPIO25 / GPIO27 / GPIO24 |
| Botones | KEY1=21, KEY2=20, KEY3=16 |
| Joystick | arriba=6, abajo=19, izquierda=5, derecha=26, pulsar=13 |
| Volcado de pantalla completa | ~70 ms |

Tres vistas, se cambia con **KEY1** o el joystick izquierda/derecha:

1. **Ahora** — temperatura grande, humedad y **antigüedad del dato**. Si pasan
   más de 10 minutos sin lectura, el número se apaga a gris y la antigüedad se
   pone en rojo: un valor congelado no debe parecer actual.
2. **Histórico** — gráfica con mínima y máxima. **KEY3** cambia el rango
   (3 h / 12 h / 24 h / 3 días).
3. **Sistema** — IP, temperatura de CPU, uptime y número de lecturas.

**KEY2** apaga y enciende la retroiluminación.

Dos detalles del ST7789 que cuestan una tarde si no se saben:

- Necesita **inversión activada** (`0x21`) o los colores salen en negativo.
- Su RAM es de 240×**320** pero el panel enseña 240×240, así que las rotaciones
  que invierten un eje necesitan un **desplazamiento de 80 píxeles** o la
  imagen sale corrida. Está en la tabla `ROTATIONS` del driver.

`--rotation 270` deja la imagen derecha con los botones a la izquierda.

## Servicios

```bash
sudo cp thermopro-recorder.service thermopro-display.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now thermopro-recorder thermopro-display
journalctl -u thermopro-recorder -f
```

Ajusta `User=` y las rutas si no lo tienes en `/home/fermax/thermopro-ble-monitor`.

## Robustez

Probado cortando el servicio de Bluetooth en caliente:

- Si pasan `--stale-after` segundos (300) sin datos, se deja de escribir y el
  panel lo marca en rojo.
- Si pasan `--restart-after` segundos (90) sin **ningún** anuncio, se reinicia
  el escaneo: BlueZ se queda mudo de vez en cuando.
- El escaneo reintenta indefinidamente con espera creciente hasta 60 s.

## Tests

El decodificador se prueba con tramas reales capturadas del sensor y no
necesita bleak ni BLE:

```bash
python3 -m unittest discover -s tests -v
```

## Problemas frecuentes

**No aparece ningún dispositivo BLE.** El adaptador puede estar bloqueado:

```bash
cat /sys/class/rfkill/rfkill0/soft     # 1 = bloqueado
sudo rfkill unblock bluetooth
bluetoothctl power on
```

**La pantalla se ve negra pero iluminada.** No le está llegando nada: revisa
que el HAT esté bien asentado (un mal contacto da exactamente ese síntoma) y
que SPI esté activo (`ls /dev/spidev*`).

**Los colores salen en negativo.** Falta el comando de inversión `0x21`.

**La imagen sale corrida o con una franja.** Es el desplazamiento de 80 píxeles
de las rotaciones 180 y 270.

**`--passive` falla.** El escaneo pasivo necesita `bluetoothd --experimental`
(BlueZ ≥ 5.56, kernel ≥ 5.10). No evita que BlueZ acumule ids rancios; la purga
hace falta en ambos modos.

## Licencia

MIT.
