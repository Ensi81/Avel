# agent-interchange

SDK Python per **AVEL** (Agent Verification Layer) — verifica
identità/reputazione di chi paga e chi riceve, su qualunque metodo di
pagamento, senza mai custodire denaro. Il server è closed-source; questo
pacchetto è lo strato client, pensato per essere pubblico e riusabile
(licenza MIT). Standalone — nessuna dipendenza dal codice del server,
solo `httpx`, `eth-account` e l'URL di un'istanza raggiungibile
(`https://aisrail.fly.dev`).

<!-- mcp-name: io.github.Ensi81/avel -->

## Install

```bash
pip install -e .
```

## Uso

```python
from agent_interchange import InterchangeClient

client = InterchangeClient("https://aisrail.fly.dev")
result = client.verify(
    payer_address=agent_a_address,
    payee_id="merchant-42",
    amount=25.0,
    currency="EUR",
    private_key=agent_a_private_key,
    fee_payment_method_id="pm_...",   # con cosa si paga LA VERIFICA
)
```

**Il pagamento fra i due agenti non passa da qui.** Questa chiamata dice
soltanto se è sicuro procedere; poi l'agente paga sul rail che già usa.
Noi incassiamo solo la provvigione per il controllo eseguito (1%
dell'importo verificato, minimo 0,50 su carta) — **dovuta anche su un
verdetto DENIED**, se la firma di chi chiama è comunque valida: è il
prezzo del controllo, non dell'esito.

Tre esiti possibili:

| Risposta | Significato |
|---|---|
| `verdict: "APPROVED"` | Controllo superato, provvigione incassata: si procede |
| `verdict: "DENIED"` | Non procedere. Provvigione comunque dovuta se la firma di chi chiama è valida — gratuito solo quando non lo è (nessuno da addebitare con certezza) |
| `verdict_withheld: true` | Verifica eseguita ma provvigione non ancora incassata: il verdetto (APPROVED o DENIED) resta trattenuto finché non si paga |

`private_key` è la chiave EVM dell'agente (mai la nostra). Il primo uso
autenticato lega quell'identità all'indirizzo che firma
(trust-on-first-use): ogni chiamata successiva deve firmare con la
STESSA chiave. Nessuna registrazione preventiva.

`verify()` usa `protocol="native"`. Se l'agente sta già pagando con
x402, AP2 o MPP non serve una seconda firma: si manda la prova che ha
già prodotto per il pagamento — un esempio per ciascun protocollo è in
`scripts/protocol_adapters_demo.py`.

### Verifica simmetrica di B

Se il venditore (B) dichiara un indirizzo (`payee_address`), deve anche
provare di controllarlo — altrimenti il verdetto è negato. La firma va
calcolata da B (o da chi negozia per suo conto) su
`build_counterparty_message(payer_address, payee_id, amount, currency,
nonce)` e passata come `payee_signature`:

```python
from agent_interchange.client import build_counterparty_message

message = build_counterparty_message(payer_address, payee_id, amount, currency, nonce)
payee_signature = "0x" + Account.sign_message(encode_defunct(text=message), private_key=b_private_key).signature.hex()

result = client.verify(
    payer_address=a_address, payee_id="merchant-42", amount=25.0, currency="EUR",
    private_key=a_private_key, nonce=nonce,
    payee_address=b_address, payee_signature=payee_signature,
)
```

### Modello B-driven (protocollo `challenge`)

Più vicino a come funzionano davvero x402/MPP: è il venditore (B) a
fissare prezzo e termini e a firmarli per primo, l'acquirente (A) li
accetta firmando lo stesso testo.

```python
# Lato B — nessuna chiamata di rete, va consegnato ad A fuori banda
challenge = client.issue_challenge(
    payee_address=b_address, payee_id="merchant-42", amount=25.0, currency="EUR",
    private_key=b_private_key, ttl_seconds=600,
)

# Lato A — accetta il challenge e chiede il verdetto in un colpo solo
result = client.verify_challenge(
    challenge, payer_address=a_address, private_key=a_private_key,
    fee_payment_method_id="pm_...",   # o fee_method="x402" per pagare in stablecoin
)
```

Il challenge è valido una sola volta per la transazione esatta che lo
accetta: un ritentativo identico (necessario per completare il
pagamento della commissione dopo un 402) resta ammesso, un riuso con
termini diversi no.

### Verdetto bidirezionale

Entrambe le parti possono leggere l'esito di una transazione specifica
— non solo chi ha chiamato `verify()`/`verify_challenge()`:

```python
verdetto = client.get_verdict(
    payee_address=b_address, nonce=nonce,
    address=b_address, private_key=b_private_key,   # o quella di A
)
```

Richiede una firma che provi di essere il pagante o il ricevente di
quella transazione — a differenza dello storico aggregato
(`GET /ais/reputation/{address}`, pubblico), il dettaglio di una singola
transazione non lo è.

### Caso marketplace

Quando `payee_id`/`payee_address` sono una vetrina e non chi fornisce
davvero il prodotto/servizio, `verify()` accetta anche
`sub_merchant_address`/`sub_merchant_signature` (stesso principio di B:
la firma va calcolata su `build_sub_merchant_message(payer_address,
payee_id, amount, currency, nonce, sub_merchant_address)`). Se
dichiarato, la sua reputazione (`sub_merchant_reputation` nella
risposta) entra nel verdetto tanto quanto quella di B.

Vedi `example.py` per una versione eseguibile del flusso base.

## Metodi degli Agent Vaults — SPENTI

`pay()`, `get_balance()`, `deposit()` e `withdraw()` appartengono al
modello precedente, in cui il servizio custodiva un saldo interno. Il
server risponde ora `410` a quegli endpoint: **non custodiamo più
denaro**, nemmeno per un istante. Il codice resta ed è riattivabile lato
server con `AGENT_VAULTS_ENABLED=true`; la documentazione qui sotto è
conservata come riferimento storico.

## `InterchangeClient`

- **`verify(payer_address, payee_id, amount, currency, private_key, nonce="", payee_address=None, payee_signature=None, sub_merchant_address=None, sub_merchant_signature=None, fee_payment_method_id=None)`**
  → `dict`. Il metodo da usare per il modello "A dichiara, B conferma".
  Firma EIP-191 sul testo canonico della transazione, chiede il verdetto
  e paga la provvigione. Un `402` non è un errore ma una risposta
  prevista ("verifica passata, ora paga"): viene restituito, non
  sollevato.

- **`issue_challenge(payee_address, payee_id, amount, currency, private_key, ttl_seconds=600)`**
  → `dict`. Lato B (venditore), modello B-driven: emette e firma un
  challenge vincolante, nessuna chiamata di rete. Il risultato va
  consegnato ad A fuori banda.

- **`verify_challenge(challenge, payer_address, private_key, fee_payment_method_id=None, fee_method=None)`**
  → `dict`. Lato A (acquirente): accetta il challenge di `issue_challenge()`
  e chiede il verdetto in un'unica chiamata.

- **`get_verdict(payee_address, nonce, address, private_key)`**
  → `dict`. Verdetto bidirezionale: legge l'esito di una transazione
  specifica, utilizzabile da entrambe le parti coinvolte. Solleva
  `InterchangeAPIError` (403) se `address` non è né il pagante né il
  ricevente di quella transazione, (404) se non esiste alcun verdetto
  registrato per quel `payee_address`/`nonce`.

- **`get_agent_reputation` non è un metodo del client** — è pubblico e
  senza autenticazione: `GET {base_url}/ais/reputation/{address}` con
  una semplice richiesta HTTP.

- ~~**`pay(payer_ref, payee_ref, amount, private_key, currency="USDC", task_id=None)`**~~ *(spento, 410)*
  → `PaymentResult`. `private_key` signs an EIP-191 message binding this
  exact transaction (trust-on-first-use — see Usage above); mandatory
  since the Interchange added wallet authentication to `/pay`.
  Auto-derives `agent_request_id` from `task_id` +
  the call's own parameters (see `client._derive_request_id`) — pass your
  agent framework's own task/run id so a genuine retry (same task_id,
  same arguments) is safely idempotent, while a different payment under
  the same task_id never false-collides with it. Omitting `task_id`
  still works but loses the retry-safety property (a fresh random id is
  used, so a real retry would be treated as a brand new transaction).

  Raises:
  - `InsufficientBalanceError` — payer's balance can't cover amount + fee.
  - `RequestConflictError` — HTTP 409: the derived id was already used
    for *different* content (a possible replay/hijack of a captured
    payment proof by untrusted middleware — rejected, not executed) or
    was reserved but never completed (never retried automatically).
  - `InterchangeAPIError` — any other non-2xx response, or the
    Interchange being unreachable. 401 means `private_key` doesn't
    match the signature; 403 means `payer_ref` is already bound to a
    different address (not this key).

  A risk-score `DENY` (as opposed to an insufficient-balance one) is
  *not* raised — it comes back as a normal `PaymentResult` with
  `.success == False`; check `.decision` / `.reasons` for why.

