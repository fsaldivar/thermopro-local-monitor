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
import math
import os
import signal
import socket
import sqlite3
import time
from datetime import datetime

from PIL import Image

import ui
from st7789 import ST7789
from ui import (CLARO, TENUE, APAGADO, PISTA, TARJETA, AZUL, ROJO, ICO, W,
                Lienzo, barra_superior, color_temp, escala_serie, fuente, icono)

log = logging.getLogger("display")

# Botones del HAT
KEY1, KEY2, KEY3 = 21, 20, 16
JOY_LEFT, JOY_RIGHT = 5, 26

RANGOS = ((3, "3 H"), (12, "12 H"), (24, "24 H"), (72, "3 DIAS"))

# Escala del medidor circular. Fija a proposito: una escala que se reajusta
# sola hace que un cambio de un grado parezca enorme.
ESCALA_MIN, ESCALA_MAX = 0.0, 45.0

# Pasados estos segundos la lectura deja de considerarse actual.
VIEJA_S = 600


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


# --------------------------------------------------------------------------- #
# Vistas
# --------------------------------------------------------------------------- #


def vista_sin_datos(datos) -> Image.Image:
    l = Lienzo()
    barra_superior(l, "aviso", "SIN DATOS")
    l.texto((W / 2, 104), ICO["termometro"], icono(40), PISTA, anchor="mm")
    l.texto((W / 2, 152), "Esperando la primera", fuente("Medium", 14), TENUE, anchor="mm")
    l.texto((W / 2, 172), "lectura del sensor", fuente("Medium", 14), TENUE, anchor="mm")
    return l.terminar()


def vista_ahora(datos) -> Image.Image:
    """Medidor circular: la posicion en el arco se lee de un vistazo."""
    ultima = datos.ultima()
    if ultima is None:
        return vista_sin_datos(datos)

    l = Lienzo()
    edad = time.time() - ultima["ts"]
    vieja = edad > VIEJA_S
    col = APAGADO if vieja else color_temp(ultima["temperatura"])

    barra_superior(l, "termometro", (ultima["nombre"] or "SENSOR").upper(),
                   str(ultima["rssi"]) if ultima["rssi"] is not None else "",
                   "senal", ROJO if vieja else TENUE)

    # El centro va desplazado hacia abajo y el radio reducido: a media escala
    # el marcador queda en lo alto del arco y se comia la cabecera.
    cx, cy = W / 2, W / 2 + 6
    r, grosor = 86, 13
    ini, barrido = 135, 270
    l.arco(cx, cy, r, ini, ini + barrido, PISTA, grosor)

    frac = (ultima["temperatura"] - ESCALA_MIN) / (ESCALA_MAX - ESCALA_MIN)
    frac = max(0.0, min(1.0, frac))
    if frac > 0:
        l.arco(cx, cy, r, ini, ini + barrido * frac, col, grosor)
        ang = math.radians(ini + barrido * frac)
        mx, my = cx + r * math.cos(ang), cy + r * math.sin(ang)
        l.circulo(mx, my, 9, CLARO)
        l.circulo(mx, my, 4.5, col)

    valor = f"{ultima['temperatura']:.1f}"
    f_num = fuente("Bold", 62, display=True)
    l.texto((cx, cy - 12), valor, f_num, CLARO, anchor="mm")
    l.texto((cx + l.ancho(valor, f_num) / 2 + 9, cy - 26), "\u00b0C",
            fuente("SemiBold", 19), TENUE, anchor="lm")

    f_hum = fuente("Medium", 22)
    texto_h = f"{ultima['humedad']}%"
    total = 22 + l.ancho(texto_h, f_hum)
    x0 = cx - total / 2
    l.texto((x0, cy + 30), ICO["gota"], icono(15), APAGADO if vieja else AZUL)
    l.texto((x0 + 22, cy + 30), texto_h, f_hum, TENUE)

    # Los extremos de la escala van al pie, no junto al arco: el angulo
    # inicial de 135 grados cae justo donde estarian y los tapaba.
    f_pie = fuente("Medium", 11)
    l.texto((16, W - 13), f"{ESCALA_MIN:.0f}", f_pie, APAGADO)
    l.texto((W - 16, W - 13), f"{ESCALA_MAX:.0f}", f_pie, APAGADO, anchor="rm")

    # La antiguedad en rojo cuando el dato ya no es de fiar: un numero
    # congelado no debe parecer actual.
    l.texto((cx, W - 13), human_age(edad), fuente("Medium", 12),
            ROJO if vieja else APAGADO, anchor="mm")
    return l.terminar()


