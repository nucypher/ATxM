import pytest_twisted

from atxm.tx import FutureTx


@pytest_twisted.inlineCallbacks
def test_machine(account, w3, legacy_transaction, eip1559_transaction, machine, clock):
    assert not machine.busy
    async_txs = machine.queue_transactions(
        params=[legacy_transaction, eip1559_transaction],
        signer=account,
        info={"message": "something wonderful is happening..."},
    )

    assert len(async_txs) == 2
    for i, atx in enumerate(async_txs):
        assert isinstance(atx, FutureTx)
        assert atx._from == account.address
        assert atx.id == i

    assert machine.busy
    assert len(machine.queued) == 2
    assert machine.pending is None
    assert len(machine.finalized) == 0

    assert not machine._task.running
    machine.start(now=True)
    assert machine._task.running

    while not all(atx.final for atx in async_txs):
        yield clock.advance(machine._task.interval)

    assert not machine.busy
    assert machine._task.running
    machine.stop()
    assert not machine._task.running

    assert not machine.busy
    assert len(machine.queued) == 0
    assert machine.pending is None
    assert len(machine.finalized) == 2
