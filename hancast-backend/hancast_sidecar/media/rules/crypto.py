"""规则包沙箱的密码学原语（纯标准库，零新依赖）

为什么自己实现：沙箱此前只有 md5/sha256/base64，签名型站点（HMAC、AES、RSA 公钥）
在规则层根本写不出来。而打包链路是 Nuitka 冻结的 sidecar，加 pycryptodome/cryptography
这类 C/Rust 扩展等于把每一轮产物都压在依赖风险上——签名运算的输入只有几百字节，
纯 Python 足够。

S 盒按定义现场生成（GF(2^8) 求逆 + 仿射变换）而不是抄表：抄错一个字节只有测试能发现，
而生成的表可以直接用 FIPS-197 官方向量自证。
"""

import os

# ── GF(2^8)，不可约多项式 0x11b ──


def _gmul(a: int, b: int) -> int:
    r = 0
    for _ in range(8):
        if b & 1:
            r ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return r


def _build_sbox():
    exp_tbl = [0] * 256
    log_tbl = [0] * 256
    x = 1
    for i in range(255):
        exp_tbl[i] = x
        log_tbl[x] = i
        x = _gmul(x, 3)          # 3 是 GF(2^8) 的本原元
    inv = [0] * 256
    for a in range(1, 256):
        inv[a] = exp_tbl[(255 - log_tbl[a]) % 255]
    sbox = []
    for a in range(256):
        b = inv[a] if a else 0
        s = b
        for _ in range(4):
            b = ((b << 1) | (b >> 7)) & 0xFF
            s ^= b
        sbox.append(s ^ 0x63)
    sbox = tuple(sbox)
    inverse = [0] * 256
    for i, v in enumerate(sbox):
        inverse[v] = i
    return sbox, tuple(inverse)


SBOX, INV_SBOX = _build_sbox()
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36,
         0x6C, 0xD8, 0xAB, 0x4D, 0x9A)


class AesError(ValueError):
    """密钥/IV 长度或填充不合法——对沙箱一律表现为「这一路取不到结果」"""


