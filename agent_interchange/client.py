"""
InterchangeClient — Python SDK.

Il metodo che serve più spesso è `verify()`: chiede un verdetto su una
transazione fra due agenti, su qualunque metodo di pagamento, e
restituisce la provvigione dovuta per il controllo — dovuta anche su un
verdetto DENIED, se la firma di chi chiama è comunque valida (il prezzo
è del controllo, non dell'esito). Il denaro fra le parti non passa mai
da qui.

Tre modelli, tutti su `POST /verify`:
- **A dichiara, B conferma** (`verify()`): A propone i termini, B (se
  dichiara un indirizzo) deve provarli con la stessa firma di A.
- **B-driven** (`issue_challenge()` + `verify_challenge()`, protocollo
  `challenge`): è B a fissare prezzo e termini e a firmarli per primo —
  più vicino a come funzionano x402/MPP davvero.
- **Verdetto bidirezionale** (`get_verdict()`): entrambe le parti
  possono leggere l'esito di una transazione specifica, non solo chi ha
  chiamato `verify()`.

Caso marketplace: `verify()` accetta anche `sub_merchant_address`/
`sub_merchant_signature` quando il payee dichiarato è una vetrina e non
chi fornisce davvero il prodotto/servizio.

`pay()`, `get_balance()`, `deposit()` e `withdraw()` appartengono agli
Agent Vaults, il modello precedente in cui il servizio custodiva un
saldo interno. Sono **spenti**: il server risponde `410` a quegli
endpoint. Il codice resta perché è riattivabile con
`AGENT_VAULTS_ENABLED=true`, ma il prodotto non custodisce più denaro —
custodire fondi altrui, anche per un istante, significa licenze di money
transmission e giurisdizioni diverse in ogni paese.

Standalone by design: this package doesn't import anything from the
Interchange server's own codebase (interchange/*), so it can be
installed and used independently of it — only a running Interchange
instance's base_url is needed at runtime.
"""
import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct

from .exceptions import (
    InsufficientBalanceError,
    InterchangeAPIError,
    RequestConflictError,
)


def _derive_request_id(task_id: str, **fields: Any) -> str:
    """Deterministic id binding task_id to THESE exact call parameters —
    mirrors the Interchange's own P1 binding (interchange/security.py).
    Calling pay() again with the same task_id AND the same arguments (a
    genuine retry) reproduces the same id, so the server treats it as a
    safe idempotent replay instead of a new transaction. Calling it again
    with the same task_id but DIFFERENT arguments (a second, distinct
    payment within the same agent task) produces a different id, so it
    is never mistaken for a replay of the first one.
    """
    canonical = task_id + "|" + "|".join(f"{k}={fields[k]}" for k in sorted(fields))
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    return f"{task_id}:{digest}"


def _compute_binding_message(agent_request_id: str, **fields: Any) -> str:
    """DEVE restare identico, byte per byte, a
    interchange.security.compute_binding_message — è il testo che
    l'agente firma per provare di controllare payer_ref/agent_ref
    (trust-on-first-use, interchange/agent_identity.py). Duplicato qui
    (non importato) perché questo SDK è standalone by design — non
    importa nulla dal codice del server Interchange."""
    return agent_request_id + "|" + "|".join(f"{k}={fields[k]}" for k in sorted(fields))


def _sign(private_key: str, message: str) -> str:
    signed = Account.sign_message(encode_defunct(text=message), private_key=private_key)
    return "0x" + signed.signature.hex().removeprefix("0x")


def build_counterparty_message(payer_address: str, payee_id: str, amount: float, currency: str, nonce: str = "") -> str:
    """Testo canonico che B deve firmare per provare la propria identità
    (verifica simmetrica) — IDENTICO byte per byte a quello che A firma
    per sé (vedi interchange.verification.build_native_message sul
    server), così una sola funzione di verifica basta per entrambe le
    parti. Esposta pubblicamente perché chi negozia con B — un tool A2A,
    un servizio terzo — deve poter calcolare questo messaggio senza
    leggere il sorgente del server Interchange."""
    return f"verify:native:{payer_address.lower()}:{payee_id}:{amount}:{currency.upper()}:{nonce}"


