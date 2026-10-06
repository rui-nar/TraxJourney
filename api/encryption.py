"""REST endpoints for zero-knowledge encryption key material (issue #26).

The server stores only opaque ciphertext and wrapped keys; it never holds the
Content Master Key (CMK) in the clear and does no crypto. These endpoints let a
client enable encryption (store the CMK wrapped to this device + a recovery
method) and, on a later session, fetch this device's wrapped CMK to unlock.

Routes:
    POST /api/encryption/enable   — turn on encryption for the account
    GET  /api/encryption/status   — encryption state + this device's wrapped CMK
    POST /api/encryption/recovery/confirm       — the user saved the recovery key shown
    PUT  /api/encryption/recovery/recovery_key  — replace an unconfirmed recovery key
"""
from __future__ import annotations

from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlmodel import select

from api.deps import get_current_user
from models.db import get_session
from models.project_db import DBDeviceKey, DBProject, DBProjectMember, DBRecoveryWrap
from models.user import UserInfo

router = APIRouter(prefix="/api/encryption", tags=["encryption"])


def _uid(user: dict) -> int:
    return int(user["sub"])


# ── Schemas ────────────────────────────────────────────────────────────────────

class DeviceWrapIn(BaseModel):
    public_key: str = Field(description="base64 X25519 device public key")
    label: str = Field(default="", description="human label, e.g. 'Chrome on Windows'")
    wrapped_cmk: str = Field(description="base64 AEAD blob: CMK wrapped to this device")
    ephemeral_public_key: str = Field(description="base64 X25519 ephemeral pubkey")


class RecoveryWrapIn(BaseModel):
    method: Literal["recovery_key", "passphrase", "qna", "escrow"] = Field(
        description="High: 'recovery_key' / 'passphrase' · Medium: 'qna' · Low: 'escrow'")
    wrapped_cmk: str = Field(description="base64 AEAD blob: CMK wrapped under the recovery secret")
    salt: str = Field(description="base64 KDF/HKDF salt")
    kdf_params_json: Optional[str] = Field(
        default=None, description="Argon2id params JSON for method='qna'; null otherwise")


class EnableIn(BaseModel):
    device: DeviceWrapIn
    recovery: RecoveryWrapIn


class DeviceStateOut(BaseModel):
    registered: bool
    approved: bool
    wrapped_cmk: Optional[str] = None
    ephemeral_public_key: Optional[str] = None


class StatusOut(BaseModel):
    enabled: bool
    recovery_methods: List[str]
    # Recovery methods whose one-time secret was never confirmed as saved
    # (only "recovery_key" can be; Decision 16). Additive: older clients ignore it.
    unconfirmed_recovery_methods: List[str] = []
    device: DeviceStateOut


class EnableOut(StatusOut):
    # The recovery wrap just stored, so the client can confirm exactly the key
    # it showed (POST /recovery/confirm). Additive.
    recovery_wrapped_cmk: str


class DeviceRegisterIn(BaseModel):
    public_key: str = Field(description="base64 X25519 public key of the new device")
    label: str = Field(default="", description="human label for the device")


class PendingDeviceOut(BaseModel):
    public_key: str
    label: str
    created_at: float


class DeviceApproveIn(BaseModel):
    public_key: str = Field(description="the pending device's public key")
    wrapped_cmk: str = Field(description="base64 AEAD blob: CMK wrapped to that device")
    ephemeral_public_key: str = Field(description="base64 X25519 ephemeral pubkey")


class RecoveryWrapOut(BaseModel):
    method: str
    wrapped_cmk: str
    salt: str
    kdf_params_json: Optional[str] = None


class RecoveryConfirmIn(BaseModel):
    method: Literal["recovery_key", "passphrase", "qna", "escrow"]
    wrapped_cmk: str = Field(description="the wrap the client showed the key for")


class RecoveryKeyReplaceIn(BaseModel):
    wrapped_cmk: str = Field(description="base64 AEAD blob: CMK wrapped under the new recovery key")
    salt: str = Field(description="base64 HKDF salt")


# ── Endpoints ───────────────────────────────────────────────────────────────────

def _require_enabled(sess, uid: int) -> None:
    ui = sess.get(UserInfo, uid)
    if ui is None or not ui.encryption_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Encryption is not enabled for this account",
        )


@router.post("/enable", response_model=EnableOut,
             status_code=status.HTTP_201_CREATED,
             summary="Enable client-side encryption for the account")
