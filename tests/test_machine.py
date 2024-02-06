import pytest
import pytest_twisted
from eth_account import Account
from twisted.internet.task import Clock

from atxm import AutomaticTxMachine
from atxm.logging import log
from atxm.tx import FutureTx

log.debug("Running tests")


@pytest.fixture
def account(accounts):
    _account = accounts[0]
    return Account.from_key(_account.private_key)


@pytest.fixture
def legacy_transaction(account, w3):
    gas_price = w3.eth.gas_price
    params = {
        'chainId': 1337,
        'nonce': 0,
        'to': account.address,
        'value': 0,
        'gas': 21000,
        'gasPrice': gas_price,
        'data': b'',
    }
    return params


@pytest.fixture
def eip1559_transaction(account, w3):
    base_fee = w3.eth.get_block("latest")["baseFeePerGas"]
    tip = w3.eth.max_priority_fee
    params = {
        'chainId': 1337,
        'nonce': 0,
        'to': account.address,
        'value': 0,
        'gas': 21000,
        'maxPriorityFeePerGas': tip,
        'maxFeePerGas': base_fee + tip,
        'data': b'',
    }
    return params


@pytest.fixture
def w3(networks):
    return networks.provider.web3


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def machine(w3, clock):
    _machine = AutomaticTxMachine(w3=w3)
    _machine._task.clock = clock
    return _machine


@pytest_twisted.inlineCallbacks
def test_machine(account, w3, legacy_transaction, transaction_eip1559, machine, clock):

    assert not machine.busy
    async_txs = machine.queue_transactions(
        params=[
            legacy_transaction,
            transaction_eip1559
        ],
        signer=account,
        info={"message": f"something wonderful is happening..."},
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
