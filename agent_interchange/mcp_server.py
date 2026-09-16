"""
MCP server for AVEL — exposes InterchangeClient's verify()/
issue_challenge()/verify_challenge()/get_verdict() as MCP tools, so any
MCP-compatible agent (Claude Code, Claude Desktop, or another MCP client)
can ask for an identity/reputation verdict on a transaction without
hand-rolling HTTP calls or signing.

One server instance represents ONE agent identity, bound to a single
private key read from env at startup — never exposed as a tool argument
the model can see or set, same principle already applied to
InterchangeTool in tools.py (see its docstring). That identity's address
is derived once at startup and used automatically wherever `verify()`/
`issue_challenge()`/`verify_challenge()`/`get_verdict()` need "my own"
address — the model only ever supplies the OTHER party's details and the
transaction terms.

Run (stdio transport, the MCP convention for a locally-launched server) —
either the console script installed with the package, or the module
directly, both equivalent:
    AVEL_BASE_URL=https://aisrail.fly.dev \
    AVEL_PRIVATE_KEY=0x... \
    uvx agent-interchange
    # or: python -m agent_interchange.mcp_server

`mcp` is a base dependency (not optional) precisely so `uvx
agent-interchange` — how the MCP Registry launches a stdio server — works
without needing an extra applied.

HONESTY NOTE (see kms_provider.py and framework_tools.py in the parent
repo for the same pattern): this module's request/response logic reuses
InterchangeClient, already verified live against a running Interchange
(see agent_tools.py's own smoke test). The FastMCP wiring itself
(decorators, stdio transport, tool schemas) is written against the
`mcp` package's documented public API but could NOT be smoke-tested in
this session's environment — Termux/Android, no Rust toolchain, and
`mcp`'s dependency `rpds-py` needs to build native extensions (maturin)
that fail here with "Failed to determine Android API level", the exact
same constraint already documented in framework_tools.py for langchain-
core/crewai/autogen-core. Verify with a real `pip install
agent-interchange[mcp]` and an MCP client before relying on this in
production.
"""
import os
from typing import Optional

from eth_account import Account

from .client import InterchangeClient

AVEL_BASE_URL = os.environ.get("AVEL_BASE_URL", "https://aisrail.fly.dev")
AVEL_PRIVATE_KEY = os.environ.get("AVEL_PRIVATE_KEY")

if not AVEL_PRIVATE_KEY:
    raise RuntimeError(
        "AVEL_PRIVATE_KEY non impostata — il server MCP rappresenta UNA identita' "
        "agente, la cui chiave serve a firmare ogni verify()/issue_challenge()/"
        "verify_challenge()/get_verdict(). Non viene mai esposta come argomento di "
        "un tool: resta qui, letta una sola volta all'avvio."
    )

_own_address = Account.from_key(AVEL_PRIVATE_KEY).address
_client = InterchangeClient(AVEL_BASE_URL)

from mcp.server.fastmcp import FastMCP  # noqa: E402 — after the fail-fast checks above

mcp = FastMCP("avel")


@mcp.tool()
def verify(
    payee_id: str,
    amount: float,
    currency: str,
    nonce: str = "",
    payee_address: Optional[str] = None,
    payee_signature: Optional[str] = None,
    sub_merchant_address: Optional[str] = None,
    sub_merchant_signature: Optional[str] = None,
    fee_payment_method_id: Optional[str] = None,
) -> dict:
    """Chiedi un verdetto su una transazione dove questa identita' e' chi
    paga (payer). Verifica identita' e reputazione di entrambe le parti
    su qualunque metodo di pagamento — non muove mai il denaro, resta sul
    rail che le due parti gia' usano. Restituisce {"verdict": "APPROVED"
    o "DENIED", "reasons": [...], ...}. Senza fee_payment_method_id la
    risposta chiede come pagare la commissione della verifica (1%,
    minimo 0.50 su carta) prima di rilasciare il verdetto."""
    return _client.verify(
        payer_address=_own_address,
        payee_id=payee_id,
        amount=amount,
        currency=currency,
        private_key=AVEL_PRIVATE_KEY,
        nonce=nonce,
        payee_address=payee_address,
        payee_signature=payee_signature,
        sub_merchant_address=sub_merchant_address,
        sub_merchant_signature=sub_merchant_signature,
        fee_payment_method_id=fee_payment_method_id,
    )


@mcp.tool()
def issue_challenge(payee_id: str, amount: float, currency: str, ttl_seconds: int = 600) -> dict:
    """Emetti un challenge di pagamento vincolante dove questa identita'
    e' chi riceve (payee, modello B-driven stile x402/MPP): fissi tu
    prezzo e termini, firmati, da consegnare fuori banda a chi paga
    (es. un tool A2A), che li accetta con verify_challenge(). Nessuna
    chiamata di rete. Scade dopo ttl_seconds."""
    return _client.issue_challenge(
        payee_address=_own_address,
        payee_id=payee_id,
        amount=amount,
        currency=currency,
        private_key=AVEL_PRIVATE_KEY,
        ttl_seconds=ttl_seconds,
    )


@mcp.tool()
def verify_challenge(
    challenge: dict,
    fee_payment_method_id: Optional[str] = None,
    fee_method: Optional[str] = None,
) -> dict:
    """Accetta un challenge emesso da un'altra identita' (vedi
    issue_challenge) dove questa identita' e' chi paga: firma gli STESSI
    termini del challenge e chiede il verdetto in un'unica chiamata.
    fee_method="x402" paga la commissione in stablecoin invece che su
    carta."""
    return _client.verify_challenge(
        challenge=challenge,
        payer_address=_own_address,
        private_key=AVEL_PRIVATE_KEY,
        fee_payment_method_id=fee_payment_method_id,
        fee_method=fee_method,
    )


@mcp.tool()
def get_verdict(payee_address: str, nonce: str) -> dict:
    """Leggi l'esito di UNA transazione specifica (payee_address+nonce,
    gli stessi usati in verify()/issue_challenge()) dove questa identita'
    e' il pagante o il ricevente — non serve aver chiamato tu stesso
    verify(). Fallisce con 403 se questa identita' non e' parte di quella
    transazione, 404 se non esiste alcun verdetto registrato per quella
    coppia payee_address/nonce."""
    return _client.get_verdict(
        payee_address=payee_address,
        nonce=nonce,
        address=_own_address,
        private_key=AVEL_PRIVATE_KEY,
    )


def main() -> None:
    """Entry point for the `agent-interchange` console script (also runnable
    via `uvx agent-interchange` once published, or `python -m
    agent_interchange.mcp_server`)."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
