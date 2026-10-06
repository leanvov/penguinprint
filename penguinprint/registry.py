"""本地指纹库（SQLite）：短指纹 ID ←→ 买家标识 ←→ 嵌入记录。

嵌入端把"买家标识 → 短 ID"的对应关系写进指纹库，
提取端只要从图上读出短 ID，就能反查到底卖给了谁；
即使换机器/没有指纹库，也能凭短码与 ID 人工核对（见 README 的溯源流程）。
"""

from __future__ import annotations

import csv
import sqlite3
import threading
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path

from .config import DEFAULT_DB_PATH
from .payload import (
    fingerprint_bytes,
    new_key_hex,
    normalize_identifier,
    sha256_hex,
    short_code,
)

__all__ = ["Registry"]

_SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS fingerprints (
    id_hex      TEXT PRIMARY KEY,
    short_code  TEXT NOT NULL,
    identifier  TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    note        TEXT DEFAULT '',
    key_fp      TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS embeds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    id_hex      TEXT NOT NULL,
    src_path    TEXT NOT NULL,
    dst_path    TEXT NOT NULL,
    embedded_at TEXT NOT NULL,
    psnr        REAL,
    blocks      INTEGER,
    verified    INTEGER DEFAULT 0,
    batch       TEXT DEFAULT '',
    src_sha256  TEXT DEFAULT '',
    dst_sha256  TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
"""

# 索引在"建表 + 补齐列"之后再建：列还不存在时建索引会直接报错。
_SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_embeds_id ON embeds(id_hex);
CREATE INDEX IF NOT EXISTS idx_fp_identifier ON fingerprints(identifier);
CREATE INDEX IF NOT EXISTS idx_embeds_batch ON embeds(batch);
"""

# 结构补齐用：表名 -> [(列名, 列定义), ...]（缺列时自动补上）
_ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "fingerprints": [("key_fp", "TEXT DEFAULT ''")],
    "embeds": [
        ("batch", "TEXT DEFAULT ''"),
        ("src_sha256", "TEXT DEFAULT ''"),
        ("dst_sha256", "TEXT DEFAULT ''"),
    ],
}

