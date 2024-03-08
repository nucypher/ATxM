import time

import pytest
import pytest_twisted
from twisted.internet import reactor
from twisted.internet.task import deferLater

from atxm.exceptions import Fault, TransactionFaulted
from atxm.tracker import _TxTracker
from atxm.tx import FaultedTx, FinalizedTx, FutureTx, PendingTx, TxHash


def test_queue(eip1559_transaction, legacy_transaction, mocker):
    tx_tracker = _TxTracker(disk_cache=False)
    assert len(tx_tracker.queue) == 0
    assert tx_tracker.pending is None
    assert len(tx_tracker.finalized) == 0

    tx = tx_tracker.queue_tx(params=eip1559_transaction)
    assert len(tx_tracker.queue) == 1
    assert isinstance(tx, FutureTx)
    assert tx_tracker.queue[0] == tx
    assert tx_tracker.queue[0].params == eip1559_transaction
    assert tx.params == eip1559_transaction
    assert tx.info is None
    assert tx.final is False
    assert tx.fault is None
    assert tx.id == 0

    assert tx_tracker.pending is None
    assert len(tx_tracker.finalized) == 0

    tx_2_info = {"description": "it's me!", "message": "me who?"}
    tx_2 = tx_tracker.queue_tx(params=legacy_transaction, info=tx_2_info)
    assert len(tx_tracker.queue) == 2
    assert isinstance(tx_2, FutureTx)
    assert tx_tracker.queue[1] == tx_2
    assert tx_tracker.queue[1].params == legacy_transaction
    assert tx_2.params == legacy_transaction
    assert tx_2.info == tx_2_info
    assert tx_2.final is False
    assert tx_2.fault is None
    assert tx_2.id == 1

    assert tx_tracker.pending is None
    assert len(tx_tracker.finalized) == 0

    # check hooks
    assert tx.on_broadcast is None
    assert tx.on_broadcast_failure is None
    assert tx.on_fault is None
    assert tx.on_finalized is None

    broadcast_hook = mocker.Mock()
    broadcast_failure_hook = mocker.Mock()
    fault_hook = mocker.Mock()
    finalized_hook = mocker.Mock()
    tx_3 = tx_tracker.queue_tx(
        params=eip1559_transaction,
        on_broadcast=broadcast_hook,
        on_broadcast_failure=broadcast_failure_hook,
        on_fault=fault_hook,
        on_finalized=finalized_hook,
    )
    assert tx_3.params == eip1559_transaction
    assert tx_3.info is None
    assert tx_3.final is False
    assert tx_3.fault is None
    assert tx_3.id == 2
    assert tx_3.on_broadcast == broadcast_hook
    assert tx_3.on_broadcast_failure == broadcast_failure_hook
    assert tx_3.on_fault == fault_hook
    assert tx_3.on_finalized == finalized_hook

    assert len(tx_tracker.queue) == 3
    assert isinstance(tx_3, FutureTx)
    assert tx_tracker.queue[2] == tx_3
    assert tx_tracker.queue[2].params == eip1559_transaction
    assert tx_tracker.pending is None
    assert len(tx_tracker.finalized) == 0


def test_pop(eip1559_transaction, legacy_transaction):
    tx_tracker = _TxTracker(disk_cache=False)
    tx_1 = tx_tracker.queue_tx(params=eip1559_transaction)
    tx_2 = tx_tracker.queue_tx(params=legacy_transaction)
    tx_3 = tx_tracker.queue_tx(params=eip1559_transaction)

    assert len(tx_tracker.queue) == 3

    for tx in [tx_1, tx_2, tx_3]:
        popped_tx = tx_tracker.pop()
        assert popped_tx is tx

    with pytest.raises(IndexError):
        tx_tracker.pop()


