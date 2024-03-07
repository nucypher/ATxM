from copy import copy, deepcopy
from typing import Dict, List, Optional, Type

from eth_account.signers.local import LocalAccount
from eth_utils import ValidationError
from statemachine import State, StateMachine
from twisted.internet import reactor
from twisted.internet.defer import Deferred
from twisted.internet.task import LoopingCall
from web3 import Web3
from web3.exceptions import Web3Exception
from web3.types import TxParams

from atxm.exceptions import (
    Fault,
    InsufficientFunds,
    TransactionFaulted,
    TransactionReverted,
)
from atxm.strategies import (
    AsyncTxStrategy,
    ExponentialSpeedupStrategy,
    TimeoutStrategy,
)
from atxm.tracker import _TxTracker
from atxm.tx import (
    AsyncTx,
    FutureTx,
    PendingTx,
    TxHash,
)
from atxm.utils import (
    _get_average_blocktime,
    _get_confirmations,
    _get_receipt,
    _is_recoverable_send_tx_error,
    _make_tx_params,
    fire_hook,
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
        TimeoutStrategy,
        ExponentialSpeedupStrategy,
    ]

    # max requeues/retries
    _MAX_REDO_ATTEMPTS = 3

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
        self._strategies = [s(w3) for s in self.STRATEGIES]
        if strategies:
            self._strategies.extend(list(strategies))

        # state
        self._pause = False

        # tx tracking
        self._tx_tracker = _TxTracker(disk_cache=disk_cache)

        # async
        self._task = LoopingCall(self._cycle)
        self._task.clock = self.__CLOCK
        self._task.interval = self._IDLE_INTERVAL

        # requeues/retries
        self._requeue_counter: Dict[int, int] = dict()
        self._retry_failure_counter: Dict[int, int] = dict()

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
        log.info("[wake] running looping call now.")
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

    def __handle_active_transaction(self) -> None:
        """
        Handles the currently tracked pending transaction.

        The 4 possible outcomes for the pending ("active") transaction in one cycle:

        1. paused
        2. reverted (fault)
        3. finalized
        4. strategize: retry, do nothing and wait, or fault

        Returns True if the next queued transaction can be broadcasted right now.
        """

        pending_tx = self._tx_tracker.pending

        try:
            receipt = _get_receipt(w3=self.w3, pending_tx=pending_tx)

        # Outcome 2: the pending transaction was reverted (final error)
        except TransactionReverted as e:
            # clear entry if exists
            self._retry_failure_counter.pop(pending_tx.id, "None")

            self._tx_tracker.fault(fault_error=e)
            return

        # Outcome 3: pending transaction is finalized (final success)
        if receipt:
            # clear entry if exists
            self._retry_failure_counter.pop(pending_tx.id, "None")

            final_txhash = receipt["transactionHash"]
            confirmations = _get_confirmations(w3=self.w3, tx=pending_tx)
            self.log.info(
                f"[finalized] Transaction #atx-{pending_tx.id} has been finalized "
                f"with {confirmations} confirmation(s) txhash: {final_txhash.hex()}"
            )
            self._tx_tracker.finalize_active_tx(receipt=receipt)
            return

        # Outcome 4: re-strategize the pending transaction
        self.__strategize()

    #
    # Broadcast tx
    #

    def __get_signer(self, address: str) -> LocalAccount:
        try:
            signer = self.signers[address]
        except KeyError:
            raise ValueError(f"Signer {address} not found")
        return signer

    def __fire(self, tx: AsyncTx, msg: str) -> TxHash:
        """
        Signs and broadcasts a transaction, handling RPC errors
        and internal state changes.

        On success, returns the `PendingTx` object.
        On failure, returns None.

        Morphs a `FutureTx` into a `PendingTx` and advances it
        into the active transaction slot if broadcast is successful.
        """
        signer: LocalAccount = self.__get_signer(tx.params["from"])
        try:
            txhash = self.w3.eth.send_raw_transaction(
                signer.sign_transaction(tx.params).rawTransaction
            )
        except ValidationError as e:
            # special case for insufficient funds
            if "Sender does not have enough" in str(e):
                # TODO raised exception should be handled in some way #13.
                raise InsufficientFunds

            raise e

        self.log.info(
            f"[{msg}] fired transaction #atx-{tx.id}|{tx.params['nonce']}|{txhash.hex()}"
        )
        return txhash

    def __strategize(self) -> None:
        """Retry the currently tracked pending transaction with the configured strategy."""
        if not self._tx_tracker.pending:
            raise RuntimeError("No active transaction to strategize")

        _active_copy = deepcopy(self._tx_tracker.pending)
        params_updated = False
        for strategy in self._strategies:
            try:
                params = strategy.execute(pending=_active_copy)
            except TransactionFaulted as e:
                self._tx_tracker.fault(fault_error=e)
                return
            if params:
                # in case the strategy accidentally returns None
                # keep the parameters as they are.
                _active_copy.params.update(params)
                params_updated = True

        if not params_updated:
            # TODO is this a potential forever wait - this is really controlled by strategies
            #  who can no longer do anything. if we limit the wait here then the TimeoutStrategy
            #  becomes useless - something to think about. #14
            log.info(
                f"[wait] strategies made no suggested updates to "
                f"pending tx #{_active_copy.id} - skipping retry round"
            )
            return

        # (!) retry the transaction with the new parameters
        _names = " -> ".join(s.name for s in self._strategies)

        try:
            txhash = self.__fire(tx=_active_copy, msg=_names)
        except InsufficientFunds:
            # special case re-raise insufficient funds (for now)
            # TODO #13
            raise
        except (ValidationError, Web3Exception, ValueError) as e:
            self.__handle_retry_failure(_active_copy, e)
            return

        # clear failed retries since successful
        self._retry_failure_counter.pop(_active_copy.id, None)

        _active_copy.txhash = txhash
        self._tx_tracker.update_after_retry(_active_copy)

        pending_tx = self._tx_tracker.pending
        self.log.info(f"[retry] transaction #{pending_tx.id} has been re-broadcasted")
        if pending_tx.on_broadcast:
            fire_hook(hook=pending_tx.on_broadcast, tx=pending_tx)

    def __handle_retry_failure(self, pending_tx: PendingTx, e: Exception):
        log.warn(
            f"[retry] transaction #atx-{pending_tx.id}|{pending_tx.params['nonce']} "
            f"failed with updated params - {str(e)}; retry again next round"
        )
        num_failed_retries = self._retry_failure_counter.get(pending_tx.id, 0)
        num_failed_retries += 1
        if num_failed_retries > self._MAX_REDO_ATTEMPTS:
            log.error(
                f"[retry] transaction #atx-{pending_tx.id}|{pending_tx.params['nonce']} "
                f"failed for {num_failed_retries} attempts; tx will no longer be retried"
            )

            # remove entry since no longer needed
            self._retry_failure_counter.pop(pending_tx.id, None)

            fault_error = TransactionFaulted(
                tx=pending_tx,
                fault=Fault.ERROR,
                message=str(e),
            )
            self._tx_tracker.fault(fault_error=fault_error)
        else:
            self._retry_failure_counter[pending_tx.id] = num_failed_retries

    def __broadcast(self):
        """
        Attempts to broadcast the next `FutureTx` in the queue.
        If the broadcast is not successful, it is re-queued.
        """
        future_tx = self._tx_tracker.pop()  # popleft
        future_tx.params = _make_tx_params(future_tx.params)

        # update nonce as necessary
        signer = self.__get_signer(future_tx._from)
        nonce = self.w3.eth.get_transaction_count(signer.address, "latest")
        if nonce > future_tx.params["nonce"]:
            self.log.warn(
                f"[broadcast] nonce {future_tx.params['nonce']} has been front-run "
                f"by another transaction. Updating queued tx "
                f"nonce {future_tx.params['nonce']} -> {nonce}"
            )
        future_tx.params["nonce"] = nonce

        try:
            txhash = self.__fire(tx=future_tx, msg="broadcast")
        except InsufficientFunds:
            # special case re-raise insufficient funds (for now)
            # TODO #13
            raise
        except (ValidationError, Web3Exception, ValueError) as e:
            # either requeue OR fail and move on to subsequent txs
            self.__handle_broadcast_failure(future_tx, e)
            return

        # clear requeue counter since successful
        self._requeue_counter.pop(future_tx.id, None)

        self._tx_tracker.morph(tx=future_tx, txhash=txhash)
        pending_tx = self._tx_tracker.pending
        if pending_tx.on_broadcast:
            fire_hook(hook=pending_tx.on_broadcast, tx=pending_tx)

    def __handle_broadcast_failure(self, future_tx: FutureTx, e: Exception):
        is_broadcast_failure = False
        if _is_recoverable_send_tx_error(e):
            num_requeues = self._requeue_counter.get(future_tx.id, 0)
            if num_requeues >= self._MAX_REDO_ATTEMPTS:
                is_broadcast_failure = True
                log.error(
                    f"[broadcast] transaction #atx-{future_tx.id}|{future_tx.params['nonce']} "
                    f"failed for {num_requeues} attempts; tx will not be requeued"
                )
            else:
                log.warn(
                    f"[broadcast] transaction #atx-{future_tx.id}|{future_tx.params['nonce']} "
                    f"failed - {str(e)}; requeueing tx"
                )
                self._tx_tracker.requeue(future_tx)
                self._requeue_counter[future_tx.id] = num_requeues + 1
        else:
            # non-recoverable
            is_broadcast_failure = True
            log.error(
                f"[broadcast] transaction #atx-{future_tx.id}|{future_tx.params['nonce']} "
                f"has non-recoverable failure - {str(e)}; tx will not be requeued"
            )

        if is_broadcast_failure:
            # remove entry since no longer needed
            self._requeue_counter.pop(future_tx.id, None)
            hook = future_tx.on_broadcast_failure
            if hook:
                fire_hook(hook, future_tx, e)

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
                        f"[monitor] stopped tracking {tx.txhash.hex()} after {confirmations} confirmations"
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
        if not self._pause:
            self._pause = True
            self.log.info("[pause] pause mode requested")
            self._cycle_state()  # force a move to PAUSED state (don't wait for next iteration)

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

        params_copy = copy(params)

        from_param = params_copy.get("from")
        if from_param is None:
            params_copy["from"] = signer.address
        if from_param and from_param != signer.address:
            raise ValueError(
                f"Mismatched 'from' value ({from_param}) and 'signer' account ({signer.address})"
            )

        tx = self._tx_tracker.queue_tx(params=params_copy, *args, **kwargs)
        if not previously_busy_or_paused:
            self._wake()

        return tx
