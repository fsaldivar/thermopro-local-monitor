"""Tests del decodificador, con tramas reales capturadas de un TP359S."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from thermopro_monitor import (  # noqa: E402
    Measurement,
    decode_advertisement_frame,
    decode_gatt_frame,
    measurements_in_advertisement,
)


class TestAdvertisementFrames(unittest.TestCase):
    def test_captured_frames(self):
        # c2 39 01 2f 22 13 01  y  c2 3a 01 2f 22 13 01
        self.assertEqual(
            decode_advertisement_frame(0x39C2, bytes.fromhex("012f221301")),
            Measurement(31.3, 47),
        )
        self.assertEqual(
            decode_advertisement_frame(0x3AC2, bytes.fromhex("012f221301")),
            Measurement(31.4, 47),
        )

    def test_humidity_comes_from_payload(self):
        self.assertEqual(
            decode_advertisement_frame(0x39C2, bytes.fromhex("012e221301")),
            Measurement(31.3, 46),
        )

    def test_negative_temperature(self):
        # -5.0 C = -50 decimas = 0xffce little endian -> c2 ce ff ...
        self.assertEqual(
            decode_advertisement_frame(0xCEC2, bytes.fromhex("ff30221301")),
            Measurement(-5.0, 48),
        )

    def test_rejects_wrong_prefix(self):
        self.assertIsNone(decode_advertisement_frame(0x004C, bytes.fromhex("012f221301")))

    def test_rejects_wrong_length(self):
        self.assertIsNone(decode_advertisement_frame(0x39C2, bytes.fromhex("012f2213")))
        self.assertIsNone(decode_advertisement_frame(0x39C2, bytes.fromhex("012f22130102")))

    def test_rejects_out_of_range(self):
        # 0x2ec2 -> temperatura 0x012e/10 = 30.2 C pero humedad 200 %
        self.assertIsNone(decode_advertisement_frame(0x2EC2, bytes.fromhex("01c8221301")))
        # temperatura 999.9 C
        self.assertIsNone(decode_advertisement_frame(0x0FC2, bytes.fromhex("272f221301")))


class TestGattFrames(unittest.TestCase):
    def test_captured_frames(self):
        # c2 00 00 39 01 2f 2c
        self.assertEqual(
            decode_gatt_frame(bytes.fromhex("c2000039012f2c")),
            Measurement(31.3, 47),
        )
        self.assertEqual(
            decode_gatt_frame(bytes.fromhex("c200003a012f2c")),
            Measurement(31.4, 47),
        )

    def test_advertisement_and_gatt_agree(self):
        self.assertEqual(
            decode_advertisement_frame(0x39C2, bytes.fromhex("012f221301")),
            decode_gatt_frame(bytes.fromhex("c2000039012f2c")),
        )

    def test_rejects_garbage(self):
        self.assertIsNone(decode_gatt_frame(b""))
        self.assertIsNone(decode_gatt_frame(bytes.fromhex("00000039012f2c")))


class TestAmbiguousAdvertisements(unittest.TestCase):
    def test_single_entry(self):
        found = measurements_in_advertisement({0x39C2: bytes.fromhex("012f221301")})
        self.assertEqual(found, {Measurement(31.3, 47)})

    def test_stale_company_id_is_detected(self):
        # Lo que entrega BlueZ cuando conserva un id viejo: dos temperaturas.
        found = measurements_in_advertisement(
            {
                0x39C2: bytes.fromhex("012e221301"),
                0x3AC2: bytes.fromhex("012e221301"),
            }
        )
        self.assertEqual(len(found), 2)

    def test_duplicate_ids_with_same_value_are_not_ambiguous(self):
        # Dos ids que decodifican al mismo valor no son una ambiguedad real.
        found = measurements_in_advertisement(
            {
                0x39C2: bytes.fromhex("012f221301"),
                0x004C: bytes.fromhex("012f221301"),  # no es ThermoPro, se ignora
            }
        )
        self.assertEqual(found, {Measurement(31.3, 47)})

    def test_ignores_foreign_devices(self):
        found = measurements_in_advertisement(
            {0x0006: bytes.fromhex("010920229334eec313d5ef42f0cfd086499f4da7685df917f76784")}
        )
        self.assertEqual(found, set())

    def test_empty(self):
        self.assertEqual(measurements_in_advertisement({}), set())
        self.assertEqual(measurements_in_advertisement(None), set())


if __name__ == "__main__":
    unittest.main()
