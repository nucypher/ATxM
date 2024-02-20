from copy import deepcopy
from logging import Logger
from typing import Optional, Union, List, Type

from eth_account.signers.local import LocalAccount
from twisted.internet import reactor
from twisted.internet.defer import Deferred
from twisted.internet.task import LoopingCall
from web3 import Web3
from web3.exceptions import TransactionNotFound
from web3.types import TxReceipt, TxParams

from atxm.exceptions import (
    Wait,
    TransactionReverted,
    TransactionFault,
    Fault,
)
from atxm.state import _State
from atxm.strategies import (
    AsyncTxStrategy,
    InsufficientFundsPause,
    TimeoutPause,
    FixedRateSpeedUp,
)
from atxm.tx import (
    FinalizedTx,
    FutureTx,
    PendingTx,
    TxHash,
)
from atxm.utils import (
    _get_average_blocktime,
    _get_receipt,
    _handle_rpc_error,
    fire_hook,
    _make_tx_params,
)
from .logging import log


class _Machine:
    """
    Do not import this - it will not work.
    Please import the publicly exposed `AutomaticTxMachine` instead.
    Thanks! :)
    """

    # internal
    __CLOCK = reactor  # twisted reactor

    # tweaks
    _TRACKING_CONFIRMATIONS = 300  # blocks until clearing a finalized transaction
    _RPC_THROTTLE = 1  # min. seconds between RPC calls (>1 recommended)
    _MIN_INTERVAL = 1
    _IDLE_INTERVAL = 60 * 5  # seconds
    _BLOCK_INTERVAL = 20  # ~20 blocks
    _BLOCK_SAMPLE_SIZE = 10_000  # blocks

    STRATEGIES: List[Type[AsyncTxStrategy]] = [
        InsufficientFundsPause,
        TimeoutPause,
        FixedRateSpeedUp,
    ]

    def __init__(
        self,
        w3: Web3,
        strategies: Optional[List[AsyncTxStrategy]] = None,
        disk_cache: bool = False,
    ):
        # public
        self.w3 = w3
        self.signers = {}
        self.log = Logger(self.__class__.__name__)
        self.strategies = [s(w3) for s in self.STRATEGIES]
        if strategies:
            self.strategies.extend(list(strategies))

        # state
        self.__pause = False
        self._state = _State(disk_cache=disk_cache)

        # async
        self._task = LoopingCall(self._cycle)
        self._task.clock = self.__CLOCK
        self._task.interval = self._IDLE_INTERVAL

    @property
    def _clock(self):
        return self.__CLOCK

    @property
    def _interval(self) -> Optional[float]:
        return self._task.interval

    @property
    def _start_time(self) -> float:
        return self._task.starttime

    @property
    def _busy(self) -> bool:
        """Returns True if the machine is busy."""
        return bool(self._state.queue or self._state.pending)

    #
    # Async
    #

    def _handle_errors(self, *args, **kwargs):
        """Handles unexpected errors during task processing."""
        self._state.commit()
        self.log.warning(
            "[recovery] error during transaction: {}".format(args[0].getTraceback())
        )
        if self._task.running:
            return
        self.log.warning("[recovery] restarting transaction machine!")
        self._start(now=False)

    def _cycle(self) -> None:
        """Execute one cycle"""

        if self.__pause:
            self.log.warning("[pause] paused")
            return

        self.__monitor_finalized()
        if not self._busy:
            self.log.info(f"[idle] cycle interval is {self._task.interval} seconds")
            return

        # sample block time and adjust the interval
        # every work cycle.
        self.__work_mode()
        self.log.info(
            f"[working] tracking {len(self._state.queue)} queued "
            f"transaction{'s' if len(self._state.queue) > 1 else ''} "
            f"{'and 1 pending transaction' if self._state.pending else ''}"
        )

        if self._state.pending:
            # There is an active transaction
            self.__handle_active_transaction()

        elif self._state.queue:
            # There is no active transaction
            # and there are queued transactions
            self.__broadcast()

        # After one work cycle, return to idle mode
        # if the machine is not busy.
        if not self._busy:
            self.__idle_mode()

    #
    # Throttle
    #

    def _start(self, now: bool = False) -> Deferred:
        if self._task.running:
            return self._task.deferred
        when = "now" if now else f"in {self._task.interval} seconds"
        self.log.info(f"[atxm] starting async transaction machine {when}")
        d = self._task.start(interval=self._interval, now=now)
        d.addErrback(self._handle_errors)
        return d

    def _stop(self):
        """Stop task."""
        if self._task.running:
            self._task.stop()

    def __idle_mode(self) -> None:
        """Return to idle mode (slow down)"""
        self._task.interval = self._IDLE_INTERVAL
        self.log.info(
            f"[done] returning to idle mode with "
            f"{self._task.interval} second interval"
        )
        if not self._busy:
            self._sleep()

    def __work_mode(self) -> None:
        """Start work mode (speed up)"""
        average_block_time = _get_average_blocktime(
            w3=self.w3, sample_size=self._BLOCK_SAMPLE_SIZE
        )
        self._task.interval = max(
            round(average_block_time * self._BLOCK_INTERVAL), self._MIN_INTERVAL
        )
        self.log.info(f"[working] cycle interval is {self._task.interval} seconds")

    def _wake(self) -> None:
        if not self._task.running:
            log.info("[wake] waking up")
            self._start(now=True)

    def _sleep(self) -> None:
        if self._task.running:
            log.info("[sleep] sleeping")
            self._stop()

    #
    # Lifecycle
    #

    def __handle_active_transaction(self) -> bool:
        """
        Handles the currently tracked pending transaction.

        The 4 possible outcomes for the pending ("active") transaction in one cycle:

        1. paused
        2. reverted (fault)
        3. finalized
        4. strategize: retry, wait, or fault

        Returns True if the next queued transaction can be broadcasted right now.
        """

        # Outcome 1: the pending transaction is paused by strategies
        if self.__pause:
            self.log.warning(
                f"[pause] transaction #{pending_tx.id} paused by strategies"
            )
            return False

        try:
            receipt = self.__get_receipt()

        # Outcome 2: the pending transaction was reverted (final error)
        except TransactionReverted:
            self._state.fault(
                error=pending_tx.txhash.hex(),
                fault=Fault.REVERT,
                clear_active=True,
            )
            return True

        # Outcome 3: pending transaction is finalized (final success)
        if receipt:
            final_txhash = receipt["transactionHash"]
            confirmations = self.__get_confirmations(tx=self._state.pending)
            self.log.info(
                f"[finalized] Transaction #atx-{self._state.pending.id} has been finalized "
                f"with {confirmations} confirmations txhash: {final_txhash.hex()}"
            )
            self._state.finalize_active_tx(receipt=receipt)
            return True

        # Outcome 4: re-strategize the pending transaction
        pending_tx = self.__strategize()
        return pending_tx is not None

    #
    # Broadcast
    #

    def __get_signer(self, address: str) -> LocalAccount:
        try:
            signer = self.signers[address]
        except KeyError:
            raise ValueError(f"Signer {address} not found")
        return signer

    def __fire(self, tx: FutureTx, msg: str) -> Optional[PendingTx]:
        """
        Signs and broadcasts a transaction, handling RPC errors
        and internal state changes.

        On success, returns the `PendingTx` object.
        On failure, returns None.

        Morphs a `FutureTx` into a `PendingTx` and advances it
        into the active transaction slot if broadcast is successful.
        """
        signer: LocalAccount = self.__get_signer(tx._from)
        try:
            txhash = self.w3.eth.send_raw_transaction(
                signer.sign_transaction(tx.params).rawTransaction
            )
        except ValueError as e:
            _handle_rpc_error(e, tx=tx)
            return
        self.log.info(
            f"[{msg}] fired transaction #atx-{tx.id}|{tx.params['nonce']}|{txhash.hex()}"
        )
        pending_tx = self._state.morph(tx=tx, txhash=txhash)
        if tx.on_broadcast:
            fire_hook(hook=tx.on_broadcast, tx=tx)
        return pending_tx

    def pause(self) -> None:
        self.__pause = True
        self.log.warning(
            f"[pause] pending transaction {self._state.pending.txhash.hex()} has been paused."
        )
        hook = self._state.pending.on_pause
        if hook:
            fire_hook(hook=hook, tx=self._state.pending)

    def resume(self) -> None:
        self.log.info("[pause] pause lifted by strategy")
        self.__pause = False  # resume

    def __strategize(self) -> Optional[PendingTx]:
        """Retry the currently tracked pending transaction with the configured strategy."""
        if not self._state.pending:
            raise RuntimeError("No active transaction to strategize")

        _active_copy = deepcopy(self._state.pending)
        for strategy in self.strategies:
            try:
                params = strategy.execute(pending=_active_copy)
            except Wait:
                self.pause()
                return
            except TransactionFault as e:
                self._state.fault(
                    error=self._state.pending.txhash.hex(),
                    fault=e.fault,
                    clear_active=e.clear,
                )
                return
            if params:
                # in case the strategy accidentally returns None
                # keep the parameters as they are.
                _active_copy.params.update(params)

            if self.__pause:
                self.resume()

        # (!) retry the transaction with the new parameters
        retry_params = TxParams(_active_copy.params)
        _names = " -> ".join(s.name for s in self.strategies)
        pending_tx = self.__fire(tx=retry_params, msg=_names)
        self.log.info(f"[retry] transaction #{pending_tx.id} has been re-broadcasted")

        return pending_tx

    def __broadcast(self) -> Optional[TxHash]:
        """
        Attempts to broadcast the next `FutureTx` in the queue.
        If the broadcast is not successful, it is re-queued.
        """
        future_tx = self._state._pop()  # popleft
        future_tx.params = _make_tx_params(future_tx.params)
        signer = self.__get_signer(future_tx._from)
        nonce = self.w3.eth.get_transaction_count(signer.address, "latest")
        if nonce > future_tx.params["nonce"]:
            self.log.warn(
                f"[broadcast] nonce {future_tx.params['nonce']} has been front-run "
                f"by another transaction. Updating queued tx nonce {future_tx.params['nonce']} -> {nonce}"
            )
        future_tx.params["nonce"] = nonce
        pending_tx = self.__fire(tx=future_tx, msg="broadcast")
        if not pending_tx:
            self._state._requeue(future_tx)
            return
        return pending_tx.txhash

    #
    # Monitoring
    #

    def __get_receipt(self) -> Optional[TxReceipt]:
        """
        Hits eth_getTransaction and eth_getTransactionReceipt
        for the active pending txhash and checks if
        it has been finalized or reverted.

        Returns the receipt if the transaction has been finalized.
        NOTE: Performs state changes
        """
        try:
            txdata = self.w3.eth.get_transaction(self._state.pending.txhash)
            self._state.pending.data = txdata
        except TransactionNotFound:
            self.log.error(
                f"[error] Transaction {self._state.pending.txhash.hex()} not found"
            )
            return

        receipt = _get_receipt(w3=self.w3, data=txdata)
        if not receipt:
            return

        status = receipt.get("status")
        if status == 0:
            # If status in response equals 1 the transaction was successful.
            # If it is equals 0 the transaction was reverted by EVM.
            # https://web3py.readthedocs.io/en/stable/web3.eth.html#web3.eth.Eth.get_transaction_receipt
            log.warning(
                f"Transaction {txdata['hash'].hex()} was reverted by EVM with status {status}"
            )
            raise TransactionReverted(receipt)

        log.info(
            f"[accepted] Transaction {txdata['nonce']}|{txdata['hash'].hex()} "
            f"has been included in block #{txdata['blockNumber']}"
        )
        return receipt

    def __get_confirmations(self, tx: Union[PendingTx, FinalizedTx]) -> int:
        current_block = self.w3.eth.block_number
        txhash = (
            tx.txhash if isinstance(tx, PendingTx) else tx.receipt["transactionHash"]
        )
        try:
            tx_receipt = self.w3.eth.get_transaction_receipt(txhash)
            tx_block = tx_receipt["blockNumber"]
            confirmations = current_block - tx_block
            return confirmations
        except TransactionNotFound:
            self.log.info(f"Transaction {txhash.hex()} is pending or unconfirmed")
            return 0

    def __monitor_finalized(self) -> None:
        """Follow-up on finalized transactions for a little while."""
        if not self._state.finalized:
            return
        for tx in self._state.finalized.copy():
            confirmations = self.__get_confirmations(tx=tx)
            txhash = tx.receipt["transactionHash"]
            if confirmations >= self._TRACKING_CONFIRMATIONS:
                if tx in self._state.finalized:
                    self._state.finalized.remove(tx)
                    self.log.info(
                        f"[clear] stopped tracking {txhash.hex()} after {confirmations} confirmations"
                    )
                continue
            self.log.info(
                f"[monitor] transaction {txhash.hex()} has {confirmations} confirmations"
            )
