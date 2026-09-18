#!/usr/bin/env python3
"""Muestra las lecturas del sensor en la LCD 1.3" del HAT.

Lee de la misma base SQLite que escribe `thermopro_monitor.py record`, en modo
solo lectura: si este proceso se cae, el registro sigue intacto.

Vistas (KEY1 o joystick izquierda/derecha para cambiar):
    1. Ahora      temperatura grande, humedad y antiguedad del dato
    2. Historico  grafica de las ultimas horas con minima y maxima
    3. Sistema    IP, temperatura de CPU, uptime y estado del registro

KEY2 apaga y enciende la retroiluminacion. KEY3 cambia el rango del historico.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sqlite3
import time
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

from st7789 import ST7789

log = logging.getLogger("display")

W = H = 240
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

BG = (16, 18, 24)
FG = (236, 238, 242)
MUTED = (122, 130, 146)
ACCENT = (86, 156, 214)
WARN = (232, 168, 72)
BAD = (226, 90, 80)
GOOD = (118, 190, 128)

# Botones del HAT
KEY1, KEY2, KEY3 = 21, 20, 16
JOY_LEFT, JOY_RIGHT = 5, 26

RANGOS = ((3, "3 h"), (12, "12 h"), (24, "24 h"), (72, "3 dias"))

_fonts: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (FONT_BOLD if bold else FONT_PATH, size)
    if key not in _fonts:
        _fonts[key] = ImageFont.truetype(key[0], key[1])
    return _fonts[key]


def temp_color(celsius: float) -> tuple[int, int, int]:
    if celsius < 10:
        return ACCENT
    if celsius < 26:
        return GOOD
    if celsius < 32:
        return WARN
    return BAD


def human_age(seconds: float) -> str:
    if seconds < 90:
        return f"hace {int(seconds)} s"
    if seconds < 5400:
        return f"hace {int(seconds // 60)} min"
    if seconds < 172800:
        return f"hace {int(seconds // 3600)} h"
    return f"hace {int(seconds // 86400)} dias"


class Datos:
    """Acceso de solo lectura al historico."""

    def __init__(self, path: str) -> None:
        self._path = os.path.expanduser(path)
        self._db: sqlite3.Connection | None = None

    def connect(self) -> bool:
        if self._db is not None:
            return True
        if not os.path.exists(self._path):
            return False
        try:
            self._db = sqlite3.connect(
                f"file:{self._path}?mode=ro", uri=True, timeout=5.0
            )
        except sqlite3.Error as exc:
            log.warning("no se pudo abrir la base: %r", exc)
            return False
        return True

    def ultima(self) -> dict | None:
        if not self.connect():
            return None
        try:
            row = self._db.execute(
                """
                SELECT r.ts, r.temperature_c, r.humidity, r.rssi, d.name
                FROM readings r LEFT JOIN devices d USING (device_id)
                ORDER BY r.ts DESC LIMIT 1
                """
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        return {
            "ts": row[0],
            "temperatura": row[1],
            "humedad": row[2],
            "rssi": row[3],
            "nombre": row[4],
        }

    def serie(self, horas: float) -> list[tuple[int, float]]:
        if not self.connect():
            return []
        desde = int(time.time() - horas * 3600)
        try:
            return self._db.execute(
                "SELECT ts, temperature_c FROM readings WHERE ts >= ? ORDER BY ts",
                (desde,),
            ).fetchall()
        except sqlite3.Error:
            return []

    def filas(self) -> int:
        if not self.connect():
            return 0
        try:
            return self._db.execute("SELECT count(*) FROM readings").fetchone()[0]
        except sqlite3.Error:
            return 0


# --------------------------------------------------------------------------- #
# Vistas
# --------------------------------------------------------------------------- #


def marco(titulo: str) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((12, 8), titulo.upper(), font=font(15, bold=True), fill=MUTED)
    d.line([(12, 30), (W - 12, 30)], fill=(40, 44, 54), width=1)
    return img, d


def vista_sin_datos(datos: Datos) -> Image.Image:
    img, d = marco("sin datos")
    d.text((12, 90), "Esperando la", font=font(26), fill=FG)
    d.text((12, 122), "primera lectura", font=font(26), fill=FG)
    d.text((12, 200), "el grabador no ha escrito aun", font=font(13), fill=MUTED)
    return img


def vista_ahora(datos: Datos) -> Image.Image:
    ultima = datos.ultima()
    if ultima is None:
        return vista_sin_datos(datos)
    img, d = marco(ultima["nombre"] or "sensor")
    edad = time.time() - ultima["ts"]
    color = temp_color(ultima["temperatura"]) if edad < 600 else MUTED

    texto = f"{ultima['temperatura']:.1f}"
    d.text((10, 44), texto, font=font(84, bold=True), fill=color)
    ancho = d.textlength(texto, font=font(84, bold=True))
    d.text((10 + ancho + 6, 56), "C", font=font(34, bold=True), fill=color)

    d.text((12, 146), f"{ultima['humedad']} %", font=font(40), fill=FG)
    d.text((12, 192), "humedad relativa", font=font(13), fill=MUTED)

    # La antiguedad en rojo cuando el dato ya no es de fiar: un numero
    # congelado no debe parecer actual.
    color_edad = MUTED if edad < 600 else BAD
    d.text((W - 12, 192), human_age(edad), font=font(14), fill=color_edad, anchor="ra")
    if ultima["rssi"] is not None:
        d.text((W - 12, 8), f"{ultima['rssi']} dBm", font=font(13), fill=MUTED, anchor="ra")
    return img


def vista_historico(datos: Datos, horas: float, etiqueta: str) -> Image.Image:
    img, d = marco(f"historico {etiqueta}")
    serie = datos.serie(horas)
    if len(serie) < 2:
        d.text((12, 110), "aun no hay suficiente", font=font(20), fill=MUTED)
        return img

    temps = [t for _, t in serie]
    lo, hi = min(temps), max(temps)
    if hi - lo < 0.5:                       # evita una linea plana sin escala
        centro = (hi + lo) / 2
        lo, hi = centro - 0.25, centro + 0.25

    x0, y0, x1, y1 = 12, 52, W - 12, 176
    t_ini, t_fin = serie[0][0], serie[-1][0]
    span = max(t_fin - t_ini, 1)

    puntos = [
        (
            x0 + (ts - t_ini) / span * (x1 - x0),
            y1 - (temp - lo) / (hi - lo) * (y1 - y0),
        )
        for ts, temp in serie
    ]
    d.line([(x0, y1), (x1, y1)], fill=(40, 44, 54))
    d.line(puntos, fill=ACCENT, width=2)

    d.text((12, 36), f"{hi:.1f}", font=font(13), fill=MUTED)
    d.text((12, y1 + 4), f"{lo:.1f}", font=font(13), fill=MUTED)
    d.text((12, 196), f"min {min(temps):.1f}   max {max(temps):.1f}", font=font(18), fill=FG)
    d.text((W - 12, 196), f"{len(serie)} pts", font=font(13), fill=MUTED, anchor="ra")
    return img


def vista_sistema(datos: Datos) -> Image.Image:
    img, d = marco("sistema")

    def ip_local() -> str:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 1))          # no envia nada, solo elige ruta
            return s.getsockname()[0]
        except OSError:
            return "sin red"
        finally:
            s.close()

    try:
        cpu = int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000
    except OSError:
        cpu = float("nan")
    uptime = float(open("/proc/uptime").read().split()[0])
    horas, minutos = int(uptime // 3600), int(uptime % 3600 // 60)

    filas = [
        ("IP", ip_local()),
        ("CPU", f"{cpu:.1f} C"),
        ("Encendida", f"{horas} h {minutos} min"),
        ("Lecturas", f"{datos.filas():,}".replace(",", " ")),
        ("Hora", datetime.now().strftime("%H:%M:%S")),
    ]
    y = 48
    for etiqueta, valor in filas:
        d.text((12, y), etiqueta, font=font(15), fill=MUTED)
        d.text((W - 12, y), valor, font=font(17), fill=FG, anchor="ra")
        y += 34
    return img


# --------------------------------------------------------------------------- #
# Aplicacion
# --------------------------------------------------------------------------- #


class App:
    def __init__(self, lcd: ST7789, datos: Datos) -> None:
        self.lcd = lcd
        self.datos = datos
        self.vista = 0
        self.rango = 2                       # indice en RANGOS: 24 h
        self.luz = True
        self.parar = False
        self._sucio = True

    def siguiente_vista(self, paso: int = 1) -> None:
        self.vista = (self.vista + paso) % 3
        self._sucio = True

    def alternar_luz(self) -> None:
        self.luz = not self.luz
        self.lcd.backlight(self.luz)

    def siguiente_rango(self) -> None:
        self.rango = (self.rango + 1) % len(RANGOS)
        self._sucio = True

    def conectar_botones(self) -> None:
        try:
            from gpiozero import Button
        except ImportError:
            log.warning("gpiozero no disponible: sin botones")
            return
        self._botones = []
        for pin, accion in (
            (KEY1, lambda: self.siguiente_vista(1)),
            (KEY2, self.alternar_luz),
            (KEY3, self.siguiente_rango),
            (JOY_LEFT, lambda: self.siguiente_vista(-1)),
            (JOY_RIGHT, lambda: self.siguiente_vista(1)),
        ):
            try:
                boton = Button(pin, pull_up=True, bounce_time=0.12)
                boton.when_pressed = accion
                self._botones.append(boton)
            except Exception as exc:
                log.warning("no se pudo usar GPIO%d: %r", pin, exc)

    def render(self) -> Image.Image:
        if self.vista == 0:
            return vista_ahora(self.datos)
        if self.vista == 1:
            horas, etiqueta = RANGOS[self.rango]
            return vista_historico(self.datos, horas, etiqueta)
        return vista_sistema(self.datos)

    def run(self, refresco: float) -> None:
        while not self.parar:
            inicio = time.monotonic()
            try:
                if self.luz:
                    self.lcd.show(self.render())
            except Exception:
                log.exception("fallo dibujando la vista %d", self.vista)
            espera = max(0.05, refresco - (time.monotonic() - inicio))
            time.sleep(espera)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Panel local en la LCD del HAT.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--db", default=os.environ.get("THERMOPRO_DB", "thermopro.db"))
    parser.add_argument("--refresh", type=float, default=2.0, help="segundos entre redibujados")
    parser.add_argument(
        "--rotation",
        type=int,
        default=int(os.environ.get("THERMOPRO_ROTATION", 270)),
        choices=[0, 90, 180, 270],
        help="270 deja la imagen derecha con los botones a la izquierda",
    )
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    lcd = ST7789(rotation=args.rotation).open()
    app = App(lcd, Datos(args.db))
    app.conectar_botones()

    def parar(*_):
        app.parar = True

    signal.signal(signal.SIGINT, parar)
    signal.signal(signal.SIGTERM, parar)

    log.info("panel arrancado (rotacion %d)", args.rotation)
    try:
        app.run(args.refresh)
    finally:
        try:
            lcd.fill((0, 0, 0))
            lcd.backlight(False)
        finally:
            lcd.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