KEY_META_NAME = "hmac_key"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Registry:
    """线程安全的指纹库封装（GUI 工作线程与主线程都会用到）。

    指纹库里除了"标识 ↔ 指纹 ID"的对应，还保存两样**法证相关**的东西：
    * **HMAC 密钥**（meta 表）：使指纹不可被第三方伪造；备份指纹库即备份密钥；
    * **交付记录**（embeds 表）：每次嵌入的源图/输出图 SHA-256、时间戳与批次号，
      用来回答"我当初交付给他的就是这份文件"。
    """

    def __init__(self, path: str | Path = DEFAULT_DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._session() as con:
            con.executescript(_SCHEMA_TABLES)
            self._add_missing_columns(con)
            con.executescript(_SCHEMA_INDEXES)
            self.key = self._ensure_key(con)

    @property
    def key_fp(self) -> str:
        """密钥指纹（SHA-256 前 8 位十六进制）：用来人工确认"是否同一把密钥"。"""
        import hashlib

        return hashlib.sha256((self.key or "").encode("utf-8")).hexdigest()[:8].upper()

    @staticmethod
    def _ensure_key(con: sqlite3.Connection) -> str:
        """取出 HMAC 密钥；没有就生成一把并存进 meta 表。"""
        row = con.execute("SELECT value FROM meta WHERE key=?", (KEY_META_NAME,)).fetchone()
        if row and row[0]:
            return str(row[0])
        key = new_key_hex(32)
        con.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (KEY_META_NAME, key)
        )
        return key

    @staticmethod
    def _add_missing_columns(con: sqlite3.Connection) -> None:
        """确保表结构完整：缺列时用 ADD COLUMN 补上，不影响原有数据。"""
        for table, cols in _ADDED_COLUMNS.items():
            have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    # ------------------------------------------------------------------ 基础
    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.path), timeout=15.0, check_same_thread=False)
        con.row_factory = sqlite3.Row
        return con

    @contextmanager
    def _session(self):
        """一次库访问的上下文：正常结束提交事务，无论成败都关闭连接。

        注意 sqlite3 的 ``with con:`` 只提交或回滚事务、不关连接；
        连接必须显式关闭才能释放库文件，因此这里把两者合到同一个连接上。
        """
        con = self._connect()
        with closing(con):
            with con:
                yield con

    # ------------------------------------------------------------------ 指纹
    def register(
        self, identifier: str, id_bytes: int | None = None, note: str = ""
    ) -> dict:
        """登记买家标识，返回指纹记录（同标识同长度重复登记是幂等的）。

        ``id_bytes`` 必填：指纹 ID 由 HMAC-SHA256 截取前 ``id_bytes`` 字节得到，
        嵌入端与登记端必须用同一个值；不同长度会算出互不匹配的 ID，
        使提取结果在库里查不到。调用方传自己使用的档位的 ``cfg.id_bytes``。

        标识可以是任意非空字符串，长度限制 1~200 字符；
        存入前做"去首尾空白 + 合并空格 + 统一小写"的归一化，
        因此大小写/多余空格不会算出两个指纹。
        """
        identifier = str(identifier).strip()
        if not identifier:
            raise ValueError("买家标识不能为空")
        if len(identifier) > 200:
            raise ValueError(f"买家标识过长（{len(identifier)} 字符，最多 200）：{identifier[:24]}…")
        if id_bytes is None or int(id_bytes) <= 0:
            raise ValueError(
                "register() 需要 id_bytes（指纹 ID 的截取长度）："
                "请传入与嵌入时一致的 cfg.id_bytes，否则登记的 ID 与图上的 ID 不匹配"
            )
        id_bytes = int(id_bytes)
        fp = fingerprint_bytes(identifier, id_bytes, key=self.key)
        id_hex = fp.hex().upper()
        rec = {
            "id_hex": id_hex,
            "short_code": short_code(fp),
            "identifier": normalize_identifier(identifier),
            "sha256": sha256_hex(identifier),
            "created_at": _now(),
            "note": note,
            "key_fp": self.key_fp,
        }
        with self._lock, self._session() as con:
            con.execute(
                "INSERT OR IGNORE INTO fingerprints"
                "(id_hex, short_code, identifier, sha256, created_at, note, key_fp)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    rec["id_hex"],
                    rec["short_code"],
                    rec["identifier"],
                    rec["sha256"],
                    rec["created_at"],
                    note,
                    rec["key_fp"],
                ),
            )
            if note:
                con.execute(
                    "UPDATE fingerprints SET note=? WHERE id_hex=? AND (note IS NULL OR note='')",
                    (note, id_hex),
                )
            row = con.execute("SELECT * FROM fingerprints WHERE id_hex=?", (id_hex,)).fetchone()
        return dict(row) if row else rec

    def get(self, id_hex: str) -> dict | None:
        with self._lock, self._session() as con:
            row = con.execute(
                "SELECT * FROM fingerprints WHERE id_hex=?", (str(id_hex).upper(),)
            ).fetchone()
        return dict(row) if row else None

    def find_by_identifier(self, identifier: str) -> list[dict]:
        with self._lock, self._session() as con:
            rows = con.execute(
                "SELECT * FROM fingerprints WHERE identifier=? ORDER BY created_at DESC",
                (normalize_identifier(identifier),),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_all(self) -> list[dict]:
        with self._lock, self._session() as con:
            rows = con.execute(
                """
                SELECT f.*, (SELECT COUNT(*) FROM embeds e WHERE e.id_hex = f.id_hex) AS embeds
                FROM fingerprints f ORDER BY f.created_at DESC
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        with self._lock, self._session() as con:
            return int(con.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0])

    def delete(self, id_hex: str) -> bool:
        with self._lock, self._session() as con:
            cur = con.execute("DELETE FROM fingerprints WHERE id_hex=?", (str(id_hex).upper(),))
            return cur.rowcount > 0

    # ------------------------------------------------------------------ 嵌入记录
    def log_embed(
        self,
        id_hex: str,
        src_path: str,
        dst_path: str,
        psnr_value: float | None = None,
        blocks: int | None = None,
        verified: bool = False,
        batch: str = "",
        src_sha256: str = "",
        dst_sha256: str = "",
    ) -> None:
        """记一次交付：时间 + 批次号 + 源图/输出图 SHA-256。

        指纹本身只能证明"指纹属于谁"，这三个字段用于证明"文件是哪一份"。
        """
        with self._lock, self._session() as con:
            con.execute(
                "INSERT INTO embeds(id_hex, src_path, dst_path, embedded_at, psnr, blocks,"
                " verified, batch, src_sha256, dst_sha256) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    str(id_hex).upper(),
                    str(src_path),
                    str(dst_path),
                    _now(),
                    float(psnr_value) if psnr_value is not None else None,
                    int(blocks) if blocks is not None else None,
                    1 if verified else 0,
                    str(batch or ""),
                    str(src_sha256 or ""),
                    str(dst_sha256 or ""),
                ),
            )

    def embeds_for(self, id_hex: str, limit: int = 200) -> list[dict]:
        with self._lock, self._session() as con:
            rows = con.execute(
                "SELECT * FROM embeds WHERE id_hex=? ORDER BY id DESC LIMIT ?",
                (str(id_hex).upper(), int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    def embed_count(self) -> int:
        with self._lock, self._session() as con:
            return int(con.execute("SELECT COUNT(*) FROM embeds").fetchone()[0])

    def stats(self) -> dict:
        with self._lock, self._session() as con:
            fps = int(con.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0])
            embs = int(con.execute("SELECT COUNT(*) FROM embeds").fetchone()[0])
            ok = int(con.execute("SELECT COUNT(*) FROM embeds WHERE verified=1").fetchone()[0])
        return {"fingerprints": fps, "embeds": embs, "verified": ok, "db": str(self.path)}

    # ------------------------------------------------------------------ 导出
    def export_csv(self, path: str | Path) -> Path:
        path = Path(path)
        rows = self.list_all()
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=[
                    "id_hex", "short_code", "identifier", "sha256", "created_at",
                    "note", "key_fp", "embeds",
                ],
                extrasaction="ignore",
            )
            writer.writeheader()
            for r in rows:
                writer.writerow(r)
        return path

    def export_embeds_csv(self, path: str | Path) -> Path:
        """导出全部交付记录（含批次号、时间、源图/输出图 SHA-256）。

        指纹库那份 CSV 一行一个买家，只有嵌入条数；批次属于**每一次交付**，
        因此单独导出这张明细表。
        """
        path = Path(path)
        with self._lock, self._session() as con:
            rows = [
                dict(r)
                for r in con.execute(
                    """
                    SELECT e.id_hex, f.short_code, f.identifier, e.batch, e.embedded_at,
                           e.src_path, e.dst_path, e.src_sha256, e.dst_sha256,
                           e.psnr, e.blocks, e.verified
                    FROM embeds e LEFT JOIN fingerprints f ON f.id_hex = e.id_hex
                    ORDER BY e.id
                    """
                ).fetchall()
            ]
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=[
                    "id_hex", "short_code", "identifier", "batch", "embedded_at",
                    "src_path", "dst_path", "src_sha256", "dst_sha256",
                    "psnr", "blocks", "verified",
                ],
                extrasaction="ignore",
            )
            writer.writeheader()
            for r in rows:
                writer.writerow(r)
        return path

        path = Path(path)
        rows = self.list_all()
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=[
                    "id_hex", "short_code", "identifier", "sha256", "created_at",
                    "note", "key_fp", "embeds",
                ],
                extrasaction="ignore",
            )
            writer.writeheader()
            for r in rows:
                writer.writerow(r)
        return path