def enable(body: EnableIn, user: dict = Depends(get_current_user)) -> EnableOut:
    uid = _uid(user)
    with get_session() as sess:
        ui = sess.get(UserInfo, uid)
        if ui is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        if ui.encryption_enabled:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Encryption already enabled for this account",
            )
        # Travel companions (issue #106) and E2EE are mutually exclusive:
        # companions could neither read nor write content encrypted under this
        # account's CMK, so block enabling while any owned trip has members.
        has_members = sess.exec(
            select(DBProjectMember)
            .join(DBProject, DBProject.id == DBProjectMember.project_id)
            .where(DBProject.user_info_id == uid)
        ).first() is not None
        if has_members:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Encryption cannot be enabled while your trips have travel "
                       "companions — remove them first.",
            )

        sess.add(DBDeviceKey(
            user_info_id=uid,
            public_key=body.device.public_key,
            label=body.device.label,
            approved=True,  # the enabling device is trusted by definition
            wrapped_cmk=body.device.wrapped_cmk,
            ephemeral_public_key=body.device.ephemeral_public_key,
        ))
        sess.add(DBRecoveryWrap(
            user_info_id=uid,
            method=body.recovery.method,
            wrapped_cmk=body.recovery.wrapped_cmk,
            salt=body.recovery.salt,
            kdf_params_json=body.recovery.kdf_params_json,
            # Only a recovery key is a one-time secret the user may not have
            # saved; it stays unconfirmed until POST /recovery/confirm.
            confirmed=body.recovery.method != "recovery_key",
        ))
        ui.encryption_enabled = True
        sess.add(ui)
        sess.commit()

    return EnableOut(
        enabled=True,
        recovery_methods=[body.recovery.method],
        unconfirmed_recovery_methods=(
            [body.recovery.method] if body.recovery.method == "recovery_key" else []),
        recovery_wrapped_cmk=body.recovery.wrapped_cmk,
        device=DeviceStateOut(
            registered=True,
            approved=True,
            wrapped_cmk=body.device.wrapped_cmk,
            ephemeral_public_key=body.device.ephemeral_public_key,
        ),
    )


@router.get("/status", response_model=StatusOut,
            summary="Encryption state and this device's wrapped CMK")
def get_status(
    device_public_key: Optional[str] = None,
    user: dict = Depends(get_current_user),
) -> StatusOut:
    uid = _uid(user)
    with get_session() as sess:
        ui = sess.get(UserInfo, uid)
        wraps = sess.exec(
            select(DBRecoveryWrap).where(DBRecoveryWrap.user_info_id == uid)
        ).all()
        device = DeviceStateOut(registered=False, approved=False)
        if device_public_key:
            row = sess.exec(
                select(DBDeviceKey).where(
                    DBDeviceKey.user_info_id == uid,
                    DBDeviceKey.public_key == device_public_key,
                )
            ).first()
            if row is not None:
                device = DeviceStateOut(
                    registered=True,
                    approved=row.approved,
                    wrapped_cmk=row.wrapped_cmk,
                    ephemeral_public_key=row.ephemeral_public_key,
                )
        return StatusOut(
            enabled=bool(ui and ui.encryption_enabled),
            recovery_methods=[r.method for r in wraps],
            unconfirmed_recovery_methods=[r.method for r in wraps if not r.confirmed],
            device=device,
        )


# ── Cross-device approval (Bitwarden trusted-device model) ──────────────────────

@router.post("/devices/register", response_model=DeviceStateOut,
             summary="Register a new device as pending approval")
def register_device(body: DeviceRegisterIn,
                    user: dict = Depends(get_current_user)) -> DeviceStateOut:
    """A new device posts its public key; it stays unapproved until a trusted
    device wraps the CMK to it. Idempotent — re-registering returns current state."""
    uid = _uid(user)
    with get_session() as sess:
        ui = sess.get(UserInfo, uid)
        if ui is None or not ui.encryption_enabled:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Encryption is not enabled for this account",
            )
        row = sess.exec(
            select(DBDeviceKey).where(
                DBDeviceKey.user_info_id == uid,
                DBDeviceKey.public_key == body.public_key,
            )
        ).first()
        if row is None:
            row = DBDeviceKey(
                user_info_id=uid,
                public_key=body.public_key,
                label=body.label,
                approved=False,
            )
            sess.add(row)
            sess.commit()
            sess.refresh(row)
        return DeviceStateOut(
            registered=True,
            approved=row.approved,
            wrapped_cmk=row.wrapped_cmk,
            ephemeral_public_key=row.ephemeral_public_key,
        )


@router.get("/recovery/{method}", response_model=RecoveryWrapOut,
            summary="Fetch the CMK wrapped under a recovery method (for unlock)")
