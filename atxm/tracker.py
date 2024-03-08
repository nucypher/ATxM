import json
import time
from collections import deque
from copy import copy
from json import JSONDecodeError
from pathlib import Path
from typing import Callable, Deque, Dict, Optional, Set, Tuple

from web3.types import TxParams, TxReceipt

from atxm.exceptions import TransactionFaulted
from atxm.logging import log
from atxm.tx import (
    FinalizedTx,
    FutureTx,
    PendingTx,
    TxHash,
    FaultedTx,
)
from atxm.utils import fire_hook


class _TxTracker:
    """State management for transaction tracking."""

    _FILEPATH = ".txs.json"
    __COUNTER = 0  # id generator

    def __init__(self, disk_cache: bool, filepath: Optional[Path] = None):
        self.__filepath = filepath or self._FILEPATH

        self.__queue: Deque[FutureTx] = deque()
        self.__active: Optional[PendingTx] = None
        self.finalized: Set[FinalizedTx] = set()

        self.disk_cache = disk_cache
        if disk_cache:
            self.restore()

    def to_dict(self) -> Dict:
        """Serialize the state to a JSON string."""
        active = self.__active.to_dict() if self.__active else {}
        queue = [tx.to_dict() for tx in self.__queue]
        finalized = [tx.to_dict() for tx in self.finalized]
        _dict = {"queue": queue, "active": active, "finalized": finalized}
        return _dict

    def commit(self) -> None:
        """Write the state to the cache file."""
        if not self.disk_cache:
            return
        with open(self.__filepath, "w+t") as file:
            data = json.dumps(self.to_dict())
            file.write(data)
        log.debug(f"[tracker] wrote transaction cache file {self.__filepath}")

    def restore(self) -> bool:
        """
        Restore the state from the cache file.
        Returns True if the cache file exists and was successfully
        restored with data.
        """
        if not self.disk_cache:
            return False

        # read & parse
        if not self.__filepath.exists():
            return False
        with open(self.__filepath, "r+t") as file:
            data = file.read()
        try:
            data = json.loads(data)
        except JSONDecodeError:
            data = dict()
        active = data.get("active", dict())
        queue = data.get("queue", list())
        final = data.get("finalized", list())

        # deserialize & restore
        self.__active = PendingTx.from_dict(active) if active else None
        self.__queue.extend(FutureTx.from_dict(tx) for tx in queue)
        self.finalized = {FinalizedTx.from_dict(tx) for tx in final}
        log.debug(
            f"[tracker] restored {len(queue)} transactions from cache file {self.__filepath}"
        )

        return bool(data)

    def __set_active(self, tx: PendingTx) -> None:
        """Update the active transaction (destructive operation)."""
        old = None
        if self.__active:
            old = self.__active.txhash
        self.__active = tx
        self.commit()
        if old:
            log.debug(
                f"[tracker] updated active transaction {old.hex()} -> {tx.txhash.hex()}"
            )
            return
        log.debug(f"[tracker] tracked active transaction {tx.txhash.hex()}")

    def update_after_retry(self, tx: PendingTx) -> PendingTx:
        if not self.__active:
            raise RuntimeError("No active transaction to update")
        if tx.id != self.__active.id:
            raise RuntimeError(
                f"Mismatch between active tx ({self.__active.id}) and provided tx ({tx.id})"
            )

        self.__active.txhash = tx.txhash
        self.__active.params = tx.params

        return self.pending

    def update_failed_retry_attempt(self, tx: PendingTx):
        if not self.__active:
            raise RuntimeError("No active transaction to update")
        if tx.id != self.__active.id:
            raise RuntimeError(
                f"Mismatch between active tx ({self.__active.id}) and provided tx ({tx.id})"
            )
        self.__active.retries += 1
        # safety check
        if tx is not self.__active:
            tx.retries = self.__active.retries

    def morph(self, tx: FutureTx, txhash: TxHash) -> PendingTx:
        """
        Morphs a future transaction into a pending transaction.
        Uses polymorphism to transform the future transaction into a pending transaction.
        """
        tx.txhash = txhash
        tx.created = int(time.time())
        tx.capped = False
        tx.__class__ = PendingTx
        tx: PendingTx
        self.__set_active(tx=tx)
        return tx

    def fault(
        self,
        fault_error: TransactionFaulted,
    ) -> None:
        """Fault the active transaction."""
        if not self.__active:
            raise RuntimeError("No active transaction to fault")
        if fault_error.tx.id != self.__active.id:
            raise RuntimeError(
                f"Mismatch between active tx ({self.__active.id}) and faulted tx ({fault_error.tx.id})"
            )

        hook = self.__active.on_fault
        tx = self.__active
        txhash = tx.txhash.hex()

        tx.fault = fault_error.fault
        tx.error = fault_error.message
        tx.__class__ = FaultedTx
        tx: FaultedTx

        log.warn(
            f"[tracker] transaction #atx-{tx.id} faulted with '{tx.fault.value}'; "
            f"{txhash}{f' ({fault_error.message})' if fault_error.message else ''}"
        )
        self.clear_active()
        if hook:
            fire_hook(hook=hook, tx=tx)

    def finalize_active_tx(self, receipt: TxReceipt) -> None:
        """
        Finalizes a pending transaction.
        Use polymorphism to transform the pending transaction into a finalized transaction.
        """
        if not self.__active:
            raise RuntimeError("No pending transaction to finalize")

        hook = self.__active.on_finalized
        self.__active.receipt = receipt
        self.__active.__class__ = FinalizedTx
        tx = self.__active
        self.finalized.add(tx)  # noqa
        log.info(f"[tracker] #atx-{tx.id} pending -> finalized")
        self.clear_active()
        if hook:
            fire_hook(hook=hook, tx=tx)

    def clear_active(self) -> None:
        """Clear the active transaction (destructive operation)."""
        self.__active = None
        self.commit()
        log.debug(
            f"[tracker] cleared 1 pending transaction \n"
            f"[tracker] {len(self.queue)} queued "
            f"transaction{'s' if len(self.queue) != 1 else ''} remaining"
        )

    @property
    def pending(self) -> Optional[PendingTx]:
        """Return the active pending transaction if there is one."""
        return copy(self.__active)

    @property
    def queue(self) -> Tuple[FutureTx, ...]:
        """Return the queue of transactions."""
        return tuple(self.__queue)

    def pop(self) -> FutureTx:
        """Pop the next transaction from the queue."""
        return self.__queue.popleft()

    def requeue(self, tx: FutureTx) -> None:
        """Re-queue a transaction for broadcast and subsequent tracking."""
        tx.requeues += 1
        self.__queue.append(tx)
        self.commit()
        log.info(
            f"[tracker] re-queued transaction #atx-{tx.id} "
            f"priority {len(self.__queue)}"
        )

    def queue_tx(
        self,
        params: TxParams,
        info: Dict[str, str] = None,
        on_broadcast: Optional[Callable[[PendingTx], None]] = None,
        on_broadcast_failure: Optional[Callable[[FutureTx, Exception], None]] = None,
        on_finalized: Optional[Callable[[FinalizedTx], None]] = None,
        on_fault: Optional[Callable[[FaultedTx], None]] = None,
    ) -> FutureTx:
        """Queue a new transaction for broadcast and subsequent tracking."""
        tx = FutureTx(
            id=self.__COUNTER,
            params=params,
            info=info,
        )

        # configure hooks
        tx.on_broadcast = on_broadcast
        tx.on_broadcast_failure = on_broadcast_failure
        tx.on_finalized = on_finalized
        tx.on_fault = on_fault

        self.__queue.append(tx)
        self.commit()
        self.__COUNTER += 1
        log.info(
            f"[tracker] queued transaction #atx-{tx.id} priority {len(self.__queue)}"
        )
        return tx
