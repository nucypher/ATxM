import pytest

import pytest_twisted
from twisted.internet import reactor
from twisted.internet.task import deferLater

from atxm.tx import FutureTx, PendingTx


@pytest.fixture()
def rpc_spy(mocker, w3):
    spy = mocker.spy(w3, "eth")
    return spy


@pytest_twisted.inlineCallbacks
def test_no_rpc_calls_when_idle(machine, state_observer, clock, rpc_spy):
    assert machine.current_state == machine._IDLE
    assert not machine.busy
    assert len(machine.queued) == 0

    machine.start(now=True)
    yield clock.advance(machine._task.interval * 3)
    machine.stop()

    # Verify that no RPC calls were made
    assert rpc_spy.call_count == 0

    assert not machine.busy
    assert len(machine.queued) == 0

    assert machine.current_state == machine._IDLE
    assert len(state_observer.transitions) == 0  # remained idle


def test_queue(
    machine,
    state_observer,
    clock,
    rpc_spy,
    account,
    eip1559_transaction,
    mock_wake_sleep,
):
    wake, sleep = mock_wake_sleep

    # The machine is idle
    assert machine.current_state == machine._IDLE
    assert not machine.running
    assert not machine.busy

    # Queue a transaction
    atx = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
    )

    assert isinstance(atx, FutureTx)

    # The machine tries to start (but it's mocked!)
    assert wake.call_count == 1
    assert machine.busy

    # Nothing is pending
    assert machine.pending is None

    # There is one queued transaction
    assert len(machine.queued) == 1
    queued = machine.queued[0]
    assert queued._from == account.address
    assert queued.id == 0

    assert machine.current_state == machine._IDLE
    assert len(state_observer.transitions) == 0  # nothing actually executed


@pytest_twisted.inlineCallbacks
def test_broadcast(
    machine, state_observer, clock, eip1559_transaction, account, mocker
):
    assert machine.current_state == machine._IDLE
    assert not machine.busy

    # Queue a transaction
    hook = mocker.Mock()
    atx = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
        on_broadcast=hook,
        info={"message": "something wonderful is happening..."},
    )

    # There is one queued transaction
    assert len(machine.queued) == 1

    # distort the time-space continuum
    machine.start(now=True)
    while machine.pending is None:
        yield clock.advance(1)
    machine.stop()

    # The transaction is no longer queued
    assert len(machine.queued) == 0

    assert machine.pending == atx
    assert isinstance(machine.pending, PendingTx)

    assert isinstance(atx, PendingTx)
    assert not atx.final
    assert atx.txhash

    # wait for the hook to be called
    yield deferLater(reactor, 0.2, lambda: None)
    assert hook.call_count == 1

    # tx only broadcasted and not finalized, so we are still busy
    assert machine.current_state == machine._BUSY
    assert len(state_observer.transitions) == 1
    assert state_observer.transitions[0] == (machine._IDLE, machine._BUSY)


@pytest_twisted.inlineCallbacks
def test_finalize(
    machine,
    state_observer,
    clock,
    eip1559_transaction,
    account,
    mock_wake_sleep,
    mocker,
):
    assert machine.current_state == machine._IDLE

    # Queue a transaction
    hook = mocker.Mock()
    atx = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
        on_finalized=hook,
    )

    # There is one queued transaction
    assert len(machine.queued) == 1

    # advance time to broadcast the transaction
    machine.start(now=True)
    while machine.pending is None:
        yield clock.advance(1)
    machine.stop()
    assert machine.pending == atx

    # advance time to finalize the transaction
    machine.start(now=True)
    while machine.pending:
        yield clock.advance(1)

    # The transaction is no longer pending
    assert machine.pending is None

    assert machine.current_state == machine._BUSY

    # The transaction is tracked as finalized
    assert len(machine.finalized) == 1

    # async transaction reflects finalized state
    assert atx.final
    assert atx.receipt

    # wait for the hook to be called
    yield deferLater(reactor, 0.2, lambda: None)
    assert hook.call_count == 1

    yield clock.advance(1)

    assert machine.current_state == machine._IDLE

    assert len(state_observer.transitions) == 2
    assert state_observer.transitions[0] == (machine._IDLE, machine._BUSY)
    assert state_observer.transitions[1] == (machine._BUSY, machine._IDLE)

    machine.stop()


@pytest_twisted.inlineCallbacks
def test_follow(
    chain, machine, state_observer, clock, eip1559_transaction, account, mock_wake_sleep
):
    wake, sleep = mock_wake_sleep

    machine.start()
    assert machine.current_state == machine._IDLE

    atx = machine.queue_transaction(
        params=eip1559_transaction,
        signer=account,
    )

    while not machine.finalized:
        yield clock.advance(1)

    assert atx.final is True

    while len(machine.finalized) > 0:
        yield clock.advance(1)
        yield chain.mine(1)

    machine.stop()

    assert len(machine.finalized) == 0
    assert len(machine.queued) == 0
    assert len(machine.faults) == 0
    assert machine.pending is None

    assert not machine.busy

    # wait for the hook to be called
    yield deferLater(reactor, 0.2, lambda: None)
    assert sleep.call_count == 1

    assert machine.current_state == machine._IDLE

    assert len(state_observer.transitions) == 2
    assert state_observer.transitions[0] == (machine._IDLE, machine._BUSY)
    assert state_observer.transitions[1] == (machine._BUSY, machine._IDLE)