def get_recovery_wrap(method: str,
                      user: dict = Depends(get_current_user)) -> RecoveryWrapOut:
    """Returns the opaque recovery wrap so a deviceless client can unlock with the
    user's recovery secret. The blob is useless without that secret (zero-knowledge)."""
    uid = _uid(user)
    with get_session() as sess:
        row = sess.exec(
            select(DBRecoveryWrap).where(
                DBRecoveryWrap.user_info_id == uid,
                DBRecoveryWrap.method == method,
            )
        ).first()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No '{method}' recovery configured",
            )
        return RecoveryWrapOut(
            method=row.method,
            wrapped_cmk=row.wrapped_cmk,
            salt=row.salt,
            kdf_params_json=row.kdf_params_json,
        )


# ── Recovery key confirm or replace (Decision 16) ──────────────────────────────
# The signed-in session is the only authority checked, as for device approval.
# Both writes are compare-and-sets so a stolen session can never overwrite a
# recovery key the user has confirmed saving.

@router.post("/recovery/confirm", status_code=status.HTTP_204_NO_CONTENT,
             summary="Record that the user saved the recovery secret they were shown")
def confirm_recovery(body: RecoveryConfirmIn,
                     user: dict = Depends(get_current_user)) -> None:
    """Confirms only the exact wrap the client showed the key for: if another
    device replaced it meanwhile, the key the user saved no longer unwraps the
    CMK, so nothing is confirmed (409). Confirming twice is a success."""
    uid = _uid(user)
    with get_session() as sess:
        _require_enabled(sess, uid)
        result = sess.execute(
            update(DBRecoveryWrap)
            .where(DBRecoveryWrap.user_info_id == uid,
                   DBRecoveryWrap.method == body.method,
                   DBRecoveryWrap.wrapped_cmk == body.wrapped_cmk)
            .values(confirmed=True)
        )
        sess.commit()
        if result.rowcount == 0:
            exists = sess.exec(
                select(DBRecoveryWrap.id).where(
                    DBRecoveryWrap.user_info_id == uid,
                    DBRecoveryWrap.method == body.method,
                )
            ).first() is not None
            if not exists:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"No '{body.method}' recovery configured",
                )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This is no longer your recovery key; nothing was confirmed",
            )


@router.put("/recovery/recovery_key", response_model=RecoveryWrapOut,
            summary="Replace a recovery key that was never confirmed as saved")
def replace_recovery_key(body: RecoveryKeyReplaceIn,
                         user: dict = Depends(get_current_user)) -> RecoveryWrapOut:
    """Overwrites the existing row only while it is unconfirmed, and leaves it
    unconfirmed: the client confirms once the user has saved the new key."""
    uid = _uid(user)
    with get_session() as sess:
        _require_enabled(sess, uid)
        result = sess.execute(
            update(DBRecoveryWrap)
            .where(DBRecoveryWrap.user_info_id == uid,
                   DBRecoveryWrap.method == "recovery_key",
                   DBRecoveryWrap.confirmed == False)  # noqa: E712 (SQL boolean)
            .values(wrapped_cmk=body.wrapped_cmk, salt=body.salt)
        )
        sess.commit()
        if result.rowcount == 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="No unconfirmed recovery key to replace",
            )
    # What this request wrote, never a read-back: another device's replace may
    # have committed since, and the client must confirm the key it shows.
    return RecoveryWrapOut(
        method="recovery_key", wrapped_cmk=body.wrapped_cmk, salt=body.salt)


@router.get("/devices/pending", response_model=List[PendingDeviceOut],
            summary="List this account's devices awaiting approval")
def list_pending_devices(
        user: dict = Depends(get_current_user)) -> List[PendingDeviceOut]:
    uid = _uid(user)
    with get_session() as sess:
        rows = sess.exec(
            select(DBDeviceKey).where(
                DBDeviceKey.user_info_id == uid,
                DBDeviceKey.approved == False,  # noqa: E712 (SQL boolean)
            )
        ).all()
        return [
            PendingDeviceOut(
                public_key=r.public_key, label=r.label, created_at=r.created_at)
            for r in rows
        ]


@router.post("/devices/approve", response_model=DeviceStateOut,
             summary="Approve a pending device by wrapping the CMK to it")
def approve_device(body: DeviceApproveIn,
                   user: dict = Depends(get_current_user)) -> DeviceStateOut:
    """Called by an already-trusted device (the only kind that holds the CMK and
    can produce a valid wrap). The server just stores the supplied wrap."""
    uid = _uid(user)
    with get_session() as sess:
        row = sess.exec(
            select(DBDeviceKey).where(
                DBDeviceKey.user_info_id == uid,
                DBDeviceKey.public_key == body.public_key,
            )
        ).first()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
        row.approved = True
        row.wrapped_cmk = body.wrapped_cmk
        row.ephemeral_public_key = body.ephemeral_public_key
        sess.add(row)
        sess.commit()
        return DeviceStateOut(
            registered=True, approved=True,
            wrapped_cmk=row.wrapped_cmk,
            ephemeral_public_key=row.ephemeral_public_key,
        )
