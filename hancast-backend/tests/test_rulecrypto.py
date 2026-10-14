"""规则包沙箱的密码学原语：与 OpenSSL 逐字节对齐

向量不是本实现自己算出来的，全部由 Node.js 内置 crypto（OpenSSL）生成后固化在
``tests/data/crypto_node_vectors.json``。所以这套测试绿了等于「与真实实现一致」，
而不是「自洽」。RSA 只有公钥加密口（填充随机），断言 PKCS#1 v1.5 结构。
"""

import base64
import hashlib
import hmac as stdlib_hmac
import json
import os

import pytest

from hancast_sidecar.media.rules.crypto import (
    AesError, INV_SBOX, SBOX, _AES, aes_cbc_decrypt, aes_cbc_encrypt,
    aes_ecb_decrypt, aes_ecb_encrypt, pkcs7_pad, pkcs7_unpad, rsa_encrypt,
)

_VECTORS = json.load(open(os.path.join(os.path.dirname(__file__),
                                       "data", "crypto_node_vectors.json")))


@pytest.mark.parametrize("v", _VECTORS["aes"])
def test_node_vectors_match_byte_for_byte(v):
    key = bytes.fromhex(v["key"])
    iv = base64.b64decode(v["iv"])
    plain = base64.b64decode(v["plain"])
    cipher = base64.b64decode(v["cipher"])
    if v["mode"] == "ecb":
        assert aes_ecb_encrypt(key, plain) == cipher
        assert aes_ecb_decrypt(key, cipher) == plain
    else:
        assert aes_cbc_encrypt(key, iv, plain) == cipher
        assert aes_cbc_decrypt(key, iv, cipher) == plain


@pytest.mark.parametrize("v", _VECTORS["fips"])
def test_fips197_single_block(v):
    """FIPS-197 附录 C：密钥 000102…，明文 00112233…"""
    cipher = _AES(bytes.fromhex(v["key"]))
    assert cipher.encrypt_block(bytes.fromhex(v["plain"])).hex() == v["cipher"]
    assert cipher.decrypt_block(bytes.fromhex(v["cipher"])).hex() == v["plain"]


def test_sbox_is_the_real_table():
    assert SBOX[:8] == (0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5)
    assert sorted(SBOX) == list(range(256))          # 置换表，不能有重复
    for i in range(256):
        assert INV_SBOX[SBOX[i]] == i


@pytest.mark.parametrize("v", _VECTORS["hmac"])
def test_hmac_vectors(v):
    algo = {"sha1": hashlib.sha1, "sha256": hashlib.sha256,
            "md5": hashlib.md5}[v["algo"]]
    mac = stdlib_hmac.new(v["key"].encode(), v["msg"].encode(), algo)
    if "hex" in v:
        assert mac.hexdigest() == v["hex"]
    else:
        assert base64.b64encode(mac.digest()).decode() == v["b64"]


def test_pkcs7_edges():
    assert pkcs7_pad(b"", 16) == bytes([16]) * 16
    assert pkcs7_unpad(pkcs7_pad(b"1234567890123456")) == b"1234567890123456"
    with pytest.raises(AesError):
        pkcs7_unpad(b"012345678901234")
    with pytest.raises(AesError):
        pkcs7_unpad(b"x" * 15 + b"\x05")


def test_bad_inputs_raise_aeserror_not_crash():
    with pytest.raises(AesError):
        aes_ecb_encrypt(b"short", b"data")
    with pytest.raises(AesError):
        aes_cbc_encrypt(b"0" * 16, b"iv", b"data")
    with pytest.raises(AesError):
        aes_ecb_decrypt(b"0" * 16, b"not-a-multiple")
    with pytest.raises(AesError):
        aes_ecb_encrypt(b"0" * 16, b"z" * (256 * 1024 + 1))


def test_rsa_pkcs1_v15_structure():
    """沙箱只有公钥加密口，所以用一次性测试私钥现场解回来验填充

    ``_rsa_unpad`` 是独立实现（只做模幂 + 剥填充），不复用被测代码，
    因此「我们加密 → 它解得出原文」才算是证据；同一条路径解 OpenSSL
    自己加密的密文也得到同一原文，双向对齐。
    """
    rsa = _VECTORS["rsa"]
    n, d = int(rsa["n_hex"], 16), int(rsa["d_hex"], 16)
    plain = rsa["plain"].encode()
    assert rsa["k"] == (n.bit_length() + 7) // 8

    ct = rsa_encrypt(plain, rsa["n_hex"], rsa["e_hex"])
    assert len(ct) == rsa["k"]
    assert _rsa_unpad(ct, n, d) == plain
    assert _rsa_unpad(base64.b64decode(rsa["cipher"]), n, d) == plain
    assert rsa_encrypt(plain, rsa["n_hex"], rsa["e_hex"]) != ct      # 填充随机

    with pytest.raises(AesError):
        rsa_encrypt(b"z" * 200, rsa["n_hex"], rsa["e_hex"])           # 超出容量


def _rsa_unpad(ct: bytes, n: int, d: int) -> bytes:
    m = pow(int.from_bytes(ct, "big"), d, n)
    block = m.to_bytes((n.bit_length() + 7) // 8, "big")
    if block[0] != 0x00 or block[1] != 0x02:
        raise AesError("PKCS#1 块类型不对")
    sep = block.index(b"\x00", 2)
    if sep < 10:
        raise AesError("填充太短")
    return block[sep + 1:]