def build_challenge_message(payee_address: str, payee_id: str, amount: float, currency: str,
                            nonce: str, expires_at: int) -> str:
    """Testo che B firma per emettere un challenge di pagamento vincolante
    (protocol='challenge', stile x402/MPP: è il venditore a fissare i
    termini). IDENTICO byte per byte a
    interchange.verification.build_challenge_message sul server."""
    return f"challenge:{payee_address.lower()}:{payee_id}:{amount}:{currency.upper()}:{nonce}:{expires_at}"


def build_fulfillment_message(payer_address: str, payee_address: str, amount: float, currency: str,
                              nonce: str, expires_at: int) -> str:
    """Testo che A firma per accettare il challenge di B, legandosi agli
    STESSI termini. IDENTICO byte per byte a
    interchange.verification.build_fulfillment_message sul server."""
    return f"fulfill:{payer_address.lower()}:{payee_address.lower()}:{amount}:{currency.upper()}:{nonce}:{expires_at}"


def build_sub_merchant_message(payer_address: str, payee_id: str, amount: float, currency: str,
                               nonce: str, sub_merchant_address: str) -> str:
    """Testo che un sub-merchant firma per provare di essere davvero chi
    fornisce prodotto/servizio quando il payee dichiarato è una vetrina
    (caso marketplace). IDENTICO byte per byte a
    interchange.verification.build_sub_merchant_message sul server."""
    return f"submerchant:{sub_merchant_address.lower()}:{payer_address.lower()}:{payee_id}:{amount}:{currency.upper()}:{nonce}"


@dataclass
class PaymentResult:
    """Response from /pay. `success` is a convenience for the common
    case — False only when the risk engine itself denied the transfer
    (a risk-score DENY, not an insufficient-balance one, which pay()
    raises InsufficientBalanceError for instead)."""

    decision: str  # ALLOW, REVIEW, or DENY
    risk_score: int
    reasons: List[str] = field(default_factory=list)
    fee_units: int = 0
    payer_asset_id: Optional[str] = None
    payee_asset_id: Optional[str] = None
    fx_rate: Optional[float] = None
    payee_credited_units: Optional[int] = None
    payer_balance_units: Optional[int] = None
    payee_balance_units: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.decision != "DENY"


_RESULT_FIELDS = (
    "decision",
    "risk_score",
    "reasons",
    "fee_units",
    "payer_asset_id",
    "payee_asset_id",
    "fx_rate",
    "payee_credited_units",
    "payer_balance_units",
    "payee_balance_units",
)