def test_requeue(eip1559_transaction, legacy_transaction):
    tx_tracker = _TxTracker(disk_cache=False)
    tx_1 = tx_tracker.queue_tx(params=eip1559_transaction)
    assert tx_1.requeues == 0
    tx_2 = tx_tracker.queue_tx(params=legacy_transaction)
    assert tx_2.requeues == 0
    tx_3 = tx_tracker.queue_tx(params=eip1559_transaction)
    assert tx_3.requeues == 0

    assert len(tx_tracker.queue) == 3

    base_num_requeues = 4
    for i in range(1, base_num_requeues + 1):
        prior_pop = None
        for _ in tx_tracker.queue:
            popped_tx = tx_tracker.pop()
            assert popped_tx is not prior_pop, "requeue was an append, not a prepend"

            tx_tracker.requeue(popped_tx)
            prior_pop = popped_tx
            assert popped_tx.requeues == i

            assert len(tx_tracker.queue) == 3, "remains the same length"

    assert tx_1.requeues == base_num_requeues
    assert tx_2.requeues == base_num_requeues
    assert tx_3.requeues == base_num_requeues

    _ = tx_tracker.pop()  # remove tx_1
    _ = tx_tracker.pop()  # remove tx_2

    tx_tracker.requeue(tx_2)
    assert tx_2.requeues == base_num_requeues + 1
    assert tx_1.requeues == base_num_requeues
    assert tx_3.requeues == base_num_requeues


def test_morph(eip1559_transaction, legacy_transaction, mocker):
    tx_tracker = _TxTracker(disk_cache=False)
    broadcast_hook = mocker.Mock()
    broadcast_failure_hook = mocker.Mock()
    fault_hook = mocker.Mock()
    finalized_hook = mocker.Mock()
    tx_1 = tx_tracker.queue_tx(
        params=eip1559_transaction,
        on_broadcast=broadcast_hook,
        on_broadcast_failure=broadcast_failure_hook,
        on_fault=fault_hook,
        on_finalized=finalized_hook,
    )
    tx_2 = tx_tracker.queue_tx(params=legacy_transaction)
    assert tx_1.id != tx_2.id

    tx_hash = TxHash("0xdeadbeef")
    assert tx_tracker.pop() == tx_1
    pending_tx = tx_tracker.morph(tx_1, tx_hash)

    assert isinstance(pending_tx, PendingTx)
    assert pending_tx is tx_1, "same underlying object"
    assert pending_tx.params == eip1559_transaction
    assert pending_tx.txhash == tx_hash
    assert pending_tx.created >= int(time.time())
    assert pending_tx.retries == 0
    assert pending_tx.final is False
    assert pending_tx.fault is None
    assert tx_tracker.pending.params == pending_tx.params
    assert tx_1.on_broadcast == broadcast_hook
    assert tx_1.on_broadcast_failure == broadcast_failure_hook
    assert tx_1.on_fault == fault_hook
    assert tx_1.on_finalized == finalized_hook

    assert isinstance(tx_2, FutureTx), "unaffected by the morph"
    assert tx_tracker.pending is not tx_2

    tx_2_hash = TxHash("0xdeadbeef2")
    assert tx_tracker.pop() == tx_2
    pending_tx_2 = tx_tracker.morph(tx_2, tx_2_hash)
    assert isinstance(pending_tx_2, PendingTx)
    assert pending_tx_2 is tx_2, "same underlying object"
    assert tx_tracker.pending.params == pending_tx_2.params
    assert tx_tracker.pending.params != pending_tx.params
    assert tx_tracker.pending.txhash == pending_tx_2.txhash

    assert (
        tx_tracker.pending is not tx_tracker.pending
    ), "copy of object always returned"