- **`get_balance(agent_ref, private_key)`** → `dict[str, int]`, atomic
  units per currency, e.g. `{"USDC": 500000}`. `private_key` proves
  control of `agent_ref` (same trust-on-first-use binding as `pay()`);
  allowed with any key only if `agent_ref` was never used in `pay()`/
  `withdraw()` yet.

- **`withdraw(agent_ref, amount_usdc, payout_address, private_key, task_id=None)`**
  → `dict`. `private_key` is `agent_ref`'s own key, proving it
  controls the balance being withdrawn — the Interchange's own treasury
  still signs and pays out on-chain, `private_key` here only
  authenticates the withdrawal request itself. Same `task_id`
  derivation and exception contract as `pay()` (`InsufficientBalanceError`
  on HTTP 402, `RequestConflictError` on 409, 401/403 same meaning as
  `pay()`'s); any other non-2xx (e.g. 403 for an unwhitelisted
  platform-treasury withdrawal) raises `InterchangeAPIError`.

- **`await deposit(agent_ref, amount_usdc, private_key, task_id=None)`**
  → `dict`. **Async** (unlike the other three methods) and needs the
  `deposit` extra — `pip install agent-interchange[deposit]` — because
  it signs a real x402 payment with the depositing agent's own key.
  Calling it without the extra installed raises `InterchangeAPIError`
  (not an ImportError) with instructions. Base install (`pip install -e
  .`) never needs `x402`/`eth-account` unless you call this.

