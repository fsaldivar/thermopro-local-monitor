#!/usr/bin/env python3
"""Genera las capturas del panel para el README.

Dibuja las vistas reales (las mismas funciones que corren en la Raspberry) y
las monta sobre un marco que sugiere el HAT. Ejecutar desde la raiz del
proyecto, en la propia Raspberry o en cualquier maquina con las fuentes:

    python3 docs/generar_capturas.py
"""

import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw

import display_app as app
import ui

ESCALA = 2                      # el panel real es de 240x240
MARGEN = 26
SEPARACION = 34
FONDO = (22, 24, 30)


def datos_de_ejemplo(ruta: str) -> str:
    """Base con una serie inventada, para que las capturas salgan iguales
    siempre y no dependan de que haya sensor a mano."""
    import math
    import random

    random.seed(11)
    db = sqlite3.connect(ruta)
    for sql in app.__dict__.get("_ESQUEMA", []):
        db.execute(sql)
    db.executescript("""
        CREATE TABLE devices (device_id TEXT PRIMARY KEY, address TEXT, name TEXT,
                              first_seen INTEGER, last_seen INTEGER);
        CREATE TABLE readings (ts INTEGER, device_id TEXT, temperature_c REAL,
                               humidity INTEGER, rssi INTEGER, battery INTEGER,
                               PRIMARY KEY (device_id, ts));
    """)
    ahora = int(time.time())
    db.execute("INSERT INTO devices VALUES (?,?,?,?,?)",
               ("fbe7c4cf3af6", "FB:E7:C4:CF:3A:F6", "TP359S (3AF6)", ahora - 86400, ahora))
    for i in range(240):
        ts = ahora - (240 - i) * 360
        t = 22.5 + math.sin(i / 26) * 3.4 + random.uniform(-0.25, 0.25)
        h = int(48 + math.cos(i / 31) * 7 + random.uniform(-1, 1))
        db.execute("INSERT INTO readings VALUES (?,?,?,?,?,?)",
                   (ts, "fbe7c4cf3af6", round(t, 1), h,
                    -62 - int(random.uniform(0, 12)), 100))
    db.commit()
    db.close()
    return ruta


def con_marco(vista: Image.Image) -> Image.Image:
    """Marco redondeado alrededor de la pantalla, como el del HAT."""
    lado = 240 * ESCALA
    pantalla = vista.resize((lado, lado), Image.LANCZOS)
    borde = 10
    marco = Image.new("RGB", (lado + borde * 2, lado + borde * 2), FONDO)
    d = ImageDraw.Draw(marco)
    d.rounded_rectangle([0, 0, marco.width - 1, marco.height - 1], radius=16, fill=(38, 41, 50))
    d.rounded_rectangle([borde - 2, borde - 2, marco.width - borde + 1, marco.height - borde + 1],
                        radius=8, fill=(0, 0, 0))
    marco.paste(pantalla, (borde, borde))
    return marco


def main() -> int:
    ui.precargar()
    salida = os.path.dirname(os.path.abspath(__file__))
    with tempfile.TemporaryDirectory() as tmp:
        datos = app.Datos(datos_de_ejemplo(os.path.join(tmp, "muestra.db")))
        vistas = [
            ("ahora", app.vista_ahora(datos)),
            ("historico", app.vista_historico(datos, 24, "24 H")),
            ("sistema", app.vista_sistema(datos)),
        ]

    marcos = [con_marco(v) for _, v in vistas]
    ancho = MARGEN * 2 + sum(m.width for m in marcos) + SEPARACION * (len(marcos) - 1)
    alto = MARGEN * 2 + marcos[0].height
    hoja = Image.new("RGB", (ancho, alto), FONDO)
    x = MARGEN
    for m in marcos:
        hoja.paste(m, (x, MARGEN))
        x += m.width + SEPARACION
    hoja.save(os.path.join(salida, "vistas.png"))

    for (nombre, _), marco in zip(vistas, marcos):
        marco.save(os.path.join(salida, f"vista-{nombre}.png"))

    print(f"generadas en {salida}: vistas.png y tres capturas sueltas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