@pytest_twisted.inlineCallbacks
def test_fault(eip1559_transaction, legacy_transaction, mocker):
    tx_tracker = _TxTracker(disk_cache=False)
    broadcast_hook = mocker.Mock()
    broadcast_failure_hook = mocker.Mock()
    fault_hook = mocker.Mock()
    finalized_hook = mocker.Mock()
    tx = tx_tracker.queue_tx(
        params=eip1559_transaction,
        on_broadcast=broadcast_hook,
        on_broadcast_failure=broadcast_failure_hook,
        on_fault=fault_hook,
        on_finalized=finalized_hook,
    )
    tx_2 = tx_tracker.queue_tx(params=legacy_transaction)

    assert len(tx_tracker.queue) == 2

    assert tx_tracker.pending is None
    with pytest.raises(RuntimeError, match="No active transaction"):
        # there is no active tx
        fault_error = TransactionFaulted(
            tx=tx_2, fault=Fault.ERROR, message="not active"
        )
        tx_tracker.fault(fault_error)

    tx_hash = TxHash("0xdeadbeef")
    assert tx_tracker.pop() == tx
    pending_tx = tx_tracker.morph(tx, tx_hash)
    assert tx_tracker.pending.params == tx.params

    fault_message = "Don't find fault, find a remedy"  # - Henry Ford

    assert tx_tracker.pending is not tx_2
    with pytest.raises(RuntimeError, match="Mismatch between active tx"):
        # tx_2 is not the active tx
        fault_error = TransactionFaulted(
            tx=tx_2, fault=Fault.ERROR, message=fault_message
        )
        tx_tracker.fault(fault_error)

    fault_error = TransactionFaulted(
        tx=pending_tx, fault=Fault.ERROR, message=fault_message
    )
    assert fault_hook.call_count == 0

    tx_tracker.fault(fault_error)
    assert isinstance(tx, FaultedTx)
    assert tx.fault == Fault.ERROR
    assert tx.error == fault_message

    # check that fault hook was called
    yield deferLater(reactor, 0.2, lambda: None)
    assert fault_hook.call_count == 1
    fault_hook.assert_called_with(tx)

    # check that other hooks were not called
    assert broadcast_hook.call_count == 0
    assert broadcast_failure_hook.call_count == 0
    assert finalized_hook.call_count == 0

    # no active tx
    assert tx_tracker.pending is None

    # serialization/deserialization of FaultedTx - not tracked so not done anywhere else
    deserialized_fault_tx = FaultedTx.from_dict(tx.to_dict())
    assert deserialized_fault_tx == tx
    assert hash(deserialized_fault_tx) == hash(tx)

    # repeat with no hook
    tx_hash_2 = TxHash("0xdeadbeef2")
    assert tx_tracker.pop() == tx_2
    pending_tx_2 = tx_tracker.morph(tx_2, tx_hash_2)
    assert tx_tracker.pending.params == tx_2.params

    assert pending_tx_2 is tx_2, "same underlying object"
    assert tx_tracker.pending.params == tx_2.params

    fault_error = TransactionFaulted(
        tx=pending_tx_2, fault=Fault.REVERT, message=fault_message
    )
    tx_tracker.fault(fault_error)
    assert isinstance(tx_2, FaultedTx)
    assert tx_2.fault == Fault.REVERT
    assert tx_2.error == fault_message

    # no active tx
    assert tx_tracker.pending is None

    # serialization/deserialization of FaultedTx - not tracked so not done anywhere else
    deserialized_fault_tx_2 = FaultedTx.from_dict(tx_2.to_dict())
    assert deserialized_fault_tx_2 == tx_2
    assert hash(deserialized_fault_tx_2) == hash(tx_2)

    assert tx_2 != tx
    assert hash(tx_2) != hash(tx)


