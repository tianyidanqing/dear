# -*- coding: utf-8 -*-
"""企业微信回调的签名校验与消息加解密。

官方规范（「接收消息服务器配置」）：
  - 签名：sha1( ''.join(sorted([token, timestamp, nonce, encrypt])) )
  - 密文：AES-256-CBC，key = Base64Decode(EncodingAESKey + "=")，iv = key[:16]
  - 明文结构：random(16B) + msg_len(4B 网络字节序) + msg + receiveid(CorpID)
  - 填充：PKCS#7，块大小 32 字节

这里只做**纯逻辑**（不碰网络、不碰数据库），方便单测覆盖到每一段字节。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

_BLOCK_SIZE = 32

try:                                    # 延迟到调用时再报错，避免没装依赖就 import 失败
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    _HAS_CRYPTO = True
except Exception:                       # pragma: no cover - 取决于环境
    _HAS_CRYPTO = False


def _require_crypto():
    if not _HAS_CRYPTO:
        raise RuntimeError(
            "企业微信回调需要加密库，请安装：pip install 'cryptography>=42'"
            "（或 pip install '.[wecom]'）")


def _pkcs7_pad(data: bytes, block: int = _BLOCK_SIZE) -> bytes:
    n = block - len(data) % block
    return data + bytes([n]) * n


def _pkcs7_unpad(data: bytes, block: int = _BLOCK_SIZE) -> bytes:
    if not data:
        raise ValueError("密文为空")
    n = data[-1]
    if n < 1 or n > block or n > len(data):
        raise ValueError("PKCS#7 填充非法（Token / EncodingAESKey 可能不匹配）")
    return data[:-n]


def _aes_cbc(key: bytes, iv: bytes, data: bytes, encrypting: bool) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    op = cipher.encryptor() if encrypting else cipher.decryptor()
    return op.update(data) + op.finalize()


class WeComCrypto:
    """企业微信消息加解密器（一个应用一个实例）。"""

    def __init__(self, token: str, encoding_aes_key: str, corp_id: str):
        if not token:
            raise ValueError("缺少回调 Token")
        if not encoding_aes_key or len(encoding_aes_key) != 43:
            raise ValueError("EncodingAESKey 必须是 43 个字符（企业微信后台生成）")
        if not corp_id:
            raise ValueError("缺少 CorpID（企业ID）")
        self.token = token
        self.corp_id = corp_id
        # 43 字符的 Base64（无 padding）补一个 '=' 后正好解出 32 字节
        self.key = base64.b64decode(encoding_aes_key + "=")
        if len(self.key) != 32:
            raise ValueError("EncodingAESKey 解码后不是 32 字节，请检查后台配置")
        self.iv = self.key[:16]

    # ---------------- 签名 ----------------
    def signature(self, timestamp, nonce, encrypt) -> str:
        parts = sorted([str(self.token), str(timestamp), str(nonce), str(encrypt)])
        return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()

    def verify(self, msg_signature, timestamp, nonce, encrypt) -> bool:
        expect = self.signature(timestamp, nonce, encrypt)
        return hmac.compare_digest(expect, str(msg_signature or ""))

    # ---------------- 解密 ----------------
    def decrypt(self, encrypt_b64: str) -> str:
        _require_crypto()
        try:
            raw = base64.b64decode(encrypt_b64)
        except Exception as e:
            raise ValueError(f"密文不是合法 Base64：{e}") from e
        if len(raw) % 16 != 0 or not raw:
            raise ValueError("密文长度不是 16 的整数倍")
        plain = _pkcs7_unpad(_aes_cbc(self.key, self.iv, raw, encrypting=False))
        if len(plain) < 20:
            raise ValueError("解密结果长度异常")
        msg_len = int.from_bytes(plain[16:20], "big")
        if msg_len < 0 or 20 + msg_len > len(plain):
            raise ValueError("解密结果中的消息长度字段非法")
        msg = plain[20:20 + msg_len].decode("utf-8")
        receive_id = plain[20 + msg_len:].decode("utf-8")
        if receive_id and receive_id != self.corp_id:
            raise ValueError(f"receiveid 不匹配：期望 {self.corp_id}，实际 {receive_id}")
        return msg

    def decrypt_echo(self, msg_signature, timestamp, nonce, echostr) -> str:
        """URL 验证：校验签名后解密 echostr，返回需要原样输出的明文。"""
        if not self.verify(msg_signature, timestamp, nonce, echostr):
            raise ValueError("签名校验失败（Token 与后台配置不一致？）")
        return self.decrypt(echostr)

    # ---------------- 加密 ----------------
    def encrypt(self, msg: str) -> str:
        _require_crypto()
        msg_bytes = msg.encode("utf-8")
        plain = (os.urandom(16)
                 + len(msg_bytes).to_bytes(4, "big")
                 + msg_bytes
                 + self.corp_id.encode("utf-8"))
        cipher = _aes_cbc(self.key, self.iv, _pkcs7_pad(plain), encrypting=True)
        return base64.b64encode(cipher).decode("ascii")

    def encrypt_reply(self, reply_xml: str, timestamp: str, nonce: str) -> str:
        """把回复 XML 加密成企业微信要求的外层信封。"""
        encrypt = self.encrypt(reply_xml)
        sig = self.signature(timestamp, nonce, encrypt)
        return (
            "<xml>"
            f"<Encrypt><![CDATA[{encrypt}]]></Encrypt>"
            f"<MsgSignature><![CDATA[{sig}]]></MsgSignature>"
            f"<TimeStamp>{timestamp}</TimeStamp>"
            f"<Nonce><![CDATA[{nonce}]]></Nonce>"
            "</xml>"
        )
