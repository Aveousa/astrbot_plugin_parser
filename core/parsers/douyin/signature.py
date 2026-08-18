"""Minimal Douyin ``a_bogus`` signer.

Algorithm adapted from F2 (https://github.com/Johnserf-Seed/f2),
licensed under the Apache License 2.0.
"""

import hashlib
import random
import string
import time

_ALPHABETS = (
    "Dkdpgh2ZmsQB80/MfvV36XI1R45-WUAlEixNLwoqYTOPuzKFjJnry79HbGcaStCe",
    "ckdp1h4ZKsUB80/Mfvw36XIgR25+WQAlEi7NLboqYTOPuzmFjJnryx9HVGDaStCe",
)

_PERMUTATION = [
    121, 243, 55, 234, 103, 36, 47, 228, 30, 231, 106, 6, 115, 95, 78,
    101, 250, 207, 198, 50, 139, 227, 220, 105, 97, 143, 34, 28, 194, 215,
    18, 100, 159, 160, 43, 8, 169, 217, 180, 120, 247, 45, 90, 11, 27,
    197, 46, 3, 84, 72, 5, 68, 62, 56, 221, 75, 144, 79, 73, 161, 178,
    81, 64, 187, 134, 117, 186, 118, 16, 241, 130, 71, 89, 147, 122, 129,
    65, 40, 88, 150, 110, 219, 199, 255, 181, 254, 48, 4, 195, 248, 208,
    32, 116, 167, 69, 201, 17, 124, 125, 104, 96, 83, 80, 127, 236, 108,
    154, 126, 204, 15, 20, 135, 112, 158, 13, 1, 188, 164, 210, 237, 222,
    98, 212, 77, 253, 42, 170, 202, 26, 22, 29, 182, 251, 10, 173, 152,
    58, 138, 54, 141, 185, 33, 157, 31, 252, 132, 233, 235, 102, 196, 191,
    223, 240, 148, 39, 123, 92, 82, 128, 109, 57, 24, 38, 113, 209, 245,
    2, 119, 153, 229, 189, 214, 230, 174, 232, 63, 52, 205, 86, 140, 66,
    175, 111, 171, 246, 133, 238, 193, 99, 60, 74, 91, 225, 51, 76, 37,
    145, 211, 166, 151, 213, 206, 0, 200, 244, 176, 218, 44, 184, 172, 49,
    216, 93, 168, 53, 21, 183, 41, 67, 85, 224, 155, 226, 242, 87, 177,
    146, 70, 190, 12, 162, 19, 137, 114, 25, 165, 163, 192, 23, 59, 9,
    94, 179, 107, 35, 7, 142, 131, 239, 203, 149, 136, 61, 249, 14, 156,
]

_SORT_INDEX = [
    18, 20, 52, 26, 30, 34, 58, 38, 40, 53, 42, 21, 27, 54, 55, 31, 35,
    57, 39, 41, 43, 22, 28, 32, 60, 36, 23, 29, 33, 37, 44, 45, 59, 46,
    47, 48, 49, 50, 24, 25, 65, 66, 70, 71,
]

_XOR_INDEX = [
    18, 20, 26, 30, 34, 38, 40, 42, 21, 27, 31, 35, 39, 41, 43, 22, 28,
    32, 36, 23, 29, 33, 37, 44, 45, 46, 47, 48, 49, 50, 24, 25, 52, 53,
    54, 55, 57, 58, 59, 60, 65, 66, 70, 71,
]


def _sm3(value: str | list[int]) -> list[int]:
    data = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    try:
        return list(hashlib.new("sm3", data).digest())
    except ValueError as e:
        raise RuntimeError("OpenSSL does not provide the SM3 hash algorithm") from e


def _hash_parameter(value: str | list[int], *, salt: bool = True) -> list[int]:
    if isinstance(value, str) and salt:
        value += "cus"
    return _sm3(value)


def _rc4(key: bytes, plaintext: str) -> bytes:
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) % 256
        state[i], state[j] = state[j], state[i]
    i = j = 0
    output = []
    for char in plaintext:
        i = (i + 1) % 256
        j = (j + state[i]) % 256
        state[i], state[j] = state[j], state[i]
        output.append(ord(char) ^ state[(state[i] + state[j]) % 256])
    return bytes(output)