def test_update_after_retry(eip1559_transaction, legacy_transaction, mocker):
    tx_tracker = _TxTracker(disk_cache=False)
    tx = tx_tracker.queue_tx(params=eip1559_transaction)

    assert tx_tracker.pending is None
    with pytest.raises(RuntimeError, match="No active transaction"):
        # there is no active tx
        tx_tracker.update_after_retry(mocker.Mock(spec=PendingTx))

    tx_hash = TxHash("0xdeadbeef")

    assert tx_tracker.pop() == tx
    tx_tracker.morph(tx, tx_hash)
    assert isinstance(tx, PendingTx)
    assert tx_tracker.pending.params == tx.params

    with pytest.raises(RuntimeError, match="Mismatch between active tx"):
        mocked_tx = mocker.Mock(spec=PendingTx)
        mocked_tx.id = 20
        tx_tracker.update_after_retry(mocked_tx)

    # first update
    new_params = legacy_transaction
    new_tx_hash = TxHash("0xdeadbeef2")
    assert tx.params != new_params
    pending_tx = tx_tracker.pending  # obtain fresh copy
    pending_tx.params = new_params
    pending_tx.txhash = new_tx_hash
    tx_tracker.update_after_retry(pending_tx)
    assert tx.params == new_params
    assert tx.txhash == new_tx_hash

    # update again
    new_params = eip1559_transaction
    new_tx_hash = TxHash("0xdeadbeef3")
    assert tx.params != new_params
    pending_tx = tx_tracker.pending  # obtain fresh copy
    pending_tx.params = new_params
    pending_tx.txhash = new_tx_hash
    tx_tracker.update_after_retry(pending_tx)
    assert tx.params == new_params
    assert tx.txhash == new_tx_hash


def test_update_failed_retry_attempt(eip1559_transaction, legacy_transaction, mocker):
    tx_tracker = _TxTracker(disk_cache=False)
    tx = tx_tracker.queue_tx(params=eip1559_transaction)

    assert tx_tracker.pending is None
    with pytest.raises(RuntimeError, match="No active transaction"):
        # there is no active tx
        tx_tracker.update_failed_retry_attempt(mocker.Mock(spec=PendingTx))

    tx_hash = TxHash("0xdeadbeef")
    assert tx_tracker.pop() == tx
    tx_tracker.morph(tx, tx_hash)
    assert isinstance(tx, PendingTx)
    pending_tx = tx_tracker.pending
    assert pending_tx is not None
    assert pending_tx.params == tx.params

    with pytest.raises(RuntimeError, match="Mismatch between active tx"):
        mocked_tx = mocker.Mock(spec=PendingTx)
        mocked_tx.id = 20
        tx_tracker.update_failed_retry_attempt(mocked_tx)

    assert tx.retries == 0

    for i in range(1, 5):
        tx_tracker.update_failed_retry_attempt(tx_tracker.pending)
        assert tx.retries == i
        assert tx_tracker.pending.retries == i


@pytest_twisted.inlineCallbacks
def test_finalize_active_tx(eip1559_transaction, mocker, tx_receipt):
    tx_tracker = _TxTracker(disk_cache=False)

    with pytest.raises(RuntimeError, match="No pending transaction to finalize"):
        # there is no active tx
        tx_tracker.finalize_active_tx(mocker.Mock())

    broadcast_hook = mocker.Mock()
    broadcast_failure_hook = mocker.Mock()
    fault_hook = mocker.Mock()
    finalized_hook = mocker.Mock()
    tx = tx_tracker.queue_tx(
        params=eip1559_transaction,
        on_broadcast=broadcast_hook,
        on_broadcast_failure=broadcast_failure_hook,
        on_fault=fault_hook,
        on_finalized=finalized_hook,
    )

    tx_hash = TxHash("0xdeadbeef")
    assert tx_tracker.pop() == tx
    tx_tracker.morph(tx, tx_hash)
    assert isinstance(tx, PendingTx)
    pending_tx = tx_tracker.pending
    assert pending_tx is not None
    assert pending_tx.params == tx.params

    assert len(tx_tracker.finalized) == 0

    tx_tracker.finalize_active_tx(tx_receipt)

    assert isinstance(tx, FinalizedTx)
    assert tx.final is True
    assert tx.receipt == tx_receipt

    # check hook called
    yield deferLater(reactor, 0.2, lambda: None)
    assert finalized_hook.call_count == 1
    finalized_hook.assert_called_with(tx)

    # other hooks not called
    assert broadcast_hook.call_count == 0
    assert broadcast_failure_hook.call_count == 0
    assert fault_hook.call_count == 0

    # active tx cleared
    assert tx_tracker.pending is None

    assert len(tx_tracker.finalized) == 1
    assert tx in tx_tracker.finalized


