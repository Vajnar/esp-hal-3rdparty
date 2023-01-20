#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2021-2022 Espressif Systems (Shanghai) CO LTD
# SPDX-License-Identifier: Apache-2.0

import argparse
import datetime
import hashlib
import hmac
import os
import random
import struct

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import _modinv as modinv  # type: ignore
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.utils import int_to_bytes

supported_targets = {'esp32s2', 'esp32c3', 'esp32s3', 'esp32c6'}
supported_key_size = {'esp32s2':[4096, 3072, 2048, 1024],
                      'esp32c3':[3072, 2048, 1024],
                      'esp32s3':[4096, 3072, 2048, 1024],
                      'esp32c6':[3072, 2048, 1024]}

NUM_HMAC_KEYS = 3
NUM_MESSAGES = 10
NUM_CASES = 6


def number_as_bignum_words(number):  # type: (int) -> str
    """
    Given a number, format result as a C array of words
    (little-endian, same as ESP32 RSA peripheral or mbedTLS)
    """
    result = []
    while number != 0:
        result.append('0x%08x' % (number & 0xFFFFFFFF))
        number >>= 32
    return '{ ' + ', '.join(result) + ' }'


def number_as_bytes(number, pad_bits=None):  # type: (int, int) -> bytes
    """
    Given a number, format as a little endian array of bytes
    """
    result = int_to_bytes(number)[::-1]  # type: bytes
    while pad_bits is not None and len(result) < (pad_bits // 8):
        result += b'\x00'
    return result


def bytes_as_char_array(b):  # type: (bytes) -> str
    """
    Given a sequence of bytes, format as a char array
    """
    return '{ ' + ', '.join('0x%02x' % x for x in b) + ' }'


def generate_tests_cases(target):  # type: (str) -> None

    max_key_size = max(supported_key_size[target])
    print('Generating tests cases for {} (max key size = {})'.format(target, max_key_size))

    hmac_keys = [os.urandom(32) for x in range(NUM_HMAC_KEYS)]

    messages = [random.randrange(0, 1 << max_key_size) for x in range(NUM_MESSAGES)]

    with open('digital_signature_test_cases.h', 'w') as f:
        f.write('/*\n')
        year = datetime.datetime.now().year
        f.write(' * SPDX-FileCopyrightText: {year} Espressif Systems (Shanghai) CO LTD\n'.format(year=year))
        f.write(' *\n')
        f.write(' * SPDX-License-Identifier: Apache-2.0\n')
        f.write(' *\n')
        f.write(' * File generated by gen_digital_signature_tests.py\n')
        f.write(' */\n')

        # Write out HMAC keys
        f.write('#define NUM_HMAC_KEYS %d\n\n' % NUM_HMAC_KEYS)
        f.write('static const uint8_t test_hmac_keys[NUM_HMAC_KEYS][32] = {\n')
        for h in hmac_keys:
            f.write('     %s,\n' % bytes_as_char_array(h))
        f.write('};\n\n')

        # Write out messages
        f.write('#define NUM_MESSAGES %d\n\n' % NUM_MESSAGES)
        f.write('static const uint32_t test_messages[NUM_MESSAGES][%d/32] = {\n' % max_key_size)
        for m in messages:
            f.write('        // Message %d\n' % messages.index(m))
            f.write('        %s,\n' % number_as_bignum_words(m))
        f.write('    };\n')
        f.write('\n\n\n')

        f.write('#define NUM_CASES %d\n\n' % NUM_CASES)
        f.write('static const encrypt_testcase_t test_cases[NUM_CASES] = {\n')

        for case in range(NUM_CASES):
            f.write('    { /* Case %d */\n' % case)

            iv = os.urandom(16)
            f.write('        .iv = %s,\n' % (bytes_as_char_array(iv)))

            hmac_key_idx = random.randrange(0, NUM_HMAC_KEYS)
            aes_key = hmac.HMAC(hmac_keys[hmac_key_idx], b'\xFF' * 32, hashlib.sha256).digest()

            sizes = supported_key_size[target]
            key_size = sizes[case % len(sizes)]

            private_key = rsa.generate_private_key(
                public_exponent=65537,
                key_size=key_size,
                backend=default_backend())

            priv_numbers = private_key.private_numbers()
            pub_numbers = private_key.public_key().public_numbers()
            Y = priv_numbers.d
            M = pub_numbers.n

            rr = 1 << (key_size * 2)
            rinv = rr % pub_numbers.n
            mprime = - modinv(M, 1 << 32)
            mprime &= 0xFFFFFFFF
            length = key_size // 32 - 1

            f.write('        .p_data = {\n')
            f.write('            .Y = %s,\n' % number_as_bignum_words(Y))
            f.write('            .M = %s,\n' % number_as_bignum_words(M))
            f.write('            .Rb = %s,\n' % number_as_bignum_words(rinv))
            f.write('            .M_prime = 0x%08x,\n' % mprime)
            f.write('            .length = %d, // %d bit\n' % (length, key_size))
            f.write('        },\n')

            # calculate MD from preceding values and IV
            # Y_max_key_size || M_max_key_size || Rb_max_key_size || M_prime32 || LENGTH32 || IV128
            md_in = number_as_bytes(Y, max_key_size) + \
                number_as_bytes(M, max_key_size) + \
                number_as_bytes(rinv, max_key_size) + \
                struct.pack('<II', mprime, length) + \
                iv

            md = hashlib.sha256(md_in).digest()

            # generate expected C value from P bitstring
            #
            # Y_max_key_size || M_max_key_size || Rb_max_key_size || M_prime32 || LENGTH32 ||  0x08*8
            # E.g. for C3: Y3072 || M3072 || Rb3072 || M_prime32 || LENGTH32 || MD256 || 0x08*8
            p = number_as_bytes(Y, max_key_size) + \
                number_as_bytes(M, max_key_size) + \
                number_as_bytes(rinv, max_key_size) + \
                md + \
                struct.pack('<II', mprime, length) + \
                b'\x08' * 8

            # expected_len = max_len_Y + max_len_M + max_len_rinv + md (32 bytes) + (mprime + length packed (8bytes)) + padding (8 bytes)
            expected_len = (max_key_size / 8) * 3 + 32 + 8 + 8
            assert len(p) == expected_len

            cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv), backend=default_backend())
            encryptor = cipher.encryptor()
            c = encryptor.update(p) + encryptor.finalize()

            f.write('        .expected_c = %s,\n' % bytes_as_char_array(c))
            f.write('        .hmac_key_idx = %d,\n' % (hmac_key_idx))

            f.write('        // results of message array encrypted with these keys\n')
            f.write('        .expected_results = {\n')
            mask = (1 << key_size) - 1  # truncate messages if needed
            for m in messages:
                f.write('        // Message %d\n' % messages.index(m))
                f.write('      %s,' % (number_as_bignum_words(pow(m & mask, Y, M))))
            f.write('     },\n')
            f.write('     },\n')

        f.write('};\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='''Generates Digital Signature Test Cases''')

    parser.add_argument(
        '--target',
        required=True,
        choices=supported_targets,
        help='Target to generate test cases for, different targets support different max key length')

    args = parser.parse_args()

    generate_tests_cases(args.target)
