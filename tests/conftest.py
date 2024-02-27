import sys
from typing import List, Tuple

import pytest
from eth_account import Account
from statemachine import State
from twisted.internet.task import Clock
from twisted.logger import globalLogPublisher, textFileLogObserver

from atxm import AutomaticTxMachine
from atxm.logging import log

observer = textFileLogObserver(sys.stdout)
globalLogPublisher.addObserver(observer)

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


@pytest.fixture
def interval(machine):
    return 1


@pytest.fixture(autouse=True)
def mock_wake_sleep(machine, mocker):
    wake = mocker.patch.object(machine, "_wake")
    sleep = mocker.patch.object(machine, "_sleep")
    return wake, sleep


class StateObserver:
    def __init__(self):
        self.transitions: List[Tuple[State, State]] = []

    def on_transition(self, source, target):
        if source.id != target.id:
            self.transitions.append((source, target))


@pytest.fixture
def state_observer(machine):
    _observer = StateObserver()
    machine.add_observer(_observer)

    return _observer