def test_commit_restore(
    eip1559_transaction, legacy_transaction, tx_receipt, tempfile_path
):
    tx_tracker = _TxTracker(disk_cache=True, filepath=tempfile_path)

    # check commit restore
    tx_tracker.commit()
    restored_tracker = _TxTracker(disk_cache=True, filepath=tempfile_path)
    _compare_trackers(tx_tracker, restored_tracker)

    tx_1 = tx_tracker.queue_tx(params=eip1559_transaction, info={"name": "tx_1"})
    tx_2 = tx_tracker.queue_tx(params=legacy_transaction)
    tx_3 = tx_tracker.queue_tx(params=eip1559_transaction, info={"name": "tx_3"})
    tx_4 = tx_tracker.queue_tx(params=legacy_transaction)
    tx_5 = tx_tracker.queue_tx(params=eip1559_transaction, info={"name": "tx_5"})
    tx_6 = tx_tracker.queue_tx(params=legacy_transaction)

    # max tx_1 finalized
    tx_hash = TxHash("0xdeadbeef")
    assert tx_tracker.pop() == tx_1
    tx_tracker.morph(tx_1, tx_hash)

    tx_tracker.finalize_active_tx(tx_receipt)
    assert len(tx_tracker.finalized) == 1

    # check commit restore
    tx_tracker.commit()
    restored_tracker = _TxTracker(disk_cache=True, filepath=tempfile_path)
    _compare_trackers(tx_tracker, restored_tracker)

    # make tx_2 finalized
    tx_hash_2 = TxHash("0xdeadbeef2")
    assert tx_tracker.pop() == tx_2
    tx_tracker.morph(tx_2, tx_hash_2)

    tx_tracker.finalize_active_tx(tx_receipt)
    assert len(tx_tracker.finalized) == 2
    assert tx_1 in tx_tracker.finalized
    assert tx_2 in tx_tracker.finalized

    # check commit restore
    tx_tracker.commit()
    restored_tracker = _TxTracker(disk_cache=True, filepath=tempfile_path)
    _compare_trackers(tx_tracker, restored_tracker)

    # make tx_3 active
    tx_hash_3 = TxHash("0xdeadbeef3")
    assert tx_tracker.pop() == tx_3
    tx_tracker.morph(tx_3, tx_hash_3)
    assert tx_tracker.pending == tx_3

    # check commit restore
    tx_tracker.commit()
    restored_tracker = _TxTracker(disk_cache=True, filepath=tempfile_path)
    _compare_trackers(tx_tracker, restored_tracker)

    # tx_4,5,6 still queued
    assert len(tx_tracker.queue) == 3
    assert tx_4 in tx_tracker.queue
    assert tx_5 in tx_tracker.queue
    assert tx_6 in tx_tracker.queue

    # check commit restore
    tx_tracker.commit()
    restored_tracker = _TxTracker(disk_cache=True, filepath=tempfile_path)
    _compare_trackers(tx_tracker, restored_tracker)


def _compare_trackers(tracker_1: _TxTracker, tracker_2: _TxTracker):
    # 1. check FutureTxs
    assert len(tracker_1.queue) == len(tracker_2.queue)
    for i, tx in enumerate(list(tracker_1.queue)):
        assert tx == tracker_2.queue[i]
        # ensure __hash__ is tested
        assert hash(tx) == hash(tracker_2.queue[i])

    # 2. check PendingTx
    assert tracker_1.pending == tracker_2.pending
    if tracker_1.pending:
        # ensure __hash__ is tested
        assert hash(tracker_2.pending) == hash(tracker_1.pending)

    # 3. check FinalizedTxs
    assert len(tracker_1.finalized) == len(tracker_2.finalized)
    # finalized already in a set
    for tx in tracker_1.finalized:
        assert tx in tracker_2.finalized
