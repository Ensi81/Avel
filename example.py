"""
Un agente che chiede un verdetto prima di pagarne un altro.

Il punto da tenere a mente leggendo questo file: il pagamento fra i due
agenti NON avviene qui e non passa da qui. Questa chiamata dice soltanto
se è sicuro procedere — poi l'agente paga sul rail che già usa (x402,
carta, bonifico, quello che sia). Noi incassiamo solo la provvigione per
il controllo eseguito.

Serve un Interchange raggiungibile e una chiave privata EVM dell'agente
(generate_wallet.py nel repo principale, oppure
eth_account.Account.create() per un'identità di prova).

Esecuzione: python example.py
"""
import os

from agent_interchange import InterchangeClient

AGENT_A_PRIVATE_KEY = os.environ["AGENT_A_PRIVATE_KEY"]
AGENT_A_ADDRESS = os.environ["AGENT_A_ADDRESS"]

client = InterchangeClient("https://aisrail.fly.dev")

result = client.verify(
    payer_address=AGENT_A_ADDRESS,
    payee_id="merchant-42",
    amount=25.0,
    currency="EUR",
    private_key=AGENT_A_PRIVATE_KEY,
    # Con quale metodo paghiamo LA VERIFICA (non la transazione).
    # Senza questo il server risponde 402 e non rilascia il verdetto:
    # la verifica si paga.
    fee_payment_method_id=os.environ.get("STRIPE_PAYMENT_METHOD_ID"),
)

if result.get("verdict_withheld"):
    # La verifica è passata ma la provvigione non è stata incassata:
    # il verdetto resta trattenuto finché non si paga.
    print(f"Verdetto trattenuto — {result.get('error')}: {result.get('message')}")
    print(f"Provvigione dovuta: {(result.get('fee') or {}).get('amount')}")
elif result["verdict"] == "APPROVED":
    print("Verdetto: APPROVED — si può procedere col pagamento sul proprio rail.")
    print("Motivi:", *result["reasons"], sep="\n  - ")
    print(f"Provvigione pagata: {result['fee']['amount']} {result['fee']['currency']}")
    # ... qui l'agente esegue il pagamento vero, dove lo esegue di solito.
else:
    print("Verdetto: DENIED — non procedere.")
    print("Motivi:", *result["reasons"], sep="\n  - ")

client.close()

# --- Nota sugli altri protocolli ---
# verify() qui sopra usa protocol="native": l'agente firma il testo
# canonico della transazione. Se l'agente sta già pagando con x402, AP2 o
# MPP, non serve una seconda firma — si manda la prova che ha già
# prodotto per il pagamento, dentro `context`. Un esempio per ciascun
# protocollo è in scripts/protocol_adapters_demo.py del repo principale.
