#!/usr/bin/env python3
"""Monitor BLE para termometros ThermoPro (TP357 / TP358 / TP359).

Lee temperatura y humedad directamente de los anuncios BLE, sin conectarse al
dispositivo: no gasta bateria del sensor, no bloquea la app movil y no hay
sesiones GATT que reconectar. Opcionalmente publica las lecturas en MQTT.

Subcomandos:
    scan    Lista dispositivos BLE cercanos y marca los ThermoPro.
    watch   Imprime cada lectura como una linea JSON.
    record  Guarda las lecturas en una base SQLite local.
    mqtt    Publica las lecturas en un broker MQTT.

bleak y paho-mqtt se importan de forma perezosa para que el decodificador y sus
tests se puedan usar sin tener el stack BLE instalado.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sqlite3
import struct
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger("thermopro")

# --------------------------------------------------------------------------- #
# Formato de trama
#
# Los dos primeros bytes del anuncio los interpreta BlueZ como "company id", pero
# en estos aparatos son datos: el byte alto del id es el byte BAJO de la
# temperatura. Trama de anuncio reconstruida (7 bytes), capturada de un TP359S:
#
#     c2 39 01 2f 22 13 01   ->  31.3 C   47 %
#     c2 3a 01 2f 22 13 01   ->  31.4 C   47 %
#      |  \___/  |
#      |    |    humedad (uint8, %)
#      |    temperatura (int16 LE, decimas de grado)
#      prefijo fijo
#
# La notificacion GATT lleva los mismos valores con otra cabecera (7 bytes):
#
#     c2 00 00 39 01 2f 2c   ->  31.3 C   47 %
#
# Los bytes finales (22 13 01 / 2c) no estan identificados; se ignoran.
# --------------------------------------------------------------------------- #

FRAME_LEN = 7
FRAME_PREFIX = 0xC2
GATT_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d2b10"

TEMP_MIN_C, TEMP_MAX_C = -40.0, 80.0
HUM_MIN, HUM_MAX = 0, 100


@dataclass(frozen=True, order=True)
class Measurement:
    """Un par temperatura/humedad ya validado."""

    temperature_c: float
    humidity: int


def _build(temp_tenths: int, humidity: int) -> Measurement | None:
    """Valida el rango antes de aceptar una trama.

    Sin esto, cualquier anuncio ajeno que case en longitud se publica como una
    lectura real.
    """
    temperature_c = temp_tenths / 10
    if not TEMP_MIN_C <= temperature_c <= TEMP_MAX_C:
        return None
    if not HUM_MIN <= humidity <= HUM_MAX:
        return None
    return Measurement(round(temperature_c, 1), humidity)


def decode_advertisement_frame(company_id: int, payload: bytes) -> Measurement | None:
    """Decodifica una entrada de manufacturer_data. None si no encaja."""
    if not 0 <= company_id <= 0xFFFF:
        return None
    raw = company_id.to_bytes(2, "little") + bytes(payload)
    if len(raw) != FRAME_LEN or raw[0] != FRAME_PREFIX:
        return None
    return _build(*struct.unpack_from("<hB", raw, 1))


def decode_gatt_frame(payload: bytes) -> Measurement | None:
    """Decodifica una notificacion GATT. None si no encaja."""
    raw = bytes(payload)
    if len(raw) != FRAME_LEN or raw[0] != FRAME_PREFIX:
        return None
    return _build(*struct.unpack_from("<hB", raw, 3))


def measurements_in_advertisement(manufacturer_data: dict) -> set[Measurement]:
    """Devuelve las lecturas distintas que contiene un anuncio.

    BlueZ acumula los company id vistos para un mismo dispositivo y nunca los
    caduca. Como el id lleva medio valor de temperatura, un anuncio puede traer
    a la vez la lectura actual y otra de hace minutos, sin orden fiable entre
    ellas (activo y pasivo las ordenan distinto). Mas de un valor distinto aqui
    significa "ambiguo": quien llama debe descartar la trama y purgar la cache.
    """
    found = set()
    for company_id, payload in (manufacturer_data or {}).items():
        m = decode_advertisement_frame(company_id, payload)
        if m is not None:
            found.add(m)
    return found


@dataclass(frozen=True)
class Reading:
    """Una lectura lista para publicar."""

    device_id: str
    address: str
    name: str | None
    measurement: Measurement
    rssi: int | None
    source: str
    timestamp: datetime

    @property
    def temperature_c(self) -> float:
        return self.measurement.temperature_c

    @property
    def humidity(self) -> int:
        return self.measurement.humidity

    def payload(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "address": self.address,
            "name": self.name,
            "temperature_c": self.temperature_c,
            "humidity": self.humidity,
            "rssi": self.rssi,
            "source": self.source,
        }


def device_id_for(address: str) -> str:
    return address.replace(":", "").replace("-", "").lower()


def now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Cache de BlueZ
# --------------------------------------------------------------------------- #


class BlueZCache:
    """Borra dispositivos de la cache de BlueZ via D-Bus.

    Es la unica forma de tirar los company id rancios: BlueZ los va sumando al
    dict del dispositivo y solo se vacia al eliminarlo. No necesita root.
    """

    def __init__(self, adapter: str = "hci0", min_interval: float = 5.0) -> None:
        self._adapter_path = f"/org/bluez/{adapter}"
        self._min_interval = min_interval
        self._bus = None
        self._message = None
        self._last_purge: dict[str, float] = {}
        self._unavailable_logged = False

    async def connect(self) -> None:
        try:
            from dbus_fast import BusType, Message
            from dbus_fast.aio import MessageBus
        except ImportError:
            log.warning(
                "dbus-fast no disponible: no se podran purgar los anuncios "
                "rancios de BlueZ y se descartaran las tramas ambiguas"
            )
            return
        self._message = Message
        self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    async def close(self) -> None:
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None

    async def purge(self, address: str) -> None:
        """Elimina un dispositivo de la cache, como mucho cada min_interval."""
        if self._bus is None:
            return
        last = self._last_purge.get(address, 0.0)
        if time.monotonic() - last < self._min_interval:
            return
        self._last_purge[address] = time.monotonic()
        path = f"{self._adapter_path}/dev_" + address.upper().replace(":", "_")
        try:
            reply = await self._bus.call(
                self._message(
                    destination="org.bluez",
                    path=self._adapter_path,
                    interface="org.bluez.Adapter1",
                    member="RemoveDevice",
                    signature="o",
                    body=[path],
                )
            )
        except Exception as exc:  # el bus puede caerse; no es fatal
            log.debug("RemoveDevice(%s) fallo: %r", address, exc)
            return
        if reply is not None and reply.message_type.name == "ERROR":
            if not self._unavailable_logged:
                log.warning("RemoveDevice no disponible: %s", reply.body)
                self._unavailable_logged = True
        else:
            log.debug("cache de %s purgada", address)


# --------------------------------------------------------------------------- #
# Escucha BLE
# --------------------------------------------------------------------------- #


def build_scanner_kwargs(adapter: str, passive: bool) -> dict:
    """Argumentos de BleakScanner validos en las dos generaciones de la API.

    bleak >= 1.0 quiere el adaptador dentro de `bluez=`; antes era un kwarg
    suelto y pasarlo ahora suelta un DeprecationWarning.
    """
    kwargs: dict = {}
    or_patterns = None
    try:
        from bleak.args.bluez import BlueZScannerArgs, OrPattern

        modern = True
    except ImportError:  # bleak < 1.0
        modern = False
        BlueZScannerArgs = OrPattern = None
        if passive:
            from bleak.backends.bluezdbus.advertisement_monitor import OrPattern
            from bleak.backends.bluezdbus.scanner import BlueZScannerArgs

    if passive:
        kwargs["scanning_mode"] = "passive"
        # BlueZ exige al menos un patron para el monitor de anuncios.
        or_patterns = [OrPattern(0, 0xFF, bytes([FRAME_PREFIX]))]

    if modern:
        bluez = {"adapter": adapter}
        if or_patterns:
            bluez["or_patterns"] = or_patterns
        kwargs["bluez"] = BlueZScannerArgs(**bluez)
    else:
        kwargs["adapter"] = adapter
        if or_patterns:
            kwargs["bluez"] = BlueZScannerArgs(or_patterns=or_patterns)
    return kwargs


class ThermoProListener:
    """Escucha anuncios y entrega lecturas validadas."""

    def __init__(
        self,
        on_reading,
        addresses: set[str] | None = None,
        name_prefix: str = "TP",
        adapter: str = "hci0",
        passive: bool = False,
        restart_after: float = 120.0,
    ) -> None:
        self._on_reading = on_reading
        self._addresses = {a.upper() for a in addresses} if addresses else None
        self._name_prefix = name_prefix.upper()
        self._adapter = adapter
        self._passive = passive
        self._restart_after = restart_after
        self._cache = BlueZCache(adapter)
        self._ambiguous: set[str] = set()
        self._last_advert = 0.0

    def _matches(self, address: str, name: str | None) -> bool:
        if self._addresses is not None:
            return address.upper() in self._addresses
        return bool(name) and name.upper().startswith(self._name_prefix)

    def _on_advertisement(self, device, adv) -> None:
        # bleak invoca esto desde el manejador de D-Bus: una excepcion aqui se
        # convierte en un traceback suelto por cada anuncio.
        try:
            self._handle_advertisement(device, adv)
        except Exception:
            log.exception("fallo procesando un anuncio de %s", device.address)

    def _handle_advertisement(self, device, adv) -> None:
        address = device.address.upper()
        name = adv.local_name or getattr(device, "name", None)
        if not self._matches(address, name):
            return

        found = measurements_in_advertisement(adv.manufacturer_data)
        if not found:
            return
        if len(found) > 1:
            # Anuncio contaminado con un valor viejo: descartar y purgar.
            log.debug("anuncio ambiguo de %s: %s", address, sorted(found))
            self._ambiguous.add(address)
            return

        self._last_advert = time.monotonic()
        self._on_reading(
            Reading(
                device_id=device_id_for(address),
                address=address,
                name=name,
                measurement=found.pop(),
                rssi=adv.rssi,
                source="advertisement",
                timestamp=now(),
            )
        )

    def _build_scanner(self):
        from bleak import BleakScanner

        return BleakScanner(
            detection_callback=self._on_advertisement,
            **build_scanner_kwargs(self._adapter, self._passive),
        )

    async def run(self, stop: asyncio.Event) -> None:
        await self._cache.connect()
        backoff = 1.0
        try:
            while not stop.is_set():
                try:
                    await self._scan_session(stop)
                    backoff = 1.0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.error("el escaneo fallo (%r); reintento en %.0fs", exc, backoff)
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=backoff)
                    except asyncio.TimeoutError:
                        pass
                    backoff = min(backoff * 2, 60.0)
        finally:
            await self._cache.close()

    async def _scan_session(self, stop: asyncio.Event) -> None:
        scanner = self._build_scanner()
        await scanner.start()
        log.info(
            "escaneando en %s (modo %s)",
            self._adapter,
            "pasivo" if self._passive else "activo",
        )
        self._last_advert = time.monotonic()
        try:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                for address in list(self._ambiguous):
                    self._ambiguous.discard(address)
                    await self._cache.purge(address)
                # BlueZ se queda mudo de vez en cuando; reiniciar el escaneo.
                if time.monotonic() - self._last_advert > self._restart_after:
                    log.warning(
                        "sin anuncios en %.0fs, reiniciando el escaneo",
                        self._restart_after,
                    )
                    return
        finally:
            try:
                await scanner.stop()
            except Exception as exc:
                log.debug("scanner.stop() fallo: %r", exc)


# --------------------------------------------------------------------------- #
# Reparto de lecturas y almacenamiento local
# --------------------------------------------------------------------------- #


class PublishGate:
    """Deja pasar como mucho una lectura NUEVA por dispositivo cada `interval`.

    Repetir la ultima lectura no aporta nada y la hace parecer reciente: si no
    ha llegado nada nuevo, no se emite.
    """

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._last_at: dict[str, float] = {}
        self._last_ts: dict[str, datetime] = {}

    def allows(self, reading: Reading) -> bool:
        if self._last_ts.get(reading.device_id) == reading.timestamp:
            return False
        return time.monotonic() - self._last_at.get(reading.device_id, 0.0) >= self._interval

    def mark(self, reading: Reading) -> None:
        self._last_at[reading.device_id] = time.monotonic()
        self._last_ts[reading.device_id] = reading.timestamp


class SqliteStore:
    """Historico local. Nada sale de la Raspberry."""

    SCHEMA = (
        """
        CREATE TABLE IF NOT EXISTS devices (
            device_id  TEXT PRIMARY KEY,
            address    TEXT NOT NULL,
            name       TEXT,
            first_seen INTEGER NOT NULL,
            last_seen  INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS readings (
            ts            INTEGER NOT NULL,
            device_id     TEXT NOT NULL,
            temperature_c REAL NOT NULL,
            humidity      INTEGER NOT NULL,
            rssi          INTEGER,
            PRIMARY KEY (device_id, ts)
        ) WITHOUT ROWID
        """,
        "CREATE INDEX IF NOT EXISTS readings_ts ON readings (ts)",
        """
        CREATE VIEW IF NOT EXISTS readings_local AS
        SELECT datetime(r.ts, 'unixepoch', 'localtime') AS hora,
               d.name, r.temperature_c, r.humidity, r.rssi
        FROM readings r JOIN devices d USING (device_id)
        ORDER BY r.ts DESC
        """,
    )

    def __init__(self, path: str) -> None:
        self._path = os.path.expanduser(path)
        self._db: sqlite3.Connection | None = None

    def open(self) -> None:
        self._db = sqlite3.connect(self._path, timeout=10.0)
        # WAL: la pantalla y el panel web leen mientras el grabador escribe.
        self._db.execute("PRAGMA journal_mode=WAL")
        # La tarjeta SD agradece no hacer fsync en cada fila.
        self._db.execute("PRAGMA synchronous=NORMAL")
        for statement in self.SCHEMA:
            self._db.execute(statement)
        self._db.commit()
        log.info("base de datos: %s", self._path)

    def close(self) -> None:
        if self._db is not None:
            self._db.commit()
            self._db.close()
            self._db = None

    def save(self, reading: Reading) -> None:
        if self._db is None:
            return
        ts = int(reading.timestamp.timestamp())
        self._db.execute(
            """
            INSERT INTO devices (device_id, address, name, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                name = COALESCE(excluded.name, devices.name),
                last_seen = excluded.last_seen
            """,
            (reading.device_id, reading.address, reading.name, ts, ts),
        )
        self._db.execute(
            """
            INSERT OR REPLACE INTO readings
                (ts, device_id, temperature_c, humidity, rssi)
            VALUES (?, ?, ?, ?, ?)
            """,
            (ts, reading.device_id, reading.temperature_c, reading.humidity, reading.rssi),
        )
        self._db.commit()

    def prune(self, days: float) -> int:
        """Borra lo mas viejo de `days`. 0 = guardar para siempre."""
        if self._db is None or days <= 0:
            return 0
        cutoff = int(time.time() - days * 86400)
        cur = self._db.execute("DELETE FROM readings WHERE ts < ?", (cutoff,))
        self._db.commit()
        return cur.rowcount


# --------------------------------------------------------------------------- #
# MQTT
# --------------------------------------------------------------------------- #


class MqttPublisher:
    """Publica lecturas en MQTT con disponibilidad y reconexion automatica."""

    def __init__(
        self,
        host: str,
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        topic_prefix: str = "thermopro",
        state_topic: str | None = None,
        client_id: str = "",
        qos: int = 0,
        retain: bool = True,
        tls: bool = False,
        keepalive: int = 60,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._prefix = topic_prefix.rstrip("/")
        self._state_topic_override = state_topic
        self._client_id = client_id
        self._qos = qos
        self._retain = retain
        self._tls = tls
        self._keepalive = keepalive
        self._client = None
        self._discovered: set[str] = set()

    @property
    def bridge_topic(self) -> str:
        return f"{self._prefix}/status"

    def state_topic(self, device_id: str) -> str:
        if self._state_topic_override:
            return self._state_topic_override
        return f"{self._prefix}/{device_id}/state"

    def availability_topic(self, device_id: str) -> str:
        return f"{self._prefix}/{device_id}/availability"

    def start(self) -> None:
        import paho.mqtt.client as mqtt

        # paho-mqtt 2.x exige declarar la version de la API de callbacks;
        # mqtt.Client() a secas ya no arranca.
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=self._client_id
        )
        if self._username:
            self._client.username_pw_set(self._username, self._password)
        if self._tls:
            self._client.tls_set()
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        # Testamento: si el proceso muere, el broker marca el puente caido.
        self._client.will_set(self.bridge_topic, "offline", qos=self._qos, retain=True)
        # connect_async + loop_start reintenta solo mientras el broker no este.
        self._client.connect_async(self._host, self._port, keepalive=self._keepalive)
        self._client.loop_start()
        log.info("MQTT: conectando a %s:%s", self._host, self._port)

    def stop(self) -> None:
        if self._client is None:
            return
        try:
            self._client.publish(self.bridge_topic, "offline", qos=self._qos, retain=True)
            self._client.disconnect()
        finally:
            self._client.loop_stop()
            self._client = None

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", False):
            log.error("MQTT: conexion rechazada (%s)", reason_code)
            return
        log.info("MQTT: conectado a %s:%s", self._host, self._port)
        client.publish(self.bridge_topic, "online", qos=self._qos, retain=True)
        # Tras una reconexion hay que reenviar el discovery.
        self._discovered.clear()

    def _on_disconnect(self, client, userdata, *args):
        reason = args[1] if len(args) > 1 else args[0] if args else "?"
        log.warning("MQTT: desconectado (%s), reintentando", reason)

    def publish_reading(self, reading: Reading) -> None:
        if self._client is None:
            return
        payload = json.dumps(reading.payload(), separators=(",", ":"))
        self._client.publish(
            self.state_topic(reading.device_id), payload, qos=self._qos, retain=self._retain
        )
        log.info(
            "%s  %.1f C  %d %%  rssi=%s",
            reading.name or reading.address,
            reading.temperature_c,
            reading.humidity,
            reading.rssi,
        )

    def publish_availability(self, device_id: str, online: bool) -> None:
        if self._client is None:
            return
        self._client.publish(
            self.availability_topic(device_id),
            "online" if online else "offline",
            qos=self._qos,
            retain=True,
        )

    def publish_discovery(self, reading: Reading, prefix: str = "homeassistant") -> None:
        """Anuncia el sensor en Home Assistant (MQTT discovery)."""
        if self._client is None or reading.device_id in self._discovered:
            return
        self._discovered.add(reading.device_id)
        device = {
            "identifiers": [reading.device_id],
            "connections": [["mac", reading.address]],
            "name": reading.name or f"ThermoPro {reading.device_id[-4:]}",
            "manufacturer": "ThermoPro",
        }
        availability = [
            {"topic": self.bridge_topic},
            {"topic": self.availability_topic(reading.device_id)},
        ]
        sensors = [
            ("temperature", "temperature", "°C", "temperature_c"),
            ("humidity", "humidity", "%", "humidity"),
        ]
        for key, device_class, unit, field in sensors:
            config = {
                "name": key.capitalize(),
                "unique_id": f"{reading.device_id}_{key}",
                "object_id": f"thermopro_{reading.device_id}_{key}",
                "state_topic": self.state_topic(reading.device_id),
                "value_template": "{{ value_json.%s }}" % field,
                "device_class": device_class,
                "state_class": "measurement",
                "unit_of_measurement": unit,
                "availability": availability,
                "availability_mode": "all",
                "device": device,
            }
            topic = f"{prefix}/sensor/thermopro_{reading.device_id}/{key}/config"
            self._client.publish(topic, json.dumps(config), qos=self._qos, retain=True)
        log.info("MQTT: discovery publicado para %s", reading.device_id)


# --------------------------------------------------------------------------- #
# Subcomandos
# --------------------------------------------------------------------------- #


async def cmd_scan(args) -> int:
    from bleak import BleakScanner

    found: dict[str, dict] = {}

    def callback(device, adv):
        entry = found.setdefault(
            device.address, {"name": None, "rssi": None, "measurement": None, "count": 0}
        )
        entry["count"] += 1
        entry["rssi"] = adv.rssi
        name = adv.local_name or getattr(device, "name", None)
        if name:
            entry["name"] = name
        readings = measurements_in_advertisement(adv.manufacturer_data)
        if len(readings) == 1:
            entry["measurement"] = next(iter(readings))

    scanner = BleakScanner(
        detection_callback=callback, **build_scanner_kwargs(args.adapter, False)
    )
    print(f"Escaneando {args.duration:.0f}s en {args.adapter}...", file=sys.stderr)
    await scanner.start()
    await asyncio.sleep(args.duration)
    await scanner.stop()

    if not found:
        print("No se encontraron dispositivos BLE.", file=sys.stderr)
        return 1
    for address, entry in sorted(found.items(), key=lambda kv: -(kv[1]["rssi"] or -999)):
        line = f"{address}  rssi={entry['rssi']:>4}  {entry['name'] or '(sin nombre)'}"
        if entry["measurement"] is not None:
            m = entry["measurement"]
            line += f"   <- ThermoPro: {m.temperature_c:.1f} C, {m.humidity} %"
        print(line)
    return 0


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass


def _addresses_from(args) -> set[str] | None:
    return set(args.address) if args.address else None


async def cmd_watch(args) -> int:
    stop = asyncio.Event()
    _install_signal_handlers(stop)

    def on_reading(reading: Reading) -> None:
        print(json.dumps(reading.payload(), separators=(",", ":")), flush=True)

    listener = ThermoProListener(
        on_reading,
        addresses=_addresses_from(args),
        name_prefix=args.name_prefix,
        adapter=args.adapter,
        passive=args.passive,
        restart_after=args.restart_after,
    )
    await listener.run(stop)
    return 0


async def cmd_record(args) -> int:
    stop = asyncio.Event()
    _install_signal_handlers(stop)

    store = SqliteStore(args.db)
    store.open()
    if args.retention_days:
        removed = store.prune(args.retention_days)
        if removed:
            log.info("purgadas %d filas mas viejas de %.0f dias", removed, args.retention_days)

    latest: dict[str, Reading] = {}
    gate = PublishGate(args.interval)
    stale_warned: set[str] = set()

    def on_reading(reading: Reading) -> None:
        latest[reading.device_id] = reading

    listener = ThermoProListener(
        on_reading,
        addresses=_addresses_from(args),
        name_prefix=args.name_prefix,
        adapter=args.adapter,
        passive=args.passive,
        restart_after=args.restart_after,
    )
    scan_task = asyncio.create_task(listener.run(stop))

    try:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            for device_id, reading in list(latest.items()):
                age = (now() - reading.timestamp).total_seconds()
                # Un hueco en la tabla es el registro honesto de "no habia dato";
                # rellenarlo con el ultimo valor seria inventarselo.
                if age > args.stale_after:
                    if device_id not in stale_warned:
                        log.warning("%s sin datos desde hace %.0fs", device_id, age)
                        stale_warned.add(device_id)
                    continue
                stale_warned.discard(device_id)
                if not gate.allows(reading):
                    continue
                try:
                    store.save(reading)
                except sqlite3.Error as exc:
                    log.error("no se pudo guardar la lectura: %r", exc)
                    continue
                gate.mark(reading)
                log.info(
                    "%s  %.1f C  %d %%  rssi=%s  -> guardado",
                    reading.name or reading.address,
                    reading.temperature_c,
                    reading.humidity,
                    reading.rssi,
                )
    finally:
        scan_task.cancel()
        try:
            await scan_task
        except asyncio.CancelledError:
            pass
        store.close()
    return 0


async def cmd_mqtt(args) -> int:
    stop = asyncio.Event()
    _install_signal_handlers(stop)

    publisher = MqttPublisher(
        host=args.broker,
        port=args.port,
        username=args.username,
        password=args.password,
        topic_prefix=args.topic_prefix,
        state_topic=args.topic,
        client_id=args.client_id,
        qos=args.qos,
        retain=not args.no_retain,
        tls=args.tls,
    )
    publisher.start()

    latest: dict[str, Reading] = {}
    gate = PublishGate(args.interval)
    online: dict[str, bool] = {}

    def on_reading(reading: Reading) -> None:
        latest[reading.device_id] = reading

    listener = ThermoProListener(
        on_reading,
        addresses=_addresses_from(args),
        name_prefix=args.name_prefix,
        adapter=args.adapter,
        passive=args.passive,
        restart_after=args.restart_after,
    )
    scan_task = asyncio.create_task(listener.run(stop))

    try:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            for device_id, reading in list(latest.items()):
                age = (now() - reading.timestamp).total_seconds()
                # Una lectura vieja no se republica: marcar el sensor caido en
                # vez de dejar un valor congelado como si fuera actual.
                if age > args.stale_after:
                    if online.get(device_id, True):
                        log.warning(
                            "%s sin datos desde hace %.0fs", device_id, age
                        )
                        publisher.publish_availability(device_id, False)
                        online[device_id] = False
                    continue
                if not online.get(device_id, False):
                    publisher.publish_availability(device_id, True)
                    online[device_id] = True
                if not gate.allows(reading):
                    continue
                if args.ha_discovery:
                    publisher.publish_discovery(reading, args.ha_discovery_prefix)
                publisher.publish_reading(reading)
                gate.mark(reading)
    finally:
        scan_task.cancel()
        try:
            await scan_task
        except asyncio.CancelledError:
            pass
        for device_id in online:
            publisher.publish_availability(device_id, False)
        publisher.stop()
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _env(name: str, default=None):
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_flag(name: str) -> bool:
    return str(os.environ.get(name, "")).lower() in {"1", "true", "yes", "on"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Monitor BLE para ThermoPro TP357/TP358/TP359.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--log-level",
        default=_env("THERMOPRO_LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    parser.add_argument(
        "--adapter", default=_env("THERMOPRO_ADAPTER", "hci0"), help="adaptador BLE"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p):
        p.add_argument(
            "--address",
            action="append",
            default=[a for a in _env("THERMOPRO_ADDRESS", "").split(",") if a],
            help="MAC a vigilar (repetible). Por defecto, cualquiera que case por nombre",
        )
        p.add_argument(
            "--name-prefix",
            default=_env("THERMOPRO_NAME_PREFIX", "TP"),
            help="prefijo de nombre para autodetectar sensores",
        )
        p.add_argument(
            "--restart-after",
            type=float,
            default=_env_float("THERMOPRO_RESTART_AFTER", 90.0),
            help="segundos sin ningun anuncio tras los que se reinicia el escaneo",
        )
        p.add_argument(
            "--passive",
            action="store_true",
            default=_env_flag("THERMOPRO_PASSIVE"),
            help="escaneo pasivo (requiere bluetoothd --experimental)",
        )

    p_scan = sub.add_parser("scan", help="listar dispositivos BLE cercanos")
    p_scan.add_argument("--duration", type=float, default=15.0, help="segundos de escaneo")
    p_scan.set_defaults(func=cmd_scan)

    p_watch = sub.add_parser("watch", help="imprimir lecturas en JSON por linea")
    add_common(p_watch)
    p_watch.set_defaults(func=cmd_watch)

    p_record = sub.add_parser("record", help="guardar las lecturas en SQLite")
    add_common(p_record)
    p_record.add_argument(
        "--db",
        default=_env("THERMOPRO_DB", "thermopro.db"),
        help="ruta de la base de datos SQLite",
    )
    p_record.add_argument(
        "--interval",
        type=float,
        default=_env_float("THERMOPRO_INTERVAL", 60.0),
        help="segundos minimos entre filas de un mismo sensor",
    )
    p_record.add_argument(
        "--stale-after",
        type=float,
        default=_env_float("THERMOPRO_STALE_AFTER", 300.0),
        help="segundos sin datos tras los que se deja de escribir (hueco en la tabla)",
    )
    p_record.add_argument(
        "--retention-days",
        type=float,
        default=_env_float("THERMOPRO_RETENTION_DAYS", 0.0),
        help="borrar al arrancar lo anterior a N dias (0 = guardar todo)",
    )
    p_record.set_defaults(func=cmd_record)

    p_mqtt = sub.add_parser("mqtt", help="publicar lecturas en MQTT")
    add_common(p_mqtt)
    p_mqtt.add_argument("--broker", default=_env("MQTT_HOST"), help="host del broker MQTT")
    p_mqtt.add_argument("--port", type=int, default=_env_int("MQTT_PORT", 1883))
    p_mqtt.add_argument("--username", default=_env("MQTT_USERNAME"))
    p_mqtt.add_argument("--password", default=_env("MQTT_PASSWORD"))
    p_mqtt.add_argument("--tls", action="store_true", default=_env_flag("MQTT_TLS"))
    p_mqtt.add_argument("--qos", type=int, default=_env_int("MQTT_QOS", 0), choices=[0, 1, 2])
    p_mqtt.add_argument(
        "--client-id", default=_env("MQTT_CLIENT_ID", "thermopro-monitor")
    )
    p_mqtt.add_argument(
        "--topic-prefix",
        default=_env("MQTT_TOPIC_PREFIX", "thermopro"),
        help="prefijo de topics: <prefijo>/<id>/state",
    )
    p_mqtt.add_argument(
        "--topic",
        default=_env("MQTT_TOPIC"),
        help="topic de estado fijo (solo util con un unico sensor)",
    )
    p_mqtt.add_argument(
        "--no-retain", action="store_true", default=_env_flag("MQTT_NO_RETAIN")
    )
    p_mqtt.add_argument(
        "--interval",
        type=float,
        default=_env_float("THERMOPRO_INTERVAL", 60.0),
        help="segundos minimos entre publicaciones de un mismo sensor",
    )
    p_mqtt.add_argument(
        "--stale-after",
        type=float,
        default=_env_float("THERMOPRO_STALE_AFTER", 300.0),
        help="segundos sin datos tras los que el sensor se marca caido",
    )
    p_mqtt.add_argument(
        "--ha-discovery",
        action="store_true",
        default=_env_flag("MQTT_HA_DISCOVERY"),
        help="publicar la configuracion de Home Assistant MQTT discovery",
    )
    p_mqtt.add_argument(
        "--ha-discovery-prefix", default=_env("MQTT_HA_DISCOVERY_PREFIX", "homeassistant")
    )
    p_mqtt.set_defaults(func=cmd_mqtt)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Nivel solo para nuestro logger: en DEBUG, bleak y dbus-fast sepultan la
    # salida bajo el trafico de D-Bus.
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    log.setLevel(getattr(logging, args.log_level))
    if args.command == "mqtt" and not args.broker:
        print("error: falta --broker (o MQTT_HOST)", file=sys.stderr)
        return 2
    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
