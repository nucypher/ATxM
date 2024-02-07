from copy import copy
from typing import List, Set

from eth_account.signers.local import LocalAccount
from web3.types import TxParams

from atxm.machine import _Machine
from atxm.tx import (
    FinalizedTx,
    FutureTx,
    PendingTx,
    AsyncTx,
)


class AutomaticTxMachine(_Machine):
    def start(self, now: bool = False) -> None:
        """Start the machine. if now is True, start immediately."""
        super()._start(now=now)

    def stop(self) -> None:
        """Stop the machine."""
        super()._stop()

    @property
    def running(self) -> bool:
        """Return True if the machine is running."""
        return bool(self._task.running)

    @property
    def paused(self) -> bool:
        """Return True if the machine is paused."""
        return bool(self.__pause)

    @property
    def busy(self) -> bool:
        """Returns True if the machine is busy."""
        return super()._busy

    @property
    def queued(self) -> List[FutureTx]:
        """Return a list of queued transactions."""
        return list(self._state.queue)

    @property
    def pending(self) -> PendingTx:
        """Return the active transaction if there is one."""
        return copy(self._state.pending or None)

    @property
    def finalized(self) -> Set[FinalizedTx]:
        """Return a set of finalized transactions."""
        return set(self._state.finalized)

    @property
    def faults(self) -> List[AsyncTx]:
        """Return a set of faulted transactions."""
        return list(self._state.faulty)

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
        tx = self._state._queue(_from=signer.address, params=params, *args, **kwargs)
        if not self._task.running:
            self._wake()
        return tx

    def queue_transactions(
        self, params: List[TxParams], signer: LocalAccount, *args, **kwargs
    ) -> List[FutureTx]:
        """
        Queue a list of transactions for broadcast and subsequent tracking.

        Sorts incoming transactions by nonce. The machine is tolerant
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
