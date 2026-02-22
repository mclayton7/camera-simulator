"""
CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflect, no XOR out)
Used for MISB ST 0601 Tag 1 (Checksum).

The checksum covers all bytes of the KLV packet *except* the last 4 bytes
(the checksum tag itself: key byte 0x01, length byte 0x02, value 2 bytes).
Per the standard the checksum is calculated over the UAS LS Universal Key
and all tag-length-value bytes up to but not including Tag 1.
"""


def _make_crc16_table() -> list[int]:
    poly = 0x1021
    table = []
    for byte in range(256):
        crc = byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ poly
            else:
                crc <<= 1
            crc &= 0xFFFF
        table.append(crc)
    return table


_CRC16_TABLE = _make_crc16_table()


def crc16_ccitt(data: bytes | bytearray, initial: int = 0xFFFF) -> int:
    """Compute CRC-16/CCITT-FALSE over *data*, starting from *initial*."""
    crc = initial & 0xFFFF
    for byte in data:
        crc = ((crc << 8) ^ _CRC16_TABLE[((crc >> 8) ^ byte) & 0xFF]) & 0xFFFF
    return crc
