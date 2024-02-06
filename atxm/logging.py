import sys

from twisted.logger import Logger, textFileLogObserver, globalLogPublisher

log = Logger("AutomaticTxMachine")


observer = textFileLogObserver(sys.stdout)

globalLogPublisher.addObserver(observer)
