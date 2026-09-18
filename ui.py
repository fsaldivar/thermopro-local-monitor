"""Estilo y utilidades de dibujo para el panel de la LCD.

Todo se dibuja a `SS` veces el tamano real y se reduce con LANCZOS al final:
es lo que quita el dentado de arcos y diagonales, que PIL no suaviza. Medido
en un Pi 3: unos 116 ms por fotograma a 3x, frente a los 2 s de refresco.
"""

from __future__ import annotations

import math

from PIL import Image, ImageDraw, ImageFont

W = 240
SS = 3

_INTER = "/usr/share/fonts/opentype/inter/Inter-%s.otf"
_DISPLAY = "/usr/share/fonts/opentype/inter/InterDisplay-%s.otf"
_ICONOS = "/usr/share/fonts/opentype/font-awesome/FontAwesome.otf"
_RESPALDO = "/usr/share/fonts/truetype/dejavu/DejaVuSans%s.ttf"

FONDO = (13, 15, 20)
CLARO = (238, 241, 246)
TENUE = (108, 116, 132)
APAGADO = (62, 68, 82)
PISTA = (32, 36, 46)
TARJETA = (26, 30, 40)
AZUL = (88, 160, 235)
ROJO = (232, 92, 84)

# Glifos de Font Awesome 4.7 (viene en el paquete fonts-font-awesome)
ICO = {
    "termometro": "", "gota": "", "wifi": "", "reloj": "",
    "senal": "", "grafica": "", "engranaje": "", "aviso": "",
    "chip": "", "arriba": "", "abajo": "",
}

_cache: dict[tuple, ImageFont.FreeTypeFont] = {}


def fuente(peso: str = "Medium", size: int = 14, display: bool = False):
    """Inter para texto, InterDisplay para los numeros grandes."""
    clave = (peso, size, display)
    if clave not in _cache:
        ruta = (_DISPLAY if display else _INTER) % peso
        try:
            f = ImageFont.truetype(ruta, int(size * SS))
        except OSError:
            # Sin Inter instalado, que al menos arranque.
            f = ImageFont.truetype(_RESPALDO % ("-Bold" if "Bold" in peso else ""),
                                   int(size * SS))
        _cache[clave] = f
    return _cache[clave]


def icono(size: int = 14):
    clave = ("__iconos__", size, False)
    if clave not in _cache:
        _cache[clave] = ImageFont.truetype(_ICONOS, int(size * SS))
    return _cache[clave]


def precargar() -> None:
    """Carga las fuentes de golpe.

    La primera vez cuesta cerca de un segundo; hacerlo al arrancar evita que
    el primer fotograma salga con retraso.
    """
    for size in (10, 11, 12, 13, 14, 17, 19, 22, 26, 62, 76):
        fuente("Medium", size)
        fuente("SemiBold", size)
        fuente("Bold", size, display=True)
        icono(size)


def color_temp(c: float) -> tuple[int, int, int]:
    """Escala de color continua por tramos, de frio a calor."""
    if c < 10:
        return (88, 160, 235)
    if c < 20:
        return (80, 196, 180)
    if c < 26:
        return (110, 200, 120)
    if c < 30:
        return (236, 186, 76)
    if c < 34:
        return (240, 146, 62)
    return (232, 92, 84)


def atenuar(color: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    return tuple(int(c * factor) for c in color)


class Lienzo:
    """Superficie a escala SS que se reduce al terminar."""

    def __init__(self, fondo=FONDO) -> None:
        self.img = Image.new("RGB", (W * SS, W * SS), fondo)
        self.d = ImageDraw.Draw(self.img)

    @staticmethod
    def p(v: float) -> float:
        """Convierte coordenadas logicas (240x240) a las del lienzo."""
        return v * SS

    def texto(self, xy, txt, font, fill, anchor="lm"):
        self.d.text((self.p(xy[0]), self.p(xy[1])), txt, font=font, fill=fill, anchor=anchor)

    def ancho(self, txt, font) -> float:
        return self.d.textlength(txt, font=font) / SS

    def linea(self, puntos, fill, grosor=1):
        self.d.line([(self.p(x), self.p(y)) for x, y in puntos], fill=fill,
                    width=max(1, int(self.p(grosor))))

    def rect(self, caja, fill, radio=0):
        c = [self.p(v) for v in caja]
        if radio:
            self.d.rounded_rectangle(c, radius=self.p(radio), fill=fill)
        else:
            self.d.rectangle(c, fill=fill)

    def circulo(self, cx, cy, r, fill):
        self.d.ellipse([self.p(cx - r), self.p(cy - r), self.p(cx + r), self.p(cy + r)], fill=fill)

    def arco(self, cx, cy, r, ini, fin, fill, grosor):
        self.d.arc([self.p(cx - r), self.p(cy - r), self.p(cx + r), self.p(cy + r)],
                   ini, fin, fill=fill, width=int(self.p(grosor)))

    def poligono(self, puntos, fill):
        self.d.polygon([(self.p(x), self.p(y)) for x, y in puntos], fill=fill)

    def terminar(self) -> Image.Image:
        return self.img.resize((W, W), Image.LANCZOS)


def barra_superior(l:Lienzo, izq_icono: str, izq_texto: str,
                   der_texto: str = "", der_icono: str = "", der_color=TENUE) -> None:
    l.texto((13, 15), ICO[izq_icono], icono(12), TENUE)
    l.texto((29, 15), izq_texto, fuente("SemiBold", 12), TENUE)
    if der_texto:
        ancho = l.ancho(der_texto, fuente("Medium", 12))
        l.texto((W - 13, 15), der_texto, fuente("Medium", 12), der_color, anchor="rm")
        if der_icono:
            l.texto((W - 17 - ancho, 15), ICO[der_icono], icono(11), der_color, anchor="rm")


def escala_serie(valores, x0, y0, x1, y1, minimo_span=0.5):
    """Convierte una serie en puntos de pantalla, evitando la linea plana."""
    lo, hi = min(valores), max(valores)
    if hi - lo < minimo_span:
        centro = (hi + lo) / 2
        lo, hi = centro - minimo_span / 2, centro + minimo_span / 2
    n = len(valores)
    paso = (x1 - x0) / max(1, n - 1)
    return [(x0 + i * paso, y1 - (v - lo) / (hi - lo) * (y1 - y0))
            for i, v in enumerate(valores)], lo, hi
