"""RABT Ledger Engine — FastAPI service layer.

Internal service: reachable ONLY from the RABT backend (private network / mTLS).
It trusts the caller for *business* authorization (who may release/refund);
it enforces *ledger* correctness (balance, state machine, idempotency, integrity).
"""
import hmac
import os
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ledger_core import (IdempotencyConflict, InsufficientFunds, InvalidState, Ledger,
                         LedgerError, NotFound, ValidationError)

API_KEY = os.environ.get("LEDGER_API_KEY", "")
if len(API_KEY) < 32:
    raise RuntimeError("LEDGER_API_KEY must be set (>= 32 chars)")
ledger = Ledger(os.environ.get("LEDGER_DB_PATH", "ledger.db"),
                commission_bps=int(os.environ.get("COMMISSION_BPS", "1000")))

app = FastAPI(title="RABT Ledger Engine", docs_url=None, redoc_url=None, openapi_url=None)

STATUS = {ValidationError: 422, NotFound: 404, InsufficientFunds: 409,
          InvalidState: 409, IdempotencyConflict: 409}


@app.exception_handler(LedgerError)
async def ledger_error_handler(_, exc: LedgerError):
    return JSONResponse(status_code=STATUS.get(type(exc), 400),
                        content={"error": exc.code, "detail": str(exc)})


def auth(authorization: Annotated[str | None, Header()] = None):
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token.encode(), API_KEY.encode()):
        raise HTTPException(status_code=401, detail="unauthorized")


Idem = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=64)]
SvcId = Annotated[str, Path(min_length=1, max_length=64)]


class DepositIn(BaseModel):
    user_id: str = Field(max_length=64)
    amount: str = Field(max_length=20)
    external_ref: str = Field(max_length=64)  # bank / gateway / agent receipt id


class HoldIn(BaseModel):
    service_id: str = Field(max_length=64)
    customer_id: str = Field(max_length=64)
    engineer_id: str = Field(max_length=64)
    amount: str = Field(max_length=20)


class ResolveIn(BaseModel):
    outcome: str = Field(pattern="^(release|refund)$")


@app.post("/v1/deposits", dependencies=[Depends(auth)])
def deposit(body: DepositIn, idem: Idem):
    return ledger.deposit(body.user_id, body.amount, idem, body.external_ref)


@app.post("/v1/escrow/hold", dependencies=[Depends(auth)])
def hold(body: HoldIn, idem: Idem):
    return ledger.hold(body.service_id, body.customer_id, body.engineer_id, body.amount, idem)


@app.post("/v1/escrow/{service_id}/release", dependencies=[Depends(auth)])
def release(service_id: SvcId, idem: Idem):
    return ledger.release(service_id, idem)


@app.post("/v1/escrow/{service_id}/refund", dependencies=[Depends(auth)])
def refund(service_id: SvcId, idem: Idem):
    return ledger.refund(service_id, idem)


@app.post("/v1/escrow/{service_id}/freeze", dependencies=[Depends(auth)])
def freeze(service_id: SvcId, idem: Idem):
    return ledger.freeze(service_id, idem)


@app.post("/v1/escrow/{service_id}/resolve", dependencies=[Depends(auth)])
def resolve(service_id: SvcId, body: ResolveIn, idem: Idem):
    return ledger.resolve(service_id, body.outcome, idem)


@app.get("/v1/escrow/{service_id}", dependencies=[Depends(auth)])
def get_escrow(service_id: SvcId):
    return ledger.get_escrow(service_id)


@app.get("/v1/accounts/{user_id}/balance", dependencies=[Depends(auth)])
def balance(user_id: SvcId):
    return {"user_id": user_id, "balance": ledger.balance(user_id), "currency": "LYD"}


@app.get("/v1/ledger/verify", dependencies=[Depends(auth)])
def verify():
    return ledger.verify_chain()