class _AES:
    def __init__(self, key: bytes):
        if len(key) not in (16, 24, 32):
            raise AesError(f"AES 密钥长度必须是 16/24/32 字节，收到 {len(key)}")
        self.nk = len(key) // 4
        self.nr = self.nk + 6
        self.rk = self._expand(key)

    def _expand(self, key: bytes) -> list:
        w = [list(key[4 * i:4 * i + 4]) for i in range(self.nk)]
        for i in range(self.nk, 4 * (self.nr + 1)):
            t = list(w[i - 1])
            if i % self.nk == 0:
                t = t[1:] + t[:1]
                t = [SBOX[b] for b in t]
                t[0] ^= _RCON[i // self.nk - 1]
            elif self.nk > 6 and i % self.nk == 4:
                t = [SBOX[b] for b in t]
            w.append([w[i - self.nk][j] ^ t[j] for j in range(4)])
        return [b for word in w for b in word]

    def _xor_key(self, s, offset):
        rk = self.rk
        for i in range(16):
            s[i] ^= rk[offset + i]
        return s

    @staticmethod
    def _sub(s, table):
        for i in range(16):
            s[i] = table[s[i]]
        return s

    @staticmethod
    def _shift_rows(s, right=False):
        # 状态按列主序存放：行 r、列 c 的元素是 s[4c + r]
        out = list(s)
        for r in range(1, 4):
            for c in range(4):
                src = (c - r) % 4 if right else (c + r) % 4
                out[4 * c + r] = s[4 * src + r]
        return out

    @classmethod
    def _mix_columns(cls, s, inverse=False):
        out = list(s)
        for c in range(4):
            a = s[4 * c:4 * c + 4]
            for r in range(4):
                if not inverse:
                    out[4 * c + r] = (_gmul(a[r], 2) ^ _gmul(a[(r + 1) % 4], 3)
                                      ^ a[(r + 2) % 4] ^ a[(r + 3) % 4])
                else:
                    out[4 * c + r] = (
                        _gmul(a[r], 0x0E) ^ _gmul(a[(r + 1) % 4], 0x0B)
                        ^ _gmul(a[(r + 2) % 4], 0x0D) ^ _gmul(a[(r + 3) % 4], 0x09))
        return out

    def encrypt_block(self, block: bytes) -> bytes:
        s = self._xor_key(list(block), 0)
        for rnd in range(1, self.nr):
            s = self._mix_columns(self._shift_rows(self._sub(s, SBOX)), False)
            s = self._xor_key(s, 16 * rnd)
        s = self._xor_key(self._shift_rows(self._sub(s, SBOX)), 16 * self.nr)
        return bytes(s)

    def decrypt_block(self, block: bytes) -> bytes:
        # 严格照 FIPS-197 §5.3：InvShiftRows → InvSubBytes → AddRoundKey → InvMixColumns
        s = self._xor_key(list(block), 16 * self.nr)
        for rnd in range(self.nr - 1, 0, -1):
            s = self._shift_rows(s, right=True)
            s = self._sub(s, INV_SBOX)
            s = self._xor_key(s, 16 * rnd)
            s = self._mix_columns(s, True)
        s = self._xor_key(self._sub(self._shift_rows(s, right=True), INV_SBOX), 0)
        return bytes(s)


# ── PKCS7 ──


def pkcs7_pad(data: bytes, size: int = 16) -> bytes:
    n = size - (len(data) % size)
    return data + bytes([n]) * n


def pkcs7_unpad(data: bytes, size: int = 16) -> bytes:
    if not data or len(data) % size:
        raise AesError("密文长度不合法")
    n = data[-1]
    if not 1 <= n <= size or data[-n:] != bytes([n]) * n:
        raise AesError("PKCS7 填充不合法")
    return data[:-n]


# ── 分组模式 ──

_MAX_CIPHER_BYTES = 256 * 1024      # 沙箱调用上限：签名场景是几百字节，超了就是滥用


def _check_size(data: bytes) -> None:
    if len(data) > _MAX_CIPHER_BYTES:
        raise AesError("待加/解密数据超过 256KB 上限")


def aes_ecb_encrypt(key: bytes, data: bytes) -> bytes:
    _check_size(data)
    cipher = _AES(key)
    padded = pkcs7_pad(data)
    return b"".join(cipher.encrypt_block(padded[i:i + 16])
                    for i in range(0, len(padded), 16))


def aes_ecb_decrypt(key: bytes, data: bytes) -> bytes:
    _check_size(data)
    if not data or len(data) % 16:
        raise AesError("密文长度不是 16 的整数倍")
    cipher = _AES(key)
    out = b"".join(cipher.decrypt_block(data[i:i + 16])
                   for i in range(0, len(data), 16))
    return pkcs7_unpad(out)


def aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    _check_size(data)
    if len(iv) != 16:
        raise AesError("IV 必须是 16 字节")
    cipher = _AES(key)
    prev, out = iv, []
    padded = pkcs7_pad(data)
    for i in range(0, len(padded), 16):
        blk = bytes(a ^ b for a, b in zip(padded[i:i + 16], prev))
        enc = cipher.encrypt_block(blk)
        out.append(enc)
        prev = enc
    return b"".join(out)


def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    _check_size(data)
    if len(iv) != 16:
        raise AesError("IV 必须是 16 字节")
    if not data or len(data) % 16:
        raise AesError("密文长度不是 16 的整数倍")
    cipher = _AES(key)
    prev, out = iv, []
    for i in range(0, len(data), 16):
        blk = data[i:i + 16]
        out.append(bytes(a ^ b for a, b in zip(cipher.decrypt_block(blk), prev)))
        prev = blk
    return pkcs7_unpad(b"".join(out))


# ── RSA（公钥加密，PKCS#1 v1.5 type 2 填充）──


def rsa_encrypt(data: bytes, modulus_hex: str, exponent_hex: str) -> bytes:
    _check_size(data)
    n = int(modulus_hex.replace(":", "").strip(), 16)
    e = int(exponent_hex.strip(), 16)
    if n <= 0 or e <= 0:
        raise AesError("RSA 模数/指数不合法")
    k = (n.bit_length() + 7) // 8
    if len(data) > k - 11:
        raise AesError("RSA 明文超出模数容量")
    pad_len = k - len(data) - 3
    padding = bytearray(os.urandom(pad_len))
    for i in range(pad_len):
        while padding[i] == 0:
            padding[i] = os.urandom(1)[0]
    block = b"\x00\x02" + bytes(padding) + b"\x00" + data
    return pow(int.from_bytes(block, "big"), e, n).to_bytes(k, "big")
