import pytest
from eth_account import Account
from twisted.internet.task import Clock

from atxm import AutomaticTxMachine
from atxm.logging import log

log.debug("Running tests")


@pytest.fixture
def account(accounts):
    _account = accounts[0]
    return Account.from_key(_account.private_key)


@pytest.fixture
def legacy_transaction(account, w3):
    gas_price = w3.eth.gas_price
    params = {
        "chainId": 1337,
        "nonce": 0,
        "to": account.address,
        "value": 0,
        "gas": 21000,
        "gasPrice": gas_price,
        "data": b"",
    }
    return params


@pytest.fixture
def eip1559_transaction(account, w3):
    base_fee = w3.eth.get_block("latest")["baseFeePerGas"]
    tip = w3.eth.max_priority_fee
    params = {
        "chainId": 1337,
        "nonce": 0,
        "to": account.address,
        "value": 0,
        "gas": 21000,
        "maxPriorityFeePerGas": tip,
        "maxFeePerGas": base_fee + tip,
        "data": b"",
    }
    return params


@pytest.fixture
def w3(networks):
    return networks.provider.web3


@pytest.fixture
def machine(w3):
    clock = Clock()
    _machine = AutomaticTxMachine(w3=w3)
    _machine._task.clock = clock
    return _machine


@pytest.fixture
def clock(machine):
    return machine._task.clock


@pytest.fixture(autouse=True)
def mock_wake_sleep(machine, mocker):
    wake = mocker.patch.object(machine, "_wake")
    sleep = mocker.patch.object(machine, "_sleep")
    return wake, sleep