def _custom_base64(value: str, alphabet_index: int) -> str:
    bits = "".join(f"{ord(char):08b}" for char in value)
    padding = (6 - len(bits) % 6) % 6
    bits += "0" * padding
    alphabet = _ALPHABETS[alphabet_index]
    result = "".join(
        alphabet[int(bits[index : index + 6], 2)]
        for index in range(0, len(bits), 6)
    )
    return result + "=" * (padding // 2)


def _encode_bytes(value: str, alphabet_index: int = 0) -> str:
    alphabet = _ALPHABETS[alphabet_index]
    output: list[str] = []
    for index in range(0, len(value), 3):
        chunk = value[index : index + 3]
        number = ord(chunk[0]) << 16
        if len(chunk) > 1:
            number |= ord(chunk[1]) << 8
        if len(chunk) > 2:
            number |= ord(chunk[2])
        output.append(alphabet[(number & 0xFC0000) >> 18])
        output.append(alphabet[(number & 0x03F000) >> 12])
        if len(chunk) > 1:
            output.append(alphabet[(number & 0x0FC0) >> 6])
        if len(chunk) > 2:
            output.append(alphabet[number & 0x3F])
    output.append("=" * ((4 - len(output) % 4) % 4))
    return "".join(output)


def _transform(values: list[int]) -> str:
    permutation = _PERMUTATION.copy()
    index_b = permutation[1]
    initial = 0
    value_e = 0
    result: list[str] = []
    for index, char_value in enumerate(values):
        if index == 0:
            initial = permutation[index_b]
            total = index_b + initial
            permutation[1] = initial
            permutation[index_b] = index_b
        else:
            total = initial + value_e
        total %= len(permutation)
        result.append(chr(char_value ^ permutation[total]))
        value_e = permutation[(index + 2) % len(permutation)]
        total = (index_b + value_e) % len(permutation)
        initial = permutation[total]
        permutation[total] = permutation[(index + 2) % len(permutation)]
        permutation[(index + 2) % len(permutation)] = initial
        index_b = total
    return "".join(result)


def _random_bytes(groups: int = 3) -> str:
    output: list[str] = []
    for _ in range(groups):
        value = int(random.random() * 10000)
        output.extend(
            (
                chr(((value & 255) & 170) | 1),
                chr(((value & 255) & 85) | 2),
                chr((((value % 0x100000000) >> 8) & 170) | 5),
                chr((((value % 0x100000000) >> 8) & 85) | 40),
            )
        )
    return "".join(output)


def _browser_fingerprint() -> str:
    inner_width = random.randint(1024, 1920)
    inner_height = random.randint(768, 1080)
    outer_width = inner_width + random.randint(24, 32)
    outer_height = inner_height + random.randint(75, 90)
    screen_y = random.choice((0, 30))
    available_width = random.randint(1280, 1920)
    available_height = random.randint(800, 1080)
    return (
        f"{inner_width}|{inner_height}|{outer_width}|{outer_height}|0|"
        f"{screen_y}|0|0|{available_width}|{available_height}|"
        f"{available_width}|{available_height}|{inner_width}|{inner_height}|"
        "24|24|Win32"
    )


def generate_ms_token(length: int = 184) -> str:
    alphabet = string.ascii_letters + string.digits + "-_"
    return "".join(random.choice(alphabet) for _ in range(length))


def generate_verify_fp() -> str:
    alphabet = string.digits + string.ascii_uppercase + string.ascii_lowercase
    timestamp = int(time.time() * 1000)
    base36 = ""
    while timestamp:
        timestamp, remainder = divmod(timestamp, 36)
        base36 = (str(remainder) if remainder < 10 else chr(87 + remainder)) + base36
    value = [""] * 36
    value[8] = value[13] = value[18] = value[23] = "_"
    value[14] = "4"
    for index in range(36):
        if value[index]:
            continue
        selected = random.randrange(len(alphabet))
        if index == 19:
            selected = (selected & 3) | 8
        value[index] = alphabet[selected]
    return f"verify_{base36}_{''.join(value)}"


def generate_a_bogus(query: str, user_agent: str, request: str = "GET") -> str:
    """Generate an ``a_bogus`` value for an already URL-encoded query string."""
    start = int(time.time() * 1000)
    parameter_hash = _hash_parameter(_hash_parameter(query))
    request_hash = _hash_parameter(_hash_parameter(request))
    encrypted_ua = _rc4(b"\x00\x01\x0e", user_agent)
    ua_base64 = _custom_base64("".join(chr(value) for value in encrypted_ua), 1)
    ua_hash = _hash_parameter(ua_base64, salt=False)
    end = int(time.time() * 1000)

    values: dict[int, int] = {
        8: 3,
        18: 44,
        19: 1,
        20: (start >> 24) & 255,
        21: (start >> 16) & 255,
        22: (start >> 8) & 255,
        23: start & 255,
        24: int(start / 256**4),
        25: int(start / 256**5),
        26: 0,
        27: 0,
        28: 0,
        29: 0,
        30: 0,
        31: 1,
        32: 0,
        33: 0,
        34: 0,
        35: 0,
        36: 0,
        37: 14,
        38: parameter_hash[21],
        39: parameter_hash[22],
        40: request_hash[21],
        41: request_hash[22],
        42: ua_hash[23],
        43: ua_hash[24],
        44: (end >> 24) & 255,
        45: (end >> 16) & 255,
        46: (end >> 8) & 255,
        47: end & 255,
        48: 3,
        49: int(end / 256**4),
        50: int(end / 256**5),
        51: 0,
        52: 0,
        53: 0,
        54: 0,
        55: 0,
        56: 6383,
        57: 6383 & 255,
        58: (6383 >> 8) & 255,
        59: 0,
        60: 0,
        66: 0,
        69: 0,
        70: 0,
        71: 0,
    }

    fingerprint = _browser_fingerprint()
    values[64] = values[65] = len(fingerprint)
    sorted_values = [values.get(index, 0) for index in _SORT_INDEX]
    checksum = values.get(_XOR_INDEX[0], 0)
    for index in _XOR_INDEX[1:]:
        checksum ^= values.get(index, 0)
    sorted_values.extend(ord(char) for char in fingerprint)
    sorted_values.append(checksum)
    return _encode_bytes(_random_bytes() + _transform(sorted_values))