class InterchangeClient:
    """Sync client for a running Interchange instance.

    Usage:
        client = InterchangeClient("https://aisrail.fly.dev")
        result = client.verify(
            payer_address=my_address,
            payee_id="merchant-42",
            amount=25.0,
            currency="EUR",
            private_key=my_key,
            fee_payment_method_id="pm_...",   # per pagare la verifica su carta
        )
    """

    def __init__(self, base_url: str = "https://aisrail.fly.dev", timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "InterchangeClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def verify(
        self,
        payer_address: str,
        payee_id: str,
        amount: float,
        currency: str,
        private_key: str,
        nonce: str = "",
        payee_address: Optional[str] = None,
        payee_signature: Optional[str] = None,
        sub_merchant_address: Optional[str] = None,
        sub_merchant_signature: Optional[str] = None,
        fee_payment_method_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Chiede un verdetto sulla transazione, firmandola con `native`
        (EIP-191 sul testo canonico) — la via che funziona per chiunque,
        senza dipendere dal protocollo di pagamento usato.

        Il denaro fra payer e payee NON passa da qui: resta sul rail che
        le due parti già usano. Questa chiamata dice solo se è sicuro
        procedere, e costa la provvigione (1% dell'importo verificato,
        minimo 0,50 su carta) — **dovuta anche su un verdetto DENIED**,
        se la firma di chi chiama è comunque valida: è il prezzo del
        controllo, non dell'esito. Gratuita solo quando la firma stessa
        non è verificabile.

        `fee_payment_method_id` è il metodo con cui si paga LA VERIFICA,
        non la transazione. Senza, il server risponde 402 e non rilascia
        il verdetto: la verifica si paga. Chi salda non deve essere per
        forza il pagante: anche B (o il sub-merchant, se dichiarato e
        verificato) può pagare — mai un terzo qualunque — firmando
        un'autorizzazione EIP-3009 verso la tesoreria del servizio (vedi
        `context.fee_authorization` nel 402 di risposta).

        `payee_address`/`payee_signature`: verifica simmetrica di B. Se
        B dichiara un indirizzo, deve anche provare di controllarlo —
        altrimenti il verdetto è negato. La firma va calcolata da B (o
        da chi negozia per suo conto, es. un tool A2A) su
        `build_counterparty_message(payer_address, payee_id, amount,
        currency, nonce)`, e passata qui già pronta: questo client non
        possiede la chiave di B, si limita a inoltrarla.

        `sub_merchant_address`/`sub_merchant_signature`: caso marketplace
        — quando `payee_id`/`payee_address` sono una vetrina e non chi
        fornisce davvero il prodotto/servizio. Stesso principio di B: la
        firma va calcolata su `build_sub_merchant_message(payer_address,
        payee_id, amount, currency, nonce, sub_merchant_address)`. Se
        dichiarato, la sua reputazione (`sub_merchant_reputation` nella
        risposta) entra nel verdetto tanto quanto quella di B.

        Per gli altri protocolli (x402, ap2, mpp, checkout) la prova del
        pagante è quella che l'agente ha già prodotto per il pagamento —
        si passa direttamente in `context`, senza firmare due volte: vedi
        scripts/protocol_adapters_demo.py per un esempio per ciascuno.
        Per il modello B-driven, dove è B a fissare i termini (stile
        x402/MPP), vedi `issue_challenge()`/`verify_challenge()` invece
        di questo metodo.

        Restituisce il corpo della risposta. `verdict` vale "APPROVED" o
        "DENIED"; una risposta con `verdict_withheld: True` significa che
        la verifica è passata ma la provvigione non è stata incassata,
        quindi il verdetto è trattenuto.
        """
        message = build_counterparty_message(payer_address, payee_id, amount, currency, nonce)
        body: Dict[str, Any] = {
            "protocol": "native",
            "payer": {"address": payer_address, "signature": _sign(private_key, message)},
            "payee": {"id": payee_id, "address": payee_address, "signature": payee_signature},
            "amount": amount,
            "currency": currency,
            "context": {"nonce": nonce},
        }
        if sub_merchant_address:
            body["sub_merchant"] = {"address": sub_merchant_address, "signature": sub_merchant_signature}
        if fee_payment_method_id:
            body["fee_payment"] = {"payment_method_id": fee_payment_method_id}

        resp = self._http.post(f"{self.base_url}/verify", json=body)
        # 402 non è un errore da sollevare: è una risposta prevista dal
        # protocollo ("la verifica è passata, ora paga la provvigione"),
        # e chi integra deve poterla leggere per completare il giro.
        if resp.status_code in (200, 402):
            return resp.json()
        raise InterchangeAPIError(resp.status_code, _error_detail(resp))

    def issue_challenge(
        self,
        payee_address: str,
        payee_id: str,
        amount: float,
        currency: str,
        private_key: str,
        ttl_seconds: int = 600,
    ) -> Dict[str, Any]:
        """Lato B (venditore): emette un challenge di pagamento
        vincolante — è B a fissare prezzo e termini, non A a dichiararli
        (modello B-driven, stile x402/MPP; vedi `verify()` per il modello
        "A dichiara, B conferma"). Nessuna chiamata di rete: il dizionario
        risultante va consegnato ad A fuori banda (es. un tool A2A), che
        lo passa a `verify_challenge()`.

        `ttl_seconds` fissa la scadenza: un challenge non accettato entro
        quella finestra non è più valido. Il challenge è comunque valido
        una sola volta per la transazione esatta che lo accetta — un
        ritentativo identico (necessario per completare il pagamento
        della commissione) resta ammesso, un riuso con termini diversi no."""
        nonce = "0x" + secrets.token_hex(16)
        expires_at = int(time.time()) + ttl_seconds
        message = build_challenge_message(payee_address, payee_id, amount, currency, nonce, expires_at)
        return {
            "payee_id": payee_id,
            "payee_address": payee_address,
            "payee_signature": _sign(private_key, message),
            "amount": amount,
            "currency": currency,
            "nonce": nonce,
            "expires_at": expires_at,
        }

    def verify_challenge(
        self,
        challenge: Dict[str, Any],
        payer_address: str,
        private_key: str,
        fee_payment_method_id: Optional[str] = None,
        fee_method: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Lato A (acquirente): accetta il challenge emesso da B (vedi
        `issue_challenge()`) firmando gli STESSI termini, e chiede il
        verdetto in un'unica chiamata. Solleva `InterchangeAPIError` se
        il challenge non ha la forma attesa (manca un campo).

        `fee_method="x402"` chiede di pagare la commissione in stablecoin
        invece che su carta — a differenza di `protocol="x402"`, per
        `protocol="challenge"` il server non lo sceglie di default, va
        dichiarato esplicitamente. Con `fee_method="x402"` la risposta
        potrebbe essere un 402 con i parametri da firmare (vedi
        `scripts/multi_protocol_verify_demo.py`, `pay_x402_fee`, per il
        giro completo)."""
        message = build_fulfillment_message(
            payer_address, challenge["payee_address"], challenge["amount"], challenge["currency"],
            challenge["nonce"], challenge["expires_at"],
        )
        body: Dict[str, Any] = {
            "protocol": "challenge",
            "payer": {"address": payer_address, "signature": _sign(private_key, message)},
            "payee": {
                "id": challenge["payee_id"], "address": challenge["payee_address"],
                "signature": challenge["payee_signature"],
            },
            "amount": challenge["amount"], "currency": challenge["currency"],
            "context": {"nonce": challenge["nonce"], "expires_at": challenge["expires_at"]},
        }
        if fee_method:
            body["fee_method"] = fee_method
        if fee_payment_method_id:
            body["fee_payment"] = {"payment_method_id": fee_payment_method_id}
        resp = self._http.post(f"{self.base_url}/verify", json=body)
        if resp.status_code in (200, 402):
            return resp.json()
        raise InterchangeAPIError(resp.status_code, _error_detail(resp))

    def get_verdict(self, payee_address: str, nonce: str, address: str, private_key: str) -> Dict[str, Any]:
        """Verdetto bidirezionale: legge l'esito di UNA transazione
        specifica — identificata da `payee_address`+`nonce`, gli stessi
        usati in `verify()`/`issue_challenge()` — utilizzabile da
        entrambe le parti coinvolte, non solo da chi ha chiamato
        `verify()`. Richiede una firma che provi di controllare
        `address`: a differenza di `get_agent_reputation` (storico
        aggregato, pubblico), il dettaglio di una singola transazione fra
        due indirizzi specifici non lo è. Solleva `InterchangeAPIError`
        se `address` non è il pagante né il ricevente di quella
        transazione (403), o se non esiste alcun verdetto registrato per
        quel `payee_address`/`nonce` (404)."""
        message = f"verdict:{payee_address.lower()}:{nonce}"
        resp = self._http.get(f"{self.base_url}/ais/verdict", params={
            "payee_address": payee_address, "nonce": nonce,
            "address": address, "signature": _sign(private_key, message),
        })
        if resp.status_code == 200:
            return resp.json()
        raise InterchangeAPIError(resp.status_code, _error_detail(resp))

    def pay(
        self,
        payer_ref: str,
        payee_ref: str,
        amount: float,
        private_key: str,
        currency: str = "USDC",
        task_id: Optional[str] = None,
    ) -> PaymentResult:
        """SPENTO: gli Agent Vaults sono disattivati, questo endpoint
        risponde 410. Il servizio non custodisce piu' denaro — usa
        verify(). Riattivabile lato server con AGENT_VAULTS_ENABLED=true.

        Pay payee_ref from payer_ref's Interchange balance.

        private_key: payer_ref's own EVM private key, used to sign this
        exact transaction (EIP-191) — required since the Interchange
        added trust-on-first-use wallet authentication on /pay: the
        first authenticated call for a given payer_ref binds it
        permanently to that key's address, every later call with the
        same payer_ref must sign with the SAME key or is rejected (403).

        task_id identifies the agent task/run making this call — pass
        your own framework's task/run id for safe idempotent retries (see
        _derive_request_id); if omitted, a random one-shot id is
        generated, which still works but loses the retry-safety property
        since a real retry would generate a different id each time.

        Raises:
            InsufficientBalanceError: payer's balance can't cover amount + fee.
            RequestConflictError: HTTP 409 — see the exception's docstring.
            InterchangeAPIError: any other non-2xx response (401 on a bad
                signature, 403 if payer_ref is already bound to a
                different address).
        """
        task_id = task_id or f"anon-{uuid.uuid4().hex}"
        agent_request_id = _derive_request_id(
            task_id, payer_ref=payer_ref, payee_ref=payee_ref, amount=amount, currency=currency
        )
        binding_message = _compute_binding_message(
            agent_request_id,
            payer_ref=payer_ref,
            payee_ref=payee_ref,
            amount=amount,
            asset_id=currency,
            payee_asset_id=currency,
        )
        account = Account.from_key(private_key)
        body = {
            "agent_request_id": agent_request_id,
            "payer_ref": payer_ref,
            "payee_ref": payee_ref,
            "amount": amount,
            "asset_id": currency,
            "payer_address": account.address,
            "payer_signature": _sign(private_key, binding_message),
        }

        try:
            resp = self._http.post(f"{self.base_url}/pay", json=body)
        except httpx.HTTPError as e:
            raise InterchangeAPIError(0, f"could not reach Interchange at {self.base_url}: {e}")

        if resp.status_code == 409:
            raise RequestConflictError(_error_detail(resp))
        if resp.status_code >= 400:
            raise InterchangeAPIError(resp.status_code, _error_detail(resp))

        data = resp.json()
        result = PaymentResult(**{k: data.get(k) for k in _RESULT_FIELDS}, raw=data)

        if result.decision == "DENY" and any("insufficient" in r.lower() for r in result.reasons):
            raise InsufficientBalanceError(result.reasons)

        return result

    def get_balance(self, agent_ref: str, private_key: str) -> Dict[str, int]:
        """SPENTO: gli Agent Vaults sono disattivati, questo endpoint
        risponde 410. Il servizio non custodisce piu' denaro — usa
        verify(). Riattivabile lato server con AGENT_VAULTS_ENABLED=true.

        Every currency agent_ref holds, atomic units per asset_id,
        e.g. {"USDC": 500000} (USDC has 6 decimals).

        private_key: same key used with pay()/withdraw() for agent_ref
        — required since /balance now needs a signature too. Allowed
        with ANY key if agent_ref has never been used in pay()/
        withdraw() yet (nothing bound to check against); once bound, a
        different key is rejected (403).
        """
        account = Account.from_key(private_key)
        message = f"balance:{agent_ref}"
        try:
            resp = self._http.get(
                f"{self.base_url}/balance/{agent_ref}",
                params={"address": account.address, "signature": _sign(private_key, message)},
            )
        except httpx.HTTPError as e:
            raise InterchangeAPIError(0, f"could not reach Interchange at {self.base_url}: {e}")

        if resp.status_code >= 400:
            raise InterchangeAPIError(resp.status_code, _error_detail(resp))

        return resp.json()["balances"]

    async def deposit(
        self,
        agent_ref: str,
        amount_usdc: float,
        private_key: str,
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """SPENTO: gli Agent Vaults sono disattivati, questo endpoint
        risponde 410. Il servizio non custodisce piu' denaro — usa
        verify(). Riattivabile lato server con AGENT_VAULTS_ENABLED=true.

        Fund agent_ref's vault with real on-chain USDC via x402, signed
        with the depositing agent's own private key — never the
        Interchange treasury's. Async (unlike pay()/withdraw()) because
        the underlying x402 handshake needs an async HTTP client —
        the same asymmetry as the main repo's agent_tools.py, for the
        same reason.

        Needs the optional `deposit` extra:
            pip install agent-interchange[deposit]
        (pulls in x402 + eth-account — not required for pay()/withdraw()/
        get_balance(), which is why this isn't a hard dependency of the
        base package).

        Raises:
            RequestConflictError: HTTP 409 — see the exception's docstring.
            InterchangeAPIError: any other non-2xx response, including a
                missing x402 install (reported as status_code=0).
        """
        try:
            from eth_account import Account
            from x402 import x402Client
            from x402.http.clients import x402HttpxClient
            from x402.mechanisms.evm import EthAccountSigner
            from x402.mechanisms.evm.exact.register import register_exact_evm_client
        except ImportError as e:
            raise InterchangeAPIError(
                0, f"deposit() requires the 'deposit' extra: pip install agent-interchange[deposit] ({e})"
            )

        task_id = task_id or f"anon-{uuid.uuid4().hex}"
        agent_request_id = _derive_request_id(task_id, agent_ref=agent_ref, amount_usdc=amount_usdc)

        account = Account.from_key(private_key)
        x_client = x402Client()
        register_exact_evm_client(x_client, EthAccountSigner(account))

        url = (
            f"{self.base_url}/deposit?agent_ref={agent_ref}&amount_usdc={amount_usdc}"
            f"&agent_request_id={agent_request_id}"
        )
        try:
            async with x402HttpxClient(x_client) as http:
                resp = await http.post(url)
        except httpx.HTTPError as e:
            raise InterchangeAPIError(0, f"could not reach Interchange at {self.base_url}: {e}")

        if resp.status_code == 409:
            raise RequestConflictError(_error_detail(resp))
        if resp.status_code >= 400:
            raise InterchangeAPIError(resp.status_code, _error_detail(resp))

        return resp.json()

    def withdraw(
        self,
        agent_ref: str,
        amount_usdc: float,
        payout_address: str,
        private_key: str,
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Withdraw agent_ref's internal USDC balance to a real on-chain
        SPENTO: gli Agent Vaults sono disattivati, questo endpoint
        risponde 410. Il servizio non custodisce piu' denaro — usa
        verify(). Riattivabile lato server con AGENT_VAULTS_ENABLED=true.

        wallet. The Interchange's own treasury signs and pays out on
        chain — private_key is only agent_ref's OWN key, proving it
        controls agent_ref (trust-on-first-use, same as pay()); a
        failed on-chain settlement is refunded automatically to
        agent_ref's vault (handled server-side).

        Raises:
            InsufficientBalanceError: agent_ref's balance can't cover amount_usdc.
            RequestConflictError: HTTP 409 — see the exception's docstring.
            InterchangeAPIError: any other non-2xx response (401 on a bad
                signature; 403 if agent_ref is already bound to a
                different address, or if agent_ref is the platform
                treasury and payout_address isn't whitelisted server-side).
        """
        task_id = task_id or f"anon-{uuid.uuid4().hex}"
        agent_request_id = _derive_request_id(
            task_id, agent_ref=agent_ref, amount_usdc=amount_usdc, payout_address=payout_address
        )
        binding_message = _compute_binding_message(
            agent_request_id, agent_ref=agent_ref, amount_usdc=amount_usdc, payout_address=payout_address
        )
        account = Account.from_key(private_key)
        body = {
            "agent_request_id": agent_request_id,
            "agent_ref": agent_ref,
            "amount_usdc": amount_usdc,
            "payout_address": payout_address,
            "agent_address": account.address,
            "agent_signature": _sign(private_key, binding_message),
        }

        try:
            resp = self._http.post(f"{self.base_url}/withdraw", json=body)
        except httpx.HTTPError as e:
            raise InterchangeAPIError(0, f"could not reach Interchange at {self.base_url}: {e}")

        if resp.status_code == 402:
            raise InsufficientBalanceError([_error_detail(resp)])
        if resp.status_code == 409:
            raise RequestConflictError(_error_detail(resp))
        if resp.status_code >= 400:
            raise InterchangeAPIError(resp.status_code, _error_detail(resp))

        return resp.json()


def _error_detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        return data.get("detail") or data.get("error") or resp.text
    except ValueError:
        return resp.text
