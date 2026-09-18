"""Driver del ST7789 240x240 (Waveshare 1.3inch LCD HAT) sobre SPI.

Pinout del HAT:  DIN=GPIO10 (MOSI)  CLK=GPIO11  CS=CE0  DC=GPIO25
                 RST=GPIO27  BL=GPIO24
Botones:         KEY1=21  KEY2=20  KEY3=16
Joystick:        arriba=6  abajo=19  izquierda=5  derecha=26  pulsar=13
"""

from __future__ import annotations

import time

# Comandos del ST7789 que usamos
SWRESET = 0x01
SLPOUT = 0x11
NORON = 0x13
INVON = 0x21
DISPON = 0x29
CASET = 0x2A
RASET = 0x2B
RAMWR = 0x2C
MADCTL = 0x36
COLMOD = 0x3A

# Orientaciones: MADCTL + desplazamiento (x, y).
#
# La RAM del ST7789 es de 240x320 pero el panel solo ensena 240x240. Las
# rotaciones que invierten un eje dejan los 80 pixeles sobrantes DENTRO del
# area visible, asi que hay que compensarlos o la imagen sale corrida.
ROTATIONS = {
    0: (0x00, 0, 0),
    90: (0x60, 0, 0),
    180: (0xC0, 0, 80),
    270: (0xA0, 80, 0),
}


class ST7789:
    def __init__(
        self,
        port: int = 0,
        cs: int = 0,
        dc_pin: int = 25,
        rst_pin: int = 27,
        bl_pin: int = 24,
        width: int = 240,
        height: int = 240,
        speed_hz: int = 32_000_000,
        rotation: int = 0,
    ) -> None:
        if rotation not in ROTATIONS:
            raise ValueError(f"rotacion no valida: {rotation}")
        self.width = width
        self.height = height
        self._port = port
        self._cs = cs
        self._dc_pin = dc_pin
        self._rst_pin = rst_pin
        self._bl_pin = bl_pin
        self._speed_hz = speed_hz
        self._rotation = rotation
        self._madctl, self._x_offset, self._y_offset = ROTATIONS[rotation]
        self._spi = None
        self._dc = self._rst = self._bl = None

    # -- ciclo de vida ----------------------------------------------------- #

    def open(self) -> "ST7789":
        import spidev
        from gpiozero import OutputDevice

        self._dc = OutputDevice(self._dc_pin)
        self._rst = OutputDevice(self._rst_pin)
        self._bl = OutputDevice(self._bl_pin)
        self._spi = spidev.SpiDev()
        self._spi.open(self._port, self._cs)
        self._spi.max_speed_hz = self._speed_hz
        self._spi.mode = 0
        self._init_panel()
        self.backlight(True)
        return self

    def close(self) -> None:
        for resource in (self._spi, self._dc, self._rst, self._bl):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
        self._spi = self._dc = self._rst = self._bl = None

    def __enter__(self) -> "ST7789":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- nivel SPI --------------------------------------------------------- #

    def _cmd(self, command: int, *data: int) -> None:
        self._dc.off()
        self._spi.writebytes([command])
        if data:
            self._dc.on()
            self._spi.writebytes(list(data))

    def _data(self, payload: bytes) -> None:
        self._dc.on()
        # spidev trocea solo, pero por debajo hay un limite de buffer.
        chunk = 4096
        for start in range(0, len(payload), chunk):
            self._spi.writebytes2(payload[start : start + chunk])

    def _reset(self) -> None:
        self._rst.on()
        time.sleep(0.05)
        self._rst.off()
        time.sleep(0.05)
        self._rst.on()
        time.sleep(0.15)

    def _init_panel(self) -> None:
        self._reset()
        self._cmd(SWRESET)
        time.sleep(0.15)
        self._cmd(SLPOUT)
        time.sleep(0.12)
        self._cmd(COLMOD, 0x05)                      # 16 bits por pixel (RGB565)
        self._cmd(MADCTL, self._madctl)
        self._cmd(0xB2, 0x0C, 0x0C, 0x00, 0x33, 0x33)  # PORCTRL
        self._cmd(0xB7, 0x35)                          # GCTRL
        self._cmd(0xBB, 0x19)                          # VCOMS
        self._cmd(0xC0, 0x2C)                          # LCMCTRL
        self._cmd(0xC2, 0x01)                          # VDVVRHEN
        self._cmd(0xC3, 0x12)                          # VRHS
        self._cmd(0xC4, 0x20)                          # VDVS
        self._cmd(0xC6, 0x0F)                          # FRCTRL2: 60 Hz
        self._cmd(0xD0, 0xA4, 0xA1)                    # PWCTRL1
        self._cmd(0xE0, 0xD0, 0x04, 0x0D, 0x11, 0x13, 0x2B, 0x3F,
                        0x54, 0x4C, 0x18, 0x0D, 0x0B, 0x1F, 0x23)
        self._cmd(0xE1, 0xD0, 0x04, 0x0C, 0x11, 0x13, 0x2C, 0x3F,
                        0x44, 0x51, 0x2F, 0x1F, 0x1F, 0x20, 0x23)
        # El ST7789 de estos modulos necesita la inversion activada; sin esto
        # los colores salen en negativo.
        self._cmd(INVON)
        self._cmd(NORON)
        time.sleep(0.01)
        self._cmd(DISPON)

    # -- dibujo ------------------------------------------------------------ #

    def backlight(self, on: bool) -> None:
        if self._bl is not None:
            self._bl.on() if on else self._bl.off()

    def _window(self, x0: int, y0: int, x1: int, y1: int) -> None:
        x0 += self._x_offset
        x1 += self._x_offset
        y0 += self._y_offset
        y1 += self._y_offset
        self._cmd(CASET, x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF)
        self._cmd(RASET, y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF)
        self._cmd(RAMWR)

    def fill(self, color: tuple[int, int, int]) -> None:
        self._window(0, 0, self.width - 1, self.height - 1)
        pixel = rgb565_bytes(color)
        self._data(pixel * (self.width * self.height))

    def show(self, image) -> None:
        """Vuelca una imagen PIL RGB del tamano del panel."""
        if image.size != (self.width, self.height):
            image = image.resize((self.width, self.height))
        if image.mode != "RGB":
            image = image.convert("RGB")
        self._window(0, 0, self.width - 1, self.height - 1)
        self._data(to_rgb565(image))


def rgb565_bytes(color: tuple[int, int, int]) -> bytes:
    r, g, b = color
    value = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
    return bytes([value >> 8, value & 0xFF])


def to_rgb565(image) -> bytes:
    """Convierte una imagen PIL RGB a RGB565 big-endian."""
    try:
        import numpy as np
    except ImportError:
        return _to_rgb565_slow(image)
    arr = np.asarray(image, dtype=np.uint16)
    packed = (
        ((arr[:, :, 0] & 0xF8) << 8)
        | ((arr[:, :, 1] & 0xFC) << 3)
        | (arr[:, :, 2] >> 3)
    )
    return packed.astype(">u2").tobytes()


def _to_rgb565_slow(image) -> bytes:
    out = bytearray()
    for r, g, b in image.getdata():
        out += rgb565_bytes((r, g, b))
    return bytes(out)