Verified live against a running Interchange (04 Sep 2026): a normal
payment (fee correctly deducted from `amount`, not added on top);
`InsufficientBalanceError` on an oversized `pay()`/`withdraw()`; an
idempotent retry (same `task_id` + same arguments) returning the
identical cached result with no second transfer/payout; the same
`task_id` with *different* arguments executing as a distinct payment (no
false conflict); a forged same-id-different-content request correctly
rejected with `RequestConflictError`, simulating the replay/hijack
scenario the Interchange's own P1 binding defends against
(`interchange/security.py` in the main repo); a real on-chain
`withdraw()` and `deposit()`, each with the resulting transaction
confirmed on Base Sepolia; and `deposit()`'s clean-failure message when
the `deposit` extra isn't installed (simulated by blocking the imports).

## `InterchangeTool` — LangChain / CrewAI / AutoGen

```python
from agent_interchange import InterchangeClient, InterchangeTool

client = InterchangeClient("http://localhost:8000")
tool = InterchangeTool(client)

langchain_tools = tool.for_langchain()   # requires: pip install langchain-core
crewai_tools = tool.for_crewai()         # requires: pip install crewai
autogen_tools = tool.for_autogen()       # requires: pip install autogen-core
```

Only wraps `pay`/`get_balance` as agent-callable tools — deliberately
*not* `deposit`/`withdraw`: those move real funds and `deposit` needs a
raw private key, both a heavier judgment call than exposing to an LLM's
tool-calling loop by default. Call them directly on `client` from your
own code instead.

Lazy imports: constructing `InterchangeTool` and calling `pay()`/
`get_balance()` on it directly never requires any of the three
frameworks — only `for_langchain()`/`for_crewai()`/`for_autogen()` do,
and each fails cleanly (`ModuleNotFoundError`) if its package isn't
installed. Verified live (construction, underlying `_get_balance` call,
and the clean-failure path for all three factories); the tool-decorated
output itself carries the same verification level as the main repo's
`framework_tools.py` — see that file's docstring for how the underlying
`@tool`/`FunctionTool` API shape was confirmed against each framework's
real published source.

## Not included here

The main repo's `agent_tools.py` / `framework_tools.py` (repo root)
cover the same ground but aren't packaged/independently installable —
prefer this SDK for anything outside the main repo's own dev environment.
