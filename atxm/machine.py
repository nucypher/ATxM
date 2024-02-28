from copy import deepcopy
from typing import Optional, List, Type

from eth_account.signers.local import LocalAccount
from statemachine import StateMachine, State
from twisted.internet import reactor
from twisted.internet.defer import Deferred
from twisted.internet.task import LoopingCall
from web3 import Web3
from web3.types import TxParams

from atxm.exceptions import (
    Wait,
    TransactionReverted,
    TransactionFault,
    Fault,
)
from atxm.tracker import _TxTracker
from atxm.strategies import (
    AsyncTxStrategy,
    InsufficientFundsPause,
    TimeoutPause,
    FixedRateSpeedUp,
)
from atxm.tx import (
    FutureTx,
    PendingTx,
    TxHash,
)
from atxm.utils import (
    _get_average_blocktime,
    _get_confirmations,
    _get_receipt,
    _handle_rpc_error,
    fire_hook,
    _make_tx_params,
)
from .logging import log


class _Machine(StateMachine):
    """
    Do not import this - it will not work.
    Please import the publicly exposed `AutomaticTxMachine` instead.
    Thanks! :)
    """

    #
    # State Machine:
    #
    #       Pause
    #      ^      ^
    #     /        \
    #    V         v
    #   Idle <---> Busy
    #   | ^        | ^
    #   V |        V |
    #    _          _
    #
    _BUSY = State("Busy")
    _IDLE = State("Idle", initial=True)
    _PAUSED = State("Paused")

    # - State Transitions -
    _transition_to_paused = _BUSY.to(_PAUSED, cond="_pause") | _IDLE.to(
        _PAUSED, cond="_pause"
    )  # Busy/Idle -> Pause
    _transition_to_idle = _BUSY.to(_IDLE, unless=["_busy", "_pause"]) | _PAUSED.to(
        _IDLE, unless=["_busy", "_pause"]
    )  # Busy/Paused -> Idle
    _transition_to_busy = _IDLE.to(_BUSY, cond="_busy") | _PAUSED.to(
        _BUSY, unless="_pause"
    )  # Idle/Pause -> Busy
    # self transitions i.e. remain in same state
    _remain_busy = _BUSY.to.itself(cond="_busy", unless="_pause")
    _remain_idle = _IDLE.to.itself(unless=["_busy", "_pause"])

    _cycle_state = (
        _transition_to_idle
        | _transition_to_paused
        | _transition_to_busy
        | _remain_busy
        | _remain_idle
    )

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

    class LogObserver:
        """StateMachine observer for logging information about state/transitions."""

        def __init__(self):
            self.log = log

        def on_transition(self, source, target):
            if source.id != target.id:
                self.log.debug(f"[transition] {source.name} --> {target.name}")

    def __init__(
        self,
        w3: Web3,
        strategies: Optional[List[AsyncTxStrategy]] = None,
        disk_cache: bool = False,
    ):
        # public
        self.w3 = w3
        self.signers = {}
        self.log = log
        self.strategies = [s(w3) for s in self.STRATEGIES]
        if strategies:
            self.strategies.extend(list(strategies))

        # state
        self._pause = False

        # tx tracking
        self._tx_tracker = _TxTracker(disk_cache=disk_cache)

        # async
        self._task = LoopingCall(self._cycle)
        self._task.clock = self.__CLOCK
        self._task.interval = self._IDLE_INTERVAL

        super().__init__()

        self.add_observer(_Machine.LogObserver())

    @property
    def _busy(self) -> bool:
        """Returns True if the machine is busy."""
        return bool(self._tx_tracker.queue or self._tx_tracker.pending)

    #
    # Async
    #

    def _handle_errors(self, *args, **kwargs):
        """Handles unexpected errors during task processing."""
        self._tx_tracker.commit()
        self.log.warn(
            "[recovery] error during transaction: {}".format(args[0].getTraceback())
        )
        if self._task.running:
            return
        self.log.warn("[recovery] restarting transaction machine!")
        self._start(now=False)

    # State Transitions
    @_transition_to_paused.before
    def _enter_pause_mode(self):
        self.log.info("[pause] pause mode activated")

    @_PAUSED.enter
    def _process_paused(self):
        self._sleep()

    @_transition_to_idle.before
    def _enter_idle_mode(self):
        self._task.interval = self._IDLE_INTERVAL
        self.log.info(
            f"[idle] returning to idle mode with {self._task.interval} second interval"
        )

    @_transition_to_busy.before
    def _enter_busy_mode(self):
        """About to enter busy work mode (speed up interval)"""
        average_block_time = _get_average_blocktime(
            w3=self.w3, sample_size=self._BLOCK_SAMPLE_SIZE
        )
        self._task.interval = max(
            round(average_block_time * self._BLOCK_INTERVAL), self._MIN_INTERVAL
        )
        self.log.info(f"[working] cycle interval is now {self._task.interval} seconds")

    @_BUSY.enter
    def _process_busy(self):
        self.log.info(
            f"[working] tracking {len(self._tx_tracker.queue)} queued "
            f"transaction{'s' if len(self._tx_tracker.queue) > 1 else ''} "
            f"{'and 1 pending transaction' if self._tx_tracker.pending else ''}"
        )

        if self._tx_tracker.pending:
            # There is an active transaction
            self.__handle_active_transaction()
        elif self._tx_tracker.queue:
            # There is no active transaction
            # and there are queued transactions
            self.__broadcast()

    def _cycle(self) -> None:
        """Execute one cycle"""
        self.__monitor_finalized()

        self._cycle_state()

    #
    # Throttle
    #

    def _start(self, now: bool = False) -> Deferred:
        if self._task.running:
            return self._task.deferred
        when = "now" if now else f"in {self._task.interval} seconds"
        self.log.info(f"[atxm] starting async transaction machine {when}")
        d = self._task.start(interval=self._task.interval, now=now)
        d.addErrback(self._handle_errors)
        return d

    def _stop(self):
        """Stop task."""
        if self._task.running:
            self._task.stop()

    def _wake(self) -> None:
        """Runs the looping call immediately."""
        log.info("[reset] running looping call now.")
        if self._task.running:
            # TODO instead of stopping/starting, can you set interval to 0
            #  and call reset to have looping call immediately?
            self._stop()

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

        pending_tx = self._tx_tracker.pending

        try:
            receipt = _get_receipt(w3=self.w3, pending_tx=pending_tx)

        # Outcome 2: the pending transaction was reverted (final error)
        except TransactionReverted:
            self._tx_tracker.fault(
                error=pending_tx.txhash.hex(),
                fault=Fault.REVERT,
                clear_active=True,
            )
            return True

        # Outcome 3: pending transaction is finalized (final success)
        if receipt:
            final_txhash = receipt["transactionHash"]
            confirmations = _get_confirmations(w3=self.w3, tx=pending_tx)
            self.log.info(
                f"[finalized] Transaction #atx-{pending_tx.id} has been finalized "
                f"with {confirmations} confirmation(s) txhash: {final_txhash.hex()}"
            )
            self._tx_tracker.finalize_active_tx(receipt=receipt)
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
        pending_tx = self._tx_tracker.morph(tx=tx, txhash=txhash)
        if tx.on_broadcast:
            fire_hook(hook=tx.on_broadcast, tx=pending_tx)
        return pending_tx

    def __strategize(self) -> Optional[PendingTx]:
        """Retry the currently tracked pending transaction with the configured strategy."""
        if not self._tx_tracker.pending:
            raise RuntimeError("No active transaction to strategize")

        _active_copy = deepcopy(self._tx_tracker.pending)
        for strategy in self.strategies:
            try:
                params = strategy.execute(pending=_active_copy)
            except Wait as e:
                log.info(f"[wait] strategy {strategy.__class__} signalled wait: {e}")
                return
            except TransactionFault as e:
                self._tx_tracker.fault(
                    error=self._tx_tracker.pending.txhash.hex(),
                    fault=e.fault,
                    clear_active=e.clear,
                )
                return
            if params:
                # in case the strategy accidentally returns None
                # keep the parameters as they are.
                _active_copy.params.update(params)

            if self._pause:
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
        future_tx = self._tx_tracker._pop()  # popleft
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
            self._tx_tracker._requeue(future_tx)
            return
        return pending_tx.txhash

    #
    # Monitoring
    #
    def __monitor_finalized(self) -> None:
        """Follow-up on finalized transactions for a little while."""
        if not self._tx_tracker.finalized:
            return
        for tx in self._tx_tracker.finalized.copy():
            confirmations = _get_confirmations(w3=self.w3, tx=tx)
            if confirmations >= self._TRACKING_CONFIRMATIONS:
                if tx in self._tx_tracker.finalized:
                    self._tx_tracker.finalized.remove(tx)
                    self.log.info(
                        f"[clear] stopped tracking {tx.txhash.hex()} after {confirmations} confirmations"
                    )
                continue
            self.log.info(
                f"[monitor] transaction {tx.txhash.hex()} has {confirmations} confirmations"
            )

    #
    # Exposed functions
    #
    def pause(self) -> None:
        """
        Pause the machine's tx processing loop; no txs are processed until unpaused (resume()).
        """
        self._pause = True
        self.log.info("[pause] pause mode requested")

    def resume(self) -> None:
        """Unpauses (resumes) the machine's tx processing loop."""
        if self._pause:
            self._pause = False
            self.log.info("[pause] pause mode deactivated")
            self._wake()

    def queue_transaction(
        self, params: TxParams, signer: LocalAccount, *args, **kwargs
    ) -> FutureTx:
        """
        Queue a new transaction for broadcast and subsequent tracking.
        Optionally provide a dictionary of additional string data
        to log during the transaction's lifecycle for identification.
        """
        previously_busy_or_paused = self._busy or self._pause

        if signer.address not in self.signers:
            self.signers[signer.address] = signer

        tx = self._tx_tracker._queue(
            _from=signer.address, params=params, *args, **kwargs
        )
        if not previously_busy_or_paused:
            self._wake()

        return tx
