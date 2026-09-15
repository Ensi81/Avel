"""
InterchangeTool — quick integration with popular agent frameworks.

Each framework is an OPTIONAL dependency, imported lazily inside its own
factory method: importing this module, or InterchangeTool itself, never
requires LangChain, CrewAI, or AutoGen to be installed — only calling
for_langchain()/for_crewai()/for_autogen() needs that specific package.

Verification note: the underlying pattern (a bare @tool decorator on a
plain function with type hints + a docstring, for both frameworks) was
checked against each package's real published source in the parent
project's framework_tools.py (repo root) — see that file's docstring for
how and why. Not independently re-verified here; same pattern, same
confidence level.
"""
from typing import List, Optional

from .client import InterchangeClient


class InterchangeTool:
    """Wraps an InterchangeClient as native tools for an agent framework.

    Usage:
        client = InterchangeClient("http://localhost:8000")
        tool = InterchangeTool(client, private_key=agent_a_key)
        agent = AssistantAgent(tools=tool.for_langchain())  # or .for_crewai()
    """

    def __init__(self, client: InterchangeClient, private_key: Optional[str] = None):
        self.client = client
        # Legato al Tool alla costruzione, non passato come argomento di
        # ogni chiamata: da quando pay()/get_balance() richiedono una
        # firma (trust-on-first-use lato Interchange), esporre
        # private_key come parametro del tool costringerebbe l'LLM a
        # maneggiarlo — finendo potenzialmente nei log/trace della
        # conversazione. Un'istanza di InterchangeTool rappresenta
        # un'unica identità; la chiave resta qui, mai nell'argomentario
        # visto dal modello.
        self.private_key = private_key

    def _require_key(self) -> str:
        if not self.private_key:
            raise ValueError(
                "InterchangeTool richiede private_key al costruttore: pay()/get_balance() "
                "sull'Interchange ora firmano ogni richiesta per provare il controllo di "
                "payer_ref/agent_ref (trust-on-first-use) — vedi docs/AIS_CONTROL_PLANE.md."
            )
        return self.private_key

    def _verify(
        self,
        payer_address: str,
        payee_id: str,
        amount: float,
        currency: str,
        nonce: str = "",
    ) -> dict:
        """Chiede un verdetto su identita' e reputazione di chi paga
        (payer_address, firmato con la chiave di questa istanza) e chi
        riceve (payee_id) — su qualunque metodo di pagamento, senza mai
        muovere denaro: il pagamento resta sul rail che le parti gia'
        usano. Restituisce {"verdict": "APPROVED"|"DENIED", "reasons":
        [...], ...} con la reputazione di entrambe le parti."""
        return self.client.verify(
            payer_address=payer_address,
            payee_id=payee_id,
            amount=amount,
            currency=currency,
            private_key=self._require_key(),
            nonce=nonce,
        )

    def _pay(
        self,
        payer_ref: str,
        payee_ref: str,
        amount: float,
        task_id: str,
        currency: str = "USDC",
    ) -> dict:
        """Pay payee_ref from payer_ref's Interchange balance. task_id
        should be this agent task/run's own id, so retries stay safe and
        idempotent. Returns the payment decision, fee, and resulting
        balances."""
        result = self.client.pay(payer_ref, payee_ref, amount, self._require_key(), currency, task_id)
        return result.raw

    def _get_balance(self, agent_ref: str) -> dict:
        """Get an agent's current Interchange balances (atomic units per
        currency, e.g. {"USDC": 500000})."""
        return self.client.get_balance(agent_ref, self._require_key())

    def for_langchain(self) -> List:
        """StructuredTool list for LangChain / LangGraph agents.
        Requires: pip install langchain-core"""
        from langchain_core.tools import tool

        return [tool(self._verify), tool(self._pay), tool(self._get_balance)]

    def for_crewai(self) -> List:
        """Tool list for CrewAI agents. Requires: pip install crewai"""
        from crewai.tools import tool as crewai_tool

        return [crewai_tool(self._verify), crewai_tool(self._pay), crewai_tool(self._get_balance)]

    def for_autogen(self) -> List:
        """FunctionTool list for AutoGen (autogen-agentchat/autogen-core,
        or the pyautogen proxy package) — pass directly as
        AssistantAgent(tools=tool.for_autogen()).
        Requires: pip install autogen-core (pulled in automatically by
        autogen-agentchat / pyautogen)."""
        from autogen_core.tools import FunctionTool

        return [
            FunctionTool(self._verify, description=self._verify.__doc__),
            FunctionTool(self._pay, description=self._pay.__doc__),
            FunctionTool(self._get_balance, description=self._get_balance.__doc__),
        ]