def vista_historico(datos, horas: float, etiqueta: str) -> Image.Image:
    """Tarjeta con la serie: franja de acento, grafica rellena y extremos."""
    l = Lienzo()
    serie_bruta = datos.serie(horas)
    if len(serie_bruta) < 2:
        barra_superior(l, "grafica", f"HISTORICO {etiqueta}")
        l.texto((W / 2, W / 2), "aun no hay suficiente",
                fuente("Medium", 15), TENUE, anchor="mm")
        return l.terminar()

    temps = [t for _, t in serie_bruta]
    col = color_temp(temps[-1])
    l.rect([0, 0, W, 4], col)

    barra_superior(l, "grafica", f"HISTORICO {etiqueta}", f"{len(temps)} pts")

    valor = f"{temps[-1]:.1f}"
    f_num = fuente("Bold", 66, display=True)
    l.texto((14, 72), valor, f_num, CLARO)
    l.texto((14 + l.ancho(valor, f_num) + 6, 54), "\u00b0C",
            fuente("SemiBold", 20), col)

    f_hum = fuente("SemiBold", 16)
    ultima = datos.ultima()
    if ultima is not None:
        texto = f"{ultima['humedad']}%"
        ancho_p = 30 + l.ancho(texto, f_hum) + 12
        l.rect([14, 104, 14 + ancho_p, 132], TARJETA, radio=14)
        l.texto((27, 118), ICO["gota"], icono(12), AZUL, anchor="mm")
        l.texto((40, 118), texto, f_hum, CLARO)

    # Muestrear a lo ancho del panel: mas puntos que pixeles no aportan nada.
    objetivo = W - 28
    paso = max(1, len(temps) // objetivo)
    muestra = temps[::paso]
    x0, y0, x1, y1 = 14, 150, W - 14, 206
    puntos, lo, hi = escala_serie(muestra, x0, y0, x1, y1)

    l.poligono(puntos + [(x1, y1), (x0, y1)], ui.atenuar(col, 0.16))
    l.linea(puntos, col, 2)
    l.circulo(puntos[-1][0], puntos[-1][1], 3.5, CLARO)

    f_pie = fuente("Medium", 12)
    l.texto((14, 224), f"min {min(temps):.1f}", f_pie, TENUE)
    l.texto((W / 2, 224), f"med {sum(temps) / len(temps):.1f}", f_pie, APAGADO, anchor="mm")
    l.texto((W - 14, 224), f"max {max(temps):.1f}", f_pie, TENUE, anchor="rm")
    return l.terminar()


def vista_sistema(datos) -> Image.Image:
    l = Lienzo()
    barra_superior(l, "engranaje", "SISTEMA")

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
    arriba = float(open("/proc/uptime").read().split()[0])

    filas = [
        ("wifi", "Red", ip_local()),
        ("chip", "CPU", f"{cpu:.1f} \u00b0C"),
        ("reloj", "Encendida", f"{int(arriba // 3600)} h {int(arriba % 3600 // 60)} min"),
        ("grafica", "Lecturas", f"{datos.filas():,}".replace(",", " ")),
    ]
    y = 56
    for glifo, etiqueta, valor in filas:
        l.rect([12, y - 17, W - 12, y + 17], TARJETA, radio=8)
        l.texto((26, y), ICO[glifo], icono(13), TENUE, anchor="mm")
        l.texto((44, y), etiqueta, fuente("Medium", 13), TENUE)
        l.texto((W - 22, y), valor, fuente("SemiBold", 15), CLARO, anchor="rm")
        y += 42

    l.texto((W / 2, W - 13), datetime.now().strftime("%H:%M:%S"),
            fuente("Medium", 12), APAGADO, anchor="mm")
    return l.terminar()


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

    ui.precargar()
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
