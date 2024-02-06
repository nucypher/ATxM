from typing import List, Set

from eth_account.signers.local import LocalAccount
from web3.types import TxParams

from atxm.tracker import _Machine
from atxm.tx import (
    FinalizedTx,
    FutureTx,
    PendingTx,
)


class AutomaticTxMachine(_Machine):

    @property
    def queued(self) -> List[FutureTx]:
        """Return a list of queued transactions."""
        return list(self._state.waiting)

    @property
    def pending(self) -> PendingTx:
        """Return the active transaction if there is one."""
        return self._state.active or None

    @property
    def finalized(self) -> Set[FinalizedTx]:
        """Return a set of finalized transactions."""
        return self._state.finalized

    def queue_transaction(
        self, params: TxParams, signer: LocalAccount, *args, **kwargs
    ) -> FutureTx:
        """
        Queue a new transaction for broadcast and subsequent tracking.
        Optionally provide a dictionary of additional string data
        to log during the transaction's lifecycle for identification.
        """
        if signer.address not in self.signers:
            self.signers[signer.address] = signer
        tx = self._state.queue(_from=signer.address, params=params, *args, **kwargs)
        if not self._task.running:
            self._wake()
        return tx

    def queue_transactions(
        self, params: List[TxParams], signer: LocalAccount, *args, **kwargs
    ) -> List[FutureTx]:
        """
        Queue a list of transactions for broadcast and subsequent tracking.

        Sorts incoming transactions by nonce. The tracker is tolerant
        to nonce collisions, but it's best to avoid them when possible,
        plus it's a good practice to broadcast transactions in the
        order they were originally created in by the caller.
        """
        params = sorted(params, key=lambda x: x["nonce"])

        future_txs = []
        for _params in params:
            future_txs.append(
                self.queue_transaction(signer=signer, params=_params, *args, **kwargs)
            )
        return future_txs
