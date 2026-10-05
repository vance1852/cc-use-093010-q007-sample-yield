"""统计资料质量服务的角色权限层。

角色：

- reporter（报送主体操作员）：登记冻结规则、导入报送记录；
- analyst（分析员）：运行质量分析、登记旧版本分析；
- approver（审批人）：发布与撤销放行决定；
- auditor（审计员）：读取报告与审计轨迹；
- admin（管理员）：拥有全部权限并管理账号。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

ROLES = {"reporter", "analyst", "approver", "auditor", "admin"}
PERMISSIONS = {
    "reporter": {"read", "policy.publish", "record.import", "record.revoke"},
    "analyst": {"read", "analysis.run", "legacy.register"},
    "approver": {"read", "decision.write", "decision.revoke"},
    "auditor": {"read", "audit.read"},
    "admin": {
        "read", "policy.publish", "record.import", "record.revoke",
        "analysis.run", "legacy.register", "decision.write", "decision.revoke",
        "audit.read", "admin",
    },
}


@dataclass(frozen=True)
class User:
    user_id: str
    role: str
    active: bool


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 80_000).hex()


class Auth:
    def __init__(self, connection: sqlite3.Connection):
        self.db = connection
        self.db.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id TEXT PRIMARY KEY, role TEXT NOT NULL, salt TEXT NOT NULL,
            password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS sessions(
            token TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1)""")
        self.db.commit()

    def create_user(self, user_id: str, password: str, role: str = "reporter") -> User:
        if role not in ROLES or len(password) < 8:
            raise ValueError("invalid role or password")
        salt = secrets.token_hex(16)
        try:
            self.db.execute(
                "INSERT INTO users VALUES(?,?,?,?,1,?)",
                (user_id, role, salt, _hash(password, salt),
                 datetime.now(timezone.utc).isoformat()),
            )
            self.db.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"user already exists: {user_id}") from exc
        return User(user_id, role, True)

    def login(self, user_id: str, password: str) -> str:
        row = self.db.execute(
            "SELECT role,salt,password_hash,active FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if not row or not row[2] or not hmac.compare_digest(_hash(password, row[1]), row[2]):
            raise PermissionError("invalid credentials")
        token = secrets.token_urlsafe(24)
        self.db.execute(
            "INSERT INTO sessions VALUES(?,?,datetime('now','+8 hours'),1)", (token, user_id)
        )
        self.db.commit()
        return token

    def current(self, token: str) -> User:
        row = self.db.execute("""SELECT u.user_id,u.role,u.active,s.active,s.expires_at
            FROM sessions s JOIN users u ON u.user_id=s.user_id
            WHERE s.token=?""", (token,)).fetchone()
        if not row:
            raise PermissionError("missing or invalid session token")
        if not row[2] or not row[3]:
            raise PermissionError("session revoked")
        if datetime.fromisoformat(row[4]).replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
            raise PermissionError("session expired")
        return User(row[0], row[1], True)

    def require(self, token: str, permission: str) -> User:
        user = self.current(token)
        if permission not in PERMISSIONS[user.role]:
            raise PermissionError(f"role {user.role} lacks permission {permission}")
        return user

    def deactivate(self, user_id: str) -> None:
        self.db.execute("UPDATE users SET active=0 WHERE user_id=?", (user_id,))
        self.db.execute("UPDATE sessions SET active=0 WHERE user_id=?", (user_id,))
        self.db.commit()
