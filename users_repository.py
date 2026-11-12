"""
Stage 10 - user account data access (manager only).

All SQL for accounts lives here. Pages never write SQL directly.

Rules that matter in a shared counter PC:
  * Only a manager may create / deactivate an account or reset a password.
  * A new account ALWAYS starts with must_change_password = 1, so the person
    must set their own password on first login. The manager's temporary
    password is therefore short-lived.
  * Passwords are hashed with the existing PBKDF2-HMAC-SHA256 helper in
    database.py. No plaintext password is stored or returned.
  * A manager cannot deactivate their own account, and the system refuses to
    deactivate the LAST active manager (that would lock everyone out of the
    management screens permanently).
  * Deactivation is a status change, never a DELETE: sales_transactions and
    stock transactions keep their created_by / voided_by foreign keys valid.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Any

import database
import permissions
from permissions import ROLE_CASHIER, ROLE_MANAGER

MAX_USERNAME_LENGTH = 40
MAX_NAME_LENGTH = 80

USER_COLUMNS = (
    "user_id", "username", "full_name", "role", "is_active",
    "must_change_password", "created_at", "last_login_at",
)


def validate_username(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        raise ValueError("Username is required.")
    if len(text) > MAX_USERNAME_LENGTH:
        raise ValueError(
            f"Username must be {MAX_USERNAME_LENGTH} characters or fewer."
        )
    if any(char.isspace() for char in text):
        raise ValueError("Username cannot contain spaces.")
    return text


def validate_full_name(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        raise ValueError("Full name is required.")
    if len(text) > MAX_NAME_LENGTH:
        raise ValueError(
            f"Full name must be {MAX_NAME_LENGTH} characters or fewer."
        )
    return text


def validate_role(value: Any) -> str:
    text = str(value if value is not None else "").strip().lower()
    if text not in permissions.ROLES:
        raise ValueError("Role must be Manager or Cashier.")
    return text


def list_users(
    db_path: Any = None, actor: Any = None
) -> list[dict[str, Any]]:
    """Return every account WITHOUT password hashes, managers first.

    Stage 10: the READ is guarded too, so a stray call from a cashier screen
    cannot enumerate the shop's accounts. No password hash is ever selected.
    """
    permissions.require(actor, permissions.CAP_MANAGE_USERS)
    columns = ", ".join(USER_COLUMNS)
    with closing(database.get_connection(db_path)) as conn:
        rows = conn.execute(
            f"""
            SELECT {columns} FROM users
            ORDER BY CASE role WHEN ? THEN 0 ELSE 1 END, username COLLATE NOCASE
            """,
            (ROLE_MANAGER,),
        ).fetchall()
        return [dict(row) for row in rows]


def count_active_managers(db_path: Any = None) -> int:
    with closing(database.get_connection(db_path)) as conn:
        return int(
            conn.execute(
                """
                SELECT COUNT(*) FROM users
                WHERE is_active = 1 AND role = ?
                """,
                (ROLE_MANAGER,),
            ).fetchone()[0]
        )


def create_user(
    username: Any,
    password: Any,
    full_name: Any,
    role: Any,
    db_path: Any = None,
    actor: Any = None,
) -> int:
    """Create one account. Returns the new user_id.

    The temporary password is hashed immediately and the account is forced
    to change it at first login.
    """
    permissions.require(actor, permissions.CAP_MANAGE_USERS)
    clean_username = validate_username(username)
    clean_name = validate_full_name(full_name)
    clean_role = validate_role(role)
    database.validate_password_strength(str(password if password is not None else ""))
    hashed = database.hash_password(str(password))
    timestamp = database.local_timestamp()

    with database.transaction(db_path) as conn:
        try:
            cursor = conn.execute(
                """
                INSERT INTO users
                    (username, password_hash, full_name, role, is_active,
                     must_change_password, created_at)
                VALUES (?, ?, ?, ?, 1, 1, ?)
                """,
                (clean_username, hashed, clean_name, clean_role, timestamp),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("That username is already taken.") from exc
        return int(cursor.lastrowid)


def set_user_active(
    user_id: Any, active: Any, db_path: Any = None, actor: Any = None
) -> None:
    """Activate / deactivate an account without deleting any history."""
    permissions.require(actor, permissions.CAP_MANAGE_USERS)
    try:
        key = int(user_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid user reference.") from exc
    flag = 1 if bool(active) else 0

    # Use the same actor resolution as permissions.require(): an explicit
    # actor wins, otherwise the live login session is used. Without this
    # fallback a caller that omits `actor` could deactivate its own account.
    effective = actor if actor is not None else permissions.current_session()
    actor_id = effective.get("user_id") if isinstance(effective, dict) else None
    if not flag and actor_id is not None and int(actor_id) == key:
        raise ValueError("You cannot deactivate your own account.")

    with database.transaction(db_path) as conn:
        row = conn.execute(
            "SELECT role, is_active FROM users WHERE user_id = ?", (key,)
        ).fetchone()
        if row is None:
            raise ValueError("User account was not found.")
        if not flag and row["role"] == ROLE_MANAGER and row["is_active"] == 1:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM users WHERE is_active = 1 AND role = ?",
                (ROLE_MANAGER,),
            ).fetchone()[0]
            if remaining <= 1:
                raise ValueError(
                    "At least one active manager is required. Create another "
                    "manager before deactivating this account."
                )
        conn.execute(
            "UPDATE users SET is_active = ? WHERE user_id = ?", (flag, key)
        )


def reset_password(
    user_id: Any, new_password: Any, db_path: Any = None, actor: Any = None
) -> None:
    """Manager sets a temporary password; the user must change it at login."""
    permissions.require(actor, permissions.CAP_MANAGE_USERS)
    try:
        key = int(user_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid user reference.") from exc
    database.validate_password_strength(
        str(new_password if new_password is not None else "")
    )
    hashed = database.hash_password(str(new_password))
    with database.transaction(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM users WHERE user_id = ?", (key,)
        ).fetchone()
        if row is None:
            raise ValueError("User account was not found.")
        conn.execute(
            """
            UPDATE users
            SET password_hash = ?, must_change_password = 1
            WHERE user_id = ?
            """,
            (hashed, key),
        )


def summary(
    db_path: Any = None, actor: Any = None
) -> dict[str, int]:
    """Small counts for the Users page. Cashiers never reach this page."""
    permissions.require(actor, permissions.CAP_MANAGE_USERS)
    with closing(database.get_connection(db_path)) as conn:
        rows = conn.execute(
            "SELECT role, is_active FROM users"
        ).fetchall()
    return {
        "total": len(rows),
        "active": sum(1 for row in rows if row["is_active"] == 1),
        "managers": sum(1 for row in rows if row["role"] == ROLE_MANAGER),
        "cashiers": sum(1 for row in rows if row["role"] == ROLE_CASHIER),
    }
